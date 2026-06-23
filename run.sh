#!/usr/bin/env bash
# One-command launcher for Claudebot.
# Creates a venv, installs deps, and starts the bot in paper mode.
# Requires a running PostgreSQL (defaults: localhost:5432, user/pass postgres).
set -e

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

echo "Installing dependencies…"
pip install --quiet --upgrade "setuptools<67"
pip install --quiet -r requirements.txt

echo "Starting Claudebot (paper mode)…"
python -m bot.main
