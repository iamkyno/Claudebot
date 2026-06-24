import logging
from datetime import datetime, date
from sqlalchemy import text
from data.db import get_session

logger = logging.getLogger(__name__)


class RiskGuards:
    def __init__(self, config: dict):
        self.daily_loss_limit = config.get("max_daily_loss_pct", 0.05)
        self.strategy_loss_limit = config.get("max_strategy_daily_loss_pct", 0.03)
        self.max_positions_per_symbol = config.get("max_positions_per_symbol", 2)
        self._killed = False
        self._kill_reason = None
        self._starting_balance = None
        self._last_day = None

    def set_starting_balance(self, balance: float):
        self._starting_balance = balance

    def check_kill_switch(self, current_balance: float) -> bool:
        if self._killed:
            return True
        if self._starting_balance is None or self._starting_balance <= 0:
            return False

        pct_loss = (current_balance - self._starting_balance) / self._starting_balance
        if pct_loss <= -self.daily_loss_limit:
            self._kill(f"Daily loss limit reached: {pct_loss:.2%}")
            return True
        return False

    def check_symbol_cap(self, symbol: str, open_trades: list) -> bool:
        """Returns True (block trade) when a symbol already has max open positions."""
        count = sum(1 for t in open_trades if t.symbol == symbol)
        if count >= self.max_positions_per_symbol:
            logger.debug(f"Symbol cap hit for {symbol}: {count}/{self.max_positions_per_symbol}")
            return True
        return False

    def check_strategy_kill(self, strategy: str, balance: float) -> bool:
        if self._killed:
            return True
        session = get_session()
        try:
            result = session.execute(text("""
                SELECT COALESCE(SUM(pnl), 0)
                FROM trades
                WHERE strategy=:strategy AND DATE(exit_time)=CURRENT_DATE AND status='closed'
            """), {"strategy": strategy})
            daily_pnl = float(result.scalar() or 0)
            if balance > 0 and (daily_pnl / balance) <= -self.strategy_loss_limit:
                logger.warning(f"Strategy {strategy} hit its daily loss limit")
                return True
            return False
        finally:
            session.close()

    def strategy_size_multiplier(self, strategy: str, lookback_days: int = 7) -> float:
        """
        Capital-allocation weight for a strategy from its recent risk-adjusted
        performance. Computes a rolling Sharpe over the last N days of closed
        trades and maps it to a 0–1 sizing multiplier:

            Sharpe >= 1.0  -> full size (1.0)
            Sharpe  0–1    -> linearly scaled 0.4 .. 1.0
            Sharpe <  0    -> 0.2 (a losing strategy is throttled, not killed,
                              so it can still prove a turnaround on small size)

        Strategies with too few trades to judge get the benefit of the doubt.
        """
        session = get_session()
        try:
            rows = session.execute(text("""
                SELECT pnl_pct FROM trades
                WHERE strategy=:strategy AND status='closed'
                  AND exit_time >= NOW() - (:days || ' days')::interval
                  AND pnl_pct IS NOT NULL
            """), {"strategy": strategy, "days": lookback_days}).fetchall()
        finally:
            session.close()

        rets = [float(r[0]) for r in rows]
        if len(rets) < 8:
            return 1.0  # not enough evidence — don't penalise yet

        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / len(rets)
        std = var ** 0.5
        if std < 1e-9:
            return 1.0 if mean >= 0 else 0.2

        sharpe = mean / std
        if sharpe >= 1.0:
            return 1.0
        if sharpe <= 0.0:
            return 0.2
        return round(0.4 + sharpe * 0.6, 3)

    def reset_daily(self):
        self._killed = False
        self._kill_reason = None
        self._starting_balance = None
        logger.info("Daily risk guards reset")

    @property
    def is_killed(self) -> bool:
        return self._killed

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    def _kill(self, reason: str):
        self._killed = True
        self._kill_reason = reason
        logger.critical(f"KILL SWITCH: {reason}")
        session = get_session()
        try:
            session.execute(text("""
                INSERT INTO bot_state (is_killed, kill_reason, snapshot_time)
                VALUES (1, :reason, NOW())
            """), {"reason": reason})
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to log kill event: {e}")
        finally:
            session.close()
