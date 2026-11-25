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

    def test_installed_dk_pr_fix_does_not_run_prose_as_a_generic_writer(self):
        transport = self.transport()
        result = dk_pr_fix.preflight(
            request={'pr': PR, 'instructions': 'Fix it.'}, configuration=None, transport=transport,
            invocation={'options': {'finalizers': ['inspect-commits', 'plain-english']}},
            deployment=self.deployment, repository=self.repo, home=self.base / 'state home')
        self.assertEqual((result['preflight'], result['error']['path']),
                         ('rejected', 'invocation.options.finalizers[1]'))
        self.calls(capabilities=1)


if __name__ == '__main__':
    unittest.main()
