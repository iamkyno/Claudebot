import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd
from sqlalchemy import text

from config.settings import get_config, get_secrets, get_tuning
from data.db import get_session
from data.fetcher import DataFetcher
from data.features import compute_features
from data.symbol_selector import SymbolSelector
from data.tradingview import TradingViewAnalyzer
from data.regime import classify_regime, regime_allows
from data.sentiment import SentimentFeed
from data.open_interest import OpenInterestTracker
from exchange.client import BinanceClient
from exchange.orders import OrderManager
from exchange.liquidation_feed import LiquidationFeed
from exchange.price_stream import PriceStream
from exchange.trade_flow import TradeFlowStream
from risk.manager import RiskManager
from risk.guards import RiskGuards
from ml.trainer import ModelTrainer
from ml.predictor import SignalPredictor
from bot.notifier import Notifier
from bot.scalp_engine import ScalpEngine
from strategies.rsi_bb import RSIBBStrategy
from strategies.ema_cross import EMACrossStrategy
from strategies.funding_rate import FundingRateStrategy
from strategies.liquidation_cascade import LiquidationCascadeStrategy
from strategies.grid import GridStrategy
from strategies.pair_trading import PairTradingStrategy
from strategies.scalping import ScalpStrategy

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self):
        cfg = get_config()
        secrets = get_secrets()
        self.cfg = cfg

        self.paper_mode = cfg["bot"].get("paper_mode", True)
        paper_balance = cfg["bot"].get("paper_balance_usdt", 10_000)

        self.exchange = BinanceClient()
        self.fetcher = DataFetcher(self.exchange)

        # Real-time price stream (one WS for all symbols) — makes fills, the
        # scalper and trailing stops react in ms instead of once per minute.
        feat = cfg.get("features", {})
        self.f_websocket   = feat.get("websocket_prices", True)
        self.f_shorts      = feat.get("short_selling", True)
        self.f_trailing    = feat.get("trailing_stops", True)
        self.f_partial_tp  = feat.get("partial_take_profit", True)
        self.f_regime      = feat.get("regime_filter", True)
        self.f_mtf         = feat.get("mtf_confirmation", True)
        self.f_sentiment   = feat.get("sentiment_filter", True)
        self.f_sharpe_size = feat.get("sharpe_sizing", True)
        self.htf_timeframe = feat.get("htf_timeframe", "4h")
        self.tp1_ratio     = feat.get("tp1_ratio", 0.5)   # trigger: fraction of the way to TP
        self.tp1_close     = feat.get("tp1_close_fraction", 0.5)  # how much to bank
        self._sent_snapshot = None

        self.price_stream = PriceStream() if self.f_websocket else None
        if self.price_stream:
            self.price_stream.start()

        self.orders = OrderManager(
            self.exchange, paper_mode=self.paper_mode, paper_balance=paper_balance,
            price_stream=self.price_stream, fees_cfg=cfg.get("fees", {}),
        )
        # Resume cleanly: rebuild the paper wallet from any positions left open
        # by a previous run so a restart doesn't reset cash or lose positions.
        self.orders.reconcile_paper_wallet()

        self.risk = RiskManager(cfg["risk"])
        self.guards = RiskGuards(cfg["risk"])
        self.sentiment = SentimentFeed() if self.f_sentiment else None

        # All strategies receive the risk config, plus their config.yaml
        # strategies: block (enabled flag etc.), plus any optimizer-derived
        # tuning for their name (config/tuning.json) — evidence over defaults.
        risk_cfg = cfg["risk"]
        strat_cfg = cfg.get("strategies", {}) or {}
        tuning = get_tuning()

        def s_cfg(name: str) -> dict:
            t = tuning.get(name, {})
            if t:
                logger.info(f"Applying optimizer tuning to {name}: {t}")
            merged = {**risk_cfg, **(strat_cfg.get(name) or {}), **t}
            if not merged.get("enabled", True):
                logger.info(f"Strategy {name} is DISABLED via config")
            return merged

        self.strategies = [
            RSIBBStrategy(s_cfg("rsi_bb")),
            EMACrossStrategy(s_cfg("ema_cross")),
            FundingRateStrategy(s_cfg("funding_rate")),
            LiquidationCascadeStrategy(s_cfg("liquidation_cascade")),
            GridStrategy(s_cfg("grid")),
            PairTradingStrategy(s_cfg("pair_trading")),
        ]

        # Fee-aware scalper — fast in/out on a low timeframe, runs separately
        # from the swing strategies above (its own timeframe + exit cadence).
        scalp_cfg = cfg.get("scalp", {})
        # Optimizer-derived parameters override hand-set defaults — written
        # by `python -m backtest.optimize --apply` from historical evidence.
        scalp_tuning = get_tuning().get("scalp", {})
        if scalp_tuning:
            logger.info(f"Applying optimizer tuning to scalp: {scalp_tuning}")
        self.scalp_enabled = (scalp_cfg.get("enabled", True)
                              and (strat_cfg.get("scalp") or {}).get("enabled", True))
        self.scalp_entry_tf = scalp_cfg.get("entry_timeframe", "1m")
        self.scalp_trend_tf = scalp_cfg.get("trend_timeframe", "5m")
        self.scalp_max_symbols = scalp_cfg.get("max_symbols", 8)
        self.scalper = (
            ScalpStrategy(
                {**risk_cfg, **scalp_cfg, **scalp_tuning},
                fee_rate=scalp_cfg.get("taker_fee_rate", 0.0005),
                timeframe=self.scalp_entry_tf,
            )
            if self.scalp_enabled else None
        )

        # Exit lookup covers every strategy that can hold a position, including
        # the scalper, so _check_exits can find the right one + its timeframe.
        self._exit_lookup = {s.name: s for s in self.strategies}
        if self.scalper:
            self._exit_lookup[self.scalper.name] = self.scalper

        self.predictor = SignalPredictor(cfg["ml"])
        self.trainer = ModelTrainer(cfg["ml"])

        tg_cfg = {**cfg.get("telegram", {}), **secrets.get("telegram", {})}
        self.notifier = Notifier(tg_cfg)

        self.timeframe = cfg["binance"]["timeframe"]

        # TradingView consensus (keyless) — extra confirmation + ML feature
        self.tv = (
            TradingViewAnalyzer(self.timeframe)
            if cfg["bot"].get("use_tradingview", True) else None
        )
        # Skip buys when TradingView consensus is at/below this bearish score.
        self.tv_veto = cfg["bot"].get("tradingview_veto_score", -0.5)

        # Dynamic symbol discovery — all eligible USDT pairs on Binance
        self._selector = SymbolSelector(
            self.exchange,
            max_symbols=cfg["binance"].get("max_symbols", 20),
            min_volume_usdt=cfg["binance"].get("min_volume_usdt", 15_000_000),
        )
        self.symbols = self._selector.get_symbols(refresh=True)
        self._symbol_refresh_secs = cfg["bot"].get("symbol_refresh_hours", 4) * 3600
        self._last_symbol_refresh = datetime.utcnow()

        self._last_day: date = None
        self._trades_since_retrain = 0
        self._retrain_every = cfg["ml"].get("retrain_every_trades", 100)
        # Heartbeat + feed watchdog (surfaced via the dashboard /api/health).
        self._last_tick_at: datetime | None = None
        self._ws_warned_at: datetime | None = None
        self._started_at: datetime | None = None

        # Start live liquidation feed (daemon thread)
        self._liq_feed = LiquidationFeed()
        self._liq_feed.start()

        # Microstructure feeds: open-interest crowding + tape aggressor flow.
        # Both are ML features (oi_change / taker_flow); flow covers the most
        # liquid symbols (the scalp set + a margin) on one combined WS.
        self.oi = OpenInterestTracker(self.exchange)
        flow_symbols = self.symbols[: max(self.scalp_max_symbols, 10)]
        self.trade_flow = TradeFlowStream(flow_symbols)
        self.trade_flow.start()

        # Sniper mode: scalps get their own fast loop (exits every few seconds
        # off the WS price, entry scans several times per 1m candle) instead
        # of waiting for this 60s tick. Falls back to in-tick scalping if off.
        sniper = cfg.get("scalp", {}).get("sniper_mode", True)
        self.scalp_engine = None
        if self.scalper and sniper:
            self.scalp_engine = ScalpEngine(
                fetcher=self.fetcher, exchange=self.exchange, orders=self.orders,
                risk=self.risk, guards=self.guards, predictor=self.predictor,
                scalper=self.scalper, price_stream=self.price_stream,
                tv=self.tv, notifier=self.notifier, cfg=cfg,
                get_symbols=lambda: self.symbols,
                trade_flow=self.trade_flow, oi=self.oi,
            )
            self.scalp_engine.start()

        # Let the dashboard's control endpoints (close-all / paper reset)
        # drive this bot instance. Best-effort: fine if the dashboard is off.
        try:
            from dashboard import app as dashboard_app
            dashboard_app.register_bot(self)
        except Exception:
            pass

    # ------------------------------------------------------------------ #

    def run(self):
        self._started_at = datetime.utcnow()
        mode = "PAPER" if self.paper_mode else "LIVE"
        logger.info(f"Claudebot starting — mode={mode} symbols={len(self.symbols)}")
        equity = self._equity()
        self.guards.set_starting_balance(equity)
        logger.info(f"Balance: ${equity:,.2f}")

        # Train at startup when no model is active but labelled data exists.
        # The retrain counter lives in memory, so frequent restarts used to
        # reset it forever and no model ever trained despite ample data.
        if not self.predictor.has_model:
            self._retrain()

        interval = self.cfg["bot"].get("loop_interval_seconds", 60)
        # Ctrl+C almost always lands inside time.sleep(), so the interrupt
        # must be caught around the WHOLE loop — tick and sleep — or the
        # shutdown path is skipped and the process dies with a raw traceback.
        try:
            while True:
                try:
                    self._tick()
                except Exception as e:
                    logger.error(f"Loop error: {e}", exc_info=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Stopped by user — shutting down gracefully…")
        finally:
            self._shutdown()

    def _shutdown(self):
        """Stop background threads and report what carries over to next start."""
        try:
            if self.scalp_engine:
                self.scalp_engine.stop()
            self._liq_feed.stop()
            self.trade_flow.stop()
            if self.price_stream:
                self.price_stream.stop()
            # Give the scalp engine a moment to finish an in-flight close —
            # its DB commits are atomic either way, this just avoids cutting
            # a close between the exchange fill and the log line.
            if self.scalp_engine:
                self.scalp_engine.join(timeout=5)

            open_trades = self.orders.get_open_trades()
            if open_trades:
                logger.info(
                    f"{len(open_trades)} position(s) remain open — they persist in "
                    f"the database and resume (wallet reconciled) on next start:"
                )
                for t in open_trades:
                    logger.info(
                        f"  #{t.id} {t.strategy} {(t.side or 'buy').upper()} "
                        f"{t.symbol} qty={float(t.quantity):.6f} "
                        f"@ {float(t.entry_price):.4f} stop={float(t.stop_loss or 0):.4f}"
                    )
                if not self.paper_mode:
                    logger.warning(
                        "LIVE MODE: these positions are UNMANAGED while the bot is "
                        "down — stops/TPs are enforced by the bot, not the exchange. "
                        "Close positions before extended downtime."
                    )
            logger.info("Shutdown complete")
        except Exception as e:
            logger.error(f"Shutdown error: {e}")

    # ------------------------------------------------------------------ #

    def _tick(self):
        self._last_tick_at = datetime.utcnow()

        # Feed watchdog: if the WS price stream has gone silently stale, exits
        # degrade to REST prices — that must be loud, not silent. Skip the
        # first 60s so a still-connecting stream at boot isn't a false alarm.
        now = datetime.utcnow()
        boot_grace = (self._started_at is not None
                      and (now - self._started_at).total_seconds() < 60)
        if (self.price_stream and not boot_grace
                and not self.price_stream.is_live(max_staleness=30)):
            if (self._ws_warned_at is None
                    or (now - self._ws_warned_at).total_seconds() > 300):
                logger.warning("Price stream is STALE (>30s without a message) "
                               "— exits are falling back to REST prices")
                self._ws_warned_at = now
                # Don't just complain — kick the zombie socket so the stream's
                # run-loop reconnects immediately instead of waiting out backoff.
                self.price_stream.kick()

        equity = self._equity()   # cash + open positions — drives risk/kill-switch
        free = self._free()       # un-deployed cash — drives position sizing

        if self.guards.check_kill_switch(equity):
            return

        today = date.today()
        if today != self._last_day:
            self.guards.reset_daily()
            self.guards.set_starting_balance(equity)
            self._send_daily_report()
            if self._last_day is not None:
                self._retrain()      # daily retrain
                self._prune_tables()  # keep hot tables from growing unbounded
            self._last_day = today

        # Refresh symbol list periodically
        if (datetime.utcnow() - self._last_symbol_refresh).total_seconds() >= self._symbol_refresh_secs:
            self.symbols = self._selector.get_symbols(refresh=True)
            self._last_symbol_refresh = datetime.utcnow()

        # One market-wide sentiment read per tick (TTL-cached underneath).
        self._sent_snapshot = self.sentiment.snapshot() if self.sentiment else None

        open_trades = self.orders.get_open_trades()
        # Sniper mode owns scalp exits (checked every few seconds in its own
        # loop) — this tick must not double-close them.
        if self.scalp_engine:
            self._check_exits([t for t in open_trades if t.strategy != "scalp"])
            self._trades_since_retrain += self.scalp_engine.drain_trade_events()
        else:
            self._check_exits(open_trades)

        # Fan out all per-symbol I/O (OHLCV, HTF frame, funding, book, TV) in
        # parallel, then take decisions sequentially so the wallet math stays
        # single-threaded. Cuts a 20-symbol tick from ~40s to a few seconds.
        prefetched = self._prefetch_symbols(self.symbols)

        for symbol in self.symbols:
            try:
                open_trades = self._process_symbol(
                    symbol, equity, free, open_trades, pre=prefetched.get(symbol)
                )
                free = self._free()
            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}")

        self._process_pairs(equity, free, open_trades)

        # Fast-timeframe scalping pass — only when the sniper engine is off
        # (otherwise it owns scalp entries on its own faster cadence).
        if self.scalper and not self.scalp_engine:
            self._process_scalps(equity)

        if self._trades_since_retrain >= self._retrain_every:
            self._retrain()

    def _prefetch_symbols(self, symbols: list) -> dict:
        """Fetch every symbol's market data concurrently. Decisions stay
        sequential; only the blocking I/O is parallelised."""
        def _one(symbol):
            try:
                return symbol, {
                    "df": self.fetcher.fetch_ohlcv(symbol, self.timeframe),
                    "htf": self._htf_direction(symbol) if self.f_mtf else None,
                    "funding": self._get_funding_rate(symbol),
                    "ob": self.exchange.get_orderbook_imbalance(symbol),
                    "tv": self.tv.score(symbol) if self.tv else None,
                    "oi": self.oi.change(symbol),
                }
            except Exception as e:
                logger.debug(f"Prefetch failed for {symbol}: {e}")
                return symbol, None

        with ThreadPoolExecutor(max_workers=6) as ex:
            return dict(ex.map(_one, symbols))

    def _process_symbol(self, symbol: str, equity: float, free: float,
                        open_trades: list, pre: dict = None) -> list:
        if pre is not None:
            df = pre["df"]
        else:
            df = self.fetcher.fetch_ohlcv(symbol, self.timeframe)
        if df.empty or len(df) < 60:
            return open_trades
        df = compute_features(df)

        regime = classify_regime(df) if self.f_regime else None
        if pre is not None:
            htf_dir = pre["htf"]
            funding_rate = pre["funding"]
            ob_imbalance = pre["ob"]
            tv_score = pre["tv"]
            oi_change = pre.get("oi")
        else:
            htf_dir = self._htf_direction(symbol) if self.f_mtf else None
            funding_rate = self._get_funding_rate(symbol)
            ob_imbalance = self.exchange.get_orderbook_imbalance(symbol)
            tv_score = self.tv.score(symbol) if self.tv else None
            oi_change = self.oi.change(symbol)
        taker_flow = self.trade_flow.flow(symbol)

        for strategy in self.strategies:
            # Bug #5 fix: re-check kill switch each iteration so a loss that
            # crosses the daily limit mid-tick stops further entries immediately.
            if self.guards.is_killed:
                break
            if strategy.name == "pair_trading" or not strategy.is_enabled():
                continue
            # Regime gate: only run a strategy in the market regime it suits.
            if regime is not None and not regime_allows(strategy.name, regime):
                continue
            if self.guards.check_strategy_kill(strategy.name, equity):
                continue
            if self.guards.check_symbol_cap(symbol, open_trades):
                continue
            if not self.risk.can_open_position(open_trades, strategy.name, free):
                continue

            kwargs = {}
            if strategy.name == "funding_rate":
                kwargs["funding_rate"] = funding_rate

            signal = strategy.generate_signal(symbol, df, **kwargs)
            if signal is None or signal.signal_type not in ("buy", "sell"):
                continue
            side = signal.signal_type
            if side == "sell" and not self.f_shorts:
                continue

            # Higher-timeframe gate: don't fight the 4h trend.
            if htf_dir == "up" and side == "sell":
                continue
            if htf_dir == "down" and side == "buy":
                continue

            # TradingView consensus: skip longs the broader market reads bearish.
            if side == "buy" and tv_score is not None and tv_score <= self.tv_veto:
                logger.debug(f"TV veto {symbol}/{strategy.name} tv={tv_score:.2f}")
                continue

            # Market-wide sentiment gate.
            if not self._sentiment_ok(side):
                continue

            signal.features["funding_rate"] = funding_rate
            signal.features["orderbook_imbalance"] = ob_imbalance
            signal.features["tv_recommendation"] = tv_score
            signal.features["oi_change"] = oi_change
            signal.features["taker_flow"] = taker_flow

            ok, ml_conf = self.predictor.should_trade(signal.features, strategy.name)
            signal_id = self.predictor.log_signal(
                symbol, strategy.name, side, ml_conf, signal.features
            )

            if not ok:
                logger.debug(f"ML rejected {symbol}/{strategy.name} conf={ml_conf:.3f}")
                continue

            price = float(df.iloc[-1]["close"])
            atr = float(df.iloc[-1].get("atr", 0)) or price * 0.01

            # Some 'sell' signals are bearish *exit alerts* with no bracket —
            # derive a proper short bracket from ATR when entering a short.
            stop_loss, take_profit = signal.stop_loss, signal.take_profit
            if not stop_loss or stop_loss <= 0:
                stop_loss = self.risk.stop_loss_price(price, atr, side)
            if not take_profit or take_profit <= 0:
                take_profit = self.risk.take_profit_price(price, atr, side)

            risk_dist = abs(price - stop_loss)
            reward_risk = abs(take_profit - price) / risk_dist if risk_dist > 0 else 2.0

            has_model = self.predictor.has_model_for(strategy.name)
            size = self.risk.calculate_position_size(
                free, price, atr, stop_loss,
                win_prob=ml_conf if has_model else None,
                reward_risk=reward_risk,
            )
            if has_model:
                size = self.risk.adjust_for_ml_confidence(size, ml_conf)
            # Capital allocation by rolling risk-adjusted performance.
            if self.f_sharpe_size:
                size = round(size * self.guards.strategy_size_multiplier(strategy.name), 2)
            if size <= 0:
                continue

            result = self.orders.open_position(
                symbol=symbol, side=side, usdt_amount=size, strategy=strategy.name,
                stop_loss=stop_loss, take_profit=take_profit,
                ml_confidence=ml_conf, signal_id=signal_id,
            )
            if result:
                self.notifier.trade_opened(
                    symbol, side, result["price"], result["quantity"],
                    strategy.name, ml_conf, stop_loss, take_profit,
                )
                self._trades_since_retrain += 1
                open_trades = self.orders.get_open_trades()  # refresh after new trade
                free = self._free()
        return open_trades

    def _process_pairs(self, equity: float, free: float, open_trades: list):
        pair_strat = next((s for s in self.strategies if s.name == "pair_trading"), None)
        if not pair_strat or not pair_strat.is_enabled():
            return

        syms = pair_strat.symbols
        df_a = self.fetcher.fetch_ohlcv(syms[0], self.timeframe)
        df_b = self.fetcher.fetch_ohlcv(syms[1], self.timeframe)
        if df_a.empty or df_b.empty:
            return

        df_a = compute_features(df_a)
        signal = pair_strat.generate_signal(syms[0], df_a, df_secondary=df_b)
        if signal is None or signal.signal_type != "buy":
            return
        if self.guards.check_symbol_cap(syms[0], open_trades):
            return
        if not self.risk.can_open_position(open_trades, "pair_trading", free):
            return

        ok, ml_conf = self.predictor.should_trade(signal.features, "pair_trading")
        if not ok:
            return

        atr = float(df_a.iloc[-1].get("atr", 0)) or float(df_a.iloc[-1]["close"]) * 0.01
        size = self.risk.calculate_position_size(
            free, float(df_a.iloc[-1]["close"]), atr, signal.stop_loss
        )
        if size <= 0:
            return

        result = self.orders.place_market_buy(
            symbol=syms[0], usdt_amount=size, strategy="pair_trading",
            stop_loss=signal.stop_loss, take_profit=signal.take_profit, ml_confidence=ml_conf,
        )
        if result:
            self.notifier.trade_opened(
                syms[0], "buy", result["price"], result["quantity"],
                "pair_trading", ml_conf, signal.stop_loss, signal.take_profit,
            )
            self._trades_since_retrain += 1

    def _process_scalps(self, equity: float):
        """Fast in/out scalping on the most liquid pairs (1m entry, 5m gate)."""
        scalp_symbols = self.symbols[: self.scalp_max_symbols]
        open_trades = self.orders.get_open_trades()
        free = self._free()

        # Prefetch both timeframes + the orderbook for every scalp symbol at once.
        def _one(symbol):
            try:
                return symbol, {
                    "df": self.fetcher.fetch_ohlcv(symbol, self.scalp_entry_tf, limit=300),
                    "trend": self.fetcher.fetch_ohlcv(symbol, self.scalp_trend_tf, limit=200),
                    "ob": self.exchange.get_orderbook_imbalance(symbol),
                }
            except Exception:
                return symbol, None
        with ThreadPoolExecutor(max_workers=6) as ex:
            pre = dict(ex.map(_one, scalp_symbols))

        for symbol in scalp_symbols:
            try:
                if self.guards.is_killed:
                    break
                if self.guards.check_strategy_kill("scalp", equity):
                    break
                if not self.risk.can_open_position(open_trades, "scalp", free):
                    break
                if self.guards.check_symbol_cap(symbol, open_trades):
                    continue

                bundle = pre.get(symbol)
                if not bundle:
                    continue
                df, df_trend, ob = bundle["df"], bundle["trend"], bundle["ob"]
                if df.empty or len(df) < 60:
                    continue

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
                    win_prob=ml_conf if has_model else None,
                    reward_risk=rr,
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
                    ml_confidence=ml_conf, signal_id=signal_id, venue="futures",
                )
                if result:
                    self.notifier.trade_opened(
                        symbol, "buy", result["price"], result["quantity"],
                        "scalp", ml_conf, signal.stop_loss, signal.take_profit,
                    )
                    self._trades_since_retrain += 1
                    open_trades = self.orders.get_open_trades()
                    free = self._free()
            except Exception as e:
                logger.error(f"Scalp error {symbol}: {e}")

    def _check_exits(self, open_trades: list):
        if not open_trades:
            return

        # Prefetch each distinct (symbol, timeframe) frame concurrently.
        needs = {}
        for trade in open_trades:
            strat = self._exit_lookup.get(trade.strategy)
            tf = getattr(strat, "timeframe", None) or self.timeframe
            needs[(trade.symbol, tf)] = None
        def _one(key):
            sym, tf = key
            try:
                return key, self.fetcher.fetch_ohlcv(sym, tf)
            except Exception:
                return key, None
        with ThreadPoolExecutor(max_workers=6) as ex:
            frames = dict(ex.map(_one, list(needs.keys())))

        for trade in open_trades:
            strat = self._exit_lookup.get(trade.strategy)
            # Scalp trades are exited on their own (fast) timeframe.
            tf = getattr(strat, "timeframe", None) or self.timeframe
            df = frames.get((trade.symbol, tf))
            if df is None or df.empty:
                continue

            side = getattr(trade, "side", "buy") or "buy"
            price = self._price(trade.symbol, df)
            atr = float(df.iloc[-1].get("atr", 0)) or price * 0.01

            # 1) Trailing stop — ratchet the stop toward price, never away.
            # The returned value is used immediately below so a stop raised
            # THIS tick can also fire this tick (no one-tick staleness).
            eff_stop = float(trade.stop_loss) if trade.stop_loss else None
            if self.f_trailing:
                new_stop = self._update_trailing(trade, side, price, atr)
                if new_stop is not None:
                    eff_stop = new_stop

            venue = "futures" if trade.strategy == "scalp" else None

            # 2) Partial take-profit — bank a slice at TP1, stop to breakeven.
            if self.f_partial_tp and not getattr(trade, "tp1_filled", 0):
                if self._tp1_reached(trade, side, price):
                    r = self.orders.close_position(
                        trade.symbol, float(trade.quantity), trade.id,
                        side=side, fraction=self.tp1_close, venue=venue,
                    )
                    if r:
                        self._trades_since_retrain += 1
                    continue  # re-evaluate the remainder next tick

            # 3) Hard bracket (side-aware) or strategy discretionary exit.
            tp = float(trade.take_profit) if trade.take_profit else None
            exit_now = self._hard_exit(side, price, eff_stop, tp)
            if not exit_now and side == "buy" and strat is not None:
                # Strategy exits are written long-centric; only apply to longs.
                try:
                    exit_now = strat.should_exit(trade.symbol, df, dict(trade._mapping))
                except Exception:
                    exit_now = False

            if exit_now:
                result = self.orders.close_position(
                    trade.symbol, float(trade.quantity), trade.id,
                    side=side, fraction=1.0, venue=venue,
                )
                if result:
                    ep = result["price"]
                    entry = float(trade.entry_price)
                    direction = 1 if side == "buy" else -1
                    pnl = (ep - entry) * float(trade.quantity) * direction
                    pnl_pct = (ep - entry) / entry * direction
                    self.notifier.trade_closed(trade.symbol, pnl, pnl_pct, trade.strategy)
                    self._trades_since_retrain += 1

    # -- exit helpers --------------------------------------------------- #

    def _price(self, symbol: str, df: pd.DataFrame) -> float:
        if self.price_stream:
            p = self.price_stream.get_price(symbol)
            if p:
                return float(p)
        return float(df.iloc[-1]["close"])

    @staticmethod
    def _hard_exit(side: str, price: float, stop: float | None, tp: float | None) -> bool:
        if side == "buy":
            return bool((stop and price <= stop) or (tp and price >= tp))
        return bool((stop and price >= stop) or (tp and price <= tp))

    def _tp1_reached(self, trade, side: str, price: float) -> bool:
        tp = float(trade.take_profit) if trade.take_profit else 0.0
        if tp <= 0:
            return False
        entry = float(trade.entry_price)
        tp1 = entry + (tp - entry) * self.tp1_ratio
        return price >= tp1 if side == "buy" else price <= tp1

    def _update_trailing(self, trade, side: str, price: float, atr: float) -> float | None:
        """Ratchet the stop toward price. Returns the new stop when it moved
        (so the caller can act on it this tick), else None."""
        venue = "futures" if trade.strategy == "scalp" else None
        kw = dict(symbol=trade.symbol, side=side,
                  quantity=float(trade.quantity), venue=venue)
        if side == "buy":
            hi = float(trade.highest_price) if trade.highest_price else float(trade.entry_price)
            if price > hi:
                self.orders.record_high_low(trade.id, highest=price)
                new_stop = self.risk.trailing_stop("buy", price, atr)
                cur = float(trade.stop_loss) if trade.stop_loss else 0.0
                if new_stop > cur:
                    self.orders.update_stop(trade.id, new_stop, **kw)
                    return new_stop
        else:
            lo = float(trade.lowest_price) if trade.lowest_price else float(trade.entry_price)
            if price < lo:
                self.orders.record_high_low(trade.id, lowest=price)
                new_stop = self.risk.trailing_stop("sell", price, atr)
                cur = float(trade.stop_loss) if trade.stop_loss else 1e18
                if new_stop < cur:
                    self.orders.update_stop(trade.id, new_stop, **kw)
                    return new_stop
        return None

    def _htf_direction(self, symbol: str):
        """4h trend direction gate: 'up', 'down', or None (mixed/unknown)."""
        try:
            df = self.fetcher.fetch_ohlcv(symbol, self.htf_timeframe, limit=120)
            if df.empty or len(df) < 55:
                return None
            last = compute_features(df).iloc[-1]
            if pd.isna(last.get("ema_21")) or pd.isna(last.get("ema_50")):
                return None
            e21, e50, close = float(last["ema_21"]), float(last["ema_50"]), float(last["close"])
            if e21 >= e50 and close >= e21:
                return "up"
            if e21 < e50 and close < e21:
                return "down"
            return None
        except Exception:
            return None

    def _sentiment_ok(self, side: str) -> bool:
        snap = self._sent_snapshot
        if not snap:
            return True
        if side == "buy":
            # Don't buy into a euphoric, over-leveraged-long market.
            if snap.get("extreme_greed") and snap.get("overleveraged_long"):
                return False
        else:
            # Don't short into capitulation — squeezes are vicious.
            if snap.get("extreme_fear"):
                return False
        return True

    def _retrain(self):
        logger.info("Retraining ML models…")
        results = self.trainer.train()   # one record per strategy class that trained
        if results:
            self.predictor.load_model()
            for r in results:
                m = r["metrics"]
                self.notifier.model_retrained(
                    r["version"], m["accuracy"], m["f1"], m["training_samples"]
                )
        self._trades_since_retrain = 0

    # ------------------------------------------------------------------ #

    def _equity(self) -> float:
        if self.paper_mode:
            return self.orders.equity()
        return self._real_equity()

    def _free(self) -> float:
        if self.paper_mode:
            return self.orders.free_cash()
        return self._real_balance()

    def _real_balance(self) -> float:
        """Free spot USDT — what's available to open new positions."""
        try:
            b = self.exchange.fetch_balance()
            return float(b.get("USDT", {}).get("free", 0))
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")
            return 0.0

    def _real_equity(self) -> float:
        """
        TRUE live equity, not just free cash: spot USDT (free+locked) plus the
        futures margin balance (which includes unrealized futures PnL) plus
        open spot positions marked to market. Free-cash-only equity made the
        kill switch blind to everything actually at risk.
        """
        total = 0.0
        try:
            spot = self.exchange.spot.fetch_balance()
            u = spot.get("USDT", {})
            total += float(u.get("total") or (u.get("free", 0) or 0) + (u.get("used", 0) or 0))
        except Exception as e:
            logger.error(f"Spot balance fetch failed: {e}")
        try:
            fut = self.exchange.futures.fetch_balance()
            info = fut.get("info", {}) or {}
            margin = info.get("totalMarginBalance")
            total += float(margin) if margin is not None else \
                float(fut.get("USDT", {}).get("total", 0) or 0)
        except Exception as e:
            logger.debug(f"Futures balance fetch failed (no futures wallet?): {e}")
        # Spot longs are held as coins — value them at the live price.
        for t in self.orders.get_open_trades():
            if (t.side or "buy") == "buy" and t.strategy != "scalp":
                px = None
                if self.price_stream:
                    px = self.price_stream.get_price(t.symbol)
                if not px:
                    px = float(t.entry_price)
                total += float(px) * float(t.quantity)
        return total

    def _get_funding_rate(self, symbol: str) -> float | None:
        try:
            perp = symbol.replace("/USDT", "/USDT:USDT")
            data = self.exchange.fetch_funding_rate(perp)
            return data.get("fundingRate") if data else None
        except Exception:
            return None

    def _prune_tables(self):
        """Retention for high-churn tables. The candle cache is a live-data
        fallback (~50k rows/day at 20 symbols), not an archive — historical
        research uses backtest/data.py downloads instead."""
        session = get_session()
        try:
            r1 = session.execute(text(
                "DELETE FROM ohlcv_cache WHERE open_time < NOW() - INTERVAL '30 days'"))
            r2 = session.execute(text(
                "DELETE FROM liquidation_events WHERE event_time < NOW() - INTERVAL '14 days'"))
            session.commit()
            logger.info(f"Pruned {r1.rowcount} cached candles, {r2.rowcount} liquidation events")
        except Exception as e:
            session.rollback()
            logger.debug(f"Prune failed: {e}")
        finally:
            session.close()

    def _send_daily_report(self):
        session = get_session()
        try:
            row = session.execute(text("""
                SELECT COALESCE(SUM(pnl), 0),
                       COUNT(*),
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)
                FROM trades
                WHERE DATE(exit_time) = CURRENT_DATE - INTERVAL '1 day' AND status='closed'
            """)).fetchone()
            if row:
                pnl, total, wins = float(row[0]), int(row[1]), int(row[2] or 0)
                win_rate = wins / total if total else 0
                self.notifier.daily_report(self._equity(), pnl, win_rate, total)
        except Exception as e:
            logger.error(f"Daily report failed: {e}")
        finally:
            session.close()
