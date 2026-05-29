"""Invocation surface tests.

Covers spec-claim: C-11 (claude --agent eldercare; mission-cli).
"""

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_agent_yaml_invocation() -> None:
    """Covers C-11: agent.yaml has the fields claude --agent eldercare needs."""
    data = yaml.safe_load((ROOT / "agent.yaml").read_text())
    assert "name" in data
    assert "model" in data


def test_main_runnable() -> None:
    """Covers C-11: agent.py is runnable as a script and accepts --message."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "agent.py"), "--message", "we had an argument"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"agent.py exited {result.returncode}: {result.stderr}"
    # Some response on stdout
    assert result.stdout.strip(), "expected output on stdout"
