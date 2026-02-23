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
    idea_id: int
    title: str
    spec_path: str
    queue_job_id: str
    status: Literal["queued", "started", "completed", "failed"]
    queued_at: datetime


class PatchApplication(BaseModel):
    """Persona patch application record."""
    patch_id: str
    persona_id: str
    from_version: str | None = None
    to_version: str | None = None
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
    patch_count: int = 0
    errors: list[str] = Field(default_factory=list)


class GateStatus(BaseModel):
    """Status of a gate (triage, build, patch) for circuit breaker."""
    gate: Literal["triage", "build", "patch"]
    consecutive_failures: int = 0
    halted: bool = False
    last_error: str | None = None
