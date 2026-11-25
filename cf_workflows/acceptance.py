"""Reusable semantic acceptance and its SQLite ledger (contract v1).

A semantic acceptance is retained per acceptance unit: one custom prompt or
preference, one task instruction, one custom adapter, one supplied skill, or the
free-text policy. Each unit is bound to digests of the inputs that are relevant
to it. Its own content, its transitive resource closure and the adapter and role
it targets are specific to the unit. The original request, policy (requirements,
prohibitions, effective permissions), repository guidance, actual capability
bindings and validator version apply to every unit. A changed input therefore
invalidates only the units bound to it, and an unchanged unit is reused with its
original native evidence instead of a new validator session.

Unit keys are content-addressed: moving an unchanged prompt to another scope,
reordering tasks or changing unrelated configuration does not invalidate it.
Baseline adapter text is bundled control text and is never a unit; it is bound
only into the units that target that adapter.

The ledger is a versioned SQLite database in the suite's own state namespace. It
holds acceptances, pending clarification questions and configuration write
intents with their receipts. It never reads or imports another suite's state.
"""
import json
import os
from pathlib import Path
import sqlite3

from cf_workflows import admission
from cf_workflows.admission import digest

SCHEME = 1  # Binding scheme; a change is a validator-version change and invalidates every acceptance.
SCHEMA_VERSION = 1
KIND = 'configuration_state'
CONTEXT = ('request', 'policy', 'guidance', 'capabilities', 'validator')
WRITE_STATES = ('prepared', 'pending', 'applied', 'not_applied', 'blocked')
UNSETTLED = ('prepared', 'pending')


class LedgerError(Exception):
    """The state database is not a supported cf-workflows configuration ledger."""


def locations(environ=None):
    """Fresh suite namespaces: configuration under XDG_CONFIG_HOME, state under XDG_STATE_HOME.

    Only absolute XDG values are honoured, as the XDG specification requires.
    Nothing here refers to a dk-workflows location.
    """
    environ = os.environ if environ is None else environ
    home = Path(environ.get('HOME') or Path.home())

    def base(name, fallback):
        value = environ.get(name, '')
        return Path(value) if os.path.isabs(value) else home / fallback
    return {'configuration': base('XDG_CONFIG_HOME', '.config') / admission.SUITE / 'configuration.json',
            'ledger': base('XDG_STATE_HOME', '.local/state') / admission.SUITE / 'configuration.sqlite3'}


# -- Bindings --------------------------------------------------------------------------------

def closure(keys, configured):
    """Digests of the configured resources reachable from keys through declared links."""
    found, pending = {}, list(keys)
    while pending:
        key = pending.pop()
        if key not in found:
            found[key] = configured[key]['sha256']
            pending.extend(configured[key]['links'])
    return dict(sorted(found.items()))


def context(admitted):
    """Bindings shared by every unit of one admitted proposal."""
    data, snapshot = admitted['admission_input'], admitted['snapshot']
    return {
        'request': digest(data['request']),
        'policy': digest({
            'requirements': sorted(item['text'] for item in data['requirements']),
            'prohibitions': sorted(item['text'] for item in data['prohibitions']),
            'permissions': {action: {key: value for key, value in entry.items() if key != 'source'}
                            for action, entry in data['permissions'].items()}}),
        'guidance': digest(snapshot['resources']['guidance']),
        'capabilities': admitted['admission']['capability_digest'],
        'validator': digest({'role': admission.VALIDATOR_ROLE, 'definition_sha256': admission.BOOTSTRAP_SHA256,
                             'scheme': SCHEME, 'contract': 1}),
    }


def units(admitted):
    """Acceptance units of a proposal with custom inputs, in a deterministic order."""
    data, snapshot = admitted['admission_input'], admitted['snapshot']
    configured = snapshot['resources']['configured']
    roles = snapshot['resources']['roles']
    shared = context(admitted)

    def adapter(name):
        definition = {key: value for key, value in snapshot['adapters'][name].items() if key != 'custom'}
        return {'name': name, 'definition': definition, 'role': roles[definition['definition']]['sha256'],
                'resources': closure(definition['resources'], configured)}

    found = {}

    def add(kind, identity, paths, **own):
        components = {**shared, **{name: digest(value) for name, value in own.items()}}
        key = digest({'kind': kind, 'components': components})
        if key in found:  # Identical content in two places is one unit cited at both paths.
            found[key]['paths'] = sorted(set(found[key]['paths']) | set(paths))
            return
        found[key] = {'key': key, 'kind': kind, 'identity': identity, 'paths': sorted(paths),
                      'components': components}

    # Untargeted text applies to every selected task, so it is bound to the selected adapters
    # (definitions, roles, writes) as a set: reordering keeps it, a different selection does not.
    selected = [adapter(name) for name in sorted({task['adapter'] for task in snapshot['plan']['tasks']})]
    custom = data['custom']
    for entry in custom['prompts']:
        targeted = entry['target'] is not None
        add('prompt', f"prompt:{entry['source']}:{entry['id']}", [entry['path']],
            content={**{key: entry[key] for key in ('slot', 'target', 'text')},
                     **({'scope': entry['scope']} if 'scope' in entry else {})},
            resources=closure(entry['resources'], configured),
            **({'adapter': adapter(entry['target'])} if targeted else {'plan': selected}))
    for entry in custom['instructions']:
        add('instruction', 'instruction:' + entry['path'], [entry['path']],
            content={'adapter': entry['adapter'], 'text': entry['text']}, adapter=adapter(entry['adapter']))
    for name, entry in custom['adapters'].items():
        bound = adapter(name)
        add('adapter', 'adapter:' + name, [entry['path']], content=bound['definition'], role=bound['role'],
            resources=bound['resources'])
    for key, skill in custom['skills'].items():
        used = sorted({task['adapter'] for task in snapshot['plan']['tasks'] if task['skill'] == key})
        add('skill', 'skill:' + key, [skill['record'], *skill['references']],
            content={key: skill[key] for key in ('name', 'sha256', 'files', 'children')},
            adapter=[adapter(name) for name in used])
    free = [item for item in data['requirements'] + data['prohibitions'] if item['text'] not in admission.ACTIONS]
    if free:
        # Free-text policy is already part of every unit's shared binding; it is one unit of its own.
        add('policy', 'policy', [item['path'] for item in free], content=None, plan=selected)
    return list(found.values())


def plan(admitted, ledger):
    """Split the proposal's units into reused acceptances and subjects that need assessment.

    A unit is reused only when it has its own current acceptance and also belongs to
    the one earlier assessment that covers most of the proposal. Every unit of that
    assessment was judged together, so none of them conflict; any other unit is a
    subject, judged against them. Units accepted separately are therefore never
    combined without a session.
    """
    current = units(admitted)
    present = {unit['key'] for unit in current}
    together = ledger.together(present) if ledger is not None else set()
    reused, subjects, invalidated = [], [], {}
    for unit in current:
        record = ledger.acceptance(unit['key'], present) if ledger is not None else None
        if record is not None and unit['key'] in together:
            reused.append({**unit, 'record': record})
            continue
        subjects.append(unit)
        if ledger is not None:
            invalidated[unit['identity']] = ledger.why(unit, present) if record is None else ['combination']
    return {'units': current, 'reused': reused, 'subjects': subjects, 'invalidated': invalidated}


# -- Ledger ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE acceptances (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    unit TEXT NOT NULL,
    identity TEXT NOT NULL,
    components TEXT NOT NULL,
    requires TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    session_id TEXT NOT NULL,
    answers TEXT NOT NULL,
    UNIQUE (unit, evidence_id)
);
CREATE INDEX acceptances_identity ON acceptances (identity);
CREATE TABLE assessments (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    units TEXT NOT NULL,
    requires TEXT NOT NULL
);
CREATE TABLE questions (
    evidence_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    validator TEXT NOT NULL,
    gate_id TEXT
);
CREATE TABLE writes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    base_revision TEXT,
    target_digest TEXT NOT NULL,
    document TEXT,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'pending', 'applied', 'not_applied', 'blocked')),
    dispatches INTEGER NOT NULL DEFAULT 0,
    receipt TEXT,
    failure TEXT
);
"""


def dumps(value):
    return admission.canonical(value).decode('utf-8')


class Ledger:
    """Serialized, transactional access to the configuration ledger."""

    def __init__(self, path, create=True):
        self.path = Path(path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise LedgerError('missing ledger: ' + str(self.path))
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        try:
            with self.transaction():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                if version == 0 and tables == 0 and create:
                    for statement in filter(str.strip, SCHEMA.split(';')):
                        self.db.execute(statement)
                    self.db.executemany('INSERT INTO meta VALUES (?, ?)', [('suite', admission.SUITE), ('kind', KIND)])
                    self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                elif version != SCHEMA_VERSION:
                    raise LedgerError(f'unsupported ledger version {version}')
                meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
                if meta != {'suite': admission.SUITE, 'kind': KIND}:
                    raise LedgerError('not a cf-workflows configuration ledger')
        except (LedgerError, sqlite3.DatabaseError) as error:
            self.db.close()
            raise LedgerError(str(error)) from error

    def close(self):
        self.db.close()

    def transaction(self):
        ledger = self

        class Transaction:
            def __enter__(self):
                ledger.db.execute('BEGIN IMMEDIATE')

            def __exit__(self, kind, value, trace):
                ledger.db.execute('COMMIT' if kind is None else 'ROLLBACK')
        return Transaction()

    # Acceptances -------------------------------------------------------------------------

    def acceptance(self, key, present):
        """The original (earliest) acceptance of a unit whose coupled units are all present."""
        for row in self.db.execute('SELECT * FROM acceptances WHERE unit = ? ORDER BY seq', (key,)):
            if set(json.loads(row['requires'])) <= present:
                return {'result_ref': row['result_ref'], 'session_id': row['session_id'],
                        'evidence_id': row['evidence_id'], 'request_id': row['request_id']}
        return None

    def together(self, present):
        """Present units of the earlier accepted assessment that covers most of them."""
        best = set()
        for row in self.db.execute('SELECT units, requires FROM assessments ORDER BY seq'):
            if set(json.loads(row['requires'])) <= present:
                covered = present & set(json.loads(row['units']))
                if len(covered) > len(best):
                    best = covered
        return best

    def why(self, unit, present):
        """Which bindings changed since this unit's identity was last accepted."""
        row = self.db.execute('SELECT * FROM acceptances WHERE identity = ? ORDER BY seq DESC LIMIT 1',
                              (unit['identity'],)).fetchone()
        if row is None:
            return ['new']
        before = json.loads(row['components'])
        changed = sorted(name for name in set(before) | set(unit['components'])
                         if before.get(name) != unit['components'].get(name))
        if changed:
            return changed
        missing = set(json.loads(row['requires'])) - present
        return ['coupled'] if missing else ['new']

    def accept(self, subjects, context, *, evidence_id, request_id, validator, clarifications):
        """Retain one accepted assessment: its subject units and every unit it was judged with.

        After a clarification dialogue the subjects were accepted together with the
        answers' effects, so each is reusable only while all of them are present.
        """
        requires = sorted(unit['key'] for unit in subjects) if clarifications else []
        judged = sorted({unit['key'] for unit in subjects + context})
        answers = [{key: item[key] for key in ('gate_id', 'answer_id', 'value')} for item in clarifications]
        with self.transaction():
            self.db.executemany(
                'INSERT OR IGNORE INTO acceptances (unit, identity, components, requires, evidence_id, request_id, '
                'result_ref, session_id, answers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [(unit['key'], unit['identity'], dumps(unit['components']), dumps(requires), evidence_id, request_id,
                  validator['result_ref'], validator['session_id'], dumps(answers)) for unit in subjects])
            self.db.execute('INSERT INTO assessments (evidence_id, result_ref, units, requires) VALUES (?, ?, ?, ?)',
                            (evidence_id, validator['result_ref'], dumps(judged), dumps(requires)))

    # Questions ---------------------------------------------------------------------------

    def question(self, evidence_id):
        row = self.db.execute('SELECT * FROM questions WHERE evidence_id = ?', (evidence_id,)).fetchone()
        if row is None:
            return None
        return {'request_id': row['request_id'], 'verdict': json.loads(row['verdict']),
                'validator': json.loads(row['validator']), 'gate_id': row['gate_id']}

    def ask(self, evidence_id, request_id, verdict, validator):
        """Retain a clarification verdict before its gate opens, so a lost gate never loses it."""
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO questions VALUES (?, ?, ?, ?, NULL)',
                            (evidence_id, request_id, dumps(verdict), dumps(validator)))

    def asked(self, evidence_id, gate_id):
        with self.transaction():
            self.db.execute('UPDATE questions SET gate_id = ? WHERE evidence_id = ?', (gate_id, evidence_id))

    # Configuration writes ----------------------------------------------------------------

    def writes(self, statuses=WRITE_STATES):
        marks = ', '.join('?' * len(statuses))
        rows = self.db.execute(f'SELECT * FROM writes WHERE status IN ({marks}) ORDER BY seq', tuple(statuses))
        return [{**dict(row), 'evidence_refs': json.loads(row['evidence_refs']),
                 'document': json.loads(row['document']) if row['document'] is not None else None,
                 'receipt': json.loads(row['receipt']) if row['receipt'] is not None else None} for row in rows]

    def prepare(self, *, request_id, evidence_refs, base_revision, document, target_digest):
        """Record the exact intended write before dispatch; at most one write is unsettled."""
        with self.transaction():
            unsettled = self.db.execute("SELECT intent_id FROM writes WHERE status IN ('prepared', 'pending')").fetchone()
            if unsettled is not None:
                raise LedgerError('configuration write ' + unsettled[0] + ' must be reconciled before another write')
            stem = 'cfgw_' + digest({'request_id': request_id, 'base_revision': base_revision,
                                     'target': target_digest})[:32]
            count = self.db.execute('SELECT count(*) FROM writes WHERE intent_id LIKE ?', (stem + '%',)).fetchone()[0]
            intent_id = stem if count == 0 else f'{stem}.{count}'
            self.db.execute('INSERT INTO writes (intent_id, request_id, evidence_refs, base_revision, target_digest, '
                            "document, status) VALUES (?, ?, ?, ?, ?, ?, 'prepared')",
                            (intent_id, request_id, dumps(evidence_refs), base_revision, target_digest, dumps(document)))
        return self.writes_by_id(intent_id)

    def writes_by_id(self, intent_id):
        [found] = [item for item in self.writes() if item['intent_id'] == intent_id]
        return found

    def dispatching(self, intent_id):
        """Durably mark a write as possibly dispatched before the effect is attempted."""
        with self.transaction():
            self.db.execute("UPDATE writes SET status = 'pending', dispatches = dispatches + 1 WHERE intent_id = ?",
                            (intent_id,))

    def settle(self, intent_id, status, receipt=None, failure=None):
        """Record the outcome; an applied write keeps its receipt and drops the payload copy."""
        assert status in WRITE_STATES
        with self.transaction():
            self.db.execute(
                'UPDATE writes SET status = ?, receipt = coalesce(?, receipt), failure = ?, '
                "document = CASE WHEN ? = 'applied' THEN NULL ELSE document END WHERE intent_id = ?",
                (status, dumps(receipt) if receipt is not None else None, failure, status, intent_id))

    # Backup ------------------------------------------------------------------------------

    def backup(self, target):
        """Consistent online copy of the whole ledger."""
        copy = sqlite3.connect(target)
        try:
            self.db.backup(copy)
        finally:
            copy.close()

    @staticmethod
    def restore(source, path):
        """Restore a backup after checking it is a supported cf-workflows ledger."""
        Ledger(source, create=False).close()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        origin, target = sqlite3.connect(source), sqlite3.connect(path)
        try:
            origin.backup(target)
        finally:
            origin.close()
            target.close()
        return Ledger(path)
