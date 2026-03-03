"""
Tests for LinearReader -- Arcade SDK integration for Linear issues.
All tests mock at the Arcade client level (no real API calls).
"""
import pytest
import json
from unittest.mock import Mock, MagicMock, patch

from readers.linear_reader import LinearReader, LINEAR_PRIORITY_MAP


# --- Fixtures ---


@pytest.fixture
def mock_arcade_client():
    """Create a mock Arcade client with configurable tool execution."""
    client = Mock()
    return client


def _make_issue(
    identifier="M2A-30",
    title="Test Issue",
    description="A test issue",
    priority=2,
    priority_label="High",
    state_name="Todo",
    state_type="unstarted",
    labels=None,
    assignee_name=None,
):
    """Helper to build a raw Linear issue dict."""
    issue = {
        "identifier": identifier,
        "title": title,
        "description": description,
        "priority": priority,
        "priorityLabel": priority_label,
        "state": {"name": state_name, "type": state_type},
        "labels": labels or [],
        "assignee": {"name": assignee_name} if assignee_name else None,
        "createdAt": "2026-02-20T10:00:00Z",
        "url": f"https://linear.app/m2ai/issue/{identifier}",
    }
    return issue


def _make_execute_result(issues=None, issue=None):
    """Build a mock Arcade tools.execute() result."""
    result = Mock()
    if issues is not None:
        result.output.value = {"issues": issues}
    elif issue is not None:
        result.output.value = {"issue": issue}
    else:
        result.output.value = {"issues": []}
    return result


# --- Initialization Tests ---


def test_linear_reader_init_no_key():
    """LinearReader raises ValueError when no API key is provided."""
    with pytest.raises(ValueError, match="ARCADE_API_KEY"):
        LinearReader(arcade_api_key="", arcade_user_id="test")


@patch("readers.linear_reader.Arcade", None)
def test_linear_reader_init_no_arcadepy():
    """LinearReader raises ValueError when arcadepy is not installed."""
    with pytest.raises(ValueError, match="arcadepy"):
        LinearReader(arcade_api_key="arc_test", arcade_user_id="test")


def test_linear_reader_init_success():
    """LinearReader initializes with valid config."""
    reader = LinearReader(
        arcade_api_key="arc_test_key",
        arcade_user_id="agent@test",
        team="M2AI",
        label_filter="metroplex",
        poll_states="Backlog,Todo",
    )
    assert reader.team == "M2AI"
    assert reader.label_filter == "metroplex"
    assert reader.poll_states == ["Backlog", "Todo"]
    reader.close()


# --- get_issues Tests ---


@patch("readers.linear_reader.Arcade")
def test_get_issues_returns_normalized(MockArcade):
    """get_issues returns normalized issue dicts."""
    mock_client = Mock()
    MockArcade.return_value = mock_client

    raw_issues = [
        _make_issue("M2A-1", "Issue One", "Desc 1", priority=1, priority_label="Urgent",
                     state_name="Backlog", state_type="backlog",
                     labels=[{"name": "metroplex"}], assignee_name="Alice"),
        _make_issue("M2A-2", "Issue Two", "Desc 2", priority=3, priority_label="Medium",
                     state_name="Todo", state_type="unstarted"),
    ]
    mock_client.tools.execute.return_value = _make_execute_result(issues=raw_issues)

    reader = LinearReader(arcade_api_key="arc_test", team="M2AI", poll_states="Backlog")
    issues = reader.get_issues()

    assert len(issues) == 2
    assert issues[0]["identifier"] == "M2A-1"
    assert issues[0]["title"] == "Issue One"
    assert issues[0]["priority"] == 1
    assert issues[0]["priority_label"] == "Urgent"
    assert issues[0]["state_name"] == "Backlog"
    assert issues[0]["state_type"] == "backlog"
    assert issues[0]["labels"] == ["metroplex"]
    assert issues[0]["assignee"] == "Alice"
    assert issues[1]["assignee"] is None
    reader.close()


@patch("readers.linear_reader.Arcade")
def test_get_issues_polls_multiple_states(MockArcade):
    """get_issues queries each configured state."""
    mock_client = Mock()
    MockArcade.return_value = mock_client

    # Different issues per state
    call_count = 0
    def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        state = kwargs["input"]["state"]
        if state == "Backlog":
            return _make_execute_result(issues=[_make_issue("M2A-1", state_name="Backlog")])
        elif state == "Todo":
            return _make_execute_result(issues=[_make_issue("M2A-2", state_name="Todo")])
        return _make_execute_result(issues=[])

    mock_client.tools.execute.side_effect = side_effect

    reader = LinearReader(arcade_api_key="arc_test", team="M2AI", poll_states="Backlog,Todo")
    issues = reader.get_issues()

    assert len(issues) == 2
    assert call_count == 2
    reader.close()


@patch("readers.linear_reader.Arcade")
def test_get_issues_respects_limit(MockArcade):
    """get_issues stops at the limit."""
    mock_client = Mock()
    MockArcade.return_value = mock_client

    many_issues = [_make_issue(f"M2A-{i}") for i in range(10)]
    mock_client.tools.execute.return_value = _make_execute_result(issues=many_issues)

    reader = LinearReader(arcade_api_key="arc_test", poll_states="Backlog")
    issues = reader.get_issues(limit=3)

    assert len(issues) == 3
    reader.close()


@patch("readers.linear_reader.Arcade")
def test_get_issues_handles_api_error(MockArcade):
    """get_issues continues on API error for a state."""
    mock_client = Mock()
    MockArcade.return_value = mock_client
    mock_client.tools.execute.side_effect = Exception("API timeout")

    reader = LinearReader(arcade_api_key="arc_test", poll_states="Backlog")
    issues = reader.get_issues()

    assert issues == []
    reader.close()


@patch("readers.linear_reader.Arcade")
def test_get_issues_skips_invalid_issues(MockArcade):
    """Issues without an identifier are filtered out."""
    mock_client = Mock()
    MockArcade.return_value = mock_client

    raw_issues = [
        _make_issue("M2A-1"),
        {"title": "No identifier"},  # Missing identifier
        {},  # Empty
    ]
    mock_client.tools.execute.return_value = _make_execute_result(issues=raw_issues)

    reader = LinearReader(arcade_api_key="arc_test", poll_states="Backlog")
    issues = reader.get_issues()

    assert len(issues) == 1
    assert issues[0]["identifier"] == "M2A-1"
    reader.close()


@patch("readers.linear_reader.Arcade")
def test_get_issues_passes_filters(MockArcade):
    """get_issues passes team and label filter to API."""
    mock_client = Mock()
    MockArcade.return_value = mock_client
    mock_client.tools.execute.return_value = _make_execute_result(issues=[])

    reader = LinearReader(
        arcade_api_key="arc_test",
        team="TestTeam",
        label_filter="custom-label",
        poll_states="Todo"
    )
    reader.get_issues()

    call_args = mock_client.tools.execute.call_args
    params = call_args.kwargs["input"]
    assert params["team"] == "TestTeam"
    assert params["label"] == "custom-label"
    assert params["state"] == "Todo"
    reader.close()


# --- get_issue_detail Tests ---


@patch("readers.linear_reader.Arcade")
def test_get_issue_detail_success(MockArcade):
    """get_issue_detail returns normalized issue."""
    mock_client = Mock()
    MockArcade.return_value = mock_client

    raw = _make_issue("M2A-30", "Detailed Issue", priority=1)
    mock_client.tools.execute.return_value = _make_execute_result(issue=raw)

    reader = LinearReader(arcade_api_key="arc_test")
    detail = reader.get_issue_detail("M2A-30")

    assert detail is not None
    assert detail["identifier"] == "M2A-30"
    assert detail["title"] == "Detailed Issue"
    reader.close()


@patch("readers.linear_reader.Arcade")
def test_get_issue_detail_not_found(MockArcade):
    """get_issue_detail returns None on API error."""
    mock_client = Mock()
    MockArcade.return_value = mock_client
    mock_client.tools.execute.side_effect = Exception("Not found")

    reader = LinearReader(arcade_api_key="arc_test")
    detail = reader.get_issue_detail("NONEXISTENT-99")

    assert detail is None
    reader.close()


# --- Priority Mapping Tests ---


def test_priority_to_score_all_levels():
    """Verify all Linear priority levels map correctly."""
    assert LinearReader.priority_to_score(1) == 100.0   # urgent
    assert LinearReader.priority_to_score(2) == 75.0    # high
    assert LinearReader.priority_to_score(3) == 50.0    # medium
    assert LinearReader.priority_to_score(4) == 25.0    # low
    assert LinearReader.priority_to_score(0) == 50.0    # none -> medium
    assert LinearReader.priority_to_score(99) == 50.0   # unknown -> fallback


# --- issue_to_idea Tests ---


def test_issue_to_idea_conversion():
    """issue_to_idea produces a dict compatible with priority queue."""
    reader = LinearReader.__new__(LinearReader)  # skip __init__

    issue = {
        "identifier": "M2A-42",
        "title": "Build API endpoint",
        "description": "Create REST API for user profiles",
        "priority": 2,
        "priority_label": "High",
        "state_name": "Todo",
    }

    idea = reader.issue_to_idea(issue)

    assert idea["id"] == "M2A-42"
    assert idea["title"] == "Build API endpoint"
    assert idea["description"] == "Create REST API for user profiles"
    assert idea["_source"] == "linear"
    assert idea["_linear_identifier"] == "M2A-42"
    assert idea["artifact_type"] == "tool"


def test_issue_to_idea_empty_description():
    """issue_to_idea falls back to title when description is empty."""
    reader = LinearReader.__new__(LinearReader)

    issue = {
        "identifier": "M2A-1",
        "title": "Quick fix",
        "description": "",
        "priority": 3,
    }

    idea = reader.issue_to_idea(issue)
    assert idea["description"] == "Quick fix"


# --- Close Tests ---


def test_close_clears_client():
    """close() clears the internal client reference."""
    reader = LinearReader(arcade_api_key="arc_test")
    reader._client = Mock()
    reader.close()
    assert reader._client is None
