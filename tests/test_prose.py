"""Prose cleanup entries: cf-workflows:dk-plain-english and cf-workflows:dk-asd-ste100.

Disposable repositories, the real acceptance and prose SQLite ledgers, and a
fail-closed fake engine whose native sessions edit the checkout once per claim
and reconcile a known claim to its original result. Unconfigured operations fail
with no live fallback. These results do not certify any engine, provider or host.
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True  # Never add caches to the installed closure.
from cf_workflows import acceptance, admission, configuration, dk_pr_fix, prose, semantic  # noqa: E402
from test_admission import CAPABILITIES, FakeTransport  # noqa: E402
from test_semantic import verdict  # noqa: E402

REPOSITORY = 'github.com/owner/repo'
PR = 'https://github.com/owner/repo/pull/7'
INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}

GUIDE_BASE = ('# Guide\n\nIntro paragraph that stays.\n\nBackground paragraph that stays.\n\n'
              'Tail paragraph that stays.\n')
SETUP = ('## Setup\n\n'
         'In order to make use of the tool, it is necessary that `make setup` is run by the user before builds.\n'
         'See [install](install.md) for details; it takes 5 minutes. The parser MUST be configured.\n\n'
         '```sh\nmake setup --fast\n```\n\n')
GUIDE_HEAD = GUIDE_BASE.replace('Tail paragraph', SETUP + 'Tail paragraph')
MODULE_BASE = 'def add(a, b):\n    return a + b\n'
MODULE_HEAD = ('def add(a, b):\n'
               '    """Add the two numbers that are given and return the result of the addition."""\n'
               '    # It is the case that we return the sum here.\n'
               '    return a + b\n')
LONG = 'In order to make use of the tool, it is necessary that `make setup` is run by the user before builds.'
SHORT = 'Before builds, the user runs `make setup` to use the tool.'


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class Engine:
    """Durable engine state: one session per new claim; its edits happen once."""

    def __init__(self):
        self.claims, self.sessions = {}, Counter()


class Transport(FakeTransport):
    def __init__(self, engine, repo, sessions=(), validators=(), lose=0):
        super().__init__(CAPABILITIES)
        self.tasks, script, checks, lost = [], list(sessions), list(validators), {'remaining': lose}

        def native(task):
            self.tasks.append(copy.deepcopy(task))
            if task['claim'] not in engine.claims:
                if not script:
                    raise AssertionError('unscripted native session')
                engine.claims[task['claim']] = script.pop(0)(task, repo)
                engine.sessions[task['adapter']] += 1
            if lost['remaining']:  # The session ran and edited, but its response is lost.
                lost['remaining'] -= 1
                raise ConnectionError('response lost')
            return copy.deepcopy(engine.claims[task['claim']])

        def validator(task):
            if not checks:
                raise AssertionError('unscripted validator session')
            return checks.pop(0)(task)
        self.configured.update(native_session=native, validator_session=validator,
                               input_gates=lambda request_id: [])


def locations(repo, path, needles):
    lines = (repo / path).read_text().splitlines()
    return [{'path': path, 'start': n, 'end': n, 'reason': 'clearer'}
            for n, line in enumerate(lines, 1) if any(needle in line for needle in needles)]


def edit(*replacements, report=None, operations=None, **observed):
    """A scripted native session: apply (path, old, new) edits, then report them."""
    def respond(task, repo):
        for path, old, new in replacements:
            target = repo / path
            body = target.read_text()
            assert old in body, (path, old)
            target.write_text(body.replace(old, new, 1))
        found = [item for path, _, new in replacements for item in locations(repo, path, [new.splitlines()[0]])]
        body = {'complete': True, 'changed_locations': found,
                'validation': ['Meaning and protected syntax checked against the source.'],
                'unresolved_ambiguities': [], 'reference_verification_limits': []}
        if replacements:
            body['commit_message'] = 'Clarify changed prose'
        else:
            body['no_op'] = {'reason': 'The changed prose is already clear.', 'sources': [
                unit['path'] for unit in task['context']['units']]}
        if task['adapter'] == 'asd-ste100':
            body['compliance'] = 'not_claimed'
        if report:
            body = report(body, task)
        ops = ([{'kind': 'read', 'target': key} for key in task['readable']]
               + [{'kind': 'write', 'target': 'repository:' + path} for path in {p for p, _, _ in replacements}])
        return {'result_ref': 'result-' + task['claim'], 'output': body,
                'observation': {'session_id': 'session-' + task['claim'][-8:], 'agent': task['agent'],
                                'role': task['role'], 'definition_sha256': task['definition_sha256'],
                                'skills_loaded': [], 'operations': ops if operations is None else operations,
                                'claim': task['claim'], **observed}}
    return respond


GOOD = ('docs/guide.md', LONG, SHORT)
DOCSTRING = ('src/mod.py', 'Add the two numbers that are given and return the result of the addition.',
             'Return the sum of the two numbers.')


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw prose ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Fixture')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.write('README.md', 'Unchanged readme with long words that stay as they are.\n')
        self.write('docs/guide.md', GUIDE_BASE)
        self.write('src/mod.py', MODULE_BASE)
        self.write('assets/logo.bin', b'\x00\x01')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        self.pr_base = git(self.repo, 'rev-parse', 'HEAD')
        self.write('docs/guide.md', GUIDE_HEAD)
        self.write('src/mod.py', MODULE_HEAD)
        self.write('assets/logo.bin', b'\x00\xff\xfe')
        git(self.repo, 'commit', '-q', '-am', 'PR change')
        self.pr_head = git(self.repo, 'rev-parse', 'HEAD')
        dk_pr_fix.verify_installation()
        self.engine = Engine()
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}
        self.prose_ledger = prose.Ledger(prose.ledger_path(self.base / 'state', REPOSITORY))
        self.addCleanup(self.prose_ledger.close)
        self.acceptance = acceptance.Ledger(self.base / 'state' / 'configuration.sqlite3')
        self.addCleanup(self.acceptance.close)

    def write(self, rel, body):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())

    def transport(self, *sessions, validators=(), lose=0):
        self.fake = Transport(self.engine, self.repo, sessions, validators, lose)
        return self.fake

    def calls(self, **expected):
        self.assertEqual(self.fake.calls, +Counter(expected))

    def clean(self):
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--untracked-files=all'), '')

    def clean_up(self, name='dk-plain-english', *sessions, instructions='Clean up the changed prose.',
                 configuration=None, invocation=None, references=None, validators=(), lose=0):
        transport = self.transport(*sessions, validators=validators, lose=lose)
        return prose.cleanup(name, request={'pr': PR, 'instructions': instructions}, configuration=configuration,
                             deployment=self.deployment, repository=self.repo, transport=transport,
                             ledger=self.prose_ledger, base=self.pr_base, invocation=invocation,
                             references=references, acceptance_ledger=self.acceptance)


# -- Presets ---------------------------------------------------------------------------------------

class Presets(Fixture):
    def test_entries_are_finalizer_presets_with_original_prompts(self):
        for name, entry in prose.ENTRIES.items():
            with self.subTest(name):
                row = INVENTORY[name]
                self.assertEqual(entry['skill'], row['entrypoint'])
                digests = {item['path']: item['sha256'] for item in row['baseline_resources']}
                self.assertEqual(hashlib.sha256(entry['role_prompt'].encode()).hexdigest(),
                                 digests[entry['baseline_prompt']])
                self.assertEqual(prose.preset(name), {'tasks': [], 'finalizers': [entry['adapter']], 'reviewers': []})
                adapter = admission.ENTRY_ADAPTERS[entry['adapter']]
                self.assertEqual((adapter['kind'], adapter['writes'], adapter['definition']), ('cleanup', True, 'leaf'))
                transport = self.transport()
                result = prose.preflight(name, request={'pr': PR, 'instructions': 'Clean up.'}, configuration=None,
                                         deployment=self.deployment, repository=self.repo, transport=transport)
                self.assertEqual((result['preflight'], result['semantic']), ('admitted', None))
                self.calls(capabilities=1)  # Bundled control text: no validator session, no skill.
                [task] = result['snapshot']['plan']['tasks']
                self.assertEqual((task['adapter'], task['phase'], task['skill'], task['writes']),
                                 (entry['adapter'], 'finalizers', None, True))

    def test_preset_cannot_expand_authority_or_target(self):
        authorization = {'push': {'request_id': 'r1', 'instruction': 'push it', 'source': 'user'}}
        for label, request, invocation in [
                ('authorization', {'pr': PR, 'instructions': 'Clean.', 'authorizations': authorization}, None),
                ('repository target', {'repository': REPOSITORY, 'instructions': 'Clean.'}, None),
                ('finalizers', {'pr': PR, 'instructions': 'Clean.'}, {'options': {'finalizers': []}}),
                ('permissions', {'pr': PR, 'instructions': 'Clean.'}, {'permissions': {'push': True}}),
                ('skills', {'pr': PR, 'instructions': 'Clean.'}, {'skills': []})]:
            with self.subTest(label):
                transport = self.transport()
                result = prose.preflight('dk-plain-english', request=request, configuration=None,
                                         invocation=invocation, deployment=self.deployment, repository=self.repo,
                                         transport=transport)
                self.assertEqual(result['preflight'], 'rejected', result)
                self.calls()
        self.write('ref.txt', 'x')
        reference = [{'id': 'spec', 'path': str(self.repo / 'ref.txt'), 'sha256': hashlib.sha256(b'x').hexdigest()}]
        rejected = self.clean_up('dk-plain-english', references=reference)
        self.assertEqual((rejected['status'], rejected['error']['path']), ('rejected', 'references'))
        self.calls(capabilities=1)
        self.assertEqual(self.engine.sessions, Counter())


# -- Direct passes ---------------------------------------------------------------------------------

class Direct(Fixture):
    def test_direct_pass_edits_only_changed_prose_and_commits_locally(self):
        result = self.clean_up('dk-plain-english', edit(GOOD, DOCSTRING))
        self.assertEqual(result['status'], 'complete', result)
        self.calls(capabilities=1, native_session=1)
        [task] = self.fake.tasks
        self.assertEqual((task['skill'], task['adapter'], task['role'], task['skills'], task['tools'], task['commit'],
                          task['publication']),
                         ('cf-workflows:dk-plain-english', 'plain-english', 'leaf', [], 'edit', False, False))
        self.assertEqual(task['role_prompt'], prose.ENTRIES['dk-plain-english']['role_prompt'])
        self.assertEqual(task['writes'], ['repository:docs/guide.md', 'repository:src/mod.py'])
        self.assertEqual(set(task['readable']), {'repository:docs/guide.md', 'repository:src/mod.py'})
        self.assertEqual(result['units'], {'docs/guide.md': 'full', 'src/mod.py': 'full'})
        self.assertEqual(result['skipped'], [{'path': 'assets/logo.bin', 'reason': 'binary or not UTF-8'}])
        commit = result['prose_cleanup']['output']
        self.assertEqual(result['prose_cleanup'], {'baseline': self.pr_base, 'head': self.pr_head, 'output': commit})
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), commit)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^'), self.pr_head)
        self.assertEqual(git(self.repo, 'diff', '--name-only', self.pr_head, commit).split(),
                         ['docs/guide.md', 'src/mod.py'])
        self.clean()
        guide = (self.repo / 'docs/guide.md').read_text()
        self.assertEqual(guide, GUIDE_HEAD.replace(LONG, SHORT))  # Unrelated and protected content unchanged.
        self.assertNotIn('certif', json.dumps(result).lower())

    def test_rerun_reuses_the_accepted_pass_without_a_session(self):
        self.clean_up('dk-plain-english', edit(GOOD))
        again = self.clean_up('dk-plain-english')
        self.assertEqual(again['status'], 'reused')
        self.assertEqual(again['units'], {'docs/guide.md': 'reused', 'src/mod.py': 'reused'})
        self.calls(capabilities=1)
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 1}))

    def test_edits_that_change_meaning_syntax_or_unrelated_text_are_rejected_and_restored(self):
        cases = {
            'unrelated prose': [('docs/guide.md', 'Intro paragraph that stays.', 'The intro stays.')],
            'code block': [('docs/guide.md', 'make setup --fast', 'make setup')],
            'code span': [('docs/guide.md', '`make setup` is run', '`make init` is run')],
            'link target': [('docs/guide.md', '(install.md)', '(setup.md)')],
            'number': [('docs/guide.md', 'takes 5 minutes', 'takes five minutes')],
            'requirement keyword': [('docs/guide.md', 'The parser MUST be configured.', 'Configure the parser.')],
            'python code': [('src/mod.py', 'return a + b', 'return b + a')],
            'unchanged file': [('README.md', 'long words', 'short words')],
        }
        for label, replacements in cases.items():
            with self.subTest(label):
                self.prose_ledger.db.execute('DELETE FROM passes')
                self.engine.claims.clear()
                result = self.clean_up('dk-plain-english', edit(*replacements))
                self.assertEqual(result['status'], 'blocked', result)
                self.calls(capabilities=1, native_session=1)
                self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.pr_head)
                self.clean()

    def test_quoted_user_wording_is_kept_verbatim(self):
        tail = ('docs/guide.md', 'Tail paragraph that stays.', 'The tail stays.')
        allowed = self.clean_up('dk-plain-english', edit(tail), instructions='Clean up the changed prose.')
        self.assertEqual(allowed['status'], 'complete', allowed)  # An adjacent coherence edit.
        git(self.repo, 'reset', '-q', '--hard', self.pr_head)
        kept = self.clean_up('dk-plain-english', edit(tail),
                             instructions='Clean up the prose but keep "Tail paragraph that stays." verbatim.')
        self.assertEqual(kept['status'], 'blocked')
        self.assertIn('exact wording', kept['error']['message'])
        self.assertEqual(kept['wording'], ['Tail paragraph that stays.'])
        self.clean()

    def test_no_op_needs_a_sourced_assessment_and_incomplete_passes_commit_nothing(self):
        noop = self.clean_up('dk-plain-english', edit())
        self.assertEqual((noop['status'], noop['commit'], noop['prose_cleanup']['output']),
                         ('complete', None, self.pr_head))
        self.assertEqual(noop['no_op']['sources'], ['docs/guide.md', 'src/mod.py'])
        self.assertEqual(self.clean_up('dk-plain-english')['status'], 'reused')

        def unsourced(body, task):
            body.pop('no_op')
            return body
        self.prose_ledger.db.execute('DELETE FROM units')
        self.prose_ledger.db.execute('DELETE FROM passes')
        self.engine.claims.clear()
        self.assertEqual(self.clean_up('dk-plain-english', edit(report=unsourced))['status'], 'blocked')

        def incomplete(body, task):
            return {**body, 'complete': False}
        self.engine.claims.clear()
        self.prose_ledger.db.execute('DELETE FROM passes')
        result = self.clean_up('dk-plain-english', edit(GOOD, report=incomplete))
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.pr_head)
        self.clean()

    def test_observed_session_must_be_the_admitted_skill_free_editor(self):
        def history(task, repo):
            response = edit(GOOD)(task, repo)
            git(repo, 'commit', '-qam', 'session commit')
            return response
        for label, session in {
                'loaded skill': edit(GOOD, skills_loaded=['some-skill']),
                'other role': edit(GOOD, role='reviewer'),
                'undeclared write': edit(GOOD, operations=[{'kind': 'write', 'target': 'repository:README.md'}]),
                'command': edit(GOOD, operations=[{'kind': 'command', 'target': 'make'}]),
                'unobserved write': edit(('README.md', 'long words', 'short words'), operations=[]),
                'unreported edit': edit(GOOD, report=lambda body, task: {**body, 'changed_locations': []}),
                'session commit': history}.items():
            with self.subTest(label):
                git(self.repo, 'reset', '-q', '--hard', self.pr_head)
                self.prose_ledger.db.execute('DELETE FROM passes')
                self.engine.claims.clear()
                result = self.clean_up('dk-plain-english', session)
                self.assertEqual(result['status'], 'blocked', result)
                self.assertNotIn('commit', result)
                self.clean()

    def test_lost_response_reconciles_the_original_claim(self):
        lost = self.clean_up('dk-plain-english', edit(GOOD), lose=1)
        self.assertEqual((lost['status'], lost['error']['path']), ('blocked', 'session'))
        self.assertIn(SHORT, (self.repo / 'docs/guide.md').read_text())  # The edits are kept for reconciliation.
        result = self.clean_up('dk-plain-english')
        self.assertEqual(result['status'], 'complete', result)
        self.calls(capabilities=1, native_session=1)
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 1}))
        self.assertEqual(self.fake.tasks[0]['claim'], lost['claim'])

    def test_interrupted_commit_settles_from_its_intent_without_a_second_commit(self):
        original = prose.git

        def crash(repository, *args, **kwargs):
            if args[0] == 'update-ref':
                raise RuntimeError('helper died')
            return original(repository, *args, **kwargs)
        with patch.object(prose, 'git', crash), self.assertRaises(RuntimeError):
            self.clean_up('dk-plain-english', edit(GOOD))
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.pr_head)
        result = self.clean_up('dk-plain-english')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), result['commit'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^'), self.pr_head)
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 1}))
        self.clean()

    def test_dirty_checkout_or_foreign_base_rejects_before_a_session(self):
        self.write('docs/guide.md', 'local edit\n')
        result = self.clean_up('dk-plain-english')
        self.assertEqual((result['status'], result['error']['path']), ('rejected', 'checkout'))
        self.calls(capabilities=1)
        git(self.repo, 'checkout', '-q', '--', '.')
        transport = self.transport()
        result = prose.cleanup('dk-plain-english', request={'pr': PR, 'instructions': 'Clean.'}, configuration=None,
                               deployment=self.deployment, repository=self.repo, transport=transport,
                               ledger=self.prose_ledger, base='0' * 40)
        self.assertEqual((result['status'], result['error']['path']), ('rejected', 'base'))
        self.calls(capabilities=1)


class Recovery(Fixture):
    """Odd tree entries, staged or renamed session edits, mode changes and interrupted commits."""

    def commit(self, message='more changes'):
        git(self.repo, 'commit', '-q', '-m', message)
        self.pr_head = git(self.repo, 'rev-parse', 'HEAD')

    def test_submodule_and_symlink_changes_are_not_candidates(self):
        (self.repo / 'sub').mkdir()
        git(self.repo, 'update-index', '--add', '--cacheinfo', f'160000,{self.pr_base},sub')
        (self.repo / 'docs/link.md').symlink_to('guide.md')
        git(self.repo, 'add', 'docs/link.md')
        self.commit()
        result = self.clean_up('dk-plain-english', edit(GOOD))
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(set(result['units']), {'docs/guide.md', 'src/mod.py'})

    def test_a_pruned_accepted_output_plans_the_file_from_base(self):
        base_blob = git(self.repo, 'rev-parse', f'{self.pr_base}:docs/guide.md')
        units = {'docs/guide.md': {'contract': 'c', 'base_blob': base_blob, 'binding': 'b', 'output_blob': '1' * 40,
                                   'claim': 'old'}}
        plan, _ = prose.candidates(self.repo, self.pr_base, self.pr_head, units, 'c', {
            'docs/guide.md': 'b', 'src/mod.py': 'b'})
        self.assertEqual(plan['docs/guide.md']['mode'], 'full')

    def test_a_non_utf8_file_name_is_skipped(self):
        self.write(os.fsdecode(b'docs/caf\xe9.md'), 'In order to make use of it, it is necessary to read it.\n')
        git(self.repo, 'add', '-A')
        self.commit()
        result = self.clean_up('dk-plain-english', edit(GOOD))
        self.assertEqual(result['status'], 'complete', result)
        self.assertIn({'path': 'docs/caf\ufffd.md', 'reason': 'file name is not UTF-8'}, result['skipped'])

    def test_staged_new_files_and_renames_are_restored_and_rejected(self):
        def added(task, repo):
            response = edit(GOOD)(task, repo)
            (repo / 'docs/new.md').write_text('New.\n')
            (repo / 'docs/nested').mkdir()
            (repo / 'docs/nested/deep.md').write_text('Deep.\n')
            git(repo, 'add', 'docs/new.md')
            return response

        def renamed(task, repo):
            response = edit(GOOD)(task, repo)
            git(repo, 'mv', 'docs/guide.md', 'docs/moved.md')
            return response
        for label, session in {'added': added, 'renamed': renamed}.items():
            with self.subTest(label):
                self.prose_ledger.db.execute('DELETE FROM passes')
                self.engine.claims.clear()
                result = self.clean_up('dk-plain-english', session)
                self.assertEqual((result['status'], result['error']['path']), ('blocked', 'checkout'), result)
                self.clean()
                [state] = [row['state'] for row in self.prose_ledger.db.execute('SELECT state FROM passes')]
                self.assertEqual(state, 'rejected')

    def test_a_mode_change_by_the_session_is_rejected_and_restored(self):
        def chmod(task, repo):
            response = edit(GOOD)(task, repo)
            (repo / 'docs/guide.md').chmod(0o755)
            return response
        result = self.clean_up('dk-plain-english', chmod)
        self.assertEqual((result['status'], result['error']), (
            'blocked', {'path': 'docs/guide.md', 'message': 'the session changed the file mode'}), result)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.pr_head)
        self.clean()

    def test_a_crash_after_the_head_moved_settles_the_original_pass(self):
        original = prose.git

        def crash(repository, *args, **kwargs):
            if args[0] == 'reset':
                raise RuntimeError('helper died')
            return original(repository, *args, **kwargs)
        with patch.object(prose, 'git', crash), self.assertRaises(RuntimeError):
            self.clean_up('dk-plain-english', edit(GOOD))
        self.assertNotEqual(git(self.repo, 'rev-parse', 'HEAD'), self.pr_head)
        result = self.clean_up('dk-plain-english')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), result['commit'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^'), self.pr_head)
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 1}))
        self.clean()
        self.assertEqual(self.clean_up('dk-plain-english')['status'], 'reused')

    def test_a_crash_before_acceptance_settles_without_a_second_session(self):
        with patch.object(prose.Ledger, 'accept', side_effect=RuntimeError('helper died')), \
                self.assertRaises(RuntimeError):
            self.clean_up('dk-plain-english', edit(GOOD))
        result = self.clean_up('dk-plain-english')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^'), self.pr_head)
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 1}))
        self.assertEqual(self.clean_up('dk-plain-english')['status'], 'reused')

    def test_line_indexes_split_on_line_feeds_only(self):
        lines = prose._lines('int x;\x0c\nint y;\x0cint z;\n// A comment.\n'.encode())
        self.assertEqual(lines[2], '// A comment.\n')
        self.assertEqual(prose.prose_lines('a.c', lines), {2})
        lines = prose._lines('x = 1\x0c\n# A comment.\u2028still\ny = 2\n'.encode())
        self.assertEqual(prose.prose_lines('a.py', lines), {1})


class SourceAndCodeBlocks(Fixture):
    """Outside documentation, only whole comment lines are prose; indented code blocks are code."""

    def setUp(self):
        super().setUp()
        self.write('src/app.js', 'function ok(count, limit) {\n  // It is the case that we check the limit here.\n'
                                 '  if (count > limit) { return false; }\n  return true;\n}\n')
        self.write('docs/indented.md', 'Run the following command in order to greet.\n\n    echo hello world\n')
        self.write('src/edge.js', 'function ok(count, limit) {\n'
                                  '  /* Check the limit here. */ if (count > limit) { return false; }\n'
                                  '  /* Multi-line\n     note. */ if (count == limit) { return true; }\n'
                                  '  const banner = `\n// Do not remove the cache before the build.\n`;\n}\n')
        self.write('src/re.js', 'const special = /[/*+?]/;\nfunction ok(count, limit) {\n'
                                '  if (count > limit) { return false; }\n}\n// It is the case that this ends. */\n')
        self.write('src/A.java', 'class A {\n  String s = """\n    He said "hi\n'
                                 '    // Do not remove the cache before the build.\n    """;\n}\n')
        self.write('settings.json', '{"note": "It is the case that this is set."}\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'more changes')
        self.pr_head = git(self.repo, 'rev-parse', 'HEAD')

    def test_only_comment_lines_and_documentation_prose_are_candidates(self):
        result = self.clean_up('dk-plain-english', edit(
            ('src/app.js', 'It is the case that we check the limit here.', 'Check the limit.'),
            ('docs/indented.md', 'in order to greet.', 'to greet.')))
        self.assertEqual(result['status'], 'complete', result)
        self.assertIn({'path': 'settings.json', 'reason': 'no recognized prose syntax for this file type'},
                      result['skipped'])
        units = {unit['path']: unit for unit in self.fake.tasks[0]['context']['units']}
        self.assertEqual(units['src/app.js']['ranges'], [[2, 2]])  # The comment line, not the code.
        self.assertEqual(units['docs/indented.md']['ranges'], [[1, 2]])  # Not the indented code block.
        self.assertNotIn('settings.json', units)

    def test_code_edits_in_non_python_sources_and_indented_blocks_are_rejected(self):
        for label, replacement in {
                'operator': ('src/app.js', 'count > limit', 'count < limit'),
                'comment to code': ('src/app.js', '// It is the case that we check the limit here.',
                                    'if (count === 0) { return true; }'),
                'code after a block comment': ('src/edge.js', '(count > limit)', '(count < limit)'),
                'code after a block comment end': ('src/edge.js', '(count == limit)', '(count != limit)'),
                'comment-like template literal': ('src/edge.js', 'Do not remove the cache before',
                                                  'Remove the cache before'),
                'code after a regex literal': ('src/re.js', '(count > limit)', '(count < limit)'),
                'comment-like text block': ('src/A.java', 'Do not remove the cache before', 'Remove the cache before'),
                'indented code block': ('docs/indented.md', 'echo hello world', 'echo hi world'),
                'unknown file type': ('settings.json', 'It is the case that this', 'This')}.items():
            with self.subTest(label):
                git(self.repo, 'reset', '-q', '--hard', self.pr_head)
                self.prose_ledger.db.execute('DELETE FROM passes')
                self.engine.claims.clear()
                result = self.clean_up('dk-plain-english', edit(replacement))
                self.assertEqual(result['status'], 'blocked', result)
                self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.pr_head)
                self.clean()
        self.assertEqual(prose.prose_lines('a.c', ['int a;\n', '/* one\n', ' * two */\n', '*p = 1;\n']), {1, 2})
        self.assertEqual(prose.prose_lines('x.py', ['#!/bin/sh\n', 'x = 1  # trailing\n', '# whole\n']), {2})
        self.assertIsNone(prose.prose_lines('data.csv', ['a,b\n']))

    def test_only_whole_comment_lines_outside_strings_are_prose(self):
        def lines(path, text):
            return prose.prose_lines(path, text.splitlines(True))
        self.assertEqual(lines('src/edge.js', (self.repo / 'src/edge.js').read_text()), {2})
        self.assertEqual(lines('a.js', 'const s = "a\\\n// in string";\n// real\n'), {2})
        self.assertEqual(lines('a.sh', 'echo "a\n# in string\n"\n# real\necho \'it\'\\\'\'s\'\n# real2\n'), {3, 5})
        self.assertEqual(lines('a.py', 's = \"\"\"\n# in string\n\"\"\"\n# real\nx = 1  # trailing\n'), {3})
        # A division keeps the lexer going; a regex literal stops it.
        self.assertEqual(lines('d.js', 'const half = total / 2; // half\n// real\nx = (a) / b;\n'), {1})
        self.assertEqual(lines('a.go', 'var s = `C:\\`\n// real\n'), {1})  # Go raw strings have no escapes.
        with self.assertRaises(prose.Blocked):  # Operators in changed prose are kept too.
            prose.check_edits('a.md', b'Use it when x > y.\n', b'Use it when x < y.\n', {0}, [])
        for path, text in {'src/re.js': (self.repo / 'src/re.js').read_text(),
                           'r.ts': 'function f() {\n  return /x/.test(y);\n}\n// real\n',
                           'src/A.java': (self.repo / 'src/A.java').read_text(),
                           'a.toml': 's = """\n# in string\n"""\n# real\n',
                           'a.sh': 'cat <<EOF\n# in heredoc\nEOF\n', 'a.yaml': 'a: |\n  # in block\n# real\n',
                           'a.cpp': 'auto s = R"(\n// raw\n)";\n// real\n',
                           'a.lua': 'local s = [[\n-- long\n]]\n-- real\n'}.items():
            with self.subTest(path):
                self.assertEqual(lines(path, text), set())  # Unlexable constructs: no editable prose.


# -- ASD-STE100 ------------------------------------------------------------------------------------

class SimplifiedTechnicalEnglish(Fixture):
    def test_unverified_pass_records_limits_and_never_claims_compliance(self):
        result = self.clean_up('dk-asd-ste100', edit(GOOD))
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['compliance'], 'not_claimed')
        limits = result['reference_verification_limits']
        self.assertTrue(any('No authoritative ASD-STE100' in item for item in limits), limits)
        self.assertTrue(any('not claimed' in item for item in limits), limits)
        [task] = self.fake.tasks
        self.assertEqual(task['context']['references'], {})
        self.assertEqual(task['context']['reference_limits'], limits)

    def test_certification_claims_and_invented_rules_are_rejected(self):
        for label, change in {
                'compliance field': lambda body, task: {**body, 'compliance': 'compliant'},
                'claim in validation': lambda body, task: {**body, 'validation': ['The text is ASD-STE100 compliant.']},
                'certified': lambda body, task: {**body, 'validation': ['Certified STE output.']},
                'rule number': lambda body, task: {**body, 'validation': ['Applied rule 3.1 throughout.']},
                'negated elsewhere': lambda body, task: {**body, 'validation': [
                    'The output is fully ASD-STE100 compliant with no exceptions.']},
                'commit message': lambda body, task: {**body,
                                                      'commit_message': 'Make docs ASD-STE100 compliant per rule 3.1'},
                'commit message rule': lambda body, task: {**body, 'commit_message': 'Apply rule 2.4 to the guide'},
                'ambiguity': lambda body, task: {**body, 'unresolved_ambiguities': [
                    {'path': 'docs/guide.md', 'line': 9, 'text': 'Certified wording per R4.2 kept.'}]},
                'unsupplied reference': lambda body, task: {**body, 'reference_checks': [
                    {'reference': 'spec', 'subject': 'dictionary'}]}}.items():
            with self.subTest(label):
                self.prose_ledger.db.execute('DELETE FROM passes')
                self.engine.claims.clear()
                result = self.clean_up('dk-asd-ste100', edit(GOOD, report=change))
                self.assertEqual(result['status'], 'blocked', result)
                self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.pr_head)
                self.clean()
        self.prose_ledger.db.execute('DELETE FROM passes')
        self.engine.claims.clear()
        honest = self.clean_up('dk-asd-ste100', edit(GOOD, report=lambda body, task: {
            **body, 'validation': ['Compliance is not claimed; rule checks were not performed.']}))
        self.assertEqual(honest['status'], 'complete', honest)

    def test_supplied_reference_enables_checks_and_refreshes_the_full_pass(self):
        self.clean_up('dk-asd-ste100', edit(GOOD))
        spec = self.base / 'ste-spec.txt'
        spec.write_text('Authoritative reference fixture.\n')
        reference = [{'id': 'spec', 'path': str(spec), 'sha256': hashlib.sha256(spec.read_bytes()).hexdigest()}]
        result = self.clean_up('dk-asd-ste100', edit(report=lambda body, task: {
            **body, 'validation': ['Checked against rule 1.1 of the supplied reference.'],
            'reference_checks': [{'reference': 'spec', 'subject': 'Approved verbs in the changed lines.'}]}),
            references=reference)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['units'], {'docs/guide.md': 'full', 'src/mod.py': 'full'})
        self.assertEqual(self.engine.sessions, Counter({'asd-ste100': 2}))
        self.assertFalse(any('No authoritative' in item for item in result['reference_verification_limits']))
        self.assertTrue(any('not claimed' in item for item in result['reference_verification_limits']))
        self.assertEqual(self.fake.tasks[0]['readable']['reference:spec'], reference[0]['sha256'])
        reference[0]['sha256'] = '0' * 64
        changed = self.clean_up('dk-asd-ste100', references=reference)
        self.assertEqual((changed['status'], changed['error']['path']), ('rejected', 'references[0].sha256'))


# -- Selective refresh -----------------------------------------------------------------------------

class Refresh(Fixture):
    def test_a_later_source_change_refreshes_only_its_file_with_a_delta(self):
        first = self.clean_up('dk-plain-english', edit(GOOD, DOCSTRING))
        self.assertEqual(first['status'], 'complete', first)
        guide = self.repo / 'docs/guide.md'
        guide.write_text(guide.read_text().replace('Tail paragraph', 'It is the case that a new note was added.\n\n'
                                                                     'Tail paragraph'))
        git(self.repo, 'commit', '-qam', 'review fix')
        second = self.clean_up('dk-plain-english', edit(('docs/guide.md', 'It is the case that a new note was added.',
                                                        'A new note was added.')))
        self.assertEqual(second['status'], 'complete', second)
        self.assertEqual(second['units'], {'docs/guide.md': 'delta', 'src/mod.py': 'reused'})
        [task] = self.fake.tasks
        [unit] = task['context']['units']
        self.assertEqual((unit['path'], unit['mode']), ('docs/guide.md', 'delta'))
        self.assertIn('+It is the case that a new note was added.', unit['patch'])
        added = [line for line in unit['patch'].splitlines() if line.startswith('+') and not line.startswith('+++')]
        self.assertEqual(added, ['+It is the case that a new note was added.', '+'])  # Accepted prose is context.
        self.assertEqual(task['writes'], ['repository:docs/guide.md'])
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 2}))

    def test_changed_instructions_refresh_the_whole_pass(self):
        self.clean_up('dk-plain-english', edit(GOOD))
        again = self.clean_up('dk-plain-english', edit(), instructions='Clean up the changed prose again.')
        self.assertEqual(again['units'], {'docs/guide.md': 'full', 'src/mod.py': 'full'})

    def test_a_preference_change_refreshes_only_the_paths_it_selects(self):
        def document(text):
            return {**configuration.empty_configuration(), 'scopes': [{
                'id': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY}, 'options': {}, 'permissions': {},
                'prompts': [], 'skills': [], 'preferences': [{
                    'id': 'pref-docs', 'text': text, 'applies_to': ['implementation'],
                    'include': [{'type': 'directory', 'path': 'docs'}]}]}]}
        first = self.clean_up('dk-plain-english', edit(GOOD, DOCSTRING), configuration=document('Prefer "you".'),
                              validators=[verdict('accepted')])
        self.assertEqual(first['status'], 'complete', first)
        self.calls(capabilities=1, input_gates=1, validator_session=1, native_session=1)
        [task] = self.fake.tasks
        self.assertEqual([record['id'] for record in task['preferences']['docs/guide.md']], ['pref-docs'])
        self.assertEqual(task['preferences']['src/mod.py'], [])
        reused = self.clean_up('dk-plain-english', configuration=document('Prefer "you".'))
        self.assertEqual(reused['status'], 'reused')
        self.calls(capabilities=1)  # Acceptance and prose units are both reused.
        changed = self.clean_up('dk-plain-english', edit(), configuration=document('Prefer "we".'),
                                validators=[verdict('accepted')])
        self.assertEqual(changed['units'], {'docs/guide.md': 'full', 'src/mod.py': 'reused'})
        self.calls(capabilities=1, input_gates=1, validator_session=1, native_session=1)
        self.assertEqual(self.fake.tasks[0]['writes'], ['repository:docs/guide.md'])


# -- Composition -----------------------------------------------------------------------------------

class Composed(Fixture):
    def admit(self, scopes, invocation=None):
        document = {**configuration.empty_configuration(), 'scopes': scopes}
        self.transport()
        return semantic.admit(request={'pr': PR, 'instructions': 'Fix the PR.'}, configuration=document,
                              invocation=invocation, deployment=self.deployment, roles=dk_pr_fix.role_catalog(),
                              roots={'repository': self.repo, dk_pr_fix.INSTALL_ROOT: dk_pr_fix.INSTALL},
                              transport=self.fake)

    @staticmethod
    def scope(ident, kind, target, finalizers):
        return {'id': ident, 'selector': {'kind': kind, 'id': target}, 'options': {'finalizers': finalizers},
                'permissions': {}, 'prompts': [], 'skills': [], 'preferences': []}

    def test_finalizer_order_replacement_and_explicit_empty_lists(self):
        repository = self.scope('repo', 'repository', REPOSITORY, ['plain-english', 'inspect-commits', 'asd-ste100'])
        ordered = self.admit([repository])
        self.assertEqual(ordered['preflight'], 'admitted', ordered)
        self.assertEqual([task['adapter'] for task in ordered['snapshot']['plan']['tasks']
                          if task['phase'] == 'finalizers'], ['plain-english', 'inspect-commits', 'asd-ste100'])
        self.assertEqual([task['adapter'] for task in prose.occurrences(ordered)], ['plain-english', 'asd-ste100'])
        replaced = self.admit([repository, self.scope('pr', 'pr', PR, ['asd-ste100'])])
        self.assertEqual([task['adapter'] for task in prose.occurrences(replaced)], ['asd-ste100'])
        empty = self.admit([repository], invocation={'options': {'finalizers': []}})
        self.assertEqual(prose.occurrences(empty), [])
        self.assertEqual(self.admit([])['snapshot']['options']['finalizers']['value'],
                         admission.DEFAULTS['finalizers'])  # No prose pass unless selected.
        self.calls(capabilities=1)

    def test_composed_passes_run_in_order_through_the_same_procedure(self):
        admitted = self.admit([self.scope('repo', 'repository', REPOSITORY, ['plain-english', 'asd-ste100'])])
        plain, ste = prose.occurrences(admitted)
        self.transport(edit(GOOD))
        first = prose.run(admitted, plain, repository=self.repo, base=self.pr_base, transport=self.fake,
                          ledger=self.prose_ledger, mode='composed')
        self.assertEqual(first['status'], 'complete', first)
        self.assertEqual(self.fake.tasks[0]['instructions'], 'Fix the PR.')
        self.transport(edit(('docs/guide.md', 'to use the tool.', 'so that the tool works.')))
        second = prose.run(admitted, ste, repository=self.repo, base=self.pr_base, transport=self.fake,
                           ledger=self.prose_ledger, mode='composed')
        self.assertEqual(second['status'], 'complete', second)
        self.assertEqual(second['prose_cleanup']['head'], first['prose_cleanup']['output'])
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 1, 'asd-ste100': 1}))
        self.assertIn('Before builds, the user runs `make setup` so that the tool works.',
                      (self.repo / 'docs/guide.md').read_text())
        # A replacement run reuses the last occurrence; the earlier one sees only its sibling's later edit.
        self.transport()
        self.assertEqual(prose.run(admitted, ste, repository=self.repo, base=self.pr_base, transport=self.fake,
                                   ledger=self.prose_ledger, mode='composed')['status'], 'reused')
        self.calls()
        self.transport(edit())
        rerun = prose.run(admitted, plain, repository=self.repo, base=self.pr_base, transport=self.fake,
                          ledger=self.prose_ledger, mode='composed')
        self.assertEqual((rerun['status'], rerun['units']),
                         ('complete', {'docs/guide.md': 'delta', 'src/mod.py': 'reused'}))
        self.assertIn('+Before builds, the user runs `make setup` so that the tool works.',
                      self.fake.tasks[0]['context']['units'][0]['patch'])

    def test_installed_dk_pr_fix_runs_prose_finalizers_in_plan_order(self):
        transport = self.transport()
        result = dk_pr_fix.preflight(
            request={'pr': PR, 'instructions': 'Fix it.'}, configuration=None, transport=transport,
            invocation={'options': {'finalizers': ['inspect-commits', 'plain-english', 'asd-ste100']}},
            deployment=self.deployment, repository=self.repo, home=self.base / 'state home')
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['inputs']['finalizers'], ['inspect-commits', 'plain-english', 'asd-ste100'])
        for adapter in admission.PROSE_ADAPTERS:
            self.assertEqual({key: result['inputs']['adapters'][adapter][key] for key in
                              ('kind', 'writes', 'definition', 'assignment', 'checklist', 'history')},
                             admission.ENTRY_ADAPTERS[adapter])
        self.calls(capabilities=1)


class Installed(Fixture):
    """The plugin entries: presets over the installed workflow (workflows/<adapter>.yml)."""

    def installed(self, name, **changes):
        arguments = {'request': {'pr': PR, 'instructions': 'Clean up the changed prose.'}, 'configuration': None,
                     'deployment': self.deployment, 'repository': self.repo, 'home': self.base / 'state home'}
        arguments.update(changes)
        result = prose.installed_preflight(name, transport=self.transport(), **arguments)
        self.assertFalse((self.base / 'state home').exists(), 'preflight writes nothing')
        return result

    def test_entries_map_onto_their_installed_workflows(self):
        descriptor = (dk_pr_fix.INSTALL / 'plugin.yaml').read_text()
        for name, entry in prose.ENTRIES.items():
            with self.subTest(name):
                result = self.installed(name)
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                self.calls(capabilities=1)
                self.assertEqual((result['workflow'], result['installation']['entry']),
                                 (entry['skill'], prose.WORKFLOWS[name]))
                inputs = result['inputs']
                self.assertEqual((inputs['tasks'], inputs['finalizers'], inputs['reviewers']),
                                 ([], [entry['adapter']], []))
                self.assertEqual(set(inputs['adapters']), {entry['adapter']})
                self.assertFalse(any(inputs['permissions'].values()))
                self.assertEqual(result['state']['request_document']['authorizations'], {})
                self.assertIn(f'  - name: {name}\n    path: skills/{name}/SKILL.md\n'
                              f'    workflow: {prose.WORKFLOWS[name]}\n', descriptor)
                # The entry runs main.yml in fix mode with every declared input and the bundled adapter text.
                body = (dk_pr_fix.INSTALL / prose.WORKFLOWS[name]).read_text()
                self.assertIn('uses: workflows/main.yml', body)
                self.assertIn('mode: fix', body)
                for key in inputs:
                    self.assertRegex(body, rf'\n      {key}:\n')
                    self.assertIn(f'{key}: ${{{{ inputs.{key} }}}}', body)
                adapter = admission.ENTRY_ADAPTERS[entry['adapter']]
                self.assertIn(json.dumps(adapter['assignment']), body)
                for item in adapter['checklist']:
                    self.assertIn(f'- id: {item["id"]}\n              text: {json.dumps(item["text"])}\n', body)

    def test_saved_scopes_cannot_replace_the_entry_finalizer(self):
        scope = {'id': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY},
                 'options': {'finalizers': ['inspect-commits'], 'tasks': ['read-feedback']},
                 'permissions': {}, 'prompts': [], 'skills': [], 'preferences': []}
        document = {**configuration.empty_configuration(), 'scopes': [scope]}
        result = self.installed('dk-plain-english', configuration=document)
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['inputs']['tasks'], result['inputs']['finalizers']), ([], ['plain-english']))

    def test_installed_entries_reject_authority_expansion_and_custom_inputs(self):
        grant = {'push': {'request_id': 'r1', 'instruction': 'push it', 'source': 'user'}}
        prompt = [{'id': 'tone', 'slot': 'additional', 'text': 'Prefer short words.'}]
        for label, changes, path in [
                ('authorization', {'request': {'pr': PR, 'instructions': 'Clean.', 'authorizations': grant}},
                 'request.authorizations'),
                ('finalizers', {'invocation': {'options': {'finalizers': []}}}, 'invocation'),
                ('permissions', {'invocation': {'permissions': {'push': True}}}, 'invocation'),
                ('repository target', {'request': {'repository': REPOSITORY, 'instructions': 'Clean.'}}, None),
                ('prompts', {'invocation': {'prompts': prompt}}, 'custom')]:
            with self.subTest(label):
                result = self.installed('dk-asd-ste100', **changes)
                self.assertEqual(result['preflight'], 'rejected', result)
                self.assertNotIn('inputs', result)
                if path:
                    self.assertEqual(result['error']['path'], path)
                self.assertEqual(self.engine.sessions, Counter())

if __name__ == '__main__':
    unittest.main()
