"""
core/pg_database.py
--------------------
PostgreSQL persistence manager.

Wraps the repository layer so the Orchestrator can call:
    pg_db.save_incident(incident)
    pg_db.save_solution(solution)
    pg_db.update_incident_status(incident_id, status)
    pg_db.log_event(event)

All operations run inside an atomic session from db.session.get_session().
Sensitive fields are never logged verbatim.

Usage (in devops.py / orchestrator):
    from core.pg_database import PostgreSQLDatabaseManager
    pg_db = PostgreSQLDatabaseManager.for_project(project_path, DATABASE_URL)
    orchestrator.set_pg_database(pg_db)
"""

import logging
import os
from typing import Optional

from db.init_db import init_db
from db.session import get_session

from repositories.incident_repo   import IncidentRepository
from repositories.solution_repo   import SolutionRepository
from repositories.action_repo     import ActionRepository
from repositories.alert_repo      import AlertRepository
from repositories.event_repo      import EventRepository
from repositories.deployment_repo import DeploymentRepository

logger = logging.getLogger(__name__)


class PostgreSQLDatabaseManager:
    """
    Thin façade over repository classes.
    One instance is shared per process (created via for_project()).
    """

    def __init__(self, project_id: str):
        self.project_id = project_id
        logger.info("[PgDB] PostgreSQLDatabaseManager ready for project=%s", project_id)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def for_project(
        cls,
        project_path: "os.PathLike | str",
        database_url: Optional[str] = None,
    ) -> "PostgreSQLDatabaseManager":
        """
        Initialize the DB engine + tables and return a manager instance.

        Args:
            project_path: Path to the project directory (used as project_id).
            database_url: PostgreSQL DSN. Falls back to DATABASE_URL env var.
        """
        url = database_url or os.getenv("DATABASE_URL", "")
        if not url:
            raise ValueError(
                "PostgreSQL DATABASE_URL is not set. "
                "Add DATABASE_URL=postgresql+psycopg2://... to your .env file."
            )
        init_db(database_url=url)
        project_id = str(project_path)
        return cls(project_id=project_id)

    # ── Incidents ─────────────────────────────────────────────────────────────

    def save_incident(self, incident_obj) -> None:
        """Persist (or deduplicate) an Incident object."""
        try:
            with get_session() as s:
                repo = IncidentRepository(s)
                repo.create(incident_obj, self.project_id)
        except Exception as exc:
            logger.error(
                "[PgDB] save_incident failed incident_id=%s: %s",
                getattr(incident_obj, "incident_id", "?"), exc,
            )
            # Don't re-raise — persistence errors must not crash the pipeline

    def update_incident_status(self, incident_id: str, status: str) -> None:
        """Update the status of a persisted incident."""
        try:
            with get_session() as s:
                repo = IncidentRepository(s)
                repo.update_status(incident_id, status)
        except Exception as exc:
            logger.error(
                "[PgDB] update_incident_status failed incident_id=%s: %s",
                incident_id, exc,
            )

    # ── Solutions ─────────────────────────────────────────────────────────────

    def save_solution(self, solution_obj) -> None:
        """
        Persist a Solution object.
        The solution must have an .incident_id attribute.
        """
        incident_id = getattr(solution_obj, "incident_id", None)
        if not incident_id:
            logger.warning("[PgDB] save_solution called without incident_id — skipping")
            return
        try:
            with get_session() as s:
                repo = SolutionRepository(s)
                repo.create(solution_obj, self.project_id, incident_id)
        except Exception as exc:
            logger.error(
                "[PgDB] save_solution failed incident_id=%s: %s",
                incident_id, exc,
            )

    # ── Actions ───────────────────────────────────────────────────────────────

    def save_action(self, action_obj, incident_id: str) -> None:
        """Persist a RemediationAction linked to an incident."""
        try:
            with get_session() as s:
                repo = ActionRepository(s)
                repo.create(action_obj, self.project_id, incident_id)
        except Exception as exc:
            logger.error(
                "[PgDB] save_action failed incident_id=%s: %s",
                incident_id, exc,
            )

    # ── Events ────────────────────────────────────────────────────────────────

    def log_event(self, event_obj) -> None:
        """Append an event to the audit log."""
        try:
            with get_session() as s:
                repo = EventRepository(s)
                repo.create(event_obj, self.project_id)
        except Exception as exc:
            logger.debug("[PgDB] log_event failed: %s", exc)

    # ── Deployments ───────────────────────────────────────────────────────────

    def save_deployment(self, dep_obj) -> None:
        """Persist (or upsert) a Deployment object."""
        try:
            with get_session() as s:
                repo = DeploymentRepository(s)
                repo.create(dep_obj, self.project_id)
        except Exception as exc:
            logger.error("[PgDB] save_deployment failed: %s", exc)

    def update_deployment_status(
        self,
        deployment_id: str,
        status       : str,
        pipeline_url : Optional[str]  = None,
        finished_at               = None,
    ) -> None:
        try:
            with get_session() as s:
                repo = DeploymentRepository(s)
                repo.update_status(deployment_id, status, pipeline_url, finished_at)
        except Exception as exc:
            logger.error(
                "[PgDB] update_deployment_status failed deployment_id=%s: %s",
                deployment_id, exc,
            )

    # ── Alerts ────────────────────────────────────────────────────────────────

    def save_alert(self, alert_obj, incident_id: str) -> None:
        """Persist an Alert linked to an incident."""
        try:
            with get_session() as s:
                repo = AlertRepository(s)
                repo.create(alert_obj, self.project_id, incident_id)
        except Exception as exc:
            logger.error(
                "[PgDB] save_alert failed incident_id=%s: %s",
                incident_id, exc,
            )

    def __repr__(self) -> str:
        return f"PostgreSQLDatabaseManager(project_id={self.project_id!r})"