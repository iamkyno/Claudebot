"""python -m journal.export [out_dir]  ->  signals.csv + orders.csv"""

import sys

from config.settings import ROOT, load_config
from journal.store import Journal


def main():
    cfg = load_config()
    out = sys.argv[1] if len(sys.argv) > 1 else ROOT / "journal"
    for p in Journal(ROOT / cfg["journal"]["path"]).export_csv(out):
        print(f"wrote {p}")


if __name__ == "__main__":
    main()
