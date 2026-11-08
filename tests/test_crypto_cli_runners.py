"""Standalone entry points must preserve pytest's failures, fixtures and parameters."""
import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["test_bg_svp.py", "test_dleq.py", "test_ristretto.py",
                                    "test_pedersen.py", "test_elgamal.py", "test_keygen_pop.py",
                                    "test_contract.py", "test_shuffle_mp.py", "test_shuffle_proof.py",
                                    "test_client_view.py"])
@pytest.mark.parametrize("failing", [False, True])
def test_direct_runner_preserves_pytest_exit(module, failing, tmp_path):
    source = (Path(__file__).parent / module).read_text()
    guard = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'")
    runner = ast.get_source_segment(source, guard)
    probe = tmp_path / "test_runner_probe.py"
    probe.write_text("import pytest, sys\n"
                     "@pytest.fixture\ndef value(): return 7\n"
                     "@pytest.mark.parametrize('n', [1, 2])\n"
                     "def test_probe(value, n):\n"
                     + ("    assert value == 8\n" if failing else "    assert value == 7 and n > 0\n")
                     + runner + "\n")
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run([sys.executable, str(probe), "-q"], capture_output=True,
                            text=True, timeout=30, env=env)
    assert result.returncode == (1 if failing else 0), result.stdout + result.stderr
    assert ("2 failed" if failing else "2 passed") in result.stdout
