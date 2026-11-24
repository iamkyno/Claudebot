"""Order executors. Mode checks live here so no code path can skip them."""

from __future__ import annotations

import logging

from exness.mt5_client import OrderResult

log = logging.getLogger(__name__)

MODES = ("dry-run", "demo", "live")


class ModeError(RuntimeError):
    pass


def check_mode(mode: str, is_demo: bool, allow_live: bool) -> None:
    if mode not in MODES:
        raise ModeError(f"Unknown mode {mode!r}; use one of {MODES}")
    if mode == "demo" and not is_demo:
        raise ModeError("Demo mode, but MT5 is logged into a REAL account. "
                        "Switch to your Exness demo account in MT5 first.")
    if mode == "live" and not allow_live:
        raise ModeError("Live mode needs allow_live: true in config/config.yaml.")


class DryRunExecutor:
    mode = "dry-run"

    def place(self, symbol, side, volume, sl, tp, entry, comment="") -> OrderResult:
        log.info(f"[dry-run] would {side.upper()} {volume} {symbol} @ {entry:.5g} "
                 f"SL {sl:.5g} TP {tp:.5g}")
        return OrderResult(True, None, entry, volume, "dry-run")

    def close(self, pos) -> OrderResult:
        return OrderResult(True, None, 0.0, 0.0, "dry-run")


class Mt5Executor:
    def __init__(self, client, mode: str, allow_live: bool):
        if mode == "dry-run":
            raise ModeError("Use DryRunExecutor for dry-run")
        self.client, self.mode, self.allow_live = client, mode, allow_live

    def _guard(self):
        # Re-checked on every order: the account can be switched while the bot runs.
        check_mode(self.mode, self.client.is_demo(), self.allow_live)

    def place(self, symbol, side, volume, sl, tp, entry, comment="") -> OrderResult:
        self._guard()
        res = self.client.market_order(symbol, side, volume, sl, tp, comment)
        log.info(f"[{self.mode}] {side.upper()} {volume} {symbol} -> {res.message}")
        return res

    def close(self, pos) -> OrderResult:
        self._guard()
        return self.client.close_position(pos)
