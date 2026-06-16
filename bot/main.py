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
    from bot.orchestrator import Orchestrator
    Orchestrator().run()


if __name__ == "__main__":
    main()
