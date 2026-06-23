import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("claudebot.log"),
    ],
)


def main():
    from data.db import init_db
    from config.settings import has_binance_keys

    log = logging.getLogger("bot.main")
    log.info("Initialising database…")
    init_db()

    if not has_binance_keys():
        log.info("No Binance keys detected → paper trading on public market data. "
                 "Add keys (env vars or config/secrets.yaml) to enable live trading.")

    from bot.orchestrator import Orchestrator
    Orchestrator().run()


if __name__ == "__main__":
    main()
