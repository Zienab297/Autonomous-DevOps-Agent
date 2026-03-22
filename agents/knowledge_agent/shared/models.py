"""
shared/models.py
--------------
All shared dataclasses and enums for the Knowledge Agent SDK.

Sections:
  1. Enums
  2. Ingestion models      (pipeline internal)
  3. RAG / Retrieval models
  4. Incident & Fix models (cross-agent)
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4


# ── 1. Enums ─────────────────────────────────────────────────────────────────

class ErrorCategory(str, Enum):
    DOCKER                    = "Docker"
    DOCKER_COMPOSE            = "Docker Compose"
    GITHUB_ACTIONS            = "GitHub Actions"
    KUBERNETES                = "Kubernetes"
    HELM                      = "Helm"
    CICD_GENERAL              = "CI/CD General"
    SECURITY                  = "Security"
    NETWORKING_INFRASTRUCTURE = "Networking & Infrastructure"
    DATABASE_REGISTRY         = "Database & Registry"
    UNKNOWN                   = "Unknown"


class IncidentStatus(str, Enum):
    OPEN          = "Open"
    INVESTIGATING = "Investigating"
    FIX_GENERATED = "Fix Generated"
    FIX_APPLIED   = "Fix Applied"
    RESOLVED      = "Resolved"
    FAILED        = "Failed"
    REOPENED      = "Reopened"


class RAGSource(str, Enum):
    KNOWLEDGE_BASE = "knowledge_base"  # found in Qdrant (score >= threshold)
    LLM_GENERATED  = "llm_generated"   # Gemini + web search fallback


# ── 2. Ingestion models ───────────────────────────────────────────────────────

@dataclass
class ParsedEntry:
    """
    Output of ingestion/parser.py
    text     → sent to Gemini embedding
    metadata → stored in Qdrant payload
    """
    text: str
    metadata: dict


@dataclass
class Chunk:
    """Output of ingestion/chunker.py — 1 chunk per entry."""
    text: str
    metadata: dict


@dataclass
class EmbeddedChunk:
    """Output of ingestion/embedder.py — vector + original data."""
    vector: list[float]
    text: str
    metadata: dict


# ── 3. RAG / Retrieval models ─────────────────────────────────────────────────

@dataclass
class RAGResult:
    """
    Output of agent/retriever.py
    Populated when Qdrant finds a match (score >= threshold).
    Attached to the Incident as knowledge_base_match.
    """
    entry_id:       str
    category:       ErrorCategory
    confidence:     float           # cosine similarity score 0.0 → 1.0
    healing_prompt: str
    root_cause:     str
    error_pattern:  str


@dataclass
class RetrievalResult:
    """
    Internal result from agent/retriever.py before building RAGResult.
    found=False means score < threshold → LLM fallback.
    """
    found: bool
    score: float
    entry: dict = field(default_factory=dict)   # raw Qdrant payload if found


@dataclass
class GeneratedSolution:
    """
    Output of agent/llm_generator.py
    Used when RetrievalResult.found is False.
    """
    healing_prompt:     str
    confidence:         float
    source:             RAGSource = RAGSource.LLM_GENERATED
    web_sources:        list[str] = field(default_factory=list)
    suggested_commands: list[str] = field(default_factory=list)


@dataclass
class AgentResponse:
    """
    Final output of agent/knowledge_agent.py
    Sent directly to the Self-Healing Agent.
    """
    source:             RAGSource
    confidence:         float
    healing_prompt:     str
    category:           ErrorCategory       = ErrorCategory.UNKNOWN
    suggested_commands: list[str]           = field(default_factory=list)
    action_needed:      bool                = True
    rag_result:         Optional[RAGResult] = None  # set if source=KNOWLEDGE_BASE
    web_sources:        list[str]           = field(default_factory=list)


# ── 4. Incident & Fix models ──────────────────────────────────────────────────

@dataclass
class Incident:
    """
    Created by the Monitoring Agent when an error is detected.
    Updated by the Knowledge Agent and Self-Healing Agent as work progresses.
    """
    # --- set at detection time ---
    category:      ErrorCategory
    error_message: str
    service:       str
    severity:      str
    failed_file:   Optional[str] = None

    # --- auto generated ---
    id:         str            = field(default_factory=lambda: str(uuid4()))
    status:     IncidentStatus = IncidentStatus.OPEN
    created_at: datetime       = field(default_factory=datetime.now)

    # --- filled by Knowledge Agent ---
    knowledge_base_match: Optional[RAGResult]     = None  # was: Optional[str]
    suggested_fix:        Optional[str]           = None
    agent_response:       Optional[AgentResponse] = None

    # --- filled after resolution ---
    resolved_at: Optional[datetime] = None


@dataclass
class FixRecord:
    """
    Created by the Self-Healing Agent after applying a fix.
    Linked to an Incident by incident_id.
    """
    # --- required at creation ---
    incident_id:  str
    file_changed: str

    # --- auto generated ---
    id:         str      = field(default_factory=lambda: str(uuid4()))
    applied_at: datetime = field(default_factory=datetime.now)

    # --- filled after verification ---
    success: Optional[bool] = None


@dataclass
class DeploymentRecord:
    """
    Created by the CI/CD Agent after a deployment.
    """
    service:         str
    version:         str
    branch:          str
    triggered_by:    str
    files_generated: list[str] = field(default_factory=list)

    # --- auto generated ---
    id:          str      = field(default_factory=lambda: str(uuid4()))
    deployed_at: datetime = field(default_factory=datetime.now)
    success:     Optional[bool] = None