"""Telegram alerts. Without a token everything just goes to the log."""

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, enabled: bool, token: str = "", chat_id: str = ""):
        self.enabled = bool(enabled and token and chat_id)
        self.token, self.chat_id = token, chat_id

    def send(self, text: str):
        log.info(f"[notify] {text}")
        if not self.enabled:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except requests.RequestException as e:
            log.warning(f"Telegram send failed: {e}")

    def trade_opened(self, mode, symbol, side, volume, price, sl, tp, reason):
        self.send(f"[{mode}] {side.upper()} {volume} {symbol} @ {price:.5g}\n"
                  f"SL {sl:.5g}  TP {tp:.5g}\n{reason}")

    def trade_closed(self, mode, symbol, profit):
        self.send(f"[{mode}] CLOSED {symbol}  P/L {profit:+.2f}")
