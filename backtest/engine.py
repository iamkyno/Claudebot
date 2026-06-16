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
    status: str = "open"


@dataclass
class BacktestResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, strategy, initial_balance: float = 10_000.0, commission: float = 0.001):
        self.strategy = strategy
        self.initial_balance = initial_balance
        self.commission = commission

    def run(self, symbol: str, df: pd.DataFrame) -> BacktestResult:
        df = compute_features(df).dropna().reset_index(drop=False)
        balance = self.initial_balance
        open_trade: BacktestTrade | None = None
        trades: List[BacktestTrade] = []
        equity = [balance]

        for i in range(55, len(df)):
            window = df.iloc[: i + 1].set_index("timestamp") if "timestamp" in df.columns else df.iloc[: i + 1]
            price = float(df.iloc[i]["close"])

            if open_trade:
                td = {"stop_loss": open_trade.stop_loss, "take_profit": open_trade.take_profit}
                if self.strategy.should_exit(symbol, window, td):
                    open_trade = self._close(open_trade, price, i, balance)
                    balance += open_trade.pnl - price * open_trade.quantity * self.commission
                    trades.append(open_trade)
                    open_trade = None

            if open_trade is None:
                signal = self.strategy.generate_signal(symbol, window)
                if signal and signal.signal_type == "buy":
                    position_value = balance * 0.10
                    quantity = position_value / price
                    balance -= position_value * self.commission
                    open_trade = BacktestTrade(
                        symbol=symbol, strategy=self.strategy.name, side="buy",
                        entry_price=price, entry_idx=i, quantity=quantity,
                        stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                    )

            equity.append(balance)

        if open_trade:
            price = float(df.iloc[-1]["close"])
            open_trade = self._close(open_trade, price, len(df) - 1, balance)
            trades.append(open_trade)

        return self._results(trades, equity)

    def _close(self, trade: BacktestTrade, price: float, idx: int, balance: float) -> BacktestTrade:
        trade.exit_price = price
        trade.exit_idx = idx
        trade.pnl = (price - trade.entry_price) * trade.quantity
        trade.pnl_pct = (price - trade.entry_price) / trade.entry_price
        trade.status = "closed"
        return trade

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
        print(f" Avg Win:      ${result.avg_win:+,.2f}")
        print(f" Avg Loss:     ${result.avg_loss:+,.2f}")
        print(f" Max Drawdown: {result.max_drawdown:.2%}")
        print(f" Sharpe:       {result.sharpe_ratio:.2f}")
        print(f"{sep}\n")
