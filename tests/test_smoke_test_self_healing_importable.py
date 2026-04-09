"""Import/argparse sanity check for the smoke-test driver.

This is NOT an end-to-end test (that requires a live daemon -- see
scripts/smoke_test_self_healing.py module docstring). It only verifies
that the driver module loads without import errors and that argparse
accepts the documented flags.
"""
import importlib.util
from pathlib import Path


DRIVER_PATH = (
    Path(__file__).parent.parent / "scripts" / "smoke_test_self_healing.py"
)


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "smoke_test_self_healing", DRIVER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_driver_module_imports():
    """Loading the script as a module must not raise."""
    driver = _load_driver()
    assert hasattr(driver, "main")
    assert hasattr(driver, "DEFAULT_TIMEOUT_SECONDS")
    assert hasattr(driver, "DEFAULT_SPEC_PATH")


def test_driver_argparse_accepts_defaults(monkeypatch):
    """argparse must accept no arguments (all defaults)."""
    driver = _load_driver()
    monkeypatch.setattr("sys.argv", ["smoke_test_self_healing.py"])
    args = driver._parse_args()
    assert args.timeout == driver.DEFAULT_TIMEOUT_SECONDS
    assert args.spec == driver.DEFAULT_SPEC_PATH


def test_driver_argparse_accepts_overrides(monkeypatch, tmp_path):
    """argparse must accept --timeout and --spec overrides."""
    driver = _load_driver()
    spec = tmp_path / "custom.md"
    spec.write_text("x")
    monkeypatch.setattr(
        "sys.argv",
        ["smoke_test_self_healing.py", "--timeout", "60", "--spec", str(spec)],
    )
    args = driver._parse_args()
    assert args.timeout == 60
    assert args.spec == spec


def test_driver_fixture_spec_exists():
    """The default fixture spec file must be checked in and readable."""
    driver = _load_driver()
    assert driver.DEFAULT_SPEC_PATH.exists()
    content = driver.DEFAULT_SPEC_PATH.read_text()
    assert "calculator" in content.lower()
