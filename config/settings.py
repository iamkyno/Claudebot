"""Config loader: config/config.yaml for behaviour, .env / environment for credentials."""

from __future__ import annotations

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


def exness_credentials() -> dict:
    """From .env. EXN_PRIVATE_KEY may instead be a file path in EXN_PRIVATE_KEY_FILE (PEM)."""
    key = _env("EXN_PRIVATE_KEY")
    key_file = _env("EXN_PRIVATE_KEY_FILE")
    if not key and key_file:
        key = Path(key_file).expanduser().read_text(encoding="utf-8")
    return {
        "api_key": _env("EXN_API_KEY"),
        "private_key": key,
        "account_id": _env("EXN_ACCOUNT_ID"),
        "gateway": _env("EXN_DISCOVERY_BASE_URL"),
    }


def imessage_recipient() -> str:
    return _env("IMESSAGE_TO")
