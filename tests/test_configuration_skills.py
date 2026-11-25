"""Configuration entries: cf-workflows:dk-repo-onboard, dk-config and dk-preferences.

Disposable repositories, a real configuration file store and SQLite ledger, and a
fail-closed fake engine with durable gates, native configuration-agent claims and
scripted validator sessions. Unconfigured operations fail with no live fallback.
These results do not certify any engine, provider or host.
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
from cf_workflows import acceptance, admission, configuration, dk_pr_fix, entries, semantic, writes  # noqa: E402
from test_admission import CAPABILITIES, FakeTransport  # noqa: E402
from test_semantic import verdict  # noqa: E402

REPOSITORY = 'github.com/owner/repo'
PR = 'https://github.com/Owner/Repo/pull/7'
INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout


class Engine:
    """Durable engine state that outlives helper restarts: gates and native claims."""

    def __init__(self):
        self.gates, self.claims, self.sessions = [], {}, Counter()

    def open(self, request):
        gate = {'gate_id': f'gate-{len(self.gates) + 1}', 'request_id': request['request_id'],
                'evidence_id': request['evidence_id'], 'status': 'waiting', 'request': copy.deepcopy(request),
                'answer': None}
        self.gates.append(gate)
        return {'gate_id': gate['gate_id']}

    def answer(self, gate_id, answer_id, value):
        [gate] = [gate for gate in self.gates if gate['gate_id'] == gate_id]
        gate.update(status='answered', answer={'answer_id': answer_id, 'value': value})


class Transport(FakeTransport):
    """One scripted native session per new claim; a known claim is reconciled to its original result."""

    def __init__(self, engine, sessions=(), validators=(), capabilities=CAPABILITIES, lose=0):
        super().__init__(capabilities)
        self.tasks, self.assessments = [], []
        script, checks, lost = list(sessions), list(validators), {'remaining': lose}

        def native(task):
            self.tasks.append(copy.deepcopy(task))
            claim = task['claim']
            if claim not in engine.claims:
                if not script:
                    raise AssertionError('unscripted native session')
                engine.claims[claim] = script.pop(0)(task)
                engine.sessions[task['mode']] += 1
            if lost['remaining']:  # The session ran, but its response is lost.
                lost['remaining'] -= 1
                raise ConnectionError('response lost')
            return copy.deepcopy(engine.claims[claim])

        def validator(task):
            self.assessments.append(task)
            if not checks:
                raise AssertionError('unscripted validator session')
            return checks.pop(0)(task)
        self.configured.update(
            native_session=native, validator_session=validator, open_input_gate=engine.open,
            input_gates=lambda request_id: copy.deepcopy([g for g in engine.gates if g['request_id'] == request_id]))


def session(output, operations=None, **observed):
    """A scripted engine-observed native session of the assigned role."""
    def respond(task):
        reads = [{'kind': 'read', 'target': key} for key in task['readable']]
        return {'result_ref': 'result-' + task['claim'],
                'output': output(task) if callable(output) else copy.deepcopy(output),
                'observation': {'session_id': 'session-' + task['claim'][-8:], 'agent': task['agent'],
                                'role': task['role'], 'definition_sha256': task['definition_sha256'],
                                'skills_loaded': [], 'operations': reads if operations is None else operations,
                                'claim': task['claim'], **observed}}
    return respond


def proposal(*operations, resolutions=None, summary='Exact change as asked.'):
    body = {'outcome': 'proposal', 'summary': summary, 'operations': list(operations)}
    if resolutions is not None:
        body['resolutions'] = resolutions
    return session(body)


def questions(*items):
    return session({'outcome': 'questions', 'summary': 'Intent is unclear.',
                    'questions': [{'question': item, 'paths': []} for item in items]})


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw configuration ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        self.write('AGENTS.md', 'Run `make test` before every change.\nSee [guide](docs/guide.md).\n')
        self.write('docs/guide.md', 'Prefer unittest.\n')
        self.write('sub/AGENTS.md', 'Never edit generated files.\n')
        self.write('CONTRIBUTING.md', 'Write short commit subjects.\n')
        self.write('.github/copilot-instructions.md', 'Use tabs.\n')
        dk_pr_fix.verify_installation()
        self.paths = {'configuration': self.base / 'xdg config' / 'cf-workflows' / 'configuration.json',
                      'ledger': self.base / 'xdg state' / 'cf-workflows' / 'configuration.sqlite3'}
        self.store = writes.FileStore(self.paths['configuration'])
        self.engine = Engine()
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}

    def write(self, rel, body, root=None):
        path = (root or self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
        return path

    def request(self, instructions, **extra):
        return {'repository': REPOSITORY, 'instructions': instructions, **extra}

    def ledger(self):
        ledger = acceptance.Ledger(self.paths['ledger'])
        self.addCleanup(ledger.close)
        return ledger

    def transport(self, *sessions, validators=(), lose=0):
        self.fake = Transport(self.engine, sessions, validators, lose=lose)
        return self.fake

    def calls(self, **expected):
        self.assertEqual(self.fake.calls, +Counter(expected))

    def nothing_saved(self):
        self.assertFalse(self.paths['configuration'].parent.parent.exists(), 'no configuration home')
        self.assertFalse(self.paths['ledger'].parent.parent.exists(), 'no state home')
        self.assertEqual(self.engine.gates, [])
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--ignored', '--', '.git'), '')


# -- dk-repo-onboard ----------------------------------------------------------------------------

def report(task, *, rules=None, skip=(), facts=None, configuration_paths=(), conflicts=(), gaps=()):
    sources = task['context']['inventory']['sources']
    return {
        'sources': [{'path': item['path'], 'status': 'inspected' if item['status'] == 'inspected'
                     and item['path'] not in skip else 'uninspected'} for item in sources],
        'rules': rules if rules is not None else [
            {'text': 'Run make test before every change.', 'source': 'AGENTS.md', 'line': 1, 'scope': '.',
             'authority': 'selected repository rule'},
            {'text': 'Never edit generated files.', 'source': 'sub/AGENTS.md', 'line': 1, 'scope': 'sub',
             'authority': 'evidence only'}],
        'facts': facts if facts is not None else [
            {'fact': 'Tests run with make test.', 'source': 'AGENTS.md', 'line': 1, 'documented': True}],
        'configuration': [{'path': path, 'status': 'active'} for path in configuration_paths],
        'conflicts': list(conflicts), 'gaps': list(gaps), 'outcome': 'Nothing saved; the caller may resume.'}


class Onboarding(Fixture):
    def onboard(self, *sessions, request=None, **extra):
        transport = self.transport(*sessions)
        return configuration.onboard(request=request or self.request('Onboard this repository.'), store=self.store,
                                     deployment=self.deployment, repository=self.repo, transport=transport, **extra)

    def test_fresh_discovery_reports_every_source_and_saves_nothing(self):
        result = self.onboard(session(lambda task: report(task)))
        self.assertEqual(result['status'], 'complete', result)
        self.calls(capabilities=1, native_session=1)
        self.nothing_saved()
        [task] = self.fake.tasks
        self.assertEqual((task['skill'], task['adapter'], task['role'], task['skills'], task['tools'], task['writes'],
                          task['publication']),
                         ('cf-workflows:dk-repo-onboard', 'onboard-repository', 'leaf', [], 'read-only', False, False))
        self.assertEqual(task['instructions'], 'Onboard this repository.')
        self.assertEqual(hashlib.sha256(task['role_prompt'].encode()).hexdigest(),
                         next(r['sha256'] for r in INVENTORY['dk-repo-onboard']['baseline_resources']
                              if r['path'] == 'claude/skills/dk-repo-onboard/prompts/default.md'))
        sources = {item['path']: item for item in result['report']['sources']}
        self.assertEqual(set(sources), {'AGENTS.md', 'docs/guide.md', 'sub/AGENTS.md', 'CONTRIBUTING.md',
                                        '.github/copilot-instructions.md'})
        self.assertEqual({path for path, item in sources.items() if item['selection'] == 'unselected'},
                         {'sub/AGENTS.md'})
        self.assertEqual((sources['sub/AGENTS.md']['kind'], sources['sub/AGENTS.md']['scope']), ('automation', 'sub'))
        self.assertEqual(sources['CONTRIBUTING.md']['kind'], 'human')
        self.assertEqual(result['report']['outcome']['saved'], [])
        self.assertEqual(result['report']['outcome']['authority'], 'none')
        self.assertEqual(set(task['readable']), {'repository:' + path for path in sources})
        # The default entry plan has no custom input: no gate, no validator session.
        self.assertIsNone(result['admission']['semantic'])

    def test_unreadable_unsafe_and_uninspected_sources_leave_discovery_incomplete(self):
        outside = self.write('CLAUDE.md', 'Outside rule.\n', root=self.base)
        os.symlink(outside, self.repo / 'sub' / 'CLAUDE.md')
        self.write('.cursorrules', b'\xff\xfe')
        result = self.onboard(session(lambda task: report(task, skip=('CONTRIBUTING.md',))))
        self.assertEqual(result['status'], 'incomplete')
        sources = {item['path']: item for item in result['report']['sources']}
        self.assertEqual((sources['sub/CLAUDE.md']['status'], sources['.cursorrules']['status']),
                         ('unsafe', 'unreadable'))
        self.assertEqual(sources['CONTRIBUTING.md']['reported'], 'uninspected')
        self.assertEqual({gap['subject'] for gap in result['report']['gaps']},
                         {'sub/CLAUDE.md', '.cursorrules', 'CONTRIBUTING.md'})
        self.assertNotIn('repository:sub/CLAUDE.md', self.fake.tasks[0]['readable'])
        self.nothing_saved()

    def test_escaping_root_guidance_rejects_like_every_admitted_run(self):
        os.symlink(self.write('CLAUDE.md', 'Outside rule.\n', root=self.base), self.repo / 'CLAUDE.md')
        result = self.onboard()
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('guidance', result['admission']['error']['path'])
        self.calls()
        self.nothing_saved()

    def test_session_cannot_upgrade_status_govern_with_evidence_or_act(self):
        self.write('.cursorrules', b'\xff\xfe')
        cases = {
            'unreadable claimed inspected': session(lambda task: {**report(task), 'sources': [
                {'path': item['path'], 'status': 'inspected'} for item in task['context']['inventory']['sources']]}),
            'evidence as a rule': session(lambda task: report(task, rules=[
                {'text': 'Never edit generated files.', 'source': 'sub/AGENTS.md', 'line': 1, 'scope': 'sub',
                 'authority': 'selected repository rule'}])),
            'line outside source': session(lambda task: report(task, rules=[
                {'text': 'x', 'source': 'AGENTS.md', 'line': 9, 'scope': '.', 'authority': 'selected repository rule'}])),
            'missing source': session(lambda task: {**report(task), 'sources': report(task)['sources'][1:]}),
            'write': session(lambda task: report(task), operations=[{'kind': 'write', 'target': 'repository:AGENTS.md'}]),
            'command': session(lambda task: report(task), operations=[{'kind': 'command', 'target': 'make test'}]),
            'skill loaded': session(lambda task: report(task), skills_loaded=['lint']),
            'other role': session(lambda task: report(task), role='reviewer'),
        }
        for name, scripted in cases.items():
            with self.subTest(name):
                self.engine = Engine()
                result = self.onboard(scripted)
                self.assertEqual(result['status'], 'blocked', result)
                self.assertNotIn('report', result)
                self.calls(capabilities=1, native_session=1)
                self.nothing_saved()

    def test_existing_configuration_is_reported_with_provenance_and_never_changed(self):
        saved = configuration.empty_configuration()
        saved['scopes'].append({'id': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY},
                                'options': {'turns': 12}, 'permissions': {'push': True}, 'prompts': [], 'skills': [],
                                'preferences': [{'id': 'pref-a', 'text': 'Use pytest fixtures.',
                                                 'applies_to': ['review'],
                                                 'include': [{'type': 'directory', 'path': 'tests'}]}]})
        self.store.write(None, saved)
        before = self.paths['configuration'].read_bytes()
        paths = ['options.turns', 'permissions.push', 'configuration.scopes[0].preferences[0]']
        result = self.onboard(session(lambda task: report(task, configuration_paths=paths, rules=[
            {'text': 'Use pytest fixtures.', 'source': 'configuration.scopes[0].preferences[0]', 'line': None,
             'scope': 'tests', 'authority': 'saved preference'}], conflicts=[
            {'summary': 'Saved preference prefers pytest; guidance prefers unittest.',
             'paths': ['configuration.scopes[0].preferences[0]', 'docs/guide.md'], 'resolution': None}])))
        self.assertEqual(result['status'], 'complete', result)
        values = self.fake.tasks[0]['context']['configuration']['values']
        self.assertEqual(values['options.turns'], {'value': 12, 'source': 'repository:repo',
                                                   'path': 'configuration.scopes[0].options.turns'})
        self.assertEqual(values['permissions.push']['value'], True)
        self.assertEqual(self.paths['configuration'].read_bytes(), before)
        self.assertFalse(self.paths['ledger'].parent.exists())
        # A saved permission is reported, never granted: the entry has no effective permission.
        self.assertFalse(any(item['effective'] for item in result['admission']['snapshot']['permissions'].values()))

    def test_invalid_saved_configuration_is_reported_as_a_gap(self):
        self.paths['configuration'].parent.mkdir(parents=True)
        self.paths['configuration'].write_text(json.dumps({**configuration.empty_configuration(), 'scopes': 'bad'}))
        result = self.onboard(session(lambda task: report(task)))
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('configuration', {gap['subject'] for gap in result['report']['gaps']})

    def test_entry_preset_cannot_expand_execution_or_publication_authority(self):
        authorization = {'push': {'request_id': 'r1', 'instruction': 'push it', 'source': 'user'}}
        for name, extra in {
                'authorization': {'request': self.request('Onboard.', authorizations=authorization)},
                'tasks': {'invocation': {'options': {'tasks': ['address-feedback']}}},
                'permissions': {'invocation': {'permissions': {'push': True}}},
                'skills': {'invocation': {'skills': []}},
                'unknown option': {'options': {'default_applies_to': ['review']}}}.items():
            with self.subTest(name):
                arguments = {'request': self.request('Onboard.'), **extra}
                transport = self.transport()
                result = entries.preflight('dk-repo-onboard', deployment=self.deployment, repository=self.repo,
                                           transport=transport, **arguments)
                self.assertEqual(result['preflight'], 'rejected', result)
                self.calls()
        self.nothing_saved()


# -- Entry presets, original prompts and options ---------------------------------------------------

class Presets(Fixture):
    def test_entries_are_presets_with_original_prompts_and_options(self):
        for name, entry in entries.ENTRIES.items():
            with self.subTest(name):
                row = INVENTORY[name]
                self.assertEqual(entry['skill'], row['entrypoint'])
                digests = {item['path']: item['sha256'] for item in row['baseline_resources']}
                self.assertEqual(hashlib.sha256(entry['role_prompt'].encode()).hexdigest(),
                                 digests[entry['baseline_prompt']])
                self.assertEqual(entries.preset(name), {'tasks': [entry['adapter']], 'finalizers': [], 'reviewers': []})
                definition = admission.ENTRY_ADAPTERS[entry['adapter']]
                self.assertEqual((definition['writes'], definition['definition']), (False, 'leaf'))
                transport = self.transport()
                result = entries.preflight(name, request=self.request('Explain.'), deployment=self.deployment,
                                           repository=self.repo, transport=transport)
                self.assertEqual((result['preflight'], result['semantic']), ('admitted', None))
                self.calls(capabilities=1)
                [task] = result['snapshot']['plan']['tasks']
                self.assertEqual((task['adapter'], task['skill'], task['required']), (entry['adapter'], None, True))
                self.assertIsNone(result['snapshot']['request']['pr'])
        self.assertEqual(entries.check_options('dk-preferences', None),
                         {'default_applies_to': {'value': ['implementation', 'review'], 'source': 'default'}})
        with self.assertRaises(admission.AdmissionError):
            entries.check_options('dk-preferences', {'default_applies_to': []})

    def test_custom_entry_prompt_uses_the_shared_semantic_admission(self):
        transport = self.transport(validators=[verdict('accepted')])
        result = entries.preflight('dk-config', request=self.request('Explain my settings.'),
                                   invocation={'prompts': [{'id': 'brief', 'slot': 'additional', 'text': 'Be brief.'}]},
                                   deployment=self.deployment, repository=self.repo, transport=transport)
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'))
        self.calls(capabilities=1, input_gates=1, validator_session=1)
        self.assertEqual(result['entry']['prompts'], [{'id': 'brief', 'slot': 'additional', 'target': None,
                                                       'text': 'Be brief.'}])

    def test_composed_onboarding_task_runs_in_the_shared_pr_workflow(self):
        transport = self.transport()
        result = dk_pr_fix.preflight(
            request={'pr': PR, 'instructions': 'Fix it.'}, configuration=None, transport=transport,
            invocation={'options': {'tasks': ['onboard-repository', 'address-feedback'], 'finalizers': []}},
            deployment=self.deployment, repository=self.repo, home=self.base / 'state')
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', None))
        self.assertEqual(result['inputs']['adapters']['onboard-repository']['assignment'],
                         admission.ENTRY_ADAPTERS['onboard-repository']['assignment'])
        for adapter in ('configure', 'admit-preferences'):
            rejected = dk_pr_fix.preflight(
                request={'pr': PR, 'instructions': 'Fix it.'}, configuration=None, transport=self.transport(),
                invocation={'options': {'tasks': [adapter], 'finalizers': []}},
                deployment=self.deployment, repository=self.repo, home=self.base / 'state')
            self.assertEqual((rejected['preflight'], rejected['error']['path']), ('rejected', 'options.tasks'))
        repository_target = dk_pr_fix.preflight(request=self.request('Fix it.'), configuration=None,
                                                transport=self.transport(), invocation=None,
                                                deployment=self.deployment, repository=self.repo,
                                                home=self.base / 'state')
        self.assertEqual(repository_target['error']['path'], 'request.pr')


# -- dk-config ------------------------------------------------------------------------------------

class Changes(Fixture):
    def configure(self, *sessions, name='dk-config', instructions='Enable pushes for this repository.',
                  validators=(), lose=0, **extra):
        transport = self.transport(*sessions, validators=validators, lose=lose)
        return configuration.configure(name, request=self.request(instructions), store=self.store,
                                       ledger=self.ledger(), deployment=self.deployment, repository=self.repo,
                                       transport=transport, **extra)

    ENABLE = ({'op': 'add_scope', 'scope': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY}},
              {'op': 'set_permission', 'scope': 'repo', 'action': 'push', 'value': True})


class Config(Changes):
    def test_explanation_is_transient(self):
        self.store.write(None, configuration.empty_configuration())
        transport = self.transport(session({'explanation': 'Pushes are off by default.',
                                            'citations': ['permissions.push']}))
        result = configuration.explain(request=self.request('Why can you not push?'), store=self.store,
                                       deployment=self.deployment, repository=self.repo, transport=transport)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['configuration']['values']['permissions.push']['source'], 'default')
        self.calls(capabilities=1, native_session=1)
        self.assertFalse(self.paths['ledger'].parent.exists())
        self.assertEqual(self.engine.gates, [])
        self.assertEqual(self.fake.tasks[0]['mode'], 'explain')
        bad = configuration.explain(request=self.request('Why?'), store=self.store, deployment=self.deployment,
                                    repository=self.repo, transport=self.transport(
                                        session({'explanation': 'x', 'citations': ['permissions.unknown']})))
        self.assertEqual(bad['status'], 'blocked')

    def test_saved_change_writes_the_exact_admitted_revision_and_reads_it_back(self):
        result = self.configure(proposal(*self.ENABLE))
        self.assertEqual(result['status'], 'applied', result)
        # No custom input: one native session, structural admission only, no validator.
        self.calls(capabilities=2, input_gates=1, native_session=1)
        saved = self.store.read()
        self.assertEqual(saved['scopes'][0]['permissions'], {'push': True})
        self.assertIn('request:' + result['request_id'], saved['admission']['evidence_refs'])
        self.assertEqual(result['readback']['values']['permissions.push']['path'],
                         'configuration.scopes[0].permissions.push')
        self.assertEqual(result['write']['status'], 'applied')
        self.assertEqual(self.engine.sessions, Counter(change=1))
        # The same request again: settled, read back, nothing dispatched.
        again = self.configure()
        self.assertEqual((again['status'], again['write']['intent_id']), ('applied', result['write']['intent_id']))
        self.calls(capabilities=1)
        # Saving a permission does not authorize an effect: the next entry run still has none.
        self.assertFalse(any(item['effective'] for item in entries.preflight(
            'dk-config', request=self.request('x'), deployment=self.deployment, repository=self.repo,
            transport=self.transport())['snapshot']['permissions'].values()))

    def test_clarification_resumes_the_same_request_without_new_sessions(self):
        first = self.configure(questions('Which repository scope: this repository or all personal settings?'))
        self.assertEqual(first['status'], 'waiting')
        self.calls(capabilities=1, input_gates=1, native_session=1, open_input_gate=1)
        waiting = self.configure()
        self.assertEqual((waiting['status'], waiting['gate']['gate_id']), ('waiting', first['gate']['gate_id']))
        self.calls(capabilities=1, input_gates=1)
        self.engine.answer(first['gate']['gate_id'], 'a1', 'This repository only.')
        done = self.configure(proposal(*self.ENABLE))
        self.assertEqual(done['status'], 'applied', done)
        self.assertEqual(done['request_id'], first['request_id'])
        self.assertEqual(self.fake.tasks[0]['clarifications'][0]['value'], 'This repository only.')
        self.assertEqual(self.engine.sessions, Counter(change=2))

    def test_lost_session_response_reconciles_the_original_claim(self):
        failed = self.configure(proposal(*self.ENABLE), lose=1)
        self.assertEqual(failed['status'], 'blocked')
        self.assertIsNone(self.store.read())
        claim = self.fake.tasks[0]['claim']
        # Nothing is scripted: the engine reconciles the claim to its original result.
        resumed = self.configure()
        self.assertEqual(resumed['status'], 'applied', resumed)
        self.assertEqual(self.engine.sessions, Counter(change=1))
        self.assertEqual(self.fake.tasks[0]['claim'], claim)

    def test_lost_write_response_is_settled_by_readback_not_retried(self):
        original = writes.FileStore.write
        reads = {'n': 0}

        def write_then_fail(store, expected, document):
            original(store, expected, document)
            raise OSError('response lost')

        def read_fails_once(store, real=writes.FileStore.read):
            reads['n'] += 1
            if reads['n'] == 3:  # Reads: admission's base, the write's own check, then readback.
                raise OSError('unavailable')
            return real(store)
        with patch.object(writes.FileStore, 'write', write_then_fail), \
                patch.object(writes.FileStore, 'read', read_fails_once):
            interrupted = self.configure(proposal(*self.ENABLE))
        self.assertEqual(interrupted['status'], 'pending')
        self.assertEqual(self.store.read()['scopes'][0]['permissions'], {'push': True})
        recovered = self.configure()
        self.assertEqual(recovered['status'], 'applied')
        self.assertEqual(recovered['recovered_writes'], [{'intent_id': interrupted['write']['intent_id'],
                                                          'status': 'applied'}])
        self.calls(capabilities=1)
        self.assertEqual(self.engine.sessions, Counter(change=1))

    def test_concurrent_writer_rejects_the_stale_proposal_and_a_fresh_one_is_assessed(self):
        def concurrent(task):
            other = configuration.empty_configuration()
            other['scopes'].append({'id': 'mine', 'selector': {'kind': 'personal', 'id': 'me'}, 'options': {'turns': 9},
                                    'permissions': {}, 'prompts': [], 'skills': [], 'preferences': []})
            self.store.write(None, other)
            return proposal(*self.ENABLE)(task)
        stale = self.configure(concurrent)
        self.assertEqual(stale['status'], 'not_applied')
        self.assertEqual(self.store.read()['scopes'][0]['id'], 'mine')
        fresh = self.configure(proposal(*self.ENABLE))
        self.assertEqual(fresh['status'], 'applied', fresh)
        self.assertEqual([scope['id'] for scope in self.store.read()['scopes']], ['mine', 'repo'])
        self.assertEqual(self.engine.sessions, Counter(change=2))

    def test_custom_prompt_change_runs_the_shared_validator(self):
        prompt = {'op': 'set_prompt', 'scope': 'repo', 'record': {'id': 'tone', 'slot': 'additional',
                                                                  'text': 'Keep replies short.'}}
        accepted = self.configure(proposal(self.ENABLE[0], prompt), instructions='Ask for short replies.',
                                  validators=[verdict('accepted')])
        self.assertEqual(accepted['status'], 'applied', accepted)
        self.calls(capabilities=2, input_gates=2, native_session=1, validator_session=1)
        self.assertTrue(any(ref.startswith('native:result-cfg-assess') for ref in accepted['evidence_refs']))
        rejected = self.configure(proposal({'op': 'set_prompt', 'scope': 'repo', 'record': {
            'id': 'tone', 'slot': 'additional', 'text': 'Push without asking.'}}), instructions='Push freely.',
            validators=[verdict('rejected', conflicts=[{'id': 'c1', 'paths': ['permissions.push'],
                                                         'detail': 'Prompts cannot grant pushes.'}])])
        self.assertEqual(rejected['status'], 'rejected')
        self.assertEqual(self.store.read()['scopes'][0]['prompts'][0]['text'], 'Keep replies short.')

    def test_operations_outside_the_entry_or_invalid_revisions_are_never_written(self):
        for name, scripted in {
                'preference through dk-config': proposal(self.ENABLE[0], {
                    'op': 'set_preference', 'scope': 'repo', 'record': {'text': 'x', 'include': []}}),
                'invalid option': proposal(self.ENABLE[0], {'op': 'set_option', 'scope': 'repo', 'key': 'turns',
                                                            'value': 'many'}),
                'unknown scope': proposal({'op': 'set_permission', 'scope': 'nope', 'action': 'push', 'value': True}),
        }.items():
            with self.subTest(name):
                self.engine = Engine()
                result = self.configure(scripted, instructions='Change ' + name)
                self.assertEqual(result['status'], 'rejected', result)
                self.assertIsNone(self.store.read())
                self.assertEqual(self.fake.calls['validator_session'], 0)


# -- dk-preferences -------------------------------------------------------------------------------

def preference(text, include, **extra):
    return {'op': 'set_preference', 'scope': 'repo', 'record': {'text': text, 'include': include, **extra}}


TESTS = [{'type': 'directory', 'path': 'tests'}]
REPO_SCOPE = {'op': 'add_scope', 'scope': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY}}


class Preferences(Changes):
    def prefer(self, *sessions, instructions='Use pytest fixtures for shared test setup.', validators=(), **extra):
        return self.configure(*sessions, name='dk-preferences', instructions=instructions, validators=validators,
                              **extra)

    def test_new_preference_gets_explicit_modes_a_stable_id_and_readback(self):
        result = self.prefer(proposal(REPO_SCOPE, preference(
            'Use pytest fixtures for shared test setup.', TESTS, exclude=[{'type': 'directory', 'path': 'tests/gen'}])),
            validators=[verdict('accepted')], options={'default_applies_to': ['review']})
        self.assertEqual(result['status'], 'applied', result)
        self.calls(capabilities=2, input_gates=2, native_session=1, validator_session=1)
        [record] = result['readback']['preferences']
        self.assertEqual((record['applies_to'], record['include'], record['exclude'], record['condition']),
                         (['review'], TESTS, [{'type': 'directory', 'path': 'tests/gen'}], None))
        self.assertTrue(record['id'].startswith('pref-'))
        self.assertEqual(record['owner'], {'kind': 'repository', 'id': REPOSITORY})
        [unit] = self.fake.assessments[0]['input']['custom']['prompts']
        self.assertEqual(unit['scope']['applies_to'], ['review'])
        # An exact duplicate is reused, not saved twice.
        duplicate = self.prefer(proposal(preference('Use pytest fixtures for shared test setup.', TESTS,
                                                    applies_to=['review'],
                                                    exclude=[{'type': 'directory', 'path': 'tests/gen'}])),
                                instructions='Again: pytest fixtures in tests.')
        self.assertEqual(duplicate['status'], 'unchanged')
        self.assertEqual(duplicate['proposal']['changes']['reused'], {'operations[0]': record['id']})
        self.assertEqual(len(self.store.read()['scopes'][0]['preferences']), 1)

    def test_update_keeps_saved_modes_when_defaults_change(self):
        self.prefer(proposal(REPO_SCOPE, preference('Use pytest fixtures.', TESTS)), validators=[verdict('accepted')],
                    options={'default_applies_to': ['review']})
        [saved] = self.store.read()['scopes'][0]['preferences']
        result = self.prefer(proposal({'op': 'set_preference', 'scope': 'repo', 'record': {
            'id': saved['id'], 'text': 'Use pytest fixtures for shared setup.', 'include': TESTS}}),
            instructions='Reword my pytest preference.', validators=[verdict('accepted')],
            options={'default_applies_to': ['implementation']})
        self.assertEqual(result['status'], 'applied', result)
        [updated] = self.store.read()['scopes'][0]['preferences']
        self.assertEqual((updated['id'], updated['applies_to']), (saved['id'], ['review']))

    def test_overlapping_preferences_need_the_user_not_an_invented_winner(self):
        self.prefer(proposal(REPO_SCOPE, preference('Use unittest setup methods.', TESTS)),
                    instructions='Use unittest setup methods in tests.', validators=[verdict('accepted')])
        [old] = self.store.read()['scopes'][0]['preferences']
        new = preference('Use pytest fixtures for shared test setup.', [{'type': 'glob', 'path': 'tests/**/*.py'}])
        first = self.prefer(proposal(new))
        self.assertEqual(first['status'], 'waiting', first)
        self.assertEqual([(pair['change'], pair['with']) for pair in first['overlaps']], [('operations[0]', old['id'])])
        self.calls(capabilities=1, input_gates=1, native_session=1, open_input_gate=1)
        self.assertEqual(len(self.store.read()['scopes'][0]['preferences']), 1)
        # Resuming before the answer runs no session and opens no gate.
        self.assertEqual(self.prefer()['status'], 'waiting')
        self.calls(capabilities=1, input_gates=1)
        self.engine.answer(first['gate']['gate_id'], 'a1', 'Replace the unittest rule.')
        done = self.prefer(proposal({'op': 'remove_preference', 'scope': 'repo', 'id': old['id']}, new,
                                    resolutions=[{'change': 'operations[1]', 'with': old['id'], 'outcome': 'resolved',
                                                  'reason': 'User replaced it.', 'answers': ['a1']}]),
                           validators=[verdict('accepted')])
        self.assertEqual(done['status'], 'applied', done)
        self.assertEqual(done['readback']['removed'], [old['id']])
        [kept] = self.store.read()['scopes'][0]['preferences']
        self.assertEqual(kept['text'], 'Use pytest fixtures for shared test setup.')
        unsupported = self.prefer(proposal(preference('Prefer tabs in tests.', TESTS), resolutions=[
            {'change': 'operations[0]', 'with': kept['id'], 'outcome': 'resolved', 'reason': 'Assumed.',
             'answers': ['invented']}]), instructions='Prefer tabs in tests.')
        self.assertEqual(unsupported['status'], 'rejected')

    def test_disjoint_or_compatible_preferences_save_without_a_question(self):
        self.prefer(proposal(REPO_SCOPE, preference('Use unittest.', TESTS)), validators=[verdict('accepted')])
        [old] = self.store.read()['scopes'][0]['preferences']
        disjoint = self.prefer(proposal(preference('Wrap docs at 80 columns.', [{'type': 'directory', 'path': 'docs'}])),
                               instructions='Wrap docs.', validators=[verdict('accepted')])
        self.assertEqual(disjoint['status'], 'applied', disjoint)
        compatible = self.prefer(proposal(preference('Name tests after behaviour.', TESTS), resolutions=[
            {'change': 'operations[0]', 'with': old['id'], 'outcome': 'compatible', 'reason': 'Different topics.'}]),
            instructions='Name tests after behaviour.', validators=[verdict('accepted')])
        self.assertEqual(compatible['status'], 'applied', compatible)
        self.assertEqual(self.engine.gates, [])

    def test_preferences_are_personal_or_for_this_repository(self):
        for operation in ({'op': 'add_scope', 'scope': 'other', 'selector': {'kind': 'repository',
                                                                              'id': 'github.com/x/y'}},
                          {'op': 'set_permission', 'scope': 'repo', 'action': 'push', 'value': True}):
            with self.subTest(operation=operation['op']):
                self.engine = Engine()
                result = self.prefer(proposal(operation), instructions='Change ' + operation['op'])
                self.assertEqual(result['status'], 'rejected', result)
                self.assertIsNone(self.store.read())

    def test_composed_pr_admission_consumes_saved_scoped_preferences(self):
        self.prefer(proposal(REPO_SCOPE, preference('Use pytest fixtures.', TESTS, applies_to=['review'])),
                    validators=[verdict('accepted')])
        saved = self.store.read()
        roles = dk_pr_fix.role_catalog()
        roots = {'repository': self.repo, dk_pr_fix.INSTALL_ROOT: dk_pr_fix.INSTALL}

        def admit(invocation, *validators):
            self.transport(validators=validators)
            return semantic.admit(request={'pr': PR, 'instructions': 'Fix it.'}, configuration=saved,
                                  invocation=invocation, deployment=self.deployment, roles=roles, roots=roots,
                                  transport=self.fake, ledger=self.ledger())
        # A plan without reviewers does not exercise a review-only preference: nothing to assess.
        unaffected = admit({'options': {'tasks': ['address-feedback'], 'finalizers': [], 'reviewers': []}})
        self.assertEqual((unaffected['preflight'], unaffected['semantic']), ('admitted', None))
        self.calls(capabilities=1)
        # The default plan reviews, so the preference is a custom input of that run.
        governed = admit(None, verdict('accepted'))
        self.assertEqual((governed['preflight'], governed['semantic']), ('admitted', 'accepted'))
        self.assertEqual(governed['snapshot']['preferences'][0]['scope']['applies_to'], ['review'])
        reused = admit(None)
        self.assertEqual(reused['semantic'], 'accepted')
        self.calls(capabilities=1)


# -- Scoped selection and admission ---------------------------------------------------------------

class Selection(unittest.TestCase):
    @staticmethod
    def entry(ident, text, **scope):
        return {'id': ident, 'source': 'repository:repo', 'text': text,
                'scope': None if not scope else admission.preference_scope(scope, ident)}

    def test_selection_binds_only_matching_records(self):
        tests = self.entry('p1', 'Use fixtures.', applies_to=['review'], include=TESTS,
                           exclude=[{'type': 'directory', 'path': 'tests/generated'}])
        docs = self.entry('p2', 'Wrap at 80.', applies_to=['review'], include=[{'type': 'glob', 'path': 'docs/**/*.md'}])
        prompt_only = self.entry('p3', 'Honor Retry-After.', applies_to=['implementation'], include=[],
                                 condition='When implementing HTTP retries')
        legacy = self.entry('p4', 'Prefer terse replies.')
        paths = ['tests/test_a.py', 'tests/generated/x.py']
        base = configuration.resolve([tests, docs, prompt_only, legacy], 'review', paths)
        self.assertEqual([(item['id'], item['paths']) for item in base['records']],
                         [('p1', ['tests/test_a.py']), ('p4', sorted(paths))])
        changed_docs = self.entry('p2', 'Wrap at 100.', applies_to=['review'], include=[{'type': 'glob',
                                                                                         'path': 'docs/**/*.md'}])
        self.assertEqual(configuration.resolve([tests, changed_docs, prompt_only, legacy], 'review', paths)['binding'],
                         base['binding'])
        changed_tests = self.entry('p1', 'Use pytest fixtures.', applies_to=['review'], include=TESTS)
        self.assertNotEqual(configuration.resolve([changed_tests, docs, prompt_only, legacy], 'review',
                                                  paths)['binding'], base['binding'])
        implementation = configuration.resolve([tests, docs, prompt_only], 'implementation', [])
        self.assertEqual([(item['id'], item['requires_condition']) for item in implementation['records']],
                         [('p3', True)])
        self.assertTrue(configuration.selects({'type': 'glob', 'path': 'docs/**/*.md'}, 'docs/a/b.md'))
        self.assertTrue(configuration.selects({'type': 'glob', 'path': 'docs/**/*.md'}, 'docs/a.md'))
        self.assertFalse(configuration.selects({'type': 'glob', 'path': 'docs/*.md'}, 'docs/a/b.md'))

    def test_overlap_detection_is_conservative(self):
        def scope(include, modes=('review',)):
            return admission.preference_scope({'applies_to': list(modes), 'include': include,
                                               **({} if include else {'condition': 'c'})}, 's')
        directory = scope(TESTS)
        self.assertTrue(configuration.may_overlap(directory, scope([{'type': 'file', 'path': 'tests/a.py'}])))
        self.assertTrue(configuration.may_overlap(directory, scope([{'type': 'glob', 'path': '**/*.py'}])))
        self.assertFalse(configuration.may_overlap(directory, scope([{'type': 'directory', 'path': 'docs'}])))
        self.assertFalse(configuration.may_overlap(directory, scope(TESTS, ('implementation',))))
        self.assertTrue(configuration.may_overlap(directory, scope([])))
        self.assertFalse(configuration.may_overlap(scope([{'type': 'file', 'path': 'a'}]),
                                                   scope([{'type': 'file', 'path': 'b'}])))

    def test_admission_rejects_malformed_preference_scopes_and_targets(self):
        for record, fragment in (
                ({'applies_to': ['review'], 'include': [{'type': 'directory', 'path': '../x'}]}, 'normalized'),
                ({'applies_to': ['review'], 'include': [{'type': 'glob', 'path': 'a**/b'}]}, 'whole path component'),
                ({'applies_to': ['review'], 'include': [{'type': 'file', 'path': 'a*.py'}]}, 'glob selector'),
                ({'applies_to': ['review'], 'include': []}, 'requires a condition'),
                ({'applies_to': ['deploy'], 'include': TESTS}, 'implementation/review'),
                ({'include': TESTS}, 'explicitly')):
            with self.subTest(record=record):
                with self.assertRaises(admission.AdmissionError) as raised:
                    admission.preference_scope(record, 'p')
                self.assertIn(fragment, raised.exception.message)
        for request, fragment in (({'instructions': 'x'}, 'exactly one'),
                                  ({'instructions': 'x', 'pr': PR, 'repository': REPOSITORY}, 'exactly one'),
                                  ({'instructions': 'x', 'repository': 'GitHub.com/O/R'}, 'canonical')):
            with self.subTest(request=request):
                with self.assertRaises(admission.AdmissionError) as raised:
                    admission.resolve_request(request)
                self.assertIn(fragment, raised.exception.message)
        request = admission.resolve_request({'instructions': 'x', 'repository': REPOSITORY})
        self.assertEqual(admission.matches({'kind': 'repository', 'id': REPOSITORY}, request, 's'), ('repository', True))
        self.assertEqual(admission.matches({'kind': 'pr', 'id': 'https://github.com/owner/repo/pull/7'}, request, 's'),
                         ('pr', False))


if __name__ == '__main__':
    unittest.main()
