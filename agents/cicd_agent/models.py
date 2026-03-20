from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeploymentStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class Pipeline:
    id: str
    repo: str
    branch: str
    status: PipelineStatus
    provider: str
    triggered_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    logs_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        return self.status in (
            PipelineStatus.SUCCESS,
            PipelineStatus.FAILED,
            PipelineStatus.CANCELLED,
        )


@dataclass
class Deployment:
    id: str
    service: str
    branch: str
    version: str
    status: DeploymentStatus
    provider: str
    deployed_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    rollback_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RollbackResult:
    deployment_id: str
    service: str
    from_version: str
    to_version: str
    status: DeploymentStatus
    executed_at: datetime = field(default_factory=datetime.utcnow)
    message: str = ""


@dataclass
class DeploymentLog:
    deployment_id: str
    lines: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.utcnow)