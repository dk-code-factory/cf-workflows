"""Current-alignment records and the alignment gate (#23, #27).

Authored for cf-workflows; no upstream counterpart. See docs/alignment.md.

This is the one implementation of the gate and of reading and writing an
alignment record. The library module ``cf_workflows/alignment.py`` loads this
file and re-exports it; the installed helpers call ``gate`` from the
``alignment_gate`` operation (app.py) before review, and the alternatives
procedure (alternatives.py) records its stages through ``Ledger``. It uses only
the standard library and imports nothing from this package, so the library can
load it.

Settled architectural stages are read as they are. Alignment and verification
must match the head (and, when the caller supplies them, the alignment inputs);
otherwise they need a refresh, which needs no historical rejection receipt. No
gate outcome is a clean review or readiness. The gate and ``Records`` never
write; nothing here starts a session or publishes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

SUITE = 'cf-workflows'
SCHEMA_VERSION = 1
KIND = 'alignment_state'
STAGES = ('assessment', 'inventory', 'comparison', 'selection', 'interactions', 'alignment', 'verification')
SETTLED = ('assessment', 'inventory', 'comparison', 'selection', 'interactions')
CURRENT = ('alignment', 'verification')
SCOPES = ('branch', 'whole-problem')
# Default ceilings; the first reservation of an allowance fixes it for the logical run.
LIMITS = {'architecture': 16, 'alignment': 4}
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
_SHA = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')


class AlignmentError(ValueError):
    """Evidence, a result or a requested attempt is not supported."""


class AuthorizationRequired(AlignmentError):
    """Settled architectural analysis would repeat without a distinct direct user request."""


class AllowanceExhausted(AlignmentError):
    """The retained allowance has no capacity left for a new attempt."""


def require(condition, message, error=AlignmentError):
    if not condition:
        raise error(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _text(value, label):
    require(isinstance(value, str) and value.strip() and '\0' not in value, label + ' requires nonempty text')
    return value


def _head(value, label='head'):
    require(isinstance(value, str) and _SHA.fullmatch(value) is not None, label + ' requires a full object ID')
    return value


def allowance(stage, account=None):
    """The allowance a stage reserves from; an account (such as a logical run) keeps its own count."""
    name = 'alignment' if stage in CURRENT else 'architecture'
    if account is not None:
        require(isinstance(account, str) and _ID.fullmatch(account) is not None, 'invalid allowance account')
        name += ':' + account
    return name


def _base(name):
    require(isinstance(name, str) and name.split(':', 1)[0] in LIMITS, 'unknown allowance')
    return LIMITS[name.split(':', 1)[0]]


def key(stage, decision=None):
    require(stage in STAGES, 'unknown alignment stage')
    if stage in ('comparison', 'selection'):
        require(isinstance(decision, str) and _ID.fullmatch(decision) is not None,
                stage + ' requires a stable decision ID')
        return f'{stage}:{decision}'
    require(decision is None, 'only comparison and selection take a decision ID')
    return stage


def ledger_path(state_directory, logical_run, scope='branch'):
    """The logical run's alignment record, next to its run ledger; run text never forms a path.

    The branch scope (PR architecture) keeps its original name. Whole-problem analysis
    (cf-workflows:dk-issue-alternatives) has its own record and never shares stage keys.
    """
    require(scope in SCOPES, 'unknown alternatives scope')
    name = hashlib.sha256(_text(logical_run, 'logical run').encode()).hexdigest()
    return Path(state_directory) / 'runs' / (name + ('' if scope == 'branch' else '.' + scope) + '.alignment.sqlite')


def pr_run(url):
    """The alternatives entries' per-PR record name: once per PR across routes, runs and sessions."""
    return 'alternatives_' + digest(_text(url, 'PR URL').lower())[:32]


def _reference(row):
    return f"{row['key']}@{row['operation']}"


class Records:
    """Read access to one alignment record. The library's writing Ledger extends it."""

    @classmethod
    def open(cls, path, *, logical_run, pr_url, scope='branch'):
        """An existing record of one PR, read-only: never created, migrated or written."""
        path = Path(path)
        require(scope in SCOPES, 'unknown alternatives scope')
        require(not path.is_symlink() and path.is_file(), 'alignment record is missing or not a regular file')
        records = cls.__new__(cls)
        records.path, records.scope = path, scope
        records.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        records.db.row_factory = sqlite3.Row
        try:
            version = records.db.execute('PRAGMA user_version').fetchone()[0]
            require(version == SCHEMA_VERSION, f'unsupported alignment ledger version {version}')
            meta = dict(records.db.execute('SELECT key, value FROM meta').fetchall())
            require({k: meta.get(k) for k in ('suite', 'kind')} == {'suite': SUITE, 'kind': KIND},
                    'not a cf-workflows alignment ledger')
            expected = {'suite': SUITE, 'kind': KIND, 'logical_run': logical_run, 'pr': pr_url.lower()}
            if scope != 'branch':
                expected['scope'] = scope
            require(meta == expected, 'alignment ledger belongs to another logical run or PR')
        except (AlignmentError, sqlite3.DatabaseError) as error:
            records.db.close()
            raise AlignmentError(str(error)) from error
        return records

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- Reads ---------------------------------------------------------------------------

    def latest(self, stage_key):
        """The current accepted result of one stage, or None."""
        row = self.db.execute("SELECT * FROM attempts WHERE key = ? AND state = 'complete' ORDER BY seq DESC LIMIT 1",
                              (stage_key,)).fetchone()
        return self._record(row)

    def open_claim(self, stage_key):
        row = self.db.execute("SELECT * FROM attempts WHERE key = ? AND state = 'claimed' ORDER BY seq DESC LIMIT 1",
                              (stage_key,)).fetchone()
        return self._record(row)

    def history(self, stage_key):
        return [self._record(row) for row in
                self.db.execute('SELECT * FROM attempts WHERE key = ? ORDER BY seq', (stage_key,))]

    @staticmethod
    def _record(row):
        if row is None:
            return None
        value = dict(row)
        value['dependencies'] = json.loads(value['dependencies'])
        value['result'] = json.loads(value['result']) if value['result'] else None
        value['reference'] = _reference(row)
        return value

    @staticmethod
    def _settled(stage, record):
        # A selection without a choice still waits for one; every other complete stage is settled.
        return not (stage == 'selection' and record['result']['choice'] is None)

    def usage(self, name):
        used = self.db.execute('SELECT count(*) FROM reservations WHERE name = ?', (name,)).fetchone()[0]
        first = self.db.execute('SELECT limit_value FROM reservations WHERE name = ? ORDER BY rowid LIMIT 1',
                                (name,)).fetchone()
        granted = self.db.execute('SELECT COALESCE(SUM(amount), 0) FROM grants WHERE name = ?', (name,)).fetchone()[0]
        ceiling = first[0] if first else _base(name)
        return {'allowance': name, 'used': used, 'limit': ceiling + granted, 'exhausted': used >= ceiling + granted}

    # -- Dependencies ----------------------------------------------------------------------

    def _need(self, stage, decision=None):
        found = self.latest(key(stage, decision))
        require(found is not None, f'{key(stage, decision)} must complete first')
        return found

    def questionable(self):
        inventory = self._need('inventory')
        return [d['id'] for d in inventory['result']['decisions'] if d['questionable']]

    def dependencies(self, stage, decision=None):
        """References to the accepted results a stage consumes, as they stand now."""
        if stage == 'assessment':
            return {}
        assessment = self._need('assessment')['reference']
        if stage == 'inventory':
            return {'assessment': assessment}
        if stage == 'comparison':
            inventory = self._need('inventory')['result']['decisions']
            entry = next((d for d in inventory if d['id'] == decision), None)
            require(entry is not None and entry['questionable'], 'comparison requires a questionable inventoried decision')
            # Only this decision's own entry: added or changed siblings leave it unchanged.
            return {'assessment': assessment, 'decision': digest(entry)}
        if stage == 'selection':
            return {'assessment': assessment, 'comparison': self._need('comparison', decision)['reference']}
        if stage == 'interactions':
            selections = {}
            for ident in self.questionable():
                selection = self._need('selection', ident)
                require(selection['result']['choice'] is not None, f'selection:{ident} has no settled choice')
                # A selection refreshed to the same option over the same comparison needs no new interaction check.
                selections[ident] = {'choice': selection['result']['choice'],
                                     'comparison': selection['dependencies']['comparison']}
            return {'assessment': assessment, 'inventory': self._need('inventory')['reference'],
                    'selections': selections}
        if stage == 'alignment':
            interactions = self._need('interactions')
            require(interactions['result']['compatible'], 'incompatible interactions block alignment')
            return {'assessment': assessment, 'interactions': interactions['reference']}
        alignment = self._need('alignment')
        require(alignment['result']['alignment'] == 'mismatch', 'verification requires a mismatch alignment')
        return {'alignment': alignment['reference']}

    def binding(self, stage, decision=None, *, inputs, head):
        require(isinstance(inputs, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in inputs.items()),
                'inputs require a mapping of names to digests')
        require(stage != 'verification' or not inputs, 'verification consumes only its alignment')
        dependencies = self.dependencies(stage, decision)
        return dependencies, digest({'key': key(stage, decision), 'inputs': inputs, 'dependencies': dependencies,
                                     'head': _head(head) if stage in CURRENT else None})


# The whole-problem and branch records of one PR share one assessment (Ledger.adopt).
SHARED = ('assessment',)
SHARED_PREFIX = 'shared:'
DIRECT_SKILL = 'cf-workflows:dk-pr-review-alternatives'
ALIGNMENTS = ('aligned', 'mismatch', 'uncertain')
DISPOSITIONS = ('confirmed', 'rejected', 'uncertain')


def direct_request(value):
    """A distinct direct user request for a fresh comparison, as retained provenance."""
    require(isinstance(value, dict) and set(value) == {'id', 'source', 'instruction', 'skill', 'origin'},
            'direct request requires exactly id, source, instruction, skill and origin', AuthorizationRequired)
    for field in ('id', 'source', 'instruction'):
        _text(value[field], 'direct request ' + field)
    require(value['skill'] == DIRECT_SKILL and value['origin'] == 'user',
            'only a direct user invocation of ' + DIRECT_SKILL + ' authorizes a fresh comparison',
            AuthorizationRequired)
    return value


def _validate(stage, result):
    require(isinstance(result, dict), stage + ' result requires an object')
    if stage == 'inventory':
        decisions = result.get('decisions')
        require(isinstance(decisions, list), 'inventory requires decisions')
        ids = [d.get('id') if isinstance(d, dict) else None for d in decisions]
        require(all(isinstance(i, str) and _ID.fullmatch(i) for i in ids) and len(set(ids)) == len(ids),
                'inventory decisions require unique stable IDs')
        require(all(type(d.get('questionable')) is bool for d in decisions), 'inventory decisions require questionable')
    elif stage == 'comparison':
        solutions = result.get('solutions')
        require(isinstance(solutions, list) and solutions, 'comparison requires solutions')
        require(all(isinstance(s, dict) and isinstance(s.get('id'), str) for s in solutions),
                'comparison solutions require IDs')
    elif stage == 'selection':
        require('choice' in result and (result['choice'] is None or isinstance(result['choice'], str)),
                'selection requires a choice or null')
    elif stage == 'interactions':
        require(type(result.get('compatible')) is bool, 'interactions requires compatible')
    elif stage == 'alignment':
        require(result.get('alignment') in ALIGNMENTS, 'alignment requires aligned, mismatch or uncertain')
        _text(result.get('reason'), 'alignment reason')
        if result['alignment'] == 'mismatch':
            _text(result.get('required_changes'), 'mismatch required changes')
    elif stage == 'verification':
        require(result.get('disposition') in DISPOSITIONS, 'verification requires confirmed, rejected or uncertain')
        _text(result.get('reason'), 'verification reason')
        if result['disposition'] == 'confirmed':
            _text(result.get('required_changes'), 'confirmed required changes')
    return result


# -- Ledger ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE attempts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    stage TEXT NOT NULL,
    operation TEXT NOT NULL UNIQUE,
    binding TEXT NOT NULL,
    head TEXT,
    dependencies TEXT NOT NULL,
    request TEXT,
    state TEXT NOT NULL CHECK (state IN ('claimed', 'complete', 'superseded')),
    session TEXT,
    result TEXT
);
CREATE INDEX attempts_key ON attempts (key, state);
CREATE TABLE requests (id TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE reservations (
    name TEXT NOT NULL,
    operation TEXT NOT NULL,
    limit_value INTEGER NOT NULL,
    PRIMARY KEY (name, operation)
);
CREATE TABLE grants (id TEXT PRIMARY KEY, name TEXT NOT NULL, amount INTEGER NOT NULL, value TEXT NOT NULL)
"""
# Answers to the installed selection gates (prfix/alternatives.py): one per gate request. The
# writer adds the table to a version 1 record that lacks it; readers never need it.
ANSWERS = """
CREATE TABLE IF NOT EXISTS answers (request_id TEXT PRIMARY KEY, gate_id TEXT NOT NULL,
                                    answer_id TEXT NOT NULL UNIQUE, value TEXT NOT NULL)
"""


def dumps(value):
    return canonical(value).decode('utf-8')


class Ledger(Records):
    """Retained architectural analysis, current alignment and allowances for one logical run and PR.

    The writing side of a record: the alternatives procedure (prfix/alternatives.py) records
    every stage through it. Reads and stage dependencies come from Records."""

    def __init__(self, path, *, logical_run, pr=None, subject=None, scope='branch'):
        """A PR's record binds its URL. Whole-problem analysis without a PR binds its repository
        subject instead and never invents a PR identity."""
        require(scope in SCOPES and (pr is None) != (subject is None) and (pr is not None or scope != 'branch'),
                'alignment ledger requires a PR, or a subject for whole-problem analysis')
        self.path, self.scope, self.pr = Path(path), scope, pr
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not self.path.is_symlink(), 'alignment ledger must not be a symlink')
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        expected = {'suite': SUITE, 'kind': KIND, 'logical_run': logical_run}
        expected.update({'pr': pr['url'].lower()} if pr is not None else {'subject': _text(subject, 'subject')})
        if scope != 'branch':
            expected['scope'] = scope
        try:
            with self.transaction():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                if version == 0 and tables == 0:
                    for statement in SCHEMA.split(';'):
                        self.db.execute(statement)
                    self.db.executemany('INSERT INTO meta VALUES (?, ?)', sorted(expected.items()))
                    self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                require(version in (0, SCHEMA_VERSION), f'unsupported alignment ledger version {version}')
                meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
                require({k: meta.get(k) for k in ('suite', 'kind')} == {k: expected[k] for k in ('suite', 'kind')},
                        'not a cf-workflows alignment ledger')
                require(meta == expected, 'alignment ledger belongs to another logical run or PR')
                self.db.execute(ANSWERS)
        except (AlignmentError, sqlite3.DatabaseError) as error:
            self.db.close()
            raise AlignmentError(str(error)) from error

    def transaction(self):
        db = self.db

        class Transaction:
            def __enter__(self):
                db.execute('BEGIN IMMEDIATE')

            def __exit__(self, kind, value, trace):
                db.execute('COMMIT' if kind is None else 'ROLLBACK')
        return Transaction()

    # -- Attempts -------------------------------------------------------------------------

    def prepare(self, stage, decision=None, *, inputs, head, operation, request=None, limit=None, account=None):
        """Decide one stage's next action: reuse, resume or run.

        reuse: an accepted result with the same binding; no session or allowance.
        resume: an interrupted claim with the same binding; continue its original
        operation, which already holds its reservation.
        run: a new attempt under this operation, reserving one unit of its allowance.
        A settled architectural stage whose binding changed raises
        AuthorizationRequired unless a distinct direct user request covers it.
        account keeps a separate count of the stage's allowance (for example per logical run).
        """
        stage_key = key(stage, decision)
        _text(operation, 'engine operation')
        _head(head)
        name = allowance(stage, account)
        ceiling = _base(name) if limit is None else limit
        require(type(ceiling) is int and ceiling >= 0, 'allowance limit requires a nonnegative integer')
        if request is not None:
            direct_request(request)
        with self.transaction():
            dependencies, binding = self.binding(stage, decision, inputs=inputs, head=head)
            replay = self.db.execute('SELECT * FROM attempts WHERE operation = ?', (operation,)).fetchone()
            if replay is not None:
                require(replay['state'] != 'superseded', 'engine operation was superseded by a later attempt')
                require(replay['key'] == stage_key and replay['binding'] == binding,
                        'engine operation identity conflict')
                return self._action('resume' if replay['state'] == 'claimed' else 'reuse', self._record(replay))
            claim = self.open_claim(stage_key)
            if claim is not None and claim['binding'] == binding:
                return self._action('resume', claim)
            latest = self.latest(stage_key)
            if latest is not None and latest['binding'] == binding:
                return self._action('reuse', latest)
            if latest is not None and stage in SETTLED and self._settled(stage, latest):
                require(request is not None, f'{stage_key} is settled; changed inputs, guidance or head do not '
                        f'authorize repeating it without a direct user request for {DIRECT_SKILL}',
                        AuthorizationRequired)
                # Any earlier attempt of this stage under the request consumed it, not only the latest.
                # A superseded or abandoned attempt did not settle the request's work.
                consumed = self.db.execute("SELECT 1 FROM attempts WHERE key = ? AND request = ? "
                                           "AND state = 'complete'", (stage_key, request['id'])).fetchone()
                require(consumed is None,
                        f'{stage_key} already ran under direct request {request["id"]}; '
                        'a distinct direct user request is required', AuthorizationRequired)
            if request is not None:
                old = self.db.execute('SELECT value FROM requests WHERE id = ?', (request['id'],)).fetchone()
                require(old is None or old['value'] == dumps(request), 'direct request identity conflict')
                self.db.execute('INSERT OR IGNORE INTO requests VALUES (?, ?)', (request['id'], dumps(request)))
            self._reserve(name, operation, ceiling)
            if claim is not None:
                # Retained as history: its reservation stays consumed and a late result stays private.
                self.db.execute("UPDATE attempts SET state = 'superseded' WHERE seq = ?", (claim['seq'],))
            self.db.execute('INSERT INTO attempts (key, stage, operation, binding, head, dependencies, request, state) '
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed')",
                            (stage_key, stage, operation, binding, head if stage in CURRENT else None,
                             dumps(dependencies), request['id'] if request else None))
            created = self.db.execute('SELECT * FROM attempts WHERE operation = ?', (operation,)).fetchone()
            return self._action('run', self._record(created), superseded=claim['operation'] if claim else None)

    def _action(self, action, record, superseded=None):
        value = {'action': action, 'key': record['key'], 'operation': record['operation'],
                 'binding': record['binding'], 'reference': record['reference'],
                 'dependencies': record['dependencies']}
        if superseded:
            value['superseded'] = superseded
        if action == 'reuse':
            value['result'] = record['result']
        return value

    def _reserve(self, name, operation, ceiling):
        first = self.db.execute('SELECT limit_value FROM reservations WHERE name = ? ORDER BY rowid LIMIT 1',
                                (name,)).fetchone()
        # The first reservation fixes the ceiling; a later run can only narrow it.
        ceiling = min(ceiling, first[0]) if first else ceiling
        used = self.db.execute('SELECT count(*) FROM reservations WHERE name = ?', (name,)).fetchone()[0]
        granted = self.db.execute('SELECT COALESCE(SUM(amount), 0) FROM grants WHERE name = ?', (name,)).fetchone()[0]
        require(used < ceiling + granted, 'retained allowance exhausted: ' + name, AllowanceExhausted)
        self.db.execute('INSERT INTO reservations VALUES (?, ?, ?)', (name, operation, ceiling))

    def abandon(self, operation):
        """Close a claim whose native result was rejected. Its reservation stays consumed, and the
        next prepare starts a new attempt instead of resuming the rejected one."""
        with self.transaction():
            row = self.db.execute('SELECT * FROM attempts WHERE operation = ?', (operation,)).fetchone()
            require(row is not None, 'unknown engine operation')
            if row['state'] == 'claimed':
                self.db.execute("UPDATE attempts SET state = 'superseded' WHERE seq = ?", (row['seq'],))
            return self._record(self.db.execute('SELECT * FROM attempts WHERE seq = ?', (row['seq'],)).fetchone())

    def grant(self, request_id, name, amount, authorization):
        """An explicit continuation adds capacity to one allowance; replay adds nothing."""
        _base(name)
        require(type(amount) is int and 1 <= amount <= 10, 'continuation amount out of bounds')
        _text(request_id, 'continuation request')
        _text(authorization, 'continuation authorization')
        value = dumps({'name': name, 'amount': amount, 'authorization': authorization})
        with self.transaction():
            old = self.db.execute('SELECT value FROM grants WHERE id = ?', (request_id,)).fetchone()
            require(old is None or old['value'] == value, 'continuation request identity conflict')
            self.db.execute('INSERT OR IGNORE INTO grants VALUES (?, ?, ?, ?)', (request_id, name, amount, value))

    def adopt(self, stage, *, inputs, head, origin):
        """Record another scope's settled result of a shared stage of the same PR as this record's own.

        Only the assessment is shared. It needs no session and reserves no allowance. Its
        reference names the origin (`<scope>:<operation>`), so the result is traceable. A
        record that already has its own result or an open claim keeps it.
        """
        require(stage in SHARED, f'{stage} is not shared between alternatives scopes')
        require(isinstance(origin, dict) and origin.get('scope') in SCOPES and origin['scope'] != self.scope,
                'a shared result comes from the other alternatives scope')
        require(self.pr is not None, 'only records of a PR share results')
        _text(origin.get('operation'), 'origin operation')
        require(not origin['operation'].startswith(SHARED_PREFIX), 'a shared result is adopted only from its origin')
        _text(origin.get('session'), 'origin session')
        _validate(stage, origin.get('result'))
        operation = f"{SHARED_PREFIX}{origin['scope']}:{origin['operation']}"
        with self.transaction():
            require(self.latest(key(stage)) is None and self.open_claim(key(stage)) is None,
                    f'{stage} already has a result or claim in this record')
            dependencies, binding = self.binding(stage, inputs=inputs, head=head)
            self.db.execute('INSERT INTO attempts (key, stage, operation, binding, head, dependencies, request, state, '
                            "session, result) VALUES (?, ?, ?, ?, NULL, ?, NULL, 'complete', ?, ?)",
                            (key(stage), stage, operation, binding, dumps(dependencies), origin['session'],
                             dumps(origin['result'])))
            return self._record(self.db.execute('SELECT * FROM attempts WHERE operation = ?',
                                                (operation,)).fetchone())

    def answers(self, request_id):
        """The recorded answer to one installed selection gate request, as [{status, answer}]."""
        row = self.db.execute('SELECT * FROM answers WHERE request_id = ?', (request_id,)).fetchone()
        return [] if row is None else [{'status': 'answered', 'gate_id': row['gate_id'], 'answer': {
            'answer_id': row['answer_id'], 'value': json.loads(row['value'])}}]

    def answer(self, request_id, *, gate_id, answer_id, value):
        """Record the answer to an installed selection gate once; replaying the same delivery is a no-op."""
        for label, item in (('gate request', request_id), ('gate', gate_id), ('answer', answer_id)):
            _text(item, label)
        encoded = dumps(value)
        with self.transaction():
            old = self.db.execute('SELECT * FROM answers WHERE request_id = ? OR answer_id = ?',
                                  (request_id, answer_id)).fetchall()
            if old:
                require(len(old) == 1 and (old[0]['request_id'], old[0]['gate_id'], old[0]['answer_id'],
                                           old[0]['value']) == (request_id, gate_id, answer_id, encoded),
                        'the gate request or answer already holds another answer')
                return False
            self.db.execute('INSERT INTO answers VALUES (?, ?, ?, ?)', (request_id, gate_id, answer_id, encoded))
            return True

    def finish(self, operation, result, *, session):
        """Accept one attempt's native result. A superseded attempt's result is retained, never current."""
        _text(session, 'native session')
        with self.transaction():
            row = self.db.execute('SELECT * FROM attempts WHERE operation = ?', (operation,)).fetchone()
            require(row is not None, 'unknown engine operation')
            _validate(row['stage'], result)
            encoded = dumps(result)
            if row['result'] is not None:
                require(row['result'] == encoded and row['session'] == session, 'attempt already holds another result')
                return self._record(row)
            if row['stage'] == 'verification':
                alignment = self.db.execute('SELECT session FROM attempts WHERE key = ? AND operation = ?',
                                            ('alignment', json.loads(row['dependencies'])['alignment'].split('@', 1)[1])
                                            ).fetchone()
                require(alignment is not None and alignment['session'] != session,
                        'mismatch verification requires an independent native session')
            state = 'complete' if row['state'] == 'claimed' else row['state']
            self.db.execute('UPDATE attempts SET state = ?, session = ?, result = ? WHERE seq = ?',
                            (state, session, encoded, row['seq']))
            return self._record(self.db.execute('SELECT * FROM attempts WHERE seq = ?', (row['seq'],)).fetchone())


# -- Gate ------------------------------------------------------------------------------------

def gate(ledger, *, head, inputs, identities=None, receipts=(), account=None):
    """Whether ordinary review may proceed at head under the current alignment inputs.

    Settled architectural stages are read as they are. Alignment and verification
    must match head and inputs; otherwise they need a refresh, which needs no
    historical rejection receipt. A receipt only completes alternatives when it
    rejected exactly the verified changes at this head. No outcome is a clean
    review or readiness.

    inputs None is a caller that holds no alignment evidence of its own (the
    installed workflow): the retained alignment's own inputs stand, so only a
    changed head or changed analysis makes it stale. New requirements evidence
    reaches the gate only through a caller that supplies inputs.
    """
    _head(head)
    provisional = identities is not None and not identities.get('current')
    base = {'head': head, 'release': False, 'clean_review': False, 'ready': False, 'provisional': provisional}

    def pending(stage, decision=None, **extra):
        stage_key = key(stage, decision)
        claim, latest = ledger.open_claim(stage_key), ledger.latest(stage_key)
        authorization = stage in SETTLED and latest is not None and Records._settled(stage, latest)
        value = {**base, 'outcome': 'analysis_required', 'next': {'stage': stage, 'decision': decision},
                 'authorization_required': authorization, **extra}
        if claim is not None:
            value['next']['resume'] = claim['operation']
        return value

    def current(stage, decision=None):
        """The latest accepted result if its dependencies are unchanged, else None."""
        latest = ledger.latest(key(stage, decision))
        return latest if latest is not None and latest['dependencies'] == ledger.dependencies(stage, decision) else None

    if current('assessment') is None:
        return pending('assessment')
    if current('inventory') is None:
        return pending('inventory')
    for decision in ledger.questionable():
        if current('comparison', decision) is None:
            return pending('comparison', decision)
        selection = current('selection', decision)
        if selection is None:
            return pending('selection', decision)
        if selection['result']['choice'] is None:
            return {**base, 'outcome': 'choice_pending', 'next': {'stage': 'selection', 'decision': decision}}
    interactions = current('interactions')
    if interactions is None:
        return pending('interactions')
    if not interactions['result']['compatible']:
        return {**base, 'outcome': 'interactions_blocked', 'reference': interactions['reference']}

    if inputs is None:
        dependencies = ledger.dependencies('alignment')

        def matches(record):
            return record['head'] == head and record['dependencies'] == dependencies
    else:
        _, binding = ledger.binding('alignment', inputs=inputs, head=head)

        def matches(record):
            return record['binding'] == binding
    alignment = ledger.latest('alignment')
    if alignment is None or not matches(alignment):
        value = {**base, 'outcome': 'alignment_required', 'allowance': ledger.usage(allowance('alignment', account)),
                 'next': {'stage': 'alignment', 'decision': None}}
        claim = ledger.open_claim('alignment')
        if claim is not None and matches(claim):
            value['next']['resume'] = claim['operation']
        if alignment is not None:
            value['reason'] = ('later_head' if alignment['head'] != head else 'changed_analysis'
                               if alignment['dependencies'] != ledger.dependencies('alignment') else 'new_evidence')
            # Earlier conclusions are context for the refresh, never current evidence.
            value['context'] = [alignment['reference']]
            verified = ledger.latest('verification')
            if verified is not None and verified['dependencies']['alignment'] == alignment['reference']:
                value['context'].append(verified['reference'])
        return value
    result = alignment['result']['alignment']
    references = [alignment['reference']]
    if result == 'aligned':
        return {**base, 'outcome': 'released', 'release': True, 'references': references}
    if result == 'uncertain':
        return {**base, 'outcome': 'alignment_uncertain', 'references': references, 'refresh': 'new_evidence',
                'allowance': ledger.usage(allowance('alignment', account))}
    verification = current('verification')
    if verification is None:
        return pending('verification')
    references.append(verification['reference'])
    disposition = verification['result']['disposition']
    if disposition == 'rejected':
        return {**base, 'outcome': 'released', 'release': True, 'references': references}
    if disposition == 'uncertain':
        return {**base, 'outcome': 'alignment_blocked', 'references': references}
    changes = verification['result']['required_changes']
    for receipt in receipts:
        if receipt.get('head') != head:
            continue
        require(receipt.get('event') == 'REQUEST_CHANGES' and receipt.get('body') == changes
                and not receipt.get('comments'),
                'alternatives rejection must contain exactly the independently verified required changes')
        return {**base, 'outcome': 'alternatives_rejected', 'complete': True, 'ordinary_review_complete': False,
                'references': references, 'receipt': _text(receipt.get('intent'), 'receipt intent')}
    return {**base, 'outcome': 'mismatch_verified', 'references': references, 'required_changes': changes}
