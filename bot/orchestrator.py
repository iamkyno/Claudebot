import logging
import time
from datetime import date, datetime

from sqlalchemy import text

from config.settings import get_config, get_secrets
from data.db import get_session
from data.fetcher import DataFetcher
from data.features import compute_features
from data.symbol_selector import SymbolSelector
from data.tradingview import TradingViewAnalyzer
from exchange.client import BinanceClient
from exchange.orders import OrderManager
from exchange.liquidation_feed import LiquidationFeed
from risk.manager import RiskManager
from risk.guards import RiskGuards
from ml.trainer import ModelTrainer
from ml.predictor import SignalPredictor
from bot.notifier import Notifier
from strategies.rsi_bb import RSIBBStrategy
from strategies.ema_cross import EMACrossStrategy
from strategies.funding_rate import FundingRateStrategy
from strategies.liquidation_cascade import LiquidationCascadeStrategy
from strategies.grid import GridStrategy
from strategies.pair_trading import PairTradingStrategy

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
        self.orders = OrderManager(
            self.exchange, paper_mode=self.paper_mode, paper_balance=paper_balance
        )

        self.risk = RiskManager(cfg["risk"])
        self.guards = RiskGuards(cfg["risk"])

        # All strategies receive only the risk config — thresholds are self-computed
        risk_cfg = cfg["risk"]
        self.strategies = [
            RSIBBStrategy(risk_cfg),
            EMACrossStrategy(risk_cfg),
            FundingRateStrategy(risk_cfg),
            LiquidationCascadeStrategy(risk_cfg),
            GridStrategy(risk_cfg),
            PairTradingStrategy(risk_cfg),
        ]

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

        # Start live liquidation feed (daemon thread)
        self._liq_feed = LiquidationFeed()
        self._liq_feed.start()

    # ------------------------------------------------------------------ #

    def run(self):
        mode = "PAPER" if self.paper_mode else "LIVE"
        logger.info(f"Claudebot starting — mode={mode} symbols={len(self.symbols)}")
        equity = self._equity()
        self.guards.set_starting_balance(equity)
        logger.info(f"Balance: ${equity:,.2f}")

        interval = self.cfg["bot"].get("loop_interval_seconds", 60)
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Stopped by user")
                self._liq_feed.stop()
                break
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(interval)

    # ------------------------------------------------------------------ #

    def _tick(self):
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
            self._last_day = today

        # Refresh symbol list periodically
        if (datetime.utcnow() - self._last_symbol_refresh).total_seconds() >= self._symbol_refresh_secs:
            self.symbols = self._selector.get_symbols(refresh=True)
            self._last_symbol_refresh = datetime.utcnow()

        open_trades = self.orders.get_open_trades()
        self._check_exits(open_trades)

        for symbol in self.symbols:
            try:
                open_trades = self._process_symbol(symbol, equity, free, open_trades)
                free = self._free()
            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}")

        self._process_pairs(equity, free, open_trades)

        if self._trades_since_retrain >= self._retrain_every:
            self._retrain()

    def _process_symbol(self, symbol: str, equity: float, free: float, open_trades: list) -> list:
        df = self.fetcher.fetch_ohlcv(symbol, self.timeframe)
        if df.empty or len(df) < 60:
            return open_trades
        df = compute_features(df)

        funding_rate = self._get_funding_rate(symbol)
        ob_imbalance = self.exchange.get_orderbook_imbalance(symbol)
        tv_score = self.tv.score(symbol) if self.tv else None

        for strategy in self.strategies:
            if strategy.name == "pair_trading" or not strategy.is_enabled():
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
            if signal is None or signal.signal_type != "buy":
                continue

            # TradingView consensus: skip buys the broader market reads as bearish.
            if tv_score is not None and tv_score <= self.tv_veto:
                logger.debug(f"TV veto {symbol}/{strategy.name} tv={tv_score:.2f}")
                continue

            signal.features["funding_rate"] = funding_rate
            signal.features["orderbook_imbalance"] = ob_imbalance
            signal.features["tv_recommendation"] = tv_score

            ok, ml_conf = self.predictor.should_trade(signal.features)
            signal_id = self.predictor.log_signal(
                symbol, strategy.name, signal.signal_type, ml_conf, signal.features
            )

            if not ok:
                logger.debug(f"ML rejected {symbol}/{strategy.name} conf={ml_conf:.3f}")
                continue

            atr = float(df.iloc[-1].get("atr", 0)) or float(df.iloc[-1]["close"]) * 0.01
            size = self.risk.calculate_position_size(
                free, float(df.iloc[-1]["close"]), atr, signal.stop_loss
            )
            # Only scale by ML confidence once a model exists; during bootstrap
            # this would otherwise zero out every trade.
            if self.predictor.has_model:
                size = self.risk.adjust_for_ml_confidence(size, ml_conf)
            if size <= 0:
                continue

            result = self.orders.place_market_buy(
                symbol=symbol, usdt_amount=size, strategy=strategy.name,
                stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                ml_confidence=ml_conf, signal_id=signal_id,
            )
            if result:
                self.notifier.trade_opened(
                    symbol, "buy", result["price"], result["quantity"],
                    strategy.name, ml_conf, signal.stop_loss, signal.take_profit,
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

        ok, ml_conf = self.predictor.should_trade(signal.features)
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

    def _check_exits(self, open_trades: list):
        for trade in open_trades:
            strat = next((s for s in self.strategies if s.name == trade.strategy), None)
            if strat is None:
                continue
            df = self.fetcher.fetch_ohlcv(trade.symbol, self.timeframe)
            if df.empty:
                continue
            trade_dict = dict(trade._mapping)
            if strat.should_exit(trade.symbol, df, trade_dict):
                result = self.orders.place_market_sell(
                    symbol=trade.symbol, quantity=float(trade.quantity),
                    strategy=trade.strategy, trade_id=trade.id,
                )
                if result:
                    price = result["price"]
                    pnl = (price - float(trade.entry_price)) * float(trade.quantity)
                    pnl_pct = (price - float(trade.entry_price)) / float(trade.entry_price)
                    self.notifier.trade_closed(trade.symbol, pnl, pnl_pct, trade.strategy)
                    self._trades_since_retrain += 1

    def _retrain(self):
        logger.info("Retraining ML model…")
        result = self.trainer.train()
        if result:
            self.predictor.load_model()
            m = result["metrics"]
            self.notifier.model_retrained(result["version"], m["accuracy"], m["f1"], m["training_samples"])
        self._trades_since_retrain = 0

    # ------------------------------------------------------------------ #

    def _equity(self) -> float:
        if self.paper_mode:
            return self.orders.equity()
        return self._real_balance()

    def _free(self) -> float:
        if self.paper_mode:
            return self.orders.free_cash()
        return self._real_balance()

    def _real_balance(self) -> float:
        try:
            b = self.exchange.fetch_balance()
            return float(b.get("USDT", {}).get("free", 0))
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")
            return 0.0

    def _get_funding_rate(self, symbol: str) -> float | None:
        try:
            perp = symbol.replace("/USDT", "/USDT:USDT")
            data = self.exchange.fetch_funding_rate(perp)
            return data.get("fundingRate") if data else None
        except Exception:
            return None

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
