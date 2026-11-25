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
namespace. Nothing here starts a session or publishes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from cf_workflows import admission
from cf_workflows.admission import digest

SCHEMA_VERSION = 1
KIND = 'alignment_state'
STAGES = ('assessment', 'inventory', 'comparison', 'selection', 'interactions', 'alignment', 'verification')
SETTLED = ('assessment', 'inventory', 'comparison', 'selection', 'interactions')
CURRENT = ('alignment', 'verification')
DIRECT_SKILL = 'cf-workflows:dk-pr-review-alternatives'
# Default ceilings; the first reservation of an allowance fixes it for the logical run.
LIMITS = {'architecture': 16, 'alignment': 4}
ALIGNMENTS = ('aligned', 'mismatch', 'uncertain')
DISPOSITIONS = ('confirmed', 'rejected', 'uncertain')
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


SCOPES = ('branch', 'whole-problem')


def ledger_path(state_directory, logical_run, scope='branch'):
    """The logical run's alignment record, next to its run ledger; run text never forms a path.

    The branch scope (PR architecture) keeps its original name. Whole-problem analysis
    (cf-workflows:dk-issue-alternatives) has its own record and never shares stage keys.
    """
    require(scope in SCOPES, 'unknown alternatives scope')
    name = hashlib.sha256(_text(logical_run, 'logical run').encode()).hexdigest()
    return Path(state_directory) / 'runs' / (name + ('' if scope == 'branch' else '.' + scope) + '.alignment.sqlite')


def dumps(value):
    return admission.canonical(value).decode('utf-8')


def _reference(row):
    return f"{row['key']}@{row['operation']}"


class Ledger:
    """Retained architectural analysis, current alignment and allowances for one logical run and PR."""

    def __init__(self, path, *, logical_run, pr=None, subject=None, scope='branch'):
        """A PR's record binds its URL. Whole-problem analysis without a PR binds its repository
        subject instead and never invents a PR identity."""
        require(scope in SCOPES and (pr is None) != (subject is None) and (pr is not None or scope != 'branch'),
                'alignment ledger requires a PR, or a subject for whole-problem analysis')
        self.path = Path(path)
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

    def close(self):
        self.db.close()

    def transaction(self):
        db = self.db

        class Transaction:
            def __enter__(self):
                db.execute('BEGIN IMMEDIATE')

            def __exit__(self, kind, value, trace):
                db.execute('COMMIT' if kind is None else 'ROLLBACK')
        return Transaction()

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

    @staticmethod
    def _settled(stage, record):
        # A selection without a choice still waits for one; every other complete stage is settled.
        return not (stage == 'selection' and record['result']['choice'] is None)

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

def gate(ledger, *, head, inputs, identities=None, receipts=()):
    """Whether ordinary review may proceed at head under the current alignment inputs.

    Settled architectural stages are read as they are. Alignment and verification
    must match head and inputs; otherwise they need a refresh, which needs no
    historical rejection receipt. A receipt only completes alternatives when it
    rejected exactly the verified changes at this head. No outcome is a clean
    review or readiness.
    """
    _head(head)
    provisional = identities is not None and not identities.get('current')
    base = {'head': head, 'release': False, 'clean_review': False, 'ready': False, 'provisional': provisional}

    def pending(stage, decision=None, **extra):
        stage_key = key(stage, decision)
        claim, latest = ledger.open_claim(stage_key), ledger.latest(stage_key)
        authorization = stage in SETTLED and latest is not None and Ledger._settled(stage, latest)
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

    _, binding = ledger.binding('alignment', inputs=inputs, head=head)
    alignment = ledger.latest('alignment')
    if alignment is None or alignment['binding'] != binding:
        value = {**base, 'outcome': 'alignment_required', 'allowance': ledger.usage('alignment'),
                 'next': {'stage': 'alignment', 'decision': None}}
        claim = ledger.open_claim('alignment')
        if claim is not None and claim['binding'] == binding:
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
                'allowance': ledger.usage('alignment')}
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
