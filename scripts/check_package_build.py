#!/usr/bin/env python3
"""Build the cf-workflows packages twice with Agent Workflow's plugin toolchain and compare them.

Development verification against exact, unreleased Agent Workflow source. It
builds the CLI from TOOLCHAIN_COMMIT of a local source clone (never fetching,
checking out or mutating it), runs `plugin check` on two read-only copies of
the plugin root under different paths with spaces, builds both host targets
from each copy, and requires:

- byte-identical SHA256SUMS, ZIP, build.json and marketplace trees;
- checksums that cover exactly the build report, ZIP and marketplace files;
- a ZIP that holds exactly the marketplace tree;
- a runtime payload that is exactly the declared closure (closure.json and the
  files it lists) plus the named generated assets, with every declared file
  shipped byte for byte except the descriptor and skill entry points, which the
  toolchain regenerates around the developer bytes.

Passing establishes no released engine, supported host, release or
certification: the source is unreleased and nothing is installed or run.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import dk_pr_fix, package  # noqa: E402

# agent-workflow main with #129-#133, #138 and #139 merged; no release contains it.
TOOLCHAIN_COMMIT = '87587758fdb3633db744aa54d0380f31774fb1e3'
# Runtime payload files the toolchain generates; none exists in the source root.
GENERATED = ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json', 'compatibility.json', 'plugin.json',
             'scripts/agent-workflow', 'scripts/engine_bootstrap.py')
# Host catalogs beside the payload in the marketplace directory.
CATALOGS = ('.agents/plugins/marketplace.json', '.claude-plugin/marketplace.json')
# Fixed builder environment: offline module resolution, no workspace or VCS stamping.
GO_ENV = {'GOFLAGS': '-mod=readonly -trimpath -buildvcs=false', 'GOPROXY': 'off', 'GOWORK': 'off',
          'CGO_ENABLED': '0'}


class BuildCheckError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise BuildCheckError(message)


def run(argv, work, umask=0o022, accept=(0,)):
    """Run argv with a scratch HOME; returns stdout bytes or raises with the diagnostic."""
    environment = {'PATH': os.environ.get('PATH', ''), 'HOME': str(work / 'home'), 'LANG': 'C.UTF-8'}
    (work / 'home').mkdir(exist_ok=True)
    completed = subprocess.run(argv, cwd=work, env=environment, capture_output=True, umask=umask, timeout=600)
    if completed.returncode not in accept:
        raise BuildCheckError(f'{" ".join(map(str, argv[1:3]))} failed ({completed.returncode}): '
                              + (completed.stderr or completed.stdout).decode(errors='replace').strip())
    return completed.stdout


def build_cli(engine, work, commit=TOOLCHAIN_COMMIT):
    """Build cmd/agent-workflow from `git archive` of the exact commit; the clone is only read."""
    engine = Path(engine)
    kind = subprocess.run(['git', '-C', str(engine), 'cat-file', '-t', commit + '^{commit}'],
                          capture_output=True).stdout.strip()
    require(kind == b'commit', f'{engine}: missing toolchain commit {commit}; fetch it into the clone first')
    archive = subprocess.run(['git', '-C', str(engine), 'archive', '--format=tar', commit],
                             capture_output=True, check=True).stdout
    source = work / 'agent-workflow source'
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(source, filter='data')
    cli = work / 'bin' / 'agent-workflow'
    environment = {**os.environ, 'HOME': os.environ.get('HOME', str(work)), **GO_ENV}
    completed = subprocess.run(['go', 'build', '-o', str(cli), './cmd/agent-workflow'], cwd=source,
                               env=environment, capture_output=True, timeout=600)
    require(completed.returncode == 0, 'go build failed (module cache and Go toolchain must be available '
            'offline): ' + completed.stderr.decode(errors='replace').strip())
    return cli


def relocate(destination, mtime=None):
    """A read-only copy of the plugin root; mtime, when given, is applied to every file."""
    shutil.copytree(package.ROOT, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for directory, _, files in os.walk(destination):
        for name in files:
            if mtime is not None:
                os.utime(Path(directory) / name, (mtime, mtime))
            os.chmod(Path(directory) / name, os.stat(Path(directory) / name).st_mode & 0o555)
        os.chmod(directory, 0o555)
    return destination


def writable(root):
    for directory, _, _ in os.walk(root):
        os.chmod(directory, 0o755)


def tree(root):
    """{relative path: (bytes, executable)} of every regular file; links and special files reject."""
    files = {}
    for directory, names, entries in os.walk(root):
        names.sort()
        for name in names:  # os.walk lists a link to a directory here and never descends into it.
            require(not os.path.islink(Path(directory) / name), f'{Path(directory) / name}: not a regular file')
        for name in sorted(entries):
            path = Path(directory) / name
            mode = os.lstat(path).st_mode
            require(stat.S_ISREG(mode), f'{path}: not a regular file')
            files[path.relative_to(root).as_posix()] = (path.read_bytes(), bool(mode & stat.S_IXUSR))
    return files


def check_plugin(cli, root, work):
    output = run([str(cli), 'plugin', 'check', '--json', str(root)], work, accept=(0, 1))
    try:
        result = json.loads(output)
    except ValueError:
        raise BuildCheckError('plugin check printed no JSON report') from None
    require(result.get('passed') is True and result.get('diagnostics') == [],
            f'plugin check rejected {root}: {json.dumps(result.get("diagnostics"))}')


def build(cli, root, output, targets, work, umask):
    run([str(cli), 'plugin', 'build', '--target', targets, '--output', str(output), str(root)], work, umask)
    return output


def compare_builds(first, second):
    """Byte-for-byte identity of the reports, ZIP and the whole output trees."""
    report = json.loads((first / 'build.json').read_bytes())
    for name in ('SHA256SUMS', report['archive'], 'build.json'):
        require((first / name).read_bytes() == (second / name).read_bytes(), f'{name} differs between builds')
    left, right = tree(first), tree(second)
    require(left.keys() == right.keys(), 'build outputs list different files: '
            + str(sorted(left.keys() ^ right.keys())))
    changed = sorted(path for path in left if left[path] != right[path])
    require(not changed, 'build outputs differ in ' + str(changed))
    return report


def check_output(output, descriptor, source):
    """Checksums, ZIP and payload of one build against the descriptor and the source plugin root."""
    report = json.loads((output / 'build.json').read_bytes())
    name, version = descriptor['name'], descriptor['version']
    require((report.get('name'), report.get('version'), report.get('engine_version'), report.get('targets'),
             report.get('archive')) == (name, version, descriptor['engine-version'], sorted(descriptor['hosts']),
                                        f'{name}-{version}.zip'), 'build.json does not match the descriptor')
    files = tree(output)
    sums = {}
    for line in files.pop('SHA256SUMS')[0].decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        require(match and match[2] not in sums, 'malformed SHA256SUMS line ' + json.dumps(line))
        sums[match[2]] = match[1]
    require(set(sums) == set(files), 'SHA256SUMS does not cover exactly the build output: '
            + str(sorted(set(sums) ^ set(files))))
    for path, digest in sums.items():
        require(hashlib.sha256(files[path][0]).hexdigest() == digest, path + ': checksum mismatch')
    marketplace = {path: entry for path, entry in files.items() if path.startswith(name + '/')}
    require(set(files) - set(marketplace) == {'build.json', report['archive']},
            'unexpected file beside the marketplace: ' + str(sorted(set(files) - set(marketplace))))
    with zipfile.ZipFile(io.BytesIO(files[report['archive']][0])) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}
    require(members == {path: entry[0] for path, entry in marketplace.items()},
            'the ZIP does not hold exactly the marketplace tree')
    prefix = f'{name}/plugins/{name}/'
    payload = {path[len(prefix):]: entry for path, entry in marketplace.items() if path.startswith(prefix)}
    require(set(marketplace) - {prefix + path for path in payload} == {f'{name}/{path}' for path in CATALOGS},
            'the marketplace holds files other than the payload and both host catalogs')
    check_payload(payload, source, descriptor)
    return report, payload


def check_payload(payload, source, descriptor):
    """Payload minus the generated assets is exactly the declared closure, shipped unchanged."""
    closure = json.loads((source / dk_pr_fix.CLOSURE).read_bytes())['files']
    declared = set(closure) | {dk_pr_fix.CLOSURE}
    missing = sorted(set(GENERATED) - set(payload))
    require(not missing, 'generated assets missing from the payload: ' + str(missing))
    shipped = set(payload) - set(GENERATED)
    require(shipped == declared, 'payload differs from closure.json: undeclared '
            + str(sorted(shipped - declared)) + ', missing ' + str(sorted(declared - shipped)))
    rewritten = {package.DESCRIPTOR} | {skill['path'] for skill in descriptor['skills']}
    for path in sorted(declared):
        data, executable = payload[path]
        original = (source / path).read_bytes()
        require(executable == bool(os.stat(source / path).st_mode & stat.S_IXUSR), path + ': executable bit changed')
        if path not in rewritten:
            require(data == original, path + ': shipped bytes differ from the source')
        elif path != package.DESCRIPTOR:
            require(data.startswith(original), path + ': generated entry point drops the developer instructions')
    # The regenerated descriptor keeps the identity and declares exactly the payload.
    text = payload[package.DESCRIPTOR][0].decode()
    for key in ('name', 'version', 'engine-version'):
        require(re.search(rf'^{key}: {re.escape(descriptor[key])}$', text, re.M),
                f'{package.DESCRIPTOR}: generated descriptor changed {key}')
    resources = re.search(r'^resources:\n((?:[ ]+- .+\n?)*)', text, re.M)
    require(resources, f'{package.DESCRIPTOR}: generated descriptor has no resource list')
    listed = {line.strip()[2:] for line in resources[1].splitlines()}
    files = set(payload) - {package.DESCRIPTOR}
    parents = {str(parent) for path in files for parent in PurePosixPath(path).parents if str(parent) != '.'}
    require(files <= listed <= files | parents, f'{package.DESCRIPTOR}: generated resources differ from the payload')


def verify(cli, work):
    """Check and build two relocated read-only copies; returns a summary or raises BuildCheckError."""
    work = Path(work)
    first = relocate(work / 'first copy' / 'cf workflows')
    second = relocate(work / 'second copy' / 'plugin root', mtime=1)
    try:
        source = package.check(first)
        outputs = []
        for number, (root, targets, umask) in enumerate(((first, 'codex,claude', 0o022),
                                                         (second, 'claude,codex', 0o077)), 1):
            require(package.check(root)['closure_digest'] == source['closure_digest'], 'relocated copy differs')
            check_plugin(cli, root, work)
            outputs.append(build(cli, root, work / f'build output {number}', targets, work, umask))
        report = compare_builds(*outputs)
        _, payload = check_output(outputs[0], source['descriptor'], first)
        for root in (first, second):
            require(package.check(root)['closure_digest'] == source['closure_digest'], 'the build changed the source')
    finally:
        writable(first.parent.parent)
    return {'schema_version': 1, 'kind': 'package_build_check', 'toolchain_commit': TOOLCHAIN_COMMIT,
            'released': False, 'targets': report['targets'], 'engine_version': report['engine_version'],
            'closure_digest': source['closure_digest'], 'source_digest': report['source_digest'],
            'package_digest': report['package_digest'], 'payload_files': len(payload),
            'generated': list(GENERATED)}


def main(engine, commit=TOOLCHAIN_COMMIT):
    with tempfile.TemporaryDirectory(prefix='cf package build ') as temp:
        work = Path(temp)
        try:
            return verify(build_cli(engine, work, commit), work)
        finally:
            writable(work)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--agent-workflow', required=True, type=Path,
                        help='local agent-workflow clone that contains the toolchain commit')
    args = parser.parse_args()
    try:
        print(json.dumps(main(args.agent_workflow), indent=2))
    except (BuildCheckError, package.PackageError) as error:
        print(json.dumps({'schema_version': 1, 'kind': 'package_build_check', 'error': str(error)}, indent=2))
        sys.exit(1)
