"""
pytest configuration for the holdem test suite.

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
import pytest


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
