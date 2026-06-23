# Claudebot — Autonomous Crypto Trading Bot

A self-improving, multi-strategy crypto trading bot that runs on Binance, stores
everything in PostgreSQL, and auto-discovers which coins to trade. No manual
thresholds, no config to fill in — it boots straight into paper trading and only
needs API keys when you decide to go live.

---

## Quick Start (zero config)

```bash
git clone https://github.com/iamkyno/Claudebot.git
cd Claudebot
./run.sh
```

That's it. With no API keys, the bot:
- runs in **paper mode** on a virtual $10,000 wallet
- pulls live market data from Binance's **public** endpoints (no key required)
- auto-creates its database and tables on first run
- starts trading all top USDT pairs immediately

Add Binance keys later (see below) only when you want to trade real money.

> `run.sh` needs a running PostgreSQL (defaults: `localhost:5432`, user/password
> `postgres`). On Windows or if you prefer manual steps, follow the sections below.

---

## What It Does

- **Auto-selects symbols**: ranks all USDT pairs on Binance by volume × volatility
  and trades the top 20 (refreshes every 4 hours)
- **7 strategies running simultaneously**:
  - RSI + Bollinger Band mean-reversion (adaptive thresholds from data percentiles)
  - EMA crossover trend-following (periods auto-selected by volatility regime)
  - Funding rate arbitrage (perpetual futures)
  - Liquidation cascade mean-reversion (real-time WebSocket feed)
  - Grid trading (ATR-derived spacing, kills itself in trending markets)
  - Statistical pair trading BTC/ETH (half-life auto-computed from spread)
  - **Fee-aware scalper** — fast in/out on 1m candles, gated by the 5m trend.
    Three micro-edges (VWAP reversion, order-flow imbalance, momentum burst);
    every target is sized to clear ~2× the round-trip fee or the trade is
    skipped, because fees are what kill scalping. Long-only for now.
- **TradingView consensus** (keyless): every buy is cross-checked against
  TradingView's own aggregated technical verdict, and the score feeds the ML model
- **ML self-improvement**: XGBoost model retrains daily on your own trade history,
  scores every signal 0–1, and gates trades below 60% confidence
- **Risk management**: 2% per trade, 5% daily kill-switch, per-symbol position cap,
  ATR-based stop losses — all auto-calculated from your balance and market data
- **Telegram alerts**: trade opens/closes, daily PnL report, ML retrain notification

### A note on the TradingView "MCP"

TradingView-style MCP servers exist, but an MCP server is designed to be driven
interactively by an AI assistant (Claude, Cursor, etc.). This bot runs an
autonomous 24/7 loop, so instead of an MCP it uses the keyless
[`tradingview-ta`](https://pypi.org/project/tradingview-ta/) Python library
directly — same underlying TradingView analysis, no separate server or
subscription. It's wired in at `data/tradingview.py` and toggled with
`bot.use_tradingview` in the config.

---

## Configuration — all optional

The bot reads credentials in this order: **environment variables → `.env` file →
`config/secrets.yaml` → built-in defaults**. Set only what you want to change.

| Method | When to use |
|---|---|
| Nothing | Paper trading on a local PostgreSQL — works out of the box |
| `.env` file | Easiest for keys: `cp .env.example .env` and edit |
| `config/secrets.yaml` | Traditional YAML: `cp config/secrets.yaml.example config/secrets.yaml` |
| Env vars | Servers / Docker / CI |

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | |
| PostgreSQL | 14+ | via Laragon (Windows) or homebrew/apt |
| Binance account | — | only for **live** trading; not needed for paper |

> TimescaleDB is **optional**. The bot runs on plain PostgreSQL. If you have
> TimescaleDB installed, uncomment the `create_hypertable` / `CREATE EXTENSION`
> lines in `schema/schema.sql` for faster time-series queries.

---

## Manual Setup (Windows, or if you skip `run.sh`)

### 1. Database — just have PostgreSQL running

You do **not** need to create the database or run any SQL by hand. On first
launch the bot connects to PostgreSQL, creates the `claudebot` database if it's
missing, and applies the schema automatically.

All you need is a running PostgreSQL server and credentials the bot can use
(defaults: host `localhost`, port `5432`, user/password `postgres`).

- **Windows (Laragon):** Laragon ships MySQL by default — install PostgreSQL 16
  from https://www.postgresql.org/download/windows/ on port **5432**, then set
  the `postgres` user password and put it in your `.env` (`DB_PASSWORD=...`).
- **Linux/macOS:** `sudo apt install postgresql` (or `brew install postgresql`),
  make sure it's running, and set `DB_USER` / `DB_PASSWORD` if they differ from
  the `postgres`/`postgres` default.

> **Prefer MySQL?** A `schema/schema_mysql.sql` is included. You'd change the
> port to `3306` in `config/config.yaml` and swap `postgresql+psycopg2` for
> `mysql+pymysql` in `data/db.py` — but PostgreSQL is the supported path and
> requires zero manual SQL.

### 2. Python Environment

```bash
git clone https://github.com/iamkyno/Claudebot.git
cd Claudebot

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install "setuptools<67"     # one-time: lets the `ta` package build cleanly
pip install -r requirements.txt
```

---

## 3. API Keys (only for live trading)

### Binance

1. Log in to [binance.com](https://www.binance.com) → Profile → API Management
2. Create a new API key
3. Enable: **Read Info** + **Spot & Margin Trading**
4. Whitelist your IP address (recommended)
5. Copy the API Key and Secret Key

### Telegram (optional but recommended)

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts → copy the **token**
3. Message your new bot once, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   to find your **chat_id**

---

## 4. Provide Keys (only for live trading)

Pick whichever is easiest — all are optional for paper mode:

**Option A — `.env` file (recommended):**

```bash
cp .env.example .env
# edit .env: BINANCE_API_KEY, BINANCE_API_SECRET, DB_PASSWORD, etc.
```

**Option B — `config/secrets.yaml`:**

```bash
cp config/secrets.yaml.example config/secrets.yaml
# edit the YAML
```

**Option C — environment variables** (Docker/servers):

```bash
export BINANCE_API_KEY=... BINANCE_API_SECRET=... DB_PASSWORD=...
```

---

## 5. Configuration (Optional Tweaks)

`config/config.yaml` has sensible defaults. The only things worth adjusting:

| Setting | Default | What It Does |
|---|---|---|
| `binance.max_symbols` | 20 | How many top pairs to trade |
| `binance.min_volume_usdt` | 15000000 | Min 24h volume filter ($15M) |
| `binance.testnet` | false | Set true to use Binance Testnet |
| `bot.paper_mode` | true | Paper trading (no real orders) |
| `bot.paper_balance_usdt` | 10000 | Virtual wallet size for paper mode |
| `bot.symbol_refresh_hours` | 4 | How often to re-rank symbols |
| `bot.use_tradingview` | true | Keyless TradingView consensus filter |
| `bot.tradingview_veto_score` | -0.5 | Skip buys when TV consensus ≤ this |
| `risk.max_portfolio_risk_pct` | 0.02 | Max 2% of balance per trade |
| `risk.max_concurrent_positions` | 10 | Max open trades at once |

**There is no `strategies:` section** — thresholds are computed from live market
data automatically.

---

## 6. Run

### Paper trading (default — safe, no real money)

```bash
python -m bot.main
```

The bot will:
1. Create/verify its database and tables
2. Discover top USDT pairs by volume (public Binance data — no key needed)
3. Start the liquidation cascade WebSocket feed
4. Begin scanning for signals every 60 seconds, trading the virtual wallet
5. Log all trades to PostgreSQL (viewable in pgAdmin/Laragon)
6. Send Telegram notifications if configured

Watch the log:

```
2026-06-16 15:00:00 | INFO | bot.main | Initialising database…
2026-06-16 15:00:01 | INFO | bot.orchestrator | Claudebot starting — mode=PAPER symbols=20
2026-06-16 15:00:01 | INFO | bot.orchestrator | Balance: $10,000.00
2026-06-16 15:00:02 | INFO | data.symbol_selector | Symbol selector refreshed: 20 symbols | Top 5: BTC/USDT, ETH/USDT, ...
2026-06-16 15:00:03 | INFO | exchange.liquidation_feed | Liquidation feed started
```

### Going live

1. Run on paper mode for at least **1 week**
2. Review the `trades` table — check win rate and PnL
3. When satisfied, open `config/config.yaml` and set:
   ```yaml
   bot:
     paper_mode: false
   ```
4. Start with a small balance (e.g. $100–500 USDT)

---

## 7. Backtesting a Strategy

```python
from exchange.client import BinanceClient
from data.fetcher import DataFetcher
from strategies.rsi_bb import RSIBBStrategy
from backtest.engine import BacktestEngine

client = BinanceClient()
fetcher = DataFetcher(client)
df = fetcher.fetch_ohlcv("BTC/USDT", "1h", limit=2000)

engine = BacktestEngine(RSIBBStrategy({}), initial_balance=10_000)
result = engine.run("BTC/USDT", df)
engine.print_report(result, "BTC/USDT")
```

---

## 8. Monitoring

### View trades in pgAdmin / Laragon

```sql
-- Today's trades
SELECT symbol, strategy, side, entry_price, exit_price,
       pnl, pnl_pct, ml_confidence, status
FROM trades
WHERE entry_time >= CURRENT_DATE
ORDER BY entry_time DESC;

-- Win rate by strategy
SELECT strategy,
       COUNT(*) AS trades,
       ROUND(AVG(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::numeric, 3) AS win_rate,
       ROUND(SUM(pnl)::numeric, 2) AS total_pnl
FROM trades
WHERE status = 'closed'
GROUP BY strategy
ORDER BY total_pnl DESC;

-- ML model performance over time
SELECT version, accuracy, f1_score, training_samples, trained_at
FROM ml_models
ORDER BY trained_at DESC;
```

### Log file

All activity logs to `claudebot.log` in the project root.

---

## 9. Architecture Overview

```
claudebot/
├── bot/
│   ├── main.py           — entry point
│   ├── orchestrator.py   — main loop, coordinates all modules
│   └── notifier.py       — Telegram alerts
├── strategies/
│   ├── rsi_bb.py         — adaptive RSI + Bollinger Band
│   ├── ema_cross.py      — volatility-regime EMA crossover
│   ├── funding_rate.py   — perpetual futures funding arb
│   ├── liquidation_cascade.py — post-liquidation reversion
│   ├── grid.py           — ATR-spaced grid trading
│   ├── pair_trading.py   — BTC/ETH statistical arbitrage
│   └── scalping.py       — fee-aware 1m scalper (5m trend gate)
├── data/
│   ├── fetcher.py        — OHLCV from Binance + cache
│   ├── features.py       — 20+ technical indicators
│   ├── symbol_selector.py — auto-rank tradeable pairs
│   ├── tradingview.py    — keyless TradingView consensus
│   └── db.py             — PostgreSQL pool + auto-init/migrate
├── exchange/
│   ├── client.py         — ccxt Binance wrapper (spot + futures)
│   ├── orders.py         — order placement + paper wallet + trade logging
│   └── liquidation_feed.py — real-time liquidation WebSocket
├── risk/
│   ├── manager.py        — ATR position sizing, Kelly scaling
│   └── guards.py         — kill-switch, per-symbol cap
├── ml/
│   ├── trainer.py        — XGBoost retraining pipeline
│   └── predictor.py      — signal scoring + logging
├── backtest/
│   └── engine.py         — historical simulation
├── schema/
│   ├── schema.sql        — PostgreSQL schema (auto-applied)
│   └── schema_mysql.sql  — MySQL fallback schema
└── config/
    ├── settings.py       — env/.env/yaml credential resolver
    ├── config.yaml       — bot settings (no thresholds)
    └── secrets.yaml      — API keys (gitignored, optional)
```

---

## 10. Troubleshooting

**`psycopg2` install fails on Windows**

```bash
pip install psycopg2-binary  # use the binary wheel, not source
```

**`ModuleNotFoundError: No module named 'ta'`**

```bash
pip install ta
```

**Paper mode — where does the balance come from?**

Paper mode uses a virtual wallet (`bot.paper_balance_usdt`, default $10,000). It
fills orders at live market prices but never touches a real exchange balance, so
no API key is required. PnL accrues in the `trades` table as positions close.

**`ccxt.errors.AuthenticationError`**

Check that your Binance API key is correct in `config/secrets.yaml` and that
Spot Trading is enabled on the key.

**TimescaleDB `create_hypertable` error**

Make sure the TimescaleDB extension was created in the `claudebot` database
specifically, not just in `postgres`:

```sql
\c claudebot
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

---

## License

MIT
