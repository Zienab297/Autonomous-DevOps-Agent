"""
db/session.py
-------------
SQLAlchemy engine + session factory.

Supports SQLite (default) and PostgreSQL (via DATABASE_URL env var).
SQLite databases are stored per-project so each project has its own history.

Usage:
    from db.session import init_engine, get_session

    init_engine("sqlite:///path/to/project.db")   # SQLite (default)
    init_engine("postgresql+psycopg2://...")       # PostgreSQL (optional)

    with get_session() as session:
        session.add(...)
        # auto-committed on __exit__
"""

import logging
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def _mask_url(url: str) -> str:
    """Return a loggable URL with password masked."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        if p.password:
            masked = p._replace(
                netloc=f"{p.username}:***@{p.hostname}"
                + (f":{p.port}" if p.port else "")
            )
            return urlunparse(masked)
    except Exception:
        pass
    return url.split("@")[-1]


def _normalize_url(url: str) -> str:
    """Normalize PostgreSQL URL to use psycopg2 driver explicitly."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def init_engine(database_url: str, **kwargs) -> Engine:
    """
    Initialize the SQLAlchemy engine.

    - SQLite: uses check_same_thread=False and enables WAL mode for
      safe concurrent reads during monitoring.
    - PostgreSQL: uses connection pooling with sane defaults.

    Safe to call multiple times — reuses existing engine if URL matches.
    """
    global _engine, _SessionLocal

    url = _normalize_url(database_url)

    # Reuse if same URL
    if _engine is not None:
        existing = _normalize_url(str(_engine.url))
        if existing == url:
            logger.debug("[DB] Engine already initialized — reusing.")
            return _engine
        logger.info("[DB] URL changed — disposing old engine.")
        _engine.dispose()

    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        connect_args = {"check_same_thread": False}
        engine_kwargs = {
            "connect_args": connect_args,
            "echo"        : kwargs.pop("echo", False),
            **kwargs,
        }
    else:
        engine_kwargs = {
            "pool_size"    : kwargs.pop("pool_size",    10),
            "max_overflow" : kwargs.pop("max_overflow",  20),
            "pool_pre_ping": kwargs.pop("pool_pre_ping", True),
            "pool_recycle" : kwargs.pop("pool_recycle",  3600),
            "echo"         : kwargs.pop("echo",          False),
            **kwargs,
        }

    _engine = create_engine(url, **engine_kwargs)

    # Enable WAL mode for SQLite — allows concurrent reads while writing
    if is_sqlite:
        @event.listens_for(_engine, "connect")
        def _set_wal(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    _SessionLocal = sessionmaker(
        bind=_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    logger.info("[DB] Engine initialized: %s", _mask_url(url))
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError(
            "Database engine not initialized. "
            "Call db.session.init_engine(url) first."
        )
    return _engine


def get_session_factory() -> sessionmaker:
    if _SessionLocal is None:
        raise RuntimeError(
            "Session factory not initialized. "
            "Call db.session.init_engine(url) first."
        )
    return _SessionLocal


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Context manager that yields a SQLAlchemy Session.
    Commits on success, rolls back on any exception, always closes.

        with get_session() as s:
            s.add(MyModel(...))
    """
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping_db() -> bool:
    """Return True if the database is reachable."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("[DB] ping failed: %s", exc)
        return False


def dispose_engine() -> None:
    """Close all pooled connections. Call at app shutdown."""
    global _engine, _SessionLocal
    if _engine:
        _engine.dispose()
        logger.info("[DB] Engine disposed.")
    _engine = None
    _SessionLocal = None