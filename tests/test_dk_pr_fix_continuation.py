"""Retained budgets, stable occurrences and selective continuation for dk-pr-fix (#15).

Every case uses a disposable Git repository, a fresh state namespace and a
fail-closed fake GitHub transport: any operation other than the configured
capture and CI observation raises instead of reaching a live service. Native
sessions are represented only by the reports and failures a test feeds to the
installed helpers; nothing here starts an engine, provider or host.
"""
from collections import Counter
import copy
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
import action
from prfix import app, ci
from prfix.core import SCHEMA_VERSION, Ledger, digest, ledger_path, read_artifact, state_directory
from prfix.prepare import CHECKLIST, _definition, prepare
from prepared_fixture import DEFAULT_ADAPTERS

PR_URL = "https://github.com/owner/repo/pull/7"
AUTH = {"request_id": "continue-1", "instruction": "Continue the exhausted work once more", "source": "chat:7"}
AGENTS = ["agent_" + str(n) * 32 for n in range(1, 10)]
REVIEW = {"kind": "review", "definition": "reviewer", "writes": False, "cacheable": False,
          "assignment": "Review the exact fixture and retain original findings."}
RULED = {"kind": "review", "definition": "reviewer", "writes": False, "resources": ["rules"],
         "assignment": "Review against the governing rules resource."}
RESPONDER = {"kind": "reply", "definition": "leaf", "writes": False,
             "assignment": "Assess every feedback source and draft each owed reply."}


def git(root, *args):
    return subprocess.run(git_fixture.command(*args), cwd=root, capture_output=True, check=True,
                          env=git_fixture.environment(), timeout=30).stdout.decode().strip()


class Transport:
    """Fake GitHub: capture and CI observation only; everything else fails closed."""

    def __init__(self, head, target):
        self.calls = Counter()
        self.last_capture = None
        self.read_only = False
        self.ci = {"status": "completed", "conclusion": "success"}
        self.evidence = {
            "metadata": {"html_url": PR_URL, "number": 7, "state": "open", "merged": False, "draft": False,
                         "title": "Fix", "body": "Fix a problem", "updated_at": "now",
                         "user": {"id": 1, "login": "owner"},
                         "head": {"sha": head, "ref": "fix", "repo": {"full_name": "owner/repo"}},
                         "base": {"sha": target, "ref": "main", "repo": {"full_name": "owner/repo"}}},
            "viewer": {"id": 1, "login": "owner"}, "comments": [], "reviews": [], "inline": [], "threads": []}

    def capture(self, pr):
        self.calls["capture"] += 1
        if pr["url"] != PR_URL:
            raise AssertionError("capture for an unexpected PR")
        self.last_capture = copy.deepcopy(self.evidence)
        return self.last_capture

    def observe_ci(self, pr, head):
        self.calls["observe_ci"] += 1
        return {"head": head, "checks": [{"id": "check", **self.ci}], "evidence": "fixture-ci"}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def unconfigured(*args, **kwargs):
            self.calls[name] += 1
            raise AssertionError("unconfigured fixture operation: " + name)
        return unconfigured


class Continuation(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw continuation ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "target"
        self.root.mkdir()
        git(self.root, "init", "--initial-branch=main")
        for key, value in (("user.email", "fixture@example.invalid"), ("user.name", "Fixture"),
                           ("commit.gpgsign", "false")):
            git(self.root, "config", key, value)
        (self.root / "code.py").write_text("value = 1\n")
        (self.root / "AGENTS.md").write_text("Retain exact evidence.\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "Initial")
        self.target = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "-c", "fix")
        (self.root / "code.py").write_text("value = 2\n")
        (self.root / "roles").mkdir()
        for role in ("leaf", "reviewer", "verifier", "fixer", "audience"):
            (self.root / "roles" / (role + ".yaml")).write_text(
                f'name: "{role}"\ndescription: "Fixture"\ninstructions: "Follow the bounded assignment."\n')
        (self.root / ".git" / "info").mkdir(exist_ok=True)
        with (self.root / ".git" / "info" / "exclude").open("a") as stream:
            stream.write("rules\n")
        (self.root / "rules").write_text("Original governing rule\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "Change value")
        self.transport = Transport(git(self.root, "rev-parse", "HEAD"), self.target)
        self.definitions = {role: _definition(self.root, "roles/" + role + ".yaml")
                            for role in ("leaf", "reviewer", "verifier", "fixer", "audience")}
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])
        self.native = 0

    # -- fixtures ---------------------------------------------------------

    def prepare(self, tasks=(), finalizers=(), execution="exec-1", logical_run=None, settings=None, **options):
        controls = {"tasks": list(tasks), "finalizers": list(finalizers), "settings": settings or {},
                    "adapters": {**DEFAULT_ADAPTERS, "review": REVIEW, "ruled-review": RULED, "reply-feedback": RESPONDER,
                                 "plain-review": {**RULED, "resources": []},
                                 # A plain read-only task: describe-pr runs the drafting procedure (#32).
                                 "summarize": {"kind": "description", "definition": "leaf", "writes": False,
                                               "assignment": "Summarize the final diff."}},
                    "resources": ["rules"], "turns": 8, **options}
        request = {"pr": PR_URL, "instructions": "Fix the selected issue.", "authorizations": {}}
        if logical_run:
            request["logical_run"] = logical_run
        result = prepare(self.root, request, controls, execution, self.transport, self.definitions)
        return read_artifact(self.root, result["plan"])

    def ledger(self, plan):
        ledger = Ledger(self.root, plan["logical_run"], plan["pr"]["url"])
        self.ledgers.append(ledger)
        return ledger

    def task(self, plan, skill, ordinal=0):
        return [task for task in plan["tasks"] if task["skill"] == skill][ordinal]

    def report(self, **values):
        evidence = {"id": "source", "path": "AGENTS.md", "sha256": app.source_file(self.root, "AGENTS.md")}
        result = {"complete": True, "findings": [], "evidence": [evidence], "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["source"]} for item in CHECKLIST]}
        result.update(values)
        return result

    def claim(self, plan, ledger, task_id, **inputs):
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": task_id, **inputs})
        self.native += bool(claimed["invoke"])
        return claimed

    def accept(self, plan, ledger, claimed, agent=AGENTS[0], report=None):
        return app.accept(self.root, self.transport, ledger, plan,
                          {"claim": claimed["claim"], "agent": agent, "report": report or self.report(), "round": 0})

    def run_task(self, plan, ledger, task_id, agent=AGENTS[0], report=None):
        claimed = self.claim(plan, ledger, task_id)
        if claimed["invoke"]:
            self.accept(plan, ledger, claimed, agent, report)
        return claimed

    def fail(self, plan, ledger, claimed, failure="provider_failure", error="engine crashed", retried=False):
        return app.turn_result(self.root, self.transport, ledger, plan,
                               {"claim": claimed["claim"], "agent": AGENTS[1], "status": "failed",
                                "failure_class": failure, "error": error, "retried": retried})

    def consumed(self, ledger, name):
        return ledger.db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE name=?", (name,)).fetchone()[0]

    def handle(self, operation, plan, payload, key):
        request = {"protocol": "action-v1", "phase": "execute", "input": {"plan": self.plan_ref(plan), **payload},
                   "context": {"operation_key": key, "execution_fingerprint": plan["execution_fingerprint"]}}
        return action.handle(operation, request, self.root, self.transport)["result"]["output"]

    def plan_ref(self, plan):
        return "artifacts/" + digest(plan) + ".json"

    # -- occurrence identity ------------------------------------------------

    def test_reordered_and_repeated_selections_keep_occurrence_identity_and_results(self):
        first = self.prepare(tasks=["read-feedback", "summarize", "summarize"], finalizers=["summarize"])
        reordered = self.prepare(tasks=["summarize", ["read-feedback", "assess-scope"], "summarize"],
                                 finalizers=["summarize"])
        self.assertEqual(self.task(first, "read-feedback")["id"], self.task(reordered, "read-feedback")["id"])
        repeated = [task["id"] for task in first["tasks"] if task["skill"] == "summarize"]
        self.assertEqual(len(set(repeated)), 3, "repeated selections and finalizers are distinct occurrences")
        self.assertEqual(repeated, [task["id"] for task in reordered["tasks"] if task["skill"] == "summarize"])
        instructed = self.prepare(tasks=[{"task": "summarize", "instructions": "Mention the migration."}])
        self.assertNotIn(self.task(instructed, "summarize")["id"], repeated)

        ledger = self.ledger(first)
        for task in first["tasks"]:
            self.run_task(first, ledger, task["id"])
        self.assertEqual(self.native, 4)
        # The reordered plan reuses every unchanged occurrence and runs only the new one.
        for task in reordered["tasks"]:
            self.run_task(reordered, ledger, task["id"])
        self.assertEqual(self.native, 5)
        self.assertEqual(app.coverage(self.root, self.transport, ledger, reordered, {})["missing"], [])

    def test_capacity_one_and_parallel_readers_produce_the_same_occurrences(self):
        results = {}
        for run in ("parallel", "serial"):
            plan = self.prepare(tasks=[["read-feedback", "assess-scope"]], logical_run=run)
            ledger = self.ledger(plan)
            ids = [task["id"] for task in plan["tasks"]]
            if run == "parallel":
                claims = [self.claim(plan, ledger, key) for key in ids]
                for claimed in reversed(claims):
                    self.accept(plan, ledger, claimed)
            else:
                for key in ids:
                    self.run_task(plan, ledger, key)
            rows = ledger.db.execute("SELECT task,fingerprint,coverage,state FROM claims ORDER BY task").fetchall()
            results[run] = [tuple(row) for row in rows]
            self.assertTrue(app.coverage(self.root, self.transport, ledger, plan, {})["complete"])
        self.assertEqual(results["parallel"], results["serial"])
        self.assertEqual(self.native, 4)

    # -- retained allowances and deadlines ---------------------------------

    def test_replacement_runs_changed_configuration_and_lost_checkpoints_keep_consumed_allowances(self):
        plan = self.prepare(tasks=["review"], limits={"review_cycles": 2})
        ledger = self.ledger(plan)
        review = self.task(plan, "review")["id"]
        for agent in AGENTS[:2]:
            self.run_task(plan, ledger, review, agent)
        self.assertEqual(self.consumed(ledger, "review_cycles"), 2)
        ledger.close()
        # Lose every preparation checkpoint and replace the run, configuration
        # and admitted execution. The logical run's ledger still governs.
        shutil.rmtree(state_directory(self.root) / "preparations", ignore_errors=True)
        for execution, cycles in (("exec-2", 5), ("exec-3", 1), ("exec-4", 10)):
            replacement = self.prepare(tasks=["review"], limits={"review_cycles": cycles}, execution=execution,
                                       settings={"note": execution})
            self.assertEqual(self.task(replacement, "review")["id"], review)
            ledger = self.ledger(replacement)
            with self.assertRaisesRegex(ValueError, "retained allowance exhausted: review_cycles"):
                self.claim(replacement, ledger, review)
            self.assertEqual(self.consumed(ledger, "review_cycles"), 2)
        self.assertEqual(self.native, 2)
        self.assertEqual(len(app._claims(ledger, review)), 2)

    def test_narrower_ceiling_applies_and_response_loss_never_consumes_twice(self):
        plan = self.prepare(tasks=["review"], limits={"review_cycles": 3})
        ledger = self.ledger(plan)
        review = self.task(plan, "review")["id"]
        self.run_task(plan, ledger, review)
        narrower = self.prepare(tasks=["review"], limits={"review_cycles": 1}, execution="exec-2")
        with self.assertRaisesRegex(ValueError, "exhausted"):
            self.claim(narrower, self.ledger(narrower), review)
        # The engine lost the claim response: replaying the same operation
        # returns the retained claim and reservation, under any configuration.
        wider = self.prepare(tasks=["review"], limits={"review_cycles": 3}, execution="exec-3")
        replayed = [self.handle("claim", wider, {"task": review, "cycle": 1}, "claim-review") for _ in range(2)]
        self.assertEqual(replayed[0], replayed[1])
        ledger = self.ledger(wider)
        self.assertEqual(self.consumed(ledger, "review_cycles"), 2)
        self.assertEqual(ledger.db.execute("SELECT count(*) FROM claims").fetchone()[0], 2)

    def test_ci_window_is_absolute_across_runs_and_narrowed_by_a_smaller_ceiling(self):
        plan = self.prepare(tasks=[{"stage": "ci"}])
        self.transport.ci = {"status": "in_progress"}
        start = time.time()
        first = ci.observe(self.transport, plan, self.ledger(plan), "poll-1")
        self.assertTrue(first["pending"])
        self.assertAlmostEqual(first["deadline"], start + 1800, delta=5)
        later = self.prepare(tasks=[{"stage": "ci"}], ci={"timeout_seconds": 7200}, execution="exec-2")
        self.assertEqual(ci.observe(self.transport, later, self.ledger(later), "poll-2")["deadline"], first["deadline"])
        narrower = self.prepare(tasks=[{"stage": "ci"}], ci={"timeout_seconds": 60}, execution="exec-3")
        self.assertAlmostEqual(ci.observe(self.transport, narrower, self.ledger(narrower), "poll-3")["deadline"],
                               first["deadline"] - 1740, delta=1)
        with patch("prfix.ci.time.time", return_value=first["deadline"] + 1), \
                patch("prfix.core.time.time", return_value=first["deadline"] + 1):
            with self.assertRaisesRegex(TimeoutError, "retained CI deadline exhausted"):
                ci.observe(self.transport, later, self.ledger(later), "poll-4")
        self.assertEqual(self.ledger(later).db.execute("SELECT count(*) FROM deadlines").fetchone()[0], 1)
        self.assertEqual(self.transport.calls, Counter(capture=self.transport.calls["capture"], observe_ci=3))

    def test_zero_and_exhausted_allowances_disable_attempts(self):
        plan = self.prepare(tasks=["read-feedback", {"stage": "ci"}],
                            limits={"retries_per_task": 0, "ci_repairs_per_cycle": 0})
        ledger = self.ledger(plan)
        reader = self.task(plan, "read-feedback")["id"]
        claimed = self.claim(plan, ledger, reader)
        with self.assertRaisesRegex(ValueError, "without eligible retained retry"):
            self.fail(plan, ledger, claimed, "usage_exhausted", "usage limit")
        self.assertEqual(self.consumed(ledger, "native-retry:" + claimed["claim"]), 0)
        head = self.transport.evidence["metadata"]["head"]["sha"]
        ledger.db.execute("INSERT INTO ci_observations VALUES(?,?,?,?)", ("obs", head, "failed", "{}"))
        # A zero repair allowance adds no repair session at all.
        with self.assertRaisesRegex(ValueError, "outside admitted plan"):
            self.claim(plan, ledger, "ci_repair", cycle=1)
        # One eligible retry is retained for the original claim and never renewed.
        retrying = self.prepare(tasks=["assess-scope"], logical_run="retry")
        ledger = self.ledger(retrying)
        claimed = self.claim(retrying, ledger, self.task(retrying, "assess-scope")["id"])
        self.assertTrue(self.fail(retrying, ledger, claimed, "usage_exhausted", "usage limit")["retry"])
        self.assertTrue(self.fail(retrying, ledger, claimed, "usage_exhausted", "usage limit")["retry"])
        self.assertEqual(self.consumed(ledger, "native-retry:" + claimed["claim"]), 1)
        with self.assertRaisesRegex(ValueError, "without eligible retained retry"):
            self.fail(retrying, ledger, claimed, "usage_exhausted", "usage limit", retried=True)
        self.assertEqual(self.native, 2)

    # -- selective refresh ---------------------------------------------------

    def test_relevant_changes_refresh_only_affected_occurrences(self):
        selections = dict(tasks=["ruled-review", "read-feedback"], finalizers=["inspect-commits"])
        plan = self.prepare(**selections)
        ledger = self.ledger(plan)
        keys = {name: self.task(plan, name)["id"] for name in ("ruled-review", "read-feedback", "inspect-commits")}

        def rerun(plan):
            return {name: self.run_task(plan, ledger, key)["invoke"] for name, key in keys.items()}

        self.assertEqual(rerun(plan), dict.fromkeys(keys, True))
        # CI, merge and requirement controls do not bind analysis occurrences.
        plan = self.prepare(**selections, ci={"timeout_seconds": 600}, merge_method="squash", execution="exec-1")
        plan["tasks"] = [{**task, "required": not task["required"]} for task in plan["tasks"]]
        self.assertEqual(rerun(plan), dict.fromkeys(keys, False))
        # A governing resource refreshes only the occurrence that consumes it.
        (self.root / "rules").write_text("Changed governing rule\n")
        plan = self.prepare(**selections)
        self.assertEqual(rerun(plan), {"ruled-review": True, "read-feedback": False, "inspect-commits": False})
        # A tree-preserving rewrite keeps tree-scoped analysis; history-scoped work refreshes.
        git(self.root, "commit", "--amend", "-m", "Reworded without changing the tree")
        self.transport.evidence["metadata"]["head"]["sha"] = git(self.root, "rev-parse", "HEAD")
        plan = self.prepare(**selections)
        self.assertEqual(rerun(plan), {"ruled-review": False, "read-feedback": False, "inspect-commits": True})
        # Shared request settings and the admitted deployment (roles, policy)
        # bind every occurrence.
        plan = self.prepare(**selections, settings={"scope": "changed"})
        self.assertEqual(rerun(plan), dict.fromkeys(keys, True))
        plan = self.prepare(**selections, settings={"scope": "changed"}, execution="exec-2")
        self.assertEqual(rerun(plan), dict.fromkeys(keys, True))
        self.assertEqual(self.native, 3 + 1 + 1 + 3 + 3)
        statuses = Counter(row[0] for row in ledger.db.execute("SELECT state FROM claims"))
        self.assertEqual(statuses, Counter(complete=11), "earlier results are retained, not replaced")

    # -- exhaustion and explicit continuation --------------------------------

    def test_exhausted_mandatory_work_blocks_and_continuation_extends_history(self):
        plan = self.prepare(tasks=["review"], limits={"review_cycles": 1})
        ledger = self.ledger(plan)
        review = self.task(plan, "review")["id"]
        finding = {"id": "R1", "text": "A sourced original defect", "sources": ["source"]}
        self.run_task(plan, ledger, review, report=self.report(findings=[finding]))
        original = app.candidates(self.root, ledger, plan)
        with self.assertRaisesRegex(ValueError, "exhausted: review_cycles"):
            self.claim(plan, ledger, review)
        covered = app.coverage(self.root, self.transport, ledger, plan, {})
        self.assertFalse(covered["complete"])
        self.assertEqual(covered["unresolved"], [original[0]["id"]])
        self.assertEqual(self.consumed(ledger, "review_cycles"), 1)
        # Explicit continuation adds capacity on top of the retained history.
        granted = {**AUTH, "request_id": "more-reviews"}
        self.handle("extend", plan, {"name": "review_cycles", "amount": 1, "authorization": granted}, "extend-1")
        self.handle("extend", plan, {"name": "review_cycles", "amount": 1, "authorization": granted}, "extend-1")
        replacement = self.prepare(tasks=["review"], limits={"review_cycles": 1}, execution="exec-2")
        ledger = self.ledger(replacement)
        self.run_task(replacement, ledger, review, AGENTS[2])
        with self.assertRaisesRegex(ValueError, "exhausted: review_cycles"):
            self.claim(replacement, ledger, review)
        self.assertEqual(self.consumed(ledger, "review_cycles"), 2)
        self.assertEqual(app.candidates(self.root, ledger, replacement), original)
        self.assertEqual(len(app._claims(ledger, review)), 2)
        self.assertEqual(self.native, 2)

    def change_code(self, value):
        (self.root / "code.py").write_text("value = " + value + "\n")
        git(self.root, "commit", "-am", "Change value to " + value)
        self.transport.evidence["metadata"]["head"]["sha"] = git(self.root, "rev-parse", "HEAD")

    def review_round(self, plan, ledger, agents=AGENTS[:2]):
        return [self.run_task(plan, ledger, self.task(plan, name)["id"], agent)["invoke"]
                for name, agent in zip(("plain-review", "ruled-review"), agents)]

    def test_reviewers_with_different_claim_histories_share_each_round(self):
        selections = dict(tasks=[["plain-review", "ruled-review"]], limits={"review_cycles": 5})
        plan = self.prepare(**selections)
        ledger = self.ledger(plan)
        self.assertEqual(self.review_round(plan, ledger), [True, True])
        self.assertEqual(self.consumed(ledger, "review_cycles"), 1)
        # Only the reviewer reading the changed resource refreshes: one round.
        (self.root / "rules").write_text("Changed governing rule\n")
        plan = self.prepare(**selections)
        self.assertEqual(self.review_round(plan, ledger), [False, True])
        self.assertEqual(self.consumed(ledger, "review_cycles"), 2)
        # Their claim counts now differ, yet a source change is still one round.
        self.change_code("3")
        plan = self.prepare(**selections)
        self.assertEqual(self.review_round(plan, ledger), [True, True])
        self.assertEqual(self.consumed(ledger, "review_cycles"), 3)
        self.assertEqual(self.native, 5)

    def test_continued_reviewer_rejoins_its_original_round(self):
        plan = self.prepare(tasks=[["plain-review", "ruled-review"]], limits={"review_cycles": 2})
        ledger = self.ledger(plan)
        plain, ruled = self.task(plan, "plain-review")["id"], self.task(plan, "ruled-review")["id"]
        self.run_task(plan, ledger, plain)
        with self.assertRaisesRegex(ValueError, "without eligible retained retry"):
            self.fail(plan, ledger, self.claim(plan, ledger, ruled))
        ledger.extend("retry-ruled", "occurrence:" + ruled, 1, {**AUTH, "request_id": "retry-ruled"})
        self.run_task(plan, ledger, ruled, AGENTS[2])
        self.assertEqual(self.consumed(ledger, "review_cycles"), 1)
        self.change_code("3")
        plan = self.prepare(tasks=[["plain-review", "ruled-review"]], limits={"review_cycles": 2})
        self.assertEqual(self.review_round(plan, ledger, AGENTS[3:5]), [True, True])
        self.assertEqual(self.consumed(ledger, "review_cycles"), 2)
        self.assertEqual(self.native, 5)

    def test_partial_parallel_failure_keeps_sibling_and_needs_explicit_continuation(self):
        plan = self.prepare(tasks=[["read-feedback", "assess-scope"]])
        ledger = self.ledger(plan)
        sibling, failed = self.task(plan, "read-feedback")["id"], self.task(plan, "assess-scope")["id"]
        claims = {key: self.claim(plan, ledger, key) for key in (sibling, failed)}
        self.accept(plan, ledger, claims[sibling])
        with self.assertRaisesRegex(ValueError, "without eligible retained retry"):
            self.fail(plan, ledger, claims[failed])
        # A replacement run of the same admitted deployment reuses the successful
        # sibling but cannot silently redo the failure.
        replacement = self.prepare(tasks=[["read-feedback", "assess-scope"]])
        ledger = self.ledger(replacement)
        self.assertFalse(self.claim(replacement, ledger, sibling)["invoke"])
        with self.assertRaisesRegex(ValueError, "grant an explicit continuation for occurrence:" + failed):
            self.claim(replacement, ledger, failed)
        covered = app.coverage(self.root, self.transport, ledger, replacement, {})
        self.assertEqual(covered["missing"], [failed])
        grant = {**AUTH, "request_id": "retry-scope"}
        ledger.extend(grant["request_id"], "occurrence:" + failed, 1, grant)
        fresh = self.claim(replacement, ledger, failed)
        self.assertTrue(fresh["invoke"])
        self.assertNotEqual(fresh["claim"], claims[failed]["claim"])
        # The original claim and failure stay retained but can no longer settle.
        self.assertEqual(app._claim(ledger, claims[failed]["claim"])["state"], "superseded")
        self.assertEqual(ledger.db.execute("SELECT count(*) FROM native_failures WHERE claim_id=?",
                                           (claims[failed]["claim"],)).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "superseded"):
            self.accept(replacement, ledger, claims[failed])
        self.accept(replacement, ledger, fresh, AGENTS[3])
        self.assertTrue(app.coverage(self.root, self.transport, ledger, replacement, {})["complete"])
        self.assertEqual(self.consumed(ledger, "occurrence:" + failed), 1)
        self.assertEqual(self.native, 3)

    def test_writers_never_supersede_an_unfinished_original(self):
        plan = self.prepare(tasks=["address-feedback"])
        ledger = self.ledger(plan)
        writer = self.task(plan, "address-feedback")["id"]
        self.claim(plan, ledger, writer)
        ledger.extend("writer-grant", "occurrence:" + writer, 1, {**AUTH, "request_id": "writer-grant"})
        with self.assertRaisesRegex(ValueError, "resume its originating engine execution$"):
            self.claim(plan, ledger, writer)
        self.assertEqual(self.consumed(ledger, "occurrence:" + writer), 0)

    # -- completion versus readiness -----------------------------------------

    def test_selected_plan_completion_stays_separate_from_readiness(self):
        for name, selections in (("empty", {}), ("custom", {"tasks": ["summarize"]}),
                                 ("finalizer", {"finalizers": ["inspect-commits"]})):
            with self.subTest(plan=name):
                plan = self.prepare(logical_run=name, **selections)
                ledger = self.ledger(plan)
                for task in plan["tasks"]:
                    with self.assertRaisesRegex(ValueError, "selected task/finalizer plan remains incomplete"):
                        self.handle("completion", plan, {}, name + "-early")
                    self.run_task(plan, ledger, task["id"])
                result = self.handle("completion", plan, {}, name + "-done")
                self.assertTrue(result["complete"])
                self.assertFalse(result["ready"])
                self.assertIn("current review and independent finding verification must be selected", result["blockers"])
        self.assertEqual(self.transport.calls["request"], 0)

    def test_interrupted_finalizer_keeps_its_claim_until_the_original_settles(self):
        plan = self.prepare(tasks=["read-feedback"], finalizers=["summarize"])
        ledger = self.ledger(plan)
        self.run_task(plan, ledger, self.task(plan, "read-feedback")["id"])
        finalizer = self.task(plan, "summarize")["id"]
        claimed = self.handle("claim", plan, {"task": finalizer, "cycle": 1}, "finalizer")
        # Response loss replays the retained operation; a replacement run cannot redispatch.
        self.assertEqual(self.handle("claim", plan, {"task": finalizer, "cycle": 1}, "finalizer"), claimed)
        with self.assertRaisesRegex(ValueError, "unfinished logical claim"):
            self.handle("claim", plan, {"task": finalizer, "cycle": 1}, "finalizer-replacement")
        with self.assertRaisesRegex(ValueError, "remains incomplete"):
            self.handle("completion", plan, {}, "completion-early")
        self.accept(plan, ledger, claimed)
        self.assertTrue(self.handle("completion", plan, {}, "completion")["complete"])
        self.assertEqual(ledger.db.execute("SELECT count(*) FROM claims WHERE task=?", (finalizer,)).fetchone()[0], 1)


class LedgerMigration(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw ledger ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "target"
        self.root.mkdir()
        self.path = ledger_path(state_directory(self.root, create=True), "run")
        self.path.parent.mkdir()

    def test_version_one_ledger_migrates_without_resetting_history(self):
        database = sqlite3.connect(self.path)
        database.executescript("""
            CREATE TABLE groups(id TEXT PRIMARY KEY, pr TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE reservations(name TEXT NOT NULL, operation TEXT NOT NULL, amount INTEGER NOT NULL,
                                      limit_value INTEGER NOT NULL, PRIMARY KEY(name,operation));
            CREATE TABLE deadlines(name TEXT PRIMARY KEY, value REAL NOT NULL);
            INSERT INTO groups VALUES('run', 'https://github.com/owner/repo/pull/7', 1);
            INSERT INTO reservations VALUES('review_cycles', 'original', 1, 1);
            INSERT INTO deadlines VALUES('ci:run', 4102444800);
        """)
        database.close()
        with Ledger(self.root, "run", PR_URL) as ledger:
            self.assertEqual(ledger.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(ledger.deadline("ci:run", 60), 4102444800)
            with self.assertRaisesRegex(ValueError, "exhausted"):
                ledger.reserve("review_cycles", "replacement", 5)
        with Ledger(self.root, "run", PR_URL, read_only=True) as ledger:
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)

    def test_newer_ledger_schema_is_refused(self):
        with Ledger(self.root, "run", PR_URL):
            pass
        database = sqlite3.connect(self.path)
        database.execute("PRAGMA user_version=" + str(SCHEMA_VERSION + 1))
        database.close()
        for read_only in (False, True):
            with self.subTest(read_only=read_only), self.assertRaisesRegex(ValueError, "newer than this helper"):
                Ledger(self.root, "run", PR_URL, read_only=read_only)


if __name__ == "__main__":
    unittest.main()
