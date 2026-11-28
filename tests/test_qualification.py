"""The 29-row acceptance matrix and its checker (#38).

The committed matrix must pass, and every way of standing in for a row (a
missing row, an alias, an aggregate reference, borrowed evidence, a dangling or
unmarked test, a status the evidence does not support, an unauthorized or stale
live record) must reject. Git provenance runs through a scripted fake. Nothing
here contacts a provider or GitHub, and no result certifies a host, provider
or engine.
"""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import package, qualification  # noqa: E402

SPEC = importlib.util.spec_from_file_location('check_acceptance', ROOT / 'scripts/check_acceptance.py')
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

INVENTORY = json.loads((ROOT / 'baselines/inventory.json').read_text())
MATRIX = qualification.load(ROOT)
CHECKED = package.check(package.ROOT)
DESCRIPTOR = CHECKED['descriptor']
LISTED = {skill['name'] for skill in DESCRIPTOR['skills']}
ENGINE = DESCRIPTOR['engine-version']
TESTS = qualification.index_tests(ROOT / 'tests')
RECORD_PATH = 'baselines/evidence/live-provider-codex.json'


def validate(matrix, records=None, tests=TESTS, closure_digest=CHECKED['closure_digest']):
    return qualification.validate(matrix, INVENTORY, descriptor_skills=LISTED, tests=tests, records=records,
                                  closure_digest=closure_digest, engine_version=ENGINE)


def row(matrix, name):
    return next(item for item in matrix['rows'] if item['skill'] == name)


def record(procedure='live_provider', skill='dk-pr-fix'):
    return {
        'schema_version': 1, 'kind': 'acceptance_evidence', 'suite': 'cf-workflows', 'procedure': procedure,
        'authorization': {'granted_by': 'owner', 'granted_at': '2027-01-01T00:00:00Z',
                          'reference': 'https://example.invalid/authorization', 'scope': 'disposable repo only'},
        'plugin': {'version': DESCRIPTOR['version'], 'closure_digest': CHECKED['closure_digest']},
        'engine': {'version': ENGINE,
                   'release': f'https://github.com/dk-code-factory/agent-workflow/releases/tag/v{ENGINE}'},
        'host': {'name': 'codex', 'version': '1.0.0'},
        'provider': {'name': 'provider', 'model': 'model', 'service_tier': 'default'},
        'target': {'repository': 'example/disposable', 'disposable': True},
        'recorded_at': '2027-01-01T01:00:00Z', 'skill': skill,
        'scenarios': [{'id': name, 'result': 'passed', 'evidence_refs': [f'session:{skill}-{procedure}-{name}']}
                      for name in qualification.PROCEDURES[procedure]],
        'cost': {'complete': True, 'usd': 1.25},
    }


def with_live_provider(matrix, documents):
    """List each {path: record} as live-provider evidence with its combination and verified row."""
    matrix = copy.deepcopy(matrix)
    matrix['live_acceptance']['live_provider'] = {'status': 'recorded', 'records': list(documents)}
    matrix['verified_combinations'] = [{'host': 'codex', 'provider': 'provider', 'engine_version': ENGINE,
                                        'record': path} for path in documents]
    for document in documents.values():
        row(matrix, document['skill'])['status']['native_execution'] = 'verified'
    return matrix


class CommittedMatrix(unittest.TestCase):
    def test_committed_matrix_traces_every_row_defect_and_case(self):
        accepted = validate(MATRIX)
        qualification.check_paths(MATRIX, ROOT)
        checker.check_guide(MATRIX, (ROOT / checker.GUIDE).read_text())
        self.assertEqual([item['skill'] for item in MATRIX['rows']],
                         [item['name'] for item in INVENTORY['skills']])
        self.assertEqual(sorted(int(d['upstream'].rsplit('#', 1)[1]) for d in MATRIX['defects']),
                         sorted(qualification.DEFECTS))
        self.assertEqual((accepted['combinations'], accepted['native_rows']), (0, set()))
        for item in MATRIX['rows']:
            self.assertEqual((item['status']['native_execution'], item['status']['live_acceptance'],
                              item['status']['parity']), ('pending', 'pending', 'not_implemented'))

    def test_committed_defect_provenance_is_in_this_history(self):
        try:
            checker.git('rev-parse', '--verify', 'da21e12a93fa4aba85ebbc45af648fbb11f3eb19^{commit}')
        except Exception:  # noqa: BLE001 - a shallow or exported tree has no history to check
            self.skipTest('the provenance commits are not in this checkout')
        qualification.check_provenance(MATRIX, TESTS, checker.git)

    def test_release_stays_blocked_on_every_unverified_capability(self):
        status = package.inventory_status(DESCRIPTOR, ROOT, closure_digest=CHECKED['closure_digest'])
        self.assertFalse(status['releasable'])
        blockers = [item['prerequisite'] for item in status['blockers']]
        for name in ('live acceptance: user_supplied_skill', 'live acceptance: live_provider',
                     'live acceptance: github_publication', 'compatibility', 'acceptance matrix'):
            self.assertIn(name, blockers)


class RowsCannotBeStoodInFor(unittest.TestCase):
    def rejects(self, matrix, message, **extra):
        with self.assertRaisesRegex(qualification.MatrixError, message):
            validate(matrix, **extra)

    def test_missing_duplicate_and_alias_rows_reject(self):
        missing = copy.deepcopy(MATRIX)
        missing['rows'] = [item for item in missing['rows'] if item['skill'] != 'dk-ci-flakes']
        self.rejects(missing, r"missing inventory row\(s\) \['dk-ci-flakes'\]")
        duplicate = copy.deepcopy(MATRIX)
        duplicate['rows'].append(copy.deepcopy(duplicate['rows'][0]))
        self.rejects(duplicate, 'appears twice')
        for alias in ('cf-workflows:dk-ci-flakes', 'dk-ci-*', 'dk-ci-flake', 'all'):
            with self.subTest(alias=alias):
                aliased = copy.deepcopy(MATRIX)
                row(aliased, 'dk-ci-flakes')['skill'] = alias
                self.rejects(aliased, 'not an inventory row')

    def test_a_row_cannot_point_at_another_row(self):
        for field, value in (('same_as', 'dk-ci'), ('alias_of', 'dk-ci'), ('covered_by', ['dk-ci'])):
            with self.subTest(field=field):
                aliased = copy.deepcopy(MATRIX)
                row(aliased, 'dk-ci-flakes')[field] = value
                self.rejects(aliased, 'unexpected field')

    def test_aggregate_or_empty_references_reject(self):
        for kind in ('direct', 'composed', 'recovery'):
            for aggregate in ('test_ci_skills', 'test_ci_skills.FlakeDiscovery', 'tests', '*', 'all tests passed',
                              'test_ci_skills.FlakeDiscovery.*', 'test_ci_skills.FlakeDiscovery.test_*'):
                with self.subTest(kind=kind, aggregate=aggregate):
                    matrix = copy.deepcopy(MATRIX)
                    row(matrix, 'dk-ci-flakes')[kind] = [aggregate]
                    self.rejects(matrix, 'not a single test method')
            with self.subTest(kind=kind, empty=True):
                matrix = copy.deepcopy(MATRIX)
                row(matrix, 'dk-ci-flakes')[kind] = []
                self.rejects(matrix, 'needs at least one scenario')

    def test_a_row_cannot_borrow_all_of_its_direct_evidence(self):
        matrix = copy.deepcopy(MATRIX)
        row(matrix, 'dk-ci-flakes')['direct'] = list(row(matrix, 'dk-ci')['direct'][:1])
        self.rejects(matrix, 'dk-ci-flakes: every direct scenario is shared with another row')

    def test_dangling_references_reject(self):
        for path in (('rows', 'direct'), ('rows', 'composed'), ('rows', 'recovery'), ('defects',), ('admission',)):
            with self.subTest(path=path):
                matrix = copy.deepcopy(MATRIX)
                ghost = 'test_ci_skills.FlakeDiscovery.test_that_does_not_exist'
                if path[0] == 'rows':
                    row(matrix, 'dk-ci-flakes')[path[1]].append(ghost)
                elif path[0] == 'defects':
                    matrix['defects'][0]['tests'].append(ghost)
                else:
                    matrix['configuration_admission'][0]['tests'].append(ghost)
                self.rejects(matrix, 'dangling test reference')

    def test_statuses_the_evidence_does_not_support_reject(self):
        cases = (({'parity': 'implemented'}, 'parity differs from the inventory'),
                 ({'native_execution': 'verified'}, 'native_execution must be pending'),
                 ({'live_acceptance': 'recorded'}, 'live_acceptance must be pending'),
                 ({'installed_workflow': False}, 'installed_workflow must match'),
                 ({'deterministic': 'live_verified'}, 'deterministic status'))
        for change, message in cases:
            with self.subTest(change=change):
                matrix = copy.deepcopy(MATRIX)
                row(matrix, 'dk-pr-alternatives')['status'].update(change)
                self.rejects(matrix, message)
        listed = copy.deepcopy(MATRIX)
        row(listed, 'dk-pr-fix')['status']['installed_workflow'] = False
        self.rejects(listed, 'installed_workflow must match')
        quiet = copy.deepcopy(MATRIX)
        row(quiet, 'dk-ci-flakes')['remaining'] = ' '
        self.rejects(quiet, 'must state what remains')

    def test_rows_must_name_existing_code_and_their_guide(self):
        matrix = copy.deepcopy(MATRIX)
        row(matrix, 'dk-ci-flakes')['behavior'] = ['cf_workflows/missing.py']
        with self.assertRaisesRegex(qualification.MatrixError, 'does not exist'):
            qualification.check_paths(matrix, ROOT)
        # Existing files outside the repository are not its implementing code or guide.
        for behavior, documentation in (([str(ROOT / 'README.md')], 'docs/qualification.md'),
                                        (['cf_workflows/package.py'], f'docs/../../{ROOT.name}/README.md')):
            with self.subTest(behavior=behavior, documentation=documentation):
                row(matrix, 'dk-ci-flakes').update(behavior=behavior, documentation=documentation)
                with self.assertRaisesRegex(qualification.MatrixError, 'is not a path inside this repository'):
                    qualification.check_paths(matrix, ROOT)

    def test_guide_table_must_match_the_matrix(self):
        text = (ROOT / checker.GUIDE).read_text()
        line = next(item for item in text.splitlines() if item.startswith('| `cf-workflows:dk-ci-flakes`'))
        for changed in (text.replace(line + '\n', ''), text.replace(line, line.replace('| pending |', '| verified |')),
                        text.replace(line, line.replace('| boundary |', '| shared_workflow |'))):
            with self.assertRaises(qualification.MatrixError):
                checker.check_guide(MATRIX, changed)


class Defects(unittest.TestCase):
    def test_exactly_the_nine_inherited_defects(self):
        missing = copy.deepcopy(MATRIX)
        missing['defects'] = missing['defects'][1:]
        extra = copy.deepcopy(MATRIX)
        extra['defects'].append({**copy.deepcopy(extra['defects'][0]), 'upstream': 'dk-code-factory/dk-workflows#999'})
        duplicate = copy.deepcopy(MATRIX)
        duplicate['defects'][1] = copy.deepcopy(duplicate['defects'][0])
        for matrix in (missing, extra, duplicate):
            with self.assertRaisesRegex(qualification.MatrixError, 'exactly the nine inherited defects'):
                validate(matrix)

    def test_defect_tests_must_name_their_defect(self):
        matrix = copy.deepcopy(MATRIX)
        defect = next(d for d in matrix['defects'] if d['upstream'].endswith('#240'))
        defect['tests'] = ['test_routing.Routing.test_own_pr_routes_to_fixing_and_another_authors_to_review']
        with self.assertRaisesRegex(qualification.MatrixError, 'does not name dk-workflows#240'):
            validate(matrix)

    def test_defects_need_provenance_and_live_procedures(self):
        for change, message in (({'provenance': []}, 'provenance missing'),
                                ({'provenance': [{'pr': 1, 'merge_commit': 'main'}]}, 'full merge commit'),
                                ({'live_acceptance': []}, 'live_acceptance'),
                                ({'live_acceptance': ['manual_check']}, 'live_acceptance')):
            with self.subTest(change=change):
                matrix = copy.deepcopy(MATRIX)
                matrix['defects'][0].update(change)
                with self.assertRaisesRegex(qualification.MatrixError, message):
                    validate(matrix)

    def git(self, ancestors, files):
        calls = []

        def git(*args):
            calls.append(args)
            if args[0] == 'cat-file':
                return b''
            if args[0] == 'merge-base':
                if args[2] not in ancestors:
                    raise RuntimeError('not an ancestor')
                return b''
            commit, path = args[1].split(':', 1)
            if (commit, path) not in files:
                raise RuntimeError('missing')
            return files[(commit, path)].encode()
        return git, calls

    def test_provenance_needs_an_ancestor_merge_commit_containing_the_test(self):
        defect = next(d for d in MATRIX['defects'] if d['upstream'].endswith('#260'))
        matrix = {**MATRIX, 'defects': [defect]}
        commit = defect['provenance'][0]['merge_commit']
        path = TESTS[defect['tests'][0]]['path']
        body = 'class PendingReviews(Fixture):\n' + ''.join(
            f'    def {TESTS[ref]["name"]}(self):\n        pass\n' for ref in defect['tests'])
        git, calls = self.git({commit}, {(commit, path): body})
        qualification.check_provenance(matrix, TESTS, git)
        self.assertIn(('merge-base', '--is-ancestor', commit, 'HEAD'), calls)
        git, _ = self.git(set(), {(commit, path): body})
        with self.assertRaisesRegex(qualification.MatrixError, 'is not in this history'):
            qualification.check_provenance(matrix, TESTS, git)
        git, _ = self.git({commit}, {(commit, path): 'class PendingReviews(Fixture):\n    pass\n'})
        with self.assertRaisesRegex(qualification.MatrixError, 'not present in any listed merge commit'):
            qualification.check_provenance(matrix, TESTS, git)
        # A same-named method of another class is not the cited test.
        elsewhere = 'class PendingReviews(Fixture):\n    pass\n\n\nclass Other(Fixture):\n' + ''.join(
            f'    def {TESTS[ref]["name"]}(self):\n        pass\n' for ref in defect['tests'])
        git, _ = self.git({commit}, {(commit, path): elsewhere})
        with self.assertRaisesRegex(qualification.MatrixError, 'not present in any listed merge commit'):
            qualification.check_provenance(matrix, TESTS, git)


class Admission(unittest.TestCase):
    def test_every_required_case_is_listed_once(self):
        missing = copy.deepcopy(MATRIX)
        missing['configuration_admission'] = missing['configuration_admission'][1:]
        renamed = copy.deepcopy(MATRIX)
        renamed['configuration_admission'][0]['case'] = 'admission'
        for matrix in (missing, renamed):
            with self.assertRaisesRegex(qualification.MatrixError, 'must list exactly'):
                validate(matrix)


WORKFLOWS = {skill['name']: skill['workflow'] for skill in DESCRIPTOR['skills']}
CONFIGURATION_ROWS = ('dk-repo-onboard', 'dk-config', 'dk-preferences')


class EngineBlockers(unittest.TestCase):
    """Each installed row names the open Agent Workflow issues its own workflow graph needs."""

    def rejects(self, matrix, message):
        with self.assertRaisesRegex(qualification.MatrixError, message):
            qualification.check_engine_blockers(matrix, package.ROOT, WORKFLOWS)

    def test_committed_rows_cite_the_blockers_of_their_installed_graphs(self):
        qualification.check_engine_blockers(MATRIX, package.ROOT, WORKFLOWS)
        derived = {name: qualification.engine_blockers(package.ROOT, workflow) for name, workflow in WORKFLOWS.items()}
        self.assertEqual(set(derived), {item['skill'] for item in MATRIX['rows']})
        for name in CONFIGURATION_ROWS:
            # configure.yml runs only the entry's native session: no container job, so no state mount.
            self.assertEqual(derived[name], {151}, name)
        for name in ('dk-pr-approvers', 'dk-ci-flakes'):
            # Their own agent/run step, not the reusable native.yml, runs the session.
            self.assertEqual(derived[name], {149, 150}, name)
        for name, needed in derived.items():
            if name not in CONFIGURATION_ROWS + ('dk-pr-approvers', 'dk-ci-flakes'):
                self.assertEqual(needed, {149, 150, 152}, name)

    def test_release_lists_each_upstream_blocker_a_listed_row_cannot_run_without(self):
        blockers = dict(package.UPSTREAM_BLOCKERS)
        self.assertIn('agent-workflow#149', blockers['helper state mount'])
        self.assertIn('agent-workflow#150', blockers['plugin test container jobs'])
        self.assertIn('agent-workflow#151', blockers['native operation receipt'])
        for name in CONFIGURATION_ROWS:
            self.assertIn(name, blockers['native operation receipt'])
        status = package.inventory_status(DESCRIPTOR, ROOT, closure_digest=CHECKED['closure_digest'])
        self.assertIn('native operation receipt', [item['prerequisite'] for item in status['blockers']])

    def test_a_row_must_cite_each_blocker_of_its_graph(self):
        for name, number in (('dk-pr-approvers', 149), ('dk-ci', 150), ('dk-pr-publish', 152), ('dk-config', 151)):
            with self.subTest(name=name, number=number):
                matrix = copy.deepcopy(MATRIX)
                text = row(matrix, name)['remaining']
                self.assertIn(f'Agent Workflow #{number}', text)
                row(matrix, name)['remaining'] = text.replace(f'Agent Workflow #{number}', 'an upstream issue')
                self.rejects(matrix, rf'row {name}: remaining must cite Agent Workflow #{number} ')

    def test_a_row_cannot_blame_a_blocker_its_graph_does_not_have(self):
        matrix = copy.deepcopy(MATRIX)
        row(matrix, 'dk-preferences')['remaining'] += ' Its helpers need a state mount (Agent Workflow #149).'
        self.rejects(matrix, r'row dk-preferences: remaining cites Agent Workflow #149, which')

    def test_a_row_must_name_the_missing_release(self):
        matrix = copy.deepcopy(MATRIX)
        for text in ('Blocked by Agent Workflow #149, Agent Workflow #150 and Agent Workflow #152.',
                     'It has never run through a released engine; blocked by Agent Workflow #149, '
                     'Agent Workflow #150 and Agent Workflow #152.'):
            with self.subTest(text=text):
                row(matrix, 'dk-ci')['remaining'] = text
                self.rejects(matrix, 'row dk-ci: remaining must name the missing engine release')

    def test_an_implemented_row_has_no_release_gap_to_name(self):
        matrix = copy.deepcopy(MATRIX)
        row(matrix, 'dk-ci')['status']['parity'] = 'implemented'
        for remaining in (None, ''):
            with self.subTest(remaining=remaining):
                row(matrix, 'dk-ci')['remaining'] = remaining
                qualification.check_engine_blockers(matrix, package.ROOT, WORKFLOWS)

    def test_blockers_follow_the_graph_not_the_row_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'workflows').mkdir()
            files = {
                'plain.yml': 'jobs:\n  session:\n    steps:\n    - uses: agent/run\n',
                'helper.yml': 'jobs:\n  action:\n    container:\n      files:\n      - helpers/\n'
                              '    steps:\n    - env:\n        CF_WORKFLOWS_STATE: /cf-workflows-state\n',
                'native.yml': 'jobs:\n  session:\n    steps:\n    - uses: agent/run\n',
                'configure.yml': 'jobs:\n  session:\n    steps:\n    - uses: agent/run\n',
                'entry.yml': 'jobs:\n  help:\n    uses: workflows/helper.yml\n  run:\n    uses: workflows/native.yml\n',
                'config.yml': 'jobs:\n  session:\n    uses: workflows/configure.yml\n',
                'cycle.yml': 'jobs:\n  again:\n    uses: workflows/cycle.yml\n  help:\n    uses: workflows/helper.yml\n',
            }
            for name, text in files.items():
                (root / 'workflows' / name).write_text(text)
            self.assertEqual(qualification.engine_blockers(root, 'workflows/plain.yml'), set())
            self.assertEqual(qualification.engine_blockers(root, 'workflows/entry.yml'), {149, 150, 152})
            self.assertEqual(qualification.engine_blockers(root, 'workflows/config.yml'), {151})
            self.assertEqual(qualification.engine_blockers(root, 'workflows/cycle.yml'), {149, 150})


class LiveEvidence(unittest.TestCase):
    def test_listed_records_must_exist_and_statuses_follow_them(self):
        recorded = copy.deepcopy(MATRIX)
        recorded['live_acceptance']['github_publication']['status'] = 'recorded'
        with self.assertRaisesRegex(qualification.MatrixError, 'pending_authorization until a record'):
            validate(recorded)
        listed = copy.deepcopy(MATRIX)
        listed['live_acceptance']['github_publication'] = {'status': 'recorded', 'records': ['baselines/evidence/x.json']}
        with self.assertRaisesRegex(qualification.MatrixError, 'record baselines/evidence/x.json is missing'):
            validate(listed)
        combination = copy.deepcopy(MATRIX)
        combination['verified_combinations'] = [{'host': 'codex', 'provider': 'p', 'engine_version': ENGINE,
                                                 'record': RECORD_PATH}]
        with self.assertRaisesRegex(qualification.MatrixError, 'needs an accepted live_provider record'):
            validate(combination)

    def test_an_accepted_record_verifies_only_its_rows_and_never_releases(self):
        document = record()
        matrix = with_live_provider(MATRIX, {RECORD_PATH: document})
        accepted = validate(matrix, records={RECORD_PATH: document})
        self.assertEqual((accepted['combinations'], accepted['native_rows']), (1, {'dk-pr-fix'}))
        self.assertEqual(row(matrix, 'dk-pr-fix')['status']['live_acceptance'], 'pending')
        unlisted = copy.deepcopy(matrix)
        row(unlisted, 'dk-pr-review')['status']['native_execution'] = 'verified'
        with self.assertRaisesRegex(qualification.MatrixError, 'dk-pr-review: native_execution must be pending'):
            validate(unlisted, records={RECORD_PATH: document})
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / 'repo copy'
            (repository / 'baselines/evidence').mkdir(parents=True)
            for name in ('sources.json', 'inventory.json'):
                shutil.copy(ROOT / 'baselines' / name, repository / 'baselines' / name)
            (repository / qualification.MATRIX).write_text(json.dumps(matrix))
            (repository / RECORD_PATH).write_text(json.dumps(document))
            status = package.inventory_status(DESCRIPTOR, repository, closure_digest=CHECKED['closure_digest'])
            blockers = [item['prerequisite'] for item in status['blockers']]
            self.assertFalse(status['releasable'])
            self.assertNotIn('live acceptance: live_provider', blockers)
            self.assertNotIn('compatibility', blockers)
            self.assertIn('live acceptance: github_publication', blockers)
            self.assertIn('engine release', blockers)
            self.assertIn('28 of 29 rows lack native-execution evidence',
                          next(item['missing'] for item in status['blockers']
                               if item['prerequisite'] == 'acceptance matrix'))
            # The same record no longer counts for a different plugin closure.
            stale = package.inventory_status(DESCRIPTOR, repository, closure_digest='0' * 64)
            self.assertIn('recorded for a different plugin closure', stale['blockers'][-1]['missing'])

    def test_incomplete_unauthorized_or_stale_records_reject(self):
        cases = (
            (lambda r: r['authorization'].update(granted_by=''), 'explicit authorization'),
            (lambda r: r.pop('authorization'), 'missing field'),
            (lambda r: r['plugin'].update(closure_digest='0' * 64), 'different plugin closure'),
            (lambda r: r['engine'].update(version='0.2.1'), 'exact pinned engine version'),
            (lambda r: r['engine'].update(release='https://example.invalid/engine'), 'exact pinned engine version'),
            (lambda r: r['engine'].update(
                release='https://github.com/dk-code-factory/agent-workflow/releases/tag/v0.2.1'),
             'exact pinned engine version'),
            (lambda r: r['host'].update(name='vscode'), 'unknown host'),
            (lambda r: r['target'].update(disposable=False), 'disposable target'),
            (lambda r: r['scenarios'].pop(), 'must record exactly the scenarios'),
            (lambda r: r['scenarios'][0].update(result='failed'), 'needs a passing result'),
            (lambda r: r['scenarios'][0].update(evidence_refs=[]), 'needs a passing result'),
            (lambda r: r['cost'].update(complete=False, usd=None), 'complete measured dollar cost'),
            (lambda r: r.update(skill='dk-everything'), 'exactly one inventory row'),
            (lambda r: r.update(skill=['dk-pr-fix', 'dk-pr-review']), 'exactly one inventory row'),
            (lambda r: r['scenarios'][0].update(evidence_refs=['']), 'evidence references must be distinct'),
            (lambda r: r['scenarios'][0].update(evidence_refs=['   ']), 'evidence references must be distinct'),
            (lambda r: r['scenarios'][0].update(evidence_refs=['looked fine']), 'evidence references must be distinct'),
            (lambda r: r['scenarios'][0].update(evidence_refs=['session:1', 'session:1']),
             'evidence references must be distinct'),
            (lambda r: r.update(procedure='github_publication'), 'procedure mismatch'),
            (lambda r: r.update(transcript='...'), 'unexpected field'),
            (lambda r: r['host'].update(version=None), 'unknown host'),
            (lambda r: r['host'].update(version=1), 'unknown host'),
            (lambda r: r['plugin'].update(version=''), 'plugin version required'),
            (lambda r: r['target'].update(repository=None), 'target repository required'),
            (lambda r: r.update(recorded_at=' '), 'recorded_at required'),
            (lambda r: r['cost'].update(usd=True), 'complete measured dollar cost'),
            (lambda r: r['cost'].update(usd=-1), 'complete measured dollar cost'),
            (lambda r: r['cost'].update(usd=float('nan')), 'complete measured dollar cost'),
            (lambda r: r['cost'].update(usd=float('inf')), 'complete measured dollar cost'),
            (lambda r: r['scenarios'][0].pop('id'), 'each scenario needs a string id'),
            (lambda r: r['scenarios'][0].update(id=['x']), 'each scenario needs a string id'),
            (lambda r: r['scenarios'].append('x'), 'each scenario needs a string id'),
        )
        for change, message in cases:
            with self.subTest(message=message):
                document = record()
                change(document)
                with self.assertRaisesRegex(qualification.MatrixError, message):
                    qualification.validate_record(document, 'live_provider', inventory=INVENTORY,
                                                  closure_digest=CHECKED['closure_digest'], engine_version=ENGINE)
        with self.assertRaisesRegex(qualification.MatrixError, 'different plugin closure'):
            qualification.validate_record(record(), 'live_provider', inventory=INVENTORY, closure_digest=None,
                                          engine_version=ENGINE)

    def test_malformed_record_listings_reject_as_matrix_errors(self):
        for name, change in (('unhashable record path', lambda m: m['live_acceptance']['live_provider'].update(
                                 status='recorded', records=[['x']])),
                             ('unhashable combination record', lambda m: m.update(verified_combinations=[
                                 {'host': 'codex', 'provider': 'p', 'engine_version': ENGINE, 'record': ['x']}]))):
            with self.subTest(name):
                matrix = copy.deepcopy(MATRIX)
                change(matrix)
                with self.assertRaises(qualification.MatrixError):
                    validate(matrix, qualification.read_records(matrix, ROOT))

    def test_record_files_outside_the_repository_are_never_read(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'repo' / 'docs').mkdir(parents=True)
            (base / 'outside.json').write_text('{}')
            (base / 'repo' / 'docs' / 'inside.json').write_text('{}')
            (base / 'repo' / 'docs' / 'link.json').symlink_to(base / 'outside.json')
            listed = [str(base / 'outside.json'), '../outside.json', 'docs/../../outside.json', 'docs/link.json',
                      'docs/inside.json']
            matrix = {'live_acceptance': {'live_provider': {'records': listed}}}
            self.assertEqual(qualification.read_records(matrix, base / 'repo'), {'docs/inside.json': {}})

    def test_a_matrix_that_is_not_an_object_is_a_release_blocker(self):
        for document in ([], 'matrix', 1, {'live_acceptance': []}, {'live_acceptance': {'x': {'records': 1}}}):
            with self.subTest(document=document), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(qualification.read_records(document, directory), {})
                (Path(directory) / qualification.MATRIX).parent.mkdir(parents=True)
                (Path(directory) / qualification.MATRIX).write_text(json.dumps(document))
                [blocker] = qualification.release_blockers(
                    directory, inventory=INVENTORY, descriptor_skills=LISTED,
                    closure_digest=CHECKED['closure_digest'], engine_version=ENGINE)
                self.assertEqual(blocker['prerequisite'], 'acceptance matrix')

    def test_one_shared_run_cannot_verify_every_row(self):
        everything = [item['name'] for item in INVENTORY['skills']]
        aggregate = record()
        aggregate.pop('skill')
        aggregate['skills'] = everything
        with self.assertRaisesRegex(qualification.MatrixError, 'missing field|unexpected field'):
            validate(with_live_provider(MATRIX, {RECORD_PATH: {**aggregate, 'skill': 'dk-pr-fix'}}),
                     records={RECORD_PATH: {**aggregate, 'skill': 'dk-pr-fix'}})
        # One record per row that reuses the same observations is still one shared run.
        shared = {f'baselines/evidence/{name}.json': {**record(), 'skill': name} for name in ('dk-pr-fix', 'dk-ci')}
        with self.assertRaisesRegex(qualification.MatrixError, 'each row needs its own observations'):
            validate(with_live_provider(MATRIX, shared), records=shared)
        # Distinct observations per row verify exactly those rows and no other.
        own = {f'baselines/evidence/{name}.json': record(skill=name) for name in ('dk-pr-fix', 'dk-ci')}
        accepted = validate(with_live_provider(MATRIX, own), records=own)
        self.assertEqual(accepted['native_rows'], {'dk-pr-fix', 'dk-ci'})

    def test_each_procedure_has_its_own_scenarios(self):
        for procedure in qualification.PROCEDURES:
            with self.subTest(procedure=procedure):
                qualification.validate_record(record(procedure), procedure, inventory=INVENTORY,
                                              closure_digest=CHECKED['closure_digest'], engine_version=ENGINE)
                other = next(name for name in qualification.PROCEDURES if name != procedure)
                with self.assertRaisesRegex(qualification.MatrixError, 'must record exactly the scenarios'):
                    qualification.validate_record({**record(other), 'procedure': procedure}, procedure,
                                                  inventory=INVENTORY, closure_digest=CHECKED['closure_digest'],
                                                  engine_version=ENGINE)


class RunReportsEachTest(unittest.TestCase):
    def test_skipped_failing_and_missing_tests_never_count_as_passing(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'test_fixture_outcomes.py').write_text(textwrap.dedent('''
                import unittest

                class Outcomes(unittest.TestCase):
                    def test_passes(self):
                        pass

                    def test_fails(self):
                        self.fail('no')

                    def test_errors(self):
                        raise RuntimeError('no')

                    @unittest.skip('not here')
                    def test_skipped(self):
                        pass

                    def test_subtest_fails(self):
                        with self.subTest(case=1):
                            self.fail('no')
            '''))
            sys.path.insert(0, temp)
            try:
                outcomes = checker.run([f'test_fixture_outcomes.Outcomes.{name}' for name in (
                    'test_passes', 'test_fails', 'test_errors', 'test_skipped', 'test_subtest_fails', 'test_absent')])
            finally:
                sys.path.remove(temp)
                sys.modules.pop('test_fixture_outcomes', None)
        prefix = 'test_fixture_outcomes.Outcomes.'
        self.assertEqual(outcomes[prefix + 'test_passes'], 'passed')
        self.assertEqual(outcomes[prefix + 'test_fails'], 'failed')
        self.assertEqual(outcomes[prefix + 'test_errors'], 'error')
        self.assertEqual(outcomes[prefix + 'test_skipped'], 'skipped')
        self.assertEqual(outcomes[prefix + 'test_subtest_fails'], 'failed')
        self.assertNotEqual(outcomes[prefix + 'test_absent'], 'passed')


if __name__ == '__main__':
    unittest.main()
