"""Semantic assessment fixtures: scripted validator sessions and a durable fake input-gate store."""
from collections import Counter
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cf_workflows import admission, semantic  # noqa: E402
from test_admission import CAPABILITIES, FakeTransport, Fixture  # noqa: E402

PROMPT = 'invocation.prompts[0]'


class Engine:
    """Durable engine-side gate state that survives helper restarts."""

    def __init__(self):
        self.gates = []

    def open(self, request):
        gate = {'gate_id': f'gate-{len(self.gates) + 1}', 'request_id': request['request_id'],
                'evidence_id': request['evidence_id'], 'status': 'waiting', 'request': copy.deepcopy(request),
                'answer': None}
        self.gates.append(gate)
        return {'gate_id': gate['gate_id']}

    def answer(self, gate_id, answer_id, value):
        [gate] = [gate for gate in self.gates if gate['gate_id'] == gate_id]
        assert gate['status'] == 'waiting'
        gate.update(status='answered', answer={'answer_id': answer_id, 'value': value})


class Transport(FakeTransport):
    """Fail-closed engine transport: one scripted response per expected validator session."""

    def __init__(self, engine, sessions=(), capabilities=CAPABILITIES):
        super().__init__(capabilities)
        self.assignments = []
        script = list(sessions)

        def session(task):
            self.assignments.append(task)
            if not script:
                raise AssertionError('unscripted validator session')
            return script.pop(0)(task)
        self.configured.update(
            validator_session=session, open_input_gate=engine.open,
            input_gates=lambda request_id: copy.deepcopy([g for g in engine.gates if g['request_id'] == request_id]))


def verdict(name, conflicts=(), uncertainties=(), resolved=(), operations=None, output=None, **observed):
    """A scripted engine-observed validator session; paths may be callables of the assignment."""
    def respond(task):
        def items(raw):
            return [{**item, 'paths': item['paths'](task) if callable(item['paths']) else item['paths']}
                    for item in raw]
        body = output if output is not None else {
            'verdict': name, 'summary': 'Fixture verdict: ' + name, 'conflicts': items(conflicts),
            'uncertainties': items(uncertainties),
            'resolved': [{**item, 'answers': [c['answer_id'] for c in task['clarifications']]} for item in items(resolved)]}
        reads = [{'kind': 'read', 'target': key} for key in task['readable']]
        return {'result_ref': 'result-' + task['claim'],
                'output': body,
                'observation': {'session_id': 'session-' + task['evidence_id'][4:12], 'agent': task['agent'],
                                'role': task['role'], 'definition_sha256': task['definition_sha256'],
                                'skills_loaded': [], 'operations': reads if operations is None else operations,
                                **observed}}
    return respond


UNCERTAIN = [{'id': 'u1', 'paths': [PROMPT], 'question': 'Does "small" forbid the test changes the request asks for?'}]


class Semantic(Fixture):
    def setUp(self):
        super().setUp()
        self.engine = Engine()
        self.invocation = {'prompts': [{'id': 'local', 'slot': 'additional', 'text': 'Keep the diff small.'}]}

    def run_semantic(self, *sessions, configuration=None, invocation='default', capabilities=CAPABILITIES):
        self.transport = Transport(self.engine, sessions, capabilities)
        invocation = self.invocation if invocation == 'default' else invocation
        result = semantic.admit(request=copy.deepcopy(self.request), configuration=copy.deepcopy(configuration),
                                invocation=copy.deepcopy(invocation), deployment=self.deployment, roles=self.roles,
                                roots=self.roots, transport=self.transport)
        # Never a target task, supplied-skill run, extra session or publication operation.
        self.assertLessEqual(set(self.transport.calls),
                             {'capabilities', 'input_gates', 'validator_session', 'open_input_gate'})
        self.assertLessEqual(self.transport.calls['validator_session'], 1)
        self.assertLessEqual(self.transport.calls['open_input_gate'], 1)
        return result

    def calls(self, sessions=0, gates_opened=0, capabilities=1, gate_reads=1):
        expected = Counter(capabilities=capabilities, input_gates=gate_reads, validator_session=sessions,
                           open_input_gate=gates_opened)
        self.assertEqual(self.transport.calls, +expected)

    def structural(self, invocation=None, configuration=None):
        return self.admit(configuration, invocation if invocation is not None else self.invocation)


class Routing(Semantic):
    def test_default_plan_incurs_no_validator_session(self):
        result = self.run_semantic(invocation=None)
        self.assertEqual((result['preflight'], result['structural'], result['semantic']), ('admitted', 'accepted', None))
        self.calls(gate_reads=0)
        # The default plan does not even require durable input gates from the engine.
        plain = {'engine': {'version': 'e'}, 'providers': CAPABILITIES['providers']}
        self.assertEqual(self.run_semantic(invocation=None, capabilities=plain)['preflight'], 'admitted')

    def test_structural_success_is_not_semantic_acceptance(self):
        result = self.structural()
        self.assertEqual((result['structural'], result['semantic'], result['preflight']),
                         ('accepted', 'blocked', 'semantic_required'))

    def test_custom_input_requires_durable_input_gates(self):
        plain = {'engine': {'version': 'e'}, 'providers': CAPABILITIES['providers']}
        result = self.run_semantic(verdict('accepted'), capabilities=plain)
        self.assertEqual((result['preflight'], result['structural']), ('rejected', 'rejected'))
        self.assertIn('unsupported engine capability: durable_input', result['error']['message'])
        self.calls(gate_reads=0)

    def test_supplied_skills_stay_rejected_before_any_session(self):
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('session_scoped_skills')
        invocation = {'options': {'tasks': [{'task': 'review', 'skill': 'lint-skill'}], 'finalizers': []},
                      'skills': [self.skill()]}
        result = self.run_semantic(verdict('accepted'), invocation=invocation, capabilities=offered)
        self.assertEqual((result['preflight'], result['structural']), ('rejected', 'accepted'))
        self.assertIn('session-scoped execution', result['error']['message'])
        self.calls(gate_reads=0)


class Verdicts(Semantic):
    def test_accepted_runs_one_fixed_role_session_without_skills(self):
        structural = self.structural()
        result = self.run_semantic(verdict('accepted'))
        self.assertEqual((result['preflight'], result['structural'], result['semantic']), ('admitted', 'accepted', 'accepted'))
        self.calls(sessions=1)
        [task] = self.transport.assignments
        self.assertEqual((task['role'], task['definition_sha256'], task['skills'], task['tools'], task['publication']),
                         (admission.VALIDATOR_ROLE, admission.BOOTSTRAP_SHA256, [], 'read-only', False))
        self.assertEqual(task['input'], structural['admission_input'])
        self.assertEqual(task['agent'], 'codex')
        self.assertIn('repository:AGENTS.md', task['readable'])
        self.assertIn(PROMPT, task['paths'])
        self.assertEqual(result['admission']['semantic'], 'accepted')
        self.assertEqual(result['admission']['evidence_refs'],
                         structural['admission']['evidence_refs'] + ['native:result-' + task['claim']])
        self.assertEqual(result['assessment']['validator']['session_id'], 'session-' + task['evidence_id'][4:12])
        # Acceptance grants nothing: the admitted snapshot is exactly the structural one.
        self.assertEqual(result['snapshot'], structural['snapshot'])

    def test_rejected_preserves_concrete_conflicts(self):
        prohibition = 'invocation.options.prohibitions[0]'
        invocation = {**self.invocation, 'options': {'prohibitions': ['Never edit vendored code.']}}
        conflict = {'id': 'c1', 'paths': [PROMPT, prohibition], 'detail': 'The prompt asks to patch vendor/.'}
        result = self.run_semantic(verdict('rejected', conflicts=[conflict]), invocation=invocation)
        self.assertEqual((result['preflight'], result['semantic']), ('rejected', 'rejected'))
        self.assertEqual(result['assessment']['conflicts'], [conflict])
        self.assertEqual(result['admission']['semantic'], 'rejected')
        self.calls(sessions=1)

    def test_needs_clarification_opens_one_durable_gate_with_the_verdict(self):
        result = self.run_semantic(verdict('needs_clarification', uncertainties=UNCERTAIN))
        self.assertEqual((result['preflight'], result['semantic']), ('waiting', 'needs_clarification'))
        self.assertEqual(result['assessment']['uncertainties'], UNCERTAIN)
        self.assertEqual(result['gate'], {'gate_id': 'gate-1', 'evidence_id': result['assessment']['evidence_id'],
                                          'status': 'waiting'})
        [gate] = self.engine.gates
        self.assertEqual(gate['request']['payload']['verdict']['uncertainties'], UNCERTAIN)
        self.assertEqual(gate['request']['payload']['editable'], {PROMPT: ['remove'], PROMPT + '.text': ['replace']})
        self.calls(sessions=1, gates_opened=1)

    def test_blocked_verdict_and_session_failure_block_preflight(self):
        result = self.run_semantic(verdict('blocked'))
        self.assertEqual((result['preflight'], result['structural'], result['semantic']), ('blocked', 'accepted', 'blocked'))
        result = self.run_semantic(lambda task: (_ for _ in ()).throw(ConnectionError('down')))
        self.assertEqual((result['preflight'], result['semantic']), ('blocked', 'blocked'))
        self.assertEqual(result['error']['message'], 'validator session failed: ConnectionError')
        self.assertEqual(result['admission']['evidence_refs'], self.structural()['admission']['evidence_refs'])

    def test_inconsistent_or_unsourced_verdicts_block(self):
        for name, response, fragment in [
            ('accepted with open uncertainty', verdict('accepted', uncertainties=UNCERTAIN), 'not acceptance'),
            ('rejected without conflict', verdict('rejected'), 'requires a concrete conflict'),
            ('clarification without item', verdict('needs_clarification'), 'requires a concrete conflict or uncertainty'),
            ('unknown path', verdict('rejected', conflicts=[{'id': 'c', 'paths': ['options.magic'], 'detail': 'x'}]),
             'unknown configuration or source path'),
            ('no path', verdict('rejected', conflicts=[{'id': 'c', 'paths': [], 'detail': 'x'}]), 'at least one'),
            ('unanswered resolution', verdict('accepted', output={
                'verdict': 'accepted', 'summary': 's', 'conflicts': [], 'uncertainties': [],
                'resolved': [{'id': 'r', 'paths': [PROMPT], 'detail': 'd', 'answers': ['invented']}]}),
             'cite recorded clarification answers'),
        ]:
            with self.subTest(name):
                self.engine = Engine()
                result = self.run_semantic(response)
                self.assertEqual((result['preflight'], result['semantic']), ('blocked', 'blocked'))
                self.assertIn(fragment, result['error']['message'])
                self.assertEqual(self.engine.gates, [])


class Authority(Semantic):
    def test_validator_cannot_waive_or_grant(self):
        for field, value in [('waivers', ['configuration.scopes[0].options.prohibitions[0]']),
                             ('permissions', {'push': True}), ('capabilities', ['merge_guard'])]:
            with self.subTest(field):
                body = {'verdict': 'accepted', 'summary': 's', 'conflicts': [], 'uncertainties': [], 'resolved': [],
                        field: value}
                result = self.run_semantic(verdict('accepted', output=body))
                self.assertEqual((result['preflight'], result['semantic']), ('blocked', 'blocked'))
                self.assertIn('unsupported field(s): ' + field, result['error']['message'])

    def test_acceptance_grants_no_permission_or_capability(self):
        config = self.configuration([self.scope('mine', 'personal', 'me', permissions={'push': True, 'merge': True},
                                                options={'prohibitions': ['push']})])
        result = self.run_semantic(verdict('accepted'), configuration=config)
        self.assertEqual(result['semantic'], 'accepted')
        permissions = result['snapshot']['permissions']
        self.assertEqual((permissions['push']['effective'], permissions['push']['prohibited'],
                          permissions['merge']['effective']), (False, True, False))
        self.assertNotIn('merge_guard', result['snapshot']['capabilities']['required'])
        # A merge grant still needs the engine merge guard; no validator session can substitute for it.
        self.request['authorizations'] = {'merge': {'request_id': 'r', 'instruction': 'Merge.', 'source': 'chat'}}
        result = self.run_semantic(verdict('accepted'), configuration=config)
        self.assertEqual(result['preflight'], 'rejected')
        self.assertIn('merge_guard', result['error']['message'])
        self.calls(gate_reads=0)

    def test_validator_attempting_target_work_is_discarded(self):
        skill = 'skill:lint-skill/SKILL.md'
        for name, response, fragment in [
            ('runs a command', verdict('accepted', operations=[{'kind': 'execute', 'target': 'pytest'}]),
             'attempted execute pytest'),
            ('writes', verdict('accepted', operations=[{'kind': 'write', 'target': 'repository:AGENTS.md'}]),
             'attempted write'),
            ('publishes', verdict('accepted', operations=[{'kind': 'publish', 'target': 'merge'}]), 'attempted publish'),
            ('reads outside proposal', verdict('accepted', operations=[{'kind': 'read', 'target': '/etc/passwd'}]),
             'attempted read /etc/passwd'),
            ('loads skills', verdict('accepted', skills_loaded=['lint']), 'loads no skills'),
            ('other role', verdict('accepted', role='fixer'), 'fixed bundled validator role'),
            ('runs skill', verdict('accepted', operations=[{'kind': 'execute', 'target': skill}]), 'attempted execute'),
        ]:
            with self.subTest(name):
                self.engine = Engine()
                result = self.run_semantic(response)
                self.assertEqual((result['preflight'], result['semantic'], result['admission']['semantic']),
                                 ('blocked', 'blocked', 'blocked'))
                self.assertIn(fragment, result['error']['message'])
                self.assertIsNone(result['assessment']['validator'])
                self.assertEqual(self.engine.gates, [])

    def test_supplied_skill_files_are_readable_but_never_run(self):
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('session_scoped_skills')
        invocation = {'options': {'tasks': [{'task': 'review', 'skill': 'lint-skill'}], 'finalizers': []},
                      'skills': [self.skill()]}
        with patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):
            result = self.run_semantic(verdict('accepted'), invocation=invocation, capabilities=offered)
            self.assertEqual(result['semantic'], 'accepted')
            [task] = self.transport.assignments
            self.assertEqual(task['skills'], [])
            self.assertTrue({'skill:lint-skill/SKILL.md', 'skill:lint-skill/refs/rules.md'} <= set(task['readable']))
            self.assertIn('invocation.options.tasks[0].skill', task['paths'])
            result = self.run_semantic(verdict('accepted', operations=[{'kind': 'execute',
                                                                        'target': 'skill:lint-skill/SKILL.md'}]),
                                       invocation=invocation, capabilities=offered)
            self.assertEqual(result['preflight'], 'blocked')


class Clarification(Semantic):
    def wait(self):
        result = self.run_semantic(verdict('needs_clarification', uncertainties=UNCERTAIN))
        self.assertEqual(result['preflight'], 'waiting')
        return result

    def test_interrupted_clarification_resumes_and_validates_the_revised_proposal(self):
        first = self.wait()
        # Restart before an answer: the gate and its verdict are recovered; nothing new runs.
        resumed = self.run_semantic()
        self.assertEqual((resumed['preflight'], resumed['gate']), ('waiting', first['gate']))
        self.assertEqual(resumed['assessment']['uncertainties'], UNCERTAIN)
        self.calls()
        self.engine.answer('gate-1', 'answer-1', {'action': 'supplement', 'text': 'Test changes are in scope.'})
        resolved = [{'id': 'u1', 'paths': [PROMPT], 'detail': 'The user put test changes in scope.'}]
        result = self.run_semantic(verdict('accepted', resolved=resolved))
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'))
        self.calls(sessions=1, capabilities=2)
        [task] = self.transport.assignments
        self.assertNotEqual(task['evidence_id'], first['gate']['evidence_id'])
        texts = [prompt['text'] for prompt in task['input']['custom']['prompts']]
        self.assertEqual(texts, ['Keep the diff small.', 'Test changes are in scope.'])
        self.assertEqual([item['answer_id'] for item in task['clarifications']], ['answer-1'])
        # Answers are recorded apart from execution evidence.
        self.assertEqual([item['answer_id'] for item in result['clarifications']], ['answer-1'])
        self.assertEqual(result['clarifications'][0]['uncertainties'], UNCERTAIN)
        self.assertFalse(any('answer' in ref for ref in result['admission']['evidence_refs']))
        self.assertEqual(result['assessment']['resolved'][0]['answers'], ['answer-1'])
        self.assertEqual(result['proposal']['invocation']['prompts'][1]['text'], 'Test changes are in scope.')

    def test_changed_proposal_is_a_new_request_and_ignores_old_answers(self):
        self.wait()
        self.engine.answer('gate-1', 'answer-1', {'action': 'remove', 'paths': [PROMPT]})
        self.invocation = {'prompts': [{'id': 'local', 'slot': 'additional', 'text': 'Keep the diff tiny.'}]}
        result = self.run_semantic(verdict('accepted'))
        self.assertEqual(result['semantic'], 'accepted')
        self.assertEqual(result['clarifications'], [])
        self.calls(sessions=1)
        self.assertEqual(self.transport.assignments[0]['input']['custom']['prompts'][0]['text'], 'Keep the diff tiny.')

    def test_removal_returns_to_the_default_path_without_a_session(self):
        self.wait()
        self.engine.answer('gate-1', 'answer-1', {'action': 'remove', 'paths': [PROMPT]})
        result = self.run_semantic()
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', None))
        self.assertEqual(result['proposal']['invocation']['prompts'], [])
        self.assertEqual([item['answer_id'] for item in result['clarifications']], ['answer-1'])
        self.calls(capabilities=2)

    def test_replacing_task_instructions_is_reassessed(self):
        self.invocation = {'options': {'tasks': [{'task': 'address-feedback', 'instructions': 'Rewrite everything.'}]}}
        path = 'invocation.options.tasks[0].instructions'
        question = [{'id': 'u1', 'paths': [path, 'request.instructions'], 'question': 'Everything, or the flaky test?'}]
        self.assertEqual(self.run_semantic(verdict('needs_clarification', uncertainties=question))['preflight'], 'waiting')
        self.assertEqual(self.engine.gates[0]['request']['payload']['editable'], {path: ['remove', 'replace']})
        self.engine.answer('gate-1', 'answer-1', {'action': 'replace', 'path': path, 'text': 'Only the flaky test.'})
        result = self.run_semantic(verdict('accepted'))
        self.assertEqual(result['semantic'], 'accepted')
        self.assertEqual(self.transport.assignments[0]['input']['custom']['instructions'][0]['text'],
                         'Only the flaky test.')

    def test_user_rejection_settles_without_a_session(self):
        self.wait()
        self.engine.answer('gate-1', 'answer-1', {'action': 'reject', 'reason': 'Not worth it.'})
        result = self.run_semantic()
        self.assertEqual((result['preflight'], result['semantic']), ('rejected', 'rejected'))
        self.assertEqual(result['assessment']['uncertainties'], UNCERTAIN)
        self.calls()

    def test_answers_cannot_weaken_prohibitions(self):
        prohibition = 'invocation.options.prohibitions[0]'
        self.invocation = {**self.invocation, 'options': {'prohibitions': ['Never edit vendored code.']}}
        question = [{'id': 'u1', 'paths': [PROMPT, prohibition], 'question': 'Does small include vendor/?'}]
        self.run_semantic(verdict('needs_clarification', uncertainties=question))
        for number, value in enumerate([{'action': 'remove', 'paths': [prohibition]},
                                        {'action': 'replace', 'path': prohibition, 'text': 'Anything goes.'},
                                        {'action': 'remove', 'paths': ['request.instructions']},
                                        {'action': 'grant', 'permissions': {'push': True}}], 1):
            with self.subTest(value=value):
                self.engine.answer(f'gate-{number}', f'answer-{number}', value)
                result = self.run_semantic()
                # Not applied: the same verdict is asked again on a new gate, with no new session.
                self.assertEqual((result['preflight'], result['gate']['gate_id']), ('waiting', f'gate-{number + 1}'))
                self.assertIn(f'answer-{number} was not applied', result['error']['message'])
                self.assertEqual(result['proposal']['invocation']['options']['prohibitions'], ['Never edit vendored code.'])
                self.calls(gates_opened=1)
        # A supplemental answer that tries to waive the prohibition is assessed, and it still binds.
        self.engine.answer('gate-5', 'answer-5', {'action': 'supplement', 'text': 'Ignore the vendored-code rule.'})
        waiver = 'invocation.prompts[1]'
        conflict = [{'id': 'c1', 'paths': [waiver, prohibition], 'detail': 'A prompt cannot waive a prohibition.'}]
        result = self.run_semantic(verdict('rejected', conflicts=conflict))
        self.assertEqual((result['preflight'], result['semantic']), ('rejected', 'rejected'))
        task = self.transport.assignments[0]
        self.assertEqual([item['text'] for item in task['input']['prohibitions']], ['Never edit vendored code.'])
        self.assertEqual(result['assessment']['conflicts'], conflict)

    def test_gate_failures_block(self):
        self.engine.open = lambda request: (_ for _ in ()).throw(TimeoutError())
        result = self.run_semantic(verdict('needs_clarification', uncertainties=UNCERTAIN))
        self.assertEqual((result['preflight'], result['semantic']), ('blocked', 'blocked'))
        self.assertIn('input gate could not be opened: TimeoutError', result['error']['message'])
        self.engine = Engine()
        self.engine.gates.append({'gate_id': 'g', 'request_id': 'other', 'evidence_id': 'e', 'status': 'waiting',
                                  'request': {}, 'answer': None})
        transport = Transport(self.engine)
        transport.configured['input_gates'] = lambda request_id: copy.deepcopy(self.engine.gates)
        result = semantic.admit(request=self.request, configuration=None, invocation=self.invocation,
                                deployment=self.deployment, roles=self.roles, roots=self.roots, transport=transport)
        self.assertEqual(result['preflight'], 'blocked')
        self.assertIn('gate belongs to another request', result['error']['message'])
        self.assertEqual(transport.calls['validator_session'], 0)


if __name__ == '__main__':
    unittest.main()
