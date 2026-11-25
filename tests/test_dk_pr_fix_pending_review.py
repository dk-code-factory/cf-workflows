"""Pending-review preflight and conclusive rejection recovery for dk-pr-fix (#18).

Ports the dk-workflows#260 scenarios: conclusive rejection, ambiguous 422,
timeout, missing current draft, unrelated draft and resumed recovery. Every case
uses a fresh state namespace, a disposable ledger and the fail-closed counting
fake GitHub transport of the #17 fixtures, which raises on any unmodeled route.
Its review endpoint answers like GitHub: while the actor owns a pending review,
creating another one fails with the exact HTTP 422. Nothing here starts an
engine, provider or host, and nothing certifies GitHub conformance.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
from prfix import capture
from prfix.capture import CommandFailed, HTTPFailure
from prfix.core import Ledger
from prfix.publication import (PENDING_REVIEW_ERROR, PendingReview, Publication, PublicationError,
                               UncertainEffect, failure_record, pending_review_rejection)
from test_dk_pr_fix_publication import REVIEW, Fixture, Transport
from test_publication import HEAD

CONFLICT = {"message": "Unprocessable Entity", "errors": [PENDING_REVIEW_ERROR], "status": "422"}
DRAFT = {"id": 77, "user": {"id": 11, "login": "maintainer"}, "state": "PENDING",
         "body": "Unrelated draft", "commit_id": HEAD}
# Each is uncertain: never proof that the original submission had no effect.
AMBIGUOUS = {
    "generic 422": HTTPFailure(422, {"message": "Validation Failed", "errors": [
        {"resource": "PullRequestReview", "code": "custom", "message": "Line could not be resolved"}]}),
    "pending error plus another": HTTPFailure(422, {**CONFLICT, "errors": [PENDING_REVIEW_ERROR, "Other"]}),
    "pending error as object": HTTPFailure(422, {**CONFLICT, "errors": [{"message": PENDING_REVIEW_ERROR}]}),
    "different message": HTTPFailure(422, {**CONFLICT, "message": "Validation Failed"}),
    "created object": HTTPFailure(422, {**CONFLICT, "id": 501}),
    "other status": HTTPFailure(400, CONFLICT),
    "status disagrees": HTTPFailure(422, {**CONFLICT, "status": "400"}),
    "malformed body": HTTPFailure(422, None),
    "timeout": TimeoutError("fixture command deadline exceeded"),
    "transport error": OSError("fixture connection reset"),
    "unclassified command failure": CommandFailed("gh", 1, b""),
}


class GitHubReviews(Transport):
    """Adds GitHub's pending-review refusal and injected review failures."""

    def __init__(self):
        super().__init__()
        self.failure = None
        self.before_send = None

    def request(self, method, path, payload=None):
        if method == "POST" and path.endswith("/reviews"):
            if self.before_send is not None:
                self.before_send(self)
            if self.failure is not None or any(
                    r["state"] == "PENDING" and r["user"]["id"] == self.ev["viewer"]["id"] for r in self.ev["reviews"]):
                self.counts["write"] += 1
                self.calls.append((method, path, copy.deepcopy(payload)))
                raise self.failure or HTTPFailure(422, copy.deepcopy(CONFLICT))
        return super().request(method, path, payload)


class PendingReviews(Fixture):
    def world(self, **kwargs):
        publisher, _, ledger = super().world(**kwargs)
        provider = GitHubReviews()
        publisher.provider = provider
        return publisher, provider, ledger

    def submission(self, ident="review"):
        return self.proposal("submit_review", copy.deepcopy(REVIEW), ident)

    def intents(self, ledger):
        return [row[0] for row in ledger.db.execute("SELECT id FROM publication_intents ORDER BY rowid")]

    def drafts(self, provider):
        return [r for r in provider.ev["reviews"] if r["state"] == "PENDING"]

    def assert_pending_without_resend(self, publisher, provider, ledger, ident):
        self.assertEqual((self.state(ledger, ident), self.lock(ledger)), ("pending", ident))
        changes = ledger.db.total_changes
        self.assertEqual(publisher.reconcile(ident)["outcome"], "unknown")
        self.assertEqual(ledger.db.total_changes, changes, "reconciliation is read-only")
        for _ in range(2):
            with self.assertRaises(UncertainEffect):
                publisher.execute(ident, self.audience(publisher.prepare(self.submission(ident))))
        self.assertEqual(self.state(ledger, ident), "pending")
        self.assertEqual(len(provider.writes), 1)

    def test_preflight_refuses_before_any_intent_and_preserves_the_draft(self):
        publisher, provider, ledger = self.world()
        provider.ev["reviews"] = [copy.deepcopy(DRAFT)]
        with self.assertRaisesRegex(PendingReview, r"\[77\].*submit or delete") as refused:
            publisher.prepare(self.submission())
        self.assertEqual(refused.exception.reviews, [77])
        self.assertEqual(self.intents(ledger), [])
        self.assertEqual(provider.writes, [])
        self.assertEqual(provider.ev["reviews"], [DRAFT])
        # Another actor's draft is not the actor's pending review.
        provider.ev["reviews"] = [{**DRAFT, "user": {"id": 21, "login": "other"}}]
        self.assertEqual(publisher.prepare(self.submission())["state"], "prepared")
        self.assertEqual(provider.writes, [])

    def test_draft_created_before_dispatch_blocks_without_writing(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.submission())
        provider.ev["reviews"] = [copy.deepcopy(DRAFT)]
        with self.assertRaises(PendingReview):
            publisher.execute(prepared["id"], self.audience(prepared))
        self.assertEqual(self.state(ledger, "review"), "blocked")
        self.assertIsNone(self.lock(ledger))
        self.assertEqual(provider.writes, [])
        self.assertEqual(provider.ev["reviews"], [DRAFT])
        # The draft stays available; a later attempt is separately prepared.
        with self.assertRaises(PendingReview):
            publisher.prepare(self.submission())
        self.assertEqual(self.intents(ledger), ["review"])
        provider.ev["reviews"] = []
        fresh = publisher.prepare(self.submission())
        self.assertEqual((fresh["id"], fresh["intent"]["supersedes"]), ("review.r1", "review"))
        publisher.execute(fresh["id"], self.audience(fresh))
        self.assertEqual(len(provider.writes), 1)

    def test_conclusive_rejection_settles_not_applied_and_keeps_the_draft(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.submission())
        # The draft appears after the dispatch recheck and before the request.
        provider.before_send = lambda fake: fake.ev["reviews"].append(copy.deepcopy(DRAFT))
        with self.assertRaisesRegex(PublicationError, "not_applied"):
            publisher.execute(prepared["id"], self.audience(prepared))
        provider.before_send = None
        self.assertEqual((self.state(ledger, "review"), self.lock(ledger)), ("not_applied", None))
        events = publisher.events("review")
        self.assertEqual([e["event"] for e in events], ["prepared", "dispatch", "error", "rejected"])
        self.assertEqual(events[2]["value"], {"type": "HTTPFailure", "status": 422, "body": CONFLICT})
        self.assertEqual(events[3]["value"], {"classifier": "pending_review_conflict", "intent": prepared["fingerprint"],
                                              "status": 422, "body": CONFLICT})
        self.assertEqual(provider.ev["reviews"], [DRAFT])
        self.assertEqual(len(provider.writes), 1)
        # No automatic resend, and the journal alone answers reconciliation.
        with self.assertRaisesRegex(PublicationError, "fresh preparation"):
            publisher.execute("review", self.audience(prepared))
        changes = ledger.db.total_changes
        self.assertEqual(publisher.reconcile("review")["outcome"], "no_effect")
        self.assertEqual(ledger.db.total_changes, changes)
        # While the draft remains, no fresh attempt is prepared.
        with self.assertRaises(PendingReview):
            publisher.prepare(self.submission())
        self.assertEqual(self.intents(ledger), ["review"])
        self.assertEqual(len(provider.writes), 1)
        # The actor submits the draft on GitHub; a separately prepared attempt
        # carries the original finding text with its own baseline and audience.
        provider.ev["reviews"][0]["state"] = "COMMENTED"
        fresh = publisher.prepare(self.submission())
        self.assertEqual((fresh["id"], fresh["intent"]["supersedes"]), ("review.r1", "review"))
        self.assertEqual(fresh["intent"]["payload"], REVIEW)
        self.assertEqual(fresh["intent"]["before"]["review_ids"], [77])
        with self.assertRaisesRegex(PublicationError, "audience"):
            publisher.execute(fresh["id"], self.audience(prepared))
        receipt = publisher.execute(fresh["id"], self.audience(fresh))
        self.assertEqual(len(provider.writes), 2)
        # The later success never becomes the rejected original's receipt.
        self.assertEqual(self.state(ledger, "review"), "not_applied")
        self.assertIsNone(publisher._load("review")["receipt"])
        self.assertFalse(publisher.validate("review", receipt))
        with self.assertRaisesRegex(PublicationError, "duplicate receipt|changed before receipt commit"):
            publisher._settle(publisher._load("review"), receipt)
        self.assertEqual(provider.ev["reviews"][0], {**DRAFT, "state": "COMMENTED"})

    def test_ambiguous_failures_stay_pending_even_with_a_current_draft(self):
        for name, failure in AMBIGUOUS.items():
            with self.subTest(failure=name):
                publisher, provider, ledger = self.world()
                prepared = publisher.prepare(self.submission())

                def fail(fake, failure=failure):
                    fake.failure = failure
                    # A draft observed now proves nothing about the original.
                    fake.ev["reviews"].append(copy.deepcopy(DRAFT))
                provider.before_send = fail
                with self.assertRaises(type(failure)):
                    publisher.execute(prepared["id"], self.audience(prepared))
                provider.before_send = provider.failure = None
                self.assertEqual(self.events(publisher, "review"), ["prepared", "dispatch", "error"])
                self.assert_pending_without_resend(publisher, provider, ledger, "review")
                self.assertEqual(self.drafts(provider), [DRAFT])

    def test_missing_current_draft_alone_proves_nothing(self):
        # A timeout, then no draft and no review: still pending, never resent.
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.submission())
        provider.failure = TimeoutError("fixture command deadline exceeded")
        with self.assertRaises(TimeoutError):
            publisher.execute(prepared["id"], self.audience(prepared))
        provider.failure = None
        self.assertEqual(provider.ev["reviews"], [])
        self.assert_pending_without_resend(publisher, provider, ledger, "review")
        # A conclusive original rejection does not depend on the draft remaining.
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.submission())
        provider.failure = HTTPFailure(422, copy.deepcopy(CONFLICT))
        with self.assertRaisesRegex(PublicationError, "not_applied"):
            publisher.execute(prepared["id"], self.audience(prepared))
        self.assertEqual((self.drafts(provider), self.state(ledger, "review")), ([], "not_applied"))
        self.assertEqual(len(provider.writes), 1)

    def test_resumed_recovery_settles_from_retained_original_evidence(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.submission())
        provider.before_send = lambda fake: fake.ev["reviews"].append(copy.deepcopy(DRAFT))

        def crash(*args):
            raise SystemExit("fixture process stopped after retaining the failure")
        with patch.object(Publication, "_not_applied", crash), self.assertRaises(SystemExit):
            publisher.execute(prepared["id"], self.audience(prepared))
        provider.before_send = None
        self.assertEqual((self.state(ledger, "review"), self.lock(ledger)), ("pending", "review"))
        # A fresh helper process: the draft is gone and an identical review now
        # exists (for example, posted by hand). Neither decides the original.
        ledger.close()
        provider.ev["reviews"] = [{"id": 640, "user": {"id": 11, "login": "maintainer"}, "state": "COMMENTED",
                                   "body": REVIEW["body"], "commit_id": HEAD}]
        provider.review_comments[640] = [{"id": 940, "user": {"id": 11}, **REVIEW["comments"][0]}]
        reopened = Ledger(publisher.root, publisher.plan["logical_run"], self.pr["url"])
        self.addCleanup(reopened.close)
        again = Publication(reopened, provider, publisher.root, publisher.plan,
                            source_validator=lambda source: source.startswith("accepted:"))
        changes = reopened.db.total_changes
        observed = again.reconcile("review")
        self.assertEqual(reopened.db.total_changes, changes, "reconciliation is read-only")
        self.assertEqual(observed["outcome"], "no_effect")
        self.assertEqual(observed["proof"]["rejection"]["body"], CONFLICT)
        replay = again.prepare(self.submission())
        self.assertEqual((replay["id"], replay["state"]), ("review", "pending"))
        with self.assertRaisesRegex(PublicationError, "not_applied"):
            again.execute("review", self.audience(prepared))
        self.assertEqual((self.state(reopened, "review"), self.lock(reopened)), ("not_applied", None))
        self.assertIsNone(again._load("review")["receipt"])
        self.assertEqual([e["event"] for e in again.events("review")], ["prepared", "dispatch", "error", "rejected"])
        self.assertEqual(reopened.db.execute("SELECT count(*) FROM publication_effects").fetchone()[0], 0)
        self.assertEqual(len(provider.writes), 1)

    def test_classifier_needs_exact_original_evidence(self):
        intent = {"kind": "submit_review", "payload": REVIEW}
        conclusive = failure_record(HTTPFailure(422, copy.deepcopy(CONFLICT)))
        self.assertEqual(pending_review_rejection(intent, conclusive)["status"], 422)
        # Without GitHub's optional status field it is the same refusal.
        body = {key: value for key, value in CONFLICT.items() if key != "status"}
        self.assertIsNotNone(pending_review_rejection(intent, failure_record(HTTPFailure(422, body))))
        for name, failure in AMBIGUOUS.items():
            with self.subTest(failure=name):
                self.assertIsNone(pending_review_rejection(intent, failure_record(failure)))
        self.assertIsNone(pending_review_rejection({"kind": "reply"}, conclusive))
        self.assertIsNone(pending_review_rejection(intent, {**conclusive, "type": "OSError"}))
        self.assertIsNone(pending_review_rejection(intent, None))

    def test_retained_evidence_requires_a_single_unacknowledged_dispatch(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.submission())
        provider.failure = OSError("fixture connection reset")
        with self.assertRaises(OSError):
            publisher.execute(prepared["id"], self.audience(prepared))
        provider.failure = None
        conclusive = failure_record(HTTPFailure(422, copy.deepcopy(CONFLICT)))
        # A second retained failure, or any acknowledged response, contradicts
        # a single conclusive refusal; the intent stays pending.
        for event, value in (("error", conclusive), ("response", {"id": 501})):
            with ledger.transaction():
                publisher._event("review", event, value)
            self.assertIsNone(publisher._retained_rejection(publisher._load("review")))
            self.assertEqual(publisher.reconcile("review")["outcome"], "unknown")
        self.assertEqual(self.state(ledger, "review"), "pending")
        self.assertEqual(len(provider.writes), 1)


class TransportFailures(unittest.TestCase):
    """The gh transport keeps a write's exact status and JSON body, never stderr."""

    def setUp(self):
        self.provider = capture.GitHub(Path.cwd())

    def failing(self, stdout):
        return patch.object(capture, "run", side_effect=CommandFailed("gh", 1, stdout))

    def test_write_failure_keeps_status_and_body(self):
        body = json.dumps(CONFLICT).encode()
        for separator in (b"\r\n\r\n", b"\n\n"):
            stdout = b"HTTP/2.0 422 Unprocessable Entity\r\nContent-Type: application/json" + separator + body
            with self.subTest(separator=separator), self.failing(stdout) as transport, \
                    self.assertRaises(HTTPFailure) as failed:
                self.provider.request("POST", "repos/o/r/pulls/7/reviews", {"body": "x"})
            self.assertEqual((failed.exception.status, failed.exception.body), (422, CONFLICT))
            self.assertIn("--include", transport.call_args.args[1])
        with self.failing(b"HTTP/2.0 422 Unprocessable Entity\r\n\r\nnot json"), \
                self.assertRaises(HTTPFailure) as failed:
            self.provider.request("POST", "repos/o/r/pulls/7/reviews", {"body": "x"})
        self.assertEqual((failed.exception.status, failed.exception.body), (422, None))

    def test_unparseable_or_read_failures_stay_unclassified(self):
        for stdout in (b"", b"{\"message\": \"Unprocessable Entity\"}", b"HTTP/2.0 200 OK\r\n\r\n{}"):
            with self.subTest(stdout=stdout), self.failing(stdout), self.assertRaises(CommandFailed):
                self.provider.request("POST", "repos/o/r/pulls/7/reviews", {"body": "x"})
        with self.failing(b"HTTP/2.0 404 Not Found\r\n\r\n{}") as transport, self.assertRaises(CommandFailed):
            self.provider.request("GET", "repos/o/r/pulls/7")
        self.assertNotIn("--include", transport.call_args.args[1])

    def test_successful_write_returns_only_the_body(self):
        stdout = b"HTTP/2.0 200 OK\r\nX-GitHub-Request-Id: 1\r\n\r\n{\"id\": 501}"
        with patch.object(capture, "run", return_value=stdout):
            self.assertEqual(self.provider.request("POST", "repos/o/r/pulls/7/reviews", {"body": "x"}), {"id": 501})
        with patch.object(capture, "run", return_value=b"HTTP/2.0 204 No Content\r\nX-GitHub-Request-Id: 1\r\n"):
            self.assertEqual(self.provider.request("DELETE", "repos/o/r/pulls/7/requested_reviewers"), {})
        with patch.object(capture, "run", return_value=b"HTTP/2.0 422 Unprocessable Entity\r\n\r\n{}"), \
                self.assertRaisesRegex(ValueError, "successful exit"):
            self.provider.request("POST", "repos/o/r/pulls/7/reviews", {"body": "x"})

    def test_command_failure_keeps_stdout_but_not_stderr(self):
        import sys
        with self.assertRaises(CommandFailed) as failed:
            capture.run(Path.cwd(), [sys.executable, "-c",
                "import sys;sys.stdout.write('body');sys.stderr.write('SECRET_CREDENTIAL');sys.exit(3)"])
        self.assertEqual((failed.exception.code, failed.exception.stdout), (3, b"body"))
        self.assertNotIn("SECRET_CREDENTIAL", str(failed.exception))


if __name__ == "__main__":
    unittest.main()
