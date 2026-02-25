"""
Triage Gate - Gate 1
Reads scored ideas from IdeaForge, applies threshold-based decisions.
Approved ideas are enqueued into the priority queue for build dispatch.
"""
import json
from datetime import datetime
from typing import TYPE_CHECKING

from config import Config
from models import TriageDecision, PriorityItem
from db import StateDB
from audit import AuditLogger
from readers.ideaforge_reader import IdeaForgeReader


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

        # Filter out ideas already triaged in a previous cycle
        already_triaged = self.state_db.get_triaged_idea_ids()
        ideas = [i for i in ideas if i["id"] not in already_triaged]

        if not ideas:
            return []

        decisions = []
        approve_count = 0

        for idea in ideas:
            # Scale score from 0-10 to 0-100
            scaled_score = idea["weighted_score"] * 10

            # Apply threshold-based decision
            if scaled_score >= self.config.approve_threshold:
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
                    )
                    self.state_db.enqueue_item(item)

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
