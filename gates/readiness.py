"""
Readiness Gate - Gate 4.9
Checks publish readiness of GitHub repos and auto-fixes common issues.
Runs after PublishGate + ReadmeGate. Uses gh CLI for GitHub API calls
and Nemotron-3 via DeepInfra for LLM-based fixes.
"""
import base64
import json
import logging
import os
import re
import subprocess
import time

from openai import OpenAI

from config import Config
from db import StateDB
from audit import AuditLogger
from models import PublishJob

logger = logging.getLogger(__name__)

# Files that indicate build artifacts left in the repo
BUILD_ARTIFACT_FILES = [
    "app_spec.txt",
    ".linear_project.json",
    "init.sh",
    ".codebase_learnings.json",
    ".claude_settings.json",
]

MIT_LICENSE_TEXT = """MIT License

Copyright (c) 2026 M2AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

TOPICS_SYSTEM_PROMPT = "You generate GitHub repository topics. Respond with ONLY a JSON array of lowercase topic strings."
TOPICS_USER_PROMPT = """Given this README content, suggest 5-8 GitHub topics as a JSON array.
Topics should be lowercase, hyphenated where needed, and relevant to the project's tech stack and purpose.
Example: ["python", "cli-tool", "automation", "devops"]

README (first 2000 chars):
{readme_excerpt}"""

DESCRIPTION_SYSTEM_PROMPT = "You write concise GitHub repository descriptions. Respond with ONLY the description text, no quotes."
DESCRIPTION_USER_PROMPT = """Write a one-sentence GitHub repo description (max 200 chars) as a value proposition.
The repo is named "{repo_name}" and here is the README excerpt:

{readme_excerpt}"""


class ReadinessGate:
    """Gate 4.9: Publish Readiness — Check and fix repo quality before public listing."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        audit_logger: AuditLogger,
    ):
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
            logger.warning("DEEPINFRA_API_KEY not set — LLM-based fixes will fail")

    def run(
        self,
        published_jobs: list[PublishJob] | None = None,
        dry_run: bool = False,
    ) -> list[dict]:
        """
        Run readiness checks on published builds.

        If published_jobs is provided, process those. Otherwise query the DB
        for published builds that haven't had readiness checks yet.

        Returns list of result dicts with keys: build_job_id, repo_name, status, checks_passed, checks_failed, fixes_applied, fixes_failed, error
        """
        if published_jobs is not None:
            candidates = [
                j for j in published_jobs
                if j.status == "published" and not self.state_db.has_readiness(j.build_job_id)
            ]
        else:
            candidates = self.state_db.get_readiness_pending()

        if not candidates:
            return []

        # Enforce per-cycle cap
        max_per_cycle = self.config.max_readiness_per_cycle
        if len(candidates) > max_per_cycle:
            candidates = candidates[:max_per_cycle]

        results = []

        for job in candidates:
            if isinstance(job, PublishJob):
                build_job_id = job.build_job_id
                repo_name = job.repo_name
                repo_url = job.repo_url or ""
            else:
                build_job_id = job["build_job_id"]
                repo_name = job.get("repo_name", "")
                repo_url = job.get("repo_url", "")

            if not repo_name and repo_url:
                # Extract repo name from URL
                repo_name = repo_url.rstrip("/").split("/")[-1]

            if dry_run:
                print(f"[DRY RUN] Would check readiness for: {repo_name}")
                results.append({
                    "build_job_id": build_job_id,
                    "repo_name": repo_name,
                    "status": "pending",
                    "error": None,
                })
                continue

            try:
                result = self._process_one(build_job_id, repo_name, repo_url)
                results.append(result)

                self.audit_logger.log_decision(
                    gate="readiness",
                    action=result["status"],
                    details={
                        "build_job_id": build_job_id,
                        "repo_name": repo_name,
                        "checks_passed": result.get("checks_passed", []),
                        "checks_failed": result.get("checks_failed", []),
                        "fixes_applied": result.get("fixes_applied", []),
                        "fixes_failed": result.get("fixes_failed", []),
                        "error": result.get("error"),
                    },
                )
            except Exception as e:
                error_msg = f"Readiness check failed for {repo_name}: {e}"
                logger.error(error_msg)
                result = {
                    "build_job_id": build_job_id,
                    "repo_name": repo_name,
                    "status": "failed",
                    "error": str(e),
                }
                results.append(result)
                self.state_db.record_readiness_job(
                    build_job_id=build_job_id,
                    repo_name=repo_name,
                    repo_url=repo_url,
                    status="failed",
                    error=str(e),
                )
                self.audit_logger.log_error("readiness", error_msg)

        return results

    def run_batch(
        self,
        repo_names: list[str],
        dry_run: bool = False,
    ) -> list[dict]:
        """
        Run readiness checks + fixes on a list of repo names (batch mode).

        Used by the readiness-fix CLI subcommand for existing repos that
        weren't built by the pipeline.

        Args:
            repo_names: List of repo names (without org prefix)
            dry_run: If True, check only, don't fix

        Returns:
            List of result dicts
        """
        org = self.config.github_org
        results = []
        consecutive_errors = 0

        for repo_name in repo_names:
            if consecutive_errors >= 3:
                print("! Circuit breaker: 3 consecutive API errors, halting batch")
                break

            repo_url = f"https://github.com/{org}/{repo_name}"

            if dry_run:
                print(f"[DRY RUN] Would check readiness for: {repo_name}")
                # Still run checks in dry-run to show what would happen
                try:
                    passed, failed = self._run_checks(repo_name)
                    print(f"  Passed: {passed}")
                    print(f"  Failed: {failed}")
                    results.append({
                        "build_job_id": None,
                        "repo_name": repo_name,
                        "status": "pending",
                        "checks_passed": passed,
                        "checks_failed": failed,
                        "fixes_applied": [],
                        "fixes_failed": [],
                        "error": None,
                    })
                    consecutive_errors = 0
                except Exception as e:
                    print(f"  Error: {e}")
                    results.append({
                        "build_job_id": None,
                        "repo_name": repo_name,
                        "status": "failed",
                        "error": str(e),
                    })
                    consecutive_errors += 1
                time.sleep(1)
                continue

            try:
                result = self._process_one(None, repo_name, repo_url)
                results.append(result)
                consecutive_errors = 0

                status_icon = "+" if result["status"] == "completed" else "~" if result["status"] == "partial" else "x"
                print(f"{status_icon} {repo_name}: {result['status']}")
                if result.get("fixes_applied"):
                    print(f"  Fixed: {result['fixes_applied']}")
                if result.get("fixes_failed"):
                    print(f"  Could not fix: {result['fixes_failed']}")
            except Exception as e:
                error_msg = f"Readiness batch failed for {repo_name}: {e}"
                logger.error(error_msg)
                results.append({
                    "build_job_id": None,
                    "repo_name": repo_name,
                    "status": "failed",
                    "error": str(e),
                })
                consecutive_errors += 1

            time.sleep(1)  # Rate limiting between repos

        return results

    def _process_one(
        self, build_job_id: str | None, repo_name: str, repo_url: str
    ) -> dict:
        """Process a single repo: run checks, apply fixes, record result."""
        # Run checks
        passed, failed = self._run_checks(repo_name)

        if not failed:
            # All checks passed
            self.state_db.record_readiness_job(
                build_job_id=build_job_id,
                repo_name=repo_name,
                repo_url=repo_url,
                status="completed",
                checks_passed=json.dumps(passed),
                checks_failed="[]",
                fixes_applied="[]",
                fixes_failed="[]",
            )
            print(f"+ Readiness OK: {repo_name} (all {len(passed)} checks passed)")
            return {
                "build_job_id": build_job_id,
                "repo_name": repo_name,
                "status": "completed",
                "checks_passed": passed,
                "checks_failed": [],
                "fixes_applied": [],
                "fixes_failed": [],
                "error": None,
            }

        # Apply fixes for failed checks
        fixes_applied, fixes_failed = self._apply_fixes(repo_name, failed)

        # Determine final status
        if not fixes_failed:
            status = "completed"
        elif fixes_applied:
            status = "partial"
        else:
            status = "failed"

        self.state_db.record_readiness_job(
            build_job_id=build_job_id,
            repo_name=repo_name,
            repo_url=repo_url,
            status=status,
            checks_passed=json.dumps(passed),
            checks_failed=json.dumps(failed),
            fixes_applied=json.dumps(fixes_applied),
            fixes_failed=json.dumps(fixes_failed),
        )

        print(f"{'~' if status == 'partial' else 'x'} Readiness {status}: {repo_name} — fixed {len(fixes_applied)}, remaining {len(fixes_failed)}")
        return {
            "build_job_id": build_job_id,
            "repo_name": repo_name,
            "status": status,
            "checks_passed": passed,
            "checks_failed": failed,
            "fixes_applied": fixes_applied,
            "fixes_failed": fixes_failed,
            "error": None,
        }

    # --- Check Methods ---

    def _run_checks(self, repo_name: str) -> tuple[list[str], list[str]]:
        """Run all 6 readiness checks on a repo. Returns (passed, failed)."""
        org = self.config.github_org
        passed = []
        failed = []

        checks = [
            ("no_build_artifacts", self._check_no_build_artifacts),
            ("has_license", self._check_has_license),
            ("has_topics", self._check_has_topics),
            ("has_description", self._check_has_description),
            ("no_placeholder_urls", self._check_no_placeholder_urls),
            ("has_banner_image", self._check_has_banner_image),
        ]

        for check_name, check_fn in checks:
            try:
                ok, detail = check_fn(org, repo_name)
                if ok:
                    passed.append(check_name)
                else:
                    failed.append(check_name)
                    logger.info(f"Check {check_name} failed for {repo_name}: {detail}")
            except Exception as e:
                failed.append(check_name)
                logger.warning(f"Check {check_name} error for {repo_name}: {e}")

        return passed, failed

    def _gh_api(self, endpoint: str, method: str = "GET", data: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
        """Call gh api with optional method and data."""
        cmd = ["gh", "api", endpoint]
        if method != "GET":
            cmd.extend(["--method", method])
        if data is not None:
            cmd.extend(["--input", "-"])
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=data,
            )
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _check_no_build_artifacts(self, org: str, repo_name: str) -> tuple[bool, str]:
        """Check that no build artifact files remain in the repo root."""
        result = self._gh_api(f"repos/{org}/{repo_name}/contents")
        if result.returncode != 0:
            return False, f"API error: {result.stderr[:200]}"

        try:
            contents = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False, "Invalid JSON from contents API"

        found = []
        for item in contents:
            if item.get("name") in BUILD_ARTIFACT_FILES:
                found.append(item["name"])

        if found:
            return False, f"Build artifacts found: {found}"
        return True, "No build artifacts"

    def _check_has_license(self, org: str, repo_name: str) -> tuple[bool, str]:
        """Check that the repo has a license file."""
        result = self._gh_api(f"repos/{org}/{repo_name}/license")
        if result.returncode == 0:
            return True, "License exists"
        return False, "No license file"

    def _check_has_topics(self, org: str, repo_name: str) -> tuple[bool, str]:
        """Check that the repo has at least 3 topics."""
        result = self._gh_api(f"repos/{org}/{repo_name}/topics")
        if result.returncode != 0:
            return False, f"API error: {result.stderr[:200]}"

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False, "Invalid JSON from topics API"

        names = data.get("names", [])
        if len(names) >= 3:
            return True, f"{len(names)} topics"
        return False, f"Only {len(names)} topics (need >= 3)"

    def _check_has_description(self, org: str, repo_name: str) -> tuple[bool, str]:
        """Check that the repo has a meaningful description."""
        result = self._gh_api(f"repos/{org}/{repo_name}")
        if result.returncode != 0:
            return False, f"API error: {result.stderr[:200]}"

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False, "Invalid JSON from repo API"

        description = data.get("description") or ""
        min_length = len(repo_name) + 10
        if len(description) > min_length:
            return True, f"Description: {description[:50]}..."
        return False, f"Description too short ({len(description)} chars, need > {min_length})"

    def _check_no_placeholder_urls(self, org: str, repo_name: str) -> tuple[bool, str]:
        """Check that README doesn't contain placeholder URLs."""
        readme_content = self._fetch_readme(org, repo_name)
        if readme_content is None:
            return True, "No README (skipped)"

        placeholder_patterns = [
            r"<repo-url>",
            r"your-org",
            r"localhost",
            r"example\.com",
        ]
        found = []
        for pattern in placeholder_patterns:
            if re.search(pattern, readme_content, re.IGNORECASE):
                found.append(pattern)

        if found:
            return False, f"Placeholder URLs found: {found}"
        return True, "No placeholder URLs"

    def _check_has_banner_image(self, org: str, repo_name: str) -> tuple[bool, str]:
        """Check that README has a banner image in the first 30 lines."""
        readme_content = self._fetch_readme(org, repo_name)
        if readme_content is None:
            return True, "No README (skipped)"

        first_30_lines = "\n".join(readme_content.splitlines()[:30])
        if re.search(r"!\[", first_30_lines):
            return True, "Banner image found"
        return False, "No banner image in first 30 lines of README"

    def _fetch_readme(self, org: str, repo_name: str) -> str | None:
        """Fetch and decode README content from GitHub. Returns None on 404."""
        result = self._gh_api(f"repos/{org}/{repo_name}/contents/README.md")
        if result.returncode != 0:
            return None

        try:
            data = json.loads(result.stdout)
            content_b64 = data.get("content", "")
            return base64.b64decode(content_b64).decode("utf-8", errors="replace")
        except (json.JSONDecodeError, Exception):
            return None

    # --- Fix Methods ---

    def _apply_fixes(self, repo_name: str, failed_checks: list[str]) -> tuple[list[str], list[str]]:
        """Apply auto-fixes for failed checks. Returns (fixes_applied, fixes_failed)."""
        org = self.config.github_org
        fixes_applied = []
        fixes_failed = []
        llm_calls = 0

        for check in failed_checks:
            if check == "no_build_artifacts":
                ok = self._fix_remove_build_artifacts(org, repo_name)
                if ok:
                    fixes_applied.append(check)
                else:
                    fixes_failed.append(check)

            elif check == "has_license":
                ok = self._fix_add_license(org, repo_name)
                if ok:
                    fixes_applied.append(check)
                else:
                    fixes_failed.append(check)

            elif check == "has_topics" and llm_calls < 2:
                ok = self._fix_generate_topics(org, repo_name)
                if ok:
                    fixes_applied.append(check)
                    llm_calls += 1
                else:
                    fixes_failed.append(check)

            elif check == "has_description" and llm_calls < 2:
                ok = self._fix_generate_description(org, repo_name)
                if ok:
                    fixes_applied.append(check)
                    llm_calls += 1
                else:
                    fixes_failed.append(check)

            elif check == "no_placeholder_urls":
                # Flag-only: no auto-fix
                fixes_failed.append(check)

            elif check == "has_banner_image":
                # Flag-only: no auto-fix
                fixes_failed.append(check)

            else:
                fixes_failed.append(check)

        return fixes_applied, fixes_failed

    def _fix_remove_build_artifacts(self, org: str, repo_name: str) -> bool:
        """Remove build artifact files from the repo."""
        result = self._gh_api(f"repos/{org}/{repo_name}/contents")
        if result.returncode != 0:
            return False

        try:
            contents = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False

        all_ok = True
        for item in contents:
            if item.get("name") in BUILD_ARTIFACT_FILES:
                sha = item.get("sha", "")
                file_name = item["name"]
                delete_data = json.dumps({
                    "message": f"Remove build artifact: {file_name}",
                    "sha": sha,
                })
                del_result = self._gh_api(
                    f"repos/{org}/{repo_name}/contents/{file_name}",
                    method="DELETE",
                    data=delete_data,
                )
                if del_result.returncode != 0:
                    logger.warning(f"Failed to delete {file_name} from {repo_name}: {del_result.stderr[:200]}")
                    all_ok = False
                else:
                    logger.info(f"Deleted {file_name} from {repo_name}")

        return all_ok

    def _fix_add_license(self, org: str, repo_name: str) -> bool:
        """Add MIT LICENSE file to the repo."""
        content_b64 = base64.b64encode(MIT_LICENSE_TEXT.encode()).decode()
        put_data = json.dumps({
            "message": "Add MIT License",
            "content": content_b64,
        })
        result = self._gh_api(
            f"repos/{org}/{repo_name}/contents/LICENSE",
            method="PUT",
            data=put_data,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to add LICENSE to {repo_name}: {result.stderr[:200]}")
            return False
        logger.info(f"Added MIT LICENSE to {repo_name}")
        return True

    def _fix_generate_topics(self, org: str, repo_name: str) -> bool:
        """Generate and set topics via LLM with retry on bad format."""
        if not self.client:
            logger.warning("No LLM client — cannot generate topics")
            return False

        readme_content = self._fetch_readme(org, repo_name)
        readme_excerpt = (readme_content or "")[:2000]

        prompt = TOPICS_USER_PROMPT.format(readme_excerpt=readme_excerpt)
        messages = [
            {"role": "system", "content": TOPICS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        topics = None
        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.spec_llm_model,
                    messages=messages,
                    max_tokens=256,
                )
                raw = response.choices[0].message.content or "[]"
                match = re.search(r"\[.*\]", raw, re.DOTALL)
                if match:
                    topics = json.loads(match.group())
                    break
                if attempt == 0:
                    logger.info(f"Topics attempt 1 for {repo_name}: no JSON array, retrying with correction")
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content": 'That was not a valid JSON array. Output ONLY a JSON array like ["python", "cli", "automation"]. Nothing else.'})
                else:
                    logger.warning(f"LLM returned no JSON array for topics after retry: {raw[:100]}")
                    return False
            except Exception as e:
                logger.warning(f"LLM topics generation failed: {e}")
                return False

        if topics is None:
            return False

        # Enforce bounds: min 3, max 8
        topics = [t.lower().strip() for t in topics if isinstance(t, str) and t.strip()]
        if len(topics) < 3:
            logger.warning(f"LLM generated too few topics ({len(topics)})")
            return False
        topics = topics[:8]

        # PUT topics
        put_data = json.dumps({"names": topics})
        result = self._gh_api(
            f"repos/{org}/{repo_name}/topics",
            method="PUT",
            data=put_data,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to set topics for {repo_name}: {result.stderr[:200]}")
            return False

        logger.info(f"Set {len(topics)} topics for {repo_name}: {topics}")
        return True

    def _fix_generate_description(self, org: str, repo_name: str) -> bool:
        """Generate and set repo description via LLM with retry on bad format."""
        if not self.client:
            logger.warning("No LLM client — cannot generate description")
            return False

        readme_content = self._fetch_readme(org, repo_name)
        readme_excerpt = (readme_content or "")[:2000]

        prompt = DESCRIPTION_USER_PROMPT.format(
            repo_name=repo_name,
            readme_excerpt=readme_excerpt,
        )
        messages = [
            {"role": "system", "content": DESCRIPTION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        description = None
        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.spec_llm_model,
                    messages=messages,
                    max_tokens=256,
                )
                raw = (response.choices[0].message.content or "").strip().strip('"').strip("'")
                # Check if it looks like thinking text (starts with common CoT patterns)
                if raw and len(raw) <= 200 and not raw.lower().startswith(("okay", "we need", "the user", "let me", "i need")):
                    description = raw
                    break
                # Extract last sentence as fallback — often the actual description is at the end
                sentences = [s.strip() for s in raw.split(".") if len(s.strip()) > 20]
                if sentences and len(sentences[-1]) <= 200:
                    candidate = sentences[-1].strip().rstrip(".")
                    if len(candidate) >= 30:
                        description = candidate
                        break
                if attempt == 0:
                    logger.info(f"Description attempt 1 for {repo_name}: got thinking text, retrying")
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content": "That included extra text. Output ONLY one sentence (max 200 chars) describing this repo's value. No preamble, no thinking, just the sentence."})
                else:
                    logger.warning(f"LLM description failed after retry: {raw[:100]}")
                    return False
            except Exception as e:
                logger.warning(f"LLM description generation failed: {e}")
                return False

        if description is None:
            return False

        # Enforce bounds: min 30, max 200 chars
        if len(description) < 30:
            logger.warning(f"LLM generated too short a description ({len(description)} chars)")
            return False
        description = description[:200]

        # PATCH repo description
        patch_data = json.dumps({"description": description})
        result = self._gh_api(
            f"repos/{org}/{repo_name}",
            method="PATCH",
            data=patch_data,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to set description for {repo_name}: {result.stderr[:200]}")
            return False

        logger.info(f"Set description for {repo_name}: {description[:50]}...")
        return True
