"""Upgrade, backup and restore of the suite's retained state (contract v1, #37).

The suite state home is `$XDG_STATE_HOME/cf-workflows`. It holds the configuration
ledger, the prose ledgers and one namespace per repository with the logical-run,
feedback, decision, alignment and drafting ledgers, the writer lease and the
content-addressed artifacts, requests and preparation checkpoints. The saved
configuration is `$XDG_CONFIG_HOME/cf-workflows/configuration.json`.

- `audit()` classifies every retained file against the catalog below. Each table
  of each store has a stated retention purpose; anything else is refused, so no
  transcript, rendered copy or foreign state enters a backup unnoticed.
- `backup()` takes one consistent snapshot of every SQLite store while writers
  may continue: read transactions are opened on all stores and kept only if no
  store changed between the first and the last, so the copies show one instant.
  The engine's database backup and recovery bundle are taken first, while its
  controller is stopped, and named by digest in the same manifest.
- `restore()` verifies the whole backup before it changes anything, never merges
  into existing state and never imports another suite's settings or state. It
  reports which logical runs can resume with their original admitted assets.
- `upgrade()` migrates every store under a journal with one retained pre-upgrade
  backup; an interrupted upgrade resumes and never replaces that backup.
- `cost()` keeps fixture proxies apart from provider-reported dollar cost.

The engine side is reached through an adapter. `Engine` binds it to the
`agent-workflow stop|backup|bundle|restore|resume` CLI of Agent Workflow main
8758775 and reads the retained admitted helpers from the bundle's `assets` trees;
no released engine has these commands yet.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile

from cf_workflows import admission, dk_pr_fix

SUITE_DIR = 'cf-workflows'
FORMAT = 'cf-workflows-state-backup-v1'
UPGRADE_FORMAT = 'cf-workflows-upgrade-v1'
JOURNAL = 'upgrade.json'
UPGRADES = 'upgrades'
MARKER = dk_pr_fix.STATE_MARKER
NAMESPACE = r'repositories/[0-9a-f]{32}/'
RUN = NAMESPACE + r'runs/[0-9a-f]{64}'
IDENTITY = 'identity: suite, kind and binding of this record'

# Every SQLite store, the schema version this release writes, the versions it can
# migrate, and why each table is retained (contract: minimal retention).
STORES = {
    'configuration': {
        'path': r'configuration\.sqlite3', 'kind': 'configuration_state', 'version': 1,
        'tables': {'meta': IDENTITY,
                   'acceptances': 'analysis: semantic acceptance per unit with its native result reference',
                   'assessments': 'analysis: the units each accepted session judged together',
                   'questions': 'recovery: a clarification verdict retained before its gate opens',
                   'writes': 'recovery: configuration write intents, dispatch counts and receipts'}},
    'prose': {
        'path': r'prose/[0-9a-f]{32}\.sqlite3', 'kind': 'prose_state', 'version': 1,
        'tables': {'meta': IDENTITY,
                   'passes': 'recovery: each claimed pass, its intent and accepted result',
                   'units': 'analysis: accepted per-file outputs that unchanged reruns reuse'}},
    'run': {
        'path': RUN + r'\.sqlite', 'kind': None, 'version': 3, 'migrates': (1, 2),
        'tables': {'groups': 'identity: the logical run and its PR',
                   'reservations': 'evidence: consumed allowances and their original ceilings',
                   'deadlines': 'evidence: absolute deadlines and their original starts',
                   'extensions': 'evidence: explicit continuation grants with their authorization',
                   'claims': 'recovery: occurrence claims, assignments and accepted coverage',
                   'submissions': 'evidence: each submitted native report',
                   'report_rejections': 'evidence: rejected reports and why',
                   'accepted_reports': 'analysis: accepted reports reused while their fingerprint holds',
                   'operations': 'recovery: replay-safe helper operation results',
                   'publication_intents': 'recovery: exact publication intents, states and receipts',
                   'publication_aliases': 'recovery: equivalent intent identities',
                   'publication_events': 'evidence: append-only publication history',
                   'publication_effects': 'recovery: unique observed external effects',
                   'publication_lock': 'recovery: the one unsettled publication',
                   'local_commits': 'recovery: local commit intents',
                   'native_failures': 'evidence: classified native failures',
                   'review_rounds': 'recovery: the round basis each review claim charged',
                   'ci_observations': 'evidence: exact-head CI observations'}},
    'feedback': {
        'path': RUN + r'\.feedback\.sqlite', 'kind': 'feedback_state', 'version': 1,
        'tables': {'meta': IDENTITY, 'sources': 'recovery: feedback source identities',
                   'assessments': 'analysis: implementation dispositions and response obligations',
                   'replies': 'evidence: answering replies and resolution'}},
    'decisions': {
        'path': RUN + r'\.decisions\.sqlite', 'kind': 'decisions_state', 'version': 1,
        'tables': {'meta': IDENTITY, 'results': 'analysis: sourced decision results',
                   'rejections': 'evidence: rejected session results'}},
    'reconciliation': {
        'path': RUN + r'\.reconciliation\.sqlite', 'kind': 'decisions_reconciliation', 'version': 1,
        'tables': {'meta': IDENTITY + ', and the discussion baseline the request was issued against',
                   'results': 'analysis: installed decision results and their source bindings',
                   'answers': 'evidence: accepted clarification answers'}},
    'alignment': {
        'path': RUN + r'(?:\.whole-problem)?\.alignment\.sqlite', 'kind': 'alignment_state', 'version': 1,
        'tables': {'meta': IDENTITY, 'attempts': 'analysis: settled stage results and their bindings',
                   'requests': 'evidence: explicit comparison requests',
                   'reservations': 'evidence: consumed analysis allowances',
                   'grants': 'evidence: explicit continuation grants',
                   'answers': 'evidence: answers to installed selection gates'}},
    'drafting': {
        'path': RUN + r'\.drafting\.sqlite', 'kind': 'drafting_state', 'version': 1,
        'tables': {'meta': IDENTITY, 'results': 'analysis: accepted drafts and their bindings',
                   'rejections': 'evidence: rejected session results'}},
    'splitting': {
        'path': RUN + r'\.splitting\.sqlite', 'kind': 'splitting_state', 'version': 1,
        'tables': {'meta': IDENTITY, 'results': 'analysis: accepted series and split proposals',
                   'rejections': 'evidence: rejected session results',
                   'operations': 'recovery: ledgered local Git operations and their reports'}},
    'split_operations': {
        'path': RUN + r'\.split-operations\.sqlite', 'kind': 'split_operations_state', 'version': 1,
        'tables': {'meta': IDENTITY,
                   'operations': 'recovery: built, applying and applied local operations of the installed '
                                 'splitting entries',
                   'validations': 'evidence: validation results of each new intermediate commit'}},
    'writer': {
        'path': NAMESPACE + r'workspace-writer\.sqlite', 'kind': None, 'version': 0,
        'tables': {'writer': 'recovery: the one writer fencing the shared Git index'}},
}

# Other retained files. Content-addressed files must match their names.
FILES = {
    'marker': NAMESPACE + re.escape(MARKER),
    'artifact': NAMESPACE + r'artifacts/[0-9a-f]{64}\.json',
    'request': NAMESPACE + r'requests/[0-9a-f]{64}\.json',
    'preparation': NAMESPACE + r'preparations/[0-9a-f]{64}(?:\.request)?\.json',
    'journal': re.escape(JOURNAL),
}
ADDRESSED = ('artifact', 'request')
# Not retained state: SQLite sidecars (the snapshot includes committed WAL),
# staging files an interrupted atomic write left behind, and pre-upgrade backups.
SKIPPED = re.compile(r'.*-(?:wal|shm|journal)|(?:.*/)?\.staged-[^/]*|' + UPGRADES + r'/.*')

# Where each inventory row retains state. Rows without stores keep none of their own:
# their runs report, route or read only.
ROWS = {
    'dk-pr-iter': ('run', 'feedback', 'reconciliation', 'alignment', 'writer'),
    'dk-pr-fix': ('run', 'feedback', 'reconciliation', 'alignment', 'writer'),
    'dk-pr-review': ('run', 'alignment'),
    'dk-repo-onboard': (),
    'dk-config': ('configuration',),
    'dk-preferences': ('configuration',),
    'dk-issue-alternatives': ('alignment', 'run'),
    'dk-pr-alternatives': ('alignment', 'run'),
    'dk-alternatives-review': ('alignment', 'run'),
    'dk-pr-review-alternatives': ('alignment', 'run'),
    'dk-plain-english': ('prose', 'configuration', 'run', 'writer'),
    'dk-asd-ste100': ('prose', 'configuration', 'run', 'writer'),
    'dk-code-simplify': ('run',),
    'dk-pr-reviewers-run': ('run',),
    'dk-pr-approvers': (),
    'dk-pr-feedback-read': ('run', 'feedback'),
    'dk-pr-feedback-address': ('run', 'feedback', 'writer'),
    'dk-pr-feedback-reply': ('run', 'feedback'),
    'dk-pr-review-post': ('run',),
    'dk-pr-description': ('drafting', 'run'),
    'dk-pr-decisions': ('decisions', 'reconciliation', 'run', 'alignment'),
    'dk-commit-split': ('splitting', 'split_operations', 'drafting', 'run'),
    'dk-pr-split': ('splitting', 'split_operations', 'run'),
    'dk-pr-publish': ('run',),
    'dk-ci-approve': ('run',),
    'dk-ci': ('run',),
    'dk-ci-flakes': (),
    'dk-commit-message': ('drafting', 'run'),
    'dk-issue': ('drafting', 'run'),
}
UNSETTLED_CLAIMS = ('claimed', 'incomplete')
UNSETTLED_PUBLICATIONS = ('prepared', 'pending')


class RecoveryError(ValueError):
    """The state, backup or upgrade cannot be used as it is."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='.staged-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _json(path):
    try:
        return admission.load_json(Path(path).read_bytes())
    except (OSError, ValueError, admission.AdmissionError) as error:
        raise RecoveryError(f'unreadable {Path(path).name}: {error}') from error


def _connect(path):
    """An existing store, opened read-write but never created.

    Only reads are issued. Write access lets SQLite roll back the hot journal a
    writer killed mid-commit leaves in a rollback-journal store, which a
    read-only connection cannot do, and which is exactly when recovery runs.
    """
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=30)


# -- Catalog ---------------------------------------------------------------------------------

def classify(relative):
    """('store'|'file'|'skipped', name) for one path relative to the suite state home."""
    for name, spec in STORES.items():
        if re.fullmatch(spec['path'], relative):
            return 'store', name
    for name, pattern in FILES.items():
        if re.fullmatch(pattern, relative):
            return 'file', name
    if SKIPPED.fullmatch(relative):
        return 'skipped', None
    raise RecoveryError('not retained cf-workflows state: ' + relative)


def check_store(path, name, relative=None):
    """Integrity, identity, justified tables and schema version of one store; returns its version."""
    spec = STORES[name]
    try:
        db = _connect(path)
        try:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise RecoveryError('store fails its integrity check: ' + str(relative or path))
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise RecoveryError('store fails its foreign key check: ' + str(relative or path))
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if not row[0].startswith('sqlite_')}
            unjustified = sorted(tables - set(spec['tables']))
            if unjustified:
                raise RecoveryError(f'unjustified retained table in {relative or path}: ' + ', '.join(unjustified))
            if spec['kind'] is not None:
                meta = dict(db.execute('SELECT key, value FROM meta').fetchall()) if 'meta' in tables else {}
                if (meta.get('suite'), meta.get('kind')) != (admission.SUITE, spec['kind']):
                    raise RecoveryError('not a cf-workflows ' + spec['kind'] + ' store: ' + str(relative or path))
            if name == 'run':
                runs = [row[0] for row in db.execute('SELECT id FROM groups')] if 'groups' in tables else []
                if len(runs) != 1 or hashlib.sha256(runs[0].encode()).hexdigest() != Path(path).stem:
                    raise RecoveryError('logical-run ledger identity differs from its name: ' + str(relative or path))
        finally:
            db.close()
    except sqlite3.DatabaseError as error:
        raise RecoveryError(f'unreadable store {relative or path}: {error}') from error
    if version > spec['version']:
        raise RecoveryError(f'{relative or path} has schema version {version}, newer than this release '
                            f'({spec["version"]}); install the newer release instead of downgrading')
    if version != spec['version'] and version not in spec.get('migrates', ()):
        raise RecoveryError(f'{relative or path} has unsupported schema version {version}')
    return version


def check_file(path, name, relative=None):
    if name in ADDRESSED and _sha256(path) != Path(path).stem:
        raise RecoveryError('content-addressed file differs from its name: ' + str(relative or path))
    if name == 'marker' and Path(path).read_bytes() != admission.canonical({'format': dk_pr_fix.STATE_FORMAT}):
        raise RecoveryError('state namespace ownership marker differs: ' + str(relative or path))


def audit(suite):
    """Every retained file under the suite state home, classified; unknown or linked entries reject."""
    suite = Path(suite)
    found = {'stores': {}, 'files': {}, 'skipped': []}
    if not suite.exists():
        return found
    if suite.is_symlink() or not suite.is_dir():
        raise RecoveryError('suite state home must be a directory, not a link')
    for directory, names, files in os.walk(suite):
        names.sort()
        for entry in names + sorted(files):
            path = Path(directory) / entry
            relative = path.relative_to(suite).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise RecoveryError('retained state must not contain links: ' + relative)
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise RecoveryError('retained state must contain only regular files: ' + relative)
            kind, name = classify(relative)
            if kind == 'skipped':
                found['skipped'].append(relative)
            else:
                found[kind + 's'][relative] = name
    return found


# -- Snapshot ----------------------------------------------------------------------------------

def _acquired(index):
    """Called after each store's read snapshot is opened (a seam for concurrency fixtures)."""


def _changed(before, after):
    """Whether any store committed between the first and the last snapshot."""
    return before != after


def _snapshot(suite, target, attempts):
    """Copy every store as of one instant; returns {relative: version}."""
    try:
        return _snapshot_once(suite, target, attempts)
    except sqlite3.DatabaseError as error:
        raise RecoveryError('a store could not be snapshotted: ' + str(error)) from error


def _snapshot_once(suite, target, attempts):
    for _ in range(attempts):
        stores = audit(suite)['stores']
        relatives = sorted(stores)
        probes, sources = [], []
        try:
            probes = [_connect(suite / relative) for relative in relatives]
            before = [probe.execute('PRAGMA data_version').fetchone()[0] for probe in probes]
            for index, relative in enumerate(relatives):
                source = _connect(suite / relative)
                sources.append(source)
                source.execute('BEGIN')
                source.execute('SELECT count(*) FROM sqlite_master').fetchone()
                _acquired(index)
            after = [probe.execute('PRAGMA data_version').fetchone()[0] for probe in probes]
            if _changed(before, after) or sorted(audit(suite)['stores']) != relatives:
                continue
            versions = {}
            for relative, source in zip(relatives, sources):
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                copy = sqlite3.connect(path)
                try:
                    source.backup(copy)
                    # A self-contained file: no WAL sidecar travels with the backup.
                    copy.execute('PRAGMA journal_mode=DELETE')
                finally:
                    copy.close()
                os.chmod(path, 0o600)
                versions[relative] = check_store(path, stores[relative], relative)
            return versions
        finally:
            for connection in sources + probes:
                connection.close()
    raise RecoveryError('retained state kept changing during the backup; retry or pause writers')


# -- Engine ------------------------------------------------------------------------------------

ENGINE_STATE = '.agent-workflow'
# A recovery bundle of `agent-workflow bundle` (Agent Workflow main 8758775, pkg/workflows/bundle.go):
# a gzip tar of manifest.json, state.db and these engine-owned trees.
BUNDLE_TREES = ('workspaces', 'providers', 'invocations', 'assets')
BUNDLE_ROOT = ('manifest.json', 'state.db')
# A committed retained asset tree: its bundle.json and the files it lists, under files/.
ASSET_TREE = re.compile(r'assets/([0-9a-f]{32})/([0-9a-f]{64})/')
# The admitted helpers sit at helpers/ in the retained tree, mounted at /actions/helpers.
HELPERS = 'helpers/'
SHA256 = re.compile(r'[0-9a-f]{64}')


def _members(archive):
    """Every member of an engine bundle; anything outside its engine-owned trees rejects.

    A workspaces/ link is kept only when its target is relative and stays in the
    workspaces tree. The engine also exports absolute links that point into its
    own tree; they name the old state path, so restore refuses them (fail closed).
    """
    import posixpath
    seen = set()
    for member in archive:
        name = member.name
        if name in seen:
            raise RecoveryError('duplicate engine bundle entry: ' + repr(name))
        seen.add(name)
        parts = name.split('/')
        unsafe = name.startswith('/') or '\\' in name or '\0' in name or any(part in ('', '.', '..') for part in parts)
        root = parts[0] in BUNDLE_ROOT and not (len(parts) == 1 and member.isfile())
        if unsafe or root or parts[0] not in BUNDLE_ROOT + BUNDLE_TREES:
            raise RecoveryError('not an engine recovery bundle entry: ' + repr(name))
        if not (member.isfile() or member.isdir() or (member.issym() and parts[0] == 'workspaces')):
            raise RecoveryError('engine bundle entry is not a file or directory: ' + name)
        if member.issym() and (member.linkname.startswith('/') or not posixpath.normpath(posixpath.join(
                posixpath.dirname(name), member.linkname)).startswith('workspaces/')):
            raise RecoveryError('engine bundle link leaves its workspaces tree: ' + name)
        yield member


def bundle_assets(bundle):
    """{'origin/id': {path: sha256}} of each retained asset tree in an engine bundle.

    Every tree must hold exactly the files its bundle.json lists, with their sizes
    and digests; a tree without a manifest, or a manifest without its files, rejects,
    and so does a versioned tree without the owner.json the engine requires. The
    engine also requires bundle.json to match its own serialization byte for byte;
    that is left to the engine, which refuses such a tree on resume (fail closed).
    """
    manifests, files, owners = {}, {}, set()
    try:
        with tarfile.open(bundle, 'r:gz') as archive:
            for member in _members(archive):
                match = ASSET_TREE.match(member.name)
                if member.name.startswith('assets/') and member.isfile() and not match:
                    raise RecoveryError('engine bundle asset outside a retained tree: ' + member.name)
                if not match or not member.isfile():
                    continue
                key, rest = '/'.join(match.groups()), member.name[match.end():]
                data = archive.extractfile(member).read()
                if rest == 'bundle.json':
                    manifests[key] = data
                elif rest.startswith('files/'):
                    files.setdefault(key, {})[rest[len('files/'):]] = (
                        len(data), hashlib.sha256(data).hexdigest(), stat.S_IMODE(member.mode))
                elif rest == 'owner.json':
                    owners.add(key)
                else:
                    raise RecoveryError('unexpected retained asset entry: ' + member.name)
    except (tarfile.TarError, OSError, EOFError) as error:
        raise RecoveryError('unreadable engine bundle: ' + str(error)) from error
    result = {}
    for key in sorted(set(manifests) | set(files)):
        if key not in manifests:
            raise RecoveryError('retained asset tree has no bundle.json: assets/' + key)
        try:
            manifest = json.loads(manifests[key])
        except ValueError as error:
            raise RecoveryError('unreadable retained asset manifest: assets/' + key) from error
        origin, ident = key.split('/')
        if not isinstance(manifest, dict) or (manifest.get('origin_run_id'), manifest.get('id')) != (origin, ident):
            raise RecoveryError('retained asset manifest names another tree: assets/' + key)
        # The historical helper bundle lists files; a versioned asset bundle lists entries with a kind.
        versioned = bool(manifest.get('version'))
        if versioned and key not in owners:  # verifyAssetBundleDirectory (retained_assets.go) requires owner.json.
            raise RecoveryError('retained asset tree has no owner.json: assets/' + key)
        entries = manifest.get('entries') if versioned else manifest.get('files')
        if not isinstance(entries, list):
            raise RecoveryError('retained asset manifest lists no files: assets/' + key)
        listed = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('kind', 'file') not in ('file', 'directory'):
                raise RecoveryError('unsupported retained asset entry: assets/' + key)
            if entry.get('kind', 'file') == 'directory':
                continue
            if (not isinstance(entry.get('path'), str) or not isinstance(entry.get('size'), int)
                    or not isinstance(entry.get('mode'), int) or not SHA256.fullmatch(str(entry.get('sha256')))):
                raise RecoveryError('invalid retained asset file entry: assets/' + key)
            # The engine checks each retained file's exact permissions (materializedAssetMode, VerifyHelperBundle).
            mode = (0o555 if entry['mode'] & 0o111 else 0o444) if versioned else entry['mode']
            listed[entry['path']] = (entry['size'], entry['sha256'], mode)
        if files.get(key, {}) != listed:
            raise RecoveryError('retained asset files differ from their manifest: assets/' + key)
        result[key] = {path: digest for path, (_, digest, _) in sorted(listed.items())}
    return result


def helper_digest(files):
    """The helper-manifest digest of a retained tree's helpers/, as an admitted plan records it; None without them."""
    helpers = {path[len(HELPERS):]: digest for path, digest in files.items() if path.startswith(HELPERS)
               and '__pycache__' not in path.split('/') and not path.endswith('.pyc')}
    return admission.digest(helpers) if helpers else None


def _exact_modes(member, path):
    """tarfile's 'data' safety checks, keeping the member's exact permission bits."""
    checked = tarfile.data_filter(member, path)
    if member.issym():
        return checked
    return checked.replace(mode=stat.S_IMODE(member.mode) & 0o777, deep=False)


def _remove(root, names):
    """Remove the bundle entries one restore created, children first, read-only directories included."""
    for name in names:
        path = root / name
        if path.is_dir() and not path.is_symlink():
            os.chmod(path, 0o700)
    for name in sorted(names, reverse=True):
        path = root / name
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        except FileNotFoundError:
            pass


class Engine:
    """The engine adapter bound to the `agent-workflow` CLI of one workspace.

    It reaches the maintenance commands of Agent Workflow main 8758775 (`stop`,
    `backup`, `bundle`, `restore`, `resume`); no released engine has them yet.
    status() reports the controller stopped only when the workspace's execution
    owner lock is free, the same lock `backup`, `bundle` and `restore` take.
    """

    def __init__(self, cli, workspace, *, timeout=600, environment=None):
        self.cli, self.workspace = str(cli), Path(workspace).resolve()
        self.state = self.workspace / ENGINE_STATE
        self.timeout, self.environment = timeout, environment

    def command(self, verb, *args, workspace=True):
        argv = [self.cli, verb, *(('--workspace', str(self.workspace)) if workspace else ()), *map(str, args)]
        completed = subprocess.run(argv, capture_output=True, timeout=self.timeout, env=self.environment,
                                   cwd=self.workspace, stdin=subprocess.DEVNULL)
        if completed.returncode != 0:
            raise RecoveryError(f'agent-workflow {verb} failed: '
                                + (completed.stderr or completed.stdout).decode(errors='replace').strip()[-2000:])
        try:
            return json.loads(completed.stdout)
        except ValueError as error:
            raise RecoveryError(f'agent-workflow {verb} printed no JSON result') from error

    def owned(self):
        """Whether an execution owner (a controller or a foreground run) holds the workspace."""
        import fcntl
        try:
            handle = open(self.state / 'owner.lock', 'rb')
        except FileNotFoundError:
            return False
        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def status(self):
        return {'controller': 'running' if self.owned() else 'stopped',
                'version': self.command('version', workspace=False).get('version')}

    def stop(self, wait=30):
        """Ask the controller to shut down and wait until it releases the workspace."""
        import time
        if not self.owned():
            return {'stopped': True}
        result = self.command('stop')
        deadline = time.monotonic() + wait
        while self.owned():
            if time.monotonic() > deadline:
                raise RecoveryError('the engine controller did not release the workspace')
            time.sleep(0.05)
        return result

    def backup(self, path):
        return self.command('backup', path)

    def bundle(self, path):
        return self.command('bundle', path)

    def retained(self, bundle):
        """The helper-manifest digests of the admitted helpers the bundle's retained asset trees hold."""
        return {digest for digest in map(helper_digest, bundle_assets(bundle).values()) if digest is not None}

    def restore(self, database, bundle):
        """Put the bundle's engine-owned trees back, then the database through `agent-workflow restore`.

        Nothing is merged: an existing entry at a bundle path refuses before any
        change. Entries keep their exact tar permission bits, since the engine
        verifies the modes of retained asset trees (0444/0555 files, 0555
        directories) on resume, on use and on the next bundle; only the setuid,
        setgid and sticky bits are dropped. If extraction or `agent-workflow
        restore` fails, every entry this call created is removed again. When
        the failure comes after `agent-workflow restore` replaced the database
        (a timeout or unreadable output), the database stays restored; a retry
        still recovers, since restore replaces it again. The engine quarantines every unfinished run as
        `restore_uncertainty`; resuming it needs resume(run, retry_uncertain=True).
        """
        if self.owned():
            raise RecoveryError('stop the engine controller before restoring its database and bundle')
        assets = bundle_assets(bundle)  # Verified before anything changes.
        with tarfile.open(bundle, 'r:gz') as archive:
            members = [member for member in _members(archive) if member.name not in BUNDLE_ROOT]
            existing = [member.name for member in members if os.path.lexists(self.state / member.name)
                        and not (member.isdir() and (self.state / member.name).is_dir()
                                 and not (self.state / member.name).is_symlink())]
            if existing:
                raise RecoveryError('engine state exists at bundle paths; restore never merges: '
                                    + ', '.join(sorted(existing)[:8]))
            # Every entry and implied parent directory that does not exist yet, so a failure can remove them.
            names = {'/'.join(member.name.split('/')[:depth]) for member in members
                     for depth in range(1, member.name.count('/') + 2)}
            created = [name for name in names if not os.path.lexists(self.state / name)]
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                archive.extractall(self.state, members=members, filter=_exact_modes)
                return {**self.command('restore', database), 'assets': sorted(assets)}
            except BaseException:
                _remove(self.state, created)
                raise

    def resume(self, run, *, retry_uncertain=False):
        """Resume an engine run through a (re)started controller; restored work needs retry_uncertain."""
        return self.command('resume', '--detach', *(('--retry-uncertain',) if retry_uncertain else ()), run)


# -- Backup ------------------------------------------------------------------------------------

def installation(install=dk_pr_fix.INSTALL):
    """The installed release: descriptor version, closure digest and helper-manifest digest."""
    closure = dk_pr_fix.verify_installation(install)
    version = re.search(r'(?m)^version: (\S+)$', (Path(install) / 'plugin.yaml').read_text())
    helpers = {path[len('workflows/helpers/'):]: value for path, value in dk_pr_fix.closure_digests(install).items()
               if path.startswith('workflows/helpers/')}
    return {'version': version[1] if version else None, 'closure': closure, 'helpers': admission.digest(helpers)}


def _configuration(path):
    """The saved configuration bytes, read under its writer lock; only this suite's document."""
    path = Path(path)
    if not path.exists():
        return None
    import fcntl
    with open(path.with_name(path.name + '.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        data = path.read_bytes()
    _check_configuration(data)
    return data


def _check_configuration(data):
    try:
        document = admission.load_json(data)
    except (ValueError, admission.AdmissionError) as error:
        raise RecoveryError('unreadable saved configuration: ' + str(error)) from error
    if not isinstance(document, dict) or (document.get('suite'), document.get('kind')) != (admission.SUITE,
                                                                                          'configuration'):
        raise RecoveryError('saved configuration is not a cf-workflows configuration; settings are never imported')
    if document.get('schema_version') != 1:
        raise RecoveryError('unsupported configuration schema version ' + repr(document.get('schema_version')))
    return document


def backup(home, destination, *, engine=None, configuration=None, install=dk_pr_fix.INSTALL, attempts=5,
           _allow_upgrade=False):
    """Write a consistent backup of the suite state home (and the engine bundle) to a new directory.

    engine is the adapter for the engine's own backup: status(), backup(path) and
    bundle(path). Without it the backup holds this suite's state only, and an
    unfinished claim later needs its engine run or an explicit continuation.
    """
    suite, destination = Path(home) / SUITE_DIR, Path(destination)
    if os.path.lexists(destination):
        raise RecoveryError('backup destination already exists')
    journal = suite / JOURNAL
    if not _allow_upgrade and journal.is_file() and _json(journal).get('status') != 'applied':
        raise RecoveryError('an upgrade is in progress; finish it before taking a backup')
    destination.mkdir(parents=True, mode=0o700)
    try:
        manifest = {'format': FORMAT, 'suite': admission.SUITE, 'installation': installation(install),
                    'engine': None, 'configuration': None, 'stores': {}, 'files': {}}
        if engine is not None:
            status = engine.status()
            if status.get('controller') != 'stopped':
                raise RecoveryError('stop the engine controller before backing up its database and bundle')
            (destination / 'engine').mkdir(mode=0o700)
            engine.backup(destination / 'engine' / 'database')
            engine.bundle(destination / 'engine' / 'bundle')
            manifest['engine'] = {'version': status.get('version'),
                                  **{name: _sha256(destination / 'engine' / name) for name in ('database', 'bundle')}}
        state = destination / 'state'
        state.mkdir(mode=0o700)
        versions = _snapshot(suite, state, attempts) if suite.exists() else {}
        for relative, version in versions.items():
            manifest['stores'][relative] = {'store': classify(relative)[1], 'schema_version': version,
                                            'sha256': _sha256(state / relative)}
        # Checkpoints before the immutable artifacts they reference, which are written first.
        files = audit(suite)['files'] if suite.exists() else {}
        order = ('marker', 'journal', 'preparation', 'request', 'artifact')
        for relative in sorted(files, key=lambda item: (order.index(files[item]), item)):
            source, target = suite / relative, state / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            check_file(target, files[relative], relative)
            manifest['files'][relative] = {'file': files[relative], 'sha256': _sha256(target)}
        for relative, name in audit(suite)['files'].items() if suite.exists() else ():
            if name == 'artifact' and relative not in manifest['files']:
                shutil.copyfile(suite / relative, state / relative)
                check_file(state / relative, name, relative)
                manifest['files'][relative] = {'file': name, 'sha256': _sha256(state / relative)}
        if configuration is not None:
            data = _configuration(configuration)
            if data is not None:
                _atomic(destination / 'configuration.json', data)
                manifest['configuration'] = {'sha256': hashlib.sha256(data).hexdigest()}
        # The manifest is written last: a backup without one is incomplete.
        _atomic(destination / 'manifest.json', admission.canonical(manifest))
        return manifest
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


# -- Restore -----------------------------------------------------------------------------------

def verify(source):
    """Check a backup completely, read-only: manifest, every digest, every store and file."""
    source = Path(source)
    if not (source / 'manifest.json').is_file():
        raise RecoveryError('incomplete backup: no manifest')
    manifest = _json(source / 'manifest.json')
    if not isinstance(manifest, dict) or (manifest.get('format'), manifest.get('suite')) != (FORMAT, admission.SUITE):
        raise RecoveryError('not a cf-workflows state backup; another suite\'s state is never imported')
    state = source / 'state'
    listed = {**manifest['stores'], **manifest['files']}
    found = audit(state)
    present = set(found['stores']) | set(found['files'])
    if present != set(listed) or found['skipped']:
        raise RecoveryError('backup files differ from its manifest: '
                            + ', '.join(sorted(present ^ set(listed) | set(found['skipped']))[:8]))
    for relative, record in listed.items():
        if _sha256(state / relative) != record['sha256']:
            raise RecoveryError('backup file differs from its recorded digest: ' + relative)
    for relative, record in manifest['stores'].items():
        if found['stores'][relative] != record['store']:
            raise RecoveryError('backup store kind differs: ' + relative)
        with tempfile.TemporaryDirectory() as scratch:  # Never let a check touch the backup itself.
            copy = Path(scratch) / Path(relative).name
            shutil.copyfile(state / relative, copy)
            if check_store(copy, record['store'], relative) != record['schema_version']:
                raise RecoveryError('backup store version differs from its manifest: ' + relative)
    for relative, record in manifest['files'].items():
        check_file(state / relative, record['file'], relative)
    if manifest['configuration'] is not None:
        data = (source / 'configuration.json').read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest['configuration']['sha256']:
            raise RecoveryError('backup configuration differs from its recorded digest')
        _check_configuration(data)
    if manifest['engine'] is not None:
        for name in ('database', 'bundle'):
            if _sha256(source / 'engine' / name) != manifest['engine'][name]:
                raise RecoveryError('engine ' + name + ' differs from its recorded digest')
    return manifest


def _plans(state):
    """Admitted plans in the retained artifacts: logical run, execution and helper-manifest digest."""
    plans = []
    for path in sorted(Path(state).glob('repositories/*/artifacts/*.json')):
        value = admission.load_json(path.read_bytes())
        if isinstance(value, dict) and {'logical_run', 'execution_fingerprint', 'helper_manifest'} <= set(value):
            plans.append({'namespace': path.parents[1].name, 'logical_run': value['logical_run'],
                          'execution': value['execution_fingerprint'],
                          'helpers': admission.digest(value['helper_manifest'])})
    return plans


def unsettled(state):
    """Unfinished original work per logical run: open claims and unsettled publications."""
    result = {}
    for path in sorted(Path(state).glob('repositories/*/runs/*.sqlite')):
        if classify(path.relative_to(state).as_posix())[1] != 'run':
            continue
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / path.name
            shutil.copyfile(path, copy)
            db = _connect(copy)
            try:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                [run] = [row[0] for row in db.execute('SELECT id FROM groups')]
                marks = ', '.join('?' * len(UNSETTLED_CLAIMS))
                claims = [row[0] for row in db.execute(f'SELECT id FROM claims WHERE state IN ({marks}) ORDER BY id',
                                                       UNSETTLED_CLAIMS)] if 'claims' in tables else []
                states = ('prepared', 'pending', 'uncertain')
                marks = ', '.join('?' * len(states))
                intents = [row[0] for row in db.execute(
                    f'SELECT id FROM publication_intents WHERE state IN ({marks}) ORDER BY id',
                    states)] if 'publication_intents' in tables else []
            finally:
                db.close()
        if claims or intents:
            result[(path.parents[1].name, run)] = {'claims': claims, 'publications': intents, 'operations': []}
    for path in sorted([*Path(state).glob('repositories/*/runs/*.splitting.sqlite'),
                        *Path(state).glob('repositories/*/runs/*.split-operations.sqlite')]):
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / path.name
            shutil.copyfile(path, copy)
            db = _connect(copy)
            try:
                run = dict(db.execute('SELECT key, value FROM meta').fetchall())['logical_run']
                # A built installed operation has not moved a ref yet; an applying one resumes.
                applying = [row[0] for row in db.execute("SELECT id FROM operations WHERE state = 'applying'")]
            finally:
                db.close()
        if applying:
            entry = result.setdefault((path.parents[1].name, run), {'claims': [], 'publications': [], 'operations': []})
            entry['operations'] = sorted(entry['operations'] + applying)
    return result


def assets(state, install, retained):
    """Whether each retained plan's admitted helpers are available: installed, retained by the engine, or missing."""
    current = installation(install)['helpers'] if install is not None and Path(install).exists() else None
    report = {}
    for plan in _plans(state):
        where = ('installed' if plan['helpers'] == current
                 else 'retained' if plan['helpers'] in retained else 'missing')
        report.setdefault((plan['namespace'], plan['logical_run']), {})[plan['execution']] = where
    return report


def restore(source, home, *, engine=None, configuration=None, install=dk_pr_fix.INSTALL):
    """Restore a verified backup into an empty suite state home; never merges or imports.

    The engine's database and bundle go back through engine.restore(database,
    bundle) first. A logical run with unfinished original work can resume only with
    its original admitted helpers, installed or retained by the engine; otherwise
    it is reported blocked rather than run with replacement resources.
    """
    source = Path(source)
    manifest = verify(source)
    suite = Path(home) / SUITE_DIR
    if os.path.lexists(suite) and (suite.is_symlink() or not suite.is_dir() or any(suite.iterdir())):
        raise RecoveryError('the suite state home is not empty; move it aside first, restore never merges')
    saved = source / 'configuration.json'
    if manifest['configuration'] is not None:
        if configuration is None:
            raise RecoveryError('the backup holds a saved configuration; name its location')
        current = Path(configuration)
        if current.exists() and current.read_bytes() != saved.read_bytes():
            raise RecoveryError('a different saved configuration exists; restore never overwrites it')
    if manifest['engine'] is not None and engine is None:
        raise RecoveryError('the backup holds the engine recovery bundle; restore it together')
    database, bundle = source / 'engine' / 'database', source / 'engine' / 'bundle'
    retained = set(engine.retained(bundle)) if manifest['engine'] is not None else set()
    report = {'installation': manifest['installation'], 'current': installation(install) if install else None,
              'engine': None, 'runs': {}, 'blocked': [],
              'upgrade': sorted(relative for relative, record in manifest['stores'].items()
                                if record['schema_version'] != STORES[record['store']]['version'])}
    open_work = unsettled(source / 'state')
    plans = assets(source / 'state', install, retained)
    for namespace, run in sorted(set(plans) | set(open_work)):
        executions = plans.get((namespace, run), {})
        entry = {'assets': executions,
                 **open_work.get((namespace, run), {'claims': [], 'publications': [], 'operations': []})}
        report['runs'][namespace + '/' + run] = entry
        if entry['claims'] and 'missing' in executions.values():
            report['blocked'].append(namespace + '/' + run)
    if manifest['engine'] is not None:
        report['engine'] = engine.restore(database, bundle)
    if manifest['configuration'] is not None and not Path(configuration).exists():
        _atomic(Path(configuration), saved.read_bytes())
    suite.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix='.cf-workflows-restore-', dir=suite.parent))
    try:
        shutil.copytree(source / 'state', staging / SUITE_DIR)
        if os.path.lexists(suite):
            suite.rmdir()
        os.rename(staging / SUITE_DIR, suite)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return report


# -- Upgrade -----------------------------------------------------------------------------------

def _migrated(relative):
    """Called after each migrated store (a seam for interruption fixtures)."""


MIGRATE = """
import os, sys, tempfile
sys.dont_write_bytecode = True
helpers, namespace, run = sys.argv[1:4]
sys.path.insert(0, helpers)
os.environ['CF_WORKFLOWS_STATE'] = namespace
from prfix.core import Ledger
with tempfile.TemporaryDirectory() as root:
    Ledger(root, run).close()
"""


def _migrate_run(path, install):
    """Open a logical-run ledger with the installed helpers, which migrate it in one transaction."""
    db = _connect(path)
    try:
        [run] = [row[0] for row in db.execute('SELECT id FROM groups')]
    finally:
        db.close()
    namespace = Path(path).parents[1]
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', MIGRATE,
                                str(Path(install) / 'workflows' / 'helpers'), str(namespace), run],
                               capture_output=True, timeout=120, env={'PATH': os.environ.get('PATH', '')})
    if completed.returncode:
        raise RecoveryError('ledger migration failed: ' + completed.stderr.decode(errors='replace')[-2000:])


def upgrade(home, *, install=dk_pr_fix.INSTALL, configuration=None):
    """Migrate every store to this release under a journal; resumable, with one retained backup.

    Every store and the saved configuration are checked before anything changes:
    a newer or unsupported version refuses the whole upgrade.
    """
    suite = Path(home) / SUITE_DIR
    target = installation(install)
    path = suite / JOURNAL
    journal = _json(path) if path.is_file() else None
    if journal is not None and journal.get('format') != UPGRADE_FORMAT:
        raise RecoveryError('unsupported upgrade journal')
    if journal is not None and journal['status'] == 'applied' and journal['to'] == target:
        return journal
    if journal is None or journal['status'] == 'applied':
        stores = audit(suite)['stores']
        versions = {relative: check_store(suite / relative, name, relative) for relative, name in stores.items()}
        if configuration is not None and Path(configuration).exists():
            _check_configuration(Path(configuration).read_bytes())
        previous = journal['to'] if journal else None
        ident = admission.digest([previous, target, versions])[:16]
        journal = {'format': UPGRADE_FORMAT, 'from': previous, 'to': target, 'status': 'prepared',
                   'backup': UPGRADES + '/' + ident, 'stores': versions}
        _atomic(path, admission.canonical(journal))
    elif journal['to'] != target:
        raise RecoveryError('an interrupted upgrade to another release must finish with that release')
    retained = suite / journal['backup']
    if journal['status'] == 'prepared':
        if not (retained / 'manifest.json').is_file():
            shutil.rmtree(retained, ignore_errors=True)  # An interrupted backup is incomplete.
            backup(home, retained, install=install, _allow_upgrade=True)
        journal['status'] = 'migrating'
        _atomic(path, admission.canonical(journal))
    for relative, version in sorted(journal['stores'].items()):
        name = classify(relative)[1]
        if check_store(suite / relative, name, relative) != STORES[name]['version']:
            _migrate_run(suite / relative, install)
            if check_store(suite / relative, name, relative) != STORES[name]['version']:
                raise RecoveryError('store did not reach this release\'s schema: ' + relative)
        _migrated(relative)
    journal['status'] = 'applied'
    _atomic(path, admission.canonical(journal))
    return journal


# -- Cost --------------------------------------------------------------------------------------

def cost(proxies, usage=()):
    """Deterministic proxies and provider-reported dollar cost, never mixed.

    usage holds the engine's per-run usage summaries (`run_id`, nullable
    `cost_usd`, `known_cost_usd`, `complete`) for the original and every later
    session. A total is measured only when every run reported a complete cost;
    otherwise it is null and only the known subtotal is shown. Nothing is priced
    or estimated here.
    """
    runs = {}
    for record in usage:
        if record['run_id'] in runs and runs[record['run_id']] != record:
            raise RecoveryError('conflicting usage for run ' + record['run_id'])
        runs[record['run_id']] = record
    known = sum(record.get('known_cost_usd') or 0 for record in runs.values())
    complete = bool(runs) and all(record.get('cost_usd') is not None and record.get('complete') for record in runs.values())
    return {'proxies': dict(proxies), 'measured': {
        'runs': sorted(runs), 'cost_usd': sum(record['cost_usd'] for record in runs.values()) if complete else None,
        'known_cost_usd': known, 'complete': complete}}
