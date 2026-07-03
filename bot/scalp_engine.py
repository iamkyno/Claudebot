"""
Sniper scalp engine — a dedicated fast loop, decoupled from the 60s swing tick.

Two cadences:
  EXITS  every ~3s: bracket / trailing / time-stop checked straight off the
         WebSocket price map. Zero REST calls, zero OHLCV fetches — a scalp
         that hits its target is out within seconds, not within a minute.
  ENTRIES every ~20s: fresh 1m candles (in-memory TTL cached), 5m trend gate,
         orderbook lean — the same fee-aware setups, scanned 3x per candle so
         a setup that forms mid-minute is caught, not missed.

The wallet is shared with the swing loop; every mutation goes through the
OrderManager lock. Risk gates (kill switch, per-strategy loss cap, symbol cap,
concurrency cap) are enforced here exactly as in the main loop.
"""

import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class ScalpEngine(threading.Thread):
    def __init__(self, *, fetcher, exchange, orders, risk, guards, predictor,
                 scalper, price_stream, tv, notifier, cfg, get_symbols):
        super().__init__(daemon=True, name="scalp-engine")
        self.fetcher = fetcher
        self.exchange = exchange
        self.orders = orders
        self.risk = risk
        self.guards = guards
        self.predictor = predictor
        self.scalper = scalper
        self.price_stream = price_stream
        self.tv = tv
        self.notifier = notifier
        self.get_symbols = get_symbols          # callable -> current symbol list

        scalp_cfg = cfg.get("scalp", {})
        feat = cfg.get("features", {})
        self.entry_tf = scalp_cfg.get("entry_timeframe", "1m")
        self.trend_tf = scalp_cfg.get("trend_timeframe", "5m")
        self.max_symbols = scalp_cfg.get("max_symbols", 8)
        self.exit_every = scalp_cfg.get("exit_check_seconds", 3)
        self.scan_every = scalp_cfg.get("entry_scan_seconds", 20)
        self.time_stop_min = scalp_cfg.get("scalp_time_stop_min", 15)
        self.tv_veto = cfg.get("bot", {}).get("tradingview_veto_score", -0.5)
        self.f_sharpe_size = feat.get("sharpe_sizing", True)
        self.f_trailing = feat.get("trailing_stops", True)

        self._running = False
        self._last_scan = 0.0
        # Opened+closed trades since last drain — the orchestrator adds this
        # to its retrain counter each tick (thread-safe).
        self._trade_events = 0
        self._counter_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------- #

    def start(self):
        self._running = True
        super().start()
        logger.info(
            f"Sniper scalp engine started — exits every {self.exit_every}s, "
            f"entry scans every {self.scan_every}s"
        )

    def stop(self):
        self._running = False

    def drain_trade_events(self) -> int:
        with self._counter_lock:
            n, self._trade_events = self._trade_events, 0
            return n

    def _bump(self):
        with self._counter_lock:
            self._trade_events += 1

    def run(self):
        while self._running:
            try:
                self._check_exits_fast()
                if time.time() - self._last_scan >= self.scan_every:
                    self._last_scan = time.time()
                    self._scan_entries()
            except Exception as e:
                logger.error(f"Scalp engine error: {e}", exc_info=True)
            time.sleep(self.exit_every)

    # -- pure helpers (unit-tested) --------------------------------------- #

    @staticmethod
    def trail_distance(entry: float, take_profit: float) -> float:
        """
        Trailing distance for a scalp, derived from its own bracket geometry:
        the scalper sets SL at 0.6x the target edge, so the trail uses the
        same 0.6x — stable even after the stop has been ratcheted.
        """
        return abs(take_profit - entry) * 0.6

    @staticmethod
    def bracket_exit(side: str, price: float, stop: float | None, tp: float | None) -> bool:
        if side == "buy":
            return bool((stop and price <= stop) or (tp and price >= tp))
        return bool((stop and price >= stop) or (tp and price <= tp))

    # -- fast exits -------------------------------------------------------- #

    def _check_exits_fast(self):
        open_scalps = [t for t in self.orders.get_open_trades()
                       if t.strategy == "scalp"]
        if not open_scalps:
            return

        for t in open_scalps:
            price = self.price_stream.get_price(t.symbol) if self.price_stream else None
            if not price:
                try:
                    price = float(self.exchange.fetch_ticker(t.symbol)["last"])
                except Exception:
                    continue

            side = getattr(t, "side", "buy") or "buy"
            entry = float(t.entry_price)
            stop = float(t.stop_loss) if t.stop_loss else None
            tp = float(t.take_profit) if t.take_profit else None

            # Trailing: ratchet using the bracket's own 0.6x-edge distance —
            # but ONLY once price has crossed halfway to target. Trailing from
            # the first uptick puts the stop inside ordinary 1m noise and
            # scratches the trade before the edge can play out (data: 48 of
            # 119 scalps died inside 60 seconds under first-tick trailing).
            if self.f_trailing and tp:
                dist = self.trail_distance(entry, tp)
                armed_at = entry + (tp - entry) * 0.5   # halfway to TP
                if side == "buy":
                    hi = float(t.highest_price) if t.highest_price else entry
                    if price > hi:
                        self.orders.record_high_low(t.id, highest=price)
                    if price >= armed_at:
                        # Breakeven floor first, then trail from the high.
                        cand = max(entry, max(price, hi) - dist)
                        if stop is None or cand > stop:
                            self.orders.update_stop(t.id, cand)
                            stop = cand
                else:
                    lo = float(t.lowest_price) if t.lowest_price else entry
                    if price < lo:
                        self.orders.record_high_low(t.id, lowest=price)
                    if price <= armed_at:
                        cand = min(entry, min(price, lo) + dist)
                        if stop is None or cand < stop:
                            self.orders.update_stop(t.id, cand)
                            stop = cand

            # Bracket + time stop.
            exit_now = self.bracket_exit(side, price, stop, tp)
            if not exit_now and t.entry_time is not None:
                held_min = (datetime.utcnow() - t.entry_time).total_seconds() / 60.0
                if held_min >= self.time_stop_min:
                    exit_now = True

            if exit_now:
                result = self.orders.close_position(
                    t.symbol, float(t.quantity), t.id, side=side, fraction=1.0,
                    venue="futures",
                )
                if result:
                    direction = 1 if side == "buy" else -1
                    pnl = (result["price"] - entry) * float(t.quantity) * direction
                    pnl_pct = (result["price"] - entry) / entry * direction
                    self.notifier.trade_closed(t.symbol, pnl, pnl_pct, "scalp")
                    self._bump()

    # -- entry scans ------------------------------------------------------- #

    def _scan_entries(self):
        if self.guards.is_killed:
            return
        equity = self.orders.equity()
        if self.guards.check_strategy_kill("scalp", equity):
            return

        symbols = list(self.get_symbols())[: self.max_symbols]
        open_trades = self.orders.get_open_trades()
        free = self.orders.free_cash()

        for symbol in symbols:
            try:
                if self.guards.is_killed:
                    break
                if not self.risk.can_open_position(open_trades, "scalp", free):
                    break
                if self.guards.check_symbol_cap(symbol, open_trades):
                    continue

                # In-memory TTL: candles are shared across scans within the
                # same bar; the DB write only upserts the newest few bars.
                df = self.fetcher.fetch_ohlcv(
                    symbol, self.entry_tf, limit=300,
                    mem_ttl=self.scan_every * 0.75, cache_tail=5,
                )
                if df.empty or len(df) < 60:
                    continue
                df_trend = self.fetcher.fetch_ohlcv(
                    symbol, self.trend_tf, limit=200,
                    mem_ttl=120, cache_tail=5,
                )
                ob = self.exchange.get_orderbook_imbalance(symbol)

                signal = self.scalper.generate_signal(
                    symbol, df, df_trend=df_trend, ob_imbalance=ob
                )
                if signal is None or signal.signal_type != "buy":
                    continue

                tv_score = self.tv.score(symbol) if self.tv else None
                if tv_score is not None and tv_score <= self.tv_veto:
                    continue

                signal.features["funding_rate"] = None
                signal.features["orderbook_imbalance"] = ob
                signal.features["tv_recommendation"] = tv_score

                ok, ml_conf = self.predictor.should_trade(signal.features, "scalp")
                signal_id = self.predictor.log_signal(
                    symbol, "scalp", "buy", ml_conf, signal.features
                )
                if not ok:
                    continue

                price = float(df.iloc[-1]["close"])
                atr = float(df.iloc[-1].get("atr", 0)) or price * 0.005
                rr = (abs(signal.take_profit - price) / abs(price - signal.stop_loss)
                      if signal.stop_loss and abs(price - signal.stop_loss) > 0 else 2.0)
                has_model = self.predictor.has_model_for("scalp")
                size = self.risk.calculate_position_size(
                    free, price, atr, signal.stop_loss,
                    win_prob=ml_conf if has_model else None, reward_risk=rr,
                )
                if has_model:
                    size = self.risk.adjust_for_ml_confidence(size, ml_conf)
                if self.f_sharpe_size:
                    size = round(size * self.guards.strategy_size_multiplier("scalp"), 2)
                if size <= 0:
                    continue

                result = self.orders.open_position(
                    symbol=symbol, side="buy", usdt_amount=size, strategy="scalp",
                    stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                    ml_confidence=ml_conf, signal_id=signal_id,
                    venue="futures",   # scalp edge math assumes futures fees
                )
                if result:
                    self.notifier.trade_opened(
                        symbol, "buy", result["price"], result["quantity"],
                        "scalp", ml_conf, signal.stop_loss, signal.take_profit,
                    )
                    self._bump()
                    open_trades = self.orders.get_open_trades()
                    free = self.orders.free_cash()
            except Exception as e:
                logger.error(f"Scalp scan error {symbol}: {e}")
