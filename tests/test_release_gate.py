import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_gate', ROOT / 'scripts/release_gate.py')
release_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_gate)

BLOCKER = {'prerequisite': 'agent-workflow', 'missing': 'no released engine'}


def report(version, releasable=False):
    return {'version': version, 'releasable': releasable, 'blockers': [] if releasable else [BLOCKER],
            'closure_digest': 'd' * 64, 'files': 3, 'engine_version': '0.2.2', 'hosts': ['codex', 'claude'],
            'listed': [], 'inventory_rows': 29}


class Gate(unittest.TestCase):
    def test_prerelease_is_a_snapshot_even_while_blocked(self):
        self.assertEqual(release_gate.gate('v0.1.0-dev', report('0.1.0-dev')), ('0.1.0-dev', True))

    def test_final_version_needs_releasable(self):
        with self.assertRaisesRegex(release_gate.ReleaseError, 'releasable: false\n  - agent-workflow'):
            release_gate.gate('v0.1.0', report('0.1.0'))
        self.assertEqual(release_gate.gate('v0.1.0', report('0.1.0', releasable=True)), ('0.1.0', False))

    def test_build_metadata_is_not_a_prerelease(self):
        with self.assertRaisesRegex(release_gate.ReleaseError, 'final version'):
            release_gate.gate('v0.1.0+build-1', report('0.1.0+build-1'))

    def test_tag_must_be_the_plugin_version(self):
        for tag in ('0.1.0-dev', 'v0.1.0', 'v0.1.0-dev.1'):
            with self.subTest(tag=tag), self.assertRaisesRegex(release_gate.ReleaseError, 'does not match'):
                release_gate.gate(tag, report('0.1.0-dev'))

    def test_failed_package_check_rejects(self):
        with self.assertRaisesRegex(release_gate.ReleaseError, 'package check failed: plugin.yaml: bad'):
            release_gate.gate('v0.1.0-dev', {'error': {'path': 'plugin.yaml', 'message': 'bad'}})

    def test_snapshot_notes_list_every_blocker(self):
        text = release_gate.notes(report('0.1.0-dev'), True, 'abc')
        self.assertIn('Development snapshot', text)
        self.assertIn('- `agent-workflow`: no released engine', text)
        self.assertIn('Releasable: `false`', text)
        self.assertIn('https://github.com/dk-code-factory/cf-workflows/blob/abc/docs/packaging.md', text)

    def test_current_tree_gates_as_a_blocked_snapshot(self):
        from cf_workflows import package
        current = package.report()
        self.assertEqual(release_gate.gate(f'v{current["version"]}', current)[1], '-' in current['version'])
        if not current['releasable']:
            self.assertTrue(current['blockers'])


if __name__ == '__main__':
    unittest.main()
