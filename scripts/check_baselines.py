#!/usr/bin/env python3
"""Verify committed inventory against exact local Git objects; never fetch or execute them."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'dk-pr-iter', 'dk-pr-fix', 'dk-pr-review', 'dk-repo-onboard', 'dk-config',
    'dk-preferences', 'dk-issue-alternatives', 'dk-pr-alternatives',
    'dk-alternatives-review', 'dk-pr-review-alternatives', 'dk-plain-english',
    'dk-asd-ste100', 'dk-code-simplify', 'dk-pr-reviewers-run', 'dk-pr-approvers',
    'dk-pr-feedback-read', 'dk-pr-feedback-address', 'dk-pr-feedback-reply',
    'dk-pr-review-post', 'dk-pr-description', 'dk-pr-decisions', 'dk-commit-split',
    'dk-pr-split', 'dk-pr-publish', 'dk-ci-approve', 'dk-ci', 'dk-ci-flakes',
    'dk-commit-message', 'dk-issue',
}


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check(dk, engine):
    sources = json.loads((ROOT / 'baselines/sources.json').read_text())
    inventory = json.loads((ROOT / 'baselines/inventory.json').read_text())
    baseline, portable = sources['sources']
    sha = baseline['commit']
    require(sha == inventory['baseline_commit'], 'inventory/source pin mismatch')
    require(len(inventory['skills']) == 29, 'inventory must contain exactly 29 rows')
    require({row['name'] for row in inventory['skills']} == EXPECTED,
            'missing, duplicate or unexpected inventory skill')
    for host, manifest_path in zip(('claude', 'codex'), baseline['host_manifests']):
        manifest = json.loads(git(dk, 'show', f'{sha}:{manifest_path}'))
        require(manifest['version'] == '5.17.0', f'{host} manifest version mismatch')
        require(manifest['name'] == 'dk-workflows', f'{host} manifest identity mismatch')
        require(manifest['skills'] == f'./{host}/skills/', f'{host} skill root mismatch')
        paths = git(dk, 'ls-tree', '-r', '--name-only', sha, f'{host}/skills/').decode().splitlines()
        names = {p.split('/')[2] for p in paths if p.endswith('/SKILL.md')}
        require(names == EXPECTED, f'{host} pinned skill directories differ')
    for row in inventory['skills']:
        for key in ('inputs', 'outputs', 'permissions', 'dependencies', 'completion',
                    'intentional_differences', 'parity_status'):
            require(bool(row[key]), f'{row["name"]}: empty {key}')
        require(row['entrypoint'] == 'cf-workflows:' + row['name'], 'wrong namespace')
        require(row['parity_status'] == 'not_implemented', 'contracts PR cannot claim parity')
        paths = {item['path']: item['sha256'] for item in row['baseline_resources']}
        expected_paths = set()
        for host in ('codex', 'claude'):
            entry = f'{host}/skills/{row["name"]}/SKILL.md'
            text = git(dk, 'show', f'{sha}:{entry}').decode()
            expected_paths.add(entry)
            for link in re.findall(r'\]\(([^)]+)\)', text):
                if '://' in link or link.startswith('#'):
                    continue
                target = (ROOT / entry).parent / link.split('#')[0]
                resolved = target.resolve().relative_to(ROOT)
                expected_paths.add(str(resolved))
        require(set(paths) == expected_paths, f'{row["name"]}: direct resource inventory differs')
        for path, digest in paths.items():
            actual = hashlib.sha256(git(dk, 'show', f'{sha}:{path}')).hexdigest()
            require(actual == digest, f'{path}: resource digest mismatch')
    for source, repo in ((baseline, dk), (portable, engine)):
        require(git(repo, 'cat-file', '-t', source['commit']).strip() == b'commit', 'missing pin')
        require((ROOT / source['license']).read_bytes() ==
                git(repo, 'show', f'{source["commit"]}:LICENSE'), 'license differs from pin')
    git(engine, 'show', f'{portable["commit"]}:examples/pr-fix/workflows/main.yml')
    git(engine, 'show', f'{portable["commit"]}:docs/PR_FIX.md')
    require((ROOT / 'licenses/architecture-designer-MIT.txt').read_bytes() ==
            git(dk, 'show', f'{sha}:docs/licenses/architecture-designer-MIT.txt'),
            'adapted third-party notice differs from pin')
    # Check every repository-relative Markdown file target; remote links are provenance only.
    for file in ROOT.rglob('*.md'):
        if '.git' in file.parts:
            continue
        for link in re.findall(r'\]\(([^)]+)\)', file.read_text()):
            if '://' in link or link.startswith('#'):
                continue
            target = (file.parent / link.split('#')[0]).resolve()
            target.relative_to(ROOT)
            require(target.is_file(), f'{file.relative_to(ROOT)}: broken link {link}')
    print('Verified 29 distinct rows, both pinned host manifests/entries, direct resource hashes, '
          'portable source paths, license notices and local Markdown file targets.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dk-workflows', required=True, type=Path)
    parser.add_argument('--agent-workflow', required=True, type=Path)
    args = parser.parse_args()
    check(args.dk_workflows, args.agent_workflow)
