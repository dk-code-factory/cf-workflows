"""Upgrades, backup recovery and fresh-session selective reuse (#37).

Every case uses disposable repositories, a fresh suite state home and fail-closed
fakes: GitHub transports, scripted native sessions, and a fake engine maintenance
adapter whose database backup and recovery bundle are snapshots of the fixture
engine's durable state (claims, gates, sessions) and of the admitted helpers it
retains. Two fake host adapters (Codex and Claude) record native dispatches.
The CLI-bound engine adapter runs against a fake of the agent-workflow maintenance
CLI; one opt-in case builds the real CLI from the pinned unreleased commit.
Unconfigured operations fail without a live fallback. Counts are fixture proxies;
no case measures or claims live cost, and none certifies an engine, provider or host.
"""
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True  # Never add caches to the installed closure.

import dk_pr_fix as _helpers  # noqa: E402,F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture  # noqa: E402
from prfix import app, core, feedback  # noqa: E402
from prfix import decisions as installed_decisions  # noqa: E402
from prfix import splitting as installed_splitting  # noqa: E402
from prfix.publication import Publication, UncertainEffect  # noqa: E402
from cf_workflows import (acceptance, admission, alignment, decisions, dk_pr_fix, drafting, prose,  # noqa: E402
                          recovery, splitting, writes)
import test_alignment as alignment_tests  # noqa: E402
import test_configuration_skills as configuration_tests  # noqa: E402
import test_decisions as decision_tests  # noqa: E402
import test_dk_pr_fix_continuation as continuation  # noqa: E402
import test_dk_pr_fix_decisions as installed_decision_tests  # noqa: E402
import test_dk_pr_fix_publication as publication_tests  # noqa: E402
import test_drafting as drafting_tests  # noqa: E402
import test_prose as prose_tests  # noqa: E402
import test_splitting as splitting_tests  # noqa: E402
from test_publication import BASE, GRANTS, HEAD  # noqa: E402

INVENTORY = [row['name'] for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']]
DRIVER = ROOT / 'tests' / 'fixtures' / 'dk_pr_fix_driver.py'
NAMESPACE = 'repositories/' + '0' * 32
PUBLISHED_PR = 'https://github.com/o/r/pull/7'


def namespace(home):
    """A repository namespace under the suite state home, claimed with the suite's marker."""
    path = Path(home) / recovery.SUITE_DIR / NAMESPACE
    path.mkdir(parents=True, exist_ok=True)
    marker = path / dk_pr_fix.STATE_MARKER
    if not marker.exists():
        marker.write_bytes(admission.canonical({'format': dk_pr_fix.STATE_FORMAT}))
    return path


def helper_files(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(Path(directory).rglob('*')) if path.is_file()}


class Host:
    """A fake Codex or Claude host adapter: one native session per new claim; nothing else."""

    def __init__(self, provider):
        assert provider in ('codex', 'claude')
        self.provider = provider
        self.claims, self.sessions = {}, Counter()

    def dispatch(self, claim, result):
        """Run the claim's session once; a known claim returns its original result (engine recovery)."""
        if claim not in self.claims:
            self.sessions['native'] += 1
            ident = ('thread_' if self.provider == 'codex' else 'session_') + claim[-12:]
            self.claims[claim] = {'session': ident, 'result': copy.deepcopy(result)}
        return copy.deepcopy(self.claims[claim])

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        raise AssertionError('unconfigured fixture operation: ' + name)


class EngineAdapter:
    """Fake engine maintenance: database backup and recovery bundle of a fixture engine.

    The bundle retains the admitted helper files of the runs it covers, as the
    engine's retained assets do. restore() quarantines unfinished claims.
    """

    def __init__(self, engine, helpers=None, retain=True):
        self.engine, self.helpers, self.retain = engine, helpers, retain
        self.calls = Counter()
        self.controller = 'stopped'

    def status(self):
        self.calls['status'] += 1
        return {'controller': self.controller, 'version': '0.0.0-fixture'}

    def backup(self, path):
        self.calls['backup'] += 1
        Path(path).write_bytes(pickle.dumps(self.engine.__dict__))

    def bundle(self, path):
        self.calls['bundle'] += 1
        retained = helper_files(self.helpers) if self.helpers is not None and self.retain else {}
        Path(path).write_bytes(pickle.dumps({'helpers': retained}))

    def retained(self, bundle):
        self.calls['retained'] += 1
        files = pickle.loads(Path(bundle).read_bytes())['helpers']
        if not files:
            return set()
        return {admission.digest({path: hashlib.sha256(data).hexdigest() for path, data in files.items()})}

    def materialize(self, bundle, target):
        """Write the retained helpers out, as the engine would mount them for a resumed run."""
        for path, data in pickle.loads(Path(bundle).read_bytes())['helpers'].items():
            (Path(target) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(target) / path).write_bytes(data)

    def lose(self):
        """The engine's own state is gone: every durable collection is emptied."""
        for key, value in list(self.engine.__dict__.items()):
            if isinstance(value, (dict, list)):
                self.engine.__dict__[key] = type(value)()

    def restore(self, database, bundle):
        self.calls['restore'] += 1
        self.engine.__dict__.clear()
        self.engine.__dict__.update(pickle.loads(Path(database).read_bytes()))
        return {'quarantined': sorted(getattr(self.engine, 'claims', {}))}

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        raise AssertionError('unconfigured fixture operation: ' + name)


def recover(test, home, engine, *, configuration=None, wipe_configuration=False, install=dk_pr_fix.INSTALL):
    """Back up, lose all state, restore into the same location; returns the restore report."""
    destination = Path(tempfile.mkdtemp(prefix='cfw backup ')) / 'backup'
    test.addCleanup(shutil.rmtree, destination.parent, True)
    recovery.backup(home, destination, engine=engine, configuration=configuration, install=install)
    shutil.rmtree(Path(home) / recovery.SUITE_DIR)
    engine.lose()
    if wipe_configuration:
        Path(configuration).unlink()
    return recovery.restore(destination, home, engine=engine, configuration=configuration, install=install)


# -- Catalog and retention ---------------------------------------------------------------------------

class Catalog(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw catalog ')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def tables(self, path):
        db = sqlite3.connect(path)
        try:
            return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    if not row[0].startswith('sqlite_')}
        finally:
            db.close()

    def test_every_retained_table_has_a_stated_purpose(self):
        suite = self.home / recovery.SUITE_DIR
        ns = namespace(self.home)
        pr = core.identity(PUBLISHED_PR)
        made = {
            'configuration': lambda path: acceptance.Ledger(path).close(),
            'prose': lambda path: prose.Ledger(path).close(),
            'feedback': lambda path: feedback.Ledger(path, logical_run='r', pr=pr).close(),
            'decisions': lambda path: decisions.Ledger(path, logical_run='r', pr=PUBLISHED_PR).close(),
            'reconciliation': lambda path: installed_decisions.Record(path, logical_run='r', pr=pr).close(),
            'alignment': lambda path: alignment.Ledger(path, logical_run='r', pr=pr).close(),
            'drafting': lambda path: drafting.Ledger(path, logical_run='r', target=PUBLISHED_PR).close(),
            'splitting': lambda path: splitting.Ledger(path, logical_run='r', target=PUBLISHED_PR).close(),
        }
        paths = {'configuration': suite / 'configuration.sqlite3',
                 'prose': prose.ledger_path(self.home, 'github.com/o/r'),
                 'feedback': feedback.ledger_path(ns, 'r'), 'decisions': decisions.ledger_path(ns, 'r'),
                 'reconciliation': installed_decisions.ledger_path(ns, 'r'),
                 'alignment': alignment.ledger_path(ns, 'r'), 'drafting': drafting.ledger_path(ns, 'r'),
                 'splitting': splitting.ledger_path(ns, 'r')}
        for name, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            made[name](path)
        checkout = Path(tempfile.mkdtemp(prefix='cfw checkout '))
        self.addCleanup(shutil.rmtree, checkout, True)
        with patch.dict(os.environ, {'CF_WORKFLOWS_STATE': str(ns)}):
            core.Ledger(checkout, 'r', PUBLISHED_PR).close()
            core.writer_lease(checkout, 'r', 'claim', acquire=True)
            installed_splitting.Store(checkout, {'logical_run': 'r', 'pr': {'url': PUBLISHED_PR}}, write=True).__exit__()
            paths['split_operations'] = installed_splitting.store_path(checkout, 'r')
        paths['run'] = core.ledger_path(ns, 'r')
        paths['writer'] = ns / 'workspace-writer.sqlite'
        self.assertEqual(set(paths), set(recovery.STORES))
        for name, path in paths.items():
            with self.subTest(store=name):
                self.assertEqual(self.tables(path), set(recovery.STORES[name]['tables']))
                relative = path.relative_to(suite).as_posix()
                self.assertEqual(recovery.classify(relative), ('store', name))
                self.assertEqual(recovery.check_store(path, name), recovery.STORES[name]['version'])
        self.assertEqual(set(recovery.audit(suite)['stores'].values()), set(recovery.STORES))
        # A derived copy (an extra table) is not retained state.
        db = sqlite3.connect(paths['decisions'])
        db.execute('CREATE TABLE transcripts (body TEXT)')
        db.close()
        with self.assertRaisesRegex(recovery.RecoveryError, 'unjustified retained table.*transcripts'):
            recovery.check_store(paths['decisions'], 'decisions')

    def test_every_inventory_row_names_where_it_retains_state(self):
        self.assertEqual(sorted(recovery.ROWS), sorted(INVENTORY))
        for row, stores in recovery.ROWS.items():
            self.assertTrue(set(stores) <= set(recovery.STORES), row)
        # Each store a delivered row retains is exercised by a recovery scenario below.
        covered = {'run', 'feedback', 'writer', 'configuration', 'prose', 'decisions', 'reconciliation', 'alignment',
                   'drafting', 'splitting', 'split_operations'}
        self.assertEqual({store for stores in recovery.ROWS.values() for store in stores}, covered)
        self.assertEqual(covered, set(recovery.STORES))

    def test_foreign_settings_state_and_links_are_never_retained(self):
        suite = self.home / recovery.SUITE_DIR
        ns = namespace(self.home)
        for foreign in ('.pr-fix-state/state.json', 'dk-workflows/settings.json', 'transcript.md'):
            with self.subTest(foreign=foreign):
                path = ns / foreign
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
                with self.assertRaisesRegex(recovery.RecoveryError, 'not retained cf-workflows state'):
                    recovery.audit(suite)
                shutil.rmtree(path.parent) if path.parent != ns else path.unlink()
        (ns / 'artifacts').mkdir()
        (ns / 'artifacts' / ('a' * 64 + '.json')).symlink_to(ns / dk_pr_fix.STATE_MARKER)
        with self.assertRaisesRegex(recovery.RecoveryError, 'links'):
            recovery.audit(suite)


# -- Consistent online backup and verified restore ---------------------------------------------------

class Backup(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix='cfw backup ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / 'xdg state'
        self.ns = namespace(self.home)
        context = patch.dict(os.environ, {'CF_WORKFLOWS_STATE': str(self.ns)})
        context.start()
        self.addCleanup(context.stop)
        self.root = self.base / 'checkout'
        self.root.mkdir()
        self.ledger = core.Ledger(self.root, 'run', PUBLISHED_PR)
        self.addCleanup(self.ledger.close)
        self.feedback = feedback.Ledger(feedback.ledger_path(self.ns, 'run'), logical_run='run',
                                        pr=core.identity(PUBLISHED_PR))
        self.addCleanup(self.feedback.close)
        self.ledger.reserve('review_cycles', 'original', 5)
        self.other = core.Ledger(self.root, 'other', PUBLISHED_PR)
        self.addCleanup(self.other.close)
        self.reference = core.artifact(self.root, {'original': 'output'})
        suite = self.home / recovery.SUITE_DIR
        relatives = sorted(recovery.audit(suite)['stores'])
        runs = sorted((relatives.index(path.relative_to(suite).as_posix()), ledger) for path, ledger in (
            (core.ledger_path(self.ns, 'run'), self.ledger), (core.ledger_path(self.ns, 'other'), self.other)))
        # The index after which exactly the first logical-run ledger has its snapshot.
        (self.split, self.first), (_, self.second) = runs

    def destination(self, name='backup'):
        return self.base / 'backups' / name

    def write_pair(self, key):
        """A writer that commits a parent to one store and then its dependent child to another.

        (The WAL logical-run ledgers keep accepting writes during a snapshot; a
        writer of a rollback-journal store simply waits for the snapshot to end.)
        """
        for ledger, name in ((self.first, 'parent'), (self.second, 'child')):
            with ledger.transaction():
                ledger.db.execute('INSERT INTO reservations VALUES (?, ?, 1, 5)', (name, key))

    def pairs(self):
        found = {}
        for run in ('run', 'other'):
            with core.Ledger(self.root, run, PUBLISHED_PR, read_only=True) as ledger:
                for name, key in ledger.db.execute("SELECT name, operation FROM reservations "
                                                   "WHERE name IN ('parent', 'child')"):
                    found.setdefault(name, []).append(key)
        return found

    def restored(self, destination):
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        return recovery.restore(destination, self.home, install=dk_pr_fix.INSTALL)

    def test_concurrent_writers_never_split_the_snapshot_between_stores(self):
        acquired = Counter()

        def writer(index):
            acquired['calls'] += 1
            if acquired['calls'] == self.split + 1:  # Only the first ledger's snapshot is open yet.
                self.write_pair('during-backup')
        with patch.object(recovery, '_acquired', writer):
            manifest = recovery.backup(self.home, self.destination())
        self.assertGreater(acquired['calls'], 4, 'the changed snapshot was retaken')
        self.restored(self.destination())
        self.assertEqual(self.pairs(), {'parent': ['during-backup'], 'child': ['during-backup']})
        self.assertEqual(manifest['files'][NAMESPACE + '/' + self.reference]['file'], 'artifact')

    def test_without_the_retry_the_copies_would_disagree(self):
        # The same interleaving with the change check disabled shows what the check prevents.
        calls = Counter()

        def writer(index):
            calls['n'] += 1
            if calls['n'] == self.split + 1:
                self.write_pair('during-backup')
        with patch.object(recovery, '_acquired', writer), \
                patch.object(recovery, '_changed', lambda before, after: False):
            recovery.backup(self.home, self.destination())
        self.restored(self.destination())
        self.assertEqual(self.pairs(), {'child': ['during-backup']})

    def test_a_writer_that_never_pauses_fails_the_backup_without_leaving_a_partial_one(self):
        with patch.object(recovery, '_acquired', lambda index: self.write_pair('w' + str(os.urandom(4).hex()))):
            with self.assertRaisesRegex(recovery.RecoveryError, 'kept changing'):
                recovery.backup(self.home, self.destination(), attempts=3)
        self.assertFalse(self.destination().exists())

    def test_committed_wal_records_are_captured_and_an_interrupted_writer_is_not(self):
        self.ledger.deadline('ci:run', 600)  # Committed in the WAL, not checkpointed.
        process = subprocess.Popen(
            [sys.executable, '-I', '-c', 'import sqlite3,sys,time\n'
             'db = sqlite3.connect(sys.argv[1], isolation_level=None)\n'
             'db.execute("BEGIN IMMEDIATE")\n'
             'db.execute("INSERT INTO reservations VALUES (\'uncommitted\', \'x\', 1, 1)")\n'
             'print("ready", flush=True); time.sleep(60)', str(core.ledger_path(self.ns, 'run'))],
            stdout=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), b'ready')
        recovery.backup(self.home, self.destination())
        process.kill()  # The controller crashes mid-transaction.
        process.wait()
        self.ledger.close()
        self.feedback.close()
        self.restored(self.destination())
        with core.Ledger(self.root, 'run', PUBLISHED_PR, read_only=True) as ledger:
            names = sorted(row[0] for row in ledger.db.execute('SELECT name FROM reservations'))
            deadlines = ledger.db.execute('SELECT count(*) FROM deadlines').fetchone()[0]
        self.assertEqual((names, deadlines), (['review_cycles'], 1))

    def test_a_hot_journal_left_by_a_killed_rollback_journal_writer_is_rolled_back(self):
        path = feedback.ledger_path(self.ns, 'run')
        self.feedback.db.execute("INSERT INTO sources VALUES ('kept', 'identity')")
        process = subprocess.Popen(
            [sys.executable, '-I', '-c', 'import sqlite3,sys,time\n'
             'db = sqlite3.connect(sys.argv[1], isolation_level=None)\n'
             'db.execute("PRAGMA cache_size=1")\n'  # Spill pages into the file before the commit.
             'db.execute("BEGIN IMMEDIATE")\n'
             'for n in range(3000):\n'
             '    db.execute("INSERT INTO sources VALUES (?, ?)", ("lost%d" % n, "x" * 2000))\n'
             'print("ready", flush=True); time.sleep(60)', str(path)],
            stdout=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), b'ready')
        process.kill()  # Killed mid-commit: a hot journal remains.
        process.wait()
        self.feedback.close()
        self.assertTrue(Path(str(path) + '-journal').is_file())
        manifest = recovery.backup(self.home, self.destination())
        self.assertIn(path.relative_to(self.home / recovery.SUITE_DIR).as_posix(), manifest['stores'])
        self.assertEqual(recovery.upgrade(self.home)['status'], 'applied')
        self.ledger.close()
        self.other.close()
        self.restored(self.destination())
        restored = sqlite3.connect(path)
        self.assertEqual([row[0] for row in restored.execute('SELECT key FROM sources')], ['kept'])
        restored.close()

    def test_restore_verifies_everything_before_it_changes_anything(self):
        recovery.backup(self.home, self.destination())
        state = self.destination() / 'state' / NAMESPACE

        def broken(name, change):
            copy_ = self.destination(name)
            shutil.copytree(self.destination(), copy_)
            change(copy_ / 'state' / NAMESPACE, copy_)
            return copy_

        cases = {
            'differs from its recorded digest': broken('tampered', lambda ns, _: (ns / self.reference).write_text('{}')),
            'differ from its manifest': broken('extra', lambda ns, _: (ns / 'artifacts' / ('f' * 64 + '.json'))
                                              .write_bytes(b'{}')),
            'incomplete backup': broken('interrupted', lambda _, root: (root / 'manifest.json').unlink()),
        }
        target = self.base / 'elsewhere'
        for message, source in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(recovery.RecoveryError, message):
                recovery.restore(source, target)
            self.assertFalse((target / recovery.SUITE_DIR).exists())
        self.assertTrue(state.is_dir())
        # Restore never merges into existing state.
        with self.assertRaisesRegex(recovery.RecoveryError, 'not empty'):
            recovery.restore(self.destination(), self.home)

    def test_foreign_or_newer_state_is_never_imported(self):
        db = sqlite3.connect(feedback.ledger_path(self.ns, 'run'))
        db.execute("UPDATE meta SET value='dk-workflows' WHERE key='suite'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(recovery.RecoveryError, 'not a cf-workflows feedback_state store'):
            recovery.backup(self.home, self.destination())
        self.assertFalse(self.destination().exists())
        db = sqlite3.connect(feedback.ledger_path(self.ns, 'run'))
        db.execute("UPDATE meta SET value='cf-workflows' WHERE key='suite'")
        db.execute('PRAGMA user_version=2')
        db.commit()
        db.close()
        with self.assertRaisesRegex(recovery.RecoveryError, 'newer than this release'):
            recovery.backup(self.home, self.destination())
        foreign = self.destination('foreign')
        foreign.mkdir(parents=True)
        (foreign / 'manifest.json').write_text(json.dumps({'format': 'dk-workflows-state', 'suite': 'dk-workflows'}))
        with self.assertRaisesRegex(recovery.RecoveryError, 'never imported'):
            recovery.restore(foreign, self.base / 'elsewhere')

    def test_saved_configuration_is_restored_only_into_an_empty_place(self):
        config = self.base / 'xdg config' / 'cf-workflows' / 'configuration.json'
        config.parent.mkdir(parents=True)
        document = drafting_tests.Fixture.configuration(None)
        config.write_text(json.dumps(document))
        recovery.backup(self.home, self.destination(), configuration=config)
        config.write_text(json.dumps({**document, 'revision': 'rev-2'}))
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        with self.assertRaisesRegex(recovery.RecoveryError, 'never overwrites'):
            recovery.restore(self.destination(), self.home, configuration=config)
        self.assertFalse((self.home / recovery.SUITE_DIR).exists())
        config.unlink()
        recovery.restore(self.destination(), self.home, configuration=config)
        self.assertEqual(json.loads(config.read_text()), document)
        # dk-workflows settings are not a configuration of this suite.
        config.write_text(json.dumps({'version': '5.17.0', 'settings': {'push': True}}))
        with self.assertRaisesRegex(recovery.RecoveryError, 'never imported'):
            recovery.backup(self.home, self.destination('settings'), configuration=config)

    def test_engine_bundle_is_taken_with_a_stopped_controller_and_restored_together(self):
        host = Host('codex')
        engine = EngineAdapter(host, helpers=dk_pr_fix.INSTALL / 'workflows' / 'helpers')
        engine.controller = 'running'
        with self.assertRaisesRegex(recovery.RecoveryError, 'stop the engine controller'):
            recovery.backup(self.home, self.destination(), engine=engine)
        self.assertFalse(self.destination().exists())
        engine.controller = 'stopped'
        manifest = recovery.backup(self.home, self.destination(), engine=engine)
        self.assertEqual(set(manifest['engine']), {'version', 'database', 'bundle'})
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        with self.assertRaisesRegex(recovery.RecoveryError, 'restore it together'):
            recovery.restore(self.destination(), self.home)
        report = recovery.restore(self.destination(), self.home, engine=engine)
        self.assertEqual(report['engine'], {'quarantined': []})
        self.assertEqual(engine.calls, Counter(status=2, backup=1, bundle=1, retained=1, restore=1))


# -- Partial recovery and fresh-session reuse through the installed helpers -------------------------

class HelperFixture(unittest.TestCase):
    """The shared logical-run ledger (dk-pr-fix and every row that runs over it), at capacity one."""

    setUp_continuation = continuation.Continuation.setUp
    prepare = continuation.Continuation.prepare
    task = continuation.Continuation.task
    report = continuation.Continuation.report
    consumed = continuation.Continuation.consumed

    def setUp(self):
        self.setUp_continuation()
        self.home = self.root.parent / 'xdg state'
        self.ns = namespace(self.home)
        context = patch.dict(os.environ, {'CF_WORKFLOWS_STATE': str(self.ns)})
        context.start()
        self.addCleanup(context.stop)

    def ledger(self, plan):
        ledger = core.Ledger(self.root, plan['logical_run'], plan['pr']['url'])
        self.ledgers.append(ledger)
        return ledger

    def close(self):
        for ledger in self.ledgers:
            ledger.close()
        self.ledgers.clear()

    def run_claim(self, host, plan, ledger, task, lose=False):
        """One capacity-one step: claim, dispatch through the host, accept unless the result is lost."""
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {'task': task})
        if claimed['invoke']:
            session = host.dispatch(claimed['claim'], self.report())
            if not lose:
                app.accept(self.root, self.transport, ledger, plan,
                           {'claim': claimed['claim'], 'agent': continuation.AGENTS[0], 'report': session['result'],
                            'round': 0})
        return claimed

    def publication(self, root):
        provider = publication_tests.Transport()
        ledger = core.Ledger(root, 'publish', PUBLISHED_PR)
        self.ledgers.append(ledger)
        plan = {'logical_run': 'publish', 'pr': core.identity(PUBLISHED_PR), 'instructions': 'Publish it.',
                'authorizations': dict(GRANTS),
                'options': {'permissions': {key: True for key in publication_tests.PERMISSION_INPUTS},
                            'limits': {'ci_reruns_per_head': 3}}}
        ready = {'ready': True, 'complete': True, 'head': HEAD, 'target': BASE, 'clean_review': True,
                 'verified': True, 'ci': {'head': HEAD, 'success': True}, 'evidence': ['accepted:live']}
        publisher = Publication(ledger, provider, root, plan, readiness=lambda intent: copy.deepcopy(ready),
                                source_validator=lambda source: source.startswith('accepted:'))
        return publisher, provider, ledger

    def retained(self, ledger):
        return {table: sorted(tuple(row) for row in ledger.db.execute('SELECT * FROM ' + table))
                for table in ('reservations', 'deadlines', 'extensions', 'claims', 'publication_intents')}



class Helpers(HelperFixture):
    def partial_run_survives_backup_and_restore(self, provider):
        host = Host(provider)
        engine = EngineAdapter(host, helpers=dk_pr_fix.INSTALL / 'workflows' / 'helpers')
        selections = dict(tasks=[['read-feedback', 'assess-scope'], 'plain-review'], limits={'review_cycles': 2})
        plan = self.prepare(**selections)
        ledger = self.ledger(plan)
        reader, scope, review = (self.task(plan, name)['id'] for name in ('read-feedback', 'assess-scope', 'plain-review'))
        self.run_claim(host, plan, ledger, reader)
        lost = self.run_claim(host, plan, ledger, scope, lose=True)  # The session ran; its result is lost.
        self.run_claim(host, plan, ledger, review)
        deadline = ledger.deadline('ci:' + plan['logical_run'], 600)
        ledger.extend('more', 'review_cycles', 1, {**continuation.AUTH, 'request_id': 'more'})
        publisher, github, _ = self.publication(self.root)
        prepared = publisher.prepare(publication_tests.Fixture.proposal('reply', {'body': 'Fixed.'}))
        github.fault = 'before'
        with self.assertRaises(OSError):
            publisher.execute(prepared['id'], publication_tests.Fixture.audience(prepared))
        github.fault = None
        before, writes_before = self.retained(ledger), len(github.writes)
        self.assertEqual(host.sessions['native'], 3)

        # Controller restart after losing every local file and engine record, then restore.
        self.close()
        report = recover(self, self.home, engine)
        run = NAMESPACE.split('/')[1] + '/' + plan['logical_run']
        self.assertEqual(report['runs'][run]['claims'], [lost['claim']])
        self.assertEqual(set(report['runs'][run]['assets'].values()), {'installed'})
        self.assertEqual(report['runs'][NAMESPACE.split('/')[1] + '/publish']['publications'], [prepared['id']])
        self.assertEqual((report['blocked'], report['upgrade']), ([], []))
        self.assertIn(lost['claim'], report['engine']['quarantined'])

        # A fresh session of the same admitted plan reuses every completed sibling without a dispatch.
        plan = self.prepare(**selections)
        ledger = self.ledger(plan)
        self.assertEqual(self.retained(ledger), before)
        self.assertFalse(self.run_claim(host, plan, ledger, reader)['invoke'])
        self.assertFalse(self.run_claim(host, plan, ledger, review)['invoke'])
        # A fresh claim cannot replace the quarantined original; its resumed engine
        # execution delivers the retained session result to the original claim.
        with self.assertRaisesRegex(ValueError, 'resume its originating engine execution'):
            self.run_claim(host, plan, ledger, scope)
        session = host.dispatch(lost['claim'], None)
        app.accept(self.root, self.transport, ledger, plan, {'claim': lost['claim'], 'agent': continuation.AGENTS[0],
                                                             'report': session['result'], 'round': 0})
        self.assertFalse(self.run_claim(host, plan, ledger, scope)['invoke'])
        covered = app.coverage(self.root, self.transport, ledger, plan, {})
        # Every selected occurrence is covered; only the review's verification stages remain.
        self.assertEqual(covered['missing'], ['finding_verification', 'finding_fix'])
        self.assertEqual(host.sessions['native'], 3, 'no duplicate dispatch after recovery')
        self.assertEqual(self.consumed(ledger, 'review_cycles'), 1)
        self.assertEqual(ledger.deadline('ci:' + plan['logical_run'], 3600), deadline)
        self.assertEqual(ledger.granted('review_cycles'), 1)
        self.assertEqual(ledger.db.execute('SELECT count(*) FROM claims').fetchone()[0], 3)
        # The uncertain publication is reconciled read-only and never resent.
        publisher, github, _ = self.publication(self.root)
        self.assertEqual(publisher.reconcile(prepared['id'])['outcome'], 'unknown')
        with self.assertRaises(UncertainEffect):
            publisher.execute(prepared['id'], publication_tests.Fixture.audience(prepared))
        self.assertEqual(len(github.writes), 0)
        self.assertEqual(writes_before, 1)
        self.assertEqual(self.transport.calls['request'], 0)

    def test_codex_host_partial_run_survives_backup_and_restore(self):
        self.partial_run_survives_backup_and_restore('codex')

    def test_claude_host_partial_run_survives_backup_and_restore(self):
        self.partial_run_survives_backup_and_restore('claude')

    def test_changed_resources_after_restore_refresh_only_affected_work_without_replenishing(self):
        host = Host('claude')
        engine = EngineAdapter(host, helpers=dk_pr_fix.INSTALL / 'workflows' / 'helpers')
        selections = dict(tasks=['ruled-review', 'read-feedback'], limits={'review_cycles': 2})
        plan = self.prepare(**selections)
        ledger = self.ledger(plan)
        keys = {name: self.task(plan, name)['id'] for name in ('ruled-review', 'read-feedback')}
        for key in keys.values():
            self.run_claim(host, plan, ledger, key)
        self.close()
        recover(self, self.home, engine)
        (self.root / 'rules').write_text('Changed governing rule\n')
        plan = self.prepare(**selections)
        ledger = self.ledger(plan)
        invoked = {name: self.run_claim(host, plan, ledger, key)['invoke'] for name, key in keys.items()}
        selections = dict(tasks=selections['tasks'])
        self.assertEqual(invoked, {'ruled-review': True, 'read-feedback': False})
        self.assertEqual(host.sessions['native'], 3)
        self.assertEqual(self.consumed(ledger, 'review_cycles'), 2)
        (self.root / 'rules').write_text('Changed again\n')
        plan = self.prepare(**selections)
        with self.assertRaisesRegex(ValueError, 'retained allowance exhausted: review_cycles'):
            self.run_claim(host, plan, self.ledger(plan), keys['ruled-review'])
        # A changed admitted capability set (another execution) refreshes, still within the retained ceiling.
        plan = self.prepare(**selections, execution='exec-2', limits={'review_cycles': 10})
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            self.run_claim(host, plan, self.ledger(plan), keys['ruled-review'])
        self.assertEqual(host.sessions['native'], 3)


class PluginReplacement(HelperFixture):
    """Replaced or deleted plugins: original runs resume only with their original admitted helpers."""

    def driver(self, helpers, plan, steps):
        spec = {'helpers': str(helpers), 'root': str(self.root), 'pr': continuation.PR_URL,
                'evidence': self.transport.evidence, 'execution': plan['execution_fingerprint'], 'steps': steps}
        environment = {'PATH': os.environ['PATH'], 'HOME': str(self.root.parent), **git_fixture.environment(),
                       'CF_WORKFLOWS_STATE': str(self.ns)}
        return subprocess.run([sys.executable, '-I', '-B', str(DRIVER)], input=json.dumps(spec).encode(),
                              capture_output=True, env=environment, cwd=self.root.parent, timeout=120)

    def replaced(self):
        install = self.root.parent / 'replaced plugin' / 'dk-pr-fix'
        shutil.copytree(dk_pr_fix.INSTALL, install, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        with (install / 'workflows' / 'helpers' / 'prfix' / 'ci.py').open('a') as stream:
            stream.write('\n# A later release.\n')
        dk_pr_fix.write_closure(install)
        dk_pr_fix.verify_installation(install)
        return install

    def unfinished(self):
        plan = self.prepare(tasks=[['read-feedback', 'assess-scope']])
        ledger = self.ledger(plan)
        host = Host('codex')
        self.run_claim(host, plan, ledger, self.task(plan, 'read-feedback')['id'])
        lost = self.run_claim(host, plan, ledger, self.task(plan, 'assess-scope')['id'], lose=True)
        self.close()
        return plan, host, lost

    def test_original_admitted_helpers_resume_after_replacement_and_deletion(self):
        plan, host, lost = self.unfinished()
        engine = EngineAdapter(host, helpers=dk_pr_fix.INSTALL / 'workflows' / 'helpers')
        install = self.replaced()  # The original installation is no longer used: deleted or replaced.
        destination = self.root.parent / 'backup'
        recovery.backup(self.home, destination, engine=engine, install=install)
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        report = recovery.restore(destination, self.home, engine=engine, install=install)
        run = NAMESPACE.split('/')[1] + '/' + plan['logical_run']
        self.assertEqual(report['runs'][run]['assets'], {plan['execution_fingerprint']: 'retained'})
        self.assertEqual(report['blocked'], [])
        step = [{'name': 'reader', 'operation': 'claim',
                 'input': {'plan': 'artifacts/' + core.digest(plan) + '.json', 'cycle': 1, 'publication': '',
                           'task': self.task(plan, 'read-feedback')['id']}}]
        # The replacement's helpers refuse the original plan instead of running it.
        refused = self.driver(install / 'workflows' / 'helpers', plan, step)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('prepared helper closure differs before dispatch', refused.stderr.decode())
        # The engine's retained admitted helpers resume it, reusing the completed sibling.
        retained = self.root.parent / 'engine assets'
        engine.materialize(destination / 'engine' / 'bundle', retained)
        resumed = self.driver(retained, plan, step)
        self.assertEqual(resumed.returncode, 0, resumed.stderr.decode()[-4000:])
        output = json.loads(resumed.stdout)
        self.assertFalse(output['results']['reader']['result']['output']['invoke'])
        for module in output['modules']:
            self.assertTrue(Path(module).is_relative_to(retained), module)
        self.assertEqual(host.sessions['native'], 2)

    def test_cli_bound_engine_reports_original_helpers_retained_in_bundle_assets(self):
        plan, host, lost = self.unfinished()
        engine = CliEngineFixture.engine_setup(self)
        # The engine bundle retains the original helpers as a container job's asset tree.
        engine_bundle(self.bundle_source, [installed_helpers()], versioned=True)
        install = self.replaced()
        destination = self.root.parent / 'backup'
        recovery.backup(self.home, destination, engine=engine, install=install)
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        remove_tree(self.workspace / recovery.ENGINE_STATE)
        report = recovery.restore(destination, self.home, engine=engine, install=install)
        run = NAMESPACE.split('/')[1] + '/' + plan['logical_run']
        self.assertEqual(report['runs'][run]['assets'], {plan['execution_fingerprint']: 'retained'})
        self.assertEqual(report['runs'][run]['claims'], [lost['claim']])
        self.assertEqual(report['blocked'], [])
        # The restored tree holds the original helpers, which still resume the original plan.
        [tree] = report['engine']['assets']
        retained = self.workspace / recovery.ENGINE_STATE / 'assets' / tree / 'files' / 'helpers'
        step = [{'name': 'reader', 'operation': 'claim',
                 'input': {'plan': 'artifacts/' + core.digest(plan) + '.json', 'cycle': 1, 'publication': '',
                           'task': self.task(plan, 'read-feedback')['id']}}]
        resumed = self.driver(retained, plan, step)
        self.assertEqual(resumed.returncode, 0, resumed.stderr.decode()[-4000:])
        self.assertFalse(json.loads(resumed.stdout)['results']['reader']['result']['output']['invoke'])
        self.assertEqual(host.sessions['native'], 2)
        # A bundle that retains only the replacement's helpers leaves the same run blocked.
        engine_bundle(self.bundle_source, [installed_helpers(install)], versioned=True)
        other = self.root.parent / 'replacement backup'
        recovery.backup(self.home, other, engine=engine, install=install)
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        remove_tree(self.workspace / recovery.ENGINE_STATE)
        report = recovery.restore(other, self.home, engine=engine, install=install)
        self.assertEqual(report['runs'][run]['assets'], {plan['execution_fingerprint']: 'missing'})
        self.assertEqual(report['blocked'], [run])

    def test_missing_original_assets_block_recovery_instead_of_using_replacements(self):
        plan, host, lost = self.unfinished()
        engine = EngineAdapter(host, helpers=dk_pr_fix.INSTALL / 'workflows' / 'helpers', retain=False)
        install = self.replaced()
        report = recover(self, self.home, engine, install=install)
        run = NAMESPACE.split('/')[1] + '/' + plan['logical_run']
        self.assertEqual(report['runs'][run]['assets'], {plan['execution_fingerprint']: 'missing'})
        self.assertEqual(report['runs'][run]['claims'], [lost['claim']])
        self.assertEqual(report['blocked'], [run])
        # The evidence is still restored; only resumption is blocked.
        with core.Ledger(self.root, plan['logical_run'], plan['pr']['url'], read_only=True) as ledger:
            self.assertEqual(ledger.db.execute('SELECT count(*) FROM claims').fetchone()[0], 2)


# -- The engine adapter bound to the agent-workflow CLI --------------------------------------------

ORIGIN = 'a' * 32
FAKE_CLI = '''
import json, os, signal, sys
from pathlib import Path
argv = sys.argv[1:]
verb, args = argv[0], argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps(argv) + '\\n')
if args[:1] == ['--workspace']:
    args = args[2:]
def emit(value):
    print(json.dumps(value))
    sys.exit(0)
if verb == 'version':
    emit({'version': '0.2.2', 'commit': 'development'})
if verb == 'backup':
    Path(args[0]).write_bytes(b'fixture engine database')
    emit({'database': args[0], 'scope': 'database-only'})
if verb == 'bundle':
    Path(args[0]).write_bytes(Path(os.environ['FAKE_BUNDLE']).read_bytes())
    emit({'bundle': args[0], 'scope': 'engine-owned recovery data; native credential stores excluded'})
if verb == 'restore':
    if Path(os.environ['FAKE_FAIL']).exists():
        sys.exit('restore: fixture failure')
    emit({'restored': args[0], 'retained_database': '',
          'recovery': 'restored runnable work requires explicit uncertain-retry acknowledgement'})
if verb == 'stop':
    os.kill(int(Path(os.environ['FAKE_OWNER']).read_text()), signal.SIGTERM)
    emit({'stopping': True})
if verb == 'resume':
    if '--retry-uncertain' not in args:
        sys.exit('controller: restored effects require explicit retry_uncertain acknowledgement')
    emit({'id': args[-1], 'status': 'queued'})
sys.exit('unconfigured fake operation: ' + verb)
'''
HOLD_LOCK = '''
import fcntl, sys, time
handle = open(sys.argv[1], 'a')
fcntl.flock(handle, fcntl.LOCK_EX)
print('held', flush=True)
time.sleep(60)
'''


def materialized(name):
    """The engine's materialized mode of a retained file: 0555 when executable (here: a .sh), else 0444."""
    return 0o555 if name.endswith('.sh') else 0o444


def asset_tree(files, versioned):
    """bundle.json of a retained asset tree in the engine's helper-bundle or versioned asset-bundle format.

    A historical helper bundle records the materialized mode; a versioned one the
    original source mode (0755 or 0644 here), which the engine materializes.
    """
    entries = [{'path': path, 'mode': materialized(path), 'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest()} for path, data in sorted(files.items())]
    ident = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    if versioned:
        entries = [{**entry, 'kind': 'file', 'mode': entry['mode'] | 0o200} for entry in entries]
        return ident, {'version': 1, 'origin_run_id': ORIGIN, 'id': ident, 'entries': entries}
    return ident, {'origin_run_id': ORIGIN, 'id': ident, 'files': entries}


def engine_bundle(path, trees=(), versioned=False, extra=(), tamper=None, mode=None, owner=True):
    """A synthetic recovery bundle in the layout `agent-workflow bundle` writes (pkg/workflows/bundle.go at 8758775).

    It is built here, not exported by the engine. trees are {path: bytes}
    retained asset trees, with every directory entry and the materialized modes
    the engine exports (0700 above files/, 0555 directories and 0444/0555 files
    in it); extra are (name, bytes or None for a directory, or ('link', target))
    entries added as they are; tamper rewrites one retained file's bytes, and
    mode its mode, after its manifest is written; owner=False leaves out owner.json.
    """
    import io
    import tarfile
    added = set()

    def add(archive, name, data=None, link=None, mode=None):
        if name in added and data is None and link is None:
            return
        added.add(name)
        info = tarfile.TarInfo(name)
        if link is not None:
            info.type, info.linkname = tarfile.SYMTYPE, link
            archive.addfile(info)
        elif data is None:
            info.type, info.mode = tarfile.DIRTYPE, 0o755 if mode is None else mode
            archive.addfile(info)
        else:
            info.size, info.mode = len(data), 0o444 if mode is None else mode
            archive.addfile(info, io.BytesIO(data))
    with tarfile.open(path, 'w:gz') as archive:
        add(archive, 'manifest.json', json.dumps({'format': 1, 'scope': 'engine-owned recovery state'}).encode())
        add(archive, 'state.db', b'fixture engine database')
        add(archive, 'invocations')
        add(archive, 'invocations/' + 'b' * 32 + '/complete.json', b'{}')
        for files in trees:
            ident, manifest = asset_tree(files, versioned)
            base = f'assets/{ORIGIN}/{ident}/'
            for directory in ('assets', f'assets/{ORIGIN}', base[:-1]):
                add(archive, directory, mode=0o700)
            add(archive, base + 'bundle.json', json.dumps(manifest).encode(), mode=0o444)
            add(archive, base + 'files', mode=0o555)
            for name, data in sorted(files.items()):
                parts = name.split('/')
                for depth in range(1, len(parts)):
                    add(archive, base + 'files/' + '/'.join(parts[:depth]), mode=0o555)
                first = name == sorted(files)[0]
                add(archive, base + 'files/' + name, tamper if tamper is not None and first else data,
                    mode=mode if mode is not None and first else materialized(name))
            if owner:
                add(archive, base + 'owner.json', json.dumps({'origin_run_id': ORIGIN, 'stage_id': 'c' * 32}).encode(),
                    mode=0o600)
        for name, data in extra:
            if isinstance(data, tuple):
                add(archive, name, link=data[1])
            else:
                add(archive, name, data)
    return path


def remove_tree(path):
    """Remove a restored engine state, whose retained asset trees are read-only as the engine keeps them."""
    # Opens every directory first instead of rmtree(onexc=...), which needs Python 3.12; the host minimum is 3.11.
    for root, directories, _ in os.walk(path):
        os.chmod(root, 0o700)
        for name in directories:
            if not os.path.islink(os.path.join(root, name)):
                os.chmod(os.path.join(root, name), 0o700)
    shutil.rmtree(path)


def tree_modes(root):
    """{relative path: permission bits} of every entry under root."""
    return {path.relative_to(root).as_posix(): stat.S_IMODE(path.lstat().st_mode) for path in root.rglob('*')}


def installed_helpers(install=dk_pr_fix.INSTALL):
    """The installation's helpers and agents as a container job's retained files place them."""
    workflows = Path(install) / 'workflows'
    return {Path(prefix, name).as_posix(): data for prefix in ('helpers', 'agents')
            for name, data in helper_files(workflows / prefix).items()
            if '__pycache__' not in name and not name.endswith('.pyc')}


class CliEngineFixture:
    def engine_setup(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw engine ')
        self.addCleanup(temp.cleanup)
        self.engine_base = Path(temp.name)
        self.workspace = self.engine_base / 'engine workspace'
        (self.workspace / recovery.ENGINE_STATE).mkdir(parents=True)
        self.cli = self.engine_base / 'bin' / 'agent-workflow'
        self.cli.parent.mkdir()
        self.cli.write_text(f'#!{sys.executable} -I\n' + FAKE_CLI)
        self.cli.chmod(0o755)
        self.log = self.engine_base / 'calls.jsonl'
        self.bundle_source = self.engine_base / 'prepared bundle'
        self.environment = {'PATH': os.environ['PATH'], 'FAKE_LOG': str(self.log),
                            'FAKE_BUNDLE': str(self.bundle_source), 'FAKE_FAIL': str(self.engine_base / 'fail'),
                            'FAKE_OWNER': str(self.engine_base / 'owner')}
        return recovery.Engine(self.cli, self.workspace, environment=self.environment, timeout=60)

    def calls(self):
        return [json.loads(line)[0] for line in self.log.read_text().splitlines()] if self.log.exists() else []


class EngineBinding(CliEngineFixture, unittest.TestCase):
    """recovery.Engine against a fail-closed fake of the agent-workflow 8758775 maintenance CLI."""

    def setUp(self):
        git_fixture.isolate(self)
        self.engine = self.engine_setup()
        self.home = self.engine_base / 'xdg state'
        namespace(self.home)

    def test_retained_asset_trees_map_to_admitted_helper_digests(self):
        helpers = installed_helpers()
        current = recovery.installation()['helpers']
        agents_only = {name: data for name, data in helpers.items() if name.startswith('agents/')}
        for versioned in (False, True):
            bundle = engine_bundle(self.engine_base / f'bundle {versioned}', [helpers, agents_only], versioned)
            self.assertEqual(len(recovery.bundle_assets(bundle)), 2)
            self.assertEqual(self.engine.retained(bundle), {current})
        changed = {**helpers, 'helpers/prfix/ci.py': helpers['helpers/prfix/ci.py'] + b'\n# later\n'}
        bundle = engine_bundle(self.engine_base / 'changed', [changed])
        self.assertNotIn(current, self.engine.retained(bundle))
        self.assertEqual(self.engine.retained(engine_bundle(self.engine_base / 'empty')), set())
        self.assertEqual(self.calls(), [])

    def test_tampered_or_foreign_bundle_entries_reject_before_any_change(self):
        helpers = {'helpers/action.py': b'print(1)\n', 'helpers/prfix/core.py': b'x = 1\n'}
        relative = engine_bundle(self.engine_base / 'relative link', extra=[
            ('workspaces/w', None), ('workspaces/w/tree', None), ('workspaces/w/current', ('link', 'tree'))])
        self.assertEqual(recovery.bundle_assets(relative), {})  # A link inside its workspaces tree is kept.
        cases = {
            'differ from their manifest': engine_bundle(self.engine_base / 'tampered', [helpers], tamper=b'evil'),
            'differ from their manifest ': engine_bundle(
                self.engine_base / 'unlisted', [helpers],
                extra=[(f'assets/{ORIGIN}/{asset_tree(helpers, False)[0]}/files/helpers/extra.py', b'x')]),
            'no bundle.json': engine_bundle(self.engine_base / 'orphan',
                                            extra=[(f'assets/{ORIGIN}/{"d" * 64}/files/helpers/a.py', b'x')]),
            'outside a retained tree': engine_bundle(self.engine_base / 'stray', extra=[('assets/loose.py', b'x')]),
            'not an engine recovery bundle entry': engine_bundle(self.engine_base / 'foreign',
                                                                 extra=[('secrets/token', b'x')]),
            'not an engine recovery bundle entry ': engine_bundle(self.engine_base / 'escape',
                                                                  extra=[('invocations/../../escape', b'x')]),
            'not a file or directory': engine_bundle(self.engine_base / 'link',
                                                     extra=[('providers/auth', ('link', '/etc/passwd'))]),
            'leaves its workspaces tree': engine_bundle(self.engine_base / 'workspace link',
                                                        extra=[('workspaces/w/escape', ('link', '../../assets'))]),
            'duplicate engine bundle entry': engine_bundle(self.engine_base / 'duplicate',
                                                           extra=[('state.db', b'another database')]),
            'differ from their manifest  ': engine_bundle(self.engine_base / 'writable', [helpers], mode=0o644),
            'no owner.json': engine_bundle(self.engine_base / 'ownerless', [helpers], versioned=True, owner=False),
        }
        database = self.engine_base / 'database'
        database.write_bytes(b'fixture engine database')
        for message, bundle in cases.items():
            with self.subTest(message):
                with self.assertRaisesRegex(recovery.RecoveryError, message.strip()):
                    self.engine.restore(database, bundle)
                self.assertEqual(list((self.workspace / recovery.ENGINE_STATE).iterdir()), [])
        self.assertEqual(self.calls(), [])

    def test_backup_needs_the_execution_owner_released_and_stop_waits_for_it(self):
        lock = self.workspace / recovery.ENGINE_STATE / 'owner.lock'
        owner = subprocess.Popen([sys.executable, '-I', '-c', HOLD_LOCK, str(lock)], stdout=subprocess.PIPE)
        self.addCleanup(owner.wait)
        self.addCleanup(owner.kill)
        self.assertEqual(owner.stdout.readline(), b'held\n')
        (self.engine_base / 'owner').write_text(str(owner.pid))
        self.assertEqual(self.engine.status(), {'controller': 'running', 'version': '0.2.2'})
        destination = self.engine_base / 'backup'
        with self.assertRaisesRegex(recovery.RecoveryError, 'stop the engine controller'):
            recovery.backup(self.home, destination, engine=self.engine)
        self.assertFalse(destination.exists())
        with self.assertRaisesRegex(recovery.RecoveryError, 'stop the engine controller'):
            self.engine.restore(self.engine_base / 'database', self.engine_base / 'bundle')
        self.assertEqual(self.engine.stop(), {'stopping': True})
        self.assertEqual(self.engine.status()['controller'], 'stopped')
        self.assertEqual(self.engine.stop(), {'stopped': True})  # Already stopped: nothing is sent.
        self.assertEqual(self.calls(), ['version', 'version', 'stop', 'version'])

    def test_backup_restore_and_resume_go_through_the_cli_and_never_merge(self):
        helpers = {**installed_helpers(), 'bin/run.sh': b'#!/bin/sh\n'}
        engine_bundle(self.bundle_source, [helpers], versioned=True)
        destination = self.engine_base / 'backup'
        manifest = recovery.backup(self.home, destination, engine=self.engine)
        self.assertEqual(manifest['engine']['version'], '0.2.2')
        self.assertEqual(manifest['engine']['bundle'], hashlib.sha256(self.bundle_source.read_bytes()).hexdigest())
        # Total loss of the suite state and of the engine's own state, then restore.
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        remove_tree(self.workspace / recovery.ENGINE_STATE)
        report = recovery.restore(destination, self.home, engine=self.engine)
        [tree] = recovery.bundle_assets(self.bundle_source)
        self.assertEqual(report['engine'], {
            'restored': str(destination / 'engine' / 'database'), 'retained_database': '', 'assets': [tree],
            'recovery': 'restored runnable work requires explicit uncertain-retry acknowledgement'})
        restored = self.workspace / recovery.ENGINE_STATE / 'assets' / tree / 'files'
        self.assertEqual({name: (restored / name).read_bytes() for name in helpers}, helpers)
        # Exact permissions, as the engine verifies them on resume, on use and on the next bundle.
        modes = tree_modes(restored)
        self.assertEqual({name: modes.pop(name) for name in helpers}, {name: materialized(name) for name in helpers})
        self.assertEqual(set(modes.values()), {0o555})  # Every directory under files/.
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(restored.parent.stat().st_mode), 0o700)
        self.assertFalse((self.workspace / recovery.ENGINE_STATE / 'state.db').exists())  # Only through the CLI.
        self.assertEqual(self.calls(), ['version', 'backup', 'bundle', 'restore'])
        # Restoring again over the restored engine state refuses before calling the CLI.
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        with self.assertRaisesRegex(recovery.RecoveryError, 'restore never merges'):
            recovery.restore(destination, self.home, engine=self.engine)
        self.assertFalse((self.home / recovery.SUITE_DIR).exists())
        self.assertEqual(self.calls().count('restore'), 1)
        # Restored runnable work resumes only with the uncertain-retry acknowledgement.
        with self.assertRaisesRegex(recovery.RecoveryError, 'retry_uncertain acknowledgement'):
            self.engine.resume('e' * 32)
        self.assertEqual(self.engine.resume('e' * 32, retry_uncertain=True), {'id': 'e' * 32, 'status': 'queued'})

    def test_failed_engine_restore_removes_the_extracted_trees_so_a_retry_can_run(self):
        engine_bundle(self.bundle_source, [installed_helpers()], extra=[
            ('workspaces', None), ('workspaces/w', None), ('workspaces/w/current', ('link', 'tree'))])
        database = self.engine_base / 'database'
        database.write_bytes(b'fixture engine database')
        state = self.workspace / recovery.ENGINE_STATE
        (state / 'owner.lock').touch()  # Engine state outside the bundle paths stays.
        (self.engine_base / 'fail').touch()
        with self.assertRaisesRegex(recovery.RecoveryError, 'restore: fixture failure'):
            self.engine.restore(database, self.bundle_source)
        self.assertEqual(sorted(path.name for path in state.iterdir()), ['owner.lock'])
        (self.engine_base / 'fail').unlink()
        report = self.engine.restore(database, self.bundle_source)
        self.assertEqual(report['assets'], sorted(recovery.bundle_assets(self.bundle_source)))
        self.assertEqual(os.readlink(state / 'workspaces' / 'w' / 'current'), 'tree')
        self.assertEqual(self.calls(), ['restore', 'restore'])

    def test_unconfigured_cli_operations_fail(self):
        with self.assertRaisesRegex(recovery.RecoveryError, 'unconfigured fake operation: cancel'):
            self.engine.command('cancel', 'e' * 32)


FIXTURE_WORKFLOW = '''name: cf-workflows recovery fixture
"on":
  workflow_call:
    inputs:
      log: {type: string, required: true}
jobs:
  dispatch:
    steps:
      - id: once
        argv: [sh, -c, 'echo dispatched >> "$1"', fixture, "${{ inputs.log }}"]
  decide:
    needs: [dispatch]
    steps:
      - id: question
        uses: input/wait
        with:
          prompt: Continue after the restore?
          evidence-id: recovery-fixture-v1
      - argv: [sh, -c, 'echo answered >> "$1"', fixture, "${{ inputs.log }}"]
'''


@unittest.skipUnless(os.environ.get('CF_AGENT_WORKFLOW'),
                     'set CF_AGENT_WORKFLOW to a local agent-workflow clone with the pinned toolchain commit')
class EngineIntegration(unittest.TestCase):
    """backup -> loss -> restore -> resume on an engine built from the pinned unreleased commit.

    The fixture run has one host command step and one external-input gate; no
    provider, container or network is used. It establishes no released engine.
    """

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('check_package_build', ROOT / 'scripts/check_package_build.py')
        build_check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_check)
        temp = tempfile.TemporaryDirectory(prefix='cfw engine integration ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.cli = build_check.build_cli(Path(os.environ['CF_AGENT_WORKFLOW']), self.base)
        self.workspace = self.base / 'engine workspace'
        (self.workspace / 'workflows').mkdir(parents=True)
        (self.workspace / 'workflows' / 'main.yml').write_text(FIXTURE_WORKFLOW)
        (self.workspace / 'agents.yml').write_text(
            'agents:\n  unused:\n    provider: codex-cli\n    execution: {mode: local}\n')
        self.environment = {'PATH': os.environ['PATH'], 'HOME': str(self.base), 'LANG': 'C.UTF-8'}
        self.engine = recovery.Engine(self.cli, self.workspace, environment=self.environment, timeout=120)
        self.addCleanup(self.engine.stop)
        self.dispatches = self.base / 'dispatches.log'
        self.home = self.base / 'xdg state'
        namespace(self.home)

    def until(self, run, status):
        import time
        deadline = time.monotonic() + 60
        while (current := self.engine.command('status', run))['status'] != status:
            self.assertLess(time.monotonic(), deadline, current['status'])
            time.sleep(0.1)
        return current

    def test_restored_waiting_run_resumes_once_after_acknowledgement_without_duplicate_dispatch(self):
        started = subprocess.run([str(self.cli), 'run', '--foreground', '--workspace', str(self.workspace),
                                  '--input', f'log={self.dispatches}', 'workflows/main.yml'],
                                 capture_output=True, env=self.environment, cwd=self.workspace, timeout=120)
        self.assertEqual(started.returncode, 2, started.stderr.decode())  # Waiting for external input.
        run = json.loads(started.stdout)['id']
        self.assertEqual(self.dispatches.read_text(), 'dispatched\n')
        destination = self.base / 'backup'
        manifest = recovery.backup(self.home, destination, engine=self.engine)
        self.assertEqual(manifest['engine']['version'], '0.2.2')
        # Total loss of the suite state and of the engine's own state, then restore.
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        remove_tree(self.workspace / recovery.ENGINE_STATE)
        report = recovery.restore(destination, self.home, engine=self.engine)
        self.assertEqual(report['engine']['recovery'],
                         'restored runnable work requires explicit uncertain-retry acknowledgement')
        quarantined = self.engine.command('status', run)
        self.assertEqual((quarantined['status'], quarantined['failure_class']),
                         ('needs_attention', 'restore_uncertainty'))
        # The restarted controller refuses restored work without the acknowledgement.
        with self.assertRaisesRegex(recovery.RecoveryError, 'retry_uncertain acknowledgement'):
            self.engine.resume(run)
        self.engine.resume(run, retry_uncertain=True)
        self.until(run, 'waiting')
        [gate] = self.engine.command('inputs', run)
        answer = self.base / 'answer.json'
        answer.write_text('true')
        self.engine.command('answer', '--gate', gate['gate_id'], '--evidence-id', gate['evidence_id'],
                            '--answer-id', 'restored-delivery', '--answer-file', answer, run)
        finished = self.until(run, 'succeeded')
        self.assertEqual({job: value['status'] for job, value in finished['jobs'].items()},
                         {'dispatch': 'succeeded', 'decide': 'succeeded'})
        # The completed step was not dispatched again; the answered step ran once.
        self.assertEqual(self.dispatches.read_text(), 'dispatched\nanswered\n')
        self.engine.stop()
        self.assertEqual(self.engine.status()['controller'], 'stopped')


# -- Upgrades -----------------------------------------------------------------------------------------

class Upgrade(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix='cfw upgrade ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / 'xdg state'
        self.suite = self.home / recovery.SUITE_DIR
        self.ns = namespace(self.home)
        (self.ns / 'runs').mkdir()
        context = patch.dict(os.environ, {'CF_WORKFLOWS_STATE': str(self.ns)})
        context.start()
        self.addCleanup(context.stop)
        self.root = self.base / 'checkout'
        self.root.mkdir()
        # A version 1 ledger with a consumed allowance and an absolute window.
        database = sqlite3.connect(core.ledger_path(self.ns, 'old'))
        database.executescript("""
            CREATE TABLE groups(id TEXT PRIMARY KEY, pr TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE reservations(name TEXT NOT NULL, operation TEXT NOT NULL, amount INTEGER NOT NULL,
                                      limit_value INTEGER NOT NULL, PRIMARY KEY(name,operation));
            CREATE TABLE deadlines(name TEXT PRIMARY KEY, value REAL NOT NULL);
            INSERT INTO groups VALUES('old', 'https://github.com/o/r/pull/7', 1);
            INSERT INTO reservations VALUES('review_cycles', 'original', 1, 1);
            INSERT INTO deadlines VALUES('ci:old', 4102444800);
            PRAGMA user_version=1;
        """)
        database.close()
        # A version 2 ledger with an uncertain and a completed publication.
        pr = core.identity(PUBLISHED_PR)
        self.pending = {'run': 'mid', 'pr': pr, 'kind': 'reply', 'payload': {'body': 'x'}}
        applied = {'run': 'mid', 'pr': pr, 'kind': 'reply', 'payload': {'body': 'y'}}
        database = sqlite3.connect(core.ledger_path(self.ns, 'mid'))
        database.executescript("""
            CREATE TABLE groups(id TEXT PRIMARY KEY, pr TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE deadlines(name TEXT PRIMARY KEY, value REAL NOT NULL, started REAL);
            CREATE TABLE publication_intents(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
             value TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '');
            PRAGMA user_version=2;
        """)
        database.execute("INSERT INTO groups VALUES('mid', ?, 1)", (PUBLISHED_PR,))
        database.execute("INSERT INTO publication_intents VALUES('applied', ?, ?, 'completed', ?)",
                         (core.digest(applied), core.canonical(applied).decode(),
                          core.canonical({'result': {'id': 9}}).decode()))
        database.execute("INSERT INTO publication_intents VALUES('pending', ?, ?, 'uncertain', ?)",
                         (core.digest(self.pending), core.canonical(self.pending).decode(),
                          core.canonical({'dispatch_response': {'id': 10}}).decode()))
        database.commit()
        database.close()
        self.current = decisions.Ledger(decisions.ledger_path(self.ns, 'mid'), logical_run='mid', pr=PUBLISHED_PR)
        self.current.close()
        self.config = self.base / 'xdg config' / 'cf-workflows' / 'configuration.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(drafting_tests.Fixture.configuration(None)))

    def versions(self):
        return {relative: recovery.check_store(self.suite / relative, name, relative)
                for relative, name in recovery.audit(self.suite)['stores'].items()}

    def test_interrupted_upgrade_resumes_with_its_one_retained_backup(self):
        config = self.config.read_bytes()
        migrated = []

        def interrupt(relative):
            migrated.append(relative)
            if len(migrated) == 1:
                raise KeyboardInterrupt('upgrade interrupted')
        with patch.object(recovery, '_migrated', interrupt), self.assertRaises(KeyboardInterrupt):
            recovery.upgrade(self.home, configuration=self.config)
        journal = json.loads((self.suite / recovery.JOURNAL).read_text())
        self.assertEqual(journal['status'], 'migrating')
        retained = self.suite / journal['backup'] / 'manifest.json'
        original = retained.read_bytes()
        self.assertEqual(sorted(record['schema_version'] for record in json.loads(original)['stores'].values()),
                         [1, 1, 2])
        with self.assertRaisesRegex(recovery.RecoveryError, 'upgrade is in progress'):
            recovery.backup(self.home, self.base / 'during')
        finished = recovery.upgrade(self.home, configuration=self.config)
        self.assertEqual(finished['status'], 'applied')
        self.assertEqual(retained.read_bytes(), original, 'the pre-upgrade backup is never replaced')
        self.assertEqual(set(self.versions().values()), {1, 3})
        self.assertEqual(recovery.upgrade(self.home), finished)
        self.assertEqual(self.config.read_bytes(), config, 'the saved configuration is never rewritten')
        # Retained history, windows and uncertainty survive the migration.
        with core.Ledger(self.root, 'old', PUBLISHED_PR) as ledger:
            self.assertEqual(ledger.deadline('ci:old', 60), 4102444800)
            with self.assertRaisesRegex(ValueError, 'exhausted'):
                ledger.reserve('review_cycles', 'replacement', 5)
        with core.Ledger(self.root, 'mid', PUBLISHED_PR, read_only=True) as ledger:
            states = dict(tuple(row) for row in ledger.db.execute('SELECT id, state FROM publication_intents'))
            lock = ledger.db.execute('SELECT intent FROM publication_lock').fetchone()[0]
        self.assertEqual((states, lock), ({'applied': 'applied', 'pending': 'pending'}, 'pending'))
        # A backup of the upgraded home leaves the retained pre-upgrade backup out.
        manifest = recovery.backup(self.home, self.base / 'after')
        self.assertFalse(any(path.startswith(recovery.UPGRADES) for path in manifest['files']))
        self.assertIn(recovery.JOURNAL, manifest['files'])

    def test_restored_pre_upgrade_backup_reports_the_needed_migration(self):
        journal = recovery.upgrade(self.home)
        backup = self.suite / journal['backup']
        report = recovery.restore(backup, self.base / 'restored')
        self.assertEqual(len(report['upgrade']), 2)
        self.assertEqual(report['runs'][NAMESPACE.split('/')[1] + '/mid']['publications'], ['pending'])

    def snapshot(self):
        return {path: path.read_bytes() for path in self.suite.rglob('*') if path.is_file()}

    def test_newer_or_foreign_versions_refuse_before_any_change(self):
        database = sqlite3.connect(decisions.ledger_path(self.ns, 'mid'))
        database.execute('PRAGMA user_version=2')
        database.close()
        before = self.snapshot()
        with self.assertRaisesRegex(recovery.RecoveryError, 'newer than this release'):
            recovery.upgrade(self.home)
        self.assertEqual(self.snapshot(), before)
        database = sqlite3.connect(decisions.ledger_path(self.ns, 'mid'))
        database.execute('PRAGMA user_version=1')
        database.close()
        before = self.snapshot()
        for document, message in (({'version': '5.17.0', 'settings': {}}, 'never imported'),
                                  ({**drafting_tests.Fixture.configuration(None), 'schema_version': 2},
                                   'unsupported configuration schema')):
            self.config.write_text(json.dumps(document))
            with self.subTest(message=message), self.assertRaisesRegex(recovery.RecoveryError, message):
                recovery.upgrade(self.home, configuration=self.config)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(self.versions().values()), [1, 1, 2])


# -- Store-level recovery for the rows with their own ledgers ----------------------------------------

class ProseRecovery(prose_tests.Fixture):
    """dk-plain-english and dk-asd-ste100: an accepted pass is reused after restore without a session."""

    def setUp(self):
        super().setUp()
        self.acceptance.close()
        self.acceptance = acceptance.Ledger(self.base / 'state' / recovery.SUITE_DIR / 'configuration.sqlite3')
        self.addCleanup(lambda: self.acceptance.close())

    def test_accepted_pass_is_reused_after_restore(self):
        self.assertEqual(self.clean_up('dk-plain-english', prose_tests.edit(prose_tests.GOOD))['status'], 'complete')
        self.prose_ledger.close()
        self.acceptance.close()
        recover(self, self.base / 'state', EngineAdapter(self.engine))
        self.prose_ledger = prose.Ledger(prose.ledger_path(self.base / 'state', prose_tests.REPOSITORY))
        self.addCleanup(self.prose_ledger.close)
        self.acceptance = acceptance.Ledger(self.base / 'state' / recovery.SUITE_DIR / 'configuration.sqlite3')
        again = self.clean_up('dk-plain-english')
        self.assertEqual((again['status'], again['units']),
                         ('reused', {'docs/guide.md': 'reused', 'src/mod.py': 'reused'}))
        self.calls(capabilities=1)
        self.assertEqual(self.engine.sessions, Counter({'plain-english': 1}))


class LedgerHome:
    """Puts a harness's per-run ledgers into a repository namespace of a suite state home."""

    def state(self):
        return namespace(self.base / 'xdg state')


class DecisionRecovery(LedgerHome, decision_tests.Fixture):
    def ledger(self, admitted):
        run = admitted['snapshot']['request']['logical_run']
        ledger = decisions.Ledger(decisions.ledger_path(self.state(), run), logical_run=run, pr=decision_tests.PR)
        self.addCleanup(ledger.close)
        return ledger

    def test_lost_session_response_recovers_its_original_claim_after_restore(self):
        admitted = self.admit()
        ledger = self.ledger(admitted)
        lost = self.reconcile(admitted, ledger, configuration_tests.session(
            decision_tests.result(decision_tests.API, decision_tests.STORE)), lose=1)
        self.assertEqual(lost['status'], 'blocked')
        ledger.close()
        report = recover(self, self.base / 'xdg state', EngineAdapter(self.engine))
        self.assertEqual(report['engine'], {'quarantined': [lost['claim']]})
        ledger = self.ledger(admitted)
        recovered = self.reconcile(admitted, ledger)
        self.assertEqual((recovered['status'], recovered['claim']), ('complete', lost['claim']))
        ledger.close()
        recover(self, self.base / 'xdg state', EngineAdapter(self.engine))
        unchanged = self.reconcile(admitted, self.ledger(admitted))
        self.assertEqual(unchanged['status'], 'complete')
        self.assertEqual(sum(self.engine.sessions.values()), 1)


class InstalledDecisionRecovery(installed_decision_tests.Fixture):
    """dk-pr-decisions through the installed helpers: the reconciliation record survives backup and restore."""

    def setUp(self):
        super().setUp()
        self.ledger.close()
        self.home = Path(tempfile.mkdtemp(prefix='cfw home '))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state_dir = str(namespace(self.home))
        env = patch.dict(os.environ, {'CF_WORKFLOWS_STATE': self.state_dir})
        env.start()
        self.addCleanup(env.stop)
        self.ledger = self.open()

    def test_retained_result_and_request_baseline_are_reused_after_restore(self):
        first, accepted = self.reconciled()
        self.ledger.close()
        destination = Path(tempfile.mkdtemp(prefix='cfw backup ')) / 'backup'
        self.addCleanup(shutil.rmtree, destination.parent, True)
        recovery.backup(self.home, destination)
        shutil.rmtree(self.home / recovery.SUITE_DIR)
        recovery.restore(destination, self.home)
        self.ledger = self.open()
        # A fresh session after restore reuses the retained result without a dispatch.
        reused = self.claim()
        self.assertEqual((reused['invoke'], reused['claim'], reused['report']),
                         (False, first['claim'], accepted['report']))
        # The restored results and request baseline still refresh only what an edit touches.
        self.github.ev['comments'][3]['body'] = 'Log cache misses at info level.'
        context = self.claim()['task_input']['decisions']
        self.assertEqual((context['subjects']['sources'], context['subjects']['decisions']),
                         (['comment:53'], ['miss-logging']))
        self.assertEqual(self.sessions, 2)


class DraftingRecovery(LedgerHome, drafting_tests.Fixture):
    def ledger(self, admitted):
        request = admitted['snapshot']['request']
        ledger = drafting.Ledger(drafting.ledger_path(self.state(), request['logical_run']),
                                 logical_run=request['logical_run'], target=request['pr']['url'])
        self.addCleanup(ledger.close)
        return ledger

    def test_accepted_draft_is_reused_after_restore(self):
        admitted = self.admit('dk-pr-description')
        ledger = self.ledger(admitted)
        report = self.draft('dk-pr-description', admitted, ledger,
                            configuration_tests.session(drafting_tests.output()))
        self.assertEqual(report['status'], 'drafted', report)
        ledger.close()
        recover(self, self.base / 'xdg state', EngineAdapter(self.engine))
        again = self.draft('dk-pr-description', admitted, self.ledger(admitted))
        self.assertEqual((again['status'], again['session'], again['result_ref']),
                         ('drafted', False, report['result_ref']))
        self.calls()
        self.assertEqual(self.engine.sessions, Counter({'dk-pr-description': 1}))


class SplittingRecovery(LedgerHome, splitting_tests.Fixture):
    def ledger(self, admitted):
        request = admitted['snapshot']['request']
        ledger = splitting.Ledger(splitting.ledger_path(self.state(), request['logical_run']),
                                  logical_run=request['logical_run'], target=request['pr']['url'])
        self.addCleanup(ledger.close)
        return ledger

    def test_interrupted_rewrite_reconciles_its_original_refs_after_restore(self):
        admitted = self.admit('dk-commit-split')
        ledger = self.ledger(admitted)
        real, updates = splitting.git, []

        def crash(repository, *args, **kwargs):
            if args[0] == 'update-ref':
                updates.append(args)
                if len(updates) == 2:
                    raise KeyboardInterrupt('interrupted between ref updates')
            return real(repository, *args, **kwargs)
        with patch.object(splitting, 'git', crash), self.assertRaises(KeyboardInterrupt):
            self.organize(admitted, ledger, configuration_tests.session(splitting_tests.reorganize),
                          configuration_tests.session(splitting_tests.unchanged), rewrite=splitting_tests.LOCAL)
        ledger.close()
        suite = self.base / 'xdg state' / recovery.SUITE_DIR
        [entry] = recovery.unsettled(suite).values()
        self.assertEqual(len(entry['operations']), 1)
        recover(self, self.base / 'xdg state', EngineAdapter(self.engine))
        self.assertEqual(recovery.unsettled(suite), {(NAMESPACE.split('/')[1], admitted['snapshot']['request']['logical_run']): entry})
        resumed = self.organize(admitted, self.ledger(admitted), rewrite=splitting_tests.LOCAL)
        self.assertEqual((resumed['status'], resumed['reused']), ('prepared', True), resumed)
        self.assertEqual(splitting_tests.git(self.repo, 'rev-parse', 'HEAD^{tree}'), self.tree)
        self.assertEqual(self.engine.sessions['dk-commit-split'], 2)
        self.assertEqual(recovery.unsettled(suite), {})


class InstalledSplittingRecovery(unittest.TestCase):
    def test_an_interrupted_installed_apply_stays_unsettled_until_it_settles(self):
        home = Path(tempfile.mkdtemp(prefix='cfw split recovery '))
        self.addCleanup(shutil.rmtree, home, True)
        checkout = Path(tempfile.mkdtemp(prefix='cfw checkout '))
        self.addCleanup(shutil.rmtree, checkout, True)
        ns, plan = namespace(home), {'logical_run': 'r', 'pr': {'url': PUBLISHED_PR}}
        suite = home / recovery.SUITE_DIR
        with patch.dict(os.environ, {'CF_WORKFLOWS_STATE': str(ns)}):
            with installed_splitting.Store(checkout, plan, write=True) as store:
                store.build('commit-split-x', 'dk-commit-split', 'claim', {'original': 'b' * 40, 'final': 'a' * 40})
                # A built operation has moved nothing; an applying one resumes from the refs it finds.
                self.assertEqual(recovery.unsettled(suite), {})
                store.advance('commit-split-x', 'built', 'applying')
                self.assertEqual(recovery.unsettled(suite), {(NAMESPACE.split('/')[1], 'r'): {
                    'claims': [], 'publications': [], 'operations': ['commit-split-x']}})
                store.advance('commit-split-x', 'applying', 'applied')
            path = installed_splitting.store_path(checkout, 'r')
        self.assertEqual(recovery.classify(path.relative_to(suite).as_posix()), ('store', 'split_operations'))
        self.assertEqual(recovery.unsettled(suite), {})


class AlignmentRecovery(alignment_tests.Fixture):
    """The alternatives rows: settled stages reuse after restore; an open stage resumes its original attempt."""

    def setUp(self):
        super().setUp()
        self.ledger.close()
        self.home = Path(self.tmp.name) / 'xdg state'
        self.path = alignment.ledger_path(namespace(self.home), 'run-1')
        self.ledger = self.open()

    def test_settled_stages_reuse_and_the_open_stage_resumes_after_restore(self):
        sessions = alignment_tests.Sessions({**alignment_tests.architecture(), 'alignment': alignment_tests.aligned()})
        head = self.heads[0]
        self.step(sessions, 'assessment', head=head)
        self.step(sessions, 'inventory', head=head)
        opened = self.step(sessions, 'comparison', 'retry', head=head, finish=False)
        self.ledger.close()
        recover(self, self.home, EngineAdapter(sessions))
        self.ledger = self.open()
        self.assertEqual(self.step(sessions, 'assessment', head=head)['action'], 'reuse')
        self.assertEqual(self.step(sessions, 'inventory', head=head)['action'], 'reuse')
        resumed = self.step(sessions, 'comparison', 'retry', head=head)
        self.assertEqual(resumed['operation'], opened['operation'])
        self.step(sessions, 'selection', 'retry', head=head)
        self.step(sessions, 'interactions', head=head)
        self.step(sessions, 'alignment', head=head, inputs=alignment_tests.REQUIREMENTS)
        self.assertEqual(sum(sessions.calls.values()), 6)
        self.assertEqual(self.ledger.usage('architecture')['used'], 5)


class ConfigurationRecovery(configuration_tests.Changes):
    """dk-config and dk-preferences: an uncertain configuration write is read back after restore."""

    def test_lost_write_response_is_settled_by_readback_after_restore(self):
        original = writes.FileStore.write

        def write_then_fail(store, expected, document):
            original(store, expected, document)
            raise OSError('response lost')
        reads = {'n': 0}

        def read_fails_once(store, real=writes.FileStore.read):
            reads['n'] += 1
            if reads['n'] == 3:
                raise OSError('unavailable')
            return real(store)
        with patch.object(writes.FileStore, 'write', write_then_fail), \
                patch.object(writes.FileStore, 'read', read_fails_once):
            interrupted = self.configure(configuration_tests.proposal(*self.ENABLE))
        self.assertEqual(interrupted['status'], 'pending')
        report = recover(self, self.base / 'xdg state', EngineAdapter(self.engine),
                         configuration=self.paths['configuration'], wipe_configuration=True)
        self.assertEqual(report['blocked'], [])
        recovered = self.configure()
        self.assertEqual(recovered['status'], 'applied')
        self.assertEqual(recovered['recovered_writes'], [{'intent_id': interrupted['write']['intent_id'],
                                                          'status': 'applied'}])
        self.assertEqual(self.engine.sessions, Counter(change=1))


# -- Cost evidence --------------------------------------------------------------------------------------

class Cost(unittest.TestCase):
    def test_proxies_stay_apart_from_provider_reported_cost(self):
        proxies = {'native_sessions': 3, 'helper_processes': 2}
        self.assertEqual(recovery.cost(proxies), {'proxies': proxies, 'measured': {
            'runs': [], 'cost_usd': None, 'known_cost_usd': 0, 'complete': False}})
        original = {'run_id': 'r1', 'cost_usd': 0.25, 'known_cost_usd': 0.25, 'complete': True}
        later = {'run_id': 'r2', 'cost_usd': 0.5, 'known_cost_usd': 0.5, 'complete': True}
        self.assertEqual(recovery.cost(proxies, [original, later, original])['measured'],
                         {'runs': ['r1', 'r2'], 'cost_usd': 0.75, 'known_cost_usd': 0.75, 'complete': True})
        unknown = {'run_id': 'r3', 'cost_usd': None, 'known_cost_usd': 0.1, 'complete': False}
        measured = recovery.cost(proxies, [original, later, unknown])['measured']
        self.assertEqual((measured['cost_usd'], measured['known_cost_usd'], measured['complete']), (None, 0.85, False))
        with self.assertRaisesRegex(recovery.RecoveryError, 'conflicting usage'):
            recovery.cost(proxies, [original, {**original, 'cost_usd': 1.0}])


if __name__ == '__main__':
    unittest.main()
