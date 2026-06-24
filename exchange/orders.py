import logging
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from data.db import get_session

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, exchange_client, paper_mode: bool = False,
                 paper_balance: float = 10_000.0, price_stream=None):
        self.client = exchange_client
        self.paper_mode = paper_mode
        self.price_stream = price_stream
        self._paper_counter = 1
        # Virtual wallet for paper trading so the bot actually trades with no keys.
        self._paper_cash = float(paper_balance)
        self._paper_start = float(paper_balance)
        # trade_id -> {symbol, qty, cost, side, entry}
        self._paper_positions: dict[int, dict] = {}

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
                "SELECT id, symbol, side, entry_price, quantity FROM trades WHERE status='open'"
            )).fetchall()
            realized = float(session.execute(text(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status='closed'"
            )).scalar() or 0.0)
        finally:
            session.close()

        self._paper_positions = {}
        deployed = 0.0
        for r in rows:
            cost = float(r[3]) * float(r[4])
            self._paper_positions[int(r[0])] = {
                "symbol": r[1], "side": r[2], "entry": float(r[3]),
                "qty": float(r[4]), "cost": cost,
            }
            deployed += cost
        self._paper_cash = max(self._paper_start + realized - deployed, 0.0)
        if rows:
            self._paper_counter = max(self._paper_counter, max(int(r[0]) for r in rows) + 1)
            logger.info(
                f"[PAPER] Reconciled {len(rows)} open position(s): "
                f"${deployed:,.2f} deployed, ${realized:+,.2f} realized, "
                f"${self._paper_cash:,.2f} cash free"
            )

    def free_cash(self) -> float:
        return self._paper_cash

    def equity(self) -> float:
        """Cash + mark-to-market value of open positions (side-aware)."""
        position_value = 0.0
        for pos in self._paper_positions.values():
            try:
                price = self._current_price(pos["symbol"])
            except Exception:
                position_value += pos["cost"]
                continue
            # value = reserved margin (cost) + unrealized PnL
            direction = 1 if pos["side"] == "buy" else -1
            unreal = (price - pos["entry"]) * pos["qty"] * direction
            position_value += pos["cost"] + unreal
        return self._paper_cash + position_value

    # -- opening -------------------------------------------------------- #

    def open_position(
        self, symbol: str, side: str, usdt_amount: float, strategy: str,
        stop_loss: float, take_profit: float,
        ml_confidence: float = None, signal_id: int = None,
    ) -> Optional[dict]:
        """Open a long (side='buy') or short (side='sell') position."""
        try:
            price = self._current_price(symbol)
            quantity = usdt_amount / price

            min_qty = self.client.get_min_order_amount(symbol)
            if quantity < min_qty:
                logger.warning(f"Order qty {quantity:.6f} below minimum {min_qty} for {symbol}")
                return None

            if self.paper_mode:
                order = self._paper_order(symbol, side, quantity, price)
                fill_price = price
            else:
                # Shorts route to the futures venue; longs to spot.
                venue = self.client.futures if side == "sell" else self.client.spot
                order = venue.create_market_order(symbol, side, quantity)
                fill_price = float(order.get("average") or order.get("price") or price)

            trade_id = self._log_trade(
                symbol, strategy, side, fill_price, quantity,
                stop_loss, take_profit, ml_confidence, signal_id,
            )

            if self.paper_mode and trade_id:
                cost = quantity * fill_price
                self._paper_cash -= cost
                self._paper_positions[trade_id] = {
                    "symbol": symbol, "side": side, "entry": fill_price,
                    "qty": quantity, "cost": cost,
                }

            prefix = "[PAPER] " if self.paper_mode else ""
            tag = "LONG" if side == "buy" else "SHORT"
            logger.info(f"{prefix}{tag} {quantity:.6f} {symbol} @ {fill_price:.4f} | {strategy}")
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
                       side: str = "buy", fraction: float = 1.0) -> Optional[dict]:
        """
        Close (fraction<1 = partially close) a position. `side` is the side of
        the OPEN position: a long is closed with a sell, a short with a buy.
        """
        try:
            price = self._current_price(symbol)
            close_side = "sell" if side == "buy" else "buy"
            close_qty = quantity * fraction

            if self.paper_mode:
                order = self._paper_order(symbol, close_side, close_qty, price)
                fill_price = price
            else:
                venue = self.client.futures if side == "sell" else self.client.spot
                order = venue.create_market_order(symbol, close_side, close_qty)
                fill_price = float(order.get("average") or order.get("price") or price)

            if fraction >= 1.0:
                close_ok = self._close_trade(trade_id, fill_price)
            else:
                close_ok = self._partial_close(trade_id, fill_price, close_qty)

            if self.paper_mode and close_ok:
                self._settle_paper(trade_id, fill_price, close_qty, side, fraction)

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

    def _settle_paper(self, trade_id, fill_price, close_qty, side, fraction):
        pos = self._paper_positions.get(trade_id)
        if not pos:
            return
        entry = pos["entry"]
        direction = 1 if side == "buy" else -1
        realized = (fill_price - entry) * close_qty * direction
        released_margin = entry * close_qty
        self._paper_cash += released_margin + realized
        if fraction >= 1.0:
            self._paper_positions.pop(trade_id, None)
        else:
            pos["qty"] -= close_qty
            pos["cost"] -= released_margin

    # -- trailing stop -------------------------------------------------- #

    def update_stop(self, trade_id: int, new_stop: float):
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
                   stop_loss, take_profit, ml_confidence, signal_id) -> Optional[int]:
        session = get_session()
        try:
            result = session.execute(text("""
                INSERT INTO trades
                    (symbol, strategy, side, entry_price, quantity, original_quantity,
                     stop_loss, take_profit, highest_price, lowest_price,
                     ml_confidence, signal_id, entry_time, status)
                VALUES
                    (:symbol, :strategy, :side, :entry_price, :quantity, :quantity,
                     :stop_loss, :take_profit, :entry_price, :entry_price,
                     :ml_confidence, :signal_id, :entry_time, 'open')
                RETURNING id
            """), {
                "symbol": symbol, "strategy": strategy, "side": side,
                "entry_price": entry_price, "quantity": quantity,
                "stop_loss": stop_loss, "take_profit": take_profit,
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

    def _close_trade(self, trade_id: int, exit_price: float) -> bool:
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
            pnl = (exit_price - entry) * qty * direction
            pnl_pct = (exit_price - entry) / entry * direction
            exit_time = datetime.utcnow()
            duration = int((exit_time - trade.entry_time).total_seconds() / 60)

            session.execute(text("""
                UPDATE trades
                SET exit_price=:exit_price, pnl=:pnl, pnl_pct=:pnl_pct,
                    exit_time=:exit_time, duration_minutes=:duration, status='closed'
                WHERE id=:id
            """), {
                "exit_price": exit_price, "pnl": pnl, "pnl_pct": pnl_pct,
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

    def _partial_close(self, trade_id: int, exit_price: float, close_qty: float) -> bool:
        """
        Realize PnL on part of a position: book a child closed-trade row for the
        slice that was sold and shrink the parent's open quantity. Keeps the
        rest of the position running with its stop moved to breakeven (TP1).
        """
        session = get_session()
        try:
            trade = session.execute(
                text("SELECT * FROM trades WHERE id=:id"), {"id": trade_id}
            ).fetchone()
            if not trade or float(trade.quantity) <= close_qty:
                # Nothing sensible to partially close — fall back to full close.
                session.close()
                return self._close_trade(trade_id, exit_price)

            entry = float(trade.entry_price)
            direction = 1 if trade.side == "buy" else -1
            pnl = (exit_price - entry) * close_qty * direction
            pnl_pct = (exit_price - entry) / entry * direction
            now = datetime.utcnow()
            duration = int((now - trade.entry_time).total_seconds() / 60)

            # Child closed row for the realized slice.
            session.execute(text("""
                INSERT INTO trades
                    (symbol, strategy, side, entry_price, exit_price, quantity,
                     pnl, pnl_pct, entry_time, exit_time, duration_minutes,
                     stop_loss, take_profit, status, notes)
                VALUES
                    (:symbol, :strategy, :side, :entry, :exit, :qty,
                     :pnl, :pnl_pct, :entry_time, :exit_time, :duration,
                     :stop, :tp, 'closed', 'partial TP1')
            """), {
                "symbol": trade.symbol, "strategy": trade.strategy, "side": trade.side,
                "entry": entry, "exit": exit_price, "qty": close_qty,
                "pnl": pnl, "pnl_pct": pnl_pct, "entry_time": trade.entry_time,
                "exit_time": now, "duration": duration,
                "stop": trade.stop_loss, "tp": trade.take_profit,
            })

            # Shrink the parent, move stop to breakeven, mark TP1 filled.
            session.execute(text("""
                UPDATE trades
                SET quantity = quantity - :cq, stop_loss = :be, tp1_filled = 1
                WHERE id = :id
            """), {"cq": close_qty, "be": entry, "id": trade_id})

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Partial close failed for {trade_id}: {e}")
            return False
        finally:
            session.close()
