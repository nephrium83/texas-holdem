"""
pytest configuration for the holdem test suite.

Run header:

The header states whether libsodium/Ristretto255 loaded, because the
crypto-gated suites skip themselves at import when it did not and a bare
"N skipped" under ``pytest -q`` cannot tell that apart from a Godot binary
being absent. See tests/crypto_gate.py for the policy and
tests/test_crypto_gate.py for the test that enforces it.

Autouse fixtures:

1. Clear the M-14 in-memory settings cache before every test. Without this,
   test_settings_persistence_roundtrip (which saves "Classic Felt") leaves a
   stale cache entry that causes test_settings_load_tolerates_garbage to fail
   when it expects the default "Cyberpunk" theme.

2. Point HOLDEM_CONFIG_DIR at a per-test temporary directory, so nothing in
   the suite reads or writes the developer's real config. This matters now
   that Session persists a device secret there: without it, running the tests
   would create (or worse, adopt) the machine's actual mental-poker key
   material. Tests that set the variable themselves still win -- monkeypatch
   applies after this fixture.
"""
import sys
from pathlib import Path

import pytest

# Both paths are set up here rather than relied upon. This conftest is
# imported before any test module has run its own sys.path line, and the
# crypto probe below imports holdem -- so the repo root has to be reachable
# even in a checkout where the package was never pip-installed. Getting
# that wrong would make the probe report "unavailable" for the wrong
# reason, and it caches.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crypto_gate


def pytest_report_header(config):
    """Name the crypto situation in the run header."""
    return crypto_gate.header_line()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say it again at the end, because CI runs ``pytest -q``.

    The run header is suppressed at negative verbosity, which is exactly
    the mode the engine-tests job uses -- so relying on the header alone
    would put the crypto status everywhere except the log that matters.
    Terminal-summary hooks run regardless of verbosity.
    """
    terminalreporter.write_line(crypto_gate.header_line())


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Redirect the client config directory to a per-test temp dir."""
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "holdem-config"))


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Reset the holdem.settings in-memory cache before and after each test."""
    try:
        from holdem import settings as _cfg
        _cfg._cache = None
    except Exception:
        pass
    yield
    try:
        from holdem import settings as _cfg
        _cfg._cache = None
    except Exception:
        pass
