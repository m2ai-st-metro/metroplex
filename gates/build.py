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
from gates.llm_expander import LLMSpecExpander
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

    def generate_spec(self, idea: dict, output_dir: Path) -> Path:
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

        # Try LLM expansion first
        if self.llm_expander is not None:
            try:
                rendered_spec = self.llm_expander.expand(idea)
                output_path.write_text(rendered_spec, encoding="utf-8")
                logger.info(
                    "LLM spec generated for idea %s: %s (%d chars)",
                    idea["id"], idea["title"], len(rendered_spec),
                )
                return output_path
            except Exception as e:
                logger.warning(
                    "LLM expansion failed for idea %s, falling back to Jinja2: %s",
                    idea["id"], e,
                )

        # Jinja2 fallback
        rendered_spec = self._render_jinja2(idea)
        output_path.write_text(rendered_spec, encoding="utf-8")
        logger.info(
            "Jinja2 spec generated for idea %s: %s (%d chars)",
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
    """Gate 2: Build Orchestration - Queue Runner Integration."""

    def __init__(self, config: Config, state_db: StateDB, spec_generator: SpecGenerator, audit_logger: AuditLogger, tyrest_gate=None, ideaforge_reader: Optional[IdeaForgeReader] = None):
        """
        Initialize Build Orchestrator.

        Args:
            config: Metroplex configuration
            state_db: State database for recording build jobs
            spec_generator: Spec generator instance
            audit_logger: Audit logger for tracking decisions
            tyrest_gate: Optional TyrestGate for pre-build spec review
            ideaforge_reader: Optional IdeaForgeReader for refreshing stale snapshot data
        """
        self.config = config
        self.state_db = state_db
        self.spec_generator = spec_generator
        self.audit_logger = audit_logger
        self.tyrest_gate = tyrest_gate
        self.ideaforge_reader = ideaforge_reader
        self.queue_runner_path = Path(config.yce_dir) / "queue_runner.py"
        self.yce_python = Path(config.yce_dir) / "venv" / "bin" / "python"

    def queue_build(self, idea: dict, spec_path: Path, dry_run: bool = False) -> BuildJob | None:
        """
        Queue a build job via queue_runner.py add.

        Args:
            idea: Idea dictionary with id and title
            spec_path: Path to generated spec file
            dry_run: If True, print command without executing

        Returns:
            BuildJob if executed, None if dry_run
        """
        source = idea.get("_source", "ideaforge")
        job_id = f"metroplex-{source}-{idea['id']}"
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

        if dry_run:
            print(f"[DRY RUN] Would execute: {' '.join(command)}")
            return None

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30
            )

            queued_at = datetime.now()

            if result.returncode == 0:
                job = BuildJob(
                    idea_id=idea["id"],
                    title=idea["title"],
                    spec_path=str(spec_path),
                    queue_job_id=job_id,
                    status="queued",
                    queued_at=queued_at
                )
                self.state_db.record_build_job(job)
                self.audit_logger.log_decision(
                    gate="build",
                    action="queue_build",
                    details={
                        "idea_id": idea["id"],
                        "job_id": job_id,
                        "spec_path": str(spec_path),
                        "status": "queued"
                    }
                )
                return job
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                job = BuildJob(
                    idea_id=idea["id"],
                    title=idea["title"],
                    spec_path=str(spec_path),
                    queue_job_id=job_id,
                    status="failed",
                    queued_at=queued_at
                )
                self.state_db.record_build_job(job)
                self.audit_logger.log_error(
                    gate="build",
                    error=f"Failed to queue build: {error_msg}",
                    details={
                        "idea_id": idea["id"],
                        "job_id": job_id,
                        "returncode": result.returncode,
                        "stderr": error_msg
                    }
                )
                return job

        except subprocess.TimeoutExpired:
            error_msg = "queue_build command timed out after 30 seconds"
            job = BuildJob(
                idea_id=idea["id"],
                title=idea["title"],
                spec_path=str(spec_path),
                queue_job_id=job_id,
                status="failed",
                queued_at=datetime.now()
            )
            self.state_db.record_build_job(job)
            self.audit_logger.log_error(gate="build", error=error_msg, details={"idea_id": idea["id"], "job_id": job_id})
            return job

        except Exception as e:
            error_msg = f"Unexpected error queuing build: {str(e)}"
            job = BuildJob(
                idea_id=idea["id"],
                title=idea["title"],
                spec_path=str(spec_path),
                queue_job_id=job_id,
                status="failed",
                queued_at=datetime.now()
            )
            self.state_db.record_build_job(job)
            self.audit_logger.log_error(gate="build", error=error_msg, details={"idea_id": idea["id"], "job_id": job_id})
            return job

    def is_runner_active(self) -> bool:
        """Check if a queue_runner process is still running from a previous dispatch."""
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

        Args:
            dry_run: If True, print command without executing

        Returns:
            True if started, False otherwise
        """
        concurrency = self.config.max_concurrent_builds
        command = [
            str(self.yce_python),
            str(self.queue_runner_path),
            "start",
            "--concurrency",
            str(concurrency),
        ]

        if dry_run:
            print(f"[DRY RUN] Would execute (background): {' '.join(command)}")
            return True

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
        Check queue status via queue_runner.py status --json.

        Returns:
            Parsed status dict, or empty dict on error
        """
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
            elif job_status == "completed":
                result["completed"].append(job_id)
                try:
                    if self.state_db.update_build_job_status(job_id, "completed"):
                        result["newly_synced"].append(job_id)
                        # Backfill project_dir from runner data
                        project_dir = job_data.get("project_dir")
                        if project_dir:
                            self.state_db.update_build_job_project_dir(job_id, project_dir)
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
                except Exception as e:
                    self.audit_logger.log_error(
                        gate="build",
                        error=f"Failed to sync failed status for {job_id}: {e}",
                        details={"job_id": job_id}
                    )

        result["running_count"] = len(result["running"])

        # Clean up PID file if runner is no longer active
        if result["running_count"] == 0 and not self.is_runner_active():
            RUNNER_PID_FILE.unlink(missing_ok=True)

        return result

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
                spec_path = self.spec_generator.generate_spec(idea, output_dir)
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
                    queue_job_id=f"metroplex-{idea.get('id', 0)}",
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
            item = state_db.get_next_pending(sources=buildable_sources)
            if item is None:
                break

            # Skip if a completed build already exists or retries are exhausted
            existing_job_id = f"metroplex-{item.source}-{item.source_id}"
            if state_db.has_completed_build(existing_job_id):
                state_db.update_item_status(item.id, "completed")
                logger.info("Skipping %s — completed build already exists", existing_job_id)
                continue
            if state_db.has_exhausted_retries(existing_job_id):
                state_db.update_item_status(item.id, "failed")
                logger.info("Skipping %s — retries exhausted", existing_job_id)
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

            source = idea.get("_source", "ideaforge")
            job_id = f"metroplex-{source}-{idea['id']}"
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
            else:
                # Local build: generate spec → Tyrest review → queue YCE
                try:
                    output_dir = Path(__file__).parent.parent / "data" / "specs"
                    spec_path = self.spec_generator.generate_spec(idea, output_dir)

                    # Tyrest pre-build review (Gate 2.5)
                    if self.tyrest_gate is not None:
                        spec_text = spec_path.read_text(encoding="utf-8")
                        tyrest_result = self.tyrest_gate.review_spec(spec_text, idea_title=idea["title"])

                        if tyrest_result.rejected:
                            logger.info(
                                "Tyrest REJECTED spec for %s: %s",
                                idea["title"], tyrest_result.reasoning,
                            )
                            job = BuildJob(
                                idea_id=idea["id"],
                                title=idea["title"],
                                spec_path=str(spec_path),
                                queue_job_id=job_id,
                                status="failed",
                                queued_at=queued_at,
                            )
                            self.state_db.record_build_job(job)
                            jobs.append(job)
                            self.audit_logger.log_decision(
                                gate="build",
                                action="tyrest_rejected",
                                details={
                                    "idea_id": idea["id"],
                                    "title": idea["title"],
                                    "reasoning": tyrest_result.reasoning,
                                    "overall": tyrest_result.overall,
                                    "risk_flags": tyrest_result.risk_flags,
                                },
                            )
                            if not dry_run and item.id:
                                state_db.update_item_status(item.id, "failed", "completed_at")
                            continue

                        self.audit_logger.log_decision(
                            gate="build",
                            action="tyrest_approved",
                            details={
                                "idea_id": idea["id"],
                                "verdict": tyrest_result.verdict,
                                "overall": tyrest_result.overall,
                            },
                        )

                    # Queue build via YCE queue_runner
                    job = self.queue_build(idea, spec_path, dry_run=dry_run)
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

            self.audit_logger.log_decision(
                gate="build",
                action="dispatch",
                details={
                    "idea_id": idea["id"],
                    "job_id": job_id,
                    "title": idea["title"],
                    "status": jobs[-1].status if jobs else "unknown",
                    "route": "oz-cloud" if oz_run_id else "yce-local",
                },
            )

            # Mark as dispatched in priority queue
            if not dry_run and item.id:
                last_status = jobs[-1].status if jobs else "failed"
                dispatch_status = "dispatched" if last_status == "queued" else "failed"
                timestamp_col = "dispatched_at" if dispatch_status == "dispatched" else "completed_at"
                state_db.update_item_status(item.id, dispatch_status, timestamp_col)

        if not jobs:
            print("No pending items in priority queue")

        # Start YCE queue runner if any jobs were queued
        if not dry_run and queued_jobs:
            self.start_queue_background(dry_run=dry_run)

        return jobs
