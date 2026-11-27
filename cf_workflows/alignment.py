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
namespace. Nothing here starts a session or publishes. The gate, the record's
reads and its writing Ledger live in the installed helpers (prfix/alignment.py),
which the installed alignment_gate operation and alternatives procedure call
too; this module re-exports them and adds partial COMMENT eligibility.
"""
from pathlib import Path
import types


# The gate, record reads and writes are the installed helpers' prfix/alignment.py, so the
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
# The writing ledger lives in the helper closure too, so the installed alternatives procedure
# (prfix/alternatives.py) and this library record every stage through one implementation.
(SHARED, SHARED_PREFIX, DIRECT_SKILL, ALIGNMENTS, DISPOSITIONS, SCHEMA, ANSWERS) = (
    _R.SHARED, _R.SHARED_PREFIX, _R.DIRECT_SKILL, _R.ALIGNMENTS, _R.DISPOSITIONS, _R.SCHEMA, _R.ANSWERS)
direct_request, _validate, dumps, Ledger = _R.direct_request, _R._validate, _R.dumps, _R.Ledger
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
