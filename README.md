# Claudebot — Autonomous Crypto Trading Bot

A self-improving, multi-strategy crypto trading bot that runs on Binance, stores
everything in TimescaleDB, and auto-discovers which coins to trade. No manual
thresholds — just add API keys and run.

---

## What It Does

- **Auto-selects symbols**: ranks all USDT pairs on Binance by volume × volatility
  and trades the top 20 (refreshes every 4 hours)
- **6 strategies running simultaneously**:
  - RSI + Bollinger Band mean-reversion (adaptive thresholds from data percentiles)
  - EMA crossover trend-following (periods auto-selected by volatility regime)
  - Funding rate arbitrage (perpetual futures)
  - Liquidation cascade mean-reversion (real-time WebSocket feed)
  - Grid trading (ATR-derived spacing, kills itself in trending markets)
  - Statistical pair trading BTC/ETH (half-life auto-computed from spread)
- **ML self-improvement**: XGBoost model retrains daily on your own trade history,
  scores every signal 0–1, and gates trades below 60% confidence
- **Risk management**: 2% per trade, 5% daily kill-switch, per-symbol position cap,
  ATR-based stop losses — all auto-calculated from your balance and market data
- **Telegram alerts**: trade opens/closes, daily PnL report, ML retrain notification

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | |
| PostgreSQL | 14+ | via Laragon (Windows) or homebrew/apt |
| TimescaleDB | 2.x | extension for PostgreSQL |
| Binance account | — | needs Spot API key with trading enabled |

---

## 1. Database Setup (TimescaleDB)

### Windows — Laragon

Laragon ships with MySQL by default. You need to add PostgreSQL:

1. Download PostgreSQL 16 from https://www.postgresql.org/download/windows/
2. Install it alongside Laragon (different port — use **5432**)
3. Install TimescaleDB:
   - Download the TimescaleDB installer from https://docs.timescale.com/self-hosted/latest/install/installation-windows/
   - Run the installer, point it at your PostgreSQL 16 installation
4. Open pgAdmin or psql and create the database:

```sql
CREATE DATABASE claudebot;
\c claudebot
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

5. Run the schema:

```bash
psql -U postgres -d claudebot -f schema/schema.sql
```

### Linux / macOS

```bash
# Ubuntu/Debian
sudo apt install postgresql-16
# Add TimescaleDB repo and install
sudo add-apt-repository ppa:timescale/timescaledb-ppa
sudo apt install timescaledb-2-postgresql-16
sudo timescaledb-tune

sudo -u postgres psql -c "CREATE DATABASE claudebot;"
sudo -u postgres psql -d claudebot -c "CREATE EXTENSION timescaledb;"
sudo -u postgres psql -d claudebot -f schema/schema.sql
```

> **Staying on MySQL?** Use `schema/schema_mysql.sql` instead, change the port
> back to `3306` in `config/config.yaml`, and in `data/db.py` replace
> `postgresql+psycopg2` with `mysql+pymysql`. Install `pymysql` instead of
> `psycopg2-binary` in requirements.txt.

---

## 2. Python Environment

```bash
git clone <your-repo-url>
cd Claudebot

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

---

## 3. API Keys

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

## 4. Configure Secrets

```bash
cp config/secrets.yaml.example config/secrets.yaml
```

Edit `config/secrets.yaml`:

```yaml
binance:
  api_key: "your_binance_api_key_here"
  api_secret: "your_binance_api_secret_here"

database:
  user: "postgres"
  password: "your_postgres_password_here"

telegram:
  token: "your_telegram_bot_token_here"   # leave blank if not using
  chat_id: "your_chat_id_here"
```

That's the only file you need to edit. Everything else is automatic.

---

## 5. Configuration (Optional Tweaks)

`config/config.yaml` has sensible defaults. The only things worth adjusting:

| Setting | Default | What It Does |
|---|---|---|
| `binance.max_symbols` | 20 | How many top pairs to trade |
| `binance.min_volume_usdt` | 15000000 | Min 24h volume filter ($15M) |
| `binance.testnet` | false | Set true to use Binance Testnet |
| `bot.paper_mode` | true | Paper trading (no real orders) |
| `bot.symbol_refresh_hours` | 4 | How often to re-rank symbols |
| `risk.max_portfolio_risk_pct` | 0.02 | Max 2% of balance per trade |
| `risk.max_concurrent_positions` | 10 | Max open trades at once |

**Do not touch the `strategies:` section** — it no longer exists. Thresholds
are computed from live market data automatically.

---

## 6. Run

### Paper trading (default — safe, no real money)

```bash
python -m bot.main
```

The bot will:
1. Connect to Binance and fetch your balance
2. Discover top USDT pairs by volume
3. Start the liquidation cascade WebSocket feed
4. Begin scanning for signals every 60 seconds
5. Log all trades to TimescaleDB (viewable in pgAdmin/Laragon)
6. Send Telegram notifications if configured

Watch the log:

```
2026-06-16 15:00:01 | INFO | bot.orchestrator | Claudebot starting — mode=PAPER symbols=20
2026-06-16 15:00:01 | INFO | bot.orchestrator | Balance: $0.00
2026-06-16 15:00:02 | INFO | data.symbol_selector | Refreshed: 20 symbols | Top 5: BTC/USDT, ETH/USDT, ...
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
│   └── pair_trading.py   — BTC/ETH statistical arbitrage
├── data/
│   ├── fetcher.py        — OHLCV from Binance + cache
│   ├── features.py       — 20+ technical indicators
│   ├── symbol_selector.py — auto-rank tradeable pairs
│   └── db.py             — TimescaleDB connection pool
├── exchange/
│   ├── client.py         — ccxt Binance wrapper (spot + futures)
│   ├── orders.py         — order placement + trade logging
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
│   ├── schema.sql        — TimescaleDB schema (primary)
│   └── schema_mysql.sql  — MySQL fallback schema
└── config/
    ├── config.yaml       — bot settings (no thresholds)
    └── secrets.yaml      — API keys (gitignored)
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

**Bot shows `Balance: $0.00` in paper mode**

This is normal — paper mode doesn't query a real balance. It simulates trades
against whatever the current market price is.

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
