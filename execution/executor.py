"""Order executors. Mode checks live here so no code path can skip them."""

from __future__ import annotations

import logging

from exness.api import OrderResult

log = logging.getLogger(__name__)

MODES = ("dry-run", "live")


class ModeError(RuntimeError):
    pass


def check_mode(mode: str, account_id: str, cfg: dict) -> None:
    """Live needs allow_live AND the API key's account pinned in config.

    The Exness API has no demo/real flag, so the safety is: you name the one
    account you mean to trade, and nothing else gets an order.
    """
    if mode not in MODES:
        raise ModeError(f"Unknown mode {mode!r}; use one of {MODES}")
    if mode == "live":
        if not cfg.get("allow_live"):
            raise ModeError("Live mode needs allow_live: true in config/config.yaml.")
        pinned = str(cfg.get("live_account_id") or "").strip()
        if pinned != str(account_id):
            raise ModeError(f"live_account_id in config.yaml ({pinned or 'not set'}) doesn't "
                            f"match EXN_ACCOUNT_ID ({account_id}). Refusing to trade. "
                            "Run: python -m bot.setup")


class DryRunExecutor:
    mode = "dry-run"

    def place(self, symbol, side, volume, sl, tp, entry, comment="") -> OrderResult:
        log.info(f"[dry-run] would {side.upper()} {volume} {symbol} @ {entry:.5g} "
                 f"SL {sl:.5g} TP {tp:.5g}")
        return OrderResult(True, None, entry, volume, "dry-run")

    def close(self, pos) -> OrderResult:
        return OrderResult(True, None, 0.0, 0.0, "dry-run")


class LiveExecutor:
    mode = "live"

    def __init__(self, client, cfg: dict):
        self.client, self.cfg = client, cfg

    def _guard(self):
        check_mode("live", self.client.account_id, self.cfg)  # re-checked on every order

    def place(self, symbol, side, volume, sl, tp, entry, comment="") -> OrderResult:
        self._guard()
        res = self.client.market_order(symbol, side, volume, sl, tp, comment)
        log.info(f"[live] {side.upper()} {volume} {symbol} -> {res.message}")
        return res

    def close(self, pos) -> OrderResult:
        self._guard()
        return self.client.close_position(pos)
