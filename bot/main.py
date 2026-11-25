"""
python -m bot.main [--mode dry-run|live] [--once]

Talks to the Exness Trader API with the key in .env (see README).
"""

import argparse
import logging
import time

from bot.engine import Engine
from bot.notifier import Notifier
from config.settings import ROOT, exness_credentials, imessage_recipient, load_config
from exness.api import ExnessApi
from execution.executor import MODES, DryRunExecutor, LiveExecutor, check_mode
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
    ap = argparse.ArgumentParser(description="Exness SMC scalper (Trader API)")
    ap.add_argument("--mode", choices=MODES, help="overrides mode in config.yaml")
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = ap.parse_args(argv)

    _setup_logging()
    cfg = load_config()
    mode = args.mode or cfg.get("mode", "dry-run")
    api = cfg.get("api", {})

    check_mode(mode, exness_credentials()["account_id"], cfg)  # refuse before connecting
    client = ExnessApi(**exness_credentials(), allow_trading=(mode == "live"),
                       user_agent=api.get("user_agent", "Claudebot/2.0"),
                       order_timeout=api.get("order_timeout_seconds", 20),
                       deviation_points=api.get("deviation_points", 30))
    details = client.connect()
    acct = client.account()
    log.info(f"Exness account {acct.login} ({details.get('account_name', '')}), "
             f"equity {acct.equity:.2f} {acct.currency}, access point {client.access_point}")

    executor = DryRunExecutor() if mode == "dry-run" else LiveExecutor(client, cfg)
    notifier = Notifier(cfg.get("imessage", {}).get("enabled", False), imessage_recipient())
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
        notifier.flush()


if __name__ == "__main__":
    main()
