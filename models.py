"""
Metroplex Pydantic Models
All data models for triage decisions, build jobs, patches, cycles, and gate status.
"""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


class TriageDecision(BaseModel):
    """Triage decision for an idea."""
    idea_id: int
    title: str
    weighted_score: float
    scaled_score: float
    decision: Literal["approve", "reject", "defer"]
    reason: str
    decided_at: datetime


class BuildJob(BaseModel):
    """Build job for an approved idea."""
    idea_id: str | int  # str for skylynx/linear source IDs, int for ideaforge
    title: str
    spec_path: str
    queue_job_id: str
    status: Literal["queued", "started", "completed", "failed"]
    queued_at: datetime
    strategic_theme: str | None = None


class PatchApplication(BaseModel):
    """Persona patch application record."""
    patch_id: str
    persona_id: str
    from_version: str | None = None
    to_version: str | None = None
    status: Literal["applied", "failed", "skipped"]
    reason: str
    applied_at: datetime


class AgentPatchApplication(BaseModel):
    """Agent patch application record (CLAUDE.md / agent.yaml section patches)."""
    patch_id: str
    agent_id: str
    target: str  # "claude_md" | "agent_yaml"
    section: str
    operation: str  # "add" | "replace" | "remove"
    status: Literal["applied", "failed", "skipped"]
    reason: str
    applied_at: datetime


class CycleResult(BaseModel):
    """Result of a full Metroplex cycle."""
    cycle_id: str
    started_at: datetime
    completed_at: datetime | None = None
    triage_count: int = 0
    build_count: int = 0
    publish_count: int = 0
    patch_count: int = 0
    errors: list[str] = Field(default_factory=list)


class GateStatus(BaseModel):
    """Status of a gate (triage, build, publish, patch) for circuit breaker."""
    gate: Literal["triage", "build", "publish", "patch"]
    consecutive_failures: int = 0
    halted: bool = False
    last_error: str | None = None


class PublishJob(BaseModel):
    """Publish job for pushing completed builds to GitHub."""
    build_job_id: str
    title: str
    repo_name: str
    repo_url: str | None = None
    status: Literal["pending", "published", "failed"]
    error: str | None = None
    project_dir: str
    created_at: datetime = Field(default_factory=datetime.now)
    published_at: datetime | None = None


class PriorityItem(BaseModel):
    """Item in the Metroplex priority queue. Represents a task from any input stream."""
    id: int | None = None  # DB-assigned
    source: Literal["ideaforge", "skylynx", "linear", "academy"]
    source_id: str  # ID within the source system (e.g. IdeaForge idea ID)
    title: str
    description: str
    priority_score: float  # Combined ranking score
    status: Literal["pending", "dispatched", "completed", "failed"] = "pending"
    idea_data: str = ""  # JSON string with full data needed for spec generation
    created_at: datetime = Field(default_factory=datetime.now)
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None
    strategic_theme: str | None = None
