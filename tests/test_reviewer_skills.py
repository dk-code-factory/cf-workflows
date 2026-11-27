"""Reviewer entries: cf-workflows:dk-pr-reviewers-run, dk-code-simplify and dk-pr-approvers (#29).

Disposable Git repositories and fail-closed fakes: the engine transport answers
only the capability query and scripted native sessions, the GitHub transports
only the reads each case configures. Anything else fails with no live
fallback. Native dispatches, sessions and GitHub reads are counted. Nothing
here starts an engine, provider or host, so none of it certifies session
adherence or GitHub behavior.
"""
import ast
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import re
import tempfile
import unittest

import yaml

import dk_pr_fix  # noqa: F401  (puts the installed helpers on sys.path)
import git_fixture
from prfix import app, publishing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from cf_workflows import admission, approvers, dk_pr_review, entries, reviewers  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import FakeTransport, PreflightFixture  # noqa: E402
from test_dk_pr_review import AGENTS, Helpers, PASSES  # noqa: E402
from test_configuration_skills import Engine, Transport as EngineTransport, session  # noqa: E402

INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}
PR = 'https://github.com/owner/repo/pull/7'


class Entries(unittest.TestCase):
    def test_role_prompts_are_the_pinned_baseline_bytes(self):
        prompts = {**{name: (entry['role_prompt'], entry['baseline_prompt']) for name, entry in reviewers.ENTRIES.items()},
                   'dk-pr-approvers': (entries.ENTRIES['dk-pr-approvers']['role_prompt'],
                                       entries.ENTRIES['dk-pr-approvers']['baseline_prompt'])}
        self.assertEqual(set(prompts), {'dk-pr-reviewers-run', 'dk-code-simplify', 'dk-pr-approvers'})
        for name, (prompt, path) in prompts.items():
            with self.subTest(name=name):
                expected = {item['path']: item['sha256'] for item in INVENTORY[name]['baseline_resources']}
                self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), expected[path])
        self.assertEqual(reviewers.check_options('dk-pr-reviewers-run', None)['reviewers'],
                         {'value': [], 'source': 'default'}, 'no default reviewer')

    def test_simplifiers_agree_with_the_installed_helpers(self):
        self.assertEqual(admission.SIMPLIFIERS, app.SIMPLIFIERS)
        names = {reviewers.simplifier(priority, related) for priority in ('change-first', 'code-first')
                 for related in (False, True)}
        self.assertEqual(names, set(admission.SIMPLIFIERS))
        self.assertEqual(reviewers.simplifier('change-first', False), admission.SIMPLIFIER)
        for name in names - {admission.SIMPLIFIER}:
            adapter = admission.ENTRY_ADAPTERS[name]
            self.assertEqual((adapter['kind'], adapter['writes'], adapter['definition']), ('review', False, 'reviewer'))
            self.assertIn('Do not edit or publish', adapter['assignment'])
            self.assertEqual('related' in name, 'directly related surrounding code' in adapter['assignment'])
            self.assertEqual('code-first' in name, 'Favor clearer resulting code' in adapter['assignment'])


class ReviewerFixture(PreflightFixture):
    def entry(self, name, **changes):
        self.transport = changes.pop('transport', None) or FakeTransport()
        arguments = {'request': self.request, 'deployment': self.deployment, 'repository': self.repo,
                     'home': self.home}
        arguments.update(changes)
        result = reviewers.preflight(name, transport=self.transport, install=self.clean, **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def configuration(self, options):
        scope = {'id': 'repo', 'selector': {'kind': 'repository', 'id': 'github.com/owner/repo'},
                 'options': options, 'permissions': {}, 'prompts': [], 'skills': [], 'preferences': []}
        return {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                'scopes': [scope], 'resources': [],
                'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                              'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}



class ReviewerPreflight(ReviewerFixture):
    def test_reviewers_need_an_explicit_selection(self):
        for options in (None, {'reviewers': []}):
            with self.subTest(options=options):
                result = self.entry('dk-pr-reviewers-run', options=options)
                self.assertEqual(result['preflight'], 'rejected')
                self.assertIn('requires explicit reviewers', result['error']['message'])
                self.assertEqual(self.transport.calls['capabilities'], 0)
        # A configured reviewer list is the runner's list too.
        result = self.entry('dk-pr-reviewers-run', configuration=self.configuration({'reviewers': ['review']}))
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['entry']['reviewers'], [['review']])
        self.assertEqual(result['snapshot']['options']['reviewers']['source'], 'repository:repo')

    def test_ordered_groups_repeats_and_optional_reviewers_are_preserved(self):
        selection = ['review', ['review', {'task': 'simplify', 'required': False}], {'task': 'review'}]
        result = self.entry('dk-pr-reviewers-run', options={'reviewers': selection})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(self.transport.calls['capabilities'], 1)
        self.assertEqual((result['workflow'], result['installation']['entry']),
                         ('cf-workflows:dk-pr-reviewers-run', 'workflows/reviewers-run.yml'))
        inputs = result['inputs']
        # The stage and the empty finalizers are fixed in the installed workflow's job.
        self.assertEqual(set(inputs), set(reviewers.INPUTS['dk-pr-reviewers-run']))
        self.assertEqual(inputs['reviewers'], selection)
        self.assertEqual(result['entry']['reviewers'], [['review'], ['review', 'simplify'], ['review']])
        tasks = result['snapshot']['plan']['tasks']
        self.assertEqual([task['required'] for task in tasks], [True, True, False, True])
        self.assertEqual(len({task['id'] for task in tasks}), 4)
        self.assertEqual(result['entry']['options']['reviewers'], {'value': selection, 'source': 'invocation'})
        self.assertEqual(result['entry']['procedure']['skills'], [])
        self.assertFalse(any(inputs['permissions'].values()))
        self.assertEqual(result['state']['request_document']['logical_run'], dk_pr_review.logical_run(PR))

    def test_direct_and_composed_entries_admit_the_same_reviewer_inputs(self):
        direct = self.entry('dk-pr-reviewers-run', options={'reviewers': [['review', 'simplify']]})
        composed = dk_pr_review.preflight(request=self.request, configuration=None, invocation=None,
                                          deployment=self.deployment, repository=self.repo, home=self.home,
                                          transport=FakeTransport(), install=self.clean)
        self.assertEqual(direct['inputs']['reviewers'], composed['inputs']['reviewers'])
        for name in ('review', 'simplify'):
            self.assertEqual(direct['inputs']['adapters'][name], composed['inputs']['adapters'][name])
        for key in ('limits', 'permissions', 'settings', 'accounts', 'turns'):
            self.assertEqual(direct['inputs'][key], composed['inputs'][key])
        self.assertEqual(direct['state']['request_document'], composed['state']['request_document'])
        review = [task for task in direct['snapshot']['plan']['tasks']]
        self.assertEqual([{k: v for k, v in task.items() if k not in ('id', 'path')} for task in review],
                         [{k: v for k, v in task.items() if k not in ('id', 'path')}
                          for task in composed['snapshot']['plan']['tasks'] if task['kind'] == 'review'])

    def test_scheduling_options_cap_only_concurrency(self):
        configured = self.configuration({'limits': {'review_cycles': 3}, 'reviewers': ['review', 'review']})
        result = self.entry('dk-pr-reviewers-run', configuration=configured, options={'execution': 'sequential'})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['inputs']['limits']['max_parallel_reviewers'], 1)
        self.assertEqual(result['inputs']['limits']['review_cycles'], 3, 'other configured limits stay')
        self.assertEqual(result['entry']['max_parallel_reviewers'], 1)
        result = self.entry('dk-pr-reviewers-run', configuration=configured, options={'max_parallel': 2})
        self.assertEqual(result['inputs']['limits'], {**admission.DEFAULTS['limits'], 'review_cycles': 3,
                                                      'max_parallel_reviewers': 2})
        result = self.entry('dk-pr-reviewers-run', configuration=configured)
        self.assertEqual(result['inputs']['limits']['max_parallel_reviewers'], 0)
        self.assertEqual(result['snapshot']['options']['limits']['source'], 'repository:repo')
        for options, path in (({'execution': 'sequential', 'max_parallel': 2}, 'options.max_parallel'),
                              ({'max_parallel': 0}, 'options.max_parallel'),
                              ({'max_parallel': True}, 'options.max_parallel'),
                              ({'execution': 'eager'}, 'options.execution'),
                              ({'priority': 'code-first'}, 'options.priority')):
            with self.subTest(options=options):
                result = self.entry('dk-pr-reviewers-run', configuration=configured, options=options)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))
                self.assertEqual(self.transport.calls['capabilities'], 0)
        for limits in (5, ['max_parallel_reviewers'], 'none', None):
            with self.subTest(limits=limits):
                result = self.entry('dk-pr-reviewers-run', configuration=configured, options={'max_parallel': 2},
                                    invocation={'options': {'limits': limits}})
                self.assertEqual((result['preflight'], result['error']['path']),
                                 ('rejected', 'invocation.options.limits'))

    def test_unsupported_controls_and_authority_reject_before_the_engine(self):
        cases = [
            ({'options': {'reviewers': [{'task': 'review', 'agent': {'model': 'm'}}]}}, 'options.reviewers[0].agent'),
            ({'options': {'reviewers': [['review', {'task': 'review', 'options': {'detail': 'high'}}]]}},
             'options.reviewers[0][1].options'),
            ({'options': {'reviewers': [{'task': 'review', 'model': 'm'}]}}, 'options.reviewers[0].model'),
            ({'request': {**self.request, 'authorizations': {'request_review': {
                'request_id': 'r', 'instruction': 'Ask them.', 'source': 'chat:1'}}}}, 'request.authorizations'),
            ({'invocation': {'permissions': {'post_findings': True}}}, 'invocation.permissions'),
            ({'invocation': {'options': {'tasks': ['read-feedback', {'stage': 'reviews'}]}}}, 'invocation.options.tasks'),
            ({'invocation': {'options': {'reviewers': ['review']}}}, 'invocation.options.reviewers'),
        ]
        for changes, path in cases:
            for name in ('dk-pr-reviewers-run', 'dk-code-simplify'):
                if name == 'dk-code-simplify' and path.startswith('options.'):
                    continue
                with self.subTest(name=name, path=path):
                    changes = copy.deepcopy(changes)
                    result = self.entry(name, options=changes.pop('options', {'reviewers': ['review']}
                                                                  if name == 'dk-pr-reviewers-run' else None),
                                        **changes)
                    self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))
                    self.assertEqual(self.transport.calls['capabilities'], 0)
        # Writers, stages, supplied skills and instructions keep the shared rejections.
        for selection in (['address-feedback'], [{'stage': 'ci'}], [{'task': 'review', 'instructions': 'Look hard.'}]):
            with self.subTest(selection=selection):
                result = self.entry('dk-pr-reviewers-run', options={'reviewers': selection})
                self.assertEqual(result['preflight'], 'rejected')

    def test_simplify_options_select_one_bounded_reviewer(self):
        result = self.entry('dk-code-simplify')
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['installation']['entry'], 'workflows/code-simplify.yml')
        self.assertEqual(set(result['inputs']), set(reviewers.INPUTS['dk-code-simplify']))
        self.assertEqual(result['inputs']['simplifier'], 'simplify')
        self.assertEqual(result['inputs']['adapters']['simplify'], fix_preflight._adapter(
            {**admission.check_adapter('simplify', admission.BASELINE_ADAPTERS['simplify'], 'x')}))
        result = self.entry('dk-code-simplify', options={'priority': 'code-first', 'allow_untouched_code': True},
                            configuration=self.configuration({'reviewers': ['review']}))
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['entry']['reviewers'], [['simplify-code-first-related-code']],
                         'a configured reviewer list never widens the simplification entry')
        self.assertEqual(result['inputs']['simplifier'], 'simplify-code-first-related-code')
        self.assertEqual(set(result['inputs']['adapters']), {'simplify-code-first-related-code'})
        self.assertEqual(result['entry']['options']['priority'], {'value': 'code-first', 'source': 'invocation'})
        for options in ({'priority': 'small'}, {'allow_untouched_code': 'yes'}, {'reviewers': ['review']}):
            with self.subTest(options=options):
                self.assertEqual(self.entry('dk-code-simplify', options=options)['preflight'], 'rejected')

    def test_approver_discovery_never_runs_as_a_plain_task(self):
        result = self.preflight(invocation={'options': {'tasks': ['discover-approvers']}})
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'options.tasks'))


class ReviewerHelpers(unittest.TestCase):
    """The installed helpers in review mode, driven as the direct entries' presets select them."""
    setUp = Helpers.setUp
    ledger, task, report, execute, verdicts, coverage, finding = (
        Helpers.ledger, Helpers.task, Helpers.report, Helpers.execute, Helpers.verdicts, Helpers.coverage,
        Helpers.finding)

    def prepare(self, tasks=None, reviewers_=None, limits=None, **options):
        import test_dk_pr_review
        adapters = test_dk_pr_review.adapters()
        adapters.update({name: dict(admission.ENTRY_ADAPTERS[name]) for name in admission.SIMPLIFIERS[1:]})
        controls = {"tasks": tasks if tasks is not None else [{"stage": "reviews"}], "finalizers": [],
                    "reviewers": reviewers_ if reviewers_ is not None else ["review"], "adapters": adapters,
                    "settings": {}, "resources": [], "turns": 8, "mode": "review", **options}
        if limits is not None:
            controls["limits"] = limits
        request = {"pr": "https://github.com/owner/repo/pull/7", "instructions": "Review this PR.",
                   "logical_run": "review_fixture", "authorizations": {}}
        from prfix.core import read_artifact
        from prfix.prepare import prepare
        result = prepare(self.root, request, controls, "exec-1", self.transport, self.definitions)
        return read_artifact(self.root, result["plan"])

    def entry_result(self, name, plan, ledger):
        review = self.coverage(plan, ledger)["review"]
        snapshot = app._snapshot(self.root, self.transport, plan)
        from prfix.core import read_artifact
        reports = {}
        for task in plan["tasks"]:
            current = app._current(ledger, snapshot, task)
            if current:
                reports[task["id"]] = read_artifact(self.root, current["report"])["report"]
        return reviewers.result(name, review, app.candidates(self.root, ledger, plan), reports)

    def limits(self, cap):
        return {**{key: value for key, value in admission.DEFAULTS["limits"].items()}, "max_parallel_reviewers": cap}

    def test_direct_and_composed_runs_share_occurrences_and_coverage(self):
        direct = self.prepare(reviewers_=[["review", "simplify"]])
        composed = self.prepare(tasks=["read-feedback", {"stage": "reviews"}], reviewers_=[["review", "simplify"]])
        pick = lambda plan: [task for task in plan["tasks"] if task["kind"] in ("review", "verification")]  # noqa: E731
        self.assertEqual(pick(direct), pick(composed))
        self.assertEqual(direct["review_contract"], composed["review_contract"])
        ledger = self.ledger(direct)
        self.execute(direct, ledger, "review", AGENTS[0])
        # The same session cannot also cover the sibling or the verifier, directly or composed.
        claimed = app.prepare_claim(self.root, self.transport, ledger, direct, {"task": self.task(direct, "simplify")["id"]})
        with self.assertRaisesRegex(ValueError, "cannot cover more than one review occurrence"):
            app.accept(self.root, self.transport, ledger, direct,
                       {"claim": claimed["claim"], "agent": AGENTS[0], "report": self.report(), "round": 0})
        app.accept(self.root, self.transport, ledger, direct,
                   {"claim": claimed["claim"], "agent": AGENTS[1], "report": self.report(), "round": 0})
        self.native += 1
        self.assertEqual(self.coverage(direct, ledger)["review"]["required_missing"], ["finding_verification"])
        self.execute(direct, ledger, "finding_verification", AGENTS[2])
        self.assertEqual(self.entry_result("dk-pr-reviewers-run", direct, ledger)["outcome"], "clean")
        self.assertEqual(self.native, 3)
        # The composed incoming review reuses every reviewer and the verifier; only its own task runs.
        for skill in ("review", "simplify", "finding_verification"):
            self.assertFalse(self.execute(composed, ledger, skill, AGENTS[5])["invoke"])
        self.execute(composed, ledger, "read-feedback", AGENTS[3])
        self.assertEqual(self.native, 4)
        self.assertEqual(self.coverage(composed, ledger)["review"]["outcome"], "clean")

    def test_capacity_one_keeps_required_passes_and_original_sibling_reports(self):
        selection = [["review"] + ["code-review-ad-" + name for name in PASSES[:2]]
                     + [{"task": "code-review-ad-spec", "required": False}]]
        concurrency = {cap: [group["concurrency"] for stage in self.prepare(reviewers_=selection, limits=self.limits(cap))
                             ["stages"] for group in stage["groups"]] for cap in (0, 1, 2, 64)}
        self.assertEqual(concurrency, {0: [4], 1: [1], 2: [2], 64: [4]})
        plan = self.prepare(reviewers_=selection, limits=self.limits(1))
        uncapped = self.prepare(reviewers_=selection, limits=self.limits(0))
        self.assertEqual([task["id"] for task in plan["tasks"]], [task["id"] for task in uncapped["tasks"]])
        self.assertEqual([task["required"] for task in plan["tasks"] if task["kind"] == "review"],
                         [True, True, True, False])
        ledger = self.ledger(plan)
        # One at a time: two required passes, then the optional one fails.
        self.execute(plan, ledger, "review", AGENTS[0], self.report(findings=[self.finding("f1")]))
        self.execute(plan, ledger, "code-review-ad-toolkit", AGENTS[1])
        optional = self.task(plan, "code-review-ad-spec")
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": optional["id"]})
        app.turn_result(self.root, self.transport, ledger, plan,
                        {"claim": claimed["claim"], "agent": AGENTS[2], "status": "failed",
                         "failure_class": "provider_failure", "error": "engine crashed"})
        self.native += 1
        result = self.entry_result("dk-pr-reviewers-run", plan, ledger)
        self.assertEqual(result["required_missing"],
                         [self.task(plan, "code-review-ad-standards")["id"], "finding_verification"])
        self.assertEqual(result["optional_omitted"], [{"id": optional["id"], "status": "failed"}])
        self.assertEqual(result["outcome"], "incomplete")
        self.execute(plan, ledger, "code-review-ad-standards", AGENTS[3])
        self.execute(plan, ledger, "finding_verification", AGENTS[4],
                     self.verdicts(ledger, plan, {"f1": "confirmed"}))
        result = self.entry_result("dk-pr-reviewers-run", plan, ledger)
        self.assertEqual((result["outcome"], result["required_missing"]), ("findings", []))
        self.assertEqual([item["original_id"] for item in result["findings"]["confirmed"]], ["f1"])
        reports = {item["adapter"]: item["report"] for item in result["occurrences"]}
        self.assertEqual(reports["review"]["findings"], [self.finding("f1")])
        self.assertIsNone(reports["code-review-ad-spec"])
        self.assertEqual({item["agent"] for item in result["occurrences"] if item["agent"]}, set(AGENTS[:2] + AGENTS[3:5]))
        self.assertEqual((result["approve"], result["ready"], result["publication"]), (False, False, False))
        self.assertEqual(self.native, 5)
        # Lifting the cap is scheduling only: the same occurrences and original reports, no new session.
        ledger.close()
        rerun = self.prepare(reviewers_=selection, limits=self.limits(0))
        reopened = self.ledger(rerun)
        for skill in ("review", "code-review-ad-toolkit", "code-review-ad-standards", "finding_verification"):
            self.assertFalse(self.execute(rerun, reopened, skill, AGENTS[6])["invoke"])
        again = self.entry_result("dk-pr-reviewers-run", rerun, reopened)
        self.assertEqual(again["occurrences"], result["occurrences"])
        self.assertEqual(self.native, 5)

    def test_repeated_occurrences_each_need_their_own_session(self):
        plan = self.prepare(reviewers_=["review", ["review", "review"]], limits=self.limits(1))
        repeated = [task for task in plan["tasks"] if task["skill"] == "review"]
        self.assertEqual(len({task["id"] for task in repeated}), 3)
        ledger = self.ledger(plan)
        for number, task in enumerate(repeated):
            claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": task["id"]})
            if number == 1:
                with self.assertRaisesRegex(ValueError, "cannot cover more than one review occurrence"):
                    app.accept(self.root, self.transport, ledger, plan,
                               {"claim": claimed["claim"], "agent": AGENTS[0], "report": self.report(), "round": 0})
            app.accept(self.root, self.transport, ledger, plan,
                       {"claim": claimed["claim"], "agent": AGENTS[number], "report": self.report(), "round": 0})
        self.execute(plan, ledger, "finding_verification", AGENTS[5])
        result = self.entry_result("dk-pr-reviewers-run", plan, ledger)
        self.assertEqual(result["outcome"], "clean")
        self.assertEqual(len({item["agent"] for item in result["occurrences"]}), 4)
        with self.assertRaisesRegex(ValueError, "requires explicit reviewers"):
            self.prepare(reviewers_=[])

    def test_simplification_recommends_without_review_coverage_or_authority(self):
        plan = self.prepare(reviewers_=["simplify-code-first"])
        ledger = self.ledger(plan)
        deferred = [{"text": "The parser module could be restructured.", "scope": "outside the changed hunks"}]
        self.execute(plan, ledger, "simplify-code-first", AGENTS[0],
                     self.report(findings=[self.finding("s1"), self.finding("s2")],
                                 deliverables={"deferred_observations": deferred}))
        self.execute(plan, ledger, "finding_verification", AGENTS[1],
                     self.verdicts(ledger, plan, {"s2": "rejected"}))
        review = self.coverage(plan, ledger)["review"]
        self.assertEqual(review["occurrences"][0]["role"], "simplification")
        self.assertEqual((review["outcome"], review["clean_review"], review["ordinary_covered"]),
                         ("incomplete", False, False))
        result = self.entry_result("dk-code-simplify", plan, ledger)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([item["original_id"] for item in result["recommendations"]], ["s1"])
        self.assertEqual([item["original_id"] for item in result["rejected"]], ["s2"])
        self.assertEqual(result["deferred_observations"], deferred)
        self.assertEqual((result["clean_review"], result["replaces_review"], result["approve"], result["ready"],
                          result["publication"], result["authority"]), (False, False, False, False, False, "none"))
        readiness = publishing.readiness(self.root, self.transport, ledger, plan)
        self.assertFalse(readiness["ready"] or readiness["clean_review"])
        self.assertFalse(publishing.merge_proposal(self.root, self.transport, ledger, plan, {})["invoke"])
        self.assertEqual(set(self.transport.calls) - {"capture", "observe_ci"}, set())

    def test_a_simplification_policy_change_refreshes_only_that_reviewer(self):
        plan = self.prepare(reviewers_=[["review", "simplify"]])
        ledger = self.ledger(plan)
        for skill, agent in (("review", 0), ("simplify", 1), ("finding_verification", 2)):
            self.execute(plan, ledger, skill, AGENTS[agent])
        changed = self.prepare(reviewers_=[["review", "simplify-related-code"]])
        self.assertEqual(self.task(changed, "review")["id"], self.task(plan, "review")["id"])
        self.assertFalse(self.execute(changed, ledger, "review", AGENTS[5])["invoke"])
        self.assertTrue(self.execute(changed, ledger, "simplify-related-code", AGENTS[3])["invoke"])
        self.assertEqual(self.native, 4)
        review = self.coverage(changed, ledger)["review"]
        self.assertEqual({item["adapter"]: item["role"] for item in review["occurrences"]},
                         {"review": "ordinary", "simplify-related-code": "simplification",
                          "finding_verification": "verification"})


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class GitHub:
    """Fake GitHub reads; unconfigured operations fail and are counted."""

    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, Counter()

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)

        def operation(*args):
            self.calls[name] += 1
            if name not in self.handlers:
                raise AssertionError('unconfigured fixture operation: ' + name)
            return copy.deepcopy(self.handlers[name](*args))
        return operation


class ApproverFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw approvers ')
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / 'target repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '--initial-branch=main')
        for key, value in (('user.email', 'fixture@example.invalid'), ('user.name', 'Fixture'),
                           ('commit.gpgsign', 'false')):
            git(self.repo, 'config', key, value)
        self.write('AGENTS.md', 'Retain exact evidence.\n')
        self.write('.github/CODEOWNERS', '# Owners\n*            @carol\n/src/        @owner/core @dan\n'
                                         'docs/        @erin @other-org/writers\n')
        self.write('CODEOWNERS', '* @ignored\n')
        self.write('src/app.py', 'value = 1\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'Initial')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-q', '-c', 'change')
        self.write('src/app.py', 'value = 2\n')
        git(self.repo, 'commit', '-q', '-am', 'Change')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-q', 'main')
        self.pull = {'html_url': PR, 'number': 7, 'head': {'sha': self.head}, 'base': {'sha': self.base, 'ref': 'main'},
                     'user': {'id': 10, 'login': 'author', 'type': 'User'},
                     'requested_reviewers': [{'id': 11, 'login': 'alice', 'type': 'User'},
                                             {'id': 99, 'login': 'helper[bot]', 'type': 'Bot'}],
                     'requested_teams': []}
        self.reviews = [{'user': {'id': 12, 'login': 'bob', 'type': 'User'}, 'state': 'APPROVED'},
                        {'user': {'id': 13, 'login': 'dave', 'type': 'User'}, 'state': 'DISMISSED'},
                        {'user': None, 'state': 'COMMENTED'},
                        {'user': {'id': 10, 'login': 'author', 'type': 'User'}, 'state': 'COMMENTED'}]
        self.permissions = {'alice': 'write', 'bob': 'admin', 'dan': 'maintain', 'carol': 'read', 'dave': 'write'}
        self.engine = Engine()
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}

    def write(self, rel, body):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def github(self, **changes):
        handlers = {'pull_request': lambda url: self.pull,
                    'reviews': lambda url, page: {'items': self.reviews, 'next': None},
                    'permission': lambda repository, login: self.permissions.get(login, 'none'),
                    'team_permission': lambda repository, slug: {'core': 'write'}.get(slug, 'none')}
        handlers.update(changes)
        return GitHub(**handlers)

    def select(self, github, sessions=(), **changes):
        self.transport = EngineTransport(self.engine, sessions=sessions)
        arguments = {'request': {'pr': PR, 'instructions': 'Who should review this?'}, 'head': self.head,
                     'deployment': self.deployment, 'repository': self.repo}
        arguments.update(changes)
        result = approvers.select(transport=self.transport, github=github, **arguments)
        self.assertLessEqual(set(self.transport.calls), {'capabilities', 'native_session'})
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        return result

    @staticmethod
    def choose(**chosen):
        chosen = {'reviewers': [], 'team_reviewers': [], **chosen}
        return session(lambda task: {'summary': 'Selected from the evidence.', **{
            key: [{('login' if key == 'reviewers' else 'slug'): name, 'reason': 'Relevant evidence.',
                   'sources': sources} for name, sources in value] for key, value in chosen.items()}})



class Approvers(ApproverFixture):
    def test_codeowners_patterns_follow_github_matching(self):
        cases = [('*', 'a/b.py', True), ('*.py', 'src/x.py', True), ('/docs/', 'docs/a.md', True),
                 ('docs/', 'x/docs/a.md', True), ('/docs/', 'x/docs/a.md', False), ('apps/', 'apps/x/y', True),
                 ('docs/*', 'docs/getting-started.md', True), ('docs/*', 'docs/build-app/troubleshooting.md', False),
                 ('src/*.py', 'src/a.py', True), ('src/*.py', 'src/b/a.py', False),
                 ('src/**/a.py', 'src/b/c/a.py', True), ('/build/logs/', 'build/logs/x', True),
                 ('apps/github', 'apps/github/x.rb', True), ('**/logs', 'deep/logs/x', True)]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(approvers._pattern(pattern)(path), expected)
        # GitHub's documented example: `docs/*` owns direct children only, so the nested directory's
        # earlier, more specific rule still wins there even though `docs/*` comes last.
        git(self.repo, 'switch', '-q', 'change')
        self.write('.github/CODEOWNERS', 'docs/build-app/ @app-owner\ndocs/* @docs-owner\n')
        self.write('docs/getting-started.md', 'Start.\n')
        self.write('docs/build-app/troubleshooting.md', 'Fix.\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'Docs')
        revision = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-q', 'main')
        owners = approvers.codeowners(self.repo, revision, ['docs/getting-started.md',
                                                            'docs/build-app/troubleshooting.md'], 'owner')['owners']
        self.assertEqual(owners['docs/getting-started.md'], [('user', 'docs-owner', '.github/CODEOWNERS:2')])
        self.assertEqual(owners['docs/build-app/troubleshooting.md'], [('user', 'app-owner', '.github/CODEOWNERS:1')])

    def test_an_exact_override_skips_discovery_including_an_empty_one(self):
        for override in ({'reviewers': [], 'team_reviewers': []},
                         {'reviewers': ['author', 'Zed'], 'team_reviewers': ['core']}):
            with self.subTest(override=override):
                github = self.github()
                result = self.select(github, override=override)
                self.assertEqual((result['complete'], result['source']), (True, 'override'))
                self.assertEqual((result['reviewers'], result['team_reviewers']),
                                 (override['reviewers'], override['team_reviewers']), 'never filtered')
                self.assertEqual(github.calls, Counter(), 'no GitHub read')
                self.assertEqual((self.transport.calls['capabilities'], self.transport.calls['native_session']), (1, 0))
                payload = approvers.request_payload(result, pr=PR, head=self.head)
                self.assertEqual(payload, None if not override['reviewers'] else
                                 {'reviewers': ['author', 'Zed'], 'team_reviewers': ['core']})
        for override in ({'reviewers': ['@alice'], 'team_reviewers': []},
                         {'reviewers': ['a', 'A'], 'team_reviewers': []},
                         {'reviewers': [], 'team_reviewers': ['owner/core']}, {'reviewers': []}):
            with self.subTest(override=override):
                result = self.select(self.github(), override=override)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(self.transport.calls, Counter())

    def test_discovery_selects_only_eligible_evidenced_recipients(self):
        github = self.github()
        result = self.select(github, sessions=[self.choose(reviewers=[('alice', ['requested']), ('bob', ['review:0'])],
                                                           team_reviewers=[('core', ['.github/CODEOWNERS:3'])])])
        self.assertEqual((result['status'], result['source']), ('complete', 'automatic'), result.get('blockers'))
        self.assertEqual((result['reviewers'], result['team_reviewers']), (['alice', 'bob'], ['core']))
        self.assertEqual(result['evidence']['codeowners'], '.github/CODEOWNERS')
        self.assertEqual(result['evidence']['paths'], ['src/app.py'])
        reasons = {item['reason'] for item in result['evidence']['excluded']}
        self.assertIn('bot account', reasons)
        self.assertIn('PR author', reasons)
        self.assertIn('deleted or unknown identity', reasons)
        self.assertIn('only dismissed reviews', reasons)
        self.assertEqual(github.calls['pull_request'], 2, 'the head is read again after discovery')
        self.assertEqual((github.calls['reviews'], github.calls['maintainers']), (1, 0))
        self.assertEqual(self.engine.sessions['select'], 1)
        [task] = self.transport.tasks
        self.assertEqual((task['role'], task['skills'], task['writes'], task['publication']),
                         ('leaf', [], False, False))
        self.assertEqual({item['name'] for item in task['context']['candidates']}, {'alice', 'bob', 'dan', 'core'})
        self.assertEqual((result['authority'], result['requested']), ('none', False))
        self.assertEqual(approvers.request_payload(result, pr=PR, head=self.head),
                         {'reviewers': ['alice', 'bob'], 'team_reviewers': ['core']})
        with self.assertRaisesRegex(ValueError, 'same PR and head'):
            approvers.request_payload(result, pr=PR, head='0' * 40)

    def test_choices_outside_the_evidence_block_the_selection(self):
        for chosen in ({'reviewers': [('mallory', ['requested'])]}, {'reviewers': [('carol', ['.github/CODEOWNERS:2'])]},
                       {'reviewers': [('author', ['review:3'])]}, {'reviewers': [('alice', ['review:0'])]},
                       {'reviewers': [('alice', ['requested']), ('Alice', ['requested'])]},
                       {'team_reviewers': [('writers', ['.github/CODEOWNERS:4'])]},
                       {'reviewers': [('alice', [{'source': 'requested'}])]}, {'reviewers': [('alice', [['requested']])]}):
            with self.subTest(chosen=chosen):
                self.engine = Engine()
                result = self.select(self.github(), sessions=[self.choose(**chosen)])
                self.assertEqual((result['complete'], result['reviewers'], result['team_reviewers']), (False, [], []))
                self.assertTrue(result['blockers'])
                with self.assertRaises(ValueError):
                    approvers.request_payload(result, pr=PR, head=self.head)

    def test_changed_paths_are_read_unquoted_for_codeowners(self):
        # git quotes non-ASCII paths unless -z; the quoted form matches no CODEOWNERS rule.
        git(self.repo, 'switch', '-q', 'change')
        self.write('docs/café.md', 'Bonjour.\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'Docs')
        self.head = self.pull['head']['sha'] = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-q', 'main')
        self.permissions['erin'] = 'write'
        result = self.select(self.github(), sessions=[self.choose(reviewers=[('erin', ['.github/CODEOWNERS:4'])])])
        self.assertEqual((result['complete'], result['reviewers']), (True, ['erin']), result.get('blockers'))
        self.assertEqual(result['evidence']['paths'], ['docs/café.md', 'src/app.py'])

    def test_unresolved_ownership_uses_a_bounded_maintainer_search(self):
        self.pull['requested_reviewers'] = []
        self.reviews = []
        self.permissions = {}
        searched = {'items': [], 'complete': True}
        none = {'team_permission': lambda repository, slug: 'none'}
        github = self.github(maintainers=lambda repository, paths: searched, **none)
        result = self.select(github)
        self.assertEqual((result['complete'], result['reviewers'], result['team_reviewers']), (True, [], []))
        self.assertEqual(result['evidence']['empty_reason'], 'complete evidence supports no eligible recipient')
        self.assertEqual((github.calls['maintainers'], self.transport.calls['native_session']), (1, 0))
        self.assertIsNone(approvers.request_payload(result, pr=PR, head=self.head))
        searched['items'] = [{'id': 20, 'login': 'frank', 'type': 'User', 'reason': 'reviewed src/ changes'}]
        self.permissions = {'frank': 'write'}
        result = self.select(self.github(maintainers=lambda repository, paths: searched, **none),
                             sessions=[self.choose(reviewers=[('frank', ['maintainers:0'])])])
        self.assertEqual(result['reviewers'], ['frank'], result.get('blockers'))
        searched['complete'] = False
        result = self.select(self.github(maintainers=lambda repository, paths: searched, **none))
        self.assertEqual((result['complete'], result['blockers']), (False, ['the maintainer search is incomplete']))

    def test_missing_or_changed_evidence_is_never_an_empty_selection(self):
        pages = {1: {'items': self.reviews[:1], 'next': 2}, 2: {'items': [], 'next': None}}
        github = self.github(reviews=lambda url, page: pages[page])
        result = self.select(github, sessions=[self.choose(reviewers=[('bob', ['review:0'])])])
        self.assertEqual((result['complete'], github.calls['reviews']), (True, 2))
        cases = [
            ('endless pages', {'reviews': lambda url, page: {'items': [], 'next': page + 1}},
             'reviews pagination exceeds its bound'),
            ('other head', {'pull_request': lambda url: {**self.pull, 'head': {'sha': self.base}}},
             'the PR head is not the requested head'),
            ('failed read', {'reviews': lambda url, page: (_ for _ in ()).throw(ConnectionError())},
             'evidence read failed: ConnectionError'),
            ('unconfigured', {'permission': None}, 'evidence read failed: AssertionError'),
        ]
        for label, changes, blocker in cases:
            with self.subTest(label=label):
                handlers = {key: value for key, value in changes.items() if value is not None}
                github = self.github(**handlers)
                if 'permission' in changes:
                    github.handlers.pop('permission')
                result = self.select(github)
                self.assertEqual((result['complete'], result['blockers']), (False, [blocker]))
                self.assertEqual(self.transport.calls['native_session'], 0)
        # The head moves while the session selects.
        heads = iter([self.head, self.base])
        github = self.github(pull_request=lambda url: {**self.pull, 'head': {'sha': next(heads)}})
        result = self.select(github, sessions=[self.choose(reviewers=[('alice', ['requested'])])])
        self.assertEqual((result['complete'], result['reviewers'], result['blockers']),
                         (False, [], ['the PR head changed during discovery']))
        # Unsupported CODEOWNERS syntax blocks instead of guessing ownership.
        git(self.repo, 'switch', '-q', 'change')
        self.write('.github/CODEOWNERS', '[Section]\n* @carol\n')
        git(self.repo, 'commit', '-q', '-am', 'Owners')
        self.base = self.pull['base']['sha'] = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-q', 'main')
        result = self.select(self.github())
        self.assertFalse(result['complete'])
        self.assertIn('CODEOWNERS syntax', result['blockers'][0])

    def test_the_entry_has_no_authority_and_needs_a_pull_request(self):
        grant = {'request_id': 'r', 'instruction': 'Request their reviews.', 'source': 'chat:1'}
        github = self.github()
        result = self.select(github, request={'pr': PR, 'instructions': 'Who?', 'authorizations': {'request_review': grant}})
        self.assertEqual((result['status'], result['admission']['error']['path']), ('rejected', 'request.authorizations'))
        result = self.select(github, request={'repository': 'github.com/owner/repo', 'instructions': 'Who?'})
        self.assertEqual(result['status'], 'rejected')
        result = self.select(github, head='main')
        self.assertEqual(result['error']['path'], 'head')
        self.assertEqual(github.calls, Counter())


def workflow(path):
    return yaml.safe_load((fix_preflight.INSTALL / path).read_text())


class InstalledEntries(ReviewerFixture):
    """The installed bindings: plugin.yaml rows, entry workflows and their declared inputs."""

    TYPES = {'string': str, 'array': list, 'object': dict, 'number': int}

    def declared(self, path):
        return workflow(path)['on']['workflow_call']['inputs']

    def assert_listed(self, name, path):
        descriptor = (fix_preflight.INSTALL / 'plugin.yaml').read_text()
        self.assertIn(f'  - name: {name}\n    path: skills/{name}/SKILL.md\n    workflow: {path}\n', descriptor)
        text = (fix_preflight.INSTALL / 'skills' / name / 'SKILL.md').read_text()
        self.assertTrue(text.startswith(f'---\nname: {name}\ndescription: '))
        self.assertIn('This plugin version is not runnable.', text)

    def assert_inputs(self, declared, inputs):
        self.assertEqual(set(declared), set(inputs))
        for key, value in inputs.items():
            self.assertIsInstance(value, self.TYPES[declared[key]['type']], key)

    def test_reviewer_entry_workflows_fix_the_preset_over_main(self):
        main = self.declared('workflows/main.yml')
        cases = {'dk-pr-reviewers-run': ({'options': {'reviewers': [['review', 'simplify'], 'review']}}, 'reviewers'),
                 'dk-code-simplify': ({'options': {'priority': 'code-first'}}, 'simplify')}
        for name, (changes, job_name) in cases.items():
            with self.subTest(name=name):
                path = reviewers.ENTRIES[name]['entry']
                self.assert_listed(name, path)
                definition = workflow(path)
                self.assertEqual(definition['name'], reviewers.ENTRIES[name]['skill'])
                declared = definition['on']['workflow_call']['inputs']
                result = self.entry(name, **changes)
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                self.assert_inputs(declared, result['inputs'])
                self.assertTrue(declared['request']['required'])
                (job,) = definition['jobs'].values()
                self.assertEqual((list(definition['jobs']), job['uses']), ([job_name], 'workflows/main.yml'))
                for key in set(declared) & set(main):
                    self.assertEqual(declared[key]['type'], main[key]['type'], key)
                    self.assertEqual(job['with'][key], '${{ inputs.%s }}' % key)
                self.assertEqual((job['with']['finalizers'], job['with']['mode'], job['with']['entry']),
                                 ([], 'review', name))
                # Every declared permission default is off; prepare rejects one that is on.
                self.assertFalse(any(declared['permissions']['default'].values()))
        run = workflow('workflows/reviewers-run.yml')
        self.assertTrue(run['on']['workflow_call']['inputs']['reviewers']['required'], 'no default reviewer')
        self.assertEqual(run['jobs']['reviewers']['with']['tasks'], [{'stage': 'reviews'}])
        simplify = workflow('workflows/code-simplify.yml')
        self.assertEqual(simplify['jobs']['simplify']['with']['tasks'],
                         [{'stage': 'reviews', 'reviewers': ['${{ inputs.simplifier }}']}])
        self.assertEqual(simplify['jobs']['simplify']['with']['reviewers'], [])
        self.assertEqual(simplify['on']['workflow_call']['inputs']['simplifier']['default'], admission.SIMPLIFIER)
        self.assertEqual(self.entry('dk-code-simplify', options={'priority': 'code-first'})['inputs']['simplifier'],
                         'simplify-code-first')

    def test_approvers_workflow_runs_one_native_selection_between_helpers(self):
        self.assert_listed('dk-pr-approvers', approvers.WORKFLOW)
        definition = workflow(approvers.WORKFLOW)
        self.assertEqual(definition['name'], 'cf-workflows:dk-pr-approvers')
        self.assertEqual(set(definition['on']['workflow_call']['inputs']), {'request', 'head', 'override', 'turns'})
        jobs = definition['jobs']
        self.assertEqual(list(jobs), ['request', 'collect', 'select', 'check'])
        self.assertEqual([jobs[name]['with']['operation'] for name in ('request', 'collect', 'check')],
                         ['snapshot_request', 'approvers_collect', 'approvers_check'])
        [step] = jobs['select']['steps']
        self.assertEqual(step['uses'], 'agent/run')
        self.assertEqual(step['with']['agent'], {'select': 'leaf', 'definitions': {'leaf': 'agents/leaf.yaml'}})
        self.assertNotIn('skills', step['with'])
        self.assertEqual(jobs['select']['if'], '${{ needs.collect.outputs.result.session }}')
        schema = definition['schemas'][step['with']['output']['schema']]
        self.assertEqual(set(schema['required']), {'reviewers', 'team_reviewers', 'summary'})
        for key, name in (('reviewers', 'login'), ('team_reviewers', 'slug')):
            self.assertEqual(set(schema['properties'][key]['items']['required']), {name, 'reason', 'sources'})
        # No job requests, approves or merges: only the three helper operations and the session.
        self.assertNotIn('publication', (fix_preflight.INSTALL / approvers.WORKFLOW).read_text())


class InstalledReviewerShape(unittest.TestCase):
    """The installed prepare helper repeats each reviewer entry's preset without changing its plan."""
    setUp = Helpers.setUp
    prepare = ReviewerHelpers.prepare

    def test_the_entry_preset_is_incoming_reviews_plan(self):
        for entry, reviewers_ in (('dk-pr-reviewers-run', ['review', ['review', {'task': 'simplify', 'required': False}]]),
                                  ('dk-code-simplify', None)):
            with self.subTest(entry=entry):
                tasks = [{'stage': 'reviews', 'reviewers': ['simplify-code-first']}] if reviewers_ is None else None
                plain = self.prepare(tasks=tasks, reviewers_=reviewers_)
                routed = self.prepare(tasks=tasks, reviewers_=reviewers_, entry=entry)
                self.assertEqual((routed['tasks'], routed['stages'], routed['review_contract']),
                                 (plain['tasks'], plain['stages'], plain['review_contract']))

    def test_every_simplifier_prepares_against_the_declared_default_adapters(self):
        declared = workflow('workflows/code-simplify.yml')['on']['workflow_call']['inputs']
        defaults = declared['adapters']['default']
        self.assertEqual(defaults, {name: {**admission.BASELINE_ADAPTERS, **admission.ENTRY_ADAPTERS}[name]
                                    for name in admission.SIMPLIFIERS})
        from prfix.core import read_artifact
        from prfix.prepare import prepare
        request = {'pr': PR, 'instructions': 'Review.', 'logical_run': 'review_fixture', 'authorizations': {}}
        for name in admission.SIMPLIFIERS:
            with self.subTest(simplifier=name):
                controls = {'tasks': [{'stage': 'reviews', 'reviewers': [name]}], 'finalizers': [], 'reviewers': [],
                            'adapters': copy.deepcopy(defaults), 'settings': {}, 'resources': [], 'turns': 8,
                            'mode': 'review', 'entry': 'dk-code-simplify'}
                plan = read_artifact(self.root, prepare(self.root, request, controls, 'exec-1', self.transport,
                                                        self.definitions)['plan'])
                self.assertIn(name, [task['skill'] for task in plan['tasks'] if task['kind'] == 'review'])

    def test_any_other_shape_rejects_before_any_session(self):
        cases = [
            ('dk-pr-reviewers-run', {'tasks': ['read-feedback', {'stage': 'reviews'}]}, 'exactly one reviewer stage'),
            ('dk-pr-reviewers-run', {'finalizers': ['read-feedback']}, 'exactly one reviewer stage'),
            ('dk-pr-reviewers-run', {'permissions': {'post_findings': True}}, 'exactly one reviewer stage'),
            ('dk-pr-reviewers-run', {'mode': 'fix'}, 'review mode only'),
            ('dk-code-simplify', {'tasks': [{'stage': 'reviews', 'reviewers': ['review']}]},
             'exactly one bundled simplification reviewer'),
            ('dk-code-simplify', {'tasks': [{'stage': 'reviews', 'reviewers': [['simplify', 'simplify']]}]},
             'exactly one bundled simplification reviewer'),
            ('dk-pr-approvers', {}, 'unknown publication entry'),
        ]
        for entry, changes, message in cases:
            with self.subTest(entry=entry, changes=changes):
                with self.assertRaisesRegex(ValueError, re.escape(message)):
                    self.prepare(entry=entry, **changes)
        import test_dk_pr_review
        from prfix.prepare import prepare
        grant = {'request_id': 'r', 'instruction': 'Post them.', 'source': 'chat:1'}
        request = {'pr': PR, 'instructions': 'Review.', 'logical_run': 'review_fixture',
                   'authorizations': {'post_findings': grant}}
        controls = {'tasks': [{'stage': 'reviews'}], 'finalizers': [], 'reviewers': ['review'], 'mode': 'review',
                    'adapters': test_dk_pr_review.adapters(),
                    'entry': 'dk-pr-reviewers-run'}
        with self.assertRaisesRegex(ValueError, 'read-only review'):
            prepare(self.root, request, controls, 'exec-1', self.transport, self.definitions)
        self.assertEqual(self.native, 0)


class InstalledApprovers(ApproverFixture):
    """helpers/prfix/approvers.py through the action-v1 entry point, with fake GitHub reads."""

    NAMES = ('CODEOWNERS', 'LOGIN', 'SLUG', 'SHA', 'REVIEWERS', 'MAX_PAGES', 'MAX_CANDIDATES', 'Incomplete',
             '_names', 'check_override', '_git', '_account', '_pattern', '_glob', 'codeowners', '_pages', 'collect',
             'check_selection')

    def setUp(self):
        super().setUp()
        git_fixture.isolate(self)
        self.reference = git_fixture.stage_request(self, {'pr': PR, 'instructions': 'Who should review this?'})
        self.keys = Counter()

    @staticmethod
    def definitions(path):
        source = Path(path).read_text()
        result = {}
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                result[node.name] = ast.get_source_segment(source, node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                result[node.targets[0].id] = ast.get_source_segment(source, node)
        return result

    def call(self, operation, payload, github, key=None, phase='execute', checkpoint=None):
        import action
        if key is None:
            self.keys[operation] += 1
            key = f'{operation}:{self.keys[operation]}'
        request = {'protocol': 'action-v1', 'phase': phase, 'input': {'operation': operation, 'payload': payload},
                   'context': {'operation_key': key, 'execution_fingerprint': 'exec-1'}}
        if checkpoint is not None:
            request['checkpoint'] = {'result': checkpoint}
        return action.handle('invoke', request, self.repo, github)

    def run_workflow(self, github, override=None, session=None, staged=None):
        """The approvers.yml jobs in order; session(task_input) stands in for the agent/run step."""
        snapshot = self.call('snapshot_request', {'request': staged or self.reference}, github)['result']['output']
        collected = self.call('approvers_collect', {'snapshot': snapshot['snapshot'], 'head': self.head,
                                                    'override': {} if override is None else override},
                              github)['result']['output']
        ran = {'status': 'skipped', 'agent': None, 'output': None}
        if collected['session']:
            self.assertIsNotNone(session, 'a session was expected')
            ran = session(collected)
        result = self.call('approvers_check', {'collected': collected, 'session': ran}, github)['result']['output']
        return collected, result

    @staticmethod
    def chosen(**chosen):
        def run(collected):
            output = {'summary': 'Selected from the evidence.', 'reviewers': [], 'team_reviewers': []}
            for key, value in chosen.items():
                output[key] = [{('login' if key == 'reviewers' else 'slug'): name, 'reason': 'Relevant evidence.',
                                'sources': sources} for name, sources in value]
            return {'status': 'succeeded', 'agent': AGENTS[0], 'output': output}
        return run

    def test_installed_checks_are_the_library_checks_verbatim(self):
        from prfix import approvers as installed
        copied = self.definitions(installed.__file__)
        original = self.definitions(approvers.__file__)
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(copied[name], original[name])
        library = self.definitions(admission.__file__)
        for name in ('AdmissionError', 'require', 'text', 'fields', 'MAX_TEXT'):
            with self.subTest(name=name):
                self.assertEqual(copied[name], library[name])
        adapter = admission.ENTRY_ADAPTERS['discover-approvers']
        self.assertEqual((installed.ROLE_PROMPT, installed.ASSIGNMENT, installed.CHECKLIST),
                         (entries.ENTRIES['dk-pr-approvers']['role_prompt'], adapter['assignment'],
                          adapter['checklist']))

    def test_an_exact_override_reads_nothing_and_runs_no_session(self):
        for override in ({'reviewers': [], 'team_reviewers': []},
                         {'reviewers': ['author', 'Zed'], 'team_reviewers': ['core']}):
            with self.subTest(override=override):
                github = self.github()
                collected, result = self.run_workflow(github, override=override)
                self.assertEqual((collected['session'], collected['evidence']), (False, None))
                self.assertEqual((result['complete'], result['source'], result['reviewers'], result['team_reviewers']),
                                 (True, 'override', override['reviewers'], override['team_reviewers']))
                self.assertEqual(github.calls, Counter(), 'no GitHub read')
                self.assertEqual(approvers.request_payload(result, pr=PR, head=self.head),
                                 None if not override['reviewers'] else
                                 {'reviewers': ['author', 'Zed'], 'team_reviewers': ['core']})
        snapshot = self.call('snapshot_request', {'request': self.reference}, self.github())['result']['output']
        for override, head in (({'reviewers': ['@alice'], 'team_reviewers': []}, self.head),
                               ({'reviewers': []}, self.head), ({}, 'main')):
            with self.subTest(override=override, head=head):
                github = self.github()
                with self.assertRaises(ValueError):
                    self.call('approvers_collect', {'snapshot': snapshot['snapshot'], 'head': head,
                                                    'override': override}, github)
                self.assertEqual(github.calls, Counter())

    def test_discovery_accepts_only_evidenced_eligible_choices(self):
        github = self.github()
        collected, result = self.run_workflow(github, session=self.chosen(
            reviewers=[('alice', ['requested']), ('bob', ['review:0'])], team_reviewers=[('core', ['.github/CODEOWNERS:3'])]))
        self.assertEqual((result['complete'], result['source']), (True, 'automatic'), result.get('blockers'))
        self.assertEqual((result['reviewers'], result['team_reviewers']), (['alice', 'bob'], ['core']))
        self.assertEqual((result['authority'], result['requested'], result['evidence_refs']),
                         ('none', False, ['native:' + AGENTS[0]]))
        self.assertEqual({item['name'] for item in collected['task_input']['candidates']}, {'alice', 'bob', 'dan', 'core'})
        self.assertIn('Request nothing, approve nothing', collected['prompt'])
        self.assertEqual(github.calls['pull_request'], 2, 'the head is read again after the session')
        self.assertEqual((github.calls['reviews'], github.calls['maintainers']), (1, 0))
        self.assertEqual(approvers.request_payload(result, pr=PR, head=self.head),
                         {'reviewers': ['alice', 'bob'], 'team_reviewers': ['core']})
        blocked = [
            ('outside the evidence', self.chosen(reviewers=[('mallory', ['requested'])]), None),
            ('foreign sources', self.chosen(reviewers=[('alice', ['review:0'])]), None),
            ('failed session', lambda collected: {'status': 'failed', 'agent': AGENTS[1], 'output': None},
             'the selection session did not complete: failed'),
        ]
        for label, session, blocker in blocked:
            with self.subTest(label=label):
                _, result = self.run_workflow(self.github(), session=session)
                self.assertEqual((result['complete'], result['reviewers'], result['team_reviewers']), (False, [], []))
                if blocker:
                    self.assertEqual(result['blockers'], [blocker])
                with self.assertRaises(ValueError):
                    approvers.request_payload(result, pr=PR, head=self.head)

    def test_a_session_that_writes_or_a_moved_head_blocks(self):
        def writes(collected):
            (self.repo / 'notes.txt').write_text('scratch\n')
            return self.chosen(reviewers=[('alice', ['requested'])])(collected)
        _, result = self.run_workflow(self.github(), session=writes)
        self.assertEqual((result['complete'], result['blockers']), (False, ['the checkout changed during discovery']))
        (self.repo / 'notes.txt').unlink()
        heads = iter([self.head, self.base])
        github = self.github(pull_request=lambda url: {**self.pull, 'head': {'sha': next(heads)}})
        _, result = self.run_workflow(github, session=self.chosen(reviewers=[('alice', ['requested'])]))
        self.assertEqual((result['complete'], result['blockers']), (False, ['the PR head changed during discovery']))
        github = self.github(reviews=lambda url, page: (_ for _ in ()).throw(ConnectionError()))
        collected, result = self.run_workflow(github)
        self.assertEqual((collected['session'], result['complete'], result['blockers']),
                         (False, False, ['evidence read failed: ConnectionError']))
        self.assertEqual(github.calls['pull_request'], 1, 'a blocked collection reads nothing more')

    def test_no_eligible_candidate_completes_empty_without_a_session(self):
        self.pull['requested_reviewers'] = []
        self.reviews = []
        self.permissions = {}
        github = self.github(maintainers=lambda repository, paths: {'items': [], 'complete': True},
                             team_permission=lambda repository, slug: 'none')
        collected, result = self.run_workflow(github)
        self.assertFalse(collected['session'])
        self.assertEqual((result['complete'], result['reviewers'], result['team_reviewers']), (True, [], []))
        self.assertEqual(result['evidence']['empty_reason'], 'complete evidence supports no eligible recipient')
        self.assertEqual((github.calls['maintainers'], github.calls['pull_request']), (1, 2))
        with self.assertRaisesRegex(ValueError, 'no session runs without an eligible candidate'):
            self.call('approvers_check', {'collected': collected, 'session': {
                'status': 'succeeded', 'agent': AGENTS[0], 'output': {}}}, github)

    def test_retained_operations_replay_without_reading_again(self):
        github = self.github()
        snapshot = self.call('snapshot_request', {'request': self.reference}, github)['result']['output']
        payload = {'snapshot': snapshot['snapshot'], 'head': self.head, 'override': {}}
        first = self.call('approvers_collect', payload, github, key='collect')
        reads = sum(github.calls.values())
        again = self.call('approvers_collect', payload, github, key='collect')
        self.assertEqual((again['result'], sum(github.calls.values())), (first['result'], reads))
        self.assertTrue(self.call('approvers_collect', payload, github, key='collect', phase='validate_checkpoint',
                                  checkpoint=first['result'])['valid'])
        self.assertEqual(self.call('approvers_collect', payload, github, key='collect', phase='reconcile')['outcome'],
                         'completed')
        self.assertEqual(self.call('approvers_collect', payload, github, key='fresh', phase='reconcile')['outcome'],
                         'no_effect')
        with self.assertRaisesRegex(ValueError, 'identity conflict'):
            self.call('approvers_collect', {**payload, 'override': {'reviewers': [], 'team_reviewers': []}}, github,
                      key='collect')

    def test_the_installed_entry_takes_no_authority(self):
        grant = {'request_id': 'r', 'instruction': 'Request their reviews.', 'source': 'chat:1'}
        staged = git_fixture.stage_request(self, {'pr': PR, 'instructions': 'Who?',
                                                  'authorizations': {'request_review': grant}})
        github = self.github()
        with self.assertRaisesRegex(ValueError, 'takes no authorization'):
            self.run_workflow(github, staged=staged)
        self.assertEqual(github.calls, Counter())

    def test_github_reads_use_bounded_read_only_routes(self):
        from prfix.approvers import GitHubEvidence, MAX_SEARCH_PATHS

        class Transport:
            host = 'github.com'

            def __init__(self):
                self.routes = []

            def request(self, method, path):
                self.routes.append((method, path))
                if path.endswith('/permission'):
                    return {'permission': 'write', 'role_name': 'maintain'}
                if '/reviews?' in path:
                    return [{'id': n} for n in range(100 if path.endswith('page=1') else 3)]
                if '/commits?' in path:
                    return [{'author': {'id': 5, 'login': 'frank', 'type': 'User'}}, {'author': None}]
                return {'html_url': PR}

            def pages(self, path):
                self.routes.append(('GET', path))
                return [{'slug': 'core', 'permission': 'push'}, {'slug': 'docs', 'permission': 'pull'}]

        transport = Transport()
        github = GitHubEvidence(transport)
        self.assertEqual(github.pull_request(PR), {'html_url': PR})
        self.assertEqual(github.reviews(PR, 1)['next'], 2)
        self.assertIsNone(github.reviews(PR, 2)['next'])
        self.assertEqual(github.permission('github.com/owner/repo', 'alice'), 'maintain')
        self.assertEqual([github.team_permission('github.com/owner/repo', slug) for slug in ('Core', 'docs', 'x')],
                         ['write', 'read', 'none'])
        found = github.maintainers('github.com/owner/repo', ['src/app.py', 'docs/a.md'])
        self.assertEqual((found['complete'], [item['login'] for item in found['items']]), (True, ['frank']))
        self.assertFalse(github.maintainers('github.com/owner/repo', ['a b.py'])['complete'])
        reads = len(transport.routes)
        self.assertFalse(github.maintainers('github.com/owner/repo',
                                            [f'f{n}.py' for n in range(MAX_SEARCH_PATHS + 1)])['complete'])
        self.assertEqual(len(transport.routes), reads, 'an over-bound search reads no commit history')
        self.assertEqual({method for method, _ in transport.routes}, {'GET'})
        self.assertEqual(sum(1 for _, path in transport.routes if path.endswith('/teams')), 1, 'teams read once')


class InstalledApproverPreflight(ApproverFixture):
    def preflight(self, **changes):
        self.transport = EngineTransport(self.engine)
        self.home = self.repo.parent / 'state home'
        arguments = {'request': {'pr': PR, 'instructions': 'Who should review this?'}, 'head': self.head,
                     'deployment': self.deployment, 'repository': self.repo, 'home': self.home}
        arguments.update(changes)
        result = approvers.installed_preflight(transport=self.transport, **arguments)
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def test_preflight_returns_the_workflow_inputs(self):
        declared = workflow(approvers.WORKFLOW)['on']['workflow_call']['inputs']
        for override, expected in ((None, {}), ({'reviewers': [], 'team_reviewers': []},) * 2):
            with self.subTest(override=override):
                result = self.preflight(override=override)
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                self.assertEqual((result['workflow'], result['installation']['entry']),
                                 ('cf-workflows:dk-pr-approvers', 'workflows/approvers.yml'))
                inputs = result['inputs']
                self.assertEqual(set(inputs), set(declared))
                self.assertEqual((inputs['head'], inputs['override'], inputs['turns']), (self.head, expected, 8))
                document = result['state']['request_document']
                self.assertEqual(set(document), {'pr', 'instructions', 'logical_run'})
                self.assertEqual(inputs['request'], 'requests/' + hashlib.sha256(
                    admission.canonical(document)).hexdigest() + '.json')
                self.assertEqual(self.transport.calls['capabilities'], 1)

    def test_preflight_rejects_authority_prompts_and_bad_inputs(self):
        grant = {'request_id': 'r', 'instruction': 'Request their reviews.', 'source': 'chat:1'}
        cases = [({'head': 'main'}, 'head', 0),
                 ({'override': {'reviewers': ['@a'], 'team_reviewers': []}}, 'override.reviewers[0]', 0),
                 ({'invocation': {'prompts': [{'id': 'p'}]}}, 'invocation.prompts', 0),
                 ({'request': {'pr': PR, 'instructions': 'Who?', 'authorizations': {'request_review': grant}}},
                  'request.authorizations', 0)]
        for changes, path, queries in cases:
            with self.subTest(path=path):
                result = self.preflight(**changes)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))
                self.assertEqual(self.transport.calls['capabilities'], queries)


if __name__ == '__main__':
    unittest.main()
