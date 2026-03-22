"""
knowledge_core/service_manager.py
----------------------------------
Loads service_graph.json and deployments.json.

Provides:
  - get_dependencies(service)      → what does this service depend on
  - get_dependents(service)        → what depends on this service
  - is_cascading(service)          → is this a cascading failure
  - get_recent_deployments(service) → deployments in last N minutes
  - was_recently_deployed(service)  → True/False
"""

import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServiceInfo:
    name:        str
    depends_on:  list[str] = field(default_factory=list)
    depended_by: list[str] = field(default_factory=list)
    type:        str = "unknown"
    critical:    bool = False


@dataclass
class DeploymentInfo:
    id:           str
    service:      str
    version:      str
    status:       str
    started_at:   str
    triggered_by: str
    changes:      list[str] = field(default_factory=list)
    error:        str = ""


class ServiceManager:

    def __init__(self, data_path: str = None):
        if data_path is None:
            _current = Path(__file__).resolve().parent
            while _current != _current.parent:
                if _current.name == "knowledge_agent":
                    data_path = str(_current / "data")
                    break
                _current = _current.parent

        data_dir = Path(data_path)

        # load service graph
        with open(data_dir / "service_graph.json", encoding="utf-8") as f:
            svc_data = json.load(f)
        self._services: dict[str, ServiceInfo] = {
            name: ServiceInfo(
                name        = name,
                depends_on  = info.get("depends_on", []),
                depended_by = info.get("depended_by", []),
                type        = info.get("type", "unknown"),
                critical    = info.get("critical", False),
            )
            for name, info in svc_data.get("services", {}).items()
        }

        # load deployments
        with open(data_dir / "deployments.json", encoding="utf-8") as f:
            dep_data = json.load(f)
        self._deployments: list[DeploymentInfo] = [
            DeploymentInfo(
                id           = d.get("id", ""),
                service      = d.get("service", ""),
                version      = d.get("version", ""),
                status       = d.get("status", ""),
                started_at   = d.get("started_at", ""),
                triggered_by = d.get("triggered_by", ""),
                changes      = d.get("changes", []),
                error        = d.get("error", ""),
            )
            for d in dep_data.get("deployments", [])
        ]

        print(f"[ServiceManager] Loaded {len(self._services)} services, "
              f"{len(self._deployments)} deployments")

    # ── service graph ─────────────────────────────────────────────────────────

    def get_service(self, name: str) -> ServiceInfo | None:
        return self._services.get(name)

    def get_dependencies(self, service: str) -> list[str]:
        """What does this service depend on."""
        svc = self._services.get(service)
        return svc.depends_on if svc else []

    def get_dependents(self, service: str) -> list[str]:
        """What services depend on this service."""
        svc = self._services.get(service)
        return svc.depended_by if svc else []

    def is_cascading(self, service: str) -> bool:
        """
        True if this service has dependents —
        meaning its failure can cascade to other services.
        """
        return len(self.get_dependents(service)) > 0

    def get_blast_radius(self, service: str) -> list[str]:
        """
        Returns all services that will be affected if this service fails.
        Walks the dependency tree upward.
        """
        affected = set()
        queue    = [service]

        while queue:
            current = queue.pop(0)
            for dependent in self.get_dependents(current):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)

        return list(affected)

    # ── deployments ───────────────────────────────────────────────────────────

    def get_recent_deployments(self, service: str, within_minutes: int = 30) -> list[DeploymentInfo]:
        """
        Return deployments for a service in the last N minutes.
        Used to correlate incidents with recent changes.
        """
        now = datetime.now(timezone.utc)
        recent = []

        for dep in self._deployments:
            if dep.service != service:
                continue
            try:
                started = datetime.fromisoformat(dep.started_at.replace("Z", "+00:00"))
                diff_minutes = (now - started).total_seconds() / 60
                if diff_minutes <= within_minutes:
                    recent.append(dep)
            except Exception:
                continue

        return recent

    def was_recently_deployed(self, service: str, within_minutes: int = 30) -> bool:
        """True if the service was deployed recently."""
        return len(self.get_recent_deployments(service, within_minutes)) > 0

    def get_failed_deployments(self, service: str) -> list[DeploymentInfo]:
        """Return all failed deployments for a service."""
        return [
            d for d in self._deployments
            if d.service == service and d.status == "failed"
        ]

    def analyze(self, service: str) -> dict:
        """
        Full system-aware analysis for a service.
        Returns a summary of service context before Qdrant search.
        """
        svc = self._services.get(service)

        if not svc:
            return {
                "service":          service,
                "found":            False,
                "is_cascading":     False,
                "blast_radius":     [],
                "recent_deploy":    False,
                "failed_deploy":    False,
                "dependencies":     [],
                "dependents":       [],
            }

        recent_deploys  = self.get_recent_deployments(service)
        failed_deploys  = self.get_failed_deployments(service)
        blast_radius    = self.get_blast_radius(service)

        result = {
            "service":          service,
            "found":            True,
            "type":             svc.type,
            "critical":         svc.critical,
            "is_cascading":     self.is_cascading(service),
            "blast_radius":     blast_radius,
            "dependencies":     svc.depends_on,
            "dependents":       svc.depended_by,
            "recent_deploy":    len(recent_deploys) > 0,
            "recent_deploys":   [d.id for d in recent_deploys],
            "failed_deploy":    len(failed_deploys) > 0,
            "failed_deploys":   [d.id for d in failed_deploys],
        }

        # print summary
        print(f"[ServiceManager] Analysis for '{service}':")
        print(f"  type         : {svc.type}")
        print(f"  critical     : {svc.critical}")
        print(f"  depends_on   : {svc.depends_on}")
        print(f"  depended_by  : {svc.depended_by}")
        print(f"  blast_radius : {blast_radius}")
        print(f"  recent_deploy: {len(recent_deploys) > 0}")
        print(f"  failed_deploy: {len(failed_deploys) > 0}")

        return result