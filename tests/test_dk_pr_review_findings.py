"""Verified inline and body-only finding publication from incoming review (#25, dk-workflows#240).

Disposable Git repositories, a fresh state namespace and a fail-closed fake
GitHub transport: it answers capture, review creation and review-comment reads
only, counts every call and raises on anything else, with no live fallback.
Review creation can lose its response before or after the effect and refuses
like GitHub while the actor owns a pending review. Reviewer, verifier and
audience sessions are only the reports a test hands the installed helpers, and
their dispatches are counted. Nothing here starts an engine, provider or host,
and nothing certifies GitHub publication conformance.
"""
from collections import Counter
import copy
from pathlib import Path
import re
import tempfile
import unittest

import yaml

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import app, findings, publishing
from prfix.capture import HTTPFailure
from prfix.core import Ledger, read_artifact
from prfix.prepare import CHECKLIST, _definition, prepare
from prfix.publication import PENDING_REVIEW_ERROR, Drift, PendingReview, PublicationError
from test_dk_pr_review import AGENTS, PR_URL, Transport, adapters, fix_preflight, git

GRANT = {"request_id": "post-1", "instruction": "Review this PR and post the verified findings.", "source": "chat:1"}
MERGE = {"request_id": "merge-1", "instruction": "Merge it once the review is clean.", "source": "chat:2"}
AUDIENCES = ["agent_" + c * 32 for c in "abcdef"]
CONFLICT = {"message": "Unprocessable Entity", "errors": [PENDING_REVIEW_ERROR], "status": "422"}
SUBJECT = "Subject of the second commit violates the repository's imperative-mood rule."
DESCRIPTION = "The PR description still names the removed helper `old_value()`."
INLINE = "The new value is not covered by a test."


class GitHub(Transport):
    """Capture plus review creation and review-comment reads; everything else fails."""

    def __init__(self, head, target, **kwargs):
        super().__init__(head, target, **kwargs)
        self.evidence["metadata"]["id"] = 700
        self.created = {}
        self.fault = None
        self.posted = []

    def pull_ref(self, pr):
        # The PR's Git ref is the checkout; only lagging metadata reads it.
        self.calls["pull_ref"] += 1
        return {"ref": "refs/pull/7/head", "object": {"type": "commit", "sha": self.git_head}}

    def request(self, method, path, payload=None):
        self.calls["request"] += 1
        prefix = "repos/owner/repo/pulls/7/reviews"
        if method == "POST" and path == prefix:
            self.calls["write"] += 1
            self.posted.append(copy.deepcopy(payload))
            viewer = self.evidence["viewer"]
            if any(r["state"] == "PENDING" and r["user"]["id"] == viewer["id"] for r in self.evidence["reviews"]):
                raise HTTPFailure(422, copy.deepcopy(CONFLICT))
            if self.fault == "before":
                raise OSError("fixture response lost before effect")
            review = {"id": 500 + len(self.evidence["reviews"]), "user": dict(viewer), "body": payload["body"],
                      "commit_id": payload["commit_id"],
                      "state": {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[payload["event"]]}
            comments = [{"id": 900 + len(self.evidence["inline"]) + n, "user": dict(viewer),
                         "pull_request_review_id": review["id"], **comment}
                        for n, comment in enumerate(payload["comments"])]
            self.evidence["reviews"].append(review)
            self.evidence["inline"].extend(copy.deepcopy(comments))
            self.created[review["id"]] = comments
            if self.fault == "after":
                raise OSError("fixture response lost after effect")
            return copy.deepcopy(review)
        match = re.fullmatch(prefix + r"/(\d+)/comments\?per_page=100", path)
        if method == "GET" and match:
            return copy.deepcopy(self.created.get(int(match[1]), []))
        raise AssertionError(f"unconfigured fixture route: {method} {path}")


class Fixture(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw review findings ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "target"
        self.root.mkdir()
        git(self.root, "init", "--initial-branch=main")
        for key, value in (("user.email", "fixture@example.invalid"), ("user.name", "Fixture"),
                           ("commit.gpgsign", "false")):
            git(self.root, "config", key, value)
        (self.root / "code.py").write_text("value = 1\nother = 1\nlast = 1\n")
        (self.root / "notes file.md").write_text("first\nsecond\n")
        (self.root / "AGENTS.md").write_text("Use imperative commit subjects.\n")
        (self.root / "roles").mkdir()
        for role in ("leaf", "reviewer", "verifier", "fixer", "audience"):
            (self.root / "roles" / (role + ".yaml")).write_text(
                f'name: "{role}"\ndescription: "Fixture"\ninstructions: "Follow the bounded assignment."\n')
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "Initial")
        self.target = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "-c", "change")
        (self.root / "code.py").write_text("value = 2\nother = 1\nlast = 1\nextra = 3\n")
        git(self.root, "commit", "-am", "Change value")
        (self.root / "notes file.md").write_text("first\n")
        git(self.root, "commit", "-am", "changed stuff")
        self.commit = git(self.root, "rev-parse", "HEAD")
        self.github = GitHub(self.commit, self.target)
        self.github.git_head = self.commit
        self.definitions = {role: _definition(self.root, "roles/" + role + ".yaml")
                            for role in ("leaf", "reviewer", "verifier", "fixer", "audience")}
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])
        self.native = Counter()

    # -- helpers ---------------------------------------------------------------

    def prepare(self, reviewers=None, authorizations=None, permissions=None, settings=None, mode="review",
                run="review_fixture", tasks=None, accounts=None):
        controls = {"tasks": tasks or [{"stage": "reviews"}], "finalizers": [], "accounts": accounts or [],
                    "reviewers": reviewers if reviewers is not None else ["review"], "adapters": adapters(),
                    "settings": settings or {}, "resources": [], "turns": 8, "mode": mode,
                    "permissions": {"post_findings": True} if permissions is None else permissions}
        request = {"pr": PR_URL, "instructions": "Review this PR and post the verified findings.", "logical_run": run,
                   "authorizations": {"post_findings": GRANT} if authorizations is None else authorizations}
        result = prepare(self.root, request, controls, "exec-1", self.github, self.definitions)
        self.assertEqual(result["findings_authorized"], mode == "review" and "post_findings" in request["authorizations"]
                         and controls["permissions"].get("post_findings") is True)
        return read_artifact(self.root, result["plan"])

    def ledger(self, plan):
        ledger = Ledger(self.root, plan["logical_run"], plan["pr"]["url"])
        self.ledgers.append(ledger)
        return ledger

    def task(self, plan, skill):
        return next(task for task in plan["tasks"] if task["skill"] == skill)

    def report(self, **values):
        evidence = [{"id": "code", "path": "code.py", "sha256": app.source_file(self.root, "code.py")},
                    {"id": "rules", "path": "AGENTS.md", "sha256": app.source_file(self.root, "AGENTS.md")}]
        result = {"complete": True, "findings": [], "evidence": evidence, "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["code"]} for item in CHECKLIST]}
        result.update(values)
        return result

    @staticmethod
    def finding(key, text, kind, sources=("code",), **fields):
        wire = {"kind": kind, "path": None, "line": None, "side": None, "commit": None, **fields}
        return {"id": key, "text": text, "sources": list(sources), "location": wire}

    def execute(self, plan, ledger, skill, agent, report=None):
        task = self.task(plan, skill)
        claimed = app.prepare_claim(self.root, self.github, ledger, plan, {"task": task["id"]})
        if claimed["invoke"]:
            self.native[task["kind"]] += 1
            app.accept(self.root, self.github, ledger, plan,
                       {"claim": claimed["claim"], "agent": agent, "report": report or self.report(), "round": 0})
        return claimed

    def verify(self, plan, ledger, agent, statuses=None):
        statuses = statuses or {}
        dispositions = [{"finding_id": item["id"], "status": statuses.get(item["original_id"], "confirmed"),
                         "sources": ["code"], "reason": "Checked against the source."}
                        for item in app.candidates(self.root, ledger, plan)]
        return self.execute(plan, ledger, "finding_verification", agent, self.report(dispositions=dispositions))

    def listed(self, plan, ledger):
        return publishing.dispatch("publication_list", self.root, self.github, ledger, plan, {"kind": "findings"})

    def barrier(self, plan, ledger):
        """publication-barrier.yml with selector findings, then publication.yml per item."""
        listing = self.listed(plan, ledger)
        receipts = []
        for item in listing["items"]:
            prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": item})
            if not prepared["invoke"]:
                receipts.append(publishing.publication_recover(self.root, self.github, ledger, plan,
                                                               {"id": prepared["id"]}))
                continue
            self.native["audience"] += 1
            verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                            "audience": "PR participants", "evidence": ["code"]})
            done = publishing.publication_execute(self.root, self.github, ledger, plan, {
                "id": prepared["id"], "agent": AUDIENCES[self.native["audience"] - 1], "report": verdict})
            receipts.append(publishing.publication_record(self.root, self.github, ledger, plan, done))
        return listing, receipts

    def reviewed(self, plan, ledger, reported, statuses=None):
        self.execute(plan, ledger, "review", AGENTS[0], self.report(findings=reported))
        self.verify(plan, ledger, AGENTS[1], statuses)

    def writes(self):
        return self.github.calls["write"]

    def payload_text(self):
        payload = self.github.posted[-1]
        return payload["body"] + "\n".join(comment["body"] for comment in payload["comments"])

    def assert_private(self, plan, ledger, text):
        """No operational diagnostic, coverage claim or local detail in review text."""
        for secret in (str(self.root), self.github.evidence["viewer"]["login"], *AGENTS, *AUDIENCES,
                       *[task["id"] for task in plan["tasks"]], plan["logical_run"], "blocker", "coverage",
                       "clean", "incomplete", "partial", "withheld", "verifier", "session", ".agent-workflow"):
            self.assertNotIn(secret, text)


class BodyAndInline(Fixture):
    def test_dk_workflows_240_body_only_findings_publish_with_summaries_disabled(self):
        plan = self.prepare()
        self.assertNotIn("include_summary", plan["settings"])
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", ("rules",), commit=self.commit),
                                     self.finding("description", DESCRIPTION, "pr_description")])
        listing, receipts = self.barrier(plan, ledger)
        self.assertEqual((len(listing["items"]), listing["withheld"]), (1, []))
        self.assertEqual(self.writes(), 1)
        payload = self.github.posted[0]
        self.assertEqual(payload["comments"], [], "no metadata finding borrows a changed-code anchor")
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertEqual(payload["commit_id"], self.commit)
        web = "https://github.com/owner/repo"
        self.assertEqual(payload["body"], "\n\n".join([
            f"- **[Commit `{self.commit[:12]}` message]({web}/commit/{self.commit})**: {SUBJECT}\n"
            f"  Evidence: [`AGENTS.md`]({web}/blob/{self.commit}/AGENTS.md)",
            f"- **[PR description]({PR_URL})**: {DESCRIPTION}\n"
            f"  Evidence: [`code.py`]({web}/blob/{self.commit}/code.py)"]))
        self.assertEqual(receipts[0]["receipt"]["result"]["review"]["state"], "CHANGES_REQUESTED")
        self.assert_private(plan, ledger, payload["body"])
        self.assertEqual(self.native, Counter({"review": 1, "verification": 1, "audience": 1}))

    def test_mixed_findings_keep_honest_anchors_and_never_duplicate(self):
        plan = self.prepare(settings={"include_summary": True})
        ledger = self.ledger(plan)
        reported = [
            self.finding("inline", INLINE, "diff", path="code.py", line=1, side="RIGHT"),
            self.finding("added", "The appended line needs a comment.", "file", path="code.py", line=4),
            self.finding("removed", "Removing the second note loses context.", "diff",
                         path="notes file.md", line=2, side="LEFT"),
            # Not changed by the PR: cited in the body at its real line, never moved to a changed one.
            self.finding("unchanged", "`other` is never read.", "diff", path="code.py", line=2, side="RIGHT"),
            self.finding("subject", SUBJECT, "commit_message", ("rules",), commit=self.commit),
            self.finding("title", "The title does not say what changes.", "pr_title"),
            # Locations that do not exist in the reviewed PR are withheld, never re-anchored.
            self.finding("missing", "Line 99 is wrong.", "file", path="code.py", line=99),
            self.finding("foreign", "The base commit message is wrong.", "commit_message", commit=self.target),
            self.finding("deleted", "Gone file.", "diff", path="gone.py", line=1, side="RIGHT"),
            {"id": "unlocated", "text": "Something is off somewhere.", "sources": ["code"], "location": None},
            self.finding("local", f"See {self.root}/code.py for details.", "diff", path="code.py", line=1, side="RIGHT"),
        ]
        self.reviewed(plan, ledger, reported, {"title": "rejected"})
        listing, _ = self.barrier(plan, ledger)
        payload = self.github.posted[0]
        self.assertEqual(sorted((c["path"], c["line"], c["side"]) for c in payload["comments"]),
                         [("code.py", 1, "RIGHT"), ("code.py", 4, "RIGHT"), ("notes file.md", 2, "LEFT")])
        self.assertTrue(payload["comments"][0]["body"].startswith(INLINE + "\n\nEvidence: [`code.py`]"))
        body = payload["body"]
        self.assertTrue(body.startswith("5 independently verified findings: 3 inline, 2 below.\n\n"), body)
        self.assertIn(f"`code.py` line 2](https://github.com/owner/repo/blob/{self.commit}/code.py#L2)**: `other` is never read.", body)
        text = self.payload_text()
        for item in reported[:6]:
            expected = 0 if item["id"] == "title" else 1  # Rejected by the verifier.
            self.assertEqual(text.count(item["text"]), expected, item["id"])
        for item in reported[6:]:
            self.assertNotIn(item["text"], text)
        reasons = {read_id(entry["finding"], ledger, plan): entry["reasons"] for entry in listing["withheld"]}
        self.assertEqual(set(reasons), {"missing", "foreign", "deleted", "unlocated", "local"})
        self.assertEqual(reasons["local"], ["private_details"])
        self.assert_private(plan, ledger, text)

    def test_changed_lines_follow_the_pr_diff_exactly(self):
        changed = findings.changed_lines(self.root, self.target, self.commit)
        self.assertEqual(changed, {("code.py", "LEFT", 1), ("code.py", "RIGHT", 1), ("code.py", "RIGHT", 4),
                                   ("notes file.md", "LEFT", 2)})
        for raw in ({"kind": "diff", "path": "/abs", "line": 1, "side": "RIGHT", "commit": None},
                    {"kind": "diff", "path": "a/../b", "line": 1, "side": "RIGHT"},
                    {"kind": "diff", "path": "a", "line": 0, "side": "RIGHT"},
                    {"kind": "diff", "path": "a", "line": 1, "side": "UP"},
                    {"kind": "file", "path": "a", "line": 1, "side": "RIGHT"},
                    {"kind": "commit_message", "commit": "abc"},
                    {"kind": "anywhere"}, None, "code.py:1"):
            with self.subTest(raw=raw):
                with self.assertRaises(findings.LocationError):
                    findings.location(raw)
        self.assertEqual(findings.location({"kind": "pr_title", "path": None, "line": None, "side": None, "commit": None}),
                         {"kind": "pr_title"})


def read_id(candidate_id, ledger, plan):
    root = Path(ledger.root)
    return next(item["original_id"] for item in app.candidates(root, ledger, plan) if item["id"] == candidate_id)


class EventsAndAuthority(Fixture):
    def test_partial_review_only_comments_and_establishes_nothing(self):
        # The required simplification reviewer never ran: coverage is partial.
        plan = self.prepare(reviewers=[["review", "simplify"]], authorizations={"post_findings": GRANT, "merge": MERGE},
                            permissions={"post_findings": True, "merge": True})
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("inline", INLINE, "diff", path="code.py", line=1, side="RIGHT"),
                                     self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
        review = app.coverage(self.root, self.github, ledger, plan, {})["review"]
        self.assertEqual((review["outcome"], review["partial"]), ("incomplete", True))
        self.barrier(plan, ledger)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.github.posted[0]["event"], "COMMENT")
        self.assertEqual(len(self.github.posted[0]["comments"]), 1)
        self.assertIn(SUBJECT, self.github.posted[0]["body"])
        self.assert_private(plan, ledger, self.payload_text())
        # Publishing findings is never a clean review, readiness, approval or merge.
        review = app.coverage(self.root, self.github, ledger, plan, {})["review"]
        self.assertEqual((review["clean_review"], review["approve"], review["ready"]), (False, False, False))
        proposal = publishing.merge_proposal(self.root, self.github, ledger, plan, {})
        self.assertEqual(proposal["invoke"], False)
        self.assertEqual(self.writes(), 1)
        # Approval is its own action (#31): a post_findings or merge grant never authorizes it.
        self.assertEqual(publishing.approval_proposal(self.root, self.github, ledger, plan, {}),
                         {"invoke": False, "proposal": {}})
        with self.assertRaisesRegex(PublicationError, "explicit user authorization required for approve_pr"):
            publishing._publication(self.root, self.github, ledger, plan).prepare(
                {"kind": "approve_pr", "head": self.commit, "target": self.target, "payload": {}, "sources": ["x"]})
        self.assertEqual(self.writes(), 1)

    def test_own_pr_and_configured_event_comment_instead_of_requesting_changes(self):
        for settings, viewer in (({"request_changes": False}, 1), ({}, 2)):
            with self.subTest(settings=settings, viewer=viewer):
                self.github.evidence["viewer"]["id"] = viewer
                self.github.evidence["reviews"], self.github.evidence["inline"] = [], []
                plan = self.prepare(settings=settings, run=f"review_{viewer}")
                ledger = self.ledger(plan)
                self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
                self.barrier(plan, ledger)
                self.assertEqual(self.github.posted[-1]["event"], "COMMENT")

    def test_configured_personal_account_pr_is_own_and_only_comments(self):
        # Actor 1 reviews a PR by configured personal account 2 on the same host: the user's own PR.
        self.github.evidence["viewer"]["id"] = 1
        plan = self.prepare(accounts=[{"host": "github.com", "id": 2}], run="review_accounts")
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
        review = app.coverage(self.root, self.github, ledger, plan, {})["review"]
        self.assertEqual((review["outcome"], review["partial"]), ("findings", False))
        self.barrier(plan, ledger)
        self.assertEqual(self.github.posted[-1]["event"], "COMMENT")
        # The same account ID on another host is not the user's.
        self.github.evidence["reviews"], self.github.evidence["inline"] = [], []
        plan = self.prepare(accounts=[{"host": "ghe.example.com", "id": 2}], run="review_other_host")
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
        self.barrier(plan, ledger)
        self.assertEqual(self.github.posted[-1]["event"], "REQUEST_CHANGES")
        self.assertEqual(self.writes(), 2)

    def test_state_references_and_execution_identities_are_withheld(self):
        plan = self.prepare()
        ledger = self.ledger(plan)
        hexes = "0123456789abcdef" * 4
        task = self.task(plan, "review")["id"]
        quoted = {
            "mount": "The assignment at /cf-workflows-state/requests/" + hexes + ".json says so.",
            "namespace": "Read " + str(Path(self.state_dir).resolve()) + "/runs first.",
            "xdg": "See $XDG_STATE_HOME/cf-workflows/repositories/abc/ for the ledger.",
            "artifact": "The retry assignment_reference artifacts/" + hexes + ".json is stale.",
            "preparation": "preparations/" + hexes + ".json differs.",
            "task": "Occurrence " + task + " was skipped.",
            "claim": "claim_" + hexes + " never finished.",
            "finding": "Duplicate of finding_" + hexes + ".",
            "agent": "Session agent_" + hexes[:32] + " timed out.",
            "publication": "Intent findings_" + hexes + " is pending.",
            "run": "Default run review_" + hexes[:32] + " is partial.",
        }
        reported = [self.finding(key, value, "pr_description") for key, value in quoted.items()]
        reported.append(self.finding("subject", SUBJECT, "commit_message", commit=self.commit))
        self.reviewed(plan, ledger, reported)
        listing, _ = self.barrier(plan, ledger)
        reasons = {read_id(entry["finding"], ledger, plan): entry["reasons"] for entry in listing["withheld"]}
        self.assertEqual(reasons, {key: ["private_details"] for key in quoted})
        # Confirmed findings left out are a visible (private) gap, never a silent omission.
        self.assertEqual(len([b for b in listing["blockers"] if "was not published: private_details" in b]),
                         len(quoted))
        self.assertEqual(self.writes(), 1)
        self.assertNotIn("assignment", self.payload_text())
        self.assertIn(SUBJECT, self.github.posted[0]["body"])

    def test_ordinary_code_identifiers_and_a_user_chosen_run_name_stay_publishable(self):
        # A user-chosen logical run name is ordinary text; only generated identity formats are private.
        plan = self.prepare(run="fix")
        ledger = self.ledger(plan)
        texts = ["This does not fix the overflow in `value`.", "`group_2` is assigned but never read.",
                 "The `merge_20240115` migration is not reversible.", "Rename `push_add` and `merge_base`.",
                 "`task_12345678` collides with `pr_deadbeef` and `review_cafe`.",
                 "`claim_" + "a" * 63 + "` is one character short of a generated claim ID."]
        self.reviewed(plan, ledger, [self.finding(f"f{n}", value, "pr_description") for n, value in enumerate(texts)])
        listing, _ = self.barrier(plan, ledger)
        self.assertEqual((listing["withheld"], listing["blockers"]), ([], []))
        body = self.github.posted[0]["body"]
        for value in texts:
            self.assertEqual(body.count(value), 1, value)

    def test_publication_needs_its_own_permission_and_authorization(self):
        for authorizations, permissions in (({}, {"post_findings": True}), ({"post_findings": GRANT}, {})):
            with self.subTest(authorizations=authorizations, permissions=permissions):
                plan = self.prepare(authorizations=authorizations, permissions=permissions,
                                    run="review_" + str(len(authorizations)))
                ledger = self.ledger(plan)
                self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
                listing = self.listed(plan, ledger)
                self.assertEqual(listing["items"], [])
                self.assertIn("post_findings", listing["blockers"][0])
        self.assertEqual(self.writes(), 0)
        # Fix mode never lists findings, and review mode lists nothing else.
        plan = self.prepare(mode="review")
        ledger = self.ledger(plan)
        for selector in ("ordinary", "responses", "all"):
            with self.assertRaisesRegex(PublicationError, "belongs to incoming review"):
                publishing.dispatch("publication_list", self.root, self.github, ledger, plan, {"kind": selector})
        self.assertEqual(self.writes(), 0)

    def test_native_review_operations_never_become_writes(self):
        plan = self.prepare()
        ledger = self.ledger(plan)
        fabricated = {"kind": "submit_review", "head": self.commit, "target": self.target,
                      "payload": {"event": "COMMENT", "body": "", "comments": [
                          {"path": "code.py", "line": 1, "side": "RIGHT", "body": SUBJECT}]}}
        self.execute(plan, ledger, "review", AGENTS[0], self.report(
            findings=[self.finding("subject", SUBJECT, "commit_message", commit=self.commit)],
            operations=[fabricated]))
        self.verify(plan, ledger, AGENTS[1])
        with self.assertRaisesRegex(PublicationError, "outside accepted source reports|unknown publication intent"):
            publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": {**fabricated, "id": "x"}})
        self.barrier(plan, ledger)
        self.assertEqual(self.github.posted[0]["comments"], [])
        self.assertIn(SUBJECT, self.github.posted[0]["body"])

    def test_captured_feedback_is_never_republished(self):
        plan = self.prepare(tasks=["read-feedback", {"stage": "reviews"}])
        ledger = self.ledger(plan)
        self.execute(plan, ledger, "read-feedback", AGENTS[2], self.report(findings=[
            self.finding("thread", "Another reviewer asked for a test.", "diff", path="code.py", line=1, side="RIGHT")]))
        self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
        listing, _ = self.barrier(plan, ledger)
        self.assertEqual(listing["withheld"], [])
        self.assertEqual(self.github.posted[0]["comments"], [])
        self.assertNotIn("Another reviewer", self.payload_text())
        self.assertIn(SUBJECT, self.github.posted[0]["body"])

    def test_unverified_and_rejected_findings_are_never_published(self):
        plan = self.prepare()
        ledger = self.ledger(plan)
        reported = [self.finding("subject", SUBJECT, "commit_message", commit=self.commit),
                    self.finding("description", DESCRIPTION, "pr_description")]
        self.execute(plan, ledger, "review", AGENTS[0], self.report(findings=reported))
        listing = self.listed(plan, ledger)
        self.assertEqual(listing["items"], [])
        self.assertEqual(sorted(entry["reasons"] for entry in listing["withheld"]), [["unverified"]] * 2)
        self.verify(plan, ledger, AGENTS[1], {"subject": "rejected", "description": "rejected"})
        self.assertEqual(self.listed(plan, ledger)["items"], [])
        self.assertEqual(self.listed(plan, ledger)["withheld"], [])
        self.assertEqual(self.writes(), 0)


class RerunsAndRecovery(Fixture):
    def published(self, settings=None):
        plan = self.prepare(settings=settings)
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("inline", INLINE, "diff", path="code.py", line=1, side="RIGHT"),
                                     self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
        return plan, ledger

    def test_unchanged_reruns_publish_once(self):
        plan, ledger = self.published()
        self.barrier(plan, ledger)
        self.assertEqual(self.writes(), 1)
        # A fresh session: new plan artifact, reopened ledger, changed publication-only settings.
        ledger.close()
        rerun = self.prepare(settings={"include_summary": True})
        reopened = self.ledger(rerun)
        for skill in ("review", "finding_verification"):
            self.assertFalse(self.execute(rerun, reopened, skill, AGENTS[5])["invoke"])
        listing, _ = self.barrier(rerun, reopened)
        self.assertEqual((listing["items"], listing["withheld"]), ([], []))
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.native, Counter({"review": 1, "verification": 1, "audience": 1}))
        # The author pushes; the same body finding is found again with a new one. Only the new one posts.
        (self.root / "code.py").write_text("value = 2\nother = 1\nlast = 1\nextra = 4\n")
        git(self.root, "commit", "-am", "Adjust extra")
        head = git(self.root, "rev-parse", "HEAD")
        self.github.evidence["metadata"]["head"]["sha"] = head
        self.execute(rerun, reopened, "review", AGENTS[2], self.report(findings=[
            self.finding("subject", SUBJECT, "commit_message", commit=self.commit),
            self.finding("extra", "`extra` changed without a test.", "diff", path="code.py", line=4, side="RIGHT")]))
        self.verify(rerun, reopened, AGENTS[3])
        self.barrier(rerun, reopened)
        self.assertEqual(self.writes(), 2)
        latest = self.github.posted[-1]
        self.assertEqual(latest["commit_id"], head)
        self.assertNotIn(SUBJECT, latest["body"])
        self.assertEqual([c["line"] for c in latest["comments"]], [4])
        self.assertTrue(latest["body"].startswith("1 independently verified finding: 1 inline, 0 below."))

    def test_lost_response_before_the_effect_stays_pending_and_is_never_resent(self):
        plan, ledger = self.published()
        self.github.fault = "before"
        with self.assertRaises(OSError):
            self.barrier(plan, ledger)
        self.github.fault = None
        self.assertEqual(self.writes(), 1)
        for _ in range(2):
            listing = self.listed(plan, ledger)
            self.assertEqual(len(listing["items"]), 1, "only the retained original, for reconciliation")
            self.assertIn("uncertain", listing["blockers"][0])
            with self.assertRaisesRegex(PublicationError, "remains unresolved"):
                self.barrier(plan, ledger)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.native["audience"], 1)

    def test_lost_response_after_the_effect_settles_by_reconciliation(self):
        plan, ledger = self.published()
        self.github.fault = "after"
        with self.assertRaises(OSError):
            self.barrier(plan, ledger)
        self.github.fault = None
        ledger.close()
        reopened = self.ledger(plan)
        listing, receipts = self.barrier(plan, reopened)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(receipts[0]["receipt"]["result"]["review"]["id"], 500)
        self.assertEqual(self.listed(plan, reopened)["items"], [])
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.native["audience"], 1)

    def test_existing_pending_draft_is_kept_and_refuses_before_any_intent(self):
        draft = {"id": 77, "user": dict(self.github.evidence["viewer"]), "state": "PENDING",
                 "body": "Unrelated draft", "commit_id": self.commit}
        self.github.evidence["reviews"] = [copy.deepcopy(draft)]
        plan, ledger = self.published()
        with self.assertRaises(PendingReview):
            self.barrier(plan, ledger)
        self.assertEqual(ledger.db.execute("SELECT count(*) FROM publication_intents").fetchone()[0], 0)
        self.assertEqual((self.writes(), self.github.evidence["reviews"]), (0, [draft]))
        # Deleting the draft changes the discussion the reviewers saw: they refresh, then it publishes once.
        self.github.evidence["reviews"] = []
        self.assertEqual(self.listed(plan, ledger)["items"], [])
        self.reviewed(plan, ledger, [self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
        self.barrier(plan, ledger)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.native, Counter({"review": 2, "verification": 2, "audience": 1}))

    def test_draft_after_preparation_blocks_and_a_fresh_attempt_publishes_once(self):
        plan, ledger = self.published()
        draft = {"id": 77, "user": dict(self.github.evidence["viewer"]), "state": "PENDING",
                 "body": "Unrelated draft", "commit_id": self.commit}
        item = self.listed(plan, ledger)["items"][0]
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": item})
        self.github.evidence["reviews"] = [copy.deepcopy(draft)]
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["code"]})
        # The draft is new discussion, so the sources are stale first: blocked, with no write.
        with self.assertRaises(Drift):
            publishing.publication_execute(self.root, self.github, ledger, plan,
                                           {"id": prepared["id"], "agent": AUDIENCES[0], "report": verdict})
        self.assertEqual(self.writes(), 0)
        self.assertEqual(ledger.db.execute("SELECT state FROM publication_intents").fetchone()[0], "blocked")
        self.assertEqual(self.github.evidence["reviews"], [draft])
        self.github.evidence["reviews"] = []
        listing, receipts = self.barrier(plan, ledger)
        self.assertEqual(listing["items"][0]["id"], item["id"])
        self.assertEqual(receipts[0]["id"], item["id"] + ".r1")
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.listed(plan, ledger)["items"], [])

    def test_conclusive_pending_review_rejection_frees_its_findings_for_a_fresh_attempt(self):
        plan, ledger = self.published()
        item = self.listed(plan, ledger)["items"][0]
        prepared = publishing.publication_prepare(self.root, self.github, ledger, plan, {"proposal": item})
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["code"]})
        original = self.github.request

        def racing(method, path, payload=None):
            if method == "POST":
                self.github.evidence["reviews"].append({"id": 77, "user": dict(self.github.evidence["viewer"]),
                                                        "state": "PENDING", "body": "Draft", "commit_id": self.commit})
            return original(method, path, payload)
        self.github.request = racing
        with self.assertRaisesRegex(PublicationError, "not_applied"):
            publishing.publication_execute(self.root, self.github, ledger, plan,
                                           {"id": prepared["id"], "agent": AUDIENCES[0], "report": verdict})
        self.github.request = original
        self.github.evidence["reviews"] = []
        listing, receipts = self.barrier(plan, ledger)
        self.assertEqual(receipts[0]["id"], item["id"] + ".r1")
        self.assertEqual(self.writes(), 2, "one refused, one applied; the refused one is never resent")

    def test_provisional_analysis_withholds_finding_publication(self):
        plan, ledger = self.published()
        self.github.evidence["metadata"]["head"]["sha"] = self.target
        listing = self.listed(plan, ledger)
        self.assertEqual(listing["items"], [])
        self.assertTrue(listing["provisional"]["provisional"])
        self.assertEqual(self.writes(), 0)

    def test_partial_findings_publish_after_identity_reconciliation_and_never_while_provisional(self):
        # #23: a partial review analyzed while PR metadata lags the pinned revision.
        lagging = git(self.root, "rev-parse", "HEAD~1")
        self.github.evidence["metadata"]["head"]["sha"] = lagging
        authorized = {"post_findings": GRANT, "merge": MERGE}
        permissions = {"post_findings": True, "merge": True}
        plan = self.prepare(reviewers=[["review", "simplify"]], authorizations=authorized, permissions=permissions)
        identities = plan["snapshot"]["identities"]
        self.assertEqual((identities["current"], identities["pin"], identities["metadata_head"]),
                         (False, self.commit, lagging))
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("inline", INLINE, "diff", path="code.py", line=1, side="RIGHT"),
                                     self.finding("subject", SUBJECT, "commit_message", commit=self.commit)])
        review = app.coverage(self.root, self.github, ledger, plan, {})["review"]
        self.assertEqual((review["outcome"], review["partial"], len(review["findings"]["confirmed"])),
                         ("incomplete", True, 2))
        # Provisional mismatch blocks every finding, verified and independent or not.
        listing = self.listed(plan, ledger)
        self.assertEqual((listing["items"], listing["provisional"]["provisional"]), ([], True))
        self.assertEqual(ledger.db.execute("SELECT count(*) FROM publication_intents").fetchone()[0], 0)

        # The metadata reaches the pinned revision: a new plan reuses the analysis.
        self.github.evidence["metadata"]["head"]["sha"] = self.commit
        ledger.close()
        reconciled = self.prepare(reviewers=[["review", "simplify"]], authorizations=authorized,
                                  permissions=permissions)
        self.assertTrue(reconciled["snapshot"]["identities"]["current"])
        ledger = self.ledger(reconciled)
        for skill in ("review", "finding_verification"):
            self.assertFalse(self.execute(reconciled, ledger, skill, AGENTS[5])["invoke"])
        item = self.listed(reconciled, ledger)["items"][0]
        self.assertEqual(item["payload"]["event"], "COMMENT")

        # Lagging again before dispatch withholds everything once more; nothing is prepared.
        self.github.evidence["metadata"]["head"]["sha"] = lagging
        self.assertEqual(self.listed(reconciled, ledger)["items"], [])
        with self.assertRaisesRegex(PublicationError, "provisional"):
            publishing.publication_prepare(self.root, self.github, ledger, reconciled, {"proposal": item})
        self.assertEqual(self.writes(), 0)

        self.github.evidence["metadata"]["head"]["sha"] = self.commit
        listing, receipts = self.barrier(reconciled, ledger)
        self.assertEqual([entry["id"] for entry in listing["items"]], [item["id"]])
        self.assertEqual(self.writes(), 1)
        posted = self.github.posted[0]
        self.assertEqual((posted["event"], posted["commit_id"], len(posted["comments"])), ("COMMENT", self.commit, 1))
        self.assertIn(SUBJECT, posted["body"])
        self.assertEqual(self.native, Counter({"review": 1, "verification": 1, "audience": 1}),
                         "reconciliation repeats no reviewer or verifier session")
        # Eligible partial findings were posted; the review is still not clean, approved or ready.
        review = app.coverage(self.root, self.github, ledger, reconciled, {})["review"]
        self.assertEqual((review["outcome"], review["clean_review"], review["approve"], review["ready"]),
                         ("incomplete", False, False, False))
        self.assertFalse(publishing.merge_proposal(self.root, self.github, ledger, reconciled, {})["invoke"])
        self.assertEqual(self.listed(reconciled, ledger)["items"], [])
        self.assertEqual(self.writes(), 1)


def workflow(path):
    return yaml.safe_load((fix_preflight.INSTALL / "workflows" / path).read_text())


def helper_call(job):
    """The retained helper operation and payload of a helper.yml job."""
    assert job["uses"] == "workflows/helper.yml", job
    return job["with"]["operation"], job["with"]["payload"]


class InstalledRoute(unittest.TestCase):
    """The installed graph that Fixture.barrier stands in for, read from the YAML only.

    The fixtures call the helpers directly, so this pins that the installed
    workflow routes finding publication through the same helper operations,
    selector, gating and order. No engine parses or runs it here.
    """

    def test_review_entry_reaches_the_findings_job_only_through_its_permission(self):
        review = workflow("review.yml")
        self.assertIs(review["on"]["workflow_call"]["inputs"]["permissions"]["default"]["post_findings"], False)
        (job,) = review["jobs"].values()
        self.assertEqual((job["uses"], job["with"]["mode"], job["with"]["permissions"]),
                         ("workflows/main.yml", "review", "${{ inputs.permissions }}"))
        jobs = workflow("main.yml")["jobs"]
        listing = {name: job["with"]["selector"] for name, job in jobs.items()
                   if job.get("uses") == "workflows/publication-barrier.yml" and "selector" in job.get("with", {})}
        self.assertEqual([name for name, selector in listing.items() if selector == "findings"], ["findings"])
        findings_job = jobs["findings"]
        self.assertEqual(findings_job["with"]["plan"], "${{ needs.prepare.outputs.result.plan }}")
        self.assertEqual(set(findings_job["needs"]), {"prepare", "cycles"})
        # prepare reports findings_authorized only for review mode with the permission and its grant
        # (Fixture.prepare asserts that); the job runs after the cycles, whatever their outcome.
        self.assertEqual(findings_job["if"], "${{ needs.prepare.outputs.result.findings_authorized and "
                                             "needs.cycles.status != 'canceled' }}")
        self.assertEqual(helper_call(jobs["prepare"])[0], "prepare")

    def test_barrier_lists_with_the_selector_and_publishes_each_item_once_in_order(self):
        barrier = workflow("publication-barrier.yml")
        self.assertEqual(barrier["on"]["workflow_call"]["inputs"]["selector"]["default"], "ordinary")
        jobs = barrier["jobs"]
        self.assertEqual(helper_call(jobs["list"]), ("publication_list",
                                                     {"plan": "${{ inputs.plan }}", "kind": "${{ inputs.selector }}"}))
        publish = jobs["publish"]
        self.assertEqual((publish["uses"], publish["needs"]), ("workflows/publication.yml", "list"))
        self.assertEqual(publish["for-each"], {"items": "${{ needs.list.outputs.result.items }}",
                                               "key": "${{ item.id }}", "concurrency": 1})
        self.assertEqual(publish["with"]["proposal"], "${{ item }}")

    def test_each_item_prepares_then_dispatches_or_only_reconciles(self):
        jobs = workflow("publication.yml")["jobs"]
        self.assertEqual(helper_call(jobs["prepare"]),
                         ("publication_prepare", {"plan": "${{ inputs.plan }}", "proposal": "${{ inputs.proposal }}"}))
        # invoke: false (a pending or applied submission) skips the audience session and the write.
        self.assertEqual(jobs["audience"]["if"], "${{ needs.prepare.outputs.result.invoke }}")
        self.assertEqual(jobs["audience"]["with"]["task"], "publication_audience")
        self.assertIn("needs.prepare.outputs.result.invoke", jobs["publish"]["if"])
        self.assertEqual([step["argv"][-1] for step in jobs["publish"]["steps"]],
                         ["publication_execute", "publication_record"])
        for step in jobs["publish"]["steps"]:
            self.assertEqual((step["protocol"], step["effect"]), ("action-v1", "external_mutation"))
        # Recovery always follows: it settles a lost response read-only and never resends.
        self.assertEqual(helper_call(jobs["receipt"])[0], "publication_recover")
        self.assertEqual(set(jobs["receipt"]["needs"]), {"prepare", "publish"})


if __name__ == "__main__":
    unittest.main()
