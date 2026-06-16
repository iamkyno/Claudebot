import logging

logger = logging.getLogger(__name__)

try:
    import asyncio
    from telegram import Bot
    _TELEGRAM_OK = True
except ImportError:
    _TELEGRAM_OK = False


class Notifier:
    def __init__(self, config: dict):
        self.enabled = config.get("enabled", False) and _TELEGRAM_OK
        self._bot = None
        if self.enabled:
            token = config.get("token", "")
            self.chat_id = config.get("chat_id", "")
            if token and self.chat_id:
                self._bot = Bot(token=token)
            else:
                self.enabled = False

    def send(self, text: str):
        if not self.enabled or not self._bot:
            logger.info(f"[NOTIFY] {text}")
            return
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                self._bot.send_message(chat_id=self.chat_id, text=text, parse_mode="Markdown")
            )
            loop.close()
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    def trade_opened(self, symbol: str, side: str, price: float, qty: float,
                      strategy: str, confidence: float, stop: float, tp: float):
        icon = "🟢" if side == "buy" else "🔴"
        self.send(
            f"{icon} *{side.upper()} {symbol}*\n"
            f"Strategy: `{strategy}`\n"
            f"Price: `${price:,.4f}` | Qty: `{qty:.6f}`\n"
            f"Stop: `${stop:,.4f}` | TP: `${tp:,.4f}`\n"
            f"ML Confidence: `{confidence:.1%}`"
        )

    def trade_closed(self, symbol: str, pnl: float, pnl_pct: float, strategy: str):
        icon = "✅" if pnl >= 0 else "❌"
        self.send(
            f"{icon} *CLOSED {symbol}*\n"
            f"Strategy: `{strategy}`\n"
            f"PnL: `${pnl:+.4f}` (`{pnl_pct:+.2%}`)"
        )

    def kill_switch(self, reason: str):
        self.send(f"🚨 *KILL SWITCH*\n`{reason}`\nAll trading halted.")

    def model_retrained(self, version: int, accuracy: float, f1: float, samples: int):
        self.send(
            f"🤖 *ML Retrained v{version}*\n"
            f"Accuracy: `{accuracy:.3f}` | F1: `{f1:.3f}`\n"
            f"Samples: `{samples}`"
        )

    def daily_report(self, balance: float, pnl: float, win_rate: float, trades: int):
        icon = "📈" if pnl >= 0 else "📉"
        self.send(
            f"{icon} *Daily Report*\n"
            f"Balance: `${balance:,.2f}`\n"
            f"PnL: `${pnl:+,.2f}` | Win Rate: `{win_rate:.1%}`\n"
            f"Trades: `{trades}`"
        )
