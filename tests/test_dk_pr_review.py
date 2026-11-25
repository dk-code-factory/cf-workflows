"""Incoming review (cf-workflows:dk-pr-review) on the shared installed workflow (#24).

Disposable Git repositories, a fresh state namespace and fail-closed fakes: the
GitHub transport answers only capture and CI observation, and the engine
transport only the capability query; anything else raises with no live
fallback. Native sessions are only the reports a test hands the installed
helpers, under the opaque agent references an engine would observe. Counts of
native dispatches and transport calls are asserted. Nothing here starts an
engine, provider or host, so none of it certifies session adherence.
"""
from collections import Counter
import copy
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import app, publishing
from prfix.core import Ledger, read_artifact
from prfix.prepare import CHECKLIST, _definition, prepare

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, dk_pr_review  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import CAPABILITIES, FakeTransport, PreflightFixture  # noqa: E402

PR_URL = "https://github.com/owner/repo/pull/7"
AGENTS = ["agent_" + str(n) * 32 for n in range(1, 10)]
GRANT = {"request_id": "merge-1", "instruction": "Merge it once the review is clean.", "source": "chat:1"}
PASSES = ("toolkit", "standards", "spec")


def git(root, *args):
    return subprocess.run(git_fixture.command(*args), cwd=root, capture_output=True, check=True,
                          env=git_fixture.environment(), timeout=30).stdout.decode().strip()


class Transport:
    """Fake GitHub for another author's PR: capture and CI observation only."""

    def __init__(self, head, target, author=2, viewer=1):
        self.calls = Counter()
        self.last_capture = None
        self.read_only = False
        self.evidence = {
            "metadata": {"html_url": PR_URL, "number": 7, "state": "open", "merged": False, "draft": False,
                         "title": "Change", "body": "Change a value", "updated_at": "now",
                         "user": {"id": author, "login": "contributor"},
                         "head": {"sha": head, "ref": "change", "repo": {"full_name": "contributor/repo"}},
                         "base": {"sha": target, "ref": "main", "repo": {"full_name": "owner/repo"}}},
            "viewer": {"id": viewer, "login": "maintainer"}, "comments": [], "reviews": [], "inline": [],
            "threads": []}

    def capture(self, pr):
        self.calls["capture"] += 1
        if pr["url"] != PR_URL:
            raise AssertionError("capture for an unexpected PR")
        self.last_capture = copy.deepcopy(self.evidence)
        return self.last_capture

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def unconfigured(*args, **kwargs):
            self.calls[name] += 1
            raise AssertionError("unconfigured fixture operation: " + name)
        return unconfigured


def adapters():
    """Baseline review adapters plus the code-review-ad pass profile's three passes."""
    result = {name: {key: value for key, value in admission.BASELINE_ADAPTERS[name].items()}
              for name in ("read-feedback", "review", "simplify", "address-feedback", "reply-feedback")}
    for name in PASSES:
        result["code-review-ad-" + name] = {"kind": "review", "definition": "reviewer", "writes": False,
                                            "assignment": "Execute only the " + name + " pass of code-review-ad."}
    return result


class Helpers(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw incoming review ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "target"
        self.root.mkdir()
        git(self.root, "init", "--initial-branch=main")
        for key, value in (("user.email", "fixture@example.invalid"), ("user.name", "Fixture"),
                           ("commit.gpgsign", "false")):
            git(self.root, "config", key, value)
        (self.root / "code.py").write_text("value = 1\n")
        (self.root / "AGENTS.md").write_text("Retain exact evidence.\n")
        (self.root / "roles").mkdir()
        for role in ("leaf", "reviewer", "verifier", "fixer", "audience"):
            (self.root / "roles" / (role + ".yaml")).write_text(
                f'name: "{role}"\ndescription: "Fixture"\ninstructions: "Follow the bounded assignment."\n')
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "Initial")
        self.target = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "-c", "change")
        (self.root / "code.py").write_text("value = 2\n")
        git(self.root, "commit", "-am", "Change value")
        self.transport = Transport(git(self.root, "rev-parse", "HEAD"), self.target)
        self.definitions = {role: _definition(self.root, "roles/" + role + ".yaml")
                            for role in ("leaf", "reviewer", "verifier", "fixer", "audience")}
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])
        self.native = 0

    def prepare(self, tasks=None, reviewers=None, mode="review", authorizations=None, **options):
        controls = {"tasks": tasks if tasks is not None else ["read-feedback", {"stage": "reviews"}],
                    "finalizers": [], "reviewers": reviewers if reviewers is not None else [["review", "simplify"]],
                    "adapters": adapters(), "settings": {}, "resources": [], "turns": 8, "mode": mode, **options}
        request = {"pr": PR_URL, "instructions": "Review this PR.", "logical_run": "review_fixture",
                   "authorizations": authorizations or {}}
        result = prepare(self.root, request, controls, "exec-1", self.transport, self.definitions)
        return read_artifact(self.root, result["plan"])

    def ledger(self, plan):
        ledger = Ledger(self.root, plan["logical_run"], plan["pr"]["url"])
        self.ledgers.append(ledger)
        return ledger

    def task(self, plan, skill):
        return next(task for task in plan["tasks"] if task["skill"] == skill)

    def report(self, **values):
        evidence = {"id": "source", "path": "code.py", "sha256": app.source_file(self.root, "code.py")}
        result = {"complete": True, "findings": [], "evidence": [evidence], "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["source"]} for item in CHECKLIST]}
        result.update(values)
        return result

    def execute(self, plan, ledger, skill, agent, report=None):
        task = self.task(plan, skill)
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": task["id"]})
        if claimed["invoke"]:
            self.native += 1
            if report is None and task["kind"] == "verification":
                report = self.verdicts(ledger, plan, {})
            app.accept(self.root, self.transport, ledger, plan,
                       {"claim": claimed["claim"], "agent": agent, "report": report or self.report(), "round": 0})
        return claimed

    def verdicts(self, ledger, plan, statuses):
        dispositions = [{"finding_id": item["id"], "status": statuses.get(item["original_id"], "confirmed"),
                         "sources": ["source"], "reason": "Checked against the source."}
                        for item in app.candidates(self.root, ledger, plan)]
        return self.report(dispositions=dispositions)

    def coverage(self, plan, ledger):
        return app.coverage(self.root, self.transport, ledger, plan, {})

    def finding(self, key="f1"):
        return {"id": key, "text": "value changed without a test", "sources": ["source"]}

    # -- preparation -------------------------------------------------------------

    def test_review_mode_reads_another_authors_pr_and_never_fixes(self):
        plan = self.prepare()
        self.assertEqual(plan["mode"], "review")
        self.assertEqual([task["id"] for task in plan["tasks"] if task["kind"] == "verification"],
                         ["finding_verification"])
        self.assertFalse(any(task["writes"] or task["kind"] == "fix" for task in plan["tasks"]))
        self.assertTrue(all(stage["fix"] is False for stage in plan["stages"]))
        self.assertEqual((plan["snapshot"]["author_id"], plan["snapshot"]["actor_id"]), (2, 1))
        # Maintaining the same PR still needs ownership or an explicit request.
        with self.assertRaisesRegex(ValueError, "explicit authority"):
            self.prepare(tasks=[{"stage": "reviews"}], mode="fix")
        for tasks, message in ((["address-feedback", {"stage": "reviews"}], "cannot write"),
                               ([{"stage": "reviews"}, {"stage": "publication"}], "task and reviewer stages"),
                               ([{"stage": "responses"}, {"stage": "reviews"}], "task and reviewer stages"),
                               ([{"stage": "reviews"}, {"stage": "ci"}], "task and reviewer stages"),
                               (["read-feedback"], "requires a selected reviewer stage")):
            with self.subTest(tasks=tasks):
                with self.assertRaisesRegex(ValueError, message):
                    self.prepare(tasks=tasks)
        with self.assertRaisesRegex(ValueError, "no branch, feedback or finding publication"):
            self.prepare(permissions={"post_findings": True})
        with self.assertRaisesRegex(ValueError, "immutable numeric identities"):
            self.prepare(accounts=["contributor"])
        self.assertEqual(self.transport.calls, Counter({"capture": 2}))

    def test_identities_route_ownership_by_numeric_id_not_login(self):
        # A renamed author whose immutable ID is a configured personal account owns the PR.
        self.transport.evidence["metadata"]["user"] = {"id": 77, "login": "renamed-login"}
        plan = self.prepare(tasks=[{"stage": "reviews"}], mode="fix", reviewers=["review"],
                            accounts=[{"host": "github.com", "id": 77}])
        self.assertEqual(plan["mode"], "fix")
        # The same login on another host's account, or a matching login with another ID, does not.
        self.transport.evidence["viewer"] = {"id": 1, "login": "renamed-login"}
        for accounts in ([{"host": "ghe.example.com", "id": 77}], []):
            with self.subTest(accounts=accounts):
                with self.assertRaisesRegex(ValueError, "explicit authority"):
                    self.prepare(tasks=[{"stage": "reviews"}], mode="fix", reviewers=["review"], accounts=accounts)

    # -- outcomes --------------------------------------------------------------

    def test_clean_review_needs_every_required_session_and_independent_verification(self):
        plan = self.prepare()
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "read-feedback", AGENTS[0])
        self.execute(plan, ledger, "review", AGENTS[1])
        self.execute(plan, ledger, "simplify", AGENTS[2])
        # Empty findings and complete reviewers are not a clean review without verification.
        covered = self.coverage(plan, ledger)
        self.assertEqual(covered["review"]["outcome"], "incomplete")
        self.assertFalse(covered["review"]["clean_review"])
        self.assertEqual(covered["review"]["required_missing"], ["finding_verification"])
        self.assertFalse(covered["complete"])
        # The verifier must be its own session, even with no findings to check.
        task = self.task(plan, "finding_verification")
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": task["id"]})
        with self.assertRaisesRegex(ValueError, "cannot cover more than one review occurrence"):
            app.accept(self.root, self.transport, ledger, plan,
                       {"claim": claimed["claim"], "agent": AGENTS[1], "report": self.report(dispositions=[]),
                        "round": 0})
        app.accept(self.root, self.transport, ledger, plan,
                   {"claim": claimed["claim"], "agent": AGENTS[3], "report": self.report(dispositions=[]), "round": 0})
        self.native += 1
        covered = self.coverage(plan, ledger)
        self.assertEqual(covered["review"]["outcome"], "clean")
        self.assertTrue(covered["review"]["clean_review"] and covered["complete"])
        self.assertEqual({item["role"] for item in covered["review"]["occurrences"]},
                         {"ordinary", "simplification", "verification"})
        self.assertEqual((covered["review"]["approve"], covered["review"]["ready"], covered["ready"]),
                         (False, False, False))
        self.assertEqual(self.native, 4)
        self.assertEqual(set(self.transport.calls), {"capture"})

    def test_simplification_never_substitutes_for_ordinary_review(self):
        plan = self.prepare(reviewers=["simplify"])
        ledger = self.ledger(plan)
        for skill, agent in (("read-feedback", 0), ("simplify", 1), ("finding_verification", 2)):
            self.execute(plan, ledger, skill, AGENTS[agent])
        review = self.coverage(plan, ledger)["review"]
        self.assertTrue(review["complete"])
        self.assertFalse(review["ordinary_covered"])
        self.assertEqual((review["outcome"], review["clean_review"], review["partial"]), ("incomplete", False, True))

    def test_report_only_pass_claims_never_cover_sibling_passes(self):
        # dk-workflows#254: a coordinator-style report that claims all three passes ran.
        plan = self.prepare(tasks=[{"stage": "reviews"}],
                            reviewers=[["code-review-ad-" + name for name in PASSES]])
        ledger = self.ledger(plan)
        claims = {"coordinator": "code-review-ad", "children": [{"pass": name, "status": "completed"}
                                                                  for name in PASSES]}
        self.execute(plan, ledger, "code-review-ad-toolkit", AGENTS[0],
                 self.report(deliverables={"report": claims}))
        self.execute(plan, ledger, "finding_verification", AGENTS[1])
        review = self.coverage(plan, ledger)["review"]
        self.assertEqual(review["outcome"], "incomplete")
        self.assertEqual(sorted(review["required_missing"]),
                         sorted(self.task(plan, "code-review-ad-" + name)["id"] for name in ("standards", "spec")))
        # The same session cannot be accepted for a sibling pass either.
        task = self.task(plan, "code-review-ad-standards")
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": task["id"]})
        with self.assertRaisesRegex(ValueError, "cannot cover more than one review occurrence"):
            app.accept(self.root, self.transport, ledger, plan,
                       {"claim": claimed["claim"], "agent": AGENTS[0], "report": self.report(), "round": 0})
        app.accept(self.root, self.transport, ledger, plan,
                   {"claim": claimed["claim"], "agent": AGENTS[2], "report": self.report(), "round": 0})
        self.execute(plan, ledger, "code-review-ad-spec", AGENTS[3])
        # The verification bound to the earlier passes' findings is still current (no new findings).
        review = self.coverage(plan, ledger)["review"]
        self.assertEqual(review["outcome"], "clean")
        self.assertEqual(len({item["agent"] for item in review["occurrences"]}), 4)

    def test_findings_keep_verification_dispositions_and_block_clean_review(self):
        plan = self.prepare(tasks=[{"stage": "reviews"}])
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "review", AGENTS[0], self.report(findings=[self.finding("f1"), self.finding("f2")]))
        self.execute(plan, ledger, "simplify", AGENTS[1])
        covered = self.coverage(plan, ledger)
        self.assertEqual(len(covered["review"]["findings"]["unverified"]), 2)
        self.assertEqual(covered["review"]["outcome"], "incomplete")
        verifier = self.task(plan, "finding_verification")
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": verifier["id"]})
        with self.assertRaisesRegex(ValueError, "independent native agent|more than one review occurrence"):
            app.accept(self.root, self.transport, ledger, plan, {"claim": claimed["claim"], "agent": AGENTS[0],
                       "report": self.verdicts(ledger, plan, {"f2": "rejected"}), "round": 0})
        app.accept(self.root, self.transport, ledger, plan, {"claim": claimed["claim"], "agent": AGENTS[2],
                   "report": self.verdicts(ledger, plan, {"f2": "rejected"}), "round": 0})
        covered = self.coverage(plan, ledger)
        review = covered["review"]
        self.assertEqual((len(review["findings"]["confirmed"]), len(review["findings"]["rejected"])), (1, 1))
        self.assertEqual((review["outcome"], review["clean_review"], review["complete"]), ("findings", False, True))
        self.assertTrue(covered["complete"], "confirmed findings settle analysis; they are never fixed here")
        self.assertEqual(covered["unresolved"], review["findings"]["confirmed"])
        self.assertFalse(any(task["kind"] == "fix" for task in plan["tasks"]))

    def test_optional_and_required_reviewers_keep_distinct_outcomes(self):
        plan = self.prepare(tasks=[{"stage": "reviews"}],
                            reviewers=[["review", {"task": "simplify", "required": False}]])
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "review", AGENTS[0])
        optional = self.task(plan, "simplify")
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": optional["id"]})
        app.turn_result(self.root, self.transport, ledger, plan,
                        {"claim": claimed["claim"], "agent": AGENTS[1], "status": "failed",
                         "failure_class": "provider_failure", "error": "engine crashed"})
        self.execute(plan, ledger, "finding_verification", AGENTS[2])
        review = self.coverage(plan, ledger)["review"]
        self.assertEqual(review["outcome"], "clean")
        self.assertEqual(review["required_missing"], [])
        self.assertEqual(review["optional_omitted"], [{"id": optional["id"], "status": "failed"}])
        # A failed required reviewer has no eligible retry: the helper call fails and nothing is clean.
        plan = self.prepare(tasks=[{"stage": "reviews"}], reviewers=[["review", "simplify"]])
        required = self.task(plan, "simplify")
        ledger = self.ledger(plan)
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": required["id"]})
        with self.assertRaisesRegex(ValueError, "without eligible retained retry"):
            app.turn_result(self.root, self.transport, ledger, plan,
                            {"claim": claimed["claim"], "agent": AGENTS[4], "status": "failed",
                             "failure_class": "provider_failure", "error": "engine crashed"})
        review = self.coverage(plan, ledger)["review"]
        self.assertIn(required["id"], review["required_missing"])
        self.assertFalse(review["clean_review"])

    # -- reuse and freshness -------------------------------------------------------

    def test_unchanged_rerun_reuses_coverage_and_a_new_head_refreshes_it(self):
        plan = self.prepare(tasks=[{"stage": "reviews"}])
        ledger = self.ledger(plan)
        for skill, agent in (("review", 0), ("simplify", 1), ("finding_verification", 2)):
            self.execute(plan, ledger, skill, AGENTS[agent])
        self.assertEqual(self.native, 3)
        # A fresh session: new plan artifact, reopened ledger, no native dispatch.
        ledger.close()
        rerun = self.prepare(tasks=[{"stage": "reviews"}])
        reopened = self.ledger(rerun)
        for skill in ("review", "simplify", "finding_verification"):
            self.assertFalse(self.execute(rerun, reopened, skill, AGENTS[5])["invoke"])
        self.assertEqual(self.native, 3)
        self.assertEqual(self.coverage(rerun, reopened)["review"]["outcome"], "clean")
        # The author pushes again: the earlier coverage is stale, never a clean review.
        (self.root / "code.py").write_text("value = 3\n")
        git(self.root, "commit", "-am", "Change again")
        self.transport.evidence["metadata"]["head"]["sha"] = git(self.root, "rev-parse", "HEAD")
        review = self.coverage(rerun, reopened)["review"]
        self.assertEqual(review["outcome"], "incomplete")
        self.assertEqual(sorted(review["required_missing"]), sorted(task["id"] for task in rerun["tasks"]))
        for skill, agent in (("review", 5), ("simplify", 6), ("finding_verification", 7)):
            self.execute(rerun, reopened, skill, AGENTS[agent])
        self.assertEqual(self.native, 6)
        self.assertEqual(self.coverage(rerun, reopened)["review"]["outcome"], "clean")

    def test_incomplete_coverage_grants_no_external_authority(self):
        plan = self.prepare(tasks=[{"stage": "reviews"}], authorizations={"merge": GRANT}, permissions={"merge": True})
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "review", AGENTS[0])
        before = Counter(self.transport.calls)
        readiness = publishing.readiness(self.root, self.transport, ledger, plan)
        self.assertFalse(readiness["ready"] or readiness["clean_review"])
        self.assertFalse(readiness["review"]["clean_review"])
        proposal = publishing.merge_proposal(self.root, self.transport, ledger, plan, {})
        self.assertFalse(proposal["invoke"])
        self.assertEqual(proposal["proposal"], {})
        writes = {name for name in self.transport.calls if name not in {"capture", "observe_ci", "request"}}
        self.assertEqual(writes, set(), "no write, approval or merge was attempted")
        self.assertGreater(self.transport.calls["capture"], before["capture"])


class ReviewPreflight(PreflightFixture):
    def review(self, **changes):
        install = self.clean
        self.transport = changes.pop('transport', None) or FakeTransport()
        arguments = {'request': self.request, 'configuration': None, 'invocation': None,
                     'deployment': self.deployment, 'repository': self.repo, 'home': self.home}
        arguments.update(changes)
        result = dk_pr_review.preflight(transport=self.transport, install=install, **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def test_review_preset_maps_onto_the_review_entry(self):
        result = self.review()
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['workflow'], result['installation']['entry']),
                         ('cf-workflows:dk-pr-review', 'workflows/review.yml'))
        inputs = result['inputs']
        self.assertEqual(inputs['tasks'], ['read-feedback', {'stage': 'reviews'}])
        self.assertEqual((inputs['finalizers'], inputs['reviewers']), ([], [['review', 'simplify']]))
        self.assertEqual(set(inputs['adapters']), {'read-feedback', 'review', 'simplify'})
        self.assertFalse(any(inputs['permissions'].values()))
        self.assertEqual(result['state']['request_document']['logical_run'],
                         dk_pr_review.logical_run(self.request['pr']))
        self.assertNotEqual(result['state']['request_document']['logical_run'],
                            fix_preflight.preflight(request=self.request, configuration=None, invocation=None,
                                                    deployment=self.deployment, repository=self.repo,
                                                    transport=FakeTransport(), home=self.home)
                            ['state']['request_document']['logical_run'])
        self.assertEqual(self.transport.calls['capabilities'], 1)
        # The installed entry runs main.yml in review mode with the same declared inputs.
        body = (fix_preflight.INSTALL / 'workflows' / 'review.yml').read_text()
        self.assertIn('uses: workflows/main.yml', body)
        self.assertIn('mode: review', body)
        for name in inputs:
            self.assertRegex(body, rf'\n      {name}:\n')
            self.assertIn(f'{name}: ${{{{ inputs.{name} }}}}', body)
        batch = (fix_preflight.INSTALL / 'workflows' / 'batch.yml').read_text()
        self.assertIn("inputs.review and inputs.fix and needs.verify.status", batch)

    def test_configured_scopes_still_apply_above_the_preset(self):
        result = self.review(invocation={'options': {'reviewers': ['review']}})
        self.assertEqual(result['inputs']['reviewers'], ['review'])
        self.assertEqual(result['snapshot']['options']['tasks']['source'], 'default')

    def test_review_plans_never_write_or_publish_here(self):
        grant = {'request_id': 'r1', 'instruction': 'Do it.', 'source': 'chat:1'}
        for invocation, path in (({'options': {'tasks': ['address-feedback', {'stage': 'reviews'}]}}, 'invocation.options.tasks'),
                                 ({'options': {'tasks': [{'stage': 'reviews'}, {'stage': 'ci'}]}}, 'plan.stages'),
                                 ({'options': {'tasks': [{'stage': 'responses'}, {'stage': 'reviews'}]}}, 'plan.stages'),
                                 ({'options': {'tasks': ['read-feedback']}}, 'plan.stages')):
            with self.subTest(invocation=invocation):
                result = self.review(invocation=invocation)
                self.assertEqual(result['preflight'], 'rejected')
                self.assertTrue(result['error']['path'].startswith(path), result['error'])
                self.assertNotIn('state', result)
        for action, owner in (('post_findings', '#25'), ('push', 'never writes')):
            with self.subTest(action=action):
                result = self.review(request={**self.request, 'authorizations': {action: grant}},
                                     invocation={'permissions': {action: True}})
                self.assertEqual(result['error']['path'], 'request.authorizations.' + action)
                self.assertIn(owner, result['error']['message'])
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        result = self.review(request={**self.request, 'authorizations': {'merge': grant}},
                             invocation={'permissions': {'merge': True}}, transport=FakeTransport(offered))
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual({key for key, value in result['inputs']['permissions'].items() if value}, {'merge'})


if __name__ == "__main__":
    unittest.main()
