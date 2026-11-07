"""The GUI must retain settings snapshot provenance across a concurrent write."""
from types import SimpleNamespace

from holdem import gui, settings


def test_gui_preference_save_does_not_restore_stale_progression(monkeypatch):
    assert settings.set("xp", 10)
    assert settings.set("bankroll", 1000)
    snapshot = settings.load()
    assert settings.set("xp", 20)
    assert settings.set("bankroll", 2000)
    app = SimpleNamespace(_bucket=lambda scope: {"theme": "Classic Felt"} if scope == settings.CLIENT else {})
    with monkeypatch.context() as context:
        context.setattr(settings, "load", lambda: snapshot)
        gui.Holdem.save_config(app)
    assert settings.get("theme") == "Classic Felt"
    assert settings.get("xp") == 20
    assert settings.get("bankroll") == 2000
