"""
python -m bot.main [--mode dry-run|demo|live] [--once]

Needs the MT5 terminal open and logged in to your Exness account (Windows).
"""

import argparse
import logging
import time

from bot.engine import Engine
from bot.notifier import Notifier
from config.settings import ROOT, load_config, mt5_credentials, telegram_credentials
from exness.mt5_client import Mt5Client
from execution.executor import MODES, DryRunExecutor, Mt5Executor, check_mode
from journal.store import Journal

log = logging.getLogger("bot")


def _setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "bot.log",
                                                               encoding="utf-8")])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exness SMC scalper (MT5)")
    ap.add_argument("--mode", choices=MODES, help="overrides mode in config.yaml")
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = ap.parse_args(argv)

    _setup_logging()
    cfg = load_config()
    mode = args.mode or cfg.get("mode", "dry-run")
    allow_live = bool(cfg.get("allow_live", False))
    m = cfg.get("mt5", {})

    client = Mt5Client(m.get("server_utc_offset_hours", 0), m.get("magic", 7140001),
                       m.get("deviation_points", 30))
    client.connect(**mt5_credentials())
    acct = client.account()
    demo = client.is_demo(acct)
    log.info(f"MT5 account {acct.login} on {acct.server} "
             f"({'DEMO' if demo else 'REAL'}), equity {acct.equity:.2f} {acct.currency}")
    check_mode(mode, demo, allow_live)  # refuse to start in the wrong place

    executor = DryRunExecutor() if mode == "dry-run" else Mt5Executor(client, mode, allow_live)
    tg = telegram_credentials()
    notifier = Notifier(cfg.get("telegram", {}).get("enabled", False), tg["token"], tg["chat_id"])
    journal = Journal(ROOT / cfg.get("journal", {}).get("path", "journal/journal.db"))
    engine = Engine(cfg, client, executor, journal, notifier, root=ROOT)
    notifier.send(f"Bot started in {mode} mode on account {acct.login}")

    try:
        while True:
            try:
                engine.run_once()
            except Exception:
                log.exception("pass failed")
            if args.once:
                break
            time.sleep(cfg.get("loop_seconds", 5))
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
