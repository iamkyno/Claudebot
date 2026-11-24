#!/usr/bin/env bash
# One-time setup: Windows Python + MetaTrader5 package + the MT5 terminal, in a
# dedicated Wine prefix (~/.wine-mt5), plus the bridge key in .env.
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/mac/wine_env.sh

mkdir -p .cache/wine
PY_EXE="python-$PY_VERSION-amd64.exe"
[ -f ".cache/wine/$PY_EXE" ] || curl -fL -o ".cache/wine/$PY_EXE" \
  "https://www.python.org/ftp/python/$PY_VERSION/$PY_EXE"
[ -f .cache/wine/mt5setup.exe ] || curl -fL -o .cache/wine/mt5setup.exe \
  "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"

echo "Creating Wine prefix at $WINEPREFIX ..."
"$WINE" wineboot -u

echo "Installing Windows Python $PY_VERSION into Wine ..."
"$WINE" ".cache/wine/$PY_EXE" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
"$WINE" "$WIN_PY" -m pip install --upgrade pip MetaTrader5

echo "Starting the MetaTrader 5 installer: click through it, then log in to your Exness DEMO account."
echo "(File > Login to Trade Account; search 'Exness' for the server.)"
"$WINE" .cache/wine/mt5setup.exe

if ! grep -qE '^MT5_BRIDGE_KEY=.+' .env 2>/dev/null; then
  [ -f .env ] || cp .env.example .env
  grep -v '^MT5_BRIDGE_KEY=' .env > .env.tmp || true
  echo "MT5_BRIDGE_KEY=$(openssl rand -hex 24)" >> .env.tmp
  mv .env.tmp .env
  echo "Wrote MT5_BRIDGE_KEY to .env"
fi
echo "Done. Next: scripts/mac/start_bridge.sh, then ./run.sh"
