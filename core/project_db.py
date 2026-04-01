"""
core/project_db.py
-------------------
ProjectDB — the single database manager for the Autonomous DevOps Agent.

Design goals:
  • One SQLite file per project, stored at <project_root>/.devops/history.db
  • Drop-in compatible with PostgreSQL: set DATABASE_URL env var for Postgres
  • ORM-based (SQLAlchemy) — easy to swap backends via URL
  • Exposes a clean query API for InteractiveCLI and agents
  • All writes are fire-and-forget (exceptions logged, never crash the pipeline)

Usage:
    from core.project_db import ProjectDB

    db = ProjectDB.for_project("/path/to/my-project")
    orchestrator.set_project_db(db)

Query API (used by InteractiveCLI):
    db.get_all_incidents()              → List[dict]
    db.get_active_incidents()           → List[dict]
    db.get_solutions_for_incident(id)   → List[dict]
    db.get_actions_for_incident(id)     → List[dict]
    db.get_all_deployments()            → List[dict]
    db.get_deployments_for_service(svc) → List[dict]
    db.get_events(...)                  → List[dict]
    db.get_alerts_for_incident(id)      → List[dict]
    db.get_summary()                    → dict
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(val, default: str = "") -> str:
    if val is None:
        return default
    if hasattr(val, "value"):       # enum
        return str(val.value)
    return str(val)


def _safe_json(val) -> str:
    try:
        return json.dumps(val, default=str)
    except Exception:
        return "[]"


def _row_to_dict(row: sqlite3.Row) -> Dict:
    return dict(row)


def _orm_to_dict(row) -> Dict:
    """SQLAlchemy ORM row → plain dict with ISO timestamps."""
    if row is None:
        return {}
    result = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name, None)
        result[col.name] = val.isoformat() if hasattr(val, "isoformat") else val
    return result


def _orm_available() -> bool:
    """
    Check whether all ORM dependencies are importable BEFORE trying to
    initialise the engine.  Returns False silently if anything is missing.
    """
    try:
        import sqlalchemy          # noqa: F401
        # Verify the project's own ORM models are on sys.path
        from db.init_db import init_db  # noqa: F401
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# _SQLiteBackend  (zero extra dependencies — always available)
# ─────────────────────────────────────────────────────────────────────────────

# NOTE: "references" is a reserved word in SQL — the column is named
#       "ref_urls" to avoid a sqlite3.OperationalError.

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS incidents (
        incident_id  TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL,
        service      TEXT DEFAULT '',
        severity     TEXT DEFAULT 'medium',
        status       TEXT DEFAULT 'open',
        description  TEXT DEFAULT '',
        metadata     TEXT DEFAULT '{}',
        created_at   TEXT NOT NULL,
        resolved_at  TEXT
    );
    CREATE TABLE IF NOT EXISTS solutions (
        id              TEXT PRIMARY KEY,
        incident_id     TEXT NOT NULL,
        project_id      TEXT NOT NULL,
        root_cause      TEXT DEFAULT '',
        healing_prompt  TEXT DEFAULT '',
        source          TEXT DEFAULT '',
        confidence      REAL DEFAULT 0.0,
        commands        TEXT DEFAULT '[]',
        ref_urls        TEXT DEFAULT '[]',
        created_at      TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS actions (
        id           TEXT PRIMARY KEY,
        incident_id  TEXT NOT NULL,
        project_id   TEXT NOT NULL,
        action_type  TEXT DEFAULT '',
        description  TEXT DEFAULT '',
        status       TEXT DEFAULT 'pending',
        result       TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS deployments (
        id           TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL,
        service      TEXT DEFAULT '',
        repo_url     TEXT DEFAULT '',
        status       TEXT DEFAULT 'unknown',
        framework    TEXT DEFAULT '',
        language     TEXT DEFAULT '',
        files_count  INTEGER DEFAULT 0,
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id   TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        source       TEXT DEFAULT '',
        incident_id  TEXT DEFAULT '',
        data         TEXT DEFAULT '{}',
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id  TEXT NOT NULL,
        project_id   TEXT NOT NULL,
        title        TEXT DEFAULT '',
        message      TEXT DEFAULT '',
        severity     TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_inc_project  ON incidents(project_id);
    CREATE INDEX IF NOT EXISTS idx_sol_incident ON solutions(incident_id);
    CREATE INDEX IF NOT EXISTS idx_evt_project  ON events(project_id);
    CREATE INDEX IF NOT EXISTS idx_dep_project  ON deployments(project_id);
    CREATE INDEX IF NOT EXISTS idx_act_incident ON actions(incident_id);
    CREATE INDEX IF NOT EXISTS idx_alt_incident ON alerts(incident_id);
"""


class _SQLiteBackend:
    """Pure-sqlite3 backend. Used for SQLite URLs and as an ORM fallback."""

    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode: allows reads while a write is in progress
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("[ProjectDB/SQLite] Connected → %s", db_path)

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_incident(self, obj, project_id: str) -> None:
        try:
            if isinstance(obj, dict):
                inc_id      = obj.get("incident_id") or obj.get("id", "")
                service     = obj.get("service", "")
                severity    = obj.get("severity", "medium")
                status      = obj.get("status", "open")
                description = obj.get("description", "")
                metadata    = _safe_json(obj.get("metadata", {}))
            else:
                inc_id      = getattr(obj, "incident_id", None) or getattr(obj, "id", "")
                service     = _safe_str(getattr(obj, "service", ""))
                severity    = _safe_str(getattr(obj, "severity", "medium"))
                status      = _safe_str(getattr(obj, "status", "open"))
                description = _safe_str(getattr(obj, "description", ""))
                metadata    = _safe_json(getattr(obj, "metadata", {}))

            if not inc_id:
                return

            self._conn.execute("""
                INSERT INTO incidents
                    (incident_id, project_id, service, severity, status,
                     description, metadata, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    status      = excluded.status,
                    severity    = excluded.severity,
                    description = excluded.description,
                    metadata    = excluded.metadata
            """, (str(inc_id), project_id, service, severity,
                  status, description, metadata, _now_iso()))
            self._conn.commit()
        except Exception as e:
            logger.error("[ProjectDB/SQLite] save_incident: %s", e)

    def update_incident_status(self, incident_id: str, status: str) -> None:
        try:
            resolved_at = _now_iso() if status in ("resolved", "RESOLVED") else None
            if resolved_at:
                self._conn.execute(
                    "UPDATE incidents SET status=?, resolved_at=? WHERE incident_id=?",
                    (status, resolved_at, incident_id))
            else:
                self._conn.execute(
                    "UPDATE incidents SET status=? WHERE incident_id=?",
                    (status, incident_id))
            self._conn.commit()
        except Exception as e:
            logger.error("[ProjectDB/SQLite] update_incident_status: %s", e)

    def save_solution(self, obj, project_id: str) -> None:
        try:
            import uuid
            if isinstance(obj, dict):
                sol_id     = obj.get("id", str(uuid.uuid4())[:12])
                inc_id     = obj.get("incident_id", "")
                root_cause = obj.get("root_cause", "")
                prompt     = obj.get("healing_prompt", "")
                source     = obj.get("source", "")
                confidence = float(obj.get("confidence", 0.0))
                commands   = _safe_json(obj.get("suggested_commands", []))
                refs       = _safe_json(obj.get("references", []))
            else:
                sol_id     = str(getattr(obj, "id", None) or uuid.uuid4())[:12]
                inc_id     = _safe_str(getattr(obj, "incident_id", ""))
                root_cause = _safe_str(getattr(obj, "root_cause", ""))
                prompt     = _safe_str(getattr(obj, "healing_prompt", ""))
                source     = _safe_str(getattr(obj, "source", ""))
                confidence = float(getattr(obj, "confidence", 0.0))
                commands   = _safe_json(getattr(obj, "suggested_commands", []))
                refs       = _safe_json(getattr(obj, "references", []))

            self._conn.execute("""
                INSERT OR REPLACE INTO solutions
                    (id, incident_id, project_id, root_cause, healing_prompt,
                     source, confidence, commands, ref_urls, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (sol_id, inc_id, project_id, root_cause, prompt,
                  source, confidence, commands, refs, _now_iso()))
            self._conn.commit()
        except Exception as e:
            logger.error("[ProjectDB/SQLite] save_solution: %s", e)

    def save_action(self, obj, project_id: str, incident_id: str) -> None:
        try:
            import uuid
            if isinstance(obj, dict):
                act_id      = obj.get("id", str(uuid.uuid4())[:12])
                action_type = obj.get("action_type", "")
                description = obj.get("description", "")
                status      = obj.get("status", "pending")
                result      = obj.get("result", "")
            else:
                act_id      = _safe_str(getattr(obj, "id", str(uuid.uuid4())[:12]))
                action_type = _safe_str(getattr(obj, "action_type", ""))
                description = _safe_str(getattr(obj, "description", ""))
                status      = _safe_str(getattr(obj, "status", "pending"))
                result      = _safe_str(getattr(obj, "result", ""))

            self._conn.execute("""
                INSERT OR REPLACE INTO actions
                    (id, incident_id, project_id, action_type, description,
                     status, result, created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (act_id, incident_id, project_id, action_type,
                  description, status, result, _now_iso()))
            self._conn.commit()
        except Exception as e:
            logger.error("[ProjectDB/SQLite] save_action: %s", e)

    def save_deployment(self, obj, project_id: str) -> None:
        try:
            import uuid
            if isinstance(obj, dict):
                dep_id      = obj.get("id", str(uuid.uuid4())[:12])
                service     = obj.get("service", "")
                repo_url    = obj.get("repo_url", "")
                status      = obj.get("status", "unknown")
                framework   = obj.get("framework", "")
                language    = obj.get("language", "")
                files_count = int(obj.get("files_count", 0))
            else:
                dep_id      = _safe_str(getattr(obj, "id", str(uuid.uuid4())[:12]))
                service     = _safe_str(getattr(obj, "service", ""))
                repo_url    = _safe_str(getattr(obj, "repo_url", ""))
                status      = _safe_str(getattr(obj, "status", "unknown"))
                framework   = _safe_str(getattr(obj, "framework", ""))
                language    = _safe_str(getattr(obj, "language", ""))
                files_count = int(getattr(obj, "files_count", 0))

            self._conn.execute("""
                INSERT OR REPLACE INTO deployments
                    (id, project_id, service, repo_url, status, framework,
                     language, files_count, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (dep_id, project_id, service, repo_url, status,
                  framework, language, files_count, _now_iso()))
            self._conn.commit()
        except Exception as e:
            logger.error("[ProjectDB/SQLite] save_deployment: %s", e)

    def log_event(self, obj, project_id: str) -> None:
        try:
            if isinstance(obj, dict):
                event_type  = str(obj.get("type", ""))
                source      = str(obj.get("source", ""))
                incident_id = str(obj.get("incident_id", "") or "")
                data        = _safe_json(obj.get("data", {}))
            else:
                etype       = getattr(obj, "type", "")
                event_type  = etype.value if hasattr(etype, "value") else str(etype)
                source      = _safe_str(getattr(obj, "source", ""))
                incident_id = _safe_str(getattr(obj, "incident_id", "") or "")
                data        = _safe_json(getattr(obj, "data", {}))

            self._conn.execute("""
                INSERT INTO events
                    (project_id, event_type, source, incident_id, data, created_at)
                VALUES (?,?,?,?,?,?)
            """, (project_id, event_type, source, incident_id, data, _now_iso()))
            self._conn.commit()
        except Exception as e:
            logger.debug("[ProjectDB/SQLite] log_event: %s", e)

    def save_alert(self, obj, project_id: str, incident_id: str) -> None:
        try:
            if isinstance(obj, dict):
                title    = obj.get("title", "")
                message  = obj.get("message", "")
                severity = obj.get("severity", "")
            else:
                title    = _safe_str(getattr(obj, "title", ""))
                message  = _safe_str(getattr(obj, "message", ""))
                severity = _safe_str(getattr(obj, "severity", ""))

            self._conn.execute("""
                INSERT INTO alerts
                    (incident_id, project_id, title, message, severity, created_at)
                VALUES (?,?,?,?,?,?)
            """, (incident_id, project_id, title, message, severity, _now_iso()))
            self._conn.commit()
        except Exception as e:
            logger.error("[ProjectDB/SQLite] save_alert: %s", e)

    # ── Query ─────────────────────────────────────────────────────────────────

    def list_incidents(self, project_id: str, limit: int = 200) -> List[Dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM incidents WHERE project_id=? "
                "ORDER BY created_at DESC LIMIT ?", (project_id, limit))
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error("[ProjectDB/SQLite] list_incidents: %s", e)
            return []

    def list_active_incidents(self, project_id: str) -> List[Dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM incidents WHERE project_id=? "
                "AND status NOT IN ('resolved','RESOLVED','failed','FAILED') "
                "ORDER BY created_at DESC", (project_id,))
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error("[ProjectDB/SQLite] list_active_incidents: %s", e)
            return []

    def list_solutions(self, project_id: str, incident_id: str) -> List[Dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM solutions WHERE project_id=? AND incident_id=? "
                "ORDER BY created_at DESC", (project_id, incident_id))
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error("[ProjectDB/SQLite] list_solutions: %s", e)
            return []

    def list_actions(self, project_id: str, incident_id: str) -> List[Dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM actions WHERE project_id=? AND incident_id=? "
                "ORDER BY created_at DESC", (project_id, incident_id))
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error("[ProjectDB/SQLite] list_actions: %s", e)
            return []

    def list_deployments(self, project_id: str, limit: int = 100) -> List[Dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM deployments WHERE project_id=? "
                "ORDER BY created_at DESC LIMIT ?", (project_id, limit))
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error("[ProjectDB/SQLite] list_deployments: %s", e)
            return []

    def list_events(
        self,
        project_id  : str,
        event_type  : Optional[str] = None,
        incident_id : Optional[str] = None,
        limit       : int = 100,
    ) -> List[Dict]:
        try:
            if event_type and incident_id:
                cur = self._conn.execute(
                    "SELECT * FROM events WHERE project_id=? AND event_type=? "
                    "AND incident_id=? ORDER BY created_at DESC LIMIT ?",
                    (project_id, event_type, incident_id, limit))
            elif event_type:
                cur = self._conn.execute(
                    "SELECT * FROM events WHERE project_id=? AND event_type=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (project_id, event_type, limit))
            elif incident_id:
                cur = self._conn.execute(
                    "SELECT * FROM events WHERE project_id=? AND incident_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (project_id, incident_id, limit))
            else:
                cur = self._conn.execute(
                    "SELECT * FROM events WHERE project_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit))
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error("[ProjectDB/SQLite] list_events: %s", e)
            return []

    def list_alerts(self, project_id: str, incident_id: str) -> List[Dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM alerts WHERE project_id=? AND incident_id=? "
                "ORDER BY created_at DESC", (project_id, incident_id))
            return [_row_to_dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error("[ProjectDB/SQLite] list_alerts: %s", e)
            return []

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# _ORMBackend  (SQLAlchemy + repositories/ — used for PostgreSQL only)
# ─────────────────────────────────────────────────────────────────────────────

class _ORMBackend:
    """
    SQLAlchemy-backed storage that delegates to repositories/.
    Only instantiated when _orm_available() returns True.
    """

    def __init__(self, database_url: str):
        from db.init_db import init_db
        init_db(database_url)
        logger.info("[ProjectDB/ORM] Ready  url=%s", database_url[:40])

    def _session(self):
        from db.session import get_session
        return get_session()

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_incident(self, obj, project_id: str) -> None:
        try:
            from repositories.incident_repo import IncidentRepository
            with self._session() as s:
                IncidentRepository(s).create(obj, project_id)
        except Exception as e:
            logger.error("[ProjectDB/ORM] save_incident: %s", e)

    def update_incident_status(self, incident_id: str, status: str) -> None:
        try:
            from repositories.incident_repo import IncidentRepository
            with self._session() as s:
                IncidentRepository(s).update_status(incident_id, status)
        except Exception as e:
            logger.error("[ProjectDB/ORM] update_incident_status: %s", e)

    def save_solution(self, obj, project_id: str) -> None:
        try:
            incident_id = getattr(obj, "incident_id", None) or (
                obj.get("incident_id") if isinstance(obj, dict) else None)
            if not incident_id:
                logger.warning("[ProjectDB/ORM] save_solution: no incident_id")
                return
            from repositories.solution_repo import SolutionRepository
            with self._session() as s:
                SolutionRepository(s).create(obj, project_id, incident_id)
        except Exception as e:
            logger.error("[ProjectDB/ORM] save_solution: %s", e)

    def save_action(self, obj, project_id: str, incident_id: str) -> None:
        try:
            from repositories.action_repo import ActionRepository
            with self._session() as s:
                ActionRepository(s).create(obj, project_id, incident_id)
        except Exception as e:
            logger.error("[ProjectDB/ORM] save_action: %s", e)

    def save_deployment(self, obj, project_id: str) -> None:
        try:
            from repositories.deployment_repo import DeploymentRepository
            with self._session() as s:
                DeploymentRepository(s).create_or_update(obj, project_id)
        except Exception as e:
            logger.error("[ProjectDB/ORM] save_deployment: %s", e)

    def log_event(self, obj, project_id: str) -> None:
        try:
            from repositories.event_repo import EventRepository
            with self._session() as s:
                EventRepository(s).create(obj, project_id)
        except Exception as e:
            logger.debug("[ProjectDB/ORM] log_event: %s", e)

    def save_alert(self, obj, project_id: str, incident_id: str) -> None:
        try:
            from repositories.alert_repo import AlertRepository
            with self._session() as s:
                AlertRepository(s).create(obj, project_id, incident_id)
        except Exception as e:
            logger.error("[ProjectDB/ORM] save_alert: %s", e)

    # ── Query ─────────────────────────────────────────────────────────────────

    def _q(self, fn) -> List[Dict]:
        try:
            with self._session() as s:
                return [_orm_to_dict(r) for r in fn(s)]
        except Exception as e:
            logger.error("[ProjectDB/ORM] query: %s", e)
            return []

    def list_incidents(self, project_id: str, limit: int = 200) -> List[Dict]:
        from repositories.incident_repo import IncidentRepository
        return self._q(lambda s: IncidentRepository(s).list_by_project(project_id, limit=limit))

    def list_active_incidents(self, project_id: str) -> List[Dict]:
        from repositories.incident_repo import IncidentRepository
        return self._q(lambda s: IncidentRepository(s).list_active(project_id))

    def list_solutions(self, project_id: str, incident_id: str) -> List[Dict]:
        from repositories.solution_repo import SolutionRepository
        return self._q(lambda s: SolutionRepository(s).list_by_incident(project_id, incident_id))

    def list_actions(self, project_id: str, incident_id: str) -> List[Dict]:
        from repositories.action_repo import ActionRepository
        return self._q(lambda s: ActionRepository(s).list_by_incident(project_id, incident_id))

    def list_deployments(self, project_id: str, limit: int = 100) -> List[Dict]:
        from repositories.deployment_repo import DeploymentRepository
        return self._q(lambda s: DeploymentRepository(s).list_recent(project_id, limit=limit))

    def list_events(
        self,
        project_id  : str,
        event_type  : Optional[str] = None,
        incident_id : Optional[str] = None,
        limit       : int = 100,
    ) -> List[Dict]:
        from repositories.event_repo import EventRepository
        return self._q(
            lambda s: EventRepository(s).list_by_project(
                project_id,
                event_type  = event_type,
                incident_id = incident_id,
                limit       = limit,
            )
        )

    def list_alerts(self, project_id: str, incident_id: str) -> List[Dict]:
        from repositories.alert_repo import AlertRepository
        return self._q(lambda s: AlertRepository(s).list_by_incident(project_id, incident_id))

    def close(self) -> None:
        pass   # engine managed globally by db/session.py


# ─────────────────────────────────────────────────────────────────────────────
# ProjectDB — public facade
# ─────────────────────────────────────────────────────────────────────────────

class ProjectDB:
    """
    Public database facade used by the orchestrator and InteractiveCLI.

    Backend selection (automatic):
      • sqlite:// URL  →  _SQLiteBackend  (no extra deps needed)
      • other URL      →  _ORMBackend if all deps available,
                          else _SQLiteBackend as safe fallback
    """

    def __init__(self, project_id: str, database_url: str):
        self.project_id   = project_id
        self.database_url = database_url
        self._backend     = self._pick_backend(database_url)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def for_project(
        cls,
        project_path: "os.PathLike | str",
        database_url: Optional[str] = None,
    ) -> "ProjectDB":
        """
        Build (or open) a ProjectDB for the given project directory.

        SQLite (default): <project_path>/.devops/history.db
        PostgreSQL:       pass database_url or set DATABASE_URL env var.

        The project_id is the folder name (e.g. "test" or "my-app").
        """
        project_path = Path(project_path).resolve()
        project_id   = project_path.name

        url = database_url or os.getenv("DATABASE_URL", "").strip()

        if not url:
            devops_dir = project_path / ".devops"
            devops_dir.mkdir(parents=True, exist_ok=True)
            db_path = devops_dir / "history.db"
            url     = f"sqlite:///{db_path}"
            logger.info(
                "[ProjectDB] SQLite → %s  project_id=%s", db_path, project_id
            )
        else:
            if url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            logger.info("[ProjectDB] PostgreSQL  project_id=%s", project_id)

        return cls(project_id=project_id, database_url=url)

    # ── Backend picker ────────────────────────────────────────────────────────

    @staticmethod
    def _pick_backend(database_url: str):
        """
        Select the best available backend.

        Rule:
          1. sqlite:///  → always _SQLiteBackend (fast, no deps)
          2. other URL   → _ORMBackend only if _orm_available() is True
                         → otherwise _SQLiteBackend with a local file path
                           derived from the URL, or in-memory as last resort
        """
        # ── SQLite path (most common case) ────────────────────────────────
        if database_url.startswith("sqlite:///"):
            db_path = database_url[len("sqlite:///"):]
            return _SQLiteBackend(db_path)

        # ── PostgreSQL / other ────────────────────────────────────────────
        # Only try ORM if all required packages are importable.
        # This prevents the ModuleNotFoundError from crashing the pipeline
        # when models/ or repositories/ are not fully wired up.
        if _orm_available():
            try:
                return _ORMBackend(database_url)
            except Exception as exc:
                logger.warning(
                    "[ProjectDB] ORM init failed (%s) — falling back to SQLite", exc
                )

        logger.warning(
            "[ProjectDB] ORM dependencies missing — "
            "falling back to in-memory SQLite (data not persisted across runs)"
        )
        return _SQLiteBackend(":memory:")

    # ── Write API ─────────────────────────────────────────────────────────────

    def save_incident(self, incident_obj) -> None:
        try:
            self._backend.save_incident(incident_obj, self.project_id)
        except Exception as e:
            logger.error("[ProjectDB] save_incident: %s", e)

    def update_incident_status(self, incident_id: str, status: str) -> None:
        try:
            self._backend.update_incident_status(incident_id, status)
        except Exception as e:
            logger.error("[ProjectDB] update_incident_status: %s", e)

    def save_solution(self, solution_obj) -> None:
        incident_id = getattr(solution_obj, "incident_id", None) or (
            solution_obj.get("incident_id") if isinstance(solution_obj, dict) else None
        )
        if not incident_id:
            logger.warning("[ProjectDB] save_solution: no incident_id — skipping")
            return
        try:
            self._backend.save_solution(solution_obj, self.project_id)
        except Exception as e:
            logger.error("[ProjectDB] save_solution: %s", e)

    def save_action(self, action_obj, incident_id: str) -> None:
        try:
            self._backend.save_action(action_obj, self.project_id, incident_id)
        except Exception as e:
            logger.error("[ProjectDB] save_action: %s", e)

    def save_deployment(self, dep_obj) -> None:
        try:
            self._backend.save_deployment(dep_obj, self.project_id)
        except Exception as e:
            logger.error("[ProjectDB] save_deployment: %s", e)

    def log_event(self, event_obj) -> None:
        """Called by orchestrator.set_project_db() on every EventBus publish."""
        try:
            self._backend.log_event(event_obj, self.project_id)
        except Exception as e:
            logger.debug("[ProjectDB] log_event: %s", e)

    def save_alert(self, alert_obj, incident_id: str) -> None:
        try:
            self._backend.save_alert(alert_obj, self.project_id, incident_id)
        except Exception as e:
            logger.error("[ProjectDB] save_alert: %s", e)

    # ── Query API (used by InteractiveCLI) ────────────────────────────────────

    def get_all_incidents(self) -> List[Dict]:
        return self._backend.list_incidents(self.project_id)

    def get_active_incidents(self) -> List[Dict]:
        return self._backend.list_active_incidents(self.project_id)

    def get_solutions_for_incident(self, incident_id: str) -> List[Dict]:
        return self._backend.list_solutions(self.project_id, incident_id)

    def get_actions_for_incident(self, incident_id: str) -> List[Dict]:
        return self._backend.list_actions(self.project_id, incident_id)

    def get_all_deployments(self) -> List[Dict]:
        return self._backend.list_deployments(self.project_id)

    def get_deployments_for_service(self, service: str) -> List[Dict]:
        return [
            d for d in self._backend.list_deployments(self.project_id)
            if d.get("service") == service
        ]

    def get_events(
        self,
        event_type  : Optional[str] = None,
        incident_id : Optional[str] = None,
        limit       : int = 100,
    ) -> List[Dict]:
        return self._backend.list_events(
            self.project_id,
            event_type  = event_type,
            incident_id = incident_id,
            limit       = limit,
        )

    def get_alerts_for_incident(self, incident_id: str) -> List[Dict]:
        return self._backend.list_alerts(self.project_id, incident_id)

    def get_summary(self) -> Dict:
        incidents   = self.get_all_incidents()
        deployments = self.get_all_deployments()
        return {
            "project_id"          : self.project_id,
            "total_incidents"     : len(incidents),
            "open_incidents"      : sum(1 for i in incidents if i.get("status") == "open"),
            "resolved_incidents"  : sum(1 for i in incidents if i.get("status") == "resolved"),
            "total_deployments"   : len(deployments),
            "success_deployments" : sum(1 for d in deployments if d.get("status") == "success"),
            "failed_deployments"  : sum(
                1 for d in deployments if d.get("status") in ("failed", "failure")
            ),
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        try:
            self._backend.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return (
            f"ProjectDB(project_id={self.project_id!r}, "
            f"backend={type(self._backend).__name__})"
        )