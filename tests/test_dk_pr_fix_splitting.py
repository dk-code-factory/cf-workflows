"""Commit organization and PR splitting through the installed dk-pr-fix helpers (#34).

Each case uses a disposable history with a commit by another author (Bob, with a
Signed-off-by), a mixed commit and a fixup by the actor, a disposable bare
remote, a fresh state namespace and run ledger, and the fail-closed fake GitHub
of the drafting tests extended with draft-PR routes: any route it does not
model raises instead of reaching a live service, and writes and pushes are
counted. The installed helpers run in-process, as the helper containers would
run them for task.yml, splitting.yml and the publication stage. Splitting and
audience sessions are only the reports a test hands the helpers, and every
claim that would dispatch one is counted. Validation commands are local Python
one-liners. Nothing here starts an engine, provider or host, runs the YAML
graph or certifies GitHub conformance.
"""
import ast
from collections import Counter
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import app, publishing, splitting
from prfix.core import canonical, read_artifact
from prfix.prepare import prepare
from test_dk_pr_fix_drafting import GitHub as DraftingGitHub
from test_dk_pr_fix_publication import LocalPublication
from test_dk_pr_review import AGENTS, PR_URL
from test_publication_skills import Helpers, grant

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, splitting as library  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import CAPABILITIES, FakeTransport, PreflightFixture  # noqa: E402

INSTALL = ROOT / 'cf_workflows' / 'workflows' / 'dk-pr-fix'
BOB = ('Bob Builder', 'bob@example.invalid')
ACTOR = ('Fixture', 'fixture@example.invalid')
LOCAL = {'request_id': 'user-2', 'instruction': 'Reorganize my commits locally.', 'source': 'chat:2'}
PASS = ['python3', '-c', 'import os, sys; sys.exit(0 if os.path.exists("code.py") else 4)']
NO_TOKEN = ['python3', '-c', 'import os, sys; sys.exit(5 if "GH_TOKEN" in os.environ else 0)']
NEEDS_FEATURE = ['python3', '-c', 'import os, sys; sys.exit(0 if os.path.exists("src/feature.py") else 3)']


def git(root, *args, author=ACTOR, date='1700000000 +0000'):
    env = {**git_fixture.environment(), 'GIT_AUTHOR_NAME': author[0], 'GIT_AUTHOR_EMAIL': author[1],
           'GIT_AUTHOR_DATE': date, 'GIT_COMMITTER_DATE': date}
    return subprocess.run(git_fixture.command('-c', 'commit.gpgsign=false', *args), cwd=root, check=True,
                          capture_output=True, env=env, timeout=30).stdout.decode().strip()


class GitHub(DraftingGitHub):
    """Adds draft PR creation and the repository's PR listing; any other route still fails."""

    def __init__(self, head, target, remote, **kwargs):
        super().__init__(head, target, **kwargs)
        self.remote = remote
        self.pulls = [{'number': 7}]

    def pages(self, path):
        if path == 'repos/owner/repo/pulls?state=all&per_page=100':
            return copy.deepcopy(self.pulls)
        return super().pages(path)

    def request(self, method, path, payload=None):
        if (method, path) == ('POST', 'repos/owner/repo/pulls'):
            self.calls['write'] += 1
            if self.fault == 'before':
                raise OSError('fixture response lost before effect')
            owner, branch = payload['head'].split(':', 1)
            head = git(self.remote, 'rev-parse', 'refs/heads/' + branch)
            meta = self.evidence['metadata']
            created = {'number': 8 + len(self.pulls) - 1, 'state': 'open', 'draft': payload['draft'],
                       'title': payload['title'], 'body': payload['body'],
                       'user': copy.deepcopy(self.evidence['viewer']),
                       'head': {'sha': head, 'ref': branch, 'repo': copy.deepcopy(meta['head']['repo'])},
                       'base': copy.deepcopy(meta['base'])}
            self.pulls.append(created)
            self._lose()
            return copy.deepcopy(created)
        return super().request(method, path, payload)


class Fixture(Helpers):
    def setUp(self):
        super().setUp()
        # A PR history on the fixture branch: Bob's signed helper, then the actor's mixed commit and fixup.
        git(self.root, 'reset', '-q', '--hard', self.target)
        self.write('lib/helper.py', 'def helper():\n    return 1\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'Add helper\n\nSigned-off-by: Bob Builder <bob@example.invalid>',
            author=BOB, date='1700000100 +0000')
        self.foreign = git(self.root, 'rev-parse', 'HEAD')
        self.write('src/feature.py', 'def feature():\n    return helper()\n')
        self.write('notes file.md', 'first\nsecond, fixed\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'Add feature and fix a typo', date='1700000200 +0000')
        self.write('src/feature.py', 'def feature():\n    return helper() + 1\n')
        git(self.root, 'commit', '-q', '-am', 'fixup! Add feature', date='1700000300 +0000')
        self.commit = git(self.root, 'rev-parse', 'HEAD')
        self.tree = git(self.root, 'rev-parse', 'HEAD^{tree}')
        temp = tempfile.TemporaryDirectory(prefix='cfw split remote ')
        self.addCleanup(temp.cleanup)
        self.remote = Path(temp.name) / 'remote.git'
        git(Path(temp.name), 'init', '-q', '--bare', str(self.remote))
        git(self.root, 'push', '-q', str(self.remote), 'change', 'main')
        self.github = GitHub(self.commit, self.target, self.remote)
        self.github.git_head = self.commit
        self.github.evidence['metadata']['user'] = {'id': 1, 'login': 'maintainer'}  # The actor's own PR.
        self.sessions = Counter()
        remote = self.remote

        class Local(LocalPublication):
            pushed = 0

            def _git_remote(self, intent):
                return str(remote)

            def _git(self, *args, input=None):
                if args and args[0] == 'push':
                    type(self).pushed += 1
                return super()._git(*args, input=input)
        self.publication = Local
        patcher = mock.patch.object(publishing, 'Publication', Local)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rel, body):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def head(self, ref='HEAD'):
        return git(self.root, 'rev-parse', ref)

    def remote_head(self, branch):
        result = subprocess.run(git_fixture.command('--git-dir', str(self.remote), 'rev-parse', '--verify', '-q',
                                                    'refs/heads/' + branch), capture_output=True,
                                env=git_fixture.environment(), timeout=30)
        return result.stdout.decode().strip() or None

    def authors(self, base, head):
        return git(self.root, 'log', '--reverse', '--format=%an <%ae> %ad', '--date=raw', f'{base}..{head}').split('\n')

    def splitting_plan(self, entry, *, authorizations=None, permissions=None, settings=None, run='split_fixture',
                       tasks=None):
        adapter = splitting.ENTRIES[entry]['adapter']
        table = {adapter: dict(admission.BUNDLED_ADAPTERS[adapter])}
        controls = {'tasks': tasks or [adapter, {'stage': 'publication'}], 'finalizers': [], 'reviewers': [],
                    'adapters': table, 'accounts': [], 'settings': settings or {}, 'resources': [], 'turns': 8,
                    'mode': 'fix', 'permissions': permissions or {}, 'entry': entry}  # commit-split.yml, pr-split.yml.
        request = {'pr': PR_URL, 'instructions': 'Organize this PR.', 'logical_run': run,
                   'authorizations': authorizations or {}}
        result = prepare(self.root, request, controls, 'exec-1', self.github, self.definitions)
        return read_artifact(self.root, result['plan'])

    def local(self, entry, commands=(), **extra):
        return {'splitting': {entry: {'rewrite': LOCAL, **({'validation_commands': list(commands)} if commands else {}),
                                      **extra}}}

    def claim(self, plan, ledger):
        task = splitting.occurrence(plan)
        claimed = app.prepare_claim(self.root, self.github, ledger, plan, {'task': task['id']})
        if claimed['invoke']:
            self.sessions[task['skill']] += 1  # One splitting session dispatched for this claim.
        return claimed

    def submit(self, plan, ledger, claimed, output, round_number=0, agent=AGENTS[0], **values):
        report = self.report(deliverables={'split': output}, **values)
        return app.accept(self.root, self.github, ledger, plan,
                          {'claim': claimed['claim'], 'agent': agent, 'report': report, 'round': round_number})

    def assessed(self, plan, ledger, output, agent=AGENTS[0]):
        claimed = self.claim(plan, ledger)
        self.assertTrue(claimed['invoke'])
        accepted = self.submit(plan, ledger, claimed, output(claimed['task_input']['splitting']), agent=agent)
        self.assertTrue(accepted['complete'], accepted)
        return claimed

    def stage(self, plan, ledger):
        """splitting.yml: build, validate each new intermediate commit, then apply when every one passed."""
        built = splitting.build(self.root, self.github, ledger, plan, {})
        for item in built['items']:
            splitting.validate(self.root, self.github, ledger, plan,
                               {'operation': built['operation'], 'commit': item['commit']})
        applied = (splitting.apply(self.root, self.github, ledger, plan, {}) if built['operation'] else None)
        return built, applied

    def listed(self, plan, ledger):
        return publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ordinary'})

    def published(self, plan, ledger, item):
        receipt = self.publish(plan, ledger, item)
        if item['kind'] in ('push', 'rewrite'):
            # The PR now shows the pushed head.
            self.github.evidence['metadata']['head']['sha'] = self.github.git_head = item['payload']['new_head']
        return receipt

    def pushes(self):
        return self.publication.pushed


def assessed(ctx, reason='One logical change.'):
    return [{'commit': item['sha'], 'reason': reason} for item in ctx['context']['commits']]


def unchanged(ctx):
    return {'outcome': 'no_change', 'assessments': assessed(ctx), 'series': [], 'summary': 'Boundaries hold.',
            'questions': []}


def reorganize(ctx, typo='Fix a typo in the notes\n'):
    """Fix the typo first, then the feature with its fixup folded in: a tree-preserving reorder."""
    _, mixed, fixup = [item['sha'] for item in ctx['context']['commits']]
    return {'outcome': 'reorganize', 'assessments': assessed(ctx, 'Mixed commit and a fixup.'), 'series': [
        {'message': typo, 'units': [{'commit': mixed, 'paths': ['notes file.md']}], 'reason': 'An unrelated fix.'},
        {'message': 'Add the feature\n', 'units': [{'commit': mixed, 'paths': ['src/feature.py']},
                                                   {'commit': fixup, 'paths': ['src/feature.py']}],
         'reason': 'The feature with its fixup folded in.'}],
        'summary': 'Split the mixed commit and fold the fixup.', 'questions': []}


def scoped(ctx, path='notes file.md'):
    return [{'path': name, 'scope': 'unrelated' if name == path else 'core', 'reason': 'Assessed.'}
            for name in ctx['context']['changes']]


def split(ctx):
    return {'outcome': 'split', 'assessments': scoped(ctx), 'splits': [{
        'id': 'notes', 'kind': 'unrelated', 'paths': ['notes file.md'], 'branch': 'split/notes',
        'title': 'Fix a typo in the notes', 'body': 'Moved out of ' + PR_URL + '.\n',
        'message': 'Fix a typo in the notes\n', 'reason': 'Unrelated to the feature.'}],
        'source_message': 'Move the notes fix to its own pull request\n', 'summary': 'Move the typo fix.',
        'questions': []}


class CommitSplit(Fixture):
    def test_reorder_is_built_validated_applied_and_assessed_again_before_one_rewrite(self):
        settings = self.local('dk-commit-split', [PASS, NO_TOKEN])
        plan = self.splitting_plan('dk-commit-split', authorizations={'push': grant('push')},
                                   permissions={'rewrite_commits': True}, settings=settings)
        ledger = self.ledger(plan)
        claimed = self.assessed(plan, ledger, reorganize)
        ctx = claimed['task_input']['splitting']
        self.assertEqual((ctx['entry'], ctx['skills'], ctx['writes'], ctx['publication'], ctx['context']['pass']),
                         ('dk-commit-split', [], False, False, 1))
        self.assertEqual([(item['owned'], item['fixed']) for item in ctx['context']['commits']],
                         [(False, True), (True, False), (True, False)])
        # The assessment changed nothing; the publication stage builds, validates, then moves the branch.
        self.assertEqual(self.head(), self.commit)
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'fixture-token'}):  # Not in a command's environment.
            built, applied = self.stage(plan, ledger)
        self.assertEqual((built['status'], len(built['items']), applied['status']), ('built', 2, 'prepared'))
        rebuilt = self.head()
        self.assertNotEqual(rebuilt, self.commit)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD^{tree}'), self.tree)
        self.assertEqual(self.head(applied['report']['recovery']), self.commit)
        self.assertEqual(self.authors(self.target, rebuilt), [
            'Bob Builder <bob@example.invalid> 1700000100 +0000', 'Fixture <fixture@example.invalid> 1700000200 +0000',
            'Fixture <fixture@example.invalid> 1700000300 +0000'])
        self.assertEqual(self.head('HEAD~2'), self.foreign)  # Bob's commit is never rewritten.
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        # The rebuilt series is not published before the next pass assesses it.
        listing = self.listed(plan, ledger)
        self.assertEqual((listing['items'], listing['unpublished']), ([], True))
        second = self.assessed(plan, ledger, unchanged, agent=AGENTS[1])
        self.assertEqual(second['task_input']['splitting']['context']['pass'], 2)
        self.assertEqual(self.stage(plan, ledger)[0]['status'], 'no_op')
        [item] = self.listed(plan, ledger)['items']
        self.assertEqual((item['kind'], item['head'], item['lease'], item['sources']),
                         ('rewrite', rebuilt, self.commit, ['splitting:' + applied['operation']]))
        self.published(plan, ledger, item)
        self.assertEqual((self.remote_head('change'), self.pushes(), self.writes()), (rebuilt, 1, 0))
        # Relisting only reconciles the applied rewrite, and the current pass is reused without a session.
        for again in self.listed(plan, ledger)['items']:
            self.published(plan, ledger, again)
        self.assertFalse(self.claim(plan, ledger)['invoke'])
        self.assertEqual((self.pushes(), self.sessions), (1, Counter({'inspect-commits': 2})))

    def test_a_failed_validation_moves_nothing_and_lists_nothing(self):
        plan = self.splitting_plan('dk-commit-split', authorizations={'push': grant('push')},
                                   permissions={'rewrite_commits': True},
                                   settings=self.local('dk-commit-split', [PASS, NEEDS_FEATURE]))
        ledger = self.ledger(plan)
        self.assessed(plan, ledger, reorganize)
        built = splitting.build(self.root, self.github, ledger, plan, {})
        typo, feature = [item['commit'] for item in built['items']]
        results = {item['commit']: splitting.validate(self.root, self.github, ledger, plan, {
            'operation': built['operation'], 'commit': item['commit']}) for item in built['items']}
        # The typo fix alone lacks src/feature.py: its second command fails, the first passed.
        self.assertEqual((results[typo]['state'], [item['exit'] for item in results[typo]['result']['commands']]),
                         ('failed', [0, 3]))
        self.assertEqual(results[feature]['state'], 'passed')
        # A replay keeps the retained result and runs nothing again.
        with mock.patch.object(splitting, '_run', side_effect=AssertionError('ran again')):
            self.assertEqual(splitting.validate(self.root, self.github, ledger, plan,
                                                {'operation': built['operation'], 'commit': typo}), results[typo])
        with self.assertRaisesRegex(ValueError, 'validation failed on intermediate commits; nothing was moved'):
            splitting.apply(self.root, self.github, ledger, plan, {})
        self.assertEqual((self.head(), git(self.root, 'for-each-ref', 'refs/cf-workflows')), (self.commit, ''))
        self.assertEqual(self.listed(plan, ledger)['items'], [])
        # The failed operation stays pending for this logical run: a rerun neither rebuilds nor moves anything.
        self.assertEqual(splitting.build(self.root, self.github, ledger, plan, {})['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'validation failed on intermediate commits'):
            splitting.apply(self.root, self.github, ledger, plan, {})
        with self.assertRaisesRegex(ValueError, 'new intermediate commits'):
            splitting.validate(self.root, self.github, ledger, plan,
                               {'operation': built['operation'], 'commit': self.commit})
        self.assertEqual(self.pushes(), 0)

    def test_exhausted_passes_publish_the_last_applied_series_with_a_concern(self):
        plan = self.splitting_plan('dk-commit-split', authorizations={'push': grant('push')},
                                   permissions={'rewrite_commits': True},
                                   settings=self.local('dk-commit-split', max_passes=1))
        ledger = self.ledger(plan)
        self.assessed(plan, ledger, reorganize)
        applied = self.stage(plan, ledger)[1]
        rebuilt = self.head()
        # max_passes=1 is spent by the one applied pass: no second session runs and nothing more is built.
        self.assertFalse(self.claim(plan, ledger)['invoke'])
        self.assertEqual(self.stage(plan, ledger)[0]['status'], 'exhausted')
        self.assertEqual((self.head(), self.sessions), (rebuilt, Counter({'inspect-commits': 1})))
        listing = self.listed(plan, ledger)
        [item] = listing['items']
        self.assertEqual((item['kind'], item['head'], item['lease'], item['sources']),
                         ('rewrite', rebuilt, self.commit, ['splitting:' + applied['operation']]))
        self.assertEqual(listing['concerns'], ['stopped after max_passes=1 with the series still changing'])

    def test_without_a_local_grant_the_verified_series_is_only_assessed(self):
        plan = self.splitting_plan('dk-commit-split', authorizations={'push': grant('push')},
                                   permissions={'rewrite_commits': True})
        ledger = self.ledger(plan)
        self.assessed(plan, ledger, reorganize)
        self.assertEqual(self.stage(plan, ledger)[0], {'operation': None, 'items': [], 'status': 'assessed'})
        self.assertEqual((self.head(), self.listed(plan, ledger)['items'], self.pushes()), (self.commit, [], 0))
        # Unchanged inputs reuse the accepted assessment without a session.
        self.assertFalse(self.claim(plan, self.ledger(plan))['invoke'])
        self.assertEqual(self.sessions, Counter({'inspect-commits': 1}))

    def test_without_push_authority_the_rewritten_branch_stays_local(self):
        plan = self.splitting_plan('dk-commit-split', settings=self.local('dk-commit-split'))
        ledger = self.ledger(plan)
        self.assessed(plan, ledger, reorganize)
        self.assertEqual(self.stage(plan, ledger)[1]['status'], 'prepared')
        self.assessed(plan, ledger, unchanged, agent=AGENTS[1])
        listing = self.listed(plan, ledger)
        self.assertEqual((listing['items'], listing['unpublished']), ([], True))
        self.assertIn('rewrite_commits', listing['blockers'][0])
        self.assertEqual((self.remote_head('change'), self.pushes()), (self.commit, 0))

    def test_unsupported_series_get_a_same_session_correction(self):
        plan = self.splitting_plan('dk-commit-split', settings=self.local('dk-commit-split'))
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger)
        ctx = claimed['task_input']['splitting']
        bob = ctx['context']['commits'][0]['sha']
        rewritten = reorganize(ctx)
        rewritten['series'][0]['units'].append({'commit': bob, 'paths': ['lib/helper.py']})
        dropped = reorganize(ctx)
        dropped['series'][1]['units'].pop()
        operation = {'kind': 'rewrite', 'payload': {'new_head': self.commit}}
        cases = [(rewritten, {}, 'never rewritten'), (dropped, {}, 'exactly once'),
                 (reorganize(ctx), {'operations': [operation]}, 'no operations')]
        for number, (output, extra, error) in enumerate(cases):
            result = self.submit(plan, ledger, claimed, output, round_number=number, **extra)
            self.assertFalse(result['complete'])
            self.assertIn(error, result['feedback']['contract_error'])
        self.assertTrue(self.submit(plan, ledger, claimed, reorganize(ctx), round_number=3)['complete'])
        # Acceptance alone writes nothing.
        self.assertEqual((self.sessions, self.head()), (Counter({'inspect-commits': 1}), self.commit))

    def test_an_interrupted_apply_resumes_from_the_refs_it_finds_and_a_moved_branch_blocks(self):
        plan = self.splitting_plan('dk-commit-split', settings=self.local('dk-commit-split'))
        ledger = self.ledger(plan)
        self.assessed(plan, ledger, reorganize)
        built = splitting.build(self.root, self.github, ledger, plan, {})
        self.assertEqual(built['items'], [])  # No validation commands configured.
        with splitting.Store(self.root, plan, write=True) as store:
            operation = store.advance(built['operation'], 'built', 'applying')
        recovery, branch = operation['value']['refs'][0], operation['value']['refs'][-1]
        git(self.root, 'update-ref', recovery['ref'], recovery['new'])  # Interrupted after the first ref.
        # A rerun of the stage resumes the recorded operation; no new build or session.
        self.assertEqual(splitting.build(self.root, self.github, ledger, plan, {})['status'], 'pending')
        self.assertEqual(splitting.apply(self.root, self.github, ledger, plan, {})['status'], 'prepared')
        self.assertEqual(self.head(), branch['new'])
        # A branch moved by someone else after another build blocks instead of being overwritten.
        git(self.root, 'reset', '-q', '--hard', self.commit)
        other = self.splitting_plan('dk-commit-split', settings=self.local('dk-commit-split'), run='split_other')
        other_ledger = self.ledger(other)
        self.assessed(other, other_ledger, reorganize)
        built = splitting.build(self.root, self.github, other_ledger, other, {})
        git(self.root, 'commit', '-q', '--allow-empty', '-m', 'Concurrent work')
        with self.assertRaisesRegex(ValueError, 'moved since the build'):
            splitting.apply(self.root, self.github, other_ledger, other, {})
        self.assertEqual(self.sessions, Counter({'inspect-commits': 2}))


class Chain(Fixture):
    def test_passes_that_revisit_an_earlier_head_lease_the_first_revision(self):
        plan = {'logical_run': 'chain_fixture', 'pr': {'url': PR_URL}}
        with splitting.Store(self.root, plan, write=True) as store:
            for number, (original, final) in enumerate((('x', 'y'), ('y', 'z'), ('z', 'y'))):
                store.build(f'op{number}', 'dk-commit-split', f'claim{number}', {'original': original, 'final': final})
                store.advance(f'op{number}', 'built', 'applied')
            self.assertEqual(splitting._chain(store, store.get('op2')), 'x')


class PrSplit(Fixture):
    def split_plan(self, authorize=True, commands=(PASS,), run='split_fixture', **extra):
        return self.splitting_plan(
            'dk-pr-split', run=run, authorizations={'create_pr': grant('create_pr'), 'push': grant('push')} if authorize else {},
            permissions={'create_draft_prs': True, 'push_commits': True} if authorize else {},
            settings=self.local('dk-pr-split', commands, **extra))

    def test_split_publishes_each_split_before_the_source_and_reconciles_a_lost_response(self):
        plan = self.split_plan()
        ledger = self.ledger(plan)
        claimed = self.assessed(plan, ledger, split)
        size = claimed['task_input']['splitting']['context']['size']
        self.assertEqual((size['threshold'], size['oversized']), (1000, False))
        built, applied = self.stage(plan, ledger)
        self.assertEqual((len(built['items']), applied['status']), (2, 'prepared'))
        report = applied['report']
        [moved] = report['splits']
        source = self.head()
        # The split holds exactly the moved work on the target; the source keeps everything else.
        self.assertEqual(git(self.root, 'diff', '--name-only', self.target, moved['commit']), 'notes file.md')
        self.assertEqual(git(self.root, 'diff', '--name-only', self.target, source), 'lib/helper.py\nsrc/feature.py')
        self.assertEqual(self.head('refs/heads/split/notes'), moved['commit'])
        self.assertEqual(self.head('HEAD~1'), self.commit)  # The source history is never rewritten.
        self.assertEqual(((self.root / 'notes file.md').read_text(), git(self.root, 'status', '--porcelain')),
                         ('first\nsecond\n', ''))
        # The assessment stays current on its own result: no second session.
        self.assertFalse(self.claim(plan, ledger)['invoke'])
        items = self.listed(plan, ledger)['items']
        self.assertEqual([item['kind'] for item in items], ['split_push', 'split_pr', 'push'])
        # The source keeps its moved work until every split PR is applied.
        with self.assertRaisesRegex(Exception, 'until every split pull request is applied'):
            publishing.publication_prepare(self.root, self.github, ledger, plan, {'proposal': items[2]})
        self.published(plan, ledger, items[0])
        self.assertEqual(self.remote_head('split/notes'), moved['commit'])
        self.github.fault = 'after'
        with self.assertRaises(Exception):
            self.published(plan, ledger, items[1])
        self.github.fault = None
        # The lost draft-PR response is reconciled from the service, never resent.
        receipt = publishing.publication_recover(self.root, self.github, ledger, plan, {'id': items[1]['id']})
        self.assertEqual(receipt['receipt']['result']['number'], 8)
        self.published(plan, ledger, items[2])
        self.assertEqual((self.remote_head('change'), self.pushes(), self.writes(), len(self.github.pulls)),
                         (source, 2, 1, 2))
        for again in self.listed(plan, ledger)['items']:
            self.published(plan, ledger, again)
        self.assertEqual((self.pushes(), self.writes(), self.sessions), (2, 1, Counter({'assess-scope': 1})))

    def test_kept_and_manual_work_builds_nothing(self):
        plan = self.split_plan()
        ledger = self.ledger(plan)
        self.assessed(plan, ledger, lambda ctx: {'outcome': 'keep', 'assessments': scoped(ctx, path=None),
                                                 'splits': [], 'source_message': None, 'summary': 'One change.',
                                                 'questions': []})
        self.assertEqual(self.stage(plan, ledger)[0]['status'], 'kept')
        manual = self.split_plan(evacuate_unrelated='manual', run='split_manual')
        ledger = self.ledger(manual)
        self.assessed(manual, ledger, split)
        self.assertEqual(self.stage(manual, ledger)[0]['status'], 'proposed')
        self.assertEqual((self.head(), self.listed(manual, ledger)['items'], self.pushes()), (self.commit, [], 0))

    def test_unsupported_splits_get_a_same_session_correction(self):
        plan = self.split_plan()
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger)
        ctx = claimed['task_input']['splitting']
        foreign = split(ctx)
        foreign['assessments'] = scoped(ctx, path='lib/helper.py')
        foreign['splits'][0]['paths'] = ['lib/helper.py']
        unlinked = split(ctx)
        unlinked['splits'][0]['body'] = 'Moved out.\n'
        everything = split(ctx)
        everything['assessments'] = [{'path': name, 'scope': 'unrelated', 'reason': 'x'}
                                     for name in ctx['context']['changes']]
        everything['splits'][0]['paths'] = ['notes file.md', 'src/feature.py']
        cases = [(foreign, "another author's work"), (unlinked, 'links back'), (everything, 'core work')]
        for number, (output, error) in enumerate(cases):
            result = self.submit(plan, ledger, claimed, output, round_number=number)
            self.assertFalse(result['complete'])
            self.assertIn(error, result['feedback']['contract_error'])
        self.assertTrue(self.submit(plan, ledger, claimed, split(ctx), round_number=3)['complete'])
        self.assertEqual(self.sessions, Counter({'assess-scope': 1}))

    def test_branch_names_git_rejects_get_a_same_session_correction(self):
        plan = self.split_plan()
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger)
        ctx = claimed['task_input']['splitting']
        for number, branch in enumerate(('split//notes', 'split/.notes', 'split.lock/notes')):
            output = split(ctx)
            output['splits'][0]['branch'] = branch
            result = self.submit(plan, ledger, claimed, output, round_number=number)
            self.assertFalse(result['complete'], branch)
            self.assertIn('a new distinct branch name', result['feedback']['contract_error'])
        self.assertTrue(self.submit(plan, ledger, claimed, split(ctx), round_number=3)['complete'])

    def test_a_moved_symlink_syncs_by_its_link_text(self):
        os.symlink('notes file.md', self.root / 'link.md')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'Link the notes')
        original = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'rm', '-q', '--cached', 'link.md')
        git(self.root, 'commit', '-q', '-m', 'Move the link out')
        final = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'reset', '-q', '--soft', original)
        git(self.root, 'reset', '-q')
        splitting._sync(self.root, {'original': original, 'final': final, 'moved': ['link.md']})
        self.assertFalse(os.path.lexists(self.root / 'link.md'))
        self.assertEqual(git(self.root, 'ls-files', 'link.md'), '')

    def test_an_existing_split_branch_blocks_the_build(self):
        git(self.root, 'branch', 'split/notes', self.target)
        plan = self.split_plan()
        ledger = self.ledger(plan)
        self.assessed(plan, ledger, split)
        with self.assertRaisesRegex(ValueError, 'already exist locally'):
            splitting.build(self.root, self.github, ledger, plan, {})
        self.assertEqual(self.head(), self.commit)


class Prepare(Fixture):
    def test_entry_presets_permissions_and_settings_reject_before_any_effect(self):
        cases = [
            ('dk-commit-split', {'tasks': ['inspect-commits']}, 'runs exactly its preset'),
            ('dk-commit-split', {'authorizations': {'create_pr': grant('c')}}, 'accepts only push'),
            ('dk-commit-split', {'permissions': {'push_commits': True}}, 'enables only rewrite_commits'),
            ('dk-pr-split', {'permissions': {'rewrite_commits': True}}, 'belong to cf-workflows:dk-commit-split'),
            ('dk-pr-split', {'settings': {'splitting': {'dk-pr-split': {'policy': None}}}}, 'library procedure'),
            ('dk-pr-split', {'settings': {'splitting': {'dk-pr-split': {'rewrite': {'request_id': 'x'}}}}},
             'explicit'),
            ('dk-commit-split', {'settings': {'splitting': {'dk-commit-split': {'max_passes': 9}}}}, '1 to 5'),
            ('dk-commit-split', {'settings': {'splitting': {'dk-commit-split': {'validation_commands': [[]]}}}},
             'validation commands'),
        ]
        for entry, extra, error in cases:
            with self.subTest(error):
                with self.assertRaisesRegex(ValueError, error):
                    self.splitting_plan(entry, **extra)
        self.assertEqual(self.github.calls['write'], 0)

    def test_other_plans_never_rewrite_history_or_run_the_procedure(self):
        controls = {'tasks': ['inspect-commits', {'stage': 'publication'}], 'finalizers': [], 'reviewers': [],
                    'adapters': {'inspect-commits': dict(admission.BUNDLED_ADAPTERS['inspect-commits'])},
                    'accounts': [], 'resources': [], 'turns': 8, 'mode': 'fix'}
        request = {'pr': PR_URL, 'instructions': 'Organize this PR.', 'logical_run': 'composed',
                   'authorizations': {}}
        for extra, error in (({'permissions': {'rewrite_commits': True}}, 'dk-commit-split'),
                             ({'settings': {'splitting': {}}}, 'settings.splitting')):
            with self.subTest(error):
                with self.assertRaisesRegex(ValueError, error):
                    prepare(self.root, request, {**controls, 'settings': {}, 'permissions': {}, **extra}, 'exec-1',
                            self.github, self.definitions)
        # A composed plan runs inspect-commits as an ordinary read-only task.
        plan = read_artifact(self.root, prepare(self.root, request, {**controls, 'settings': {}, 'permissions': {}},
                                                'exec-1', self.github, self.definitions)['plan'])
        self.assertIsNone(splitting.occurrence(plan))
        self.assertNotIn('splitting', plan['stages'][1])

    def test_installed_procedure_is_the_library_procedure_verbatim(self):
        def definitions(path):
            source = Path(path).read_text()
            result = {}
            for node in ast.parse(source).body:
                if isinstance(node, ast.FunctionDef):
                    start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
                    result[node.name] = '\n'.join(source.split('\n')[start:node.end_lineno])
                elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                    result[node.targets[0].id] = ast.get_source_segment(source, node)
            return result
        installed, original = definitions(splitting.__file__), definitions(library.__file__)
        names = ('MAX_COMMITS', 'MAX_PATHS', 'MAX_PATCH', 'MODES', 'SCOPES', 'MODE_OPTION', 'RECOVERY', '_TRAILER',
                 '_AUTHOR', '_SLUG', '_BRANCH', 'git', '_sha', '_objects', '_text', '_state', '_diff', '_states', '_tree',
                 '_identity', '_commit', '_record', 'series', '_dirty', '_ref', '_owned', '_fixed', '_patch',
                 '_items', 'check_message', '_common', 'check_commits', '_public', 'check_split', 'build_series',
                 'build_split', 'plan_digest', '_sync')
        for name in names:
            with self.subTest(name):
                self.assertEqual(installed[name], original[name])
        for name, entry in library.ENTRIES.items():
            self.assertEqual({key: splitting.ENTRIES[name][key] for key in ('adapter', 'actions', 'options',
                                                                            'role_prompt')},
                             {key: entry[key] for key in ('adapter', 'actions', 'options', 'role_prompt')})
            self.assertEqual(set(splitting.ENTRIES[name]['permissions'].values()),
                             set(library.INSTALLED_PERMISSIONS[name].values()))


class InstalledPreflight(PreflightFixture):
    def entry(self, name, authorizations=(), permissions=(), request=None, **extra):
        request = dict(request or self.request)
        if authorizations:
            request['authorizations'] = {action: grant(action) for action in authorizations}
        if permissions:
            extra['invocation'] = {'permissions': {action: True for action in permissions}}
        self.transport = FakeTransport(CAPABILITIES)
        result = library.installed(name, request=request, configuration=None, deployment=self.deployment,
                                   repository=self.repo, transport=self.transport, home=self.home,
                                   install=self.clean, **extra)
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def test_direct_entries_map_onto_their_installed_presets(self):
        descriptor = (INSTALL / 'plugin.yaml').read_text()
        cases = (('dk-commit-split', ('push',), {'options': {'max_passes': 2}, 'rewrite': LOCAL,
                                                 'validation_commands': [PASS]},
                  {'max_passes': 2, 'rewrite': LOCAL, 'validation_commands': [PASS]}, {'rewrite_commits': True}),
                 ('dk-pr-split', ('create_pr',), {'owners': ['me@example.invalid']},
                  {'owners': ['me@example.invalid']}, {'create_draft_prs': True, 'push_commits': False}),
                 ('dk-pr-split', (), {}, None, {'create_draft_prs': False, 'push_commits': False}))
        for name, actions, extra, settings, permissions in cases:
            with self.subTest(name):
                result = self.entry(name, actions, actions, **extra)
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                workflow = library.ENTRY_WORKFLOWS[name]
                self.assertEqual((result['workflow'], result['installation']['entry']),
                                 ('cf-workflows:' + name, workflow))
                self.assertIn(f'  - name: {name}\n    path: skills/{name}/SKILL.md\n    workflow: {workflow}\n',
                              descriptor)
                self.assertTrue((INSTALL / 'skills' / name / 'SKILL.md').is_file())
                body = (INSTALL / workflow).read_text()
                self.assertIn(f'name: cf-workflows:{name}\n', body)
                self.assertIn(f'      mode: fix\n      entry: {name}\n', body)
                for key in result['inputs']:
                    self.assertRegex(body, rf'\n      {key}:\n')
                    self.assertIn(f'{key}: ${{{{ inputs.{key} }}}}', body)
                for setting in permissions:
                    self.assertIn(f'          {setting}: false\n', body)
                self.assertEqual(result['inputs']['tasks'], library.preset(name)['tasks'])
                self.assertEqual((result['inputs']['finalizers'], result['inputs']['reviewers']), ([], []))
                self.assertEqual(result['inputs']['permissions'], permissions)
                self.assertEqual(result['inputs']['settings'].get('splitting'),
                                 None if settings is None else {name: settings})
                self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_unsupported_inputs_reject_before_the_engine(self):
        cases = (
            ('dk-commit-split', {'authorizations': ('create_pr',)}, 'request.authorizations'),
            ('dk-commit-split', {'invocation': {'prompts': []}}, 'invocation'),
            ('dk-commit-split', {'options': {'policy': {'prompt': 'Squash fixups.'}}}, 'options.policy'),
            ('dk-pr-split', {'rewrite': {'request_id': 'x'}}, 'rewrite'),
            ('dk-pr-split', {'owners': ['nobody']}, 'owners[0]'),
            ('dk-pr-split', {'validation_commands': [['codex', 'exec']]}, 'validation_commands[0]'),
            ('dk-pr-split', {'options': {'split_related': 'sometimes'}}, 'options.split_related'),
        )
        for name, extra, path in cases:
            with self.subTest(path):
                extra = dict(extra)
                authorizations = extra.pop('authorizations', ())
                result = self.entry(name, authorizations, authorizations, **extra)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))

    def test_history_rewrite_input_stays_false_for_dk_pr_fix(self):
        request = {**self.request, 'authorizations': {'push': grant('push')}}
        result = fix_preflight.preflight(request=request, configuration=None,
                                         invocation={'permissions': {'push': True}}, deployment=self.deployment,
                                         repository=self.repo, transport=FakeTransport(CAPABILITIES),
                                         home=self.home, install=self.clean)
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['inputs']['permissions']['push_commits'],
                          result['inputs']['permissions']['rewrite_commits']), (True, False))


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
            canonical(dirty)


if __name__ == '__main__':
    unittest.main()
