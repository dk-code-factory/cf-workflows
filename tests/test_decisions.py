"""cf-workflows:dk-pr-decisions: sourced decisions, reconciliation and section publication (#28).

A disposable repository and state directory, a disposable decision ledger, a
fail-closed fake engine with durable gates and claim-reconciled native sessions,
and the fail-closed fake GitHub transport of the shared publication tests. Every
unscripted session and unconfigured operation fails with no live fallback. These
results do not certify any engine, provider, host or GitHub conformance.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True  # Never add caches to the installed closure.
import dk_pr_fix as _helpers  # noqa: E402,F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture  # noqa: E402
from prfix.core import Ledger as RunLedger  # noqa: E402
from prfix.publication import Publication  # noqa: E402
from cf_workflows import admission, decisions, dk_pr_fix  # noqa: E402
from test_configuration_skills import Engine, Transport, session  # noqa: E402
from test_dk_pr_fix_publication import Transport as GitHub  # noqa: E402
from test_semantic import verdict  # noqa: E402

PR = 'https://github.com/o/r/pull/7'
INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}
ALICE = {'id': 21, 'login': 'alice'}  # The requesting user's immutable account.
BOB = {'id': 31, 'login': 'bob'}
CAROL = {'id': 41, 'login': 'carol'}
BOT = {'id': 51, 'login': 'ci[bot]', 'type': 'Bot'}
GRANT = {'request_id': 'user-1', 'instruction': 'Publish the settled decisions.', 'source': 'chat:1'}


def comment(ident, user, body, day, association='NONE'):
    return {'id': ident, 'user': dict(user), 'body': body, 'author_association': association,
            'created_at': f'2026-01-{day:02d}T00:00:00Z', 'html_url': f'{PR}#issuecomment-{ident}'}


def decision(ident, sources, status='settled', text=None, **extra):
    return {'id': ident, 'subject': ident.replace('-', ' '), 'text': text or f'Decision {ident}.', 'status': status,
            'scope': 'this PR', 'sources': list(sources), **extra}


def contradiction(ident, involved, sources, resolution=None):
    return {'id': ident, 'subject': ident, 'decisions': list(involved), 'sources': list(sources),
            'question': 'Which direction applies?', 'resolution': resolution}


def result(*items, contradictions=(), questions=(), language=None, **extra):
    return {'summary': 'Reconciled from the cited sources.', 'decisions': list(items),
            'contradictions': list(contradictions), 'questions': list(questions),
            'language_review': language or {'passes': 1, 'changed': [False], 'ambiguities': []}, **extra}


API = decision('api-compat', ['comment:101'], text='Keep the v1 API stable.')
STORE = decision('state-store', ['comment:102'], text='Store run state in SQLite.')
REDIS = decision('redis', ['comment:103'], status='proposal', text='Consider Redis for run state.')
DROP = decision('api-drop', ['comment:104'], text='Drop v1 API compatibility.')
CONFLICT = contradiction('api-conflict', ['api-compat', 'api-drop'], ['comment:101', 'comment:104'])


class Fixture(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix='cfw decisions ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo'
        self.repo.mkdir()
        subprocess.run(git_fixture.command('init', '-q', str(self.repo)), check=True, env=git_fixture.environment())
        self.engine = Engine()
        self.github = GitHub()
        self.github.ev['metadata']['body'] = 'Adds run state storage.\n\n## Testing\n\nRan the unit tests.'
        self.github.ev['comments'] = [
            comment(101, ALICE, 'Keep the v1 API stable.', 1),
            comment(102, BOB, 'Store run state in SQLite.', 2, 'MEMBER'),
            comment(103, CAROL, 'Maybe use Redis instead?', 3),
            comment(105, BOT, 'All checks passed.', 5)]
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}
        self.ledgers = 0

    def configuration(self, permission=False, **options):
        scope = {'id': 'me', 'selector': {'kind': 'personal', 'id': 'me'},
                 'options': {'accounts': [{'host': 'github.com', 'id': ALICE['id']}], **options},
                 'permissions': {'edit_pr': True} if permission else {}, 'prompts': [], 'skills': [],
                 'preferences': []}
        return {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                'scopes': [scope], 'resources': [],
                'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                              'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}

    def transport(self, *sessions, validators=(), lose=0):
        self.fake = Transport(self.engine, sessions, validators, lose=lose)
        return self.fake

    def calls(self, **expected):
        self.assertEqual(self.fake.calls, +Counter(expected))

    def admit(self, instructions='Record the decisions of this PR.', *, authorize=False, permission=None,
              configuration=None, validators=(), run=None, **extra):
        request = {'pr': PR, 'instructions': instructions}
        if run is not None:
            request['logical_run'] = run
        if authorize:
            request['authorizations'] = {'edit_pr': GRANT}
        configuration = configuration or self.configuration(authorize if permission is None else permission)
        transport = self.transport(validators=validators)
        return decisions.preflight(request=request, configuration=configuration, deployment=self.deployment,
                                   repository=self.repo, transport=transport, **extra)

    def ledger(self, admitted):
        ledger = decisions.Ledger(decisions.ledger_path(self.base / 'state', admitted['snapshot']['request']['logical_run']),
                                  logical_run=admitted['snapshot']['request']['logical_run'], pr=PR)
        self.addCleanup(ledger.close)
        return ledger

    def reconcile(self, admitted, ledger, *sessions, lose=0):
        transport = self.transport(*sessions, lose=lose)
        return decisions.reconcile(admitted, capture=self.github.capture(None), ledger=ledger, transport=transport)

    @staticmethod
    def ids(items):
        return sorted(item['id'] for item in items)


class Preset(Fixture):
    def test_baseline_bytes_options_and_preset(self):
        digests = {item['path']: item['sha256'] for item in INVENTORY['dk-pr-decisions']['baseline_resources']}
        for host in ('claude', 'codex'):
            self.assertEqual(hashlib.sha256(decisions.BASELINE_CONFIG.encode()).hexdigest(),
                             digests[f'{host}/skills/dk-pr-decisions/config.json'])
            self.assertEqual(hashlib.sha256(decisions.ROLE_PROMPT.encode()).hexdigest(),
                             digests[f'{host}/skills/dk-pr-decisions/prompts/default.md'])
        self.assertEqual(set(decisions.OPTIONS), {'reconciliation_prompt', 'language_review_passes', 'section_heading'})
        self.assertEqual(decisions.PRESET, {'tasks': ['reconcile-decisions', {'stage': 'publication'}],
                                            'finalizers': [], 'reviewers': []})
        self.assertEqual(admission.BASELINE_ADAPTERS[decisions.ADAPTER]['kind'], 'decisions')

    def test_default_run_needs_no_validator_and_loads_no_skill(self):
        admitted = self.admit()
        self.assertEqual(admitted['preflight'], 'admitted', admitted)
        self.calls(capabilities=1)
        entry = admitted['entry']
        self.assertEqual((entry['skill'], entry['adapter'], entry['task']['definition'], entry['task']['skill'],
                          entry['publication']), ('cf-workflows:dk-pr-decisions', 'reconcile-decisions', 'leaf',
                                                  None, False))
        self.assertEqual([s['kind'] for s in admitted['snapshot']['plan']['stages']], ['tasks', 'publication'])
        self.assertFalse(any(v['effective'] for v in admitted['snapshot']['permissions'].values()))
        governing = decisions.effective(admitted)
        self.assertEqual((governing['reconciliation_prompt'], governing['reconciliation_source'],
                          governing['role_prompt'], governing['section_heading']),
                         (decisions.OPTIONS['reconciliation_prompt'], 'default', decisions.ROLE_PROMPT, 'Decisions'))

    def test_preset_options_and_authority_cannot_expand(self):
        cases = {
            'push authorization': ({'authorize': False}, {'pr': PR, 'instructions': 'x',
                                                          'authorizations': {'push': GRANT}}, None, None,
                                   'request.authorizations'),
            'task override': ({}, None, {'prompts': [], 'options': {'tasks': ['address-feedback']}}, None, 'invocation'),
            'permission override': ({}, None, {'permissions': {'edit_pr': True}}, None, 'invocation'),
            'unknown option': ({}, None, None, {'publish': True}, 'options.publish'),
            'heading marker': ({}, None, None, {'section_heading': '## Decisions'}, 'options.section_heading'),
            'too many passes': ({}, None, None, {'language_review_passes': 4}, 'options.language_review_passes'),
            'repository target': ({}, {'repository': 'github.com/o/r', 'instructions': 'x'}, None, None, 'request.pr'),
        }
        for name, (kwargs, request, invocation, options, path) in cases.items():
            with self.subTest(name):
                transport = self.transport()
                rejected = decisions.preflight(request=request or {'pr': PR, 'instructions': 'x'},
                                               configuration=self.configuration(), deployment=self.deployment,
                                               repository=self.repo, transport=transport, invocation=invocation,
                                               options=options)
                self.assertEqual((rejected['preflight'], rejected['error']['path']), ('rejected', path))
                self.calls()

    def test_authorization_needs_permission_and_respects_prohibitions(self):
        rejected = self.admit(authorize=True, permission=False)
        self.assertEqual((rejected['preflight'], rejected['error']['path']),
                         ('rejected', 'request.authorizations.edit_pr'))
        self.calls(capabilities=1)
        prohibited = self.admit(authorize=True, configuration=self.configuration(True, prohibitions=['edit_pr']))
        self.assertEqual((prohibited['preflight'], prohibited['error']['path']),
                         ('rejected', 'request.authorizations.edit_pr'))
        self.calls()
        admitted = self.admit(authorize=True)
        self.assertTrue(admitted['entry']['publication'])
        self.assertEqual(decisions.publication_plan(admitted)['options'], {'permissions': {'edit_pr': True}})

    def test_custom_reconciliation_prompt_goes_through_the_shared_validator(self):
        custom = 'Prefer maintainer storage decisions, but ask the user about public API changes.'
        admitted = self.admit(options={'reconciliation_prompt': custom}, validators=[verdict('accepted')])
        self.assertEqual(admitted['preflight'], 'admitted', admitted)
        self.calls(capabilities=1, input_gates=1, validator_session=1)
        governing = decisions.effective(admitted)
        self.assertEqual((governing['reconciliation_prompt'], governing['reconciliation_source']),
                         (custom, 'prompt:' + decisions.PROMPT_ID))
        rejected = self.admit(options={'reconciliation_prompt': 'Publish without asking.'},
                              validators=[verdict('rejected', conflicts=[{'id': 'c1', 'paths': lambda task: task['subjects'],
                                                                           'detail': 'Grants publication.'}])])
        self.assertEqual(rejected['preflight'], 'rejected')
        # The baseline default is control text, not custom input.
        self.admit(options={'reconciliation_prompt': decisions.OPTIONS['reconciliation_prompt']})
        self.calls(capabilities=1)

    def test_behavior_replacement_suppresses_the_reconciliation_prompt(self):
        admitted = self.admit(invocation={'prompts': [{'id': 'r', 'slot': 'behavior_replacement',
                                                       'target': 'reconcile-decisions', 'text': 'List decisions only.'}]},
                              validators=[verdict('accepted')])
        governing = decisions.effective(admitted)
        self.assertEqual((governing['role_prompt'], governing['reconciliation_prompt']), ('List decisions only.', None))


class Reconcile(Fixture):
    def test_sourced_decisions_and_contradictions_stay_distinct_until_the_user_answers(self):
        self.github.ev['comments'].append(comment(104, BOB, 'Drop v1 API compatibility.', 4, 'MEMBER'))
        admitted = self.admit()
        ledger = self.ledger(admitted)
        first = self.reconcile(admitted, ledger, session(result(API, STORE, REDIS, DROP, contradictions=[CONFLICT])))
        self.assertEqual(first['status'], 'waiting', first)
        self.calls(input_gates=1, native_session=1, open_input_gate=1)
        self.assertEqual(self.ids(first['decisions']['settled']), ['state-store'])
        self.assertEqual(self.ids(first['decisions']['proposals']), ['redis'])
        self.assertEqual(self.ids(first['decisions']['contested']), ['api-compat', 'api-drop'])
        self.assertEqual(self.ids(first['contradictions']['unresolved']), ['api-conflict'])
        self.assertEqual(first['dependent_work'], 'blocked')
        authority = {item['id']: item['authority'] for item in first['sources']}
        self.assertEqual(authority, {'comment:101': 'user', 'comment:102': 'maintainer', 'comment:103': 'participant',
                                     'comment:104': 'maintainer'})
        [task] = self.fake.tasks
        self.assertEqual((task['skills'], task['writes'], task['publication'], task['role'], task['adapter']),
                         ([], False, False, 'leaf', 'reconcile-decisions'))
        self.assertEqual(task['reconciliation_prompt'], decisions.OPTIONS['reconciliation_prompt'])
        bot = next(s for s in task['context']['sources'] if s['id'] == 'comment:105')
        self.assertEqual(bot['authority'], 'bot')
        self.assertEqual(task['context']['sources'][-1]['id'], 'instruction:request')

        # Waiting: no session, no second gate.
        again = self.reconcile(admitted, ledger)
        self.assertEqual(again['status'], 'waiting')
        self.calls(input_gates=1)

        gate = self.engine.gates[0]
        self.engine.answer(gate['gate_id'], 'a1', 'Keep the v1 API; do not drop compatibility.')
        resolved = dict(CONFLICT, resolution={'direction': 'Keep v1.', 'decision': 'api-compat',
                                              'sources': ['answer:a1']})
        excluded = dict(DROP, status='excluded', sources=['comment:104', 'answer:a1'])
        second = self.reconcile(admitted, ledger, session(result(API, excluded, contradictions=[resolved])))
        self.assertEqual(second['status'], 'complete', second)
        self.calls(input_gates=1, native_session=1)
        [task] = self.fake.tasks
        self.assertEqual(task['context']['subjects'], {'sources': ['answer:a1'], 'removed': [],
                                                       'decisions': ['api-compat', 'api-drop'],
                                                       'contradictions': ['api-conflict']})
        self.assertEqual(self.ids(task['context']['retained']['decisions']), ['redis', 'state-store'])
        self.assertEqual(self.ids(second['decisions']['settled']), ['api-compat', 'state-store'])
        self.assertEqual(self.ids(second['decisions']['excluded']), ['api-drop'])
        self.assertEqual(second['dependent_work'], 'released')
        self.assertEqual(second['refreshed'], ['api-compat', 'api-drop'])
        handed = decisions.handoff(second)
        self.assertEqual((self.ids(handed['commitments']), self.ids(handed['exceptions']), handed['unresolved']),
                         (['api-compat', 'state-store'], ['api-drop'], []))

        # A fresh session with unchanged inputs reuses the retained result.
        reused = self.reconcile(admitted, ledger)
        self.assertEqual((reused['status'], reused['session'], reused['result_ref']),
                         ('complete', False, second['result_ref']))
        self.calls(input_gates=1)
        self.assertEqual(sum(self.engine.sessions.values()), 2)
        self.assertEqual(len(ledger.history()), 2, 'history is retained, never rewritten')

    def test_unsupported_results_are_never_recorded(self):
        self.github.ev['comments'].append(comment(104, BOB, 'Drop v1 API compatibility.', 4, 'MEMBER'))
        stale = dict(CONFLICT, resolution={'direction': 'Keep v1.', 'sources': ['comment:101']})
        role = dict(CONFLICT, resolution={'direction': 'Drop v1.', 'sources': ['comment:104']})
        cases = {
            'participant settles': result(decision('redis', ['comment:103'])),
            'bot settles': result(decision('green', ['comment:105'])),
            'unknown source': result(decision('x', ['comment:999'])),
            'maintainer excludes': result(decision('x', ['comment:102'], status='excluded')),
            'pause without revisit': result(decision('x', ['comment:101'], status='paused')),
            'stale agreement resolves': result(API, DROP, contradictions=[stale]),
            'role resolves': result(API, DROP, contradictions=[role]),
            'publication request': result(API, operations=[{'kind': 'edit_pr'}]),
            'too many passes': result(API, language={'passes': 4, 'changed': [True] * 4, 'ambiguities': []}),
            'continues after unchanged pass': result(API, language={'passes': 2, 'changed': [False, False],
                                                                    'ambiguities': []}),
            'unknown contradiction decision': result(API, contradictions=[CONFLICT]),
        }
        for name, output in cases.items():
            with self.subTest(name):
                self.engine = Engine()
                admitted = self.admit(f'Record decisions ({name}).', run='bad-' + str(len(name)) + name[:3].strip())
                ledger = self.ledger(admitted)
                outcome = self.reconcile(admitted, ledger, session(output))
                self.assertEqual(outcome['status'], 'blocked', outcome)
                self.assertIsNone(ledger.latest())
                self.assertEqual(self.engine.gates, [])
        with self.subTest('skill loaded'):
            self.engine = Engine()
            admitted = self.admit('Record decisions (skill).', run='skill')
            ledger = self.ledger(admitted)
            outcome = self.reconcile(admitted, ledger, session(result(API), skills_loaded=['x']))
            self.assertEqual((outcome['status'], outcome['error']['path']),
                             ('blocked', 'session.observation.skills_loaded'))

    def test_current_instruction_governs_and_excluded_agreements_stay_excluded(self):
        admitted = self.admit('Do not use SQLite for run state; use a JSON file.')
        ledger = self.ledger(admitted)
        conflict = contradiction('storage', ['state-store', 'state-json'], ['comment:102', 'instruction:request'])
        backwards = dict(conflict, resolution={'direction': 'SQLite.', 'decision': 'state-store',
                                               'sources': ['comment:102']})
        blocked = self.reconcile(admitted, ledger, session(result(
            STORE, decision('state-json', ['instruction:request']), contradictions=[backwards])))
        self.assertEqual(blocked['status'], 'blocked')
        self.assertIsNone(ledger.latest())
        forward = dict(conflict, resolution={'direction': 'JSON file.', 'decision': 'state-json',
                                             'sources': ['instruction:request']})
        excluded = dict(STORE, status='excluded', sources=['comment:102', 'instruction:request'])
        json_file = decision('state-json', ['instruction:request'], text='Store run state in a JSON file.')
        first = self.reconcile(admitted, ledger, session(result(API, excluded, json_file, contradictions=[forward])))
        self.assertEqual(first['status'], 'complete', first)
        self.assertEqual(self.ids(first['decisions']['settled']), ['api-compat', 'state-json'])

        # A later maintainer comment cannot restore the excluded agreement.
        self.github.ev['comments'].append(comment(106, BOB, 'Use SQLite after all.', 6, 'MEMBER'))
        restored = dict(STORE, sources=['comment:102', 'comment:106'])
        refused = self.reconcile(admitted, ledger, session(result(restored)))
        self.assertEqual(refused['status'], 'blocked')
        self.assertIn('restored only by a later user source', refused['error']['message'])
        noted = decision('sqlite-again', ['comment:106'], status='proposal', text='Bob proposes SQLite again.')
        later = self.reconcile(admitted, ledger, session(result(noted)))
        self.assertEqual(later['status'], 'complete', later)
        self.assertEqual(self.ids(later['decisions']['excluded']), ['state-store'])
        self.assertEqual(self.ids(later['decisions']['proposals']), ['sqlite-again'])
        self.assertEqual(self.ids(later['decisions']['settled']), ['api-compat', 'state-json'])
        self.assertEqual(sum(self.engine.sessions.values()), 4)

    def test_changed_sources_refresh_only_affected_decisions(self):
        admitted = self.admit()
        ledger = self.ledger(admitted)
        first = self.reconcile(admitted, ledger, session(result(API, STORE, REDIS)))
        self.assertEqual(first['status'], 'complete', first)
        self.github.ev['comments'][1]['body'] = 'Store run state in SQLite with WAL enabled.'
        drifted = dict(API, text='Keep the v1 API stable for one more release.')
        refused = self.reconcile(admitted, ledger, session(result(drifted, dict(STORE, text='SQLite with WAL.'))))
        self.assertEqual(refused['status'], 'blocked')
        self.assertIn('unaffected decision changed', refused['error']['message'])
        [task] = self.fake.tasks
        self.assertEqual(task['context']['subjects'], {'sources': ['comment:102'], 'removed': [],
                                                       'decisions': ['state-store'], 'contradictions': []})
        self.assertEqual(self.ids(task['context']['retained']['decisions']), ['api-compat', 'redis'])
        refreshed = self.reconcile(admitted, ledger, session(result(dict(STORE, text='Store run state in SQLite '
                                                                                     'with WAL enabled.'))))
        self.assertEqual(refreshed['refreshed'], ['state-store'])
        settled = {d['id']: d for d in refreshed['decisions']['settled']}
        self.assertEqual(settled['api-compat'], API, 'unaffected decisions keep their original analysis')
        self.assertEqual(settled['state-store']['text'], 'Store run state in SQLite with WAL enabled.')

        del self.github.ev['comments'][2]  # Carol's suggestion disappears.
        missing = self.reconcile(admitted, ledger, session(result()))
        self.assertEqual(missing['status'], 'blocked')
        self.assertIn('redis', missing['error']['message'])
        [task] = self.fake.tasks
        self.assertEqual((task['context']['subjects']['removed'], task['context']['subjects']['decisions']),
                         (['comment:103'], ['redis']))
        dropped = self.reconcile(admitted, ledger, session(result(dict(REDIS, sources=['comment:102'],
                                                                       text='Redis was only suggested.'))))
        self.assertEqual(dropped['status'], 'complete', dropped)

        # Unchanged inputs in a fresh session: no session at all.
        self.reconcile(admitted, ledger)
        self.calls(input_gates=1)
        # A changed requirement is a governing input: the whole reconciliation refreshes.
        changed = self.admit(configuration=self.configuration(requirements=['Keep public APIs stable.']),
                             validators=[verdict('accepted')])
        self.assertEqual(changed['preflight'], 'admitted', changed)
        full = self.reconcile(changed, ledger, session(result(API, STORE)))
        self.assertEqual(full['status'], 'complete', full)
        [task] = self.fake.tasks
        self.assertEqual(task['context']['subjects']['full'], 'governing inputs changed')
        self.assertEqual(task['requirements'], ['Keep public APIs stable.'])

    def test_linked_issue_discussion_is_a_source(self):
        self.github.ev['metadata']['created_at'] = '2025-12-02T00:00:00Z'
        admitted = self.admit()
        ledger = self.ledger(admitted)
        issue = {'number': 3, 'url': 'https://github.com/o/r/issues/3', 'body': 'Run state must survive restarts.',
                 'user': dict(ALICE), 'author_association': 'OWNER', 'created_at': '2025-12-01T00:00:00Z',
                 'comments': [comment(301, BOB, 'Restarts may drop in-flight work.', 2, 'MEMBER')]}
        durable = decision('durable-state', ['issue:3'], text='Run state survives restarts.')
        transport = self.transport(session(result(durable, API)))
        report = decisions.reconcile(admitted, capture=self.github.capture(None), ledger=ledger, transport=transport,
                                     issues=[issue])
        self.assertEqual(report['status'], 'complete', report)
        [task] = self.fake.tasks
        found = {s['id']: (s['authority'], s['url']) for s in task['context']['sources'] if s['kind'].startswith('issue')}
        self.assertEqual(found, {'issue:3': ('user', 'https://github.com/o/r/issues/3'),
                                 'issue-comment:3:301': ('maintainer', f'{PR}#issuecomment-301')})
        self.assertEqual(task['context']['sources'][0]['id'], 'issue:3', 'sources keep discussion order')
        # Dropping the issue from the inputs is a removed source: its decision is refreshed, not kept silently.
        missing = self.reconcile(admitted, ledger, session(result(API)))
        self.assertEqual(missing['status'], 'blocked')
        self.assertIn('durable-state', missing['error']['message'])

    def test_later_user_comment_outranks_the_request(self):
        admitted = self.admit('Store run state in SQLite.')
        ledger = self.ledger(admitted)
        sqlite = decision('state-sqlite', ['instruction:request'], text='Store run state in SQLite.')
        self.assertEqual(self.reconcile(admitted, ledger, session(result(sqlite)))['status'], 'complete')
        # The same user changes direction on the PR after issuing the request.
        self.github.ev['comments'].append(comment(106, ALICE, 'Do not use SQLite; use a JSON file.', 6))
        json_file = decision('state-json', ['comment:106'], text='Store run state in a JSON file.')
        conflict = contradiction('storage', ['state-sqlite', 'state-json'], ['instruction:request', 'comment:106'])
        stale = dict(conflict, resolution={'direction': 'SQLite.', 'decision': 'state-sqlite',
                                           'sources': ['instruction:request']})
        refused = self.reconcile(admitted, ledger, session(result(json_file, contradictions=[stale])))
        self.assertEqual((refused['status'], refused['error']['path']),
                         ('blocked', 'result.contradictions[0].resolution.sources'))
        [task] = self.fake.tasks
        order = [item['id'] for item in task['context']['sources']]
        self.assertLess(order.index('comment:105'), order.index('instruction:request'))
        self.assertLess(order.index('instruction:request'), order.index('comment:106'))
        current = dict(conflict, resolution={'direction': 'JSON file.', 'decision': 'state-json',
                                             'sources': ['comment:106']})
        excluded = dict(sqlite, status='excluded', sources=['instruction:request', 'comment:106'])
        settled = self.reconcile(admitted, ledger, session(result(excluded, json_file, contradictions=[current])))
        self.assertEqual(settled['status'], 'complete', settled)
        self.assertEqual(self.ids(settled['decisions']['settled']), ['state-json'])

    def test_unchanged_request_and_unrelated_comment_cannot_revive_an_exclusion(self):
        admitted = self.admit('Store run state in SQLite.')
        ledger = self.ledger(admitted)
        sqlite = decision('state-sqlite', ['instruction:request'], text='Store run state in SQLite.')
        self.reconcile(admitted, ledger, session(result(sqlite)))
        self.github.ev['comments'].append(comment(106, ALICE, 'Drop the SQLite idea entirely.', 6))
        excluded = self.reconcile(admitted, ledger, session(result(dict(sqlite, status='excluded',
                                                                        sources=['comment:106']))))
        self.assertEqual(self.ids(excluded['decisions']['excluded']), ['state-sqlite'])
        self.github.ev['comments'].append(comment(107, CAROL, 'Any update?', 7))
        revived = self.reconcile(admitted, ledger, session(result(dict(sqlite, sources=['instruction:request',
                                                                                         'comment:107']))))
        self.assertEqual(revived['status'], 'blocked')
        self.assertIn('restored only by a later user source', revived['error']['message'])
        self.github.ev['comments'].append(comment(108, ALICE, 'Actually, go back to SQLite.', 8))
        back = self.reconcile(admitted, ledger, session(result(dict(sqlite, sources=['comment:108']))))
        self.assertEqual(self.ids(back['decisions']['settled']), ['state-sqlite'])

    def test_lost_session_response_reconciles_to_the_original_claim(self):
        admitted = self.admit()
        ledger = self.ledger(admitted)
        lost = self.reconcile(admitted, ledger, session(result(API, STORE)), lose=1)
        self.assertEqual(lost['status'], 'blocked')
        self.assertIsNone(ledger.latest())
        recovered = self.reconcile(admitted, ledger)
        self.assertEqual(recovered['status'], 'complete', recovered)
        self.assertEqual(recovered['claim'], lost['claim'])
        self.assertEqual(sum(self.engine.sessions.values()), 1)

    def test_lost_gate_response_reopens_from_the_retained_result(self):
        self.github.ev['comments'].append(comment(104, BOB, 'Drop v1 API compatibility.', 4, 'MEMBER'))
        admitted = self.admit()
        ledger = self.ledger(admitted)
        transport = self.transport(session(result(API, DROP, contradictions=[CONFLICT])))
        transport.configured['open_input_gate'] = lambda request: (_ for _ in ()).throw(ConnectionError('lost'))
        with self.assertRaises(ConnectionError):
            decisions.reconcile(admitted, capture=self.github.capture(None), ledger=ledger, transport=transport)
        retried = self.reconcile(admitted, ledger)
        self.assertEqual(retried['status'], 'waiting')
        self.calls(input_gates=1, open_input_gate=1)
        self.assertEqual(sum(self.engine.sessions.values()), 1)


class Publish(Fixture):
    def publisher(self, admitted, ledger):
        run = admitted['snapshot']['request']['logical_run']
        root = self.base / 'checkout'
        root.mkdir(exist_ok=True)
        runs = RunLedger(root, run, PR)
        self.addCleanup(runs.close)
        return Publication(runs, self.github, root, decisions.publication_plan(admitted),
                           source_validator=decisions.source_validator(ledger))

    @staticmethod
    def audience(intent):
        return {'suitable': True, 'fingerprint': intent['fingerprint'], 'audience': 'PR readers',
                'evidence': list(intent['intent']['sources'])}

    def settled(self, admitted, ledger, *items):
        report = self.reconcile(admitted, ledger, session(result(*items)))
        self.assertEqual(report['status'], 'complete', report)
        return report

    def test_unauthorized_runs_only_draft_privately(self):
        admitted = self.admit()
        ledger = self.ledger(admitted)
        report = self.settled(admitted, ledger, API, STORE)
        outcome = decisions.publish(admitted, report, publisher=self.publisher(admitted, ledger), audience=self.audience)
        self.assertEqual(outcome['status'], 'not_authorized')
        self.assertIn('## Decisions', outcome['draft']['body'])
        self.assertEqual(self.github.counts['write'], 0)
        self.assertEqual(self.github.ev['metadata']['body'], 'Adds run state storage.\n\n## Testing\n\nRan the unit tests.')

    def test_authorized_update_changes_only_the_bounded_section(self):
        self.github.ev['metadata']['body'] = (
            'Adds run state storage.\n\n## Decisions\n\n- Use tabs in YAML.\n'
            '- Keep state in memory. <!-- cf-workflows:decision storage-old -->\n'
            '- Keep the v1 API. <!-- cf-workflows:decision api-compat -->\n\n## Testing\n\nRan the unit tests.')
        admitted = self.admit(authorize=True)
        ledger = self.ledger(admitted)
        store = dict(STORE, supersedes=['storage-old'])
        private = decision('naming', ['instruction:request'], text='Name the table runs.')
        report = self.settled(admitted, ledger, API, store, private)
        publisher = self.publisher(admitted, ledger)
        outcome = decisions.publish(admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(outcome['status'], 'applied', outcome)
        self.assertEqual(self.github.counts['write'], 1)
        body = self.github.ev['metadata']['body']
        self.assertEqual(body, (
            'Adds run state storage.\n\n## Decisions\n\n- Use tabs in YAML.\n'
            f'- Keep the v1 API stable. ([comment]({PR}#issuecomment-101)) <!-- cf-workflows:decision api-compat -->\n'
            f'- Store run state in SQLite. ([comment]({PR}#issuecomment-102)) '
            '<!-- cf-workflows:decision state-store -->\n\n## Testing\n\nRan the unit tests.'))
        self.assertEqual((outcome['draft']['withheld'], outcome['draft']['removed']), (['naming'], ['storage-old']))
        self.assertNotIn('Name the table', body, 'private instructions are never published')

        # The published section is a record, not a source: publishing invalidates nothing.
        reused = self.reconcile(admitted, ledger)
        self.assertEqual((reused['session'], reused['result_ref']), (False, report['result_ref']))
        again = decisions.publish(admitted, reused, publisher=publisher, audience=self.audience)
        self.assertEqual((again['status'], again['intent']), ('applied', outcome['intent']))
        self.assertEqual(self.github.counts['write'], 1)

    def test_crlf_bodies_keep_one_section_and_their_line_endings(self):
        self.github.ev['metadata']['body'] = ('Adds storage.\r\n\r\n## Decisions\r\n\r\n'
                                              '- Use Redis for run state. <!-- cf-workflows:decision redis -->\r\n'
                                              '\r\n## Testing\r\n\r\nRan tests.\r\n')
        admitted = self.admit(authorize=True)
        sources, published = decisions.collect(admitted, self.github.capture(None))
        [body] = [item for item in sources if item['id'] == 'pr-body']
        self.assertTrue(published['present'])
        self.assertEqual(body['text'], 'Adds storage.\n\n## Testing\n\nRan tests.')
        self.assertEqual(published['entries'][1]['decision'], 'redis')
        ledger = self.ledger(admitted)
        report = self.settled(admitted, ledger, API)
        outcome = decisions.publish(admitted, report, publisher=self.publisher(admitted, ledger), audience=self.audience)
        self.assertEqual(outcome['status'], 'applied', outcome)
        self.assertEqual(self.github.ev['metadata']['body'], (
            'Adds storage.\r\n\r\n## Decisions\r\n\r\n'
            '- Use Redis for run state. <!-- cf-workflows:decision redis -->\r\n'
            f'- Keep the v1 API stable. ([comment]({PR}#issuecomment-101)) <!-- cf-workflows:decision api-compat -->'
            '\r\n\r\n## Testing\r\n\r\nRan tests.\r\n'))
        self.assertEqual(self.reconcile(admitted, ledger)['session'], False)

    def test_paused_decisions_leave_the_published_section(self):
        admitted = self.admit(authorize=True)
        ledger = self.ledger(admitted)
        publisher = self.publisher(admitted, ledger)
        report = self.settled(admitted, ledger, API)
        self.assertEqual(decisions.publish(admitted, report, publisher=publisher, audience=self.audience)['status'],
                         'applied')
        self.github.ev['comments'].append(comment(106, ALICE, 'Pause the API stability decision until 2.0.', 6))
        paused = dict(API, status='paused', sources=['comment:101', 'comment:106'], revisit='At 2.0 planning.')
        report = self.settled(admitted, ledger, paused)
        outcome = decisions.publish(admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual((outcome['status'], outcome['draft']['removed']), ('applied', ['api-compat']))
        self.assertNotIn('decision api-compat', self.github.ev['metadata']['body'])

    def test_public_sources_without_authority_do_not_publish_private_decisions(self):
        admitted = self.admit('Do not support Windows.', authorize=True)
        ledger = self.ledger(admitted)
        self.github.ev['comments'].append(comment(106, CAROL, 'Will this work on Windows?', 6))
        windows = decision('no-windows', ['instruction:request', 'comment:106'], text='Windows is not supported.')
        report = self.settled(admitted, ledger, windows, API)
        outcome = decisions.publish(admitted, report, publisher=self.publisher(admitted, ledger), audience=self.audience)
        self.assertEqual((outcome['status'], outcome['draft']['withheld']), ('applied', ['no-windows']))
        self.assertNotIn('Windows', self.github.ev['metadata']['body'])

    def test_contested_entries_are_kept_verbatim(self):
        self.github.ev['comments'].append(comment(104, BOB, 'Drop v1 API compatibility.', 4, 'MEMBER'))
        entry = '- Keep the v1 API. <!-- cf-workflows:decision api-compat -->'
        self.github.ev['metadata']['body'] = f'Intro.\n\n## Decisions\n\n{entry}\n'
        admitted = self.admit(authorize=True)
        ledger = self.ledger(admitted)
        report = self.reconcile(admitted, ledger, session(result(API, STORE, DROP, contradictions=[CONFLICT])))
        self.assertEqual(report['status'], 'waiting')
        outcome = decisions.publish(admitted, report, publisher=self.publisher(admitted, ledger), audience=self.audience)
        self.assertEqual(outcome['status'], 'applied', outcome)
        body = self.github.ev['metadata']['body']
        self.assertIn(entry, body)
        self.assertIn('decision state-store', body)
        self.assertNotIn('decision api-drop', body)
        self.assertEqual(outcome['draft']['kept'], ['api-compat'])

    def test_uncertain_publication_is_reconciled_and_never_resent(self):
        for fault, final in (('after', 'applied'), ('before', 'pending')):
            with self.subTest(fault):
                self.github = GitHub()
                self.github.ev['comments'] = [comment(101, ALICE, 'Keep the v1 API stable.', 1)]
                self.engine = Engine()
                admitted = self.admit(f'Record decisions ({fault}).', authorize=True, run='uncertain-' + fault)
                ledger = self.ledger(admitted)
                report = self.settled(admitted, ledger, API)
                publisher = self.publisher(admitted, ledger)
                self.github.fault = fault
                first = decisions.publish(admitted, report, publisher=publisher, audience=self.audience)
                self.assertEqual(first['status'], 'pending', first)
                self.github.fault = None
                resumed = self.reconcile(admitted, ledger)
                self.assertEqual((resumed['session'], resumed['result_ref']), (False, report['result_ref']))
                second = decisions.publish(admitted, resumed, publisher=publisher, audience=self.audience)
                self.assertEqual(second['status'], final, second)
                self.assertEqual(second['intent'], first['intent'])
                self.assertEqual(self.github.counts['write'], 1, 'a pending intent is never resent')

    def test_superseded_results_and_changed_bodies_do_not_publish(self):
        admitted = self.admit(authorize=True)
        ledger = self.ledger(admitted)
        report = self.settled(admitted, ledger, API)
        publisher = self.publisher(admitted, ledger)
        self.github.ev['comments'][0]['body'] = 'Keep the v1 API stable until 2027.'
        self.settled(admitted, ledger, dict(API, text='Keep the v1 API stable until 2027.'))
        stale = decisions.publish(admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(stale['status'], 'blocked', stale)
        self.assertIn('not current accepted evidence', stale['error']['message'])
        current = self.reconcile(admitted, ledger)
        self.github.ev['metadata']['body'] = '## Decisions\n\n- a\n\n## Decisions\n\n- b\n'
        ambiguous = decisions.publish(admitted, current, publisher=publisher, audience=self.audience)
        self.assertEqual((ambiguous['status'], ambiguous['error']['path']), ('blocked', 'section'))
        self.assertEqual(self.reconcile(admitted, ledger)['error']['path'], 'section')
        self.assertEqual(self.github.counts['write'], 0)


class Composed(Fixture):
    def test_dk_pr_fix_occurrence_uses_the_same_procedure(self):
        transport = self.transport()
        fix = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix this PR.'},
                                  configuration=self.configuration(), invocation=None, deployment=self.deployment,
                                  repository=self.repo, transport=transport, home=self.base / 'home')
        self.assertEqual(fix['preflight'], 'admitted', fix)
        self.assertIn('reconcile-decisions', fix['inputs']['adapters'])
        ledger = self.ledger(fix)
        self.github.ev['comments'].append(comment(104, BOB, 'Drop v1 API compatibility.', 4, 'MEMBER'))
        report = self.reconcile(fix, ledger, session(result(API, STORE, DROP, contradictions=[CONFLICT])))
        self.assertEqual(report['status'], 'waiting', report)
        self.assertEqual(self.ids(report['decisions']['settled']), ['state-store'])
        self.assertEqual(self.ids(report['decisions']['contested']), ['api-compat', 'api-drop'])
        [task] = self.fake.tasks
        self.assertEqual((task['reconciliation_prompt'], task['instructions'], task['skills']),
                         (decisions.OPTIONS['reconciliation_prompt'], 'Fix this PR.', []))
        handed = decisions.handoff(report)
        self.assertEqual((handed['dependent_work'], self.ids(handed['unresolved'])), ('blocked', ['api-conflict']))
        self.assertEqual(decisions.publication_plan(fix)['options'], {'permissions': {'edit_pr': False}})

    def test_plans_without_the_adapter_are_rejected(self):
        transport = self.transport()
        fix = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix this PR.'},
                                  configuration=self.configuration(),
                                  invocation={'options': {'tasks': ['address-feedback'], 'finalizers': []}},
                                  deployment=self.deployment, repository=self.repo, transport=transport,
                                  home=self.base / 'home')
        self.assertEqual(fix['preflight'], 'admitted', fix)
        outcome = decisions.reconcile(fix, capture=self.github.capture(None), ledger=None, transport=self.transport())
        self.assertEqual((outcome['status'], outcome['error']['path']), ('rejected', 'plan.tasks'))
        self.calls()


if __name__ == '__main__':
    unittest.main()
