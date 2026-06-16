import logging
from datetime import datetime, date
from sqlalchemy import text
from data.db import get_session

logger = logging.getLogger(__name__)


class RiskGuards:
    def __init__(self, config: dict):
        self.daily_loss_limit = config.get("max_daily_loss_pct", 0.07)
        self.strategy_loss_limit = config.get("max_strategy_daily_loss_pct", 0.03)
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

    def check_strategy_kill(self, strategy: str, balance: float) -> bool:
        if self._killed:
            return True
        session = get_session()
        try:
            result = session.execute(text("""
                SELECT COALESCE(SUM(pnl), 0)
                FROM trades
                WHERE strategy=:strategy AND DATE(exit_time)=CURDATE() AND status='closed'
            """), {"strategy": strategy})
            daily_pnl = float(result.scalar() or 0)
            if balance > 0 and (daily_pnl / balance) <= -self.strategy_loss_limit:
                logger.warning(f"Strategy {strategy} hit its daily loss limit")
                return True
            return False
        finally:
            session.close()

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
                VALUES (1, :reason, :now)
            """), {"reason": reason, "now": datetime.utcnow()})
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to log kill event: {e}")
        finally:
            session.close()
