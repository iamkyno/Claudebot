import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from pathlib import Path

_engine = None
_Session = None


def _load_config():
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    secrets_path = Path(__file__).parent.parent / "config" / "secrets.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f)
    return config, secrets


def get_engine():
    global _engine
    if _engine is None:
        config, secrets = _load_config()
        db = config["database"]
        creds = secrets["database"]
        url = (
            f"postgresql+psycopg2://{creds['user']}:{creds['password']}"
            f"@{db['host']}:{db['port']}/{db['name']}"
        )
        _engine = create_engine(url, poolclass=QueuePool, pool_size=5, max_overflow=10)
    return _engine


def get_session():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine())
    return _Session()
