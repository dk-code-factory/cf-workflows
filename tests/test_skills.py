"""Occurrence-scoped supplied skills and engine-observed evidence (#16).

Admission uses disposable repositories and the fail-closed fake engine
transport from test_admission. Sessions are represented by a fake provider
gateway per provider that records loaded skills and emits the provider-native
events Agent Workflow #139 retains: Claude's `Skill` call and its successful
tool result, `Agent` calls and task messages; Codex's native skill-consumption
fragment. Nothing here loads a skill, starts a session or reaches a provider.
"""
from collections import Counter
import copy
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cf_workflows import admission, skills  # noqa: E402
from test_admission import SKILL_MD, FakeTransport, Fixture, skill_offering  # noqa: E402

TASKS = [['read-feedback', 'assess-scope'],
         {'stage': 'reviews', 'reviewers': [[{'task': 'review', 'skill': 'lint-skill'}, 'review']]}]
CHILD = {'name': 'review', 'prompt': 'Review the retained reference and return the full result.',
         'description': 'Review retained reference', 'resources': ['refs/rules.md']}
CLAUDE = (admission.CLAUDE_SKILL_PROFILE, admission.CLAUDE_CHILD_PROFILE)
CODEX_ROOT = '/engine/retained/plan-1'


def offering(*profiles, provider='claude-sdk'):
    return skill_offering(provider, *profiles)


class Gateway:
    """Fake provider gateway: each session records its loaded skills and emits provider-native events.

    It produces the engine's session records; any other operation fails without
    a live fallback.
    """

    def __init__(self, provider, binding, body=SKILL_MD):
        self.provider, self.binding, self.body = provider, binding, body
        self.calls = Counter()

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        raise AssertionError('unconfigured fixture operation: ' + name)

    def locator(self, name):
        """Where the engine registers a skill: its Claude plugin command or the retained Codex entry."""
        return skills.qualified(name) if self.provider == 'claude' else f'{CODEX_ROOT}/skills/{name}/SKILL.md'

    def session(self, continuation='new', loaded=None, invoke=True, children=(), outcome='completed',
                invoked=None, nested=False, locator=None, body=None, arguments=None, controls=None,
                succeeded=True, fragment=None):
        self.calls['session'] += 1
        session = f'{self.provider}-session-{self.calls["session"]}'
        skill = self.binding['skills'][0] if self.binding['skills'] else None
        if loaded is None:
            loaded = [{'name': skill['name'], 'sha256': skill['sha256']}] if skill else []
        loaded = [{'locator': self.locator(item['name']), **item} for item in loaded]
        name = invoked or (skill['name'] if skill else 'lint')
        locator = locator or self.locator(name)
        events = []
        if self.provider == 'codex':
            # The typed skill input is only a request; the native fragment is the evidence.
            events.append({'method': 'turn/start', 'params': {'input': [
                {'type': 'text', 'text': 'Perform the assignment.'}, {'type': 'skill', 'name': name, 'path': locator}]}})
            if invoke:
                text = f'<skill>\n<name>{name}</name>\n<path>{locator}</path>\n{body or self.body}\n</skill>'
                item = {'type': 'message', 'role': 'user', 'id': session + '-fragment',
                        'content': [{'type': 'input_text', 'text': text}],
                        'internal_chat_message_metadata_passthrough': {
                            'turn_id': 'turn-1', 'content_item_kinds': [skills.CODEX_SKILL_KIND],
                            'content_item_metadata': [{'provenance': {'type': 'skills'}}]}}
                params = {'threadId': session, 'turnId': 'turn-1', 'item': item}
                if fragment:
                    fragment(params)
                events.append({'method': 'item/completed', 'params': params})
            for number, (agent_type, _) in enumerate(children, 1):
                events.append({'method': 'item/completed', 'params': {'threadId': session, 'item': {
                    'type': 'collabAgentToolCall', 'id': f'call-{number}', 'tool': 'spawnAgent',
                    'senderThreadId': session, 'receiverThreadIds': [f'{session}-child'], 'agentType': agent_type}}})
        else:
            if invoke:
                call = f'{session}-skill'
                selected = arguments if arguments is not None else (skill['arguments'] if skill else '')
                events.append({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'content': [
                    {'type': 'tool_use', 'id': call, 'name': 'Skill',
                     'input': {'skill': locator, **({'args': selected} if selected else {})}}]}})
                if succeeded is not None:
                    events.append({'type': 'user', 'tool_use_result': {'success': succeeded, 'commandName': locator},
                                   'message': {'content': [{'type': 'tool_result', 'tool_use_id': call,
                                                            'is_error': not succeeded}]}})
            for number, (agent_type, result) in enumerate(children, 1):
                tool = f'{session}-agent-{number}'
                call = {'subagent_type': agent_type, 'prompt': CHILD['prompt'], 'description': CHILD['description'],
                        'run_in_background': False, **(controls or {})}
                events.append({'type': 'assistant', 'parent_tool_use_id': 'outer' if nested else None,
                               'message': {'content': [{'type': 'tool_use', 'id': tool, 'name': 'Agent',
                                                        'input': call}]}})
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
    def admit_skill(self, tasks=TASKS, children=None, offered=None, provider='claude-sdk', **skill):
        record = self.skill(**skill)
        if children is not None:
            record['children'] = children
        invocation = {'options': {'tasks': tasks, 'finalizers': []}, 'skills': [record]}
        if offered is None:
            offered = offering(*CLAUDE) if provider == 'claude-sdk' else offering(provider=provider)
        with patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):  # As if the workflow were wired.
            return self.admit(invocation=invocation, transport=FakeTransport(offered),
                              deployment=self.skill_deployment(provider))

    def by_adapter(self, plan):
        return [(item['adapter'], item['skills'] != []) for item in plan['occurrences']]

    def test_default_plan_loads_no_skills_and_needs_no_skill_profile(self):
        result = self.admit()
        self.assertEqual(result['preflight'], 'admitted')
        self.assertNotIn('skill_profiles', result['snapshot']['capabilities'])
        plan = skills.session_plan(result)
        self.assertEqual(plan['with_skills'], [])
        self.assertIsNone(plan['verification'])
        for item in plan['occurrences']:
            self.assertEqual((item['skills'], item['evidence'], item['publication']), ([], None, False))
            self.assertEqual(item['controls'], {'skill_tool': False, 'agent_tool': False, 'agent_types': []})
            self.assertFalse({'input', 'selection', 'output'} & set(item))
        self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_only_the_selected_occurrence_loads_its_admitted_skill(self):
        result = self.admit_skill()
        self.assertEqual(result['preflight'], 'semantic_required')
        plan = skills.session_plan(result)
        self.assertEqual(self.by_adapter(plan), [('read-feedback', False), ('assess-scope', False),
                                                  ('review', True), ('review', False)])
        selected = plan['occurrences'][2]
        self.assertEqual(plan['with_skills'], [selected['occurrence']])
        loaded = selected['skills'][0]
        self.assertEqual(loaded['sha256'], self.skill()['sha256'])
        self.assertEqual((loaded['plugin'], loaded['qualified']), ('aw-lint', 'aw-lint:lint'))
        self.assertEqual((loaded['entry'], loaded['resources'], loaded['arguments']), ('SKILL.md', ['refs/rules.md'], ''))
        self.assertEqual(selected['evidence'], {'invocation': 'lint', 'children': [], 'outcome': 'completed'})
        self.assertEqual(selected['controls'], {'skill_tool': True, 'agent_tool': False, 'agent_types': []})
        # The engine's `type: skill` directory input and `with.skill` selection, with repairs disabled.
        self.assertEqual(selected['input'], {'directory': {'root': 'skills', 'path': 'lint'}})
        self.assertEqual(selected['selection'], {'input': 'skill', 'arguments': '', 'resources': ['refs/rules.md'],
                                                 'evidence': {'invocation': 'required', 'children': []},
                                                 'children': []})
        self.assertEqual(selected['output'], {'format': 'json', 'repair-attempts': 0})
        self.assertEqual(result['snapshot']['capabilities']['skill_profiles'], {'task': [admission.CLAUDE_SKILL_PROFILE]})
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

    def test_upstream_profiles_are_required_per_runtime_before_any_claim(self):
        # A runtime outside the two #139 providers rejects locally, before the engine is queried.
        invocation = {'options': {'tasks': TASKS, 'finalizers': []}, 'skills': [self.skill()]}
        self.assertRejected(self.admit(invocation=invocation, transport=FakeTransport(offering(*CLAUDE))),
                            'unsupported supplied-skill provider; use claude-sdk or codex-app-server')
        for name, provider, children, offered, missing in [
                ('claude skill', 'claude-sdk', None, offering(), admission.CLAUDE_SKILL_PROFILE),
                ('claude child', 'claude-sdk', [CHILD], offering(admission.CLAUDE_SKILL_PROFILE),
                 admission.CLAUDE_CHILD_PROFILE),
                ('codex skill', 'codex-app-server', None, offering(admission.CLAUDE_SKILL_PROFILE,
                                                                   provider='codex-app-server'),
                 admission.CODEX_SKILL_PROFILE)]:
            with self.subTest(name):
                if name != 'codex skill':
                    offered['providers']['claude-sdk'] = ['admitted_roles', 'native_session']
                offered['providers'][provider] = [item for item in offered['providers'][provider] if item != missing]
                result = self.admit_skill(children=children, offered=offered, provider=provider)
                self.assertRejected(result, 'unsupported capability: ' + missing, probed=True)
        result = self.admit_skill(children=[CHILD])
        self.assertEqual(result['snapshot']['capabilities']['skill_profiles'], {'task': [admission.CLAUDE_CHILD_PROFILE]})
        result = self.admit_skill(provider='codex-app-server')
        self.assertEqual(result['snapshot']['capabilities']['skill_profiles'], {'task': [admission.CODEX_SKILL_PROFILE]})
        # Any deployed runtime may run the occurrence, so every runtime needs its own profile.
        offered = offering(*CLAUDE)
        offered['providers']['codex-app-server'].append(admission.CODEX_SKILL_PROFILE)
        mixed = {**self.deployment, 'agents': {'claude': {'provider': 'claude-sdk', 'execution': {'mode': 'local'}},
                                               'codex': {'provider': 'codex-app-server', 'execution': {'mode': 'local'}}}}
        with patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):
            result = self.admit(invocation=invocation, transport=FakeTransport(offered), deployment=mixed)
            self.assertEqual(result['snapshot']['capabilities']['skill_profiles'],
                             {'claude': [admission.CLAUDE_SKILL_PROFILE], 'codex': [admission.CODEX_SKILL_PROFILE]})
            # Codex has no child profile: a skill with a child cannot run on a mixed deployment.
            self.assertRejected(self.admit(invocation={**invocation, 'skills': [{**self.skill(), 'children': [CHILD]}]},
                                           transport=FakeTransport(offered), deployment=mixed),
                                'Codex native children are not supported')

    def test_codex_rejects_children_and_arguments_before_any_claim(self):
        for name, changes, fragment in [
                ('children', {'children': [CHILD]}, 'Codex native children are not supported (skill lint-skill)'),
                ('arguments', {'arguments': 'exact arguments'}, 'Codex skill arguments must be empty')]:
            with self.subTest(name):
                self.assertRejected(self.admit_skill(provider='codex-app-server', **changes),
                                    'deployment.agents.task.provider: ' + fragment)
        # Upstream admits a Codex skill only under a read-only (or unset) sandbox.
        offered = offering(provider='codex-app-server')
        for sandbox, accepted in (('read-only', True), ('workspace-write', False)):
            with self.subTest(sandbox=sandbox), patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):
                deployment = {**self.deployment, 'agents': {'task': {'provider': 'codex-app-server',
                                                                     'sandbox': sandbox, 'execution': {'mode': 'local'}}}}
                result = self.admit(invocation={'options': {'tasks': TASKS, 'finalizers': []},
                                                'skills': [self.skill()]},
                                    transport=FakeTransport(offered), deployment=deployment)
                if accepted:
                    self.assertEqual(result['structural'], 'accepted')
                else:
                    self.assertRejected(result, 'deployment.agents.task.sandbox: '
                                                'Codex supplied skills need a read-only sandbox')
        # Neither runtime admits a supplied skill in container execution, including the deployment's
        # container execution an agent without its own inherits upstream.
        for provider, execution in [(p, e) for p in ('claude-sdk', 'codex-app-server')
                                    for e in ({'execution': {'mode': 'container'}}, {}, {'execution': {}})]:
            with self.subTest(provider=provider, execution=execution), \
                    patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):
                deployment = {**self.deployment, 'agents': {'task': {'provider': provider, **execution}}}
                self.assertRejected(self.admit(invocation={'options': {'tasks': TASKS, 'finalizers': []},
                                                           'skills': [self.skill()]},
                                               transport=FakeTransport(offered), deployment=deployment),
                                    'deployment.agents.task.execution.mode: '
                                    'supplied skills need explicit local execution; '
                                    'an agent without one inherits the container execution')
        result = self.admit_skill(arguments='exact arguments')
        selected = skills.session_plan(result)['occurrences'][2]
        self.assertEqual(selected['selection']['arguments'], 'exact arguments')
        self.assertEqual(selected['skills'][0]['arguments'], 'exact arguments')

    def test_skill_arguments_are_literal_and_bounded(self):
        for name, value, fragment in [
                ('expression', '${{ inputs.target }}', 'literal; expressions are not admitted'),
                ('not a string', ['x'], 'must be a string'),
                ('nul', 'a\0b', 'without NUL'),
                ('too long', 'x' * ((64 << 10) + 1), 'at most 64 KiB')]:
            with self.subTest(name):
                result = self.admit_skill(arguments=value)
                self.assertRejected(result, 'invocation.skills[0].arguments')
                self.assertIn(fragment, result['error']['message'])

    def test_skill_md_frontmatter_is_limited_to_name_and_description(self):
        body = 'Use [rules](refs/rules.md).\n'
        for name, content, fragment in [
                ('no frontmatter', body, 'needs name/description frontmatter'),
                ('unterminated', '---\nname: lint\ndescription: d\n' + body, 'unterminated'),
                ('authority', '---\nname: lint\ndescription: d\nallowed-tools: Bash\n---\n' + body,
                 'only name and description'),
                ('model', '---\nname: lint\ndescription: d\nmodel: opus\n---\n' + body, 'only name and description'),
                ('duplicate', '---\nname: lint\nname: lint\ndescription: d\n---\n' + body, 'duplicate'),
                ('missing description', '---\nname: lint\n---\n' + body, 'needs name and description'),
                ('blank description', "---\nname: lint\ndescription: ' '\n---\n" + body, 'nonempty'),
                ('boolean', '---\nname: lint\ndescription: true\n---\n' + body, 'must be strings'),
                ('number', '---\nname: lint\ndescription: 12\n---\n' + body, 'must be strings'),
                # yaml.v3 tags these plain scalars !!timestamp and !!merge, not !!str.
                ('date', '---\nname: lint\ndescription: 2026-11-26\n---\n' + body, 'must be strings'),
                ('timestamp', '---\nname: lint\ndescription: 2001-12-14 21:59:43.10\n---\n' + body,
                 'must be strings'),
                ('merge key', '---\nname: lint\ndescription: <<\n---\n' + body, 'must be strings'),
                ('block scalar', '---\nname: lint\ndescription: |\n  d\n---\n' + body, 'metadata'),
                ('flow', '---\nname: lint\ndescription: [a]\n---\n' + body, 'quote it'),
                ('other name', '---\nname: other\ndescription: d\n---\n' + body, 'differs from the SKILL.md'),
                ('unportable name', '---\nname: Lint_X\ndescription: d\n---\n' + body, 'portable skill name'),
                # 11000 bytes become 66000 after Go's HTML-safe JSON escaping: over the 64 KiB bound.
                ('escaped size', '---\nname: lint\ndescription: d\n---\n' + body + '<' * 11000,
                 'exceeds the 64 KiB')]:
            with self.subTest(name):
                self.write(self.skills_root, 'lint/SKILL.md', content)
                result = self.admit_skill()
                self.assertRejected(result, 'invocation.skills[0]')
                self.assertIn(fragment, result['error']['message'])
        (self.skills_root / 'lint/SKILL.md').write_bytes(b'---\nname: lint\ndescription: \xff\n---\n' + body.encode())
        self.assertRejected(self.admit_skill(), 'UTF-8')
        for content in ('---\nname: lint\ndescription: "Lint: the change."\n---\n' + body,
                        "---\nname: 'lint'\ndescription: 'It''s fine'\n---\n" + body,
                        "---\nname: lint\ndescription: '2026-11-26'\n---\n" + body,
                        '---\ndescription: Lint the change.\nname: lint\n---\n' + body + '<' * 10000):
            with self.subTest(content=content[:40]):
                self.write(self.skills_root, 'lint/SKILL.md', content)
                self.assertEqual(self.admit_skill()['structural'], 'accepted')

    def test_plain_metadata_scalars_resolve_as_yaml_v3_does(self):
        # Tags checked against gopkg.in/yaml.v3 v3.0.1: every non-!!str plain scalar must reject.
        for raw in ('0X1F', '0B101', '0O17', '+0x1F', '+0b101', '+0o17', '+0X_1f', '0b+1', '0o-7', '+00', '08',
                    '1_000', '.0_5', '.5e1_0', '+._5', '1.', '+.INF', '.NaN', '~', 'FALSE', '<<'):
            with self.subTest(raw=raw):
                self.assertTrue(admission.yaml_non_string(raw))
                with self.assertRaisesRegex(admission.AdmissionError, 'must be strings'):
                    admission.frontmatter_value(raw, 'p')
        for raw in ('0x', '0xG', '0B2', '.0_', '._0', '.e1', '0b+-1', 'yes', 'on', 'Null0', '1a', 'lint'):
            with self.subTest(raw=raw):
                self.assertFalse(admission.yaml_non_string(raw))

    def test_skill_directory_has_one_entry_and_no_symlinks(self):
        self.write(self.skills_root, 'lint/refs/SKILL.md', '---\nname: refs\ndescription: d\n---\n')
        files = {rel: admission.hashlib.sha256((self.skills_root / 'lint' / rel).read_bytes()).hexdigest()
                 for rel in ('SKILL.md', 'refs/rules.md', 'refs/SKILL.md')}
        record = {'sha256': admission.skill_digest(files),
                  'files': [{'path': rel, 'sha256': value} for rel, value in sorted(files.items())]}
        self.assertRejected(self.admit_skill(**record), 'exactly one skill entry: refs/SKILL.md')
        os.unlink(self.skills_root / 'lint/refs/SKILL.md')
        os.symlink(self.skills_root / 'lint/refs/rules.md', self.skills_root / 'lint/refs/link.md')
        self.assertRejected(self.admit_skill(), 'symlink')
        os.unlink(self.skills_root / 'lint/refs/link.md')
        self.assertRejected(self.admit_skill(name='Lint'), 'portable skill name')
        self.assertRejected(self.admit_skill(name='l' * 65), 'portable skill name')
        self.assertEqual(self.admit_skill()['structural'], 'accepted')

    def test_child_declaration_matches_the_constrained_claude_profile(self):
        for name, children, fragment in [
                ('not a list', {'review': 1}, 'must be an array of at most 1'),
                ('two children', [CHILD, {**CHILD, 'name': 'second'}], 'must be an array of at most 1'),
                ('former shape', [{'agent_type': 'Explore', 'count': 1}], 'unsupported field'),
                ('override', [{**CHILD, 'model': 'opus'}], 'unsupported field(s): model'),
                ('tools', [{**CHILD, 'tools': ['Bash']}], 'unsupported field(s): tools'),
                ('background', [{**CHILD, 'background': True}], 'unsupported field(s): background'),
                ('bad name', [{**CHILD, 'name': 'Review'}], 'portable child name'),
                ('long name', [{**CHILD, 'name': 'r' * 49}], 'portable child name'),
                ('no resources', [{**CHILD, 'resources': []}], 'nonempty array'),
                ('entry resource', [{**CHILD, 'resources': ['SKILL.md']}], 'distinct declared skill file'),
                ('undeclared resource', [{**CHILD, 'resources': ['refs/other.md']}], 'distinct declared skill file'),
                ('duplicate resource', [{**CHILD, 'resources': ['refs/rules.md'] * 2}], 'distinct declared'),
                ('expression', [{**CHILD, 'prompt': 'Review ${{ inputs.target }}'}], 'literal')]:
            with self.subTest(name):
                result = self.admit_skill(children=children)
                self.assertRejected(result, 'invocation.skills[0].children')
                self.assertIn(fragment, result['error']['message'])
        result = self.admit_skill(children=[CHILD])
        selected = skills.session_plan(result)['occurrences'][2]
        self.assertEqual(selected['controls'], {'skill_tool': True, 'agent_tool': True,
                                                'agent_types': ['aw-child-review']})
        self.assertEqual(selected['selection']['evidence'], {'invocation': 'required', 'children': ['review']})
        self.assertEqual(selected['selection']['children'], [{
            **CHILD, 'tools': ['Read'], 'model': 'inherit', 'max-turns': 2, 'max-calls': 1, 'background': False}])
        self.assertEqual(result['admission_input']['custom']['skills']['lint-skill']['children'], [{
            **CHILD, 'type': 'aw-child-review', 'tools': ['Read'], 'model': 'inherit', 'max_turns': 2,
            'max_calls': 1, 'background': False}])

    def test_supplied_skills_stay_rejected_until_the_workflow_is_wired(self):
        invocation = {'options': {'tasks': TASKS, 'finalizers': []}, 'skills': [self.skill()]}
        result = self.admit(invocation=invocation, transport=FakeTransport(offering(*CLAUDE)),
                            deployment=self.skill_deployment())
        self.assertEqual((result['preflight'], result['structural']), ('rejected', 'accepted'))
        self.assertIn('Agent Workflow #139', result['error']['message'])
        self.assertIn('reusable', result['error']['message'])
        self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_admitted_digest_survives_source_change_and_removal(self):
        result = self.admit_skill()
        before = skills.session_plan(result)
        selected = before['occurrences'][2]
        shutil.rmtree(self.skills_root / 'lint')
        self.assertEqual(skills.session_plan(result), before)
        gateway = Gateway('codex', selected)
        self.assertEqual(skills.coverage(selected, [gateway.session()])['status'], 'covered')
        # A changed source cannot reuse the earlier admission: the stale record rejects.
        changed = SKILL_MD.replace('Use', 'Now use')
        self.write(self.skills_root, 'lint/SKILL.md', changed)
        self.write(self.skills_root, 'lint/refs/rules.md', 'Rule one.\n')
        stale = {**self.skill(), 'sha256': selected['skills'][0]['sha256'],
                 'files': [{'path': 'SKILL.md', 'sha256': selected['skills'][0]['files']['SKILL.md']},
                           {'path': 'refs/rules.md', 'sha256': selected['skills'][0]['files']['refs/rules.md']}]}
        self.assertEqual(self.admit(invocation={'options': {'tasks': TASKS, 'finalizers': []}, 'skills': [stale]},
                                    deployment=self.skill_deployment())['error']['message'],
                         'skill file digest mismatch: SKILL.md')
        # Re-admitted new bytes keep every occurrence ID; only the selected binding changes.
        after = skills.session_plan(self.admit_skill())
        self.assertEqual([item['occurrence'] for item in after['occurrences']],
                         [item['occurrence'] for item in before['occurrences']])
        self.assertEqual([a['binding'] == b['binding'] for a, b in zip(after['occurrences'], before['occurrences'])],
                         [True, True, False, True])
        # Evidence for the old bytes does not cover the new binding.
        result = skills.coverage(after['occurrences'][2], [Gateway('codex', selected).session()])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('another occurrence or binding', result['reasons'][0])
        # Old bytes consumed under the new binding are not the admitted instructions.
        current = after['occurrences'][2]
        result = skills.coverage(current, [Gateway('codex', current).session()])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('differ from the admitted bytes', result['reasons'][0])
        self.assertEqual(skills.coverage(current, [Gateway('codex', current, changed).session()])['status'],
                         'covered')


class Evidence(Fixture):
    """Coverage decisions from engine records, for both providers."""

    def setUp(self):
        super().setUp()
        plan = self.plan()
        self.plain, self.single = plan['occurrences'][0], plan['occurrences'][2]
        self.parent = self.plan([CHILD])['occurrences'][2]

    def plan(self, children=None):
        record = self.skill(**({'children': children} if children else {}))
        invocation = {'options': {'tasks': TASKS, 'finalizers': []}, 'skills': [record]}
        with patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):
            result = self.admit(invocation=invocation, transport=FakeTransport(offering(*CLAUDE)),
                                deployment=self.skill_deployment())
        return skills.session_plan(result)

    def assertCoverage(self, binding, records, status, fragment=None):
        result = skills.coverage(binding, records)
        self.assertEqual(result['status'], status, result)
        if fragment:
            self.assertTrue(any(fragment in reason for reason in result['reasons']), result['reasons'])
        return result

    def test_default_sessions_load_and_invoke_nothing(self):
        for provider, child in (('claude', 'without a declared child'), ('codex', 'delegation is disabled')):
            with self.subTest(provider):
                gateway = Gateway(provider, self.plain)
                self.assertCoverage(self.plain, [gateway.session(invoke=False)], 'covered')
                self.assertCoverage(self.plain, [gateway.session(invoke=False, loaded=[{'name': 'lint', 'sha256': 'f' * 64}])],
                                    'failed', 'unadmitted skill loaded: lint')
                self.assertCoverage(self.plain, [gateway.session()], 'failed', 'without a supplied skill')
                self.assertCoverage(self.plain, [gateway.session(invoke=False, children=[('Explore', 'completed')])],
                                    'failed', child)
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
                # Loaded-skill values that are not strings are a malformed record, not a crash.
                for value in (['x'], None):
                    loaded = [{**session['loaded'][0], 'name': value},
                              {'name': 'stray', 'sha256': '0' * 64, 'locator': 'x'}]
                    self.assertCoverage(self.single, [{**session, 'loaded': loaded}], 'incomplete',
                                        'malformed engine record')

    def test_claude_invocation_needs_its_original_successful_result(self):
        gateway = Gateway('claude', self.single)
        self.assertCoverage(self.single, [gateway.session(succeeded=None)], 'incomplete', 'no engine-observed invocation')
        self.assertCoverage(self.single, [gateway.session(succeeded=False)], 'incomplete', 'no engine-observed invocation')
        self.assertCoverage(self.single, [gateway.session(arguments='other')], 'failed', 'arguments other than')
        record = gateway.session()
        record['events'][0]['message']['content'][0]['input']['model'] = 'opus'
        self.assertCoverage(self.single, [record], 'failed', 'unsupported options')
        # A result for another call does not complete this one.
        record = gateway.session()
        record['events'][1]['message']['content'][0]['tool_use_id'] = 'other'
        self.assertCoverage(self.single, [record], 'incomplete', 'no engine-observed invocation')

    def test_a_non_object_tool_result_never_hides_a_later_successful_retry(self):
        gateway = Gateway('claude', self.single)
        record = gateway.session()
        call, result = copy.deepcopy(record['events'][0]), copy.deepcopy(record['events'][1])
        call['message']['content'][0]['id'] = 'first-call'
        result['message']['content'][0] = {'type': 'tool_result', 'tool_use_id': 'first-call'}
        for value in ('Error: skill unavailable', [{'type': 'text', 'text': 'Error'}]):
            with self.subTest(value=value):
                retried = {**record, 'events': [call, {**result, 'tool_use_result': value}, *record['events']]}
                self.assertEqual(self.assertCoverage(self.single, [retried], 'covered')['invocations'], 1)

    def test_codex_consumption_fragment_must_carry_the_admitted_bytes_and_provenance(self):
        gateway = Gateway('codex', self.single)
        # The typed `skill` input alone is a request, not evidence.
        self.assertCoverage(self.single, [gateway.session(invoke=False)], 'incomplete', 'no engine-observed invocation')
        self.assertCoverage(self.single, [gateway.session(body='Other instructions.\n')], 'failed',
                            'differ from the admitted bytes')

        def provenance(params):
            params['item']['internal_chat_message_metadata_passthrough']['content_item_metadata'] = [
                {'provenance': {'type': 'user'}}]

        def turn(params):
            params['turnId'] = 'turn-2'

        def thread(params):
            params['threadId'] = 'another-thread'

        def role(params):
            params['item']['role'] = 'assistant'

        def malformed(params):
            params['item']['content'][0]['text'] = 'Use the lint skill.'

        for name, change, fragment in [('provenance', provenance, 'mismatched identity or provenance'),
                                       ('turn', turn, 'mismatched identity or provenance'),
                                       ('thread', thread, 'mismatched identity or provenance'),
                                       ('role', role, 'mismatched identity or provenance'),
                                       ('malformed', malformed, 'fragment is malformed')]:
            with self.subTest(name):
                result = self.assertCoverage(self.single, [gateway.session(fragment=change)], 'failed', fragment)
                self.assertEqual(result['invocations'], 0)

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

    def test_same_named_skill_from_another_plugin_never_counts(self):
        admitted = self.single['skills'][0]
        for provider in skills.PROVIDERS:
            with self.subTest(provider):
                gateway = Gateway(provider, self.single)
                host = {'claude': 'host-plugin:lint', 'codex': '/home/user/.codex/skills/lint/SKILL.md'}[provider]
                self.assertCoverage(self.single, [gateway.session(locator=host)], 'failed',
                                    'other than the admitted one: ' + host)
                # The admitted digest registered outside the engine-owned plugin is not the admitted registration.
                moved = {'claude': 'aw-other:lint', 'codex': '/home/user/.codex/skills/lint'}[provider]
                result = self.assertCoverage(
                    self.single, [gateway.session(loaded=[{'name': 'lint', 'sha256': admitted['sha256'],
                                                           'locator': moved}], locator=moved)],
                    'failed', 'outside its engine-owned plugin')
                self.assertEqual(result['invocations'], 0)
        self.assertFalse(skills.locator_matches('codex', f'{CODEX_ROOT}/../skills/lint/SKILL.md', 'lint'))
        self.assertFalse(skills.locator_matches('codex', 'skills/lint/SKILL.md', 'lint'))
        self.assertTrue(skills.locator_matches('claude', 'aw-lint:lint', 'lint'))

    def test_the_declared_child_must_be_observed_exactly_once(self):
        gateway = Gateway('claude', self.parent)
        one = [('aw-child-review', 'completed')]
        result = self.assertCoverage(self.parent, [gateway.session(children=one)], 'covered')
        self.assertEqual([child['outcome'] for child in result['children']], ['completed'])
        self.assertCoverage(self.parent, [gateway.session()], 'incomplete', 'missing aw-child-review children: 0 of 1')
        self.assertCoverage(self.parent, [gateway.session(children=one * 2)], 'incomplete',
                            'duplicate aw-child-review children: 2 of 1')
        for outcome in ('failed', None):
            self.assertCoverage(self.parent, [gateway.session(children=[('aw-child-review', outcome)])],
                                'incomplete', 'no completed terminal outcome')
        for agent_type in ('general-purpose', 'Explore', 'review'):
            self.assertCoverage(self.parent, [gateway.session(children=[*one, (agent_type, 'completed')])],
                                'failed', 'child agent type not permitted: ' + agent_type)
        self.assertCoverage(self.parent, [gateway.session(children=one, nested=True)], 'failed', 'nested child')
        self.assertCoverage(self.parent, [gateway.session(invoke=False, children=one)], 'incomplete',
                            'before the admitted skill was invoked')
        for controls in ({'prompt': 'Do something else.'}, {'description': 'Other'}, {'run_in_background': True},
                         {'model': 'opus'}):
            self.assertCoverage(self.parent, [gateway.session(children=one, controls=controls)], 'failed',
                                'differs from the declared controls')
        # A skill without a declared child may not start any.
        self.assertCoverage(self.single, [Gateway('claude', self.single).session(children=one)], 'failed',
                            'without a declared child')
        # Codex disables native delegation: any spawned agent fails the occurrence.
        self.assertCoverage(self.parent, [Gateway('codex', self.parent).session(children=one)], 'failed',
                            'delegation is disabled')

    def test_resumed_and_checklist_sessions_must_still_load_the_skill(self):
        for provider, binding, children in (('claude', self.parent, [('aw-child-review', 'completed')]),
                                            ('codex', self.single, [])):
            with self.subTest(provider):
                gateway = Gateway(provider, binding)
                first = gateway.session(invoke=provider == 'codex', outcome='interrupted')
                self.assertCoverage(binding, [first], 'reconcile', 'reconcile its effects')
                self.assertFalse(skills.attempt_allowed(binding, [first]))
                # A fresh Codex process resuming a thread re-registers the root without a new fragment.
                resumed = gateway.session('resume', invoke=provider == 'claude', children=children)
                self.assertCoverage(binding, [first, resumed], 'covered')
                checklist = gateway.session('checklist', invoke=False)
                self.assertCoverage(binding, [first, resumed, checklist], 'covered')
                # Re-registration missing on the new process/session: a visible failure, never completion.
                lost = gateway.session('checklist', loaded=[], invoke=False)
                self.assertCoverage(binding, [first, resumed, lost], 'failed', 'unavailable in checklist session')
                self.assertCoverage(binding, [first, gateway.session('resume', loaded=[], children=children)],
                                    'failed', 'unavailable in resume session')
                self.assertCoverage(binding, [resumed], 'incomplete', 'start with one new session')

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


class IncomingReviewCoordinator(Fixture):
    """dk-workflows#254 on incoming review: a supplied coordinator skill and its required child pass.

    Agent Workflow #139 admits one required custom child on the Claude profile
    only, so the coordinator declares exactly one pass.
    """

    def setUp(self):
        super().setUp()
        from cf_workflows import dk_pr_review
        record = {**self.skill(), 'children': [CHILD]}
        invocation = {'options': {'reviewers': [[{'task': 'review', 'skill': 'lint-skill'}, 'simplify']]},
                      'skills': [record]}
        with patch.object(admission, 'SUPPLIED_SKILL_EXECUTION', True):  # As if the workflow were wired.
            self.result = self.admit(invocation=invocation, defaults=dk_pr_review.DEFAULTS,
                                     transport=FakeTransport(offering(*CLAUDE)), deployment=self.skill_deployment())
        self.plan = skills.session_plan(self.result)

    def test_only_the_coordinator_occurrence_loads_the_skill_and_its_findings_are_verified_separately(self):
        self.assertEqual([(item['adapter'], bool(item['skills'])) for item in self.plan['occurrences']],
                         [('read-feedback', False), ('review', True), ('simplify', False)])
        self.assertEqual(self.plan['verification']['skills'], [])
        self.assertEqual(self.plan['verification']['sources'], [self.plan['occurrences'][1]['occurrence']])

    def test_report_only_and_partial_children_never_cover_the_coordinator(self):
        coordinator = self.plan['occurrences'][1]
        gateway = Gateway('claude', coordinator)
        full = [('aw-child-review', 'completed')]
        observed = gateway.session(children=full)
        self.assertEqual(skills.coverage(coordinator, [observed])['status'], 'covered')
        # The coordinator's own successful report, claiming the pass, is not evidence.
        report = {**observed, 'source': 'report', 'claims': {'children': ['review']}}
        result = skills.coverage(coordinator, [report])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('report-only evidence is ignored', result['reasons'])
        # Aggregate session success with a missing or unfinished pass is incomplete.
        for children in ([], [('aw-child-review', 'running')]):
            result = skills.coverage(coordinator, [gateway.session(children=children)])
            self.assertEqual(result['status'], 'incomplete', children)
        self.assertEqual(gateway.calls['session'], 3)


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
