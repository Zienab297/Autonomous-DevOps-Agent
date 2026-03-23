"""
core/orchestrator.py
---------------------
The Orchestrator is the brain of the system.
It coordinates all Agents, manages incident workflows,
and ensures the right Agent is called at the right time.

Workflow:
    Incident Created
         │
         ▼
    Knowledge Agent  →  Solution
         │
         ▼
    Self-Healing Agent  →  Remediation
         │
         ▼
    Alerting Agent  →  Notification
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.models import (
    AgentStatus,
    Incident,
    IncidentStatus,
    Solution,
)
from core.event_bus import EventBus, Event, EventType
from core.state_manager import StateManager
from core.context_manager import ContextManager
from core.agent_registery import AgentRegistry
from core.approval_manager import ApprovalManager

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    The Orchestrator coordinates all Agents in the system.

    Responsibilities:
        - Register and manage Agents
        - Listen for Events from the EventBus
        - Trigger the correct Agent for each Event
        - Track incident workflow from detection to resolution

    Example:
        orchestrator = Orchestrator()

        # Register agents
        orchestrator.register_agent("monitoring_agent", monitoring_agent)
        orchestrator.register_agent("knowledge_agent", knowledge_agent)
        orchestrator.register_agent("self_healing_agent", healing_agent)
        orchestrator.register_agent("alerting_agent", alerting_agent)

        # Start the system
        await orchestrator.start()
    """

    def __init__(self):
        self.event_bus       = EventBus()
        self.state_manager   = StateManager()
        self.context_manager = ContextManager()
        self.registry        = AgentRegistry()
        self.approval        = ApprovalManager()
        self._running        = False

        self._subscribe_to_events()
        logger.info("Orchestrator initialized")

    def _subscribe_to_events(self) -> None:
        """Subscribe to all core EventBus events."""
        # Scaffold workflow
        self.event_bus.subscribe(EventType.SCAFFOLD_STARTED,    self._on_scaffold_started)
        self.event_bus.subscribe(EventType.SCAFFOLD_COMPLETE,   self._on_scaffold_complete)
        self.event_bus.subscribe(EventType.SCAFFOLD_FAILED,     self._on_scaffold_failed)

        # Incident workflow
        self.event_bus.subscribe(EventType.INCIDENT_CREATED,       self._on_incident_created)
        self.event_bus.subscribe(EventType.INVESTIGATION_COMPLETE,  self._on_investigation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_COMPLETE,    self._on_remediation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_FAILED,      self._on_remediation_failed)

    def register_agent(
        self,
        name: str,
        agent: object,
        metadata: Optional[dict] = None,
    ) -> None:
        """Register an Agent with the Orchestrator."""
        self.registry.register(name, agent, metadata)
        self.state_manager.set_agent_status(name, AgentStatus.IDLE)
        logger.info(f"[Orchestrator] Agent registered: '{name}'")

    # ============================================================
    # Lifecycle
    # ============================================================

    async def start(self) -> None:
        self._running = True
        logger.info("[Orchestrator] Started")

    async def stop(self) -> None:
        self._running = False
        for record in self.registry.get_all():
            self.state_manager.set_agent_status(record.name, AgentStatus.STOPPED)
        logger.info("[Orchestrator] Stopped")

    # ============================================================
    # Scaffold Entry Point
    # ============================================================

    async def run_scaffold(self, project_path: str, dry_run: bool = False) -> None:
        """Entry point from CLI — publishes SCAFFOLD_STARTED."""
        logger.info(f"[Orchestrator] run_scaffold: {project_path}")
        await self.event_bus.publish(Event(
            type=EventType.SCAFFOLD_STARTED,
            source="cli",
            data={"project_path": project_path, "dry_run": dry_run},
        ))

    # ============================================================
    # Scaffold Handlers
    # ============================================================

    async def _on_scaffold_started(self, event: Event) -> None:
        """Step 1 — ScaffoldAgent builds deployment files."""
        logger.info("[Orchestrator] SCAFFOLD_STARTED → ScaffoldAgent")

        scaffold_agent = self.registry.get_agent("scaffold_agent")
        if not scaffold_agent:
            logger.error("[Orchestrator] ScaffoldAgent not registered!")
            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_FAILED,
                source="orchestrator",
                data={"error": "ScaffoldAgent not registered"},
            ))
            return

        self.state_manager.set_agent_status("scaffold_agent", AgentStatus.RUNNING)
        project_path = event.data.get("project_path")
        dry_run      = event.data.get("dry_run", False)

        try:
            result = scaffold_agent.run(project_path, dry_run=dry_run)

            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_COMPLETE,
                source="scaffold_agent",
                data={
                    "project_path"   : project_path,
                    "dry_run"        : dry_run,
                    "language"       : result.language.value,
                    "framework"      : result.framework.value,
                    "generated_files": [f.filename for f in result.generated_files],
                },
            ))

        except Exception as e:
            logger.error(f"[Orchestrator] ScaffoldAgent failed: {e}")
            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_FAILED,
                source="scaffold_agent",
                data={"error": str(e), "project_path": project_path},
            ))
        finally:
            self.state_manager.set_agent_status("scaffold_agent", AgentStatus.IDLE)

    async def _on_scaffold_complete(self, event: Event) -> None:
        """
        Step 2 — Scaffold done.
        Ask for approval → if granted → CI/CD Agent.
        """
        files        = event.data.get("generated_files", [])
        project_path = event.data.get("project_path")
        dry_run      = event.data.get("dry_run", False)
        language     = event.data.get("language", "")
        framework    = event.data.get("framework", "")

        logger.info(f"[Orchestrator] SCAFFOLD_COMPLETE ({len(files)} files)")

        if dry_run:
            return

        # ── Approval after Scaffold ───────────────────────────────
        approved = await self.approval.request_approval(
            title=f"Scaffold complete — {framework} ({language}). Proceed to CI/CD?",
            details=files,
            context={"project_path": project_path},
        )

        if not approved:
            logger.info("[Orchestrator] CI/CD cancelled by developer.")
            print("\n  Pipeline stopped. No CI/CD triggered.\n")
            return

        # ── Ask for GitHub repo URL ───────────────────────────────
        repo_url = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: input("\n  GitHub repo URL (https://github.com/user/repo): ").strip()
        )
        if not repo_url:
            print("  No repo URL — stopping.\n")
            return

        # ── Ask for GitHub token ──────────────────────────────────
        import getpass
        token = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: getpass.getpass("  GitHub token (hidden): ").strip()
        )
        if not token:
            print("  No token — stopping.\n")
            return

        # ── Push files to GitHub ──────────────────────────────────
        pushed = await self._push_to_github(
            project_path=project_path,
            repo_url=repo_url,
            token=token,
        )
        if not pushed:
            print("  Git push failed — stopping.\n")
            return

        # ── Trigger CI/CD Agent ───────────────────────────────────
        cicd_agent = self.registry.get_agent("cicd_agent")
        if not cicd_agent:
            logger.warning("[Orchestrator] CI/CD Agent not registered — done after scaffold")
            return

        self.state_manager.set_agent_status("cicd_agent", AgentStatus.RUNNING)
        try:
            logs = await cicd_agent.run_pipeline(project_path=project_path)

            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="cicd_agent",
                data={"project_path": project_path, "logs": logs},
            ))
        except Exception as e:
            logger.error(f"[Orchestrator] CI/CD Agent failed: {e}")
        finally:
            self.state_manager.set_agent_status("cicd_agent", AgentStatus.IDLE)

    async def _on_scaffold_failed(self, event: Event) -> None:
        logger.error(f"[Orchestrator] Scaffold failed: {event.data.get('error')}")

    async def _push_to_github(
        self,
        project_path: str,
        repo_url    : str,
        token       : str,
    ) -> bool:
        """
        Push all scaffold-generated files to the GitHub repo.
        Uses git commands via subprocess.
        """
        import subprocess

        # Inject token into URL
        # https://github.com/user/repo → https://token@github.com/user/repo
        if repo_url.startswith("https://"):
            auth_url = repo_url.replace("https://", f"https://{token}@")
        else:
            auth_url = repo_url

        def run(cmd: list, cwd: str) -> tuple[int, str]:
            result = subprocess.run(
                cmd, cwd=cwd,
                capture_output=True, text=True,
            )
            return result.returncode, result.stdout + result.stderr

        print(f"\n  Pushing to: {repo_url}")

        steps = [
            (["git", "init"],                                   "git init"),
            (["git", "add", "."],                               "git add"),
            (["git", "commit", "-m", "chore: add DevOps scaffold files"], "git commit"),
            (["git", "branch", "-M", "main"],                   "git branch"),
            (["git", "remote", "remove", "origin"],             "remove old remote (ok if fails)"),
            (["git", "remote", "add", "origin", auth_url],      "git remote add"),
            (["git", "push", "-u", "origin", "main", "--force"], "git push"),
        ]

        for cmd, label in steps:
            code, out = run(cmd, project_path)
            if code != 0 and "remove old remote" not in label:
                print(f"  Failed at [{label}]: {out.strip()[:200]}")
                logger.error(f"[Orchestrator] git push failed at {label}: {out}")
                return False
            else:
                print(f"  [{label}] OK")

        print(f"  Pushed successfully to {repo_url}\n")
        return True

    # ============================================================
    # Incident Workflow
    # ============================================================

    async def handle_incident(self, incident: Incident) -> None:
        """
        Main entry point — trigger the full incident workflow.

        Steps:
            1. Save incident to state
            2. Create context
            3. Publish INCIDENT_CREATED event
            4. Knowledge Agent investigates
            5. Self-Healing Agent remediates
            6. Alerting Agent notifies
        """
        logger.info(f"[Orchestrator] Handling: {incident}")

        # Step 1 — Save to state
        self.state_manager.add_incident(incident)
        self.state_manager.update_incident_status(
            incident.incident_id, IncidentStatus.INVESTIGATING
        )

        # Step 2 — Create context
        self.context_manager.create_context(incident)

        # Step 3 — Publish event
        await self.event_bus.publish(Event(
            type=EventType.INCIDENT_CREATED,
            source="orchestrator",
            incident_id=incident.incident_id,
            data={
                "incident_id": incident.incident_id,
                "service"    : incident.service,
                "severity"   : incident.severity.value,
                "description": incident.description,
            }
        ))

    # ============================================================
    # Event Handlers
    # ============================================================

    async def _on_incident_created(self, event: Event) -> None:
        """Triggered when a new Incident is created — approval then Knowledge Agent."""
        logger.info(f"[Orchestrator] Incident created → approval before Knowledge Agent")

        # ── Approval before Knowledge Agent ──────────────────────
        approved = await self.approval.request_approval(
            title=f"Incident detected — run Knowledge Agent to investigate?",
            details=[
                f"Incident : {event.incident_id}",
                f"Service  : {event.data.get('service', 'unknown')}",
                f"Severity : {event.data.get('severity', 'unknown')}",
                f"Desc     : {event.data.get('description', '')}",
            ],
        )
        if not approved:
            logger.info("[Orchestrator] Knowledge Agent cancelled.")
            return

        knowledge_agent = self.registry.get_agent("knowledge_agent")
        if not knowledge_agent:
            logger.error("[Orchestrator] Knowledge Agent not registered!")
            return

        self.state_manager.set_agent_status("knowledge_agent", AgentStatus.RUNNING)
        context = self.context_manager.get_context(event.incident_id)

        try:
            solution = await knowledge_agent.investigate(context)
            if solution:
                self.state_manager.add_solution(solution)
                await self.event_bus.publish(Event(
                    type=EventType.INVESTIGATION_COMPLETE,
                    source="knowledge_agent",
                    incident_id=event.incident_id,
                    data={"solution": solution},
                ))
        except Exception as e:
            logger.error(f"[Orchestrator] Knowledge Agent failed: {e}")
        finally:
            self.state_manager.set_agent_status("knowledge_agent", AgentStatus.IDLE)

    async def _on_investigation_complete(self, event: Event) -> None:
        """Investigation done — approval then Self-Healing Agent."""
        logger.info(f"[Orchestrator] Investigation complete → approval before Self-Healing")

        solution: Solution = event.data.get("solution")

        # ── Approval before Self-Healing ──────────────────────────
        approved = await self.approval.request_approval(
            title="Investigation complete — apply self-healing fix?",
            details=[
                f"Root cause : {getattr(solution, 'root_cause', 'unknown')}",
                f"Confidence : {getattr(solution, 'confidence', 0):.0%}",
            ],
        )
        if not approved:
            logger.info("[Orchestrator] Self-Healing cancelled.")
            return

        healing_agent = self.registry.get_agent("self_healing_agent")
        if not healing_agent:
            logger.error("[Orchestrator] Self-Healing Agent not registered!")
            return

        self.state_manager.set_agent_status("self_healing_agent", AgentStatus.RUNNING)
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.REMEDIATING)

        try:
            await healing_agent.remediate(solution)
        except Exception as e:
            logger.error(f"[Orchestrator] Self-Healing Agent failed: {e}")
        finally:
            self.state_manager.set_agent_status("self_healing_agent", AgentStatus.IDLE)

    async def _on_remediation_complete(self, event: Event) -> None:
        """Triggered when remediation succeeds — resolve incident and notify."""
        logger.info(f"[Orchestrator] Remediation complete → resolving incident")

        self.state_manager.update_incident_status(
            event.incident_id, IncidentStatus.RESOLVED
        )

        await self._send_alert(
            incident_id=event.incident_id,
            title="Incident Resolved",
            message=f"Incident {event.incident_id} has been resolved automatically.",
        )

        self.context_manager.drop_context(event.incident_id)

    async def _on_remediation_failed(self, event: Event) -> None:
        """Triggered when remediation fails — mark failed and notify."""
        logger.warning(f"[Orchestrator] Remediation failed for {event.incident_id}")

        self.state_manager.update_incident_status(
            event.incident_id, IncidentStatus.FAILED
        )

        await self._send_alert(
            incident_id=event.incident_id,
            title="Remediation Failed",
            message=f"Incident {event.incident_id} could not be resolved automatically. Manual intervention required.",
        )

    # ============================================================
    # Helpers
    # ============================================================

    async def _send_alert(
        self,
        incident_id: str,
        title: str,
        message: str,
    ) -> None:
        """Send an alert via the Alerting Agent if available."""
        alerting_agent = self.registry.get_agent("alerting_agent")
        if not alerting_agent:
            logger.warning("[Orchestrator] Alerting Agent not registered — skipping alert")
            return

        try:
            await alerting_agent.send(
                incident_id=incident_id,
                title=title,
                message=message,
            )
        except Exception as e:
            logger.error(f"[Orchestrator] Alerting Agent failed: {e}")

    # ============================================================
    # Summary
    # ============================================================

    def summary(self) -> dict:
        """Return a full system summary."""
        return {
            "orchestrator" : "running" if self._running else "stopped",
            "agents"       : self.registry.summary(),
            "state"        : self.state_manager.summary(),
            "event_history": len(self.event_bus.get_history()),
        }

    def __repr__(self):
        return (
            f"Orchestrator("
            f"running={self._running}, "
            f"agents={self.registry.get_all_names()})"
        )