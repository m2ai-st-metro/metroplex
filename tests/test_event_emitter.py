"""Tests for the Sky-Lynx event emitter (Phase F)."""
import json

from event_emitter import EventEmitter


def test_emit_creates_json_file(tmp_path):
    emitter = EventEmitter(events_dir=tmp_path)
    result = emitter.emit("build_failed", {"job_id": "test-1", "title": "Test Build"})
    assert result is not None
    assert result.exists()
    data = json.loads(result.read_text())
    assert data["event_type"] == "build_failed"
    # Portfolio-wide event correlation schema: source_repo is canonical,
    # source is a deprecated alias kept equal to source_repo.
    assert data["source_repo"] == "metroplex"
    assert data["source"] == "metroplex"
    # correlation_id is a top-level field; default is None.
    assert "correlation_id" in data
    assert data["correlation_id"] is None
    assert data["details"]["job_id"] == "test-1"
    assert "timestamp" in data


def test_emit_with_correlation_id(tmp_path):
    """correlation_id passed to emit() lands as a top-level JSON field."""
    emitter = EventEmitter(events_dir=tmp_path)
    result = emitter.emit(
        "build_failed",
        {"job_id": "test-1"},
        correlation_id="idea-42",
    )
    assert result is not None
    data = json.loads(result.read_text())
    assert data["correlation_id"] == "idea-42"
    assert data["source_repo"] == "metroplex"
    assert data["source"] == "metroplex"


def test_emit_atomic_no_tmp_files(tmp_path):
    emitter = EventEmitter(events_dir=tmp_path)
    emitter.emit("build_completed", {"job_id": "test-2"})
    tmp_files = list(tmp_path.glob(".*.tmp"))
    assert len(tmp_files) == 0


def test_emit_creates_directory(tmp_path):
    events_dir = tmp_path / "nested" / "events"
    emitter = EventEmitter(events_dir=events_dir)
    result = emitter.emit("ratchet_tightened", {"previous": 45.0, "new": 47.0})
    assert result is not None
    assert events_dir.exists()


def test_emit_multiple_events_unique_files(tmp_path):
    emitter = EventEmitter(events_dir=tmp_path)
    paths = set()
    for i in range(5):
        result = emitter.emit("build_failed", {"job_id": f"test-{i}"})
        assert result is not None
        paths.add(result)
    assert len(paths) == 5


def test_emit_returns_none_on_readonly_dir(tmp_path):
    events_dir = tmp_path / "readonly"
    events_dir.mkdir()
    events_dir.chmod(0o444)
    emitter = EventEmitter(events_dir=events_dir)
    result = emitter.emit("build_failed", {"job_id": "test"})
    assert result is None
    events_dir.chmod(0o755)  # cleanup
