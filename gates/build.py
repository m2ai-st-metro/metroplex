"""
Build Gate - Gate 2
Generates yce-harness app spec files from approved ideas using Jinja2 templates.
Orchestrates build queue via queue_runner.py subprocess calls.
Supports background dispatch (Popen) and status polling.
"""
import json
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

RUNNER_PID_FILE = Path("data/runner.pid")


class SpecGenerator:
    """Gate 2: Spec Generation - Jinja2 Template Rendering."""

    def __init__(self, config: Config, template_dir: Path):
        """
        Initialize Spec Generator.

        Args:
            config: Metroplex configuration
            template_dir: Path to spec_templates/ directory

        Raises:
            FileNotFoundError: If template_dir does not exist
        """
        self.config = config
        self.template_dir = template_dir

        if not template_dir.exists():
            raise FileNotFoundError(f"Template directory not found at {template_dir}")

        # Set up Jinja2 environment
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True
        )

    def generate_spec(self, idea: dict, output_dir: Path) -> Path:
        """
        Generate app spec file from idea data.

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
            FileNotFoundError: If template file not found
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

        # Load template
        try:
            template = self.env.get_template("app_spec_template.md")
        except TemplateNotFound:
            raise FileNotFoundError(
                f"Template not found: app_spec_template.md in {self.template_dir}"
            )

        # Prepare template variables
        template_vars = {
            "title": idea["title"],
            "description": idea["description"],
            "problem_statement": idea["problem_statement"],
            "target_audience": idea["target_audience"],
            "artifact_type": idea["artifact_type"],
            "tech_stack": idea.get("tech_stack", None)  # Optional field
        }

        # Render template
        rendered_spec = template.render(**template_vars)

        # Write to output file
        output_path = output_dir / f"app_spec_{idea['id']}.txt"
        output_path.write_text(rendered_spec, encoding="utf-8")

        return output_path


class BuildOrchestrator:
    """Gate 2: Build Orchestration - Queue Runner Integration."""

    def __init__(self, config: Config, state_db: StateDB, spec_generator: SpecGenerator, audit_logger: AuditLogger):
        """
        Initialize Build Orchestrator.

        Args:
            config: Metroplex configuration
            state_db: State database for recording build jobs
            spec_generator: Spec generator instance
            audit_logger: Audit logger for tracking decisions
        """
        self.config = config
        self.state_db = state_db
        self.spec_generator = spec_generator
        self.audit_logger = audit_logger
        self.queue_runner_path = Path(config.yce_dir) / "queue_runner.py"

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
        job_id = f"metroplex-{idea['id']}"
        command = [
            sys.executable,
            str(self.queue_runner_path),
            "add",
            str(spec_path),
            "--id",
            job_id,
            "--model",
            self.config.build_model
        ]

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
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
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
        command = [
            sys.executable,
            str(self.queue_runner_path),
            "start"
        ]

        if dry_run:
            print(f"[DRY RUN] Would execute (background): {' '.join(command)}")
            return True

        if self.is_runner_active():
            print("Queue runner already active, skipping start")
            return True

        try:
            log_path = Path("data/runner.log")
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
            sys.executable,
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
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
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
            sys.executable,
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

        Returns:
            dict with keys: running (bool), completed (list), failed (list)
        """
        result = {"running": False, "completed": [], "failed": []}

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
                result["running"] = True
            elif job_status == "completed":
                result["completed"].append(job_id)
            elif job_status == "failed":
                result["failed"].append(job_id)

        # Clean up PID file if runner is no longer active
        if not result["running"] and not self.is_runner_active():
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
                output_dir = Path("data/specs")
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
        Pull items from the priority queue and dispatch builds.
        This is the primary entry point for autonomous operation.

        Args:
            state_db: StateDB instance (for priority queue access)
            dry_run: If True, only print commands without executing

        Returns:
            List of BuildJob results
        """
        # Check if runner is already active
        if self.is_runner_active():
            print("Queue runner still active from previous dispatch, polling status...")
            sync = self.poll_and_sync_status()
            if sync["running"]:
                print("Build in progress, skipping new dispatch")
                return []

        # Pull pending items from priority queue
        approved_ideas = []
        for _ in range(self.config.max_approve_per_cycle):
            item = state_db.get_next_pending()
            if item is None:
                break

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
                    "artifact_type": "tool"
                }

            approved_ideas.append(idea)

            # Mark as dispatched
            if not dry_run and item.id:
                state_db.update_item_status(item.id, "dispatched", "dispatched_at")

        if not approved_ideas:
            print("No pending items in priority queue")
            return []

        return self.run(approved_ideas, dry_run=dry_run)
