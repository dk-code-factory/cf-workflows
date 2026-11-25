"""Author-aware routing for cf-workflows:dk-pr-iter (#24).

A fail-closed fake GitHub transport answers only the PR and authenticated-account
reads; any other operation raises with no live fallback. Routed requests are
then admitted by the real dk-pr-fix and dk-pr-review preflights against a
disposable repository and a fail-closed fake engine transport.
"""
from collections import Counter
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, dk_pr_fix, dk_pr_review, routing  # noqa: E402
from test_dk_pr_fix import CAPABILITIES, FakeTransport, PreflightFixture  # noqa: E402

PR = 'https://github.com/Owner/Repo/pull/7'
GHE = 'https://ghe.example.com/team/service/pull/3'
MERGE = {'request_id': 'merge-1', 'instruction': 'Check out this PR, iterate, then merge it.', 'source': 'chat:1'}


class GitHub:
    """Answers pull_request and viewer from fixture identities; everything else fails closed."""

    def __init__(self, author, viewer, url=PR, number=7):
        self.pull = {'html_url': url, 'number': number, 'user': author}
        self.user = viewer
        self.calls = Counter()
        self.hosts = []

    def pull_request(self, pr):
        self.calls['pull_request'] += 1
        if isinstance(self.pull, Exception):
            raise self.pull
        return copy.deepcopy(self.pull)

    def viewer(self, host):
        self.calls['viewer'] += 1
        self.hosts.append(host)
        if isinstance(self.user, Exception):
            raise self.user
        return copy.deepcopy(self.user)

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)

        def unconfigured(*args, **kwargs):
            self.calls[name] += 1
            raise AssertionError('unconfigured fixture operation: ' + name)
        return unconfigured


def request(pr=PR, **extra):
    return {'pr': pr, 'instructions': '  Iterate on this PR exactly as discussed.\n', **extra}


class Routing(unittest.TestCase):
    def route(self, github, **arguments):
        result = routing.route(transport=github, **{'request': request(), **arguments})
        self.assertLessEqual(set(github.calls), {'pull_request', 'viewer'})
        self.assertTrue(all(count <= 1 for count in github.calls.values()), github.calls)
        return result

    def test_own_pr_routes_to_fixing_and_another_authors_to_review(self):
        mine = self.route(GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}))
        self.assertEqual((mine['status'], mine['route'], mine['skill'], mine['ownership'], mine['basis']),
                         ('routed', 'fix', 'cf-workflows:dk-pr-fix', 'mine', 'identity'))
        self.assertEqual(mine['identity'], {'matched': 'actor', 'compared': 'numeric_id'})
        others = self.route(GitHub({'id': 12, 'login': 'them'}, {'id': 11, 'login': 'me'}))
        self.assertEqual((others['route'], others['skill'], others['ownership']),
                         ('review', 'cf-workflows:dk-pr-review', 'others'))
        self.assertIsNone(others['maintain_other'])
        self.assertIsNone(others['invocation'])

    def test_multi_account_hosts_match_by_host_and_numeric_id(self):
        accounts = [{'host': 'github.com', 'id': 42}, {'host': 'ghe.example.com', 'id': 7}]
        actor = {'id': 11, 'login': 'me-work'}
        cases = ((PR, 42, 'fix', 'account'), (PR, 7, 'review', None),
                 (GHE, 7, 'fix', 'account'), (GHE, 42, 'review', None))
        for url, author, route, matched in cases:
            with self.subTest(url=url, author=author):
                number = int(url.rsplit('/', 1)[1])
                github = GitHub({'id': author, 'login': 'someone'}, actor, url=url, number=number)
                result = self.route(github, request=request(url), accounts=accounts)
                self.assertEqual((result['route'], result['identity']['matched']), (route, matched))
                self.assertEqual(github.hosts, [url.split('/')[2]])

    def test_renamed_and_reused_logins_never_decide_ownership(self):
        # The author renamed their login: the immutable ID still makes the PR theirs.
        renamed = self.route(GitHub({'id': 11, 'login': 'new-name'}, {'id': 11, 'login': 'old-name'}))
        self.assertEqual(renamed['route'], 'fix')
        # Another account now holds the actor's old login: same login, different ID.
        reused = self.route(GitHub({'id': 99, 'login': 'me'}, {'id': 11, 'login': 'me'}))
        self.assertEqual((reused['route'], reused['ownership']), ('review', 'others'))
        # Configured personal accounts are IDs; a login entry is invalid and routes nothing.
        github = GitHub({'id': 42, 'login': 'me-bot'}, {'id': 11, 'login': 'me'})
        blocked = routing.route(request=request(), accounts=['me-bot'], transport=github)
        self.assertEqual((blocked['status'], blocked['route']), ('blocked', None))
        self.assertEqual(github.calls, Counter(), 'invalid configuration blocks before any read')

    def test_missing_or_inconsistent_identity_never_guesses_a_route(self):
        actor = {'id': 11, 'login': 'me'}
        cases = {
            'author without id': GitHub({'login': 'them'}, actor),
            'author id as text': GitHub({'id': '12', 'login': 'them'}, actor),
            'author id zero': GitHub({'id': 0, 'login': 'them'}, actor),
            'ghost author': GitHub(None, actor),
            'actor without id': GitHub({'id': 12, 'login': 'them'}, {'login': 'me'}),
            'actor boolean id': GitHub({'id': 12, 'login': 'them'}, {'id': True, 'login': 'me'}),
            'pull read fails': GitHub(None, actor),
            'viewer read fails': GitHub({'id': 12, 'login': 'them'}, PermissionError('token lacks user scope')),
            'another PR': GitHub({'id': 11, 'login': 'me'}, actor, url='https://github.com/owner/repo/pull/8',
                                 number=8),
            'another repository': GitHub({'id': 11, 'login': 'me'}, actor, url='https://github.com/owner/fork/pull/7'),
        }
        cases['pull read fails'].pull = TimeoutError('network')
        for name, github in cases.items():
            with self.subTest(name):
                result = self.route(github)
                self.assertEqual((result['status'], result['route'], result['skill']), ('blocked', None, None))
                self.assertTrue(result['reason'])
        # A malformed PR URL reads nothing.
        github = GitHub({'id': 11, 'login': 'me'}, actor)
        result = routing.route(request=request('https://github.com/owner/repo/issues/7'), transport=github)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(github.calls, Counter())

    def test_routing_preserves_the_exact_request_and_grants_nothing(self):
        original = request(logical_run='iteration-7', authorizations={'merge': MERGE})
        result = self.route(GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}), request=original)
        self.assertEqual(result['request'], original)
        self.assertEqual(result['withheld'], [])
        self.assertIsNone(result['invocation'])
        review = self.route(GitHub({'id': 12, 'login': 'them'}, {'id': 11, 'login': 'me'}), request=original)
        self.assertEqual(review['request']['authorizations'], {'merge': MERGE})
        self.assertEqual(review['request']['instructions'], original['instructions'])

    def test_analysis_only_requests_stay_read_only_on_any_route(self):
        original = request(authorizations={'merge': MERGE})
        for author in (11, 12):
            with self.subTest(author=author):
                result = self.route(GitHub({'id': author, 'login': 'x'}, {'id': 11, 'login': 'me'}),
                                    request=original, analysis_only=True)
                self.assertEqual((result['route'], result['basis']), ('review', 'analysis_only'))
                self.assertNotIn('authorizations', result['request'])
                self.assertEqual(result['withheld'], ['merge'])
                self.assertEqual(result['invocation']['options']['prohibitions'], list(admission.ACTIONS))
                self.assertEqual(result['request']['instructions'], original['instructions'])
        explicit = {'route': 'fix', 'request_id': 'r2', 'instruction': 'Fix it.', 'source': 'chat:2'}
        blocked = self.route(GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}),
                             explicit=explicit, analysis_only=True)
        self.assertEqual(blocked['status'], 'blocked')

    def test_explicit_requests_override_selection_without_new_authority(self):
        review = {'route': 'review', 'request_id': 'r1', 'instruction': 'Just review my PR.', 'source': 'chat:1'}
        own = self.route(GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}), explicit=review)
        self.assertEqual((own['route'], own['ownership'], own['basis']), ('review', 'mine', 'explicit'))
        self.assertEqual(own['invocation']['options']['prohibitions'], ['approve_pr'])
        approval = {'request_id': 'a1', 'instruction': 'Approve it.', 'source': 'chat:1'}
        refused = self.route(GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}), explicit=review,
                             request=request(authorizations={'approve_pr': approval}))
        self.assertEqual(refused['status'], 'blocked')
        fix = {'route': 'fix', 'request_id': 'r3', 'instruction': 'Maintain their PR.', 'source': 'chat:3'}
        other = self.route(GitHub({'id': 12, 'login': 'them'}, {'id': 11, 'login': 'me'}), explicit=fix)
        self.assertEqual((other['route'], other['ownership']), ('fix', 'others'))
        self.assertEqual(other['maintain_other'], {'request_id': 'r3', 'instruction': 'Maintain their PR.',
                                                   'source': 'chat:3'})
        self.assertNotIn('authorizations', other['request'])
        for bad in ({**fix, 'route': 'merge'}, {'route': 'fix'}, {**fix, 'source': ' '}):
            with self.subTest(bad=bad):
                self.assertEqual(self.route(GitHub({'id': 12, 'login': 'them'}, {'id': 11, 'login': 'me'}),
                                            explicit=bad)['status'], 'blocked')

    def test_invocation_scope_keeps_routed_prohibitions(self):
        routed = {'invocation': {'options': {'prohibitions': ['push', 'merge']}}}
        merged = routing.invocation(routed, {'options': {'prohibitions': ['push'], 'reviewers': ['review']},
                                             'permissions': {'merge': True}})
        self.assertEqual(merged['options']['prohibitions'], ['push', 'merge'])
        self.assertEqual(merged['options']['reviewers'], ['review'])
        self.assertEqual(merged['permissions'], {'merge': True})
        self.assertIsNone(routing.invocation({'invocation': None}))


class RoutedPreflight(PreflightFixture):
    def admit(self, entry, routed, invocation=None, transport=None, **extra):
        self.transport = transport or FakeTransport()
        return entry(request=routed['request'], configuration=None,
                     invocation=routing.invocation(routed, invocation), deployment=self.deployment,
                     repository=self.repo, transport=self.transport, home=self.home, install=self.clean, **extra)

    def test_routed_requests_admit_on_their_entry_points(self):
        mine = routing.route(request=request(), transport=GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}))
        result = self.admit(dk_pr_fix.preflight, mine)
        self.assertEqual((result['preflight'], result['workflow']), ('admitted', 'cf-workflows:dk-pr-fix'))
        self.assertEqual(result['state']['request_document']['instructions'], request()['instructions'])
        self.assertNotIn('maintain_other', result['state']['request_document'])

        theirs = routing.route(request=request(), transport=GitHub({'id': 12, 'login': 'x'}, {'id': 11, 'login': 'me'}))
        result = self.admit(dk_pr_review.preflight, theirs)
        self.assertEqual((result['preflight'], result['workflow']), ('admitted', 'cf-workflows:dk-pr-review'))

        explicit = {'route': 'fix', 'request_id': 'r3', 'instruction': 'Maintain their PR.', 'source': 'chat:3'}
        maintain = routing.route(request=request(), explicit=explicit,
                                 transport=GitHub({'id': 12, 'login': 'x'}, {'id': 11, 'login': 'me'}))
        result = self.admit(dk_pr_fix.preflight, maintain, maintain_other=maintain['maintain_other'])
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['state']['request_document']['maintain_other'], maintain['maintain_other'])
        self.assertFalse(any(result['inputs']['permissions'].values()))
        self.assertFalse(self.home.exists())

    def test_analysis_only_routing_disables_every_enabled_write(self):
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        routed = routing.route(request=request(authorizations={'merge': MERGE}), analysis_only=True,
                               transport=GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}))
        result = self.admit(dk_pr_review.preflight, routed, invocation={'permissions': {'merge': True}},
                            transport=FakeTransport(offered))
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertFalse(any(value['effective'] for value in result['snapshot']['permissions'].values()))
        self.assertTrue(result['snapshot']['permissions']['merge']['prohibited'])
        self.assertFalse(any(result['inputs']['permissions'].values()))
        self.assertEqual(result['state']['request_document']['authorizations'], {})
        # Restoring the withheld grant contradicts the routed prohibition and rejects.
        result = self.admit(dk_pr_review.preflight, {**routed, 'request': request(authorizations={'merge': MERGE})},
                            invocation={'permissions': {'merge': True}}, transport=FakeTransport(offered))
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'request.authorizations.merge'))

    def test_maintain_other_needs_an_exact_request_on_the_fix_entry(self):
        for value in ({'request_id': 'r'}, {'request_id': 'r', 'instruction': ' ', 'source': 's'}):
            with self.subTest(value=value):
                self.transport = FakeTransport()
                result = dk_pr_fix.preflight(request=request(), configuration=None, invocation=None,
                                             deployment=self.deployment, repository=self.repo,
                                             transport=self.transport, home=self.home, install=self.clean,
                                             maintain_other=value)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'maintain_other'))
                self.assertEqual(self.transport.calls, Counter())


if __name__ == '__main__':
    unittest.main()
