"""Occurrence-scoped supplied skills and engine-observed evidence (#16).

Admission uses disposable repositories and the fail-closed fake engine
transport from test_admission. Sessions are represented by a fake provider
gateway per provider that records loaded skills and emits the provider's own
invocation and child events as the engine would observe them. Nothing here
loads a skill, starts a session or reaches a provider.
"""
from collections import Counter
import copy
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cf_workflows import admission, skills  # noqa: E402
from test_admission import CAPABILITIES, FakeTransport, Fixture, sha  # noqa: E402

SKILL_CAPABILITIES = ['session_scoped_skills', 'observed_skill_evidence']
TASKS = [['read-feedback', 'assess-scope'],
         {'stage': 'reviews', 'reviewers': [[{'task': 'review', 'skill': 'lint-skill'}, 'review']]}]


def offering(*extra):
    offered = copy.deepcopy(CAPABILITIES)
    offered['providers']['fixture-provider'].extend(extra)
    return offered


class Gateway:
    """Fake provider gateway: each session records its loaded skills and emits provider-native events.

    It produces the engine's session records; any other operation fails without
    a live fallback.
    """

    def __init__(self, provider, binding):
        self.provider, self.binding = provider, binding
        self.calls = Counter()

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        raise AssertionError('unconfigured fixture operation: ' + name)

    def locator(self, name, sha256, namespace=None):
        """Where the engine registers a skill: a Claude plugin name or a Codex asset path."""
        namespace = namespace or skills.namespace(sha256)
        return f'{namespace}:{name}' if self.provider == 'claude' else f'/engine/assets/{namespace}/{name}'

    def session(self, continuation='new', loaded=None, invoke=True, children=(), outcome='completed',
                invoked=None, nested=False, locator=None):
        self.calls['session'] += 1
        session = f'{self.provider}-session-{self.calls["session"]}'
        skill = self.binding['skills'][0] if self.binding['skills'] else None
        if loaded is None:
            loaded = [{'name': skill['name'], 'sha256': skill['sha256']}] if skill else []
        loaded = [{'locator': self.locator(item['name'], item['sha256']), **item} for item in loaded]
        name = invoked or (skill['name'] if skill else 'lint')
        if locator is None:
            locator = self.locator(name, skill['sha256'] if skill else 'f' * 64)
        events = []
        if self.provider == 'codex':
            turn = [{'type': 'text', 'text': 'Perform the assignment.'}]
            if invoke:
                turn.append({'type': 'skill', 'name': name, 'path': locator})
            events.append({'method': 'turn/start', 'params': {'input': turn}})
            for number, (agent_type, result) in enumerate(children, 1):
                child = f'{session}-child-{number}'
                sender = 'other-thread' if nested else session
                events.append({'method': 'item/completed', 'params': {'threadId': sender, 'item': {
                    'type': 'collabAgentToolCall', 'id': f'call-{number}', 'tool': 'spawnAgent',
                    'senderThreadId': sender, 'receiverThreadIds': [child], 'agentType': agent_type,
                    'status': 'failed' if result == 'failed' else 'completed'}}})
                events.append({'method': 'item/completed', 'params': {'item': {
                    'type': 'subAgentActivity', 'kind': 'started', 'agentThreadId': child, 'agentPath': agent_type}}})
                if result == 'completed':
                    events.append({'method': 'item/completed', 'params': {'item': {
                        'type': 'subAgentActivity', 'kind': 'completed', 'agentThreadId': child,
                        'agentPath': agent_type}}})
        else:
            if invoke:
                events.append({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'content': [
                    {'type': 'tool_use', 'id': f'{session}-skill', 'name': 'Skill',
                     'input': {'skill': locator}}]}})
            for number, (agent_type, result) in enumerate(children, 1):
                tool = f'{session}-agent-{number}'
                events.append({'type': 'assistant', 'parent_tool_use_id': 'outer' if nested else None,
                               'message': {'content': [{'type': 'tool_use', 'id': tool, 'name': 'Agent',
                                                        'input': {'subagent_type': agent_type}}]}})
                events.append({'type': 'system', 'subtype': 'task_started', 'task_id': tool + '-task',
                               'tool_use_id': tool})
                events.append({'type': 'assistant', 'parent_tool_use_id': tool,
                               'message': {'content': [{'type': 'text', 'text': 'child work'}]}})
                if result in ('completed', 'failed'):
                    events.append({'type': 'system', 'subtype': 'task_notification', 'task_id': tool + '-task',
                                   'status': result})
        return {'schema_version': 1, 'kind': 'session_observation', 'source': 'engine', 'provider': self.provider,
                'occurrence': self.binding['occurrence'], 'binding': self.binding['binding'], 'session': session,
                'continuation': continuation, 'loaded': loaded, 'events': events, 'outcome': outcome}


class Bindings(Fixture):
    def admit_skill(self, tasks=TASKS, children=None, offered=None, **skill):
        record = self.skill(**skill)
        if children is not None:
            record['children'] = children
        invocation = {'options': {'tasks': tasks, 'finalizers': []}, 'skills': [record]}
        with patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):  # As if Agent Workflow #139 were present.
            return self.admit(invocation=invocation,
                              transport=FakeTransport(offered or offering(*SKILL_CAPABILITIES, 'observed_child_agents')))

    def by_adapter(self, plan):
        return [(item['adapter'], item['skills'] != []) for item in plan['occurrences']]

    def test_default_plan_loads_no_skills_and_needs_no_skill_capability(self):
        result = self.admit()
        self.assertEqual(result['preflight'], 'admitted')
        self.assertNotIn('session_scoped_skills', result['snapshot']['capabilities']['required'])
        plan = skills.session_plan(result)
        self.assertEqual(plan['with_skills'], [])
        self.assertIsNone(plan['verification'])
        for item in plan['occurrences']:
            self.assertEqual((item['skills'], item['evidence'], item['publication']), ([], None, False))
            self.assertEqual(item['controls'], {'skill_tool': False, 'agent_tool': False, 'agent_types': []})
        self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_only_the_selected_occurrence_loads_its_admitted_skill(self):
        result = self.admit_skill()
        self.assertEqual(result['preflight'], 'semantic_required')
        plan = skills.session_plan(result)
        self.assertEqual(self.by_adapter(plan), [('read-feedback', False), ('assess-scope', False),
                                                  ('review', True), ('review', False)])
        selected = plan['occurrences'][2]
        self.assertEqual(plan['with_skills'], [selected['occurrence']])
        self.assertEqual(selected['skills'][0]['sha256'], self.skill()['sha256'])
        self.assertEqual(selected['evidence'], {'invocation': 'lint', 'children': [], 'outcome': 'completed'})
        self.assertEqual(selected['controls'], {'skill_tool': True, 'agent_tool': False, 'agent_types': []})
        # The sibling reviewer and the independent verifier run as baseline sessions.
        self.assertEqual(plan['occurrences'][3]['skills'], [])
        self.assertEqual(plan['verification'], {
            'occurrence': 'finding_verification', 'definition': 'verifier', 'skills': [],
            'controls': {'skill_tool': False, 'agent_tool': False, 'agent_types': []},
            'sources': [selected['occurrence']]})
        # Unselected siblings keep the bindings they have in a plan without the skill.
        plain = skills.session_plan(self.admit(invocation={'options': {'tasks': [
            ['read-feedback', 'assess-scope'], {'stage': 'reviews', 'reviewers': [['review', 'review']]}],
            'finalizers': []}}))
        self.assertEqual([item['binding'] for item in plain['occurrences'][:2]],
                         [item['binding'] for item in plan['occurrences'][:2]])

    def test_missing_skill_or_evidence_capabilities_reject_before_any_claim(self):
        children = [{'agent_type': 'Explore', 'count': 2}]
        for name, offered, kids, missing in [
                ('loading', offering('observed_skill_evidence'), None, 'session_scoped_skills'),
                ('evidence', offering('session_scoped_skills'), None, 'observed_skill_evidence'),
                ('children', offering(*SKILL_CAPABILITIES), children, 'observed_child_agents')]:
            with self.subTest(name):
                result = self.admit_skill(children=kids, offered=offered)
                self.assertEqual((result['preflight'], result['structural']), ('rejected', 'rejected'))
                self.assertIn('unsupported capability: ' + missing, result['error']['message'])
                self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_supplied_skills_stay_rejected_until_session_scoped_loading_exists(self):
        invocation = {'options': {'tasks': TASKS, 'finalizers': []}, 'skills': [self.skill()]}
        result = self.admit(invocation=invocation, transport=FakeTransport(offering(*SKILL_CAPABILITIES)))
        self.assertEqual((result['preflight'], result['structural']), ('rejected', 'accepted'))
        self.assertIn('Agent Workflow #139', result['error']['message'])
        self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_child_declarations_are_explicit_and_bounded(self):
        for name, children, fragment in [
                ('not a list', {'Explore': 1}, 'must be an array'),
                ('unknown field', [{'agent_type': 'Explore', 'count': 1, 'model': 'x'}], 'unsupported field'),
                ('bad type', [{'agent_type': '../x', 'count': 1}], 'invalid agent type'),
                ('duplicate', [{'agent_type': 'Explore', 'count': 1}] * 2, 'duplicate agent type'),
                ('zero', [{'agent_type': 'Explore', 'count': 0}], 'must be an integer'),
                ('too many', [{'agent_type': f'T{n}', 'count': 1} for n in range(9)], 'at most 8')]:
            with self.subTest(name):
                result = self.admit_skill(children=children)
                self.assertEqual(result['preflight'], 'rejected')
                self.assertIn(fragment, result['error']['message'])
                self.assertEqual(self.transport.calls['capabilities'], 0)
        result = self.admit_skill(children=[{'agent_type': 'Explore', 'count': 2}])
        selected = skills.session_plan(result)['occurrences'][2]
        self.assertEqual(selected['controls'], {'skill_tool': True, 'agent_tool': True, 'agent_types': ['Explore']})
        self.assertIn('observed_child_agents', result['snapshot']['capabilities']['required'])
        self.assertEqual(result['admission_input']['custom']['skills']['lint-skill']['children'],
                         [{'agent_type': 'Explore', 'count': 2}])

    def test_admitted_digest_survives_source_change_and_removal(self):
        result = self.admit_skill()
        before = skills.session_plan(result)
        selected = before['occurrences'][2]
        shutil.rmtree(self.skills_root / 'lint')
        self.assertEqual(skills.session_plan(result), before)
        gateway = Gateway('codex', selected)
        self.assertEqual(skills.coverage(selected, [gateway.session()])['status'], 'covered')
        # A changed source cannot reuse the earlier admission: the stale record rejects.
        self.write(self.skills_root, 'lint/SKILL.md', 'Changed.\n')
        self.write(self.skills_root, 'lint/refs/rules.md', 'Rule one.\n')
        self.assertEqual(self.admit(invocation={'options': {'tasks': TASKS, 'finalizers': []}, 'skills': [
            {**self.skill(), 'sha256': selected['skills'][0]['sha256'],
             'files': [{'path': 'SKILL.md', 'sha256': selected['skills'][0]['files']['SKILL.md']},
                       {'path': 'refs/rules.md', 'sha256': selected['skills'][0]['files']['refs/rules.md']}]}]}
        )['error']['message'], 'skill file digest mismatch: SKILL.md')
        # Re-admitted new bytes keep every occurrence ID; only the selected binding changes.
        after = skills.session_plan(self.admit_skill())
        self.assertEqual([item['occurrence'] for item in after['occurrences']],
                         [item['occurrence'] for item in before['occurrences']])
        self.assertEqual([a['binding'] == b['binding'] for a, b in zip(after['occurrences'], before['occurrences'])],
                         [True, True, False, True])
        # Evidence for the old bytes does not cover the new binding.
        old = Gateway('codex', selected).session()
        result = skills.coverage(after['occurrences'][2], [old])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('another occurrence or binding', result['reasons'][0])


class Evidence(Fixture):
    """Coverage decisions from engine records, for both providers."""

    def setUp(self):
        super().setUp()
        plan = self.plan()
        self.plain, self.single = plan['occurrences'][0], plan['occurrences'][2]
        self.parent = self.plan([{'agent_type': 'Explore', 'count': 2}])['occurrences'][2]

    def plan(self, children=None):
        record = self.skill(**({'children': children} if children else {}))
        invocation = {'options': {'tasks': TASKS, 'finalizers': []}, 'skills': [record]}
        with patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):
            result = self.admit(invocation=invocation,
                                transport=FakeTransport(offering(*SKILL_CAPABILITIES, 'observed_child_agents')))
        return skills.session_plan(result)

    def assertCoverage(self, binding, records, status, fragment=None):
        result = skills.coverage(binding, records)
        self.assertEqual(result['status'], status, result)
        if fragment:
            self.assertTrue(any(fragment in reason for reason in result['reasons']), result['reasons'])
        return result

    def test_default_sessions_load_and_invoke_nothing(self):
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.plain)
                self.assertCoverage(self.plain, [gateway.session(invoke=False)], 'covered')
                self.assertCoverage(self.plain, [gateway.session(invoke=False, loaded=[{'name': 'lint', 'sha256': 'f' * 64}])],
                                    'failed', 'unadmitted skill loaded: lint')
                self.assertCoverage(self.plain, [gateway.session()], 'failed', 'without a supplied skill')
                self.assertCoverage(self.plain, [gateway.session(invoke=False, children=[('Explore', 'completed')])],
                                    'failed', 'without a declared child')
                self.assertEqual(gateway.calls['session'], 4)

    def test_observed_invocation_covers_and_report_only_does_not(self):
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.single)
                session = gateway.session()
                result = self.assertCoverage(self.single, [session], 'covered')
                self.assertEqual((result['invocations'], result['sessions']), (1, [session['session']]))
                # Result loss: rereading the same engine records decides the same, without another session.
                self.assertEqual(skills.coverage(self.single, [copy.deepcopy(session)]), result)
                self.assertFalse(skills.attempt_allowed(self.single, [session]))
                self.assertEqual(gateway.calls['session'], 1)
                report = {**session, 'source': 'report', 'events': [], 'claims': {'skill_invoked': True}}
                self.assertCoverage(self.single, [report], 'incomplete', 'report-only evidence is ignored')
                self.assertCoverage(self.single, [gateway.session(invoke=False)], 'incomplete',
                                    'no engine-observed invocation')
                self.assertCoverage(self.single, [gateway.session(invoked='other')], 'failed',
                                    'other than the admitted one')
                self.assertCoverage(self.single, [{**session, 'kind': 'report'}], 'incomplete', 'malformed engine record')

    def test_host_installed_and_mismatched_digests_never_count(self):
        admitted = self.single['skills'][0]
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.single)
                same_name = [{'name': 'lint', 'sha256': 'e' * 64}]
                self.assertCoverage(self.single, [gateway.session(loaded=same_name)], 'failed', 'not the admitted digest')
                extra = [{'name': 'lint', 'sha256': admitted['sha256']}, {'name': 'host-helper', 'sha256': 'e' * 64}]
                self.assertCoverage(self.single, [gateway.session(loaded=extra)], 'failed', 'unadmitted skill loaded')
                self.assertCoverage(self.single, [gateway.session(loaded=[])], 'failed', 'was not loaded')

    def test_same_named_skill_from_another_namespace_never_counts(self):
        admitted = self.single['skills'][0]
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.single)
                host = {'claude': 'host-plugin:lint', 'codex': '/home/user/.codex/skills/lint'}[provider]
                self.assertCoverage(self.single, [gateway.session(locator=host)], 'failed',
                                    'other than the admitted one: ' + host)
                other = gateway.locator('lint', admitted['sha256'], namespace='cfw-skill-' + '0' * 32)
                self.assertCoverage(self.single, [gateway.session(locator=other)], 'failed', 'other than the admitted')
                # The admitted digest registered under a foreign namespace is not the admitted registration.
                moved = [{'name': 'lint', 'sha256': admitted['sha256'], 'locator': host}]
                result = self.assertCoverage(self.single, [gateway.session(loaded=moved, locator=host)], 'failed',
                                             'outside its admitted namespace')
                self.assertEqual(result['invocations'], 0)
        escape = Gateway('codex', self.single).locator('lint', admitted['sha256']).replace('/engine/', '/engine/../')
        self.assertFalse(skills.locator_matches('codex', escape, 'lint', admitted['sha256']))
        self.assertTrue(admitted['namespace'].startswith('cfw-skill-'))

    def test_declared_children_must_be_observed_exactly(self):
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.parent)
                two = [('Explore', 'completed')] * 2
                result = self.assertCoverage(self.parent, [gateway.session(children=two)], 'covered')
                self.assertEqual([child['outcome'] for child in result['children']], ['completed', 'completed'])
                self.assertCoverage(self.parent, [gateway.session(children=two[:1])], 'incomplete', 'missing Explore children: 1 of 2')
                self.assertCoverage(self.parent, [gateway.session(children=two * 2)], 'incomplete', 'duplicate Explore children: 4 of 2')
                self.assertCoverage(self.parent, [gateway.session(children=[('Explore', 'completed'), ('Explore', 'failed')])],
                                    'incomplete', 'no completed terminal outcome')
                self.assertCoverage(self.parent, [gateway.session(children=[('Explore', 'completed'), ('Explore', None)])],
                                    'incomplete', 'no completed terminal outcome')
                self.assertCoverage(self.parent, [gateway.session(children=[*two, ('general-purpose', 'completed')])],
                                    'failed', 'child agent type not permitted: general-purpose')
                self.assertCoverage(self.parent, [gateway.session(children=two, nested=True)], 'failed', 'nested child')
                self.assertCoverage(self.parent, [gateway.session(invoke=False, children=two)], 'incomplete',
                                    'before the admitted skill was invoked')
                # A skill without declared children may not start any.
                self.assertCoverage(self.single, [Gateway(provider, self.single).session(children=two[:1])], 'failed',
                                    'without a declared child')

    def test_resumed_and_checklist_sessions_must_still_load_the_skill(self):
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.parent)
                two = [('Explore', 'completed')] * 2
                first = gateway.session(children=two[:1], outcome='interrupted')
                self.assertCoverage(self.parent, [first], 'reconcile', 'reconcile its effects')
                self.assertFalse(skills.attempt_allowed(self.parent, [first]))
                resumed = gateway.session('resume', children=two)
                self.assertCoverage(self.parent, [first, resumed], 'covered')
                checklist = gateway.session('checklist', invoke=False)
                self.assertCoverage(self.parent, [first, resumed, checklist], 'covered')
                # Re-registration missing on the new process/session: a visible failure, never completion.
                lost = gateway.session('checklist', loaded=[], invoke=False)
                self.assertCoverage(self.parent, [first, resumed, lost], 'failed',
                                    'unavailable in checklist session')
                self.assertCoverage(self.parent, [first, gateway.session('resume', loaded=[], children=two)], 'failed',
                                    'unavailable in resume session')
                self.assertCoverage(self.parent, [resumed], 'incomplete', 'start with one new session')

    def test_failed_writer_reconciles_and_reader_may_retry(self):
        writer = {**self.single, 'writes': True}
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.single)
                failed = gateway.session(outcome='failed')
                self.assertCoverage(self.single, [failed], 'incomplete', 'last session failed')
                self.assertTrue(skills.attempt_allowed(self.single, [failed]))
                self.assertCoverage(writer, [{**failed, 'binding': writer['binding']}], 'reconcile')
                self.assertFalse(skills.attempt_allowed(writer, [{**failed, 'binding': writer['binding']}]))

    def test_violations_never_lift_reconciliation(self):
        writer = {**self.single, 'writes': True}
        admitted = self.single['skills'][0]
        host = [{'name': 'lint', 'sha256': admitted['sha256']}, {'name': 'host', 'sha256': 'e' * 64}]
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.single)
                for binding, record in [
                        (self.single, gateway.session(outcome='interrupted', loaded=host)),
                        (self.single, gateway.session(outcome='unknown', loaded=[])),
                        (writer, gateway.session(outcome='failed', loaded=host)),
                        (self.parent, Gateway(provider, self.parent).session(
                            outcome='interrupted', children=[('general-purpose', 'completed')]))]:
                    record = {**record, 'binding': binding['binding']}
                    result = self.assertCoverage(binding, [record], 'failed')
                    self.assertTrue(result['reconcile'], result)
                    self.assertFalse(skills.attempt_allowed(binding, [record]))
                first = gateway.session(outcome='interrupted')
                lost = gateway.session('resume', loaded=[], outcome='unknown')
                self.assertFalse(skills.attempt_allowed(self.single, [first, lost]))
                clean = gateway.session(outcome='failed', loaded=host)
                self.assertFalse(skills.coverage(self.single, [clean])['reconcile'])
                self.assertTrue(skills.attempt_allowed(self.single, [clean]))


class InstalledOccurrences(Fixture):
    """Bindings name the installed workflow's own stable occurrence IDs."""

    def test_occurrence_ids_match_the_installed_prepare_helper(self):
        import test_dk_pr_fix_continuation as installed
        tasks = [['read-feedback', 'assess-scope', {'task': 'describe-pr', 'instructions': 'Short.'}],
                 {'stage': 'responses'}, 'describe-pr', {'stage': 'reviews', 'reviewers': [['review', 'review']]}]
        finalizers = ['inspect-commits', {'stage': 'responses'}]
        harness = installed.Continuation('run')
        harness.setUp()
        try:
            prepared = installed.Continuation.prepare(harness, tasks=tasks, finalizers=finalizers)
        finally:
            harness.doCleanups()
        fixed = {'finding_verification', 'finding_fix', 'ci_repair'}
        expected = [task['id'] for task in prepared['tasks'] if task['id'] not in fixed]
        result = self.admit(invocation={'options': {'tasks': tasks, 'finalizers': finalizers}})
        self.assertEqual([item['occurrence'] for item in skills.session_plan(result)['occurrences']], expected)


if __name__ == '__main__':
    unittest.main()
