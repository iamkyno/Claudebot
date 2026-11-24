"""iMessage alerts through macOS Messages. When off, everything just goes to the log."""

import logging
import subprocess
import sys
import threading

log = logging.getLogger(__name__)

# Message text and recipient are passed as arguments, never pasted into the script.
_SCRIPT = """on run argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send (item 1 of argv) to participant (item 2 of argv) of svc
  end tell
end run"""


class Notifier:
    def __init__(self, enabled: bool, to: str = "", runner=subprocess.run,
                 platform: str = sys.platform, background: bool = True):
        self.to = to
        self.enabled = bool(enabled and to)
        if self.enabled and platform != "darwin":
            log.warning("iMessage alerts need macOS; logging alerts instead.")
            self.enabled = False
        self._run, self._background = runner, background
        self._threads: list[threading.Thread] = []

    def send(self, text: str):
        log.info(f"[notify] {text}")
        if not self.enabled:
            return
        if self._background:  # never hold up the trading loop on Messages
            th = threading.Thread(target=self._deliver, args=(text,), daemon=True)
            th.start()
            self._threads = [t for t in self._threads if t.is_alive()] + [th]
        else:
            self._deliver(text)

    def flush(self, timeout: float = 20):
        """Wait for queued messages, e.g. before the process exits."""
        for th in self._threads:
            th.join(timeout)

    def _deliver(self, text: str):
        try:
            r = self._run(["osascript", "-e", _SCRIPT, text, self.to],
                          capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                log.warning(f"iMessage send failed: {r.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning(f"iMessage send failed: {e}")

    def trade_opened(self, mode, symbol, side, volume, price, sl, tp, reason):
        self.send(f"[{mode}] {side.upper()} {volume} {symbol} @ {price:.5g}\n"
                  f"SL {sl:.5g}  TP {tp:.5g}\n{reason}")

    def trade_closed(self, mode, symbol, profit):
        self.send(f"[{mode}] CLOSED {symbol}  P/L {profit:+.2f}")
