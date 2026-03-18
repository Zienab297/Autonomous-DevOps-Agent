"""
core/context_manager.py
------------------------
Collects and manages all context related to an Incident.
The Knowledge Agent uses this to build a full picture
before generating a solution.
"""
from dataclasses import dataclass, field
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.models import Incident, Log, Metric, Solution, Deployment

logger = logging.getLogger(__name__)


@dataclass
class IncidentContext:
    """
    Full context collected for a single Incident.
    Passed to the Knowledge Agent for investigation.

    Contains:
        - The Incident itself
        - Related Metrics
        - Related Logs
        - Recent Deployments
        - Past Solutions for similar incidents
        - Any extra metadata
    """
    incident: Incident
    metrics: List[Metric] = field(default_factory=list)
    logs: List[Log] = field(default_factory=list)
    recent_deployments: List[Deployment] = field(default_factory=list)
    past_solutions: List[Solution] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_text(self) -> str:
        """
        Convert the full context to a text string
        ready to be injected into an LLM prompt.
        """
        lines = []

        lines.append(f"=== INCIDENT ===")
        lines.append(f"ID       : {self.incident.incident_id}")
        lines.append(f"Service  : {self.incident.service}")
        lines.append(f"Severity : {self.incident.severity.value}")
        lines.append(f"Description: {self.incident.description}")
        lines.append("")

        if self.metrics:
            lines.append("=== METRICS ===")
            for m in self.metrics[-10:]:  # last 10 metrics
                lines.append(f"  {m.name}: {m.value}{m.unit} ({m.service})")
            lines.append("")

        if self.logs:
            lines.append("=== LOGS ===")
            for log in self.logs[-10:]:  # last 10 logs
                lines.append(f"  [{log.level}] {log.message[:120]}")
            lines.append("")

        if self.recent_deployments:
            lines.append("=== RECENT DEPLOYMENTS ===")
            for dep in self.recent_deployments[-3:]:  # last 3 deployments
                lines.append(f"  {dep.deployment_id}: {dep.service} @ {dep.branch} → {dep.status.value}")
            lines.append("")

        if self.past_solutions:
            lines.append("=== PAST SOLUTIONS ===")
            for sol in self.past_solutions[-3:]:  # last 3 solutions
                lines.append(f"  [{sol.source}] confidence={sol.confidence:.2f}: {sol.root_cause[:100]}")
            lines.append("")

        return "\n".join(lines)

    def __str__(self):
        return (
            f"IncidentContext("
            f"incident={self.incident.incident_id}, "
            f"metrics={len(self.metrics)}, "
            f"logs={len(self.logs)})"
        )


class ContextManager:
    """
    Builds and stores IncidentContext for each active Incident.

    Example:
        ctx_manager = ContextManager()

        # Build context for an incident
        ctx_manager.create_context(incident)

        # Add data as it arrives
        ctx_manager.add_metrics("INC-001", metrics)
        ctx_manager.add_logs("INC-001", logs)

        # Get full context for Knowledge Agent
        context = ctx_manager.get_context("INC-001")
        prompt_text = context.to_text()
    """

    def __init__(self):
        # incident_id → IncidentContext
        self._contexts: Dict[str, IncidentContext] = {}
        logger.info("ContextManager initialized")

    # ============================================================
    # Context Lifecycle
    # ============================================================

    def create_context(self, incident: Incident) -> IncidentContext:
        """Create a new empty context for an Incident."""
        context = IncidentContext(incident=incident)
        self._contexts[incident.incident_id] = context
        logger.info(f"[ContextManager] Context created for {incident.incident_id}")
        return context

    def get_context(self, incident_id: str) -> Optional[IncidentContext]:
        """Get the full context for an Incident."""
        return self._contexts.get(incident_id)

    def drop_context(self, incident_id: str) -> None:
        """Remove context after Incident is resolved."""
        if incident_id in self._contexts:
            del self._contexts[incident_id]
            logger.info(f"[ContextManager] Context dropped for {incident_id}")

    # ============================================================
    # Adding Data to Context
    # ============================================================

    def add_metrics(self, incident_id: str, metrics: List[Metric]) -> None:
        """Add metrics to an Incident context."""
        ctx = self._contexts.get(incident_id)
        if ctx:
            ctx.metrics.extend(metrics)
            logger.debug(f"[ContextManager] Added {len(metrics)} metrics to {incident_id}")

    def add_logs(self, incident_id: str, logs: List[Log]) -> None:
        """Add logs to an Incident context."""
        ctx = self._contexts.get(incident_id)
        if ctx:
            ctx.logs.extend(logs)
            logger.debug(f"[ContextManager] Added {len(logs)} logs to {incident_id}")

    def add_deployment(self, incident_id: str, deployment: Deployment) -> None:
        """Add a recent deployment to an Incident context."""
        ctx = self._contexts.get(incident_id)
        if ctx:
            ctx.recent_deployments.append(deployment)
            logger.debug(f"[ContextManager] Added deployment to {incident_id}")

    def add_past_solution(self, incident_id: str, solution: Solution) -> None:
        """Add a past solution to an Incident context."""
        ctx = self._contexts.get(incident_id)
        if ctx:
            ctx.past_solutions.append(solution)
            logger.debug(f"[ContextManager] Added past solution to {incident_id}")

    def add_extra(self, incident_id: str, key: str, value: Any) -> None:
        """Add any extra metadata to an Incident context."""
        ctx = self._contexts.get(incident_id)
        if ctx:
            ctx.extra[key] = value

    # ============================================================
    # Summary
    # ============================================================

    def get_all_contexts(self) -> List[IncidentContext]:
        """Return all active contexts."""
        return list(self._contexts.values())

    def __repr__(self):
        return f"ContextManager(active_contexts={len(self._contexts)})"