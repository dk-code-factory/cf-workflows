"""Configuration writes with exact intents and readback recovery (contract v1).

An accepted clarification dialogue can revise the saved configuration, for
example by removing a saved prompt. That exact admitted revision is written back
without another approval step. The write is recorded in the ledger as an
intent before dispatch: request ID, evidence references, observed base
revision, the exact document and its content digest. It is then marked
`pending` before the store is touched.

A lost response never causes a blind retry. Every unsettled intent is read back
before admission reads the configuration or prepares another write:

- the store holds the intended content: `applied`, with a readback receipt;
- the store still holds the base revision: the same intent is dispatched again,
  and compare-and-swap on that base prevents a duplicate application;
- anything else: `blocked`. Another writer changed the store, so whether this
  intent applied first is unknown. The intent and its uncertainty are kept, and
  admission reassesses whatever the current configuration now holds.

A conclusive compare-and-swap rejection settles `not_applied`. Settled intents,
including successful earlier siblings, are never dispatched again.
"""
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile

from cf_workflows import admission, semantic
from cf_workflows.admission import digest


class RevisionConflict(Exception):
    """The store's revision differs from the expected base: the write was not applied."""


def content(document):
    """Digest of a configuration document apart from its store-assigned revision."""
    if document is None:
        return None
    return digest({key: value for key, value in document.items() if key != 'revision'})


def revision(document):
    """A store revision derived from content, so an edit that keeps the stored field still changes it."""
    return 'rev_' + content(document)[:32]


class FileStore:
    """The suite's configuration file with atomic compare-and-swap writes.

    A revision is derived from the content, so the same admitted document always
    lands as the same revision.
    """

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        """The current document; its revision is recomputed from content, never trusted from the file."""
        if not self.path.exists():
            return None
        document = admission.load_json(self.path.read_bytes())
        return {**document, 'revision': revision(document)}

    def write(self, expected_revision, document):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + '.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.read()
            if (current['revision'] if current is not None else None) != expected_revision:
                raise RevisionConflict('configuration revision changed')
            written = revision(document)
            body = json.dumps({**document, 'revision': written}, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
            descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.configuration-')
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
            return written


def readback(intent, current):
    if content(current) == intent['target_digest']:
        return 'applied'
    if (current['revision'] if current is not None else None) == intent['base_revision']:
        return 'base'
    return 'changed'


def dispatch(store, ledger, intent):
    """Dispatch one prepared intent once; a lost response is settled by readback, never by retry."""
    ledger.dispatching(intent['intent_id'])
    try:
        written = store.write(intent['base_revision'], copy.deepcopy(intent['document']))
    except Exception as failure:
        reason = type(failure).__name__
        try:
            current = store.read()
        except Exception:
            return 'pending'  # Unknown effect: kept pending for the next readback.
        observed = readback(intent, current)
        if observed == 'applied':
            ledger.settle(intent['intent_id'], 'applied', {'revision': current['revision'], 'by': 'readback'})
            return 'applied'
        if isinstance(failure, RevisionConflict) and observed == 'changed':
            ledger.settle(intent['intent_id'], 'not_applied', failure='compare-and-swap rejected: ' + reason)
            return 'not_applied'
        if observed == 'changed':
            ledger.settle(intent['intent_id'], 'blocked',
                          failure=f'{reason}; the store changed, so this effect is uncertain; reassess the current revision')
            return 'blocked'
        return 'pending'  # Still at the base revision: the next reconciliation reads back before any dispatch.
    ledger.settle(intent["intent_id"], "applied", {"revision": written, "by": "response"})
    return 'applied'


def reconcile(store, ledger):
    """Read back every unsettled intent before any new write; returns what was settled."""
    unsettled = ledger.writes(('prepared', 'pending'))
    settled = []
    for intent in unsettled:
        current = store.read()
        observed = readback(intent, current)
        if observed == 'applied':
            ledger.settle(intent['intent_id'], 'applied', {'revision': current['revision'], 'by': 'readback'})
            status = 'applied'
        elif observed == 'base':
            status = dispatch(store, ledger, intent)
        else:
            ledger.settle(intent['intent_id'], 'blocked',
                          failure='the store changed before readback, so this effect is uncertain; '
                                  'reassess the current revision')
            status = 'blocked'
        settled.append({'intent_id': intent['intent_id'], 'status': status})
    return settled


def commit(store, ledger, base, revised, result):
    """Prepare and dispatch the exact admitted configuration revision."""
    record = result['admission']
    document = {**copy.deepcopy(revised), 'admission': copy.deepcopy(record)}
    document.pop('revision', None)
    intent = ledger.prepare(request_id=record['request_id'], evidence_refs=record['evidence_refs'],
                            base_revision=base['revision'] if base is not None else None,
                            document=document, target_digest=content(document))
    status = dispatch(store, ledger, intent)
    return {'intent_id': intent['intent_id'], 'status': status}


def admit(*, request, invocation, deployment, roles, roots, transport, store, ledger):
    """Recover configuration writes, then admit against the current saved configuration.

    store offers read() and write(expected_revision, document); ledger is an
    acceptance.Ledger. Neither is ever pointed at another suite's files.
    """
    try:
        recovered = reconcile(store, ledger)
        configuration = store.read()
    except Exception as failure:
        return {'preflight': 'blocked', 'structural': 'pending', 'semantic': None,
                'error': {'path': 'configuration_store', 'message': 'configuration store unavailable: '
                          + type(failure).__name__}}
    result = semantic.admit(request=request, configuration=copy.deepcopy(configuration), invocation=invocation,
                            deployment=deployment, roles=roles, roots=roots, transport=transport, ledger=ledger)
    result['recovered_writes'] = recovered
    revised = result.get('proposal', {}).get('configuration', configuration)
    if result['preflight'] == 'admitted' and revised != configuration:
        if ledger.writes(('prepared', 'pending')):
            # An earlier write is still unknown; never stack another on top of it.
            result['configuration_write'] = {'intent_id': None, 'status': 'deferred'}
        else:
            result['configuration_write'] = commit(store, ledger, configuration, revised, result)
    return result
