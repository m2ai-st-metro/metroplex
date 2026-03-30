"""
Outcome Emitter - Phase 14a
Emits OutcomeRecords to ST Records when ideas reach terminal states.
Uses ContractStore via sys.path injection for dual-write (JSONL + SQLite).
"""
import logging
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# sys.path injection for st-records contracts (not a pip package)
_ST_RECORDS_ROOT = Path(__file__).parent.parent / "st-records"
if str(_ST_RECORDS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ST_RECORDS_ROOT))


class OutcomeEmitter:
    """Emits OutcomeRecords to ST Records' dual-write store."""

    def __init__(self, st_records_data_dir: Path | None = None):
        """
        Initialize the emitter.

        Args:
            st_records_data_dir: Path to st-records/data/. If None, uses default.

        Raises:
            ImportError: If st-records contracts are not found.
        """
        from contracts.store import ContractStore

        self.store = ContractStore(data_dir=st_records_data_dir)
        self._emit_count = 0
        self._emitted: dict[tuple[int, str], int] = {}  # (idea_id, outcome) -> count
        self.max_emissions_per_idea = 3

    def emit(
        self,
        idea_id: int,
        idea_title: str,
        outcome: str,
        overall_score: float | None = None,
        build_outcome: str | None = None,
        artifact_count: int = 0,
        tech_stack: list[str] | None = None,
        github_url: str | None = None,
        pipeline_trace: list[dict] | None = None,
        tags: list[str] | None = None,
        idea_type: str | None = None,
    ) -> bool:
        """
        Emit an OutcomeRecord for a terminal-state idea.

        Args:
            idea_id: IdeaForge idea ID
            idea_title: Idea title
            outcome: Terminal outcome (published, rejected, deferred, build_failed, feature_backlog)
            overall_score: Scaled triage score (0-100) if available
            build_outcome: Build result description
            artifact_count: Number of artifacts produced
            tech_stack: Technologies used
            github_url: Published repo URL
            pipeline_trace: List of stage trace dicts
            tags: Tags for categorization
            idea_type: Type of idea (tool, agent, etc.)

        Returns:
            True if emitted successfully, False on error.
        """
        from contracts.outcome_record import OutcomeRecord, TerminalOutcome, PipelineTrace

        # Dedup guard: prevent cascading duplicate emissions per session
        key = (idea_id, outcome)
        prior = self._emitted.get(key, 0)
        if prior >= self.max_emissions_per_idea:
            logger.debug(
                "Suppressed duplicate outcome: idea=%s outcome=%s (already emitted %d times)",
                idea_id, outcome, prior,
            )
            return True

        try:
            # Build pipeline trace objects
            trace_objects = []
            if pipeline_trace:
                for t in pipeline_trace:
                    trace_objects.append(PipelineTrace(
                        stage=t["stage"],
                        entered_at=t.get("entered_at", datetime.now()),
                        exited_at=t.get("exited_at"),
                        persona_used=t.get("persona_used"),
                    ))

            record = OutcomeRecord(
                idea_id=idea_id,
                idea_title=idea_title,
                outcome=TerminalOutcome(outcome),
                overall_score=overall_score,
                build_outcome=build_outcome,
                artifact_count=artifact_count,
                tech_stack=tech_stack or [],
                github_url=github_url,
                pipeline_trace=trace_objects,
                tags=tags or [],
                idea_type=idea_type,
                emitted_at=datetime.now(),
            )

            self.store.write_outcome(record)
            self._emit_count += 1
            self._emitted[key] = prior + 1
            logger.info(
                "Outcome emitted: idea=%s outcome=%s title=%s",
                idea_id, outcome, idea_title,
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to emit outcome for idea %s (%s): %s",
                idea_id, idea_title, e,
            )
            return False

    @property
    def emit_count(self) -> int:
        """Number of outcomes emitted in this session."""
        return self._emit_count

    def close(self):
        """Close the underlying store."""
        self.store.close()


def create_outcome_emitter(st_records_data_dir: Path | None = None) -> OutcomeEmitter | None:
    """Factory function that returns None if st-records is unavailable."""
    try:
        return OutcomeEmitter(st_records_data_dir=st_records_data_dir)
    except (ImportError, FileNotFoundError) as e:
        logger.warning("OutcomeEmitter unavailable: %s", e)
        return None
