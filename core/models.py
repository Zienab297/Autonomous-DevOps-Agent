#pydantic

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4

class ErrorCategory(str, Enum):
    DOCKER = "Docker"
    DOCKER_COMPOSE = "Docker Compose"
    GITHUB_ACTIONS = "GitHub Actions"
    KUBERNETES = "Kubernetes"
    HELM = "Helm"
    CICD_GENERAL = "CI/CD General"
    SECURITY = "Security"
    NETWORKING_INFRASTRUCTURE = "Networking & Infrastructure"
    DATABASE_REGISTRY = "Database & Registry"
    UNKNOWN = "Unknown"


class IncidentStatus(str, Enum):
    OPEN = "Open"
    INVESTIGATING = "Investigating"
    FIX_GENERATED = "Fix Generated"
    FIX_APPLIED = "Fix Applied"
    RESOLVED = "Resolved"
    FAILED = "Failed"
    REOPENED = "Reopened"

@dataclass
class Incident:
    # --- at detection time ---
    category: ErrorCategory
    error_message: str
    service: str
    severity: str
    failed_file: Optional[str] = None

    # --- auto generated ---
    id: str = field(default_factory=lambda: str(uuid4()))
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime = field(default_factory=datetime.now)

    # --- filled in by agents ---
    knowledge_base_match: Optional[str] = None
    suggested_fix: Optional[str] = None
    resolved_at: Optional[datetime] = None

@dataclass
class FixRecord:
    # --- required at creation ---
    incident_id: str
    file_changed: str

    # --- auto generated ---
    id: str = field(default_factory=lambda: str(uuid4()))
    applied_at: datetime = field(default_factory=datetime.now)

    # --- filled after verification ---
    success: Optional[bool] = None


@dataclass
class DeploymentRecord:
    # --- required at creation ---
    service: str
    version: str
    branch: str
    triggered_by: str
    files_generated: list[str]
@dataclass
class RAGResult:
    entry_id: str
    category: ErrorCategory
    confidence: float
    healing_prompt: str
    root_cause: str
    error_pattern: str