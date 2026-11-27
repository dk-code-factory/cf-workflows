"""Current-alignment recovery (dk-workflows#239, #241): a disposable Git repository for real
heads, a disposable SQLite ledger per test and a fail-closed fake native-session transport
that counts dispatches per stage. No network, provider or publication is used."""
from collections import Counter
import copy
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cf_workflows import alignment  # noqa: E402
from cf_workflows.alignment import AllowanceExhausted, AlignmentError, AuthorizationRequired  # noqa: E402

PR = {'url': 'https://github.com/owner/repo/pull/7', 'number': 7}
REQUIREMENTS = {'requirements': 'a' * 64}
TICKET = {'requirements': 'a' * 64, 'ticket': 'b' * 64}
POST = {'post_findings': {'effective': True}}
DENIED = {'post_findings': {'effective': False}}
CHANGES = 'Route retries through the shared scheduler.'


def request(ident, origin='user', skill=alignment.DIRECT_SKILL):
    return {'id': ident, 'source': 'chat:' + ident, 'instruction': 'Compare the retry design again.',
            'skill': skill, 'origin': origin}


class Sessions:
    """Native sessions scripted per stage key; any unscripted dispatch fails without a live fallback."""

    def __init__(self, results):
        self.results = results
        self.calls = Counter()
        self.sessions = Counter()

    def dispatch(self, stage_key, assignment):
        if stage_key not in self.results:
            raise AssertionError('unconfigured fixture operation: ' + stage_key)
        self.calls[stage_key] += 1
        self.sessions['all'] += 1
        return copy.deepcopy(self.results[stage_key]), f"session-{self.sessions['all']}"


def architecture(choice='retain', compatible=True, decisions=('retry',)):
    results = {'assessment': {'purpose': 'Bound retries', 'rubrics': []},
               'inventory': {'decisions': [{'id': d, 'questionable': True} for d in decisions]},
               'interactions': {'compatible': compatible, 'reason': 'Selections combine.'}}
    for decision in decisions:
        results['comparison:' + decision] = {'solutions': [{'id': 'retain'}, {'id': 'smallest'}]}
        results['selection:' + decision] = {'choice': choice}
    return results


def aligned(value='aligned'):
    result = {'alignment': value, 'reason': 'Judged against the current sources.'}
    if value == 'mismatch':
        result['required_changes'] = CHANGES
    return result


def verified(disposition='confirmed'):
    result = {'disposition': disposition, 'reason': 'Independently checked.'}
    if disposition == 'confirmed':
        result['required_changes'] = CHANGES
    return result


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.heads = [self.commit(f'v{n}') for n in range(3)]
        self.path = alignment.ledger_path(root / 'state', 'run-1')
        self.ledger = self.open()
        self.operations = Counter()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args], cwd=self.repo,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, content):
        (self.repo / 'retry.py').write_text(content)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', content)
        return self.git('rev-parse', 'HEAD')

    def open(self, **kwargs):
        return alignment.Ledger(self.path, logical_run=kwargs.get('logical_run', 'run-1'), pr=kwargs.get('pr', PR))

    def reopen(self):
        """A replacement run or fresh session: new connection, same retained record."""
        self.ledger.close()
        self.ledger = self.open()

    def operation(self, stage_key):
        self.operations[stage_key] += 1
        return f'op-{stage_key}-{self.operations[stage_key]}'

    def step(self, sessions, stage, decision=None, *, head, inputs=None, request=None, limit=None, finish=True):
        stage_key = alignment.key(stage, decision)
        prepared = self.ledger.prepare(stage, decision, inputs=inputs or {}, head=head,
                                       operation=self.operation(stage_key), request=request, limit=limit)
        if prepared['action'] != 'reuse' and finish:
            result, session = sessions.dispatch(stage_key, prepared)
            self.ledger.finish(prepared['operation'], result, session=session)
        return prepared

    def settle(self, sessions, head, decisions=('retry',)):
        self.step(sessions, 'assessment', head=head)
        self.step(sessions, 'inventory', head=head)
        for decision in decisions:
            self.step(sessions, 'comparison', decision, head=head)
            self.step(sessions, 'selection', decision, head=head)
        self.step(sessions, 'interactions', head=head)

    def gate(self, head, inputs=REQUIREMENTS, **kwargs):
        return alignment.gate(self.ledger, head=head, inputs=inputs, **kwargs)


class ArchitectureOnceTest(Fixture):
    def test_initial_analysis_runs_once_and_unchanged_reuse_starts_no_session(self):
        sessions = Sessions({**architecture(), 'alignment': aligned()})
        h0 = self.heads[0]
        self.assertEqual(self.gate(h0)['next'], {'stage': 'assessment', 'decision': None})
        self.settle(sessions, h0)
        self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)
        released = self.gate(h0)
        self.assertEqual((released['outcome'], released['release']), ('released', True))
        self.assertEqual((released['clean_review'], released['ready']), (False, False))
        self.assertEqual(sum(sessions.calls.values()), 6)

        self.reopen()
        self.settle(sessions, h0)
        self.assertEqual(self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)['action'], 'reuse')
        self.assertEqual(sum(sessions.calls.values()), 6)
        self.assertEqual(self.ledger.usage('architecture')['used'], 5)
        self.assertEqual(self.ledger.usage('alignment')['used'], 1)

    def test_head_guidance_and_fingerprint_changes_never_authorize_a_fresh_comparison(self):
        sessions = Sessions(architecture())
        self.settle(sessions, self.heads[0])
        # A later head alone: settled stages do not bind the head and are reused.
        self.settle(sessions, self.heads[1])
        self.assertEqual(sessions.calls['comparison:retry'], 1)
        guidance = {'guidance': 'c' * 64}
        with self.assertRaisesRegex(AuthorizationRequired, 'settled'):
            self.step(sessions, 'comparison', 'retry', head=self.heads[1], inputs=guidance)
        for wrong in (request('r1', origin='agent'), request('r1', skill='cf-workflows:dk-pr-alternatives'),
                      {**request('r1'), 'extra': True}):
            with self.assertRaises(AuthorizationRequired):
                self.step(sessions, 'comparison', 'retry', head=self.heads[1], inputs=guidance, request=wrong)
        self.assertEqual(sessions.calls['comparison:retry'], 1)
        self.assertEqual(self.ledger.usage('architecture')['used'], 5)
        self.assertIsNone(self.ledger.open_claim('comparison:retry'))

        self.assertEqual(self.step(sessions, 'comparison', 'retry', head=self.heads[1], inputs=guidance,
                                   request=request('r1'))['action'], 'run')
        self.assertEqual(sessions.calls['comparison:retry'], 2)
        # Completed work of the same direct request cannot restart; a distinct one can.
        with self.assertRaisesRegex(AuthorizationRequired, 'distinct'):
            self.step(sessions, 'comparison', 'retry', head=self.heads[1], inputs={'guidance': 'd' * 64},
                      request=request('r1'))
        conflicting = {**request('r1'), 'instruction': 'Something else.'}
        with self.assertRaisesRegex(AlignmentError, 'identity conflict'):
            self.step(sessions, 'selection', 'retry', head=self.heads[1], request=conflicting)
        self.step(sessions, 'comparison', 'retry', head=self.heads[1], inputs={'guidance': 'd' * 64},
                  request=request('r2'))
        self.assertEqual(sessions.calls['comparison:retry'], 3)
        self.assertEqual(len(self.ledger.history('comparison:retry')), 3)
        # An earlier request stays consumed after a later one ran: R1, R2, then R1 again is refused.
        with self.assertRaisesRegex(AuthorizationRequired, 'distinct'):
            self.step(sessions, 'comparison', 'retry', head=self.heads[1], inputs={'guidance': '0' * 64},
                      request=request('r1'))
        self.assertEqual(sessions.calls['comparison:retry'], 3)
        self.assertEqual(self.ledger.usage('architecture')['used'], 7)

    def test_downstream_of_an_authorized_comparison_needs_its_own_coverage(self):
        sessions = Sessions({**architecture(decisions=('retry', 'cache')), 'alignment': aligned()})
        h0 = self.heads[0]
        self.settle(sessions, h0, decisions=('retry', 'cache'))
        self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)
        self.step(sessions, 'comparison', 'retry', head=h0, inputs={'guidance': 'c' * 64}, request=request('r1'))
        pending = self.gate(h0)
        self.assertEqual((pending['outcome'], pending['next']['stage'], pending['authorization_required']),
                         ('analysis_required', 'selection', True))
        with self.assertRaises(AuthorizationRequired):
            self.step(sessions, 'selection', 'retry', head=h0)
        self.step(sessions, 'selection', 'retry', head=h0, request=request('r1'))
        # The same option over the refreshed comparison: interactions consumed that comparison, so it refreshes.
        self.assertEqual(self.gate(h0)['next']['stage'], 'interactions')
        self.step(sessions, 'interactions', head=h0, request=request('r1'))
        refreshed = self.gate(h0)
        self.assertEqual((refreshed['outcome'], refreshed['reason']), ('alignment_required', 'changed_analysis'))
        self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)
        self.assertEqual(self.gate(h0)['outcome'], 'released')
        # The unchanged sibling comparison and selection were never repeated.
        self.assertEqual((sessions.calls['comparison:cache'], sessions.calls['selection:cache']), (1, 1))
        self.assertEqual(sessions.calls['assessment'] + sessions.calls['inventory'], 2)

    def test_the_other_scope_adopts_the_settled_assessment_without_a_session(self):
        sessions = Sessions(architecture())
        self.step(sessions, 'assessment', head=self.heads[0])
        own = self.ledger.latest('assessment')
        whole = alignment.Ledger(alignment.ledger_path(Path(self.tmp.name) / 'state', 'run-1', 'whole-problem'),
                                 logical_run='run-1', pr=PR, scope='whole-problem')
        self.addCleanup(whole.close)
        origin = {'scope': 'branch', 'operation': own['operation'], 'session': own['session'],
                  'result': own['result']}
        refused = [
            ('inventory', origin), ('assessment', {**origin, 'scope': 'whole-problem'}),
            ('assessment', {**origin, 'operation': alignment.SHARED_PREFIX + 'branch:op'}),
            ('assessment', {**origin, 'result': None}),
        ]
        for stage, value in refused:
            with self.assertRaises(AlignmentError, msg=(stage, value)):
                whole.adopt(stage, inputs={}, head=self.heads[0], origin=value)
        adopted = whole.adopt('assessment', inputs={}, head=self.heads[1], origin=origin)
        self.assertEqual((adopted['state'], adopted['session'], adopted['result'], adopted['head']),
                         ('complete', own['session'], own['result'], None))
        self.assertEqual(adopted['reference'], f"assessment@shared:branch:{own['operation']}")
        self.assertEqual(whole.usage('architecture')['used'], 0)
        with self.assertRaises(AlignmentError):  # Its own settled assessment is never replaced.
            whole.adopt('assessment', inputs={}, head=self.heads[1], origin=origin)
        # Downstream stages consume it like an own result, with no assessment session.
        prepared = whole.prepare('inventory', inputs={}, head=self.heads[1], operation='op-inventory-whole')
        self.assertEqual((prepared['action'], prepared['dependencies']), ('run', {'assessment': adopted['reference']}))
        self.assertEqual(sessions.calls['assessment'], 1)
        # A record without a PR shares nothing.
        issue = alignment.Ledger(Path(self.tmp.name) / 'issue.sqlite', logical_run='issue',
                                 subject='repository:github.com/owner/repo', scope='whole-problem')
        self.addCleanup(issue.close)
        with self.assertRaises(AlignmentError):
            issue.adopt('assessment', inputs={}, head=self.heads[0], origin=origin)

    def test_added_inventory_sibling_preserves_unchanged_comparisons(self):
        sessions = Sessions(architecture())
        h0 = self.heads[0]
        self.settle(sessions, h0)
        sessions.results.update(architecture(decisions=('retry', 'cache')))
        self.step(sessions, 'inventory', head=h0, inputs={'scope': 'e' * 64}, request=request('r1'))
        self.assertEqual(self.gate(h0)['next'], {'stage': 'comparison', 'decision': 'cache'})
        self.assertEqual(self.step(sessions, 'comparison', 'retry', head=h0)['action'], 'reuse')
        self.step(sessions, 'comparison', 'cache', head=h0)
        self.step(sessions, 'selection', 'cache', head=h0)
        self.assertEqual(sessions.calls['comparison:retry'], 1)

    def test_pending_choice_and_incompatible_interactions_block(self):
        sessions = Sessions(architecture(choice=None))
        self.step(sessions, 'assessment', head=self.heads[0])
        self.step(sessions, 'inventory', head=self.heads[0])
        self.step(sessions, 'comparison', 'retry', head=self.heads[0])
        self.step(sessions, 'selection', 'retry', head=self.heads[0])
        self.assertEqual(self.gate(self.heads[0])['outcome'], 'choice_pending')
        with self.assertRaisesRegex(AlignmentError, 'no settled choice'):
            self.step(sessions, 'interactions', head=self.heads[0])
        # A selection without a choice is not settled: a user choice refreshes it without a direct request.
        sessions.results['selection:retry'] = {'choice': 'smallest'}
        self.step(sessions, 'selection', 'retry', head=self.heads[0], inputs={'user_choice': 'f' * 64})
        sessions.results['interactions'] = {'compatible': False, 'reason': 'Conflicts.'}
        self.step(sessions, 'interactions', head=self.heads[0])
        self.assertEqual(self.gate(self.heads[0])['outcome'], 'interactions_blocked')
        with self.assertRaisesRegex(AlignmentError, 'incompatible'):
            self.step(sessions, 'alignment', head=self.heads[0], inputs=REQUIREMENTS)


class CurrentAlignmentTest(Fixture):
    def mismatch_at(self, sessions, head):
        self.step(sessions, 'alignment', head=head, inputs=REQUIREMENTS)
        self.step(sessions, 'verification', head=head)

    def test_239_later_head_recovers_without_a_historical_rejection_receipt(self):
        sessions = Sessions({**architecture(), 'alignment': aligned('mismatch'), 'verification': verified()})
        h0, h1 = self.heads[:2]
        self.settle(sessions, h0)
        self.mismatch_at(sessions, h0)
        at_h0 = self.gate(h0)
        self.assertEqual((at_h0['outcome'], at_h0['release'], at_h0['required_changes']),
                         ('mismatch_verified', False, CHANGES))

        # The author pushed h1. No rejection was ever published, and none is fabricated.
        self.reopen()
        at_h1 = self.gate(h1)
        self.assertEqual((at_h1['outcome'], at_h1['reason']), ('alignment_required', 'later_head'))
        self.assertEqual(at_h1['context'], [self.ledger.latest('alignment')['reference'],
                                            self.ledger.latest('verification')['reference']])
        self.settle(sessions, h1)
        sessions.results['alignment'] = aligned()
        self.assertEqual(self.step(sessions, 'alignment', head=h1, inputs=REQUIREMENTS)['action'], 'run')
        released = self.gate(h1)
        self.assertEqual((released['outcome'], released['release'], released['clean_review']), ('released', True, False))
        self.assertEqual(sessions.calls['comparison:retry'], 1)
        self.assertEqual(sessions.calls['selection:retry'], 1)
        self.assertEqual(sessions.calls['alignment'], 2)
        # The original mismatch, its verification and their allowance remain.
        history = self.ledger.history('alignment')
        self.assertEqual([(h['head'], h['result']['alignment']) for h in history], [(h0, 'mismatch'), (h1, 'aligned')])
        self.assertEqual(self.ledger.usage('alignment')['used'], 3)

    def test_a_caller_without_alignment_evidence_gates_on_head_and_analysis_only(self):
        # The installed workflow holds no requirements evidence: inputs None keeps the retained ones.
        sessions = Sessions({**architecture(), 'alignment': aligned()})
        h0, h1 = self.heads[:2]
        self.settle(sessions, h0)
        self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)
        self.assertEqual(self.gate(h0, inputs=None)['outcome'], 'released')
        # New requirements reach the gate only through a caller that supplies them.
        self.assertEqual(self.gate(h0, inputs=TICKET)['reason'], 'new_evidence')
        later = self.gate(h1, inputs=None)
        self.assertEqual((later['outcome'], later['reason']), ('alignment_required', 'later_head'))
        interrupted = self.step(sessions, 'alignment', head=h1, inputs=REQUIREMENTS, finish=False)
        self.assertEqual(self.gate(h1, inputs=None)['next']['resume'], interrupted['operation'])
        self.ledger.finish(interrupted['operation'], aligned(), session='session-h1')
        self.assertEqual(self.gate(h1, inputs=None)['outcome'], 'released')
        # A record read through Records alone (the installed helpers) gives the same outcome, read-only.
        before = self.path.read_bytes()
        with alignment.Records.open(self.path, logical_run='run-1', pr_url=PR['url'].upper()) as records:
            self.assertEqual(alignment.gate(records, head=h1, inputs=None), self.gate(h1, inputs=None))
            with self.assertRaises(sqlite3.OperationalError):
                records.db.execute('DELETE FROM attempts')
        self.assertEqual(self.path.read_bytes(), before)
        for wrong in ({'logical_run': 'run-2', 'pr_url': PR['url']},
                      {'logical_run': 'run-1', 'pr_url': 'https://github.com/owner/repo/pull/8'}):
            with self.assertRaisesRegex(AlignmentError, 'another logical run'):
                alignment.Records.open(self.path, **wrong)
        missing = self.path.with_name('missing.alignment.sqlite')
        with self.assertRaisesRegex(AlignmentError, 'missing'):
            alignment.Records.open(missing, logical_run='run-1', pr_url=PR['url'])
        self.assertFalse(missing.exists())

    def test_an_existing_legitimate_rejection_stays_reusable(self):
        sessions = Sessions({**architecture(), 'alignment': aligned('mismatch'), 'verification': verified()})
        h0, h1 = self.heads[:2]
        self.settle(sessions, h0)
        self.mismatch_at(sessions, h0)
        receipt = {'intent': 'intent-1', 'head': h0, 'event': 'REQUEST_CHANGES', 'body': CHANGES, 'comments': []}
        rejected = self.gate(h0, receipts=[receipt])
        self.assertEqual((rejected['outcome'], rejected['complete'], rejected['ordinary_review_complete'],
                          rejected['receipt']), ('alternatives_rejected', True, False, 'intent-1'))
        with self.assertRaisesRegex(AlignmentError, 'exactly'):
            self.gate(h0, receipts=[{**receipt, 'body': 'Something else.'}])
        # After the author's commits the receipt is history: current alignment is refreshed, not replayed.
        self.assertEqual(self.gate(h1, receipts=[receipt])['outcome'], 'alignment_required')

    def test_241_uncertain_alignment_consumes_new_requirements_without_repeating_comparison(self):
        sessions = Sessions({**architecture(), 'alignment': aligned('uncertain')})
        h0 = self.heads[0]
        self.settle(sessions, h0)
        self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)
        uncertain = self.gate(h0)
        self.assertEqual((uncertain['outcome'], uncertain['release'], uncertain['refresh']),
                         ('alignment_uncertain', False, 'new_evidence'))
        # An unchanged retry reuses the uncertain result; it does not run again.
        self.assertEqual(self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)['action'], 'reuse')

        self.reopen()
        stale = self.gate(h0, inputs=TICKET)
        self.assertEqual((stale['outcome'], stale['reason']), ('alignment_required', 'new_evidence'))
        self.assertEqual(stale['allowance']['used'], 1)
        sessions.results['alignment'] = aligned()
        for stage, decision in (('assessment', None), ('inventory', None), ('comparison', 'retry'),
                                ('selection', 'retry'), ('interactions', None)):
            self.assertEqual(self.step(sessions, stage, decision, head=h0)['action'], 'reuse')
        self.assertEqual(self.step(sessions, 'alignment', head=h0, inputs=TICKET)['action'], 'run')
        self.assertEqual(self.gate(h0, inputs=TICKET)['outcome'], 'released')
        self.assertEqual(sessions.calls, Counter({'assessment': 1, 'inventory': 1, 'comparison:retry': 1,
                                                  'selection:retry': 1, 'interactions': 1, 'alignment': 2}))
        self.assertEqual(self.ledger.usage('alignment')['used'], 2)
        self.assertEqual(self.ledger.usage('architecture')['used'], 5)
        self.assertEqual([h['result']['alignment'] for h in self.ledger.history('alignment')], ['uncertain', 'aligned'])

    def test_interrupted_later_head_refresh_resumes_its_original_attempt(self):
        sessions = Sessions({**architecture(), 'alignment': aligned()})
        h0, h1, h2 = self.heads
        self.settle(sessions, h0)
        self.step(sessions, 'alignment', head=h0, inputs=REQUIREMENTS)
        interrupted = self.step(sessions, 'alignment', head=h1, inputs=REQUIREMENTS, finish=False)
        self.assertEqual(sessions.calls['alignment'], 1)

        self.reopen()
        recovery = self.gate(h1)
        self.assertEqual((recovery['outcome'], recovery['next']['resume']),
                         ('alignment_required', interrupted['operation']))
        resumed = self.ledger.prepare('alignment', inputs=REQUIREMENTS, head=h1, operation='op-replacement')
        self.assertEqual((resumed['action'], resumed['operation']), ('resume', interrupted['operation']))
        self.assertEqual(self.ledger.usage('alignment')['used'], 2)
        # The engine operation's replay returns the same attempt without consuming.
        self.assertEqual(self.ledger.prepare('alignment', inputs=REQUIREMENTS, head=h1,
                                             operation=interrupted['operation'])['action'], 'resume')

        # The head moves again before the native session finished: the claim is superseded, not erased.
        later = self.step(sessions, 'alignment', head=h2, inputs=REQUIREMENTS)
        self.assertEqual((later['action'], later['superseded']), ('run', interrupted['operation']))
        late = self.ledger.finish(interrupted['operation'], aligned('mismatch'), session='session-late')
        self.assertEqual(late['state'], 'superseded')
        self.assertEqual(self.ledger.latest('alignment')['head'], h2)
        self.assertEqual(self.gate(h2)['outcome'], 'released')
        with self.assertRaisesRegex(AlignmentError, 'superseded'):
            self.ledger.prepare('alignment', inputs=REQUIREMENTS, head=h1, operation=interrupted['operation'])
        self.assertEqual(self.ledger.usage('alignment')['used'], 3)
        self.assertEqual(sessions.calls['comparison:retry'], 1)

    def test_refresh_allowance_is_retained_narrowed_and_only_extended_by_a_grant(self):
        sessions = Sessions({**architecture(), 'alignment': aligned('uncertain')})
        self.settle(sessions, self.heads[0])
        for n, head in enumerate(self.heads[:2]):
            self.step(sessions, 'alignment', head=head, inputs=REQUIREMENTS, limit=2)
        self.reopen()
        # A wider configuration in a new run cannot raise the original ceiling.
        with self.assertRaises(AllowanceExhausted):
            self.step(sessions, 'alignment', head=self.heads[2], inputs=REQUIREMENTS, limit=9)
        self.assertEqual(self.gate(self.heads[2])['allowance'], {'allowance': 'alignment', 'used': 2, 'limit': 2,
                                                                  'exhausted': True})
        self.ledger.grant('continue-1', 'alignment', 1, 'Refresh alignment once more.')
        self.ledger.grant('continue-1', 'alignment', 1, 'Refresh alignment once more.')
        with self.assertRaisesRegex(AlignmentError, 'identity conflict'):
            self.ledger.grant('continue-1', 'alignment', 2, 'Refresh alignment once more.')
        self.step(sessions, 'alignment', head=self.heads[2], inputs=REQUIREMENTS, limit=9)
        self.assertEqual(self.ledger.usage('alignment'), {'allowance': 'alignment', 'used': 3, 'limit': 3,
                                                          'exhausted': True})
        self.assertEqual(sessions.calls['alignment'], 3)

    def test_mismatch_verification_requires_an_independent_session(self):
        sessions = Sessions({**architecture(), 'alignment': aligned('mismatch')})
        self.settle(sessions, self.heads[0])
        self.step(sessions, 'alignment', head=self.heads[0], inputs=REQUIREMENTS)
        session = self.ledger.latest('alignment')['session']
        prepared = self.ledger.prepare('verification', inputs={}, head=self.heads[0], operation='op-verify')
        with self.assertRaisesRegex(AlignmentError, 'independent'):
            self.ledger.finish('op-verify', verified(), session=session)
        self.assertEqual(self.gate(self.heads[0])['next'], {'stage': 'verification', 'decision': None,
                                                            'resume': prepared['operation']})
        self.ledger.finish('op-verify', verified('uncertain'), session='session-other')
        self.assertEqual(self.gate(self.heads[0])['outcome'], 'alignment_blocked')
        with self.assertRaisesRegex(AlignmentError, 'another result'):
            self.ledger.finish('op-verify', verified(), session='session-other')

    def test_rejected_mismatch_releases_and_provisional_identity_is_reported(self):
        sessions = Sessions({**architecture(), 'alignment': aligned('mismatch'), 'verification': verified('rejected')})
        self.settle(sessions, self.heads[0])
        self.mismatch_at(sessions, self.heads[0])
        identities = {'current': False, 'pin': self.heads[0], 'metadata_head': self.heads[1],
                      'git_head': self.heads[0], 'mismatched': ['head']}
        result = self.gate(self.heads[0], identities=identities)
        self.assertEqual((result['outcome'], result['provisional'], result['ready']), ('released', True, False))


class PartialCommentTest(Fixture):
    def setUp(self):
        super().setUp()
        self.head = self.heads[1]
        self.finding = {'id': 'F1', 'pr': PR['url'], 'head': self.head, 'fingerprint': 'f' * 64,
                        'session': 'reviewer-1', 'text': 'Retries ignore the deadline.'}
        self.verification = {'finding': 'F1', 'head': self.head, 'fingerprint': 'f' * 64,
                             'disposition': 'confirmed', 'session': 'verifier-1'}

    def snapshot(self, current=True):
        identities = ({'current': True, 'pin': self.head, 'metadata_head': self.head, 'git_head': None,
                       'mismatched': []} if current else
                      {'current': False, 'pin': self.head, 'metadata_head': self.heads[0], 'git_head': self.head,
                       'mismatched': ['head']})
        return {'pr': PR, 'head': self.head, 'identities': identities}

    def check(self, finding=None, verification=None, *, current=True, permissions=POST, publications=()):
        return alignment.partial_comment(finding or self.finding, verification or self.verification,
                                         snapshot=self.snapshot(current), permissions=permissions,
                                         publications=publications)

    def test_verified_finding_is_eligible_while_unrelated_alternatives_recovery_is_incomplete(self):
        sessions = Sessions({**architecture(), 'alignment': aligned('mismatch'), 'verification': verified()})
        self.settle(sessions, self.heads[0])
        self.step(sessions, 'alignment', head=self.heads[0], inputs=REQUIREMENTS)
        self.step(sessions, 'verification', head=self.heads[0])
        recovery = self.gate(self.head)
        self.assertEqual((recovery['outcome'], recovery['release']), ('alignment_required', False))
        eligible = self.check()
        self.assertEqual(eligible, {'finding': 'F1', 'eligible': True, 'event': 'COMMENT', 'reasons': [],
                                    'clean_review': False, 'approve': False, 'ready': False})
        # Eligibility changed nothing in the alternatives state.
        self.assertEqual(self.gate(self.head), recovery)
        self.assertEqual(sum(sessions.calls.values()), 7)

    def test_provisional_identity_blocks_until_reconciled(self):
        self.assertEqual(self.check(current=False)['reasons'], ['provisional'])
        self.assertTrue(self.check(current=True)['eligible'])

    def test_each_own_check_withholds_the_finding(self):
        cases = {
            'other_pr': ({**self.finding, 'pr': 'https://github.com/owner/repo/pull/8'}, None, POST, ()),
            'stale_head': ({**self.finding, 'head': self.heads[0]},
                           {**self.verification, 'head': self.heads[0]}, POST, ()),
            'stale_verification': (None, {**self.verification, 'fingerprint': 'e' * 64}, POST, ()),
            'unverified': (None, {**self.verification, 'disposition': 'uncertain'}, POST, ()),
            'not_independent': (None, {**self.verification, 'session': 'reviewer-1'}, POST, ()),
            'permission': (None, None, DENIED, ()),
            'uncertain_publication': (None, None, POST, [{'findings': ['F1'], 'state': 'pending'}]),
            'already_applied': (None, None, POST, [{'findings': ['F1'], 'state': 'applied'}]),
        }
        for reason, (finding, verification, permissions, publications) in cases.items():
            with self.subTest(reason):
                result = self.check(finding, verification, permissions=permissions, publications=publications)
                self.assertFalse(result['eligible'])
                self.assertEqual(result['reasons'], [reason])
                self.assertFalse(result['clean_review'])
        settled = self.check(publications=[{'findings': ['F1'], 'state': 'not_applied'},
                                           {'findings': ['F2'], 'state': 'pending'}])
        self.assertTrue(settled['eligible'])

    def test_a_released_gate_is_still_not_a_clean_review(self):
        sessions = Sessions({**architecture(), 'alignment': aligned()})
        self.settle(sessions, self.head)
        self.step(sessions, 'alignment', head=self.head, inputs=REQUIREMENTS)
        self.assertTrue(self.gate(self.head)['release'])
        self.assertEqual((self.gate(self.head)['clean_review'], self.check()['clean_review']), (False, False))


class LedgerTest(Fixture):
    def test_foreign_run_pr_and_version_are_refused(self):
        self.ledger.close()
        with self.assertRaisesRegex(AlignmentError, 'another logical run'):
            self.open(logical_run='run-2')
        with self.assertRaisesRegex(AlignmentError, 'another logical run'):
            self.open(pr={'url': 'https://github.com/owner/repo/pull/8'})
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA user_version = 2')
        db.close()
        with self.assertRaisesRegex(AlignmentError, 'unsupported'):
            self.open()
        self.ledger = sqlite3.connect(':memory:')

    def test_invalid_requests_and_results_reject_without_effects(self):
        with self.assertRaisesRegex(AlignmentError, 'must complete first'):
            self.ledger.prepare('inventory', inputs={}, head=self.heads[0], operation='op-1')
        with self.assertRaisesRegex(AlignmentError, 'decision ID'):
            self.ledger.prepare('comparison', inputs={}, head=self.heads[0], operation='op-1')
        with self.assertRaisesRegex(AlignmentError, 'full object ID'):
            self.ledger.prepare('assessment', inputs={}, head='main', operation='op-1')
        self.ledger.prepare('assessment', inputs={}, head=self.heads[0], operation='op-1')
        with self.assertRaisesRegex(AlignmentError, 'identity conflict'):
            self.ledger.prepare('assessment', inputs={'x': 'y'}, head=self.heads[0], operation='op-1')
        self.ledger.finish('op-1', {'purpose': 'p'}, session='s1')
        self.ledger.prepare('inventory', inputs={}, head=self.heads[0], operation='op-2')
        with self.assertRaisesRegex(AlignmentError, 'unique stable IDs'):
            self.ledger.finish('op-2', {'decisions': [{'id': 'a', 'questionable': True}] * 2}, session='s2')
        self.assertIsNone(self.ledger.latest('inventory'))
        self.assertEqual(self.ledger.usage('architecture')['used'], 2)

    def test_unconfigured_fixture_operation_fails_without_fallback(self):
        with self.assertRaisesRegex(AssertionError, 'unconfigured'):
            self.step(Sessions({}), 'assessment', head=self.heads[0])
        self.assertIsNotNone(self.ledger.open_claim('assessment'))


if __name__ == '__main__':
    unittest.main()
