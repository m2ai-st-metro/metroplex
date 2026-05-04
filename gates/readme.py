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
from gates._idea_context import load_idea_context

logger = logging.getLogger(__name__)

BANANA_MAKER_SCRIPT = Path.home() / ".claude" / "skills" / "banana-maker" / "generate_image.py"
BANANA_MAKER_PYTHON = Path.home() / ".claude" / "skills" / "banana-maker" / "venv" / "bin" / "python3"

README_SYSTEM_PROMPT = """\
You are a technical writer creating polished, high-quality GitHub README.md files.
These READMEs are the public face of open-source developer tools -- they must be
visually appealing, informative, and demonstrate clear value within 30 seconds of scanning.
Write clear, professional documentation with proper markdown formatting.
Do NOT wrap the output in a markdown code fence -- output raw markdown directly.
Do NOT use em-dashes. Use commas, periods, or double-hyphens instead."""

README_USER_PROMPT = """\
Generate a comprehensive, visually polished README.md for this project.

**Title**: {title}

**Plain-speak description** (use this verbatim as the one-line tagline in the
banner block -- do NOT invent a new tagline):
{plain_description}

**Problem this solves** (use this verbatim, lightly edited only for grammar,
in the dedicated "Problem" section described below):
{problem_statement}

**App Specification**:
{spec_text}

**File Tree**:
{file_tree}

## Requirements

Include these sections in this exact order. Output raw markdown only.

### 1. Centered Banner Block (HTML)
```html
<p align="center">
  <img src="assets/infographic.png" alt="{title}" width="800">
</p>

<h3 align="center">USE THE PLAIN-SPEAK DESCRIPTION PROVIDED ABOVE -- DO NOT INVENT</h3>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#features">Features</a> &bull;
  <a href="#examples">Examples</a> &bull;
  <a href="#contributing">Contributing</a>
</p>
```

### 2. What is this?
2-3 sentences explaining what the tool does and who it's for. Include a short
code block showing a realistic usage example with the command AND its output:
```
$ command --flag input
[show realistic output here]
```

### 3. Problem
Render the "Problem this solves" text provided above as a short prose block
(2-4 sentences). You may lightly edit for grammar and flow but do not change
its meaning, do not invent new pain points, and do not omit any concrete
detail. If the provided problem statement is empty, omit this entire section.

### 4. Features
A markdown table with two columns: Feature | Description. 4-8 rows covering the
key capabilities. Derive features from the source code and spec, not generic filler.

### 5. Quick Start
Numbered steps to get running. Include clone, install, and first command. Use actual
package/command names from the file tree and spec.

### 6. Examples
2-3 concrete usage examples. Each example should have:
- A bold title describing the use case
- The command to run
- Realistic sample output (not just "output here" placeholders)
Make examples progressively more advanced.

### 7. File Structure
A clean file tree showing the project layout. Use the provided file tree but clean
it up -- remove noise files, group logically, add inline comments for key files:
```
{title}/
  src/          # Core source code
  tests/        # Test suite
  ...
```

### 8. Tech Stack
A compact markdown table: Technology | Purpose. Only include what's actually used.

### 9. Contributing
Brief section: fork, edit, test, PR. 4 lines max.

### 10. License
MIT

### 11. Author
```
{author_line}
```

## Quality Rules
- Every example must show BOTH input AND output -- never leave output as a placeholder
- Use realistic data in examples, not "foo/bar/example.txt"
- Feature table rows must describe actual capabilities from the code, not marketing fluff
- Keep total length between 150-250 lines
- No em-dashes -- use commas, periods, or double-hyphens"""


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

        # 2b. Resolve original IdeaForge framing (plain-speak + problem). May be None.
        idea_ctx = load_idea_context(
            self.state_db, build_job_id, self.config.ideaforge_db
        )
        plain_description = (idea_ctx or {}).get("description", "") or ""
        problem_statement = (idea_ctx or {}).get("problem_statement", "") or ""

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

        readme_content = self._generate_readme_content(
            spec_text, file_tree, title,
            plain_description=plain_description,
            problem_statement=problem_statement,
        )

        # 4. Generate infographic via banana-maker
        assets_dir = project_path / "assets"
        assets_dir.mkdir(exist_ok=True)
        infographic_path = assets_dir / "infographic.png"

        # Prefer the plain-speak description as the visual brief; fall back to
        # problem statement, then to extracted features (legacy behavior).
        value_prop = plain_description or problem_statement or self._extract_features(readme_content)
        infographic_ok = self._generate_infographic(title, value_prop, infographic_path)
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

    def _generate_readme_content(
        self,
        spec_text: str,
        file_tree: str,
        title: str,
        plain_description: str = "",
        problem_statement: str = "",
    ) -> str:
        """
        Generate README content using Nemotron-3 via DeepInfra.

        Args:
            spec_text: The app specification text
            file_tree: Project file tree string
            title: Project title
            plain_description: Plain-speak one-liner from IdeaForge (used verbatim
                in banner). Empty string if not available.
            problem_statement: Problem framing from IdeaForge (used verbatim in
                Problem section). Empty string if not available.

        Returns:
            Generated README markdown content
        """
        prompt = README_USER_PROMPT.format(
            title=title,
            spec_text=spec_text,
            file_tree=file_tree,
            plain_description=plain_description or "(not provided -- write a concise one-liner from the spec)",
            problem_statement=problem_statement or "",
            author_line=self._build_author_line(),
        )

        model = self.config.spec_llm_model
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": README_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=6144,
        )

        content = response.choices[0].message.content or ""

        # Record cost if state_db is available (tolerates state_db=None for tests)
        if self.state_db is not None:
            try:
                from cost_rates import estimate_cost
                input_tokens = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens
                cost = estimate_cost(model, input_tokens, output_tokens)
                self.state_db.record_cost(
                    source="readme_generation",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=cost,
                )
            except Exception as e:
                logger.warning("Failed to record readme generation cost: %s", e)

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

    def _build_author_line(self) -> str:
        """Generate the README footer author line from configured publish targets."""
        links = ["[M2AI](https://m2ai.co)"]
        for target in self.config.publish_targets:
            if target == "github":
                links.append(f"[@{self.config.github_org}](https://github.com/{self.config.github_org})")
            elif target == "gitlab":
                links.append(
                    f"[{self.config.gitlab_host}/{self.config.gitlab_namespace}]"
                    f"(https://{self.config.gitlab_host}/{self.config.gitlab_namespace})"
                )
        return "Matthew Snow -- " + " | ".join(links)

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

    def _generate_infographic(self, title: str, value_prop: str, output_path: Path) -> bool:
        """
        Generate a hero image using banana-maker.

        Args:
            title: Project title
            value_prop: One-line plain-speak description of what the project does
                (used as the visual brief). May fall back to a feature list if no
                IdeaForge context is available.
            output_path: Where to save the generated image

        Returns:
            True if generation succeeded
        """
        if not BANANA_MAKER_SCRIPT.is_file():
            logger.warning(f"banana-maker script not found at {BANANA_MAKER_SCRIPT}")
            return False

        prompt = self._build_infographic_prompt(title, value_prop)

        # Ensure parent dir exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale images before generation so the .png/.jpg check below
        # cannot be fooled by a leftover file from a previous run. banana-maker
        # always writes .jpg regardless of the requested extension, so an old
        # .png file would shadow the new output if not removed first.
        for stale in (output_path, output_path.with_suffix(".jpg")):
            if stale.is_file():
                stale.unlink()
                logger.info(f"Removed stale {stale.name} before regeneration")

        try:
            python_bin = str(BANANA_MAKER_PYTHON) if BANANA_MAKER_PYTHON.is_file() else "python3"
            result = subprocess.run(
                [
                    python_bin,
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

            # banana-maker saves as .jpg regardless of requested extension
            jpg_path = output_path.with_suffix(".jpg")
            if jpg_path.is_file():
                jpg_path.rename(output_path)
                return True
            if output_path.is_file():
                return True
            logger.error("banana-maker produced no output file")
            return False

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
            # Detect default branch
            branch_result = subprocess.run(
                ["git", "-C", str(project_path), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            branch = branch_result.stdout.strip() or "main"

            # Pull latest from remote before committing (Gate 4.9 may have pushed)
            subprocess.run(
                ["git", "-C", str(project_path), "pull", "origin", branch, "--rebase"],
                capture_output=True,
                text=True,
                timeout=60,
            )

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
                ["git", "-C", str(project_path), "push", "origin", branch],
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

    def _build_infographic_prompt(self, title: str, value_prop: str) -> str:
        """
        Build the banana-maker prompt for a project hero image.

        Style brief: vibrant magazine-cover, single visual metaphor, warm palette.
        Explicit negatives keep banana-maker from defaulting to flat-vector
        infographic clutter (per banana-maker prompt-pattern feedback).
        """
        brief = (value_prop or "a developer tool").strip().rstrip(".")
        return (
            f"Create a vibrant, warm hero image for a developer tool called '{title}'. "
            f"Single central visual metaphor representing: {brief}. "
            f"NO feature lists, NO bullet points, NO multiple panels, NO grids, "
            f"NO icons-in-boxes, NO text labels beyond the project name. "
            f"Think editorial magazine cover, not infographic. "
            f"Warm vibrant palette: sunset oranges, magentas, gold and amber accents "
            f"on a deep indigo or charcoal background. Cinematic lighting, modern "
            f"editorial illustration style with rich color saturation. "
            f"NOT flat vector, NOT clip-art, NOT a schematic diagram."
        )

    def build_infographic_command(self, title: str, value_prop: str, output_path: str) -> list[str]:
        """
        Build the banana-maker subprocess command (exposed for testing).

        Args:
            title: Project title
            value_prop: One-line value proposition (visual brief)
            output_path: Output file path

        Returns:
            Command list for subprocess
        """
        prompt = self._build_infographic_prompt(title, value_prop)
        python_bin = str(BANANA_MAKER_PYTHON) if BANANA_MAKER_PYTHON.is_file() else "python3"
        return [
            python_bin,
            str(BANANA_MAKER_SCRIPT),
            prompt,
            "--model", "flash",
            "--output", output_path,
            "--aspect-ratio", "16:9",
        ]
