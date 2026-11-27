"""Feedback replies settled before independent review and preserved on resume (#21).

Each case uses a fresh state namespace, a disposable run ledger and feedback
record, and a fail-closed fake GitHub transport that counts captures and writes:
any route it does not model raises instead of reaching a live service. The
installed helpers run in-process; the responder and audience sessions are
represented only by the reports a test feeds them. Nothing here starts an
engine, provider or host, runs the YAML graph, or certifies GitHub conformance.
"""
from collections import Counter
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import app, feedback, publishing, responses
from prfix.core import Ledger, artifact, canonical, identity
from prfix.prepare import CHECKLIST
from prfix.publication import PublicationError
from test_publication import BASE, GRANTS, HEAD, OTHER, FakeGitHub

URL = "https://github.com/o/r/pull/7"
PRODUCER = "agent_" + "1" * 32
AUDIENCE = "agent_" + "2" * 32
MAINTAINER = {"id": 11, "login": "maintainer", "type": "User"}
REVIEWER = {"id": 2, "login": "reviewer", "type": "User"}
BOT = {"id": 4, "login": "lint[bot]", "type": "Bot"}
WRITES = ("PATCH", "PUT", "POST")


def at(minute):
    return f"2026-11-25T10:{minute:02d}:00Z"


class GitHub(FakeGitHub):
    """Counted captures and writes, timestamps on created items, injected response loss."""

    def __init__(self):
        super().__init__()
        self.counts = Counter()
        self.fault = None
        self.minute = 10
        self.ev["metadata"]["user"] = copy.deepcopy(MAINTAINER)
        self.ev["inline"].append({"id": 30, "user": REVIEWER, "body": "Please rename `value` to `count`.",
                                  "created_at": at(1), "html_url": URL + "#discussion_r30", "in_reply_to_id": None})
        self.ev["threads"].append({"id": "T30", "isResolved": False, "comments": {"nodes": [{"databaseId": 30}]}})
        self.ev["comments"].append({"id": 40, "user": BOT, "created_at": at(2), "html_url": URL + "#issuecomment-40",
                                    "body": "Consider replacing the magic number 2 with a named constant."})
        self.ev["comments"].append({"id": 41, "user": REVIEWER, "created_at": at(3), "html_url": URL + "#issuecomment-41",
                                    "body": "@bob can you confirm the release date?"})

    def capture(self, pr):
        self.counts["capture"] += 1
        for key in ("comments", "inline"):
            for item in self.ev[key]:
                if "created_at" not in item:
                    self.minute += 1
                    item["created_at"] = at(self.minute)
        return super().capture(pr)

    def request(self, method, path, payload=None):
        write = method in WRITES and not (path == "graphql" and payload["query"].startswith("query"))
        if method == "POST" and path.endswith("/replies"):
            target = int(path.split("/")[-2])
            # GitHub accepts a reply only to a thread's root comment.
            if any(item["id"] == target and item.get("in_reply_to_id") for item in self.ev["inline"]):
                raise AssertionError("fixture refuses a reply to a reply")
        if write:
            self.counts["write"] += 1
            if self.fault == "before":
                self.calls.append((method, path, copy.deepcopy(payload)))
                raise OSError("fixture response lost before effect")
        result = super().request(method, path, payload)
        if write and self.fault == "after":
            raise OSError("fixture response lost after effect")
        return result


def assessment(view, key, *requests):
    return {"source": key, "fingerprint": feedback.View.fingerprint(view.sources[key]), "requests": list(requests)}


def request(ident, excerpt, implementation="none", addressee="author", response=None, **extra):
    return {"id": ident, "excerpt": excerpt, "addressee": addressee, "implementation": implementation,
            "response": response or {"disposition": "reply"}, **extra}


class Fixture(unittest.TestCase):
    def setUp(self):
        git_fixture.state(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "source.txt").write_text("published fix\n")
        self.pr = identity(URL)
        self.plan = {
            "logical_run": "fixture", "pr": self.pr, "instructions": "Fix the feedback and answer it.",
            "options": {"permissions": {"push_commits": True, "reply_to_feedback": True, "resolve_threads": True}},
            "authorizations": dict(GRANTS),
            "tasks": [self.task("responder", "reply"), self.task("review", "review"),
                      self.task("finding_verification", "verification")],
            "stages": [{"id": "stage_1", "kind": "publication", "phase": "tasks"},
                       {"id": "stage_2", "kind": "responses", "phase": "tasks", "task": "responder"},
                       {"id": "stage_3", "kind": "reviews", "phase": "tasks"},
                       {"id": "stage_4", "kind": "responses", "phase": "finalizers", "task": "responder"}],
            "definitions": {"audience": "audience-definition"}, "helper_manifest": {},
            "limits": {"retries_per_task": 1}}
        self.ledger = self.open()
        self.github = GitHub()
        self.snapshot = {"pr": self.pr, "author_id": 11, "actor_id": 11,
            "head_repository": "github.com/o/r", "head_branch": "feature", "target_branch": "main",
            "target_tree": "target-tree", "base_tree": "base-tree", "local_tree": "fixed-tree",
            "diff": "diff", "discussion": "discussion", "instructions": "instructions", "settings": "settings",
            "resource_digests": {}, "definition_digests": {}, "head": HEAD, "target": BASE,
            "local_head": HEAD, "dirty": False,
            "identities": {"current": True, "pin": HEAD, "metadata_head": HEAD, "git_head": None, "mismatched": []}}
        snapshot = patch.object(app, "_snapshot", side_effect=lambda *args: copy.deepcopy(self.snapshot))
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.claims = 0

    def open(self):
        """A new connection to the same run ledger, as a replacement run would open it."""
        ledger = Ledger(self.root, "fixture", self.pr["url"])
        self.addCleanup(ledger.close)
        return ledger

    @staticmethod
    def task(key, kind):
        return {"id": key, "kind": kind, "definition_digest": "definition-" + key,
                "resources": [], "checklist": CHECKLIST, "required": True, "writes": False}

    def report(self, **values):
        evidence = [{"id": "source", "path": "source.txt",
                     "sha256": hashlib.sha256((self.root / "source.txt").read_bytes()).hexdigest()}]
        report = {"complete": True, "findings": [], "evidence": evidence, "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["source"]} for item in CHECKLIST]}
        report.update(values)
        return report

    def view(self):
        return feedback.View(self.pr, copy.deepcopy(self.github.ev))

    def drafts(self, view=None, inline="inline:30"):
        view = view or self.view()
        assessments = [
            assessment(view, inline, request("rename", "rename `value` to `count`", "fixed")),
            # dk-workflows#244: a declined bot suggestion still owes its author an answer.
            assessment(view, "comment:40", request("constant", "named constant", "rejected", category="style")),
            assessment(view, "comment:41", request("date", "confirm the release date", addressee="participant",
                                                   participant="bob", response={
                                                       "disposition": "no_reply", "reason": "addressed_to_participant",
                                                       "justification": "Asked of bob, not the PR author."})),
        ]
        replies = [{"source": inline, "body": "Renamed to `count` in the published fix."},
                   {"source": "comment:40", "body": "Kept the literal: it is the documented protocol version."}]
        return {"assessments": assessments, "replies": replies}

    def respond(self, value=None):
        """The responder's report: retained at acceptance (as app.accept does), then accepted."""
        report = self.report(deliverables={"feedback": value or self.drafts()})
        responses.accept(self.root, self.github, self.plan, report)
        self.claims += 1
        task = self.plan["tasks"][0]
        fingerprint = app._fingerprint(self.snapshot, task, self.ledger)
        claim = f"claim_responder_{self.claims}"
        reference = artifact(self.root, {"claim": claim, "agent": PRODUCER, "round": 0, "report": report})
        self.ledger.db.execute("INSERT INTO claims(id,task,fingerprint,state,agent,assignment,report,coverage) "
                               "VALUES(?,?,?,'complete',?,?,?,?)",
                               (claim, "responder", fingerprint, PRODUCER, "fixture-assignment", reference, fingerprint))
        self.ledger.db.execute("INSERT INTO accepted_reports VALUES(?,?,?,?,?,?)",
                               (reference, "responder", "reply", fingerprint, PRODUCER, canonical(report).decode()))
        return reference

    def listed(self, ledger=None):
        return publishing.dispatch("publication_list", self.root, self.github, ledger or self.ledger, self.plan,
                                   {"kind": "responses"})

    def publish(self, item, ledger=None):
        ledger = ledger or self.ledger
        prepared = publishing.publication_prepare(self.root, self.github, ledger, self.plan, {"proposal": item})
        if not prepared["invoke"]:
            # publication.yml: an already dispatched original is only reconciled.
            return publishing.publication_recover(self.root, self.github, ledger, self.plan, {"id": prepared["id"]})
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["source"]})
        return publishing.publication_execute(self.root, self.github, ledger, self.plan,
                                              {"id": prepared["id"], "agent": AUDIENCE, "report": verdict})

    def stage(self, ledger=None):
        """responses.yml after the responder: replies, settle, resolutions, status."""
        ledger = ledger or self.ledger
        for item in self.listed(ledger)["items"]:
            self.publish(item, ledger)
        responses.settle(self.root, self.github, ledger, self.plan, {})
        for item in self.listed(ledger)["items"]:
            self.publish(item, ledger)
        return responses.status(self.root, self.github, ledger, self.plan)

    def writes(self):
        return [call for call in self.github.writes]


class BeforeReview(Fixture):
    def test_dk_workflows_255_replies_settle_before_a_failing_review_and_survive_resume(self):
        # The fix is still local: replies wait for its publication, with no write.
        self.snapshot["local_head"] = OTHER
        self.respond()
        waiting = self.listed()
        self.assertEqual(waiting["items"], [])
        self.assertEqual(waiting["blockers"], ["publish the accepted fixes before replying to feedback"])
        self.assertEqual(self.github.counts["write"], 0)
        # The publication stage pushed the fix (exact-intent push is covered by #17's fixtures).
        self.snapshot["local_head"] = HEAD

        result = self.stage()
        self.assertEqual(result["status"], "complete", result)
        posted = self.writes()
        self.assertEqual([(method, path.rsplit("/", 2)[-2:]) for method, path, _ in posted],
                         [("POST", ["7", "comments"]), ("POST", ["30", "replies"]), ("POST", ["graphql"])])
        # A PR comment answers comment 40 only by linking its exact URL.
        self.assertTrue(posted[0][2]["body"].startswith(URL + "#issuecomment-40\n\n"))
        self.assertTrue(self.github.ev["threads"][0]["isResolved"])
        self.assertEqual(result["unanswered"][0]["reason"], "addressed_to_participant")

        # The required review then fails: the plan stays incomplete, the responses stay settled.
        covered = app.coverage(self.root, self.github, self.ledger, self.plan, {})
        self.assertFalse(covered["complete"])
        self.assertIn("review", covered["missing"])
        self.assertNotIn("feedback_responses", covered["missing"])
        self.assertEqual(covered["responses"]["status"], "complete")

        # A replacement run resumes on the same logical run: nothing is answered twice,
        # including by the finalizer response stage.
        for _ in range(2):
            resumed = self.stage(self.open())
            self.assertEqual(resumed["status"], "complete")
        self.assertEqual(self.github.counts["write"], 3)

    def test_lost_reply_response_is_reconciled_never_resent(self):
        for fault in ("after", "before"):
            with self.subTest(fault=fault):
                self.setUp()
                self.respond()
                self.github.fault = fault
                with self.assertRaises(OSError):
                    self.stage()
                self.github.fault = None
                self.assertEqual(self.github.counts["write"], 1)
                status = responses.status(self.root, self.github, self.ledger, self.plan)
                self.assertNotEqual(status["status"], "complete")
                self.assertEqual(len(status["uncertain"]), 1)
                self.assertIn("reconcile", status["blockers"][-1])
                if fault == "before":
                    # Absence proves nothing: the original stays pending and is never resent.
                    for _ in range(2):
                        with self.assertRaisesRegex(PublicationError, "remains unresolved"):
                            self.stage()
                    self.assertEqual(self.github.counts["write"], 1)
                    self.assertFalse(app.coverage(self.root, self.github, self.ledger, self.plan, {})["complete"])
                    continue
                # The effect happened: reconciliation settles the original, then the rest proceeds.
                self.assertEqual(self.stage()["status"], "complete")
                self.assertEqual(self.github.counts["write"], 3)
                replies = [c for c in self.github.ev["inline"] if c["user"]["id"] == 11]
                self.assertEqual(len(replies), 1)

    def test_reaction_between_publication_and_settle_still_records_the_original(self):
        # Review cycle 1: the raw source digest changes with reactions; the body does not.
        self.respond()
        for item in self.listed()["items"]:
            self.publish(item)
        self.assertEqual(self.github.counts["write"], 2)
        # Interrupted before responses_settle; someone reacts to both answered sources.
        for item in self.github.ev["comments"] + self.github.ev["inline"]:
            if item["id"] in (30, 40):
                item["reactions"] = {"total_count": 1, "+1": 1}
        for _ in range(2):
            self.assertEqual(self.stage(self.open())["status"], "complete")
        self.assertEqual(self.github.counts["write"], 3, "two replies and one resolution, nothing resent")

    def test_reaction_to_our_own_reply_before_settle_still_records_it(self):
        # Review cycle 2: an applied original already holds its receipt; it is never
        # re-observed for recovery, where a reaction to our reply would differ from it.
        self.respond()
        for item in self.listed()["items"]:
            self.publish(item)
        self.assertEqual(self.github.counts["write"], 2)
        for item in self.github.ev["comments"] + self.github.ev["inline"]:
            if item["user"]["id"] == 11:
                item["reactions"] = {"total_count": 1, "+1": 1}
        resumed = self.open()
        self.assertEqual(self.listed(resumed)["items"], [], "applied originals are not relisted")
        for _ in range(2):
            self.assertEqual(self.stage(self.open())["status"], "complete")
        self.assertEqual(self.github.counts["write"], 3, "two replies and one resolution, nothing resent")

    def test_an_id_prefix_link_does_not_stand_in_for_the_source_link(self):
        # Review cycle 1: #issuecomment-401 contains #issuecomment-40 as a substring only.
        value = self.drafts()
        value["replies"][1]["body"] = "Kept the literal; see " + URL + "#issuecomment-401 for the protocol."
        self.respond(value)
        self.assertEqual(self.stage()["status"], "complete")
        posted = [call for call in self.github.writes if call[1].endswith("/comments")]
        self.assertTrue(posted[0][2]["body"].startswith(URL + "#issuecomment-40\n\n"))
        self.assertEqual(self.stage(self.open())["status"], "complete")
        self.assertEqual(self.github.counts["write"], 3)
        self.assertFalse(feedback.links("see " + URL + "#issuecomment-401", URL + "#issuecomment-40"))
        self.assertTrue(feedback.links("see " + URL + "#issuecomment-40.", URL + "#issuecomment-40"))

    def test_an_applied_reply_that_cannot_be_recorded_is_a_visible_blocker(self):
        self.respond()
        for item in self.listed()["items"]:
            self.publish(item)
        # The applied comment reply lost its link (edited by someone else) before it was recorded.
        reply = next(item for item in self.github.ev["comments"] if item["user"]["id"] == 11)
        reply["body"] = "edited"
        responses.settle(self.root, self.github, self.ledger, self.plan, {})
        status = responses.status(self.root, self.github, self.ledger, self.plan)
        self.assertEqual(status["replies_needed"], ["comment:40"])
        self.assertTrue(any("does not answer comment:40" in blocker for blocker in status["blockers"]), status)
        self.assertEqual(self.github.counts["write"], 2)

    def test_changed_feedback_gets_one_new_reply_and_settled_sources_none(self):
        self.respond()
        self.assertEqual(self.stage()["status"], "complete")
        self.assertEqual(self.github.counts["write"], 3)
        self.github.ev["comments"][0]["body"] += " Also document it."
        self.snapshot["discussion"] = "changed discussion"
        changed = responses.status(self.root, self.github, self.ledger, self.plan)
        self.assertEqual((changed["status"], changed["unassessed"]), ("incomplete", ["comment:40"]))
        # Until the changed source is assessed, the stage proposes nothing at all.
        self.assertEqual(self.listed()["items"], [])
        self.assertFalse(app.coverage(self.root, self.github, self.ledger, self.plan, {})["complete"])
        view = self.view()
        value = self.drafts(view)
        value["assessments"][1] = assessment(view, "comment:40", request("constant", "named constant", "rejected"),
                                             request("document", "Also document it.", "fixed"))
        value["replies"] = [{"source": "comment:40", "body": "Documented the protocol version; kept the literal."}]
        self.respond(value)
        listed = self.listed()["items"]
        self.assertEqual([item["source"]["id"] for item in listed], [40])
        self.assertEqual(self.stage()["status"], "complete")
        self.assertEqual(self.github.counts["write"], 4)

    def test_a_follow_up_inline_comment_is_answered_at_its_thread_root(self):
        # code-scan #2: the reviewer's request is a follow-up in the PR author's thread.
        root = self.github.ev["inline"][0]
        self.github.ev["inline"].append({**copy.deepcopy(root), "id": 31, "in_reply_to_id": 30, "created_at": at(4),
                                         "html_url": URL + "#discussion_r31"})
        root.update(user=copy.deepcopy(MAINTAINER), body="Is `value` the right name here?")
        self.respond(self.drafts(inline="inline:31"))
        result = self.stage()
        self.assertEqual(result["status"], "complete", result)
        self.assertIn(("POST", ["30", "replies"]),
                      [(method, path.rsplit("/", 2)[-2:]) for method, path, _ in self.writes()])
        self.assertTrue(self.github.ev["threads"][0]["isResolved"])
        self.assertEqual(self.stage(self.open())["status"], "complete")
        self.assertEqual(self.github.counts["write"], 3)

    def test_an_unsettled_reply_to_an_earlier_body_never_settles_the_edited_source(self):
        # code-scan #3: comment 40 is edited after its reply was applied but before settle.
        self.respond()
        for item in self.listed()["items"]:
            self.publish(item)
        self.assertEqual(self.github.counts["write"], 2)
        self.github.ev["comments"][0]["body"] += " Also document it."
        self.snapshot["discussion"] = "changed discussion"
        view = self.view()
        value = self.drafts(view)
        value["assessments"][1] = assessment(view, "comment:40", request("constant", "named constant", "rejected"),
                                             request("document", "Also document it.", "fixed"))
        value["replies"][1] = {"source": "comment:40", "body": "Documented the protocol version; kept the literal."}
        self.respond(value)
        settled = responses.settle(self.root, self.github, self.ledger, self.plan, {})
        self.assertIn("comment:40", settled["replies_needed"])
        self.assertEqual([item["source"]["id"] for item in self.listed()["items"] if "source" in item], [40])
        self.assertEqual(self.stage()["status"], "complete")
        self.assertEqual(self.github.counts["write"], 4)

    def test_disabled_resolution_still_answers_and_leaves_the_thread_open(self):
        self.plan["options"]["permissions"]["resolve_threads"] = False
        self.respond()
        self.assertEqual(self.stage()["status"], "complete")
        self.assertEqual(self.github.counts["write"], 2)
        self.assertFalse(self.github.ev["threads"][0]["isResolved"])
        self.plan["options"]["permissions"]["resolve_threads"] = True
        del self.plan["authorizations"]["resolve_thread"]
        self.assertEqual(self.stage()["status"], "complete")
        self.assertEqual(self.github.counts["write"], 2)


class HonestStatus(Fixture):
    def test_denied_reply_authority_blocks_without_claiming_completion(self):
        for denial in ("permission", "authorization"):
            with self.subTest(denial=denial):
                self.setUp()
                if denial == "permission":
                    self.plan["options"]["permissions"]["reply_to_feedback"] = False
                else:
                    del self.plan["authorizations"]["reply"]
                self.respond()
                result = self.stage()
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["replies_needed"], ["comment:40", "inline:30"])
                self.assertEqual(self.github.counts["write"], 0)
                covered = app.coverage(self.root, self.github, self.ledger, self.plan, {})
                self.assertIn("feedback_responses", covered["missing"])

    def test_responder_must_assess_everything_and_draft_every_owed_reply(self):
        view = self.view()
        missing = self.drafts(view)
        missing["assessments"].pop()
        undrafted = self.drafts(view)
        undrafted["replies"].pop()
        declined = self.drafts(view)
        declined["assessments"][1] = assessment(view, "comment:40", request(
            "constant", "named constant", "rejected", response={
                "disposition": "no_reply", "reason": "no_author_reply_needed", "justification": "Bot noise."}))
        for value, message in ((missing, "unassessed: comment:41"), (undrafted, "drafted no reply for: comment:40"),
                               (declined, "concrete author request needs a reply")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(feedback.FeedbackError, message):
                    responses.accept(self.root, self.github, self.plan,
                                     self.report(deliverables={"feedback": value}))
        self.assertEqual(self.github.counts["write"], 0)

    def test_provisional_analysis_withholds_replies(self):
        self.respond()
        self.snapshot["identities"] = {**self.snapshot["identities"], "current": False, "reason": "metadata_lag", "git_head": OTHER}
        withheld = self.listed()
        self.assertEqual(withheld["items"], [])
        self.assertIn("publication is withheld while analysis is provisional", withheld["blockers"])
        self.assertEqual(self.github.counts["write"], 0)

    def test_unselected_or_empty_plans_report_not_selected(self):
        for tasks in ([], [self.task("review", "review")]):
            with self.subTest(tasks=len(tasks)):
                plan = {**self.plan, "tasks": tasks, "stages": []}
                before = self.github.counts["capture"]
                result = responses.status(self.root, self.github, self.ledger, plan)
                self.assertEqual((result["selected"], result["status"]), (False, "not_selected"))
                self.assertTrue(result["blockers"])
                self.assertEqual(self.github.counts["capture"], before)
                self.assertEqual(publishing.publication_list(self.root, self.github, self.ledger, plan,
                                                             {"kind": "responses"})["items"], [])
                if not tasks:
                    covered = app.coverage(self.root, self.github, self.ledger, plan, {})
                    self.assertTrue(covered["complete"])
                    self.assertEqual(covered["responses"]["status"], "not_selected")
        self.assertEqual(self.github.counts["write"], 0)

    def test_native_reply_proposals_to_feedback_belong_to_the_response_stage(self):
        reference = self.respond()
        self.plan["tasks"].append(self.task("description", "description"))
        report = self.report(operations=[{"kind": "reply_inline", "head": HEAD, "target": BASE,
                                          "payload": {"body": "Done.", "comment_id": 30},
                                          "source": {"kind": "inline", "id": 30, "fingerprint": "x"}}])
        fingerprint = app._fingerprint(self.snapshot, self.plan["tasks"][-1], self.ledger)
        other = artifact(self.root, {"claim": "claim_description", "agent": PRODUCER, "round": 0, "report": report})
        self.ledger.db.execute("INSERT INTO claims(id,task,fingerprint,state,agent,assignment,report,coverage) "
                               "VALUES(?,?,?,'complete',?,?,?,?)", ("claim_description", "description", fingerprint,
                                                                     PRODUCER, "fixture-assignment", other, fingerprint))
        self.ledger.db.execute("INSERT INTO accepted_reports VALUES(?,?,?,?,?,?)",
                               (other, "description", "description", fingerprint, PRODUCER, canonical(report).decode()))
        self.assertEqual(publishing.publication_list(self.root, self.github, self.ledger, self.plan, {})["items"], [])
        self.assertEqual({item["sources"][0] for item in self.listed()["items"]}, {reference})


class Selection(unittest.TestCase):
    """Plan preparation: one shared responder occurrence, never a plain task."""

    def harness(self):
        import test_dk_pr_fix_continuation as installed
        harness = installed.Continuation("run")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

    def test_response_stages_share_one_read_only_responder(self):
        harness = self.harness()
        plan = harness.prepare(tasks=["read-feedback", {"stage": "responses"}],
                               finalizers=[{"stage": "responses"}])
        responders = [task for task in plan["tasks"] if task["kind"] == "reply"]
        self.assertEqual(len(responders), 1)
        self.assertFalse(responders[0]["writes"])
        self.assertEqual({stage["task"] for stage in plan["stages"] if stage["kind"] == "responses"},
                         {responders[0]["id"]})
        self.assertIs(responses.responder(plan), responders[0])
        with self.assertRaisesRegex(ValueError, "only inside the response stage"):
            harness.prepare(tasks=["reply-feedback"])

    def test_admission_rejects_a_plain_responder_task(self):
        from cf_workflows import admission
        self.assertIn({"stage": "responses"}, admission.DEFAULTS["tasks"])
        self.assertLess(admission.DEFAULTS["tasks"].index({"stage": "responses"}),
                        admission.DEFAULTS["tasks"].index({"stage": "reviews"}))
        adapters = {name: admission.check_adapter(name, value, name)
                    for name, value in admission.BASELINE_ADAPTERS.items()}
        roles = {role: {"sha256": "0" * 64} for role in admission.ADMITTED_ROLES}
        options = {"tasks": {"value": ["reply-feedback"], "source": "default"},
                   "finalizers": {"value": [], "source": "default"},
                   "reviewers": {"value": [], "source": "default"}}
        with self.assertRaisesRegex(admission.AdmissionError, "only inside the response stage"):
            admission.build_plan(options, adapters, roles, {}, {}, {"default": "default"})


if __name__ == "__main__":
    unittest.main()
