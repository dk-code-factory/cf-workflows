"""Current-alignment records and the alignment gate (#23).

Authored for cf-workflows; no upstream counterpart. See docs/alignment.md.

This is the one implementation of the gate and of reading an alignment record.
The library module ``cf_workflows/alignment.py`` loads this file and builds its
writing ``Ledger`` on ``Records``; the installed helpers call ``gate`` from the
``alignment_gate`` operation (app.py) before review. It uses only the standard
library and imports nothing from this package, so the library can load it.

Settled architectural stages are read as they are. Alignment and verification
must match the head (and, when the caller supplies them, the alignment inputs);
otherwise they need a refresh, which needs no historical rejection receipt. No
gate outcome is a clean review or readiness. Nothing here writes, starts a
session or publishes.
"""
import hashlib
import json
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
    def open(cls, path, *, logical_run, pr_url):
        """An existing branch record of one PR, read-only: never created, migrated or written."""
        path = Path(path)
        require(not path.is_symlink() and path.is_file(), 'alignment record is missing or not a regular file')
        records = cls.__new__(cls)
        records.path, records.scope = path, 'branch'
        records.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        records.db.row_factory = sqlite3.Row
        try:
            version = records.db.execute('PRAGMA user_version').fetchone()[0]
            require(version == SCHEMA_VERSION, f'unsupported alignment ledger version {version}')
            meta = dict(records.db.execute('SELECT key, value FROM meta').fetchall())
            require({k: meta.get(k) for k in ('suite', 'kind')} == {'suite': SUITE, 'kind': KIND},
                    'not a cf-workflows alignment ledger')
            require(meta == {'suite': SUITE, 'kind': KIND, 'logical_run': logical_run, 'pr': pr_url.lower()},
                    'alignment ledger belongs to another logical run or PR')
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
