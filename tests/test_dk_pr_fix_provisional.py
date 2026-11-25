"""Pinned provisional analysis while PR metadata reconciles (#22, dk-workflows#259).

Each case uses a disposable Git repository, a fresh state namespace and a
fail-closed fake GitHub transport. It models PR capture, the PR's Git ref and
the read-only routes publication preparation needs; any other operation
raises instead of reaching a live service. Native sessions are represented only
by the reports a test feeds to the installed helpers.
"""
from collections import Counter
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
import action
from prfix import app, publishing
from prfix.capture import GitHub
from prfix.core import Ledger, digest, read_artifact
from prfix.prepare import CHECKLIST, _definition, prepare
from prfix.publication import PublicationError
from prepared_fixture import DEFAULT_ADAPTERS

PR_URL = "https://github.com/owner/repo/pull/7"
AUTH = {"request_id": "edit-1", "instruction": "Update the PR title when it is reviewed", "source": "chat:7"}
PUSH = {"request_id": "push-1", "instruction": "Push the reviewed fixes", "source": "chat:7"}
MORE = {"request_id": "wait-more", "instruction": "Wait once more for GitHub to catch up", "source": "chat:7"}
AGENTS = ["agent_" + str(n) * 32 for n in range(1, 10)]
READER = {"kind": "inspection", "definition": "leaf", "writes": False,
          "assignment": "Inspect the exact tree and retain original findings."}
HEADED = {**READER, "head_sensitive": True}
RULED = {**READER, "resources": ["rules"]}
WRITER = {"kind": "fix", "definition": "leaf", "writes": True, "assignment": "Fix the code."}
TASKS = ["inspect", "headed", "ruled", "describe-pr"]
WAIT = "identity-reconciliation:"


def git(root, *args):
    return subprocess.run(git_fixture.command(*args), cwd=root, capture_output=True, check=True,
                          env=git_fixture.environment(), timeout=30).stdout.decode().strip()


class Transport:
    """Fake GitHub: capture, the PR's Git ref and read-only routes; the rest fails closed."""

    def __init__(self, head, target):
        self.calls = Counter()
        self.last_capture = None
        self.read_only = False
        self.ref = {"ref": "refs/pull/7/head", "object": {"type": "commit", "sha": head}}
        self.evidence = {
            "metadata": {"id": 70, "html_url": PR_URL, "number": 7, "state": "open", "merged": False,
                         "draft": False, "title": "Fix", "body": "Fix a problem", "updated_at": "now",
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

    def pull_ref(self, pr):
        self.calls["pull_ref"] += 1
        if pr["url"] != PR_URL:
            raise AssertionError("Git ref for an unexpected PR")
        if isinstance(self.ref, Exception):
            raise self.ref
        return copy.deepcopy(self.ref)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def unconfigured(*args, **kwargs):
            self.calls[name] += 1
            raise AssertionError("unconfigured fixture operation: " + name)
        return unconfigured


class Provisional(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw provisional ")
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
        (self.root / ".git" / "info").mkdir(exist_ok=True)
        with (self.root / ".git" / "info" / "exclude").open("a") as stream:
            stream.write("rules\n")
        (self.root / "rules").write_text("Original governing rule\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "Initial")
        self.target = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "-c", "fix")
        self.commit("value = 2\n", "First change")
        self.metadata_head = git(self.root, "rev-parse", "HEAD")
        self.commit("value = 3\n", "Second change")
        self.pin = git(self.root, "rev-parse", "HEAD")
        # dk-workflows#259: the PR's Git ref is at the checkout, the PR metadata lags.
        self.transport = Transport(self.metadata_head, self.target)
        self.transport.ref["object"]["sha"] = self.pin
        self.definitions = {role: _definition(self.root, "roles/" + role + ".yaml")
                            for role in ("leaf", "reviewer", "verifier", "fixer", "audience")}
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])
        self.native = 0

    # -- fixtures ---------------------------------------------------------

    def commit(self, content, message):
        (self.root / "code.py").write_text(content)
        git(self.root, "commit", "-am", message)

    def catch_up(self, head=None):
        self.transport.evidence["metadata"]["head"]["sha"] = head or self.pin
        self.transport.ref["object"]["sha"] = head or self.pin

    def prepare(self, tasks=TASKS, execution="exec-1", authorizations=None, permissions=None):
        controls = {"tasks": list(tasks), "finalizers": [], "settings": {}, "resources": ["rules"], "turns": 8,
                    "adapters": {**DEFAULT_ADAPTERS, "inspect": READER, "headed": HEADED, "ruled": RULED,
                                 "write": WRITER},
                    "permissions": permissions or {"edit_pr": True, "push_commits": True}}
        request = {"pr": PR_URL, "instructions": "Fix the selected issue.",
                   "authorizations": {"edit_pr": AUTH, "push": PUSH} if authorizations is None else authorizations}
        result = prepare(self.root, request, controls, execution, self.transport, self.definitions)
        return read_artifact(self.root, result["plan"])

    def ledger(self, plan):
        ledger = Ledger(self.root, plan["logical_run"], plan["pr"]["url"])
        self.ledgers.append(ledger)
        return ledger

    def task(self, plan, skill):
        return next(task for task in plan["tasks"] if task["skill"] == skill)

    def report(self, head, **values):
        evidence = {"id": "source", "path": "AGENTS.md", "sha256": app.source_file(self.root, "AGENTS.md")}
        result = {"complete": True, "findings": [], "evidence": [evidence], "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["source"]} for item in CHECKLIST]}
        result.update(values)
        return result

    def proposal(self, head):
        return {"id": "edit-title", "kind": "edit_pr", "head": head, "lease": head, "target": self.target,
                "payload": {"title": "Fix the exact problem"}}

    def run_all(self, plan, ledger, head):
        for task in plan["tasks"]:
            claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": task["id"]})
            if claimed["invoke"]:
                self.native += 1
                operations = [self.proposal(head)] if task["skill"] == "describe-pr" else []
                app.accept(self.root, self.transport, ledger, plan, {
                    "claim": claimed["claim"], "agent": AGENTS[0], "round": 0,
                    "report": self.report(head, operations=operations)})

    def handle(self, operation, plan, payload, key, phase="execute"):
        request = {"protocol": "action-v1", "phase": phase,
                   "input": {"plan": "artifacts/" + digest(plan) + ".json", **payload},
                   "context": {"operation_key": key, "execution_fingerprint": plan["execution_fingerprint"]}}
        return action.handle(operation, request, self.root, self.transport)

    def wait(self, plan, key):
        return self.handle("alignment", plan, {"wait": True}, key)["result"]["output"]

    def consumed(self, ledger):
        return ledger.db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE name=?",
                                 (WAIT + self.pin,)).fetchone()[0]

    def provisional_run(self):
        plan = self.prepare()
        ledger = self.ledger(plan)
        self.run_all(plan, ledger, self.pin)
        return plan, ledger

    # -- lagging metadata ----------------------------------------------------

    def test_lagging_metadata_permits_private_provisional_analysis_without_external_effects(self):
        plan, ledger = self.provisional_run()
        identities = plan["snapshot"]["identities"]
        self.assertEqual((identities["current"], identities["pin"], identities["metadata_head"], identities["git_head"]),
                         (False, self.pin, self.metadata_head, self.pin))
        self.assertEqual(plan["snapshot"]["head"], self.pin)
        self.assertEqual(self.native, 4, "the pinned tree is analyzed while metadata lags")
        # Each assignment retains the actual metadata identity it was analyzed under.
        for row in ledger.db.execute("SELECT assignment FROM claims"):
            retained = read_artifact(self.root, row["assignment"])["snapshot"]["identities"]
            self.assertEqual(retained["metadata_head"], self.metadata_head)
            self.assertFalse(retained["current"])

        covered = app.coverage(self.root, self.transport, ledger, plan, {})
        self.assertTrue(covered["complete"], "selected-plan coverage of the pinned revision")
        self.assertFalse(covered["current"])
        self.assertEqual(covered["provisional"], {
            "provisional": True, "reason": identities["reason"], "pin": self.pin,
            "mismatched": {"head": {"metadata": self.metadata_head, "git": self.pin}}})
        self.assertTrue(covered["waiting"])
        self.assertFalse(covered["settled"])

        listed = publishing.publication_list(self.root, self.transport, ledger, plan, {"kind": "all"})
        self.assertEqual(listed["items"], [])
        self.assertTrue(listed["provisional"]["provisional"])
        with self.assertRaisesRegex(PublicationError, "provisional"):
            publishing.publication_prepare(self.root, self.transport, ledger, plan, {"proposal": self.proposal(self.pin)})
        ready = publishing.readiness(self.root, self.transport, ledger, plan)
        self.assertFalse(ready["ready"])
        self.assertTrue(ready["provisional"]["provisional"])
        self.assertTrue(any("provisional" in blocker for blocker in ready["blockers"]))
        self.assertFalse(publishing.merge_proposal(self.root, self.transport, ledger, plan, {})["invoke"])
        status = self.handle("alignment", plan, {"wait": False}, "ci-stage")["result"]["output"]
        self.assertFalse(status["current"], "the CI stage skips observation while provisional")
        self.assertEqual(self.consumed(ledger), 0, "a status check consumes no wait")
        with self.assertRaisesRegex(ValueError, "provisional"):
            app.require_current(app._snapshot(self.root, self.transport, plan), "CI repair")

        self.assertEqual(ledger.db.execute("SELECT count(*) FROM publication_intents").fetchone()[0], 0)
        self.assertLessEqual(set(self.transport.calls), {"capture", "pull_ref"})
        self.assertEqual(self.transport.calls["capture"], self.transport.calls["pull_ref"])

    def test_same_revision_reconciliation_reuses_eligible_analysis_and_unblocks_publication(self):
        plan, ledger = self.provisional_run()
        self.catch_up()
        refs = self.transport.calls["pull_ref"]
        replacement = self.prepare()
        self.assertTrue(replacement["snapshot"]["identities"]["current"])
        self.run_all(replacement, ledger, self.pin)
        self.assertEqual(self.native, 4, "reconciliation to the pinned revision reuses every result")
        covered = app.coverage(self.root, self.transport, ledger, replacement, {})
        self.assertTrue(covered["complete"] and covered["current"] and covered["settled"])
        self.assertEqual(covered["provisional"], {"provisional": False})
        listed = publishing.publication_list(self.root, self.transport, ledger, replacement, {"kind": "all"})
        self.assertEqual([item["kind"] for item in listed["items"]], ["edit_pr"])
        prepared = publishing.publication_prepare(self.root, self.transport, ledger, replacement,
                                                  {"proposal": listed["items"][0]})
        self.assertTrue(prepared["invoke"])
        self.assertEqual(prepared["intent"]["lease"], self.pin)
        self.assertEqual(self.transport.calls["pull_ref"], refs, "agreeing metadata reads no Git ref")
        self.assertLessEqual(set(self.transport.calls), {"capture", "pull_ref"})

        # The metadata lags again before dispatch: the intent stays prepared, unsent and unblocked.
        self.transport.evidence["metadata"]["head"]["sha"] = self.metadata_head
        with self.assertRaisesRegex(PublicationError, "provisional"):
            publishing.publication_execute(self.root, self.transport, ledger, replacement,
                                           {"id": prepared["id"], "agent": AGENTS[5], "report": {}})
        state = ledger.db.execute("SELECT state FROM publication_intents WHERE id=?", (prepared["id"],)).fetchone()
        self.assertEqual(state[0], "prepared")
        self.assertLessEqual(set(self.transport.calls), {"capture", "pull_ref"})

    def test_provisional_writer_commits_privately_and_pushes_only_after_reconciliation(self):
        plan = self.prepare(tasks=["write"])
        ledger = self.ledger(plan)
        writer = self.task(plan, "write")["id"]
        claimed = app.prepare_claim(self.root, self.transport, ledger, plan, {"task": writer})
        before = hashlib.sha256(b"value = 3\n").hexdigest()
        (self.root / "code.py").write_text("value = 4\n")
        change = {"path": "code.py", "before": before, "after": hashlib.sha256(b"value = 4\n").hexdigest()}
        app.accept(self.root, self.transport, ledger, plan, {
            "claim": claimed["claim"], "agent": AGENTS[0], "round": 0,
            "report": self.report(self.pin, changes=[change], commit_message="Fix value")})
        local = git(self.root, "rev-parse", "HEAD")
        self.assertNotEqual(local, self.pin)
        # The lagging metadata head is an ancestor of the local commit but not
        # the revision it was built on: analysis stays pinned and provisional.
        snapshot = app._snapshot(self.root, self.transport, plan)
        self.assertEqual((snapshot["head"], snapshot["local_head"], snapshot["identities"]["current"]),
                         (self.pin, local, False))
        self.assertEqual(publishing.publication_list(self.root, self.transport, ledger, plan, {})["items"], [])
        self.catch_up()
        items = publishing.publication_list(self.root, self.transport, ledger, plan, {})["items"]
        self.assertEqual([(item["kind"], item["lease"], item["head"]) for item in items], [("push", self.pin, local)])
        self.assertLessEqual(set(self.transport.calls), {"capture", "pull_ref"})

    # -- refresh -------------------------------------------------------------

    def test_changed_tree_after_reconciliation_refreshes_coverage(self):
        plan, ledger = self.provisional_run()
        self.commit("value = 5\n", "Another change")
        moved = git(self.root, "rev-parse", "HEAD")
        self.catch_up(moved)
        replacement = self.prepare()
        self.run_all(replacement, ledger, moved)
        self.assertEqual(self.native, 8, "a changed tree refreshes every tree-scoped occurrence")

    def test_same_tree_new_revision_refreshes_only_head_sensitive_work(self):
        plan, ledger = self.provisional_run()
        git(self.root, "commit", "--amend", "-m", "Second change, reworded")
        amended = git(self.root, "rev-parse", "HEAD")
        self.catch_up(amended)
        replacement = self.prepare()
        self.run_all(replacement, ledger, amended)
        self.assertEqual(self.native, 5)
        refreshed = ledger.db.execute("SELECT task FROM claims GROUP BY task HAVING count(*) > 1").fetchall()
        self.assertEqual([row[0] for row in refreshed], [self.task(plan, "headed")["id"]])

    def test_relevant_requirements_refresh_only_affected_work_at_the_same_revision(self):
        plan, ledger = self.provisional_run()
        self.catch_up()
        (self.root / "rules").write_text("Changed governing rule\n")
        replacement = self.prepare()
        self.run_all(replacement, ledger, self.pin)
        self.assertEqual(self.native, 5, "only the occurrence reading the changed resource refreshes")
        self.transport.evidence["comments"].append({"id": 11, "user": {"id": 2, "login": "reviewer"},
                                                    "body": "Please also handle zero."})
        self.run_all(replacement, ledger, self.pin)
        self.assertEqual(self.native, 9, "new discussion refreshes every discussion-bound occurrence")

    # -- errors --------------------------------------------------------------

    def test_corrupt_cross_pr_missing_and_inconsistent_snapshots_remain_errors(self):
        cases = {
            "another PR": ({"ref": "refs/pull/8/head", "object": {"type": "commit", "sha": self.pin}}, "another PR"),
            "tag object": ({"ref": "refs/pull/7/head", "object": {"type": "tag", "sha": self.pin}}, "corrupt"),
            "short SHA": ({"ref": "refs/pull/7/head", "object": {"type": "commit", "sha": self.pin[:12]}}, "corrupt"),
            "no object": ({"ref": "refs/pull/7/head"}, "corrupt"),
            "missing ref": (RuntimeError("gh exited with status 1"), "status 1"),
            "Git agrees with metadata": ({"ref": "refs/pull/7/head", "object": {"type": "commit", "sha": self.metadata_head}},
                                         "retained local commit"),
            "checkout at neither": ({"ref": "refs/pull/7/head", "object": {"type": "commit", "sha": self.target}},
                                    "retained local commit"),
        }
        for name, (ref, message) in cases.items():
            with self.subTest(name):
                self.transport.ref = ref
                calls = self.transport.calls["pull_ref"]
                with self.assertRaisesRegex((ValueError, RuntimeError), message):
                    self.prepare()
                self.assertEqual(self.transport.calls["pull_ref"], calls + 1)
        self.transport.ref = {"ref": "refs/pull/7/head", "object": {"type": "commit", "sha": self.pin}}
        self.transport.evidence["metadata"]["html_url"] = "https://github.com/owner/repo/pull/8"
        with self.assertRaises(ValueError):
            self.prepare()
        self.transport.evidence["metadata"]["html_url"] = PR_URL
        self.transport.evidence["metadata"]["head"]["sha"] = "not-a-sha"
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.prepare()
        # A snapshot that turns corrupt mid-run blocks the claim instead of reusing analysis.
        self.transport.evidence["metadata"]["head"]["sha"] = self.metadata_head
        plan = self.prepare()
        ledger = self.ledger(plan)
        self.transport.ref = {"ref": "refs/pull/8/head", "object": {"type": "commit", "sha": self.pin}}
        with self.assertRaisesRegex(ValueError, "another PR"):
            app.prepare_claim(self.root, self.transport, ledger, plan, {"task": plan["tasks"][0]["id"]})
        self.assertEqual(ledger.db.execute("SELECT count(*) FROM claims").fetchone()[0], 0)
        self.assertLessEqual(set(self.transport.calls), {"capture", "pull_ref"})

    # -- retained bounded waits -------------------------------------------------

    def test_interrupted_bounded_waits_keep_their_allowance_and_uncertainty(self):
        plan, ledger = self.provisional_run()
        first = self.wait(plan, "wait-1")
        self.assertTrue(first["waiting"])
        self.assertEqual(first["reconciliation"]["attempts"], 1)
        captures = self.transport.calls["capture"]
        self.assertEqual(self.wait(plan, "wait-1"), first, "a lost response replays the retained result")
        self.assertEqual(self.transport.calls["capture"], captures)
        self.assertEqual(self.consumed(ledger), 1)

        # Interrupted after the wait was reserved: the operation stays uncertain.
        original = app.reconciliation
        seen = []

        def interrupted(*args):
            seen.append(1)
            if len(seen) == 2:
                raise KeyboardInterrupt("helper interrupted")
            return original(*args)
        with patch.object(app, "reconciliation", interrupted), self.assertRaises(KeyboardInterrupt):
            self.wait(plan, "wait-2")
        self.assertEqual(self.consumed(ledger), 2)
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.wait(plan, "wait-2")
        self.assertEqual(self.handle("alignment", plan, {"wait": True}, "wait-2", phase="reconcile")["outcome"], "unknown")

        # A replacement run reopens the same allowance.
        replacement = self.prepare()
        self.assertEqual(self.wait(replacement, "wait-3")["reconciliation"]["attempts"], 3)
        for key in ("wait-4", "wait-5"):
            self.assertTrue(self.wait(replacement, key)["waiting"])
        exhausted = self.wait(replacement, "wait-6")
        self.assertEqual((exhausted["waiting"], exhausted["exhausted"]), (False, True))
        self.assertEqual(self.consumed(ledger), 5)
        covered = app.coverage(self.root, self.transport, ledger, replacement, {})
        self.assertEqual((covered["waiting"], covered["settled"], covered["current"]), (False, True, False))
        self.assertFalse(publishing.readiness(self.root, self.transport, ledger, replacement)["ready"])

        # Only an explicit continuation adds a wait; reconciling consumes nothing.
        ledger.extend(MORE["request_id"], WAIT + self.pin, 1, MORE)
        self.assertTrue(self.wait(replacement, "wait-7")["waiting"])
        self.catch_up()
        current = self.wait(replacement, "wait-8")
        self.assertEqual((current["current"], current["waiting"]), (True, False))
        self.assertEqual(self.consumed(ledger), 6)
        self.assertEqual(self.native, 4)
        self.assertLessEqual(set(self.transport.calls), {"capture", "pull_ref"})

    # -- transport and orchestration ----------------------------------------------

    def test_github_transport_reads_only_the_exact_pr_git_ref(self):
        transport = GitHub(self.root)
        with patch.object(GitHub, "request", return_value={"ref": "refs/pull/7/head"}) as request:
            transport.pull_ref({"url": PR_URL, "host": "github.com", "owner": "owner", "name": "repo",
                                "number": 7, "repository": "github.com/owner/repo"})
        request.assert_called_once_with("GET", "repos/owner/repo/git/ref/pull/7/head")

    def test_workflow_waits_boundedly_and_skips_ci_while_provisional(self):
        workflows = Path(action.__file__).resolve().parent.parent
        main = (workflows / "main.yml").read_text()
        cycle = (workflows / "cycle.yml").read_text()
        stage = (workflows / "stage.yml").read_text()
        self.assertIn("until: ${{ iteration.outputs.settled }}", main)
        self.assertIn("provisional:", main)
        self.assertIn("- alignment\n", cycle)
        self.assertIn("if: ${{ needs.coverage.outputs.result.waiting }}", cycle)
        self.assertIn("value: ${{ jobs.coverage.outputs.result.settled }}", cycle)
        self.assertEqual(stage.count("needs.identities.outputs.result.current"), 2)


if __name__ == "__main__":
    unittest.main()
