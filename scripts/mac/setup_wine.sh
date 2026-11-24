#!/usr/bin/env bash
# One-time setup. Installs (only what's missing): Homebrew, Rosetta 2, Wine,
# Python 3.12, then Windows Python + the MetaTrader5 package + the MT5 terminal
# in a dedicated Wine prefix (~/.wine-mt5), and writes the bridge key to .env.
set -euo pipefail
cd "$(dirname "$0")/../.."

# --- Homebrew ------------------------------------------------------------------
if ! command -v brew >/dev/null; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
if ! command -v brew >/dev/null; then
  echo "Installing Homebrew (it will ask for your Mac password) ..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi

# --- Rosetta 2 (Apple Silicon only; the Wine build is Intel) ---------------------
if [ "$(uname -m)" = "arm64" ] && ! arch -x86_64 /usr/bin/true 2>/dev/null; then
  echo "Installing Rosetta 2 ..."
  softwareupdate --install-rosetta --agree-to-license
fi

# --- Wine ----------------------------------------------------------------------
# Homebrew's own wine casks were disabled on 2026-09-01 (Gatekeeper); the
# maintainer's tap still ships the same builds.
if ! command -v wine >/dev/null && ! command -v wine64 >/dev/null; then
  echo "Installing Wine (gcenx/wine/wine-stable) ..."
  brew install --cask gcenx/wine/wine-stable
  # Unsigned app: clear the quarantine flag so macOS lets it run.
  xattr -dr com.apple.quarantine "/Applications/Wine Stable.app" 2>/dev/null || true
fi

# --- Mac-side Python for the bot -------------------------------------------------
command -v python3.12 >/dev/null || brew install python@3.12

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
