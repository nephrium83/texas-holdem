"""The built rules package runs in another application's empty environment.

Source-tree imports can hide missing wheel files and dependencies supplied
by the developer's desktop environment. Exercise an actual wheel with both
of those conveniences removed, without contacting a package index.
"""
from __future__ import annotations

from email.parser import Parser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _package_env():
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    return env


def _run(command, *, cwd, env):
    result = subprocess.run(
        [str(part) for part in command], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def core_wheel(tmp_path_factory):
    work = tmp_path_factory.mktemp("installed-poker-package")
    source = work / "source snapshot"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(
        ROOT / "holdem", source / "holdem",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    wheel_dir = work / "wheelhouse"
    _run(
        [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check",
         "wheel", source, "--no-index", "--no-deps", "--no-build-isolation",
         "--wheel-dir", wheel_dir],
        cwd=work, env=_package_env(),
    )
    wheels = list(wheel_dir.glob("holdem-*.whl"))
    assert len(wheels) == 1
    return work, wheels[0]


def test_wheel_exposes_optional_desktop_and_p2p_dependencies(core_wheel):
    _, wheel = core_wheel
    with zipfile.ZipFile(wheel) as archive:
        metadata_path, = (name for name in archive.namelist()
                          if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_path).decode("utf-8"))
        assert "holdem/embedding.py" in archive.namelist()
    requirements = metadata.get_all("Requires-Dist") or []
    assert all("extra ==" in requirement for requirement in requirements), requirements
    assert {"desktop", "p2p", "dev"} <= set(metadata.get_all("Provides-Extra") or [])
    for package, extra in (("Pillow", "desktop"),
                           ("cryptography", "desktop"),
                           ("cryptography", "p2p")):
        assert any(requirement.lower().startswith(package.lower())
                   and f'extra == "{extra}"' in requirement
                   for requirement in requirements), requirements


@pytest.mark.timeout(120)
def test_installed_core_runs_outside_checkout_without_host_dependencies(core_wheel):
    work, wheel = core_wheel
    environment = work / "host application's environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [python, "-m", "pip", "--isolated", "--disable-pip-version-check",
         "install", "--no-index", "--no-deps", wheel],
        cwd=work, env=_package_env(),
    )
    script = r'''
import importlib.util
import json
from pathlib import Path
import random
import sys

for dependency in ("PIL", "cryptography", "miniupnpc"):
    assert importlib.util.find_spec(dependency) is None, dependency

forbidden = {"tkinter", "PIL", "cryptography", "socket", "asyncio"}
assert not (forbidden & sys.modules.keys()), "probe setup loaded host modules"

import holdem
from holdem import embedding
from holdem.embedding import PlayerSeat, PokerTable, TableConfig

installed = Path(holdem.__file__).resolve()
assert installed.is_relative_to(Path(sys.prefix).resolve()), installed
assert Path(embedding.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
players = [PlayerSeat("guest", "RPG guest", 1000),
           PlayerSeat("regular", "Tavern regular", 1000)]
table = PokerTable("installed-tavern-table", players, rng=random.Random(7),
                   config=TableConfig(small_blind=25, big_blind=50))
assert table.api_version == embedding.API_VERSION
table.start_hand()
for _ in range(20):
    if table.phase == "settled":
        break
    view = table.view("guest")
    actor = players[view["action_on"]].player_id
    outcome = table.command(actor, "check_call")
    assert outcome["ok"] is True
assert table.phase == "settled"
assert len(table.view("guest")["board"]) == 5
assert table.view("guest")["pot"] == 0
settlement = table.hand_result()
assert settlement["winner_ids"]
assert sum(settlement["final_chips"].values()) == 2000
receipt = table.close()
repeated = table.close()
assert receipt == repeated == table.result()
assert receipt["final_chips"] == settlement["final_chips"]
assert receipt["result_id"] == "installed-tavern-table/close"
receipt["final_chips"]["guest"] = -1
assert table.close() == repeated
assert not (forbidden & sys.modules.keys()), sorted(forbidden & sys.modules.keys())

# Metadata inspection imports email.utils and socket on Python 3.10/3.12.
# Inspect distribution metadata only after checking the installed core's
# import and runtime behavior, so the probe cannot contaminate that check.
import importlib.metadata
assert importlib.metadata.version("holdem") == holdem.__version__
installed_distributions = sorted(distribution.metadata["Name"].lower()
                                 for distribution in importlib.metadata.distributions())
assert not ({"pillow", "cryptography", "miniupnpc"} & set(installed_distributions))
print(json.dumps({"holdem_path": str(installed), "hand_settled": True,
                  "stable_close_receipt": True,
                  "installed_distributions": installed_distributions}))
'''
    report = json.loads(_run([python, "-I", "-c", script], cwd=work, env=_package_env()))
    assert report["hand_settled"] is True
    assert report["stable_close_receipt"] is True
    assert "holdem" in report["installed_distributions"]

    # A second host must work with the installed wheel alone. Copy only the
    # sample application, not the checkout, and retain isolated import rules.
    host = work / "another RPG host"
    host.mkdir()
    example = host / "rpg_tavern.py"
    shutil.copy2(ROOT / "examples" / "rpg_tavern.py", example)
    demo = json.loads(_run(
        [python, "-I", example, "--demo"], cwd=host, env=_package_env(),
    ))
    assert demo["tables_completed"] == 2
    assert demo["fresh_visit"] is True
    assert demo["private_views_checked"] is True
    assert demo["duplicate_payout_prevented"] is True
    assert demo["total_funds"] == 8000
