"""
pytest configuration for the holdem test suite.

Autouse fixture: clear the M-14 in-memory settings cache before every test.
Without this, test_settings_persistence_roundtrip (which saves "Classic Felt")
leaves a stale cache entry that causes test_settings_load_tolerates_garbage to
fail when it expects the default "Cyberpunk" theme.
"""
import pytest


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
