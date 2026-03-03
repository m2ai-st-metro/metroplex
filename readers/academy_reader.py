"""
Academy Promotion Reader
Reads persona promotion requests from the Academy promotions JSONL file.

Academy promotions bypass triage (they are pre-validated by graduation gates)
and enqueue directly into the Metroplex priority queue with academy_weight applied.

Each line in the promotions JSONL file is a JSON object with:
- promotion_id (str): Unique ID for this promotion request
- persona_id (str): Academy persona ID (e.g. "code-reviewer")
- persona_name (str): Display name (e.g. "Code Reviewer")
- persona_role (str): Role description
- model (str): Target model (haiku/sonnet/opus)
- tool_groups (list[str]): Tool groups from agent_config
- prompt_file (str): Path to agent prompt file
- priority (str): critical/high/medium/low
- status (str): pending/dispatched/completed/failed
- promoted_at (str): ISO timestamp
- promotion_reason (str): Why this persona is being promoted
"""
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Map promotion priority labels to numeric base scores (0-100 scale).
# Academy promotions are high-value (pre-validated), so scores are elevated.
PRIORITY_SCORE_MAP = {
    "critical": 95.0,
    "high": 90.0,
    "medium": 80.0,
    "low": 65.0,
}


class AcademyReader:
    """Reader for Academy persona promotion requests from JSONL file."""

    def __init__(self, promotions_path: str, academy_dir: str = ""):
        """
        Initialize Academy reader.

        Args:
            promotions_path: Path to promotions.jsonl file
            academy_dir: Path to Academy project root (for persona YAML resolution)
        """
        self.promotions_path = Path(promotions_path)
        self.academy_dir = Path(academy_dir) if academy_dir else None

    def get_pending_promotions(self) -> list[dict]:
        """
        Get all pending promotion requests.

        Returns promotions where status = 'pending'.
        Reads the JSONL file line by line.

        Returns:
            List of promotion dictionaries
        """
        if not self.promotions_path.exists():
            return []

        results = []
        try:
            with open(self.promotions_path, "r") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("status") == "pending":
                            record["_line_num"] = line_num
                            results.append(record)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "Skipping invalid JSON at line %d in %s: %s",
                            line_num, self.promotions_path, e
                        )
        except Exception as e:
            logger.error("Failed to read promotions file %s: %s", self.promotions_path, e)

        # Sort by priority (critical first)
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        results.sort(key=lambda r: priority_order.get(r.get("priority", "medium"), 4))

        return results

    def priority_to_score(self, priority: str) -> float:
        """
        Convert a priority label to a numeric score.

        Args:
            priority: Priority string (critical/high/medium/low)

        Returns:
            Numeric score on 0-100 scale
        """
        return PRIORITY_SCORE_MAP.get(priority, 70.0)

    def promotion_to_idea(self, promo: dict) -> dict:
        """
        Convert a promotion dict into an idea dict suitable for spec generation.

        The idea_data stored in the priority queue must contain the fields
        expected by SpecGenerator (id, title, description, problem_statement,
        target_audience, artifact_type) plus Academy-specific metadata.

        Args:
            promo: Promotion dictionary from get_pending_promotions()

        Returns:
            Idea dictionary compatible with SpecGenerator
        """
        persona_id = promo.get("persona_id", "unknown")
        persona_name = promo.get("persona_name", persona_id)
        persona_role = promo.get("persona_role", "Agent")
        model = promo.get("model", "sonnet")
        tool_groups = promo.get("tool_groups", ["file_readonly"])
        prompt_file = promo.get("prompt_file", f"{persona_id}_agent_prompt.md")
        promotion_reason = promo.get("promotion_reason", "Graduation gates passed")

        description = (
            f"Build an autonomous Claude Agent SDK agent for the {persona_name} persona. "
            f"Role: {persona_role}. Model: {model}. "
            f"Tool groups: {', '.join(tool_groups)}. "
            f"The agent loads its system prompt from the persona YAML and resolves tools "
            f"from the Academy tool group catalog."
        )

        return {
            "id": promo.get("promotion_id", f"promo-{persona_id}"),
            "title": f"{persona_name} - Tier 1 Agent Build",
            "description": description,
            "problem_statement": (
                f"Promote {persona_name} from persona mode to autonomous agent mode. "
                f"Reason: {promotion_reason}"
            ),
            "target_audience": "ST Metro ecosystem",
            "artifact_type": "agent",
            "_source": "academy",
            "_persona_id": persona_id,
            "_model": model,
            "_tool_groups": tool_groups,
            "_prompt_file": prompt_file,
            "_promotion_reason": promotion_reason,
        }

    def mark_dispatched(self, promotion_id: str) -> None:
        """
        Mark a promotion as dispatched by rewriting the JSONL file.

        Updates the status of the matching record from 'pending' to 'dispatched'.

        Args:
            promotion_id: The promotion_id to update
        """
        if not self.promotions_path.exists():
            return

        lines = []
        modified = False

        try:
            with open(self.promotions_path, "r") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        lines.append(line)
                        continue
                    try:
                        record = json.loads(stripped)
                        if record.get("promotion_id") == promotion_id and record.get("status") == "pending":
                            record["status"] = "dispatched"
                            modified = True
                        lines.append(json.dumps(record) + "\n")
                    except json.JSONDecodeError:
                        lines.append(line)

            if modified:
                with open(self.promotions_path, "w") as f:
                    f.writelines(lines)
                logger.info("Marked promotion %s as dispatched", promotion_id)

        except Exception as e:
            logger.error("Failed to mark promotion %s as dispatched: %s", promotion_id, e)
