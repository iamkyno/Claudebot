#!/usr/bin/env bash
# Starts the MT5 terminal (if it isn't open) and the bridge, both in Wine.
# Leave this running in its own Terminal window.
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/mac/wine_env.sh

if ! pgrep -f terminal64.exe >/dev/null; then
  echo "Starting MetaTrader 5 ..."
  "$WINE" "$MT5_EXE" >/dev/null 2>&1 &
  sleep 10
fi
SERVER_PY="$("$WINE" winepath -w "$PWD/bridge/mt5_server.py" 2>/dev/null | tr -d '\r')"
echo "Starting MT5 bridge ..."
exec "$WINE" "$WIN_PY" "$SERVER_PY"
