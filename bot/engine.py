"""One pass of the bot: sync closed trades, then scan each symbol on a fresh M1 close."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.executor import ModeError
from risk.guard import Context, Decision, RiskGuard, lot_size, sast_day_start
from signals.smc import SmcParams, find_entry, structure_bias

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Engine:
    def __init__(self, cfg: dict, client, executor, journal, notifier, root: Path | None = None,
                 now=_utcnow):
        self.cfg, self.client, self.executor = cfg, client, executor
        self.journal, self.notifier, self.now = journal, notifier, now
        self.mode = executor.mode
        self.params = SmcParams.from_cfg(cfg.get("strategy"))
        self.strategy = cfg.get("strategy", {})
        self.risk_cfg = cfg.get("risk", {})
        self.guard = RiskGuard(self.risk_cfg, root)
        self.max_hold = timedelta(minutes=self.risk_cfg.get("max_hold_minutes", 30))
        self.sessions = {}
        self.symbols = {}
        for base, scfg in (cfg.get("symbols") or {}).items():
            name = client.resolve_symbol(base)
            self.symbols[base] = name
            self.sessions[name] = (scfg or {}).get("sessions", "always")
        self._last_bar: dict[str, datetime] = {}
        self._loss_alerted_day = None
        log.info(f"Trading {', '.join(self.symbols.values())} in {self.mode} mode")

    def run_once(self):
        now = self.now()
        equity = self.client.account().equity
        day, day_start_utc = sast_day_start(now)
        start_equity = self.journal.day_start_equity(day, self.mode, equity)
        self._sync_positions(now)
        for name in self.symbols.values():
            try:
                self._scan(name, now, equity, start_equity, day_start_utc)
            except Exception:
                log.exception(f"{name}: scan failed")

    # --- open/closed position upkeep -----------------------------------------------
    def _sync_positions(self, now: datetime):
        if self.mode == "dry-run":
            return
        live = {p.ticket: p for p in self.client.positions()}
        for order in self.journal.open_orders(self.mode):
            pos = live.get(order["ticket"])
            if pos is not None:
                opened = datetime.fromisoformat(order["opened_at"])
                if now - opened >= self.max_hold:
                    res = self.executor.close(pos)
                    log.info(f"{order['symbol']}: time stop after {self.max_hold} -> {res.message}")
                continue
            closed = self.client.closed_result(order["ticket"],
                                               datetime.fromisoformat(order["opened_at"]))
            if closed:
                self.journal.close_order(order["id"], closed.exit_time, closed.exit_price,
                                         closed.profit)
                self.notifier.trade_closed(self.mode, order["symbol"], closed.profit)

    def _open_count(self, name: str, now: datetime) -> int:
        if self.mode == "dry-run":  # treat a dry-run trade as open for max_hold minutes
            return self.journal.orders_since(self.mode, now - self.max_hold, name)
        return len(self.client.positions(name))

    # --- signal -> order -----------------------------------------------------------
    def _scan(self, name, now, equity, start_equity, day_start_utc):
        m1 = self.client.rates(name, "M1", self.strategy.get("entry_bars", 150))
        if len(m1) == 0 or self._last_bar.get(name) == m1.time[-1]:
            return
        self._last_bar[name] = m1.time[-1]

        m5 = self.client.rates(name, "M5", self.strategy.get("trend_bars", 300))
        bias = structure_bias(m5, self.params.trend_swing_length)
        sig = find_entry(m1, bias, self.params)
        if sig is None:
            return
        sid = self.journal.record_signal(self.mode, name, sig, now)
        if sid is None:
            return  # setup already handled

        bid, ask, _ = self.client.tick(name)
        sig = sig.repriced(ask if sig.side == "buy" else bid, self.params.reward_risk)
        spec = self.client.spec(name)
        ctx = Context(now, bid, ask, equity, start_equity,
                      self.journal.orders_since(self.mode, day_start_utc),
                      self._open_count(name, now))
        decision = self.guard.evaluate(sig, ctx, self.sessions[name], spec)
        if decision.ok:
            volume = lot_size(equity, self.risk_cfg.get("risk_per_trade_pct", 0.5),
                              sig.entry, sig.sl, spec, self.risk_cfg.get("max_lots_per_trade"))
            if volume <= 0:
                decision = Decision(False, "risk budget below the minimum lot")
        if not decision.ok:
            self.journal.set_decision(sid, "blocked", decision.reason)
            log.info(f"{name}: {sig.side} signal blocked: {decision.reason}")
            if self.guard.daily_loss_hit(ctx) and self._loss_alerted_day != day_start_utc:
                self._loss_alerted_day = day_start_utc
                self.notifier.send("Daily loss limit hit — no new trades until SAST midnight.")
            return

        try:
            res = self.executor.place(name, sig.side, volume, sig.sl, sig.tp, sig.entry,
                                      comment=f"smc#{sid}")
        except ModeError as e:
            self.journal.set_decision(sid, "refused", str(e))
            log.error(str(e))
            self.notifier.send(f"Order refused: {e}")
            return
        except Exception as e:  # API/network error: record it, don't lose the signal
            self.journal.set_decision(sid, "failed", str(e))
            log.exception(f"{name}: order failed")
            self.notifier.send(f"{name} order failed: {e}")
            return
        if not res.ok:
            self.journal.set_decision(sid, "failed", res.message)
            self.notifier.send(f"{name} order failed: {res.message}")
            return
        self.journal.set_decision(sid, "placed", res.message)
        self.journal.record_order(sid, self.mode, name, sig.side, res.volume, res.price or sig.entry,
                                  sig.sl, sig.tp, res.ticket, now)
        self.notifier.trade_opened(self.mode, name, sig.side, res.volume, res.price or sig.entry,
                                   sig.sl, sig.tp, sig.reason)
