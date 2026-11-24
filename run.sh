#!/usr/bin/env bash
# Creates a venv, installs requirements and starts the bot (mode from config.yaml).
set -e
cd "$(dirname "$0")"
for PY in python3.12 python3.11 python3.10 python3; do command -v "$PY" >/dev/null && break; done
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || { echo "Python 3.10+ needed: brew install python@3.12"; exit 1; }
[ -d venv ] || "$PY" -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet -r requirements.txt
if command -v caffeinate >/dev/null; then
  exec caffeinate -i python -m bot.main "$@"   # keep the Mac awake while the bot runs
fi
exec python -m bot.main "$@"
