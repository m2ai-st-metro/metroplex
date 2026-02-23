"""
Tests for Patch Gate (Gate 3) - Git Operations & YAML Patches.
"""
import pytest
import sqlite3
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock, call

from config import Config
from db import StateDB
from audit import AuditLogger
from readers.stfactory_reader import STFactoryReader
from gates.patcher import PatchGate
from models import PatchApplication


# --- Fixtures ---


@pytest.fixture
def patch_config():
    """Provide test configuration."""
    config = Config()
    config.max_patches_per_cycle = 5
    config.academy_repo = "m2ai-portfolio/agent-persona-academy"
    config.yce_dir = "/home/apexaipc/projects/yce-harness"
    return config


@pytest.fixture
def in_memory_state_db():
    """Provide in-memory state database."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def temp_audit_log():
    """Provide temporary audit log file."""
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="w") as f:
        log_path = f.name

    yield log_path

    # Cleanup
    Path(log_path).unlink(missing_ok=True)


@pytest.fixture
def create_stfactory_db():
    """Factory fixture to create ST Factory test database with patches."""
    def _create_db(patches_data):
        """
        Create ST Factory test database with specified patches.

        Args:
            patches_data: List of tuples (patch_id, persona_id, from_version, to_version, rationale, raw_json, status)
        """
        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()

        # Create persona_patches table
        cursor.execute("""
            CREATE TABLE persona_patches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patch_id TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                from_version TEXT,
                to_version TEXT,
                rationale TEXT,
                raw_json TEXT,
                status TEXT
            )
        """)

        # Insert patches
        for patch_data in patches_data:
            patch_id, persona_id, from_version, to_version, rationale, raw_json, status = patch_data
            cursor.execute("""
                INSERT INTO persona_patches (
                    patch_id, persona_id, from_version, to_version, rationale, raw_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (patch_id, persona_id, from_version, to_version, rationale, raw_json, status))

        conn.commit()
        return conn

    return _create_db


# --- YAML Patch Operation Tests ---


def test_apply_yaml_patch_add_operation():
    """Test applying an 'add' operation to YAML data."""
    config = Config()
    state_db = StateDB(":memory:")
    state_db.init_db()

    # Create mock ST Factory reader
    mock_reader = Mock(spec=STFactoryReader)

    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, mock_reader, audit_logger)

    # Test data
    yaml_data = {
        "name": "TestPersona",
        "voice": {}
    }

    operations = [
        {"op": "add", "path": "/voice/tone", "value": "concise"}
    ]

    # Apply patch
    result = gate._apply_yaml_patch(yaml_data, operations)

    # Verify key was added
    assert "tone" in result["voice"]
    assert result["voice"]["tone"] == "concise"

    state_db.close()


def test_apply_yaml_patch_replace_operation():
    """Test applying a 'replace' operation to YAML data."""
    config = Config()
    state_db = StateDB(":memory:")
    state_db.init_db()

    mock_reader = Mock(spec=STFactoryReader)
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, mock_reader, audit_logger)

    # Test data with existing value
    yaml_data = {
        "name": "TestPersona",
        "voice": {
            "tone": "concise"
        }
    }

    operations = [
        {"op": "replace", "path": "/voice/tone", "value": "formal"}
    ]

    # Apply patch
    result = gate._apply_yaml_patch(yaml_data, operations)

    # Verify key was replaced
    assert result["voice"]["tone"] == "formal"

    state_db.close()


def test_apply_yaml_patch_remove_operation():
    """Test applying a 'remove' operation to YAML data."""
    config = Config()
    state_db = StateDB(":memory:")
    state_db.init_db()

    mock_reader = Mock(spec=STFactoryReader)
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, mock_reader, audit_logger)

    # Test data with value to remove
    yaml_data = {
        "name": "TestPersona",
        "voice": {
            "tone": "concise",
            "style": "professional"
        }
    }

    operations = [
        {"op": "remove", "path": "/voice/tone"}
    ]

    # Apply patch
    result = gate._apply_yaml_patch(yaml_data, operations)

    # Verify key was removed
    assert "tone" not in result["voice"]
    assert "style" in result["voice"]  # Other keys remain

    state_db.close()


def test_apply_yaml_patch_multiple_operations():
    """Test applying multiple operations in sequence."""
    config = Config()
    state_db = StateDB(":memory:")
    state_db.init_db()

    mock_reader = Mock(spec=STFactoryReader)
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, mock_reader, audit_logger)

    yaml_data = {
        "name": "TestPersona",
        "voice": {}
    }

    operations = [
        {"op": "add", "path": "/voice/tone", "value": "concise"},
        {"op": "add", "path": "/voice/style", "value": "professional"},
        {"op": "replace", "path": "/voice/tone", "value": "formal"}
    ]

    # Apply patch
    result = gate._apply_yaml_patch(yaml_data, operations)

    # Verify all operations applied
    assert result["voice"]["tone"] == "formal"
    assert result["voice"]["style"] == "professional"

    state_db.close()


def test_apply_yaml_patch_nested_path():
    """Test applying operations to deeply nested paths."""
    config = Config()
    state_db = StateDB(":memory:")
    state_db.init_db()

    mock_reader = Mock(spec=STFactoryReader)
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, mock_reader, audit_logger)

    yaml_data = {
        "name": "TestPersona",
        "metadata": {}
    }

    operations = [
        {"op": "add", "path": "/metadata/config/llm/temperature", "value": 0.7}
    ]

    # Apply patch
    result = gate._apply_yaml_patch(yaml_data, operations)

    # Verify nested structure created
    assert result["metadata"]["config"]["llm"]["temperature"] == 0.7

    state_db.close()


# --- Per-Cycle Cap Tests ---


def test_per_cycle_cap_enforcement(create_stfactory_db):
    """Test that only max_patches_per_cycle patches are processed."""
    import json

    # Create 7 patches
    patches_data = []
    for i in range(1, 8):
        patch_id = f"patch-{i}"
        persona_id = f"persona-{i}"
        operations = [{"op": "add", "path": "/voice/tone", "value": "concise"}]
        raw_json = json.dumps({"operations": operations})
        patches_data.append((patch_id, persona_id, "1.0", "1.1", "test", raw_json, "proposed"))

    stfactory_conn = create_stfactory_db(patches_data)

    # Write temp DB to file for reader
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Copy in-memory to file
    file_conn = sqlite3.connect(db_path)
    stfactory_conn.backup(file_conn)
    file_conn.close()
    stfactory_conn.close()

    # Create reader
    reader = STFactoryReader(db_path)

    # Create config with cap of 5
    config = Config()
    config.max_patches_per_cycle = 5

    state_db = StateDB(":memory:")
    state_db.init_db()
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, reader, audit_logger)

    # Run in dry-run mode
    results = gate.run(dry_run=True)

    # Verify only 5 patches processed
    assert len(results) == 5

    # Verify correct patches processed (first 5)
    for i, result in enumerate(results, start=1):
        assert result.patch_id == f"patch-{i}"

    # Cleanup
    reader.close()
    state_db.close()
    Path(db_path).unlink()


# --- Dry Run Tests ---


def test_dry_run_no_subprocess_calls(create_stfactory_db, capsys):
    """Test that dry_run=True does not call subprocess or modify ST Factory DB."""
    import json

    # Create 1 patch
    operations = [{"op": "add", "path": "/voice/tone", "value": "concise"}]
    raw_json = json.dumps({"operations": operations})
    patches_data = [
        ("patch-1", "persona-1", "1.0", "1.1", "test", raw_json, "proposed")
    ]

    stfactory_conn = create_stfactory_db(patches_data)

    # Write temp DB to file
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    file_conn = sqlite3.connect(db_path)
    stfactory_conn.backup(file_conn)
    file_conn.close()
    stfactory_conn.close()

    reader = STFactoryReader(db_path)

    config = Config()
    state_db = StateDB(":memory:")
    state_db.init_db()
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, reader, audit_logger)

    # Run with dry_run=True
    with patch('subprocess.run') as mock_subprocess:
        results = gate.run(dry_run=True)

        # Verify subprocess was NOT called
        mock_subprocess.assert_not_called()

    # Verify output was printed
    captured = capsys.readouterr()
    assert "[DRY RUN]" in captured.out
    assert "patch-1" in captured.out
    assert "persona-1" in captured.out

    # Verify no records in state DB
    state_db.connect()
    cursor = state_db.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM patch_applications")
    count = cursor.fetchone()[0]
    assert count == 0

    # Cleanup
    reader.close()
    state_db.close()
    Path(db_path).unlink()


# --- Git Operations Tests ---


def test_git_operations_commands(create_stfactory_db, tmp_path):
    """Test that correct git commands are constructed."""
    import json

    # Create 1 patch
    operations = [{"op": "add", "path": "/voice/tone", "value": "concise"}]
    raw_json = json.dumps({"operations": operations})
    patches_data = [
        ("patch-1", "persona-1", "1.0", "1.1", "test", raw_json, "proposed")
    ]

    stfactory_conn = create_stfactory_db(patches_data)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    file_conn = sqlite3.connect(db_path)
    stfactory_conn.backup(file_conn)
    file_conn.close()
    stfactory_conn.close()

    reader = STFactoryReader(db_path)

    config = Config()
    config.yce_dir = str(tmp_path)
    state_db = StateDB(":memory:")
    state_db.init_db()
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, reader, audit_logger)

    # Create mock work directory with YAML file
    work_dir = tmp_path / "tmp" / "academy"
    work_dir.mkdir(parents=True)
    personas_dir = work_dir / "personas"
    personas_dir.mkdir()

    # Create .git directory
    git_dir = work_dir / ".git"
    git_dir.mkdir()

    # Create persona YAML file
    yaml_file = personas_dir / "persona-1.yaml"
    yaml_file.write_text("name: TestPersona\nvoice: {}\n")

    # Mock subprocess calls
    with patch('subprocess.run') as mock_subprocess:
        # Mock successful git operations
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

        results = gate.run(dry_run=False)

        # Verify git commands were called
        calls = mock_subprocess.call_args_list

        # Find git pull, add, commit, push calls
        git_pull_called = any("pull" in str(call) for call in calls)
        git_add_called = any("add" in str(call) for call in calls)
        git_commit_called = any("commit" in str(call) for call in calls)
        git_push_called = any("push" in str(call) for call in calls)

        assert git_pull_called or git_add_called  # At least one git operation

        # Verify commands use list format (not shell=True)
        for call_obj in calls:
            args, kwargs = call_obj
            if args:  # Check if there are positional args
                cmd = args[0]
                assert isinstance(cmd, list), "subprocess.run should use list format"

    # Cleanup
    reader.close()
    state_db.close()
    Path(db_path).unlink()


def test_git_push_failure_continues_to_next_patch(create_stfactory_db, tmp_path):
    """Test that failed git push results in status='failed' but next patch is still attempted."""
    import json

    # Create 2 patches
    operations = [{"op": "add", "path": "/voice/tone", "value": "concise"}]
    raw_json = json.dumps({"operations": operations})
    patches_data = [
        ("patch-1", "persona-1", "1.0", "1.1", "test", raw_json, "proposed"),
        ("patch-2", "persona-2", "1.0", "1.1", "test", raw_json, "proposed")
    ]

    stfactory_conn = create_stfactory_db(patches_data)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    file_conn = sqlite3.connect(db_path)
    stfactory_conn.backup(file_conn)
    file_conn.close()
    stfactory_conn.close()

    reader = STFactoryReader(db_path)

    config = Config()
    config.yce_dir = str(tmp_path)
    state_db = StateDB(":memory:")
    state_db.init_db()
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, reader, audit_logger)

    # Create mock work directory
    work_dir = tmp_path / "tmp" / "academy"
    work_dir.mkdir(parents=True)
    personas_dir = work_dir / "personas"
    personas_dir.mkdir()
    git_dir = work_dir / ".git"
    git_dir.mkdir()

    # Create persona YAML files
    (personas_dir / "persona-1.yaml").write_text("name: Persona1\nvoice: {}\n")
    (personas_dir / "persona-2.yaml").write_text("name: Persona2\nvoice: {}\n")

    # Mock subprocess: first patch fails on push, second succeeds
    call_count = {"push": 0}

    def mock_run_side_effect(cmd, **kwargs):
        if "push" in cmd:
            call_count["push"] += 1
            if call_count["push"] == 1:
                # First push fails
                return MagicMock(returncode=1, stdout="", stderr="permission denied")
            else:
                # Second push succeeds
                return MagicMock(returncode=0, stdout="", stderr="")
        else:
            # All other git commands succeed
            return MagicMock(returncode=0, stdout="", stderr="")

    with patch('subprocess.run', side_effect=mock_run_side_effect):
        results = gate.run(dry_run=False)

    # Verify both patches were attempted
    assert len(results) == 2

    # First patch should have failed
    assert results[0].status == "failed"
    assert "push failed" in results[0].reason

    # Second patch should have succeeded
    assert results[1].status == "applied"

    # Cleanup
    reader.close()
    state_db.close()
    Path(db_path).unlink()


def test_no_operations_skips_patch(create_stfactory_db):
    """Test that patches with no operations are skipped."""
    import json

    # Create patch with empty operations
    raw_json = json.dumps({"operations": []})
    patches_data = [
        ("patch-1", "persona-1", "1.0", "1.1", "test", raw_json, "proposed")
    ]

    stfactory_conn = create_stfactory_db(patches_data)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    file_conn = sqlite3.connect(db_path)
    stfactory_conn.backup(file_conn)
    file_conn.close()
    stfactory_conn.close()

    reader = STFactoryReader(db_path)

    config = Config()
    state_db = StateDB(":memory:")
    state_db.init_db()
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, reader, audit_logger)

    results = gate.run(dry_run=False)

    # Verify patch was skipped
    assert len(results) == 1
    assert results[0].status == "skipped"
    assert "no operations" in results[0].reason

    # Cleanup
    reader.close()
    state_db.close()
    Path(db_path).unlink()


def test_missing_target_file_fails(create_stfactory_db, tmp_path):
    """Test that missing target file results in failure."""
    import json

    # Create patch
    operations = [{"op": "add", "path": "/voice/tone", "value": "concise"}]
    raw_json = json.dumps({"operations": operations})
    patches_data = [
        ("patch-1", "persona-missing", "1.0", "1.1", "test", raw_json, "proposed")
    ]

    stfactory_conn = create_stfactory_db(patches_data)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    file_conn = sqlite3.connect(db_path)
    stfactory_conn.backup(file_conn)
    file_conn.close()
    stfactory_conn.close()

    reader = STFactoryReader(db_path)

    config = Config()
    config.yce_dir = str(tmp_path)
    state_db = StateDB(":memory:")
    state_db.init_db()
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, reader, audit_logger)

    # Create work directory but NO persona file
    work_dir = tmp_path / "tmp" / "academy"
    work_dir.mkdir(parents=True)
    personas_dir = work_dir / "personas"
    personas_dir.mkdir()
    git_dir = work_dir / ".git"
    git_dir.mkdir()

    # Mock git pull to succeed
    with patch('subprocess.run') as mock_subprocess:
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

        results = gate.run(dry_run=False)

    # Verify patch failed
    assert len(results) == 1
    assert results[0].status == "failed"
    assert "not found" in results[0].reason

    # Cleanup
    reader.close()
    state_db.close()
    Path(db_path).unlink()


def test_successful_patch_updates_stfactory_status(create_stfactory_db, tmp_path):
    """Test that successful patch updates status in ST Factory DB."""
    import json

    # Create patch
    operations = [{"op": "add", "path": "/voice/tone", "value": "concise"}]
    raw_json = json.dumps({"operations": operations})
    patches_data = [
        ("patch-1", "persona-1", "1.0", "1.1", "test", raw_json, "proposed")
    ]

    stfactory_conn = create_stfactory_db(patches_data)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    file_conn = sqlite3.connect(db_path)
    stfactory_conn.backup(file_conn)
    file_conn.close()
    stfactory_conn.close()

    reader = STFactoryReader(db_path)

    config = Config()
    config.yce_dir = str(tmp_path)
    state_db = StateDB(":memory:")
    state_db.init_db()
    audit_logger = AuditLogger(":memory:")

    gate = PatchGate(config, state_db, reader, audit_logger)

    # Create work directory with YAML file
    work_dir = tmp_path / "tmp" / "academy"
    work_dir.mkdir(parents=True)
    personas_dir = work_dir / "personas"
    personas_dir.mkdir()
    git_dir = work_dir / ".git"
    git_dir.mkdir()

    yaml_file = personas_dir / "persona-1.yaml"
    yaml_file.write_text("name: TestPersona\nvoice: {}\n")

    # Mock git operations to succeed
    with patch('subprocess.run') as mock_subprocess:
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

        results = gate.run(dry_run=False)

    # Verify patch was applied
    assert len(results) == 1
    assert results[0].status == "applied"

    # Verify ST Factory DB was updated
    # Reopen reader with writable connection to check
    check_conn = sqlite3.connect(db_path)
    cursor = check_conn.cursor()
    cursor.execute("SELECT status FROM persona_patches WHERE patch_id = 'patch-1'")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "applied"
    check_conn.close()

    # Cleanup
    reader.close()
    state_db.close()
    Path(db_path).unlink()
