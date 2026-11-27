#!/usr/bin/env python3
"""Check a release tag against the suite identity and assemble the release assets.

The tag must be `v` plus the plugin version, which is also the suite version in
baselines/sources.json. A final version (no prerelease part) is published only
when the package check reports `releasable: true`. A prerelease version is a
source-only development snapshot: it ships the tagged source, the package check
report and checksums, and its notes list every open release blocker. Nothing
here builds or publishes a plugin package.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cf_workflows import package  # noqa: E402


class ReleaseError(Exception):
    pass


def gate(tag, report):
    """Return (version, prerelease) for `tag`, or raise ReleaseError when it may not be released."""
    if 'error' in report:
        raise ReleaseError(f'package check failed: {report["error"]["path"]}: {report["error"]["message"]}')
    version = report['version']
    if tag != f'v{version}':
        raise ReleaseError(f'tag {tag} does not match the plugin version v{version}')
    prerelease = '-' in version.split('+', 1)[0]
    if not prerelease and not report['releasable']:
        listed = '\n'.join(f'  - {item["prerequisite"]}: {item["missing"]}' for item in report['blockers'])
        raise ReleaseError(f'{version} is a final version but the package check reports releasable: false\n{listed}')
    return version, prerelease


def notes(report, prerelease, commit, repository='dk-code-factory/cf-workflows'):
    docs = f'https://github.com/{repository}/blob/{commit}/docs'
    lines = [f'cf-workflows {report["version"]} (`{commit}`).', '']
    if prerelease:
        lines += ['**Development snapshot.** Source only: no plugin package is built or published, the suite '
                  'is not runnable, and no host/provider/engine combination is verified. See '
                  f'[packaging]({docs}/packaging.md#what-remains-blocked) and the '
                  f'[acceptance matrix]({docs}/qualification.md).', '']
    lines += [f'- Plugin closure digest: `{report["closure_digest"]}` ({report["files"]} files)',
              f'- Pinned engine version: `{report["engine_version"]}`',
              f'- Hosts: {", ".join(report["hosts"])}',
              f'- Listed skills: {len(report["listed"])} of {report["inventory_rows"]} inventory rows',
              f'- Releasable: `{str(report["releasable"]).lower()}`']
    if report['blockers']:
        lines += ['', f'## Open release blockers ({len(report["blockers"])})', '']
        lines += [f'- `{item["prerequisite"]}`: {item["missing"]}' for item in report['blockers']]
    return '\n'.join(lines) + '\n'


def assemble(tag, out, repository=ROOT):
    """Gate `tag` at HEAD and write the release assets into `out`; returns the summary."""
    try:
        report = package.report(repository=repository)
    except package.PackageError as error:
        report = {'error': {'path': error.path, 'message': error.message}}
    version, prerelease = gate(tag, report)

    def git(*args):
        return subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True).stdout

    commit = git('rev-parse', 'HEAD').decode().strip()
    tagged = git('rev-parse', f'{tag}^{{commit}}').decode().strip()
    if tagged != commit:
        raise ReleaseError(f'tag {tag} points at {tagged}, not the checked-out HEAD {commit}')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    name = f'cf-workflows-{version}'
    assets = {
        f'{name}.tar.gz': git('archive', '--format=tar.gz', f'--prefix={name}/', commit),
        f'{name}-package-check.json': (json.dumps(report, indent=2) + '\n').encode(),
    }
    for asset, data in assets.items():
        (out / asset).write_bytes(data)
    sums = ''.join(f'{hashlib.sha256(data).hexdigest()}  {asset}\n' for asset, data in sorted(assets.items()))
    (out / 'SHA256SUMS').write_text(sums)
    (out / 'release-notes.md').write_text(
        notes(report, prerelease, commit, os.environ.get('GITHUB_REPOSITORY', 'dk-code-factory/cf-workflows')))
    return {'tag': tag, 'version': version, 'prerelease': prerelease, 'commit': commit,
            'releasable': report['releasable'], 'assets': sorted(assets) + ['SHA256SUMS']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tag', required=True, help='release tag, v<plugin version>')
    parser.add_argument('--out', required=True, help='directory for the release assets and notes')
    args = parser.parse_args(argv)
    try:
        summary = assemble(args.tag, args.out)
    except (ReleaseError, subprocess.CalledProcessError) as error:
        print(f'release gate: {error}', file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
