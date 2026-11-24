#!/usr/bin/env bash
# Creates a venv, installs requirements and starts the bot (mode from config.yaml).
# Needs scripts/mac/start_bridge.sh running in another window.
set -e
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet -r requirements.txt
if command -v caffeinate >/dev/null; then
  exec caffeinate -i python -m bot.main "$@"   # keep the Mac awake while the bot runs
fi
exec python -m bot.main "$@"
