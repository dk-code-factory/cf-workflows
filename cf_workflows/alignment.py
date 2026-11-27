"""Current alignment over settled architectural analysis (contract v1).

A PR's architectural analysis runs once: shared assessment, decision inventory,
a comparison and a selection per questionable decision, and their interactions.
Once complete, each of those stages is settled. A changed input, guidance or head
never repeats it; only a distinct direct user request for
cf-workflows:dk-pr-review-alternatives does, once per stage.

Alignment and its independent verification are different. They judge the current
head under current requirements, so a later head or new evidence refreshes them
without authorization (dk-workflows#239, #241), and no historical rejection
receipt is needed. An uncertain alignment is never settled.

Every native attempt reserves one unit of a retained allowance under its engine
operation's identity. Replays, interrupted attempts and new sessions never
consume twice or reset a count; only an explicit grant adds capacity.

Partial COMMENT eligibility of a finding is decided separately, from the finding's
own identity, freshness, independent verification, permission and publication
uncertainty. Alternatives state neither blocks it nor implies a clean review.
Provisional identity blocks it like every other publication.

The record is a versioned SQLite database per logical run in the suite's state
namespace. Nothing here starts a session or publishes. The gate and the record's
reads live in the installed helpers (prfix/alignment.py), which the installed
alignment_gate operation calls too; this module adds the writes.
"""
import json
import os
from pathlib import Path
import sqlite3
import types

from cf_workflows import admission

# The gate and record reads are the installed helpers' prfix/alignment.py, so the
# library and the installed workflow apply one implementation (docs/alignment.md#installed-gate).
_SOURCE = (Path(__file__).resolve().parent / 'workflows' / 'dk-pr-fix' / 'workflows' / 'helpers' / 'prfix'
           / 'alignment.py')


def _records():
    # Compiled in memory: loading it never adds bytecode to the installed closure.
    module = types.ModuleType('cf_workflows._alignment_records')
    module.__file__ = str(_SOURCE)
    exec(compile(_SOURCE.read_bytes(), str(_SOURCE), 'exec'), module.__dict__)
    return module


_R = _records()
(SCHEMA_VERSION, KIND, STAGES, SETTLED, CURRENT, SCOPES, LIMITS) = (
    _R.SCHEMA_VERSION, _R.KIND, _R.STAGES, _R.SETTLED, _R.CURRENT, _R.SCOPES, _R.LIMITS)
AlignmentError, AuthorizationRequired, AllowanceExhausted = _R.AlignmentError, _R.AuthorizationRequired, _R.AllowanceExhausted
require, _text, _head, allowance, _base, key = _R.require, _R._text, _R._head, _R.allowance, _R._base, _R.key
digest, ledger_path, pr_run, gate, Records, _ID = _R.digest, _R.ledger_path, _R.pr_run, _R.gate, _R.Records, _R._ID
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


def dumps(value):
    return admission.canonical(value).decode('utf-8')


class Ledger(Records):
    """Retained architectural analysis, current alignment and allowances for one logical run and PR.

    Reads and stage dependencies come from Records (the installed helpers' prfix/alignment.py)."""

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
        expected = {'suite': admission.SUITE, 'kind': KIND, 'logical_run': logical_run}
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


# -- Partial COMMENT eligibility ---------------------------------------------------------------

def partial_comment(finding, verification, *, snapshot, permissions, publications=()):
    """Whether one independently verified finding may be posted in a partial COMMENT review.

    Decided only from the finding's own checks; alternatives state is not an input.
    Eligibility is never a clean review, an approval or readiness.
    """
    require(isinstance(finding, dict) and isinstance(verification, dict), 'finding and verification require objects')
    identities = snapshot['identities']
    reasons = []
    if not identities.get('current'):
        reasons.append('provisional')
    if finding.get('pr', '').lower() != snapshot['pr']['url'].lower():
        reasons.append('other_pr')
    if finding.get('head') != snapshot['head'] or finding.get('head') != identities.get('pin'):
        reasons.append('stale_head')
    if (verification.get('finding') != finding.get('id') or verification.get('head') != finding.get('head')
            or verification.get('fingerprint') != finding.get('fingerprint')):
        reasons.append('stale_verification')
    if verification.get('disposition') != 'confirmed':
        reasons.append('unverified')
    if not verification.get('session') or verification.get('session') == finding.get('session'):
        reasons.append('not_independent')
    entry = (permissions or {}).get('post_findings')
    if not (isinstance(entry, dict) and entry.get('effective') is True):
        reasons.append('permission')
    for publication in publications:
        if finding.get('id') not in publication.get('findings', ()):
            continue
        if publication.get('state') == 'pending':
            reasons.append('uncertain_publication')
        elif publication.get('state') in ('prepared', 'applied'):
            reasons.append('already_' + publication['state'])
    return {'finding': finding.get('id'), 'eligible': not reasons, 'event': 'COMMENT', 'reasons': reasons,
            'clean_review': False, 'approve': False, 'ready': False}
