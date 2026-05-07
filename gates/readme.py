"""
Readme Gate - Gate 4.7
Generates story-driven README files with life-scene hero images for published repos.
Runs after PublishGate succeeds. Uses Nemotron-3 via DeepInfra for README content
and banana-maker (Gemini) for hero image generation.

README structure: Scene (the struggle) -> Weight (the cost) -> Turn (the possibility)
-> Solution (what the tool does) -> Quick Start (try it). Reads like a magazine
article, not a product manual. Hero images depict real-life scenes of the problem
being solved, not abstract tech illustrations.
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
You are a storyteller who writes compelling GitHub README.md files that lead with
a human struggle and end with a technical solution. Your READMEs read like a short
magazine article, not a product manual. The reader should feel the pain before they
see the tool. By the time they reach the install command, they already want it.
Write in clear, direct prose. No jargon in the opening sections. Technical details
come only in the final sections for people who are already convinced.
Do NOT wrap the output in a markdown code fence -- output raw markdown directly.
Do NOT use em-dashes. Use commas, periods, or double-hyphens instead."""

README_USER_PROMPT = """\
Generate a story-driven README.md for this project. The README should read like
a short magazine article: open with the human struggle, build tension with real
stakes, then reveal the tool as the resolution. By the time the reader hits
"Quick Start," they should already want it.

**Title**: {title}

**Plain-speak description** (use this verbatim as the one-line tagline in the
banner block -- do NOT invent a new tagline):
{plain_description}

**Problem this solves** (the human struggle -- use this as raw material for
the opening scene, not as a section to paste verbatim):
{problem_statement}

**Target audience** (who lives this problem daily):
{target_audience}

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
  <a href="#the-turn">How it helps</a> &bull;
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#how-it-works">How It Works</a>
</p>
```

### 2. The Scene (opening -- no heading, just prose)
2-4 paragraphs of vivid, specific prose painting the daily reality of the person
who lives this problem. No tech jargon. No feature lists. Think opening paragraph
of an Atlantic or Wired article. Name the specific frustrations, the time sinks,
the moments where things go wrong. Use second person ("You") to pull the reader in.

Use the "Problem this solves" text above as raw material but transform it into a
scene, not a statement. Do NOT just restate the problem -- dramatize it. If the
problem statement is thin, invent concrete details that would resonate with the
target audience. A parent juggling logistics. A developer context-switching between
12 tabs. A consultant buried in manual reporting.

### 3. The Weight
One short section with a bold stat, a cost figure, or a time estimate that makes
the problem feel measurable. Can be a real data point (if provided) or a reasonable
estimate framed as "most people in this situation spend X hours per week on Y."
This section lands the emotional setup with something concrete.

Format as a centered blockquote:
```
> **X hours per week** -- that is what the average [persona] spends on [thing].
```

### 4. The Turn
2-3 sentences that pivot from problem to possibility. "What if [the painful thing]
just... happened?" This is the hinge of the entire README. The reader should feel
relief reading it. Do NOT name the tool yet -- keep it abstract for one beat.

### 5. What {title} Does
Now introduce the tool. 3-5 bullet points, each one sentence, plain language.
Connect each bullet back to a pain point from The Scene. Do not use technical
terms the target audience would not know. Frame capabilities as outcomes, not
features: "Stops you from missing the deadline" not "Automated scheduling engine."

### 6. Quick Start
Numbered steps to get running. Include clone, install, and first command. Use actual
package/command names from the file tree and spec. Keep it to 4-6 steps max.

### 7. See It In Action
One concrete example showing a realistic usage scenario. Show the command AND its
output. Frame it as a mini-story: "Say you have [situation]. You run [command].
Here is what happens:" followed by a code block with realistic output.

### 8. How It Works
A compact technical section for readers who are already convinced and want to
understand the internals. Include:
- A clean file tree (use the provided tree, cleaned up, with inline comments)
- A compact tech stack table: Technology | Purpose
Keep this section SHORT. The reader already wants the tool. This is reference, not pitch.

### 9. License & Author
MIT license. Author line:
```
{author_line}
```

## Quality Rules
- The Scene must be vivid and specific -- no generic "many people struggle with..."
- The Weight must include at least one concrete number (hours, dollars, percentage)
- The Turn must NOT mention the tool by name -- it is a moment of imagination
- Quick Start examples must show BOTH input AND output with realistic data
- Keep total length between 120-200 lines
- No em-dashes -- use commas, periods, or double-hyphens
- No AI cliches: no "leverage", "streamline", "empower", "unlock", "supercharge"
- Write like a journalist, not a marketer"""


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

        target_audience = (idea_ctx or {}).get("target_audience", "") or ""

        readme_content = self._generate_readme_content(
            spec_text, file_tree, title,
            plain_description=plain_description,
            problem_statement=problem_statement,
            target_audience=target_audience,
        )

        # 4. Generate infographic via banana-maker
        assets_dir = project_path / "assets"
        assets_dir.mkdir(exist_ok=True)
        infographic_path = assets_dir / "infographic.png"

        # For story-driven READMEs, prefer problem_statement as the visual brief
        # (the image should depict the struggle, not the solution). Fall back to
        # plain description, then to extracted features (legacy behavior).
        value_prop = problem_statement or plain_description or self._extract_features(readme_content)
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
        target_audience: str = "",
    ) -> str:
        """
        Generate README content using Nemotron-3 via DeepInfra.

        Args:
            spec_text: The app specification text
            file_tree: Project file tree string
            title: Project title
            plain_description: Plain-speak one-liner from IdeaForge (used verbatim
                in banner). Empty string if not available.
            problem_statement: Problem framing from IdeaForge (used as raw material
                for The Scene opening). Empty string if not available.
            target_audience: Who lives this problem daily (used for The Scene and
                The Weight sections). Empty string if not available.

        Returns:
            Generated README markdown content
        """
        prompt = README_USER_PROMPT.format(
            title=title,
            spec_text=spec_text,
            file_tree=file_tree,
            plain_description=plain_description or "(not provided -- write a concise one-liner from the spec)",
            problem_statement=problem_statement or "(not provided -- invent a concrete daily struggle for the target audience)",
            target_audience=target_audience or "(not provided -- infer from the spec who would use this daily)",
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

        Style brief: realistic life scene depicting the daily struggle that the
        tool solves. The viewer should see themselves in the image before they
        know what the tool does. Think editorial photography, not tech illustration.
        """
        brief = (value_prop or "a person overwhelmed by daily tasks").strip().rstrip(".")
        return (
            f"Create a warm, cinematic photograph-style image depicting a real life "
            f"moment of: {brief}. "
            f"Show a PERSON in a REAL SETTING experiencing this situation. "
            f"Kitchen counter with laptop open, kids' backpacks on the floor. "
            f"Office desk with too many browser tabs reflected in glasses. "
            f"A parent checking their phone while juggling groceries. "
            f"The scene should feel relatable, slightly chaotic, deeply human. "
            f"Warm golden-hour lighting, shallow depth of field, editorial photo style. "
            f"Color palette: warm ambers, soft whites, natural tones with pops of "
            f"blue from screens. "
            f"NO text, NO UI mockups, NO abstract shapes, NO icons, NO logos. "
            f"NO flat vector, NOT clip-art, NOT a diagram, NOT a schematic. "
            f"Think New York Times feature photo, not stock photography. "
            f"The mood is empathetic, not dramatic -- 'I know this feeling.'"
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
