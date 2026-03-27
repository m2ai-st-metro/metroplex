"""
Readme Gate - Gate 4.7
Generates enhanced README files with infographics for newly published repos.
Runs after PublishGate succeeds. Uses Nemotron-3 via DeepInfra for README content
and banana-maker (Gemini) for infographic generation.
"""
import os
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from config import Config
from db import StateDB
from audit import AuditLogger
from models import PublishJob

logger = logging.getLogger(__name__)

BANANA_MAKER_SCRIPT = Path.home() / ".claude" / "skills" / "banana-maker" / "generate_image.py"

README_SYSTEM_PROMPT = """\
You are a technical writer creating polished GitHub README.md files.
Write clear, professional documentation with proper markdown formatting.
Do NOT wrap the output in a markdown code fence — output raw markdown directly."""

README_USER_PROMPT = """\
Generate a comprehensive README.md for this project.

**Title**: {title}

**App Specification**:
{spec_text}

**File Tree**:
{file_tree}

## Requirements

Include these sections in order:

1. **Title** with relevant badges (e.g. Python version, license)
2. **Infographic** — include `![{title} Overview](assets/infographic.png)` right after the title
3. **Overview** — what this project does and who it's for (derive from the spec)
4. **Problem Statement** — the core problem being solved
5. **Features** — key features derived from the source code and spec (bullet list)
6. **Tech Stack** — languages, frameworks, libraries used
7. **Quick Start / Installation** — steps to get running locally
8. **Usage** — example commands or workflows
9. **Architecture** — brief description of how the code is organized
10. **License** — MIT

Keep it concise but informative. Use the spec for context and the file tree for accuracy.
Output raw markdown only — no code fences wrapping the entire document."""


class ReadmeGate:
    """Gate 4.7: README Enhancement — Generate polished READMEs with infographics."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        audit_logger: AuditLogger,
    ):
        """
        Initialize README Gate.

        Args:
            config: Metroplex configuration
            state_db: State database manager
            audit_logger: Audit logger
        """
        self.config = config
        self.state_db = state_db
        self.audit_logger = audit_logger

        # Initialize OpenAI-compatible client for DeepInfra
        api_key = os.environ.get("DEEPINFRA_API_KEY", "")
        if api_key:
            self.client = OpenAI(
                api_key=api_key,
                base_url="https://api.deepinfra.com/v1/openai",
            )
        else:
            self.client = None
            logger.warning("DEEPINFRA_API_KEY not set — README generation will fail")

    def run(
        self,
        published_jobs: list[PublishJob] | None = None,
        dry_run: bool = False,
    ) -> list[dict]:
        """
        Run README enhancement on published builds.

        If published_jobs is provided, process those. Otherwise query the DB
        for published builds that haven't had README enhancement yet.

        Args:
            published_jobs: List of PublishJob objects to process (optional)
            dry_run: If True, print what would happen but don't modify anything

        Returns:
            List of result dicts with keys: build_job_id, status, error
        """
        # Determine which jobs to process
        if published_jobs is not None:
            candidates = [
                j for j in published_jobs
                if j.status == "published" and not self.state_db.has_readme(j.build_job_id)
            ]
        else:
            candidates_raw = self.state_db.get_readme_pending()
            candidates = candidates_raw

        if not candidates:
            return []

        results = []

        for job in candidates:
            if isinstance(job, PublishJob):
                build_job_id = job.build_job_id
                title = job.title
                project_dir = job.project_dir
                repo_url = job.repo_url or ""
            else:
                # Dict from get_readme_pending()
                build_job_id = job["build_job_id"]
                title = job["title"]
                project_dir = job["project_dir"]
                repo_url = job.get("repo_url", "")

            if dry_run:
                print(f"[DRY RUN] Would generate README for: {title}")
                print(f"  Build ID: {build_job_id}")
                print(f"  Dir:      {project_dir}")
                results.append({
                    "build_job_id": build_job_id,
                    "status": "pending",
                    "error": None,
                })
                continue

            try:
                result = self._process_one(build_job_id, title, project_dir, repo_url)
                results.append(result)

                self.audit_logger.log_decision(
                    gate="readme",
                    action=result["status"],
                    details={
                        "build_job_id": build_job_id,
                        "title": title,
                        "error": result.get("error"),
                    },
                )
            except Exception as e:
                error_msg = f"README generation failed for {build_job_id}: {e}"
                logger.error(error_msg)
                results.append({
                    "build_job_id": build_job_id,
                    "status": "failed",
                    "error": str(e),
                })
                self.state_db.record_readme_job(
                    build_job_id=build_job_id,
                    repo_url=repo_url,
                    status="failed",
                    error=str(e),
                )
                self.audit_logger.log_error("readme", error_msg)

        return results

    def _process_one(
        self, build_job_id: str, title: str, project_dir: str, repo_url: str
    ) -> dict:
        """
        Process a single published build: generate README + infographic.

        Args:
            build_job_id: Queue job ID
            title: Project title
            project_dir: Path to the project directory
            repo_url: GitHub repo URL

        Returns:
            Result dict with keys: build_job_id, status, error
        """
        project_path = Path(project_dir)
        if not project_path.is_dir():
            error = f"Project directory not found: {project_dir}"
            self.state_db.record_readme_job(
                build_job_id=build_job_id,
                repo_url=repo_url,
                status="failed",
                error=error,
            )
            return {"build_job_id": build_job_id, "status": "failed", "error": error}

        # 1. Read the spec file
        spec_text = self._read_spec(build_job_id)

        # 2. Build file tree
        file_tree = self._build_file_tree(project_path)

        # 3. Generate README content via LLM
        if not self.client:
            error = "DEEPINFRA_API_KEY not set — cannot generate README"
            self.state_db.record_readme_job(
                build_job_id=build_job_id,
                repo_url=repo_url,
                status="failed",
                error=error,
            )
            return {"build_job_id": build_job_id, "status": "failed", "error": error}

        readme_content = self._generate_readme_content(spec_text, file_tree, title)

        # 4. Generate infographic via banana-maker
        assets_dir = project_path / "assets"
        assets_dir.mkdir(exist_ok=True)
        infographic_path = assets_dir / "infographic.png"

        features = self._extract_features(readme_content)
        infographic_ok = self._generate_infographic(title, features, infographic_path)
        if not infographic_ok:
            logger.warning(f"Infographic generation failed for {title} — continuing without it")
            # Remove the infographic reference from README if generation failed
            readme_content = readme_content.replace(
                f"![{title} Overview](assets/infographic.png)\n", ""
            )
            readme_content = readme_content.replace(
                f"![{title} Overview](assets/infographic.png)", ""
            )

        # 5. Write README.md
        readme_path = project_path / "README.md"
        readme_path.write_text(readme_content)
        logger.info(f"Wrote README.md to {readme_path}")

        # 6. Commit and push
        commit_ok, commit_error = self._commit_and_push(project_path, infographic_ok)
        if not commit_ok:
            self.state_db.record_readme_job(
                build_job_id=build_job_id,
                repo_url=repo_url,
                status="failed",
                error=f"git push failed: {commit_error}",
            )
            return {
                "build_job_id": build_job_id,
                "status": "failed",
                "error": f"git push failed: {commit_error}",
            }

        # 7. Record success
        self.state_db.record_readme_job(
            build_job_id=build_job_id,
            repo_url=repo_url,
            status="completed",
            error=None,
        )

        print(f"+ README enhanced: {title}")
        return {"build_job_id": build_job_id, "status": "completed", "error": None}

    def _read_spec(self, build_job_id: str) -> str:
        """Read the spec file for a build job from the DB."""
        build = self.state_db.get_build_by_queue_job_id(build_job_id)
        if not build:
            return "(spec not available)"

        spec_path = build.get("spec_path", "")
        if spec_path and Path(spec_path).is_file():
            try:
                return Path(spec_path).read_text()[:8000]  # Cap at 8k chars
            except Exception as e:
                logger.warning(f"Failed to read spec at {spec_path}: {e}")

        return "(spec not available)"

    def _build_file_tree(self, project_path: Path, max_depth: int = 3) -> str:
        """
        Build a file tree string for the project directory.
        Excludes common noise directories.
        """
        exclude_dirs = {
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
            ".egg-info", ".tox",
        }
        lines = []

        def _walk(path: Path, prefix: str, depth: int):
            if depth > max_depth:
                return
            try:
                entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            except PermissionError:
                return

            for i, entry in enumerate(entries):
                if entry.name in exclude_dirs:
                    continue
                is_last = i == len(entries) - 1
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{entry.name}")
                if entry.is_dir():
                    extension = "    " if is_last else "│   "
                    _walk(entry, prefix + extension, depth + 1)

        lines.append(project_path.name + "/")
        _walk(project_path, "", 0)
        return "\n".join(lines[:100])  # Cap at 100 lines

    def _generate_readme_content(self, spec_text: str, file_tree: str, title: str) -> str:
        """
        Generate README content using Nemotron-3 via DeepInfra.

        Args:
            spec_text: The app specification text
            file_tree: Project file tree string
            title: Project title

        Returns:
            Generated README markdown content
        """
        prompt = README_USER_PROMPT.format(
            title=title,
            spec_text=spec_text,
            file_tree=file_tree,
        )

        response = self.client.chat.completions.create(
            model=self.config.spec_llm_model,
            messages=[
                {"role": "system", "content": README_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
        )

        content = response.choices[0].message.content or ""

        # Strip wrapping code fence if the LLM added one
        if content.startswith("```markdown"):
            content = content[len("```markdown"):].strip()
        if content.startswith("```md"):
            content = content[len("```md"):].strip()
        if content.startswith("```"):
            content = content[3:].strip()
        if content.endswith("```"):
            content = content[:-3].strip()

        return content

    def _extract_features(self, readme_content: str) -> str:
        """Extract feature names from generated README for infographic prompt."""
        features = []
        in_features = False
        for line in readme_content.splitlines():
            if "feature" in line.lower() and line.startswith("#"):
                in_features = True
                continue
            if in_features and line.startswith("#"):
                break
            if in_features and line.strip().startswith(("-", "*", "+")):
                feature = line.strip().lstrip("-*+ ").split("—")[0].split("–")[0].strip()
                if feature and len(feature) < 80:
                    features.append(feature)

        return ", ".join(features[:6]) if features else "developer tool, automation, CLI"

    def _generate_infographic(self, title: str, features: str, output_path: Path) -> bool:
        """
        Generate an infographic using banana-maker.

        Args:
            title: Project title
            features: Comma-separated feature list
            output_path: Where to save the generated image

        Returns:
            True if generation succeeded
        """
        if not BANANA_MAKER_SCRIPT.is_file():
            logger.warning(f"banana-maker script not found at {BANANA_MAKER_SCRIPT}")
            return False

        prompt = (
            f"Create a clean, modern infographic for a developer tool called '{title}'. "
            f"Show the key features: {features}. "
            f"Use a dark theme with blue/purple accents. "
            f"Minimalist style, no text-heavy elements."
        )

        # Ensure parent dir exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            result = subprocess.run(
                [
                    "python3",
                    str(BANANA_MAKER_SCRIPT),
                    prompt,
                    "--model", "flash",
                    "--output", str(output_path),
                    "--aspect-ratio", "16:9",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                logger.error(f"banana-maker failed: {result.stderr.strip()}")
                return False

            return output_path.is_file()

        except subprocess.TimeoutExpired:
            logger.error("banana-maker timed out (120s)")
            return False
        except Exception as e:
            logger.error(f"banana-maker error: {e}")
            return False

    def _commit_and_push(self, project_path: Path, has_infographic: bool) -> tuple[bool, str | None]:
        """
        Commit README and assets, then push to GitHub.

        Args:
            project_path: Path to the project directory
            has_infographic: Whether an infographic was generated

        Returns:
            Tuple of (success, error_message)
        """
        try:
            # Stage files
            add_paths = ["README.md"]
            if has_infographic:
                add_paths.append("assets/")

            for path in add_paths:
                subprocess.run(
                    ["git", "-C", str(project_path), "add", path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            # Commit
            commit_result = subprocess.run(
                [
                    "git", "-C", str(project_path),
                    "commit", "-m", "Add enhanced README with infographic",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if commit_result.returncode != 0:
                stderr = commit_result.stderr.strip()
                # "nothing to commit" is not a real error
                if "nothing to commit" in stderr or "nothing to commit" in commit_result.stdout:
                    return (True, None)
                return (False, f"git commit failed: {stderr}")

            # Push
            push_result = subprocess.run(
                ["git", "-C", str(project_path), "push", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if push_result.returncode != 0:
                return (False, f"git push failed: {push_result.stderr.strip()}")

            return (True, None)

        except subprocess.TimeoutExpired:
            return (False, "git operation timed out")
        except Exception as e:
            return (False, f"git error: {str(e)}")

    def build_infographic_command(self, title: str, features: str, output_path: str) -> list[str]:
        """
        Build the banana-maker subprocess command (exposed for testing).

        Args:
            title: Project title
            features: Comma-separated feature list
            output_path: Output file path

        Returns:
            Command list for subprocess
        """
        prompt = (
            f"Create a clean, modern infographic for a developer tool called '{title}'. "
            f"Show the key features: {features}. "
            f"Use a dark theme with blue/purple accents. "
            f"Minimalist style, no text-heavy elements."
        )
        return [
            "python3",
            str(BANANA_MAKER_SCRIPT),
            prompt,
            "--model", "flash",
            "--output", output_path,
            "--aspect-ratio", "16:9",
        ]
