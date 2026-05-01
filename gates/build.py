"""
Build Gate - Gate 2
Generates specs from approved ideas via LLM, runs Tyrest pre-build
review, then dispatches to YCE Harness for autonomous building.
"""
import json
import logging
import os
import signal
import sys
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, Template, TemplateNotFound

from config import Config
from models import BuildJob, PriorityItem
from db import StateDB
from audit import AuditLogger
from cost_rates import estimate_cost
from gates.llm_expander import LLMSpecExpander, validate_spec, format_failure_feedback
from postmortem import capture_postmortem, get_failure_patterns
from oz_bridge import submit_to_oz
from readers.ideaforge_reader import IdeaForgeReader

logger = logging.getLogger(__name__)

RUNNER_PID_FILE = Path(__file__).parent.parent / "data" / "runner.pid"


class SpecGenerator:
    """Gate 2: Spec Generation - LLM expansion with Jinja2 fallback."""

    def __init__(self, config: Config, template_dir: Path, state_db: Optional[StateDB] = None):
        """
        Initialize Spec Generator.

        Args:
            config: Metroplex configuration
            template_dir: Path to spec_templates/ directory
            state_db: Optional StateDB for cost recording

        Raises:
            FileNotFoundError: If template_dir does not exist
        """
        self.config = config
        self.template_dir = template_dir
        self.state_db = state_db

        if not template_dir.exists():
            raise FileNotFoundError(f"Template directory not found at {template_dir}")

        # Set up Jinja2 environment (fallback)
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True
        )

        # Initialize LLM expander if configured
        self.llm_expander: Optional[LLMSpecExpander] = None
        if config.spec_use_llm:
            try:
                self.llm_expander = LLMSpecExpander(
                    model=config.spec_llm_model,
                    max_tokens=config.spec_llm_max_tokens,
                    state_db=state_db,
                )
                logger.info(
                    "LLM spec expansion enabled (model=%s)", config.spec_llm_model
                )
            except (ValueError, Exception) as e:
                logger.warning(
                    "LLM spec expansion unavailable, using Jinja2 fallback: %s", e
                )

    def generate_spec(
        self,
        idea: dict,
        output_dir: Path,
        queue_job_id: str | None = None,
    ) -> Path:
        """
        Generate app spec file from idea data.

        Uses LLM expansion when available for idea-specific specs.
        Falls back to Jinja2 template rendering on LLM failure or when disabled.

        Args:
            idea: Idea dictionary with required fields:
                - id (int): Idea ID
                - title (str): Idea title
                - description (str): Idea description
                - problem_statement (str): Problem statement
                - target_audience (str): Target audience
                - artifact_type (str): Artifact type (tool, agent, product)
            output_dir: Directory to write generated spec
            queue_job_id: Optional build job ID to attribute LLM cost ledger
                entries to (Phase G — per-build cost tracking).

        Returns:
            Path to generated spec file

        Raises:
            FileNotFoundError: If template file not found (Jinja2 fallback only)
            ValueError: If required fields missing from idea
        """
        # Validate required fields
        required_fields = [
            "id", "title", "description", "problem_statement",
            "target_audience", "artifact_type"
        ]
        missing_fields = [f for f in required_fields if f not in idea or idea[f] is None]
        if missing_fields:
            raise ValueError(f"Idea missing required fields: {missing_fields}")

        # Create output directory if it doesn't exist
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"app_spec_{idea['id']}.txt"

        # LLM expansion only -- no fallback to generic templates.
        # A bad spec wastes a build slot. Better to skip and retry next cycle.
        if self.llm_expander is None:
            raise RuntimeError(
                f"LLM expander not configured. Cannot generate spec for idea {idea['id']}."
            )

        # Fetch failure patterns from past builds to inject as constraints
        failure_patterns = []
        if self.state_db is not None:
            try:
                failure_patterns = get_failure_patterns(self.state_db, min_count=2)
            except Exception as e:
                logger.warning("Failed to fetch failure patterns for spec feedback: %s", e)

        # Retry loop: Nemotron-3 leaks CoT ~43% of the time and occasionally
        # parrots prompt instructions. These are stochastic -- a second call
        # usually produces a clean spec. Retry here instead of burning a
        # build-level retry on spec generation failures.
        max_spec_attempts = 3
        last_rejection = ""
        for spec_attempt in range(max_spec_attempts):
            try:
                rendered_spec = self.llm_expander.expand(
                    idea,
                    failure_patterns=failure_patterns,
                    queue_job_id=queue_job_id,
                )
            except Exception as e:
                raise RuntimeError(
                    f"LLM expansion failed for idea {idea['id']} ({idea['title']}): {e}"
                ) from e

            is_valid, rejection_reason = validate_spec(rendered_spec)
            if is_valid:
                break
            last_rejection = rejection_reason
            logger.warning(
                "Spec validation failed for idea %s (attempt %d/%d): %s",
                idea["id"], spec_attempt + 1, max_spec_attempts, rejection_reason,
            )
        else:
            raise ValueError(
                f"LLM spec rejected for idea {idea['id']} ({idea['title']}) "
                f"after {max_spec_attempts} attempts: {last_rejection}"
            )

        output_path.write_text(rendered_spec, encoding="utf-8")
        logger.info(
            "LLM spec generated for idea %s: %s (%d chars)",
            idea["id"], idea["title"], len(rendered_spec),
        )

        return output_path

    def _render_jinja2(self, idea: dict) -> str:
        """Render spec using Jinja2 template (fallback path).

        Selects template based on source:
        - Academy promotions use tier1_agent_template.md
        - All other sources use app_spec_template.md
        """
        # Select template based on source
        is_academy = idea.get("_source") == "academy"
        template_name = "tier1_agent_template.md" if is_academy else "app_spec_template.md"

        try:
            template = self.env.get_template(template_name)
        except TemplateNotFound:
            raise FileNotFoundError(
                f"Template not found: {template_name} in {self.template_dir}"
            )

        template_vars = {
            "title": idea["title"],
            "description": idea["description"],
            "problem_statement": idea["problem_statement"],
            "target_audience": idea["target_audience"],
            "artifact_type": idea["artifact_type"],
            "tech_stack": idea.get("tech_stack", None),
        }

        # Add Academy-specific template variables for agent builds
        if is_academy:
            template_vars.update({
                "persona_id": idea.get("_persona_id", "unknown"),
                "model": idea.get("_model", "sonnet"),
                "tool_groups": idea.get("_tool_groups", ["file_readonly"]),
                "prompt_file": idea.get("_prompt_file", "agent_prompt.md"),
                "promotion_reason": idea.get("_promotion_reason", "Graduation gates passed"),
            })

        return template.render(**template_vars)


class BuildOrchestrator:
    """Gate 2: Build Orchestration - Queue Runner Integration.

    Supports pluggable build adapters (Paperclip pattern). When an adapter is
    provided, queue/poll/start operations delegate to it. Otherwise, falls back
    to the inline subprocess implementation for backward compatibility.
    """

    def __init__(self, config: Config, state_db: StateDB, spec_generator: SpecGenerator, audit_logger: AuditLogger, ideaforge_reader: Optional[IdeaForgeReader] = None, adapter=None):
        """
        Initialize Build Orchestrator.

        Args:
            config: Metroplex configuration
            state_db: State database for recording build jobs
            spec_generator: Spec generator instance
            audit_logger: Audit logger for tracking decisions
            ideaforge_reader: Optional IdeaForgeReader for refreshing stale snapshot data
            adapter: Optional BuildAdapter for runtime-agnostic dispatch
        """
        self.config = config
        self.state_db = state_db
        self.spec_generator = spec_generator
        self.audit_logger = audit_logger
        self.ideaforge_reader = ideaforge_reader
        self.adapter = adapter
        self.queue_runner_path = Path(config.yce_dir) / "queue_runner.py"
        self.yce_python = Path(config.yce_dir) / "venv" / "bin" / "python"

    def queue_build(self, idea: dict, spec_path: Path, dry_run: bool = False, attempt: int = 0) -> BuildJob | None:
        """
        Queue a build job. Delegates to adapter if available, otherwise
        falls back to inline subprocess logic.

        Args:
            idea: Idea dictionary with id and title
            spec_path: Path to generated spec file
            dry_run: If True, print command without executing
            attempt: Retry attempt number (0 = first try, 1+ = retries)

        Returns:
            BuildJob if executed, None if dry_run
        """
        source = idea.get("_source", "ideaforge")
        base_job_id = f"metroplex-{source}-{idea['id']}"
        job_id = f"{base_job_id}-r{attempt}" if attempt > 0 else base_job_id

        if dry_run:
            print(f"[DRY RUN] Would queue build {job_id} from {spec_path}")
            return None

        queued_at = datetime.now()
        error_msg = None

        if (
            self.config.build_target == "self_healing"
            and self.adapter is not None
            and not self.adapter.is_active()
        ):
            logger.warning(
                "BuildGate: self-healing daemon heartbeat is stale — skipping "
                "dispatch of %s. Start the daemon with: "
                "`(cd /home/apexaipc/projects/metroplex && claude)` then "
                "`/self-healing-daemon start`.",
                job_id,
            )
            return None

        if self.adapter is not None:
            # Delegate to pluggable adapter
            result = self.adapter.queue(
                spec_path=spec_path,
                job_id=job_id,
                model=self.config.build_model,
                parallel=self.config.build_parallel,
                max_workers=self.config.build_max_workers,
            )
            status = result.status
            error_msg = result.error
        else:
            # Inline subprocess fallback (original implementation)
            command = [
                str(self.yce_python),
                str(self.queue_runner_path),
                "add",
                str(spec_path.resolve()),
                "--id",
                job_id,
                "--model",
                self.config.build_model,
            ]
            if self.config.build_parallel:
                command.append("--parallel")
                command.extend(["--max-workers", str(self.config.build_max_workers)])

            try:
                proc_result = subprocess.run(
                    command, capture_output=True, text=True, timeout=30
                )
                if proc_result.returncode == 0:
                    status = "queued"
                else:
                    status = "failed"
                    error_msg = proc_result.stderr.strip() or proc_result.stdout.strip() or "Unknown error"
            except subprocess.TimeoutExpired:
                status = "failed"
                error_msg = "queue_build command timed out after 30 seconds"
            except Exception as e:
                status = "failed"
                error_msg = f"Unexpected error queuing build: {e}"

        job = BuildJob(
            idea_id=idea["id"],
            title=idea["title"],
            spec_path=str(spec_path),
            queue_job_id=job_id,
            status=status,
            queued_at=queued_at,
        )
        self.state_db.record_build_job(job)

        if status == "queued":
            self.audit_logger.log_decision(
                gate="build", action="queue_build",
                details={"idea_id": idea["id"], "job_id": job_id, "spec_path": str(spec_path)},
            )
        else:
            self.audit_logger.log_error(
                gate="build", error=error_msg or f"Failed to queue build {job_id}",
                details={"idea_id": idea["id"], "job_id": job_id},
            )
        return job

    def is_runner_active(self) -> bool:
        """Check if a queue_runner process is still running from a previous dispatch."""
        if self.adapter is not None:
            return self.adapter.is_active()
        if not RUNNER_PID_FILE.exists():
            return False
        try:
            pid = int(RUNNER_PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Signal 0 = check if process exists
            return True
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale PID file -- clean up
            RUNNER_PID_FILE.unlink(missing_ok=True)
            return False

    def start_queue_background(self, dry_run: bool = False) -> bool:
        """
        Start queue_runner.py as a background process (non-blocking).
        Stores PID in data/runner.pid for later monitoring.
        Delegates to adapter if available.

        Args:
            dry_run: If True, print command without executing

        Returns:
            True if started, False otherwise
        """
        concurrency = self.config.max_concurrent_builds

        if dry_run:
            print(f"[DRY RUN] Would start queue runner (concurrency={concurrency})")
            return True

        if self.adapter is not None:
            return self.adapter.start(concurrency)

        command = [
            str(self.yce_python),
            str(self.queue_runner_path),
            "start",
            "--concurrency",
            str(concurrency),
        ]

        if self.is_runner_active():
            print("Queue runner already active, skipping start")
            return True

        try:
            log_path = Path(__file__).parent.parent / "data" / "runner.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "a")

            proc = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(Path(self.config.yce_dir)),
                start_new_session=True,  # Detach from parent process group
            )

            # Write PID for later monitoring
            RUNNER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            RUNNER_PID_FILE.write_text(str(proc.pid))

            self.audit_logger.log_decision(
                gate="build",
                action="start_queue_background",
                details={"pid": proc.pid, "log": str(log_path)}
            )
            print(f"Queue runner started (PID {proc.pid})")
            return True

        except Exception as e:
            self.audit_logger.log_error(
                gate="build",
                error=f"Failed to start queue runner: {str(e)}",
                details={}
            )
            return False

    def start_queue(self, dry_run: bool = False) -> bool:
        """
        Start the queue runner (blocking, kept for backward compatibility / CLI use).

        Args:
            dry_run: If True, print command without executing

        Returns:
            True if successful, False otherwise
        """
        command = [
            str(self.yce_python),
            str(self.queue_runner_path),
            "start"
        ]

        if dry_run:
            print(f"[DRY RUN] Would execute: {' '.join(command)}")
            return True

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=None  # Long-running process
            )

            if result.returncode == 0:
                self.audit_logger.log_decision(
                    gate="build",
                    action="start_queue",
                    details={"status": "success"}
                )
                return True
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                self.audit_logger.log_error(
                    gate="build",
                    error=f"Failed to start queue: {error_msg}",
                    details={"returncode": result.returncode}
                )
                return False

        except Exception as e:
            self.audit_logger.log_error(
                gate="build",
                error=f"Failed to start queue: {str(e)}",
                details={}
            )
            return False

    def check_status(self) -> dict:
        """
        Check queue status. Delegates to adapter if available.

        Returns:
            Parsed status dict, or empty dict on error
        """
        if self.adapter is not None:
            return self.adapter.poll()

        command = [
            str(self.yce_python),
            str(self.queue_runner_path),
            "status",
            "--json"
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                self.audit_logger.log_error(
                    gate="build",
                    error="Failed to check queue status",
                    details={"returncode": result.returncode}
                )
                return {}

        except Exception as e:
            self.audit_logger.log_error(
                gate="build",
                error=f"Failed to check queue status: {str(e)}",
                details={}
            )
            return {}

    def _record_build_session(self, job_id: str, project_dir: str) -> None:
        """Record a session snapshot after a build reaches terminal state.

        Reads state.json and the last judge-brief from the workspace to build
        a summary that future retries can use as context.
        """
        state_dir = Path(project_dir) / ".self-healing-pipeline"
        state_file = state_dir / "state.json"
        if not state_file.exists():
            return

        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Session record: unreadable state.json for %s: %s", job_id, e)
            return

        # Parse base_job_id and attempt from job_id
        # Format: "metroplex-ideaforge-210-r2" -> base="metroplex-ideaforge-210", attempt=2
        # Format: "metroplex-ideaforge-210" -> base="metroplex-ideaforge-210", attempt=0
        if "-r" in job_id and job_id.rsplit("-r", 1)[-1].isdigit():
            base_job_id = job_id.rsplit("-r", 1)[0]
            attempt = int(job_id.rsplit("-r", 1)[-1])
        else:
            base_job_id = job_id
            attempt = 0

        # Build summary from state.json history
        history = state.get("history", [])
        summary_parts = []
        summary_parts.append(f"Build {job_id}: status={state.get('status', 'unknown')}, "
                             f"attempts={state.get('attempt', 0)}/{state.get('max_attempts', 3)}")

        for entry in history:
            att = entry.get("attempt", "?")
            verdict = entry.get("judge_verdict", "unknown")
            failure = entry.get("failure_summary")
            line = f"  Attempt {att}: builder={entry.get('builder_result', '?')}, judge={verdict}"
            if failure:
                line += f", failure: {failure}"
            summary_parts.append(line)

        # Read the last judge-brief for richer failure context
        last_attempt = state.get("attempt", 1)
        judge_brief_path = state_dir / f"judge-brief-{last_attempt}.md"
        if judge_brief_path.exists():
            try:
                brief_text = judge_brief_path.read_text(encoding="utf-8")
                # Truncate to keep session records manageable (max 2000 chars)
                if len(brief_text) > 2000:
                    brief_text = brief_text[:2000] + "\n... (truncated)"
                summary_parts.append(f"\nJudge brief (attempt {last_attempt}):\n{brief_text}")
            except OSError:
                pass

        # Include Ravage review verdict if present
        review_verdict = state.get("review_verdict")
        if review_verdict:
            summary_parts.append(
                f"\nRavage review: verdict={review_verdict}, "
                f"critical_count={state.get('review_critical_count', 0)}"
            )
            review_report = state_dir / "review-report.md"
            if review_report.exists():
                try:
                    report_text = review_report.read_text(encoding="utf-8")
                    if len(report_text) > 2000:
                        report_text = report_text[:2000] + "\n... (truncated)"
                    summary_parts.append(f"\nReview report:\n{report_text}")
                except OSError:
                    pass

        summary = "\n".join(summary_parts)

        try:
            self.state_db.record_session(
                base_job_id=base_job_id,
                attempt=attempt,
                session_summary=summary,
            )
            logger.info("Recorded build session for %s (attempt %d)", base_job_id, attempt)
        except Exception as e:
            logger.warning("Failed to record build session for %s: %s", job_id, e)

    def _inject_session_context(
        self, base_job_id: str, attempt: int, spec_path: Path
    ) -> None:
        """Append prior-attempt context to the spec file before retry dispatch.

        Checks budget guards (token count and age) from config before injecting.
        If no prior session exists or budget is exceeded, does nothing.
        """
        session = self.state_db.get_latest_session(base_job_id)
        if not session:
            logger.info("No prior session for %s, dispatching retry without context", base_job_id)
            return

        # Budget guard: token limit
        if session.get("input_tokens_total", 0) > self.config.max_session_input_tokens:
            logger.info(
                "Session context for %s exceeds token budget (%d > %d), skipping injection",
                base_job_id,
                session["input_tokens_total"],
                self.config.max_session_input_tokens,
            )
            return

        # Budget guard: age limit
        created_at = session.get("created_at", "")
        if created_at:
            try:
                session_time = datetime.fromisoformat(created_at)
                age_hours = (datetime.now() - session_time).total_seconds() / 3600
                if age_hours > self.config.max_session_age_hours:
                    logger.info(
                        "Session context for %s is too old (%.1fh > %dh), skipping injection",
                        base_job_id, age_hours, self.config.max_session_age_hours,
                    )
                    return
            except (ValueError, TypeError):
                pass  # If we can't parse the date, proceed with injection

        summary = session.get("session_summary", "")
        if not summary:
            return

        # Append prior-attempt context to spec
        context_section = (
            f"\n\n## Prior Build Attempts (attempt {attempt})\n\n"
            f"**IMPORTANT**: This is retry attempt #{attempt}. The previous attempt(s) "
            f"failed. Use the context below to avoid repeating the same mistakes. "
            f"Focus on what went wrong and take a different approach where needed.\n\n"
            f"{summary}\n"
        )

        try:
            existing = spec_path.read_text(encoding="utf-8")
            spec_path.write_text(existing + context_section, encoding="utf-8")
            logger.info(
                "Injected %d chars of session context into spec for %s (attempt %d)",
                len(context_section), base_job_id, attempt,
            )
        except OSError as e:
            logger.warning("Failed to inject session context for %s: %s", base_job_id, e)

    def poll_and_sync_status(self) -> dict:
        """
        Poll queue_runner status and sync completed/failed jobs back to metroplex DB.

        Writes terminal statuses (completed/failed) back to both build_jobs
        and priority_queue tables so dispatched items don't stay stuck forever.

        Returns:
            dict with keys:
                running (list[str]): IDs of currently running jobs
                running_count (int): number of running jobs
                completed (list[str]): IDs of completed jobs
                failed (list[str]): IDs of failed jobs
                newly_synced (list[str]): IDs synced to DB this poll
        """
        result: dict = {
            "running": [],
            "running_count": 0,
            "completed": [],
            "failed": [],
            "newly_synced": [],
        }

        status = self.check_status()
        if not status or "jobs" not in status:
            # Runner not reachable or empty queue
            if not self.is_runner_active():
                RUNNER_PID_FILE.unlink(missing_ok=True)
            return result

        for job_data in status["jobs"]:
            job_status = job_data.get("status", "")
            job_id = job_data.get("id", "")

            if job_status == "running":
                result["running"].append(job_id)
                # Fix A: transition queued → started so the stale-queued recovery
                # (30-min threshold) doesn't destroy rows for legitimately long
                # Opus builds (40-70 min). The status check constraint allows
                # 'started' — see db.py CHECK IN ('queued','started','completed','failed').
                try:
                    if self.state_db.update_build_job_status(job_id, "started"):
                        result["newly_synced"].append(job_id)
                except Exception as e:
                    self.audit_logger.log_error(
                        gate="build",
                        error=f"Failed to sync running status for {job_id}: {e}",
                        details={"job_id": job_id}
                    )
            elif job_status == "completed":
                result["completed"].append(job_id)
                try:
                    if self.state_db.update_build_job_status(job_id, "completed"):
                        result["newly_synced"].append(job_id)
                        # Backfill project_dir from runner data
                        project_dir = job_data.get("project_dir")
                        if project_dir:
                            self.state_db.update_build_job_project_dir(job_id, project_dir)
                        # Extract per-build log for postmortem analysis
                        self._extract_build_log(job_id)
                        # Record build cost in ledger
                        self._record_build_cost(job_id, job_data)
                        # Aggregate per-build cost onto build_jobs (Phase G)
                        self._aggregate_build_actual_cost(job_id)
                        # Record session snapshot for retry context (Phase 15g)
                        if project_dir:
                            self._record_build_session(job_id, project_dir)
                except Exception as e:
                    self.audit_logger.log_error(
                        gate="build",
                        error=f"Failed to sync completed status for {job_id}: {e}",
                        details={"job_id": job_id}
                    )
            elif job_status in ("failed", "interrupted"):
                result["failed"].append(job_id)
                try:
                    if self.state_db.update_build_job_status(job_id, "failed"):
                        result["newly_synced"].append(job_id)
                        # Backfill project_dir even for failed/interrupted builds
                        project_dir = job_data.get("project_dir")
                        if project_dir:
                            self.state_db.update_build_job_project_dir(job_id, project_dir)
                        # Extract per-build log for postmortem analysis
                        self._extract_build_log(job_id)
                        # Record build cost even for failed builds (tokens were still consumed)
                        self._record_build_cost(job_id, job_data)
                        # Aggregate per-build cost onto build_jobs (Phase G)
                        self._aggregate_build_actual_cost(job_id)
                        # Record session snapshot for retry context (Phase 15g)
                        if project_dir:
                            self._record_build_session(job_id, project_dir)
                except Exception as e:
                    self.audit_logger.log_error(
                        gate="build",
                        error=f"Failed to sync failed status for {job_id}: {e}",
                        details={"job_id": job_id}
                    )

        result["running_count"] = len(result["running"])

        # Filesystem fallback: detect orphaned builds where the queue_runner
        # shows "pending" but a completed generation directory already exists.
        # This covers the case where the runner process was killed before it
        # could persist completion status back to queue.json.
        yce_generations = Path(self.config.yce_dir) / "generations"
        for job_data in status["jobs"]:
            job_status = job_data.get("status", "")
            job_id = job_data.get("id", "")
            if job_status == "pending" and job_id and not job_data.get("project_dir"):
                candidate_dir = yce_generations / job_id
                if candidate_dir.is_dir() and self._has_source_code(candidate_dir):
                    try:
                        changed = self.state_db.update_build_job_status(job_id, "completed")
                        if changed:
                            logger.info(
                                "Filesystem fallback: detected orphaned completed build for %s at %s",
                                job_id, candidate_dir,
                            )
                            result["newly_synced"].append(job_id)
                            self.state_db.update_build_job_project_dir(job_id, str(candidate_dir))
                            result["completed"].append(job_id)
                            self.audit_logger.log_decision(
                                gate="build",
                                action="filesystem_fallback_sync",
                                details={
                                    "job_id": job_id,
                                    "project_dir": str(candidate_dir),
                                },
                            )
                    except Exception as e:
                        self.audit_logger.log_error(
                            gate="build",
                            error=f"Failed filesystem fallback sync for {job_id}: {e}",
                            details={"job_id": job_id},
                        )

        # Fix C: Before checking for stale queued builds, read the YCE
        # queue.json directly to find jobs the runner is ACTIVELY working on.
        # Without this, a build that just transitioned to running in YCE but
        # hasn't been picked up by this poll's `check_status()` snapshot could
        # still be classified stale and marked abandoned.  Excluding these
        # IDs from the stale check prevents that race.
        excluded_running_jobs: set[str] = set()
        try:
            yce_queue_path = Path(self.config.yce_dir) / "data" / "queue.json"
            if yce_queue_path.exists():
                with open(yce_queue_path, "r", encoding="utf-8") as f:
                    yce_data = json.load(f)
                # YCE's queue.json is {"version": N, "jobs": [...]}; older
                # fixtures and tests used a bare list. Handle both shapes.
                yce_jobs = (
                    yce_data.get("jobs", [])
                    if isinstance(yce_data, dict)
                    else yce_data
                )
                for yj in yce_jobs:
                    if yj.get("status") in ("running", "pending"):
                        yjid = yj.get("job_id") or yj.get("id")
                        if yjid:
                            excluded_running_jobs.add(yjid)
        except Exception as e:
            self.audit_logger.log_error(
                gate="build",
                error=f"Failed to read YCE queue.json for stale exclusion: {e}",
                details={},
            )

        # Stale queued build recovery: detect builds stuck in 'queued' status
        # where priority_queue says 'dispatched' but the YCE runner never picked
        # them up.  Reset to 'pending' so the next cycle re-dispatches.
        try:
            stale_builds = self.state_db.get_stale_queued_builds(
                exclude_job_ids=excluded_running_jobs
            )
            for sb in stale_builds:
                job_id = sb["queue_job_id"]
                logger.warning(
                    "Stale queued build detected: %s (queued at %s) — resetting to pending",
                    job_id, sb["queued_at"],
                )
                self.state_db.reset_stale_queued_build(job_id, sb["priority_queue_id"])
                self.audit_logger.log_decision(
                    gate="build",
                    action="stale_queued_reset",
                    details={
                        "queue_job_id": job_id,
                        "idea_id": sb["idea_id"],
                        "title": sb["title"],
                        "queued_at": sb["queued_at"],
                        "threshold_minutes": self.state_db.STALE_QUEUED_THRESHOLD_MINUTES,
                    },
                )
                result.setdefault("stale_reset", []).append(job_id)
        except Exception as e:
            self.audit_logger.log_error(
                gate="build",
                error=f"Stale queued build check failed: {e}",
                details={},
            )

        # Clean up PID file if runner is no longer active
        if result["running_count"] == 0 and not self.is_runner_active():
            RUNNER_PID_FILE.unlink(missing_ok=True)

        return result

    def _extract_build_log(self, job_id: str) -> None:
        """Extract per-build log from runner.log into data/build_logs/{job_id}.log.

        Scans runner.log for lines prefixed with [{job_id}] and writes them
        to a dedicated log file so postmortem analysis can read build output.
        Best-effort: silently returns on any error.
        """
        runner_log = Path(__file__).parent.parent / "data" / "runner.log"
        if not runner_log.exists():
            return

        build_log_dir = Path(__file__).parent.parent / "data" / "build_logs"
        build_log_dir.mkdir(parents=True, exist_ok=True)
        build_log_path = build_log_dir / f"{job_id}.log"

        # Skip if already extracted
        if build_log_path.exists() and build_log_path.stat().st_size > 0:
            return

        try:
            prefix = f"[{job_id}]"
            matched_lines = []
            with open(runner_log, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.lstrip().startswith(prefix):
                        matched_lines.append(line)

            if matched_lines:
                build_log_path.write_text("".join(matched_lines), encoding="utf-8")
                logger.info(
                    "Extracted %d lines of build log for %s -> %s",
                    len(matched_lines), job_id, build_log_path,
                )
        except Exception as e:
            logger.warning("Failed to extract build log for %s: %s", job_id, e)

    def _record_build_cost(self, job_id: str, job_data: dict) -> None:
        """Record build cost in the cost ledger.

        Uses actual token counts from the queue runner if available,
        otherwise falls back to the configured per-build cost estimate.
        Best-effort: silently returns on any error.
        """
        try:
            input_tokens = job_data.get("input_tokens")
            output_tokens = job_data.get("output_tokens")
            model_used = job_data.get("model_used") or job_data.get("model", "unknown")
            duration = job_data.get("duration_seconds")

            if input_tokens is not None and output_tokens is not None:
                # Real token counts available — use actual cost calculation
                cost = estimate_cost(model_used, input_tokens, output_tokens)
                details = json.dumps({
                    "source_type": "actual_tokens",
                    "duration_seconds": duration,
                })
            else:
                # No token counts (Max subscription builds) — use per-build estimate
                input_tokens = 0
                output_tokens = 0
                cost = self.config.build_cost_estimate
                details = json.dumps({
                    "source_type": "estimate",
                    "duration_seconds": duration,
                    "note": "Max subscription build, no token counts available",
                })

            self.state_db.record_cost(
                source="yce_build",
                model=model_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=cost,
                queue_job_id=job_id,
                details=details,
            )
            logger.info(
                "Recorded build cost for %s: $%.2f (model=%s, tokens=%d/%d)",
                job_id, cost, model_used, input_tokens, output_tokens,
            )
        except Exception as e:
            logger.warning("Failed to record build cost for %s: %s", job_id, e)

    def _aggregate_build_actual_cost(self, job_id: str) -> None:
        """Sum cost_ledger entries for this build into build_jobs.actual_cost_usd.

        Best-effort: a failure here must never block the status transition.
        """
        try:
            total = self.state_db.update_build_actual_cost(job_id)
            logger.info("Aggregated actual_cost_usd for %s: $%.4f", job_id, total)
        except Exception as e:
            logger.warning("Failed to aggregate actual_cost_usd for %s: %s", job_id, e)

    @staticmethod
    def _has_source_code(project_dir: Path) -> bool:
        """Check if a generation directory contains actual source code (not just scaffolding).

        Requires at least one .py or .js/.ts file and a README to consider
        the build complete.
        """
        has_readme = (project_dir / "README.md").exists()
        has_code = False
        for ext in ("*.py", "*.js", "*.ts"):
            if list(project_dir.glob(f"**/{ext}"))[:1]:
                has_code = True
                break
        return has_readme and has_code

    def run(self, approved_ideas: list[dict], dry_run: bool = False) -> list[BuildJob]:
        """
        Run build orchestration for approved ideas.
        Generates specs, queues builds, and starts runner as background process.

        Args:
            approved_ideas: List of approved idea dictionaries
            dry_run: If True, only print commands without executing

        Returns:
            List of BuildJob results
        """
        jobs = []

        if self.spec_generator is None:
            print("Warning: Spec generator not initialized (template directory not found)")
            return []

        for idea in approved_ideas:
            try:
                output_dir = Path(__file__).parent.parent / "data" / "specs"
                source = idea.get("_source", "ideaforge")
                pre_dispatch_job_id = f"metroplex-{source}-{idea.get('id', 0)}"
                spec_path = self.spec_generator.generate_spec(
                    idea, output_dir, queue_job_id=pre_dispatch_job_id,
                )
                job = self.queue_build(idea, spec_path, dry_run=dry_run)
                if job:
                    jobs.append(job)

            except Exception as e:
                error_msg = f"Failed to process idea {idea.get('id')}: {str(e)}"
                self.audit_logger.log_error(
                    gate="build",
                    error=error_msg,
                    details={"idea_id": idea.get("id")}
                )
                job = BuildJob(
                    idea_id=idea.get("id", 0),
                    title=idea.get("title", "Unknown"),
                    spec_path="",
                    queue_job_id=f"metroplex-ideaforge-{idea.get('id', 0)}",
                    status="failed",
                    queued_at=datetime.now()
                )
                self.state_db.record_build_job(job)
                jobs.append(job)

        # Start queue as background process (non-blocking)
        if not dry_run and jobs:
            queued_jobs = [j for j in jobs if j.status == "queued"]
            if queued_jobs:
                self.start_queue_background(dry_run=dry_run)

        return jobs

    def run_from_queue(self, state_db: StateDB, dry_run: bool = False) -> list[BuildJob]:
        """
        Pull items from the priority queue, generate specs, and dispatch to YCE.

        Flow: pull pending idea → generate spec (LLMSpecExpander) → Tyrest
        pre-build review → queue YCE build → start runner.

        Args:
            state_db: StateDB instance (for priority queue access)
            dry_run: If True, only print commands without executing

        Returns:
            List of BuildJob results
        """
        buildable_sources = ("ideaforge", "linear", "academy")
        dispatch_limit = self.config.max_approve_per_cycle
        jobs = []
        queued_jobs = []

        for _ in range(dispatch_limit):
            # Tracks the job appended by *this* iteration. Used by the bottom-of-loop
            # audit log and item-status update. Never read jobs[-1] here — early-exit
            # continues skip the audit, and a future code path that reaches the audit
            # without appending would silently reference the PREVIOUS iteration's job.
            current_job: BuildJob | None = None

            if dry_run:
                item = state_db.get_next_pending(sources=buildable_sources)
            else:
                item = state_db.claim_next_pending(
                    claimer_id=f"build-{os.getpid()}", sources=buildable_sources
                )
            if item is None:
                break

            # Skip if a completed build already exists or retries are exhausted
            base_job_id = f"metroplex-{item.source}-{item.source_id}"
            if state_db.has_completed_build(base_job_id):
                state_db.update_item_status(item.id, "completed")
                logger.info("Skipping %s — completed build already exists", base_job_id)
                continue
            if state_db.has_exhausted_retries(base_job_id):
                state_db.update_item_status(item.id, "failed")
                logger.info("Skipping %s — retries exhausted", base_job_id)
                continue

            # Parse idea data
            try:
                idea = json.loads(item.idea_data)
            except (json.JSONDecodeError, TypeError):
                idea = {
                    "id": item.source_id,
                    "title": item.title,
                    "description": item.description,
                    "problem_statement": item.description,
                    "target_audience": "General",
                    "artifact_type": "tool",
                }
            idea["_source"] = item.source

            # Refresh fields that may be stale in the priority queue snapshot.
            # The triage gate snapshots idea_data at enqueue time, but fields
            # like artifact_type may be populated by a later classification step.
            if idea.get("artifact_type") is None and self.ideaforge_reader and item.source == "ideaforge":
                try:
                    fresh = self.ideaforge_reader.get_idea_by_id(int(item.source_id))
                    if fresh:
                        for field in ("artifact_type", "problem_statement", "target_audience"):
                            if fresh.get(field) and not idea.get(field):
                                idea[field] = fresh[field]
                        logger.info(
                            "Refreshed stale snapshot for idea %s: artifact_type=%s",
                            item.source_id, idea.get("artifact_type"),
                        )
                except Exception as e:
                    logger.warning("Failed to refresh idea %s from IdeaForge: %s", item.source_id, e)

            # Idea quality gate: reject ideas with insufficient data
            idea_description = idea.get("description") or ""
            idea_feasibility = idea.get("feasibility_score")
            if idea_feasibility is not None and float(idea_feasibility) < 5.0:
                logger.info(
                    "Idea quality gate REJECT for %s: feasibility_score=%.1f < 5.0",
                    idea.get("title", "?"), float(idea_feasibility),
                )
                self.audit_logger.log_decision(
                    gate="build",
                    action="idea_quality_rejected",
                    details={
                        "idea_id": idea.get("id"),
                        "title": idea.get("title"),
                        "reason": f"feasibility_score {idea_feasibility} < 5.0",
                    },
                )
                if not dry_run and item.id:
                    state_db.update_item_status(item.id, "failed", "completed_at")
                continue
            if len(idea_description.strip()) < 20:
                logger.info(
                    "Idea quality gate REJECT for %s: description too short (%d chars)",
                    idea.get("title", "?"), len(idea_description.strip()),
                )
                self.audit_logger.log_decision(
                    gate="build",
                    action="idea_quality_rejected",
                    details={
                        "idea_id": idea.get("id"),
                        "title": idea.get("title"),
                        "reason": f"description too short ({len(idea_description.strip())} chars)",
                    },
                )
                if not dry_run and item.id:
                    state_db.update_item_status(item.id, "failed", "completed_at")
                continue

            source = idea.get("_source", "ideaforge")
            base_job_id = f"metroplex-{source}-{idea['id']}"
            attempt = state_db.count_failed_builds(base_job_id)

            # Backoff guard: skip if this is a retry and the backoff timer
            # hasn't expired yet. The priority_queue was reset to 'pending'
            # by _reset_priority_queue_for_retry, but the actual backoff
            # lives on build_jobs.next_retry_at. Without this check, every
            # cycle (~60s) re-dispatches the build, bypassing the 5/20/60
            # minute backoff schedule.
            if attempt > 0 and state_db.is_backoff_active(base_job_id):
                logger.info(
                    "Skipping %s — backoff timer still active (attempt %d)",
                    base_job_id, attempt,
                )
                continue

            job_id = f"{base_job_id}-r{attempt}" if attempt > 0 else base_job_id
            queued_at = datetime.now()

            # Pre-build feasibility check (L5 B2)
            try:
                from feasibility_scorer import (
                    score_feasibility,
                    record_prediction,
                    get_reject_threshold,
                )
                feasibility = score_feasibility(idea, state_db)
                feas_score = feasibility["score"]
                reject_thresh = get_reject_threshold(state_db)

                logger.info(
                    "Feasibility score for %s: %.1f (threshold=%d, learned=%s)",
                    idea.get("title", "?"), feas_score, reject_thresh, feasibility["learned_active"],
                )

                # Store feasibility score on the build job (set after job creation below)
                idea["_feasibility_score"] = feas_score

                if feas_score < reject_thresh:
                    logger.info(
                        "Feasibility REJECT for %s: score %.1f < threshold %d",
                        idea.get("title", "?"), feas_score, reject_thresh,
                    )
                    self.audit_logger.log_decision(
                        gate="build",
                        action="feasibility_rejected",
                        details={
                            "idea_id": idea["id"],
                            "title": idea.get("title"),
                            "feasibility_score": feas_score,
                            "threshold": reject_thresh,
                            "breakdown": feasibility["breakdown"],
                        },
                    )
                    record_prediction(
                        state_db, job_id, feas_score,
                        feasibility["predicted_outcome"],
                        feasibility["feature_weights"],
                    )
                    if not dry_run and item.id:
                        state_db.update_item_status(item.id, "failed", "completed_at")
                    continue
                elif feas_score < 40:
                    logger.warning(
                        "Feasibility WARNING for %s: score %.1f is low (25-40 range)",
                        idea.get("title", "?"), feas_score,
                    )

                # Record prediction for later accuracy tracking
                record_prediction(
                    state_db, job_id, feas_score,
                    feasibility["predicted_outcome"],
                    feasibility["feature_weights"],
                )
            except Exception as e:
                logger.warning("Feasibility scoring failed for %s, proceeding: %s", idea.get("title", "?"), e)

            # Route to Oz cloud if configured and local slot busy
            build_target = self.config.build_target
            oz_run_id = None

            if build_target == "cloud" or (
                build_target == "auto" and self.config.oz_environment_id
                and self.is_runner_active()
            ):
                if self.config.oz_environment_id:
                    oz_run_id = submit_to_oz(
                        idea,
                        environment_id=self.config.oz_environment_id,
                        model_id=self.config.oz_build_model,
                        dry_run=dry_run,
                    )

            if oz_run_id:
                # Cloud build — no local spec needed
                job_id = f"oz-{oz_run_id}"
                job = BuildJob(
                    idea_id=idea["id"],
                    title=idea["title"],
                    spec_path="",
                    queue_job_id=job_id,
                    status="queued",
                    queued_at=queued_at,
                )
                self.state_db.record_build_job(job)
                jobs.append(job)
                current_job = job
            else:
                # Local build: generate spec → queue YCE
                try:
                    output_dir = Path(__file__).parent.parent / "data" / "specs"
                    spec_path = self.spec_generator.generate_spec(
                        idea, output_dir, queue_job_id=job_id,
                    )

                    # Inject prior-attempt context for retries (Phase 15g)
                    if attempt > 0 and not dry_run:
                        self._inject_session_context(base_job_id, attempt, spec_path)

                    # Queue build via YCE queue_runner
                    job = self.queue_build(idea, spec_path, dry_run=dry_run, attempt=attempt)
                    if job is None:
                        # Skip dispatch (e.g., self-healing daemon down). Release the
                        # priority-queue claim so the next cycle re-attempts without
                        # burning the retry budget.
                        if not dry_run and item.id:
                            state_db.release_claim(item.id)
                        continue
                    if job:
                        # Store feasibility score on the build job (L5 B2)
                        feas = idea.get("_feasibility_score")
                        if feas is not None and not dry_run:
                            try:
                                state_db.connect()
                                state_db.conn.execute(
                                    "UPDATE build_jobs SET feasibility_score = ? WHERE queue_job_id = ?",
                                    (feas, job.queue_job_id),
                                )
                                state_db.conn.commit()
                            except Exception as e:
                                logger.warning("Failed to store feasibility score: %s", e)
                        jobs.append(job)
                        current_job = job
                        if job.status == "queued":
                            queued_jobs.append(job)

                except Exception as e:
                    error_msg = f"Failed to process idea {idea.get('id')}: {str(e)}"
                    logger.error(error_msg)
                    self.audit_logger.log_error(gate="build", error=error_msg)
                    job = BuildJob(
                        idea_id=idea.get("id", 0),
                        title=idea.get("title", "Unknown"),
                        spec_path="",
                        queue_job_id=job_id,
                        status="failed",
                        queued_at=queued_at,
                    )
                    self.state_db.record_build_job(job)
                    jobs.append(job)
                    current_job = job
                    # Capture postmortem for pre-build failures
                    if not dry_run:
                        capture_postmortem(
                            state_db=self.state_db,
                            queue_job_id=job_id,
                            idea_id=idea.get("id", 0),
                            title=idea.get("title", "Unknown"),
                            log_path=None,
                            spec_path=None,
                            error_message=str(e),
                        )

            self.audit_logger.log_decision(
                gate="build",
                action="dispatch",
                details={
                    "idea_id": idea["id"],
                    "job_id": job_id,
                    "title": idea["title"],
                    "status": current_job.status if current_job is not None else "unknown",
                    "route": "oz-cloud" if oz_run_id else "yce-local",
                },
            )

            # Item was already atomically claimed as 'dispatched' by claim_next_pending().
            # Only update if the build dispatch actually failed.
            if not dry_run and item.id:
                last_status = current_job.status if current_job is not None else "failed"
                if last_status != "queued":
                    state_db.update_item_status(item.id, "failed", "completed_at")

        if not jobs:
            print("No pending items in priority queue")

        # Start YCE queue runner if any jobs were queued
        if not dry_run and queued_jobs:
            self.start_queue_background(dry_run=dry_run)

        return jobs
