import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cf_workflows import package
from scripts import check_package_build as build_check
from tests.test_package import FAKE_TOOLCHAIN

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

    def test_numbered_version_needs_a_verified_package_not_runtime_qualification(self):
        for qualified in (False, True):
            current = report('0.1.1', releasable=qualified)
            with self.subTest(qualified=qualified):
                with self.assertRaisesRegex(release_gate.ReleaseError, 'requires a verified plugin package'):
                    release_gate.gate('v0.1.1', current)
                self.assertEqual(release_gate.gate('v0.1.1', current, packaged=True), ('0.1.1', False))

    def test_build_metadata_is_not_a_prerelease(self):
        with self.assertRaisesRegex(release_gate.ReleaseError, 'numbered release'):
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
        self.assertIn('Prerelease', text)
        self.assertIn('- `agent-workflow`: no released engine', text)
        self.assertIn('Runtime qualified (`package_check.releasable`): `false`', text)
        self.assertIn('https://github.com/dk-code-factory/cf-workflows/blob/abc/docs/packaging.md', text)

    def test_numbered_package_notes_keep_runtime_blockers(self):
        text = release_gate.notes(report('0.1.1'), False, 'abc', packaged=True)
        self.assertIn('installable Codex/Claude marketplace', text)
        self.assertIn('Installation does not enable workflow execution', text)
        self.assertIn('is not runnable', text)
        self.assertIn('- `agent-workflow`: no released engine', text)
        self.assertIn(build_check.TOOLCHAIN_COMMIT, text)
        self.assertIn('/docs/installation.md', text)
        self.assertIn('Runtime qualification pending', text)
        self.assertNotIn('Prerelease', text)

    def test_qualified_package_notes_do_not_invent_runtime_blockers(self):
        text = release_gate.notes(report('0.1.1', releasable=True), False, 'abc', packaged=True)
        self.assertIn('Runtime qualified (`package_check.releasable`): `true`', text)
        self.assertNotIn('is not runnable', text)
        self.assertNotIn('Runtime qualification blockers', text)

    def test_current_tree_gates_as_a_numbered_package_with_runtime_gaps(self):
        from cf_workflows import package
        current = package.report()
        self.assertEqual(release_gate.gate(f'v{current["version"]}', current, packaged=True), ('0.1.1', False))
        if not current['releasable']:
            self.assertTrue(current['blockers'])


class ReleaseAssets(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release assets ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.addCleanup(build_check.writable, self.base)
        cli = self.base / 'toolchain'
        cli.write_text(FAKE_TOOLCHAIN.format(python=sys.executable, fault=None,
                                           log=str(self.base / 'calls.jsonl')))
        cli.chmod(0o755)
        work = self.base / 'work'
        work.mkdir()
        self.built = self.base / 'packages'
        build_check.verify(cli, work, output=self.built)
        self.version = package.report()['version']

    def assemble(self, out, package_dir=None):
        def git(argv, **kwargs):
            if argv[3] == 'rev-parse':
                return SimpleNamespace(stdout=b'a' * 40 + b'\n')
            if argv[3] == 'archive':
                return SimpleNamespace(stdout=b'tagged source archive')
            self.fail(f'unconfigured git command: {argv}')
        with patch.object(release_gate.subprocess, 'run', side_effect=git):
            return release_gate.assemble(f'v{self.version}', out, package_dir=package_dir)

    def test_verified_zip_and_report_join_source_assets_and_checksums(self):
        out = self.base / 'release'
        summary = self.assemble(out, self.built)
        archive = f'cf-workflows-{self.version}.zip'
        build_report = f'cf-workflows-{self.version}-build.json'
        self.assertEqual((out / archive).read_bytes(), (self.built / archive).read_bytes())
        self.assertEqual(json.loads((out / build_report).read_bytes()),
                         json.loads((self.built / 'build.json').read_bytes()))
        sums = dict(line.split('  ')[::-1] for line in (out / 'SHA256SUMS').read_text().splitlines())
        self.assertEqual(set(sums), set(summary['assets']) - {'SHA256SUMS'})
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)
        self.assertFalse(summary['releasable'])
        self.assertFalse(summary['prerelease'])
        self.assertTrue(summary['distribution_ready'])
        self.assertIn('installable Codex/Claude marketplace', (out / 'release-notes.md').read_text())

    def test_corrupt_package_rejects_before_any_asset_is_written(self):
        archive = self.built / f'cf-workflows-{self.version}.zip'
        archive.write_bytes(archive.read_bytes() + b'corrupt')
        out = self.base / 'rejected'
        with self.assertRaisesRegex(build_check.BuildCheckError, 'checksum mismatch'):
            self.assemble(out, self.built)
        self.assertFalse(out.exists())

    def test_output_cannot_retain_assets_from_an_older_release(self):
        out = self.base / 'existing'
        out.mkdir()
        (out / 'old.zip').write_bytes(b'old release')
        with self.assertRaisesRegex(release_gate.ReleaseError, 'output must be empty'):
            self.assemble(out, self.built)
        self.assertEqual(list(out.iterdir()), [out / 'old.zip'])

    def test_source_only_numbered_release_rejects_before_writing(self):
        out = self.base / 'source-only'
        with self.assertRaisesRegex(release_gate.ReleaseError, 'requires a verified plugin package'):
            self.assemble(out)
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
