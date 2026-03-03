"""
Metroplex Dispatcher
Routes completed priority queue items to EA-Claude workers via direct SQLite write.

Follows the Notifier/LogNotifier pattern:
- Protocol defines the interface
- EAClaudeDispatcher writes to claudeclaw.db dispatch_queue
- LogDispatcher is a no-op fallback for testing/dry-run
- create_dispatcher() factory selects based on config

Phase 9 scope: CLI-only dispatch. Full orchestrator integration deferred to Phase 10.
"""
import sqlite3
import uuid
import time
import json
from typing import Protocol, runtime_checkable
from pathlib import Path


# Worker routing: map (source, recommendation_type) -> worker_type
WORKER_ROUTES = {
    # Sky-Lynx recommendation types
    ("skylynx", "claude_md_update"): "ravage",
    ("skylynx", "pipeline_change"): "ravage",
    ("skylynx", "infrastructure"): "ravage",
    ("skylynx", "case_study_addition"): "soundwave",
    ("skylynx", ""): "ravage",  # default for unknown skylynx types
    # Linear issues default to ravage (coding tasks)
    ("linear", ""): "ravage",
    # IdeaForge items default to ravage
    ("ideaforge", ""): "ravage",
}

VALID_WORKER_TYPES = {"starscream", "ravage", "soundwave", "astrotrain", "default"}


@runtime_checkable
class Dispatcher(Protocol):
    """Protocol for dispatching tasks to workers."""

    def dispatch(
        self,
        prompt: str,
        worker_type: str,
        chat_id: str = "",
        metadata: dict | None = None,
    ) -> str:
        """
        Dispatch a task to a worker.

        Args:
            prompt: Task instruction for the worker
            worker_type: Target worker ('ravage', 'soundwave', etc.)
            chat_id: Telegram chat ID for result delivery
            metadata: Optional metadata dict (not stored in DB, for logging)

        Returns:
            Task ID (UUID string)
        """
        ...

    def check_result(self, task_id: str) -> dict | None:
        """
        Check the result of a dispatched task.

        Args:
            task_id: UUID of the task

        Returns:
            Task dict with status/result/error, or None if not found
        """
        ...


class EAClaudeDispatcher:
    """Dispatches tasks to EA-Claude workers via direct SQLite write."""

    def __init__(self, db_path: str, default_chat_id: str = ""):
        """
        Initialize EA-Claude dispatcher.

        Args:
            db_path: Path to claudeclaw.db
            default_chat_id: Default Telegram chat ID for result delivery

        Raises:
            FileNotFoundError: If db_path does not exist
        """
        self.db_path = db_path
        self.default_chat_id = default_chat_id

        if not Path(db_path).exists():
            raise FileNotFoundError(f"EA-Claude database not found at {db_path}")

    def dispatch(
        self,
        prompt: str,
        worker_type: str,
        chat_id: str = "",
        metadata: dict | None = None,
    ) -> str:
        """
        Enqueue a task into the EA-Claude dispatch_queue.

        Args:
            prompt: Full task instruction
            worker_type: Target worker type
            chat_id: Telegram chat ID (falls back to default_chat_id)
            metadata: Optional metadata (logged but not stored in DB)

        Returns:
            UUID of the created task

        Raises:
            ValueError: If worker_type is invalid
        """
        if worker_type not in VALID_WORKER_TYPES:
            raise ValueError(
                f"Invalid worker_type '{worker_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_WORKER_TYPES))}"
            )

        effective_chat_id = chat_id or self.default_chat_id
        if not effective_chat_id:
            raise ValueError(
                "No chat_id provided and no default_chat_id configured. "
                "Set METROPLEX_DISPATCH_CHAT_ID or pass chat_id."
            )

        task_id = str(uuid.uuid4())
        now = int(time.time())

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        try:
            conn.execute(
                """INSERT INTO dispatch_queue
                   (id, chat_id, prompt, worker_type, status, created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?)""",
                (task_id, effective_chat_id, prompt, worker_type, now),
            )
            conn.commit()
        finally:
            conn.close()

        return task_id

    def check_result(self, task_id: str) -> dict | None:
        """
        Poll the dispatch_queue for a task's current state.

        Args:
            task_id: UUID of the task

        Returns:
            Dict with all task fields, or None if not found
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")

        try:
            row = conn.execute(
                "SELECT * FROM dispatch_queue WHERE id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


class LogDispatcher:
    """No-op dispatcher that logs dispatch calls. Used when no DB is configured."""

    def __init__(self):
        self.dispatched: list[dict] = []

    def dispatch(
        self,
        prompt: str,
        worker_type: str,
        chat_id: str = "",
        metadata: dict | None = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        self.dispatched.append({
            "task_id": task_id,
            "prompt": prompt,
            "worker_type": worker_type,
            "chat_id": chat_id,
            "metadata": metadata,
        })
        print(f"  [dispatch/log] Would dispatch to {worker_type}: {prompt[:80]}...")
        return task_id

    def check_result(self, task_id: str) -> dict | None:
        return None


def create_dispatcher(dispatch_db: str, dispatch_chat_id: str) -> Dispatcher:
    """
    Factory: create the appropriate dispatcher based on config.

    Args:
        dispatch_db: Path to claudeclaw.db (empty string = use LogDispatcher)
        dispatch_chat_id: Telegram chat ID for result delivery

    Returns:
        EAClaudeDispatcher if db exists, LogDispatcher otherwise
    """
    if dispatch_db and Path(dispatch_db).exists():
        return EAClaudeDispatcher(dispatch_db, dispatch_chat_id)
    else:
        if dispatch_db:
            print(f"Warning: Dispatch DB not found at {dispatch_db}, using log dispatcher")
        return LogDispatcher()


def route_to_worker(source: str, recommendation_type: str = "") -> str:
    """
    Determine the appropriate worker for a queue item.

    Args:
        source: Item source ('ideaforge', 'skylynx', 'linear')
        recommendation_type: Type of recommendation (for skylynx items)

    Returns:
        Worker type string
    """
    # Try specific (source, type) match first
    worker = WORKER_ROUTES.get((source, recommendation_type))
    if worker:
        return worker

    # Fall back to (source, "") default
    worker = WORKER_ROUTES.get((source, ""))
    if worker:
        return worker

    return "default"


def build_dispatch_prompt(item: dict) -> str:
    """
    Build a task prompt for a worker from a priority queue item.

    Args:
        item: Priority queue item dict (from StateDB)

    Returns:
        Formatted prompt string for the worker
    """
    idea_data = {}
    if item.get("idea_data"):
        try:
            idea_data = json.loads(item["idea_data"])
        except (json.JSONDecodeError, TypeError):
            pass

    source = item.get("source", "unknown")
    title = item.get("title", "Untitled")
    description = idea_data.get("description", item.get("description", ""))
    problem = idea_data.get("problem_statement", "")

    lines = [
        f"[metroplex:{source}] {title}",
        "",
    ]

    if description:
        lines.append(f"Description: {description}")
    if problem and problem != description:
        lines.append(f"Problem: {problem}")

    rec_type = idea_data.get("_recommendation_type", "")
    if rec_type:
        lines.append(f"Type: {rec_type}")

    scope = idea_data.get("_scope", "")
    if scope:
        lines.append(f"Scope: {scope}")

    linear_id = idea_data.get("_linear_identifier", "")
    if linear_id:
        lines.append(f"Linear: {linear_id}")

    lines.append("")
    lines.append(f"Source: {source} | Priority score: {item.get('priority_score', 0):.1f}")

    return "\n".join(lines)
