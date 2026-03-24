"""
core/orchestrator.py
"""

import asyncio
import logging
import time
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
        self.event_bus.subscribe(EventType.SCAFFOLD_STARTED,    self._on_scaffold_started)
        self.event_bus.subscribe(EventType.SCAFFOLD_COMPLETE,   self._on_scaffold_complete)
        self.event_bus.subscribe(EventType.SCAFFOLD_FAILED,     self._on_scaffold_failed)
        self.event_bus.subscribe(EventType.INCIDENT_CREATED,       self._on_incident_created)
        self.event_bus.subscribe(EventType.INVESTIGATION_COMPLETE,  self._on_investigation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_COMPLETE,    self._on_remediation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_FAILED,      self._on_remediation_failed)

    def register_agent(self, name: str, agent: object, metadata: Optional[dict] = None) -> None:
        self.registry.register(name, agent, metadata)
        self.state_manager.set_agent_status(name, AgentStatus.IDLE)
        logger.info(f"[Orchestrator] Agent registered: '{name}'")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        logger.info("[Orchestrator] Started")

    async def stop(self) -> None:
        self._running = False
        for record in self.registry.get_all():
            self.state_manager.set_agent_status(record.name, AgentStatus.STOPPED)
        logger.info("[Orchestrator] Stopped")

    # ── Scaffold ──────────────────────────────────────────────────────────────

    async def run_scaffold(self, project_path: str, dry_run: bool = False) -> None:
        logger.info(f"[Orchestrator] run_scaffold: {project_path}")
        await self.event_bus.publish(Event(
            type=EventType.SCAFFOLD_STARTED,
            source="cli",
            data={"project_path": project_path, "dry_run": dry_run},
        ))

    async def _on_scaffold_started(self, event: Event) -> None:
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
        files        = event.data.get("generated_files", [])
        project_path = event.data.get("project_path")
        dry_run      = event.data.get("dry_run", False)
        language     = event.data.get("language", "")
        framework    = event.data.get("framework", "")

        logger.info(f"[Orchestrator] SCAFFOLD_COMPLETE ({len(files)} files)")

        if dry_run:
            return

        # ── Approval after Scaffold ───────────────────────────────────────
        approved = await self.approval.request_approval(
            title=f"Scaffold complete — {framework} ({language}). Proceed to CI/CD?",
            details=files,
            context={"project_path": project_path},
        )
        if not approved:
            logger.info("[Orchestrator] CI/CD cancelled by developer.")
            print("\n  Pipeline stopped. No CI/CD triggered.\n")
            return

        # ── Ask for GitHub repo URL ───────────────────────────────────────
        repo_url = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: input("\n  GitHub repo URL (https://github.com/user/repo): ").strip()
        )
        if not repo_url:
            print("  No repo URL — stopping.\n")
            return

        # ── Get GitHub token from .env ────────────────────────────────────
        import os
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            print("  No GITHUB_TOKEN in .env — stopping.\n")
            return
        print("  [OK]  GITHUB_TOKEN loaded from .env")

        # ── Push files to GitHub ──────────────────────────────────────────
        pushed = await self._push_to_github(
            project_path=project_path,
            repo_url=repo_url,
            token=token,
        )
        if not pushed:
            print("  Git push failed — stopping.\n")
            return

        # ── CI/CD Agent ───────────────────────────────────────────────────
        cicd_agent = self.registry.get_agent("cicd_agent")
        if not cicd_agent:
            logger.info("[Orchestrator] CI/CD Agent not registered — pipeline running on GitHub")
            print("\n  GitHub Actions workflow triggered by git push.")
            print("  Register CI/CD Agent to collect logs.\n")
            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="orchestrator",
                data={"project_path": project_path, "logs": [], "repo_url": repo_url},
            ))
            return

        self.state_manager.set_agent_status("cicd_agent", AgentStatus.RUNNING)
        print("\n----------------------------------------------------------------")
        print("  CI/CD — collecting logs from GitHub Actions")
        print("----------------------------------------------------------------")

        try:
            repo = repo_url.replace("https://github.com/", "").replace(".git", "")

            # ── انتظر GitHub Actions يبدأ ─────────────────────────────────
            print("  Waiting for GitHub Actions to start...")
            await asyncio.sleep(10)

            # ── جيب الـ latest run عن طريق الـ provider مباشرة ──────────
            run = await self._get_latest_run(cicd_agent, repo, token)

            if not run:
                print("  Could not find pipeline run — workflow may not have started yet")
                logs = []
            else:
                print(f"  Run ID    : {run.id}")
                print(f"  Status    : {run.status}")
                print(f"  URL       : {run.url}")

                # ── استنى يخلص ────────────────────────────────────────────
                deadline = 120
                elapsed  = 0
                while run.status not in ("success", "failed", "cancelled") and elapsed < deadline:
                    await asyncio.sleep(8)
                    elapsed += 8
                    run = await cicd_agent.get_pipeline_status(run.id, repo)
                    print(f"  [{elapsed}s] status: {run.status}")

                print(f"\n  Final status: {run.status}")

                # ── جمع الـ logs ───────────────────────────────────────────
                logs = await cicd_agent.collect_deployment_logs(run.id, repo)
                print(f"\n  -- CI/CD Logs ({len(logs)} lines) --")
                for line in logs:
                    print(f"    {line}")
                print("  --------------------------------\n")

            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="cicd_agent",
                data={
                    "project_path": project_path,
                    "repo_url"    : repo_url,
                    "logs"        : logs,
                    "status"      : run.status if run else "unknown",
                },
            ))

            # ── Approval before Monitoring Agent ──────────────────────────
            if logs:
                approved = await self.approval.request_approval(
                    title=f"CI/CD {'succeeded' if run and run.status == 'success' else 'finished'} — run Monitoring Agent to analyze logs?",
                    details=logs[:10],
                    context={"project_path": project_path},
                )
                if not approved:
                    logger.info("[Orchestrator] Monitoring Agent cancelled.")
                    print("\n  Monitoring skipped.\n")
                    return

                # ── Call Monitoring Agent ─────────────────────────────────
                monitoring_agent = self.registry.get_agent("monitoring_agent")
                if not monitoring_agent:
                    logger.warning("[Orchestrator] Monitoring Agent not registered")
                    return

                self.state_manager.set_agent_status("monitoring_agent", AgentStatus.RUNNING)
                try:
                    print("\n  -- Monitoring Agent Analyzing Logs --")
                    incident = await monitoring_agent.analyze_logs(logs)

                    if incident:
                        print(f"\n  -- Incident Detected --")
                        print(f"    service  : {incident.service}")
                        print(f"    severity : {incident.severity.value}")
                        print(f"    desc     : {incident.description}")
                        print(f"  ----------------------\n")
                        await self.handle_incident(incident)
                    else:
                        print("\n  No incidents detected — system healthy.\n")
                finally:
                    self.state_manager.set_agent_status("monitoring_agent", AgentStatus.IDLE)

        except Exception as e:
            logger.error(f"[Orchestrator] CI/CD Agent failed: {e}")
            print(f"\n  [FAIL] CI/CD Agent error: {e}")
            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="cicd_agent",
                data={"project_path": project_path, "logs": [], "repo_url": repo_url},
            ))
        finally:
            self.state_manager.set_agent_status("cicd_agent", AgentStatus.IDLE)

    async def _get_latest_run(self, cicd_agent, repo: str, token: str):
        """
        Get the latest workflow run from GitHub using the provider directly.
        Retries for up to 30 seconds waiting for the run to appear.
        """
        import aiohttp

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept"       : "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=1&branch=main"

        for attempt in range(4):
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            runs = data.get("workflow_runs", [])
                            if runs:
                                run_id = str(runs[0]["id"])
                                return await cicd_agent.get_pipeline_status(run_id, repo)
            except Exception as e:
                logger.warning(f"[Orchestrator] get_latest_run attempt {attempt+1}: {e}")

            if attempt < 3:
                print(f"  Waiting for run to appear... (attempt {attempt+2}/4)")
                await asyncio.sleep(8)

        return None

    async def _on_scaffold_failed(self, event: Event) -> None:
        logger.error(f"[Orchestrator] Scaffold failed: {event.data.get('error')}")

    async def _push_to_github(self, project_path: str, repo_url: str, token: str) -> bool:
        import subprocess

        if repo_url.startswith("https://"):
            auth_url = repo_url.replace("https://", f"https://{token}@")
        else:
            auth_url = repo_url

        def run(cmd: list, cwd: str) -> tuple[int, str]:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

        print(f"\n  Pushing to: {repo_url}")

        steps = [
            (["git", "init"],                                    "git init"),
            (["git", "add", "."],                                "git add"),
            (["git", "commit", "-m", "chore: add DevOps scaffold files"], "git commit"),
            (["git", "branch", "-M", "main"],                    "git branch"),
            (["git", "remote", "remove", "origin"],              "remove old remote (ok if fails)"),
            (["git", "remote", "add", "origin", auth_url],       "git remote add"),
            (["git", "push", "-u", "origin", "main", "--force"], "git push"),
        ]

        for cmd, label in steps:
            code, out = run(cmd, project_path)
            if code != 0 and "remove old remote" not in label:
                print(f"  Failed at [{label}]: {out.strip()[:200]}")
                return False
            else:
                print(f"  [{label}] OK")

        print(f"  Pushed successfully to {repo_url}\n")
        return True

    # ── Incident Workflow ─────────────────────────────────────────────────────

    async def handle_incident(self, incident: Incident) -> None:
        logger.info(f"[Orchestrator] Handling: {incident}")

        self.state_manager.add_incident(incident)
        self.state_manager.update_incident_status(incident.incident_id, IncidentStatus.INVESTIGATING)
        self.context_manager.create_context(incident)

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

    async def _on_incident_created(self, event: Event) -> None:
        logger.info(f"[Orchestrator] Incident created → approval before Knowledge Agent")

        approved = await self.approval.request_approval(
            title="Incident detected — run Knowledge Agent to investigate?",
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
            solution = await knowledge_agent.investigate(context, self.approval)
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
        logger.info(f"[Orchestrator] Investigation complete → approval before Self-Healing")

        solution: Solution = event.data.get("solution")

        approved = await self.approval.request_approval(
            title="Investigation complete — apply self-healing fix?",
            details=[
                f"Root cause : {getattr(solution, 'root_cause', 'unknown')[:100]}",
                f"Confidence : {getattr(solution, 'confidence', 0):.0%}",
                f"Commands   : {', '.join(getattr(solution, 'suggested_commands', [])[:2])}",
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
        logger.info(f"[Orchestrator] Remediation complete → resolving incident")
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.RESOLVED)
        await self._send_alert(
            incident_id=event.incident_id,
            title="Incident Resolved",
            message=f"Incident {event.incident_id} has been resolved automatically.",
        )
        self.context_manager.drop_context(event.incident_id)

    async def _on_remediation_failed(self, event: Event) -> None:
        logger.warning(f"[Orchestrator] Remediation failed for {event.incident_id}")
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.FAILED)
        await self._send_alert(
            incident_id=event.incident_id,
            title="Remediation Failed",
            message=f"Incident {event.incident_id} could not be resolved. Manual intervention required.",
        )

    async def _send_alert(self, incident_id: str, title: str, message: str) -> None:
        alerting_agent = self.registry.get_agent("alerting_agent")
        if not alerting_agent:
            logger.warning("[Orchestrator] Alerting Agent not registered — skipping alert")
            return
        try:
            await alerting_agent.send(incident_id=incident_id, title=title, message=message)
        except Exception as e:
            logger.error(f"[Orchestrator] Alerting Agent failed: {e}")

    def summary(self) -> dict:
        return {
            "orchestrator" : "running" if self._running else "stopped",
            "agents"       : self.registry.summary(),
            "state"        : self.state_manager.summary(),
            "event_history": len(self.event_bus.get_history()),
        }

    def __repr__(self):
        return f"Orchestrator(running={self._running}, agents={self.registry.get_all_names()})"