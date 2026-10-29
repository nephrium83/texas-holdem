"""Player commands select Godot; the Tk harness must be an explicit choice."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('entry', ['module', 'main.py', 'packaging/windows_entrypoint.py'])
def test_player_entrypoint_delegates_to_godot_and_preserves_exit_code(entry):
    code = r'''
import builtins, json, runpy, sys, types
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'tkinter' or name.startswith('tkinter.') or name == 'holdem.gui':
        raise AssertionError('Player command reached the legacy Tkinter harness')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
entry = sys.argv[1]
sys.argv = ['player-command', '--self-test', '--godot', 'chosen-runtime.exe']
calls = []
launcher = types.ModuleType('holdem.desktop_launcher')
def main():
    calls.append(sys.argv[1:])
    return 23
launcher.main = main
sys.modules['holdem.desktop_launcher'] = launcher
try:
    if entry == 'module':
        runpy.run_module('holdem', run_name='__main__')
    else:
        runpy.run_path(entry, run_name='__main__')
except SystemExit as exc:
    print(json.dumps({'exit': exc.code, 'calls': calls}))
else:
    raise AssertionError('Launcher exit status was discarded')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, entry], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        'exit': 23, 'calls': [['--self-test', '--godot', 'chosen-runtime.exe']]}


def test_source_player_help_exposes_godot_runtime_options_without_loading_tkinter():
    code = r'''
import builtins, runpy, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'tkinter' or name.startswith('tkinter.') or name == 'holdem.gui':
        raise AssertionError('Player help loaded the legacy Tkinter harness')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ['holdem', '--help']
runpy.run_module('holdem', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(option in result.stdout for option in ('--godot', '--pck', '--libsodium'))


def test_retired_build_target_cannot_silently_package_the_tkinter_client():
    result = subprocess.run([sys.executable, '-X', 'utf8', 'poker.spec'], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode != 0
    assert 'retired' in result.stderr.lower()
    assert 'tools/build_windows.py' in result.stderr
