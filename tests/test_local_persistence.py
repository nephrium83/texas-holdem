"""Local preferences and notes preserve other writers and failed-write data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from holdem import notes, settings
from process_output import stop_process


def test_stale_settings_snapshot_only_writes_changed_keys():
    snapshot = settings.load()
    assert settings.set("xp", 1234)
    snapshot["client"]["theme"] = "Classic Felt"
    assert settings.save(snapshot["client"], snapshot["last_table"])
    assert settings.get("xp") == 1234
    assert settings.get("theme") == "Classic Felt"
    # A successful save advances this snapshot's baseline.
    assert settings.set("theme", "Cyberpunk")
    snapshot["client"]["sound_volume"] = 12
    assert settings.save(snapshot["client"], snapshot["last_table"])
    assert settings.get("theme") == "Cyberpunk"


def test_normalized_values_are_written_to_disk_in_both_buckets():
    assert settings.save({"xp": 10**15, "avatar_b64": "a" * 20000, "nickname": False}, {"sb": -5})
    raw = json.loads(settings.config_path().read_text(encoding="utf-8"))
    assert raw["client"]["xp"] == settings.SPEC["xp"]["hi"]
    assert len(raw["client"]["avatar_b64"]) == settings.SPEC["avatar_b64"]["maxlen"]
    assert raw["client"]["nickname"] == ""
    assert raw["last_table"]["sb"] == 1
    assert settings.load()["last_table"] == raw["last_table"]


@pytest.mark.parametrize("invalid", [
    {"sb": 40, "bb": 20}, {"buyin_min_bb": 300, "buyin_max_bb": 200},
    {"bank_start": 300, "bank_cap": 120}, {"stack": 20, "bb": 100},
])
def test_inconsistent_table_rules_cannot_be_constructed_saved_or_hashed(invalid):
    assert settings.save({}, {})
    original = settings.config_path().read_bytes()
    with pytest.raises(ValueError):
        settings.table_rules(**invalid)
    with pytest.raises(ValueError):
        settings.rules_hash({**settings.defaults(settings.TABLE_RULE), **invalid})
    assert not settings.save({}, invalid)
    assert settings.config_path().read_bytes() == original


@pytest.mark.parametrize("contents", ['{broken', '[]', '{"client": []}', '{"last_table":{"sb":40,"bb":20}}'])
def test_read_failure_defaults_are_not_cached_or_allowed_to_overwrite_original(contents, caplog):
    path = settings.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    assert settings.load()["client"]["theme"] == "Cyberpunk"
    assert not settings.set("theme", "Classic Felt")
    assert path.read_text(encoding="utf-8") == contents
    assert "original preserved" in caplog.text
    # A repaired file is seen on the next read without restarting a cache.
    path.write_text(json.dumps({"client": {"theme": "Classic Felt"}}), encoding="utf-8")
    assert settings.get("theme") == "Classic Felt"


def test_notes_failure_preserves_file_and_successful_memory_state(tmp_path, monkeypatch, caplog):
    path = tmp_path / "notes.json"
    monkeypatch.setattr(notes, "_NOTES_FILE", path)
    assert notes.set_note("one", "original", "Ada")
    original = path.read_bytes()
    cached = notes._data
    def fail(*args):
        raise OSError("controlled replace failure")
    with monkeypatch.context() as context:
        context.setattr(settings.os, "replace", fail)
        assert not notes.set_note("one", "lost")
    assert path.read_bytes() == original
    assert notes._data is cached
    assert notes.get("one")["note"] == "original"
    assert "notes save failed" in caplog.text
    assert not list(tmp_path.glob("*.tmp"))
    path.write_text('{broken', encoding="utf-8")
    assert notes.get("one")["note"] == ""
    assert not notes.set_color("one", "red")
    assert path.read_text(encoding="utf-8") == '{broken'


def _wait(path, children):
    deadline = time.monotonic() + 8
    while not path.exists():
        assert all(child.poll() is None for child in children), "writer exited before barrier"
        if time.monotonic() >= deadline:
            pytest.fail(f"writer did not reach {path.name}")
        time.sleep(0.01)


def test_two_process_settings_snapshot_cannot_restore_stale_progression(tmp_path):
    source = """
import sys,time
from pathlib import Path
from holdem import settings
root=Path(sys.argv[1]); who=sys.argv[2]
snapshot=settings.load()
(root/(who+'.ready')).touch()
while not (root/(who+'.go')).exists(): time.sleep(.01)
if who=='a': assert settings.set('xp',4321)
else:
    snapshot['client']['theme']='Classic Felt'
    assert settings.save(snapshot['client'],snapshot['last_table'])
"""
    children = [subprocess.Popen([sys.executable, '-c', source, str(tmp_path), who]) for who in ('a', 'b')]
    try:
        for who in ('a', 'b'):
            _wait(tmp_path / (who + '.ready'), children)
        (tmp_path / 'a.go').touch()
        assert children[0].wait(8) == 0
        (tmp_path / 'b.go').touch()
        assert children[1].wait(8) == 0
    finally:
        for child in children:
            stop_process(child)
    assert settings.get('xp') == 4321
    assert settings.get('theme') == 'Classic Felt'


def test_two_process_notes_writers_preserve_both_entries(tmp_path, monkeypatch):
    path = tmp_path / 'notes.json'
    monkeypatch.setattr(notes, '_NOTES_FILE', path)
    source = """
import sys,time
from pathlib import Path
from holdem import notes
root=Path(sys.argv[1]); who=sys.argv[2]
notes._NOTES_FILE=root/'notes.json'
notes.all()
(root/(who+'.ready')).touch()
while not (root/'go').exists(): time.sleep(.01)
assert notes.set_note(who,'note '+who)
"""
    children = [subprocess.Popen([sys.executable, '-c', source, str(tmp_path), who]) for who in ('a', 'b')]
    try:
        for who in ('a', 'b'):
            _wait(tmp_path / (who + '.ready'), children)
        (tmp_path / 'go').touch()
        assert all(child.wait(8) == 0 for child in children)
    finally:
        for child in children:
            stop_process(child)
    assert {entry['peer_id']: entry['note'] for entry in notes.all()} == {'a': 'note a', 'b': 'note b'}
