"""
Linear Issue Reader
Pulls issues from Linear via the Arcade Python SDK (arcadepy).
Filters by team, label, and workflow state, then converts to priority queue items.

Uses the direct arcadepy SDK (Method A from external_interfaces.md) --
no MCP server or Claude session required. Proven pattern from yce-harness.
"""
import os
import json
from typing import Optional, Any

try:
    from arcadepy import Arcade
except ImportError:
    Arcade = None  # Allows import without arcadepy installed (tests mock it)


# Linear priority integer -> base score (0-100 scale).
# These get multiplied by config.linear_weight (default 2.0x) when enqueued.
LINEAR_PRIORITY_MAP = {
    1: 100,   # urgent
    2: 75,    # high
    3: 50,    # medium
    4: 25,    # low
    0: 50,    # none (treat as medium)
}


class LinearReader:
    """Reader for Linear issues via Arcade SDK."""

    def __init__(
        self,
        arcade_api_key: str = "",
        arcade_user_id: str = "agent@local",
        team: str = "",
        label_filter: str = "metroplex",
        poll_states: str = "Backlog,Todo",
    ):
        """
        Initialize Linear reader.

        Args:
            arcade_api_key: Arcade API key (starts with 'arc_')
            arcade_user_id: Arcade user ID for OAuth context
            team: Linear team name/key to filter issues
            label_filter: Label name to filter issues (default: 'metroplex')
            poll_states: Comma-separated workflow state names to poll

        Raises:
            ValueError: If arcade_api_key is empty or arcadepy not installed
        """
        if Arcade is None:
            raise ValueError(
                "arcadepy package not installed. Run: pip install arcadepy"
            )

        self.arcade_api_key = arcade_api_key or os.environ.get("ARCADE_API_KEY", "")
        self.arcade_user_id = arcade_user_id or os.environ.get("ARCADE_USER_ID", "agent@local")
        self.team = team
        self.label_filter = label_filter
        self.poll_states = [s.strip() for s in poll_states.split(",") if s.strip()]

        if not self.arcade_api_key:
            raise ValueError(
                "ARCADE_API_KEY not set. Cannot read Linear issues.\n"
                "Set it in ~/.env.shared or pass arcade_api_key parameter."
            )

        self._client: Optional[Any] = None

    def _get_client(self):
        """Lazy-create Arcade client."""
        if self._client is None:
            self._client = Arcade(api_key=self.arcade_api_key)
        return self._client

    def close(self):
        """Release client resources."""
        self._client = None

    def get_issues(self, limit: int = 50) -> list[dict]:
        """
        Get Linear issues matching the configured filters.

        Polls each configured state separately to build a combined list.
        Applies label filter if set. Handles pagination for large result sets.

        Args:
            limit: Max issues to return across all states

        Returns:
            List of normalized issue dicts with fields:
            - identifier (str): e.g. 'M2A-30'
            - title (str)
            - description (str)
            - priority (int): 0-4
            - priority_label (str): e.g. 'High'
            - state_name (str): e.g. 'Todo'
            - state_type (str): e.g. 'unstarted'
            - labels (list[str])
            - assignee (str|None)
            - created_at (str)
            - url (str|None)
        """
        client = self._get_client()
        all_issues = []

        for state in self.poll_states:
            if len(all_issues) >= limit:
                break

            params = {"state": state, "limit": min(50, limit - len(all_issues))}
            if self.team:
                params["team"] = self.team
            if self.label_filter:
                params["label"] = self.label_filter

            try:
                result = client.tools.execute(
                    tool_name="Linear_ListIssues",
                    input=params,
                    user_id=self.arcade_user_id,
                )
                raw = result.output.value
                if isinstance(raw, dict):
                    issues = raw.get("issues", [])
                elif isinstance(raw, list):
                    issues = raw
                else:
                    issues = []

                for issue in issues:
                    normalized = self._normalize_issue(issue)
                    if normalized:
                        all_issues.append(normalized)

            except Exception as e:
                print(f"  [linear] Warning: Failed to fetch state={state}: {e}")
                continue

        return all_issues[:limit]

    def get_issue_detail(self, issue_identifier: str) -> dict | None:
        """
        Get full detail for a single issue.

        Args:
            issue_identifier: Linear issue ID (e.g. 'M2A-30' or UUID)

        Returns:
            Normalized issue dict or None if not found
        """
        client = self._get_client()

        try:
            result = client.tools.execute(
                tool_name="Linear_GetIssue",
                input={
                    "issue_id": issue_identifier,
                    "include_comments": False,
                    "include_children": False,
                    "include_relations": False,
                    "include_attachments": False,
                },
                user_id=self.arcade_user_id,
            )
            raw = result.output.value
            issue = raw.get("issue", raw) if isinstance(raw, dict) else {}
            return self._normalize_issue(issue)
        except Exception as e:
            print(f"  [linear] Warning: Could not fetch {issue_identifier}: {e}")
            return None

    def _normalize_issue(self, issue: dict) -> dict | None:
        """
        Normalize a raw Linear issue into a consistent dict.

        Args:
            issue: Raw issue dict from Arcade SDK

        Returns:
            Normalized dict or None if issue is empty/invalid
        """
        if not issue or not issue.get("identifier"):
            return None

        state = issue.get("state", {}) or {}
        assignee = issue.get("assignee", {}) or {}
        labels = issue.get("labels", []) or []

        return {
            "identifier": issue["identifier"],
            "title": issue.get("title", ""),
            "description": issue.get("description", ""),
            "priority": issue.get("priority", 0),
            "priority_label": issue.get("priorityLabel", "None"),
            "state_name": state.get("name", "Unknown"),
            "state_type": state.get("type", "unknown"),
            "labels": [l.get("name", "") for l in labels if isinstance(l, dict)],
            "assignee": assignee.get("name") if assignee else None,
            "created_at": issue.get("createdAt", ""),
            "url": issue.get("url"),
        }

    def issue_to_idea(self, issue: dict) -> dict:
        """
        Convert a normalized issue dict into an idea dict for SpecGenerator.

        Args:
            issue: Normalized issue dict from get_issues()

        Returns:
            Idea dictionary compatible with priority queue enqueue
        """
        description = issue.get("description") or issue["title"]

        return {
            "id": issue["identifier"],
            "title": issue["title"],
            "description": description,
            "problem_statement": description,
            "target_audience": "M2AI engineering",
            "artifact_type": "tool",
            "weighted_score": self.priority_to_score(issue.get("priority", 0)) / 10.0,
            "_source": "linear",
            "_linear_identifier": issue["identifier"],
            "_linear_state": issue.get("state_name", ""),
            "_linear_priority": issue.get("priority_label", ""),
        }

    @staticmethod
    def priority_to_score(priority: int) -> float:
        """
        Convert Linear priority integer to numeric score.

        Args:
            priority: Linear priority (0=none, 1=urgent, 2=high, 3=medium, 4=low)

        Returns:
            Numeric score on 0-100 scale
        """
        return float(LINEAR_PRIORITY_MAP.get(priority, 50))
