# Claudebot — Exness SMC Scalper (MetaTrader 5)

Scalps **XAUUSD** and **BTCUSD** on an Exness MT5 account using smart-money concepts:
M5 market structure sets the direction, M1 times the entry. Built from the
[Exness Scalping Execution Script plan](https://claude.ai/artifact/KpyhnAw33Q7hPt3LtrVn6v).

It runs on a Mac. The MT5 terminal and the `MetaTrader5` Python package are Windows-only,
so they run inside Wine, and the bot (normal macOS Python) reaches them through a small
local bridge on `127.0.0.1`. It never handles your Exness website password and needs no
API keys. Alerts go to your phone over iMessage.

```
macOS Python: bot, journal, iMessage  <-- 127.0.0.1:18812, shared key -->  Wine: bridge + MetaTrader5 package + MT5 terminal
```

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

## Mac setup

1. **Install Wine.** Any recent 64-bit Wine works. Options include Gcenx's macOS Wine
   builds, CrossOver, or a Homebrew cask if one is still available. Apple Silicon Macs
   also need Rosetta 2 (`softwareupdate --install-rosetta`). If `wine64`/`wine` isn't on
   your PATH, set `WINE=/path/to/wine` before the next steps.
2. **One-time setup:**
   ```bash
   scripts/mac/setup_wine.sh
   ```
   This creates a Wine prefix at `~/.wine-mt5`, installs Windows Python 3.11 and the
   `MetaTrader5` package into it, and opens the MT5 installer. Install it, then log in to
   your **Exness demo** account (File → Login to Trade Account; search "Exness" for the
   server), and turn on *Algo Trading* in the toolbar. It also writes `MT5_BRIDGE_KEY` to `.env`.
3. **Start MT5 and the bridge** (leave this window open):
   ```bash
   scripts/mac/start_bridge.sh
   ```
4. **Start the bot** in another window (dry-run by default):
   ```bash
   ./run.sh
   ```
   `run.sh` keeps the Mac awake with `caffeinate`. The Mac still sleeps if you close the
   lid (unless it's on power with an external display), and BTC trades 24/7, so leave it open.

Exness symbol suffixes (`XAUUSDm` on Standard accounts) are found automatically.

```bash
./run.sh --mode demo     # demo orders
./run.sh --once          # single pass, then exit
```

### iMessage alerts

Set `IMESSAGE_TO` in `.env` to your phone number (`+27…`) or Apple ID email, and
`imessage.enabled: true` in the config. The Mac must be signed in to Messages. The first
alert makes macOS ask whether Terminal may control Messages: allow it. You'll get trade
opens and closes, refused or failed orders, and the daily loss stop.

## Testing one trade

1. Run dry-run for a session and compare the logged signals against your charts.
2. Switch MT5 to your demo account and run `./run.sh --mode demo`. Set
   `risk_per_trade_pct` low so the first order is near 0.01 lots, watch it appear in MT5,
   then close it.

## Backtest

Replays M1 history through the same signal and risk code. Results are in R
(multiples of the stop distance).

```bash
python -m backtest.run --symbol XAUUSD --mt5 --days 30      # bridge must be running
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
bot/         main loop, engine, iMessage notifier
bridge/      MT5 bridge server (runs in Wine)
scripts/mac/ Wine setup and bridge launcher
signals/     SMC swing, structure, sweep, FVG logic (pure Python)
risk/        risk guard, lot sizing, SAST sessions
execution/   dry-run and MT5 executors and the mode checks
exness/      MetaTrader5 wrapper and bridge client
journal/     SQLite journal and CSV export
backtest/    M1 replay
tests/       pytest suite (MT5 is faked, so it runs without Wine)
```

Run the tests with `python -m pytest`.
