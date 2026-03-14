"""
Context - Incident Context Object

logger = logging.getLogger(__name__)


@dataclass
class IncidentContext:
    """
    The full context of one incident as it moves through the pipeline.

    Every Agent receives this object, reads what it needs,
    and writes its output back into it before passing it forward.

    Example:
        # MonitoringAgent creates it
        ctx = IncidentContext(incident=incident)

        # KnowledgeAgent adds its findings
        ctx.add_solution(solution)

        # SelfHealingAgent adds the result
        ctx.add_remediation_result(result)

        # AlertingAgent reads the full picture
        ctx.incident.status   -> RESOLVED
        ctx.solution          -> Solution(...)
        ctx.remediation_result -> RemediationResult(...)
    """

    # The incident at the center of this context
    incident: Incident

    # Filled in by KnowledgeAgent
    solution: Optional[Solution] = None

    # Filled in by SelfHealingAgent
    remediation_result: Optional[RemediationResult] = None

    # Filled in by AlertingAgent
    alerts_sent: List[Alert] = field(default_factory=list)

    # Filled in by CICDAgent (if deployment is involved)
    deployment: Optional[DeploymentRecord] = None

    # Full audit trail of Agent actions
    timeline: List[dict] = field(default_factory=list)

    # When the context was created
    created_at: datetime = field(default_factory=datetime.utcnow)

    # --------------------------------------------------------
    # Shortcut properties
    # --------------------------------------------------------

    @property
    def incident_id(self) -> str:
        """Quick access to the incident ID."""
        return self.incident.incident_id

    @property
    def service(self) -> str:
        """Quick access to the affected service name."""
        return self.incident.service

    @property
    def is_resolved(self) -> bool:
        """True if the incident has been resolved."""
        return self.incident.status == IncidentStatus.RESOLVED

    @property
    def is_escalated(self) -> bool:
        """True if the incident was escalated for human review."""
        return self.incident.status == IncidentStatus.ESCALATED

    # --------------------------------------------------------
    # Write methods — called by Agents to update the context
    # --------------------------------------------------------

    def add_solution(self, solution: Solution) -> None:
        """
        Called by KnowledgeAgent after investigation is complete.
        Stores the recommended solution and logs it to the timeline.
        """
        self.solution = solution
        self.incident.status = IncidentStatus.REMEDIATING
        self.incident.updated_at = datetime.utcnow()

        self._log_event(
            agent="knowledge_agent",
            action="solution_generated",
            details={
                "root_cause": solution.root_cause,
                "recommended_action": solution.recommended_action,
                "confidence": solution.confidence,
            }
        )
        logger.info(f"[Context] Solution added for {self.incident_id}: {solution}")

    def add_remediation_result(self, result: RemediationResult) -> None:
        """
        Called by SelfHealingAgent after executing the fix.
        Updates incident status based on whether it succeeded.
        """
        self.remediation_result = result

        if result.success:
            self.incident.status = IncidentStatus.RESOLVED
        else:
            self.incident.status = IncidentStatus.FAILED

        self.incident.updated_at = datetime.utcnow()

        self._log_event(
            agent="self_healing_agent",
            action="remediation_executed",
            details={
                "action": result.action,
                "success": result.success,
                "output": result.output,
            }
        )
        logger.info(f"[Context] Remediation result added for {self.incident_id}: {result}")

    def add_alert(self, alert: Alert) -> None:
        """
        Called by AlertingAgent after sending a notification.
        """
        self.alerts_sent.append(alert)

        self._log_event(
            agent="alerting_agent",
            action="alert_sent",
            details={
                "channel": alert.channel,
                "title": alert.title,
                "delivered": alert.delivered,
            }
        )
        logger.info(f"[Context] Alert sent for {self.incident_id} via {alert.channel}")

    def set_deployment(self, deployment: DeploymentRecord) -> None:
        """
        Called by CICDAgent when a deployment is part of the workflow.
        """
        self.deployment = deployment

        self._log_event(
            agent="cicd_agent",
            action="deployment_tracked",
            details={
                "deployment_id": deployment.deployment_id,
                "service": deployment.service,
                "branch": deployment.branch,
            }
        )

    def escalate(self) -> None:
        """
        Mark this incident as escalated for human intervention.
        Called by Orchestrator when automated resolution is not possible.
        """
        self.incident.status = IncidentStatus.ESCALATED
        self.incident.updated_at = datetime.utcnow()

        self._log_event(
            agent="orchestrator",
            action="escalated",
            details={"reason": "automated resolution failed"}
        )
        logger.warning(f"[Context] Incident escalated: {self.incident_id}")

    # --------------------------------------------------------
    # Timeline
    # --------------------------------------------------------

    def _log_event(self, agent: str, action: str, details: dict) -> None:
        """Append an entry to the timeline audit trail."""
        self.timeline.append({
            "timestamp": datetime.utcnow().isoformat(),
            "agent": agent,
            "action": action,
            "details": details,
        })

    def get_timeline(self) -> List[dict]:
        """Return the full ordered timeline of Agent actions."""
        return self.timeline

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    def summary(self) -> dict:
        """
        Return a human-readable summary of the full incident context.
        Used by AlertingAgent to build notification messages.
        """
        return {
            "incident_id": self.incident_id,
            "service": self.service,
            "severity": self.incident.severity,
            "status": self.incident.status,
            "description": self.incident.description,
            "root_cause": self.solution.root_cause if self.solution else None,
            "action_taken": self.remediation_result.action if self.remediation_result else None,
            "remediation_success": self.remediation_result.success if self.remediation_result else None,
            "alerts_sent": len(self.alerts_sent),
            "timeline_steps": len(self.timeline),
            "created_at": self.created_at.isoformat(),
        }

    def __repr__(self):
        return (
            f"IncidentContext("
            f"id={self.incident_id}, "
            f"service={self.service}, "
            f"status={self.incident.status})"
        )
