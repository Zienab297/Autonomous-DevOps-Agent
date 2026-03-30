"""
core/database.py
----------------
DatabaseManager — Persistent SQLite history for every DevOps project.

A separate .db file is created per project (named after the project folder).
The database captures the full lifecycle of every deployment:
    - deployments   : every pipeline run from start to finish
    - incidents     : every anomaly/alert detected by MonitoringAgent
    - solutions     : every solution produced by KnowledgeAgent
    - remediation_actions : every fix attempted by SelfHealingAgent
    - events_log    : every EventBus event (full audit trail)
    - alerts        : every notification sent by AlertingAgent

Usage (automatic — wired into Orchestrator and devops.py):
    db = DatabaseManager.for_project("/path/to/my-service")
    # db is now writing to: /path/to/my-service/.devops/my-service.db

Manual queries:
    db.get_all_incidents()
    db.get_deployments_for_service("auth-api")
    db.get_solutions_for_incident("INC-ABC123")
    db.get_full_history()          # everything, sorted by time
"""

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat()


def _slug(text: str) -> str:
    """Turn an arbitrary path or name into a safe filename slug."""
    name = Path(text).name or text
    name = re.sub(r"[^\w\-]", "_", name)
    return name.strip("_") or "project"


# ── schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    service       TEXT NOT NULL,
    branch        TEXT NOT NULL,
    version       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    pipeline_url  TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    service     TEXT NOT NULL,
    severity    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    description TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS solutions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  TEXT NOT NULL,
    project      TEXT NOT NULL,
    root_cause   TEXT,
    healing_prompt TEXT,
    confidence   REAL,
    source       TEXT,
    commands_json TEXT,
    references_json TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
);

CREATE TABLE IF NOT EXISTS remediation_actions (
    action_id   TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    project     TEXT NOT NULL,
    command     TEXT,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    output      TEXT,
    error       TEXT,
    executed_at TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
);

CREATE TABLE IF NOT EXISTS events_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    source      TEXT,
    incident_id TEXT,
    project     TEXT NOT NULL,
    data_json   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id    TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    project     TEXT NOT NULL,
    title       TEXT,
    message     TEXT,
    severity    TEXT,
    channel     TEXT,
    sent        INTEGER DEFAULT 0,
    sent_at     TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
);

CREATE INDEX IF NOT EXISTS idx_incidents_project  ON incidents(project);
CREATE INDEX IF NOT EXISTS idx_incidents_service  ON incidents(service);
CREATE INDEX IF NOT EXISTS idx_incidents_status   ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_events_type        ON events_log(event_type);
CREATE INDEX IF NOT EXISTS idx_events_project     ON events_log(project);
CREATE INDEX IF NOT EXISTS idx_deployments_project ON deployments(project);
CREATE INDEX IF NOT EXISTS idx_solutions_incident  ON solutions(incident_id);
CREATE INDEX IF NOT EXISTS idx_actions_incident    ON remediation_actions(incident_id);
"""


# ── DatabaseManager ───────────────────────────────────────────────────────────

class DatabaseManager:
    """
    Persistent SQLite store for all DevOps history belonging to ONE project.

    Class method:
        db = DatabaseManager.for_project("/path/to/my-project")
        # Creates .devops/my-project.db inside the project folder
        # and returns a ready-to-use DatabaseManager.

    Direct construction:
        db = DatabaseManager(db_path=Path("/custom/path/history.db"), project="my-project")
    """

    def __init__(self, db_path: Path, project: str):
        self.db_path = db_path
        self.project = project
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()

    # ── factory ──────────────────────────────────────────────────────────────

    @classmethod
    def for_project(cls, project_path: str) -> "DatabaseManager":
        """
        Create (or open) the history database for a project.

        The database is stored at:
            {project_path}/.devops/{project_slug}.db

        Args:
            project_path: Absolute or relative path to the project root.

        Returns:
            A connected DatabaseManager instance.
        """
        root   = Path(project_path).resolve()
        slug   = _slug(str(root))
        db_dir = root / ".devops"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / f"{slug}.db"
        logger.info(f"[DatabaseManager] Opening DB: {db_path}")
        return cls(db_path=db_path, project=slug)

    # ── connection ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,   # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        logger.info(f"[DatabaseManager] Schema ready — project='{self.project}'")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        try:
            return self._conn.execute(sql, params)
        except sqlite3.Error as e:
            logger.error(f"[DatabaseManager] SQL error: {e} | sql={sql[:80]}")
            raise

    # ── deployments ──────────────────────────────────────────────────────────

    def save_deployment(self, deployment) -> None:
        """Upsert a Deployment object."""
        self._exec(
            """
            INSERT INTO deployments
                (deployment_id, project, service, branch, version,
                 status, pipeline_url, started_at, finished_at,
                 created_at, metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(deployment_id) DO UPDATE SET
                status       = excluded.status,
                pipeline_url = excluded.pipeline_url,
                started_at   = excluded.started_at,
                finished_at  = excluded.finished_at,
                metadata_json= excluded.metadata_json
            """,
            (
                deployment.deployment_id,
                self.project,
                deployment.service,
                deployment.branch,
                getattr(deployment, "version", ""),
                deployment.status.value if hasattr(deployment.status, "value") else str(deployment.status),
                getattr(deployment, "pipeline_url", None),
                deployment.started_at.isoformat() if getattr(deployment, "started_at", None) else None,
                deployment.finished_at.isoformat() if getattr(deployment, "finished_at", None) else None,
                deployment.created_at.isoformat() if getattr(deployment, "created_at", None) else _now(),
                json.dumps(getattr(deployment, "metadata", {})),
            ),
        )
        logger.debug(f"[DB] Deployment saved: {deployment.deployment_id}")

    def update_deployment_status(self, deployment_id: str, status: str,
                                  finished_at: Optional[datetime] = None,
                                  pipeline_url: Optional[str] = None) -> None:
        self._exec(
            """
            UPDATE deployments SET status=?, finished_at=?, pipeline_url=COALESCE(?,pipeline_url)
            WHERE deployment_id=?
            """,
            (
                status,
                finished_at.isoformat() if finished_at else None,
                pipeline_url,
                deployment_id,
            ),
        )

    def get_all_deployments(self) -> List[Dict]:
        cur = self._exec(
            "SELECT * FROM deployments WHERE project=? ORDER BY created_at DESC",
            (self.project,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_deployments_for_service(self, service: str) -> List[Dict]:
        cur = self._exec(
            "SELECT * FROM deployments WHERE project=? AND service=? ORDER BY created_at DESC",
            (self.project, service),
        )
        return [dict(r) for r in cur.fetchall()]

    # ── incidents ────────────────────────────────────────────────────────────

    def save_incident(self, incident) -> None:
        """Upsert an Incident object."""
        self._exec(
            """
            INSERT INTO incidents
                (incident_id, project, service, severity, status,
                 description, created_at, updated_at, metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(incident_id) DO UPDATE SET
                status        = excluded.status,
                updated_at    = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                incident.incident_id,
                self.project,
                incident.service,
                incident.severity.value if hasattr(incident.severity, "value") else str(incident.severity),
                incident.status.value   if hasattr(incident.status,   "value") else str(incident.status),
                incident.description,
                incident.created_at.isoformat() if getattr(incident, "created_at", None) else _now(),
                incident.updated_at.isoformat() if getattr(incident, "updated_at", None) else _now(),
                json.dumps(getattr(incident, "metadata", {})),
            ),
        )
        logger.debug(f"[DB] Incident saved: {incident.incident_id}")

    def update_incident_status(self, incident_id: str, status: str) -> None:
        self._exec(
            "UPDATE incidents SET status=?, updated_at=? WHERE incident_id=?",
            (status, _now(), incident_id),
        )

    def get_all_incidents(self) -> List[Dict]:
        cur = self._exec(
            "SELECT * FROM incidents WHERE project=? ORDER BY created_at DESC",
            (self.project,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_active_incidents(self) -> List[Dict]:
        cur = self._exec(
            """
            SELECT * FROM incidents
            WHERE project=? AND status NOT IN ('resolved','failed')
            ORDER BY created_at DESC
            """,
            (self.project,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_incident(self, incident_id: str) -> Optional[Dict]:
        cur = self._exec(
            "SELECT * FROM incidents WHERE incident_id=?",
            (incident_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ── solutions ────────────────────────────────────────────────────────────

    def save_solution(self, solution) -> None:
        """Insert a Solution (always a new row — multiple solutions per incident)."""
        self._exec(
            """
            INSERT INTO solutions
                (incident_id, project, root_cause, healing_prompt,
                 confidence, source, commands_json, references_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                solution.incident_id,
                self.project,
                getattr(solution, "root_cause",     ""),
                getattr(solution, "healing_prompt",  ""),
                getattr(solution, "confidence",      0.0),
                getattr(solution, "source",          "unknown"),
                json.dumps(getattr(solution, "suggested_commands", [])),
                json.dumps(getattr(solution, "references",         [])),
                solution.created_at.isoformat() if getattr(solution, "created_at", None) else _now(),
            ),
        )
        logger.debug(f"[DB] Solution saved for incident: {solution.incident_id}")

    def get_solutions_for_incident(self, incident_id: str) -> List[Dict]:
        cur = self._exec(
            "SELECT * FROM solutions WHERE incident_id=? ORDER BY confidence DESC",
            (incident_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ── remediation actions ───────────────────────────────────────────────────

    def save_remediation_action(self, action) -> None:
        """Upsert a RemediationAction object."""
        self._exec(
            """
            INSERT INTO remediation_actions
                (action_id, incident_id, project, command, description,
                 status, output, error, executed_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(action_id) DO UPDATE SET
                status      = excluded.status,
                output      = excluded.output,
                error       = excluded.error,
                executed_at = excluded.executed_at
            """,
            (
                action.action_id,
                action.incident_id,
                self.project,
                getattr(action, "command",     ""),
                getattr(action, "description", ""),
                action.status.value if hasattr(action.status, "value") else str(action.status),
                getattr(action, "output",  None),
                getattr(action, "error",   None),
                action.executed_at.isoformat() if getattr(action, "executed_at", None) else None,
                action.created_at.isoformat()  if getattr(action, "created_at",  None) else _now(),
            ),
        )
        logger.debug(f"[DB] Remediation action saved: {action.action_id}")

    def get_actions_for_incident(self, incident_id: str) -> List[Dict]:
        cur = self._exec(
            "SELECT * FROM remediation_actions WHERE incident_id=? ORDER BY created_at ASC",
            (incident_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ── events log ───────────────────────────────────────────────────────────

    def log_event(self, event) -> None:
        """
        Persist any EventBus Event to the events_log table.
        data is serialised to JSON — complex objects are string-coerced.
        """
        try:
            data_json = json.dumps(event.data, default=str)
        except Exception:
            data_json = "{}"

        self._exec(
            """
            INSERT INTO events_log
                (event_id, event_type, source, incident_id, project, data_json, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                event.event_id,
                str(event.type),
                getattr(event, "source",      "unknown"),
                getattr(event, "incident_id", None),
                self.project,
                data_json,
                event.timestamp.isoformat() if getattr(event, "timestamp", None) else _now(),
            ),
        )

    def get_events(self, event_type: Optional[str] = None,
                   incident_id: Optional[str] = None,
                   limit: int = 100) -> List[Dict]:
        sql    = "SELECT * FROM events_log WHERE project=?"
        params: list = [self.project]
        if event_type:
            sql += " AND event_type=?"
            params.append(event_type)
        if incident_id:
            sql += " AND incident_id=?"
            params.append(incident_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cur = self._exec(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    # ── alerts ───────────────────────────────────────────────────────────────

    def save_alert(self, alert) -> None:
        """Upsert an Alert object."""
        self._exec(
            """
            INSERT INTO alerts
                (alert_id, incident_id, project, title, message,
                 severity, channel, sent, sent_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(alert_id) DO UPDATE SET
                sent    = excluded.sent,
                sent_at = excluded.sent_at
            """,
            (
                alert.alert_id,
                alert.incident_id,
                self.project,
                getattr(alert, "title",   ""),
                getattr(alert, "message", ""),
                alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity),
                getattr(alert, "channel", ""),
                1 if getattr(alert, "sent", False) else 0,
                alert.sent_at.isoformat() if getattr(alert, "sent_at", None) else None,
                alert.created_at.isoformat() if getattr(alert, "created_at", None) else _now(),
            ),
        )
        logger.debug(f"[DB] Alert saved: {alert.alert_id}")

    def get_alerts_for_incident(self, incident_id: str) -> List[Dict]:
        cur = self._exec(
            "SELECT * FROM alerts WHERE incident_id=? ORDER BY created_at ASC",
            (incident_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ── full history query ────────────────────────────────────────────────────

    def get_full_history(self) -> Dict[str, Any]:
        """
        Return a complete snapshot of everything stored for this project.

        Returns a dict with keys:
            project, generated_at,
            deployments, incidents, solutions,
            remediation_actions, events, alerts
        """
        return {
            "project"             : self.project,
            "generated_at"        : _now(),
            "deployments"         : self.get_all_deployments(),
            "incidents"           : self.get_all_incidents(),
            "solutions"           : self._exec(
                "SELECT * FROM solutions WHERE project=? ORDER BY created_at DESC",
                (self.project,),
            ).fetchall().__class__([dict(r) for r in self._exec(
                "SELECT * FROM solutions WHERE project=? ORDER BY created_at DESC",
                (self.project,),
            ).fetchall()]),
            "remediation_actions" : self._exec(
                "SELECT * FROM remediation_actions WHERE project=? ORDER BY created_at DESC",
                (self.project,),
            ).__class__([dict(r) for r in self._exec(
                "SELECT * FROM remediation_actions WHERE project=? ORDER BY created_at DESC",
                (self.project,),
            ).fetchall()]),
            "events"              : self.get_events(limit=500),
            "alerts"              : self._exec(
                "SELECT * FROM alerts WHERE project=? ORDER BY created_at DESC",
                (self.project,),
            ).__class__([dict(r) for r in self._exec(
                "SELECT * FROM alerts WHERE project=? ORDER BY created_at DESC",
                (self.project,),
            ).fetchall()]),
        }

    def get_summary(self) -> Dict[str, Any]:
        """Return a lightweight summary suitable for dashboards."""
        def _count(table: str, where: str = "") -> int:
            sql = f"SELECT COUNT(*) FROM {table} WHERE project=?"
            if where:
                sql += f" AND {where}"
            return self._exec(sql, (self.project,)).fetchone()[0]

        return {
            "project"              : self.project,
            "db_path"              : str(self.db_path),
            "total_deployments"    : _count("deployments"),
            "successful_deploys"   : _count("deployments", "status='success'"),
            "total_incidents"      : _count("incidents"),
            "open_incidents"       : _count("incidents", "status='open'"),
            "resolved_incidents"   : _count("incidents", "status='resolved'"),
            "total_solutions"      : _count("solutions"),
            "total_actions"        : _count("remediation_actions"),
            "total_events"         : _count("events_log"),
            "total_alerts"         : _count("alerts"),
        }

    def print_summary(self) -> None:
        """Print a formatted summary to stdout."""
        s = self.get_summary()
        print(f"\n{'─'*55}")
        print(f"  📦 Project DB: {s['project']}")
        print(f"  📍 Path: {s['db_path']}")
        print(f"{'─'*55}")
        print(f"  Deployments : {s['total_deployments']}  (✅ {s['successful_deploys']} succeeded)")
        print(f"  Incidents   : {s['total_incidents']}  (🔓 {s['open_incidents']} open / ✅ {s['resolved_incidents']} resolved)")
        print(f"  Solutions   : {s['total_solutions']}")
        print(f"  Actions     : {s['total_actions']}")
        print(f"  Events      : {s['total_events']}")
        print(f"  Alerts      : {s['total_alerts']}")
        print(f"{'─'*55}\n")

    def __repr__(self):
        return f"DatabaseManager(project={self.project!r}, db={self.db_path})"