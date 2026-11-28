"""cf-workflows:dk-commit-split and dk-pr-split: commit organization and PR splitting (#34).

Disposable histories with mixed authors, a moved target, unrelated dirty files
and a disposable bare remote; a disposable splitting and run ledger per logical
run; the fail-closed fake engine of the configuration tests (claim-reconciled
native sessions) and the fail-closed fake GitHub of the publication tests.
Every unscripted session and unconfigured operation fails with no live
fallback. Each case asserts sessions, writes or pushes. These results do not
certify any engine, provider, host or GitHub conformance.
"""
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True  # Never add caches to the installed closure.
import dk_pr_fix as _helpers  # noqa: E402,F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture  # noqa: E402
from prfix.core import Ledger as RunLedger  # noqa: E402
from cf_workflows import admission, dk_pr_fix, drafting, splitting  # noqa: E402
from test_configuration_skills import Engine, Transport, session  # noqa: E402
from test_dk_pr_fix_publication import LocalPublication, Transport as PublicationTransport  # noqa: E402
from test_drafting import message as commit_message  # noqa: E402
from test_semantic import verdict  # noqa: E402

PR = 'https://github.com/o/r/pull/7'
INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}
GRANTS = {action: {'request_id': 'user-1', 'instruction': f'Allow {action}.', 'source': 'chat:1'}
          for action in ('push', 'create_pr', 'edit_pr')}
LOCAL = {'request_id': 'user-2', 'instruction': 'Reorganize my commits locally.', 'source': 'chat:2'}
ADA = ('Ada Lovelace', 'ada@example.invalid')
BOB = ('Bob Builder', 'bob@example.invalid')


def git(root, *args, author=ADA, date='1700000000 +0000'):
    env = {**git_fixture.environment(), 'GIT_AUTHOR_NAME': author[0], 'GIT_AUTHOR_EMAIL': author[1],
           'GIT_AUTHOR_DATE': date, 'GIT_COMMITTER_DATE': date}
    return subprocess.run(git_fixture.command('-c', 'commit.gpgsign=false', *args), cwd=root, check=True,
                          capture_output=True, env=env).stdout.decode().strip()


class GitHub(PublicationTransport):
    """Counted fake GitHub; created split PRs get their own numbers and the head of their pushed branch."""

    def __init__(self, remote):
        super().__init__()
        self.remote = remote

    def request(self, method, path, payload=None):
        if method == 'POST' and path == 'repos/o/r/pulls' and self.fault is None:
            self.counts['write'] += 1
            self.calls.append((method, path, copy.deepcopy(payload)))
            owner, branch = payload['head'].split(':', 1)
            head = git(self.remote, 'rev-parse', 'refs/heads/' + branch)
            result = {'number': 8 + len(self.split_pulls), 'state': 'open', 'draft': True, 'title': payload['title'],
                      'body': payload['body'], 'user': copy.deepcopy(self.ev['viewer']),
                      'head': {'sha': head, 'ref': branch, 'repo': {'full_name': 'o/r'}},
                      'base': copy.deepcopy(self.ev['metadata']['base'])}
            self.split_pulls.append(copy.deepcopy(result))
            return result
        return super().request(method, path, payload)


def assessed(task, reason='One logical change.'):
    return [{'commit': item['sha'], 'reason': reason} for item in task['context']['commits']]


def unchanged(task):
    return {'outcome': 'no_change', 'assessments': assessed(task), 'series': [], 'summary': 'Boundaries hold.',
            'questions': []}


class Fixture(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix='cfw splitting ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo'
        self.remote = self.base / 'remote.git'
        self.repo.mkdir()
        git(self.base, 'init', '-q', '--bare', str(self.remote))
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.name', ADA[0])
        git(self.repo, 'config', 'user.email', ADA[1])
        self.write('README.md', 'Project\n')
        self.write('docs/notes.md', 'Teh notes\n')
        self.write('other.txt', 'other\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'Initial')
        git(self.repo, 'checkout', '-q', '-b', 'feature')
        self.write('lib/helper.py', 'def helper():\n    return 1\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'Add helper\n\nSigned-off-by: Bob Builder <bob@example.invalid>',
            author=BOB, date='1700000100 +0000')
        self.foreign = git(self.repo, 'rev-parse', 'HEAD')
        self.write('src/feature.py', 'def feature():\n    return helper()\n')
        self.write('docs/notes.md', 'The notes\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'Add feature and fix a typo', date='1700000200 +0000')
        self.write('src/feature.py', 'def feature():\n    return helper() + 1\n')
        git(self.repo, 'commit', '-q', '-am', 'fixup! Add feature', date='1700000300 +0000')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.tree = git(self.repo, 'rev-parse', 'HEAD^{tree}')
        git(self.repo, 'checkout', '-q', 'main')
        self.write('other.txt', 'other, moved on\n')
        git(self.repo, 'commit', '-q', '-am', 'Target moves on', date='1700000400 +0000')
        self.target = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-q', 'feature')
        git(self.repo, 'push', '-q', str(self.remote), 'feature', 'main')
        self.engine = Engine()
        self.github = GitHub(self.remote)
        meta = self.github.ev['metadata']
        meta['head']['sha'], meta['base']['sha'] = self.head, self.target
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}

    def write(self, rel, body):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def dirty(self):
        """Unrelated staged, unstaged and untracked work that every local operation must keep."""
        self.write('README.md', 'Project, staged\n')
        git(self.repo, 'add', 'README.md')
        self.write('README.md', 'Project, staged and edited\n')
        self.write('scratch.txt', 'untracked\n')
        return self.status()

    def status(self):
        return (git(self.repo, 'status', '--porcelain=v1', '--untracked-files=all', '--', 'README.md', 'scratch.txt'),
                (self.repo / 'README.md').read_text(), git(self.repo, 'diff', '--cached', '--', 'README.md'))

    def configuration(self, *permissions):
        scope = {'id': 'me', 'selector': {'kind': 'personal', 'id': 'me'}, 'options': {},
                 'permissions': {action: True for action in permissions}, 'prompts': [], 'skills': [],
                 'preferences': []}
        return {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                'scopes': [scope], 'resources': [],
                'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                              'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}

    def transport(self, *sessions, validators=(), lose=0):
        self.fake = Transport(self.engine, sessions, validators=validators, lose=lose)
        return self.fake

    def calls(self, **expected):
        self.assertEqual(self.fake.calls, +Counter(expected))

    def admit(self, name, *, authorize=(), permissions=None, options=None, **extra):
        request = {'instructions': 'Organize this PR.', 'pr': PR}
        if authorize:
            request['authorizations'] = {action: GRANTS[action] for action in authorize}
        permissions = authorize if permissions is None else permissions
        return splitting.preflight(name, request=request, configuration=self.configuration(*permissions),
                                   deployment=self.deployment, repository=self.repo, transport=self.transport(),
                                   options=options, **extra)

    def ledger(self, admitted):
        request = admitted['snapshot']['request']
        ledger = splitting.Ledger(splitting.ledger_path(self.base / 'state', request['logical_run']),
                                  logical_run=request['logical_run'], target=request['pr']['url'])
        self.addCleanup(ledger.close)
        return ledger

    def organize(self, admitted, ledger, *sessions, **extra):
        return splitting.organize(admitted, repository=self.repo, capture=self.github.capture(None), ledger=ledger,
                                  transport=self.transport(*sessions), **extra)

    def partition(self, admitted, ledger, *sessions, **extra):
        return splitting.partition(admitted, repository=self.repo, capture=self.github.capture(None), ledger=ledger,
                                   transport=self.transport(*sessions), **extra)

    def publisher(self, name, admitted, ledger):
        request = admitted['snapshot']['request']
        runs = RunLedger(self.repo, request['logical_run'], PR)
        self.addCleanup(runs.close)
        publisher = LocalPublication(runs, self.github, self.repo, splitting.publication_plan(name, admitted),
                                     source_validator=splitting.source_validator(ledger, name))
        publisher.remote = self.remote
        return publisher

    @staticmethod
    def audience(intent):
        return {'suitable': True, 'fingerprint': intent['fingerprint'], 'audience': 'Repository reviewers',
                'evidence': list(intent['intent']['sources'])}

    def remote_head(self, branch):
        return subprocess.run(git_fixture.command('--git-dir', str(self.remote), 'rev-parse', '--verify', '-q',
                                                  'refs/heads/' + branch), capture_output=True,
                              env=git_fixture.environment()).stdout.decode().strip() or None

    def authors(self, base, head):
        return git(self.repo, 'log', '--reverse', '--format=%an <%ae> %ad', '--date=raw', f'{base}..{head}').split('\n')


def reorganize(task):
    """Fix the typo first, then the feature with its fixup folded in: a tree-preserving reorder."""
    commits = [item['sha'] for item in task['context']['commits']]
    _, mixed, fixup = commits
    return {'outcome': 'reorganize', 'assessments': assessed(task, 'Mixed commit and a fixup.'), 'series': [
        {'message': 'Fix a typo in the notes\n', 'units': [{'commit': mixed, 'paths': ['docs/notes.md']}],
         'reason': 'An unrelated typo fix.'},
        {'message': 'Add the feature\n', 'units': [{'commit': mixed, 'paths': ['src/feature.py']},
                                                   {'commit': fixup, 'paths': ['src/feature.py']}],
         'reason': 'The feature with its fixup folded in.'}],
        'summary': 'Split the mixed commit and fold the fixup.', 'questions': []}


# -- Presets ---------------------------------------------------------------------------------------

class Preset(Fixture):
    def test_baseline_bytes_options_and_presets(self):
        for name, entry in splitting.ENTRIES.items():
            digests = {item['path']: item['sha256'] for item in INVENTORY[name]['baseline_resources']}
            for host in ('claude', 'codex'):
                self.assertEqual(hashlib.sha256(entry['config'].encode()).hexdigest(),
                                 digests[f'{host}/skills/{name}/config.json'], name)
                self.assertEqual(hashlib.sha256(entry['role_prompt'].encode()).hexdigest(),
                                 digests[f'{host}/skills/{name}/prompts/default.md'], name)
            self.assertEqual(splitting.preset(name), {'tasks': [entry['adapter'], {'stage': 'publication'}],
                                                      'finalizers': [], 'reviewers': []})
            self.assertFalse(admission.BASELINE_ADAPTERS[entry['adapter']]['writes'])
            self.assertEqual(INVENTORY[name]['parity_status'], 'not_implemented')

    def test_default_runs_need_no_validator_and_load_no_skill(self):
        for name in splitting.ENTRIES:
            admitted = self.admit(name)
            self.assertEqual(admitted['preflight'], 'admitted', admitted)
            self.calls(capabilities=1)
            self.assertEqual((admitted['entry']['task']['definition'], admitted['entry']['task']['skill']),
                             ('leaf', None))
            self.assertFalse(any(value['effective'] for value in admitted['snapshot']['permissions'].values()))

    def test_presets_options_and_authority_cannot_expand(self):
        cases = [
            ('dk-commit-split', {'authorizations': {'create_pr': GRANTS['create_pr']}}, None, None,
             'request.authorizations'),
            ('dk-pr-split', {'authorizations': {'edit_pr': GRANTS['edit_pr']}}, None, None, 'request.authorizations'),
            ('dk-pr-split', {'pr': None, 'repository': 'github.com/o/r'}, None, None, 'request.pr'),
            ('dk-commit-split', {}, {'options': {'tasks': ['describe-pr']}}, None, 'invocation'),
            ('dk-commit-split', {}, {'permissions': {'push': True}}, None, 'invocation'),
            ('dk-commit-split', {}, None, {'policy': {'skill': 'my:policy'}}, 'options.policy'),
            ('dk-commit-split', {}, None, {'max_passes': 6}, 'options.max_passes'),
            ('dk-pr-split', {}, None, {'evacuate_unrelated': 'sometimes'}, 'options.evacuate_unrelated'),
            ('dk-pr-split', {}, None, {'added_lines_threshold': 0}, 'options.added_lines_threshold'),
            ('dk-pr-split', {}, None, {'max_passes': 2}, 'options.max_passes'),
        ]
        for name, request, invocation, options, path in cases:
            with self.subTest(name=name, path=path):
                body = {'pr': PR, 'instructions': 'x', **request}
                if body.get('pr') is None:
                    body.pop('pr')
                rejected = splitting.preflight(name, request=body, configuration=self.configuration(),
                                               deployment=self.deployment, repository=self.repo,
                                               transport=self.transport(), invocation=invocation, options=options)
                self.assertEqual((rejected['preflight'], rejected['error']['path']), ('rejected', path))
                self.calls()

    def test_authorization_needs_the_configured_permission_and_local_work_never_implies_it(self):
        rejected = self.admit('dk-pr-split', authorize=('create_pr',), permissions=())
        self.assertEqual(rejected['error']['path'], 'request.authorizations.create_pr')
        admitted = self.admit('dk-pr-split', authorize=('create_pr',))
        self.assertEqual(admitted['entry']['publication'], {'create_pr': True, 'push': False})
        self.assertEqual(splitting.publication_plan('dk-pr-split', admitted)['options'],
                         {'permissions': {'create_draft_prs': True, 'push_commits': False}})
        plain = self.admit('dk-commit-split')
        self.assertEqual(splitting.publication_plan('dk-commit-split', plain)['options'],
                         {'permissions': {'rewrite_commits': False}})

    def test_a_policy_prompt_is_assessed_by_the_shared_validator(self):
        transport = self.transport(validators=[verdict('accepted')])
        admitted = splitting.preflight('dk-commit-split', request={'instructions': 'Organize.', 'pr': PR},
                                       configuration=self.configuration(), deployment=self.deployment,
                                       repository=self.repo, transport=transport,
                                       options={'policy': {'prompt': 'Squash into one commit with a short subject.'}})
        self.assertEqual(admitted['preflight'], 'admitted', admitted)
        self.assertEqual(len(transport.assessments), 1)
        self.assertEqual(splitting.effective('dk-commit-split', admitted)['policy'],
                         'Squash into one commit with a short subject.')


# -- dk-commit-split -------------------------------------------------------------------------------

class CommitSplit(Fixture):
    def test_every_commit_is_assessed_and_an_unchanged_series_is_a_no_op_reused_without_a_session(self):
        admitted = self.admit('dk-commit-split')
        ledger = self.ledger(admitted)
        report = self.organize(admitted, ledger, session(unchanged))
        self.assertEqual(report['status'], 'no_op', report)
        self.assertEqual(self.engine.sessions['dk-commit-split'], 1)
        [task] = self.fake.tasks
        self.assertEqual((task['tools'], task['writes'], task['publication'], task['skills']),
                         ('read-only', False, False, []))
        self.assertEqual([item['fixed'] for item in task['context']['commits']], [True, False, False])
        self.assertEqual(len(report['passes'][0]['assessments']), 3)
        again = self.organize(admitted, ledger)
        self.assertEqual((again['status'], again['passes'][0]['session']), ('no_op', False))
        self.assertEqual(self.engine.sessions['dk-commit-split'], 1)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)

    def test_tree_preserving_reorder_keeps_authorship_dirty_work_and_a_recovery_ref(self):
        before = self.dirty()
        admitted = self.admit('dk-commit-split')
        ledger = self.ledger(admitted)
        refs = git(self.repo, 'for-each-ref')
        assessed_only = self.organize(admitted, ledger, session(reorganize), session(unchanged))
        self.assertEqual(assessed_only['status'], 'assessed', assessed_only)
        self.assertEqual((git(self.repo, 'for-each-ref'), git(self.repo, 'rev-parse', 'HEAD')), (refs, self.head))
        self.assertEqual(self.engine.sessions['dk-commit-split'], 2)  # The second pass finds nothing to change.
        # Pass 1 split the mixed commit and folded the fixup; the final tree is the reviewed tree.
        prepared = self.organize(admitted, ledger, rewrite=LOCAL)
        self.assertEqual(prepared['status'], 'prepared', prepared)
        self.assertEqual(self.engine.sessions['dk-commit-split'], 2)  # Both passes reused: identical objects.
        new = git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(new, assessed_only['final']['head'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^{tree}'), self.tree)
        self.assertEqual(git(self.repo, 'rev-parse', prepared['recovery']), self.head)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD~2'), self.foreign)  # Another author's commit is kept.
        self.assertEqual(git(self.repo, 'log', '--format=%s', f'{self.foreign}..HEAD').split('\n'),
                         ['Add the feature', 'Fix a typo in the notes'])
        self.assertEqual(self.authors(self.foreign, 'HEAD'),
                         [f'{ADA[0]} <{ADA[1]}> 1700000200 +0000', f'{ADA[0]} <{ADA[1]}> 1700000300 +0000'])
        self.assertEqual(self.status(), before)
        again = self.organize(admitted, ledger, rewrite=LOCAL)
        self.assertEqual((again['status'], again['reused']), ('prepared', True))
        self.assertEqual(self.engine.sessions['dk-commit-split'], 2)

    def test_interrupted_rewrite_reconciles_its_original_refs_before_continuing(self):
        admitted = self.admit('dk-commit-split')
        ledger = self.ledger(admitted)
        real, updates = splitting.git, []

        def crash(repository, *args, **kwargs):
            if args[0] == 'update-ref':
                updates.append(args)
                if len(updates) == 2:
                    raise KeyboardInterrupt('interrupted between ref updates')
            return real(repository, *args, **kwargs)
        with mock.patch.object(splitting, 'git', crash), self.assertRaises(KeyboardInterrupt):
            self.organize(admitted, ledger, session(reorganize), session(unchanged), rewrite=LOCAL)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)  # The branch did not move yet.
        resumed = self.organize(admitted, ledger, rewrite=LOCAL)
        self.assertEqual((resumed['status'], resumed['reused']), ('prepared', True), resumed)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), resumed['final']['head'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^{tree}'), self.tree)
        self.assertEqual(self.engine.sessions['dk-commit-split'], 2)

    def test_unscripted_sessions_fail_and_a_lost_response_reconciles_by_claim(self):
        admitted = self.admit('dk-commit-split')
        ledger = self.ledger(admitted)
        unscripted = self.organize(admitted, ledger)
        self.assertEqual((unscripted['status'], unscripted['error']['path']), ('blocked', 'session'))
        transport = self.transport(session(unchanged), lose=1)
        lost = splitting.organize(admitted, repository=self.repo, capture=self.github.capture(None), ledger=ledger,
                                  transport=transport)
        self.assertEqual((lost['status'], lost['error']['path']), ('blocked', 'session'))
        found = splitting.organize(admitted, repository=self.repo, capture=self.github.capture(None), ledger=ledger,
                                   transport=transport)
        self.assertEqual(found['status'], 'no_op', found)
        self.assertEqual(self.engine.sessions['dk-commit-split'], 1)  # The same claim, never a second session.

    def test_a_moved_branch_is_never_overwritten(self):
        admitted = self.admit('dk-commit-split')
        ledger = self.ledger(admitted)
        self.write('src/feature.py', 'changed elsewhere\n')
        git(self.repo, 'commit', '-q', '-am', 'Someone else commits')
        blocked = self.organize(admitted, ledger)
        self.assertEqual((blocked['status'], blocked['error']['path']), ('blocked', 'checkout'))
        self.calls()

    def test_unsupported_series_are_rejected_and_retained(self):
        admitted = self.admit('dk-commit-split')
        evidence = splitting.collect('dk-commit-split', admitted, repository=self.repo,
                                     capture=self.github.capture(None))
        governing = splitting.effective('dk-commit-split', admitted)
        records = evidence['records']
        foreign, mixed, fixup = (item['sha'] for item in records)
        base = {'outcome': 'reorganize', 'assessments': [{'commit': sha, 'reason': 'r'} for sha in
                                                          (foreign, mixed, fixup)], 'summary': 's', 'questions': []}
        good = [{'message': 'Fix a typo in the notes', 'units': [{'commit': mixed, 'paths': ['docs/notes.md']}],
                 'reason': 'r'},
                {'message': 'Add the feature', 'units': [{'commit': mixed, 'paths': ['src/feature.py']},
                                                         {'commit': fixup, 'paths': ['src/feature.py']}],
                 'reason': 'r'}]
        splitting.check_commits({**base, 'series': good}, evidence, records, governing)
        cases = {
            'series[0].units[0].commit': [{'message': 'Rewrite helper', 'units': [
                {'commit': foreign, 'paths': ['lib/helper.py']}], 'reason': 'r'}] + good,
            'series': good[1:],  # The typo fix would be dropped.
            'series[1].units[2].paths': good[:1] + [{**good[1], 'units': good[1]['units'] + [
                {'commit': fixup, 'paths': ['src/feature.py']}]}],
            'series[0].message': [{**good[0], 'message': 'Fix typo\n\nCo-authored-by: Claude <noreply@anthropic.com>'}]
            + good[1:],
        }
        for path, series in cases.items():
            with self.subTest(path), self.assertRaises(admission.AdmissionError) as raised:
                splitting.check_commits({**base, 'series': series}, evidence, records, governing)
            self.assertEqual(raised.exception.path, path)
        reordered = [{'message': 'Fixup first', 'units': [{'commit': fixup, 'paths': ['src/feature.py']}],
                      'reason': 'r'},
                     {'message': 'Then the rest', 'units': [{'commit': mixed, 'paths': ['src/feature.py',
                                                                                      'docs/notes.md']}],
                      'reason': 'r'}]
        with self.assertRaisesRegex(admission.AdmissionError, 'original order'):
            splitting.check_commits({**base, 'series': reordered}, evidence, records, governing)
        with self.assertRaisesRegex(admission.AdmissionError, 'every commit is assessed'):
            splitting.check_commits({**base, 'assessments': base['assessments'][1:], 'series': good}, evidence,
                                    records, governing)
        # With Bob's commit owned too, folding his and Ada's work would merge two authors into one commit.
        owned = splitting.collect('dk-commit-split', admitted, repository=self.repo,
                                  capture=self.github.capture(None), owners=[BOB[1]])
        mixed_authors = [{'message': 'Everything\n\nSigned-off-by: Bob Builder <bob@example.invalid>', 'units': [
            {'commit': foreign, 'paths': ['lib/helper.py']}, {'commit': mixed, 'paths': ['src/feature.py',
                                                                                   'docs/notes.md']},
            {'commit': fixup, 'paths': ['src/feature.py']}], 'reason': 'r'}]
        with self.assertRaisesRegex(admission.AdmissionError, 'one author'):
            splitting.check_commits({**base, 'series': mixed_authors}, owned, owned['records'], governing)
        # A session result that fails the checks is retained and never redispatched.
        ledger = self.ledger(admitted)
        bad = self.organize(admitted, ledger, session(lambda task: {**reorganize(task), 'series': []}))
        self.assertEqual(bad['status'], 'blocked', bad)
        self.assertEqual(ledger.attempts(ledger.db.execute('SELECT binding FROM rejections').fetchone()[0]), 1)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)

    def test_publication_needs_its_own_grant_an_exact_lease_and_a_clean_checkout(self):
        admitted = self.admit('dk-commit-split')
        ledger = self.ledger(admitted)
        prepared = self.organize(admitted, ledger, session(reorganize), session(unchanged), rewrite=LOCAL)
        self.assertEqual(splitting.publish('dk-commit-split', admitted, prepared,
                                           publisher=self.publisher('dk-commit-split', admitted, ledger),
                                           audience=self.audience),
                         {'status': 'not_authorized', 'needs': ['rewrite_commits']})
        granted = self.admit('dk-commit-split', authorize=('push',))
        publisher = self.publisher('dk-commit-split', granted, ledger)
        self.write('scratch.txt', 'untracked\n')
        held = splitting.publish('dk-commit-split', granted, prepared, publisher=publisher, audience=self.audience)
        self.assertEqual(held['status'], 'blocked', held)
        self.assertEqual((self.remote_head('feature'), getattr(publisher, 'pushes', 0)), (self.head, 0))
        (self.repo / 'scratch.txt').unlink()
        done = splitting.publish('dk-commit-split', granted, prepared, publisher=publisher, audience=self.audience)
        self.assertEqual(done['status'], 'applied', done)
        self.assertEqual([step['kind'] for step in done['steps']], ['rewrite'])
        self.assertEqual(self.remote_head('feature'), prepared['final']['head'])
        again = splitting.publish('dk-commit-split', granted, prepared, publisher=publisher, audience=self.audience)
        self.assertEqual((again['status'], publisher.pushes), ('applied', 1))

    def test_composed_dk_pr_fix_occurrence_uses_the_same_procedure(self):
        fix = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix this PR.'},
                                  configuration=self.configuration(), invocation=None, deployment=self.deployment,
                                  repository=self.repo, transport=self.transport(), home=self.base / 'home')
        self.assertEqual(fix['preflight'], 'admitted', fix)
        ledger = self.ledger(fix)
        report = self.organize(fix, ledger, session(reorganize), session(unchanged), rewrite=LOCAL,
                               options={'max_passes': 2})
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^{tree}'), self.tree)
        [first, _] = self.fake.tasks
        self.assertEqual((first['instructions'], first['adapter']), ('Fix this PR.', 'inspect-commits'))
        without = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix.'}, configuration=self.configuration(),
                                      invocation={'options': {'tasks': ['address-feedback'], 'finalizers': []}},
                                      deployment=self.deployment, repository=self.repo, transport=self.transport(),
                                      home=self.base / 'home')
        outcome = splitting.organize(without, repository=self.repo, capture=self.github.capture(None),
                                     ledger=ledger, transport=self.transport())
        self.assertEqual((outcome['status'], outcome['error']['path']), ('rejected', 'plan.tasks'))
        self.calls()


# -- dk-pr-split -----------------------------------------------------------------------------------

def scope(task, outcome='split', **split):
    paths = task['context']['changes']
    scopes = {path: 'unrelated' if path == 'docs/notes.md' else 'core' for path in paths}
    body = {'outcome': outcome, 'assessments': [{'path': path, 'scope': scopes[path], 'reason': 'r'} for path in paths],
            'splits': [], 'source_message': None, 'summary': 'Assessed.', 'questions': []}
    if outcome == 'split':
        body['splits'] = [{'id': 'notes', 'kind': 'unrelated', 'paths': ['docs/notes.md'], 'branch': 'split/notes',
                           'title': 'Fix a typo in the notes', 'body': 'Split from ' + PR + ' to keep it focused.',
                           'message': 'Fix a typo in the notes', 'reason': 'Unrelated to the feature.', **split}]
        body['source_message'] = 'Move the notes fix to its own pull request'
    return body


class PrSplit(Fixture):
    def test_kept_work_reports_size_and_is_reused(self):
        admitted = self.admit('dk-pr-split', options={'added_lines_threshold': 1})
        ledger = self.ledger(admitted)
        kept = self.partition(admitted, ledger, session(lambda task: scope(task, 'keep')))
        self.assertEqual(kept['status'], 'kept', kept)
        self.assertEqual(kept['size'], {'added_lines': 5, 'threshold': 1, 'oversized': True})
        self.assertEqual(self.partition(admitted, ledger)['session'], False)
        self.assertEqual(self.engine.sessions['dk-pr-split'], 1)

    def test_split_preserves_every_change_authorship_and_unrelated_dirty_work(self):
        before = self.dirty()
        admitted = self.admit('dk-pr-split')
        ledger = self.ledger(admitted)
        refs = git(self.repo, 'for-each-ref')
        proposed = self.partition(admitted, ledger, session(scope))
        self.assertEqual(proposed['status'], 'assessed', proposed)
        self.assertEqual((git(self.repo, 'for-each-ref'), self.status()), (refs, before))
        prepared = self.partition(admitted, ledger, rewrite=LOCAL)
        self.assertEqual(prepared['status'], 'prepared', prepared)
        self.assertEqual(self.engine.sessions['dk-pr-split'], 1)
        [split] = prepared['splits']
        self.assertEqual(git(self.repo, 'rev-parse', 'split/notes'), split['commit'])
        self.assertEqual(git(self.repo, 'rev-parse', 'split/notes^'), self.target)  # Built on the target.
        self.assertEqual(git(self.repo, 'diff', '--name-only', self.target, 'split/notes'), 'docs/notes.md')
        self.assertEqual(git(self.repo, 'show', 'split/notes:docs/notes.md'), 'The notes')
        self.assertEqual(self.authors(self.target, 'split/notes'), [f'{ADA[0]} <{ADA[1]}> 1700000200 +0000'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^'), self.head)  # No history rewrite.
        self.assertEqual(git(self.repo, 'rev-parse', prepared['recovery']), self.head)
        self.assertEqual(git(self.repo, 'diff', '--name-only', self.head, 'HEAD'), 'docs/notes.md')
        self.assertEqual((self.repo / 'docs/notes.md').read_text(), 'Teh notes\n')
        # The source and the split together hold the reviewed tree: nothing was dropped.
        combined = git(self.repo, 'diff', 'HEAD', self.head)
        self.assertEqual(combined, git(self.repo, 'diff', self.target, 'split/notes'))
        self.assertEqual(self.status(), before)
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--', 'docs/notes.md'), '')

    def test_partial_publication_failure_is_reconciled_without_a_resend(self):
        admitted = self.admit('dk-pr-split', authorize=('create_pr', 'push'))
        ledger = self.ledger(admitted)
        prepared = self.partition(admitted, ledger, session(scope), rewrite=LOCAL)
        publisher = self.publisher('dk-pr-split', admitted, ledger)
        self.github.fault = 'before'  # The draft PR request is sent but never lands.
        first = splitting.publish('dk-pr-split', admitted, prepared, publisher=publisher, audience=self.audience)
        self.assertEqual(first['status'], 'pending', first)
        self.assertEqual([step['kind'] for step in first['steps']], ['split_push', 'split_pr'])
        self.assertEqual((self.remote_head('split/notes'), self.remote_head('feature')),
                         (prepared['splits'][0]['commit'], self.head))  # The source keeps its work.
        self.github.fault = None
        second = splitting.publish('dk-pr-split', admitted, prepared, publisher=publisher, audience=self.audience)
        self.assertEqual(second['status'], 'pending', second)
        self.assertEqual(self.github.counts['write'], 1)  # Reconciled read-only, never resent.
        # The request lands late; reconciliation observes it and the source push follows.
        self.github.request('POST', 'repos/o/r/pulls', publisher._load(second['steps'][-1]['id'])['intent']['payload']
                            | {'push_operation': None})
        self.github.calls.pop()
        done = splitting.publish('dk-pr-split', admitted, prepared, publisher=publisher, audience=self.audience)
        self.assertEqual(done['status'], 'applied', done)
        self.assertEqual([step['kind'] for step in done['steps']], ['split_push', 'split_pr', 'push'])
        self.assertEqual(self.remote_head('feature'), prepared['source']['head'])
        self.assertEqual((publisher.pushes, len(self.github.split_pulls)), (2, 1))
        rerun = splitting.partition(admitted, repository=self.repo, capture=self.github.capture(None), ledger=ledger,
                                    transport=self.transport(), rewrite=LOCAL)
        self.assertEqual((rerun['status'], rerun['reused']), ('prepared', True))
        self.calls()

    def test_policies_core_work_and_other_authors_bound_the_split(self):
        admitted = self.admit('dk-pr-split')
        evidence = splitting.collect('dk-pr-split', admitted, repository=self.repo, capture=self.github.capture(None))
        governing = splitting.effective('dk-pr-split', admitted)
        task = {'context': {'changes': sorted(evidence['changes'])}}
        splitting.check_split(scope(task), evidence, governing)
        bodies = {
            'splits[0].paths': lambda body: body['splits'][0].update(paths=['lib/helper.py']) or body['assessments'][
                [item['path'] for item in body['assessments']].index('lib/helper.py')].update(scope='unrelated'),
            'splits[0].body': lambda body: body['splits'][0].update(body='No link back.'),
            'splits[0].branch': lambda body: body['splits'][0].update(branch='feature'),
            # Nothing core would stay: no size-only excuse discards the PR's required work.
            'splits': lambda body: [item.update(scope='related' if item['path'] == 'lib/helper.py' else 'unrelated')
                                    for item in body['assessments']] and
            body['splits'][0].update(paths=['docs/notes.md', 'src/feature.py']),
        }
        for path, change in bodies.items():
            body = scope(task)
            change(body)
            with self.subTest(path), self.assertRaises(admission.AdmissionError) as raised:
                splitting.check_split(body, evidence, governing)
            self.assertEqual(raised.exception.path, path)
        for branch in ('a//b', 'a/.b', 'x.lock/y', 'a/b.'):
            body = scope(task, branch=branch)
            with self.subTest(branch), self.assertRaises(admission.AdmissionError) as raised:
                splitting.check_split(body, evidence, governing)
            self.assertEqual(raised.exception.path, 'splits[0].branch')
        submodule = copy.deepcopy(evidence)
        submodule['changes']['docs/notes.md'][1] = ['160000', self.target]
        with self.assertRaisesRegex(admission.AdmissionError, 'submodule') as raised:
            splitting.check_split(scope(task), submodule, governing)
        self.assertEqual(raised.exception.path, 'splits[0].paths')
        disabled = splitting.effective('dk-pr-split', self.admit('dk-pr-split',
                                                                  options={'evacuate_unrelated': 'disabled'}))
        with self.assertRaisesRegex(admission.AdmissionError, 'disabled'):
            splitting.check_split(scope(task), evidence, disabled)

    def test_manual_mode_needs_an_approval_of_the_concrete_plan(self):
        admitted = self.admit('dk-pr-split', options={'evacuate_unrelated': 'manual'})
        ledger = self.ledger(admitted)
        proposed = self.partition(admitted, ledger, session(scope), rewrite=LOCAL)
        self.assertEqual(proposed['status'], 'proposed', proposed)
        self.assertIsNone(splitting._ref(self.repo, 'refs/heads/split/notes'))
        wrong = self.partition(admitted, ledger, rewrite=LOCAL, approval={**LOCAL, 'plan': 'other'})
        self.assertEqual(wrong['status'], 'proposed')
        approved = self.partition(admitted, ledger, rewrite=LOCAL, approval={**LOCAL, 'plan': proposed['plan']})
        self.assertEqual(approved['status'], 'prepared', approved)
        self.assertEqual(self.engine.sessions['dk-pr-split'], 1)

    def test_collisions_block_before_any_write(self):
        admitted = self.admit('dk-pr-split')
        ledger = self.ledger(admitted)
        self.write('docs/notes.md', 'Edited, not committed\n')
        dirty = self.partition(admitted, ledger, session(scope), rewrite=LOCAL)
        self.assertEqual((dirty['status'], dirty['error']['path']), ('blocked', 'collision'), dirty)
        git(self.repo, 'checkout', '-q', '--', 'docs/notes.md')
        git(self.repo, 'branch', 'split/notes', self.target)
        taken = self.partition(admitted, ledger, rewrite=LOCAL)
        self.assertEqual((taken['status'], taken['error']['path']), ('blocked', 'collision'))
        git(self.repo, 'branch', '-D', 'split/notes')
        git(self.repo, 'checkout', '-q', 'main')
        self.write('docs/notes.md', 'Target rewrote the notes\n')
        git(self.repo, 'commit', '-q', '-am', 'Target edits notes')
        self.github.ev['metadata']['base']['sha'] = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-q', 'feature')
        moved = self.partition(admitted, ledger, session(scope), rewrite=LOCAL)
        self.assertEqual((moved['status'], moved['error']['path']), ('blocked', 'collision'), moved)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)
        self.assertIsNone(splitting._ref(self.repo, 'refs/heads/split/notes'))

    def test_a_moved_symlink_is_compared_by_its_link_text(self):
        (self.repo / 'docs/link.md').symlink_to('notes.md')
        git(self.repo, 'add', 'docs/link.md')
        git(self.repo, 'commit', '-q', '-m', 'Link the notes', date='1700000350 +0000')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'push', '-q', str(self.remote), 'feature')
        self.github.ev['metadata']['head']['sha'] = self.head
        admitted = self.admit('dk-pr-split')
        ledger = self.ledger(admitted)

        def linked(task):
            body = scope(task, paths=['docs/link.md', 'docs/notes.md'])
            for item in body['assessments']:
                if item['path'] == 'docs/link.md':
                    item['scope'] = 'unrelated'
            return body
        prepared = self.partition(admitted, ledger, session(linked), rewrite=LOCAL)
        self.assertEqual(prepared['status'], 'prepared', prepared)
        self.assertFalse((self.repo / 'docs/link.md').is_symlink())
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')

    def test_interrupted_split_reconciles_refs_and_the_moved_paths(self):
        admitted = self.admit('dk-pr-split')
        ledger = self.ledger(admitted)
        real, updates = splitting.git, []

        def crash(repository, *args, **kwargs):
            if args[0] == 'checkout':
                raise KeyboardInterrupt('interrupted before the checkout follows')
            return real(repository, *args, **kwargs)
        with mock.patch.object(splitting, 'git', crash), self.assertRaises(KeyboardInterrupt):
            self.partition(admitted, ledger, session(scope), rewrite=LOCAL)
        self.assertNotEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)
        resumed = self.partition(admitted, ledger, rewrite=LOCAL)
        self.assertEqual((resumed['status'], resumed['reused']), ('prepared', True), resumed)
        self.assertEqual((self.repo / 'docs/notes.md').read_text(), 'Teh notes\n')
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual(self.engine.sessions['dk-pr-split'], 1)

    def test_composed_dk_pr_fix_occurrence_uses_the_same_procedure(self):
        fix = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix this PR.'},
                                  configuration=self.configuration(), invocation=None, deployment=self.deployment,
                                  repository=self.repo, transport=self.transport(), home=self.base / 'home')
        report = self.partition(fix, self.ledger(fix), session(scope), rewrite=LOCAL)
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(self.fake.tasks[0]['adapter'], 'assess-scope')
        self.assertEqual(git(self.repo, 'diff', 'HEAD', self.head), git(self.repo, 'diff', self.target, 'split/notes'))


# -- Commit on request -----------------------------------------------------------------------------

class CommitOnRequest(Fixture):
    def test_a_current_draft_commits_only_its_included_paths_when_asked(self):
        self.write('src/feature.py', 'def feature():\n    return helper() + 2\n')
        self.write('README.md', 'Unrelated staged edit\n')
        git(self.repo, 'add', 'README.md')
        self.write('scratch.txt', 'untracked\n')
        admitted = drafting.preflight('dk-commit-message', request={'instructions': 'Commit the feature.', 'pr': PR},
                                      configuration=self.configuration(), deployment=self.deployment,
                                      repository=self.repo, transport=self.transport())
        state = self.base / 'state'
        drafts = drafting.Ledger(drafting.ledger_path(state, admitted['snapshot']['request']['logical_run']),
                                 logical_run=admitted['snapshot']['request']['logical_run'], target=PR)
        self.addCleanup(drafts.close)
        output = commit_message('Bump the feature result', included=('src/feature.py',),
                                excluded=(('README.md', 'Unrelated.'), ('scratch.txt', 'Scratch.')),
                                claims=[{'text': 'The result grows.', 'sources': ['diff:unstaged']}])
        report = drafting.draft('dk-commit-message', admitted, repository=self.repo, ledger=drafts,
                                transport=self.transport(session(output)))
        self.assertEqual(report['status'], 'drafted', report)
        ledger = self.ledger(admitted)
        refused = splitting.commit(admitted, report, repository=self.repo, drafting_ledger=drafts, ledger=ledger,
                                   authorization=None)
        self.assertEqual(refused['status'], 'rejected')
        committed = splitting.commit(admitted, report, repository=self.repo, drafting_ledger=drafts, ledger=ledger,
                                     authorization=LOCAL)
        self.assertEqual(committed['status'], 'committed', committed)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^'), self.head)
        self.assertEqual(git(self.repo, 'show', '--name-only', '--format=%s', 'HEAD').split('\n'),
                         ['Bump the feature result', '', 'src/feature.py'])
        self.assertEqual(git(self.repo, 'status', '--porcelain=v1', '--untracked-files=all'),
                         'M  README.md\n?? scratch.txt'.strip())
        again = splitting.commit(admitted, report, repository=self.repo, drafting_ledger=drafts, ledger=ledger,
                                 authorization=LOCAL)
        self.assertEqual((again['commit'], again['reused']), (committed['commit'], True))
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), committed['commit'])

    def test_successive_drafts_in_one_run_each_commit_once_at_the_current_time(self):
        self.write('src/feature.py', 'def feature():\n    return helper() + 2\n')
        admitted = drafting.preflight('dk-commit-message', request={'instructions': 'Commit it.', 'pr': PR},
                                      configuration=self.configuration(), deployment=self.deployment,
                                      repository=self.repo, transport=self.transport())
        run = admitted['snapshot']['request']['logical_run']
        drafts = drafting.Ledger(drafting.ledger_path(self.base / 'state', run), logical_run=run, target=PR)
        self.addCleanup(drafts.close)
        ledger = self.ledger(admitted)
        commits = []
        for subject, body in (('Return two more', 'helper() + 2'), ('Return three more', 'helper() + 3')):
            self.write('src/feature.py', f'def feature():\n    return {body}\n')
            output = commit_message(subject, included=('src/feature.py',), excluded=(),
                                    claims=[{'text': 'The result grows.', 'sources': ['diff:unstaged']}])
            report = drafting.draft('dk-commit-message', admitted, repository=self.repo, ledger=drafts,
                                    transport=self.transport(session(output)))
            with mock.patch.object(splitting.time, 'time', return_value=1800000000 + len(commits)):
                done = splitting.commit(admitted, report, repository=self.repo, drafting_ledger=drafts,
                                        ledger=ledger, authorization=LOCAL)
            self.assertEqual((done['status'], done.get('reused')), ('committed', None), done)
            commits.append(done['commit'])
            again = splitting.commit(admitted, report, repository=self.repo, drafting_ledger=drafts, ledger=ledger,
                                     authorization=LOCAL)
            self.assertEqual((again['commit'], again['reused']), (done['commit'], True))
        self.assertEqual(git(self.repo, 'rev-list', f'{self.head}..HEAD').split('\n'), commits[::-1])
        self.assertEqual(git(self.repo, 'log', '--format=%s %ad %cd', '--date=raw', f'{self.head}..HEAD').split('\n'),
                         ['Return three more 1800000001 +0000 1800000001 +0000',
                          'Return two more 1800000000 +0000 1800000000 +0000'])
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')

    def test_a_stale_draft_is_never_committed(self):
        self.write('src/feature.py', 'def feature():\n    return 2\n')
        admitted = drafting.preflight('dk-commit-message', request={'instructions': 'Commit it.', 'pr': PR},
                                      configuration=self.configuration(), deployment=self.deployment,
                                      repository=self.repo, transport=self.transport())
        drafts = drafting.Ledger(drafting.ledger_path(self.base / 'state', admitted['snapshot']['request']
                                                      ['logical_run']),
                                 logical_run=admitted['snapshot']['request']['logical_run'], target=PR)
        self.addCleanup(drafts.close)
        output = commit_message('Return two', included=('src/feature.py',), excluded=(),
                                claims=[{'text': 'Returns two.', 'sources': ['diff:unstaged']}])
        report = drafting.draft('dk-commit-message', admitted, repository=self.repo, ledger=drafts,
                                transport=self.transport(session(output)))
        self.write('src/feature.py', 'def feature():\n    return 3\n')
        stale = splitting.commit(admitted, report, repository=self.repo, drafting_ledger=drafts,
                                 ledger=self.ledger(admitted), authorization=LOCAL)
        self.assertEqual(stale['status'], 'stale', stale)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)


class GitFailures(unittest.TestCase):
    def test_a_git_timeout_or_missing_git_is_blocked(self):
        for error in (subprocess.TimeoutExpired(['git'], 60), FileNotFoundError('git')):
            with self.subTest(type(error).__name__), mock.patch.object(splitting.subprocess, 'run', side_effect=error):
                with self.assertRaises(splitting.Blocked) as caught:
                    splitting.git('.', 'status')
                self.assertEqual(caught.exception.path, 'git')

    def test_non_utf8_commit_data_and_paths_block_and_dirty_names_stay_encodable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(*args, data=None):
                return subprocess.run(git_fixture.command('-c', 'commit.gpgsign=false', *args), cwd=root,
                                      input=data, check=True, capture_output=True,
                                      env={**git_fixture.environment(), 'GIT_AUTHOR_NAME': 'Ada',
                                           'GIT_AUTHOR_EMAIL': 'ada@example.invalid', 'GIT_COMMITTER_NAME': 'Ada',
                                           'GIT_COMMITTER_EMAIL': 'ada@example.invalid'}).stdout.decode().strip()
            run('init', '-q')
            (root / 'a.txt').write_text('a\n')
            run('add', 'a.txt')
            run('commit', '-q', '-m', 'base')
            base = run('rev-parse', 'HEAD')
            # A legacy latin-1 commit message.
            latin = run('-c', 'i18n.commitEncoding=latin1', 'commit-tree', base + '^{tree}', '-p', base,
                        data='Caf\xe9\n'.encode('latin-1'))
            with self.assertRaises(splitting.Blocked) as caught:
                splitting.series(root, base, latin)
            self.assertEqual(caught.exception.path, 'commits')
            # A non-UTF-8 path, committed and left untracked.
            name = os.fsdecode(b'caf\xe9.txt')
            (root / name).write_text('x\n')
            run('add', '--', name)
            run('commit', '-q', '-m', 'add a latin-1 path')
            head = run('rev-parse', 'HEAD')
            with self.assertRaises(splitting.Blocked) as caught:
                splitting.series(root, base, head)
            self.assertEqual(caught.exception.path, 'changes')
            (root / os.fsdecode(b'new\xe9.txt')).write_text('y\n')
            dirty = splitting._dirty(root)
            self.assertEqual(len(dirty), 1)
            admission.canonical(dirty)


if __name__ == '__main__':
    unittest.main()
