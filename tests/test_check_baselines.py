"""Regression coverage for partial host verification and consumed upstream corrections, without touching source clones."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'check_baselines', Path(__file__).resolve().parents[1] / 'scripts/check_baselines.py')
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class HostManifestValidation(unittest.TestCase):
    def test_missing_or_extra_host_manifest_cannot_report_success(self):
        sources = json.loads((checker.ROOT / 'baselines/sources.json').read_text())
        inventory = (checker.ROOT / 'baselines/inventory.json').read_bytes()
        manifests = sources['sources'][0]['host_manifests']
        for invalid in ([], manifests[:1], manifests[1:], manifests + manifests[:1]):
            with self.subTest(manifests=invalid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'baselines').mkdir()
                sources['sources'][0]['host_manifests'] = invalid
                (root / 'baselines/sources.json').write_text(json.dumps(sources))
                (root / 'baselines/inventory.json').write_bytes(inventory)
                with patch.object(checker, 'ROOT', root), patch.object(checker, 'git') as git:
                    with self.assertRaisesRegex(ValueError, 'exact Claude and Codex'):
                        checker.check(Path('unused-dk'), Path('unused-engine'))
                    git.assert_not_called()


class UpstreamCorrections(unittest.TestCase):
    """A consumed correction must stay the exact upstream definition from a descendant commit."""

    def run_git(self, repo, *args):
        env = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'HOME': str(repo),
               'GIT_AUTHOR_NAME': 'F', 'GIT_AUTHOR_EMAIL': 'f@example.invalid',
               'GIT_COMMITTER_NAME': 'F', 'GIT_COMMITTER_EMAIL': 'f@example.invalid'}
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                              env=env).stdout.decode().strip()

    def commit(self, engine, text):
        (engine / 'guard.py').write_text(text)
        self.run_git(engine, 'add', 'guard.py')
        self.run_git(engine, 'commit', '-qm', 'guard')
        return self.run_git(engine, 'rev-parse', 'HEAD')

    def test_tampered_guard_or_unrelated_commit_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, engine = Path(tmp) / 'suite', Path(tmp) / 'engine'
            root.mkdir()
            engine.mkdir()
            self.run_git(engine, 'init', '-q')
            pin = self.commit(engine, 'def guard():\n    return True\n')
            fixed_text = 'def guard():\n    return False\n\n\ndef other():\n    pass\n'
            fix = self.commit(engine, fixed_text)
            (root / 'guard.py').write_text('# local\ndef guard():\n    return False\n\n\ndef other():\n    return 1\n')
            entry = {'path': 'guard.py', 'upstream': 'guard.py',
                     'upstream_sha256': hashlib.sha256(fixed_text.encode()).hexdigest(),
                     'symbols': ['guard'], 'adapted_symbols': {'other': 'Local change.'}}
            provenance = {'files': [{'path': 'guard.py', 'upstream': 'guard.py', 'change': 'adapted'}],
                          'corrections': [{'commit': fix, 'files': [entry]}]}
            with patch.object(checker, 'ROOT', root):
                checker.check_corrections(engine, pin, provenance)
                (root / 'guard.py').write_text('def guard():\n    return True\n\n\ndef other():\n    pass\n')
                with self.assertRaisesRegex(ValueError, 'guard differs from correction'):
                    checker.check_corrections(engine, pin, provenance)
                with self.assertRaisesRegex(ValueError, 'does not descend'):
                    checker.check_corrections(engine, fix, {**provenance, 'corrections': [{'commit': pin, 'files': [entry]}]})
                # A decorator is part of the definition the correction fixed.
                (root / 'guard.py').write_text('@staticmethod\ndef guard():\n    return False\n\n\ndef other():\n    pass\n')
                with self.assertRaisesRegex(ValueError, 'guard differs from correction'):
                    checker.check_corrections(engine, pin, provenance)
                provenance['files'][0]['change'] = 'verbatim'
                with self.assertRaisesRegex(ValueError, 'not adapted'):
                    checker.check_corrections(engine, pin, provenance)


if __name__ == '__main__':
    unittest.main()
