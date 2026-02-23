"""
Build Gate - Gate 2
Generates yce-harness app spec files from approved ideas using Jinja2 templates.
Orchestrates build queue via queue_runner.py subprocess calls.
"""
import sys
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, Template, TemplateNotFound

from config import Config
from models import BuildJob
from db import StateDB
from audit import AuditLogger


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

        Process:
        1. Load spec_templates/app_spec_template.md as Jinja2 template
        2. Render with idea data: title, description, problem_statement,
           target_audience, artifact_type, tech_stack hints
        3. Write rendered spec to output_dir / f"app_spec_{idea['id']}.txt"
        4. Return the output path

        Template variables:
        - title: Idea title
        - description: Idea description
        - problem_statement: Problem being solved
        - target_audience: Who the app is for
        - artifact_type: Type of artifact (tool, agent, product)
        - tech_stack: Additional tech stack hints (optional)

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
        Queue a build job via queue_runner.py.

        Process:
        1. Build command: [sys.executable, queue_runner_path, "add", str(spec_path), "--id", job_id, "--model", config.build_model]
        2. If dry_run: print command, return None
        3. Run subprocess with capture_output=True, text=True, timeout=30
        4. Check returncode. If 0: record BuildJob with status="queued". If non-zero: record as "failed", log error.
        5. Return BuildJob

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
                # Success - record as queued
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
                # Failed - record as failed
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
            self.audit_logger.log_error(
                gate="build",
                error=error_msg,
                details={"idea_id": idea["id"], "job_id": job_id}
            )

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
            self.audit_logger.log_error(
                gate="build",
                error=error_msg,
                details={"idea_id": idea["id"], "job_id": job_id}
            )

            return job

    def start_queue(self, dry_run: bool = False) -> bool:
        """
        Start the queue runner.

        Process:
        1. Build command: [sys.executable, queue_runner_path, "start"]
        2. If dry_run: print command, return True
        3. Run subprocess (this is long-running — use timeout=None or very large timeout)
        4. Return True if returncode==0

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
        Check queue status.

        Process:
        1. Run [sys.executable, queue_runner_path, "status", "--json"]
        2. Parse JSON output
        3. Return parsed dict

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
                import json
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

    def run(self, approved_ideas: list[dict], dry_run: bool = False) -> list[BuildJob]:
        """
        Run build orchestration for all approved ideas.

        Process:
        1. For each approved idea: generate spec, queue build
        2. If not dry_run and at least one job queued: start queue
        3. Return list of BuildJob results

        Args:
            approved_ideas: List of approved idea dictionaries
            dry_run: If True, only print commands without executing

        Returns:
            List of BuildJob results
        """
        jobs = []

        # Check if spec generator is available
        if self.spec_generator is None:
            print("Warning: Spec generator not initialized (template directory not found)")
            return []

        # Generate specs and queue builds
        for idea in approved_ideas:
            try:
                # Generate spec (assuming output_dir is data/specs)
                output_dir = Path("data/specs")
                spec_path = self.spec_generator.generate_spec(idea, output_dir)

                # Queue build
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

                # Record failed job even if spec generation failed
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

        # Start queue if any jobs were queued
        if not dry_run and jobs:
            queued_jobs = [j for j in jobs if j.status == "queued"]
            if queued_jobs:
                self.start_queue(dry_run=dry_run)

        return jobs
