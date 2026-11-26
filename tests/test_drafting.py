"""cf-workflows:dk-pr-description, dk-commit-message and dk-issue: drafting and issue creation (#32).

Disposable repositories with and without templates, a disposable drafting ledger
and run ledger per logical run, the fail-closed fake engine of the configuration
tests (claim-reconciled native sessions) and the fail-closed fake GitHub of the
publication tests, extended with issue, label and assignee routes. Every
unscripted session and unconfigured operation fails with no live fallback. Each
case asserts sessions, transport calls or writes. These results do not certify
any engine, provider, host or GitHub conformance.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
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
from prfix.publication import ISSUE_KINDS, Publication, PublicationError  # noqa: E402
from cf_workflows import admission, configuration, dk_pr_fix, drafting, semantic  # noqa: E402
from test_configuration_skills import Engine, Transport, session  # noqa: E402
from test_dk_pr_fix_publication import Transport as PublicationTransport  # noqa: E402
from test_publication import FakeGitHub  # noqa: E402

PR = 'https://github.com/o/r/pull/7'
REPOSITORY = 'github.com/o/r'
INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}
GRANTS = {action: {'request_id': 'user-1', 'instruction': f'Allow {action}.', 'source': 'chat:1'}
          for action in ('edit_pr', 'create_issue', 'edit_issue', 'push')}
ISSUE_BODY = ('## Problem\n\nUploads fail when the network drops briefly.\n\n'
              '## Expected behavior\n\nUploads retry transient failures.\n')
DESCRIPTION_BODY = ('## Issue\n\nUploads fail on transient network errors.\n\n'
                    '## Fix\n\nRetry each upload up to three times.\n')
PR_TEMPLATE = '## Summary\n\n<!-- What changed and why? -->\n\n## Testing\n\n<!-- How was this tested? -->\n'
ISSUE_FORM = ('name: Bug report\ndescription: Report a bug\nbody:\n  - type: textarea\n    attributes:\n'
              '      label: What happened?\n  - type: textarea\n    attributes:\n      label: Steps to reproduce\n')


def git(root, *args):
    return subprocess.run(git_fixture.command('-c', 'commit.gpgsign=false', *args), cwd=root, check=True,
                          capture_output=True, env=git_fixture.environment()).stdout.decode().strip()


class IssueRoutes(FakeGitHub):
    """Issue, label and assignee routes; any other issue or listing route fails closed."""

    def __init__(self):
        super().__init__()
        self.repository = {'id': 99, 'full_name': 'o/r'}
        self.labels = [{'name': 'bug'}, {'name': 'flaky-test'}]
        self.assignees = [{'login': 'maintainer'}, {'login': 'alice'}]
        self.issues = []
        self.listed = Counter()

    def issue(self, number):
        [found] = [item for item in self.issues if item['number'] == number]
        return found

    def add_issue(self, title, body, *, labels=(), assignees=(), user=None, state='open'):
        number = 100 + len(self.issues)
        self.issues.append({'number': number, 'id': 9000 + number, 'title': title, 'body': body, 'state': state,
                            'labels': [{'name': name} for name in labels],
                            'assignees': [{'login': login} for login in assignees],
                            'user': copy.deepcopy(user or self.ev['viewer']),
                            'html_url': f'https://github.com/o/r/issues/{number}'})
        return self.issues[-1]

    def pages(self, path):
        self.listed[path.split('?')[0]] += 1
        if path == 'repos/o/r/labels?per_page=100':
            return copy.deepcopy(self.labels)
        if path == 'repos/o/r/assignees?per_page=100':
            return copy.deepcopy(self.assignees)
        match = re.fullmatch(r'repos/o/r/issues\?state=all&creator=([A-Za-z0-9-]+)&per_page=100', path)
        if match:
            return copy.deepcopy([item for item in self.issues if item['user']['login'] == match[1]])
        if '/pulls?' in path:
            return super().pages(path)
        raise AssertionError(('unconfigured pages', path))

    def request(self, method, path, payload=None):
        match = re.fullmatch(r'repos/o/r/issues/([0-9]+)', path)
        if (method, path) == ('GET', 'user'):
            return copy.deepcopy(self.ev['viewer'])
        if (method, path) == ('GET', 'repos/o/r'):
            return copy.deepcopy(self.repository)
        if match and method == 'GET':
            return copy.deepcopy(self.issue(int(match[1])))
        if match and method == 'PATCH':
            self.calls.append((method, path, copy.deepcopy(payload)))
            issue = self.issue(int(match[1]))
            issue.update({key: value for key, value in payload.items() if key in ('title', 'body')})
            if 'labels' in payload:
                issue['labels'] = [{'name': name} for name in payload['labels']]
            if 'assignees' in payload:
                issue['assignees'] = [{'login': login} for login in payload['assignees']]
            return copy.deepcopy(issue)
        if (method, path) == ('POST', 'repos/o/r/issues'):
            self.calls.append((method, path, copy.deepcopy(payload)))
            return copy.deepcopy(self.add_issue(payload['title'], payload['body'], labels=payload['labels'],
                                                assignees=payload['assignees']))
        if 'issues' in path or path == 'user':
            raise AssertionError(('unconfigured route', method, path))
        return super().request(method, path, payload)


class GitHub(PublicationTransport, IssueRoutes):
    """Counted reads and writes with injected response loss before or after the effect."""


def output(title='Retry flaky uploads', body=DESCRIPTION_BODY, *, template=None, omitted=(), claims=None,
           questions=()):
    return {'title': title, 'body': body, 'template': template, 'omitted': list(omitted),
            'claims': claims or [{'text': 'Uploads are retried.', 'sources': ['diff']}],
            'questions': list(questions), 'summary': 'Drafted from the evidence.'}


def message(text='Retry transient upload failures\n\nUploads retry up to three times before failing.\n',
            included=('src/upload.py', 'tests/test_upload.py'), excluded=(('notes.txt', 'Unrelated notes.'),),
            claims=None):
    return {'message': text, 'scope': {'included': list(included),
                                       'excluded': [{'path': p, 'reason': r} for p, r in excluded]},
            'claims': claims or [{'text': 'Uploads are retried.', 'sources': ['diff:staged']}],
            'questions': [], 'summary': 'Drafted from the local changes.'}


def issue(title='Uploads fail on transient network errors', body=ISSUE_BODY, **extra):
    extra.setdefault('claims', [{'text': 'Uploads fail on short outages.', 'sources': ['fact:outage']}])
    return output(title, body, **extra)


OUTAGE = {'id': 'outage', 'kind': 'fact', 'text': 'Uploads fail when the network drops for a second.'}


class Fixture(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix='cfw drafting ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Ada Lovelace')
        git(self.repo, 'config', 'user.email', 'ada@example.invalid')
        self.write('AGENTS.md', 'Write short imperative commit subjects.\n')
        self.write('src/upload.py', 'def upload(data):\n    return send(data)\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'Add uploads')
        self.base_sha = git(self.repo, 'rev-parse', 'HEAD')
        self.write('src/upload.py', 'def upload(data):\n    for _ in range(3):\n        try:\n'
                                    '            return send(data)\n        except OSError:\n            pass\n')
        git(self.repo, 'commit', '-q', '-am', 'Retry uploads')
        self.head_sha = git(self.repo, 'rev-parse', 'HEAD')
        self.engine = Engine()
        self.github = GitHub()
        meta = self.github.ev['metadata']
        meta.update(title='WIP', body='Old description')
        meta['head']['sha'], meta['base']['sha'] = self.head_sha, self.base_sha
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}

    def write(self, rel, body):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def configuration(self, *permissions):
        scope = {'id': 'me', 'selector': {'kind': 'personal', 'id': 'me'}, 'options': {},
                 'permissions': {action: True for action in permissions}, 'prompts': [], 'skills': [],
                 'preferences': []}
        return {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                'scopes': [scope], 'resources': [],
                'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                              'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}

    def transport(self, *sessions, lose=0):
        self.fake = Transport(self.engine, sessions, lose=lose)
        return self.fake

    def calls(self, **expected):
        self.assertEqual(self.fake.calls, +Counter(expected))

    def admit(self, name, instructions='Draft it.', *, authorize=(), permissions=None, target=None, **extra):
        request = {'instructions': instructions, **(target or {'pr': PR})}
        if authorize:
            request['authorizations'] = {action: GRANTS[action] for action in authorize}
        permissions = authorize if permissions is None else permissions
        return drafting.preflight(name, request=request, configuration=self.configuration(*permissions),
                                  deployment=self.deployment, repository=self.repo, transport=self.transport(),
                                  **extra)

    def ledger(self, admitted):
        request = admitted['snapshot']['request']
        target = request['pr']['url'] if request['pr'] else request['repository']
        ledger = drafting.Ledger(drafting.ledger_path(self.base / 'state', request['logical_run']),
                                 logical_run=request['logical_run'], target=target)
        self.addCleanup(ledger.close)
        return ledger

    def draft(self, name, admitted, ledger, *sessions, lose=0, **inputs):
        transport = self.transport(*sessions, lose=lose)
        if name == 'dk-pr-description':
            inputs.setdefault('capture', self.github.capture(None))
        if name == 'dk-issue':
            inputs.setdefault('github', self.github)
        return drafting.draft(name, admitted, repository=self.repo, ledger=ledger, transport=transport, **inputs)

    def publisher(self, name, admitted, ledger):
        request = admitted['snapshot']['request']
        root = self.base / 'checkout'
        root.mkdir(exist_ok=True)
        runs = RunLedger(root, request['logical_run'], request['pr']['url'] if request['pr'] else None)
        self.addCleanup(runs.close)
        return Publication(runs, self.github, root, drafting.publication_plan(name, admitted),
                           source_validator=drafting.source_validator(ledger, name))

    @staticmethod
    def audience(intent):
        return {'suitable': True, 'fingerprint': intent['fingerprint'], 'audience': 'Repository readers',
                'evidence': list(intent['intent']['sources'])}

    def writes(self):
        return [call for call in self.github.writes if call[0] in ('POST', 'PATCH', 'PUT')]


# -- Presets ---------------------------------------------------------------------------------------

class Preset(Fixture):
    def test_baseline_bytes_options_and_presets(self):
        for name, entry in drafting.ENTRIES.items():
            digests = {item['path']: item['sha256'] for item in INVENTORY[name]['baseline_resources']}
            for host in ('claude', 'codex'):
                self.assertEqual(hashlib.sha256(entry['config'].encode()).hexdigest(),
                                 digests[f'{host}/skills/{name}/config.json'], name)
                self.assertEqual(hashlib.sha256(entry['role_prompt'].encode()).hexdigest(),
                                 digests[f'{host}/skills/{name}/prompts/default.md'], name)
        self.assertEqual(drafting.preset('dk-pr-description'),
                         {'tasks': ['describe-pr', {'stage': 'publication'}], 'finalizers': [], 'reviewers': []})
        self.assertEqual(drafting.preset('dk-commit-message'),
                         {'tasks': ['draft-commit-message'], 'finalizers': [], 'reviewers': []})
        self.assertEqual(drafting.preset('dk-issue'),
                         {'tasks': ['draft-issue', {'stage': 'publication'}], 'finalizers': [], 'reviewers': []})
        self.assertIn('describe-pr', admission.BASELINE_ADAPTERS)
        for adapter in ('draft-commit-message', 'draft-issue'):
            self.assertFalse(admission.ENTRY_ADAPTERS[adapter]['writes'])
        # draft-issue runs as a composed occurrence; draft-commit-message only under its own entry.
        self.assertNotIn('draft-issue', dk_pr_fix.ENTRY_ONLY)
        self.assertEqual(dk_pr_fix.ENTRY_ONLY['draft-commit-message'], 'cf-workflows:dk-commit-message')

    def test_default_runs_need_no_validator_and_load_no_skill(self):
        for name, target in (('dk-pr-description', {'pr': PR}), ('dk-commit-message', {'repository': REPOSITORY}),
                             ('dk-issue', {'repository': REPOSITORY})):
            with self.subTest(name):
                admitted = self.admit(name, target=target)
                self.assertEqual(admitted['preflight'], 'admitted', admitted)
                self.calls(capabilities=1)
                entry = admitted['entry']
                self.assertEqual((entry['adapter'], entry['task']['definition'], entry['task']['skill']),
                                 (drafting.ENTRIES[name]['adapter'], 'leaf', None))
                self.assertFalse(any(value['effective'] for value in admitted['snapshot']['permissions'].values()))

    def test_presets_options_and_authority_cannot_expand(self):
        cases = [
            ('dk-commit-message', {'authorizations': {'push': GRANTS['push']}}, None, None, 'request.authorizations'),
            ('dk-pr-description', {'authorizations': {'create_issue': GRANTS['create_issue']}}, None, None,
             'request.authorizations'),
            ('dk-issue', {'authorizations': {'edit_pr': GRANTS['edit_pr']}}, None, None, 'request.authorizations'),
            ('dk-pr-description', {'repository': REPOSITORY, 'pr': None}, None, None, 'request.pr'),
            ('dk-issue', {}, {'options': {'tasks': ['describe-pr']}}, None, 'invocation'),
            ('dk-issue', {}, {'permissions': {'create_issue': True}}, None, 'invocation'),
            ('dk-issue', {}, None, {'labels': 'bug'}, 'options.labels'),
            ('dk-issue', {}, None, {'template': '../outside.md'}, 'options.template'),
            ('dk-pr-description', {}, None, {'update_title': 'no'}, 'options.update_title'),
            ('dk-commit-message', {}, None, {'subject_target_length': 0}, 'options.subject_target_length'),
            ('dk-commit-message', {}, None, {'commit': True}, 'options.commit'),
        ]
        for name, request, invocation, options, path in cases:
            with self.subTest(name=name, path=path):
                body = {'pr': PR, 'instructions': 'x', **request}
                if body.get('pr') is None:
                    body.pop('pr')
                rejected = drafting.preflight(name, request=body, configuration=self.configuration(),
                                              deployment=self.deployment, repository=self.repo,
                                              transport=self.transport(), invocation=invocation, options=options)
                self.assertEqual((rejected['preflight'], rejected['error']['path']), ('rejected', path))
                self.calls()

    def test_authorization_needs_the_configured_permission(self):
        rejected = self.admit('dk-issue', target={'repository': REPOSITORY}, authorize=('create_issue',),
                              permissions=())
        self.assertEqual((rejected['preflight'], rejected['error']['path']),
                         ('rejected', 'request.authorizations.create_issue'))
        self.calls(capabilities=1)
        admitted = self.admit('dk-issue', target={'repository': REPOSITORY}, authorize=('create_issue',))
        self.assertEqual(admitted['entry']['publication'], {'create_issue': True, 'edit_issue': False})
        self.assertEqual(drafting.publication_plan('dk-issue', admitted)['options'],
                         {'permissions': {'create_issue': True, 'edit_issue': False}})

    def test_installed_dk_pr_fix_runs_draft_issue_but_never_commit_messages_or_issue_writes(self):
        # draft-issue runs through the installed drafting procedure (tests/test_dk_pr_fix_drafting.py);
        # draft-commit-message runs only under its own entry.
        for tasks, admitted in ((['draft-issue'], True), (['draft-commit-message'], False)):
            result = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix.'},
                                         configuration=self.configuration(), transport=self.transport(),
                                         invocation={'options': {'tasks': tasks, 'finalizers': []}},
                                         deployment=self.deployment, repository=self.repo, home=self.base / 'home')
            if admitted:
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
            else:
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'options.tasks'))
        result = dk_pr_fix.preflight(
            request={'pr': PR, 'instructions': 'Fix.', 'authorizations': {'create_issue': GRANTS['create_issue']}},
            configuration=self.configuration('create_issue'), transport=self.transport(), invocation=None,
            deployment=self.deployment, repository=self.repo, home=self.base / 'home')
        self.assertEqual(result['error']['path'], 'request.authorizations.create_issue')
        self.assertIn('dk-issue', result['error']['message'])


# -- dk-pr-description ------------------------------------------------------------------------------

class Description(Fixture):
    def test_fallback_draft_is_private_without_authority_and_reused_unchanged(self):
        admitted = self.admit('dk-pr-description')
        ledger = self.ledger(admitted)
        report = self.draft('dk-pr-description', admitted, ledger, session(output()))
        self.assertEqual(report['status'], 'drafted', report)
        self.assertEqual(self.engine.sessions, Counter({'dk-pr-description': 1}))
        [task] = self.fake.tasks
        self.assertEqual((task['adapter'], task['skills'], task['writes'], task['publication'], task['tools']),
                         ('describe-pr', [], False, False, 'read-only'))
        self.assertEqual(task['context']['templates'], [])
        self.assertIn('diff', {item['id'] for item in task['context']['sources']})
        self.assertIn('+    for _ in range(3):', next(item['text'] for item in task['context']['sources']
                                                      if item['id'] == 'diff'))
        self.assertEqual(report['draft'], {'title': 'Retry flaky uploads', 'body': DESCRIPTION_BODY})
        published = drafting.publish('dk-pr-description', admitted, report,
                                     publisher=self.publisher('dk-pr-description', admitted, ledger),
                                     audience=self.audience)
        self.assertEqual(published['status'], 'not_authorized')
        again = self.draft('dk-pr-description', admitted, ledger)
        self.assertEqual((again['status'], again['session'], again['result_ref']),
                         ('drafted', False, report['result_ref']))
        self.calls()
        self.assertEqual(self.writes(), [])

    def test_repository_template_governs_and_validation_needs_evidence(self):
        self.write('.github/pull_request_template.md', PR_TEMPLATE)
        admitted = self.admit('dk-pr-description')
        ledger = self.ledger(admitted)
        body = '## Summary\n\nRetry each upload up to three times.\n\n## Testing\n\nAll tests pass.\n'
        fabricated = self.draft('dk-pr-description', admitted, ledger,
                                session(output(body=body, template='.github/pull_request_template.md')))
        self.assertEqual((fabricated['status'], fabricated['error']['path']), ('blocked', 'body'))
        self.assertIn('validation', fabricated['error']['message'])
        omitted = self.draft('dk-pr-description', admitted, ledger, session(output(
            body='## Summary\n\nRetry each upload up to three times.\n', template='.github/pull_request_template.md',
            omitted=[{'section': 'Testing', 'reason': 'No validation evidence was supplied.'}])))
        self.assertEqual(omitted['status'], 'drafted', omitted)
        self.assertEqual(self.engine.sessions, Counter({'dk-pr-description': 2}))
        self.assertEqual(self.fake.tasks[0]['claim'], fabricated['claim'] + '-r1')
        # With actual validation evidence the section is supported.
        ran = {'id': 'unit', 'kind': 'validation', 'text': 'python3 -m unittest passed at ' + self.head_sha}
        cited = self.draft('dk-pr-description', admitted, ledger, session(output(
            body=body.replace('All tests pass.', 'The unit tests pass at the head commit.'),
            template='.github/pull_request_template.md',
            claims=[{'text': 'Unit tests pass.', 'sources': ['validation:unit']}])), facts=[ran])
        self.assertEqual(cited['status'], 'drafted', cited)
        missing = self.draft('dk-pr-description', admitted, ledger, session(output(body='Retry uploads.\n',
                             template='.github/pull_request_template.md')), facts=[ran, OUTAGE])
        self.assertEqual((missing['status'], missing['error']['path']), ('blocked', 'body'))

    def test_disabled_fields_quoted_wording_and_decisions_are_preserved(self):
        decided = 'Context.\n\n## Decisions\n\n- Keep three attempts. <!-- cf-workflows:decision attempts -->\n'
        self.github.ev['metadata']['body'] = decided
        admitted = self.admit('dk-pr-description', 'Describe it and keep the phrase "three attempts at most".',
                              options={'update_title': False})
        ledger = self.ledger(admitted)
        body = DESCRIPTION_BODY.replace('up to three times', 'three attempts at most')
        titled = self.draft('dk-pr-description', admitted, ledger, session(output(body=body)))
        self.assertEqual((titled['status'], titled['error']['path']), ('blocked', 'title'))
        dropped = self.draft('dk-pr-description', admitted, ledger, session(output(None, body)))
        self.assertEqual((dropped['status'], dropped['error']['path']), ('blocked', 'body'))
        self.assertIn('Decisions', dropped['error']['message'])
        kept = body + '\n## Decisions\n\n- Keep three attempts. <!-- cf-workflows:decision attempts -->\n'
        admitted = self.admit('dk-pr-description', 'Describe it; keep the phrase "three attempts at most".',
                              options={'update_title': False})
        ledger = self.ledger(admitted)
        reworded = self.draft('dk-pr-description', admitted, ledger, session(output(
            None, kept.replace('three attempts at most', 'at most three attempts'))))
        self.assertEqual((reworded['status'], reworded['error']['path']), ('blocked', 'constraints'))
        accepted = self.draft('dk-pr-description', admitted, ledger, session(output(None, kept)))
        self.assertEqual(accepted['status'], 'drafted', accepted)
        self.assertEqual((accepted['draft']['title'], accepted['draft']['body']), ('WIP', kept))
        self.assertEqual(self.engine.sessions, Counter({'dk-pr-description': 4}))

    def test_authorized_update_reconciles_lost_responses_and_never_duplicates(self):
        admitted = self.admit('dk-pr-description', authorize=('edit_pr',))
        ledger = self.ledger(admitted)
        report = self.draft('dk-pr-description', admitted, ledger, session(output()))
        publisher = self.publisher('dk-pr-description', admitted, ledger)
        self.github.fault = 'after'  # The edit happens; its response is lost.
        lost = drafting.publish('dk-pr-description', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(lost['status'], 'pending', lost)
        self.github.fault = None
        settled = drafting.publish('dk-pr-description', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(settled['status'], 'applied', settled)
        again = drafting.publish('dk-pr-description', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(again['status'], 'applied')
        self.assertEqual([call[0] for call in self.writes()], ['PATCH'])
        self.assertEqual(self.github.ev['metadata']['body'], DESCRIPTION_BODY)
        # Publication changed the current title and body; the draft it came from is still current.
        resumed = self.draft('dk-pr-description', admitted, ledger)
        self.assertEqual((resumed['session'], resumed['result_ref']), (False, report['result_ref']))
        self.calls()

    def test_edits_after_the_draft_are_never_overwritten(self):
        admitted = self.admit('dk-pr-description', authorize=('edit_pr',))
        ledger = self.ledger(admitted)
        report = self.draft('dk-pr-description', admitted, ledger, session(output()))
        self.assertEqual((report['seen'], report['fields']),
                         ({'title': 'WIP', 'body': 'Old description'}, ['title', 'body']))
        self.github.ev['metadata']['body'] = 'The author rewrote the description.'
        publisher = self.publisher('dk-pr-description', admitted, ledger)
        stale = drafting.publish('dk-pr-description', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual((stale['status'], stale['error']['message']), ('stale', 'changed since the draft: body'))
        self.assertEqual((self.github.ev['metadata']['body'], self.writes()),
                         ('The author rewrote the description.', []))
        redrafted = self.draft('dk-pr-description', admitted, ledger, session(output()))
        self.assertTrue(redrafted['session'])
        self.assertEqual(drafting.publish('dk-pr-description', admitted, redrafted, publisher=publisher,
                                          audience=self.audience)['status'], 'applied')
        self.assertEqual(len(self.writes()), 1)

    def test_a_disabled_field_changed_after_the_draft_is_left_alone(self):
        admitted = self.admit('dk-pr-description', authorize=('edit_pr',), options={'update_title': False})
        ledger = self.ledger(admitted)
        report = self.draft('dk-pr-description', admitted, ledger, session(output(None)))
        self.assertEqual(report['fields'], ['body'])
        self.github.ev['metadata']['title'] = 'Retitled by the author'
        published = drafting.publish('dk-pr-description', admitted, report,
                                     publisher=self.publisher('dk-pr-description', admitted, ledger),
                                     audience=self.audience)
        self.assertEqual(published['status'], 'applied', published)
        self.assertEqual([call[2] for call in self.writes()], [{'body': DESCRIPTION_BODY}])
        self.assertEqual(self.github.ev['metadata']['title'], 'Retitled by the author')

    def test_lost_request_before_the_effect_stays_pending_and_is_never_resent(self):
        admitted = self.admit('dk-pr-description', authorize=('edit_pr',))
        ledger = self.ledger(admitted)
        report = self.draft('dk-pr-description', admitted, ledger, session(output()))
        publisher = self.publisher('dk-pr-description', admitted, ledger)
        self.github.fault = 'before'
        first = drafting.publish('dk-pr-description', admitted, report, publisher=publisher, audience=self.audience)
        self.github.fault = None
        second = drafting.publish('dk-pr-description', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual((first['status'], second['status']), ('pending', 'pending'))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.github.ev['metadata']['title'], 'WIP')

    def test_composed_dk_pr_fix_occurrence_uses_the_same_procedure(self):
        transport = self.transport()
        fix = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix this PR.'},
                                  configuration=self.configuration(), invocation=None, deployment=self.deployment,
                                  repository=self.repo, transport=transport, home=self.base / 'home')
        self.assertEqual(fix['preflight'], 'admitted', fix)
        self.assertIn('describe-pr', fix['inputs']['adapters'])
        report = self.draft('dk-pr-description', fix, self.ledger(fix), session(output()))
        self.assertEqual(report['status'], 'drafted', report)
        [task] = self.fake.tasks
        self.assertEqual((task['instructions'], task['role_prompt'], task['options']['fallback_sections']),
                         ('Fix this PR.', drafting.ENTRIES['dk-pr-description']['role_prompt'], ['Issue', 'Fix']))
        self.assertEqual(drafting.handoff(report)['body'], DESCRIPTION_BODY)
        self.assertEqual(drafting.publication_plan('dk-pr-description', fix)['options'],
                         {'permissions': {'edit_pr': False}})
        without = dk_pr_fix.preflight(request={'pr': PR, 'instructions': 'Fix.'}, configuration=self.configuration(),
                                      invocation={'options': {'tasks': ['address-feedback'], 'finalizers': []}},
                                      deployment=self.deployment, repository=self.repo, transport=self.transport(),
                                      home=self.base / 'home')
        outcome = drafting.draft('dk-pr-description', without, repository=self.repo, ledger=None,
                                 transport=self.transport(), capture=self.github.capture(None))
        self.assertEqual((outcome['status'], outcome['error']['path']), ('rejected', 'plan.tasks'))
        self.calls()


class Unsupported(Fixture):
    """Every unsupported claim rejects before it can be recorded or published."""

    def evidence(self, **inputs):
        admitted = self.admit('dk-pr-description', 'Describe it.')
        governing = drafting.effective('dk-pr-description', admitted)
        evidence = drafting.collect('dk-pr-description', admitted, repository=self.repo,
                                    capture=self.github.capture(None), **inputs)
        return evidence, governing

    def test_fabricated_references_links_and_attribution_reject(self):
        evidence, governing = self.evidence(issues=[{'number': 3, 'title': 'Uploads fail', 'body': 'Flaky.',
                                                     'url': 'https://github.com/o/r/issues/3'}])
        cases = {
            'unknown issue': 'See #99 for context.',
            'closing an unlinked issue': 'Fixes #7.',
            'unknown issue URL': 'See https://github.com/o/r/issues/42.',
            'branch code link': 'See https://github.com/o/r/blob/main/src/upload.py#L2.',
            'path and line': 'See src/upload.py:2.',
            'tool banner': 'Generated with an assistant.',
            'template comment': '<!-- How was this tested? -->',
        }
        for name, sentence in cases.items():
            with self.subTest(name):
                body = DESCRIPTION_BODY + '\n' + sentence + '\n'
                with self.assertRaises(drafting.Blocked) as caught:
                    drafting.check_result('dk-pr-description', output(body=body), evidence, governing)
                self.assertEqual(caught.exception.path, 'body')
        pinned = (DESCRIPTION_BODY + f'\nFixes #3. See https://github.com/o/r/blob/{self.head_sha}/src/upload.py#L2.\n')
        accepted = drafting.check_result('dk-pr-description', output(body=pinned), evidence, governing)
        self.assertEqual(accepted['draft']['body'], pinned)
        unknown = output(claims=[{'text': 'Benchmarks improved.', 'sources': ['benchmark']}])
        with self.assertRaises(admission.AdmissionError):
            drafting.check_result('dk-pr-description', unknown, evidence, governing)

    def test_the_author_s_existing_sections_are_not_fabrication(self):
        self.github.ev['metadata']['body'] = 'Context.\n\n## Testing\n\nRan the uploader by hand.\n'
        evidence, governing = self.evidence()
        body = DESCRIPTION_BODY + '\n## Testing\n\nRan the uploader by hand.\n'
        self.assertEqual(drafting.check_result('dk-pr-description', output(body=body), evidence,
                                               governing)['draft']['body'], body)


# -- dk-commit-message -------------------------------------------------------------------------------

class CommitMessage(Fixture):
    def setUp(self):
        super().setUp()
        self.write('tests/test_upload.py', 'def test_upload():\n    pass\n')
        git(self.repo, 'add', 'tests/test_upload.py')
        git(self.repo, 'commit', '-q', '-m', 'Add upload test')
        self.write('src/upload.py', 'def upload(data, attempts=3):\n    return send(data)\n')
        git(self.repo, 'add', 'src/upload.py')
        self.write('tests/test_upload.py', 'def test_upload():\n    assert True\n')
        self.write('notes.txt', 'scratch\n')

    def state(self):
        git_dir = self.repo / '.git'
        return {'index': (git_dir / 'index').read_bytes(), 'head': git(self.repo, 'rev-parse', 'HEAD'),
                'refs': git(self.repo, 'for-each-ref'), 'status': git(self.repo, 'status', '--porcelain=v1'),
                'staged': git(self.repo, 'diff', '--cached')}

    def admitted(self, instructions='Draft a commit message.', **extra):
        admitted = self.admit('dk-commit-message', instructions, target={'repository': REPOSITORY}, **extra)
        self.assertEqual(admitted['preflight'], 'admitted', admitted)
        return admitted, self.ledger(admitted)

    def test_draft_accounts_for_every_change_and_changes_no_git_state(self):
        before = self.state()
        admitted, ledger = self.admitted()
        report = self.draft('dk-commit-message', admitted, ledger, session(message()))
        self.assertEqual(report['status'], 'drafted', report)
        self.assertEqual(self.state(), before)
        [task] = self.fake.tasks
        self.assertEqual(task['context']['changed_paths'], ['notes.txt', 'src/upload.py', 'tests/test_upload.py'])
        self.assertEqual(task['context']['identity'], 'Ada Lovelace <ada@example.invalid>')
        sources = {item['id']: item for item in task['context']['sources']}
        self.assertIn('+def upload(data, attempts=3):', sources['diff:staged']['text'])
        self.assertIn('+    assert True', sources['diff:unstaged']['text'])
        self.assertEqual(sources['untracked']['text'], 'notes.txt')
        self.assertIn('Add upload test', sources['history']['text'])
        self.assertEqual(drafting.handoff(report)['scope']['excluded'],
                         [{'path': 'notes.txt', 'reason': 'Unrelated notes.'}])
        self.assertEqual(drafting.propose('dk-commit-message', admitted, report)['status'], 'not_published')
        reused = self.draft('dk-commit-message', admitted, ledger)
        self.assertFalse(reused['session'])
        self.calls()
        self.assertEqual(self.state(), before)

    def test_unsupported_messages_reject(self):
        long = 'Retry transient upload failures in the uploader so flaky networks stop failing every release'
        cases = {
            'unaccounted path': message(excluded=()),
            'invented path': message(included=('src/upload.py', 'tests/test_upload.py', 'docs/x.md')),
            'long subject': message(long + '\n'),
            'tool co-author': message('Retry uploads\n\nCo-authored-by: Claude <noreply@anthropic.com>\n'),
            'other signer': message('Retry uploads\n\nSigned-off-by: Eve <eve@example.invalid>\n'),
            'unsupported testing': message('Retry uploads\n\nTested: make test passes.\n'),
            'unknown issue': message('Retry uploads\n\nRefs #12.\n'),
        }
        for name, result in cases.items():
            with self.subTest(name):
                admitted, ledger = self.admitted(f'Draft a commit message ({name}).')
                report = self.draft('dk-commit-message', admitted, ledger, session(result))
                self.assertEqual(report['status'], 'blocked', (name, report))
        admitted, ledger = self.admitted('Draft a signed commit message.')
        signed = self.draft('dk-commit-message', admitted, ledger, session(message(
            'Retry uploads\n\nSigned-off-by: Ada Lovelace <ada@example.invalid>\n')))
        self.assertEqual(signed['status'], 'drafted', signed)

    def test_policy_replaces_fallbacks_and_validation_needs_evidence(self):
        long = 'Retry transient upload failures in the uploader so flaky networks stop failing every release'
        admitted, ledger = self.admitted()
        policy = 'Use one descriptive subject line; length is not limited.'
        report = self.draft('dk-commit-message', admitted, ledger, session(message(long + '\n')), policy=policy)
        self.assertEqual(report['status'], 'drafted', report)
        self.assertIn('policy', {item['id'] for item in self.fake.tasks[0]['context']['sources']})
        ran = {'id': 'make', 'kind': 'validation', 'text': 'make test passed with the staged changes.'}
        tested = self.draft('dk-commit-message', admitted, ledger, session(message(
            'Retry uploads\n\nTested: make test passes.\n',
            claims=[{'text': 'make test passes.', 'sources': ['validation:make']}])), facts=[ran])
        self.assertEqual(tested['status'], 'drafted', tested)
        self.assertEqual(self.engine.sessions, Counter({'dk-commit-message': 2}))

    def test_lost_session_response_reconciles_by_claim(self):
        admitted, ledger = self.admitted()
        lost = self.draft('dk-commit-message', admitted, ledger, session(message()), lose=1)
        self.assertEqual((lost['status'], lost['error']['path']), ('blocked', 'session'))
        recovered = self.draft('dk-commit-message', admitted, ledger)
        self.assertEqual((recovered['status'], recovered['session']), ('drafted', True))
        self.assertEqual(self.engine.sessions, Counter({'dk-commit-message': 1}))

    def test_composed_occurrence_hands_the_message_to_its_committer(self):
        document = {**configuration.empty_configuration(), 'scopes': [{
            'id': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY},
            'options': {'tasks': ['address-feedback', 'draft-commit-message'], 'finalizers': []},
            'permissions': {}, 'prompts': [], 'skills': [], 'preferences': []}]}
        admitted = semantic.admit(request={'pr': PR, 'instructions': 'Fix the PR.'}, configuration=document,
                                  invocation=None, deployment=self.deployment, roles=dk_pr_fix.role_catalog(),
                                  roots={'repository': self.repo, dk_pr_fix.INSTALL_ROOT: dk_pr_fix.INSTALL},
                                  transport=self.transport())
        self.assertEqual(admitted['preflight'], 'admitted', admitted)
        before = self.state()
        report = self.draft('dk-commit-message', admitted, self.ledger(admitted), session(message()),
                            options={'subject_target_length': 50})
        self.assertEqual(report['status'], 'drafted', report)
        self.assertEqual(self.fake.tasks[0]['options']['subject_target_length'], 50)
        self.assertEqual(drafting.handoff(report)['message'], message()['message'].strip('\n'))
        self.assertEqual(self.state(), before)


# -- dk-issue ----------------------------------------------------------------------------------------

class Issue(Fixture):
    def admitted(self, instructions='File an issue about upload failures.', authorize=(), target=None, **extra):
        admitted = self.admit('dk-issue', instructions, target=target or {'repository': REPOSITORY},
                              authorize=authorize, **extra)
        self.assertEqual(admitted['preflight'], 'admitted', admitted)
        return admitted, self.ledger(admitted)

    def test_fallback_draft_with_verified_metadata_and_no_write(self):
        admitted, ledger = self.admitted(options={'labels': ['bug', 'triage'], 'assignees': ['alice']})
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE])
        self.assertEqual(report['status'], 'drafted', report)
        self.assertEqual((report['draft']['labels'], report['draft']['assignees'], report['draft']['metadata']),
                         (['bug'], ['alice'], {'labels': 'configuration', 'assignees': 'configuration'}))
        self.assertEqual(report['notes'], ['configured labels not in the target repository were left out: triage'])
        self.assertEqual(self.github.listed, Counter({'repos/o/r/labels': 1, 'repos/o/r/assignees': 1}))
        published = drafting.publish('dk-issue', admitted, report,
                                     publisher=self.publisher('dk-issue', admitted, ledger), audience=self.audience)
        self.assertEqual(published['status'], 'not_authorized')
        self.assertEqual(self.writes(), [])

    def test_requested_metadata_takes_precedence_and_missing_names_block_before_a_session(self):
        admitted, ledger = self.admitted(options={'labels': ['bug']})
        missing = self.draft('dk-issue', admitted, ledger, facts=[OUTAGE], metadata={'labels': ['regression']})
        self.assertEqual((missing['status'], missing['error']['path']), ('blocked', 'metadata.labels'))
        self.assertEqual(self.engine.sessions, Counter())
        self.calls()
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE],
                            metadata={'labels': ['flaky-test'], 'assignees': []})
        self.assertEqual((report['draft']['labels'], report['draft']['metadata']['labels']),
                         (['flaky-test'], 'request'))
        unverified = self.draft('dk-issue', admitted, ledger, facts=[OUTAGE], github=None,
                                metadata={'labels': ['bug']})
        self.assertEqual((unverified['status'], unverified['error']['path']), ('blocked', 'github'))

    def test_unconfigured_operations_fail_without_a_live_fallback(self):
        admitted, ledger = self.admitted()
        unknown = self.draft('dk-issue', admitted, ledger, facts=[OUTAGE], issue=404)
        self.assertEqual((unknown['status'], unknown['error']['path']), ('blocked', 'evidence'))
        unscripted = self.draft('dk-issue', admitted, ledger, facts=[OUTAGE])
        self.assertEqual((unscripted['status'], unscripted['error']['path']), ('blocked', 'session'))
        self.calls(native_session=1)
        self.assertEqual((self.engine.sessions, self.writes()), (Counter(), []))

    def test_issue_form_template_and_reproduction_need_evidence(self):
        self.write('.github/ISSUE_TEMPLATE/bug.yml', ISSUE_FORM)
        self.write('.github/ISSUE_TEMPLATE/config.yml', 'blank_issues_enabled: false\n')
        admitted, ledger = self.admitted()
        steps = '### What happened?\n\nUploads failed.\n\n### Steps to reproduce\n\n1. Drop the network.\n'
        fabricated = self.draft('dk-issue', admitted, ledger, session(issue(body=steps,
                                template='.github/ISSUE_TEMPLATE/bug.yml')), facts=[OUTAGE])
        self.assertEqual((fabricated['status'], fabricated['error']['path']), ('blocked', 'body'))
        self.assertIn('reproduction', fabricated['error']['message'])
        fallback = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE])
        self.assertEqual((fallback['status'], fallback['error']['path']), ('blocked', 'template'))
        observed = {'id': 'steps', 'kind': 'reproduction', 'text': '1. Disable the network for one second.'}
        report = self.draft('dk-issue', admitted, ledger, session(issue(
            body=steps, template='.github/ISSUE_TEMPLATE/bug.yml',
            claims=[{'text': 'Disabling the network reproduces it.', 'sources': ['reproduction:steps']}])),
            facts=[OUTAGE, observed])
        self.assertEqual(report['status'], 'drafted', report)
        self.assertEqual([item['path'] for item in self.fake.tasks[0]['context']['templates']],
                         ['.github/ISSUE_TEMPLATE/bug.yml'])

    def test_creation_reconciles_uncertainty_and_never_duplicates(self):
        admitted, ledger = self.admitted(authorize=('create_issue',), options={'labels': ['bug']})
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE])
        publisher = self.publisher('dk-issue', admitted, ledger)
        self.github.fault = 'after'  # The issue is created; the response is lost.
        lost = drafting.publish('dk-issue', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(lost['status'], 'pending', lost)
        self.github.fault = None
        settled = drafting.publish('dk-issue', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(settled['status'], 'applied', settled)
        self.assertEqual(settled['receipt']['result']['number'], 100)
        self.assertEqual(settled['receipt']['repository'], REPOSITORY)
        again = drafting.publish('dk-issue', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(again['status'], 'applied')
        self.assertEqual([call[:2] for call in self.writes()], [('POST', 'repos/o/r/issues')])
        self.assertEqual([item['labels'] for item in self.github.issues], [[{'name': 'bug'}]])
        resumed = self.draft('dk-issue', admitted, ledger, facts=[OUTAGE])
        self.assertFalse(resumed['session'])
        self.calls()

    def test_lost_request_before_creation_stays_pending_and_is_never_resent(self):
        admitted, ledger = self.admitted(authorize=('create_issue',))
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE])
        publisher = self.publisher('dk-issue', admitted, ledger)
        self.github.fault = 'before'
        first = drafting.publish('dk-issue', admitted, report, publisher=publisher, audience=self.audience)
        self.github.fault = None
        second = drafting.publish('dk-issue', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual((first['status'], second['status']), ('pending', 'pending'))
        self.assertEqual((len(self.writes()), self.github.issues), (1, []))

    def test_an_identical_existing_issue_settles_without_a_write(self):
        self.github.add_issue('Uploads fail on transient network errors', ISSUE_BODY)
        admitted, ledger = self.admitted(authorize=('create_issue',))
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE])
        published = drafting.publish('dk-issue', admitted, report,
                                     publisher=self.publisher('dk-issue', admitted, ledger), audience=self.audience)
        self.assertEqual((published['status'], published['receipt']['result']['number']), ('applied', 100))
        self.assertEqual(self.writes(), [])

    def test_metadata_removed_after_preparation_blocks_the_write(self):
        admitted, ledger = self.admitted(authorize=('create_issue',), options={'labels': ['flaky-test']})
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE])
        publisher = self.publisher('dk-issue', admitted, ledger)
        planned = drafting.propose('dk-issue', admitted, report)
        intent = publisher.prepare(planned['proposal'])
        self.github.labels = [{'name': 'bug'}]
        with self.assertRaisesRegex(PublicationError, 'labels unavailable'):
            publisher.execute(intent['id'], self.audience(intent))
        self.assertEqual(publisher.events(intent['id'])[-1]['event'], 'blocked')
        self.assertEqual(self.writes(), [])

    def test_issue_update_uses_the_current_issue_and_refuses_concurrent_edits(self):
        existing = self.github.add_issue('Uploads flaky', 'Old text.', labels=['bug'])
        admitted, ledger = self.admitted('Update #100 with what we know.', authorize=('edit_issue',))
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE], issue=100)
        self.assertEqual(report['status'], 'drafted', report)
        self.assertNotIn('labels', report['draft'])  # Unrequested metadata is left alone.
        self.assertIn('issue:100', {item['id'] for item in self.fake.tasks[0]['context']['sources']})
        publisher = self.publisher('dk-issue', admitted, ledger)
        planned = drafting.propose('dk-issue', admitted, report, current=copy.deepcopy(existing))
        self.assertEqual(planned['proposal']['kind'], 'edit_issue')
        intent = publisher.prepare(planned['proposal'])
        existing['body'] = 'Someone else edited this.'
        with self.assertRaisesRegex(PublicationError, 'changed after preparation'):
            publisher.execute(intent['id'], self.audience(intent))
        self.assertEqual(self.writes(), [])
        existing['body'] = 'Old text.'
        admitted, ledger = self.admitted('Update #100 with what we know, again.', authorize=('edit_issue',))
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE], issue=100)
        published = drafting.publish('dk-issue', admitted, report,
                                     publisher=self.publisher('dk-issue', admitted, ledger), audience=self.audience)
        self.assertEqual(published['status'], 'applied', published)
        self.assertEqual((existing['title'], existing['body'], existing['labels']),
                         ('Uploads fail on transient network errors', ISSUE_BODY, [{'name': 'bug'}]))
        self.assertEqual([call[:2] for call in self.writes()], [('PATCH', 'repos/o/r/issues/100')])

    def test_an_edit_between_draft_and_publication_is_never_overwritten(self):
        existing = self.github.add_issue('Uploads flaky', 'Old text.')
        admitted, ledger = self.admitted('Update #100 with what we know.', authorize=('edit_issue',))
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE], issue=100)
        self.assertEqual(report['seen'], {'title': 'Uploads flaky', 'body': 'Old text.'})
        existing['body'] = 'A maintainer added a workaround after the draft.'
        publisher = self.publisher('dk-issue', admitted, ledger)
        stale = drafting.publish('dk-issue', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual((stale['status'], stale['error']['path']), ('stale', 'publication'))
        self.assertIn('body', stale['error']['message'])
        self.assertEqual((existing['body'], self.writes()), ('A maintainer added a workaround after the draft.', []))
        # The edit is new evidence: drafting again reads it, and that draft publishes.
        redrafted = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE], issue=100)
        self.assertTrue(redrafted['session'])
        self.assertIn('workaround', next(item['text'] for item in self.fake.tasks[0]['context']['sources']
                                         if item['id'] == 'issue:100'))
        published = drafting.publish('dk-issue', admitted, redrafted, publisher=publisher, audience=self.audience)
        self.assertEqual(published['status'], 'applied', published)
        self.assertEqual([call[:2] for call in self.writes()], [('PATCH', 'repos/o/r/issues/100')])

    def test_an_edit_inside_preparation_never_becomes_the_baseline(self):
        existing = self.github.add_issue('Uploads flaky', 'Old text.')
        admitted, ledger = self.admitted('Update #100 with what we know.', authorize=('edit_issue',))
        report = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE], issue=100)
        publisher = self.publisher('dk-issue', admitted, ledger)
        prepare = publisher.prepare

        def edited_first(proposal):  # The maintainer edits after the live check, before preparation reads.
            existing['body'] = 'Edited in between.'
            return prepare(proposal)
        publisher.prepare = edited_first
        stale = drafting.publish('dk-issue', admitted, report, publisher=publisher, audience=self.audience)
        self.assertEqual(stale['status'], 'stale', stale)
        self.assertEqual(publisher.events(stale['intent'])[-1]['event'], 'prepared')  # Never dispatched.
        self.assertEqual((existing['body'], self.writes()), ('Edited in between.', []))

    def test_composed_pr_occurrence_shares_the_pr_run_journal(self):
        document = {**configuration.empty_configuration(), 'scopes': [{
            'id': 'repo', 'selector': {'kind': 'repository', 'id': REPOSITORY},
            'options': {'tasks': ['describe-pr', 'draft-issue', {'stage': 'publication'}], 'finalizers': []},
            'permissions': {'edit_pr': True, 'create_issue': True}, 'prompts': [], 'skills': [],
            'preferences': []}]}
        admitted = semantic.admit(
            request={'pr': PR, 'instructions': 'Describe the PR and file the follow-up.',
                     'authorizations': {action: GRANTS[action] for action in ('edit_pr', 'create_issue')}},
            configuration=document, invocation=None, deployment=self.deployment, roles=dk_pr_fix.role_catalog(),
            roots={'repository': self.repo, dk_pr_fix.INSTALL_ROOT: dk_pr_fix.INSTALL}, transport=self.transport())
        self.assertEqual(admitted['preflight'], 'admitted', admitted)
        ledger = self.ledger(admitted)
        described = self.draft('dk-pr-description', admitted, ledger, session(output()))
        filed = self.draft('dk-issue', admitted, ledger, session(issue()), facts=[OUTAGE])
        self.assertEqual((described['status'], filed['status']), ('drafted', 'drafted'))
        for name, report in (('dk-pr-description', described), ('dk-issue', filed)):
            result = drafting.publish(name, admitted, report, publisher=self.publisher(name, admitted, ledger),
                                      audience=self.audience)
            self.assertEqual(result['status'], 'applied', result)
        self.assertEqual([call[:2] for call in self.writes()], [('PATCH', 'repos/o/r/pulls/7'),
                                                                ('POST', 'repos/o/r/issues')])
        runs = RunLedger(self.base / 'checkout', admitted['snapshot']['request']['logical_run'], PR)
        self.addCleanup(runs.close)
        kinds = [row[0] for row in runs.db.execute(
            "SELECT json_extract(value, '$.kind') FROM publication_intents ORDER BY rowid")]
        self.assertEqual(kinds, ['edit_pr', 'create_issue'])  # One journal for both kinds.


class IssuePublication(Fixture):
    """The shared publisher's issue kinds at unit level."""

    def world(self, permissions=('create_issue', 'edit_issue')):
        root = self.base / 'unit'
        root.mkdir(exist_ok=True)
        ledger = RunLedger(root, 'issues')
        self.addCleanup(ledger.close)
        plan = {'logical_run': 'issues', 'pr': None, 'repository': {'host': 'github.com', 'owner': 'o', 'name': 'r'},
                'authorizations': {action: GRANTS[action] for action in ISSUE_KINDS},
                'options': {'permissions': {action: action in permissions for action in ISSUE_KINDS}}}
        return Publication(ledger, self.github, root, plan, source_validator=lambda source: source == 'accepted:x')

    def proposal(self, kind='create_issue', **payload):
        body = {'title': 'Uploads fail', 'body': 'Text.', 'labels': [], 'assignees': []}
        body.update(payload)
        return {'id': 'unit-' + kind, 'kind': kind, 'payload': body, 'sources': ['accepted:x']}

    def test_payloads_permissions_and_repository_plans_are_bounded(self):
        publisher = self.world()
        for proposal, message in (
                (self.proposal(extra=1), 'exactly'),
                (self.proposal(title='Two\nlines'), 'one bounded line'),
                (self.proposal(assignees=['not a login']), 'assignee'),
                (self.proposal(labels=['bug', 'bug']), 'distinct'),
                ({**self.proposal(), 'head': 'a' * 40}, 'not a PR head'),
                (self.proposal('edit_issue'), 'number'),
                ({'id': 'x', 'kind': 'edit_pr', 'head': 'a' * 40, 'target': 'b' * 40, 'payload': {'body': 'x'},
                  'sources': ['accepted:x']}, 'authorization required'),
                (self.proposal(labels=['missing']), 'labels unavailable')):
            with self.subTest(message), self.assertRaisesRegex(PublicationError, message):
                publisher.prepare(proposal)
        with self.assertRaisesRegex(PublicationError, 'permission forbids'):
            self.world(permissions=()).prepare(self.proposal())
        self.assertEqual(self.writes(), [])

    def test_two_intents_never_settle_on_one_created_issue(self):
        publisher = self.world()
        first = publisher.prepare(self.proposal())
        self.assertEqual(publisher.execute(first['id'], self.audience(first))['result']['number'], 100)
        other = publisher.prepare({**self.proposal(), 'id': 'unit-other'})
        self.assertEqual(other['intent']['before']['satisfied_issue'], 100)
        with self.assertRaisesRegex(PublicationError, 'duplicate receipt'):
            publisher.execute(other['id'], self.audience(other))
        self.assertEqual(len(self.writes()), 1)


if __name__ == '__main__':
    unittest.main()
