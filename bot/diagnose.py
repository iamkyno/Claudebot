"""
Data-pipeline health check. Answers: "is my trade data being processed
correctly, and is the ML actually learning from it?"

    python -m bot.diagnose

Audits every hop of the learning loop:
  trades logged -> closed with NET pnl -> signal outcomes written back ->
  per-class training sets growing -> models trained and active.
"""

import logging
import sys

logging.basicConfig(level=logging.WARNING)

GREEN, RED, YELLOW, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"


def _p(status, name, detail=""):
    color = {"PASS": GREEN, "FAIL": RED, "WARN": YELLOW, "INFO": DIM}[status]
    print(f"  {color}[{status}]{RESET} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from sqlalchemy import text
    from data.db import get_session
    from config.settings import get_config

    cfg = get_config()
    min_train = cfg.get("ml", {}).get("min_trades_to_train", 50)
    label_edge = cfg.get("ml", {}).get("label_min_edge", 0.0015)

    print("\nClaudebot data-pipeline diagnosis\n" + "=" * 46)
    failures = 0
    session = get_session()
    try:
        # -- 1. Trades ---------------------------------------------------- #
        row = session.execute(text("""
            SELECT COUNT(*),
                   SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END)
            FROM trades
        """)).fetchone()
        total, open_n, closed_n = int(row[0]), int(row[1] or 0), int(row[2] or 0)
        print(f"\nTRADES  ({total} total: {closed_n} closed, {open_n} open)")

        null_pnl = session.execute(text(
            "SELECT COUNT(*) FROM trades WHERE status='closed' AND pnl IS NULL"
        )).scalar()
        if null_pnl:
            _p("FAIL", "Closed trades missing PnL", f"{null_pnl} rows — closes not settling")
            failures += 1
        else:
            _p("PASS", "Every closed trade has PnL recorded")

        legacy = session.execute(text(
            "SELECT COUNT(*) FROM trades WHERE status='closed' AND COALESCE(fees,0)=0"
        )).scalar()
        if legacy:
            _p("WARN", "Legacy gross-PnL rows", f"{legacy} closed trades have zero fees "
               "(recorded before the fee-aware engine). Their PnL is optimistic and "
               "their labels are inconsistent with new rows — a dashboard RESET "
               "gives the ML a clean diet.")
        else:
            _p("PASS", "All closed trades carry fees", "net-PnL era only")

        partials = session.execute(text(
            "SELECT COUNT(*) FROM trades WHERE notes='partial TP1'"
        )).scalar() or 0
        if partials:
            _p("INFO", "Partial TP1 child rows", f"{partials} (counted as trades, "
               "intentionally carry no signal linkage)")

        # -- 2. Signal feedback loop --------------------------------------- #
        print("\nSIGNAL FEEDBACK  (what the ML learns from)")
        linked = session.execute(text(
            "SELECT COUNT(*) FROM trades WHERE signal_id IS NOT NULL"
        )).scalar() or 0
        cov = linked / total * 100 if total else 0
        _p("PASS" if cov >= 60 else "WARN", "Trades linked to a signal",
           f"{linked}/{total} ({cov:.0f}%) — unlinked trades (pair_trading, "
           "partial children) don't feed training")

        broken = session.execute(text("""
            SELECT COUNT(*) FROM trades t JOIN signals s ON s.id = t.signal_id
            WHERE t.status='closed' AND s.outcome IS NULL
        """)).scalar() or 0
        if broken:
            _p("FAIL", "Broken outcome write-back", f"{broken} closed trades whose "
               "signal never received an outcome — the learning loop is leaking")
            failures += 1
        else:
            _p("PASS", "Outcome write-back intact", "every closed+linked trade "
               "updated its signal")

        # -- 3. Training sets per model class ------------------------------ #
        print(f"\nTRAINING SETS  (need {min_train}+ labelled, both win & loss classes)")
        for cls, flt in (("scalp", "strategy = 'scalp'"), ("swing", "strategy != 'scalp'")):
            r = session.execute(text(f"""
                SELECT COUNT(*),
                       SUM(CASE WHEN actual_pnl_pct >  :edge THEN 1 ELSE 0 END),
                       SUM(CASE WHEN actual_pnl_pct <= :edge THEN 1 ELSE 0 END)
                FROM signals WHERE outcome IS NOT NULL AND {flt}
            """), {"edge": label_edge}).fetchone()
            n, wins, losses = int(r[0]), int(r[1] or 0), int(r[2] or 0)
            ready = n >= min_train and wins > 0 and losses > 0
            status = "PASS" if ready else ("WARN" if n else "INFO")
            _p(status, f"{cls} training set",
               f"{n} labelled ({wins} wins / {losses} losses vs {label_edge:.2%} edge)"
               + ("" if ready else f" — trains at {min_train} with both classes"))

        # -- 4. Models ------------------------------------------------------ #
        print("\nMODELS")
        rows = session.execute(text("""
            SELECT model_name, version, accuracy, f1_score, training_samples, trained_at
            FROM ml_models WHERE is_active=1 ORDER BY model_name
        """)).fetchall()
        if not rows:
            _p("INFO", "No active models yet",
               "bootstrap mode: all signals trade & get labelled until the "
               "training sets above fill up")
        for m in rows:
            _p("PASS", f"{m[0]} v{m[1]} active",
               f"acc={float(m[2] or 0):.3f} f1={float(m[3] or 0):.3f} "
               f"samples={m[4]} trained={m[5]:%Y-%m-%d %H:%M}")

    finally:
        session.close()

    print("=" * 46)
    if failures:
        print(f"{RED}PIPELINE BROKEN{RESET} — fix the FAIL items above.\n")
        return 1
    print(f"{GREEN}PIPELINE HEALTHY{RESET} — data is flowing and being learned from.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
