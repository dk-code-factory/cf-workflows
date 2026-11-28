"""Author-aware routing for cf-workflows:dk-pr-iter (#24).

A fail-closed fake GitHub transport answers only the PR and authenticated-account
reads; any other operation raises with no live fallback. Routed requests are
then admitted by the real dk-pr-fix and dk-pr-review preflights against a
disposable repository and a fail-closed fake engine transport. The installed
entry (workflows/iter.yml) is checked as YAML, and its iter_route helper runs
through the action-v1 entry point on the staged request in a fresh state
namespace. Nothing here starts an engine, provider or host.
"""
from collections import Counter
import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

import dk_pr_fix as _helpers  # noqa: F401  (puts the installed helpers on sys.path)
import git_fixture

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

    def test_repository_targeted_request_is_blocked_without_reads(self):
        github = GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'})
        result = routing.route(request={'repository': 'github.com/owner/repo', 'instructions': 'Iterate.'},
                               transport=github)
        self.assertEqual((result['status'], result['route'], result['skill']), ('blocked', None, None))
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
        for bad in ({**fix, 'route': 'merge'}, {'route': 'fix'}, {**fix, 'source': ' '},
                    {**fix, 'route': ['fix']}, {**fix, 'route': {'fix': 1}}):
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
        for malformed in (['options'], {'options': ['prohibitions']}, {'options': {'prohibitions': 'push'}},
                          {'options': {'prohibitions': {'push': True}}}):
            with self.subTest(malformed=malformed):
                self.assertEqual(routing.invocation(routed, malformed), malformed)


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

    def test_a_malformed_invocation_is_rejected_not_coerced_by_routing(self):
        routed = routing.route(request=request(), analysis_only=True,
                               transport=GitHub({'id': 11, 'login': 'me'}, {'id': 11, 'login': 'me'}))
        result = self.admit(dk_pr_review.preflight, routed, invocation={'options': {'prohibitions': 'push'}})
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'invocation.options.prohibitions'))

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


ME, THEM = {'id': 11, 'login': 'me'}, {'id': 12, 'login': 'them'}


def iter_workflow():
    return yaml.safe_load((dk_pr_fix.INSTALL / routing.ENTRY).read_text())


class InstalledIter(PreflightFixture):
    """routing.preflight admits the routed request for workflows/iter.yml; iter_route checks it again."""

    def setUp(self):
        super().setUp()
        git_fixture.isolate(self)
        self.keys = Counter()

    def iterate(self, github, transport=None, **changes):
        self.transport = transport or FakeTransport()
        arguments = {'request': request(), 'deployment': self.deployment, 'repository': self.repo,
                     'home': self.home, 'install': self.clean}
        arguments.update(changes)
        staged = self.home.exists()
        result = routing.preflight(github=github, transport=self.transport, **copy.deepcopy(arguments))
        self.assertLessEqual(set(github.calls), {'pull_request', 'viewer'})
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertEqual(self.home.exists(), staged, 'preflight writes nothing')
        return result

    def stage(self, result):
        """Stage the admitted request as the host does, and mount its namespace for the helpers."""
        dk_pr_fix.stage_request(result)
        context = patch.dict(os.environ, {'CF_WORKFLOWS_STATE': result['state']['namespace']})
        context.start()
        self.addCleanup(context.stop)
        return result['inputs']['request']

    def check(self, github, inputs, key=None, phase='execute', **changes):
        import action
        payload = {name: inputs[name] for name in ('request', 'route', 'identity', 'accounts', 'permissions')}
        payload.update(changes)
        self.keys['iter_route'] += 1
        request = {'protocol': 'action-v1', 'phase': phase, 'input': {'operation': 'iter_route', 'payload': payload},
                   'context': {'operation_key': key or 'route:%d' % self.keys['iter_route'],
                               'execution_fingerprint': 'exec-1'}}
        return action.handle('invoke', request, self.repo, github)

    def test_entry_is_listed_and_runs_exactly_one_routed_entry(self):
        descriptor = (dk_pr_fix.INSTALL / 'plugin.yaml').read_text()
        self.assertIn('  - name: dk-pr-iter\n    path: skills/dk-pr-iter/SKILL.md\n    workflow: workflows/iter.yml\n',
                      descriptor)
        skill = (dk_pr_fix.INSTALL / 'skills/dk-pr-iter/SKILL.md').read_text()
        self.assertTrue(skill.startswith('---\nname: dk-pr-iter\ndescription: '))
        self.assertIn('This plugin version is not runnable.', skill)
        definition = iter_workflow()
        self.assertEqual(definition['name'], routing.WORKFLOW)
        declared = definition['on']['workflow_call']['inputs']
        jobs = definition['jobs']
        self.assertEqual(list(jobs), ['route', 'fix', 'review'])
        self.assertEqual((jobs['route']['uses'], jobs['route']['with']['operation']),
                         ('workflows/helper.yml', 'iter_route'))
        self.assertNotIn('needs', jobs['route'])
        self.assertEqual(set(jobs['route']['with']['payload']),
                         {'request', 'route', 'identity', 'accounts', 'permissions'})
        for name, uses in (('fix', 'workflows/main.yml'), ('review', dk_pr_review.ENTRY)):
            with self.subTest(job=name):
                job = jobs[name]
                self.assertEqual((job['uses'], job['needs']), (uses, 'route'))
                self.assertEqual(job['if'], "${{ needs.route.outputs.result.route == '%s' }}" % name)
                target = yaml.safe_load((dk_pr_fix.INSTALL / uses).read_text())['on']['workflow_call']['inputs']
                passed = set(declared) - {'route', 'identity'}
                self.assertLessEqual(passed, set(target))
                for key in passed:
                    self.assertEqual(job['with'][key], '${{ inputs.%s }}' % key)
                    self.assertEqual(declared[key]['type'], target[key]['type'], key)
        self.assertEqual(jobs['fix']['with']['mode'], 'fix')
        self.assertNotIn('mode', jobs['review']['with'])
        self.assertNotIn('entry', jobs['fix']['with'])
        self.assertFalse(any(declared['permissions']['default'].values()))
        for key in ('request', 'route', 'identity', 'tasks', 'finalizers', 'reviewers', 'adapters'):
            self.assertTrue(declared[key].get('required'), key)

    def test_preflight_readmits_the_routed_request_on_the_selected_entry(self):
        declared = iter_workflow()['on']['workflow_call']['inputs']
        original = request(logical_run='iteration-7', authorizations={'merge': MERGE})
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        cases = ((ME, 'fix', dk_pr_fix.preflight), (THEM, 'review', dk_pr_review.preflight))
        for author, route, entry in cases:
            with self.subTest(route=route):
                result = self.iterate(GitHub(author, ME), request=original, transport=FakeTransport(offered),
                                      invocation={'permissions': {'merge': True}})
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                self.assertEqual((result['workflow'], result['entry_workflow'], result['installation']['entry']),
                                 (routing.WORKFLOW, routing.ROUTES[route], routing.ENTRY))
                inputs = result['inputs']
                self.assertLessEqual(set(inputs), set(declared))
                self.assertEqual((inputs['route'], inputs['identity']), (route, {'author': author['id'], 'actor': 11}))
                # The same admission the selected entry gives the routed request directly.
                self.transport = FakeTransport(offered)
                direct = entry(request=original, configuration=None, invocation={'permissions': {'merge': True}},
                               deployment=self.deployment, repository=self.repo, transport=self.transport,
                               home=self.home, install=self.clean)
                self.assertEqual({k: v for k, v in inputs.items() if k not in ('route', 'identity')}, direct['inputs'])
                self.assertEqual(result['state'], direct['state'])
                document = result['state']['request_document']
                self.assertEqual((document['instructions'], document['logical_run'], document['authorizations']),
                                 (original['instructions'], 'iteration-7', {'merge': MERGE}))

    def test_routed_prohibitions_and_maintain_other_reach_the_staged_request(self):
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        result = self.iterate(GitHub(ME, ME), transport=FakeTransport(offered), analysis_only=True,
                              request=request(authorizations={'merge': MERGE}),
                              invocation={'permissions': {'merge': True}})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['inputs']['route'], result['routing']['withheld']), ('review', ['merge']))
        prohibitions = [item['text'] for item in result['snapshot']['options']['prohibitions']['value']]
        self.assertEqual(prohibitions, list(admission.ACTIONS))
        self.assertFalse(any(result['inputs']['permissions'].values()))
        self.assertEqual(result['state']['request_document']['authorizations'], {})
        self.assertEqual(result['state']['request_document']['instructions'], request()['instructions'])
        explicit = {'route': 'fix', 'request_id': 'r3', 'instruction': 'Maintain their PR.', 'source': 'chat:3'}
        result = self.iterate(GitHub(THEM, ME), explicit=explicit)
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['inputs']['route'], 'fix')
        self.assertEqual(result['state']['request_document']['maintain_other'],
                         {key: explicit[key] for key in ('request_id', 'instruction', 'source')})
        own = {'route': 'review', 'request_id': 'r1', 'instruction': 'Just review my PR.', 'source': 'chat:1'}
        result = self.iterate(GitHub(ME, ME), explicit=own)
        self.assertEqual((result['preflight'], result['inputs']['route']), ('admitted', 'review'))
        self.assertIn('approve_pr', [item['text'] for item in result['snapshot']['options']['prohibitions']['value']])

    def test_blocked_routing_or_other_accounts_admit_nothing(self):
        github = GitHub({'login': 'them'}, ME)
        result = self.iterate(github)
        self.assertEqual((result['preflight'], result['error']['path']), ('blocked', 'routing'))
        self.assertNotIn('inputs', result)
        self.assertEqual(self.transport.calls, Counter(), 'no admission without a route')
        # Routing with a personal account the admitted configuration does not carry.
        account = [{'host': 'github.com', 'id': 12}]
        result = self.iterate(GitHub(THEM, ME), accounts=account)
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'accounts'))
        self.assertNotIn('state', result)
        result = self.iterate(GitHub(THEM, ME), accounts=account, invocation={'options': {'accounts': account}})
        self.assertEqual((result['preflight'], result['inputs']['route']), ('admitted', 'fix'), result.get('error'))
        self.assertEqual(result['inputs']['accounts'], account)

    def test_installed_route_check_confirms_identities_before_either_entry(self):
        result = self.iterate(GitHub(THEM, ME))
        inputs = result['inputs']
        self.stage(result)
        github = GitHub(THEM, ME)
        output = self.check(github, inputs)['result']['output']
        self.assertEqual(output, {'route': 'review', 'ownership': 'others', 'identity': {'author': 12, 'actor': 11}})
        self.assertEqual(github.calls, Counter({'pull_request': 1, 'viewer': 1}))
        # A retained result replays without reading again.
        again = GitHub(THEM, ME)
        self.assertEqual(self.check(again, inputs, key='route:1')['result']['output'], output)
        self.assertEqual(again.calls, Counter())
        # The staged request is the exact routed request, read from the namespace.
        import action
        call = {'protocol': 'action-v1', 'phase': 'execute',
                'input': {'operation': 'snapshot_request', 'payload': {'request': inputs['request']}},
                'context': {'operation_key': 'snapshot', 'execution_fingerprint': 'exec-1'}}
        snapshot = action.handle('invoke', call, self.repo, None)['result']['output']['snapshot']
        from prfix.core import read_artifact
        self.assertEqual(read_artifact(self.repo, snapshot), result['state']['request_document'])
        # Over the installed gh transport the check issues exactly two GET reads.
        from prfix.iteration import GitHubIdentity

        class Transport:
            host, routes = None, []

            def request(self, method, path, payload=None):
                self.routes.append((method, self.host, path))
                return {'repos/owner/repo/pulls/7': {'html_url': PR, 'number': 7, 'user': THEM},
                        'user': ME}[path]
        transport = Transport()
        self.assertEqual(self.check(GitHubIdentity(transport), inputs, key='route:gh')['result']['output'], output)
        self.assertEqual(transport.routes, [('GET', 'github.com', 'repos/owner/repo/pulls/7'),
                                            ('GET', 'github.com', 'user')])

    def test_installed_route_check_rejects_changed_identity_or_route(self):
        own = self.iterate(GitHub(ME, ME))
        self.stage(own)
        cases = {
            'another token': (GitHub(ME, {'id': 13, 'login': 'me'}), {}, 'differs from the identities'),
            'renamed login keeps the route': (GitHub({'id': 11, 'login': 'renamed'}, ME), {}, None),
            'viewer read fails': (GitHub(ME, PermissionError('scope')), {}, 'unavailable'),
            'another PR': (GitHub(ME, ME, url='https://github.com/owner/repo/pull/8', number=8), {}, 'another PR'),
            'author without ID': (GitHub({'login': 'me'}, ME), {}, 'no immutable numeric ID'),
            'review of own PR with approval': (GitHub(ME, ME), {'route': 'review',
                                                                'permissions': {'approve_pr': True}}, 'never approves'),
            'unknown route': (GitHub(ME, ME), {'route': 'merge'}, 'fix or review only'),
            'login as identity': (GitHub(ME, ME), {'identity': {'author': 'me', 'actor': 11}}, 'positive integer'),
            'other accounts': (GitHub(ME, ME), {'accounts': ['me']}, '{host, id}'),
        }
        for name, (github, changes, message) in cases.items():
            with self.subTest(name):
                if message is None:
                    self.assertEqual(self.check(github, own['inputs'], **changes)['result']['output']['route'], 'fix')
                    continue
                with self.assertRaisesRegex(ValueError, message.replace('{', '\\{').replace('}', '\\}')):
                    self.check(github, own['inputs'], **changes)
        theirs = self.iterate(GitHub(THEM, ME))
        self.stage(theirs)
        with self.assertRaisesRegex(ValueError, 'maintain_other'):
            self.check(GitHub(THEM, ME), theirs['inputs'], route='fix')
        explicit = {'route': 'fix', 'request_id': 'r3', 'instruction': 'Maintain their PR.', 'source': 'chat:3'}
        maintain = self.iterate(GitHub(THEM, ME), explicit=explicit)
        self.stage(maintain)
        self.assertEqual(self.check(GitHub(THEM, ME), maintain['inputs'])['result']['output']['route'], 'fix')
        with self.assertRaisesRegex(ValueError, 'never maintains'):
            self.check(GitHub(THEM, ME), maintain['inputs'], route='review')


if __name__ == '__main__':
    unittest.main()
