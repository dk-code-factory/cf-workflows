"""PR descriptions, commit messages and issues through the installed dk-pr-fix helpers (#32).

Each case uses a disposable repository, a fresh state namespace and run ledger,
and the fail-closed fake GitHub of the publication-entry tests, extended with
issue, label and assignee routes: any route it does not model raises instead of
reaching a live service, and writes are counted. The installed helpers run
in-process, as the helper containers would run them for task.yml and the
publication stage. Drafting and audience sessions are only the reports a test
hands the helpers, and every claim that would dispatch one is counted. Nothing
here starts an engine, provider or host, runs the YAML graph or certifies
GitHub conformance.
"""
import ast
from collections import Counter
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import app, drafting, publishing
from prfix.core import Ledger, read_artifact
from prfix.prepare import prepare
from test_dk_pr_review import AGENTS, PR_URL
from test_publication_skills import GitHub as PublicationGitHub, Helpers, grant

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, drafting as library  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import CAPABILITIES, FakeTransport, PreflightFixture  # noqa: E402

INSTALL = ROOT / 'cf_workflows' / 'workflows' / 'dk-pr-fix'
DESCRIPTION = '## Issue\n\nThe value was stale.\n\n## Fix\n\nSet the value to 2 and add an extra value.\n'
ISSUE = '## Problem\n\nThe extra value has no test.\n\n## Expected behavior\n\nA test covers the extra value.\n'


def git(root, *args):
    return subprocess.run(git_fixture.command(*args), cwd=root, capture_output=True, check=True,
                          env=git_fixture.environment(), timeout=30).stdout.decode().strip()


def draft(title='Set the value to 2', body=DESCRIPTION, *, template=None, omitted=(), claims=None):
    return {'title': title, 'body': body, 'template': template, 'omitted': list(omitted),
            'claims': claims or [{'text': 'The value changes to 2.', 'sources': ['diff']}],
            'questions': [], 'summary': 'Drafted from the diff.'}


def message(text='Set the value to 2\n\nThe extra value is added as well.\n', included=('code.py',), excluded=()):
    return {'message': text, 'scope': {'included': list(included),
                                       'excluded': [{'path': p, 'reason': r} for p, r in excluded]},
            'claims': [{'text': 'The value changes.', 'sources': ['diff:unstaged']}], 'questions': [],
            'summary': 'Drafted from the local changes.'}


class GitHub(PublicationGitHub):
    """Adds issue, label, assignee, actor and repository routes; any other route still fails."""

    def __init__(self, head, target, **kwargs):
        super().__init__(head, target, **kwargs)
        self.labels = [{'name': 'bug'}, {'name': 'tests'}]
        self.assignees = [{'login': 'maintainer'}, {'login': 'alice'}]
        self.issues = []
        self.listed = Counter()

    def pages(self, path):
        self.listed[path.split('?')[0]] += 1
        if path == 'repos/owner/repo/labels?per_page=100':
            return copy.deepcopy(self.labels)
        if path == 'repos/owner/repo/assignees?per_page=100':
            return copy.deepcopy(self.assignees)
        match = re.fullmatch(r'repos/owner/repo/issues\?state=all&creator=([A-Za-z0-9-]+)&per_page=100', path)
        if match:
            return copy.deepcopy([item for item in self.issues if item['user']['login'] == match[1]])
        raise AssertionError(('unconfigured pages', path))

    def add_issue(self, title, body, labels=(), assignees=()):
        number = 100 + len(self.issues)
        self.issues.append({'number': number, 'id': 9000 + number, 'title': title, 'body': body, 'state': 'open',
                            'labels': [{'name': name} for name in labels],
                            'assignees': [{'login': login} for login in assignees],
                            'user': copy.deepcopy(self.evidence['viewer']),
                            'html_url': f'https://github.com/owner/repo/issues/{number}'})
        return self.issues[-1]

    def request(self, method, path, payload=None):
        match = re.fullmatch(r'repos/owner/repo/issues/([0-9]+)', path)
        if (method, path) == ('GET', 'user'):
            return copy.deepcopy(self.evidence['viewer'])
        if (method, path) == ('GET', 'repos/owner/repo'):
            return {'id': 99, 'full_name': 'owner/repo'}
        if match and method == 'GET':
            return copy.deepcopy(next(item for item in self.issues if item['number'] == int(match[1])))
        if match and method == 'PATCH':
            self.calls['write'] += 1
            if self.fault == 'before':
                raise OSError('fixture response lost before effect')
            issue = next(item for item in self.issues if item['number'] == int(match[1]))
            issue.update({key: value for key, value in payload.items() if key in ('title', 'body')})
            self._lose()
            return copy.deepcopy(issue)
        if (method, path) == ('POST', 'repos/owner/repo/issues'):
            self.calls['write'] += 1
            if self.fault == 'before':
                raise OSError('fixture response lost before effect')
            created = self.add_issue(payload['title'], payload['body'], payload['labels'], payload['assignees'])
            self._lose()
            return copy.deepcopy(created)
        return super().request(method, path, payload)


class Fixture(Helpers):
    def setUp(self):
        super().setUp()
        self.github = GitHub(self.commit, self.target)
        self.github.git_head = self.commit
        self.github.evidence['metadata']['user'] = {'id': 1, 'login': 'maintainer'}  # The actor's own PR.
        self.sessions = Counter()

    def drafting_plan(self, tasks, *, entry=None, authorizations=None, permissions=None, settings=None,
                      run='draft_fixture', instructions='Describe this PR.'):
        table = {name: dict(admission.BUNDLED_ADAPTERS[name])
                 for name in ('describe-pr', 'draft-issue', 'draft-commit-message', 'read-feedback')}
        controls = {'tasks': tasks, 'finalizers': [], 'reviewers': [], 'adapters': table, 'accounts': [],
                    'settings': settings or {}, 'resources': [], 'turns': 8, 'mode': 'fix',
                    'permissions': permissions or {}}
        if entry is not None:
            controls['entry'] = entry  # description.yml, commit-message.yml or issue.yml (plugin.yaml).
        request = {'pr': PR_URL, 'instructions': instructions, 'logical_run': run,
                   'authorizations': authorizations or {}}
        result = prepare(self.root, request, controls, 'exec-1', self.github, self.definitions)
        return read_artifact(self.root, result['plan'])

    def claim(self, plan, ledger, skill):
        claimed = app.prepare_claim(self.root, self.github, ledger, plan, {'task': self.task(plan, skill)['id']})
        if claimed['invoke']:
            self.sessions[skill] += 1  # One drafting session dispatched for this claim.
        return claimed

    def submit(self, plan, ledger, claimed, output, round_number=0, agent=AGENTS[0], **values):
        report = self.report(deliverables={'draft': output}, **values)
        return app.accept(self.root, self.github, ledger, plan,
                          {'claim': claimed['claim'], 'agent': agent, 'report': report, 'round': round_number})

    def drafted(self, plan, ledger, skill, output):
        claimed = self.claim(plan, ledger, skill)
        self.assertTrue(claimed['invoke'])
        accepted = self.submit(plan, ledger, claimed, output)
        self.assertTrue(accepted['complete'], accepted)
        return claimed

    def listed(self, plan, ledger):
        return publishing.publication_list(self.root, self.github, ledger, plan, {'kind': 'ordinary'})['items']


class Description(Fixture):
    def test_draft_is_checked_private_without_authority_and_reused_unchanged(self):
        plan = self.drafting_plan(['describe-pr', {'stage': 'publication'}], entry='dk-pr-description')
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'describe-pr')
        context = claimed['task_input']['drafting']
        self.assertEqual((context['entry'], context['skills'], context['writes'], context['publication']),
                         ('dk-pr-description', [], False, False))
        evidence = context['evidence']
        self.assertEqual([item['id'] for item in evidence['sources']],
                         ['instruction:request', 'guidance:AGENTS.md', 'diff', 'commits', 'pr:title', 'pr:body'])
        self.assertIn('+extra = 3', evidence['sources'][2]['text'])
        self.assertEqual((evidence['templates'], evidence['current']),
                         ([], {'title': 'Change', 'body': 'Change a value'}))
        self.assertTrue(self.submit(plan, ledger, claimed, draft())['complete'])
        # No edit_pr authority: the draft stays private and nothing is listed or written.
        self.assertEqual(self.listed(plan, ledger), [])
        task = self.task(plan, 'describe-pr')
        snapshot = app._snapshot(self.root, self.github, plan)
        self.assertEqual(drafting.proposal(self.root, self.github, ledger, plan, task, snapshot)['status'],
                         'not_authorized')
        # Unchanged inputs reuse the accepted draft without a session, also from a new connection.
        self.assertFalse(self.claim(plan, self.ledger(plan), 'describe-pr')['invoke'])
        self.assertEqual((self.sessions, self.writes()), (Counter({'describe-pr': 1}), 0))

    def test_unsupported_claims_get_a_same_session_correction(self):
        plan = self.drafting_plan(['describe-pr'], instructions='Describe this PR and say "value to 2" exactly.')
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'describe-pr')
        fabricated = self.submit(plan, ledger, claimed, draft(body=DESCRIPTION + '\n## Testing\n\nAll tests pass.\n'))
        self.assertFalse(fabricated['complete'])
        self.assertIn('validation', fabricated['feedback']['contract_error'])
        # A report that proposes operations, or drops the quoted wording, is corrected the same way.
        operation = json.dumps({'kind': 'edit_pr', 'payload': {'title': 'x'}})
        result = self.submit(plan, ledger, claimed, draft(), round_number=1, operations=[operation])
        self.assertIn('no operations', result['feedback']['contract_error'])
        result = self.submit(plan, ledger, claimed, draft(title='Change the value',
                                                          body=DESCRIPTION.replace('value to 2', 'value')),
                             round_number=2)
        self.assertIn('value to 2', result['feedback']['contract_error'])
        accepted = self.submit(plan, ledger, claimed, draft(), round_number=3)
        self.assertTrue(accepted['complete'], accepted)
        self.assertEqual(self.sessions, Counter({'describe-pr': 1}))

    def test_each_unsupported_draft_rejects(self):
        cases = [
            (draft(body=DESCRIPTION + '\n## Testing\n\nAll tests pass.\n'), 'validation'),
            (draft(body=DESCRIPTION.replace('## Fix', '## Fixes')), 'Fix'),
            (draft(body=DESCRIPTION + '\nCloses #12.\n'), '#12'),
            (draft(body=DESCRIPTION + '\nSee code.py:2.\n'), 'code.py:2'),
            (draft(body=DESCRIPTION + '\nGenerated by a tool.\n'), 'attribution'),
            (draft(claims=[{'text': 'Invented.', 'sources': ['ci']}]), 'unknown source'),
            (draft(body=DESCRIPTION + '\nSee https://github.com/owner/repo/blob/main/code.py.\n'), 'pinned'),
        ]
        for number, (output, error) in enumerate(cases):
            with self.subTest(error):
                plan = self.drafting_plan(['describe-pr'], run=f'reject_{number}')
                ledger = self.ledger(plan)
                result = self.submit(plan, ledger, self.claim(plan, ledger, 'describe-pr'), output)
                self.assertFalse(result['complete'])
                self.assertIn(error, result['feedback']['contract_error'])

    def test_closing_keywords_and_decisions_of_the_current_body_are_kept(self):
        # A body typed in the web UI: no trailing newline after the last (Decisions) section.
        current = DESCRIPTION + '\nFixes #12.\n\n## Decisions\n\n- Keep it. <!-- cf-workflows:decision keep -->'
        self.github.evidence['metadata']['body'] = current
        plan = self.drafting_plan(['describe-pr'])
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'describe-pr')
        self.assertEqual(claimed['task_input']['drafting']['evidence']['linked'], [12])
        accepted = self.submit(plan, ledger, claimed, draft(body=current))
        self.assertTrue(accepted['complete'], accepted)

    def test_repository_template_governs_and_quoted_wording_is_kept(self):
        (self.root / '.github').mkdir()
        (self.root / '.github' / 'pull_request_template.md').write_text('## Summary\n\n<!-- What? -->\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Add a PR template')
        self.github.evidence['metadata']['head']['sha'] = self.github.git_head = self.commit = git(
            self.root, 'rev-parse', 'HEAD')
        plan = self.drafting_plan(['describe-pr'], instructions='Describe it and keep "value to 2" as written.')
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'describe-pr')
        self.assertEqual(claimed['task_input']['drafting']['evidence']['constraints'], ['value to 2'])
        fallback = self.submit(plan, ledger, claimed, draft(title='Set the value to 2'))
        self.assertIn('repository templates', fallback['feedback']['contract_error'])
        accepted = self.submit(plan, ledger, claimed, draft(
            body='## Summary\n\nSet the value to 2.\n', template='.github/pull_request_template.md'),
            round_number=1)
        self.assertTrue(accepted['complete'], accepted)

    def test_authorized_edit_publishes_once_and_reconciles_lost_responses(self):
        for fault in ('before', 'after'):
            with self.subTest(fault=fault):
                self.setUp()
                plan = self.drafting_plan(['describe-pr', {'stage': 'publication'}], entry='dk-pr-description',
                                          authorizations={'edit_pr': grant('edit_pr')},
                                          permissions={'edit_pr': True}, run='edit_' + fault)
                ledger = self.ledger(plan)
                self.drafted(plan, ledger, 'describe-pr', draft())
                [item] = self.listed(plan, ledger)
                self.assertEqual((item['kind'], item['payload']), ('edit_pr', {'title': 'Set the value to 2',
                                                                               'body': DESCRIPTION}))
                self.github.fault = fault
                with self.assertRaises(Exception):
                    self.publish(plan, ledger, item)
                self.github.fault = None
                if fault == 'before':
                    # Pending after a lost request: only reconciled, never sent again here.
                    self.assertEqual(self.github.evidence['metadata']['title'], 'Change')
                    self.assertEqual(self.writes(), 1)
                    continue
                receipt = publishing.publication_recover(self.root, self.github, ledger, plan, {'id': item['id']})
                self.assertEqual(receipt['receipt']['result']['title'], 'Set the value to 2')
                # The draft stays current after its own publication: no new session, nothing listed.
                self.assertEqual(self.listed(plan, ledger), [])
                self.assertFalse(self.claim(plan, ledger, 'describe-pr')['invoke'])
                self.assertEqual((self.writes(), self.sessions['describe-pr']), (1, 1))

    def test_an_edit_after_the_draft_is_never_overwritten(self):
        plan = self.drafting_plan(['describe-pr', {'stage': 'publication'}], entry='dk-pr-description',
                                  authorizations={'edit_pr': grant('edit_pr')}, permissions={'edit_pr': True})
        ledger = self.ledger(plan)
        self.drafted(plan, ledger, 'describe-pr', draft())
        self.github.evidence['metadata']['title'] = 'Edited by the author'
        self.assertEqual(self.listed(plan, ledger), [])
        # The next occurrence drafts again from the new text.
        claimed = self.claim(plan, ledger, 'describe-pr')
        self.assertTrue(claimed['invoke'])
        self.assertEqual(claimed['task_input']['drafting']['evidence']['current']['title'], 'Edited by the author')
        self.assertEqual(self.writes(), 0)

    def test_a_disabled_title_is_kept(self):
        settings = {'drafting': {'dk-pr-description': {'update_title': False}}}
        plan = self.drafting_plan(['describe-pr', {'stage': 'publication'}], entry='dk-pr-description',
                                  authorizations={'edit_pr': grant('edit_pr')}, permissions={'edit_pr': True},
                                  settings=settings)
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'describe-pr')
        rejected = self.submit(plan, ledger, claimed, draft())
        self.assertIn('update_title is disabled', rejected['feedback']['contract_error'])
        self.assertTrue(self.submit(plan, ledger, claimed, draft(title=None), round_number=1)['complete'])
        [item] = self.listed(plan, ledger)
        self.assertEqual(item['payload'], {'body': DESCRIPTION})

    def test_composed_dk_pr_fix_occurrence_and_decisions_section_share_one_intent(self):
        self.github.evidence['metadata']['body'] = 'Change a value'
        plan = self.drafting_plan(['read-feedback', 'describe-pr', {'stage': 'publication'}],
                                  authorizations={'edit_pr': grant('edit_pr')}, permissions={'edit_pr': True},
                                  run='composed')
        ledger = self.ledger(plan)
        self.execute(plan, ledger, 'read-feedback', AGENTS[1])
        self.drafted(plan, ledger, 'describe-pr', draft())
        [item] = self.listed(plan, ledger)
        self.assertTrue(item['id'].startswith('description_'))
        self.publish(plan, ledger, item)
        self.assertEqual((self.github.evidence['metadata']['body'], self.writes()), (DESCRIPTION, 1))


class CommitMessage(Fixture):
    def test_entry_drafts_uncommitted_changes_and_changes_no_git_state(self):
        (self.root / 'code.py').write_text('value = 3\n')
        state = [git(self.root, *args) for args in (('rev-parse', 'HEAD'), ('status', '--porcelain'),
                                                     ('for-each-ref',), ('ls-files', '-s'))]
        plan = self.drafting_plan(['draft-commit-message'], entry='dk-commit-message',
                                  instructions='Draft a commit message for my changes.')
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'draft-commit-message')
        evidence = claimed['task_input']['drafting']['evidence']
        self.assertEqual((evidence['changes']['paths'], evidence['identity']), (['code.py'], 'Fixture <fixture@example.invalid>'))
        unaccounted = self.submit(plan, ledger, claimed, message(included=()))
        self.assertIn('at least one', unaccounted['feedback']['contract_error'])
        signed = message(text='Set the value to 3\n\nSigned-off-by: Someone <x@example.invalid>\n')
        self.assertIn('Signed-off-by', self.submit(plan, ledger, claimed, signed, round_number=1)
                      ['feedback']['contract_error'])
        self.assertTrue(self.submit(plan, ledger, claimed, message(), round_number=2)['complete'])
        self.assertEqual(self.listed(plan, ledger), [])
        self.assertEqual([git(self.root, *args) for args in (('rev-parse', 'HEAD'), ('status', '--porcelain'),
                                                              ('for-each-ref',), ('ls-files', '-s'))], state)
        self.assertEqual(self.writes(), 0)

    def test_a_conventional_test_subject_is_not_a_validation_section(self):
        (self.root / 'code.py').write_text('value = 3\n')
        plan = self.drafting_plan(['draft-commit-message'], entry='dk-commit-message',
                                  instructions='Draft a commit message for my changes.')
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'draft-commit-message')
        labelled = self.submit(plan, ledger, claimed, message(text='test: set the value to 3\n\nTested: by hand.\n'))
        self.assertIn('validation', labelled['feedback']['contract_error'])
        accepted = self.submit(plan, ledger, claimed, message(text='test: set the value to 3\n\nThe value is 3.\n'),
                               round_number=1)
        self.assertTrue(accepted['complete'], accepted)

    def test_only_its_entry_runs_the_adapter_and_other_plans_start_clean(self):
        (self.root / 'code.py').write_text('value = 3\n')
        with self.assertRaisesRegex(ValueError, 'only through cf-workflows:dk-commit-message'):
            self.drafting_plan(['draft-commit-message'])
        with self.assertRaisesRegex(ValueError, 'clean worktree'):
            self.drafting_plan(['describe-pr', {'stage': 'publication'}], entry='dk-pr-description')


class Issue(Fixture):
    def issue_plan(self, authorize=True, settings=None, run='issue_fixture'):
        return self.drafting_plan(
            ['draft-issue', {'stage': 'publication'}], entry='dk-issue', run=run, settings=settings,
            authorizations={'create_issue': grant('create_issue'), 'edit_issue': grant('edit_issue')}
            if authorize else {},
            permissions={'create_issue': True, 'edit_issue': True} if authorize else {},
            instructions='File an issue: the extra value has no test.')

    def issue_draft(self, **extra):
        extra.setdefault('claims', [{'text': 'The extra value has no test.', 'sources': ['instruction:request']}])
        return draft('Cover the extra value with a test', ISSUE, **extra)

    def test_creation_publishes_once_with_verified_metadata_and_reconciles_uncertainty(self):
        settings = {'drafting': {'dk-issue': {'labels': ['tests', 'missing'], 'metadata': {'assignees': ['alice']}}}}
        plan = self.issue_plan(settings=settings)
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'draft-issue')
        evidence = claimed['task_input']['drafting']['evidence']
        self.assertEqual(evidence['metadata'], {'labels': {'value': ['tests'], 'source': 'configuration'},
                                                'assignees': {'value': ['alice'], 'source': 'request'}})
        self.assertIn('missing', evidence['notes'][0])
        self.assertTrue(self.submit(plan, ledger, claimed, self.issue_draft())['complete'])
        [item] = self.listed(plan, ledger)
        self.assertEqual((item['kind'], item['payload']['labels'], item['payload']['assignees']),
                         ('create_issue', ['tests'], ['alice']))
        self.github.fault = 'after'
        with self.assertRaises(Exception):
            self.publish(plan, ledger, item)
        self.github.fault = None
        receipt = publishing.publication_recover(self.root, self.github, ledger, plan, {'id': item['id']})
        self.assertEqual(receipt['receipt']['result']['number'], 100)
        # Relisting the same draft only reconciles the applied intent: one issue, one write.
        for again in self.listed(plan, ledger):
            self.publish(plan, ledger, again)
        self.assertEqual((len(self.github.issues), self.writes(), self.sessions['draft-issue']), (1, 1, 1))

    def test_missing_requested_metadata_blocks_before_any_session(self):
        plan = self.issue_plan(settings={'drafting': {'dk-issue': {'metadata': {'labels': ['nope']}}}})
        ledger = self.ledger(plan)
        with self.assertRaisesRegex(ValueError, 'requested labels do not exist'):
            self.claim(plan, ledger, 'draft-issue')
        self.assertEqual((self.sessions, self.writes()), (Counter(), 0))

    def test_update_edits_only_after_an_unchanged_issue(self):
        existing = self.github.add_issue('Old title', 'Old body')
        plan = self.issue_plan(settings={'drafting': {'dk-issue': {'issue': existing['number']}}})
        ledger = self.ledger(plan)
        claimed = self.claim(plan, ledger, 'draft-issue')
        self.assertEqual(claimed['task_input']['drafting']['evidence']['current'],
                         {'title': 'Old title', 'body': 'Old body'})
        self.assertTrue(self.submit(plan, ledger, claimed, self.issue_draft())['complete'])
        existing = self.github.issues[0]
        existing['body'] = 'Edited meanwhile'
        self.assertEqual(self.listed(plan, ledger), [])
        existing['body'] = 'Old body'
        [item] = self.listed(plan, ledger)
        self.assertEqual((item['kind'], item['payload']['number']), ('edit_issue', 100))
        self.publish(plan, ledger, item)
        self.assertEqual((self.github.issues[0]['title'], self.writes()), ('Cover the extra value with a test', 1))

    def test_unauthorized_draft_stays_private(self):
        plan = self.issue_plan(authorize=False)
        ledger = self.ledger(plan)
        self.drafted(plan, ledger, 'draft-issue', self.issue_draft())
        self.assertEqual((self.listed(plan, ledger), self.writes()), ([], 0))

    def test_composed_dk_pr_fix_occurrence_drafts_privately(self):
        plan = self.drafting_plan(['draft-issue', 'describe-pr'], instructions='File an issue and describe the PR.')
        ledger = self.ledger(plan)
        self.drafted(plan, ledger, 'draft-issue', self.issue_draft(claims=[
            {'text': 'An issue is wanted.', 'sources': ['instruction:request']}]))
        self.assertEqual((self.listed(plan, ledger), self.writes()), ([], 0))


class Prepare(Fixture):
    def test_entry_presets_and_issue_ownership_reject_before_any_effect(self):
        cases = [
            (['describe-pr', 'read-feedback', {'stage': 'publication'}], {'entry': 'dk-pr-description'}, 'preset'),
            (['draft-issue'], {'entry': 'dk-issue'}, 'preset'),
            (['describe-pr'], {'entry': 'dk-pr-description', 'authorizations': {'create_issue': grant('c')}},
             'accepts only edit_pr'),
            (['describe-pr'], {'authorizations': {'create_issue': grant('c')}}, 'belongs to cf-workflows:dk-issue'),
            (['draft-issue'], {'permissions': {'create_issue': True}}, 'belong to cf-workflows:dk-issue'),
            (['draft-commit-message'], {'entry': 'dk-commit-message', 'authorizations': {'edit_pr': grant('e')}},
             'accepts only no'),
            (['describe-pr'], {'settings': {'drafting': {'dk-pr-description': {'labels': []}}}}, 'entry options'),
            (['draft-issue'], {'settings': {'drafting': {'dk-issue': {'issue': 0}}}}, 'issue number'),
        ]
        for tasks, extra, error in cases:
            with self.subTest(error):
                with self.assertRaisesRegex(ValueError, error):
                    self.drafting_plan(tasks, **extra)
        self.assertEqual(self.github.calls['write'], 0)

    def test_installed_checks_are_the_library_checks_verbatim(self):
        def definitions(path):
            source = Path(path).read_text()
            result = {}
            for node in ast.parse(source).body:
                if isinstance(node, ast.FunctionDef):
                    result[node.name] = ast.get_source_segment(source, node)
                elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                    result[node.targets[0].id] = ast.get_source_segment(source, node)
            return result
        installed, original = definitions(drafting.__file__), definitions(library.__file__)
        names = ('_HEADING', '_FENCE', '_CODE', '_LABEL', 'VALIDATION', 'REPRODUCTION', '_REF', '_URL_REF',
                 '_CLOSING', '_CODE_LINK', '_PATH_LINE', '_ATTRIBUTION', '_TRAILER', '_MARKER', '_SHA', '_BOT',
                 '_names', 'git', '_bounded', '_source', '_strip_code', 'headings', '_front_matter', '_form_labels',
                 'local_changes', '_identity', '_history', '_labels', '_check_sections', '_settled', '_check_text',
                 '_check_layout', '_items', '_check_message', 'check_result', 'MAX_SOURCE', 'MAX_PATCH',
                 'MAX_ITEMS', 'HISTORY', 'PR_TEMPLATES', 'ISSUE_TEMPLATES')
        for name in names:
            with self.subTest(name):
                self.assertEqual(installed[name], original[name])
        for name, entry in library.ENTRIES.items():
            self.assertEqual({key: drafting.ENTRIES[name][key] for key in ('adapter', 'actions', 'options',
                                                                           'role_prompt')},
                             {key: entry[key] for key in ('adapter', 'actions', 'options', 'role_prompt')})


class InstalledPreflight(PreflightFixture):
    def entry(self, name, authorizations=(), permissions=(), request=None, **extra):
        request = dict(request or self.request)
        if authorizations:
            request['authorizations'] = {action: grant(action) for action in authorizations}
        if permissions:
            extra['invocation'] = {'permissions': {action: True for action in permissions}}
        self.transport = FakeTransport(CAPABILITIES)
        result = library.installed(name, request=request, configuration=None, deployment=self.deployment,
                                   repository=self.repo, transport=self.transport, home=self.home,
                                   install=self.clean, **extra)
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def test_direct_entries_map_onto_their_installed_presets(self):
        descriptor = (INSTALL / 'plugin.yaml').read_text()
        cases = (('dk-pr-description', ('edit_pr',), {'options': {'update_title': False}},
                  {'update_title': False}),
                 ('dk-commit-message', (), {}, None),
                 ('dk-issue', ('create_issue',), {'options': {'labels': ['bug']}, 'metadata': {'assignees': ['a']},
                                                  'issue': 3}, {'labels': ['bug'], 'metadata': {'assignees': ['a']},
                                                                'issue': 3}))
        for name, actions, extra, settings in cases:
            with self.subTest(name):
                result = self.entry(name, actions, actions, **extra)
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                workflow = library.ENTRY_WORKFLOWS[name]
                self.assertEqual((result['workflow'], result['installation']['entry']),
                                 ('cf-workflows:' + name, workflow))
                self.assertIn(f'  - name: {name}\n    path: skills/{name}/SKILL.md\n    workflow: {workflow}\n',
                              descriptor)
                self.assertTrue((INSTALL / 'skills' / name / 'SKILL.md').is_file())
                body = (INSTALL / workflow).read_text()
                self.assertIn(f'name: cf-workflows:{name}\n', body)
                self.assertIn(f'      mode: fix\n      entry: {name}\n', body)
                for key in result['inputs']:
                    self.assertRegex(body, rf'\n      {key}:\n')
                    self.assertIn(f'{key}: ${{{{ inputs.{key} }}}}', body)
                preset = library.preset(name)
                self.assertEqual(result['inputs']['tasks'], preset['tasks'])
                self.assertEqual((result['inputs']['finalizers'], result['inputs']['reviewers']), ([], []))
                self.assertEqual(result['inputs']['permissions'],
                                 {action: action in actions for action in library.ENTRIES[name]['actions']})
                self.assertEqual(result['inputs']['settings'].get('drafting'),
                                 None if settings is None else {name: settings})
                self.assertEqual(self.transport.calls, Counter(capabilities=1))

    def test_unsupported_inputs_reject_before_the_engine(self):
        cases = (
            ('dk-pr-description', {'request': {'repository': 'github.com/owner/repo'}}, 'request.repository'),
            ('dk-pr-description', {'authorizations': ('create_issue',)}, 'request.authorizations'),
            ('dk-commit-message', {'authorizations': ('edit_pr',)}, 'request.authorizations'),
            ('dk-pr-description', {'invocation': {'prompts': []}}, 'invocation'),
            ('dk-pr-description', {'metadata': {'labels': ['bug']}}, 'metadata'),
            ('dk-issue', {'issue': 0}, 'issue'),
            ('dk-issue', {'options': {'update_title': False}}, 'options.update_title'),
        )
        for name, extra, path in cases:
            with self.subTest(path):
                extra = dict(extra)
                authorizations = extra.pop('authorizations', ())
                if 'request' in extra:
                    extra['request'] = {'instructions': 'x', **extra['request']}
                result = self.entry(name, authorizations, authorizations, **extra)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))

    def test_issue_authorization_needs_the_issue_entry(self):
        request = {**self.request, 'authorizations': {'create_issue': grant('create_issue')}}
        result = fix_preflight.preflight(request=request, configuration=None,
                                         invocation={'permissions': {'create_issue': True}},
                                         deployment=self.deployment, repository=self.repo,
                                         transport=FakeTransport(CAPABILITIES), home=self.home, install=self.clean)
        self.assertEqual((result['preflight'], result['error']['path']),
                         ('rejected', 'request.authorizations.create_issue'))
        self.assertIn('cf-workflows:dk-issue', result['error']['message'])

if __name__ == '__main__':
    unittest.main()
