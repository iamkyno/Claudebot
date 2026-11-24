"""Config loader: config/config.yaml for behaviour, .env / environment for credentials."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.yaml"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def load_config(path: Path | str | None = None) -> dict:
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def mt5_credentials() -> dict:
    """Blank login means: attach to the terminal that is already logged in."""
    login = _env("MT5_LOGIN")
    return {
        "login": int(login) if login else None,
        "password": _env("MT5_PASSWORD"),
        "server": _env("MT5_SERVER"),
        "path": _env("MT5_PATH"),
    }


def bridge_settings(cfg: dict) -> dict:
    b = (cfg.get("mt5") or {}).get("bridge") or {}
    return {
        "host": _env("MT5_BRIDGE_HOST") or b.get("host", "127.0.0.1"),
        "port": int(_env("MT5_BRIDGE_PORT") or b.get("port", 18812)),
        "key": _env("MT5_BRIDGE_KEY"),
    }


def imessage_recipient() -> str:
    return _env("IMESSAGE_TO")
