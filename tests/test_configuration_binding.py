"""Configuration entries through their installed workflows, over a fake Agent Workflow engine port.

The host-side procedures (cf_workflows.configuration) run unchanged with
configuration_binding.EngineTransport: discovery, the configuration view, the
checks, admission and the single configuration writer stay on the host, and
each native session is one run of the entry's installed preset. The fake port
records every installed-workflow run, retains one run per correlation key and
keeps ExternalInputGate records the way the engine journal does. A session or
validator run it has no script for fails. No engine, provider or network is
used; these results do not show that a real engine or provider behaves this way.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True  # Never add caches to the installed closure.
from cf_workflows import admission, configuration, configuration_binding, dk_pr_fix, entries, package  # noqa: E402
from cf_workflows import validator_binding  # noqa: E402
from test_configuration_skills import REPOSITORY, Fixture, report  # noqa: E402
from test_validator_binding import ENGINE  # noqa: E402

INSTALL = dk_pr_fix.INSTALL
ENABLE = ({'op': 'add_scope', 'scope': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY}},
          {'op': 'set_permission', 'scope': 'repo', 'action': 'push', 'value': True})


class Port:
    """Agent Workflow engine port: installed-workflow runs, one retained run per key, and durable gates."""

    def __init__(self, sessions=(), receipt=False, model=None, lose=0):
        self.runs, self.retained, self.gates, self.calls = [], {}, {}, Counter()
        self.script, self.receipt, self.model, self.lose = list(sessions), receipt, model, lose

    def capabilities(self):
        self.calls['capabilities'] += 1
        return copy.deepcopy(ENGINE)

    def run(self, workflow, inputs, key):
        self.calls['run:' + workflow] += 1
        if inputs['mode'] == 'clarify':
            run_id = f'run-{len(self.runs) + 1}'
            self.runs.append({'run_id': run_id, 'workflow': workflow, 'inputs': copy.deepcopy(inputs), 'key': key})
            number = sum(map(len, self.gates.values())) + 1
            self.gates.setdefault(key, []).append({
                'gate_id': f'gate-{number}', 'origin_run_id': run_id, 'run_id': run_id,
                'evidence_id': inputs['evidence_id'], 'prompt': inputs['prompt'],
                'payload': copy.deepcopy(inputs['payload']), 'state': 'pending', 'answer': None})
            return {'run_id': run_id, 'status': 'waiting'}
        if inputs['mode'] == 'validate':
            raise AssertionError('unscripted validator run')
        if key not in self.retained:  # The engine admits one run per correlation key.
            if not self.script:
                raise AssertionError('unscripted configuration session')
            run_id = f'run-{len(self.runs) + 1}'
            self.runs.append({'run_id': run_id, 'workflow': workflow, 'inputs': copy.deepcopy(inputs), 'key': key})
            task = inputs['input']['assignment']
            step = {'agent': 'agent-' + run_id, 'provider': 'fixture-provider',
                    'structured': self.script.pop(0)(task)}
            if self.model is not None:
                step['model'] = self.model
            if self.receipt:  # Hypothetical: engine 8758775 records no tool operations for native tasks.
                step['operations'] = [{'kind': 'read', 'target': name} for name in task['readable']]
            self.retained[key] = {'run_id': run_id, 'status': 'succeeded', 'outputs': {inputs['mode']: step}}
        if self.lose:  # The run finished, but its response is lost.
            self.lose -= 1
            raise ConnectionError('response lost')
        return copy.deepcopy(self.retained[key])

    def inputs(self, key):
        self.calls['inputs'] += 1
        return copy.deepcopy(self.gates.get(key, []))

    def answer(self, gate_id, answer_id, value):
        [gate] = [gate for gates in self.gates.values() for gate in gates if gate['gate_id'] == gate_id]
        gate.update(state='accepted', answer={'answer_id': answer_id, 'value': value,
                                              'evidence_id': gate['evidence_id']})

    def sessions(self):
        return [run for run in self.runs if run['inputs']['mode'] != 'clarify']


def change(*operations, outcome='proposal', questions=(), summary='Exact change as asked.'):
    """A `change` result in the installed schema: every key present, operations as JSON strings."""
    return lambda task: {'outcome': outcome, 'summary': summary,
                         'operations': [json.dumps(item) for item in operations],
                         'questions': [{'question': item, 'paths': []} for item in questions], 'resolutions': []}


def onboarding(task):
    """The onboarding report in the installed schema: nullable keys are always present."""
    body = report(task)
    body['sources'] = [{**item, 'reason': None} for item in body['sources']]
    return body


class Binding(Fixture):
    def setUp(self):
        super().setUp()
        self.roots = {'repository': self.repo, dk_pr_fix.INSTALL_ROOT: INSTALL}

    def transport(self, port):
        self.port = port
        return configuration_binding.EngineTransport(port, self.roots)

    def onboard(self, port, **extra):
        return configuration.onboard(request=self.request('Onboard this repository.'), store=self.store,
                                     deployment=self.deployment, repository=self.repo,
                                     transport=self.transport(port), **extra)

    def configure(self, port, name='dk-config', instructions='Enable pushes for this repository.', **extra):
        return configuration.configure(name, request=self.request(instructions), store=self.store,
                                       ledger=self.ledger(), deployment=self.deployment, repository=self.repo,
                                       transport=self.transport(port), **extra)


def code(body):
    """Workflow YAML without its comments."""
    return '\n'.join(line for line in body.splitlines() if not line.lstrip().startswith('#'))


class InstalledEntries(unittest.TestCase):
    def test_entries_are_listed_with_presets_over_the_shared_session_graph(self):
        descriptor = package.validate_descriptor(package.parse_descriptor((INSTALL / 'plugin.yaml').read_bytes()))
        rows = {row['name']: row for row in descriptor['skills']}
        for skill, (workflow, modes) in configuration_binding.WORKFLOWS.items():
            name = skill.split(':', 1)[1]
            with self.subTest(name):
                self.assertEqual(rows[name], {'name': name, 'path': f'skills/{name}/SKILL.md', 'workflow': workflow})
                self.assertEqual(entries.ENTRIES[name]['skill'], skill)
                instructions = (INSTALL / rows[name]['path']).read_text()
                self.assertTrue(instructions.startswith(f'---\nname: {name}\n'))
                self.assertIn('This plugin version is not runnable.', instructions)
                body = (INSTALL / workflow).read_text()
                # A thin preset: its own modes only, one call of the shared graph, nothing else.
                self.assertEqual(re.findall(r'^\s*uses:\s*(\S+)\s*$', body, re.M), [configuration_binding.SHARED])
                condition = ' or '.join(f"inputs.mode == '{mode}'" for mode in modes)
                self.assertIn(f'if: ${{{{ {condition} }}}}', body)
                self.assertEqual(re.findall(r'^      (\w+):\n        value:', body, re.M), list(modes))
                self.assertNotIn('container', code(body))
        shared = (INSTALL / configuration_binding.SHARED).read_text()
        # One agent/run per mode on the literal leaf definition, no skill, a named schema and no repair;
        # no helper container, PR capture, publication stage, gate or other workflow.
        self.assertEqual(re.findall(r'^\s*uses:\s*(\S+)\s*$', shared, re.M), ['agent/run'] * 3)
        for mode in ('onboard', 'explain', 'change'):
            job = shared.split(f'\n  {mode}:\n    if:', 1)[1]
            self.assertIn(f"${{{{ inputs.mode == '{mode}' }}}}", job.split('\n', 1)[0])
            self.assertIn(f'schema: {mode}\n', job)
        self.assertEqual(shared.count('leaf: agents/leaf.yaml'), 3)
        self.assertEqual(shared.count('repair-attempts: 0'), 3)
        self.assertEqual(shared.count('model: ${{ inputs.model }}'), 3)
        for absent in ('container', 'skill:', 'helper.yml', 'main.yml', 'input/wait', 'secrets', 'request'):
            self.assertNotIn(absent, code(shared))
        # No request, PR or secret input: the target travels in the typed assignment.
        self.assertEqual(re.findall(r'^      (\w+):\n        (?:#.*\n        )?type:', shared.split('schemas:')[0], re.M),
                         ['mode', 'prompt', 'model', 'input', 'turns'])
        self.assertEqual(set(re.findall(r'^  (\w+):\n    type: object', shared, re.M)), {'onboard', 'explain', 'change'})


class EngineBound(Binding):
    def test_onboarding_blocks_without_an_operation_receipt_and_saves_nothing(self):
        result = self.onboard(Port([onboarding]))
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(result['error']['path'], 'session.observation.operations')
        self.assertIn('no operation receipt', result['error']['message'])
        [run] = self.port.runs
        self.assertEqual(run['workflow'], 'workflows/onboard.yml')
        self.assertEqual(self.port.calls, Counter({'capabilities': 1, 'run:workflows/onboard.yml': 1}))
        self.nothing_saved()

    def test_onboarding_with_a_hypothetical_receipt_reports_from_exact_bytes_and_saves_nothing(self):
        result = self.onboard(Port([onboarding], receipt=True))
        self.assertEqual(result['status'], 'complete', result)
        [run] = self.port.runs
        inputs = run['inputs']
        task = inputs['input']['assignment']
        self.assertEqual((inputs['mode'], inputs['model'], run['key']), ('onboard', '', task['claim']))
        self.assertEqual(inputs['prompt'], configuration_binding.PROMPTS['onboard'])
        # Every readable source is passed as its exact text, read again and checked against its digest.
        self.assertEqual(inputs['input']['files'], {key: (self.repo / key.split(':', 1)[1]).read_text()
                                                    for key in task['readable']})
        self.assertEqual(set(inputs['input']['files']), {'repository:' + path for path in (
            'AGENTS.md', 'docs/guide.md', 'sub/AGENTS.md', 'CONTRIBUTING.md', '.github/copilot-instructions.md')})
        self.assertEqual(result['evidence_refs'], ['native:agent-run:' + admission.digest(
            {'run': 'run-1', 'agent': 'agent-run-1', 'result': onboarding(task)})])
        self.assertEqual(result['report']['outcome']['saved'], [])
        self.nothing_saved()

    def test_saved_change_runs_one_session_and_writes_once_on_the_host(self):
        result = self.configure(Port([change(*ENABLE)], receipt=True))
        self.assertEqual(result['status'], 'applied', result)
        [run] = self.port.runs
        self.assertEqual((run['workflow'], run['inputs']['mode'], run['inputs']['input']['files']),
                         ('workflows/config.yml', 'change', {}))
        self.assertEqual(self.store.read()['scopes'][0]['permissions'], {'push': True})
        self.assertEqual(result['readback']['values']['permissions.push']['path'],
                         'configuration.scopes[0].permissions.push')
        # The same request again is settled from the ledger with no run.
        again = self.configure(Port(receipt=True))
        self.assertEqual((again['status'], again['write']['intent_id']), ('applied', result['write']['intent_id']))
        self.assertEqual(self.port.runs, [])

    def test_a_lost_response_reconciles_to_the_retained_run(self):
        port = Port([change(*ENABLE)], receipt=True, lose=1)
        lost = self.configure(port)
        self.assertEqual(lost['status'], 'blocked', lost)
        self.assertFalse(self.paths['configuration'].exists())
        resumed = configuration.configure('dk-config', request=self.request('Enable pushes for this repository.'),
                                          store=self.store, ledger=self.ledger(), deployment=self.deployment,
                                          repository=self.repo, transport=self.transport(port))
        self.assertEqual(resumed['status'], 'applied', resumed)
        # One session: the replayed claim returned the engine's retained run.
        self.assertEqual(len(port.sessions()), 1)
        self.assertEqual(port.calls['run:workflows/config.yml'], 2)

    def test_questions_open_one_engine_gate_and_the_answer_resumes_with_a_new_claim(self):
        port = Port([change(outcome='questions', questions=['Which repository?']), change(*ENABLE)], receipt=True)
        asked = self.configure(port)
        self.assertEqual(asked['status'], 'waiting', asked)
        [gate] = port.gates[asked['request_id']]
        self.assertEqual(port.runs[-1]['workflow'], validator_binding.ASSESSMENT)
        self.assertEqual(gate['payload']['assessment']['questions'], [{'question': 'Which repository?', 'paths': []}])
        # Waiting again opens nothing and runs nothing.
        self.assertEqual(self.configure(port)['status'], 'waiting')
        self.assertEqual((len(port.sessions()), len(port.gates[asked['request_id']])), (1, 1))
        port.answer(gate['gate_id'], 'answer-1', 'This one.')
        applied = self.configure(port)
        self.assertEqual(applied['status'], 'applied', applied)
        first, second = port.sessions()
        self.assertNotEqual(first['key'], second['key'])
        self.assertEqual(second['inputs']['input']['assignment']['clarifications'][0]['answer_id'], 'answer-1')

    def test_preferences_and_explanations_use_their_own_presets(self):
        record = {'text': 'Prefer pytest fixtures.', 'include': [{'type': 'directory', 'path': 'tests'}],
                  'applies_to': ['review']}
        preferred = self.configure(Port([change(
            {'op': 'add_scope', 'scope': 'me', 'selector': {'kind': 'personal', 'id': 'me'}},
            {'op': 'set_preference', 'scope': 'me', 'record': record})], receipt=True),
            name='dk-preferences', instructions='Prefer pytest fixtures in tests.')
        # A preference is custom input: the revision goes to the shared validator (assess.yml `validate`),
        # which this port does not script, so nothing is written.
        self.assertEqual((preferred['status'], preferred['assessment']['error']['path']), ('blocked', 'validator'))
        self.assertEqual([run['workflow'] for run in self.port.sessions()], ['workflows/preferences.yml'])
        self.assertEqual(self.port.calls['run:' + validator_binding.ASSESSMENT], 1)
        self.assertFalse(self.paths['configuration'].exists())
        self.store.write(None, configuration.empty_configuration())
        explained = configuration.explain(
            request=self.request('Why can you not push?'), store=self.store, deployment=self.deployment,
            repository=self.repo, transport=self.transport(Port(
                [lambda task: {'explanation': 'Pushes are off by default.', 'citations': ['permissions.push']}],
                receipt=True)))
        self.assertEqual(explained['status'], 'complete', explained)
        [run] = self.port.runs
        self.assertEqual((run['workflow'], run['inputs']['mode']), ('workflows/config.yml', 'explain'))

    def test_model_control_is_the_step_model_and_must_be_reported(self):
        controls = {'model': 'gpt-5.1-codex'}
        reported = self.onboard(Port([onboarding], receipt=True, model='gpt-5.1-codex'), controls=controls)
        self.assertEqual(reported['status'], 'complete', reported)
        self.assertEqual(self.port.runs[0]['inputs']['model'], 'gpt-5.1-codex')
        for name, model in (('unreported', None), ('another model', 'gpt-5')):
            with self.subTest(name):
                result = self.onboard(Port([onboarding], receipt=True, model=model), controls=controls)
                self.assertEqual((result['status'], result['error']['path']), ('blocked', 'session'), result)
        # A per-run reasoning effort has no engine parameter: it blocks before any run.
        effort = self.onboard(Port([onboarding], receipt=True), controls={'reasoning_effort': 'high'})
        self.assertEqual((effort['status'], effort['error']['path']), ('blocked', 'session'))
        self.assertEqual(self.port.runs, [])
        self.nothing_saved()

    def test_definition_mode_mailbox_provider_and_output_checks_block(self):
        admitted = entries.preflight('dk-config', request=self.request('Explain.'), deployment=self.deployment,
                                     repository=self.repo, transport=self.transport(Port()))
        task = configuration.assignment(admitted, mode='change', claim='claim-1', context={}, readable={})
        binding = configuration_binding
        self.assertEqual(task['definition_sha256'],
                         hashlib.sha256((INSTALL / binding.DEFINITION).read_bytes()).hexdigest())
        cases = [
            ('another definition', {**task, 'definition_sha256': '0' * 64}, 'leaf definition differs'),
            ('another role', {**task, 'role': 'fixer'}, 'leaf role only'),
            ('mode outside the entry', {**task, 'skill': 'cf-workflows:dk-preferences', 'mode': 'explain'},
             'no installed configuration workflow'),
            ('unknown entry', {**task, 'skill': 'cf-workflows:dk-pr-fix'}, 'no installed configuration workflow'),
            ('oversized input', {**task, 'context': {'blob': 'x' * validator_binding.MAILBOX}}, 'mailbox'),
            ('reasoning effort', {**task, 'native': {'reasoning_effort': 'low'}}, 'reasoning effort'),
        ]
        for name, changed, message in cases:
            with self.subTest(name):
                port = Port([change(*ENABLE)], receipt=True)
                with self.assertRaisesRegex(validator_binding.BindingError, message):
                    binding.EngineTransport(port, self.roots).native_session(changed)
                self.assertEqual(port.runs, [])
        outcome = {'run_id': 'run-1', 'status': 'succeeded',
                   'outputs': {'change': {'agent': 'agent-1', 'provider': 'fixture-provider',
                                          'structured': change(*ENABLE)(task)}}}
        self.assertEqual(binding.session(task, outcome)['output']['operations'], list(ENABLE))
        self.assertIsNone(binding.session(task, outcome)['observation']['operations'])
        for name, broken, message in [
                ('failed run', {**outcome, 'status': 'failed'}, 'did not succeed'),
                ('another mode', {**outcome, 'outputs': {'explain': outcome['outputs']['change']}}, 'no agent'),
                ('another provider', {**outcome, 'outputs': {'change': {**outcome['outputs']['change'],
                                                                         'provider': 'other'}}}, 'another provider'),
                ('object operations', {**outcome, 'outputs': {'change': {**outcome['outputs']['change'], 'structured': {
                    **change()(task), 'operations': list(ENABLE)}}}}, 'JSON strings'),
                ('invalid JSON', {**outcome, 'outputs': {'change': {**outcome['outputs']['change'], 'structured': {
                    **change()(task), 'operations': ['{']}}}}, 'not JSON')]:
            with self.subTest(name):
                with self.assertRaisesRegex(validator_binding.BindingError, message):
                    binding.session(task, broken)


if __name__ == '__main__':
    unittest.main()
