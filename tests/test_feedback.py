"""Feedback obligations (dk-workflows#244): a disposable SQLite ledger per test and a
fail-closed fake GitHub transport whose only operation is a counted discussion capture.
No network, provider or publication is used."""
from collections import Counter
import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dk_pr_fix  # noqa: E402,F401  (puts the installed helpers on sys.path without bytecode)
from cf_workflows import admission  # noqa: E402
from prfix import feedback  # noqa: E402

PR = {'url': 'https://github.com/owner/repo/pull/7', 'number': 7}
URL = 'https://github.com/owner/repo/pull/7'
AUTHOR = {'id': 1, 'login': 'author', 'type': 'User'}
REVIEWER = {'id': 2, 'login': 'reviewer', 'type': 'User'}
BOB = {'id': 3, 'login': 'bob', 'type': 'User'}
BOT = {'id': 4, 'login': 'github-code-quality[bot]', 'type': 'Bot'}
BOTH = {'reply': {'effective': True}, 'resolve_thread': {'effective': True}}
REPLY_ONLY = {'reply': {'effective': True}, 'resolve_thread': {'effective': False}}
NONE = {'reply': {'effective': False}, 'resolve_thread': {'effective': False}}


def at(minute):
    return f'2026-11-25T10:{minute:02d}:00Z'


class GitHub:
    """Serves one mutable discussion; every operation other than capture fails."""

    def __init__(self):
        self.calls = Counter()
        self.ev = {'metadata': {'html_url': URL, 'number': 7, 'user': AUTHOR},
                   'viewer': {'id': 1, 'login': 'author'},
                   'comments': [], 'reviews': [], 'inline': [], 'threads': []}

    def __getattr__(self, name):
        def operation(*args, **kwargs):
            self.calls[name] += 1
            if name != 'capture':
                raise AssertionError('unconfigured fixture operation: ' + name)
            return copy.deepcopy(self.ev)
        return operation

    def inline(self, ident, user, body, minute, parent=None, thread=None):
        item = {'id': ident, 'user': user, 'body': body, 'created_at': at(minute),
                'html_url': f'{URL}#discussion_r{ident}', 'in_reply_to_id': parent}
        self.ev['inline'].append(item)
        if parent is None:
            self.ev['threads'].append({'id': thread or f'T{ident}', 'isResolved': False, 'isOutdated': False,
                                       'comments': {'nodes': [{'id': f'N{ident}', 'databaseId': ident}]}})
        return item

    def comment(self, ident, user, body, minute):
        item = {'id': ident, 'user': user, 'body': body, 'created_at': at(minute),
                'html_url': f'{URL}#issuecomment-{ident}'}
        self.ev['comments'].append(item)
        return item

    def review(self, ident, user, body, minute):
        item = {'id': ident, 'user': user, 'body': body, 'submitted_at': at(minute), 'state': 'COMMENTED',
                'html_url': f'{URL}#pullrequestreview-{ident}'}
        self.ev['reviews'].append(item)
        return item


def request(ident, excerpt, addressee='author', implementation='none', response=None, **extra):
    return {'id': ident, 'excerpt': excerpt, 'addressee': addressee, 'implementation': implementation,
            'response': response or {'disposition': 'reply'}, **extra}


def no_reply(reason, why='Not addressed to the PR author.'):
    return {'disposition': 'no_reply', 'reason': reason, 'justification': why}


def answered(kind, ident, why='The existing answer covers the concern.'):
    return {'disposition': 'already_answered', 'reply': {'kind': kind, 'id': ident}, 'justification': why}


class Feedback(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.path = feedback.ledger_path(self.state, 'run-1')
        self.github = GitHub()
        self.ledger = self.open()

    def open(self):
        """A fresh session: a new connection to the same retained record."""
        ledger = feedback.Ledger(self.path, logical_run='run-1', pr=PR)
        self.addCleanup(ledger.close)
        return ledger

    def view(self):
        before = self.github.calls['capture']
        result = feedback.observe(self.github, PR)
        self.assertEqual(self.github.calls, Counter(capture=before + 1))
        return result

    def assess(self, view, key, *requests, ledger=None):
        source = view.sources[key]
        return (ledger or self.ledger).assess(view, {'source': key, 'fingerprint': feedback.View.fingerprint(source),
                                                     'requests': list(requests)})

    def rejects(self, message, view, key, *requests):
        with self.assertRaisesRegex(feedback.FeedbackError, message):
            self.assess(view, key, *requests)


class RejectedSuggestions(Feedback):
    """Declining a code change never removes the concrete author's need for an answer."""

    def setUp(self):
        super().setUp()
        self.github.inline(100, BOT, 'Writing to a static field from an instance method is a code smell.', 1)
        self.github.inline(200, REVIEWER, 'nit: rename `x` to `count` for readability.', 2)

    def test_rejected_bot_and_style_suggestions_keep_their_response_obligation(self):
        view = self.view()
        for key, excerpt, category in (('inline:100', 'static field', 'correctness'),
                                       ('inline:200', 'rename `x`', 'style')):
            for addressee, reason in (('author', 'no_author_reply_needed'), ('none', 'no_author_reply_needed'),
                                      ('author', 'addressed_to_participant')):
                self.rejects('author|addressee', view, key, request(
                    'r1', excerpt, addressee, 'intentional' if category == 'correctness' else 'rejected',
                    no_reply(reason, 'Bot style suggestion; the existing code is intentional.'), category=category))
        self.assertEqual(self.ledger.history('inline:100'), [])

        self.assess(view, 'inline:100', request('r1', 'static field', implementation='intentional', category='correctness'))
        self.assess(view, 'inline:200', request('r1', 'rename `x`', implementation='rejected', category='style'))
        result = feedback.stage(self.ledger, view, BOTH)
        self.assertEqual((result['status'], result['replies_needed']), ('pending', ['inline:100', 'inline:200']))
        self.assertTrue(result['sources']['inline:100']['author']['bot'])

        self.github.inline(101, AUTHOR, 'Intentional: the field is thread-local by design.', 3, parent=100)
        self.github.inline(201, AUTHOR, 'Keeping `x`: it matches the surrounding loop indices.', 4, parent=200)
        view = self.view()
        self.ledger.record_reply(view, 'inline:100', {'kind': 'inline', 'id': 101}, 'intent:1')
        self.ledger.record_reply(view, 'inline:200', {'kind': 'inline', 'id': 201}, 'intent:2')
        result = feedback.stage(self.ledger, view, BOTH)
        self.assertEqual((result['status'], result['replies_needed'], result['unanswered']), ('complete', [], []))

    def test_unauthorized_replies_block_without_removing_the_obligation(self):
        view = self.view()
        self.assess(view, 'inline:100', request('r1', 'static field', implementation='intentional'))
        self.assess(view, 'inline:200', request('r1', 'rename `x`', implementation='fixed'))
        for permissions in (NONE, {}, {'reply': {'enabled': True, 'effective': False}}):
            result = feedback.stage(self.ledger, view, permissions)
            self.assertEqual((result['status'], result['replies_needed']), ('blocked', ['inline:100', 'inline:200']))
        self.assertEqual(self.github.calls, Counter(capture=1))

    def test_a_reassessment_cannot_drop_an_obligation(self):
        view = self.view()
        self.assess(view, 'inline:100', request('r1', 'static field', implementation='intentional'))
        self.rejects('drops the response obligation', view, 'inline:100',
                     request('r2', 'static field', 'none', response=no_reply('no_author_reply_needed')))
        self.rejects('drops the response obligation', view, 'inline:100',
                     request('r1', 'static field', 'none', response=no_reply('no_author_reply_needed')))
        # Adding an obligation is allowed; an identical repeat in a new session adds nothing.
        both = (request('r1', 'static field', implementation='intentional'), request('r2', 'code smell'))
        self.assess(view, 'inline:100', *both)
        self.assess(view, 'inline:100', *both, ledger=self.open())
        self.assertEqual(len(self.ledger.history('inline:100')), 2)

    def test_replies_count_only_when_observed_from_a_responder_in_place(self):
        self.github.inline(300, REVIEWER, 'Please also guard against null input.', 5)
        self.github.comment(900, REVIEWER, 'Agreed with the bot.', 6)
        view = self.view()
        self.assess(view, 'inline:100', request('r1', 'static field', implementation='intentional'))
        with self.assertRaisesRegex(feedback.FeedbackError, 'not observable'):
            self.ledger.record_reply(view, 'inline:100', {'kind': 'inline', 'id': 101}, 'intent:uncertain')
        self.github.inline(101, AUTHOR, 'Intentional.', 7, parent=300)  # Wrong thread.
        self.github.inline(102, REVIEWER, 'I think it is fine.', 8, parent=100)  # Not a responder.
        self.github.comment(901, AUTHOR, 'Intentional, see the field docs.', 9)  # Wrong location.
        view = self.view()
        for kind, ident in (('inline', 101), ('inline', 102), ('comment', 901)):
            with self.assertRaisesRegex(feedback.FeedbackError, 'own location'):
                self.ledger.record_reply(view, 'inline:100', {'kind': kind, 'id': ident}, 'intent:x')
        with self.assertRaisesRegex(feedback.FeedbackError, 'no current response obligation'):
            self.ledger.record_reply(view, 'inline:300', {'kind': 'inline', 'id': 101}, 'intent:x')

    def test_a_deleted_reply_reopens_the_obligation_and_keeps_its_record(self):
        view = self.view()
        self.assess(view, 'inline:100', request('r1', 'static field', implementation='intentional'))
        reply = self.github.inline(101, AUTHOR, 'Intentional.', 3, parent=100)
        self.ledger.record_reply(self.view(), 'inline:100', {'kind': 'inline', 'id': 101}, 'intent:1')
        self.github.ev['inline'].remove(reply)
        result = feedback.stage(self.open(), self.view(), BOTH)['sources']['inline:100']
        self.assertEqual((result['status'], result['reason'], result['replies']),
                         ('reply_needed', 'reply_unavailable', []))
        self.assertEqual(self.ledger.replies('inline:100', result['fingerprint']), ['inline:101'])


class MixedThreads(Feedback):
    """Author requests and questions for other participants in one thread."""

    def setUp(self):
        super().setUp()
        self.github.inline(100, REVIEWER, 'This leaks the handle on error. @bob, does the pool rely on it?', 1,
                           thread='T1')

    def mixed(self, view):
        return self.assess(view, 'inline:100',
                           request('r1', 'This leaks the handle on error.', implementation='fixed'),
                           request('r2', '@bob, does the pool rely on it?', 'participant', participant='bob',
                                   response=no_reply('addressed_to_participant')))

    def test_mixed_thread_needs_an_answer_and_the_participant_before_resolution(self):
        view = self.view()
        self.mixed(view)
        result = feedback.stage(self.ledger, view, BOTH)
        self.assertEqual((result['status'], result['replies_needed']), ('pending', ['inline:100']))
        self.assertEqual([(u['request'], u['reason']) for u in result['unanswered']],
                         [('r2', 'addressed_to_participant')])
        decision = feedback.resolution(self.ledger, view, 'T1', BOTH)
        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['blockers'], ['inline:100 still needs an answer', 'inline:100 awaits bob'])

        # Someone resolves the thread on GitHub: the answer is still owed.
        self.github.ev['threads'][0]['isResolved'] = True
        view = self.view()
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['replies_needed'], ['inline:100'])
        self.github.ev['threads'][0]['isResolved'] = False

        self.github.inline(101, AUTHOR, 'Fixed: the handle is closed on every error path.', 2, parent=100)
        view = self.view()
        self.ledger.record_reply(view, 'inline:100', {'kind': 'inline', 'id': 101}, 'intent:1')
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['status'], 'complete')
        self.assertEqual(feedback.resolution(self.ledger, view, 'T1', BOTH)['blockers'], ['inline:100 awaits bob'])

        self.github.inline(102, BOB, 'No, the pool does not rely on it.', 3, parent=100)
        view = self.view()
        # Bob's reply is itself a source; it needs its own assessment first.
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['unassessed'], ['inline:102'])
        self.assertEqual(feedback.resolution(self.ledger, view, 'T1', BOTH)['blockers'],
                         ['inline:102 lacks a current assessment'])
        self.assess(view, 'inline:102', request('r1', 'does not rely on it', 'none',
                                                response=no_reply('no_author_reply_needed', 'Answer to another question.')))
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['status'], 'complete')
        self.assertEqual(feedback.resolution(self.ledger, view, 'T1', REPLY_ONLY)['blockers'],
                         ['thread resolution lacks an effective permission and authorization'])
        self.assertEqual(feedback.resolution(self.ledger, view, 'T1', BOTH), {'thread': 'T1', 'allowed': True, 'blockers': []})
        self.assertEqual(self.github.calls, Counter(capture=4))

    def test_a_question_only_for_another_participant_needs_no_reply_but_blocks_resolution(self):
        self.github.inline(200, REVIEWER, '@bob can you confirm the retry budget?', 5, thread='T2')
        view = self.view()
        self.assess(view, 'inline:200', request('r1', '@bob can you confirm', 'participant', participant='bob',
                                                response=no_reply('addressed_to_participant')))
        self.mixed(view)
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['sources']['inline:200']['status'], 'settled')
        self.assertEqual(feedback.resolution(self.ledger, view, 'T2', BOTH)['blockers'], ['inline:200 awaits bob'])

    def test_participant_claims_must_be_sourced(self):
        view = self.view()
        for participant, message in (('carol', 'neither mentioned'), ('author', 'someone other'),
                                     ('reviewer', 'someone other'), ('not a login', 'account login')):
            self.rejects(message, view, 'inline:100',
                         request('r1', 'This leaks the handle on error.', implementation='fixed'),
                         request('r2', 'does the pool rely on it?', 'participant', participant=participant,
                                 response=no_reply('addressed_to_participant')))
        self.rejects('reason must be addressed_to_participant', view, 'inline:100',
                     request('r2', 'does the pool rely on it?', 'participant', participant='bob',
                             response=no_reply('no_author_reply_needed')))
        self.rejects('optional discussion stays optional', view, 'inline:100',
                     request('r2', 'does the pool rely on it?', 'participant', participant='bob'))
        self.rejects('not quoted', view, 'inline:100', request('r1', 'The handle is fine.', implementation='fixed'))
        # An explicit user instruction to answer makes the question required.
        self.assess(view, 'inline:100', request('r2', 'does the pool rely on it?', 'participant', participant='bob',
                                                instructed=True))
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['replies_needed'], ['inline:100'])


class AnsweredAndOptional(Feedback):
    """Existing answers and genuinely optional discussion keep justified dispositions."""

    def test_already_answered_sources_get_no_duplicate_obligation(self):
        review = self.github.review(500, REVIEWER, 'Why not cache the parsed config?', 1)
        self.github.comment(600, AUTHOR, f'Reply to [review]({review["html_url"]}): parsing is per request by design.', 2)
        self.github.inline(100, REVIEWER, 'Is this lock needed?', 3)
        self.github.inline(101, AUTHOR, 'Yes, it guards the shared map.', 4, parent=100)
        view = self.view()
        self.rejects('later reply', view, 'review:500',
                     request('r1', 'cache the parsed config', implementation='rejected', response=answered('inline', 101)))
        self.rejects('later reply', view, 'inline:100', request('r1', 'lock needed', response=answered('comment', 600)))
        self.assess(view, 'review:500', request('r1', 'cache the parsed config', implementation='rejected',
                                                response=answered('comment', 600)))
        self.assess(view, 'inline:100', request('r1', 'lock needed', response=answered('inline', 101)))
        for ledger in (self.ledger, self.open()):
            result = feedback.stage(ledger, self.view(), NONE)
            self.assertEqual((result['status'], result['replies_needed']), ('complete', []))
        self.assertEqual(feedback.resolution(self.ledger, view, 'T100', BOTH)['allowed'], True)
        # Repeating the assessment in a fresh session adds no record and no obligation.
        self.assess(view, 'inline:100', request('r1', 'lock needed', response=answered('inline', 101)), ledger=self.open())
        self.assertEqual(len(self.ledger.history('inline:100')), 1)

    def test_an_answer_must_follow_the_source_and_come_from_someone_else(self):
        self.github.inline(100, REVIEWER, 'Is this lock needed?', 3)
        self.github.inline(99, REVIEWER, 'Also: same question as above.', 4, parent=100)
        self.github.inline(98, AUTHOR, 'Earlier note.', 2, parent=100)
        view = self.view()
        for ident in (99, 98):
            self.rejects('later reply', view, 'inline:100', request('r1', 'lock needed', response=answered('inline', ident)))

    def test_optional_discussion_and_changed_sources(self):
        source = self.github.comment(700, REVIEWER, 'Nice cleanup, thanks!', 1)
        self.github.comment(701, BOT, 'Coverage report: 91% (+0.2%).', 2)
        view = self.view()
        self.rejects('author', view, 'comment:700', request('r1', 'Nice cleanup', 'none', 'rejected',
                                                            no_reply('no_author_reply_needed')))
        self.assess(view, 'comment:700', request('r1', 'Nice cleanup', 'none',
                                                 response=no_reply('no_author_reply_needed', 'Praise; no request.')))
        self.assertEqual(feedback.stage(self.ledger, view, NONE)['unassessed'], ['comment:701'])
        self.assess(view, 'comment:701', request('r1', 'Coverage report', 'none',
                                                 response=no_reply('no_author_reply_needed', 'Informational bot report.')))
        result = feedback.stage(self.ledger, view, NONE)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([u['source'] for u in result['unanswered']], ['comment:700', 'comment:701'])

        source['body'] = 'Nice cleanup, thanks! Could you also add a changelog entry?'
        view = self.view()
        result = feedback.stage(self.ledger, view, NONE)
        self.assertEqual((result['status'], result['sources']['comment:700']['reason']), ('incomplete', 'changed'))
        stale = {'source': 'comment:700', 'fingerprint': self.ledger.history('comment:700')[0]['fingerprint'],
                 'requests': [request('r1', 'Nice cleanup', 'none', response=no_reply('no_author_reply_needed'))]}
        with self.assertRaisesRegex(feedback.FeedbackError, 'source changed'):
            self.ledger.assess(view, stale)
        self.assess(view, 'comment:700', request('r1', 'add a changelog entry', implementation='fixed'))
        self.assertEqual(feedback.stage(self.ledger, view, REPLY_ONLY)['replies_needed'], ['comment:700'])

    def test_responders_and_empty_review_bodies_are_not_sources(self):
        self.github.comment(800, AUTHOR, 'Rebased onto main.', 1)
        self.github.review(801, REVIEWER, '', 2)
        self.github.ev['viewer'] = {'id': 9, 'login': 'automation'}
        self.github.comment(802, {'id': 9, 'login': 'automation', 'type': 'User'}, 'Published fixes.', 3)
        view = self.view()
        self.assertEqual(view.sources, {})
        self.assertEqual(feedback.stage(self.ledger, view, NONE)['status'], 'complete')
        self.assertEqual(view.responders, {1, 9})


class ReviewRegressions(Feedback):
    """Review cycle 1 of PR #45."""

    def test_a_link_to_another_comment_with_a_longer_id_does_not_answer(self):
        self.github.comment(12, REVIEWER, 'Why does this leak the handle?', 1)
        self.github.comment(123, BOB, 'Unrelated question', 2)
        self.github.review(45, REVIEWER, 'Please split this method.', 3)
        self.github.comment(500, AUTHOR, f'Re {URL}#issuecomment-123 and {URL}#pullrequestreview-456: done.', 4)
        self.github.comment(501, AUTHOR, f'Reply to [comment]({URL}#issuecomment-12): fixed.', 5)
        view = self.view()
        self.assess(view, 'comment:12', request('q', 'leak the handle'))
        for key, excerpt in (('comment:12', 'leak the handle'), ('review:45', 'split')):
            self.rejects('later reply', view, key, request('q', excerpt, response=answered('comment', 500)))
        with self.assertRaisesRegex(feedback.FeedbackError, 'own location'):
            self.ledger.record_reply(view, 'comment:12', {'kind': 'comment', 'id': 500}, 'intent:x')
        self.assertEqual(self.ledger.record_reply(view, 'comment:12', {'kind': 'comment', 'id': 501}, 'intent:1'),
                         'comment:501')

    def test_a_question_for_another_participant_cannot_be_reassessed_away(self):
        self.github.inline(100, REVIEWER, '@bob did you benchmark this?', 1)
        view = self.view()
        self.assess(view, 'inline:100', request('q', '@bob did you benchmark this?', 'participant', participant='bob',
                                                response=no_reply('addressed_to_participant')))
        for ident in ('q', 'other'):
            self.rejects('drops the question for bob', view, 'inline:100',
                         request(ident, '@bob did you benchmark this?', 'none',
                                 response=no_reply('no_author_reply_needed')))
        self.assertEqual(feedback.resolution(self.ledger, view, 'T100', BOTH)['blockers'], ['inline:100 awaits bob'])
        # Upgrading to a required answer is allowed.
        self.assess(view, 'inline:100', request('q', '@bob did you benchmark this?', 'participant', participant='bob',
                                                instructed=True))

    def test_an_edit_keeps_obligations_whose_text_remains(self):
        source = self.github.comment(10, REVIEWER, 'Please fix the leak.', 1)
        self.assess(self.view(), 'comment:10', request('q', 'Please fix the leak.', implementation='rejected'))
        source['body'] = 'Please fix the leak. (typo)'
        view = self.view()
        for ident in ('q', 'other'):
            self.rejects('drops the response obligation of q', view, 'comment:10',
                         request(ident, 'Please fix the leak.', 'none', response=no_reply('no_author_reply_needed')))
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['status'], 'incomplete')
        self.assess(view, 'comment:10', request('q', 'Please fix the leak.', implementation='rejected'))
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['replies_needed'], ['comment:10'])
        # An edit that removes the quoted request releases it.
        source['body'] = 'Never mind, the leak was elsewhere.'
        view = self.view()
        self.assess(view, 'comment:10', request('n', 'Never mind', 'none', response=no_reply('no_author_reply_needed')))
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['status'], 'complete')

    def test_a_verified_reply_settles_a_request_whose_cited_answer_was_deleted(self):
        self.github.inline(100, REVIEWER, 'Is this thread-safe?', 1)
        answer = self.github.inline(101, BOB, 'Yes, guarded by the lock.', 2, parent=100)
        view = self.view()
        self.assess(view, 'inline:100', request('q', 'thread-safe', response=answered('inline', 101)))
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['unassessed'], ['inline:101'])
        self.assertEqual(feedback.stage(self.ledger, view, BOTH)['sources']['inline:100']['status'], 'settled')
        self.github.ev['inline'].remove(answer)
        self.assertEqual(feedback.stage(self.ledger, self.view(), BOTH)['replies_needed'], ['inline:100'])
        self.github.inline(102, AUTHOR, 'Yes, see the lock in `put`.', 3, parent=100)
        view = self.view()
        self.ledger.record_reply(view, 'inline:100', {'kind': 'inline', 'id': 102}, 'intent:1')
        result = feedback.stage(self.ledger, self.view(), BOTH)
        self.assertEqual((result['status'], result['replies_needed']), ('complete', []))


class Evidence(Feedback):
    def test_incomplete_or_foreign_evidence_rejects(self):
        cases = [lambda ev: ev.pop('threads'), lambda ev: ev['metadata'].update(number=8),
                 lambda ev: ev['inline'].append({'id': 5, 'user': REVIEWER, 'body': 'x', 'in_reply_to_id': 4}),
                 lambda ev: ev['comments'].extend([{'id': 5, 'user': REVIEWER}] * 2)]
        for mutate in cases:
            github = GitHub()
            mutate(github.ev)
            with self.assertRaises(feedback.FeedbackError):
                feedback.observe(github, PR)
            self.assertEqual(github.calls, Counter(capture=1))

    def test_ledger_binding_and_schema(self):
        self.assertEqual(self.path.parent, self.state / 'runs')
        self.assertNotIn('run-1', self.path.name)
        with self.assertRaisesRegex(feedback.FeedbackError, 'another logical run'):
            feedback.Ledger(self.path, logical_run='run-2', pr=PR)
        with self.assertRaisesRegex(feedback.FeedbackError, 'another logical run'):
            feedback.Ledger(self.path, logical_run='run-1', pr={'url': URL.replace('7', '8'), 'number': 8})
        foreign = self.state / 'foreign.sqlite'
        sqlite3.connect(foreign).execute('CREATE TABLE claims (id TEXT)').connection.close()
        with self.assertRaises(feedback.FeedbackError):
            feedback.Ledger(foreign, logical_run='run-1', pr=PR)
        newer = self.state / 'newer.sqlite'
        feedback.Ledger(newer, logical_run='run-1', pr=PR).close()
        connection = sqlite3.connect(newer)
        connection.execute('PRAGMA user_version = 2')
        connection.close()
        with self.assertRaisesRegex(feedback.FeedbackError, 'unsupported feedback ledger version'):
            feedback.Ledger(newer, logical_run='run-1', pr=PR)
        # The installed copy binds the same suite as admission.
        self.assertEqual(feedback.SUITE, admission.SUITE)

    def test_read_only_open_never_creates_or_changes_the_record(self):
        missing = self.state / 'runs' / 'missing.feedback.sqlite'
        with self.assertRaisesRegex(feedback.FeedbackError, 'unavailable'):
            feedback.Ledger(missing, logical_run='run-1', pr=PR, read_only=True)
        self.assertFalse(missing.exists())
        reader = feedback.Ledger(self.path, logical_run='run-1', pr=PR, read_only=True)
        self.addCleanup(reader.close)
        with self.assertRaises(sqlite3.OperationalError):
            reader.db.execute("INSERT INTO meta VALUES ('x', 'y')")
        with self.assertRaisesRegex(feedback.FeedbackError, 'another logical run'):
            feedback.Ledger(self.path, logical_run='run-2', pr=PR, read_only=True)
        view = self.view()
        self.assertEqual(feedback.stage(feedback.Unrecorded(), view, BOTH)['status'], 'complete')

    def test_unconfigured_operations_fail_without_live_fallback(self):
        with self.assertRaisesRegex(AssertionError, 'unconfigured fixture operation: request'):
            self.github.request('POST', 'repos/owner/repo/issues/7/comments', {'body': 'x'})


if __name__ == '__main__':
    unittest.main()
