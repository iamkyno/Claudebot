"""
Event-driven backtester with live-engine parity.

Simulates what the bot actually does — not just raw signals:
  - side-aware entries (longs AND shorts)
  - fees + slippage on every fill (same cost model as OrderManager)
  - trailing stops ratcheting with the best price seen
  - partial take-profit at TP1 with stop moved to breakeven
  - optional ML gate + Kelly sizing (pass a predictor / RiskManager)

Run every strategy over real history before risking a cent:

    python -m backtest.engine --symbol BTC/USDT --timeframe 1h --limit 1500
"""

import logging
from dataclasses import dataclass, field
from typing import List

import numpy as np
import pandas as pd

from data.features import compute_features

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    side: str
    entry_price: float
    entry_idx: int
    quantity: float
    stop_loss: float
    take_profit: float
    exit_price: float = None
    exit_idx: int = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    status: str = "open"
    tp1_filled: bool = False
    best_price: float = 0.0


@dataclass
class BacktestResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    total_fees: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, strategy, initial_balance: float = 10_000.0,
                 fee_rate: float = 0.001, slippage_bps: float = 2.0,
                 atr_stop_mult: float = 1.5,
                 trailing: bool = True, partial_tp: bool = True,
                 tp1_ratio: float = 0.5, tp1_close: float = 0.5,
                 predictor=None, risk=None):
        self.strategy = strategy
        self.initial_balance = initial_balance
        self.fee_rate = fee_rate
        self.slip = slippage_bps / 10_000.0
        self.atr_stop_mult = atr_stop_mult
        self.trailing = trailing
        self.partial_tp = partial_tp
        self.tp1_ratio = tp1_ratio
        self.tp1_close = tp1_close
        self.predictor = predictor    # optional: live ML gate
        self.risk = risk              # optional: live Kelly sizing

    # -- fills ----------------------------------------------------------- #

    def _fill(self, price: float, order_side: str) -> float:
        """Market orders cross the spread: buys fill high, sells fill low."""
        return price * (1 + self.slip) if order_side == "buy" else price * (1 - self.slip)

    def _open_fill(self, price: float, side: str) -> float:
        return self._fill(price, side)                      # long opens buy, short opens sell

    def _close_fill(self, price: float, side: str) -> float:
        return self._fill(price, "sell" if side == "buy" else "buy")

    # -- run ------------------------------------------------------------- #

    def run(self, symbol: str, df: pd.DataFrame) -> BacktestResult:
        # Drop only rows missing the core inputs. A blanket dropna() would let
        # slow indicators (ema_200 needs 200 bars) erase most of the history.
        df = compute_features(df)
        core = [c for c in ("rsi", "atr", "ema_50", "bb_position", "volume_ratio")
                if c in df.columns]
        df = df.dropna(subset=core).reset_index(drop=False)
        balance = self.initial_balance
        open_trade: BacktestTrade | None = None
        trades: List[BacktestTrade] = []
        equity = [balance]

        for i in range(55, len(df)):
            window = df.iloc[: i + 1].set_index("timestamp") if "timestamp" in df.columns else df.iloc[: i + 1]
            row = df.iloc[i]
            price = float(row["close"])
            high, low = float(row["high"]), float(row["low"])
            atr = float(row["atr"]) if pd.notna(row.get("atr")) else price * 0.01

            if open_trade:
                t = open_trade
                direction = 1 if t.side == "buy" else -1

                # Trailing stop: ratchet with the best price seen this bar.
                if self.trailing:
                    if t.side == "buy" and high > t.best_price:
                        t.best_price = high
                        t.stop_loss = max(t.stop_loss, high - atr * self.atr_stop_mult)
                    elif t.side == "sell" and low < t.best_price:
                        t.best_price = low
                        t.stop_loss = min(t.stop_loss, low + atr * self.atr_stop_mult)

                # Partial TP at the halfway mark; stop to breakeven.
                if self.partial_tp and not t.tp1_filled and t.take_profit:
                    tp1 = t.entry_price + (t.take_profit - t.entry_price) * self.tp1_ratio
                    hit = high >= tp1 if t.side == "buy" else low <= tp1
                    if hit:
                        fill = self._close_fill(tp1, t.side)
                        qty = t.quantity * self.tp1_close
                        fee = fill * qty * self.fee_rate
                        gross = (fill - t.entry_price) * qty * direction
                        balance += gross - fee
                        t.pnl += gross - fee
                        t.fees += fee
                        t.quantity -= qty
                        t.stop_loss = t.entry_price
                        t.tp1_filled = True

                # Bracket, intrabar (stop checked before TP — pessimistic).
                exit_price = None
                if t.side == "buy":
                    if t.stop_loss and low <= t.stop_loss:
                        exit_price = t.stop_loss
                    elif t.take_profit and high >= t.take_profit:
                        exit_price = t.take_profit
                else:
                    if t.stop_loss and high >= t.stop_loss:
                        exit_price = t.stop_loss
                    elif t.take_profit and low <= t.take_profit:
                        exit_price = t.take_profit

                # Strategy discretionary exit (long-centric, like live).
                if exit_price is None and t.side == "buy":
                    td = {"side": t.side, "entry_price": t.entry_price,
                          "stop_loss": t.stop_loss, "take_profit": t.take_profit}
                    try:
                        if self.strategy.should_exit(symbol, window, td):
                            exit_price = price
                    except Exception:
                        pass

                if exit_price is not None:
                    balance += self._settle(t, exit_price, i)
                    trades.append(t)
                    open_trade = None

            if open_trade is None:
                signal = None
                try:
                    signal = self.strategy.generate_signal(symbol, window)
                except Exception:
                    pass
                if signal and signal.signal_type in ("buy", "sell"):
                    side = signal.signal_type
                    ml_conf = None
                    if self.predictor is not None:
                        ok, ml_conf = self.predictor.should_trade(
                            signal.features, self.strategy.name)
                        if not ok:
                            equity.append(balance)
                            continue
                    # Sizing: live Kelly path when a RiskManager is supplied.
                    if self.risk is not None:
                        stop = signal.stop_loss or 0.0
                        rr = (abs((signal.take_profit or price) - price)
                              / abs(price - stop)) if stop and abs(price - stop) > 0 else 2.0
                        position_value = self.risk.calculate_position_size(
                            balance, price, atr, stop,
                            win_prob=ml_conf, reward_risk=rr)
                    else:
                        position_value = balance * 0.10
                    if position_value <= 0:
                        equity.append(balance)
                        continue

                    fill = self._open_fill(price, side)
                    quantity = position_value / fill
                    entry_fee = fill * quantity * self.fee_rate
                    balance -= entry_fee
                    open_trade = BacktestTrade(
                        symbol=symbol, strategy=self.strategy.name, side=side,
                        entry_price=fill, entry_idx=i, quantity=quantity,
                        stop_loss=signal.stop_loss or 0.0,
                        take_profit=signal.take_profit or 0.0,
                        fees=entry_fee, best_price=fill,
                    )
                    # pnl is seeded with -entry_fee so that, after every
                    # partial/final settle adds (gross - exit_fee), t.pnl is
                    # exactly the trade's total balance impact. Notional is
                    # remembered for an honest pnl_pct after partial closes.
                    open_trade.pnl = -entry_fee
                    open_trade._notional = fill * quantity

            equity.append(balance)

        if open_trade:
            balance += self._settle(open_trade, float(df.iloc[-1]["close"]), len(df) - 1)
            trades.append(open_trade)
            equity.append(balance)

        return self._results(trades, equity)

    def _settle(self, t: BacktestTrade, raw_exit: float, idx: int) -> float:
        """Close the remainder of a trade; returns the balance delta.
        t.pnl was seeded with -entry_fee at open and every partial close added
        its own (gross - exit_fee), so after this it equals the trade's exact
        net balance impact — fees and slippage fully accounted."""
        direction = 1 if t.side == "buy" else -1
        fill = self._close_fill(raw_exit, t.side)
        exit_fee = fill * t.quantity * self.fee_rate
        gross = (fill - t.entry_price) * t.quantity * direction
        t.exit_price = fill
        t.exit_idx = idx
        t.pnl += gross - exit_fee
        t.fees += exit_fee
        t.status = "closed"
        notional = getattr(t, "_notional", t.entry_price * t.quantity)
        t.pnl_pct = t.pnl / notional if notional else 0.0
        return gross - exit_fee

    def _results(self, trades: List[BacktestTrade], equity: List[float]) -> BacktestResult:
        if not trades:
            return BacktestResult()

        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        eq = pd.Series(equity)
        drawdown = (eq - eq.cummax()) / eq.cummax().replace(0, np.nan)
        max_dd = float(drawdown.min())

        rets = eq.pct_change().dropna()
        sharpe = float(rets.mean() / rets.std() * np.sqrt(365 * 24)) if rets.std() > 0 else 0.0

        total_pnl = sum(t.pnl for t in trades)

        return BacktestResult(
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl / self.initial_balance,
            total_fees=sum(t.fees for t in trades),
            win_rate=len(winners) / len(trades),
            avg_win=float(np.mean([t.pnl for t in winners])) if winners else 0.0,
            avg_loss=float(np.mean([t.pnl for t in losers])) if losers else 0.0,
            max_drawdown=max_dd,
            sharpe_ratio=sharpe,
            trades=trades,
        )

    def print_report(self, result: BacktestResult, symbol: str):
        sep = "=" * 52
        print(f"\n{sep}")
        print(f" BACKTEST — {symbol} | {self.strategy.name}")
        print(sep)
        print(f" Trades:       {result.total_trades}")
        print(f" Win Rate:     {result.win_rate:.1%}")
        print(f" Total PnL:    ${result.total_pnl:+,.2f}  ({result.total_pnl_pct:+.2%})")
        print(f" Fees Paid:    ${result.total_fees:,.2f}")
        print(f" Avg Win:      ${result.avg_win:+,.2f}")
        print(f" Avg Loss:     ${result.avg_loss:+,.2f}")
        print(f" Max Drawdown: {result.max_drawdown:.2%}")
        print(f" Sharpe:       {result.sharpe_ratio:.2f}")
        print(f"{sep}\n")


# ---------------------------------------------------------------------- #
# CLI: rank every strategy on real history, with live-parity costs.
#   python -m backtest.engine --symbol BTC/USDT --timeframe 1h --limit 1500

def _all_strategies(risk_cfg):
    from strategies.rsi_bb import RSIBBStrategy
    from strategies.ema_cross import EMACrossStrategy
    from strategies.funding_rate import FundingRateStrategy
    from strategies.grid import GridStrategy
    from strategies.scalping import ScalpStrategy
    return [
        RSIBBStrategy(risk_cfg), EMACrossStrategy(risk_cfg),
        FundingRateStrategy(risk_cfg), GridStrategy(risk_cfg),
        ScalpStrategy(risk_cfg),
    ]


def main():
    import argparse
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Claudebot strategy backtester")
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=1500)
    args = ap.parse_args()

    from config.settings import get_config
    from exchange.client import BinanceClient
    from data.fetcher import DataFetcher

    cfg = get_config()
    risk_cfg = cfg.get("risk", {})
    fees_cfg = cfg.get("fees", {})
    df = DataFetcher(BinanceClient()).fetch_ohlcv(args.symbol, args.timeframe, limit=args.limit)
    if df.empty:
        print(f"No data for {args.symbol} {args.timeframe}")
        return

    print(f"\nBacktest {args.symbol} {args.timeframe}  ({len(df)} bars, "
          f"fees+slippage modeled)\n")
    hdr = (f"{'strategy':<18}{'trades':>7}{'win%':>8}{'netPnL%':>10}"
           f"{'fees$':>9}{'maxDD%':>9}{'sharpe':>9}")
    print(hdr); print("-" * len(hdr))
    for strat in _all_strategies(risk_cfg):
        try:
            eng = BacktestEngine(
                strat,
                fee_rate=fees_cfg.get("spot_taker", 0.001),
                slippage_bps=fees_cfg.get("slippage_bps", 2),
                atr_stop_mult=risk_cfg.get("atr_stop_multiplier", 1.5),
            )
            r = eng.run(args.symbol, df.copy())
            print(f"{strat.name:<18}{r.total_trades:>7}{r.win_rate*100:>8.1f}"
                  f"{r.total_pnl_pct*100:>10.2f}{r.total_fees:>9.2f}"
                  f"{r.max_drawdown*100:>9.2f}{r.sharpe_ratio:>9.2f}")
        except Exception as e:
            print(f"{strat.name:<18}  error: {e}")
    print()


if __name__ == "__main__":
    main()
