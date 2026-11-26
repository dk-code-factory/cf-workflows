"""Engine binding of the semantic transport against a fake Agent Workflow engine port.

The fake records each installed-workflow run and keeps ExternalInputGate records
the way the engine journal does. No engine, provider or network is used; these
results do not show that a real engine or provider session behaves this way.
"""
from collections import Counter
import copy
import hashlib
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cf_workflows import acceptance, admission, semantic, validator_binding  # noqa: E402
from test_admission import Fixture  # noqa: E402
from test_semantic import UNCERTAIN, verdict  # noqa: E402

PROMPT = 'invocation.prompts[0]'
ENGINE = {'version': 'fixture-engine', 'capabilities': ['durable-task-state-v1', 'durable-external-input-v1'],
          'providers': {'fixture-provider': ['admitted_roles', 'native_session']}}


class FakeEngine:
    """Agent Workflow port: installed-workflow runs and durable ExternalInputGate records."""

    def __init__(self, validate=None, capabilities=ENGINE):
        self.runs, self.gates, self.calls = [], {}, Counter()
        self.validate, self.offered = validate, capabilities

    def capabilities(self):
        self.calls['capabilities'] += 1
        return copy.deepcopy(self.offered)

    def run(self, workflow, inputs, key):
        self.calls['run:' + inputs['mode']] += 1
        run_id = f'run-{len(self.runs) + 1}'
        self.runs.append({'run_id': run_id, 'workflow': workflow, 'inputs': copy.deepcopy(inputs), 'key': key})
        if inputs['mode'] == 'validate':
            return {'run_id': run_id, **self.validate(inputs)}
        number = sum(map(len, self.gates.values())) + 1
        self.gates.setdefault(key, []).append({'gate_id': f'gate-{number}', 'origin_run_id': run_id,
                      'run_id': run_id, 'evidence_id': inputs['evidence_id'], 'prompt': inputs['prompt'],
                      'payload': copy.deepcopy(inputs['payload']), 'state': 'pending', 'answer': None,
                      'history': [{'state': 'pending'}]})
        return {'run_id': run_id, 'status': 'waiting'}

    def inputs(self, key):
        self.calls['inputs'] += 1
        return copy.deepcopy(self.gates.get(key, []))

    def answer(self, gate_id, answer_id, value, evidence_id=None):
        [gate] = [gate for gates in self.gates.values() for gate in gates if gate['gate_id'] == gate_id]
        gate.update(state='accepted', answer={'answer_id': answer_id, 'value': value,
                                              'evidence_id': evidence_id or gate['evidence_id']})


def succeeded(verdict_body, provider='fixture-provider', **outputs):
    """A succeeded `validate` run as the engine records it: no operation receipt."""
    return lambda inputs: {'status': 'succeeded', 'outputs': {'agent': 'agent-ref-1', 'provider': provider,
                                                              'verdict': verdict_body, **outputs}}


ACCEPTED = {'verdict': 'accepted', 'summary': 'Fine.', 'conflicts': [], 'uncertainties': [], 'resolved': []}


class Binding(Fixture):
    def setUp(self):
        super().setUp()
        self.invocation = {'prompts': [{'id': 'local', 'slot': 'additional', 'text': 'Keep the diff small.'}]}

    def admit(self, engine, invocation='default', transport=None, ledger=None):
        self.transport = transport or validator_binding.EngineTransport(engine, self.roots)
        return semantic.admit(request=copy.deepcopy(self.request), configuration=None,
                              invocation=copy.deepcopy(self.invocation if invocation == 'default' else invocation),
                              deployment=self.deployment, roles=self.roles, roots=self.roots,
                              transport=self.transport, ledger=ledger)

    def ledger(self):
        """A fresh connection to one disposable configuration ledger."""
        ledger = acceptance.Ledger(self.base / 'state' / 'configuration.sqlite3')
        self.addCleanup(ledger.close)
        return ledger


class Definition(unittest.TestCase):
    def test_installed_definition_is_the_conversion_of_the_pinned_bootstrap(self):
        digest = validator_binding.check_definition()
        installed = (validator_binding.INSTALL / validator_binding.DEFINITION).read_bytes()
        self.assertEqual(digest, hashlib.sha256(installed).hexdigest())
        # The engine's YAML agent contract: name, description and instructions only.
        self.assertEqual(set(re.findall(r'^([a-z-]+):', installed.decode(), re.M)), {'name', 'description', 'instructions'})
        with tempfile.TemporaryDirectory() as temp:
            copy_root = Path(temp) / 'install'
            shutil.copytree(validator_binding.INSTALL, copy_root, ignore=shutil.ignore_patterns('__pycache__'))
            (copy_root / validator_binding.DEFINITION).write_bytes(installed.replace(b'never run', b'may run'))
            with self.assertRaisesRegex(validator_binding.BindingError, 'pinned bootstrap'):
                validator_binding.check_definition(copy_root)

    def test_assessment_workflow_runs_the_literal_definition_without_skills(self):
        body = (validator_binding.INSTALL / validator_binding.ASSESSMENT).read_text()
        definition = validator_binding.DEFINITION.split('/', 1)[1]
        self.assertEqual(re.findall(r'^\s+agent: (\S+)$', body, re.M), [definition])
        self.assertEqual(re.findall(r'^\s+uses: (\S+)$', body, re.M), ['agent/run', 'input/wait'])
        self.assertNotRegex(body, r'^\s+skill:')
        self.assertIn('repair-attempts: 0', body)
        self.assertIn('evidence-id: ${{ inputs.evidence_id }}', body)
        # The output schema requires exactly the verdict fields the semantic check accepts.
        self.assertIn('required: [verdict, summary, conflicts, uncertainties, resolved]', body)
        self.assertIn('enum: [' + ', '.join(semantic.VERDICTS) + ']', body)


class Capabilities(Binding):
    def test_engine_contract_names_map_to_suite_capabilities(self):
        engine = FakeEngine()
        offered = validator_binding.EngineTransport(engine, self.roots).capabilities()
        self.assertEqual(offered['engine'], {'version': 'fixture-engine', 'capabilities': ['durable_input']})
        without = FakeEngine(capabilities={**ENGINE, 'capabilities': ['durable-task-state-v1']})
        result = self.admit(without)
        self.assertEqual(result['preflight'], 'rejected')
        self.assertIn('unsupported engine capability: durable_input', result['error']['message'])
        self.assertEqual(without.calls, Counter(capabilities=1))


class ValidatorTask(Binding):
    def test_the_task_carries_the_assignment_and_exact_readable_bytes(self):
        engine = FakeEngine(succeeded(ACCEPTED))
        self.admit(engine)
        [run] = engine.runs
        self.assertEqual(run['workflow'], validator_binding.ASSESSMENT)
        task = run['inputs']['input']['assignment']
        self.assertEqual(run['key'], task['claim'])
        self.assertEqual((task['role'], task['skills'], task['tools']), (admission.VALIDATOR_ROLE, [], 'read-only'))
        files = run['inputs']['input']['files']
        self.assertEqual(set(files), set(task['readable']))
        self.assertEqual(files['repository:AGENTS.md'], (self.repo / 'AGENTS.md').read_text())
        for key, text in files.items():
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), task['readable'][key], key)

    def test_without_an_operation_receipt_the_session_blocks(self):
        # The engine records agent, provider and structured output, not the session's tool use.
        engine = FakeEngine(succeeded(ACCEPTED))
        result = self.admit(engine)
        self.assertEqual((result['preflight'], result['semantic']), ('blocked', 'blocked'))
        self.assertEqual(result['error']['path'], 'validator.observation.operations')
        self.assertIn('no operation receipt', result['error']['message'])
        self.assertEqual(engine.calls, Counter(capabilities=1, inputs=1, **{'run:validate': 1}))
        self.assertEqual(engine.gates, {})

    def test_a_recorded_receipt_is_checked_like_any_observation(self):
        for operations, outcome in (([], 'admitted'), ([{'kind': 'read', 'target': 'repository:AGENTS.md'}], 'admitted'),
                                    ([{'kind': 'command', 'target': 'make'}], 'blocked')):
            with self.subTest(operations=operations):
                engine = FakeEngine(succeeded(ACCEPTED, operations=operations))
                result = self.admit(engine)
                self.assertEqual(result['preflight'], outcome, result.get('error'))
                if outcome == 'admitted':
                    session = result['assessment']['validator']
                    self.assertEqual(session['session_id'], 'agent-ref-1')
                    self.assertTrue(result['admission']['evidence_refs'][-1].startswith('native:agent-run:'))

    def test_a_blocked_task_is_never_retained_so_each_fresh_preflight_runs_one(self):
        for _ in range(2):
            engine = FakeEngine(succeeded(ACCEPTED))
            result = self.admit(engine, ledger=self.ledger())
            self.assertEqual(result['preflight'], 'blocked')
            self.assertEqual(engine.calls['run:validate'], 1)
        ledger = self.ledger()
        self.assertEqual(ledger.db.execute('SELECT count(*) FROM acceptances').fetchone()[0], 0)

    def test_with_a_recorded_receipt_the_agent_run_reference_is_the_reused_evidence(self):
        # Hypothetical: assess.yml declares no operation receipt output and engine 8758775 records none.
        engine = FakeEngine(succeeded(ACCEPTED, operations=[]))
        first = self.admit(engine, ledger=self.ledger())
        self.assertEqual(first['preflight'], 'admitted', first.get('error'))
        reference = first['assessment']['validator']['result_ref']
        self.assertTrue(reference.startswith('agent-run:'))
        fresh = FakeEngine()
        again = self.admit(fresh, ledger=self.ledger())
        self.assertEqual((again['preflight'], again['semantic']), ('admitted', 'accepted'))
        self.assertEqual(fresh.calls, Counter(capabilities=1))
        self.assertEqual(set(again['reuse']['reused'].values()), {reference})
        self.assertIn('native:' + reference, again['admission']['evidence_refs'])

    def test_other_provider_failed_task_or_drifted_file_blocks(self):
        drift = FakeEngine(succeeded(ACCEPTED, operations=[]))
        cases = [('provider', FakeEngine(succeeded(ACCEPTED, provider='other', operations=[]))),
                 ('failed', FakeEngine(lambda inputs: {'status': 'failed', 'outputs': {}})),
                 ('no agent', FakeEngine(lambda inputs: {'status': 'succeeded', 'outputs': {'provider': 'fixture-provider'}})),
                 ('drift', drift)]
        for name, engine in cases:
            with self.subTest(name):
                if name == 'drift':
                    original = engine.capabilities

                    def capabilities(original=original):
                        # The guidance changes after structural admission read it.
                        result = original()
                        self.write(self.repo, 'AGENTS.md', 'Changed.\n')
                        return result
                    engine.capabilities = capabilities
                result = self.admit(engine)
                self.assertEqual((result['preflight'], result['semantic']), ('blocked', 'blocked'))
                self.assertEqual(result['error']['message'], 'validator session failed: BindingError')
                self.assertEqual(result['admission']['evidence_refs'][-1][:9], 'snapshot:')
        self.assertEqual(drift.runs, [])

    def test_oversized_assignment_never_reaches_the_engine(self):
        engine = FakeEngine(succeeded(ACCEPTED, operations=[]))
        with mock.patch.object(validator_binding, 'MAILBOX', validator_binding.MAILBOX_RESERVE + 1024):
            result = self.admit(engine)
        self.assertEqual(result['preflight'], 'blocked')
        self.assertEqual(engine.runs, [])


class Gates(Binding):
    """Clarification through engine input gates, with scripted validator sessions."""

    def scripted(self, engine, *sessions):
        transport = validator_binding.EngineTransport(engine, self.roots)
        script = list(sessions)
        transport.validator_session = lambda task: script.pop(0)(task)
        return transport

    def test_clarification_opens_one_pinned_gate_and_its_answer_revises_the_proposal(self):
        engine = FakeEngine()
        result = self.admit(engine, transport=self.scripted(engine, verdict('needs_clarification',
                                                                            uncertainties=UNCERTAIN)))
        self.assertEqual((result['preflight'], result['gate']['gate_id']), ('waiting', 'gate-1'))
        [run] = engine.runs
        self.assertEqual((run['inputs']['mode'], run['key']), ('clarify', result['admission']['request_id']))
        self.assertEqual(run['inputs']['evidence_id'], result['assessment']['evidence_id'])
        self.assertEqual(run['inputs']['payload']['request_id'], result['admission']['request_id'])
        self.assertEqual(run['inputs']['payload']['assessment']['verdict']['uncertainties'], UNCERTAIN)
        # Interrupted: the pending gate is read back; no new run.
        again = self.admit(engine, transport=self.scripted(engine))
        self.assertEqual((again['preflight'], again['gate']['gate_id']), ('waiting', 'gate-1'))
        self.assertEqual(len(engine.runs), 1)
        engine.answer('gate-1', 'delivery-1', {'action': 'replace', 'path': PROMPT + '.text',
                                               'text': 'Keep the diff small; tests still apply.'})
        revised = self.admit(engine, transport=self.scripted(engine, verdict('accepted')))
        self.assertEqual((revised['preflight'], revised['semantic']), ('admitted', 'accepted'))
        self.assertEqual(revised['proposal']['invocation']['prompts'][0]['text'],
                         'Keep the diff small; tests still apply.')
        self.assertEqual(revised['clarifications'][0]['gate_id'], 'gate-1')

    def test_lost_clarification_response_resumes_the_engine_gate_without_a_new_task(self):
        engine = FakeEngine()
        run = engine.run

        def created_then_lost(workflow, inputs, key):
            run(workflow, inputs, key)
            raise ConnectionError('response lost after the engine opened the gate')
        engine.run = created_then_lost
        first = self.admit(engine, ledger=self.ledger(), transport=self.scripted(
            engine, verdict('needs_clarification', uncertainties=UNCERTAIN)))
        self.assertEqual((first['preflight'], first['error']['path']), ('blocked', 'gate'))
        engine.run = run
        # A fresh preflight reads the engine's pending gate back: no validator task and no second gate.
        again = self.admit(engine, ledger=self.ledger(), transport=self.scripted(engine))
        self.assertEqual((again['preflight'], again['gate']['gate_id']), ('waiting', 'gate-1'))
        self.assertEqual([item['inputs']['mode'] for item in engine.runs], ['clarify'])
        engine.answer('gate-1', 'delivery-1', {'action': 'replace', 'path': PROMPT + '.text',
                                               'text': 'Keep the diff small; tests still apply.'})
        revised = self.admit(engine, ledger=self.ledger(), transport=self.scripted(engine, verdict('accepted')))
        self.assertEqual(revised['semantic'], 'accepted')
        # The accepted answer replays to the same revision; it is current, so nothing runs again.
        replayed = self.admit(engine, ledger=self.ledger(), transport=self.scripted(engine))
        self.assertEqual((replayed['semantic'], replayed['proposal']), ('accepted', revised['proposal']))
        self.assertEqual(len(engine.runs), 1)

    def test_invalidated_gates_are_skipped_and_unpinned_answers_block(self):
        engine = FakeEngine()
        self.admit(engine, transport=self.scripted(engine, verdict('needs_clarification', uncertainties=UNCERTAIN)))
        [gate] = engine.gates[next(iter(engine.gates))]
        gate['state'] = 'canceled'
        # A canceled question has no answer: the proposal is assessed and asked again.
        result = self.admit(engine, transport=self.scripted(engine, verdict('needs_clarification',
                                                                            uncertainties=UNCERTAIN)))
        self.assertEqual((result['preflight'], result['gate']['gate_id']), ('waiting', 'gate-2'))
        engine.answer('gate-2', 'delivery-2', {'action': 'reject'}, evidence_id='cfg_other')
        result = self.admit(engine, transport=self.scripted(engine))
        self.assertEqual((result['preflight'], result['error']['path']), ('blocked', 'gates'))
        self.assertIn('BindingError', result['error']['message'])


if __name__ == '__main__':
    unittest.main()
