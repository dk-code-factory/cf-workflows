"""Direct and composed publication entries: dk-pr-publish and dk-pr-review-post (#31).

Preflight cases use the fail-closed fake engine transport of the dk-pr-fix
tests (only the capability query is configured). Helper cases drive the
installed helpers on a disposable repository with a fresh state namespace and a
fail-closed fake GitHub transport: it answers capture, PR edits, review
requests, review creation (including approval), review-comment reads, CI
observation, the merge requirements query, merge and merge-commit reads; any
other route raises with no live fallback. Native sessions are only the reports a
test hands the helpers, and dispatches and writes are counted. Nothing here
starts an engine, provider or host, and nothing certifies GitHub conformance.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
from prfix import app, publishing
from prfix.capture import HTTPFailure
from prfix.core import digest, read_artifact
from prfix.prepare import ENTRY_ACTIONS, ENTRY_OWNERS, ENTRY_PRESETS, entry_preset, prepare
from prfix.publication import EXTENSIONS, ISSUE_KINDS, SUPPORTED, PendingReview, PublicationError, UncertainEffect
import git_fixture
from test_dk_pr_review import AGENTS, PR_URL, adapters
from test_dk_pr_review_findings import CONFLICT, INLINE, SUBJECT, Fixture as FindingsFixture, GitHub as FindingsGitHub
from test_publication import review_page, review_request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, dk_pr_publish, dk_pr_review  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import CAPABILITIES, FakeTransport, PreflightFixture  # noqa: E402


def grant(name):
    return {"request_id": name, "instruction": "Explicitly do " + name + " on this PR.", "source": "chat:" + name}


AUDIENCE = ["agent_" + format(0xa0000 + n, "032x") for n in range(32)]


class GitHub(FindingsGitHub):
    """Adds PR edits, review requests, approval, CI, merge requirements and merge; the rest fails."""

    def __init__(self, head, target, **kwargs):
        super().__init__(head, target, **kwargs)
        self.evidence["metadata"].update(mergeable=True, mergeable_state="clean")
        self.requested = {"users": [], "teams": []}
        self.ci = {"status": "completed", "conclusion": "success"}

    def observe_ci(self, pr, head):
        self.calls["observe_ci"] += 1
        return {"head": head, "checks": [{"id": "check", **self.ci}], "evidence": "fixture-ci"}

    def _lose(self):
        if self.fault == "after":
            raise OSError("fixture response lost after effect")

    def request(self, method, path, payload=None):
        pull = "repos/owner/repo/pulls/7"
        meta = self.evidence["metadata"]
        if method == "POST" and path == pull + "/reviews" and payload.get("event") == "APPROVE":
            self.calls["request"] += 1
            self.calls["write"] += 1
            self.posted.append(copy.deepcopy(payload))
            viewer = self.evidence["viewer"]
            if any(r["state"] == "PENDING" and r["user"]["id"] == viewer["id"] for r in self.evidence["reviews"]):
                raise HTTPFailure(422, copy.deepcopy(CONFLICT))
            if self.fault == "before":
                raise OSError("fixture response lost before effect")
            review = {"id": 500 + len(self.evidence["reviews"]), "user": dict(viewer), "body": payload["body"],
                      "commit_id": payload["commit_id"], "state": "APPROVED"}
            self.evidence["reviews"].append(review)
            self.created[review["id"]] = []
            self._lose()
            return copy.deepcopy(review)
        if path.startswith(pull + "/reviews"):
            return super().request(method, path, payload)
        self.calls["request"] += 1
        if method == "PATCH" and path == pull:
            self.calls["write"] += 1
            if self.fault == "before":
                raise OSError("fixture response lost before effect")
            meta.update(payload)
            self._lose()
            return copy.deepcopy(meta)
        if path == pull + "/requested_reviewers":
            if method == "GET":
                return copy.deepcopy(self.requested)
            self.calls["write"] += 1
            if self.fault == "before":
                raise OSError("fixture response lost before effect")
            self.requested["users"] += [{"login": n} for n in payload.get("reviewers", [])]
            self.requested["teams"] += [{"slug": n} for n in payload.get("team_reviewers", [])]
            self._lose()
            return copy.deepcopy(self.requested)
        if method == "POST" and path == "graphql" and "reviewRequests" in payload["query"]:
            self.calls["requests_read"] += 1
            nodes = [review_request("User", u["login"]) for u in self.requested["users"]]
            nodes += [review_request("Team", t["slug"]) for t in self.requested["teams"]]
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": meta["head"]["sha"], "isDraft": False, "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN", "reviewDecision": None, "reviewRequests": review_page(nodes)}}}}
        if method == "PUT" and path == pull + "/merge":
            self.calls["write"] += 1
            if self.fault == "before":
                raise OSError("fixture response lost before effect")
            meta.update(state="closed", merged=True, merge_commit_sha="d" * 40,
                        merged_by={"id": self.evidence["viewer"]["id"]})
            self._lose()
            return {"merged": True, "sha": "d" * 40}
        if method == "GET" and path == "repos/owner/repo/git/commits/" + "d" * 40:
            return {"sha": "d" * 40, "parents": [{"sha": meta["base"]["sha"]}, {"sha": meta["head"]["sha"]}]}
        raise AssertionError(f"unconfigured fixture route: {method} {path}")


class Helpers(FindingsFixture):
    def setUp(self):
        super().setUp()
        self.github = GitHub(self.commit, self.target)
        self.github.git_head = self.commit
        self.audiences = 0

    def plan(self, tasks, reviewers, authorizations, permissions, mode, run, settings=None,
             instructions="Review this PR.", entry=None):
        table = {**adapters(), **{name: dict(admission.BUNDLED_ADAPTERS[name])
                                  for name in ("prepare-publication", "describe-pr")}}
        controls = {"tasks": tasks, "finalizers": [], "reviewers": reviewers, "adapters": table, "accounts": [],
                    "settings": settings or {}, "resources": [], "turns": 8, "mode": mode, "permissions": permissions}
        if entry is not None:
            controls["entry"] = entry  # publish.yml or review-post.yml (plugin.yaml).
        request = {"pr": PR_URL, "instructions": instructions, "logical_run": run, "authorizations": authorizations}
        result = prepare(self.root, request, controls, "exec-1", self.github, self.definitions)
        return result, read_artifact(self.root, result["plan"])

    def publish(self, plan, ledger, proposal):
        """publication.yml: prepare, independent audience, execute and record, or read-only recovery."""
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": proposal})
        if not prepared["invoke"]:
            return publishing.publication_recover(self.root, self.github, ledger, plan, {"id": prepared["id"]})
        self.native["audience"] += 1
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["code"]})
        done = publishing.publication_execute(self.root, self.github, ledger, plan, {
            "id": prepared["id"], "agent": AUDIENCE[self.native["audience"]], "report": verdict})
        return publishing.publication_record(self.root, self.github, ledger, plan, done)


class Publish(Helpers):
    """dk-pr-publish, changes preset: the bundled session proposes, the shared journal publishes."""

    TITLE = {"title": "Clearer title"}

    def setUp(self):
        super().setUp()
        self.github.evidence["metadata"]["user"] = {"id": 1, "login": "maintainer"}  # The actor's own PR.

    def direct(self, authorizations, run="pr_fixture"):
        result, plan = self.plan(dk_pr_publish.CHANGES["tasks"], [], authorizations,
                                 {dk_pr_publish.CAPABILITIES[a]["permission"]: True for a in authorizations},
                                 "fix", run, instructions="Publish the clearer title and ask alice to review.")
        self.assertFalse(result["merge_authorized"] or result["approval_authorized"] or result["findings_authorized"])
        return plan

    def operations(self, *extra):
        return [{"kind": "edit_pr", "head": self.commit, "target": self.target, "payload": dict(self.TITLE)},
                {"kind": "request_review", "head": self.commit, "target": self.target,
                 "payload": {"reviewers": ["alice"]}}, *extra]

    def ordinary(self, plan, ledger):
        return publishing.publication_list(self.root, self.github, ledger, plan, {"kind": "ordinary"})["items"]

    def test_direct_publication_uses_exact_intents_audience_and_receipts_once(self):
        plan = self.direct({"edit_pr": grant("edit_pr"), "request_review": grant("request_review")})
        self.assertEqual([task["skill"] for task in plan["tasks"]], ["prepare-publication"])
        ledger = self.ledger(plan)
        # A session-proposed merge or approval is never listed; they come only from their own gates.
        extra = ({"kind": "merge", "head": self.commit, "target": self.target, "payload": {"merge_method": "merge"}},
                 {"kind": "approve_pr", "head": self.commit, "target": self.target, "payload": {"body": ""}})
        self.execute(plan, ledger, "prepare-publication", AGENTS[0], self.report(operations=self.operations(*extra)))
        items = self.ordinary(plan, ledger)
        self.assertEqual([item["kind"] for item in items], ["edit_pr", "request_review"])
        receipts = [self.publish(plan, ledger, item) for item in items]
        self.assertEqual(self.writes(), 2)
        self.assertEqual(self.github.evidence["metadata"]["title"], "Clearer title")
        self.assertEqual(self.github.requested["users"], [{"login": "alice"}])
        for receipt, item in zip(receipts, items):
            self.assertEqual(set(receipt["receipt"]), {"id", "fingerprint", "run", "kind", "pr", "head", "actor_id",
                                                       "result", "evidence"})
            self.assertEqual((receipt["receipt"]["kind"], receipt["receipt"]["run"]), (item["kind"], "pr_fixture"))
        # An unchanged rerun reconciles the applied intents read-only.
        again = [self.publish(plan, ledger, item) for item in self.ordinary(plan, ledger)]
        self.assertEqual(again, receipts)
        self.assertEqual((self.writes(), self.native), (2, Counter({"task": 1, "audience": 2})))
        self.assertEqual(publishing.merge_proposal(self.root, self.github, ledger, plan, {}), {"invoke": False, "proposal": {}})
        self.assertEqual(publishing.approval_proposal(self.root, self.github, ledger, plan, {}),
                         {"invoke": False, "proposal": {}})

    def test_denied_action_rejects_before_any_write(self):
        plan = self.direct({"edit_pr": grant("edit_pr")})
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "prepare-publication", AGENTS[0], self.report(operations=self.operations()))
        edit, review = self.ordinary(plan, ledger)
        with self.assertRaisesRegex(PublicationError, "explicit user authorization required for request_review"):
            self.publish(plan, ledger, review)
        self.publish(plan, ledger, edit)
        self.assertEqual((self.writes(), self.github.requested["users"]), (1, []))

    def test_payload_drift_blocks_without_a_write_and_a_fresh_attempt_follows(self):
        plan = self.direct({"edit_pr": grant("edit_pr")})
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "prepare-publication", AGENTS[0], self.report(operations=self.operations()[:1]))
        [item] = self.ordinary(plan, ledger)
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": item})
        self.github.evidence["metadata"]["title"] = "Someone else's title"  # Changed after preparation.
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["code"]})
        # The edited PR is new discussion: the proposing report is stale, so the intent blocks.
        with self.assertRaisesRegex(PublicationError, "stale|changed after preparation"):
            publishing.publication_execute(self.root, self.github, ledger, plan, {
                "id": prepared["id"], "agent": AUDIENCE[0], "report": verdict})
        self.assertEqual(self.writes(), 0)
        state = ledger.db.execute("SELECT state FROM publication_intents WHERE id=?", (prepared["id"],)).fetchone()[0]
        self.assertEqual(state, "blocked")
        self.assertEqual(self.ordinary(plan, ledger), [], "a stale proposal is never listed again")
        with self.assertRaises(PublicationError):
            self.publish(plan, ledger, item)
        # Only a refreshed session proposes again, against the new baseline.
        self.execute(plan, ledger, "prepare-publication", AGENTS[1], self.report(operations=self.operations()[:1]))
        [fresh] = self.ordinary(plan, ledger)
        self.assertNotEqual(fresh["id"], item["id"])
        receipt = self.publish(plan, ledger, fresh)
        self.assertEqual(receipt["receipt"]["result"], {"title": "Clearer title"})
        self.assertEqual(self.writes(), 1)

    def test_lost_effects_are_reconciled_and_never_resent(self):
        for fault in ("before", "after"):
            with self.subTest(fault=fault):
                self.github.evidence["metadata"]["title"] = "Change"
                self.github.calls.clear()
                plan = self.direct({"edit_pr": grant("edit_pr")}, run="pr_" + fault)
                ledger = self.ledger(plan)
                self.execute(plan, ledger, "prepare-publication", AGENTS[0],
                             self.report(operations=self.operations()[:1]))
                [item] = self.ordinary(plan, ledger)
                self.github.fault = fault
                with self.assertRaises(OSError):
                    self.publish(plan, ledger, item)
                self.github.fault = None
                if fault == "before":
                    for _ in range(2):
                        with self.assertRaisesRegex(PublicationError, "unresolved"):
                            self.publish(plan, ledger, item)
                    with self.assertRaises(UncertainEffect):
                        publishing._publication(self.root, self.github, ledger, plan).execute(item["id"], {})
                else:
                    receipt = self.publish(plan, ledger, item)
                    self.assertEqual(receipt["receipt"]["result"], {"title": "Clearer title"})
                self.assertEqual(self.writes(), 1)

    def test_composed_dk_pr_fix_publication_shares_the_journal_and_never_duplicates(self):
        plan = self.direct({"edit_pr": grant("edit_pr")})
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "prepare-publication", AGENTS[0], self.report(operations=self.operations()[:1]))
        direct = self.publish(plan, ledger, self.ordinary(plan, ledger)[0])
        # The composed run of the same logical run: describe-pr proposes the same edit.
        _, composed = self.plan(["describe-pr", {"stage": "publication"}], [], {"edit_pr": grant("edit_pr")},
                                {"edit_pr": True}, "fix", "pr_fixture", instructions="Fix and describe this PR.")
        ledger = self.ledger(composed)
        self.execute(composed, ledger, "describe-pr", AGENTS[2], self.report(operations=self.operations()[:1]))
        [item] = self.ordinary(composed, ledger)
        receipt = self.publish(composed, ledger, item)
        self.assertEqual(self.writes(), 1, "the exact existing effect settles the composed intent without a write")
        self.assertEqual(set(receipt["receipt"]), set(direct["receipt"]))
        self.assertEqual(receipt["receipt"]["result"], direct["receipt"]["result"])


class ReviewPost(Helpers):
    """dk-pr-review-post and approval over the shared incoming-review run."""

    RUN = "review_fixture"

    def review(self, authorizations, permissions, settings=None, reviewers=None):
        result, plan = self.plan([{"stage": "reviews"}], reviewers or ["review"], authorizations, permissions,
                                 "review", self.RUN, settings)
        return result, plan, self.ledger(plan)

    def current(self, plan, ledger, skill):
        snapshot = app._snapshot(self.root, self.github, plan)
        return app._current(ledger, snapshot, self.task(plan, skill))["report"]

    def approve(self, plan, ledger):
        final = publishing.approval_proposal(self.root, self.github, ledger, plan, {})
        return final, (self.publish(plan, ledger, final["proposal"]) if final["invoke"] else None)

    def test_direct_post_reuses_composed_review_and_composed_rerun_adds_nothing(self):
        # Composed incoming review without publication authority.
        _, plan, ledger = self.review({}, {})
        self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", ("rules",), commit=self.commit)])
        # Direct dk-pr-review-post: same run and instruction, blocking_event COMMENT.
        result, post, ledger = self.review({"post_findings": grant("post")}, {"post_findings": True},
                                           {"request_changes": False})
        self.assertTrue(result["findings_authorized"])
        for skill, agent in (("review", AGENTS[3]), ("finding_verification", AGENTS[4])):
            self.assertFalse(self.execute(post, ledger, skill, agent)["invoke"], "current review is reused")
        listing, receipts = self.barrier(post, ledger)
        self.assertEqual(self.writes(), 1)
        payload = self.github.posted[0]
        self.assertEqual((payload["event"], payload["comments"]), ("COMMENT", []))
        self.assertIn(SUBJECT, payload["body"])
        self.assertFalse(payload["body"].startswith("1 independently"), "summary suppressed")
        # Composed dk-pr-review with both publication actions: nothing new, and no approval of findings.
        _, composed, ledger = self.review({"post_findings": grant("post"), "approve_pr": grant("approve")},
                                          {"post_findings": True, "approve_pr": True})
        self.assertEqual(self.barrier(composed, ledger)[0]["items"], [])
        final, _ = self.approve(composed, ledger)
        self.assertFalse(final["invoke"])
        self.assertIn("approval needs complete clean independent review", final["blockers"])
        self.assertEqual((self.writes(), self.native), (1, Counter({"review": 1, "verification": 1, "audience": 1})))

    def test_review_post_options_keep_configured_settings_and_current_review(self):
        # The settings dk-pr-review-post admits for configured {strict, request_changes: false}
        # plus include_summary (Entries.test_review_post_options_set_only_their_settings_over_configuration).
        configured = {"strict": True, "request_changes": False}
        _, plan, ledger = self.review({}, {}, dict(configured))
        self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", ("rules",), commit=self.commit)])
        _, post, ledger = self.review({"post_findings": grant("post")}, {"post_findings": True},
                                      {**configured, "include_summary": True})
        for skill, agent in (("review", AGENTS[3]), ("finding_verification", AGENTS[4])):
            self.assertFalse(self.execute(post, ledger, skill, agent)["invoke"], "current review is reused")
        self.barrier(post, ledger)
        payload = self.github.posted[0]
        self.assertEqual(payload["event"], "COMMENT", "configured request_changes: false is kept")
        self.assertTrue(payload["body"].startswith("1 independently verified finding"))
        self.assertEqual((self.writes(), self.native), (1, Counter({"review": 1, "verification": 1, "audience": 1})))
        # Dropping an analysis setting such as strict would make the review stale.
        _, dropped, ledger = self.review({"post_findings": grant("post")}, {"post_findings": True},
                                         {"request_changes": False, "include_summary": True})
        self.assertTrue(self.execute(dropped, ledger, "review", AGENTS[5])["invoke"])

    def test_partial_review_comments_and_is_never_approved(self):
        _, plan, ledger = self.review({"post_findings": grant("post"), "approve_pr": grant("approve")},
                                      {"post_findings": True, "approve_pr": True}, reviewers=[["review", "simplify"]])
        self.reviewed(plan, ledger, [self.finding("inline", INLINE, "diff", path="code.py", line=1, side="RIGHT")])
        self.barrier(plan, ledger)
        self.assertEqual(self.github.posted[0]["event"], "COMMENT")
        self.assertFalse(self.approve(plan, ledger)[0]["invoke"])
        self.assertEqual(self.writes(), 1)

    def test_clean_review_is_approved_once_with_an_empty_body(self):
        _, plan, ledger = self.review({"approve_pr": grant("approve")}, {"approve_pr": True})
        self.reviewed(plan, ledger, [])
        self.assertEqual(self.listed(plan, ledger)["items"], [], "approval never posts findings")
        final, receipt = self.approve(plan, ledger)
        self.assertEqual((final["proposal"]["kind"], final["proposal"]["payload"]), ("approve_pr", {"body": ""}))
        self.assertEqual(self.github.posted, [{"commit_id": self.commit, "event": "APPROVE", "body": "", "comments": []}])
        self.assertEqual(receipt["receipt"]["result"]["review"]["state"], "APPROVED")
        self.assertEqual(self.approve(plan, ledger)[1], receipt)
        self.assertEqual((self.writes(), self.native["audience"]), (1, 1))
        # Merge readiness stays separate: approval never readies the PR.
        self.assertEqual(publishing.merge_proposal(self.root, self.github, ledger, plan, {})["invoke"], False)

    def test_approval_needs_its_own_grant_and_never_approves_own_pr(self):
        _, plan, ledger = self.review({"post_findings": grant("post")}, {"post_findings": True})
        self.reviewed(plan, ledger, [])
        self.assertEqual(self.approve(plan, ledger)[0], {"invoke": False, "proposal": {}})
        _, plan, ledger = self.review({"approve_pr": grant("approve")}, {"approve_pr": True})
        self.github.evidence["metadata"]["user"] = {"id": 1, "login": "maintainer"}
        self.reviewed(plan, ledger, [])
        final, _ = self.approve(plan, ledger)
        self.assertIn("the authenticated account never approves its own PR", final["blockers"])
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM publication_intents").fetchone()[0], 0)
        self.assertEqual(self.writes(), 0)

    def test_existing_pending_draft_refuses_approval_before_any_intent(self):
        draft = {"id": 77, "user": {"id": 1}, "state": "PENDING", "body": "Draft", "commit_id": self.commit}
        self.github.evidence["reviews"].append(copy.deepcopy(draft))
        _, plan, ledger = self.review({"approve_pr": grant("approve")}, {"approve_pr": True})
        self.reviewed(plan, ledger, [])
        final = publishing.approval_proposal(self.root, self.github, ledger, plan, {})
        self.assertTrue(final["invoke"], final)
        with self.assertRaises(PendingReview):
            self.publish(plan, ledger, final["proposal"])
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM publication_intents").fetchone()[0], 0)
        self.assertEqual((self.writes(), self.github.evidence["reviews"]), (0, [draft]))

    def test_draft_after_preparation_blocks_and_lost_effects_never_resend(self):
        _, plan, ledger = self.review({"approve_pr": grant("approve")}, {"approve_pr": True})
        self.reviewed(plan, ledger, [])
        final = publishing.approval_proposal(self.root, self.github, ledger, plan, {})
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": final["proposal"]})
        draft = {"id": 77, "user": {"id": 1}, "state": "PENDING", "body": "Draft", "commit_id": self.commit}
        self.github.evidence["reviews"].append(copy.deepcopy(draft))
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["code"]})
        with self.assertRaises(PublicationError):
            publishing.publication_execute(self.root, self.github, ledger, plan,
                                           {"id": prepared["id"], "agent": AUDIENCE[0], "report": verdict})
        state = ledger.db.execute("SELECT state FROM publication_intents WHERE id=?", (prepared["id"],)).fetchone()[0]
        self.assertEqual((state, self.writes(), self.github.evidence["reviews"]), ("blocked", 0, [draft]))
        self.github.evidence["reviews"] = []
        self.github.fault = "before"
        with self.assertRaises(OSError):
            self.publish(plan, ledger, final["proposal"])
        self.github.fault = None
        attempt = ledger.db.execute("SELECT id,state FROM publication_intents ORDER BY rowid DESC").fetchone()
        self.assertEqual(tuple(attempt), (prepared["id"] + ".r1", "pending"))
        for _ in range(2):
            with self.assertRaisesRegex(PublicationError, "unresolved"):
                self.publish(plan, ledger, final["proposal"])
        self.assertEqual((self.writes(), self.github.evidence["reviews"]), (1, []))

    def test_lost_approval_response_after_the_effect_settles_by_reconciliation(self):
        _, plan, ledger = self.review({"approve_pr": grant("approve")}, {"approve_pr": True})
        self.reviewed(plan, ledger, [])
        final = publishing.approval_proposal(self.root, self.github, ledger, plan, {})
        self.github.fault = "after"
        with self.assertRaises(OSError):
            self.publish(plan, ledger, final["proposal"])
        self.github.fault = None
        receipt = self.publish(plan, ledger, final["proposal"])
        self.assertEqual(receipt["receipt"]["result"]["review"]["state"], "APPROVED")
        self.assertEqual(self.writes(), 1)


class DirectMerge(Helpers):
    """dk-pr-publish, merge preset: incoming-review readiness; nothing bypasses the live merge checks."""

    def merge_plan(self):
        _, plan = self.plan([{"stage": "reviews"}], ["review"], {"merge": grant("merge")}, {"merge": True},
                            "review", "review_fixture")
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [])
        return plan, ledger

    def test_pending_requests_hold_merge_before_and_after_preparation(self):
        plan, ledger = self.merge_plan()
        self.github.requested["users"] = [{"login": "alice"}]
        held = publishing.merge_proposal(self.root, self.github, ledger, plan, {})
        self.assertFalse(held["invoke"])
        self.assertTrue(any("user @alice" in blocker for blocker in held["blockers"]), held)
        self.github.requested["users"] = []
        final = publishing.merge_proposal(self.root, self.github, ledger, plan, {})
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": final["proposal"]})
        self.github.requested["teams"] = [{"slug": "core"}]  # Requested after preparation.
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["code"]})
        with self.assertRaisesRegex(PublicationError, "incomplete|team @o/core"):
            publishing.publication_execute(self.root, self.github, ledger, plan, {
                "id": prepared["id"], "agent": AUDIENCE[0], "report": verdict})
        state = ledger.db.execute("SELECT state FROM publication_intents WHERE id=?", (prepared["id"],)).fetchone()[0]
        self.assertEqual((state, self.writes()), ("prepared", 0))
        # A native merge proposal never becomes a write in review mode.
        self.assertEqual(publishing.publication_list(self.root, self.github, ledger, plan, {"kind": "merge"})["items"], [])
        self.github.requested["teams"] = []
        receipt = publishing.publication_execute(self.root, self.github, ledger, plan, {
            "id": prepared["id"], "agent": AUDIENCE[0], "report": verdict})
        self.assertEqual(receipt["receipt"]["kind"], "merge")
        self.assertEqual(self.writes(), 1)

    def test_current_head_and_lost_merge_response(self):
        plan, ledger = self.merge_plan()
        final = publishing.merge_proposal(self.root, self.github, ledger, plan, {})
        # The PR head moved away from the reviewed revision: analysis is provisional, nothing publishes.
        self.github.evidence["metadata"]["head"]["sha"] = "e" * 40
        self.assertFalse(publishing.merge_proposal(self.root, self.github, ledger, plan, {})["invoke"])
        with self.assertRaisesRegex(PublicationError, "withheld while analysis is provisional"):
            self.publish(plan, ledger, final["proposal"])
        self.assertEqual(self.writes(), 0)
        self.github.evidence["metadata"]["head"]["sha"] = self.commit
        self.github.fault = "after"
        with self.assertRaises(OSError):
            self.publish(plan, ledger, final["proposal"])
        self.github.fault = None
        receipt = publishing.publication_recover(self.root, self.github, ledger, plan, {"id": final["proposal"]["id"]})
        self.assertEqual(receipt["receipt"]["result"]["commit"]["sha"], "d" * 40)
        self.assertEqual(self.writes(), 1)


class InstalledRouting(Helpers):
    """The installed helpers repeat each entry's routing, so a direct run of its workflow rejects before any effect."""

    PRESETS = {"changes": (dk_pr_publish.CHANGES, "fix", "pr_fixture"),
               "merge": (dk_pr_publish.REVIEW, "review", "review_fixture"),
               "review-post": (dk_pr_publish.REVIEW, "review", "review_fixture")}

    def run_preset(self, entry, authorizations, preset=None, **changes):
        options, mode, run = self.PRESETS[preset or dk_pr_publish.CAPABILITIES[next(iter(authorizations))]["preset"]]
        arguments = {"tasks": options["tasks"], "reviewers": options["reviewers"], "mode": mode, "run": run, **changes}
        permissions = {dk_pr_publish.CAPABILITIES[a]["permission"]: True for a in authorizations
                       if a in dk_pr_publish.CAPABILITIES}
        return self.plan(arguments["tasks"], arguments["reviewers"], authorizations, permissions, arguments["mode"],
                         arguments["run"], entry=entry)

    def staged(self, authorizations):
        return git_fixture.stage_request(self, {"pr": PR_URL, "instructions": "Publish exactly this.",
                                                "authorizations": authorizations})

    def route(self, reference, entry, phase="execute"):
        import action
        request = {"protocol": "action-v1", "phase": phase, "input": {"request": reference, "entry": entry},
                   "context": {"operation_key": "admit:" + entry + ":" + reference, "execution_fingerprint": "exec-1"}}
        return action.handle("entry_preset", request, self.root, self.github)

    def setUp(self):
        super().setUp()
        self.github.evidence["metadata"]["user"] = {"id": 1, "login": "maintainer"}  # The actor's own PR.

    def test_declared_actions_match_the_entry_capabilities(self):
        # The installed table is the host CAPABILITIES table of these two entries; every other
        # declared action (fork-CI approval, issue writes) has its own owner in both.
        own = {action: record for action, record in dk_pr_publish.CAPABILITIES.items()
               if record["entry"] in ENTRY_ACTIONS}
        for action, record in own.items():
            self.assertEqual(ENTRY_ACTIONS[record["entry"]][action], record["preset"], action)
        self.assertEqual({a for actions in ENTRY_ACTIONS.values() for a in actions}, set(own))
        self.assertEqual(set(ENTRY_OWNERS), set(dk_pr_publish.CAPABILITIES) - set(own) | set(dk_pr_publish.OWNERS))
        for preset, (options, mode, _) in self.PRESETS.items():
            self.assertEqual(ENTRY_PRESETS[preset][0], mode)
            self.assertEqual(dk_pr_publish.PRESETS[preset]["mode"], mode)

    def test_each_entry_runs_exactly_its_preset(self):
        cases = (("dk-pr-publish", {"edit_pr": grant("e"), "request_review": grant("r")}),
                 ("dk-pr-publish", {"merge": grant("m")}),
                 ("dk-pr-review-post", {"post_findings": grant("p"), "approve_pr": grant("a")}))
        for entry, authorizations in cases:
            with self.subTest(entry=entry, actions=sorted(authorizations)):
                result, plan = self.run_preset(entry, authorizations)
                self.assertEqual(plan["mode"], dk_pr_publish.PRESETS[
                    dk_pr_publish.CAPABILITIES[next(iter(authorizations))]["preset"]]["mode"])
                self.assertEqual(set(plan["authorizations"]), set(authorizations))
        self.assertEqual(self.writes(), 0)

    def test_installed_routing_rejects_before_any_session_or_effect(self):
        cases = [
            ("dk-pr-publish", {}, "explicit authorization"),
            ("dk-pr-publish", {"approve_pr": grant("a")}, "belongs to cf-workflows:dk-pr-review-post"),
            ("dk-pr-publish", {"post_findings": grant("p")}, "belongs to cf-workflows:dk-pr-review-post"),
            ("dk-pr-review-post", {"merge": grant("m")}, "belongs to cf-workflows:dk-pr-publish"),
            ("dk-pr-review-post", {"push": grant("p")}, "belongs to cf-workflows:dk-pr-publish"),
            ("dk-pr-publish", {"approve_fork_ci": grant("a")}, "dk-ci-approve"),
            ("dk-pr-review-post", {"approve_fork_ci": grant("a")}, "dk-ci-approve"),
            ("dk-pr-publish", {"create_issue": grant("i")}, "#32"),
            ("dk-pr-publish", {"edit_issue": grant("i")}, "#32"),
            ("dk-pr-publish", {"iterate": grant("i")}, "no declared publication capability"),
            ("dk-pr-publish", {"merge": grant("m"), "push": grant("p")}, "separate invocations"),
            ("dk-pr-fix", {"edit_pr": grant("e")}, "unknown publication entry"),
        ]
        for entry, authorizations, message in cases:
            with self.subTest(entry=entry, actions=sorted(authorizations)):
                with self.assertRaisesRegex(ValueError, re.escape(message)):
                    entry_preset(entry, authorizations)
                if entry in ENTRY_ACTIONS:
                    with self.assertRaisesRegex(ValueError, re.escape(message)):
                        self.run_preset(entry, authorizations, preset="changes" if entry == "dk-pr-publish"
                                        else "review-post")
        # A preset in the wrong mode, or with another stage shape or a writing task, never runs as the entry.
        overrides = [
            ("dk-pr-publish", {"edit_pr": grant("e")}, {"mode": "review", "tasks": ["read-feedback", {"stage": "reviews"}],
                                                       "reviewers": [["review"]]}, "fix mode only"),
            ("dk-pr-publish", {"merge": grant("m")}, {"mode": "fix", "tasks": dk_pr_publish.CHANGES["tasks"],
                                                     "reviewers": []}, "review mode only"),
            ("dk-pr-publish", {"edit_pr": grant("e")}, {"tasks": ["describe-pr", {"stage": "publication"}]},
             "exactly its changes preset"),
            ("dk-pr-publish", {"edit_pr": grant("e")}, {"tasks": ["prepare-publication", {"stage": "reviews"},
                                                                  {"stage": "publication"}], "reviewers": [["review"]]},
             "exactly its changes preset"),
            ("dk-pr-review-post", {"post_findings": grant("p")}, {"tasks": [{"stage": "reviews"}]},
             "exactly its review-post preset"),
        ]
        for entry, authorizations, changes, message in overrides:
            with self.subTest(entry=entry, changes=changes):
                with self.assertRaisesRegex(ValueError, re.escape(message)):
                    self.run_preset(entry, authorizations, **changes)
        self.assertEqual((self.writes(), self.native), (0, Counter()))

    def test_installed_admit_job_routes_the_staged_request_once(self):
        for entry, authorizations, preset, mode in (
                ("dk-pr-publish", {"edit_pr": grant("e")}, "changes", "fix"),
                ("dk-pr-publish", {"merge": grant("m")}, "merge", "review"),
                ("dk-pr-review-post", {"post_findings": grant("p")}, "review-post", "review")):
            with self.subTest(entry=entry, preset=preset):
                reference = self.staged(authorizations)
                self.assertEqual(self.route(reference, entry, "reconcile")["outcome"], "no_effect")
                result = self.route(reference, entry)["result"]
                self.assertEqual(result["output"], {"preset": preset, "mode": mode})
                # A lost response is answered from the retained result, never re-read.
                self.assertEqual(self.route(reference, entry, "reconcile")["result"], result)
                self.assertTrue(self.route(reference, entry, "validate_checkpoint")["valid"] is False)
        for entry, authorizations, message in (
                ("dk-pr-publish", {"merge": grant("m"), "edit_pr": grant("e")}, "separate invocations"),
                ("dk-pr-review-post", {"merge": grant("m")}, "belongs to cf-workflows:dk-pr-publish"),
                ("dk-pr-publish", {}, "explicit authorization")):
            with self.subTest(entry=entry, actions=sorted(authorizations)):
                reference = self.staged(authorizations)
                with self.assertRaisesRegex(ValueError, re.escape(message)):
                    self.route(reference, entry)
                # No routing result is retained, so neither preset job can start.
                self.assertEqual(self.route(reference, entry, "reconcile")["outcome"], "no_effect")
        self.assertEqual((self.writes(), self.native, self.github.calls), (0, Counter(), Counter()))


class ApproveKind(unittest.TestCase):
    """The approve_pr extension kind on the shared Publication boundary, unit level."""

    def setUp(self):
        import tempfile
        from prfix.core import Ledger, identity
        from test_dk_pr_fix_publication import Transport
        from test_publication import BASE, GRANTS, HEAD
        import git_fixture
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw approve ")
        self.addCleanup(temp.cleanup)
        self.head, self.base = HEAD, BASE
        pr = identity("https://github.com/o/r/pull/7")
        self.plan = {"logical_run": "approve", "pr": pr, "instructions": "Approve when clean.",
                     "authorizations": dict(GRANTS), "options": {"permissions": {"approve_pr": True}, "accounts": []}}
        self.root = Path(temp.name)
        self.ledger = self.fresh("approve")

        class Approving(Transport):
            def request(inner, method, path, payload=None):
                if method == "POST" and path.endswith("/reviews") and payload["event"] == "APPROVE":
                    inner.counts["write"] += 1
                    inner.calls.append((method, path, copy.deepcopy(payload)))
                    if inner.fault == "before":
                        raise OSError("fixture response lost before effect")
                    review = {"id": 500 + len(inner.ev["reviews"]), "user": copy.deepcopy(inner.ev["viewer"]),
                              "body": payload["body"], "commit_id": payload["commit_id"], "state": "APPROVED"}
                    inner.ev["reviews"].append(review)
                    inner.review_comments[review["id"]] = []
                    if inner.fault == "after":
                        raise OSError("fixture response lost after effect")
                    return copy.deepcopy(review)
                return super().request(method, path, payload)

        self.provider = Approving()
        self.provider.ev["metadata"]["user"] = {"id": 21, "login": "author"}
        self.evidence = {"approve": True, "head": HEAD, "target": BASE, "evidence": ["accepted:review"]}
        self.publisher = self.make()

    def fresh(self, run):
        from prfix.core import Ledger
        self.plan["logical_run"] = run
        ledger = Ledger(self.root, run, self.plan["pr"]["url"])
        self.addCleanup(ledger.close)
        return ledger

    def make(self, approval=True):
        from prfix.publication import Publication
        return Publication(self.ledger, self.provider, Path(self.ledger.root), self.plan,
                           source_validator=lambda source: source.startswith("accepted:"),
                           approval=(lambda intent: copy.deepcopy(self.evidence)) if approval else None)

    def proposal(self, ident="approve"):
        return {"id": ident, "kind": "approve_pr", "head": self.head, "target": self.base, "payload": {"body": ""},
                "sources": ["accepted:review"]}

    def audience(self, prepared):
        return {"fingerprint": prepared["fingerprint"], "suitable": True, "audience": "PR participants",
                "evidence": ["accepted:audience"]}

    def state(self, ident):
        return self.ledger.db.execute("SELECT state FROM publication_intents WHERE id=?", (ident,)).fetchone()[0]

    def test_prepare_dispatch_and_reconcile_with_lost_responses(self):
        for fault in (None, "before", "after"):
            with self.subTest(fault=fault):
                self.ledger, self.provider.counts["write"] = self.fresh("approve_" + str(fault)), 0
                self.publisher = self.make()
                prepared = self.publisher.prepare(self.proposal())
                self.assertEqual((prepared["intent"]["action"], prepared["intent"]["permission"]),
                                 ("approve_pr", "approve_pr"))
                self.provider.fault = fault
                if fault is None:
                    receipt = self.publisher.execute(prepared["id"], self.audience(prepared))
                else:
                    with self.assertRaises(OSError):
                        self.publisher.execute(prepared["id"], self.audience(prepared))
                    self.provider.fault = None
                    self.assertEqual(self.state(prepared["id"]), "pending")
                    if fault == "before":
                        with self.assertRaises(UncertainEffect):
                            self.publisher.execute(prepared["id"], self.audience(prepared))
                        self.assertEqual(self.publisher.reconcile(prepared["id"])["outcome"], "unknown")
                        self.assertEqual((self.state(prepared["id"]), self.provider.counts["write"]), ("pending", 1))
                        continue
                    receipt = self.publisher.execute(prepared["id"], self.audience(prepared))
                self.assertEqual(receipt["result"]["review"]["state"], "APPROVED")
                self.assertTrue(self.publisher.validate(prepared["id"], receipt))
                self.assertEqual(self.publisher.execute(prepared["id"], self.audience(prepared)), receipt)
                self.assertEqual(self.provider.counts["write"], 1)

    def test_own_pr_missing_evidence_and_drafts_never_approve(self):
        self.provider.ev["metadata"]["user"] = {"id": 11, "login": "maintainer"}  # The actor.
        with self.assertRaisesRegex(PublicationError, "never approves its own PR"):
            self.publisher.prepare(self.proposal())
        self.provider.ev["metadata"]["user"] = {"id": 21, "login": "author"}
        self.plan["options"]["accounts"] = [{"host": "github.com", "id": 21}]  # A configured personal account.
        with self.assertRaisesRegex(PublicationError, "never approves its own PR"):
            self.publisher.prepare(self.proposal())
        self.plan["options"]["accounts"] = []
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM publication_intents").fetchone()[0], 0)
        prepared = self.publisher.prepare(self.proposal())
        for evidence, message in (({**self.evidence, "approve": False}, "clean independent review"),
                                  ({**self.evidence, "head": "c" * 40}, "another head"),
                                  (None, "unavailable")):
            with self.subTest(message=message):
                publisher = self.make(approval=evidence is not None)
                if evidence is not None:
                    self.evidence, saved = evidence, self.evidence
                with self.assertRaisesRegex(PublicationError, message):
                    publisher.execute(prepared["id"], self.audience(prepared))
                if evidence is not None:
                    self.evidence = saved
                self.assertEqual(self.state(prepared["id"]), "prepared")
        self.provider.ev["reviews"] = [{"id": 77, "user": {"id": 11}, "state": "PENDING", "body": "Draft",
                                        "commit_id": self.head}]
        with self.assertRaises(PendingReview):
            self.publisher.execute(prepared["id"], self.audience(prepared))
        self.assertEqual((self.state(prepared["id"]), self.provider.counts["write"]), ("blocked", 0))

    def test_approval_needs_its_own_permission_and_authorization(self):
        for permissions, authorizations, message in (({"post_findings": True, "merge": True}, None, "permission"),
                                                     (None, {"post_findings": {"request_id": "x", "instruction": "y",
                                                                               "source": "z"}}, "authorization")):
            with self.subTest(message=message):
                plan = copy.deepcopy(self.plan)
                if permissions is not None:
                    plan["options"]["permissions"] = permissions
                if authorizations is not None:
                    plan["authorizations"] = authorizations
                from prfix.publication import Publication
                with self.assertRaisesRegex(PublicationError, message):
                    Publication(self.ledger, self.provider, Path(self.ledger.root), plan,
                                source_validator=lambda source: True).prepare(self.proposal())
        self.assertEqual(sum(self.provider.counts.values()), 0)


class Entries(PreflightFixture):
    """Entry presets, declared capabilities and rejections before any effect."""

    def entry(self, name, authorizations, invocation=None, **changes):
        self.transport = changes.pop("transport", FakeTransport(changes.pop("capabilities", CAPABILITIES)))
        arguments = {'request': {**self.request, 'authorizations': authorizations}, 'deployment': self.deployment,
                     'repository': self.repo, 'home': self.home, 'invocation': invocation, **changes}
        result = dk_pr_publish.preflight(name, transport=self.transport, install=self.clean, **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls), {'capabilities'})
        self.assertFalse(self.home.exists(), 'preflight writes nothing')
        return result

    def enable(self, *actions):
        return {'permissions': {action: True for action in actions}}

    def test_every_supported_action_has_one_declared_capability(self):
        kinds = [kind for record in dk_pr_publish.CAPABILITIES.values() for kind in record['kinds']]
        self.assertEqual(sorted(kinds), sorted(set(SUPPORTED) - {'rewrite'}))
        for action, record in dk_pr_publish.CAPABILITIES.items():
            for kind in record['kinds']:
                self.assertEqual(SUPPORTED[kind][0], action)
            self.assertEqual(fix_preflight.PUBLICATION_ACTIONS[action], record['permission'])
        self.assertEqual(set(dk_pr_publish.CAPABILITIES) | set(dk_pr_publish.OWNERS), set(admission.ACTIONS))
        # Issue kinds are supported for cf-workflows:dk-issue (#32), never for these entries.
        self.assertEqual(set(EXTENSIONS) | set(ISSUE_KINDS), set(dk_pr_publish.OWNERS))
        self.assertNotIn('approve_pr', EXTENSIONS)

    def test_role_prompts_match_the_pinned_baseline(self):
        inventory = json.loads((ROOT / 'baselines' / 'inventory.json').read_text())
        rows = {row['name']: row for row in inventory['skills']}
        for name, entry in dk_pr_publish.ENTRIES.items():
            resources = {item['path']: item['sha256'] for item in rows[name]['baseline_resources']}
            self.assertEqual(hashlib.sha256(entry['role_prompt'].encode()).hexdigest(),
                             resources[entry['baseline_prompt']])
            self.assertEqual(rows[name]['entrypoint'], entry['skill'])

    def test_changes_preset_runs_its_own_adapter_and_publication_stage_only(self):
        result = self.entry('dk-pr-publish', {'edit_pr': grant('e'), 'request_review': grant('r')},
                            self.enable('edit_pr', 'request_review'))
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['semantic'], self.transport.calls), (None, Counter(capabilities=1)))
        inputs = result['inputs']
        self.assertEqual((inputs['tasks'], inputs['finalizers'], inputs['reviewers']),
                         (dk_pr_publish.CHANGES['tasks'], [], []))
        self.assertEqual(set(inputs['adapters']), {'prepare-publication'})
        self.assertEqual({k for k, v in inputs['permissions'].items() if v}, {'edit_pr', 'request_review'})
        self.assertEqual((result['workflow'], result['installation']['entry']), ('cf-workflows:dk-pr-publish',
                                                                                 'workflows/publish.yml'))
        self.assertEqual(result['entry']['preset'], 'changes')
        self.assertEqual(result['state']['request_document']['logical_run'],
                         'pr_' + admission.digest(admission.pr_identity(self.request['pr'], 'pr'))[:32])

    def test_merge_preset_is_incoming_review_with_its_own_grant(self):
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        result = self.entry('dk-pr-publish', {'merge': grant('m')}, self.enable('merge'), capabilities=offered)
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual((result['installation']['entry'], result['entry']['mode']), ('workflows/publish.yml', 'review'))
        # publish.yml takes the merge path's selections under its review_ inputs.
        inputs = result['inputs']
        self.assertFalse({'tasks', 'reviewers', 'adapters'} & set(inputs))
        self.assertEqual((inputs['review_tasks'], inputs['finalizers'], inputs['review_reviewers']),
                         (dk_pr_review.DEFAULTS['tasks'], [], dk_pr_review.DEFAULTS['reviewers']))
        self.assertEqual(set(inputs['review_adapters']), {'read-feedback', 'review', 'simplify'})
        self.assertEqual(result['inputs']['merge_method'], 'squash')
        self.assertEqual({k for k, v in result['inputs']['permissions'].items() if v}, {'merge'})
        self.assertEqual(result['state']['request_document']['logical_run'],
                         dk_pr_review.logical_run(self.request['pr']))
        result = self.entry('dk-pr-publish', {'merge': grant('m')}, self.enable('merge'), capabilities=offered,
                            options={'default_merge_method': 'merge'})
        self.assertEqual(result['inputs']['merge_method'], 'merge')
        # The engine merge guard is still required.
        self.assertEqual(self.entry('dk-pr-publish', {'merge': grant('m')}, self.enable('merge'))['preflight'], 'rejected')

    def test_installed_entries_declare_and_route_every_preflight_input(self):
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')
        descriptor = (fix_preflight.INSTALL / 'plugin.yaml').read_text()
        cases = (('dk-pr-publish', {'edit_pr': grant('e')}, 'changes', 'fix', ''),
                 ('dk-pr-publish', {'merge': grant('m')}, 'merge', 'review', 'review_'),
                 ('dk-pr-review-post', {'post_findings': grant('p'), 'approve_pr': grant('a')}, 'post', 'review', ''))
        for name, authorizations, job, mode, prefix in cases:
            with self.subTest(name=name, job=job):
                result = self.entry(name, authorizations, self.enable(*authorizations), capabilities=offered)
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                entry = result['installation']['entry']
                self.assertIn(f'  - name: {name}\n    path: skills/{name}/SKILL.md\n    workflow: {entry}\n',
                              descriptor)
                body = (fix_preflight.INSTALL / entry).read_text()
                self.assertIn(f'name: cf-workflows:{name}\n', body)
                # The preset's job runs main.yml in the preset's mode under the entry's routing.
                section = re.search(rf'\n  {job}:\n    uses: workflows/main.yml\n(.*?)(?=\n  [a-z_]+:\n|\Z)',
                                    body, re.S).group(1)
                self.assertIn(f'      mode: {mode}\n      entry: {name}\n', section)
                for key in result['inputs']:
                    self.assertRegex(body, rf'\n      {key}:\n')
                    self.assertIn(f'{key.removeprefix(prefix)}: ${{{{ inputs.{key} }}}}', section)
                # The installed defaults are the entry's own preset.
                preset = dk_pr_publish.PRESETS[result['entry']['preset']]['options']
                tasks = '\n'.join(f'        - {t}' if isinstance(t, str) else f'        - stage: {t["stage"]}'
                                   for t in preset['tasks'])
                self.assertRegex(body, rf'\n      {prefix}tasks:\n(?:        #.*\n)?        type: array\n'
                                       rf'        default:\n{re.escape(tasks)}\n')
        publish = (fix_preflight.INSTALL / 'workflows' / 'publish.yml').read_text()
        # The installed helper routes the staged request before either preset can start.
        self.assertIn('operation: entry_preset', publish)
        self.assertIn("if: ${{ needs.admit.outputs.result.preset == 'changes' }}", publish)
        self.assertIn("if: ${{ needs.admit.outputs.result.preset == 'merge' }}", publish)

    def test_review_post_maps_options_and_keeps_actions_separate(self):
        result = self.entry('dk-pr-review-post', {'post_findings': grant('p'), 'approve_pr': grant('a')},
                            self.enable('post_findings', 'approve_pr'),
                            options={'blocking_event': 'COMMENT', 'include_summary': True})
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual(result['inputs']['settings'], {'include_summary': True, 'request_changes': False})
        self.assertEqual(self.transport.calls, Counter(capabilities=2))
        self.assertEqual({k for k, v in result['inputs']['permissions'].items() if v}, {'post_findings', 'approve_pr'})
        self.assertEqual(result['inputs']['tasks'], dk_pr_review.DEFAULTS['tasks'])
        default = self.entry('dk-pr-review-post', {'post_findings': grant('p')}, self.enable('post_findings'))
        self.assertEqual((default['inputs']['settings'], default['inputs']['permissions']['approve_pr']), ({}, False))

    def test_unsupported_or_misrouted_actions_reject_before_any_effect(self):
        cases = [
            ('dk-pr-publish', {}, None, 'request.authorizations', 'explicit authorization'),
            ('dk-pr-publish', {'approve_pr': grant('a')}, None, 'request.authorizations.approve_pr', 'dk-pr-review-post'),
            ('dk-pr-publish', {'post_findings': grant('a')}, None, 'request.authorizations.post_findings', 'dk-pr-review-post'),
            ('dk-pr-review-post', {'merge': grant('a')}, None, 'request.authorizations.merge', 'dk-pr-publish'),
            ('dk-pr-review-post', {'push': grant('a')}, None, 'request.authorizations.push', 'dk-pr-publish'),
            ('dk-pr-publish', {'approve_fork_ci': grant('a')}, None, 'request.authorizations.approve_fork_ci',
             'dk-ci-approve'),
            ('dk-pr-review-post', {'approve_fork_ci': grant('a')}, None, 'request.authorizations.approve_fork_ci',
             'dk-ci-approve'),
            ('dk-pr-publish', {'create_issue': grant('a')}, None, 'request.authorizations.create_issue', '#32'),
            ('dk-pr-publish', {'edit_issue': grant('a')}, None, 'request.authorizations.edit_issue', '#32'),
            ('dk-pr-publish', {'iterate': grant('a')}, None, 'request.authorizations.iterate', 'no declared'),
            ('dk-pr-publish', {'merge': grant('m'), 'push': grant('p')}, None, 'request.authorizations', 'separate'),
            ('dk-pr-publish', {'edit_pr': grant('e')}, {'options': {'tasks': ['describe-pr']}}, 'invocation', 'preset'),
            ('dk-pr-publish', {'edit_pr': grant('e')}, {'prompts': []}, 'invocation', 'preset'),
            ('dk-pr-publish', {'edit_pr': grant('e')}, {'permissions': {'merge': True}}, 'invocation.permissions.merge',
             'requested'),
        ]
        for name, authorizations, invocation, path, message in cases:
            with self.subTest(name=name, authorizations=sorted(authorizations), invocation=invocation):
                result = self.entry(name, authorizations, invocation)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))
                self.assertIn(message, result['error']['message'])
                self.assertEqual(self.transport.calls, Counter(), 'rejected before the engine is contacted')
        for name, options, path in (('dk-pr-publish', {'default_merge_method': 'rebase'}, 'options.default_merge_method'),
                                    ('dk-pr-publish', {'review_requests': {'reviewers': ['a'], 'team_reviewers': []}},
                                     'options.review_requests'),
                                    ('dk-pr-publish', {'unknown': 1}, 'options.unknown'),
                                    ('dk-pr-review-post', {'blocking_event': 'APPROVE'}, 'options.blocking_event'),
                                    ('dk-pr-review-post', {'nonblocking_event': 'REQUEST_CHANGES'},
                                     'options.nonblocking_event'),
                                    ('dk-pr-review-post', {'include_summary': 'yes'}, 'options.include_summary')):
            with self.subTest(options=options):
                result = self.entry(name, {'post_findings' if name.endswith('post') else 'edit_pr': grant('x')},
                                    options=options)
                self.assertEqual((result['preflight'], result['error']['path']), ('rejected', path))
                self.assertEqual(self.transport.calls, Counter())

    def configuration(self, **body):
        scope = {'id': 'all', 'selector': {'kind': 'repository', 'id': 'github.com/owner/repo'}, 'options': {},
                 'permissions': {}, 'prompts': [], 'skills': [], 'preferences': [], **body}
        return {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                'resources': [], 'scopes': [scope],
                'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                              'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}

    def test_review_post_options_set_only_their_settings_over_configuration(self):
        configured = {'strict': True, 'request_changes': False}
        configuration = self.configuration(options={'settings': dict(configured)})
        cases = ((None, configured, 1),
                 ({'include_summary': True}, {**configured, 'include_summary': True}, 2),
                 ({'blocking_event': 'REQUEST_CHANGES'}, {**configured, 'request_changes': True}, 2),
                 ({'blocking_event': 'COMMENT'}, configured, 1),
                 ({'nonblocking_event': 'COMMENT'}, configured, 1))
        for options, settings, queries in cases:
            with self.subTest(options=options):
                result = self.entry('dk-pr-review-post', {'post_findings': grant('p')}, self.enable('post_findings'),
                                    configuration=configuration, options=options)
                self.assertEqual(result['preflight'], 'admitted', result.get('error'))
                self.assertEqual(result['inputs']['settings'], settings)
                self.assertEqual(result['snapshot']['options']['settings']['value'], settings)
                self.assertEqual(self.transport.calls, Counter(capabilities=queries))

    def test_configured_permission_never_supplies_missing_authority(self):
        configuration = {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                         'resources': [], 'admission': {'structural': 'pending', 'semantic': None, 'request_id': None,
                                                        'context_digest': None, 'resource_digest': None,
                                                        'capability_digest': None, 'evidence_refs': []},
                         'scopes': [{'id': 'all', 'selector': {'kind': 'repository', 'id': 'github.com/owner/repo'},
                                     'options': {},
                                     'permissions': {action: True for action in admission.ACTIONS},
                                     'prompts': [], 'skills': [], 'preferences': []}]}
        result = self.entry('dk-pr-publish', {'edit_pr': grant('e')}, configuration=configuration)
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertEqual({k for k, v in result['inputs']['permissions'].items() if v}, {'edit_pr'})
        result = self.entry('dk-pr-review-post', {'post_findings': grant('p')}, configuration=configuration)
        self.assertEqual({k for k, v in result['inputs']['permissions'].items() if v}, {'post_findings'})
        # An authorization without the enabled permission rejects instead of being dropped.
        result = self.entry('dk-pr-publish', {'edit_pr': grant('e')})
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'request.authorizations.edit_pr'))

    def test_dk_pr_fix_never_approves_and_review_preflight_accepts_approval(self):
        grant_ = grant('a')
        result = fix_preflight.preflight(request={**self.request, 'authorizations': {'approve_pr': grant_}},
                                         configuration=None, invocation={'permissions': {'approve_pr': True}},
                                         deployment=self.deployment, repository=self.repo, home=self.home,
                                         transport=FakeTransport(), install=self.clean)
        self.assertEqual((result['preflight'], result['error']['path']), ('rejected', 'request.authorizations.approve_pr'))
        result = dk_pr_review.preflight(request={**self.request, 'authorizations': {'approve_pr': grant_}},
                                        configuration=None, invocation={'permissions': {'approve_pr': True}},
                                        deployment=self.deployment, repository=self.repo, home=self.home,
                                        transport=FakeTransport(), install=self.clean)
        self.assertEqual(result['preflight'], 'admitted', result.get('error'))
        self.assertTrue(result['inputs']['permissions']['approve_pr'])


if __name__ == '__main__':
    unittest.main()
