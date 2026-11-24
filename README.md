# Claudebot — Exness SMC Scalper (MetaTrader 5)

Scalps **XAUUSD** and **BTCUSD** on an Exness MT5 account using smart-money concepts:
M5 market structure sets the direction, M1 times the entry. Built from the
[Exness Scalping Execution Script plan](https://claude.ai/artifact/KpyhnAw33Q7hPt3LtrVn6v).

It talks to the MT5 desktop terminal through the official `MetaTrader5` Python
package, so it never handles your Exness website password and doesn't need API keys.

## How a trade happens

| Step | Rule |
| --- | --- |
| 1. Bias (M5) | Direction of the last close through a confirmed M5 swing point |
| 2. Liquidity sweep (M1) | Price wicks past the last swing low (long) / high (short), by at most 1.5 ATR |
| 3. Change of character | M1 closes through the swing high/low that came before the sweep |
| 4. FVG retest | First pullback into the fair value gap left by that move triggers a market order |
| Stop / target | Stop beyond the sweep wick (+0.1 ATR, at least 0.5 ATR); target 1.8R |

Each setup trades once. Every number above is in `config/config.yaml`.

## Risk guard (checked before every order)

- 0.5% of equity risked per trade, lots rounded down. Skipped if that's below the broker minimum
- Spread must be under 25% of the stop distance
- At most 6 trades per SAST day, and 1 open position per symbol
- Trading halts for the day at a 2% equity drawdown from the SAST day's start. The start
  equity is stored, so restarting the bot doesn't reset the limit
- Session hours: gold Mon–Fri 09:00–22:00 SAST, BTC 24/7
- Time stop: a scalp still open after 30 minutes is closed
- Kill switch: create a file called `KILL` in the project folder and no new trades open

## Modes

| Mode | What it does |
| --- | --- |
| `dry-run` (default) | Reads live prices, logs `would BUY 0.20 XAUUSDm @ … SL … TP …`, sends nothing |
| `demo` | Sends real orders, but **refuses unless MT5 is logged into a demo account**. Re-checked on every order |
| `live` | Real money. Also needs `allow_live: true` in the config |

## Setup (Windows)

1. Install the Exness **MT5 desktop terminal**, log in (demo first), and turn on
   *Tools → Options → Expert Advisors → Allow algorithmic trading*.
2. Install Python 3.11+ and run `run.bat`. It creates a venv, installs requirements
   and starts the bot in dry-run.
3. Optional: `copy .env.example .env` for Telegram alerts, or for MT5 login details if you
   want the bot to log in for you. Leave the MT5 lines blank to use the terminal you're
   already logged into.

Exness symbol suffixes (`XAUUSDm` on Standard accounts) are found automatically.

```bat
python -m bot.main                 :: mode from config (dry-run)
python -m bot.main --mode demo     :: demo orders
python -m bot.main --once          :: single pass, then exit
```

## Testing one trade

1. Run dry-run for a session and compare the logged signals against your charts.
2. Switch MT5 to your demo account and run `python -m bot.main --mode demo`. Set
   `risk_per_trade_pct` low so the first order is near 0.01 lots, watch it appear in MT5,
   then close it.

## Backtest

Replays M1 history through the same signal and risk code. Results are in R
(multiples of the stop distance).

```bat
python -m backtest.run --symbol XAUUSD --mt5 --days 30
python -m backtest.run --symbol XAUUSD --csv XAUUSD_M1.csv --trades-csv trades.csv
```

The CSV can be an MT5 bar export or any file with `time,open,high,low,close` columns.
Fills are at the next bar's open with the spread from `backtest.spread`. When a stop and
a target are both hit in the same bar, it assumes the stop.

## Journal

Every signal, the decision on it (placed / blocked + why / refused / failed), every order
and its closed P/L goes to `journal/journal.db` (SQLite).
`python -m journal.export` writes `signals.csv` and `orders.csv`.

## Layout

```
bot/         main loop, engine, Telegram notifier
signals/     SMC swing, structure, sweep, FVG logic (pure Python)
risk/        risk guard, lot sizing, SAST sessions
execution/   dry-run and MT5 executors and the mode checks
exness/      MetaTrader5 wrapper
journal/     SQLite journal and CSV export
backtest/    M1 replay
tests/       pytest suite (runs on any OS; MT5 is faked)
```

Run the tests with `python -m pytest`.
