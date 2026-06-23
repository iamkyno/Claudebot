import logging
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from data.db import get_session

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, exchange_client, paper_mode: bool = False,
                 paper_balance: float = 10_000.0):
        self.client = exchange_client
        self.paper_mode = paper_mode
        self._paper_counter = 1
        # Virtual wallet for paper trading so the bot actually trades with no keys.
        self._paper_cash = float(paper_balance)
        self._paper_start = float(paper_balance)
        self._paper_positions: dict[int, float] = {}  # trade_id -> cost basis (USDT)

    # -- paper wallet --------------------------------------------------- #

    def free_cash(self) -> float:
        """Un-deployed USDT available to open new positions (paper mode)."""
        return self._paper_cash

    def equity(self) -> float:
        """Cash + cost basis of open positions = starting balance + realized PnL."""
        return self._paper_cash + sum(self._paper_positions.values())

    def place_market_buy(
        self,
        symbol: str,
        usdt_amount: float,
        strategy: str,
        stop_loss: float,
        take_profit: float,
        ml_confidence: float = None,
        signal_id: int = None,
    ) -> Optional[dict]:
        try:
            ticker = self.client.fetch_ticker(symbol)
            price = ticker["last"]
            quantity = usdt_amount / price

            min_qty = self.client.get_min_order_amount(symbol)
            if quantity < min_qty:
                logger.warning(f"Order qty {quantity:.6f} below minimum {min_qty} for {symbol}")
                return None

            if self.paper_mode:
                order = self._paper_order(symbol, "buy", quantity, price)
            else:
                order = self.client.create_market_order(symbol, "buy", quantity)

            trade_id = self._log_trade(
                symbol, strategy, "buy", price, quantity,
                stop_loss, take_profit, ml_confidence, signal_id,
            )

            if self.paper_mode and trade_id:
                cost = quantity * price
                self._paper_cash -= cost
                self._paper_positions[trade_id] = cost

            prefix = "[PAPER] " if self.paper_mode else ""
            logger.info(f"{prefix}BUY {quantity:.6f} {symbol} @ {price:.4f} | {strategy}")
            return {"order": order, "trade_id": trade_id, "price": price, "quantity": quantity}
        except Exception as e:
            logger.error(f"Buy order failed for {symbol}: {e}")
            return None

    def place_market_sell(
        self,
        symbol: str,
        quantity: float,
        strategy: str,
        trade_id: int = None,
    ) -> Optional[dict]:
        try:
            ticker = self.client.fetch_ticker(symbol)
            price = ticker["last"]

            if self.paper_mode:
                order = self._paper_order(symbol, "sell", quantity, price)
            else:
                order = self.client.create_market_order(symbol, "sell", quantity)

            if self.paper_mode:
                self._paper_cash += quantity * price
                self._paper_positions.pop(trade_id, None)

            if trade_id:
                self._close_trade(trade_id, price)

            prefix = "[PAPER] " if self.paper_mode else ""
            logger.info(f"{prefix}SELL {quantity:.6f} {symbol} @ {price:.4f}")
            return {"order": order, "price": price, "quantity": quantity}
        except Exception as e:
            logger.error(f"Sell order failed for {symbol}: {e}")
            return None

    def get_open_trades(self) -> list:
        session = get_session()
        try:
            result = session.execute(text("SELECT * FROM trades WHERE status='open'"))
            return result.fetchall()
        finally:
            session.close()

    def _paper_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        oid = f"paper_{self._paper_counter}"
        self._paper_counter += 1
        return {"id": oid, "symbol": symbol, "side": side, "amount": amount, "price": price, "status": "closed"}

    def _log_trade(self, symbol, strategy, side, entry_price, quantity,
                    stop_loss, take_profit, ml_confidence, signal_id) -> Optional[int]:
        session = get_session()
        try:
            result = session.execute(text("""
                INSERT INTO trades
                    (symbol, strategy, side, entry_price, quantity,
                     stop_loss, take_profit, ml_confidence, signal_id, entry_time, status)
                VALUES
                    (:symbol, :strategy, :side, :entry_price, :quantity,
                     :stop_loss, :take_profit, :ml_confidence, :signal_id, :entry_time, 'open')
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

    def _close_trade(self, trade_id: int, exit_price: float):
        session = get_session()
        try:
            trade = session.execute(
                text("SELECT * FROM trades WHERE id=:id"), {"id": trade_id}
            ).fetchone()
            if not trade:
                return

            entry = float(trade.entry_price)
            qty = float(trade.quantity)
            pnl = (exit_price - entry) * qty
            pnl_pct = (exit_price - entry) / entry
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
                """), {"outcome": outcome, "pnl_pct": pnl_pct, "trade_id": trade_id, "signal_id": trade.signal_id})

            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to close trade {trade_id}: {e}")
        finally:
            session.close()
