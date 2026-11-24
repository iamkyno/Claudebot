# Sourced by the other scripts. Override any of these before running them.
WINE="${WINE:-$(command -v wine64 || command -v wine || true)}"
if [ -z "$WINE" ]; then
  echo "Wine not found. Install it (see README, 'Mac setup'), or set WINE=/path/to/wine." >&2
  exit 1
fi
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-mt5}"
export WINEDEBUG="${WINEDEBUG:--all}"
PY_VERSION="3.11.9"
WIN_PY="C:\\users\\$USER\\AppData\\Local\\Programs\\Python\\Python311\\python.exe"
MT5_EXE="C:\\Program Files\\MetaTrader 5\\terminal64.exe"
