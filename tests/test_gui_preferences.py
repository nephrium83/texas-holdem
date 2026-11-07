"""Drive the real preference dialog with isolated widget doubles."""
from types import SimpleNamespace
import pytest
from holdem import gui, settings


class Var:
    def __init__(self, value=None, **kwargs): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class Widget:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.destroyed = False
    def __getattr__(self, name):
        if name.startswith("winfo_"):
            return lambda: 100
        return lambda *args, **kwargs: None
    def pack(self, *args, **kwargs): pass
    def config(self, **kwargs): self.kwargs.update(kwargs)
    def destroy(self): self.destroyed = True


def dialog(monkeypatch):
    widgets, errors, sounds = [], [], []
    def factory(kind):
        def create(*args, **kwargs):
            widget = Widget(*args, **kwargs)
            widgets.append((kind, widget))
            return widget
        return create
    for kind in ("Toplevel", "Frame", "Label", "Button", "Checkbutton", "Entry", "Scale"):
        monkeypatch.setattr(gui.tk, kind, factory(kind))
    for kind in ("StringVar", "IntVar", "BooleanVar"):
        monkeypatch.setattr(gui.tk, kind, Var)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(gui.colorchooser, "askcolor", lambda **k: (None, "#123456"))
    monkeypatch.setattr(gui._audio, "set_enabled", lambda v: sounds.append(("enabled", v)))
    monkeypatch.setattr(gui._audio, "set_volume", lambda v: sounds.append(("volume", v)))
    app = gui.Holdem.__new__(gui.Holdem)
    app.root = app.sizes = Widget()
    app.theme = gui.THEMES["Cyberpunk"]
    app.size_btns = [Widget()]
    app.redraw = lambda: None
    app.preset = lambda *a: None
    values = {"felt_color": "#35654d", "four_color_deck": False,
              "sounds_enabled": True, "sound_volume": 70, "bet_buttons": "0.5,1,2,3"}
    for name, value in values.items(): setattr(app, "v_" + name, Var(value))
    assert settings.save(values, {})
    app.open_preferences()
    buttons = {w.kwargs["text"]: w.kwargs["command"] for k, w in widgets if k == "Button"}
    return app, widgets, buttons, errors, sounds, values


def change_draft(widgets, buttons, shortcuts="0.75,2"):
    buttons["Pick..."]()
    for kind, widget in widgets:
        if kind == "Checkbutton":
            widget.kwargs["variable"].set("Four-color" in widget.kwargs["text"])
        if kind == "Scale": widget.kwargs["variable"].set(25)
        if kind == "Entry": widget.kwargs["textvariable"].set(shortcuts)


def test_cancel_discards_all_runtime_and_persisted_changes(monkeypatch):
    app, widgets, buttons, errors, sounds, values = dialog(monkeypatch)
    before = settings.config_path().read_bytes()
    change_draft(widgets, buttons)
    buttons["Cancel"]()
    assert settings.config_path().read_bytes() == before
    assert {name: getattr(app, "v_" + name).get() for name in values} == values
    assert not errors and not sounds
    assert next(w for k, w in widgets if k == "Toplevel").destroyed


def test_save_applies_complete_draft_in_one_transaction(monkeypatch):
    app, widgets, buttons, errors, sounds, values = dialog(monkeypatch)
    change_draft(widgets, buttons)
    real_save = settings.save
    saves = []
    def save(changes, last_table):
        saves.append(dict(changes))
        assert last_table == {}
        return real_save(changes, last_table)
    monkeypatch.setattr(settings, "save", save)
    buttons["Save"]()
    expected = dict(felt_color="#123456", four_color_deck=True, sounds_enabled=False,
                    sound_volume=25, bet_buttons="0.75,2")
    assert saves == [expected]
    assert all(settings.get(name) == value == getattr(app, "v_" + name).get()
               for name, value in expected.items())
    assert not errors
    assert sounds == [("enabled", False), ("volume", 0.25)]
    assert [button.kwargs["text"] for button in app.size_btns] == ["3/4 pot", "2x pot", "All-in"]


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-1", "101", "bad", "1,", ",".join(["1"] * 9)])
def test_invalid_shortcuts_do_not_poison_config_or_destroy_controls(monkeypatch, bad):
    app, widgets, buttons, errors, sounds, values = dialog(monkeypatch)
    before = settings.config_path().read_bytes()
    old = list(app.size_btns)
    change_draft(widgets, buttons, bad)
    buttons["Save"]()
    assert errors and not sounds
    assert settings.config_path().read_bytes() == before
    assert {name: getattr(app, "v_" + name).get() for name in values} == values
    assert app.size_btns == old and not old[0].destroyed
    assert not next(w for k, w in widgets if k == "Toplevel").destroyed


def test_failed_save_keeps_draft_open_and_does_not_apply(monkeypatch):
    app, widgets, buttons, errors, sounds, values = dialog(monkeypatch)
    change_draft(widgets, buttons)
    monkeypatch.setattr(settings, "save", lambda *args: False)
    buttons["Save"]()
    assert errors and not sounds
    assert {name: getattr(app, "v_" + name).get() for name in values} == values
    assert not next(w for k, w in widgets if k == "Toplevel").destroyed


def test_invalid_persisted_shortcuts_have_safe_controls_without_rewriting(monkeypatch):
    app, widgets, buttons, errors, sounds, values = dialog(monkeypatch)
    app.v_bet_buttons.set("nan,inf")
    before = settings.config_path().read_bytes()
    app._rebuild_bet_buttons()
    assert [b.kwargs["text"] for b in app.size_btns] == ["1/2 pot", "Pot", "2x pot", "3x pot", "All-in"]
    assert settings.config_path().read_bytes() == before


def test_widget_construction_failure_keeps_old_controls(monkeypatch):
    app, widgets, buttons, errors, sounds, values = dialog(monkeypatch)
    old = list(app.size_btns)
    def broken(*args, **kwargs): raise RuntimeError("cannot construct button")
    monkeypatch.setattr(gui.tk, "Button", broken)
    with pytest.raises(RuntimeError, match="construct button"):
        app._rebuild_bet_buttons()
    assert app.size_btns == old and not old[0].destroyed
