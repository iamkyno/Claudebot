import logging
import time
from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy import text

from data.db import get_session
from data.fetcher import DataFetcher
from data.features import compute_features
from exchange.client import BinanceClient
from exchange.orders import OrderManager
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


def _load_yaml(name: str) -> dict:
    path = Path(__file__).parent.parent / "config" / name
    with open(path) as f:
        return yaml.safe_load(f)


class Orchestrator:
    def __init__(self):
        cfg = _load_yaml("config.yaml")
        secrets = _load_yaml("secrets.yaml")
        self.cfg = cfg

        self.exchange = BinanceClient()
        self.fetcher = DataFetcher(self.exchange)
        self.orders = OrderManager(self.exchange, paper_mode=cfg["bot"].get("paper_mode", True))

        self.risk = RiskManager(cfg["risk"])
        self.guards = RiskGuards(cfg["risk"])

        sc = cfg["strategies"]
        self.strategies = []
        if sc["rsi_bb"]["enabled"]:
            self.strategies.append(RSIBBStrategy({**sc["rsi_bb"], **cfg["risk"]}))
        if sc["ema_cross"]["enabled"]:
            self.strategies.append(EMACrossStrategy(sc["ema_cross"]))
        if sc["funding_rate"]["enabled"]:
            self.strategies.append(FundingRateStrategy(sc["funding_rate"]))
        if sc["liquidation_cascade"]["enabled"]:
            self.strategies.append(LiquidationCascadeStrategy(sc["liquidation_cascade"]))
        if sc["grid"]["enabled"]:
            self.strategies.append(GridStrategy(sc["grid"]))
        if sc["pair_trading"]["enabled"]:
            self.strategies.append(PairTradingStrategy(sc["pair_trading"]))

        self.predictor = SignalPredictor(cfg["ml"])
        self.trainer = ModelTrainer(cfg["ml"])

        tg_cfg = {**cfg.get("telegram", {}), **secrets.get("telegram", {})}
        self.notifier = Notifier(tg_cfg)

        self.symbols = cfg["binance"]["symbols"]
        self.timeframe = cfg["binance"]["timeframe"]
        self._last_day: date = None
        self._trades_since_retrain = 0

    # ------------------------------------------------------------------ #

    def run(self):
        logger.info("Claudebot starting…")
        balance = self._balance()
        self.guards.set_starting_balance(balance)
        logger.info(f"Balance: ${balance:,.2f} | paper={self.cfg['bot'].get('paper_mode')}")

        interval = self.cfg["bot"].get("loop_interval_seconds", 60)
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Stopped by user")
                break
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(interval)

    # ------------------------------------------------------------------ #

    def _tick(self):
        balance = self._balance()

        if self.guards.check_kill_switch(balance):
            return

        today = date.today()
        if today != self._last_day:
            self.guards.reset_daily()
            self.guards.set_starting_balance(balance)
            self._send_daily_report()
            self._last_day = today

        open_trades = self.orders.get_open_trades()
        self._check_exits(open_trades)

        for symbol in self.symbols:
            try:
                self._process_symbol(symbol, balance, open_trades)
            except Exception as e:
                logger.error(f"Error on {symbol}: {e}")

        self._process_pairs(balance, open_trades)
        self._maybe_retrain()

    def _process_symbol(self, symbol: str, balance: float, open_trades: list):
        df = self.fetcher.fetch_ohlcv(symbol, self.timeframe)
        if df.empty or len(df) < 55:
            return
        df = compute_features(df)

        funding_rate = self._get_funding_rate(symbol)
        ob_imbalance = self.exchange.get_orderbook_imbalance(symbol)

        for strategy in self.strategies:
            if strategy.name in ("pair_trading",) or not strategy.is_enabled():
                continue
            if self.guards.check_strategy_kill(strategy.name, balance):
                continue
            if not self.risk.can_open_position(open_trades, strategy.name, balance):
                continue

            kwargs = {}
            if strategy.name == "funding_rate":
                kwargs["funding_rate"] = funding_rate

            signal = strategy.generate_signal(symbol, df, **kwargs)
            if signal is None or signal.signal_type != "buy":
                continue

            signal.features["funding_rate"] = funding_rate
            signal.features["orderbook_imbalance"] = ob_imbalance

            ok, ml_conf = self.predictor.should_trade(signal.features)
            signal_id = self.predictor.log_signal(
                symbol, strategy.name, signal.signal_type, ml_conf, signal.features
            )

            if not ok:
                logger.debug(f"ML rejected {symbol}/{strategy.name} conf={ml_conf:.3f}")
                continue

            atr = float(df.iloc[-1].get("atr", 0)) or float(df.iloc[-1]["close"]) * 0.01
            size = self.risk.calculate_position_size(balance, float(df.iloc[-1]["close"]), atr, signal.stop_loss)
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

    def _process_pairs(self, balance: float, open_trades: list):
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
        if not self.risk.can_open_position(open_trades, "pair_trading", balance):
            return

        ok, ml_conf = self.predictor.should_trade(signal.features)
        if not ok:
            return

        atr = float(df_a.iloc[-1].get("atr", 0)) or float(df_a.iloc[-1]["close"]) * 0.01
        size = self.risk.calculate_position_size(balance, float(df_a.iloc[-1]["close"]), atr, signal.stop_loss)
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

    def _maybe_retrain(self):
        if self._trades_since_retrain < 100 and date.today() == self._last_day:
            return
        logger.info("Retraining ML model…")
        result = self.trainer.train()
        if result:
            self.predictor.load_model()
            m = result["metrics"]
            self.notifier.model_retrained(result["version"], m["accuracy"], m["f1"], m["training_samples"])
        self._trades_since_retrain = 0

    # ------------------------------------------------------------------ #

    def _balance(self) -> float:
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
                WHERE DATE(exit_time) = CURDATE() - INTERVAL 1 DAY AND status='closed'
            """)).fetchone()
            if row:
                pnl, total, wins = float(row[0]), int(row[1]), int(row[2] or 0)
                win_rate = wins / total if total else 0
                self.notifier.daily_report(self._balance(), pnl, win_rate, total)
        except Exception as e:
            logger.error(f"Daily report failed: {e}")
        finally:
            session.close()
