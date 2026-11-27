#!/usr/bin/env python3
"""Check the 29-row acceptance matrix, defect coverage and release evidence; never contacts a network.

Without --run it checks the matrix against the inventory, the plugin
descriptor, the test sources, local Git provenance and the qualification
guide, and that each installed row names the engine blockers its workflow
graph has. With --run it also runs every referenced test method on its own and
reports each row from its own results: a skipped, failing or missing test fails
its row, and a passing suite elsewhere never stands in for it.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import package, qualification  # noqa: E402

GUIDE = 'docs/qualification.md'


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], check=True, capture_output=True).stdout


def check_guide(matrix, text):
    """The guide's row table lists every matrix row exactly once, in matrix order, with its status."""
    rows = re.findall(r'^\| `cf-workflows:([a-z0-9-]+)` \| ([^|]+)\| ([^|]+)\|', text, re.M)
    names = [name for name, _, _ in rows]
    qualification.require(names == [row['skill'] for row in matrix['rows']],
                          f'{GUIDE}: the row table must list every matrix row once, in matrix order')
    for (name, composition, native), row in zip(rows, matrix['rows']):
        qualification.require(composition.strip() == row['composition']
                              and native.strip() == row['status']['native_execution'],
                              f'{GUIDE}: {name} composition or native status differs from the matrix')


def run(ids):
    """Run each test ID once; return {id: outcome}."""
    sys.path.insert(0, str(ROOT / 'tests'))
    outcomes = {}

    class Result(unittest.TestResult):
        def addSuccess(self, test):
            outcomes[test.id()] = 'passed'

        def addFailure(self, test, err):
            outcomes[test.id()] = 'failed'

        def addError(self, test, err):
            outcomes[getattr(test, 'id', lambda: str(test))()] = 'error'

        def addSkip(self, test, reason):
            outcomes[test.id()] = 'skipped'

        def addExpectedFailure(self, test, err):
            outcomes[test.id()] = 'expected failure'

        def addUnexpectedSuccess(self, test):
            outcomes[test.id()] = 'unexpected success'

        def addSubTest(self, test, subtest, err):
            if err is not None:
                outcomes[test.id()] = 'failed'
            super().addSubTest(test, subtest, err)

    loader = unittest.TestLoader()
    result = Result()
    for test_id in sorted(ids):
        try:
            suite = loader.loadTestsFromName(test_id)
        except Exception as error:  # noqa: BLE001
            outcomes[test_id] = f'not loadable: {error}'
            continue
        suite.run(result)
        if outcomes.get(test_id) is None:
            outcomes[test_id] = 'not run'
    return outcomes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='also run every referenced test and report per row')
    args = parser.parse_args(argv)
    inventory = json.loads((ROOT / 'baselines/inventory.json').read_text())
    checked = package.check(package.ROOT)
    descriptor = checked['descriptor']
    matrix = qualification.load(ROOT)
    records = qualification.read_records(matrix, ROOT)
    tests = qualification.index_tests(ROOT / 'tests')
    accepted = qualification.validate(
        matrix, inventory, descriptor_skills={skill['name'] for skill in descriptor['skills']}, tests=tests,
        records=records, closure_digest=checked['closure_digest'], engine_version=descriptor['engine-version'])
    qualification.check_paths(matrix, ROOT)
    qualification.check_engine_blockers(matrix, package.ROOT,
                                        {skill['name']: skill['workflow'] for skill in descriptor['skills']})
    qualification.check_provenance(matrix, tests, git)
    check_guide(matrix, (ROOT / GUIDE).read_text())
    cited = qualification.references(matrix)
    summary = {'rows': len(matrix['rows']), 'defects': len(matrix['defects']),
               'admission_cases': len(matrix['configuration_admission']), 'test_references': len(cited),
               'verified_combinations': accepted['combinations'],
               'native_rows': sorted(accepted['native_rows']),
               'live_acceptance': {name: sorted(rows) for name, rows in accepted['procedures'].items()}}
    if args.run:
        outcomes = run(cited)
        failed = {ref: outcome for ref, outcome in outcomes.items() if outcome != 'passed'}
        owners = {}
        for ref, places in cited.items():
            for owner, _ in places:
                owners.setdefault(owner, []).append(ref)
        summary['run'] = {'tests': len(outcomes), 'failed': failed,
                          'owners': {owner: 'failed' if any(ref in failed for ref in refs) else 'passed'
                                     for owner, refs in owners.items()}}
        print(json.dumps(summary, indent=2))
        return 1 if failed else 0
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (qualification.MatrixError, package.PackageError, OSError, ValueError) as error:
        print(f'acceptance matrix rejected: {error}', file=sys.stderr)
        sys.exit(1)
