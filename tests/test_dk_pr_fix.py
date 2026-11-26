"""Installed dk-pr-fix: closure, provenance, preflight and a relocated read-only run.

Disposable repositories, a fail-closed fake engine transport and a fail-closed
fake GitHub transport. No provider, engine or network is used; these results do
not certify any engine, provider or host.
"""
import ast
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from cf_workflows import admission, dk_pr_fix, validator_binding  # noqa: E402
from test_semantic import Engine, verdict  # noqa: E402

sys.dont_write_bytecode = True  # Importing the helpers must not add caches to the installed closure.

INSTALL = dk_pr_fix.INSTALL
HELPERS = INSTALL / 'workflows' / 'helpers'
DRIVER = ROOT / 'tests' / 'fixtures' / 'dk_pr_fix_driver.py'
PROVENANCE = json.loads((ROOT / 'baselines' / 'dk-pr-fix-closure.json').read_text())
PR = 'https://github.com/Owner/Repo/pull/7'
CAPABILITIES = {'engine': {'version': 'fixture-engine', 'capabilities': ['durable_input']},
                'providers': {'fixture-provider': ['admitted_roles', 'native_session']}}
GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TEMPLATE_DIR': ''}


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


def git(root, *args):
    return subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, *args], cwd=root, check=True,
                          capture_output=True, env={**os.environ, **GIT_ENV}).stdout.decode().strip()


def clean_install(destination):
    """A copy of the source installation without any developer bytecode caches."""
    shutil.copytree(INSTALL, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return destination


def workflow_files():
    return {path.name: path.read_text() for path in sorted((INSTALL / 'workflows').glob('*.yml'))}


class InstalledClosure(unittest.TestCase):
    def test_installation_matches_its_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            install = clean_install(Path(temp) / 'install')
            dk_pr_fix.verify_installation(install)
            manifest = json.loads((install / dk_pr_fix.CLOSURE).read_text())
            self.assertEqual(manifest['files'], dk_pr_fix.closure_digests(install))

    def test_bytecode_caches_are_never_part_of_a_verified_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            install = clean_install(Path(temp) / 'install')
            prfix = install / 'workflows' / 'helpers' / 'prfix'
            for planted in (prfix / '__pycache__' / 'ci.cpython-312.pyc', prfix / 'ci.pyc'):
                with self.subTest(planted=planted.name):
                    planted.parent.mkdir(exist_ok=True)
                    planted.write_bytes(b'planted bytecode')
                    with self.assertRaisesRegex(dk_pr_fix.InstallationError, 'bytecode'):
                        dk_pr_fix.verify_installation(install)
                    planted.unlink()
            (prfix / '__pycache__').rmdir()
            (prfix / '__pycache__').mkdir()
            with self.assertRaisesRegex(dk_pr_fix.InstallationError, 'bytecode'):
                dk_pr_fix.verify_installation(install)

    def test_reusable_references_resolve_inside_the_installation(self):
        files = workflow_files()
        # Every descriptor skill row names an entry: main.yml (dk-pr-fix), review.yml
        # (docs/review.md), feedback-*.yml (docs/feedback-skills.md), publish.yml and
        # review-post.yml (docs/publication-skills.md), plain-english.yml and
        # asd-ste100.yml (docs/prose.md) and decisions.yml (docs/decisions.md).
        entries = re.findall(r'^    workflow: workflows/(\S+)$', (INSTALL / 'plugin.yaml').read_text(), re.M)
        self.assertEqual(entries, ['main.yml', 'review.yml', 'feedback-read.yml', 'feedback-address.yml',
                                   'feedback-reply.yml', 'publish.yml', 'review-post.yml',
                                   'plain-english.yml', 'asd-ste100.yml', 'decisions.yml'])
        # Preflight runs the semantic assessment workflow itself, before any entry dispatches.
        reachable, pending = set(), [*entries, validator_binding.ASSESSMENT.split('/', 1)[1]]
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            body = files[name]
            self.assertNotRegex(body, r'uses:\s*examples/')
            for target in re.findall(r'^\s*uses:\s*(\S+)\s*$', body, re.M):
                if target in ('agent/run', 'input/wait'):
                    continue
                self.assertRegex(target, r'^workflows/[a-z-]+\.yml$')
                self.assertTrue((INSTALL / target).is_file(), target)
                pending.append(target.split('/', 1)[1])
            for item in re.findall(r'^\s*- (helpers/|agents/)\s*$', body, re.M):
                self.assertTrue((INSTALL / 'workflows' / item).is_dir(), item)
        self.assertEqual(reachable, set(files), 'every installed workflow is reachable from an entry')
        native = files['native.yml']
        for role in admission.ADMITTED_ROLES:
            self.assertIn(f'{role}: agents/{role}.yaml', native)
        self.assertNotIn('.toml', ''.join(files.values()))

    def test_roles_are_canonical_yaml_for_admission_and_helpers(self):
        sys.path.insert(0, str(HELPERS))
        try:
            from prfix.prepare import agent_definition
        finally:
            sys.path.remove(str(HELPERS))
        catalog = dk_pr_fix.role_catalog()
        self.assertEqual(set(catalog), set(admission.ADMITTED_ROLES))
        for role, record in catalog.items():
            data = (INSTALL / record['path']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), record['sha256'])
            admission.role_definition(data, role)
            self.assertTrue(agent_definition(data)['instructions'].strip())
        for invalid in (b'name = "toml"\n', b'name: "a"\ndescription: "b"\n', b'name: plain\n',
                        b'name: "a"\ndescription: "b"\ninstructions: "c"\nname: "d"\n',
                        b'name: "a"\ndescription: "b"\ninstructions: "c"\nhooks: "x"\n',
                        b'name: "a"\ndescription: "b"\ninstructions: "\\u0063"\n',
                        b'name: "a"\r\ndescription: "b"\r\ninstructions: "c"\r\n'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(admission.AdmissionError):
                    admission.role_definition(invalid, 'role')
                with self.assertRaises(ValueError):
                    agent_definition(invalid)

    def test_provenance_covers_the_extracted_closure(self):
        self.assertEqual(PROVENANCE['commit'], json.loads((ROOT / 'baselines/sources.json').read_text())
                         ['sources'][1]['commit'])
        local = {entry['path']: entry for entry in PROVENANCE['files']}
        installed = {'cf_workflows/workflows/dk-pr-fix/' + path for path in dk_pr_fix.closure_digests()}
        ported = {'tests/dk_pr_fix/' + path.name for path in (ROOT / 'tests/dk_pr_fix').glob('*.py')
                  if path.name != '__init__.py'}
        authored = {entry['path'] for entry in PROVENANCE['authored']}
        self.assertFalse(authored & set(local))
        self.assertEqual(set(local) | authored, installed | ported)
        for path, entry in local.items():
            digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            self.assertEqual(entry['change'] == 'verbatim', digest == entry['upstream_sha256'], path)
            self.assertEqual(entry['change'] == 'converted', path.endswith('.yaml'), path)
        self.assertEqual((INSTALL / 'LICENSE').read_bytes(), (ROOT / PROVENANCE['license']).read_bytes())

    def test_helpers_launch_no_provider_or_controller(self):
        banned = {'codex', 'claude', 'agent-workflow', 'docker', 'podman', 'npx', 'node'}
        for path in sorted(HELPERS.rglob('*.py')):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value, banned, path)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] + [getattr(node, 'module', None) or '']
                    self.assertFalse({'threading', 'multiprocessing', 'asyncio', 'socket'} & set(names), path)
        for name, body in workflow_files().items():
            # Only engine-owned agent/run steps start sessions; every helper step runs one action
            # operation, apart from the bounded polling delays. CI polls at the helper's validated
            # configured interval (prfix/ci.py observe), never at a plan-supplied string.
            for argv in re.findall(r'argv:\s*\n((?:\s*- .*\n)+)', body):
                argv = [line.strip()[2:] for line in argv.strip().splitlines()]
                self.assertTrue(argv[:3] == ['python3', '-B', '/actions/helpers/action.py'] and len(argv) == 4
                                or argv == ['sleep', "'10'"]
                                or name == 'ci-poll.yml'
                                and argv == ['sleep', '${{ steps.ci.outputs.poll_interval }}'], name)
            for argv in re.findall(r'argv: \[(.*)\]', body):
                self.assertTrue(argv.startswith('python3, -B, /actions/helpers/action.py'), name)
            # Every helper step sees the mounted state namespace.
            self.assertEqual(body.count('CF_WORKFLOWS_STATE: /cf-workflows-state'), body.count('/actions/helpers/action.py'), name)


class PreflightFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw dk pr fix ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'AGENTS.md').write_text('Run tests.\n')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        git(self.repo, 'config', 'user.name', 'Fixture')
        git(self.repo, 'config', 'commit.gpgsign', 'false')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'Guidance')
        self.home = self.base / 'state home'
        self.clean = clean_install(self.base / 'clean install')
        self.request = {'pr': PR, 'instructions': '  Fix the flaky test exactly as discussed.\n'}
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}

    def preflight(self, install=None, transport=None, **changes):
        install = install or self.clean
        self.transport = transport or FakeTransport()
        arguments = {'request': self.request, 'configuration': None, 'invocation': None,
                     'deployment': self.deployment, 'repository': self.repo, 'home': self.home}
        arguments.update(changes)
        result = dk_pr_fix.preflight(transport=self.transport, install=install, **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--ignored'), '')
        return result


class Preflight(PreflightFixture):
    def test_default_plan_maps_admitted_options_to_declared_inputs(self):
        result = self.preflight()
        self.assertEqual((result['preflight'], result['structural'], result['semantic']), ('admitted', 'accepted', None))
        self.assertEqual(self.transport.calls['capabilities'], 1)
        inputs = result['inputs']
        self.assertEqual(set(inputs), {'request', 'tasks', 'finalizers', 'reviewers', 'adapters', 'limits',
                                       'permissions', 'ci', 'merge_method', 'accounts', 'settings', 'resources', 'turns'})
        self.assertEqual(inputs['tasks'], admission.DEFAULTS['tasks'])
        self.assertEqual(inputs['limits'], {'review_cycles': 5, 'retries_per_task': 1,
                                            'ci_repairs_per_cycle': 5, 'ci_reruns_per_head': 3,
                                            'max_parallel_reviewers': 0})
        self.assertEqual(inputs['ci'], {'timeout_seconds': 1800})
        self.assertFalse(any(inputs['permissions'].values()))
        # The simplification reviewer belongs to incoming review's preset, not the fix plan.
        self.assertEqual(set(inputs['adapters']), set(admission.BASELINE_ADAPTERS) - {admission.SIMPLIFIER})
        for name, adapter in inputs['adapters'].items():
            self.assertEqual(adapter['assignment'], admission.BASELINE_ADAPTERS[name]['assignment'])
        state = result['state']
        self.assertEqual(state['request_document']['instructions'], self.request['instructions'])
        self.assertEqual(state['request_document']['pr'], 'https://github.com/owner/repo/pull/7')
        self.assertEqual(inputs['request'], state['request'])
        self.assertTrue(Path(state['namespace']).is_relative_to(self.home / 'cf-workflows' / 'repositories'))
        self.assertEqual(result['installation']['closure_digest'], dk_pr_fix.verify_installation(self.clean))

    def test_supplied_skills_reject_at_preflight(self):
        skills = self.base / 'skills'
        (skills / 'lint').mkdir(parents=True)
        body = b'---\nname: lint\ndescription: Lint the change.\n---\nLint.\n'
        (skills / 'lint' / 'SKILL.md').write_bytes(body)
        files = {'SKILL.md': hashlib.sha256(body).hexdigest()}
        skill = {'id': 'lint-skill', 'name': 'lint', 'root': 'skills', 'path': 'lint',
                 'sha256': admission.skill_digest(files),
                 'files': [{'path': 'SKILL.md', 'sha256': files['SKILL.md']}]}
        # Supplied skills run only on an Agent Workflow #139 provider profile.
        skilled = {**self.deployment, 'agents': {'task': {'provider': 'claude-sdk', 'execution': {'mode': 'local'}}}}
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['claude-sdk'] = ['admitted_roles', 'native_session', admission.CLAUDE_SKILL_PROFILE]
        # Rejected structurally, before any gate read or validator session (asserted by preflight()).
        result = self.preflight(invocation={'options': {'tasks': [{'task': 'review', 'skill': 'lint-skill'}],
                                                        'finalizers': []}, 'skills': [skill]},
                                transport=FakeTransport(offered), roots={'skills': skills}, deployment=skilled)
        self.assertEqual((result['preflight'], result['structural'], result['semantic']),
                         ('rejected', 'accepted', 'blocked'))
        self.assertEqual(result['error']['path'], 'custom.skills')
        self.assertNotIn('inputs', result)
        self.assertNotIn('state', result)

    def test_publication_needs_enabled_permission_and_explicit_authorization(self):
        grant = {'request_id': 'r1', 'instruction': 'Push the fix and answer the review.', 'source': 'chat:1'}
        # An enabled permission alone grants nothing.
        result = self.preflight(invocation={'permissions': {'push': True}})
        self.assertEqual(result['preflight'], 'admitted')
        self.assertFalse(any(result['inputs']['permissions'].values()))
        self.assertEqual(result['state']['request_document']['authorizations'], {})
        # An authorization without the accepted permission rejects instead of silently dropping it.
        result = self.preflight(request={**self.request, 'authorizations': {'push': grant}})
        self.assertEqual((result['preflight'], result['structural']), ('rejected', 'accepted'))
        self.assertEqual(result['error']['path'], 'request.authorizations.push')
        self.assertNotIn('state', result)
        # Both together enable exactly the mapped inputs and stage exactly those grants.
        result = self.preflight(request={**self.request, 'authorizations': {'push': grant, 'reply': grant}},
                                invocation={'permissions': {'push': True, 'reply': True, 'edit_pr': True}})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual({key for key, value in result['inputs']['permissions'].items() if value},
                         {'push_commits', 'reply_to_feedback'})
        self.assertEqual(set(result['inputs']['permissions']), set(dk_pr_fix.PERMISSION_INPUTS))
        self.assertEqual(result['state']['request_document']['authorizations'], {'push': grant, 'reply': grant})
        self.assertEqual(self.transport.calls['capabilities'], 1)

    def test_unavailable_publication_actions_reject(self):
        grant = {'request_id': 'r1', 'instruction': 'Do it.', 'source': 'chat:1'}
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        for action, owner in (('approve_pr', 'incoming review'), ('create_issue', '#32'),
                              ('edit_issue', '#32'), ('approve_fork_ci', 'incoming review')):
            with self.subTest(action=action):
                result = self.preflight(request={**self.request, 'authorizations': {action: grant}},
                                        invocation={'permissions': {action: True}}, transport=FakeTransport(offered))
                self.assertEqual(result['preflight'], 'rejected')
                self.assertEqual(result['error']['path'], 'request.authorizations.' + action)
                self.assertIn(owner, result['error']['message'])
                self.assertNotIn('state', result)

    def test_merge_maps_only_with_its_own_grant_and_the_engine_merge_guard(self):
        grant = {'request_id': 'r1', 'instruction': 'Merge when ready.', 'source': 'chat:1'}
        request = {**self.request, 'authorizations': {'merge': grant}}
        # Without the engine's merge guard, admission rejects after one capability query.
        transport = FakeTransport(CAPABILITIES)
        result = self.preflight(request=request, invocation={'permissions': {'merge': True}}, transport=transport)
        self.assertEqual(result['preflight'], 'rejected')
        self.assertIn('merge_guard', result['error']['message'])
        self.assertNotIn('state', result)
        self.assertEqual(transport.calls['capabilities'], 1)
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        # Another action's grant never enables merge.
        other = {**self.request, 'authorizations': {'push': grant}}
        result = self.preflight(request=other, invocation={'permissions': {'push': True, 'merge': True}},
                                transport=FakeTransport(offered))
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertFalse(result['inputs']['permissions']['merge'])
        transport = FakeTransport(offered)
        result = self.preflight(request=request, invocation={'permissions': {'merge': True}}, transport=transport)
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual({key for key, value in result['inputs']['permissions'].items() if value}, {'merge'})
        self.assertEqual(result['state']['request_document']['authorizations'], {'merge': grant})
        self.assertEqual(transport.calls['capabilities'], 1)

    def test_installed_permission_inputs_match_the_publication_helpers(self):
        body = (INSTALL / 'workflows' / 'main.yml').read_text()
        block = body.split('      permissions:\n', 1)[1].split('      ci:\n', 1)[0]
        declared = re.findall(r'^          ([a-z_]+): false$', block, re.M)
        self.assertEqual(declared, list(dk_pr_fix.PERMISSION_INPUTS))
        sys.path.insert(0, str(HELPERS))
        try:
            from prfix.publication import PERMISSION_INPUTS
        finally:
            sys.path.remove(str(HELPERS))
        self.assertEqual(sorted(declared), PERMISSION_INPUTS)
        self.assertLessEqual(set(dk_pr_fix.PUBLICATION_ACTIONS), set(admission.ACTIONS))
        self.assertEqual(set(dk_pr_fix.PUBLICATION_ACTIONS) | set(dk_pr_fix.UNAVAILABLE_ACTIONS), set(admission.ACTIONS))

    def test_tampered_or_overlapping_installation_rejects_before_the_engine(self):
        copy_root = clean_install(self.base / 'install copy')
        (copy_root / 'workflows' / 'agents' / 'leaf.yaml').write_text('name: "x"\ndescription: "y"\ninstructions: "z"\n')
        result = self.preflight(install=copy_root)
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'installation'))
        self.assertEqual(self.transport.calls['capabilities'], 0)
        shutil.copy2(INSTALL / 'workflows' / 'agents' / 'leaf.yaml', copy_root / 'workflows' / 'agents' / 'leaf.yaml')
        (copy_root / 'workflows' / 'extra.yml').write_text('name: extra\n')
        self.assertEqual(self.preflight(install=copy_root)['error']['path'], 'installation')
        (copy_root / 'workflows' / 'extra.yml').unlink()
        (copy_root / 'workflows' / 'link.yml').symlink_to(copy_root / 'workflows' / 'main.yml')
        self.assertEqual(self.preflight(install=copy_root)['error']['path'], 'installation')
        (copy_root / 'workflows' / 'link.yml').unlink()
        for repository in (copy_root, copy_root / 'workflows'):
            with self.subTest(repository=repository):
                result = self.preflight(install=copy_root, repository=repository)
                self.assertEqual(result['error']['path'], 'repository')
        result = self.preflight(install=copy_root, roots={'cf-workflows': self.base})
        self.assertEqual(result['error']['path'], 'roots')
        self.assertEqual(self.transport.calls['capabilities'], 0)

    def test_engine_failure_blocks_without_staging(self):
        transport = FakeTransport()
        transport.configured['capabilities'] = lambda: (_ for _ in ()).throw(OSError('engine down'))
        result = self.preflight(transport=transport)
        self.assertEqual((result['preflight'], result['structural']), ('blocked', 'accepted'))

    def test_staging_creates_a_fresh_owned_namespace_once(self):
        prepared = self.preflight()
        reference = dk_pr_fix.stage_request(prepared)
        namespace = Path(prepared['state']['namespace'])
        self.assertEqual(sorted(os.listdir(namespace)), [dk_pr_fix.STATE_MARKER, 'requests'])
        data = (namespace / reference).read_bytes()
        self.assertEqual(reference, 'requests/' + hashlib.sha256(data).hexdigest() + '.json')
        self.assertEqual(json.loads(data)['instructions'], self.request['instructions'])
        self.assertEqual(stat.S_IMODE((namespace / reference).stat().st_mode), 0o600)
        self.assertEqual(dk_pr_fix.stage_request(prepared), reference)
        (namespace / reference).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            dk_pr_fix.stage_request(prepared)
        shutil.rmtree(namespace)
        namespace.mkdir()
        (namespace / '.pr-fix-state').mkdir()
        with self.assertRaisesRegex(ValueError, 'nonempty'):
            dk_pr_fix.stage_request(prepared)
        self.assertEqual(os.listdir(namespace), ['.pr-fix-state'])


class SemanticPreflight(PreflightFixture):
    """Custom inputs pass one engine validator task and durable gates before anything is mapped."""

    CUSTOM = {'prompts': [{'id': 'tone', 'slot': 'additional', 'text': 'Keep the diff small.'}],
              'options': {'tasks': [{'task': 'address-feedback', 'instructions': 'Only touch tests/.'}],
                          'requirements': ['Every fix gets a regression test.'],
                          'prohibitions': ['merge', 'Never edit vendored code.'],
                          'adapters': {'check-docs': {'kind': 'inspection', 'writes': False, 'definition': 'leaf',
                                                      'assignment': 'Check that docs match the change.'}},
                          'finalizers': ['check-docs']}}

    def setUp(self):
        super().setUp()
        self.engine = Engine()

    def assess(self, *sessions, invocation=None, **changes):
        """One preflight against a fake engine: scripted validator tasks and a durable gate store."""
        self.transport = FakeTransport()
        script = list(sessions)
        self.assignments = []

        def session(task):
            self.assignments.append(task)
            if not script:
                raise AssertionError('unscripted validator session')
            return script.pop(0)(task)
        self.transport.configured.update(
            validator_session=session, open_input_gate=self.engine.open,
            input_gates=lambda request_id: copy.deepcopy([gate for gate in self.engine.gates
                                                          if gate['request_id'] == request_id]))
        arguments = {'request': self.request, 'configuration': None,
                     'invocation': self.CUSTOM if invocation is None else invocation,
                     'deployment': self.deployment, 'repository': self.repo, 'home': self.home, **changes}
        result = dk_pr_fix.preflight(transport=self.transport, install=self.clean, **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls),
                             {'capabilities', 'input_gates', 'validator_session', 'open_input_gate'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--ignored'), '')
        return result

    def test_default_plan_runs_no_validator_task(self):
        result = self.assess(invocation={})
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', None))
        self.assertEqual(self.transport.calls, Counter(capabilities=1))
        self.assertNotIn('prompts', result['inputs'])
        self.assertNotIn('policy', result['inputs'])

    def test_accepted_custom_inputs_reach_the_declared_inputs(self):
        structural = admission.admit(**{key: copy.deepcopy(value) for key, value in (
            ('request', self.request), ('configuration', None), ('invocation', self.CUSTOM),
            ('deployment', self.deployment))}, roles=dk_pr_fix.role_catalog(self.clean),
            roots={'repository': self.repo, dk_pr_fix.INSTALL_ROOT: self.clean}, transport=FakeTransport())
        self.assertEqual(structural['preflight'], 'semantic_required')
        result = self.assess(verdict('accepted'))
        self.assertEqual((result['preflight'], result['structural'], result['semantic']),
                         ('admitted', 'accepted', 'accepted'), result.get('error'))
        self.assertEqual(self.transport.calls, Counter(capabilities=1, input_gates=1, validator_session=1))
        [task] = self.assignments
        self.assertEqual(task['input'], structural['admission_input'])
        self.assertEqual(sorted(task['subjects']), sorted(
            ['invocation.prompts[0]', 'invocation.options.tasks[0].instructions',
             'invocation.options.adapters.check-docs', 'invocation.options.requirements[0]',
             'invocation.options.prohibitions[1]']))
        inputs = result['inputs']
        self.assertEqual(inputs['prompts'], [{'id': 'tone', 'slot': 'additional', 'target': None,
                                              'text': 'Keep the diff small.'}])
        # Action prohibitions stay permissions; only free text is policy.
        self.assertEqual(inputs['policy'], {'requirements': ['Every fix gets a regression test.'],
                                            'prohibitions': ['Never edit vendored code.']})
        self.assertEqual(inputs['tasks'], self.CUSTOM['options']['tasks'])
        self.assertEqual(inputs['finalizers'], ['check-docs'])
        self.assertEqual(inputs['adapters']['check-docs']['assignment'], 'Check that docs match the change.')
        self.assertEqual(result['admission']['evidence_refs'][-1], 'native:result-' + task['claim'])
        # Acceptance grants nothing: permissions and the staged request are the structural ones.
        self.assertFalse(any(inputs['permissions'].values()))
        self.assertEqual(result['snapshot'], structural['snapshot'])

    def test_clarification_waits_then_the_answered_revision_is_assessed_again(self):
        question = [{'id': 'u1', 'paths': ['invocation.prompts[0]'], 'question': 'Small enough to skip tests?'}]
        result = self.assess(verdict('needs_clarification', uncertainties=question))
        self.assertEqual((result['preflight'], result['semantic']), ('waiting', 'needs_clarification'))
        self.assertEqual(result['gate']['status'], 'waiting')
        self.assertNotIn('inputs', result)
        self.assertNotIn('state', result)
        # Interrupted: the same proposal keeps waiting on the same gate, with no new session.
        self.assertEqual(self.assess()['preflight'], 'waiting')
        self.assertEqual(self.transport.calls, Counter(capabilities=1, input_gates=1))
        self.engine.answer('gate-1', 'answer-1', {'action': 'supplement', 'text': 'Tests still apply.'})
        result = self.assess(verdict('accepted', resolved=[{'id': 'r1', 'paths': ['invocation.prompts[0]'],
                                                            'detail': 'Answered.'}]))
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'), result.get('error'))
        self.assertEqual(self.transport.calls, Counter(capabilities=2, input_gates=1, validator_session=1))
        self.assertEqual([item['text'] for item in result['inputs']['prompts']],
                         ['Keep the diff small.', 'Tests still apply.'])
        self.assertEqual(result['clarifications'][0]['answer_id'], 'answer-1')

    def test_rejection_or_a_blocked_validator_maps_nothing(self):
        conflict = [{'id': 'c1', 'paths': ['invocation.prompts[0]', 'invocation.options.prohibitions[1]'],
                     'detail': 'Contradiction.'}]
        for response, outcome in ((verdict('rejected', conflicts=conflict), 'rejected'),
                                  (verdict('accepted', operations=[{'kind': 'write', 'target': 'repository:x'}]),
                                   'blocked'),
                                  (verdict('accepted', operations=None, skills_loaded=['lint']), 'blocked')):
            with self.subTest(outcome=outcome):
                self.engine = Engine()
                result = self.assess(response)
                self.assertEqual(result['preflight'], outcome)
                self.assertNotIn('inputs', result)
                self.assertEqual(self.transport.calls['validator_session'], 1)

    def test_installed_workflow_limits_reject_before_any_gate_read_or_session(self):
        cases = [
            ({'request': {'repository': 'github.com/owner/repo', 'instructions': 'Fix it.'}}, 'request.pr'),
            ({'invocation': {**self.CUSTOM, 'prompts': [{'id': 'all', 'slot': 'behavior_replacement',
                                                          'text': 'Do something else.'}]}}, 'prompts'),
            ({'invocation': {**self.CUSTOM, 'options': {**self.CUSTOM['options'], 'tasks': ['draft-issue']}}},
             'options.tasks'),
        ]
        for changes, path in cases:
            with self.subTest(path=path):
                result = self.assess(**changes)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path), result)
                self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_resource_bearing_custom_inputs_reject_before_any_gate_read_or_session(self):
        # The workflow carries no configured resource, so an adapter's resources or a prompt's
        # files would reach no occurrence; they reject instead of running without them.
        guidance = self.base / 'guidance'
        (guidance / 'prompts').mkdir(parents=True)
        files = {'guide.md': b'See [notes](prompts/notes.md).\n', 'prompts/notes.md': b'Prefer small diffs.\n'}
        for rel, body in files.items():
            (guidance / rel).write_bytes(body)
        configuration = {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                         'scopes': [], 'resources': [{'root': 'configuration', 'path': rel,
                                                      'sha256': hashlib.sha256(body).hexdigest(),
                                                      'purpose': 'Guidance.', 'owner': 'maintainers'}
                                                     for rel, body in files.items()],
                         'admission': {'structural': 'pending', 'semantic': None, 'request_id': None,
                                       'context_digest': None, 'resource_digest': None,
                                       'capability_digest': None, 'evidence_refs': []}}
        adapter = self.CUSTOM['options']['adapters']['check-docs']
        cases = [
            ({**self.CUSTOM, 'options': {**self.CUSTOM['options'], 'adapters': {
                'check-docs': {**adapter, 'resources': ['configuration:prompts/notes.md']}}}},
             'options.adapters.check-docs.resources'),
            ({**self.CUSTOM, 'prompts': [{**self.CUSTOM['prompts'][0], 'resources': ['configuration:prompts/notes.md']}]},
             'invocation.prompts[0].resources'),
            ({**self.CUSTOM, 'prompts': [{'id': 'tone', 'slot': 'additional', 'resource': 'configuration:guide.md'}]},
             'invocation.prompts[0].resource'),
            ({**self.CUSTOM, 'preferences': [{'id': 'small', 'text': 'Keep it small.',
                                              'resources': ['configuration:prompts/notes.md']}]},
             'invocation.preferences[0].resources'),
        ]
        for invocation, path in cases:
            with self.subTest(path=path):
                result = self.assess(invocation=invocation, configuration=configuration,
                                     roots={'configuration': guidance})
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path), result)
                self.assertNotIn('inputs', result)
                self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_preset_entries_keep_rejecting_custom_inputs_before_any_gate_read(self):
        self.transport = FakeTransport()
        result = dk_pr_fix.preflight(request=self.request, configuration=None, invocation=copy.deepcopy(self.CUSTOM),
                                     deployment=self.deployment, repository=self.repo, home=self.home,
                                     transport=self.transport, install=self.clean, custom=False)
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'custom'))
        self.assertEqual(self.transport.calls, Counter(capabilities=1))


class RelocatedReadOnlyRun(PreflightFixture):
    """Admit, stage and run preparation and a first claim from a relocated read-only install."""

    def setUp(self):
        super().setUp()
        (self.repo / 'code.py').write_text('value = 1\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'Initial')
        self.target = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-q', '-c', 'fix')
        (self.repo / 'code.py').write_text('value = 2\n')
        git(self.repo, 'commit', '-q', '-am', 'Change')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.install = self.base / 'installed plugins' / 'cf workflows' / 'dk-pr-fix'
        clean_install(self.install)
        for path in [self.install, *self.install.rglob('*')]:
            path.chmod(path.stat().st_mode & ~0o222)
        self.addCleanup(lambda: [path.chmod(path.stat().st_mode | 0o200)
                                 for path in [self.install, *self.install.rglob('*')]])

    def evidence(self):
        metadata = {'html_url': 'https://github.com/owner/repo/pull/7', 'number': 7, 'state': 'open',
                    'merged': False, 'draft': False, 'title': 'Fix', 'body': 'Fix a problem', 'updated_at': 'now',
                    'user': {'id': 1, 'login': 'owner'},
                    'head': {'sha': self.head, 'ref': 'fix', 'repo': {'full_name': 'owner/repo'}},
                    'base': {'sha': self.target, 'ref': 'main', 'repo': {'full_name': 'owner/repo'}}}
        return {'metadata': metadata, 'viewer': {'id': 1, 'login': 'owner'},
                'comments': [], 'reviews': [], 'inline': [], 'threads': []}

    def run_driver(self, prepared, steps):
        spec = {'helpers': str(self.install / 'workflows' / 'helpers'), 'root': str(self.repo),
                'pr': prepared['state']['request_document']['pr'], 'evidence': self.evidence(),
                'execution': 'fixture-admitted-execution', 'steps': steps}
        environment = {'PATH': os.environ['PATH'], 'HOME': str(self.base), **GIT_ENV,
                       'CF_WORKFLOWS_STATE': prepared['state']['namespace']}
        completed = subprocess.run([sys.executable, '-I', '-B', str(DRIVER)], input=json.dumps(spec).encode(),
                                   capture_output=True, env=environment, cwd=self.base, timeout=120)
        return completed

    def test_preparation_and_first_claim_from_installed_resources(self):
        before = dk_pr_fix.verify_installation(self.install)
        prepared = self.preflight(install=self.install)
        self.assertEqual(prepared['preflight'], 'admitted', prepared)
        dk_pr_fix.stage_request(prepared)
        controls = {key: value for key, value in prepared['inputs'].items() if key != 'request'}
        steps = [{'name': 'request', 'operation': 'snapshot_request', 'input': {'request': prepared['inputs']['request']}},
                 {'name': 'prepare', 'operation': 'prepare', 'input': {'controls': controls},
                  'refs': {'snapshot': ['request', 'result', 'output', 'snapshot']}},
                 {'name': 'claim', 'operation': 'claim', 'input': {'cycle': 1, 'publication': ''},
                  'refs': {'plan': ['prepare', 'result', 'output', 'plan'],
                           'task': ['prepare', 'result', 'output', 'stages', 0, 'groups', 0, 'tasks', 0, 'id']}}]
        completed = self.run_driver(prepared, steps)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode()[-4000:])
        output = json.loads(completed.stdout)
        for module in output['modules']:
            self.assertTrue(Path(module).is_relative_to(self.install), module)
        # Preparation, the claim's snapshot and the read-feedback occurrence's retained feedback.
        self.assertEqual(output['calls'], {'capture': 3})
        claim = output['results']['claim']['result']['output']
        self.assertTrue(claim['invoke'])
        assignment = claim['task_input']
        self.assertEqual(assignment['feedback'], {'sources': []})
        self.assertEqual(assignment['instructions'], self.request['instructions'])
        self.assertEqual((assignment['snapshot']['head'], assignment['snapshot']['target']), (self.head, self.target))
        self.assertEqual(assignment['task']['skill'], 'read-feedback')
        # Nothing reaches the target diff or the installation; state lives in its own namespace.
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--ignored'), '')
        self.assertEqual(dk_pr_fix.verify_installation(self.install), before)
        namespace = Path(prepared['state']['namespace'])
        self.assertTrue({'artifacts', 'requests', 'preparations', dk_pr_fix.STATE_MARKER} <= set(os.listdir(namespace)))
        plan_ref = output['results']['prepare']['result']['output']['plan']
        plan = json.loads((namespace / plan_ref).read_text())
        self.assertEqual(plan['instructions'], self.request['instructions'])
        self.assertEqual(plan['definitions'], {role: record['sha256'] for role, record in
                                               dk_pr_fix.role_catalog(self.install).items()})
        tasks = {task['id']: task for task in plan['tasks']}
        # Independent verification is its own required task on a separate admitted role.
        self.assertEqual((tasks['finding_verification']['definition'], tasks['finding_verification']['required']),
                         ('verifier', True))
        self.assertEqual({task['definition'] for task in plan['tasks'] if task['kind'] == 'review'}, {'reviewer'})
        # Without accepted custom inputs no occurrence carries prompts or policy, so fingerprints are unchanged.
        self.assertFalse([task['id'] for task in plan['tasks'] if {'prompts', 'policy', 'behavior'} & set(task)])

    def test_accepted_custom_inputs_reach_the_first_claim(self):
        engine = Engine()
        transport = FakeTransport()
        transport.configured.update(validator_session=verdict('accepted'), open_input_gate=engine.open,
                                    input_gates=lambda request_id: [])
        invocation = {'prompts': [{'id': 'scope', 'slot': 'behavior_replacement', 'target': 'read-feedback',
                                   'text': 'Capture only threads on tests/.'},
                                  {'id': 'tone', 'slot': 'additional', 'text': 'Quote each source exactly.'},
                                  {'id': 'reviews', 'slot': 'reviewer', 'text': 'Check the test names.'}],
                      'options': {'tasks': [{'task': 'read-feedback', 'instructions': 'Skip bot comments.'}],
                                  'finalizers': [], 'requirements': ['Keep every source ID.']}}
        prepared = dk_pr_fix.preflight(request=self.request, configuration=None, invocation=invocation,
                                       deployment=self.deployment, repository=self.repo, home=self.home,
                                       transport=transport, install=self.install)
        self.assertEqual((prepared['preflight'], prepared['semantic']), ('admitted', 'accepted'), prepared.get('error'))
        self.assertEqual(transport.calls['validator_session'], 1)
        dk_pr_fix.stage_request(prepared)
        controls = {key: value for key, value in prepared['inputs'].items() if key != 'request'}
        steps = [{'name': 'request', 'operation': 'snapshot_request', 'input': {'request': prepared['inputs']['request']}},
                 {'name': 'prepare', 'operation': 'prepare', 'input': {'controls': controls},
                  'refs': {'snapshot': ['request', 'result', 'output', 'snapshot']}},
                 {'name': 'claim', 'operation': 'claim', 'input': {'cycle': 1, 'publication': ''},
                  'refs': {'plan': ['prepare', 'result', 'output', 'plan'],
                           'task': ['prepare', 'result', 'output', 'stages', 0, 'groups', 0, 'tasks', 0, 'id']}}]
        completed = self.run_driver(prepared, steps)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode()[-4000:])
        task = json.loads(completed.stdout)['results']['claim']['result']['output']['task_input']['task']
        # The accepted replacement stands in for the adapter assignment; the instructions still follow it.
        self.assertEqual(task['assignment'], 'Capture only threads on tests/.\nSkip bot comments.')
        self.assertEqual(task['behavior'], 'prompt:scope')
        # A reviewer-slot prompt reaches review occurrences only.
        self.assertEqual(task['prompts'], [{'id': 'tone', 'slot': 'additional', 'text': 'Quote each source exactly.'}])
        self.assertEqual(task['policy'], {'requirements': ['Keep every source ID.']})

    def test_unconfigured_github_operation_fails_without_fallback(self):
        prepared = self.preflight(install=self.install)
        dk_pr_fix.stage_request(prepared)
        controls = {key: value for key, value in prepared['inputs'].items() if key != 'request'}
        steps = [{'name': 'request', 'operation': 'snapshot_request', 'input': {'request': prepared['inputs']['request']}},
                 {'name': 'prepare', 'operation': 'prepare', 'input': {'controls': controls},
                  'refs': {'snapshot': ['request', 'result', 'output', 'snapshot']}},
                 {'name': 'ci', 'operation': 'ci', 'input': {},
                  'refs': {'plan': ['prepare', 'result', 'output', 'plan']}}]
        completed = self.run_driver(prepared, steps)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('unconfigured fixture operation', completed.stderr.decode())
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--ignored'), '')

    def test_helpers_refuse_a_state_namespace_inside_the_checkout(self):
        prepared = self.preflight(install=self.install)
        dk_pr_fix.stage_request(prepared)
        inside = copy.deepcopy(prepared)
        inside['state']['namespace'] = str(self.repo / '.git' / 'cf state')
        Path(inside['state']['namespace']).mkdir()
        steps = [{'name': 'request', 'operation': 'snapshot_request', 'input': {'request': prepared['inputs']['request']}}]
        completed = self.run_driver(inside, steps)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('disjoint from the target checkout', completed.stderr.decode())


if __name__ == '__main__':
    unittest.main()
