"""Regression coverage for partial host verification, without touching source clones."""
import importlib.util
import json
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
