"""Plugin descriptor, declared resource closure, package build comparison and honest inventory status.

Every case checks disposable copies of the plugin root, including read-only
copies under paths with spaces. The build comparison runs against a scripted
fake toolchain whose unconfigured commands fail; with CF_AGENT_WORKFLOW set to
a local agent-workflow clone it also builds the real CLI from the pinned,
unreleased source commit. Nothing is installed, downloaded or run; these
results do not certify any engine, host, package or release.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import dk_pr_fix, package  # noqa: E402

SPEC = importlib.util.spec_from_file_location('check_package_build', ROOT / 'scripts/check_package_build.py')
build_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_check)

DESCRIPTOR = (package.ROOT / package.DESCRIPTOR).read_text()


def copy_root(destination):
    shutil.copytree(package.ROOT, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return destination


def read_only(root):
    for directory, names, files in os.walk(root):
        for name in files:
            os.chmod(Path(directory) / name, 0o444)
        os.chmod(directory, 0o555)


def writable(root):
    for directory, names, files in os.walk(root):
        os.chmod(directory, 0o755)


def regenerate(root):
    dk_pr_fix.write_closure(root)


class Descriptor(unittest.TestCase):
    def test_source_descriptor_lists_both_hosts_and_its_own_version(self):
        descriptor = package.validate_descriptor(package.parse_descriptor(DESCRIPTOR.encode()))
        self.assertEqual(descriptor['hosts'], ['codex', 'claude'])
        self.assertEqual(descriptor['skills'], [{'name': 'dk-pr-fix', 'path': 'skills/dk-pr-fix/SKILL.md',
                                                 'workflow': 'workflows/main.yml'},
                                                {'name': 'dk-pr-review', 'path': 'skills/dk-pr-review/SKILL.md',
                                                 'workflow': 'workflows/review.yml'},
                                                {'name': 'dk-pr-iter', 'path': 'skills/dk-pr-iter/SKILL.md',
                                                 'workflow': 'workflows/iter.yml'}]
                         + [{'name': f'dk-pr-feedback-{part}', 'path': f'skills/dk-pr-feedback-{part}/SKILL.md',
                             'workflow': f'workflows/feedback-{part}.yml'} for part in ('read', 'address', 'reply')]
                         + [{'name': name, 'path': f'skills/{name}/SKILL.md', 'workflow': f'workflows/{entry}.yml'}
                            for name, entry in (('dk-pr-publish', 'publish'), ('dk-pr-review-post', 'review-post'),
                                                ('dk-plain-english', 'plain-english'),
                                                ('dk-asd-ste100', 'asd-ste100'),
                                                ('dk-pr-decisions', 'decisions'),
                                                ('dk-pr-reviewers-run', 'reviewers-run'),
                                                ('dk-code-simplify', 'code-simplify'),
                                                ('dk-pr-approvers', 'approvers'),
                                                ('dk-pr-description', 'description'),
                                                ('dk-commit-message', 'commit-message'), ('dk-issue', 'issue'),
                                                ('dk-commit-split', 'commit-split'), ('dk-pr-split', 'pr-split'),
                                                ('dk-repo-onboard', 'onboard'), ('dk-config', 'config'),
                                                ('dk-preferences', 'preferences'),
                                                ('dk-ci', 'ci'), ('dk-ci-approve', 'ci-approve'),
                                                ('dk-ci-flakes', 'flakes'),
                                                ('dk-issue-alternatives', 'issue-alternatives'),
                                                ('dk-pr-alternatives', 'pr-alternatives'),
                                                ('dk-alternatives-review', 'alternatives-review'),
                                                ('dk-pr-review-alternatives', 'pr-review-alternatives'))])
        # Plugin and engine versions are independent bindings.
        self.assertNotEqual(descriptor['version'], descriptor['engine-version'])

    def test_invalid_descriptors_reject(self):
        cases = {
            'unknown field': DESCRIPTOR + 'launcher: scripts/run\n',
            'duplicate key': DESCRIPTOR + 'name: other\n',
            'version range': DESCRIPTOR.replace('engine-version: 0.2.2', 'engine-version: ^0.2.2'),
            'v prefix': DESCRIPTOR.replace('engine-version: 0.2.2', 'engine-version: v0.2.2'),
            'leading zero prerelease': DESCRIPTOR.replace('version: 0.1.1', 'version: 0.1.0-01'),
            'one host': DESCRIPTOR.replace('[codex, claude]', '[codex]'),
            'unknown host': DESCRIPTOR.replace('[codex, claude]', '[codex, claude, cursor]'),
            'duplicate host': DESCRIPTOR.replace('[codex, claude]', '[codex, codex]'),
            'integer host': DESCRIPTOR.replace('[codex, claude]', '[codex, 1]'),
            'escaping workflow': DESCRIPTOR.replace('workflow: workflows/main.yml', 'workflow: ../main.yml'),
            'remote workflow': DESCRIPTOR.replace('workflow: workflows/main.yml', 'workflow: o/r/main.yml@v1'),
            'skill path': DESCRIPTOR.replace('skills/dk-pr-fix/SKILL.md', 'skills/dk-pr-fix/README.md'),
            'absolute resource': DESCRIPTOR + '  - /etc/passwd\n',
            'whole root': DESCRIPTOR + '  - .\n',
            'expression': DESCRIPTOR + '  - ${{ inputs.x }}\n',
            'anchor': DESCRIPTOR.replace('name: cf-workflows', 'name: &n cf-workflows'),
            'tab': DESCRIPTOR.replace('  - LICENSE', '\t- LICENSE'),
            'extra skill field': DESCRIPTOR.replace('    workflow: workflows/main.yml',
                                                    '    workflow: workflows/main.yml\n    host: codex'),
            'schema': DESCRIPTOR.replace('schema-version: 1', 'schema-version: 2'),
            'root workflow': DESCRIPTOR.replace('workflow: workflows/main.yml', 'workflow: main.yml'),
            'empty component': DESCRIPTOR.replace('workflow: workflows/main.yml', 'workflow: workflows//main.yml'),
            'duplicate skill': DESCRIPTOR.replace('resources:', '  - name: dk-pr-fix\n    path: skills/x/SKILL.md\n'
                                                  '    workflow: workflows/x.yml\nresources:'),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(package.PackageError):
                package.validate_descriptor(package.parse_descriptor(text.encode()))


class Closure(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(writable, self.base)

    def test_relocated_read_only_copies_have_identical_closures(self):
        first = copy_root(self.base / 'first install' / 'cf workflows')
        second = copy_root(self.base / 'second install' / 'plugin root')
        os.utime(second / 'plugin.yaml', (1, 1))
        read_only(first)
        read_only(second)
        results = [package.report(root) for root in (first, second)]
        self.assertEqual(results[0], results[1])
        checked = package.check(first)
        installed = json.loads((first / dk_pr_fix.CLOSURE).read_text())['files']
        self.assertEqual({path: entry['sha256'] for path, entry in checked['files'].items()
                          if path != dk_pr_fix.CLOSURE}, installed)
        self.assertEqual(results[0]['files'], len(installed) + 1)
        # The checks wrote nothing.
        self.assertEqual(package.check(first), checked)

    def test_closure_follows_workflow_references_helpers_and_agents(self):
        descriptor, closure = package.declared_closure(copy_root(self.base / 'root'))
        expected = {'plugin.yaml', 'skills/dk-pr-fix/SKILL.md', 'LICENSE', 'agents.yml', 'closure.json',
                    'request.example.json'}
        self.assertTrue(expected <= set(closure))
        self.assertTrue({'workflows/main.yml', 'workflows/native.yml', 'workflows/helpers/action.py',
                         'workflows/helpers/prfix/core.py', 'workflows/agents/verifier.yaml'} <= set(closure))

    def test_undeclared_missing_and_linked_resources_reject(self):
        def plant(root):
            (root / 'notes.txt').write_text('scratch')

        def unreachable(root):
            (root / 'workflows' / 'extra.yml').write_text('name: extra\n')

        def missing(root):
            (root / 'request.example.json').unlink()

        def link(root):
            (root / 'skills' / 'dk-pr-fix' / 'SKILL.md').unlink()
            (root / 'skills' / 'dk-pr-fix' / 'SKILL.md').symlink_to(package.ROOT / 'skills/dk-pr-fix/SKILL.md')

        def manifest(root):
            (root / '.codex-plugin').mkdir()
            (root / '.codex-plugin' / 'plugin.json').write_text('{}')

        def collision(root):
            (root / 'license').write_text('copy')

        def bytecode(root):
            (root / 'workflows' / 'helpers' / '__pycache__').mkdir()

        def mismatch(root):
            text = (root / 'skills/dk-pr-fix/SKILL.md').read_text().replace('name: dk-pr-fix', 'name: dk-pr-review')
            (root / 'skills/dk-pr-fix/SKILL.md').write_text(text)

        def remote(root):
            text = (root / 'workflows/main.yml').read_text().replace('uses: workflows/cycle.yml', 'uses: o/r/x.yml@v1')
            (root / 'workflows/main.yml').write_text(text)

        def escaping(root):
            text = (root / 'workflows/helper.yml').read_text().replace('- helpers/', '- ../helpers/')
            (root / 'workflows/helper.yml').write_text(text)

        def cycle(root):
            text = (root / 'workflows/cycle.yml').read_text().replace('uses: workflows/stage.yml',
                                                                      'uses: workflows/main.yml')
            (root / 'workflows/cycle.yml').write_text(text)

        def native_agent(root):
            text = (root / 'workflows/native.yml').read_text().replace('agents/leaf.yaml', 'agents/leaf.toml')
            (root / 'workflows/native.yml').write_text(text)

        def flow_files(root):
            text = (root / 'workflows/ci-poll.yml').read_text().replace(
                '      files:\n      - helpers/\n      - agents/\n', '      files: [helpers/, agents/, helpers/absent.py]\n')
            (root / 'workflows/ci-poll.yml').write_text(text)

        def flow_definitions(root):
            text = (root / 'workflows/native.yml').read_text().replace(
                'definitions:\n', 'definitions: {leaf: agents/absent.yaml}\n          x-definitions:\n')
            (root / 'workflows/native.yml').write_text(text)

        def not_reusable(root):
            text = (root / 'workflows/helper.yml').read_text().replace('  workflow_call:', '  workflow_dispatch:')
            (root / 'workflows/helper.yml').write_text(text)

        def first_binding(root):
            # A workflow bound to a skill is still checked when another skill's workflow calls it.
            self.skill(root, 'x-helper', 'workflows/helper.yml', first=True)
            not_reusable(root)

        def skill_directory(root):
            (root / 'skills/dk-pr-fix/SKILL.md').unlink()
            (root / 'skills/dk-pr-fix/SKILL.md').mkdir()
            (root / 'skills/dk-pr-fix/SKILL.md/x').write_text('x')

        def workflow_directory(root):
            (root / 'workflows/helper.yml').unlink()
            (root / 'workflows/helper.yml').mkdir()
            (root / 'workflows/helper.yml/x').write_text('x')

        def invalid_utf8(root):
            (root / 'skills/dk-pr-fix/SKILL.md').write_bytes(b'---\nname: dk-pr-fix\ndescription: \xff\n---\nx\n')

        def invalid_workflow_utf8(root):
            with (root / 'workflows/helper.yml').open('ab') as stream:
                stream.write(b'# \xff\n')

        def normalization(root):
            (root / 'extra').mkdir()
            for form in ('NFC', 'NFD'):
                (root / 'extra' / unicodedata.normalize(form, 'caf\u00e9.txt')).write_text(form)
            text = (root / 'plugin.yaml').read_text().replace('  - LICENSE', '  - LICENSE\n  - extra')
            (root / 'plugin.yaml').write_text(text)

        cases = {'flow files': (flow_files, 'workflows/ci-poll.yml:21: files must be a block list'),
                 'flow definitions': (flow_definitions, 'definitions must be a block list'),
                 'not reusable': (not_reusable, 'workflows/helper.yml: a called plugin workflow requires'),
                 'not reusable but bound first': (first_binding,
                                                  'workflows/helper.yml: a called plugin workflow requires'),
                 'skill directory': (skill_directory, 'SKILL.md: requires a regular file'),
                 'workflow directory': (workflow_directory, 'helper.yml: requires a regular file'),
                 'invalid UTF-8 skill': (invalid_utf8, 'SKILL.md: must be UTF-8'),
                 'invalid UTF-8 workflow': (invalid_workflow_utf8, 'helper.yml: must be UTF-8'),
                 'normalization collision': (normalization, 'normalizing hosts'),
                 'undeclared': (plant, 'notes.txt'), 'unreachable workflow': (unreachable, 'workflows/extra.yml'),
                 'missing resource': (missing, 'request.example.json'), 'symlink': (link, 'skills/dk-pr-fix/SKILL.md'),
                 'hand-maintained manifest': (manifest, '.codex-plugin'), 'case collision': (collision, 'license'),
                 'bytecode': (bytecode, 'workflows/helpers/__pycache__'),
                 'skill name': (mismatch, 'skills/dk-pr-fix/SKILL.md'), 'remote reference': (remote, 'workflows/main.yml'),
                 'helper traversal': (escaping, 'workflows/helper.yml'), 'native agent': (native_agent, 'leaf.toml'),
                 'cycle': (cycle, 'dependency cycle')}
        for name, (change, path) in cases.items():
            with self.subTest(name):
                root = copy_root(self.base / name)
                change(root)
                if name not in ('hand-maintained manifest', 'case collision', 'bytecode', 'symlink',
                                'normalization collision', 'skill directory', 'workflow directory'):
                    regenerate(root)  # Even a refreshed closure.json cannot admit the change.
                with self.assertRaises(package.PackageError) as raised:
                    package.check(root)
                self.assertIn(path, str(raised.exception))

    @staticmethod
    def skill(root, name, workflow, first=False):
        (root / 'skills' / name).mkdir()
        (root / 'skills' / name / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: x\n---\nRun it.\n')
        entry = f'  - name: {name}\n    path: skills/{name}/SKILL.md\n    workflow: {workflow}\n'
        text = (root / 'plugin.yaml').read_text()
        (root / 'plugin.yaml').write_text(text.replace('skills:\n', 'skills:\n' + entry) if first
                                          else text.replace('resources:', entry + 'resources:'))

    def test_dot_prefixed_skill_workflow_is_the_declared_file(self):
        root = copy_root(self.base / 'root')
        text = (root / 'plugin.yaml').read_text().replace('workflow: workflows/main.yml',
                                                          'workflow: ./workflows/main.yml')
        (root / 'plugin.yaml').write_text(text)
        regenerate(root)
        descriptor, closure = package.declared_closure(root)
        self.assertEqual(descriptor['skills'][0]['workflow'], 'workflows/main.yml')
        self.assertNotIn('./workflows/main.yml', closure)
        package.check(root)

    def test_installed_files_must_match_their_manifest(self):
        root = copy_root(self.base / 'root')
        with (root / 'agents.yml').open('a') as stream:
            stream.write('# edited\n')
        with self.assertRaisesRegex(package.PackageError, 'closure.json: installed closure differs'):
            package.check(root)

    def test_executable_mode_changes_the_closure_digest(self):
        root = copy_root(self.base / 'root')
        before = package.check(root)['closure_digest']
        helper = root / 'workflows' / 'helpers' / 'action.py'
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
        self.assertNotEqual(package.check(root)['closure_digest'], before)


# A scripted stand-in for `agent-workflow plugin check|build`: it records every
# invocation, fails any other command, and injects one FAULT into its output.
FAKE_TOOLCHAIN = r"""#!{python}
import hashlib, io, json, os, sys, time, zipfile
from pathlib import Path
FAULT, LOG = {fault!r}, {log!r}
with open(LOG, 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\n')
args = sys.argv[1:]
if args[:3] == ['plugin', 'check', '--json']:
    print(json.dumps({{'schema-version': 1, 'kind': 'structure', 'passed': FAULT != 'check',
                      'diagnostics': [{{'code': 'fake'}}] if FAULT == 'check' else []}}))
    sys.exit(FAULT == 'check')
if args[:3] != ['plugin', 'build', '--target'] or args[4] != '--output' or len(args) != 7:
    sys.exit('unconfigured fake operation ' + json.dumps(args))
targets, output, source = sorted(args[3].split(',')), Path(args[5]), Path(args[6])
name, version = 'cf-workflows', '0.1.1'
files = {{}}
for directory, _, entries in os.walk(source):
    for entry in entries:
        path = Path(directory) / entry
        files[path.relative_to(source).as_posix()] = path.read_bytes()
for skill in ('skills/dk-pr-fix/SKILL.md', 'skills/dk-pr-review/SKILL.md'):
    files[skill] += b'\n## Generated invocation guidance\n'
generated = {{'.claude-plugin/plugin.json': b'{{}}', '.codex-plugin/plugin.json': b'{{}}', 'plugin.json': b'{{}}',
             'compatibility.json': b'{{}}', 'scripts/agent-workflow': b'#!/bin/sh\n',
             'scripts/engine_bootstrap.py': b'# bootstrap\n'}}
files.update(generated)
if FAULT == 'undeclared':
    files['notes.txt'] = b'scratch'
if FAULT == 'dropped':
    del files['request.example.json']
if FAULT == 'altered':
    files['LICENSE'] += b'changed'
if FAULT == 'guidance':
    files['skills/dk-pr-fix/SKILL.md'] = b'generated only'
listed = sorted(set(files) - {{'plugin.yaml'}} | ({{'notes'}} if FAULT == 'descriptor' else set()))
files['plugin.yaml'] = ('schema-version: 1\nname: ' + name + '\nversion: ' + version + '\nengine-version: 0.2.2\n'
                        'resources:\n' + ''.join('    - ' + path + '\n' for path in listed)).encode()
tree = {{name + '/plugins/' + name + '/' + path: data for path, data in files.items()}}
tree[name + '/.agents/plugins/marketplace.json'] = b'{{}}'
tree[name + '/.claude-plugin/marketplace.json'] = b'{{}}'
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, 'w') as archive:
    for path in sorted(tree):
        if FAULT == 'zip' and path.endswith('LICENSE'):
            continue
        archive.writestr(zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0)), tree[path])
report = {{'schema_version': 1, 'name': name, 'version': version, 'engine_version': '0.2.2', 'targets': targets,
          'source_digest': '0' * 64, 'package_digest': '1' * 64, 'archive': name + '-' + version + '.zip'}}
if FAULT == 'timestamp':
    report['built_at'] = time.time_ns()
everything = {{**tree, report['archive']: buffer.getvalue(), 'build.json': json.dumps(report).encode()}}
for path, data in everything.items():
    (output / path).parent.mkdir(parents=True, exist_ok=True)
    (output / path).write_bytes(data)
    if path.endswith('scripts/agent-workflow'):
        (output / path).chmod(0o755)
sums = ''.join(hashlib.sha256(data).hexdigest() + '  ' + path + '\n' for path, data in sorted(everything.items())
               if not (FAULT == 'checksum' and path == 'build.json'))
(output / 'SHA256SUMS').write_text(sums)
"""


class PackageBuild(unittest.TestCase):
    """Two builds of relocated read-only copies compared byte for byte, and the payload against the closure."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='package build ')
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(writable, self.base)

    def toolchain(self, fault=None):
        log = self.base / 'invocations.jsonl'
        log.unlink(missing_ok=True)
        cli = self.base / 'fake bin' / 'agent-workflow'
        cli.parent.mkdir(exist_ok=True)
        cli.write_text(FAKE_TOOLCHAIN.format(python=sys.executable, fault=fault, log=str(log)))
        cli.chmod(0o755)
        return cli, log

    def work(self, name):
        work = self.base / name
        work.mkdir()
        return work

    def invocations(self, log):
        return [json.loads(line)[:2] for line in log.read_text().splitlines()] if log.exists() else []

    def test_identical_builds_ship_exactly_the_closure_and_generated_assets(self):
        cli, log = self.toolchain()
        retained = self.base / 'retained'
        work = self.work('clean run')
        summary = build_check.verify(cli, work, output=retained)
        self.assertEqual(build_check.tree(retained), build_check.tree(work / 'build output 1'))
        build_check.check_output(retained, package.check()['descriptor'], package.ROOT)
        closure = json.loads((package.ROOT / dk_pr_fix.CLOSURE).read_text())['files']
        self.assertEqual(summary['payload_files'], len(closure) + 1 + len(build_check.GENERATED))
        self.assertFalse(summary['released'])
        self.assertEqual(summary['closure_digest'], package.check(copy_root(self.base / 'copy'))['closure_digest'])
        self.assertEqual(self.invocations(log), [['plugin', 'check'], ['plugin', 'build']] * 2)
        builds = [json.loads(line) for line in log.read_text().splitlines() if json.loads(line)[1] == 'build']
        # Different target order, paths with spaces and a different umask still yield the same bytes.
        self.assertEqual([args[3] for args in builds], ['codex,claude', 'claude,codex'])
        self.assertTrue(all(' ' in args[-1] and ' ' in args[5] for args in builds))

    def test_build_tree_rejects_links_to_directories(self):
        output = self.base / 'output'
        (output / 'plugin').mkdir(parents=True)
        (output / 'plugin' / 'file').write_text('x')
        self.assertEqual(set(build_check.tree(output)), {'plugin/file'})
        (output / 'plugin' / 'outside').symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(build_check.BuildCheckError, 'outside: not a regular file'):
            build_check.tree(output)

    def test_differences_and_undeclared_resources_reject(self):
        cases = {'check': ('plugin check rejected', 1, 0), 'timestamp': ('SHA256SUMS differs between builds', 2, 2),
                 'undeclared': ("undeclared ['notes.txt']", 2, 2), 'dropped': ("missing ['request.example.json']", 2, 2),
                 'altered': ('LICENSE: shipped bytes differ', 2, 2),
                 'guidance': ('drops the developer instructions', 2, 2),
                 'descriptor': ('generated resources differ', 2, 2),
                 'checksum': ('SHA256SUMS does not cover exactly', 2, 2),
                 'zip': ('ZIP does not hold exactly the marketplace', 2, 2)}
        for fault, (message, checks, builds) in cases.items():
            with self.subTest(fault):
                cli, log = self.toolchain(fault)
                retained = self.base / 'failed build'
                with self.assertRaises(build_check.BuildCheckError) as raised:
                    build_check.verify(cli, self.work(fault), output=retained)
                self.assertIn(message, str(raised.exception))
                self.assertFalse(retained.exists())
                calls = self.invocations(log)
                self.assertEqual((calls.count(['plugin', 'check']), calls.count(['plugin', 'build'])),
                                 (checks, builds))

    def test_unconfigured_toolchain_operations_fail(self):
        cli, log = self.toolchain()
        with self.assertRaisesRegex(build_check.BuildCheckError, 'unconfigured fake operation'):
            build_check.run([str(cli), 'plugin', 'test', '--json', str(self.base)], self.work('unconfigured'))
        self.assertEqual(self.invocations(log), [['plugin', 'test']])

    def test_existing_output_rejects_before_building(self):
        with self.assertRaisesRegex(build_check.BuildCheckError, 'output must not exist'):
            build_check.main(self.base / 'missing engine', output=self.base)

    @unittest.skipUnless(os.environ.get('CF_AGENT_WORKFLOW'),
                         'set CF_AGENT_WORKFLOW to a local agent-workflow clone with the pinned toolchain commit')
    def test_pinned_unreleased_toolchain_builds_identical_packages(self):
        summary = build_check.main(Path(os.environ['CF_AGENT_WORKFLOW']))
        self.assertEqual(summary['toolchain_commit'], build_check.TOOLCHAIN_COMMIT)
        self.assertEqual(summary['targets'], ['claude', 'codex'])
        self.assertFalse(summary['released'])


class InventoryStatus(unittest.TestCase):
    def descriptor(self):
        return package.validate_descriptor(package.parse_descriptor(DESCRIPTOR.encode()))

    def test_lists_only_delivered_rows_and_is_not_releasable(self):
        status = package.inventory_status(self.descriptor())
        self.assertEqual(status['listed'], [{'skill': 'cf-workflows:' + name, 'parity_status': 'not_implemented'}
                                            for name in ('dk-pr-fix', 'dk-pr-review', 'dk-pr-iter',
                                                         'dk-pr-feedback-read', 'dk-pr-feedback-address',
                                                         'dk-pr-feedback-reply',
                                                         'dk-pr-publish', 'dk-pr-review-post', 'dk-plain-english',
                                                         'dk-asd-ste100', 'dk-pr-decisions', 'dk-pr-reviewers-run',
                                                         'dk-code-simplify', 'dk-pr-approvers',
                                                         'dk-pr-description', 'dk-commit-message', 'dk-issue',
                                                         'dk-commit-split', 'dk-pr-split', 'dk-repo-onboard',
                                                         'dk-config', 'dk-preferences', 'dk-ci', 'dk-ci-approve',
                                                         'dk-ci-flakes', 'dk-issue-alternatives',
                                                         'dk-pr-alternatives', 'dk-alternatives-review',
                                                         'dk-pr-review-alternatives')])
        self.assertEqual(status['inventory_rows'], 29)
        self.assertFalse(status['releasable'])
        missing = {item['prerequisite'] for item in status['blockers']}
        self.assertEqual([item['prerequisite'] for item in status['blockers'][:3]],
                         ['engine release', 'helper state mount', 'plugin test container jobs'])
        blockers = {item['prerequisite']: item['missing'] for item in status['blockers']}
        self.assertIn('agent-workflow#149', blockers['helper state mount'])
        self.assertIn('agent-workflow#150', blockers['plugin test container jobs'])
        # Merged upstream capabilities are not listed as missing; only their release is.
        self.assertFalse({'agent-workflow#130', 'agent-workflow#131', 'agent-workflow#132',
                          'agent-workflow#133'} & missing)
        self.assertTrue({'engine release', 'helper state mount', 'plugin test container jobs', 'cf-workflows', 'cf-workflows:dk-pr-fix',
                         'cf-workflows:dk-pr-review', 'cf-workflows:dk-pr-iter', 'cf-workflows:dk-pr-feedback-read',
                         'cf-workflows:dk-pr-feedback-address', 'cf-workflows:dk-pr-feedback-reply',
                         'cf-workflows:dk-plain-english', 'cf-workflows:dk-asd-ste100',
                         'live acceptance: user_supplied_skill',
                         'live acceptance: live_provider', 'live acceptance: github_publication',
                         'compatibility', 'acceptance matrix'} <= missing)

    def test_unknown_rows_and_foreign_identities_reject(self):
        for change in ({'skills': [{'name': 'dk-everything', 'path': 'skills/x/SKILL.md',
                                    'workflow': 'workflows/main.yml'}]},
                       {'version': '5.17.0'}, {'name': 'dk-workflows'}):
            with self.subTest(change):
                descriptor = {**copy.deepcopy(self.descriptor()), **change}
                with self.assertRaises(package.PackageError):
                    package.inventory_status(descriptor)

    def test_status_cannot_become_releasable_by_editing_the_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / 'repo copy'
            (repository / 'baselines').mkdir(parents=True)
            sources = json.loads((ROOT / 'baselines/sources.json').read_text())
            sources['suite']['runnable'] = True
            inventory = json.loads((ROOT / 'baselines/inventory.json').read_text())
            for row in inventory['skills']:
                row['parity_status'] = 'implemented'
            (repository / 'baselines/sources.json').write_text(json.dumps(sources))
            (repository / 'baselines/inventory.json').write_text(json.dumps(inventory))
            matrix = json.loads((ROOT / 'baselines/acceptance-matrix.json').read_text())
            for row in matrix['rows']:
                row['status'].update(parity='implemented', native_execution='verified', live_acceptance='recorded')
            (repository / 'baselines/acceptance-matrix.json').write_text(json.dumps(matrix))
            status = package.inventory_status(self.descriptor(), repository)
            # Upstream toolchain and engine release blockers remain until actually consumed, and
            # matrix statuses without accepted live evidence invalidate the matrix instead of passing.
            self.assertFalse(status['releasable'])
            self.assertEqual([item['prerequisite'] for item in status['blockers']],
                             [name for name, _ in package.UPSTREAM_BLOCKERS] + ['acceptance matrix'])
            self.assertIn('native_execution must be pending', status['blockers'][-1]['missing'])


if __name__ == '__main__':
    unittest.main()
