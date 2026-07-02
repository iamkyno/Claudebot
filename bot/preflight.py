"""
Live-trading pre-flight checks. Run BEFORE flipping paper_mode off:

    python -m bot.preflight

Verifies, without placing any real order:
  1. API keys are loaded
  2. Authenticated spot access works (proves IP whitelist + key validity)
  3. Order placement permission via Binance's TEST order endpoint
     (validated server-side, never reaches the matching engine)
  4. Futures access (needed for shorts) — warns if unavailable
  5. Position sizing vs exchange minimums for your actual balance
"""

import logging
import sys

logging.basicConfig(level=logging.WARNING)

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def _p(status: str, name: str, detail: str = ""):
    color = {"PASS": GREEN, "FAIL": RED, "WARN": YELLOW}[status]
    print(f"  {color}[{status}]{RESET} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("\nClaudebot live pre-flight\n" + "=" * 40)
    failures = 0

    from config.settings import has_binance_keys, get_config
    from exchange.client import BinanceClient

    # 1. Keys present ---------------------------------------------------- #
    if not has_binance_keys():
        _p("FAIL", "API keys", "none found in secrets.yaml / .env / env vars")
        print("\nAdd keys before going live. Aborting remaining checks.\n")
        return 1
    _p("PASS", "API keys", "loaded")

    client = BinanceClient()

    # 2. Authenticated spot access --------------------------------------- #
    usdt_free = 0.0
    try:
        bal = client.spot.fetch_balance()
        usdt_free = float(bal.get("USDT", {}).get("free", 0) or 0)
        _p("PASS", "Spot authentication", f"free USDT: ${usdt_free:,.2f}")
        if usdt_free < 50:
            _p("WARN", "Balance", "under $50 — most orders will fall below "
               "exchange minimums; consider funding more or staying on paper")
    except Exception as e:
        _p("FAIL", "Spot authentication", str(e)[:160])
        failures += 1

    # 3. Order permission via TEST endpoint ------------------------------ #
    try:
        test_fn = getattr(client.spot, "privatePostOrderTest", None)
        if test_fn is None:
            _p("WARN", "Order permission", "test endpoint unavailable in this "
               "ccxt version — skipped (auth already verified above)")
        else:
            test_fn({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                     "quoteOrderQty": "10"})
            _p("PASS", "Order permission", "TEST order accepted (nothing executed)")
    except Exception as e:
        msg = str(e)
        if "-2015" in msg:
            _p("FAIL", "Order permission", "-2015: IP not whitelisted for "
               "trading, or 'Enable Spot Trading' missing on the key")
            failures += 1
        elif "insufficient" in msg.lower():
            _p("WARN", "Order permission", "permission OK, balance too small "
               "for the $10 test amount")
        else:
            _p("FAIL", "Order permission", msg[:160])
            failures += 1

    # 4. Futures access (shorts) ----------------------------------------- #
    cfg = get_config()
    shorts_on = cfg.get("features", {}).get("short_selling", True)
    try:
        fbal = client.futures.fetch_balance()
        fusdt = float(fbal.get("USDT", {}).get("free", 0) or 0)
        if fusdt > 0:
            _p("PASS", "Futures access", f"free USDT in futures wallet: ${fusdt:,.2f}")
        else:
            _p("WARN", "Futures access", "reachable but futures wallet is empty — "
               "shorts will fail until you transfer USDT "
               + ("(or set features.short_selling: false)" if shorts_on else "(shorts disabled anyway)"))
    except Exception as e:
        if shorts_on:
            _p("WARN", "Futures access", "unavailable — shorts will fail. Enable "
               f"Futures on the key or set features.short_selling: false ({str(e)[:100]})")
        else:
            _p("PASS", "Futures access", "unavailable, but shorts are disabled")

    # 5. Sizing vs exchange minimums -------------------------------------- #
    try:
        risk_pct = cfg.get("risk", {}).get("max_portfolio_risk_pct", 0.02)
        # Typical position ≈ risk budget / (stop distance ≈ 1.5%) capped at 20%
        typical_pos = min(usdt_free * risk_pct / 0.015, usdt_free * 0.20)
        min_notional = 10.0  # Binance spot NOTIONAL floor is $5–10 by pair
        if typical_pos >= min_notional:
            _p("PASS", "Position sizing",
               f"typical position ≈ ${typical_pos:,.0f} (min ≈ ${min_notional:.0f})")
        else:
            _p("WARN", "Position sizing",
               f"typical position ≈ ${typical_pos:,.2f} is under the ~${min_notional:.0f} "
               "exchange minimum — orders will be skipped; fund more USDT")
    except Exception as e:
        _p("WARN", "Position sizing", str(e)[:120])

    # Verdict -------------------------------------------------------------- #
    print("=" * 40)
    mode = "PAPER" if cfg["bot"].get("paper_mode", True) else "LIVE"
    if failures:
        print(f"{RED}NOT READY{RESET} — fix the FAIL items above. "
              f"(configured mode: {mode})\n")
        return 1
    print(f"{GREEN}READY{RESET} — no blocking failures. Configured mode: {mode}.")
    print("Flip the toggle on the dashboard (or set bot.paper_mode: false), "
          "restart, and start SMALL.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
