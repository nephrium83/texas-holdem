"""CI must retain its check contract and reject false green GUT runs."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from gut_report_check import check_report

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / 'tests' / 'gut_report_check.py'


def _workflow():
    return yaml.load((ROOT / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)


def _report(tmp_path, suites, tests=1, failures=0, extra=''):
    path = tmp_path / 'gut.xml'
    path.write_text(f'<testsuites tests="{tests}" failures="{failures}" {extra}>{suites}</testsuites>')
    return path


def _suite(cases='<testcase status="pass"/>', tests=1, failures=0, extra=''):
    return f'<testsuite tests="{tests}" failures="{failures}" {extra}>{cases}</testsuite>'


def test_valid_gut_report_preserves_inner_classes_pending_and_parameterized_cases(tmp_path):
    suites = _suite('', tests=0)
    suites += _suite('<testcase name="test[0]" status="pass"/>'
                     '<testcase name="test[1]" status="no asserts"/>'
                     '<testcase status="pending"><skipped>pending</skipped></testcase>'
                     '<testcase status="skipped"/>', tests=4, extra='skipped="3"')
    assert check_report(_report(tmp_path, suites, tests=4)) == 2


@pytest.mark.parametrize('suites,tests,failures,extra', [
    ('', 0, 0, ''),
    (_suite('', tests=0), 0, 0, ''),
    (_suite(), 2, 0, ''),
    (_suite(tests=2), 1, 0, ''),
    (_suite(), -1, 0, ''),
    (_suite(), 1, 1, ''),
    (_suite(failures=2), 1, 0, ''),  # Failed before_all with passing cases.
    (_suite(extra='errors="1"'), 1, 0, ''),
    (_suite(), 1, 0, 'errors="1"'),
    (_suite('<testcase status="pass"><failure/></testcase>'), 1, 0, ''),
    (_suite('<testcase status="pass"><error/></testcase>'), 1, 0, ''),
    (_suite('<testcase status="fail"/>'), 1, 0, ''),
    (_suite('<testcase status="unknown"/>'), 1, 0, ''),
    (_suite('<testcase status="pending"><skipped/></testcase>'), 1, 0, ''),
    (_suite('<testcase status="skipped"/>'), 1, 0, ''),
    (_suite('<testcase status="not run"/>'), 1, 0, ''),
])
def test_bad_or_empty_gut_results_refuse(tmp_path, suites, tests, failures, extra):
    with pytest.raises(ValueError):
        check_report(_report(tmp_path, suites, tests, failures, extra))


@pytest.mark.parametrize('content', ['', '<testsuites', '<unrelated/>'])
def test_missing_or_malformed_reports_refuse(tmp_path, content):
    path = tmp_path / 'gut.xml'
    with pytest.raises(ValueError):
        check_report(path)
    path.write_text(content)
    with pytest.raises(ValueError):
        check_report(path)


def test_report_cli_returns_failure_without_a_report_and_success_for_real_cases(tmp_path):
    path = tmp_path / 'gut.xml'
    failed = subprocess.run([sys.executable, '-B', str(CHECKER), str(path)], capture_output=True, timeout=5)
    assert failed.returncode != 0
    _report(tmp_path, _suite())
    passed = subprocess.run([sys.executable, '-B', str(CHECKER), str(path)], capture_output=True, timeout=5)
    assert passed.returncode == 0, passed.stderr


def test_ci_pins_actions_and_limits_checkout_credentials():
    workflow = _workflow()
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['jobs']['engine-tests']['strategy']['matrix']['python-version'] == ['3.10', '3.11', '3.12', '3.13']
    assert set(workflow['jobs']) == {'engine-tests', 'sidecar-integration', 'godot-sidecar',
                                     'gui-smoke', 'godot-tests', 'desktop-windows', 'journal-windows'}
    for job in workflow['jobs'].values():
        for step in job['steps']:
            if 'uses' not in step:
                continue
            assert re.fullmatch(r'[\w.-]+/[\w.-]+@[a-f0-9]{40}', step['uses'])
            if step['uses'].startswith('actions/checkout@'):
                assert step['with']['persist-credentials'] == 'false'


def test_gut_workflow_rejects_failed_process_even_with_a_passing_summary(tmp_path):
    steps = _workflow()['jobs']['godot-tests']['steps']
    run = next(step for step in steps if step.get('name') == 'Run GUT tests headless')
    assert run.get('shell') == 'bash'
    assert '-gjunit_xml_file=' in run['run']
    bash = shutil.which('bash') or r'C:\Program Files\Git\bin\bash.exe'
    if not Path(bash).is_file():
        pytest.skip('Bash is needed to exercise GitHub shell pipeline semantics')
    source = 'godot() { printf "Run Summary\\nAll tests passed!\\n"; return 7; }\n' + run['run']
    env = dict(os.environ, RUNNER_TEMP=tmp_path.as_posix())
    result = subprocess.run([bash, '--noprofile', '--norc', '-eo', 'pipefail', '-c', source],
                            cwd=tmp_path, env=env, capture_output=True, timeout=10)
    assert result.returncode != 0, result.stdout
    validation = next(step for step in steps if step.get('name') == 'Validate nonempty GUT results')
    assert 'gut_report_check.py' in validation['run']
