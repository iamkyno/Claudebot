# Claudebot — Exness SMC Scalper (Exness Trader API)

Scalps **XAUUSD** and **BTCUSD** on Exness using smart-money concepts: M5 market structure
sets the direction, M1 times the entry. Built from the
[Exness Scalping Execution Script plan](https://claude.ai/artifact/KpyhnAw33Q7hPt3LtrVn6v).

It runs in plain Python on your Mac and talks to the
[Exness Trader API](https://www.exness-api.com) over signed HTTPS. Nothing else is
installed: no MetaTrader, no Wine, no unsigned apps. Alerts go to your phone over iMessage.

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

- 0.5% of equity risked per trade, lots rounded down, and **capped at 0.01 lots** until you
  raise `max_lots_per_trade`
- Spread must be under 25% of the stop distance
- At most 6 trades per SAST day, and 1 open position per symbol
- Trading halts for the day at a 2% equity drawdown from the SAST day's start (stored, so a
  restart doesn't reset it)
- Session hours: gold Mon–Fri 09:00–22:00 SAST, BTC 24/7
- Time stop: a scalp still open after 30 minutes is closed
- Kill switch: create a file called `KILL` in the project folder and no new trades open

## Modes and why they're safe

The Exness API has no demo/real flag, so the safety comes from the key and the config:

| Mode | What it does | What stops a mistake |
| --- | --- | --- |
| `dry-run` (default) | Reads live prices and logs `would BUY 0.01 XAUUSDm @ … SL … TP …` | The client refuses every order call locally. Use a **Read-only** key and the Exness server refuses them too |
| `live` | Sends real orders | Needs `allow_live: true` **and** `live_account_id` equal to the key's account. A Full-access key can only reach one account, which you choose in Key Management |

The bot only time-stops positions it opened itself. Trades you place by hand are left alone,
but they do count as "a position is open" on that symbol.

## Setup (Mac)

1. **Create an API key.** In Exness Personal Area, open **Key Management** and create a key.
   For the dry-run, choose the **Trading → Read-only** scope. Trading scopes need an active
   Exness trading account; Key Management greys out scopes you can't use.
   Save the API key, the private signing key (shown once) and the account ID. Then copy the
   **Gateway host** from the DNS widget. If you set "allowed IPs" on the key, include your
   Mac's public IP.
2. **Fill in `.env`:**
   ```bash
   cp .env.example .env
   ```
   Then set `EXN_API_KEY`, `EXN_PRIVATE_KEY`, `EXN_ACCOUNT_ID` and `EXN_DISCOVERY_BASE_URL`.
   `.env` is git-ignored. Keep the private key out of chats and screenshots.
3. **Run it** (dry-run):
   ```bash
   ./run.sh
   ```
   `run.sh` needs Python 3.10+ (`brew install python@3.12` if you don't have it). It keeps
   the Mac awake with `caffeinate`; closing the lid still sleeps it unless it's on power with
   an external display.

### iMessage alerts

Set `IMESSAGE_TO` in `.env` to your phone number (`+27…`) or Apple ID email, and
`imessage.enabled: true` in the config. The Mac must be signed in to Messages. The first
alert makes macOS ask whether Terminal may control Messages: allow it.

## Going live with one trade

1. Run dry-run for a session and compare the logged signals against your charts.
2. Create a second key with **Trading → Full access** for the one account you want to trade,
   and put it in `.env`.
3. In `config/config.yaml` set `allow_live: true` and `live_account_id: "<that account>"`.
   Leave `max_lots_per_trade: 0.01`.
4. Run `./run.sh --mode live`, watch the first 0.01-lot order appear in your Exness terminal,
   and close it if you want.

## Backtest

Replays M1 history through the same signal and risk code. Results are in R (multiples of the
stop distance). The API serves a year of M1 history.

```bash
python -m backtest.run --symbol XAUUSD --api --days 30
python -m backtest.run --symbol XAUUSD --csv XAUUSD_M1.csv --trades-csv trades.csv
```

Fills are at the next bar's open, with the spread from `backtest.spread`. When a stop and a
target are both hit in the same bar, it assumes the stop.

## Journal

Every signal, the decision on it (placed / blocked + why / refused / failed), every order and
its closed P/L goes to `journal/journal.db` (SQLite). `python -m journal.export` writes
`signals.csv` and `orders.csv`.

## Layout

```
bot/         main loop, engine, iMessage notifier
signals/     SMC swing, structure, sweep, FVG logic (pure Python)
risk/        risk guard, lot sizing, SAST sessions
execution/   dry-run and live executors and the mode checks
exness/      Exness Trader API client (Ed25519 signing, host discovery, rate limits)
journal/     SQLite journal and CSV export
backtest/    M1 replay
tests/       pytest suite (the API is faked; signing is checked against the docs' example)
```

Run the tests with `python -m pytest`.
