"""CI failure diagnosis and bounded reruns of unrelated retryable failures (#35).

The installed helpers run in-process the way ci-pass.yml calls them: an
exact-head observation (the `ci` operation), the `ci_diagnosis` occurrence
(claim and accept, task.yml), the `ci_rerun` publication barrier and
publication.yml per item. They use a disposable repository, a fresh state
namespace, the real SQLite ledger and a fail-closed fake GitHub transport that
answers only capture, the PR's check inventory, run and job reads, attempt job
inventories, failed-step logs, an empty flake search and rerun-failed-jobs; any
other route raises, with no live fallback. Diagnosis and audience sessions are
only the reports a test hands the helpers, and sessions, reads and writes are
counted. Nothing here starts an engine, provider or host, and nothing
certifies GitHub behavior or provider adherence.
"""
import copy
from pathlib import Path
import re
import unittest

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
from prfix import app, ci, ci_retry, publishing
from prfix.core import canonical
from prfix.publication import Drift, PublicationError
from test_dk_pr_review import AGENTS
from test_publication_skills import AUDIENCE, GitHub as PublicationGitHub, Helpers, grant

INSTALL = Path(__file__).resolve().parents[1] / 'cf_workflows/workflows/dk-pr-fix'
WORKFLOW = '.github/workflows/test.yml'
MIRROR = 'Temporary failure resolving archive.ubuntu.com'
NETWORK = ('2026-11-26T12:00:00.0000000Z ##[group]Run apt-get update\n'
           f'2026-11-26T12:00:01.0000000Z E: {MIRROR}\n'
           '2026-11-26T12:00:01.0000000Z ##[error]Process completed with exit code 100.\n')
RUNNER = ('2026-11-26T12:00:00.0000000Z The runner has received a shutdown signal.\n'
          '2026-11-26T12:00:01.0000000Z ##[error]The operation was canceled.\n')
ASSERTION = ('2026-11-26T12:00:00.0000000Z --- FAIL: TestValue (0.00s)\n'
             '2026-11-26T12:00:00.0000000Z     code_test.go:7: value = 2, want 1\n')


class RecoveryGitHub(PublicationGitHub):
    """Adds the head's checks, runs, attempt jobs, failed-step logs and rerun-failed-jobs."""

    def __init__(self, head, target, **kwargs):
        super().__init__(head, target, **kwargs)
        self.head = head
        self.checks, self.runs, self.jobs, self.logs = [], {}, {}, {}

    def run(self, ident, jobs, attempt=1, conclusion='failure', shown=None):
        """A completed run attempt with {job_id: (name, conclusion, logs)}; ``shown`` jobs get checks."""
        self.runs[ident] = {'id': ident, 'run_attempt': attempt, 'head_sha': self.head, 'status': 'completed',
                            'conclusion': conclusion, 'path': WORKFLOW + '@refs/pull/7/merge', 'name': 'Test'}
        self.jobs[(ident, attempt)] = []
        for job_id, (name, result, logs) in jobs.items():
            self.jobs[(ident, attempt)].append({'id': job_id, 'run_id': ident, 'run_attempt': attempt,
                                                'head_sha': self.head, 'name': name, 'status': 'completed',
                                                'conclusion': result})
            self.logs[(ident, job_id, attempt)] = logs
            if shown is None or job_id in shown:
                self.checks = [check for check in self.checks if check['job_id'] != str(job_id)]
                self.checks.append({'id': str(9000 + job_id), 'name': name, 'status': 'completed',
                                    'conclusion': result, 'url': '', 'run_id': str(ident),
                                    'job_id': str(job_id), 'attempt': attempt})

    def observe_ci(self, pr, head):
        self.calls['observe_ci'] += 1
        return {'head': head, 'checks': sorted(copy.deepcopy(self.checks), key=lambda c: c['id']),
                'evidence': repr(sorted(map(repr, self.checks)))}

    def failure_logs(self, pr, run, job, attempt):
        self.calls['logs'] += 1
        value = self.logs[(int(run), int(job), attempt)]
        if isinstance(value, Exception):
            raise value
        return value

    def pages(self, path, key=None):
        if path.startswith('search/issues?q=') and key == 'items':
            self.calls['search'] += 1
            return []  # No flake declaration: learning accepts nothing.
        raise AssertionError('unconfigured fixture route: pages ' + path)

    def request(self, method, path, payload=None):
        match = re.fullmatch(r'repos/owner/repo/actions/(runs|jobs)/(\d+)(/rerun-failed-jobs|'
                             r'/attempts/(\d+)/jobs\?per_page=100)?', path)
        if not match:
            return super().request(method, path, payload)
        self.calls['actions'] += 1
        ident = int(match[2])
        if match[1] == 'jobs' and method == 'GET' and match[3] is None:
            job = next(job for jobs in self.jobs.values() for job in jobs if job['id'] == ident)
            return copy.deepcopy(job)
        run = self.runs[ident]
        if match[3] == '/rerun-failed-jobs' and method == 'POST':
            self.calls['write'] += 1
            self.calls['rerun'] += 1
            if self.fault == 'before':
                raise OSError('fixture response lost before effect')
            run.update(run_attempt=run['run_attempt'] + 1, status='queued', conclusion=None)
            self._lose()
            return {}
        if method != 'GET':
            raise AssertionError(f'unconfigured fixture route: {method} {path}')
        if match[3] is None:
            return copy.deepcopy(run)
        jobs = self.jobs[(ident, int(match[4]))]
        return {'total_count': len(jobs), 'jobs': copy.deepcopy(jobs)}


class Recovery(Helpers):
    RUN = 'pr_recovery'

    def setUp(self):
        super().setUp()
        self.github = RecoveryGitHub(self.commit, self.target)
        self.github.git_head = self.commit
        self.github.evidence['metadata']['user'] = {'id': 1, 'login': 'maintainer'}  # The actor's own PR.
        self.observations = 0

    def maintained(self, authorizations=None, permissions=None, run=None, limits=None):
        authorizations = {'rerun_ci': grant('rerun')} if authorizations is None else authorizations
        permissions = {'rerun_ci': True} if permissions is None else permissions
        result, plan = self.plan([{'stage': 'ci'}], [], authorizations, permissions, 'fix', run or self.RUN)
        if limits:
            plan['limits'].update(limits)
            plan['options'].setdefault('limits', {}).update(limits)
        return result, plan, self.ledger(plan)

    def observe(self, plan, ledger):
        """ci-poll.yml: the installed `ci` operation, retained like action.py retains it."""
        self.observations += 1
        operation = f'ci-{self.observations}'
        observed = ci.observe(self.github, plan, ledger, operation)
        with ledger.transaction():
            ledger.db.execute('INSERT INTO ci_observations VALUES(?,?,?,?)',
                              (operation, observed['head'], observed['status'], canonical(observed).decode()))
        return observed

    def claim(self, plan, ledger):
        return app.prepare_claim(self.root, self.github, ledger, plan, {'task': ci_retry.TASK})

    @staticmethod
    def verdicts(handoff, overrides=None):
        """A diagnosis of every handed-off job: unrelated and retryable unless overridden by job ID."""
        overrides = overrides or {}
        runs = []
        for run in handoff['runs']:
            jobs = []
            for job in run['jobs']:
                excerpt = next((line for line in (MIRROR, 'The runner has received a shutdown signal.')
                                if job['logs'] and line in job['logs']), None)
                entry = {'job_id': job['job_id'], 'verdict': 'unrelated', 'retryable': True,
                         'reason': 'The failure happened before project code ran.',
                         'change_comparison': 'The PR changes only code.py; it changes no workflow, host or package source.',
                         'log_evidence': [excerpt] if excerpt else []}
                entry.update(overrides.get(job['job_id'], {}))
                jobs.append(entry)
            runs.append({'run_id': run['run_id'], 'attempt': run['attempt'], 'jobs': jobs})
        return {'capture': handoff['capture'], 'runs': runs}

    def diagnose(self, plan, ledger, overrides=None, agent=AGENTS[3], diagnosis=None, **report):
        """ci-pass.yml diagnose: claim, one session and acceptance."""
        claimed = self.claim(plan, ledger)
        self.assertTrue(claimed['invoke'])
        handoff = claimed['task_input']['ci_diagnosis']
        self.native['diagnosis'] += 1
        values = {'deliverables': {'diagnosis': diagnosis or self.verdicts(handoff, overrides)}, **report}
        accepted = app.accept(self.root, self.github, ledger, plan,
                              {'claim': claimed['claim'], 'agent': agent, 'report': self.report(**values), 'round': 0})
        return handoff, accepted

    def recovery(self, plan, ledger):
        """ci-pass.yml recovery: publication-barrier.yml with selector ci_rerun, then publication.yml per item."""
        listing = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ci_rerun'})
        return listing, [self.publish(plan, ledger, item) for item in listing['items']]

    def network_and_test_failures(self):
        # Run 31: a mirror outage, plus a runner loss in a job that no displayed check names.
        self.github.run(31, {311: ('Test', 'failure', NETWORK), 312: ('Lint', 'failure', RUNNER),
                             313: ('Build', 'success', '')}, shown={311, 313})
        # Run 32: an assertion the PR's change causes.
        self.github.run(32, {321: ('Unit', 'failure', ASSERTION)})

    # -- Diagnosis and automatic reruns -------------------------------------------------------------

    def test_only_unrelated_retryable_runs_rerun_once_through_the_publisher(self):
        self.network_and_test_failures()
        result, plan, ledger = self.maintained()
        self.assertTrue(result['recovery_authorized'])
        [stage] = plan['stages']
        self.assertTrue(stage['recovery'])
        task = app._task(plan, ci_retry.TASK)
        self.assertEqual((task['definition'], task['kind'], task['writes'], task['required']),
                         ('leaf', 'task', False, False))
        self.assertTrue(task['assignment'].startswith(ci_retry.ROLE_PROMPT))
        self.assertTrue(self.observe(plan, ledger)['failed'])
        handoff, accepted = self.diagnose(plan, ledger, {321: {
            'verdict': 'caused_by_pr', 'retryable': False, 'reason': 'TestValue asserts the old value.',
            'change_comparison': 'code.py changes value from 1 to 2.', 'log_evidence': []}})
        self.assertTrue(accepted['complete'], accepted.get('feedback'))
        # Every failed job of each exact attempt, including one no displayed check names.
        self.assertEqual([(run['run_id'], [job['job_id'] for job in run['jobs']]) for run in handoff['runs']],
                         [(31, [311, 312]), (32, [321])])
        self.assertIn(MIRROR, handoff['runs'][0]['jobs'][0]['logs'])
        listing, receipts = self.recovery(plan, ledger)
        self.assertTrue(listing['rerun'])
        self.assertEqual([item['payload']['run_id'] for item in listing['items']], [31])
        self.assertEqual(listing['blockers'], ['workflow run 32 attempt 1: a PR-caused failure needs repair'])
        self.assertEqual(receipts[0]['receipt']['kind'], 'ci_rerun')
        self.assertEqual((self.github.runs[31]['run_attempt'], self.github.runs[32]['run_attempt']), (2, 1))
        self.assertEqual(self.github.calls['rerun'], 1)
        # Relisting only reconciles the applied rerun; nothing is sent again.
        again, _ = self.recovery(plan, ledger)
        self.assertEqual((self.github.calls['rerun'], self.writes()), (1, 1))
        self.assertEqual([item['id'] for item in again['items']], [item['id'] for item in listing['items']])
        reserved = ledger.db.execute("SELECT operation FROM reservations WHERE name=?",
                                     ('ci-reruns:' + self.commit,)).fetchall()
        self.assertEqual([row[0] for row in reserved], ['run:31:1'])
        self.assertEqual((self.native['diagnosis'], self.native['audience']), (1, 1))
        # The rerun proves only that a retry occurred: the next observation sees it pending, so the
        # earlier diagnosis no longer covers the inputs and nothing more is listed from it.
        self.github.checks = [dict(check, status='in_progress', conclusion=None, attempt=2)
                              if check['run_id'] == '31' else check for check in self.github.checks]
        self.assertFalse(self.observe(plan, ledger)['ready'])
        after = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ci_rerun'})
        self.assertEqual((after['items'], after['rerun']),
                         ([], False))
        self.assertEqual(after['blockers'], ['no current CI failure diagnosis of the latest exact-head observation'])

    def test_independent_runs_proceed_while_unreadable_or_unsupported_ones_stay_failed(self):
        self.github.run(31, {311: ('Test', 'failure', NETWORK)})
        self.github.run(33, {331: ('Deploy', 'failure', OSError('logs expired'))})
        self.github.run(34, {341: ('Other', 'failure', NETWORK)})
        self.github.runs[34]['run_attempt'] = 2  # Already rerun by someone else: not this exact attempt.
        self.github.checks.append({'id': 'status:7', 'name': 'external/ci', 'status': 'completed',
                                   'conclusion': 'failure', 'url': '', 'run_id': '', 'job_id': '', 'attempt': 0})
        _, plan, ledger = self.maintained()
        self.observe(plan, ledger)
        handoff, accepted = self.diagnose(plan, ledger, {331: {'verdict': 'ambiguous', 'retryable': False,
                                                               'reason': 'Its failed-step logs are unavailable.'}})
        self.assertTrue(accepted['complete'], accepted.get('feedback'))
        self.assertEqual(handoff['runs'][1]['jobs'][0]['logs'], None)
        self.assertEqual(handoff['runs'][1]['jobs'][0]['gap'], 'logs expired')
        listing, _ = self.recovery(plan, ledger)
        self.assertEqual([item['payload']['run_id'] for item in listing['items']], [31])
        self.assertEqual(listing['blockers'], [
            'workflow run 33 attempt 1: an ambiguous or non-retryable failure stays failed',
            'workflow run 34 attempt 1: workflow run is no longer this failed exact attempt',
            'check external/ci: not an Actions job; it cannot be rerun'])
        self.assertEqual(self.github.calls['rerun'], 1)

    def test_the_session_is_held_to_the_handed_off_evidence(self):
        self.github.run(31, {311: ('Test', 'failure', NETWORK), 312: ('Lint', 'failure', RUNNER)})
        self.github.run(33, {331: ('Deploy', 'failure', OSError('logs expired'))})
        _, plan, ledger = self.maintained()
        self.observe(plan, ledger)
        claimed = self.claim(plan, ledger)
        handoff = claimed['task_input']['ci_diagnosis']
        good = self.verdicts(handoff, {331: {'verdict': 'ambiguous', 'retryable': False}})
        cases = {
            'verbatim': self.verdicts(handoff, {311: {'log_evidence': ['DNS was flaky']},
                                                331: {'verdict': 'ambiguous', 'retryable': False}}),
            'unavailable': self.verdicts(handoff, {331: {'log_evidence': []}}),
            'only an unrelated': self.verdicts(handoff, {311: {'verdict': 'caused_by_pr'},
                                                         331: {'verdict': 'ambiguous', 'retryable': False}}),
            'needs its own log excerpts': self.verdicts(handoff, {311: {'log_evidence': []},
                                                                  331: {'verdict': 'ambiguous', 'retryable': False}}),
            'omits a failed job': {**good, 'runs': [{**good['runs'][0], 'jobs': good['runs'][0]['jobs'][:1]},
                                                    good['runs'][1]]},
            'omits a handed-off failed run': {**good, 'runs': good['runs'][:1]},
            'not one handed-off failed attempt': {**good, 'runs': [*good['runs'], {**good['runs'][0], 'attempt': 2}]},
            'capture differs': {**good, 'capture': 'sha256:' + '0' * 64},
            'verdict is unrelated': self.verdicts(handoff, {311: {'verdict': 'flaky'},
                                                            331: {'verdict': 'ambiguous', 'retryable': False}}),
            # code-scan #22: unhashable or merely equal identities are corrected, not raised or accepted.
            'run_id and attempt': {**good, 'runs': [{**good['runs'][0], 'run_id': [31]}, good['runs'][1]]},
            'must be the handed-off integers': {**good, 'runs': [
                {**good['runs'][0], 'attempt': float(good['runs'][0]['attempt'])}, good['runs'][1]]},
            'job_id must be': {**good, 'runs': [{**good['runs'][0], 'jobs': [
                {**good['runs'][0]['jobs'][0], 'job_id': [311]}, *good['runs'][0]['jobs'][1:]]}, good['runs'][1]]},
        }
        for message, diagnosis in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ci_retry.Blocked, message):
                    ci_retry.check(self.report(deliverables={'diagnosis': diagnosis}), {'ci_diagnosis': handoff})
        with self.assertRaisesRegex(ci_retry.Blocked, 'proposes no operations'):
            ci_retry.check(self.report(deliverables={'diagnosis': good}, operations=[{'kind': 'ci_rerun'}]),
                           {'ci_diagnosis': handoff})
        self.assertEqual(ci_retry.check(self.report(deliverables={'diagnosis': good}), {'ci_diagnosis': handoff}), good)
        # Through acceptance, a violation is a same-session correction, never coverage.
        rejected = app.accept(self.root, self.github, ledger, plan, {
            'claim': claimed['claim'], 'agent': AGENTS[3], 'round': 0,
            'report': self.report(deliverables={'diagnosis': cases['unavailable']})})
        self.assertFalse(rejected['complete'])
        self.assertIn('CI diagnosis: run 33 job 331', rejected['feedback']['contract_error'])
        self.assertEqual(publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ci_rerun'})['items'],
                         [])
        corrected = app.accept(self.root, self.github, ledger, plan, {
            'claim': claimed['claim'], 'agent': AGENTS[3], 'round': 1,
            'report': self.report(deliverables={'diagnosis': good})})
        self.assertTrue(corrected['complete'])
        self.assertEqual(self.writes(), 0)

    def test_a_newer_observation_or_head_makes_a_prepared_rerun_stale_before_dispatch(self):
        self.github.run(31, {311: ('Test', 'failure', NETWORK)})
        _, plan, ledger = self.maintained()
        self.observe(plan, ledger)
        self.diagnose(plan, ledger)
        [item] = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ci_rerun'})['items']
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {'proposal': item})
        self.observe(plan, ledger)  # A newer observation of the same failure.
        verdict = self.report(audience={'fingerprint': prepared['fingerprint'], 'suitable': True,
                                        'audience': 'Repository maintainers', 'evidence': ['code']})
        with self.assertRaises(Drift):
            publishing.publication_execute(self.root, self.github, ledger, plan,
                                           {'id': prepared['id'], 'agent': AUDIENCE[0], 'report': verdict})
        state = ledger.db.execute('SELECT state FROM publication_intents WHERE id=?', (prepared['id'],)).fetchone()[0]
        self.assertEqual((state, self.writes()), ('blocked', 0))
        # A proposal the current diagnosis does not list is never prepared.
        forged = {**item, 'id': 'ci_rerun_' + '0' * 64}
        with self.assertRaisesRegex(PublicationError, 'not a current diagnosed rerun'):
            publishing.publication_prepare(self.root, self.github, ledger, plan, {'proposal': forged})
        # A fresh diagnosis of the newer observation lists the run again, under a new identity.
        self.diagnose(plan, ledger, agent=AGENTS[4])
        listing, receipts = self.recovery(plan, ledger)
        self.assertNotEqual(listing['items'][0]['id'], item['id'])
        self.assertEqual((len(receipts), self.github.calls['rerun']), (1, 1))

    def test_lost_rerun_responses_are_reconciled_and_never_resent(self):
        for fault in ('before', 'after'):
            with self.subTest(fault=fault):
                self.github.calls.clear()
                self.github.run(31, {311: ('Test', 'failure', NETWORK)})
                _, plan, ledger = self.maintained(run='pr_' + fault)
                self.observe(plan, ledger)
                self.diagnose(plan, ledger)
                self.github.fault = fault
                with self.assertRaises(OSError):
                    self.recovery(plan, ledger)
                self.github.fault = None
                if fault == 'before':
                    for _ in range(2):
                        listing = publishing.publication_list(self.root, self.github, ledger, plan,
                                                              {'kind': 'ci_rerun'})
                        self.assertEqual((len(listing['items']), listing['rerun']), (1, False))
                        with self.assertRaisesRegex(PublicationError, 'unresolved'):
                            self.recovery(plan, ledger)
                else:
                    _, [receipt] = self.recovery(plan, ledger)
                    self.assertEqual(receipt['receipt']['kind'], 'ci_rerun')
                self.assertEqual(self.github.calls['rerun'], 1)

    # -- Allowances ---------------------------------------------------------------------------------

    def rerun_again(self, plan, ledger, run=31, attempt=2):
        """The rerun attempt fails the same way; a fresh observation and diagnosis follow."""
        self.github.run(run, {311: ('Test', 'failure', NETWORK)}, attempt=attempt)
        self.observe(plan, ledger)
        self.diagnose(plan, ledger, agent=AGENTS[3 + attempt])

    def user_retry(self, plan, ledger, authority, attempt):
        """dk-pr-publish's prepare-publication proposes the user's explicit retry of the exact attempt."""
        source = app._current(ledger, app._snapshot(self.root, self.github, plan), app._task(plan, ci_retry.TASK))
        proposal = {'id': f'user-retry-{attempt}', 'kind': 'ci_rerun', 'head': self.commit, 'target': self.target,
                    'payload': {'run_id': 31, 'attempt': attempt, 'reason': 'The user asked to retry Test once.',
                                'user_retry': authority},
                    'sources': [source['report']]}
        prepared = publishing._publication(self.root, self.github, ledger, plan).prepare(proposal)
        verdict = self.report(audience={'fingerprint': prepared['fingerprint'], 'suitable': True,
                                        'audience': 'Repository maintainers', 'evidence': ['code']})
        return publishing.publication_execute(self.root, self.github, ledger, plan,
                                              {'id': prepared['id'], 'agent': AUDIENCE[5 + attempt], 'report': verdict})

    def test_the_per_head_allowance_is_retained_and_a_user_retry_is_one_exact_attempt(self):
        self.github.run(31, {311: ('Test', 'failure', NETWORK)})
        _, plan, ledger = self.maintained(limits={'ci_reruns_per_head': 1})
        self.observe(plan, ledger)
        self.diagnose(plan, ledger)
        self.recovery(plan, ledger)
        self.rerun_again(plan, ledger)
        # The automatic allowance is spent at this head, in this and in a new ledger connection.
        for current in (ledger, self.ledger(plan)):
            listing = publishing.publication_list(self.root, self.github, current, plan, {'kind': 'ci_rerun'})
            self.assertEqual((listing['items'], listing['rerun']), ([], False))
            self.assertEqual(listing['blockers'], ['workflow run 31 attempt 2: the automatic rerun allowance of this '
                                                   'head is exhausted; a bounded explicit user retry is required'])
        # The user's explicit retry of this exact attempt runs once, beside the spent allowance.
        authority = plan['authorizations']['rerun_ci']
        with self.assertRaisesRegex(PublicationError, 'retained explicit rerun_ci authorization'):
            self.user_retry(plan, ledger, {**authority, 'request_id': 'another-request'}, 2)
        self.user_retry(plan, ledger, authority, 2)
        self.assertEqual((self.github.runs[31]['run_attempt'], self.github.calls['rerun']), (3, 2))
        names = sorted(row[0] for row in ledger.db.execute('SELECT name FROM reservations'))
        self.assertEqual(names, ['ci-reruns:' + self.commit, 'ci-user-retry:rerun:31'])
        # The same request identity never binds another attempt of the run.
        self.rerun_again(plan, ledger, attempt=3)
        with self.assertRaisesRegex(PublicationError, 'already binds another exact attempt'):
            self.user_retry(plan, ledger, authority, 3)
        self.assertEqual(self.github.calls['rerun'], 2)

    # -- Authority ----------------------------------------------------------------------------------

    def test_without_grant_permission_or_fix_mode_nothing_is_diagnosed_or_rerun(self):
        self.github.run(31, {311: ('Test', 'failure', NETWORK)})
        for name, authorizations, permissions in (('no grant', {}, {'rerun_ci': True}),
                                                  ('no permission', {'rerun_ci': grant('rerun')}, {})):
            with self.subTest(name=name):
                result, plan, ledger = self.maintained(authorizations, permissions, run='pr_' + name.replace(' ', '_'))
                self.assertFalse(result['recovery_authorized'])
                self.assertNotIn(ci_retry.TASK, [task['id'] for task in plan['tasks']])
                self.assertNotIn('recovery', plan['stages'][0])
                self.observe(plan, ledger)
                listing = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ci_rerun'})
                self.assertEqual(listing['items'], [])
        # Another action's grant never stands in for rerun_ci.
        _, plan, ledger = self.maintained({'push': grant('push')}, {'rerun_ci': True, 'push_commits': True},
                                          run='pr_push')
        self.assertNotIn(ci_retry.TASK, [task['id'] for task in plan['tasks']])
        # Incoming review never diagnoses or reruns CI.
        _, review = self.plan([{'stage': 'reviews'}], ['review'], {}, {}, 'review', 'review_recovery')
        with self.assertRaisesRegex(PublicationError, 'incoming review never reruns CI'):
            publishing.publication_list(self.root, self.github, self.ledger(review), review, {'kind': 'ci_rerun'})
        self.assertEqual((self.writes(), self.native['diagnosis']), (0, 0))

    def test_a_successful_observation_needs_no_diagnosis(self):
        self.github.run(31, {311: ('Test', 'success', '')}, conclusion='success')
        _, plan, ledger = self.maintained()
        self.assertTrue(self.observe(plan, ledger)['ready'])
        with self.assertRaisesRegex(ValueError, 'current exact-head failed observation'):
            self.claim(plan, ledger)
        self.assertEqual(self.github.calls['logs'], 0)

    def test_nothing_diagnosable_settles_without_a_session_and_leaves_the_failure_to_repair(self):
        status = {'id': 'status:7', 'name': 'external/ci', 'status': 'completed', 'conclusion': 'failure',
                  'url': '', 'run_id': '', 'job_id': '', 'attempt': 0}

        def moved():
            self.github.run(31, {311: ('Test', 'failure', NETWORK)})
            self.github.runs[31]['run_attempt'] = 2  # Already rerun by someone else.

        def many():
            for ident in range(41, 41 + ci_retry.MAX_RUNS + 1):
                self.github.run(ident, {ident * 10: ('Test', 'failure', NETWORK)})

        for name, arrange, reason in (('status only', lambda: self.github.checks.append(status), 'not an Actions job'),
                                      ('moved attempt', moved, 'no longer this failed exact attempt'),
                                      ('too many runs', many, 'too many failed workflow runs')):
            with self.subTest(name=name):
                self.github.checks, self.github.runs, self.github.jobs = [], {}, {}
                arrange()
                _, plan, ledger = self.maintained(run='pr_' + name.replace(' ', '_'))
                self.assertTrue(self.observe(plan, ledger)['failed'])
                claimed = self.claim(plan, ledger)
                self.assertEqual((claimed['invoke'], claimed['complete']), (False, False))
                assignment = app.read_artifact(self.root, app._claim(ledger, claimed['claim'])['assignment'])
                self.assertIn(reason, assignment['ci_diagnosis_blocked'])
                # task.yml status succeeds with complete false, so ci-pass.yml skips recovery ...
                status_result = app.task_status(self.root, self.github, ledger, plan, {'claim': claimed['claim']})
                self.assertFalse(status_result['complete'])
        # ... and the failure reaches ci_repair exactly as without the grant.
        repair = app.prepare_claim(self.root, self.github, ledger, plan, {'task': 'ci_repair', 'cycle': 1})
        self.assertTrue(repair['invoke'])
        self.assertEqual(repair['task_input']['ci']['status'], 'failed')
        self.assertEqual((self.native['diagnosis'], self.writes()), (0, 0))

    def test_an_invalid_rerun_allowance_is_listed_as_a_blocker(self):
        self.github.run(31, {311: ('Test', 'failure', NETWORK)})
        _, plan, ledger = self.maintained()
        self.observe(plan, ledger)
        self.diagnose(plan, ledger)
        for value in (True, -1, 4, '3'):
            with self.subTest(value=value):
                plan['options'].setdefault('limits', {})['ci_reruns_per_head'] = value
                listing = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ci_rerun'})
                self.assertEqual((listing['items'], listing['rerun'], listing['blockers']),
                                 ([], False, ['invalid retained per-head CI rerun allowance']))
        self.assertEqual(self.github.calls['rerun'], 0)

    # -- Installed graph ----------------------------------------------------------------------------

    def test_ci_pass_diagnoses_reruns_and_repairs_only_when_nothing_was_rerun(self):
        root = INSTALL / 'workflows'
        body = (root / 'ci-pass.yml').read_text()
        jobs = dict(re.findall(r'(?m)^  ([a-z_]+):\n((?:    .*\n|\s*\n)*)', body.split('\njobs:\n', 1)[1]))
        self.assertEqual(list(jobs), ['poll', 'diagnose', 'recovery', 'repair', 'publish'])
        self.assertIn('task: ci_diagnosis', jobs['diagnose'])
        self.assertIn('if: ${{ needs.poll.outputs.failed and inputs.recovery }}', jobs['diagnose'])
        self.assertIn('uses: workflows/publication-barrier.yml', jobs['recovery'])
        self.assertIn('selector: ci_rerun', jobs['recovery'])
        self.assertIn("needs.diagnose.status == 'succeeded' and needs.diagnose.outputs.complete", jobs['recovery'])
        self.assertIn('needs.recovery.outputs.rerun != true', jobs['repair'])
        self.assertIn('rerun:\n        # ci_rerun only', (root / 'publication-barrier.yml').read_text())
        self.assertIn('recovery: ${{ inputs.stage.recovery == true }}', (root / 'stage.yml').read_text())


if __name__ == '__main__':
    unittest.main()
