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
    init_db(database_url="postgresql://user:pass@localhost:5432/devops_db")

Or from CLI (first-time setup):
    python -m db.init_db
"""

import logging
import os
import sys
from pathlib import Path

# ── Ensure ROOT/models/ (PostgreSQL ORM) is importable even when
#    devops_agent/ is first in sys.path ────────────────────────────────────────
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db.base    import Base
from db.session import init_engine, get_engine

# Import all ORM models so SQLAlchemy Base knows about them before create_all.
# We import them by their full module path to avoid any ambiguity with
# devops_agent/models/ which lives under the same bare name.
import importlib
for _mod in [
    "models.incident",
    "models.deployment",
    "models.solution",
    "models.action",
    "models.alert",
    "models.event",
]:
    importlib.import_module(_mod)

logger = logging.getLogger(__name__)

# Module-level flag — tracks whether init_db has already run this process
_initialized: bool = False


def init_db(database_url: str | None = None) -> None:
    """
    Initialize the engine and ensure all tables exist.

    Behaviour:
    - First call: creates the engine + runs CREATE TABLE IF NOT EXISTS
    - Subsequent calls in the same process: skips everything (engine reused,
      tables already exist — no DB round-trip needed)
    - Tables are NEVER dropped or truncated — all existing rows are preserved
    - Adding new columns in a future version requires a migration (Alembic);
      this function only handles table creation, not schema changes.

    Args:
        database_url: PostgreSQL DSN. Falls back to DATABASE_URL env var.
    """
    global _initialized

    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise ValueError(
            "No database URL provided. "
            "Pass database_url= or set the DATABASE_URL environment variable."
        )

    # init_engine is already idempotent — reuses existing engine if URL matches
    init_engine(url)
    engine = get_engine()

    if _initialized:
        logger.debug("[DB] init_db already ran this session — skipping CREATE TABLE.")
        return

    # checkfirst=True → SQLAlchemy emits CREATE TABLE IF NOT EXISTS
    # so existing tables and their data are left completely untouched.
    logger.info("[DB] Ensuring tables exist (CREATE TABLE IF NOT EXISTS)...")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    _initialized = True
    logger.info("[DB] Tables ready — existing data preserved.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()
    print("✅ Database initialized — tables ready, existing data preserved.")