"""The legacy spec must require and bundle verified native input/provenance."""
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tools import prepare_ice_sodium

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'packaging' / 'legacy_tk.spec'


def _execute(monkeypatch):
    hooks = ModuleType('PyInstaller.utils.hooks')
    hooks.collect_all = lambda name: ([], [], [])
    hooks.collect_submodules = lambda name: []
    for name in ('PyInstaller', 'PyInstaller.utils'):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, 'PyInstaller.utils.hooks', hooks)
    analysis = []
    def analyze(*args, **kwargs):
        analysis.append(kwargs)
        # Only current PyInstaller6 attributes: obsolete zip/cipher use fails.
        return SimpleNamespace(pure=[], scripts=[], binaries=kwargs['binaries'], datas=kwargs['datas'])
    namespace = dict(SPECPATH=str(ROOT/'packaging'), Analysis=analyze,
                     PYZ=lambda pure: object(), EXE=lambda *args, **kwargs: None)
    exec(compile(SPEC.read_text(encoding='utf-8'), str(SPEC), 'exec'), namespace)
    return analysis


def test_missing_receipt_cannot_fall_back_to_a_developer_dll(monkeypatch):
    monkeypatch.delenv('POKER_SODIUM_RESULT', raising=False)
    monkeypatch.setenv('HOLDEM_LIBSODIUM', 'developer.dll')
    with pytest.raises(SystemExit, match='POKER_SODIUM_RESULT'):
        _execute(monkeypatch)


def test_invalid_receipt_refuses_build_before_analysis(tmp_path, monkeypatch):
    receipt = tmp_path/'invalid.json'
    receipt.write_text('{"v":1}', encoding='utf-8')
    monkeypatch.setenv('POKER_SODIUM_RESULT', str(receipt))
    with pytest.raises(RuntimeError, match='schema'):
        _execute(monkeypatch)


def test_verified_receipt_selects_exact_dll_license_and_manifest(tmp_path, monkeypatch):
    receipt = tmp_path/'verified.json'
    paths = {key: str(tmp_path/name) for key, name in {
        'dll':'libsodium.dll', 'license':'libsodium-ISC.txt',
        'attribution':'libsodium-SOURCE.txt', 'manifest':'sodium-manifest.json'}.items()}
    seen = []
    # This control tests spec wiring. The real verifier's hash/schema tests
    # live in test_prepare_ice_sodium, rather than faking crypto evidence here.
    monkeypatch.setattr(prepare_ice_sodium, 'verify_result', lambda selected: seen.append(selected) or paths)
    monkeypatch.setenv('POKER_SODIUM_RESULT', str(receipt))
    analysis = _execute(monkeypatch)
    assert seen == [str(receipt)]
    assert len(analysis) == 1
    assert analysis[0]['binaries'] == [(paths['dll'], 'native')]
    assert analysis[0]['datas'] == [(paths['license'], 'licenses'),
                                   (paths['attribution'], 'licenses'), (paths['manifest'], '.')]
