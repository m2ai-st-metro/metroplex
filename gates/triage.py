"""
Triage Gate - Gate 1
Reads scored ideas from IdeaForge, applies threshold-based decisions.
Approved ideas are enqueued into the priority queue for build dispatch.
"""
import json
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# Internal ST Metro infrastructure names — ideas referencing these are self-referential
# and should not enter the pipeline for external distribution.
# This is a content filter, NOT a dispatch list. Agent identity terms (galvatron, starscream,
# soundwave) stay here permanently: even after retirement and revival in CCOS, their work must
# not re-enter ingestion and loop back through the pipeline.
INTERNAL_KEYWORDS = {
    "ultra magnus", "metroplex", "st metro", "ideaforge", "idea forge",
    "sky-lynx", "skylynx", "galvatron", "starscream", "soundwave",
    "yce harness", "st records", "swindle", "teletraan",
    "m2ai-portfolio", "m2ai portfolio",
}

from config import Config
from models import TriageDecision, PriorityItem
from db import StateDB
from audit import AuditLogger
from readers.ideaforge_reader import IdeaForgeReader


def _is_internal_project(idea: dict) -> bool:
    """Check if an idea references internal ST Metro infrastructure."""
    text = " ".join([
        idea.get("title", ""),
        idea.get("description", ""),
        idea.get("problem_statement", ""),
    ]).lower()
    return any(kw in text for kw in INTERNAL_KEYWORDS)


def _normalize_title(title: str) -> str:
    """Normalize a title for dedup comparison."""
    t = title.lower().strip()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _is_duplicate(title: str, existing_titles: list[str], threshold: float = 0.85) -> str | None:
    """Check if a title is a duplicate of any existing title.

    Returns the matching title if found, None otherwise.
    Uses SequenceMatcher for fuzzy matching — catches 'AgentGuard' vs 'Agent Guard',
    'AI Circuit Breaker' vs 'AI Agent Circuit Breaker', etc.
    """
    norm = _normalize_title(title)
    for existing in existing_titles:
        norm_existing = _normalize_title(existing)
        ratio = SequenceMatcher(None, norm, norm_existing).ratio()
        if ratio >= threshold:
            return existing
    return None


class TriageGate:
    """Gate 1: Idea Triage - Score & Decision Logic."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        ideaforge_reader: IdeaForgeReader,
        audit_logger: AuditLogger
    ):
        """
        Initialize Triage Gate.

        Args:
            config: Metroplex configuration
            state_db: State database manager
            ideaforge_reader: IdeaForge database reader
            audit_logger: Audit logger
        """
        self.config = config
        self.state_db = state_db
        self.ideaforge_reader = ideaforge_reader
        self.audit_logger = audit_logger

    def run(self, dry_run: bool = False) -> list[TriageDecision]:
        """
        Run triage gate on unprocessed ideas.

        Process flow:
        1. Get unprocessed ideas from IdeaForge
        2. Scale weighted_score from 0-10 to 0-100
        3. Apply threshold-based decisions
        4. Enforce per-cycle approval cap
        5. Record decisions (unless dry_run)

        Args:
            dry_run: If True, print decisions but don't write to DB

        Returns:
            List of TriageDecision objects
        """
        # Get unprocessed ideas
        if self.ideaforge_reader is None:
            print("Warning: IdeaForge reader not initialized (DB not found)")
            return []

        ideas = self.ideaforge_reader.get_unprocessed_ideas()

        # Filter out ideas that have a final triage decision (approve/reject).
        # Deferred ideas are allowed back for re-triage — their score may have
        # changed since the original deferral (e.g. re-scoring by IdeaForge).
        already_triaged = self.state_db.get_triaged_idea_ids(decisions=("approve", "reject"))
        ideas = [i for i in ideas if i["id"] not in already_triaged]

        # Filter out self-referential internal infrastructure projects
        internal_ideas = [i for i in ideas if _is_internal_project(i)]
        for idea in internal_ideas:
            decision = TriageDecision(
                idea_id=idea["id"],
                title=idea["title"],
                weighted_score=idea.get("weighted_score", 0) or 0,
                scaled_score=0.0,
                decision="reject",
                reason="self-referential internal project",
                decided_at=datetime.now(),
            )
            if not dry_run:
                self.state_db.record_triage_decision(decision)
            self.audit_logger.log_decision(
                gate="triage",
                action="reject",
                details={
                    "idea_id": idea["id"],
                    "title": idea["title"],
                    "reason": "self-referential internal project",
                },
            )
        ideas = [i for i in ideas if not _is_internal_project(i)]

        if not ideas:
            return []

        # Load titles of already-approved/built ideas for dedup
        approved_titles = self.state_db.get_approved_titles()

        decisions = []
        approve_count = 0

        for idea in ideas:
            # IdeaForge scores are 0-10; scale to 0-100 for threshold comparison.
            # Guard: if a score is already >10, it's likely pre-scaled or corrupt.
            raw_score = idea["weighted_score"]
            if raw_score is None or raw_score < 0:
                continue
            if raw_score > 10:
                logger.warning(
                    "Idea %s has weighted_score %.1f (expected 0-10). Skipping.",
                    idea["id"], raw_score,
                )
                continue
            scaled_score = raw_score * 10

            # Dedup: reject if title matches an already-approved idea
            dup_match = _is_duplicate(idea["title"], approved_titles)
            if dup_match:
                decision = "reject"
                reason = f"duplicate of already-approved: {dup_match}"
            # Apply threshold-based decision
            elif scaled_score >= self.config.approve_threshold:
                if approve_count < self.config.max_approve_per_cycle:
                    decision = "approve"
                    reason = "meets approval threshold"
                    approve_count += 1
                else:
                    decision = "defer"
                    reason = "per-cycle cap reached"
            elif scaled_score < self.config.reject_threshold:
                decision = "reject"
                reason = "below rejection threshold"
            else:
                decision = "defer"
                reason = "in deferral range"

            # Check deferral count before creating redundant defer records.
            # Ideas that have already been deferred max_deferrals times should
            # be auto-rejected instead of accumulating unbounded defer rows.
            if decision == "defer":
                deferral_count = self.state_db.get_deferral_count(idea["id"])
                if deferral_count >= self.config.max_deferrals:
                    decision = "reject"
                    reason = f"exceeded max deferrals ({deferral_count})"

            # Create TriageDecision object
            triage_decision = TriageDecision(
                idea_id=idea["id"],
                title=idea["title"],
                weighted_score=idea["weighted_score"],
                scaled_score=scaled_score,
                decision=decision,
                reason=reason,
                decided_at=datetime.now()
            )

            decisions.append(triage_decision)

            if dry_run:
                # Print decision to stdout
                print(self._format_decision(idea, scaled_score, decision))
            else:
                # Record decision in state DB
                self.state_db.record_triage_decision(triage_decision)

                # Enqueue approved ideas into priority queue
                if decision == "approve":
                    priority_score = scaled_score * self.config.ideaforge_weight
                    item = PriorityItem(
                        source="ideaforge",
                        source_id=str(idea["id"]),
                        title=idea["title"],
                        description=idea.get("description", idea["title"]),
                        priority_score=priority_score,
                        idea_data=json.dumps(idea, default=str),
                        strategic_theme=idea.get("strategic_theme"),
                    )
                    self.state_db.enqueue_item(item)

                # Claim idea in IdeaForge and update status
                self.ideaforge_reader.claim_idea(idea["id"])
                if decision == "approve":
                    self.ideaforge_reader.update_idea_status(idea["id"], "exported")

                # Log decision in audit log
                self.audit_logger.log_decision(
                    gate="triage",
                    action=decision,
                    details={
                        "idea_id": idea["id"],
                        "title": idea["title"],
                        "weighted_score": idea["weighted_score"],
                        "scaled_score": scaled_score,
                        "reason": reason
                    }
                )

        return decisions

    def _format_decision(self, idea: dict, scaled_score: float, decision: str) -> str:
        """
        Format decision as human-readable single-line summary.

        Args:
            idea: Idea dictionary from IdeaForge
            scaled_score: Scaled score (0-100)
            decision: Decision (approve/reject/defer)

        Returns:
            Human-readable decision string
        """
        return f"[{decision.upper()}] ID={idea['id']} Score={scaled_score:.1f} Title='{idea['title']}'"
