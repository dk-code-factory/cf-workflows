"""Plugin descriptor, declared resource closure and honest inventory status.

Every case checks disposable copies of the plugin root, including read-only
copies under paths with spaces. Nothing is built, installed, downloaded or run;
these results do not certify any engine, host or package.
"""
import copy
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
                                                 'workflow': 'workflows/review.yml'}])
        # Plugin and engine versions are independent bindings.
        self.assertNotEqual(descriptor['version'], descriptor['engine-version'])

    def test_invalid_descriptors_reject(self):
        cases = {
            'unknown field': DESCRIPTOR + 'launcher: scripts/run\n',
            'duplicate key': DESCRIPTOR + 'name: other\n',
            'version range': DESCRIPTOR.replace('engine-version: 0.2.2', 'engine-version: ^0.2.2'),
            'v prefix': DESCRIPTOR.replace('engine-version: 0.2.2', 'engine-version: v0.2.2'),
            'leading zero prerelease': DESCRIPTOR.replace('version: 0.1.0-dev', 'version: 0.1.0-01'),
            'one host': DESCRIPTOR.replace('[codex, claude]', '[codex]'),
            'unknown host': DESCRIPTOR.replace('[codex, claude]', '[codex, claude, cursor]'),
            'duplicate host': DESCRIPTOR.replace('[codex, claude]', '[codex, codex]'),
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
                                'normalization collision'):
                    regenerate(root)  # Even a refreshed closure.json cannot admit the change.
                with self.assertRaises(package.PackageError) as raised:
                    package.check(root)
                self.assertIn(path, str(raised.exception))

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


class InventoryStatus(unittest.TestCase):
    def descriptor(self):
        return package.validate_descriptor(package.parse_descriptor(DESCRIPTOR.encode()))

    def test_lists_only_delivered_rows_and_is_not_releasable(self):
        status = package.inventory_status(self.descriptor())
        self.assertEqual(status['listed'], [{'skill': 'cf-workflows:dk-pr-fix', 'parity_status': 'not_implemented'},
                                            {'skill': 'cf-workflows:dk-pr-review', 'parity_status': 'not_implemented'}])
        self.assertEqual(status['inventory_rows'], 29)
        self.assertFalse(status['releasable'])
        missing = {item['prerequisite'] for item in status['blockers']}
        self.assertTrue({'agent-workflow#130', 'agent-workflow#131', 'agent-workflow#132', 'agent-workflow#133',
                         'engine release', 'cf-workflows', 'cf-workflows:dk-pr-fix',
                         'cf-workflows:dk-pr-review', 'live acceptance: user_supplied_skill',
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
