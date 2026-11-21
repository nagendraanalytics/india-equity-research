import os
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_URL = "postgresql+psycopg://postgres@localhost:5432/ier"


def database_url() -> str:
    return os.environ.get("IER_DATABASE_URL", DEFAULT_URL)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


_engine = None
_factory = None


_engine_lock = threading.Lock()


def get_engine():
    """Shared engine, created once (double-checked under a lock, safe for worker threads)."""
    global _engine, _factory
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                # events.emit borrows a second connection per busy worker, so the default pool
                # (5 + 10) runs dry with several workers plus the API: size it generously.
                eng = create_engine(database_url(), future=True, pool_size=_env_int("IER_DB_POOL_SIZE", 20),
                                    max_overflow=_env_int("IER_DB_MAX_OVERFLOW", 20), pool_pre_ping=True)
                _factory = sessionmaker(eng, expire_on_commit=False)
                _engine = eng  # published last: a non-None _engine implies _factory is set
    return _engine


def get_session() -> Session:
    get_engine()
    return _factory()
