"""
db/init_db.py
--------------
Creates all PostgreSQL tables on first run — safe to call on every run.

- Uses CREATE TABLE IF NOT EXISTS (via SQLAlchemy checkfirst=True)
- NEVER drops or truncates existing tables
- NEVER deletes existing data
- Safe to call every time `devops` starts

Usage:
    from db.init_db import init_db
    init_db(database_url="postgresql+psycopg2://user:pass@localhost:5432/devops_db")

Or from CLI (first-time setup):
    python -m db.init_db
"""

import logging
import os

from db.base    import Base
from db.session import init_engine, get_engine

# Import all models so Base knows about them before create_all
from models.incident   import IncidentModel     # noqa: F401
from models.deployment import DeploymentModel   # noqa: F401
from models.solution   import SolutionModel     # noqa: F401
from models.action     import ActionModel       # noqa: F401
from models.alert      import AlertModel        # noqa: F401
from models.event      import EventLogModel     # noqa: F401

logger = logging.getLogger(__name__)

# Module-level flag — tracks whether init_db has already run this process
_initialized: bool = False


def init_db(database_url: str | None = None) -> None:
    """
    Initialize the engine and ensure all tables exist.

    - First call: creates the engine + runs CREATE TABLE IF NOT EXISTS
    - Subsequent calls in the same process: skips everything
    - Tables are NEVER dropped or truncated
    - Schema changes require Alembic migrations

    Args:
        database_url: PostgreSQL DSN. Falls back to DATABASE_URL env var.
                      Accepts both "postgresql://" and "postgresql+psycopg2://"
    """
    global _initialized

    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise ValueError(
            "No database URL provided. "
            "Pass database_url= or set the DATABASE_URL environment variable."
        )

    # init_engine normalises the URL and reuses existing engine if URL matches
    init_engine(url)
    engine = get_engine()

    if _initialized:
        logger.debug("[DB] init_db already ran this session — skipping CREATE TABLE.")
        return

    # checkfirst=True → SQLAlchemy emits CREATE TABLE IF NOT EXISTS
    logger.info("[DB] Ensuring tables exist (CREATE TABLE IF NOT EXISTS)...")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    _initialized = True
    logger.info("[DB] Tables ready — existing data preserved.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()
    print("✅ Database initialized — tables ready, existing data preserved.")