"""Require real, successful execution in the bundled GUT JUnit report."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def _count(element, name, *, required=False):
    value = element.get(name, None if required else '0')
    if value is None or not value.isascii() or not value.isdecimal():
        raise ValueError(f'Invalid or missing {name} count on {element.tag}')
    return int(value)


def check_report(path: Path) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f'Cannot read GUT report: {exc}') from exc
    if root.tag != 'testsuites':
        raise ValueError('Expected GUT testsuites report')
    suites = root.findall('testsuite')
    cases = []
    for element in [root, *suites]:
        # GUT counts failing tests at the root and failing assertions in
        # suites. In particular, before_all/after_all can fail only a suite.
        if _count(element, 'failures', required=True) or _count(element, 'errors'):
            raise ValueError('GUT reported failures or errors')
        if element.get('skipped') is not None:
            _count(element, 'skipped')  # Pending calls need not equal cases.
    for suite in suites:
        children = suite.findall('testcase')
        if _count(suite, 'tests', required=True) != len(children):
            raise ValueError('GUT suite test count does not match its cases')
        cases.extend(children)
    if _count(root, 'tests', required=True) != len(cases):
        raise ValueError('GUT total test count does not match its cases')
    if any(element.tag in ('failure', 'error') for element in root.iter()):
        raise ValueError('GUT reported a failed test')
    executed = 0
    for case in cases:
        status = case.get('status')
        if status not in ('pass', 'no asserts', 'pending', 'skipped', 'not run'):
            raise ValueError(f'Unsuccessful or unknown GUT status: {status!r}')
        if status in ('pass', 'no asserts') and case.find('skipped') is None:
            executed += 1
    if not executed:
        raise ValueError('GUT did not execute any non-skipped tests')
    return executed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args(argv)
    try:
        executed = check_report(args.report)
    except ValueError as exc:
        print(f'::error::{exc}', file=sys.stderr)
        return 1
    print(f'GUT report: {executed} non-skipped tests executed; no failures')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
