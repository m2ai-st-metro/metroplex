"""
Patch Gate - Gate 3
Reads proposed persona patches from ST Records, applies YAML modifications to Academy repo.
Also applies approved agent patches to st-agent-registry and syncs to runtime.
"""
import hashlib
import re
import subprocess
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any

from config import Config
from models import PatchApplication, AgentPatchApplication
from db import StateDB
from audit import AuditLogger
from readers.st_records_reader import STRecordsReader


class PatchGate:
    """Gate 3: Patch Auto-Apply - Git Operations & YAML Patches."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        st_records_reader: STRecordsReader,
        audit_logger: AuditLogger
    ):
        """
        Initialize Patch Gate.

        Args:
            config: Metroplex configuration
            state_db: State database manager
            st_records_reader: ST Records database reader
            audit_logger: Audit logger
        """
        self.config = config
        self.state_db = state_db
        self.st_records_reader = st_records_reader
        self.audit_logger = audit_logger

    def run(self, dry_run: bool = False) -> list[PatchApplication]:
        """
        Run patch gate on proposed patches.

        Process flow:
        1. Get proposed patches from ST Records
        2. Enforce per-cycle cap (max_patches_per_cycle)
        3. For each patch:
           a. Parse operations from raw_json
           b. Determine target file: personas/{persona_id}.yaml
           c. If dry_run: print what would change
           d. If not dry_run:
              - Ensure local repo clone exists (git clone/pull)
              - Load persona YAML file
              - Apply patch operations
              - Write modified YAML back
              - Git add, commit, push
              - Update patch status in ST Records
        4. Record PatchApplication in state_db and audit_logger
        5. Continue to next patch on git failures

        Args:
            dry_run: If True, print changes but don't apply

        Returns:
            List of PatchApplication objects
        """
        # Get proposed patches
        if self.st_records_reader is None:
            print("Warning: ST Records reader not initialized (DB not found)")
            return []

        patches = self.st_records_reader.get_proposed_patches()

        # Enforce per-cycle cap
        max_patches = self.config.max_patches_per_cycle
        if len(patches) > max_patches:
            patches = patches[:max_patches]

        results = []

        # Process each patch
        for patch in patches:
            patch_id = patch["patch_id"]
            persona_id = patch["persona_id"]
            from_version = patch.get("from_version")
            to_version = patch.get("to_version")

            # Validate persona_id to prevent path traversal
            if not self._is_safe_persona_id(persona_id):
                patch_app = PatchApplication(
                    patch_id=patch_id,
                    persona_id=persona_id,
                    from_version=from_version,
                    to_version=to_version,
                    status="failed",
                    reason="invalid persona_id (path traversal risk)",
                    applied_at=datetime.now()
                )
                results.append(patch_app)

                if not dry_run:
                    self.state_db.record_patch_application(patch_app)
                    self.audit_logger.log_decision(
                        gate="patch",
                        action="failed",
                        details={
                            "patch_id": patch_id,
                            "persona_id": persona_id,
                            "reason": "invalid persona_id (path traversal risk)"
                        }
                    )
                continue

            # Validate patch_id to prevent path traversal
            if not self._is_safe_patch_id(patch_id):
                patch_app = PatchApplication(
                    patch_id=patch_id,
                    persona_id=persona_id,
                    from_version=from_version,
                    to_version=to_version,
                    status="failed",
                    reason="invalid patch_id (unsafe characters)",
                    applied_at=datetime.now()
                )
                results.append(patch_app)

                if not dry_run:
                    self.state_db.record_patch_application(patch_app)
                    self.audit_logger.log_decision(
                        gate="patch",
                        action="failed",
                        details={
                            "patch_id": patch_id,
                            "persona_id": persona_id,
                            "reason": "invalid patch_id (unsafe characters)"
                        }
                    )
                continue

            # Parse operations from raw_json
            # ST Records patches use "patches" key with "operation" field per entry
            raw_json = patch.get("raw_json", {})
            operations = raw_json.get("patches", []) or raw_json.get("operations", [])

            if not operations:
                # No operations to apply
                patch_app = PatchApplication(
                    patch_id=patch_id,
                    persona_id=persona_id,
                    from_version=from_version,
                    to_version=to_version,
                    status="skipped",
                    reason="no operations in patch",
                    applied_at=datetime.now()
                )
                results.append(patch_app)

                if not dry_run:
                    self.state_db.record_patch_application(patch_app)
                    self.audit_logger.log_decision(
                        gate="patch",
                        action="skipped",
                        details={
                            "patch_id": patch_id,
                            "persona_id": persona_id,
                            "reason": "no operations in patch"
                        }
                    )
                    # Sync status back to ST Records so it stops re-proposing
                    try:
                        self.st_records_reader.update_patch_status(patch_id, "skipped")
                    except Exception as e:
                        self.audit_logger.log_error(
                            gate="patch",
                            error=f"Failed to update skipped patch status in ST Records: {str(e)}",
                            details={"patch_id": patch_id}
                        )
                continue

            # Determine target file path
            target_file = f"personas/{persona_id}/persona.yaml"

            if dry_run:
                # Dry run: print what would change
                print(f"[DRY RUN] Would apply patch {patch_id} to {persona_id}")
                print(f"  Target file: {target_file}")
                print(f"  Operations: {len(operations)}")
                for op in operations:
                    op_name = op.get('operation') or op.get('op')
                    print(f"    - {op_name} {op.get('path')}: {op.get('value', 'N/A')}")

                patch_app = PatchApplication(
                    patch_id=patch_id,
                    persona_id=persona_id,
                    from_version=from_version,
                    to_version=to_version,
                    status="applied",
                    reason="dry run",
                    applied_at=datetime.now()
                )
                results.append(patch_app)
            else:
                # Actually apply the patch
                status, reason = self._apply_patch(patch_id, persona_id, target_file, operations)

                patch_app = PatchApplication(
                    patch_id=patch_id,
                    persona_id=persona_id,
                    from_version=from_version,
                    to_version=to_version,
                    status=status,
                    reason=reason,
                    applied_at=datetime.now()
                )
                results.append(patch_app)

                # Record in state DB
                self.state_db.record_patch_application(patch_app)

                # Log in audit log
                self.audit_logger.log_decision(
                    gate="patch",
                    action=status,
                    details={
                        "patch_id": patch_id,
                        "persona_id": persona_id,
                        "reason": reason
                    }
                )

                # Update patch status in ST Records (if applied)
                if status == "applied":
                    try:
                        self.st_records_reader.update_patch_status(patch_id, "applied")
                    except Exception as e:
                        # Log error but don't fail the patch application
                        self.audit_logger.log_error(
                            gate="patch",
                            error=f"Failed to update patch status in ST Records: {str(e)}",
                            details={"patch_id": patch_id}
                        )

        # --- Agent Patches ---
        agent_patch_results = self._run_agent_patches(dry_run=dry_run)
        for apr in agent_patch_results:
            results.append(PatchApplication(
                patch_id=apr.patch_id,
                persona_id=f"agent:{apr.agent_id}",
                status=apr.status,
                reason=apr.reason,
                applied_at=apr.applied_at,
            ))

        return results

    def _apply_patch(self, patch_id: str, persona_id: str, target_file: str, operations: list[dict]) -> tuple[str, str]:
        """
        Apply a patch to the Academy repo.

        Args:
            patch_id: Patch ID
            persona_id: Persona ID
            target_file: Target file path (relative to repo root)
            operations: List of patch operations

        Returns:
            Tuple of (status, reason)
        """
        # Determine work directory
        work_dir = Path(self.config.yce_dir) / "tmp" / "academy"

        try:
            # Ensure repo exists and is up to date
            if not self._ensure_repo(work_dir):
                return ("failed", "failed to clone/pull repository")

            # Load YAML file
            yaml_file_path = work_dir / target_file
            if not yaml_file_path.exists():
                return ("failed", f"target file {target_file} not found")

            with open(yaml_file_path, "r") as f:
                yaml_data = yaml.safe_load(f) or {}

            # Apply operations
            try:
                modified_data = self._apply_yaml_patch(yaml_data, operations)
            except Exception as e:
                return ("failed", f"failed to apply YAML patch: {str(e)}")

            # Write modified YAML back
            with open(yaml_file_path, "w") as f:
                yaml.dump(modified_data, f, default_flow_style=False, sort_keys=False)

            # Git add
            add_result = subprocess.run(
                ["git", "-C", str(work_dir), "add", "."],
                capture_output=True,
                text=True,
                timeout=30
            )
            if add_result.returncode != 0:
                return ("failed", f"git add failed: {add_result.stderr}")

            # Git commit
            commit_msg = f"metroplex: apply patch {patch_id} to {persona_id}"
            commit_result = subprocess.run(
                ["git", "-C", str(work_dir), "commit", "-m", commit_msg],
                capture_output=True,
                text=True,
                timeout=30
            )
            if commit_result.returncode != 0:
                # Check if it's because there's nothing to commit
                if "nothing to commit" in commit_result.stdout or "nothing to commit" in commit_result.stderr:
                    return ("skipped", "no changes to commit")
                return ("failed", f"git commit failed: {commit_result.stderr}")

            # Git push
            push_result = subprocess.run(
                ["git", "-C", str(work_dir), "push"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if push_result.returncode != 0:
                return ("failed", f"git push failed: {push_result.stderr}")

            return ("applied", "patch applied successfully")

        except Exception as e:
            return ("failed", f"unexpected error: {str(e)}")

    def _apply_yaml_patch(self, yaml_data: dict, operations: list[dict]) -> dict:
        """
        Apply JSON Pointer style patch operations to YAML data.

        Each operation has:
        - op: "add" | "replace" | "remove"
        - path: JSON Pointer string (e.g., "/voice/tone")
        - value: New value (for add/replace)

        Args:
            yaml_data: Original YAML data as dict
            operations: List of patch operations

        Returns:
            Modified YAML data as dict
        """
        # Deep copy to avoid modifying original
        import copy
        result = copy.deepcopy(yaml_data)

        for operation in operations:
            # Support both ST Records format ("operation") and JSON Patch format ("op")
            op = operation.get("operation") or operation.get("op")
            path = operation.get("path", "")
            value = operation.get("value")

            # Parse JSON Pointer path (skip leading "/")
            path_parts = [p for p in path.split("/") if p]

            if not path_parts:
                # Root-level operation (rarely used)
                if op == "replace" and value is not None:
                    result = value
                continue

            # Navigate to parent of target
            current = result
            for i, part in enumerate(path_parts[:-1]):
                if part not in current:
                    if op == "add" or op == "replace":
                        # Create intermediate dictionaries
                        current[part] = {}
                    else:
                        # Can't navigate, skip this operation
                        break
                current = current[part]
            else:
                # We successfully navigated to parent
                final_key = path_parts[-1]

                if op == "add" or op == "replace":
                    current[final_key] = value
                elif op == "remove":
                    if final_key in current:
                        del current[final_key]

        return result

    def _ensure_repo(self, work_dir: Path) -> bool:
        """
        Ensure Academy repo exists and is up to date.

        If work_dir exists and has .git: git pull
        Else: git clone

        Args:
            work_dir: Local directory for repo

        Returns:
            True if successful, False otherwise
        """
        repo_url = f"https://github.com/{self.config.academy_repo}.git"

        try:
            if work_dir.exists() and (work_dir / ".git").exists():
                # Repo exists, pull latest
                result = subprocess.run(
                    ["git", "-C", str(work_dir), "pull"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                return result.returncode == 0
            else:
                # Clone repo
                work_dir.parent.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    ["git", "clone", repo_url, str(work_dir)],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                return result.returncode == 0
        except Exception:
            return False

    # --- Agent Patch Methods ---

    def _run_agent_patches(self, dry_run: bool = False) -> list[AgentPatchApplication]:
        """
        Process approved agent patches from ST Records.

        Reads approved patches from agent_patches table, applies section-level
        changes to CLAUDE.md or agent.yaml in st-agent-registry, commits/pushes,
        and syncs patched files to ~/.claudeclaw/agents/{id}/.
        """
        if self.st_records_reader is None:
            return []

        agent_patches = self.st_records_reader.get_approved_agent_patches()
        if not agent_patches:
            return []

        results = []

        for patch in agent_patches:
            patch_id = patch["patch_id"]
            agent_id = patch["agent_id"]
            target = patch["target"]  # "claude_md" | "agent_yaml"
            section = patch["section"]
            operation = patch["operation"]
            value = patch.get("value")
            rationale = patch.get("rationale", "")

            # Validate agent_id (reuse persona safety check)
            if not self._is_safe_persona_id(agent_id):
                app = AgentPatchApplication(
                    patch_id=patch_id, agent_id=agent_id, target=target,
                    section=section, operation=operation,
                    status="failed", reason="invalid agent_id (path traversal risk)",
                    applied_at=datetime.now(),
                )
                results.append(app)
                if not dry_run:
                    self.state_db.record_agent_patch_application(app)
                    self.audit_logger.log_decision(gate="patch", action="failed", details={
                        "patch_id": patch_id, "agent_id": agent_id,
                        "reason": "invalid agent_id",
                    })
                continue

            if not self._is_safe_patch_id(patch_id):
                app = AgentPatchApplication(
                    patch_id=patch_id, agent_id=agent_id, target=target,
                    section=section, operation=operation,
                    status="failed", reason="invalid patch_id (unsafe characters)",
                    applied_at=datetime.now(),
                )
                results.append(app)
                if not dry_run:
                    self.state_db.record_agent_patch_application(app)
                    self.audit_logger.log_decision(gate="patch", action="failed", details={
                        "patch_id": patch_id, "agent_id": agent_id,
                        "reason": "invalid patch_id",
                    })
                continue

            if dry_run:
                print(f"[DRY RUN] Would apply agent patch {patch_id} to {agent_id}")
                print(f"  Target: {target}, Section: {section}, Op: {operation}")
                if value:
                    preview = value[:120] + "..." if len(value) > 120 else value
                    print(f"  Value: {preview}")
                app = AgentPatchApplication(
                    patch_id=patch_id, agent_id=agent_id, target=target,
                    section=section, operation=operation,
                    status="applied", reason="dry run",
                    applied_at=datetime.now(),
                )
                results.append(app)
                continue

            # Apply the patch
            status, reason = self._apply_agent_patch(
                patch_id, agent_id, target, section, operation, value, rationale
            )

            app = AgentPatchApplication(
                patch_id=patch_id, agent_id=agent_id, target=target,
                section=section, operation=operation,
                status=status, reason=reason,
                applied_at=datetime.now(),
            )
            results.append(app)

            self.state_db.record_agent_patch_application(app)
            self.audit_logger.log_decision(gate="patch", action=status, details={
                "patch_id": patch_id, "agent_id": agent_id,
                "target": target, "section": section, "reason": reason,
            })

            if status == "applied":
                try:
                    self.st_records_reader.update_agent_patch_status(patch_id, "applied")
                except Exception as e:
                    self.audit_logger.log_error(
                        gate="patch",
                        error=f"Failed to update agent patch status in ST Records: {str(e)}",
                        details={"patch_id": patch_id},
                    )

        return results

    def _apply_agent_patch(
        self,
        patch_id: str,
        agent_id: str,
        target: str,
        section: str,
        operation: str,
        value: str | None,
        rationale: str,
    ) -> tuple[str, str]:
        """
        Apply an agent patch to the st-agent-registry repo and sync to runtime.

        Steps:
        1. Ensure registry repo is cloned/pulled
        2. Read target file (CLAUDE.md or agent.yaml)
        3. Apply section-level patch
        4. Write back, commit, push
        5. Sync to ~/.claudeclaw/agents/{agent_id}/
        6. Update registry.yaml (sync_hash, last_synced_at)

        Returns:
            Tuple of (status, reason)
        """
        registry_dir = Path(self.config.academy_dir)

        try:
            # Ensure repo is up to date
            if not self._ensure_registry_repo(registry_dir):
                return ("failed", "failed to clone/pull registry repository")

            # Determine target file
            agent_dir = registry_dir / "agents" / agent_id
            if not agent_dir.exists():
                return ("failed", f"agent directory agents/{agent_id}/ not found in registry")

            if target == "claude_md":
                target_file = agent_dir / "CLAUDE.md"
            elif target == "agent_yaml":
                target_file = agent_dir / "agent.yaml"
            else:
                return ("failed", f"unknown target type: {target}")

            if not target_file.exists():
                return ("failed", f"{target_file.name} not found for agent {agent_id}")

            # Read current content
            original_content = target_file.read_text(encoding="utf-8")

            # Apply patch based on target type
            if target == "claude_md":
                new_content = self._apply_markdown_section_patch(
                    original_content, section, operation, value
                )
            else:
                # agent.yaml: top-level key patch
                new_content = self._apply_yaml_key_patch(
                    original_content, section, operation, value
                )

            if new_content == original_content:
                return ("skipped", "no changes after applying patch")

            # Write modified content
            target_file.write_text(new_content, encoding="utf-8")

            # Git add + commit + push
            rel_path = str(target_file.relative_to(registry_dir))
            git_ok, git_msg = self._git_commit_push(
                registry_dir,
                [rel_path],
                f"metroplex: apply agent patch {patch_id} to {agent_id}/{target_file.name}",
            )
            if not git_ok:
                return ("failed", git_msg)

            # Sync to runtime: ~/.claudeclaw/agents/{agent_id}/
            runtime_dir = Path.home() / ".claudeclaw" / "agents" / agent_id
            if runtime_dir.exists():
                runtime_file = runtime_dir / target_file.name
                runtime_file.write_text(new_content, encoding="utf-8")

            # Update registry.yaml
            registry_yaml_path = agent_dir / "registry.yaml"
            if registry_yaml_path.exists():
                self._update_registry_yaml(registry_yaml_path, new_content, registry_dir)

            return ("applied", "agent patch applied successfully")

        except Exception as e:
            return ("failed", f"unexpected error: {str(e)}")

    def _ensure_registry_repo(self, registry_dir: Path) -> bool:
        """Ensure st-agent-registry repo is cloned and up to date."""
        repo_url = f"https://github.com/{self.config.academy_repo}.git"

        try:
            if registry_dir.exists() and (registry_dir / ".git").exists():
                result = subprocess.run(
                    ["git", "-C", str(registry_dir), "pull"],
                    capture_output=True, text=True, timeout=30,
                )
                return result.returncode == 0
            else:
                registry_dir.parent.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    ["git", "clone", repo_url, str(registry_dir)],
                    capture_output=True, text=True, timeout=60,
                )
                return result.returncode == 0
        except Exception:
            return False

    def _apply_markdown_section_patch(
        self, content: str, section: str, operation: str, value: str | None
    ) -> str:
        """
        Apply a section-level patch to markdown content.

        Sections are identified by markdown headers (## Section Name).
        - add: insert new section at end (or append to existing section)
        - replace: replace the content of an existing section
        - remove: remove an entire section

        Args:
            content: Original markdown content
            section: Section header text (without ## prefix)
            operation: "add" | "replace" | "remove"
            value: New content for add/replace operations

        Returns:
            Modified markdown content
        """
        # Match section: starts with ## header, ends before next ## or EOF
        # Handles ##, ###, etc. by matching the exact header level
        pattern = re.compile(
            rf'^(#{{1,6}})\s+{re.escape(section)}\s*$',
            re.MULTILINE,
        )

        match = pattern.search(content)

        if operation == "add":
            if match:
                # Section exists — append value to it
                header_level = match.group(1)
                section_start = match.end()
                # Find the end of this section (next header of same or higher level, or EOF)
                next_section = re.search(
                    rf'^#{{{1},{len(header_level)}}}\s',
                    content[section_start:],
                    re.MULTILINE,
                )
                if next_section:
                    insert_pos = section_start + next_section.start()
                else:
                    insert_pos = len(content)

                return content[:insert_pos].rstrip() + "\n\n" + value.strip() + "\n\n" + content[insert_pos:]
            else:
                # Section doesn't exist — add at end
                return content.rstrip() + f"\n\n## {section}\n\n{value.strip()}\n"

        elif operation == "replace":
            if not match:
                return content  # Section not found, no change

            header_level = match.group(1)
            header_line_end = match.end()
            # Find end of section
            next_section = re.search(
                rf'^#{{{1},{len(header_level)}}}\s',
                content[header_line_end:],
                re.MULTILINE,
            )
            if next_section:
                section_end = header_line_end + next_section.start()
            else:
                section_end = len(content)

            # Reconstruct: header + new content + rest
            header_line = content[match.start():header_line_end]
            return (
                content[:match.start()]
                + header_line + "\n\n"
                + (value.strip() if value else "") + "\n\n"
                + content[section_end:].lstrip("\n")
            )

        elif operation == "remove":
            if not match:
                return content  # Nothing to remove

            header_level = match.group(1)
            header_line_end = match.end()
            next_section = re.search(
                rf'^#{{{1},{len(header_level)}}}\s',
                content[header_line_end:],
                re.MULTILINE,
            )
            if next_section:
                section_end = header_line_end + next_section.start()
            else:
                section_end = len(content)

            return (content[:match.start()] + content[section_end:]).strip() + "\n"

        return content

    def _apply_yaml_key_patch(
        self, content: str, key: str, operation: str, value: str | None
    ) -> str:
        """Apply a top-level key patch to agent.yaml content."""
        data = yaml.safe_load(content) or {}

        if operation == "add" or operation == "replace":
            # Try to parse value as YAML for structured data
            try:
                parsed = yaml.safe_load(value)
            except Exception:
                parsed = value
            data[key] = parsed
        elif operation == "remove":
            data.pop(key, None)

        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def _git_commit_push(
        self, repo_dir: Path, files: list[str], commit_msg: str
    ) -> tuple[bool, str]:
        """Git add, commit, and push in the given repo directory."""
        try:
            # Git add specific files
            add_result = subprocess.run(
                ["git", "-C", str(repo_dir), "add"] + files,
                capture_output=True, text=True, timeout=30,
            )
            if add_result.returncode != 0:
                return (False, f"git add failed: {add_result.stderr}")

            # Git commit
            commit_result = subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", commit_msg],
                capture_output=True, text=True, timeout=30,
            )
            if commit_result.returncode != 0:
                if "nothing to commit" in (commit_result.stdout + commit_result.stderr):
                    return (True, "no changes to commit")
                return (False, f"git commit failed: {commit_result.stderr}")

            # Git push
            push_result = subprocess.run(
                ["git", "-C", str(repo_dir), "push"],
                capture_output=True, text=True, timeout=30,
            )
            if push_result.returncode != 0:
                return (False, f"git push failed: {push_result.stderr}")

            return (True, "committed and pushed")
        except Exception as e:
            return (False, f"git error: {str(e)}")

    def _update_registry_yaml(
        self, registry_yaml_path: Path, content: str, repo_dir: Path
    ) -> None:
        """Update registry.yaml with new sync_hash and timestamp."""
        try:
            reg_data = yaml.safe_load(registry_yaml_path.read_text()) or {}
            reg_data["sync_hash"] = hashlib.sha256(content.encode()).hexdigest()
            reg_data["last_synced_at"] = datetime.now().strftime("%Y-%m-%d")
            learning = reg_data.get("learning", {})
            learning["total_patches_applied"] = learning.get("total_patches_applied", 0) + 1
            learning["last_patch_at"] = datetime.now().strftime("%Y-%m-%d")
            reg_data["learning"] = learning

            registry_yaml_path.write_text(
                yaml.dump(reg_data, default_flow_style=False, sort_keys=False)
            )

            # Commit the registry.yaml update too
            rel_path = str(registry_yaml_path.relative_to(repo_dir))
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", rel_path],
                capture_output=True, text=True, timeout=10,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m",
                 f"metroplex: update registry.yaml for {registry_yaml_path.parent.name}"],
                capture_output=True, text=True, timeout=10,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "push"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass  # Best-effort metadata update

    def _is_safe_persona_id(self, persona_id: str) -> bool:
        """
        Validate persona_id to prevent path traversal attacks.

        Args:
            persona_id: Persona ID to validate

        Returns:
            True if safe, False otherwise
        """
        if not persona_id:
            return False

        # Check for path traversal patterns
        if "/" in persona_id or "\\" in persona_id or ".." in persona_id:
            return False

        return True

    def _is_safe_patch_id(self, patch_id: str) -> bool:
        """
        Validate patch_id to ensure it contains only safe characters.

        Args:
            patch_id: Patch ID to validate

        Returns:
            True if safe (alphanumeric, hyphens, underscores), False otherwise
        """
        if not patch_id:
            return False

        # Allow alphanumeric, hyphens, and underscores only
        return bool(re.match(r'^[a-zA-Z0-9_-]+$', patch_id))
