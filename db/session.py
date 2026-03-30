"""
db/session.py
-------------
SQLAlchemy engine + session factory for PostgreSQL (psycopg2).

Usage:
    from db.session import get_session, init_engine

    init_engine("postgresql+psycopg2://user:pass@localhost:5432/devops_db")

    with get_session() as session:
        session.add(...)
        session.commit()
"""

import logging
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, text
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
            masked = p._replace(netloc=f"{p.username}:***@{p.hostname}" +
                                (f":{p.port}" if p.port else ""))
            return urlunparse(masked)
    except Exception:
        pass
    return url.split("@")[-1]


def init_engine(database_url: str, **kwargs) -> Engine:
    """
    Initialize the SQLAlchemy engine for PostgreSQL.
    Safe to call multiple times — reuses existing engine if URL matches.

    Args:
        database_url: PostgreSQL DSN with psycopg2 driver, e.g.
                      "postgresql+psycopg2://user:pass@localhost:5432/devops_db"
                      Plain "postgresql://..." is also accepted and normalized.
        **kwargs: Extra create_engine kwargs (pool_size, echo, etc.)

    Returns:
        The configured Engine instance.
    """
    global _engine, _SessionLocal

    # Normalise driver: ensure psycopg2 is explicit
    url = database_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]

    # ── Already initialized with the same URL → reuse ─────────────────────
    if _engine is not None:
        existing_url = str(_engine.url)
        # Normalise existing URL for comparison
        existing_norm = existing_url
        if existing_norm.startswith("postgresql://"):
            existing_norm = "postgresql+psycopg2://" + existing_norm[len("postgresql://"):]
        if existing_norm == url:
            logger.debug("[DB] Engine already initialized — reusing existing engine.")
            return _engine
        # Different URL — dispose old engine before creating new one
        logger.info("[DB] URL changed — disposing old engine and creating new one.")
        _engine.dispose()

    engine_kwargs = {
        "pool_size"     : kwargs.pop("pool_size",    10),
        "max_overflow"  : kwargs.pop("max_overflow",  20),
        "pool_pre_ping" : kwargs.pop("pool_pre_ping", True),
        "pool_recycle"  : kwargs.pop("pool_recycle",  3600),
        "echo"          : kwargs.pop("echo",          False),
        **kwargs,
    }

    _engine = create_engine(url, **engine_kwargs)
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
            "Call db.session.init_engine(DATABASE_URL) first."
        )
    return _engine


def get_session_factory() -> sessionmaker:
    if _SessionLocal is None:
        raise RuntimeError(
            "Session factory not initialized. "
            "Call db.session.init_engine(DATABASE_URL) first."
        )
    return _SessionLocal


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Context manager that yields a SQLAlchemy Session.
    Commits on success, rolls back on any exception, always closes.

    Usage:
        with get_session() as session:
            session.add(MyModel(...))
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
    """Return True if the database is reachable, False otherwise."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
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