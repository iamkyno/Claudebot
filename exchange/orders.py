import logging
import threading
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from data.db import get_session

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, exchange_client, paper_mode: bool = False,
                 paper_balance: float = 10_000.0, price_stream=None,
                 fees_cfg: dict | None = None):
        self.client = exchange_client
        self.paper_mode = paper_mode
        self.price_stream = price_stream
        fees_cfg = fees_cfg or {}
        # Cost model: fees + slippage are applied to every paper fill and every
        # PnL number so results are net, not gross-optimistic.
        self.spot_fee = fees_cfg.get("spot_taker", 0.001)
        self.spot_maker = fees_cfg.get("spot_maker", 0.001)
        self.futures_fee = fees_cfg.get("futures_taker", 0.0005)
        self.futures_maker = fees_cfg.get("futures_maker", 0.0002)
        self.slippage = fees_cfg.get("slippage_bps", 2) / 10_000.0
        self._paper_counter = 1
        # Virtual wallet for paper trading so the bot actually trades with no keys.
        self._paper_cash = float(paper_balance)
        self._paper_start = float(paper_balance)
        # trade_id -> {symbol, qty, cost, side, entry, entry_fee}
        self._paper_positions: dict[int, dict] = {}
        # The swing tick and the sniper scalp loop trade concurrently — every
        # wallet mutation goes through this lock so cash math can't interleave.
        self._lock = threading.RLock()

    # -- cost model ------------------------------------------------------ #

    def fee_rate(self, side: str, venue: str = None, liquidity: str = "taker") -> float:
        """Futures venue (all shorts + strategies that request it, e.g. the
        scalper) pays futures rates; everything else spot. Post-only entries
        pay maker (0.02% futures) instead of taker (0.05%)."""
        futures = venue == "futures" or side == "sell"
        if liquidity == "maker":
            return self.futures_maker if futures else self.spot_maker
        return self.futures_fee if futures else self.spot_fee

    def _slip(self, price: float, order_side: str) -> float:
        """Market orders cross the spread: buys fill high, sells fill low."""
        if order_side == "buy":
            return price * (1 + self.slippage)
        return price * (1 - self.slippage)

    # -- pricing -------------------------------------------------------- #

    def _current_price(self, symbol: str) -> float:
        """Millisecond-fresh price from the WS stream if live, else REST."""
        if self.price_stream is not None:
            p = self.price_stream.get_price(symbol)
            if p:
                return float(p)
        return float(self.client.fetch_ticker(symbol)["last"])

    # -- paper wallet --------------------------------------------------- #

    def reconcile_paper_wallet(self):
        """Rebuild the in-memory paper wallet from open trades in the DB."""
        if not self.paper_mode:
            return
        session = get_session()
        try:
            rows = session.execute(text(
                "SELECT id, symbol, side, entry_price, quantity, COALESCE(fees, 0) "
                "FROM trades WHERE status='open'"
            )).fetchall()
            # Realized (net) PnL from closed trades persists across restarts.
            realized = float(session.execute(text(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status='closed'"
            )).scalar() or 0.0)
        finally:
            session.close()

        self._paper_positions = {}
        deployed = 0.0
        for r in rows:
            cost = float(r[3]) * float(r[4])
            entry_fee = float(r[5])
            self._paper_positions[int(r[0])] = {
                "symbol": r[1], "side": r[2], "entry": float(r[3]),
                "qty": float(r[4]), "cost": cost, "entry_fee": entry_fee,
            }
            deployed += cost + entry_fee   # entry fee already left the wallet
        self._paper_cash = max(self._paper_start + realized - deployed, 0.0)
        if rows:
            self._paper_counter = max(self._paper_counter, max(int(r[0]) for r in rows) + 1)
            logger.info(
                f"[PAPER] Reconciled {len(rows)} open position(s): "
                f"${deployed:,.2f} deployed, ${realized:+,.2f} realized, "
                f"${self._paper_cash:,.2f} cash free"
            )

    def reset_paper_wallet(self):
        """Fresh start: full starting balance, no positions. Paper mode only —
        callers are expected to have wiped/closed the DB trades first."""
        if not self.paper_mode:
            return
        with self._lock:
            self._paper_cash = self._paper_start
            self._paper_positions = {}
        logger.info(f"[PAPER] Wallet reset to ${self._paper_start:,.2f}")

    def free_cash(self) -> float:
        with self._lock:
            return self._paper_cash

    def equity(self) -> float:
        """Cash + mark-to-market value of open positions (side-aware)."""
        with self._lock:
            cash = self._paper_cash
            positions = list(self._paper_positions.values())
        position_value = 0.0
        for pos in positions:
            try:
                price = self._current_price(pos["symbol"])
            except Exception:
                position_value += pos["cost"]
                continue
            direction = 1 if pos["side"] == "buy" else -1
            unreal = (price - pos["entry"]) * pos["qty"] * direction
            position_value += pos["cost"] + unreal
        return cash + position_value

    # -- opening -------------------------------------------------------- #

    def open_position(
        self, symbol: str, side: str, usdt_amount: float, strategy: str,
        stop_loss: float, take_profit: float,
        ml_confidence: float = None, signal_id: int = None,
        venue: str = None, order_type: str = "taker",
        maker_timeout: float = 10.0,
    ) -> Optional[dict]:
        """Open a long (side='buy') or short (side='sell') position.
        venue='futures' routes to futures and pays futures fees — the scalper
        uses this: its edge math assumes futures costs (spot fees exceed its
        typical target and would guarantee a net loss even on TP hits).
        order_type='maker' rests a post-only limit at the current price:
        cheaper fee, no spread crossed — live entries that don't fill within
        maker_timeout are cancelled and the trade is skipped."""
        try:
            price = self._current_price(symbol)
            quantity = usdt_amount / price
            use_futures = venue == "futures" or side == "sell"
            rate = self.fee_rate(side, venue, liquidity=order_type)

            min_qty = self.client.get_min_order_amount(
                symbol, venue="futures" if use_futures else "spot"
            )
            if quantity < min_qty:
                logger.warning(f"Order qty {quantity:.6f} below minimum {min_qty} for {symbol}")
                return None

            if self.paper_mode:
                if order_type == "maker":
                    # A resting order doesn't cross the spread: fill at the
                    # quoted price, maker fee. (Slightly optimistic — a real
                    # resting bid can miss fast moves; live mode handles that
                    # by cancelling unfilled entries.)
                    fill_price = price
                else:
                    # Market order: buys fill high, sells fill low.
                    fill_price = self._slip(price, side)
                order = self._paper_order(symbol, side, quantity, fill_price)
            elif order_type == "maker":
                o = self.client.place_post_only(
                    symbol, side, quantity, price,
                    venue="futures" if use_futures else "spot",
                )
                filled = self.client.wait_fill(
                    o["id"], symbol,
                    venue="futures" if use_futures else "spot",
                    timeout=maker_timeout,
                )
                if not filled:
                    logger.info(f"Maker entry unfilled for {symbol} within "
                                f"{maker_timeout:.0f}s — skipped")
                    return None
                order = filled
                fill_price = float(filled.get("average") or filled.get("price") or price)
            else:
                order = self.client.open_futures_position(symbol, side, quantity) \
                    if use_futures else \
                    self.client.create_market_order(symbol, side, quantity)
                fill_price = float(order.get("average") or order.get("price") or price)

            entry_fee = fill_price * quantity * rate
            trade_id = self._log_trade(
                symbol, strategy, side, fill_price, quantity,
                stop_loss, take_profit, ml_confidence, signal_id, entry_fee,
            )

            if self.paper_mode and trade_id:
                cost = quantity * fill_price
                with self._lock:
                    self._paper_cash -= cost + entry_fee
                    self._paper_positions[trade_id] = {
                        "symbol": symbol, "side": side, "entry": fill_price,
                        "qty": quantity, "cost": cost, "entry_fee": entry_fee,
                    }

            # LIVE: park a protective stop on the exchange itself so the
            # position survives bot downtime. Paper stops stay bot-managed.
            if not self.paper_mode and trade_id and stop_loss:
                po = self.client.place_protective_stop(
                    symbol, side, quantity, stop_loss,
                    venue="futures" if use_futures else "spot",
                )
                if po and po.get("id"):
                    self._set_protective(trade_id, str(po["id"]))
                else:
                    logger.warning(f"{symbol} trade #{trade_id} is UNPROTECTED "
                                   f"on-exchange — bot-managed stop only")

            prefix = "[PAPER] " if self.paper_mode else ""
            tag = "LONG" if side == "buy" else "SHORT"
            logger.info(
                f"{prefix}{tag} {quantity:.6f} {symbol} @ {fill_price:.4f} "
                f"| {strategy} | fee ${entry_fee:.4f}"
            )
            return {"order": order, "trade_id": trade_id, "price": fill_price,
                    "quantity": quantity, "side": side}
        except Exception as e:
            logger.error(f"Open {side} failed for {symbol}: {e}")
            return None

    # Backwards-compatible long open.
    def place_market_buy(self, symbol, usdt_amount, strategy, stop_loss,
                         take_profit, ml_confidence=None, signal_id=None):
        return self.open_position(symbol, "buy", usdt_amount, strategy,
                                  stop_loss, take_profit, ml_confidence, signal_id)

    # -- closing -------------------------------------------------------- #

    def close_position(self, symbol: str, quantity: float, trade_id: int,
                       side: str = "buy", fraction: float = 1.0,
                       venue: str = None) -> Optional[dict]:
        """
        Close (fraction<1 = partially close) a position. `side` is the side of
        the OPEN position: a long is closed with a sell, a short with a buy.
        Pass the same venue used at open so fees and routing match.
        """
        try:
            price = self._current_price(symbol)
            close_side = "sell" if side == "buy" else "buy"
            close_qty = quantity * fraction
            use_futures = venue == "futures" or side == "sell"
            rate = self.fee_rate(side, venue)

            # LIVE: lift the parked exchange stop before closing, otherwise it
            # can double-fire on the flat position. (Re-parked below on trims.)
            if not self.paper_mode and trade_id:
                old = self._get_protective(trade_id)
                if old:
                    self.client.cancel_protective_stop(
                        symbol, old, venue="futures" if use_futures else "spot")
                    self._set_protective(trade_id, None)

            if self.paper_mode:
                fill_price = self._slip(price, close_side)
                order = self._paper_order(symbol, close_side, close_qty, fill_price)
            else:
                order = self.client.close_futures_position(symbol, close_qty, close_side) \
                    if use_futures else \
                    self.client.create_market_order(symbol, close_side, close_qty)
                fill_price = float(order.get("average") or order.get("price") or price)

            if fraction >= 1.0:
                close_ok = self._close_trade(trade_id, fill_price, rate)
            else:
                close_ok = self._partial_close(trade_id, fill_price, close_qty, rate)

            if self.paper_mode and close_ok:
                self._settle_paper(trade_id, fill_price, close_qty, side, fraction, rate)

            # LIVE partial (TP1): re-park a breakeven stop for the remainder.
            if not self.paper_mode and close_ok and fraction < 1.0 and trade_id:
                session = get_session()
                try:
                    row = session.execute(text(
                        "SELECT entry_price, quantity FROM trades WHERE id=:id AND status='open'"
                    ), {"id": trade_id}).fetchone()
                finally:
                    session.close()
                if row:
                    po = self.client.place_protective_stop(
                        symbol, side, float(row[1]), float(row[0]),
                        venue="futures" if use_futures else "spot")
                    if po and po.get("id"):
                        self._set_protective(trade_id, str(po["id"]))

            prefix = "[PAPER] " if self.paper_mode else ""
            tag = "CLOSE" if fraction >= 1.0 else f"TRIM {fraction:.0%}"
            logger.info(f"{prefix}{tag} {close_qty:.6f} {symbol} @ {fill_price:.4f}")
            return {"order": order, "price": fill_price, "quantity": close_qty}
        except Exception as e:
            logger.error(f"Close failed for {symbol}: {e}")
            return None

    # Backwards-compatible full close of a long.
    def place_market_sell(self, symbol, quantity, strategy, trade_id=None):
        return self.close_position(symbol, quantity, trade_id, side="buy", fraction=1.0)

    def _settle_paper(self, trade_id, fill_price, close_qty, side, fraction, rate):
        """
        Wallet identity: cash_out = cost + entry_fee at open;
        cash_in = cost + gross - exit_fee at close, so over a round trip
        cash change == gross - entry_fee - exit_fee == net PnL. Entry fee was
        already paid at open, so it is NOT deducted again here.
        """
        with self._lock:
            pos = self._paper_positions.get(trade_id)
            if not pos:
                return
            entry = pos["entry"]
            direction = 1 if side == "buy" else -1
            gross = (fill_price - entry) * close_qty * direction
            exit_fee = fill_price * close_qty * rate
            released_margin = entry * close_qty
            self._paper_cash += released_margin + gross - exit_fee
            if fraction >= 1.0:
                self._paper_positions.pop(trade_id, None)
            else:
                pos["qty"] -= close_qty
                pos["cost"] -= released_margin
                pos["entry_fee"] *= (1 - fraction)

    # -- protective stop bookkeeping ------------------------------------- #

    def _set_protective(self, trade_id: int, order_id: str | None):
        session = get_session()
        try:
            session.execute(
                text("UPDATE trades SET protective_order_id=:oid WHERE id=:id"),
                {"oid": order_id, "id": trade_id},
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.debug(f"Protective id save failed for {trade_id}: {e}")
        finally:
            session.close()

    def _get_protective(self, trade_id: int) -> str | None:
        session = get_session()
        try:
            row = session.execute(text(
                "SELECT protective_order_id FROM trades WHERE id=:id"
            ), {"id": trade_id}).fetchone()
            return row[0] if row and row[0] else None
        finally:
            session.close()

    # -- trailing stop -------------------------------------------------- #

    def update_stop(self, trade_id: int, new_stop: float, symbol: str = None,
                    side: str = "buy", quantity: float = None, venue: str = None):
        """Move a stop. In live mode the parked exchange stop is replaced too
        (cancel old, park new) so on-exchange protection trails with the bot."""
        session = get_session()
        try:
            session.execute(
                text("UPDATE trades SET stop_loss=:s WHERE id=:id AND status='open'"),
                {"s": new_stop, "id": trade_id},
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.debug(f"Stop update failed for {trade_id}: {e}")
        finally:
            session.close()

        if self.paper_mode or not symbol or not quantity:
            return
        use_futures = venue == "futures" or side == "sell"
        old = self._get_protective(trade_id)
        if old:
            self.client.cancel_protective_stop(
                symbol, old, venue="futures" if use_futures else "spot")
        po = self.client.place_protective_stop(
            symbol, side, quantity, new_stop,
            venue="futures" if use_futures else "spot")
        self._set_protective(trade_id, str(po["id"]) if po and po.get("id") else None)

    def record_high_low(self, trade_id: int, highest: float = None, lowest: float = None):
        """Persist the best price seen so the trailing stop survives restarts."""
        sets, params = [], {"id": trade_id}
        if highest is not None:
            sets.append("highest_price=:h"); params["h"] = highest
        if lowest is not None:
            sets.append("lowest_price=:l"); params["l"] = lowest
        if not sets:
            return
        session = get_session()
        try:
            session.execute(
                text(f"UPDATE trades SET {', '.join(sets)} WHERE id=:id AND status='open'"),
                params,
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.debug(f"High/low update failed for {trade_id}: {e}")
        finally:
            session.close()

    # -- queries -------------------------------------------------------- #

    def get_open_trades(self) -> list:
        session = get_session()
        try:
            return session.execute(text("SELECT * FROM trades WHERE status='open'")).fetchall()
        finally:
            session.close()

    # -- internals ------------------------------------------------------ #

    def _paper_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        oid = f"paper_{self._paper_counter}"
        self._paper_counter += 1
        return {"id": oid, "symbol": symbol, "side": side, "amount": amount,
                "price": price, "status": "closed"}

    def _log_trade(self, symbol, strategy, side, entry_price, quantity,
                   stop_loss, take_profit, ml_confidence, signal_id,
                   entry_fee: float = 0.0) -> Optional[int]:
        session = get_session()
        try:
            result = session.execute(text("""
                INSERT INTO trades
                    (symbol, strategy, side, entry_price, quantity, original_quantity,
                     stop_loss, take_profit, highest_price, lowest_price, fees,
                     ml_confidence, signal_id, entry_time, status)
                VALUES
                    (:symbol, :strategy, :side, :entry_price, :quantity, :quantity,
                     :stop_loss, :take_profit, :entry_price, :entry_price, :fees,
                     :ml_confidence, :signal_id, :entry_time, 'open')
                RETURNING id
            """), {
                "symbol": symbol, "strategy": strategy, "side": side,
                "entry_price": entry_price, "quantity": quantity,
                "stop_loss": stop_loss, "take_profit": take_profit,
                "fees": entry_fee,
                "ml_confidence": ml_confidence, "signal_id": signal_id,
                "entry_time": datetime.utcnow(),
            })
            trade_id = result.scalar()
            session.commit()
            return trade_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to log trade: {e}")
            return None
        finally:
            session.close()

    def _close_trade(self, trade_id: int, exit_price: float, fee_rate: float = 0.0) -> bool:
        session = get_session()
        try:
            trade = session.execute(
                text("SELECT * FROM trades WHERE id=:id"), {"id": trade_id}
            ).fetchone()
            if not trade:
                return False

            entry = float(trade.entry_price)
            qty = float(trade.quantity)
            direction = 1 if trade.side == "buy" else -1
            entry_fee = float(trade.fees or 0.0)
            exit_fee = exit_price * qty * fee_rate
            gross = (exit_price - entry) * qty * direction
            pnl = gross - entry_fee - exit_fee            # NET of all costs
            notional = entry * qty
            pnl_pct = pnl / notional if notional else 0.0  # net %, fee-aware
            exit_time = datetime.utcnow()
            duration = int((exit_time - trade.entry_time).total_seconds() / 60)

            session.execute(text("""
                UPDATE trades
                SET exit_price=:exit_price, pnl=:pnl, pnl_pct=:pnl_pct,
                    fees=:fees, exit_time=:exit_time,
                    duration_minutes=:duration, status='closed'
                WHERE id=:id
            """), {
                "exit_price": exit_price, "pnl": pnl, "pnl_pct": pnl_pct,
                "fees": entry_fee + exit_fee,
                "exit_time": exit_time, "duration": duration, "id": trade_id,
            })

            if trade.signal_id:
                outcome = 1 if pnl > 0 else (-1 if pnl < 0 else 0)
                session.execute(text("""
                    UPDATE signals
                    SET outcome=:outcome, actual_pnl_pct=:pnl_pct, trade_id=:trade_id
                    WHERE id=:signal_id
                """), {"outcome": outcome, "pnl_pct": pnl_pct, "trade_id": trade_id,
                       "signal_id": trade.signal_id})

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to close trade {trade_id}: {e}")
            return False
        finally:
            session.close()

    def _partial_close(self, trade_id: int, exit_price: float, close_qty: float,
                       fee_rate: float = 0.0) -> bool:
        """
        Realize NET PnL on part of a position: book a child closed-trade row
        (with its pro-rata share of the entry fee + its own exit fee) and
        shrink the parent, moving its stop to breakeven (TP1).
        """
        session = get_session()
        try:
            trade = session.execute(
                text("SELECT * FROM trades WHERE id=:id"), {"id": trade_id}
            ).fetchone()
            if not trade or float(trade.quantity) <= close_qty:
                session.close()
                return self._close_trade(trade_id, exit_price, fee_rate)

            entry = float(trade.entry_price)
            qty = float(trade.quantity)
            direction = 1 if trade.side == "buy" else -1
            frac = close_qty / qty
            entry_fee_slice = float(trade.fees or 0.0) * frac
            exit_fee = exit_price * close_qty * fee_rate
            gross = (exit_price - entry) * close_qty * direction
            pnl = gross - entry_fee_slice - exit_fee
            notional = entry * close_qty
            pnl_pct = pnl / notional if notional else 0.0
            now = datetime.utcnow()
            duration = int((now - trade.entry_time).total_seconds() / 60)

            # Child closed row for the realized slice.
            session.execute(text("""
                INSERT INTO trades
                    (symbol, strategy, side, entry_price, exit_price, quantity,
                     pnl, pnl_pct, fees, entry_time, exit_time, duration_minutes,
                     stop_loss, take_profit, status, notes)
                VALUES
                    (:symbol, :strategy, :side, :entry, :exit, :qty,
                     :pnl, :pnl_pct, :fees, :entry_time, :exit_time, :duration,
                     :stop, :tp, 'closed', 'partial TP1')
            """), {
                "symbol": trade.symbol, "strategy": trade.strategy, "side": trade.side,
                "entry": entry, "exit": exit_price, "qty": close_qty,
                "pnl": pnl, "pnl_pct": pnl_pct, "fees": entry_fee_slice + exit_fee,
                "entry_time": trade.entry_time, "exit_time": now, "duration": duration,
                "stop": trade.stop_loss, "tp": trade.take_profit,
            })

            # Shrink the parent (quantity + its remaining entry-fee share),
            # move stop to breakeven, mark TP1 filled.
            session.execute(text("""
                UPDATE trades
                SET quantity = quantity - :cq, fees = fees * :keep,
                    stop_loss = :be, tp1_filled = 1
                WHERE id = :id
            """), {"cq": close_qty, "keep": 1 - frac, "be": entry, "id": trade_id})

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Partial close failed for {trade_id}: {e}")
            return False
        finally:
            session.close()
