import logging
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from config.settings import get_db_settings

logger = logging.getLogger(__name__)

_engine = None
_Session = None

_SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "schema.sql"

# Idempotent migrations applied on every startup (safe to re-run).
_MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS tv_recommendation NUMERIC(5, 4)",
]


def _url(db: dict, name: str | None = None) -> str:
    name = name or db["name"]
    return (
        f"postgresql+psycopg2://{db['user']}:{db['password']}"
        f"@{db['host']}:{db['port']}/{name}"
    )


def get_engine():
    global _engine
    if _engine is None:
        db = get_db_settings()
        _engine = create_engine(
            _url(db), poolclass=QueuePool, pool_size=5, max_overflow=10,
            pool_pre_ping=True,
        )
    return _engine


def get_session():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine())
    return _Session()


def _ensure_database_exists(db: dict):
    """Create the target database if it doesn't exist (connects to 'postgres')."""
    admin = create_engine(_url(db, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db["name"]}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db["name"]}"'))
                logger.info(f"Created database '{db['name']}'")
    finally:
        admin.dispose()


def init_db():
    """
    Make the database usable with zero manual steps:
      1. create the database if missing
      2. apply schema.sql (idempotent — all CREATE ... IF NOT EXISTS)
      3. run idempotent migrations
    """
    db = get_db_settings()
    try:
        _ensure_database_exists(db)
    except Exception as e:
        logger.warning(f"Could not auto-create database (continuing): {e}")

    engine = get_engine()
    if _SCHEMA_PATH.exists():
        sql = _SCHEMA_PATH.read_text()
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
        logger.info("Schema applied")

    with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            conn.exec_driver_sql(stmt)
    logger.info("Database ready")
