"""CI entries: cf-workflows:dk-ci, dk-ci-flakes and dk-ci-approve (#35).

Preflight cases use the fail-closed fake engine transport of the dk-pr-fix
tests (only the capability query is configured). Helper cases run the installed
helpers against the real SQLite ledger in a fresh state namespace and
disposable repositories, with fail-closed fake GitHub transports: each answers
only the routes a case configures and raises on anything else, with no live
fallback. GitHub reads, writes, native and audience sessions are counted.
Nothing here starts an engine, provider or host, and nothing certifies GitHub
behavior or provider adherence.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import ci, publishing
from prfix.capture import identity
from prfix.core import Ledger
from prfix.prepare import prepare
from prfix.publication import SUPPORTED, Drift, PublicationError
from test_publication_skills import AUDIENCE, GitHub as PublicationGitHub, Helpers, grant

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from cf_workflows import admission, ci_flakes, dk_ci, dk_pr_publish, dk_pr_review, entries  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import CAPABILITIES, FakeTransport, PreflightFixture  # noqa: E402
from test_configuration_skills import Engine, Transport as EngineTransport, session  # noqa: E402

INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}
PR = identity('https://github.com/owner/repo/pull/17')
HEAD = 'a' * 40
WORKFLOW = '.github/workflows/test.yml'
AUTHOR = 999  # The PR author: a fork contributor whose CI logs a flake declaration must not be able to waive.
MAINTAINER = {'id': 5, 'login': 'maintainer'}


# -- Entries -------------------------------------------------------------------------------------

class Entries(PreflightFixture):
    """Presets, options and rejections before any effect."""

    def configuration(self, options, permissions=None):
        scope = {'id': 'repo', 'selector': {'kind': 'repository', 'id': 'github.com/owner/repo'},
                 'options': options, 'permissions': permissions or {}, 'prompts': [], 'skills': [], 'preferences': []}
        return {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                'scopes': [scope], 'resources': [],
                'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                              'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}

    def observe(self, **changes):
        self.transport = changes.pop('transport', None) or FakeTransport()
        arguments = {'request': self.request, 'deployment': self.deployment, 'repository': self.repo,
                     'home': self.home, **changes}
        result = dk_ci.preflight(transport=self.transport, install=self.clean, **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def approve(self, authorizations, invocation=None):
        self.transport = FakeTransport()
        result = dk_pr_publish.preflight('dk-ci-approve', request={**self.request, 'authorizations': authorizations},
                                         deployment=self.deployment, repository=self.repo, home=self.home,
                                         invocation=invocation, transport=self.transport, install=self.clean)
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def test_role_prompts_are_the_pinned_baseline_bytes(self):
        prompts = {'dk-ci': (dk_ci.ENTRY['role_prompt'], dk_ci.ENTRY['baseline_prompt'], dk_ci.ENTRY['skill']),
                   'dk-ci-flakes': tuple(entries.ENTRIES['dk-ci-flakes'][key]
                                         for key in ('role_prompt', 'baseline_prompt', 'skill')),
                   'dk-ci-approve': tuple(dk_pr_publish.ENTRIES['dk-ci-approve'][key]
                                          for key in ('role_prompt', 'baseline_prompt', 'skill'))}
        for name, (prompt, path, skill) in prompts.items():
            with self.subTest(name=name):
                resources = {item['path']: item['sha256'] for item in INVENTORY[name]['baseline_resources']}
                self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), resources[path])
                self.assertEqual(INVENTORY[name]['entrypoint'], skill)
                self.assertEqual(INVENTORY[name]['parity_status'], 'not_implemented')

    def test_dk_ci_observes_only_in_the_shared_fix_run(self):
        result = self.observe()
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['semantic'], self.transport.calls), (None, Counter(capabilities=1)))
        inputs = result['inputs']
        self.assertEqual((inputs['tasks'], inputs['finalizers'], inputs['reviewers'], inputs['adapters']),
                         ([{'stage': 'ci'}], [], [], {}))
        self.assertEqual(inputs['limits']['ci_repairs_per_cycle'], 0)
        self.assertEqual(inputs['ci'], {'timeout_seconds': 1800, 'poll_interval_seconds': 30,
                                        'classification_timeout_seconds': 60, 'no_checks_grace_seconds': 60,
                                        'pending_extension_seconds': 1800, 'max_pending_extensions': 1})
        self.assertFalse(any(inputs['permissions'].values()))
        self.assertEqual((result['workflow'], result['installation']['entry']), ('cf-workflows:dk-ci',
                                                                                 'workflows/main.yml'))
        # The PR's default run: a composed dk-pr-fix observation shares its deadline and allowances.
        composed = fix_preflight.preflight(request=self.request, configuration=None, invocation=None,
                                           deployment=self.deployment, repository=self.repo, home=self.home,
                                           transport=FakeTransport(), install=self.clean)
        self.assertEqual(result['state']['request_document']['logical_run'],
                         composed['state']['request_document']['logical_run'])
        self.assertEqual(composed['inputs']['ci'], {'timeout_seconds': 1800}, 'composed default has no extension')

    def test_configured_controls_stay_and_invocation_sets_only_its_own(self):
        document = self.configuration({'ci': {'timeout_seconds': 600}, 'limits': {'ci_reruns_per_head': 1}})
        result = self.observe(configuration=document, options={'max_pending_extensions': 0})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['inputs']['ci'], {'timeout_seconds': 600, 'poll_interval_seconds': 30,
                                                  'classification_timeout_seconds': 60, 'no_checks_grace_seconds': 60,
                                                  'pending_extension_seconds': 1800, 'max_pending_extensions': 0})
        self.assertEqual((result['inputs']['limits']['ci_reruns_per_head'],
                          result['inputs']['limits']['ci_repairs_per_cycle']), (1, 0))
        self.assertEqual(result['entry']['options']['max_pending_extensions'], {'value': 0, 'source': 'invocation'})
        result = self.observe(configuration=document, options={'timeout_seconds': 900})
        self.assertEqual(result['inputs']['ci']['timeout_seconds'], 900)
        # Configured observation windows stay; an invocation option sets only its own.
        document = self.configuration({'ci': {'poll_interval_seconds': 5, 'no_checks_grace_seconds': 0}})
        result = self.observe(configuration=document, options={'classification_timeout_seconds': 120})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual({key: result['inputs']['ci'][key] for key in admission.CI_WINDOWS},
                         {'poll_interval_seconds': 5, 'classification_timeout_seconds': 120,
                          'no_checks_grace_seconds': 0})
        self.assertEqual(result['entry']['options']['poll_interval_seconds'], {'value': 30, 'source': 'default'})
        # Composed dk-pr-fix carries only the windows it is configured with.
        composed = fix_preflight.preflight(request=self.request, configuration=document, invocation=None,
                                           deployment=self.deployment, repository=self.repo, home=self.home,
                                           transport=FakeTransport(), install=self.clean)
        self.assertEqual(composed['preflight'], 'admitted', composed.get('error'))
        self.assertEqual(composed['inputs']['ci'], {'timeout_seconds': 1800, 'poll_interval_seconds': 5,
                                                    'no_checks_grace_seconds': 0})
        for key, value in (('poll_interval_seconds', 0), ('classification_timeout_seconds', 3601),
                           ('no_checks_grace_seconds', -1), ('poll_interval_seconds', True)):
            with self.subTest(key=key, value=value):
                result = self.observe(configuration=self.configuration({'ci': {key: value}}))
                self.assertEqual(result['preflight'], 'rejected')

    def test_dk_ci_rejects_authority_overrides_and_unsupported_options(self):
        cases = [
            ({'request': {**self.request, 'authorizations': {'rerun_ci': grant('r')}}}, 'request.authorizations',
             'dk-pr-publish'),
            ({'invocation': {'options': {'tasks': ['read-feedback']}}}, 'invocation', 'preset'),
            ({'invocation': {'permissions': {'rerun_ci': True}}}, 'invocation', 'preset'),
            ({'options': {'poll_interval_seconds': 0}}, 'options.poll_interval_seconds', '1..3600'),
            ({'options': {'classification_timeout_seconds': 0}}, 'options.classification_timeout_seconds', '1..3600'),
            ({'options': {'no_checks_grace_seconds': -1}}, 'options.no_checks_grace_seconds', '0..86400'),
            ({'options': {'max_pending_extensions': 2}}, 'options.max_pending_extensions', '0..1'),
            ({'options': {'timeout_seconds': 0}}, 'options.timeout_seconds', '1..86400'),
            ({'options': {'lookback_days': 3}}, 'options.lookback_days', 'unsupported option'),
        ]
        for changes, path, message in cases:
            with self.subTest(path=path, changes=changes):
                result = self.observe(**changes)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))
                self.assertIn(message, result['error']['message'])
                self.assertEqual(self.transport.calls, Counter(), 'rejected before the engine is contacted')

    def test_fork_approval_has_its_own_entry_grant_and_permission(self):
        self.assertEqual(dk_pr_publish.CAPABILITIES['approve_fork_ci'],
                         {'entry': 'dk-ci-approve', 'preset': 'fork-ci', 'kinds': ('approve_fork_ci',),
                          'permission': 'approve_fork_ci'})
        self.assertEqual(SUPPORTED['approve_fork_ci'], ('approve_fork_ci', 'approve_fork_ci'))
        result = self.approve({'approve_fork_ci': grant('fork')}, {'permissions': {'approve_fork_ci': True}})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['installation']['entry'], result['entry']['mode'], result['entry']['preset']),
                         ('workflows/review.yml', 'review', 'fork-ci'))
        self.assertEqual({k for k, v in result['inputs']['permissions'].items() if v}, {'approve_fork_ci'})
        self.assertEqual(result['state']['request_document']['logical_run'], dk_pr_review.logical_run(self.request['pr']))
        cases = [
            ({}, None, 'request.authorizations', 'explicit authorization'),
            ({'approve_fork_ci': grant('fork')}, None, 'request.authorizations.approve_fork_ci', 'not enabled'),
            ({'post_findings': grant('post')}, None, 'request.authorizations.post_findings', 'dk-pr-review-post'),
            ({'approve_pr': grant('approve')}, None, 'request.authorizations.approve_pr', 'dk-pr-review-post'),
            ({'merge': grant('merge')}, None, 'request.authorizations.merge', 'dk-pr-publish'),
            ({'approve_fork_ci': grant('fork'), 'approve_pr': grant('approve')}, None,
             'request.authorizations.approve_pr', 'dk-pr-review-post'),
        ]
        for authorizations, invocation, path, message in cases:
            with self.subTest(authorizations=sorted(authorizations)):
                result = self.approve(authorizations, invocation)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))
                self.assertIn(message, result['error']['message'])

    def test_dk_pr_fix_never_approves_fork_workflows(self):
        result = self.preflight(request={**self.request, 'authorizations': {'approve_fork_ci': grant('fork')}},
                                invocation={'permissions': {'approve_fork_ci': True}})
        self.assertEqual((result['preflight'], result['error']['path']),
                         ('rejected', 'request.authorizations.approve_fork_ci'))
        self.assertIn('incoming review', result['error']['message'])
        # Composed incoming review may carry the separate grant.
        review = dk_pr_review.preflight(request={**self.request, 'authorizations': {'approve_fork_ci': grant('fork')}},
                                        configuration=None, invocation={'permissions': {'approve_fork_ci': True}},
                                        deployment=self.deployment, repository=self.repo, home=self.home,
                                        transport=FakeTransport(), install=self.clean)
        self.assertEqual(review['preflight'], 'admitted', review.get('error'))
        self.assertTrue(review['inputs']['permissions']['approve_fork_ci'])

    def test_flake_entry_options_and_adapter_stay_in_their_entry(self):
        checked = entries.check_options('dk-ci-flakes', {'max_runs': 5})
        self.assertEqual((checked['max_runs'], checked['lookback_days']),
                         ({'value': 5, 'source': 'invocation'}, {'value': 30, 'source': 'default'}))
        for options in ({'max_runs': 0}, {'lookback_days': True}, {'max_issues': '5'}, {'days': 3}):
            with self.subTest(options=options), self.assertRaises(admission.AdmissionError):
                entries.check_options('dk-ci-flakes', options)
        adapter = admission.ENTRY_ADAPTERS['inspect-flakes']
        self.assertEqual((adapter['kind'], adapter['writes'], adapter['definition']), ('inspection', False, 'leaf'))
        result = self.preflight(invocation={'options': {'tasks': ['inspect-flakes'], 'finalizers': []}})
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'options.tasks'))


# -- Exact-head observation, deadlines and issue-backed flakes ---------------------------------------

def declaration(test='TestFlaky', signature='intermittent gateway timeout', job='Test', workflow=WORKFLOW):
    body = {'workflow': workflow, 'job': job, 'test': test, 'signature': signature, 'flaky': True}
    return 'Known flake.\n```dk-flaky\n' + json.dumps(body) + '\n```\n'


class CIGitHub:
    """Capture, CI, Actions runs, jobs, failed-step logs, issues and issue search; nothing else."""

    def __init__(self):
        self.head = HEAD
        self.calls = Counter()
        self.checks, self.runs, self.jobs, self.logs, self.issues = [], {}, {}, {}, {}
        self.unavailable = set()

    def failed(self, check_id, run_id, job_id, attempt=1, lines=None, head=None):
        """A failed Actions check of the exact attempt with its run, job and failed-step logs."""
        head = head or self.head
        self.checks.append({'id': str(check_id), 'name': 'Test', 'status': 'completed', 'conclusion': 'failure',
                            'run_id': str(run_id), 'job_id': str(job_id), 'attempt': attempt})
        self.runs[run_id] = {'id': run_id, 'head_sha': head, 'run_attempt': attempt,
                             'path': WORKFLOW + '@refs/pull/17/merge', 'created_at': '2026-11-26T12:00:00Z'}
        self.jobs[job_id] = {'id': job_id, 'head_sha': head, 'run_id': run_id, 'run_attempt': attempt, 'name': 'Test',
                             'status': 'completed', 'conclusion': 'failure',
                             'check_run_url': f'https://api.github.com/repos/owner/repo/check-runs/{check_id}',
                             'steps': [{'name': 'Tests', 'number': 2, 'status': 'completed', 'conclusion': 'failure'}]}
        lines = lines or ['=== RUN   TestFlaky', '    client.go:17: intermittent gateway timeout',
                          '--- FAIL: TestFlaky (0.10s)', 'FAIL', 'exit status 1']
        self.logs[(str(run_id), str(job_id), attempt)] = '\n'.join('Test\tTests\t' + line for line in lines)

    def issue(self, number, body, state='open', repository='owner/repo', user=None, association='MEMBER',
              created='2026-01-01T00:00:00Z'):
        self.issues[number] = {'number': number, 'state': state, 'title': 'Flaky test', 'body': body,
                               'html_url': f'https://github.com/{repository}/issues/{number}',
                               'user': dict(user or MAINTAINER), 'author_association': association,
                               'created_at': created}

    def capture(self, pr):
        self.calls['capture'] += 1
        return {'metadata': {'head': {'sha': self.head}}}

    def observe_ci(self, pr, head):
        self.calls['observe_ci'] += 1
        if 'observe_ci' in self.unavailable:
            raise ConnectionError('observation interrupted')
        return {'head': head, 'checks': copy.deepcopy(self.checks), 'evidence': 'fixture-ci'}

    def request(self, method, path, payload=None):
        self.calls['request'] += 1
        match = re.fullmatch(r'repos/owner/repo/(actions/runs|actions/jobs|issues)/(\d+)', path)
        if method != 'GET' or not match:
            raise AssertionError(f'unconfigured fixture route: {method} {path}')
        table = {'actions/runs': self.runs, 'actions/jobs': self.jobs, 'issues': self.issues}[match[1]]
        self.calls[match[1]] += 1
        return copy.deepcopy(table[int(match[2])])

    def failure_logs(self, pr, run, job, attempt):
        self.calls['logs'] += 1
        if (run, job, attempt) not in self.logs or 'logs' in self.unavailable:
            raise OSError('failed-step logs expired')
        return self.logs[(run, job, attempt)]

    def pages(self, path, key=None):
        self.calls['search'] += 1
        if 'search' in self.unavailable:
            raise RuntimeError('search unavailable')
        match = re.fullmatch(r'search/issues\?q=(.+)', path)
        if not match or key != 'items':
            raise AssertionError('unconfigured fixture route: pages ' + path)
        query = unquote(match[1])
        test = re.search(r'"([^"]+)"', query)[1]
        self.assertion = query
        return [copy.deepcopy(issue) for issue in self.issues.values() if test in issue['body']]


class Observation(unittest.TestCase):
    def setUp(self):
        git_fixture.state(self)
        temp = tempfile.TemporaryDirectory(prefix='cfw ci ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.github = CIGitHub()
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])

    def ledger(self, run='pr_shared'):
        ledger = Ledger(self.root, run, PR['url'])
        self.ledgers.append(ledger)
        return ledger

    @staticmethod
    def plan(ci_controls=None, run='pr_shared', **options):
        return {'pr': PR, 'logical_run': run, 'snapshot': {'author_id': AUTHOR},
                'options': {'ci': ci_controls or {}, **options}}

    @staticmethod
    def direct():
        # dk-ci's admitted controls (Entries.test_dk_ci_observes_only_in_the_shared_fix_run), shortened.
        return {'timeout_seconds': 120, 'pending_extension_seconds': 600, 'max_pending_extensions': 1}

    def pending(self):
        self.github.checks = [{'id': '9', 'name': 'Test', 'status': 'in_progress', 'conclusion': None,
                               'run_id': '', 'job_id': '', 'attempt': 0}]

    def at(self, moment):
        return [patch('prfix.ci.time.time', return_value=moment), patch('prfix.core.time.time', return_value=moment)]

    def observe(self, plan, operation, moment=None, ledger=None):
        ledger = ledger or self.ledger(plan['logical_run'])
        if moment is None:
            return ci.observe(self.github, plan, ledger, operation)
        first, second = self.at(moment)
        with first, second:
            return ci.observe(self.github, plan, ledger, operation)

    def grants(self, run='pr_shared'):
        return self.ledger(run).db.execute("SELECT count(*) FROM reservations WHERE name=?",
                                           ('ci-pending-extension:' + run,)).fetchone()[0]

    # -- deadlines -----------------------------------------------------------------------------

    def test_direct_and_composed_observation_share_one_window_and_never_renew_it(self):
        self.pending()
        first = self.observe(self.plan(self.direct()), 'direct-1', 1000.0)
        self.assertEqual((first['status'], first['deadline'], first['extended']), ('pending', 1120.0, False))
        # A composed dk-pr-fix observation of the same run, resumed later on a new head with a larger timeout.
        self.github.head = 'b' * 40
        composed = self.observe(self.plan({'timeout_seconds': 3600}), 'composed-1', 1100.0)
        self.assertEqual((composed['deadline'], composed['head']), (1120.0, 'b' * 40))
        # Without a configured extension the composed run stops at the original end, without observing.
        before = self.github.calls['observe_ci']
        with self.assertRaisesRegex(TimeoutError, 'deadline exhausted'):
            self.observe(self.plan({'timeout_seconds': 3600}), 'composed-2', 1121.0)
        self.assertEqual(self.github.calls['observe_ci'], before)
        self.assertEqual(self.ledger().db.execute('SELECT count(*) FROM deadlines').fetchone()[0], 1)

    def test_pending_only_extension_is_granted_once_and_survives_resume(self):
        self.pending()
        self.observe(self.plan(self.direct()), 'poll-1', 1000.0)
        extended = self.observe(self.plan(self.direct()), 'poll-2', 1121.0)
        self.assertEqual((extended['status'], extended['extended'], extended['deadline'], extended['original_deadline']),
                         ('pending', True, 1720.0, 1120.0))
        self.assertEqual(self.grants(), 1)
        # A resumed (fresh ledger connection) or composed observation reuses the same grant.
        resumed = self.observe(self.plan({'timeout_seconds': 3600, 'pending_extension_seconds': 3600,
                                          'max_pending_extensions': 1}), 'poll-3', 1500.0)
        self.assertEqual((resumed['deadline'], self.grants()), (1720.0, 1))
        # Configuration can shorten or disable it, never renew it.
        shorter = self.observe(self.plan({**self.direct(), 'pending_extension_seconds': 400}), 'poll-4', 1500.0)
        self.assertEqual(shorter['deadline'], 1520.0)
        with self.assertRaisesRegex(TimeoutError, 'exhausted'):
            self.observe(self.plan({**self.direct(), 'max_pending_extensions': 0}), 'poll-5', 1500.0)
        before = self.github.calls['observe_ci']
        with self.assertRaisesRegex(TimeoutError, 'exhausted'):
            self.observe(self.plan(self.direct()), 'poll-6', 1720.0)
        self.assertEqual((self.github.calls['observe_ci'], self.grants()), (before, 1), 'no second grant or poll')

    def test_a_settled_post_window_capture_is_reported_and_grants_no_extension(self):
        self.pending()
        self.observe(self.plan(self.direct()), 'poll-1', 1000.0)
        for conclusion, status in (('failure', 'failed'), ('success', 'success')):
            with self.subTest(conclusion=conclusion):
                self.github.checks = [{'id': '9', 'name': 'Test', 'status': 'completed', 'conclusion': conclusion,
                                       'run_id': '', 'job_id': '', 'attempt': 0}]
                result = self.observe(self.plan(self.direct()), 'late-' + conclusion, 1121.0)
                self.assertEqual((result['status'], result['extended'], result['deadline']), (status, False, 1120.0))
                self.assertEqual(self.grants(), 0)
        # Without an available extension the same late capture is a timeout.
        with self.assertRaisesRegex(TimeoutError, 'exhausted'):
            self.observe(self.plan({**self.direct(), 'max_pending_extensions': 0}), 'late-off', 1121.0)
        # A still-pending capture takes the one extension; a later settled one inside it is reported.
        self.pending()
        self.assertTrue(self.observe(self.plan(self.direct()), 'late-pending', 1122.0)['extended'])
        self.github.checks[0].update(status='completed', conclusion='success')
        result = self.observe(self.plan(self.direct()), 'in-extension', 1200.0)
        self.assertEqual((result['status'], result['extended'], self.grants()), ('success', True, 1))

    def test_interrupted_wait_resumes_the_original_window(self):
        self.pending()
        self.github.unavailable.add('observe_ci')
        with self.assertRaises(ConnectionError):
            self.observe(self.plan(self.direct()), 'poll-1', 1000.0)
        self.github.unavailable.clear()
        resumed = self.observe(self.plan(self.direct()), 'poll-2', 1100.0)
        self.assertEqual(resumed['deadline'], 1120.0, 'the interrupted poll already fixed the window')
        self.assertEqual(self.github.calls['observe_ci'], 2)

    def test_exhausted_extension_allowance_is_not_replenished_in_a_new_run_ledger_connection(self):
        self.pending()
        ledger = self.ledger()
        self.observe(self.plan(self.direct()), 'poll-1', 1000.0, ledger)
        self.observe(self.plan(self.direct()), 'poll-2', 1121.0, ledger)
        ledger.close()
        # Even with the window rows lost, the reservation alone forbids a second grant.
        fresh = self.ledger()
        fresh.db.execute("DELETE FROM deadlines WHERE name='ci-pending-extension:pr_shared'")
        with self.assertRaises(ValueError):
            fresh.reserve('ci-pending-extension:pr_shared', 'another-extension', 1)
        self.assertEqual(self.grants(), 1)

    # -- poll interval, absent-check grace and classification window -----------------------------

    def test_poll_interval_comes_from_the_validated_plan(self):
        self.pending()
        self.assertEqual(self.observe(self.plan(self.direct()), 'poll-1', 1000.0)['poll_interval'], '10',
                         'the composed default keeps the original delay')
        result = self.observe(self.plan({**self.direct(), 'poll_interval_seconds': 45}), 'poll-2', 1001.0)
        self.assertEqual(result['poll_interval'], '45')
        # ci-poll.yml sleeps for exactly this output; an invalid plan value never reaches it.
        poll = (fix_preflight.INSTALL / 'workflows/ci-poll.yml').read_text()
        self.assertIn('- sleep\n      - ${{ steps.ci.outputs.poll_interval }}\n', poll)
        for value in (0, 3601, '30', True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'poll_interval_seconds'):
                self.observe(self.plan({**self.direct(), 'poll_interval_seconds': value}), 'bad', 1002.0)

    def test_absent_checks_become_no_checks_after_a_retained_grace(self):
        controls = {**self.direct(), 'no_checks_grace_seconds': 60}
        first = self.observe(self.plan(controls), 'poll-1', 1000.0)
        self.assertEqual((first['status'], first['pending'], first['ready']), ('pending', True, False))
        # A resumed observation (new ledger connection) with a larger grace keeps the original end.
        resumed = self.observe(self.plan({**controls, 'no_checks_grace_seconds': 600}), 'poll-2', 1061.0)
        self.assertEqual((resumed['status'], resumed['outcome'], resumed['pending'], resumed['failed'],
                          resumed['ready']), ('no_checks', 'no_checks', False, False, False))
        # A settled blocker: never a repairable failure and never CI success.
        self.assertEqual(ci.classify({'head': HEAD, 'checks': []})['status'], 'pending')
        # Checks that appear later are observed as usual.
        self.github.checks = [{'id': '9', 'name': 'Test', 'status': 'completed', 'conclusion': 'success',
                               'run_id': '', 'job_id': '', 'attempt': 0}]
        self.assertEqual(self.observe(self.plan(controls), 'poll-3', 1062.0)['status'], 'success')
        # A zero grace reports no_checks at once; without a grace absent checks stay pending until the deadline.
        self.github.checks = []
        self.assertEqual(self.observe(self.plan({**controls, 'no_checks_grace_seconds': 0}), 'poll-4', 1063.0,
                                      self.ledger('zero'))['status'], 'no_checks')
        self.assertEqual(self.observe(self.plan(self.direct(), run='none'), 'poll-5', 1000.0)['status'], 'pending')
        late = self.observe(self.plan(self.direct(), run='none'), 'poll-6', 1119.0)
        self.assertEqual(late['status'], 'pending')
        # no_checks is settled, so a post-window capture takes no pending-only extension.
        self.observe(self.plan(controls, run='late'), 'poll-7', 1000.0)
        result = self.observe(self.plan(controls, run='late'), 'poll-8', 1121.0)
        self.assertEqual((result['status'], result['extended']), ('no_checks', False))
        self.assertEqual(self.grants('late'), 0)

    def test_no_checks_never_satisfies_readiness_or_starts_a_repair(self):
        controls = {**self.direct(), 'no_checks_grace_seconds': 0}
        result = self.observe(self.plan(controls), 'poll-1', 1000.0)
        self.assertEqual((result['status'], result['ready'], result['failed'], result['repair']),
                         ('no_checks', False, False, False))

    def test_classification_window_is_retained_per_failed_attempt(self):
        self.github.failed(9, 2, 3)
        self.github.issue(8, declaration())
        controls = {**self.direct(), 'classification_timeout_seconds': 30}
        # The first observation opens the window and classifies inside it.
        self.assertEqual(self.observe(self.plan(controls), 'poll-1', 1000.0)['outcome'], 'known_flakes')
        reads = self.github.calls['request']
        # A resumed observation after the window ends neither renews it nor reads any evidence.
        late = self.observe(self.plan({**controls, 'classification_timeout_seconds': 3600}), 'poll-2', 1031.0)
        self.assertEqual((late['status'], late['rejected']),
                         ('failed', [{'check_id': '9', 'reason': 'classification window exhausted'}]))
        self.assertEqual(self.github.calls['request'], reads)
        # A rerun attempt is another exact attempt with its own window.
        self.github = CIGitHub()
        self.github.failed(9, 2, 3, attempt=2)
        self.github.issue(8, declaration())
        self.assertEqual(self.observe(self.plan(controls), 'poll-3', 1032.0)['status'], 'success')
        # Without a configured timeout classification is unbounded, as before.
        self.assertEqual(self.observe(self.plan(self.direct(), run='unbounded'), 'poll-4', 5000.0)['status'],
                         'success')

    def test_a_window_that_ends_during_verification_discards_the_proof(self):
        self.github.failed(9, 2, 3)
        self.github.failed(10, 4, 5)
        self.github.issue(8, declaration())
        controls = {**self.direct(), 'classification_timeout_seconds': 30}
        now = [1000.0]
        read = self.github.request

        def slow(method, path, payload=None):
            # Check 9's issue reread ends its window; check 10's window opens only when it is classified.
            if path.endswith('/issues/8') and now[0] == 1000.0:
                now[0] = 1040.0
            return read(method, path, payload)
        with patch.object(self.github, 'request', side_effect=slow), \
                patch('prfix.ci.time.time', side_effect=lambda: now[0]):
            result = ci.observe(self.github, self.plan(controls), self.ledger(), 'poll-1')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['rejected'], [{'check_id': '9',
                                               'reason': 'classification window ended during verification'}])
        # Check 10's own window opened when it was classified, so only its own proof applies.
        self.assertEqual([proof['check_id'] for proof in result['classifications']], ['10'])
        # A later observation never applies check 9's evidence late.
        with patch('prfix.ci.time.time', return_value=1041.0):
            again = ci.observe(self.github, self.plan(controls), self.ledger(), 'poll-2')
        self.assertEqual([item['check_id'] for item in again['rejected']], ['9'])
        self.assertEqual(again['rejected'][0]['reason'], 'classification window exhausted')

    # -- issue-backed flakes ---------------------------------------------------------------------

    def test_issue_backed_flake_accepts_only_its_exact_attempt(self):
        self.github.failed(9, 2, 3)
        self.github.issue(8, declaration())
        result = ci.classify_current(self.github, self.plan(),
                                     {'head': HEAD, 'checks': copy.deepcopy(self.github.checks), 'evidence': 'e'})
        self.assertEqual((result['status'], result['outcome'], result['rejected']), ('success', 'known_flakes', []))
        [proof] = result['classifications']
        self.assertEqual((proof['run_id'], proof['job_id'], proof['attempt'], proof['head']), ('2', '3', 1, HEAD))
        self.assertEqual(proof['classifications'][0]['issue'], 'https://github.com/owner/repo/issues/8')
        # Learning reads the attempt once; verification rereads it and the issue.
        self.assertEqual((self.github.calls['actions/runs'], self.github.calls['actions/jobs'],
                          self.github.calls['logs'], self.github.calls['search'], self.github.calls['issues']),
                         (2, 2, 2, 1, 1))
        # A rerun attempt of the same run is a different exact attempt: the earlier proof never covers it.
        self.github.checks[0]['attempt'] = 2
        self.assertEqual(ci.classify({'head': HEAD, 'checks': self.github.checks}, [proof])['status'], 'failed')

    def test_rerun_attempt_and_changed_head_need_their_own_evidence(self):
        self.github.failed(9, 2, 3, attempt=2)
        self.github.issue(8, declaration())
        observation = {'head': HEAD, 'checks': copy.deepcopy(self.github.checks)}
        self.assertEqual(ci.classify_current(self.github, self.plan(), observation)['status'], 'success')
        # The run reports another head than the observed one.
        self.github.runs[2]['head_sha'] = 'c' * 40
        result = ci.classify_current(self.github, self.plan(), observation)
        self.assertEqual(result['status'], 'failed')
        self.assertRegex(result['rejected'][0]['reason'], 'does not bind exact')

    def test_mismatched_issue_evidence_never_waives_the_failure(self):
        cases = {
            'closed issue': lambda g: g.issue(8, declaration(), state='closed'),
            'other job': lambda g: g.issue(8, declaration(job='Lint')),
            'other workflow': lambda g: g.issue(8, declaration(workflow='.github/workflows/other.yml')),
            'absent signature': lambda g: g.issue(8, declaration(signature='connection reset')),
            'other test': lambda g: g.issue(8, declaration(test='TestOther') + ' TestFlaky'),
            'prose only': lambda g: g.issue(8, 'TestFlaky might be flaky?'),
            'other repository': lambda g: g.issue(8, declaration(), repository='someone/fork'),
        }
        for label, arrange in cases.items():
            with self.subTest(label=label):
                self.github = CIGitHub()
                self.github.failed(9, 2, 3)
                arrange(self.github)
                observation = {'head': HEAD, 'checks': copy.deepcopy(self.github.checks)}
                result = ci.classify_current(self.github, self.plan(), observation)
                self.assertEqual((result['status'], result['outcome'], result['classifications']),
                                 ('failed', 'failure', []))
                if label == 'other repository':
                    self.assertRegex(result['rejected'][0]['reason'], 'canonical issue in the PR repository')

    def test_only_a_trusted_earlier_declaration_is_learned(self):
        # The PR adds TestNewFeature and it really fails; a declaration must not waive it.
        lines = ['=== RUN   TestNewFeature', '    feature_test.go:9: got 1 want 2', '--- FAIL: TestNewFeature (0.01s)',
                 'FAIL', 'exit status 1']
        body = declaration(test='TestNewFeature', signature='got 1 want 2')
        cases = {
            'PR author, even as a member': ({'user': {'id': AUTHOR, 'login': 'contributor'}}, None, 'failed'),
            'outsider': ({'user': {'id': 77, 'login': 'outsider'}, 'association': 'NONE'}, None, 'failed'),
            'contributor association': ({'user': {'id': 77, 'login': 'outsider'}, 'association': 'CONTRIBUTOR'},
                                        None, 'failed'),
            'opened after the run': ({'created': '2026-11-26T12:30:00Z'}, None, 'failed'),
            'no creation time': ({'created': None}, None, 'failed'),
            'maintainer before the run': ({}, None, 'success'),
            'configured personal account': ({'user': {'id': 77, 'login': 'personal'}, 'association': 'NONE'},
                                            [{'host': 'github.com', 'id': 77}], 'success'),
        }
        for label, (issue, accounts, status) in cases.items():
            with self.subTest(label=label):
                self.github = CIGitHub()
                self.github.failed(9, 2, 3, lines=lines)
                self.github.issue(77, body, **issue)
                plan = self.plan(**({'accounts': accounts} if accounts else {}))
                result = ci.classify_current(self.github, plan, {'head': HEAD, 'checks': copy.deepcopy(self.github.checks)})
                self.assertEqual(result['status'], status)
                self.assertEqual(self.github.calls['issues'], 1 if status == 'success' else 0,
                                 'an untrusted declaration is never even verified')
        # Without the PR author's identity nothing is learned, and the check stays failed.
        self.github = CIGitHub()
        self.github.failed(9, 2, 3, lines=lines)
        self.github.issue(77, body)
        plan = dict(self.plan(), snapshot={})
        result = ci.classify_current(self.github, plan, {'head': HEAD, 'checks': copy.deepcopy(self.github.checks)})
        self.assertEqual((result['status'], result['rejected'][0]['reason']),
                         ('failed', 'PR author identity unavailable for flake learning'))
        self.assertEqual(self.github.calls['search'], 0)

    def test_one_declared_flake_never_covers_another_failure_in_the_same_step(self):
        for label, extra in (('undeclared test', ['--- FAIL: TestBroken (0.20s)']),
                             ('unknown diagnostic', ['panic: nil pointer dereference'])):
            with self.subTest(label=label):
                self.github = CIGitHub()
                self.github.failed(9, 2, 3, lines=['=== RUN   TestFlaky', '    client.go:17: intermittent gateway timeout',
                                                   '--- FAIL: TestFlaky (0.10s)', *extra, 'FAIL', 'exit status 1'])
                self.github.issue(8, declaration())
                result = ci.classify_current(self.github, self.plan(), {'head': HEAD,
                                                                        'checks': copy.deepcopy(self.github.checks)})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual([item['check_id'] for item in result['rejected']], ['9'])

    def test_unavailable_evidence_fails_only_its_own_check(self):
        for missing in ('logs', 'search'):
            with self.subTest(missing=missing):
                self.github = CIGitHub()
                self.github.failed(9, 2, 3)
                self.github.issue(8, declaration())
                self.github.unavailable.add(missing)
                result = ci.classify_current(self.github, self.plan(), {'head': HEAD,
                                                                        'checks': copy.deepcopy(self.github.checks)})
                self.assertEqual((result['status'], [r['check_id'] for r in result['rejected']]), ('failed', ['9']))
        # Two failed checks: one with verified evidence, one whose logs expired.
        self.github = CIGitHub()
        self.github.failed(9, 2, 3)
        self.github.failed(10, 4, 5)
        del self.github.logs[('4', '5', 1)]
        self.github.issue(8, declaration())
        result = ci.classify_current(self.github, self.plan(), {'head': HEAD, 'checks': copy.deepcopy(self.github.checks)})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([proof['check_id'] for proof in result['classifications']], ['9'])
        self.assertEqual([item['check_id'] for item in result['rejected']], ['10'])
        self.assertRegex(result['rejected'][0]['reason'], 'expired')

    def test_configured_candidates_for_other_checks_are_reported_not_trusted(self):
        self.github.failed(9, 2, 3)
        stale = {'check_id': '99', 'run_id': '7', 'job_id': '8', 'attempt': 1, 'kind': 'flake', 'workflow': WORKFLOW,
                 'job': 'Test', 'test': 'TestFlaky', 'signature': 'x', 'issue': 'https://github.com/owner/repo/issues/8'}
        result = ci.classify_current(self.github, self.plan({'classifications': [stale]}),
                                     {'head': HEAD, 'checks': copy.deepcopy(self.github.checks)})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['rejected'][0], {'check_id': '99',
                                                 'reason': 'classification names no failed check of this observation'})

    def test_unconfigured_fixture_routes_fail_loudly(self):
        self.github.failed(9, 2, 3)
        def unconfigured(path, key=None):
            raise AssertionError('unconfigured fixture route: pages ' + path)
        self.github.pages = unconfigured  # Never swallowed as unavailable evidence, and no live fallback.
        with self.assertRaisesRegex(AssertionError, 'unconfigured fixture route'):
            ci.classify_current(self.github, self.plan(), {'head': HEAD, 'checks': copy.deepcopy(self.github.checks)})

    def test_observation_records_the_outcome_for_readiness(self):
        self.github.failed(9, 2, 3)
        self.github.issue(8, declaration())
        result = self.observe(self.plan(self.direct()), 'poll-1', 1000.0)
        self.assertEqual((result['status'], result['outcome'], result['ready']), ('success', 'known_flakes', True))
        self.github.issue(8, declaration(), state='closed')
        result = self.observe(self.plan(self.direct()), 'poll-2', 1001.0)
        self.assertEqual((result['status'], result['ready'], result['failed']), ('failed', False, True))


# -- Fork workflow approval ---------------------------------------------------------------------------

class ForkGitHub(PublicationGitHub):
    """Adds the head's pull_request run listing, run reads, attempt jobs, pending deployments and approval."""

    def __init__(self, head, target, **kwargs):
        super().__init__(head, target, **kwargs)
        self.runs, self.jobs = {}, Counter()
        self.head = head

    def gate(self, ident, **changes):
        self.runs[ident] = {'id': ident, 'run_attempt': 1, 'event': 'pull_request', 'head_sha': self.head,
                            'status': 'action_required', 'conclusion': 'action_required',
                            'repository': {'full_name': 'owner/repo'}, 'head_repository': {'full_name': 'contributor/repo'},
                            'head_branch': 'change', 'pull_requests': [], 'path': WORKFLOW + '@refs/pull/7/merge',
                            **changes}

    def pages(self, path, key=None):
        self.calls['pages'] += 1
        if path != f'repos/owner/repo/actions/runs?event=pull_request&head_sha={self.head}' or key != 'workflow_runs':
            raise AssertionError('unconfigured fixture route: pages ' + path)
        return [copy.deepcopy(run) for run in self.runs.values() if run['head_sha'] == self.head]

    def request(self, method, path, payload=None):
        match = re.fullmatch(r'repos/owner/repo/actions/runs/(\d+)(/approve|/attempts/(\d+)/jobs\?per_page=100|'
                             r'/pending_deployments)?', path)
        if not match:
            return super().request(method, path, payload)
        self.calls['actions'] += 1
        run = self.runs[int(match[1])]
        if match[2] == '/approve' and method == 'POST':
            self.calls['write'] += 1
            self.calls['approve'] += 1
            if self.fault == 'before':
                raise OSError('fixture response lost before effect')
            run.update(status='queued', conclusion=None)
            self.jobs[(run['id'], run['run_attempt'])] = 2
            self._lose()
            return {}
        if method != 'GET':
            raise AssertionError(f'unconfigured fixture route: {method} {path}')
        if match[2] is None:
            return copy.deepcopy(run)
        if match[2] == '/pending_deployments':
            return []
        return {'total_count': self.jobs[(run['id'], int(match[3]))], 'jobs': []}


class ForkApproval(Helpers):
    RUN = 'review_fixture'

    def setUp(self):
        super().setUp()
        self.github = ForkGitHub(self.commit, self.target)
        self.github.git_head = self.commit

    def review(self, authorizations=None, permissions=None, findings=()):
        authorizations = {'approve_fork_ci': grant('fork')} if authorizations is None else authorizations
        permissions = {'approve_fork_ci': True} if permissions is None else permissions
        result, plan = self.plan([{'stage': 'reviews'}], ['review'], authorizations, permissions, 'review', self.RUN)
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, list(findings))
        return result, plan, ledger

    def fork_barrier(self, plan, ledger):
        """main.yml fork_ci: publication-barrier.yml with selector fork_ci, then publication.yml per item."""
        listing = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'fork_ci'})
        return listing, [self.publish(plan, ledger, item) for item in listing['items']]

    def test_complete_review_approves_each_eligible_run_attempt_once(self):
        self.github.gate(41)
        self.github.gate(42, run_attempt=2)
        self.github.gate(43, status='completed', conclusion='success')  # Already ran.
        self.github.gate(44, head_repository={'full_name': 'owner/repo'})  # Not from the fork.
        self.github.gate(45, head_sha='e' * 40)  # An earlier head.
        result, plan, ledger = self.review()
        self.assertTrue(result['fork_ci_authorized'])
        self.assertFalse(result['approval_authorized'] or result['findings_authorized'] or result['merge_authorized'])
        listing, receipts = self.fork_barrier(plan, ledger)
        self.assertEqual([item['payload'] for item in listing['items']],
                         [{'run_id': 41, 'attempt': 1}, {'run_id': 42, 'attempt': 2}])
        self.assertEqual((self.github.calls['approve'], self.writes()), (2, 2))
        for receipt in receipts:
            self.assertEqual(receipt['receipt']['kind'], 'approve_fork_ci')
            self.assertTrue(receipt['receipt']['result']['executing'])
        # Nothing is gated any more: a rerun of the barrier is the verified no-op and writes nothing.
        again, _ = self.fork_barrier(plan, ledger)
        self.assertEqual((again['items'], again['noop']), ([], True))
        self.assertEqual((self.writes(), self.native['audience']), (2, 2))
        # Fork approval establishes neither PR approval nor merge readiness.
        self.assertEqual(publishing.approval_proposal(self.root, self.github, ledger, plan, {}),
                         {'invoke': False, 'proposal': {}})
        self.assertEqual(publishing.merge_proposal(self.root, self.github, ledger, plan, {}),
                         {'invoke': False, 'proposal': {}})

    def test_no_gated_run_is_the_verified_noop(self):
        _, plan, ledger = self.review()
        listing, receipts = self.fork_barrier(plan, ledger)
        self.assertEqual((listing['items'], listing['noop'], listing['blockers'], receipts), ([], True, [], []))
        self.assertEqual((self.github.calls['pages'], self.writes()), (1, 0))

    def test_other_publication_or_merge_authority_never_implies_fork_approval(self):
        self.github.gate(41)
        others = {'post_findings': grant('post'), 'approve_pr': grant('approve'), 'merge': grant('merge')}
        _, plan, ledger = self.review(others, {key: True for key in others})
        listing = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'fork_ci'})
        self.assertEqual((listing['items'], listing['blockers']),
                         ([], ['fork approval needs its own explicit authorization']))
        manual = {'id': 'manual', 'kind': 'approve_fork_ci', 'head': self.commit, 'target': self.target,
                  'payload': {'run_id': 41, 'attempt': 1}, 'sources': ['x']}
        with self.assertRaisesRegex(PublicationError, 'not a current eligible fork approval'):
            publishing.publication_prepare(self.root, self.github, ledger, plan, {'proposal': manual})
        with self.assertRaisesRegex(PublicationError, 'explicit user authorization required for approve_fork_ci'):
            publishing._publication(self.root, self.github, ledger, plan).prepare(manual)
        # The grant without the permission is denied too.
        _, plan, ledger = self.review(permissions={})
        self.assertEqual(publishing.fork_ci_proposals(self.root, self.github, ledger, plan)['blockers'],
                         ['configured permission forbids approve_fork_ci'])
        self.assertEqual((self.github.calls['pages'], self.writes()), (0, 0))

    def test_incomplete_unclean_draft_or_own_review_blocks_before_any_intent(self):
        self.github.gate(41)
        _, plan, ledger = self.review(findings=[self.finding('inline', 'Not covered by a test.', 'diff',
                                                             path='code.py', line=1, side='RIGHT')])
        self.assertIn('approval needs complete clean independent review',
                      publishing.fork_ci_proposals(self.root, self.github, ledger, plan)['blockers'])
        result, plan = self.plan([{'stage': 'reviews'}], ['review'], {'approve_fork_ci': grant('fork')},
                                 {'approve_fork_ci': True}, 'review', 'review_unverified')
        ledger = self.ledger(plan)
        self.execute(plan, ledger, 'review', 'agent_' + '7' * 32, self.report())
        self.assertFalse(publishing.fork_ci(self.root, self.github, ledger, plan)['ready'], 'no verifier yet')
        self.github.evidence['metadata']['draft'] = True
        _, plan, ledger = self.review()
        self.assertIn('fork approval needs a non-draft PR',
                      publishing.fork_ci_proposals(self.root, self.github, ledger, plan)['blockers'])
        self.github.evidence['metadata']['draft'] = False
        self.github.evidence['metadata']['user'] = {'id': 1, 'login': 'maintainer'}
        _, plan, ledger = self.review()
        self.assertIn('the authenticated account never approves its own PR',
                      publishing.fork_ci_proposals(self.root, self.github, ledger, plan)['blockers'])
        self.assertEqual((self.github.calls['pages'], self.writes()), (0, 0))
        self.assertEqual(ledger.db.execute('SELECT count(*) FROM publication_intents').fetchone()[0], 0)

    def test_ambiguous_association_blocks_every_approval(self):
        self.github.gate(41)
        self.github.gate(42, pull_requests=[{'number': 8}])
        _, plan, ledger = self.review()
        listing, receipts = self.fork_barrier(plan, ledger)
        self.assertEqual((listing['items'], receipts), ([], []))
        self.assertEqual(listing['blockers'], ['workflow run 42: ambiguous pull request association'])
        self.assertEqual(self.writes(), 0)

    def test_a_moved_attempt_blocks_the_intent_without_a_write(self):
        self.github.gate(41)
        _, plan, ledger = self.review()
        [item] = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'fork_ci'})['items']
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {'proposal': item})
        self.github.jobs[(41, 1)] = 3  # Someone else approved it after preparation.
        verdict = self.report(audience={'fingerprint': prepared['fingerprint'], 'suitable': True,
                                        'audience': 'Repository maintainers', 'evidence': ['code']})
        with self.assertRaises(Drift):
            publishing.publication_execute(self.root, self.github, ledger, plan,
                                           {'id': prepared['id'], 'agent': AUDIENCE[0], 'report': verdict})
        state = ledger.db.execute('SELECT state FROM publication_intents WHERE id=?', (prepared['id'],)).fetchone()[0]
        self.assertEqual((state, self.writes()), ('blocked', 0))

    def test_lost_responses_are_reconciled_and_an_uncertain_approval_is_never_resent(self):
        for fault in ('before', 'after'):
            with self.subTest(fault=fault):
                self.github.calls.clear()
                self.github.runs.clear()
                self.github.gate(41)
                self.RUN = 'review_' + fault
                _, plan, ledger = self.review()
                self.github.fault = fault
                with self.assertRaises(OSError):
                    self.fork_barrier(plan, ledger)
                self.github.fault = None
                if fault == 'before':
                    for _ in range(2):
                        # Only the retained original is listed, for read-only reconciliation.
                        listing = publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'fork_ci'})
                        self.assertEqual(len(listing['items']), 1)
                        with self.assertRaisesRegex(PublicationError, 'unresolved'):
                            self.fork_barrier(plan, ledger)
                    self.assertFalse(publishing.fork_ci(self.root, self.github, ledger, plan)['ready'])
                else:
                    _, [receipt] = self.fork_barrier(plan, ledger)
                    self.assertTrue(receipt['receipt']['result']['executing'])
                    self.assertEqual(self.fork_barrier(plan, ledger)[0]['items'], [])
                self.assertEqual(self.github.calls['approve'], 1)

    def test_dk_pr_fix_plans_never_list_or_prepare_fork_approval(self):
        with self.assertRaisesRegex(ValueError, 'fork workflow approval belongs to incoming review'):
            prepare(self.root, {'pr': 'https://github.com/owner/repo/pull/7', 'instructions': 'Fix it.',
                                'logical_run': 'fix_fixture', 'authorizations': {}},
                    {'tasks': [], 'finalizers': [], 'reviewers': [], 'mode': 'fix',
                     'permissions': {'approve_fork_ci': True}}, 'exec-1', self.github, self.definitions)
        self.github.evidence['metadata']['user'] = {'id': 1, 'login': 'maintainer'}
        _, plan = self.plan(['describe-pr'], [], {'approve_fork_ci': grant('fork')}, {}, 'fix', 'fix_fixture')
        ledger = self.ledger(plan)
        with self.assertRaisesRegex(PublicationError, 'belongs to incoming review'):
            publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'fork_ci'})
        self.assertEqual(self.github.calls['pages'], 0)


# -- Flaky-test discovery -------------------------------------------------------------------------------

def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout


class FlakeGitHub:
    """Failed run listing, attempt jobs, failed-step logs and open issues; anything else fails and is counted."""

    def __init__(self):
        self.calls = Counter()
        self.runs, self.jobs, self.logs, self.issues = [], {}, {}, []
        self.broken = set()

    def run(self, ident, jobs, attempt=1):
        self.runs.append({'id': ident, 'run_attempt': attempt, 'head_sha': 'f' * 40, 'path': WORKFLOW + '@refs/heads/main'})
        self.jobs[(ident, attempt)] = [{'id': job, 'name': 'Test', 'conclusion': 'failure'} for job, _ in jobs]
        for job, lines in jobs:
            if lines is not None:
                self.logs[(str(ident), str(job), attempt)] = '\n'.join('Test\tTests\t' + line for line in lines)

    def request(self, method, path, payload=None):
        self.calls['request'] += 1
        if 'listing' in self.broken:
            raise ConnectionError('listing failed')
        match = re.fullmatch(r'repos/owner/repo/actions/runs\?status=failure&created=%3E%3D\d{4}-\d\d-\d\d'
                             r'&per_page=100&page=(\d+)', path)
        if method == 'GET' and match:
            self.calls['runs'] += 1
            page = int(match[1])
            return {'total_count': len(self.runs), 'workflow_runs': copy.deepcopy(self.runs[(page - 1) * 100:page * 100])}
        match = re.fullmatch(r'repos/owner/repo/actions/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100', path)
        if method == 'GET' and match:
            self.calls['jobs'] += 1
            jobs = self.jobs[(int(match[1]), int(match[2]))]
            return {'total_count': len(jobs), 'jobs': copy.deepcopy(jobs)}
        match = re.fullmatch(r'repos/owner/repo/issues\?state=open&per_page=100&page=(\d+)', path)
        if method == 'GET' and match:
            self.calls['issues'] += 1
            page = int(match[1])
            return copy.deepcopy(self.issues[(page - 1) * 100:page * 100])
        raise AssertionError(f'unconfigured fixture route: {method} {path}')

    def failure_logs(self, target, run, job, attempt):
        self.calls['logs'] += 1
        if target['repository'] != 'github.com/owner/repo' or (run, job, attempt) not in self.logs:
            raise OSError('failed-step logs expired')
        return self.logs[(run, job, attempt)]


FLAKY = ['=== RUN   TestFlaky', '    client.go:17: intermittent gateway timeout', '--- FAIL: TestFlaky (0.10s)']
BROKEN = ['--- FAIL: TestBroken (0.20s)']


def report(**overrides):
    def write(task):
        tests = []
        for item in task['context']['tests']:
            entry = {key: item[key] for key in ('workflow', 'job', 'test', 'status')}
            entry['observations'] = item['observations'][:1]
            if item['links']:
                entry['issue'] = item['links'][0]['issue']
            tests.append(entry)
        body = {'summary': 'Flaky-test evidence from the bounded scan.', 'tests': tests, 'gaps': []}
        for key, change in overrides.items():
            body[key] = change(body[key]) if callable(change) else change
        return body
    return session(write)


class FlakeDiscovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw ci flakes ')
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / 'target repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'AGENTS.md').write_text('Keep CI evidence exact.\n')
        self.engine = Engine()
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}
        self.github = FlakeGitHub()
        self.github.run(31, [(301, FLAKY), (302, BROKEN)])
        self.github.run(32, [(303, FLAKY)], attempt=2)
        self.github.issues = [{'number': 8, 'state': 'open', 'title': 'Flaky', 'body': declaration(),
                               'html_url': 'https://github.com/owner/repo/issues/8'},
                              {'number': 9, 'state': 'open', 'title': 'PR', 'body': declaration(test='TestBroken'),
                               'html_url': 'https://github.com/owner/repo/pull/9', 'pull_request': {}}]

    def inspect(self, *sessions, **changes):
        self.transport = EngineTransport(self.engine, sessions=sessions)
        arguments = {'request': {'repository': 'github.com/owner/repo', 'instructions': 'Scan CI for flaky tests.'},
                     'deployment': self.deployment, 'repository': self.repo, 'now': 1_700_000_000.0, **changes}
        result = ci_flakes.inspect(transport=self.transport, github=self.github, **arguments)
        self.assertLessEqual(set(self.transport.calls), {'capabilities', 'native_session'})
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '?? AGENTS.md\n', 'nothing written')
        return result

    def test_linked_and_candidate_contexts_come_only_from_verified_evidence(self):
        result = self.inspect(report())
        self.assertEqual((result['status'], result['covered'], result['waives'], result['authority']),
                         ('complete', True, False, 'none'), result.get('blockers'))
        tests = {item['test']: item for item in result['tests']}
        self.assertEqual((tests['TestFlaky']['status'], tests['TestFlaky']['issue']),
                         ('linked', 'https://github.com/owner/repo/issues/8'))
        self.assertEqual((tests['TestBroken']['status'], tests['TestBroken']['issue']), ('candidate', None))
        self.assertEqual(result['evidence']['runs'], 2)
        self.assertEqual(self.github.calls, Counter(request=4, runs=1, jobs=2, logs=3, issues=1))
        [task] = self.transport.tasks
        self.assertEqual((task['role'], task['skills'], task['writes'], task['publication'], task['adapter']),
                         ('leaf', [], False, False, 'inspect-flakes'))
        observed = {item['test']: item['observations'] for item in task['context']['tests']}
        self.assertEqual(observed['TestFlaky'], ['31:1:301:TestFlaky', '32:2:303:TestFlaky'])
        self.assertEqual(self.engine.sessions['inspect'], 1)

    def test_the_session_cannot_link_drop_or_invent_evidence(self):
        cases = {
            'links a candidate': {'tests': lambda tests: [dict(t, status='linked', issue='https://github.com/owner/repo/issues/8')
                                                          for t in tests]},
            'unlinks a flake': {'tests': lambda tests: [{**{k: v for k, v in t.items() if k != 'issue'},
                                                         'status': 'candidate'} for t in tests]},
            'drops a context': {'tests': lambda tests: tests[:1]},
            'invents a test': {'tests': lambda tests: tests + [dict(tests[0], test='TestInvented')]},
            'cites another observation': {'tests': lambda tests: [dict(t, observations=['99:1:1:' + t['test']])
                                                                  for t in tests]},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                self.engine = Engine()
                result = self.inspect(report(**overrides))
                self.assertEqual((result['status'], result['tests']), ('incomplete', []))
                self.assertTrue(result['blockers'])

    def test_limits_targets_and_gaps_are_reported_never_coverage(self):
        self.github.issues.append({'number': 10, 'state': 'open', 'title': 'Docs', 'body': 'Unrelated.',
                                   'html_url': 'https://github.com/owner/repo/issues/10'})
        result = self.inspect(report(), options={'max_runs': 1, 'max_issues': 1})
        self.assertEqual((result['complete'], result['covered'], result['limits_reached']),
                         (True, False, ['max_runs', 'max_issues']))
        self.assertEqual(result['evidence']['runs'], 1)
        self.engine = Engine()
        self.github.run(33, [(304, None), (305, ['Error: compiler failed'])])
        result = self.inspect(report(), targets=[{'workflow': WORKFLOW, 'job': 'Test', 'test': 'TestFlaky',
                                                  'signature': 'intermittent gateway timeout'}])
        self.assertEqual([item['test'] for item in result['tests']], ['TestFlaky'])
        self.assertFalse(result['covered'])
        self.assertTrue(any('logs unavailable' in gap for gap in result['gaps']))
        self.assertTrue(any('no named failed test' in gap for gap in result['gaps']))

    def test_failed_reads_block_and_quiet_history_needs_no_session(self):
        self.github.broken.add('listing')
        result = self.inspect()
        self.assertEqual((result['status'], result['blockers']), ('incomplete', ['evidence read failed: ConnectionError']))
        self.assertEqual(self.transport.calls['native_session'], 0)
        self.github = FlakeGitHub()
        result = self.inspect()
        self.assertEqual((result['status'], result['tests'], result['covered']), ('complete', [], True))
        self.assertEqual(self.transport.calls['native_session'], 0)

    def test_the_entry_has_no_authority(self):
        result = self.inspect(request={'repository': 'github.com/owner/repo', 'instructions': 'Scan.',
                                       'authorizations': {'create_issue': grant('issue')}})
        self.assertEqual((result['status'], result['admission']['error']['path']), ('rejected', 'request.authorizations'))
        result = self.inspect(targets=[{'workflow': WORKFLOW, 'job': 'Test', 'test': 'TestFlaky'}])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual((self.github.calls, self.transport.calls), (Counter(), Counter()))


if __name__ == '__main__':
    unittest.main()
