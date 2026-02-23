"""
Patch Gate - Gate 3
Reads proposed persona patches from ST Factory, applies YAML modifications to Academy repo.
"""
import re
import subprocess
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any

from config import Config
from models import PatchApplication
from db import StateDB
from audit import AuditLogger
from readers.stfactory_reader import STFactoryReader


class PatchGate:
    """Gate 3: Patch Auto-Apply - Git Operations & YAML Patches."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        stfactory_reader: STFactoryReader,
        audit_logger: AuditLogger
    ):
        """
        Initialize Patch Gate.

        Args:
            config: Metroplex configuration
            state_db: State database manager
            stfactory_reader: ST Factory database reader
            audit_logger: Audit logger
        """
        self.config = config
        self.state_db = state_db
        self.stfactory_reader = stfactory_reader
        self.audit_logger = audit_logger

    def run(self, dry_run: bool = False) -> list[PatchApplication]:
        """
        Run patch gate on proposed patches.

        Process flow:
        1. Get proposed patches from ST Factory
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
              - Update patch status in ST Factory
        4. Record PatchApplication in state_db and audit_logger
        5. Continue to next patch on git failures

        Args:
            dry_run: If True, print changes but don't apply

        Returns:
            List of PatchApplication objects
        """
        # Get proposed patches
        if self.stfactory_reader is None:
            print("Warning: ST Factory reader not initialized (DB not found)")
            return []

        patches = self.stfactory_reader.get_proposed_patches()

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
            raw_json = patch.get("raw_json", {})
            operations = raw_json.get("operations", [])

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
                continue

            # Determine target file path
            target_file = f"personas/{persona_id}.yaml"

            if dry_run:
                # Dry run: print what would change
                print(f"[DRY RUN] Would apply patch {patch_id} to {persona_id}")
                print(f"  Target file: {target_file}")
                print(f"  Operations: {len(operations)}")
                for op in operations:
                    print(f"    - {op.get('op')} {op.get('path')}: {op.get('value', 'N/A')}")

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

                # Update patch status in ST Factory (if applied)
                if status == "applied":
                    try:
                        self.stfactory_reader.update_patch_status(patch_id, "applied")
                    except Exception as e:
                        # Log error but don't fail the patch application
                        self.audit_logger.log_error(
                            gate="patch",
                            error=f"Failed to update patch status in ST Factory: {str(e)}",
                            details={"patch_id": patch_id}
                        )

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
            op = operation.get("op")
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
