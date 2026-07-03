"""
Export trading data to a JSON file for external analysis.

    python -m bot.export

Writes export/claudebot_export.json containing all trades, all signals
(features + outcomes) and the model registry. No secrets, no keys — safe
to commit and push.
"""

import json
import logging
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

EXPORT_DIR = Path(__file__).parent.parent / "export"


def _clean(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _dump(session, sql: str) -> list[dict]:
    from sqlalchemy import text
    rows = session.execute(text(sql)).fetchall()
    return [{k: _clean(v) for k, v in r._mapping.items()} for r in rows]


def main() -> int:
    from data.db import get_session

    EXPORT_DIR.mkdir(exist_ok=True)
    out_path = EXPORT_DIR / "claudebot_export.json"

    session = get_session()
    try:
        payload = {
            "exported_at": datetime.utcnow().isoformat(),
            "trades": _dump(session, "SELECT * FROM trades ORDER BY id"),
            "signals": _dump(session, "SELECT * FROM signals ORDER BY id"),
            "ml_models": _dump(session, "SELECT * FROM ml_models ORDER BY id"),
        }
    finally:
        session.close()

    out_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    kb = out_path.stat().st_size / 1024
    print(f"Exported {len(payload['trades'])} trades, "
          f"{len(payload['signals'])} signals, "
          f"{len(payload['ml_models'])} model records")
    print(f"-> {out_path}  ({kb:,.0f} KB)")
    print("\nTo share for analysis:")
    print("  git add export/")
    print('  git commit -m "data export"')
    print("  git push origin claude/crypto-trading-bot-plan-rm3biz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
