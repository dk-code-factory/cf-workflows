"""Structural admission fixtures: disposable repositories and a fail-closed fake engine transport."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cf_workflows import admission  # noqa: E402

PR = 'https://github.com/Owner/Repo/pull/7'
CAPABILITIES = {'engine': {'version': 'fixture-engine', 'capabilities': ['durable_input']},
                'providers': {'fixture-provider': ['admitted_roles', 'native_session'],
                              'claude-sdk': ['admitted_roles', 'native_session'],
                              'codex-app-server': ['admitted_roles', 'native_session']}}
SKILL_MD = '---\nname: lint\ndescription: Lint the change.\n---\nUse [rules](refs/rules.md).\n'


def skill_offering(provider='claude-sdk', *profiles):
    """Engine capabilities whose provider offers the given supplied-skill profiles (Agent Workflow #139)."""
    offered = copy.deepcopy(CAPABILITIES)
    offered['providers'][provider].extend(profiles or [admission.CLAUDE_SKILL_PROFILE if provider == 'claude-sdk'
                                                      else admission.CODEX_SKILL_PROFILE])
    return offered


class FakeTransport:
    """Records every operation; anything not configured fails without a live fallback."""

    def __init__(self, capabilities=CAPABILITIES):
        self.configured = {'capabilities': lambda: copy.deepcopy(capabilities)}
        self.calls = Counter()

    def __getattr__(self, name):
        def operation(*args, **kwargs):
            self.calls[name] += 1
            if name not in self.configured:
                raise AssertionError('unconfigured fixture operation: ' + name)
            return self.configured[name](*args, **kwargs)
        return operation


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw-admission-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.write(self.repo, 'AGENTS.md', 'Read [the guide](docs/guide.md) and [docs](docs/).\n')
        self.write(self.repo, 'docs/guide.md', 'Run tests. See [site](https://example.com).\n')
        self.config_root = self.base / 'config'
        self.write(self.config_root, 'prompts/review.md', 'Focus on [policy](policy.md).\n')
        self.write(self.config_root, 'prompts/policy.md', 'Never weaken tests.\n')
        self.roles_root = self.base / 'roles'
        self.roles = {}
        for name in admission.ADMITTED_ROLES:
            body = f'name: "{name}"\ndescription: "Fixture role"\ninstructions: "Follow the assignment."\n'
            self.write(self.roles_root, name + '.yaml', body)
            self.roles[name] = {'root': 'roles', 'path': name + '.yaml', 'sha256': sha(body)}
        self.skills_root = self.base / 'skills'
        self.write(self.skills_root, 'lint/SKILL.md', SKILL_MD)
        self.write(self.skills_root, 'lint/refs/rules.md', 'Rule one.\n')
        self.roots = {'repository': self.repo, 'configuration': self.config_root,
                      'roles': self.roles_root, 'skills': self.skills_root}
        self.request = {'pr': PR, 'instructions': '  Fix the flaky test exactly as discussed.\n',
                        'authorizations': {}}
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}

    def write(self, root, rel, body):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return sha(body)

    def resource(self, rel, root='configuration', purpose='prompt'):
        data = (self.roots[root] / rel).read_bytes()
        return {'root': root, 'path': rel, 'sha256': sha(data), 'purpose': purpose, 'owner': 'user'}

    def skill(self, **overrides):
        files = {rel: sha((self.skills_root / 'lint' / rel).read_bytes()) for rel in ('SKILL.md', 'refs/rules.md')}
        record = {'id': 'lint-skill', 'name': 'lint', 'root': 'skills', 'path': 'lint',
                  'sha256': admission.skill_digest(files),
                  'files': [{'path': path, 'sha256': digest} for path, digest in sorted(files.items())]}
        return {**record, **overrides}

    def skill_deployment(self, provider='claude-sdk'):
        """A deployment whose one runtime is a provider that admits supplied skills."""
        return {**self.deployment, 'agents': {'task': {'provider': provider, 'execution': {'mode': 'local'}}}}

    def configuration(self, scopes=(), resources=()):
        return {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                'scopes': list(scopes), 'resources': list(resources),
                'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                              'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}

    @staticmethod
    def scope(scope_id, kind, ident, **body):
        return {'id': scope_id, 'selector': {'kind': kind, 'id': ident},
                **{key: body.get(key, {} if key in ('options', 'permissions') else [])
                   for key in ('options', 'permissions', 'prompts', 'skills', 'preferences')}}

    def admit(self, configuration=None, invocation=None, transport=None, **changes):
        self.transport = transport or FakeTransport()
        arguments = {'request': self.request, 'configuration': configuration, 'invocation': invocation,
                     'deployment': self.deployment, 'roles': self.roles, 'roots': self.roots}
        arguments.update(changes)
        result = admission.admit(transport=self.transport, **copy.deepcopy(arguments))
        # Admission may only query capabilities: zero target-skill, session or publication operations.
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertLessEqual(self.transport.calls['capabilities'], 1)
        return result

    def assertRejected(self, result, fragment, probed=False):
        self.assertEqual(result['preflight'], 'rejected', result)
        self.assertEqual(result['structural'], 'rejected', result)
        self.assertIn(fragment, result['error']['path'] + ': ' + result['error']['message'])
        self.assertEqual(self.transport.calls['capabilities'], 1 if probed else 0)


class DefaultPlan(Fixture):
    def test_default_plan_is_admitted_structurally_without_semantic_stage(self):
        result = self.admit()
        self.assertEqual((result['preflight'], result['structural'], result['semantic']), ('admitted', 'accepted', None))
        self.assertIsNone(result['admission_input']['validator'])
        snapshot = result['snapshot']
        self.assertEqual(snapshot['request']['original_instructions'], self.request['instructions'])
        self.assertEqual(snapshot['request']['pr']['url'], 'https://github.com/owner/repo/pull/7')
        self.assertEqual(set(snapshot['resources']['guidance']['files']), {'AGENTS.md', 'docs/guide.md'})
        self.assertEqual([stage['kind'] for stage in snapshot['plan']['stages']],
                         ['tasks'] * 3 + ['publication', 'responses', 'tasks', 'tasks', 'reviews', 'publication', 'tasks', 'tasks',
                          'responses', 'ci'])
        self.assertTrue(all(entry['instructions'] is None for entry in snapshot['plan']['tasks']))
        self.assertFalse(any(item['effective'] or item['enabled'] for item in snapshot['permissions'].values()))
        self.assertEqual(self.transport.calls['capabilities'], 1)
        self.assertEqual(result['admission']['evidence_refs'], ['snapshot:' + result['snapshot_digest']])

    def test_snapshot_is_deterministic_and_tracks_guidance(self):
        first, second = self.admit(), self.admit()
        self.assertEqual(first['snapshot_digest'], second['snapshot_digest'])
        self.write(self.repo, 'docs/guide.md', 'Run all tests.\n')
        third = self.admit()
        self.assertNotEqual(first['admission']['resource_digest'], third['admission']['resource_digest'])
        self.assertEqual(first['admission']['capability_digest'], third['admission']['capability_digest'])

    def test_remote_writes_need_permission_and_authorization(self):
        grant = {'request_id': 'r1', 'instruction': 'Push the fix.', 'source': 'chat:1'}
        config = self.configuration([self.scope('mine', 'personal', 'me', permissions={'push': True, 'reply': True})])
        self.request['authorizations'] = {'push': grant, 'merge': grant}
        permissions = self.admit(config)['snapshot']['permissions']
        self.assertTrue(permissions['push']['effective'])
        self.assertFalse(permissions['reply']['effective'])  # enabled, not authorized
        self.assertFalse(permissions['merge']['effective'])  # authorized, not enabled

    def test_merge_grant_requires_engine_merge_guard(self):
        grant = {'request_id': 'r1', 'instruction': 'Merge when ready.', 'source': 'chat:1'}
        self.request['authorizations'] = {'merge': grant}
        config = self.configuration([self.scope('mine', 'personal', 'me', permissions={'merge': True})])
        self.assertRejected(self.admit(config), 'unsupported capability: merge_guard', probed=True)
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        self.assertEqual(self.admit(config, transport=FakeTransport(offered))['preflight'], 'admitted')

    def test_precedence_provenance_and_explicit_empty_plan(self):
        config = self.configuration([
            self.scope('pr', 'pr', 'https://github.com/owner/repo/pull/7', options={'turns': 9}),
            self.scope('repo', 'repository', 'github.com/owner/repo', options={'turns': 7, 'merge_method': 'squash'}),
            self.scope('mine', 'personal', 'me', options={'turns': 6, 'prohibitions': ['merge']}),
            self.scope('other', 'repository', 'github.com/owner/other', options={'turns': 100}),
        ])
        result = self.admit(config, {'options': {'tasks': [], 'finalizers': [],
                                                 'prohibitions': ['Do not touch vendored code.']}})
        options = result['snapshot']['options']
        self.assertEqual(options['turns'], {'value': 9, 'source': 'pr:pr'})
        self.assertEqual(options['merge_method'], {'value': 'squash', 'source': 'repository:repo'})
        self.assertEqual(options['tasks'], {'value': [], 'source': 'invocation:invocation'})
        self.assertEqual(result['snapshot']['plan'], {'tasks': [], 'stages': []})
        self.assertEqual(result['snapshot']['sources']['scopes'], ['personal:mine', 'repository:repo', 'pr:pr',
                                                                   'invocation:invocation'])
        self.assertEqual([item['source'] for item in result['admission_input']['prohibitions']],
                         ['personal:mine', 'invocation:invocation'])
        self.assertTrue(result['snapshot']['permissions']['merge']['prohibited'])


class CustomInputs(Fixture):
    def test_saved_prompt_resource_is_resolved_and_requires_semantic_stage(self):
        prompt = {'id': 'focus', 'slot': 'reviewer', 'target': 'review', 'resource': 'configuration:prompts/review.md'}
        config = self.configuration([self.scope('repo', 'repository', 'github.com/owner/repo', prompts=[prompt])],
                                    [self.resource('prompts/review.md'), self.resource('prompts/policy.md')])
        result = self.admit(config)
        self.assertEqual((result['preflight'], result['structural'], result['semantic']), ('semantic_required', 'accepted', 'blocked'))
        self.assertEqual(result['admission']['semantic'], 'blocked')
        [entry] = result['admission_input']['custom']['prompts']
        self.assertEqual(entry['source'], 'repository:repo')
        self.assertEqual(entry['resources'], ['configuration:prompts/policy.md', 'configuration:prompts/review.md'])
        validator = result['admission_input']['validator']
        self.assertEqual((validator['bootstrap'], validator['semantic'], validator['sha256']),
                         (True, None, admission.BOOTSTRAP_SHA256))

    def test_invocation_local_changes_are_identified_and_kept_apart_from_the_request(self):
        baseline = self.admit()
        invocation = {'options': {'tasks': [{'task': 'address-feedback', 'instructions': 'Only touch tests/.'}]},
                      'prompts': [{'id': 'local', 'slot': 'additional', 'text': 'Keep the diff small.'}]}
        result = self.admit(invocation=invocation)
        self.assertNotEqual(baseline['snapshot_digest'], result['snapshot_digest'])
        self.assertEqual(result['semantic'], 'blocked')
        custom = result['admission_input']['custom']
        self.assertEqual([(item['id'], item['source']) for item in custom['prompts']], [('local', 'invocation:invocation')])
        self.assertEqual(custom['instructions'], [{'task': 'task_1', 'adapter': 'address-feedback',
                                                   'text': 'Only touch tests/.',
                                                   'path': 'invocation.options.tasks[0].instructions'}])
        self.assertEqual(result['snapshot']['request']['original_instructions'], self.request['instructions'])

    def test_prompt_for_unselected_adapter_is_resolved_but_not_applicable(self):
        invocation = {'options': {'tasks': ['read-feedback'], 'finalizers': []},
                      'prompts': [{'id': 'x', 'slot': 'task', 'target': 'describe-pr', 'text': 'Be brief.'}]}
        result = self.admit(invocation=invocation)
        self.assertEqual(result['preflight'], 'admitted')
        self.assertFalse(result['snapshot']['prompts'][0]['applicable'])

    def test_custom_adapter_and_preference_require_semantic_stage(self):
        for invocation in ({'options': {'tasks': ['describe-pr'], 'finalizers': [],
                                        'adapters': {'describe-pr': {'assignment': 'Write a haiku.'}}}},
                           {'preferences': [{'id': 'tone', 'text': 'Prefer terse replies.'}]}):
            with self.subTest(invocation=invocation):
                result = self.admit(invocation=invocation)
                self.assertEqual((result['structural'], result['semantic'], result['preflight']),
                                 ('accepted', 'blocked', 'semantic_required'))

    def test_nested_selection_with_supplied_skill(self):
        tasks = [['read-feedback', 'assess-scope'],
                 {'stage': 'reviews', 'reviewers': [[{'task': 'review', 'skill': 'lint-skill'}, 'review']]}]
        invocation = {'options': {'tasks': tasks, 'finalizers': []}, 'skills': [self.skill()]}
        self.assertRejected(self.admit(invocation=invocation), 'unsupported supplied-skill provider')
        self.assertRejected(self.admit(invocation=invocation, deployment=self.skill_deployment()),
                            'unsupported capability: ' + admission.CLAUDE_SKILL_PROFILE, probed=True)
        result = self.admit(invocation=invocation, transport=FakeTransport(skill_offering()),
                            deployment=self.skill_deployment())
        self.assertEqual((result['structural'], result['semantic']), ('accepted', 'blocked'))
        plan = result['snapshot']['plan']
        self.assertEqual(plan['stages'][0]['groups'], [['task_1', 'task_2']])
        self.assertEqual(plan['stages'][1]['groups'], [['task_3', 'task_4']])
        self.assertEqual(plan['tasks'][2]['skill'], 'lint-skill')
        skill = result['admission_input']['custom']['skills']['lint-skill']
        self.assertEqual(set(skill['files']), {'SKILL.md', 'refs/rules.md'})
        self.assertEqual(skill['sha256'], self.skill()['sha256'])

    def test_custom_inputs_carry_exact_configuration_paths(self):
        config = self.configuration([self.scope('repo', 'repository', 'github.com/owner/repo', options={
            'reviewers': [{'task': 'review', 'instructions': 'Check docs.'}], 'requirements': ['Keep CI green.']})])
        tasks = [['read-feedback', {'task': 'assess-scope', 'instructions': 'Only src/.'}],
                 {'stage': 'reviews', 'reviewers': [[{'task': 'review', 'instructions': 'Check tests.'}]]},
                 {'stage': 'reviews'}]
        data = self.admit(config, {'options': {'tasks': tasks, 'finalizers': []}})['admission_input']
        self.assertEqual([item['path'] for item in data['custom']['instructions']],
                         ['invocation.options.tasks[0][1].instructions',
                          'invocation.options.tasks[1].reviewers[0][0].instructions',
                          'configuration.scopes[0].options.reviewers[0].instructions'])
        self.assertEqual([item['path'] for item in data['requirements']], ['configuration.scopes[0].options.requirements[0]'])

    def test_unreferenced_supplied_skill_is_resolved_but_not_applicable(self):
        result = self.admit(invocation={'skills': [self.skill()]})
        self.assertEqual(result['preflight'], 'admitted')
        self.assertIn('lint-skill', result['snapshot']['resources']['skills'])


class Rejections(Fixture):
    def cases(self):
        resource = self.resource('prompts/policy.md')
        prompt = {'id': 'p', 'slot': 'additional', 'resource': 'configuration:prompts/policy.md'}
        mine = lambda **body: self.configuration([self.scope('mine', 'personal', 'me', **body)])
        local = lambda **options: {'options': options}
        return [
            ('missing resource', {'configuration': self.configuration(resources=[{**resource, 'path': 'prompts/gone.md'}])}, 'missing resource'),
            ('digest mismatch', {'configuration': self.configuration(resources=[{**resource, 'sha256': 'b' * 64}])}, 'digest mismatch'),
            ('escaping path', {'configuration': self.configuration(resources=[{**resource, 'path': '../repo/AGENTS.md'}])}, 'normalized relative'),
            ('absolute path', {'configuration': self.configuration(resources=[{**resource, 'path': '/etc/passwd'}])}, 'normalized relative'),
            ('incomplete closure', {'configuration': self.configuration(resources=[self.resource('prompts/review.md')])}, 'incomplete resource closure'),
            ('undeclared prompt resource', {'configuration': mine(prompts=[prompt])}, 'undeclared resource'),
            ('unknown root', {'configuration': self.configuration(resources=[{**resource, 'root': 'elsewhere'}])}, 'unknown resource root'),
            ('legacy selector', {'invocation': local(tasks=['dk-workflows:dk-pr-fix'])}, 'qualified/legacy'),
            ('task options', {'invocation': local(tasks=[{'task': 'review', 'options': {'x': 1}}])}, 'task/required/instructions/skill'),
            ('stage in parallel group', {'invocation': local(tasks=[['review', {'stage': 'ci'}]])}, 'parallel group'),
            ('unsupported stage', {'invocation': local(tasks=[{'stage': 'deploy'}])}, 'unsupported managed stage'),
            ('parallel writers', {'invocation': local(tasks=[['address-feedback', 'sync-guidance']])}, 'parallel writing'),
            ('unknown adapter', {'invocation': local(tasks=['frobnicate'])}, 'unavailable selected adapter'),
            ('reviewer must review', {'invocation': local(reviewers=['describe-pr'])}, 'read-only review adapters'),
            ('empty reviewers', {'invocation': local(reviewers=[])}, 'explicit reviewers'),
            ('cycle hooks', {'invocation': local(before_cycle=['review'])}, 'cycle hooks'),
            ('unknown option', {'invocation': local(magic=True)}, 'unsupported option'),
            ('null option', {'invocation': local(tasks=None)}, 'null has no declared meaning'),
            ('string setting', {'invocation': local(settings={'tone': 'casual'})}, 'boolean/integer'),
            ('limit bound', {'invocation': local(limits={'review_cycles': 99})}, 'review_cycles'),
            ('writing reviewer', {'invocation': local(adapters={'review': {'assignment': 'x', 'kind': 'review', 'writes': True}})}, 'reviewers cannot write'),
            ('validator adapter', {'invocation': local(adapters={'v': {'assignment': 'x', 'definition': 'configuration-validator'}})}, 'reserved'),
            ('unknown role', {'invocation': local(adapters={'v': {'assignment': 'x', 'definition': 'root'}})}, 'admitted catalog'),
            ('undeclared skill ref', {'invocation': local(tasks=[{'task': 'review', 'skill': 'nope'}])}, 'undeclared supplied skill'),
            ('unknown prompt target', {'invocation': {'prompts': [{'id': 'p', 'slot': 'task', 'target': 'nope', 'text': 'x'}]}}, 'unknown adapter'),
            ('validator prompt target', {'invocation': {'prompts': [{'id': 'p', 'slot': 'task', 'target': 'configuration-validator', 'text': 'x'}]}}, 'configuration validator'),
            ('inline local link', {'invocation': {'prompts': [{'id': 'p', 'slot': 'task', 'text': 'See [x](../secret.md)'}]}}, 'declare a resource'),
            ('prompt slot', {'invocation': {'prompts': [{'id': 'p', 'slot': 'mystery', 'text': 'x'}]}}, 'unsupported prompt slot'),
            ('duplicate prompt id', {'invocation': {'prompts': [{'id': 'p', 'slot': 'task', 'text': 'x'}] * 2}}, 'duplicate prompts id'),
            ('unknown permission', {'configuration': mine(permissions={'deploy': True})}, 'unknown remote-write action'),
            ('path selector', {'configuration': self.configuration([{**self.scope('s', 'repository', 'github.com/owner/repo'),
                                                                      'selector': {'kind': 'repository', 'id': 'x', 'include': ['src/']}}])}, 'path selectors'),
            ('saved invocation scope', {'configuration': self.configuration([self.scope('s', 'invocation', 'x')])}, 'saved scopes'),
            ('duplicate scope', {'configuration': self.configuration([self.scope('s', 'personal', 'a'), self.scope('s', 'personal', 'b')])}, 'duplicate scope'),
            ('same precedence conflict', {'configuration': self.configuration([
                self.scope('a', 'personal', 'a', options={'turns': 6}), self.scope('b', 'personal', 'b', options={'turns': 7})])}, 'same precedence'),
            ('schema version', {'configuration': {**self.configuration(), 'schema_version': 2}}, 'unsupported schema version'),
            ('unknown config field', {'configuration': {**self.configuration(), 'extra': 1}}, 'unsupported field'),
            ('wrong scope type', {'configuration': self.configuration(['not-a-scope'])}, 'must be an object'),
            ('wrong types', {'configuration': self.configuration(resources=[{**resource, 'root': ['x']}])}, 'malformed input'),
            ('request url', {'request': {**self.request, 'pr': 'http://github.com/o/r/pull/1'}}, 'canonical HTTPS'),
            ('request extra', {'request': {**self.request, 'tasks': []}}, 'unsupported field'),
            ('empty instruction', {'request': {**self.request, 'instructions': ' '}}, 'nonempty'),
            ('unknown authorization', {'request': {**self.request, 'authorizations': {'deploy': {}}}}, 'unknown remote-write action'),
            ('placeholder image', {'deployment': {**self.deployment, 'execution': {'mode': 'container', 'image': 'x/y@sha256:' + '0' * 64}}}, 'real sha256'),
            ('unpinned image', {'deployment': {**self.deployment, 'execution': {'mode': 'container', 'image': 'x/y:latest'}}}, 'real sha256'),
            ('api authentication', {'deployment': {**self.deployment, 'agents': {'a': {'provider': 'p', 'authentication': {'mode': 'api-key'}}}}}, 'native provider authentication'),
            ('role digest', {'roles': {**self.roles, 'leaf': {**self.roles['leaf'], 'sha256': 'c' * 64}}}, 'role definition digest'),
            ('missing role', {'roles': {k: v for k, v in self.roles.items() if k != 'audience'}}, 'exactly'),
            ('validator role supplied', {'roles': {**self.roles, 'configuration-validator': self.roles['leaf']}}, 'bundled'),
            ('host-installed skill', {'invocation': {'skills': [{**self.skill(), 'host': 'claude'}]}}, 'host-installed'),
            ('skill digest', {'invocation': {'skills': [self.skill(sha256='d' * 64)]}}, 'skill content digest'),
            ('skill missing file', {'invocation': {'skills': [self.skill(files=self.skill()['files'][:1])]}}, 'declared files differ'),
            ('skill escaping dir', {'invocation': {'skills': [self.skill(path='../config')]}}, 'normalized relative'),
            ('skill missing dir', {'invocation': {'skills': [self.skill(path='absent')]}}, 'missing skill directory'),
            ('skill entry', {'invocation': {'skills': [self.skill(entry='README.md')]}}, 'skill entry must be SKILL.md'),
        ]

    def test_invalid_inputs_fail_before_any_engine_operation(self):
        for name, changes, fragment in self.cases():
            with self.subTest(name):
                self.assertRejected(self.admit(**changes), fragment)

    def test_prohibition_contradicting_authorization_rejects(self):
        self.request['authorizations'] = {'push': {'request_id': 'r', 'instruction': 'Push.', 'source': 's'}}
        config = self.configuration([self.scope('mine', 'personal', 'me', options={'prohibitions': ['push']})])
        self.assertRejected(self.admit(config), 'contradicts a configured prohibition')

    def test_symlinks_and_escaping_links_reject(self):
        outside = self.write(self.base, 'outside.md', 'secret\n')
        os.symlink(self.base / 'outside.md', self.config_root / 'prompts/link.md')
        record = {'root': 'configuration', 'path': 'prompts/link.md', 'sha256': outside, 'purpose': 'p', 'owner': 'u'}
        self.assertRejected(self.admit(self.configuration(resources=[record])), 'symlink')
        os.unlink(self.config_root / 'prompts/link.md')
        self.write(self.config_root, 'prompts/escape.md', 'See [x](../../outside.md).\n')
        self.assertRejected(self.admit(self.configuration(resources=[self.resource('prompts/escape.md')])),
                            'links outside its root')

    def test_skill_closure_symlink_and_escape_reject(self):
        self.write(self.skills_root, 'lint/SKILL.md', 'Use [x](../../config/prompts/policy.md).\n')
        self.assertRejected(self.admit(invocation={'skills': [self.skill()]}), 'links outside its root')
        self.write(self.skills_root, 'lint/SKILL.md', 'Use [x](refs/missing.md).\n')
        self.assertRejected(self.admit(invocation={'skills': [self.skill()]}), 'incomplete resource closure')
        self.write(self.skills_root, 'lint/SKILL.md', 'Plain.\n')
        os.symlink('/etc/hostname', self.skills_root / 'lint/refs/host')
        self.assertRejected(self.admit(invocation={'skills': [self.skill()]}), 'symlink')

    def test_guidance_reference_problems_reject(self):
        self.write(self.repo, 'docs/guide.md', 'See [gone](gone.md).\n')
        self.assertRejected(self.admit(), 'missing resource docs/gone.md')
        self.write(self.repo, 'docs/guide.md', 'See [up](../../outside.md).\n')
        self.assertRejected(self.admit(), 'links outside its root')

    def test_unavailable_provider_rejects_after_one_probe(self):
        offered = {'engine': {'version': 'e'}, 'providers': {}}
        self.assertRejected(self.admit(transport=FakeTransport(offered)), 'not available from the engine', probed=True)
        self.assertRejected(self.admit(transport=FakeTransport({'engine': {}, 'providers': {}})), 'missing field', probed=True)

    def test_unconfigured_fixture_operation_fails_closed(self):
        transport = FakeTransport()
        with self.assertRaisesRegex(AssertionError, 'unconfigured fixture operation: publish'):
            transport.publish({'action': 'merge'})
        self.assertEqual(transport.calls, Counter({'publish': 1}))


class ReviewRegressions(Fixture):
    def test_overridden_and_non_matching_scope_values_are_still_validated(self):
        override = {'options': {'tasks': ['read-feedback']}}
        for name, scope, fragment in [
            ('legacy selector', self.scope('mine', 'personal', 'me', options={'tasks': ['dk-workflows:dk-pr-fix']}),
             'qualified/legacy'),
            ('task options', self.scope('mine', 'personal', 'me', options={'finalizers': [{'task': 'review', 'options': {}}]}),
             'task/required/instructions/skill'),
            ('nested reviewers', self.scope('mine', 'personal', 'me', options={'tasks': [{'stage': 'reviews', 'reviewers': [['x:y']]}]}),
             'qualified/legacy'),
            ('unselected adapter resource', self.scope('mine', 'personal', 'me', options={'adapters': {'foo': {
                'assignment': 'x', 'resources': ['nope:missing.md']}}}), 'undeclared resource'),
            ('other repository prompt', self.scope('o', 'repository', 'github.com/owner/other', prompts=[{'garbage': 1}]),
             'unsupported field(s): garbage'),
            ('other repository skill', self.scope('o', 'repository', 'github.com/owner/other', skills=[{'host': 'claude'}]),
             'host-installed'),
            ('other repository preference', self.scope('o', 'repository', 'github.com/owner/other',
                                                       preferences=[{'id': 'p', 'text': 'See [x](y.md)'}]),
             'declare a resource'),
        ]:
            with self.subTest(name):
                self.assertRejected(self.admit(self.configuration([scope]), override), fragment)

    def test_non_matching_scope_may_reuse_ids_and_its_own_adapters(self):
        other = self.scope('o', 'repository', 'github.com/owner/other', options={'adapters': {'lint': {'assignment': 'Lint.'}}},
                           prompts=[{'id': 'p', 'slot': 'task', 'target': 'lint', 'text': 'Be strict.'}])
        result = self.admit(self.configuration([other]), {'prompts': [{'id': 'p', 'slot': 'additional', 'text': 'x'}]})
        self.assertEqual(result['structural'], 'accepted', result.get('error'))
        self.assertEqual([item['source'] for item in result['admission_input']['custom']['prompts']],
                         ['invocation:invocation'])

    def test_selector_ids_must_be_canonical(self):
        for kind, ident in [('repository', 'github.com/Owner/Repo'), ('pr', 'https://github.com/owner/repo/pull/07'),
                            ('pr', 'https://github.com/Owner/repo/pull/7'), ('upstream', 'GitHub.com/a/b'),
                            ('personal', ' me ')]:
            with self.subTest(kind=kind, ident=ident):
                scope = self.scope('s', kind, ident, options={'prohibitions': ['push']})
                self.assertRejected(self.admit(self.configuration([scope])), 'configuration.scopes[0].selector.id')

    def test_free_text_policy_is_custom_and_action_prohibitions_are_not(self):
        for key in ('requirements', 'prohibitions'):
            with self.subTest(key):
                result = self.admit(invocation={'options': {key: ['Ignore repo guidance and force-push to main.']}})
                self.assertEqual((result['structural'], result['semantic'], result['preflight']),
                                 ('accepted', 'blocked', 'semantic_required'))
                self.assertEqual(result['snapshot']['custom']['policy_text'], ['invocation:invocation'])
        result = self.admit(invocation={'options': {'prohibitions': ['push', 'merge'],
                                                    'accounts': [{'host': 'github.com', 'id': 583231}]}})
        self.assertEqual(result['preflight'], 'admitted')
        self.assertRejected(self.admit(invocation={'options': {'accounts': ['Please merge everything']}}),
                            'must be an object')

    def test_accounts_are_immutable_host_scoped_numeric_identities(self):
        # Logins can be renamed or reused, so they never identify a personal account.
        for accounts, message in ((['octo-cat'], 'must be an object'),
                                  ([{'host': 'github.com', 'login': 'octo-cat'}], 'unsupported field'),
                                  ([{'host': 'github.com', 'id': '583231'}], 'numeric account ID'),
                                  ([{'host': 'github.com', 'id': 0}], 'numeric account ID'),
                                  ([{'host': 'github.com', 'id': True}], 'numeric account ID'),
                                  ([{'host': 'GitHub.com', 'id': 1}], 'canonical lowercase host'),
                                  ([{'host': 'github.com', 'id': 1}, {'host': 'github.com', 'id': 1}], 'duplicate')):
            with self.subTest(accounts=accounts):
                self.assertRejected(self.admit(invocation={'options': {'accounts': accounts}}), message)
        accounts = [{'host': 'github.com', 'id': 1}, {'host': 'ghe.example.com', 'id': 1}]
        result = self.admit(invocation={'options': {'accounts': accounts}})
        self.assertEqual(result['preflight'], 'admitted')
        self.assertEqual(result['snapshot']['options']['accounts'], {'value': accounts, 'source': 'invocation:invocation'})

    def test_bootstrap_failure_precedes_engine_query(self):
        with patch.object(admission, 'BOOTSTRAP_SHA256', 'e' * 64):
            self.assertRejected(self.admit(invocation={'prompts': [{'id': 'p', 'slot': 'additional', 'text': 'x'}]}),
                                'differs from its pin')

    def test_markdown_reference_forms(self):
        self.write(self.repo, 'AGENTS.md', '\n'.join([
            'See [ref][r], [angle](<docs/a b.md>), [root](/docs/root.md "title") and `[span](nope.md)`.',
            '```', '[fenced](nope.md)', '```', '[r]: docs/ref.md', '']))
        for name in ('docs/a b.md', 'docs/root.md', 'docs/ref.md'):
            self.write(self.repo, name, 'x\n')
        files = self.admit()['snapshot']['resources']['guidance']['files']
        self.assertEqual(set(files), {'AGENTS.md', 'docs/a b.md', 'docs/root.md', 'docs/ref.md'})
        self.write(self.repo, 'AGENTS.md', '[r]: docs/missing.md\n')
        self.assertRejected(self.admit(), 'missing resource docs/missing.md')
        self.write(self.config_root, 'prompts/review.md', 'Use [p][p].\n\n[p]: <policy.md>\n')
        config = self.configuration(resources=[self.resource('prompts/review.md')])
        self.assertRejected(self.admit(config), 'incomplete resource closure')

    def test_root_names_cannot_collide_with_readable_key_syntax(self):
        for name in ('skill', 'a:b', ''):
            with self.subTest(name=name):
                result = self.admit(self.configuration(resources=[self.resource('prompts/policy.md')]),
                                    roots={**self.roots, name: self.config_root})
                self.assertRejected(result, 'reserved name skill')

    def test_escaped_or_unbalanced_backticks_never_hide_a_link(self):
        for body in ('\\` [doc](secret.md) \\`', '` [doc](secret.md) ``', '`` [doc](secret.md) `',
                     '``` [doc](secret.md) ``'):
            with self.subTest(body=body):
                self.assertEqual(admission.link_targets(body), ['secret.md'])
        # Real spans, including ones holding shorter backtick runs, still hide their text.
        self.assertEqual(admission.link_targets('`[a](a.md)` `` b`[c](c.md)`d `` [e](e.md)'), ['e.md'])

    def test_links_across_line_ends_and_after_a_list_item_fence_are_found(self):
        for body in ('[doc](\nsecret.md)', '[doc](\n  <secret.md>\n  "Title")', '[doc]:\n  secret.md',
                     '- a\n  ```\n  code\ntext [doc](secret.md)\n',
                     # Without a list item the indented fence runs to its closer.
                     '  ```\ncode\n  ```\n[doc](secret.md)\n'):
            with self.subTest(body=body):
                self.assertIn('secret.md', admission.link_targets(body))
        self.assertEqual(admission.link_targets('```\n[a](a.md)\n```\n[b](\nb.md)\n'), ['b.md'])

    def test_nested_lists_footnotes_and_parentheses(self):
        self.assertEqual(admission.link_targets('- Rules:\n    - see [policy](policy.md)\n\n  continued [c](c.md)\n'),
                         ['policy.md', 'c.md'])
        self.assertEqual(admission.link_targets('Note[^1].\n\n[^1]: See the hidden notes.\n'), [])
        self.assertEqual(admission.link_targets('[x](a(b).md "t") [y](<z w.md>)'), ['a(b).md', 'z w.md'])
        # A backtick line with a backtick in its info string is a span, and a closer takes no info string.
        self.assertEqual(admission.link_targets('```code``` see [x](x.md)\n[y](y.md)\n'), ['x.md', 'y.md'])
        self.assertEqual(admission.link_targets('```\n[a](a.md)\n```python\n```\n[b](b.md)\n'), ['b.md'])
        self.assertEqual(admission.link_targets('~~~\n[a](a.md)\n~~~ \n[b](b.md)\n'), ['b.md'])
        # Indented code is scanned too: a link there can only reject, never drop out of the closure.
        self.write(self.repo, 'AGENTS.md', 'Intro.\n\n    [indented](missing.md)\n')
        self.assertRejected(self.admit(), 'missing resource missing.md')
        self.write(self.repo, 'AGENTS.md', '- Rules:\n    - see [x](docs/a(b).md)\n\n[^1]: Hidden notes.\n')
        self.write(self.repo, 'docs/a(b).md', 'x\n')
        self.assertEqual(set(self.admit()['snapshot']['resources']['guidance']['files']), {'AGENTS.md', 'docs/a(b).md'})

    def test_engine_transport_failure_is_not_a_structural_rejection(self):
        transport = FakeTransport()
        transport.configured['capabilities'] = lambda: (_ for _ in ()).throw(ConnectionError('engine down'))
        result = self.admit(transport=transport)
        self.assertEqual((result['preflight'], result['structural']), ('blocked', 'accepted'))
        self.assertEqual(result['error'], {'path': 'capabilities',
                                           'message': 'engine capability query failed: ConnectionError'})
        self.assertNotIn('snapshot', result)
        self.assertEqual(transport.calls['capabilities'], 1)

    def test_guidance_symlinks_inside_repository_are_resolved(self):
        os.symlink('AGENTS.md', self.repo / 'CLAUDE.md')
        guidance = self.admit()['snapshot']['resources']['guidance']
        self.assertEqual(guidance['symlinks'], {'CLAUDE.md': 'AGENTS.md'})
        self.assertNotIn('CLAUDE.md', guidance['files'])
        os.unlink(self.repo / 'CLAUDE.md')
        self.write(self.base, 'outside.md', 'x\n')
        os.symlink(self.base / 'outside.md', self.repo / 'CLAUDE.md')
        self.assertRejected(self.admit(), 'symlink escapes its root')

    def test_invalid_text_and_numbers_reject_structurally(self):
        self.assertRejected(self.admit(request={**self.request, 'instructions': 'bad \ud800'}), 'valid Unicode')
        config = self.configuration()
        config['admission']['evidence_refs'] = [float('nan')]
        self.assertRejected(self.admit(config), 'evidence_refs[0]')
        config['admission'] = {**self.configuration()['admission'], 'context_digest': 'nope'}
        self.assertRejected(self.admit(config), 'context_digest')

    def test_prompt_text_is_the_admitted_resource_bytes(self):
        prompt = {'id': 'p', 'slot': 'additional', 'resource': 'configuration:prompts/policy.md'}
        config = self.configuration([self.scope('mine', 'personal', 'me', prompts=[prompt])],
                                    [self.resource('prompts/policy.md')])
        original = admission.read_file
        reads = Counter()

        def counting(root, rel, path, limit=admission.MAX_FILE):
            reads[rel] += 1
            content = original(root, rel, path, limit)
            if reads[rel] > 1:
                content = b'swapped after admission\n'
            return content
        with patch.object(admission, 'read_file', counting):
            result = self.admit(config)
        [entry] = result['snapshot']['prompts']
        self.assertEqual(reads['prompts/policy.md'], 1)
        self.assertEqual(entry['sha256'], self.resource('prompts/policy.md')['sha256'])


class Bootstrap(unittest.TestCase):
    def test_bundled_validator_is_structural_only(self):
        result = admission.bootstrap()
        self.assertEqual((result['structural'], result['semantic'], result['bootstrap']), ('accepted', None, True))
        self.assertEqual((result['prompts'], result['skills']), ([], []))

    def test_changed_validator_definition_rejects_instead_of_recursing(self):
        with patch.object(admission, 'BOOTSTRAP_SHA256', 'e' * 64):
            with self.assertRaisesRegex(admission.AdmissionError, 'differs from its pin'):
                admission.bootstrap()

    def test_contract_json_rejects_duplicates_and_nonfinite_numbers(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(admission.AdmissionError):
                admission.load_json(raw)
        self.assertEqual(admission.load_json(json.dumps({'a': 1})), {'a': 1})
        with self.assertRaisesRegex(admission.AdmissionError, 'depth'):
            admission.load_json('[' * 100000 + ']' * 100000)


if __name__ == '__main__':
    unittest.main()
