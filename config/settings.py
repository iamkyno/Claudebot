"""
Unified configuration loader.

Resolution order for every secret/credential:
    1. Environment variable  (e.g. BINANCE_API_KEY)
    2. config/secrets.yaml    (optional — never required)
    3. Safe built-in default

This means the bot runs in paper mode with ZERO manual setup: no secrets.yaml,
no exported variables. Add API keys only when you want to go live.
"""

import os
from pathlib import Path

import yaml

_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _ROOT / "config" / "config.yaml"
_SECRETS_PATH = _ROOT / "config" / "secrets.yaml"

# Load a local .env file if present so credentials can live there instead of
# being exported manually. Optional — absence is fine.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass


def _load_yaml(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _clean(value) -> str:
    """Treat placeholder values (YOUR_..., empty) as not-set."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.upper().startswith("YOUR_"):
        return ""
    return s


def get_config() -> dict:
    return _load_yaml(_CONFIG_PATH)


def get_secrets() -> dict:
    """Binance + Telegram credentials, env-vars first, then yaml, then empty."""
    y = _load_yaml(_SECRETS_PATH)
    b = y.get("binance", {}) or {}
    t = y.get("telegram", {}) or {}
    return {
        "binance": {
            "api_key": _clean(os.getenv("BINANCE_API_KEY", b.get("api_key"))),
            "api_secret": _clean(os.getenv("BINANCE_API_SECRET", b.get("api_secret"))),
        },
        "telegram": {
            "token": _clean(os.getenv("TELEGRAM_TOKEN", t.get("token"))),
            "chat_id": _clean(os.getenv("TELEGRAM_CHAT_ID", t.get("chat_id"))),
        },
    }


def get_db_settings() -> dict:
    """
    Database connection settings.
    host/port/name come from config.yaml; user/password from secrets.yaml.
    All overridable by env vars; all have local-dev defaults.
    """
    cfg = get_config().get("database", {}) or {}
    sec = _load_yaml(_SECRETS_PATH).get("database", {}) or {}
    return {
        "host": os.getenv("DB_HOST", cfg.get("host") or "localhost"),
        "port": int(os.getenv("DB_PORT", cfg.get("port") or 5432)),
        "name": os.getenv("DB_NAME", cfg.get("name") or "claudebot"),
        "user": os.getenv("DB_USER", sec.get("user") or "postgres"),
        "password": os.getenv("DB_PASSWORD", sec.get("password") if sec.get("password") is not None else "postgres"),
    }


def has_binance_keys() -> bool:
    b = get_secrets()["binance"]
    return bool(b["api_key"] and b["api_secret"])
