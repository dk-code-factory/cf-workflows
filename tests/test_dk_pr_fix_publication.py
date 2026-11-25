"""Exact-intent publication and uncertain-effect reconciliation for dk-pr-fix (#17).

Every case uses a fresh state namespace, a disposable ledger and a fail-closed
fake GitHub transport that counts operations: any route it does not model raises
instead of reaching a live service. Git kinds push to a disposable bare
repository. Response loss is injected before and after the effect. Nothing here
starts an engine, provider or host, and nothing certifies GitHub conformance.
"""
import ast
from collections import Counter
import copy
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import unittest

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import publication as publication_module
from prfix.core import SCHEMA_VERSION, Ledger, canonical, digest, identity, ledger_path, state_directory
from prfix.publication import (EXTENSIONS, FRESH_ATTEMPTS, PERMISSION_INPUTS, SUPPORTED, Publication,
                               PublicationError, Rejected, UncertainEffect)
from test_publication import AUTH, BASE, GRANTS, HEAD, OTHER, FakeGitHub

HELPERS = Path(publication_module.__file__).resolve().parents[1]
WORKFLOWS = HELPERS.parent
REVIEW = {"event": "COMMENT", "body": "Two independently verified findings.",
          "comments": [{"path": "code.py", "line": 2, "side": "RIGHT", "body": "Verified finding."}]}


def git(root, *args):
    return subprocess.run(git_fixture.command("-c", "commit.gpgsign=false", "-c", "user.name=Fixture",
                                              "-c", "user.email=fixture@example.invalid", *args),
                          cwd=root, capture_output=True, check=True, env=git_fixture.environment(),
                          timeout=30).stdout.decode().strip()


class Transport(FakeGitHub):
    """Fail-closed fake GitHub: counted operations and injected response loss.

    ``fault`` is ``before`` (the write is sent but has no effect) or ``after``
    (the effect happens and the response is lost). Unmodeled routes raise.
    """

    def __init__(self):
        super().__init__()
        self.counts = Counter()
        self.fault = None
        self.rejection = None
        self.review_comments = {}

    def capture(self, pr):
        self.counts["capture"] += 1
        return super().capture(pr)

    def pages(self, path):
        self.counts["pages"] += 1
        return super().pages(path)

    def request(self, method, path, payload=None):
        write = method in ("PATCH", "PUT") or method == "POST" and not (
            path == "graphql" and payload["query"].startswith("query"))
        self.counts["write" if write else "read"] += 1
        if write and (self.fault == "before" or self.rejection is not None):
            self.calls.append((method, path, copy.deepcopy(payload)))
            if self.rejection is not None:
                raise Rejected(self.rejection)
            raise OSError("fixture response lost before effect")
        if method == "POST" and path.endswith("/reviews"):
            self.calls.append((method, path, copy.deepcopy(payload)))
            viewer = copy.deepcopy(self.ev["viewer"])
            review = {"id": 500 + len(self.ev["reviews"]), "user": viewer, "body": payload["body"],
                      "commit_id": payload["commit_id"],
                      "state": {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[payload["event"]]}
            comments = [{"id": 900 + len(self.ev["inline"]) + n, "user": viewer, "pull_request_review_id": review["id"],
                         **comment} for n, comment in enumerate(payload["comments"])]
            self.ev["reviews"].append(review)
            self.ev["inline"].extend(copy.deepcopy(comments))
            self.review_comments[review["id"]] = comments
            result = copy.deepcopy(review)
        elif method == "GET" and "/reviews/" in path and path.split("?")[0].endswith("/comments"):
            return copy.deepcopy(self.review_comments.get(int(path.split("/reviews/")[1].split("/")[0]), []))
        else:
            result = super().request(method, path, payload)
        if write and self.fault == "after":
            raise OSError("fixture response lost after effect")
        return result


class LocalPublication(Publication):
    """Publishes Git kinds to a disposable bare repository; can lose the push response."""

    remote = None
    fault = None

    def _git_remote(self, intent):
        return str(self.remote)

    def _git(self, *args, input=None):
        if args and args[0] == "push":
            self.pushes = getattr(self, "pushes", 0) + 1
            if self.fault == "before":
                raise OSError("fixture push response lost before effect")
            output = super()._git(*args, input=input)
            if self.fault == "after":
                raise OSError("fixture push response lost after effect")
            return output
        return super()._git(*args, input=input)


class Fixture(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw publication ")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.pr = identity("https://github.com/o/r/pull/7")
        self.ready = {"ready": True, "complete": True, "head": HEAD, "target": BASE, "clean_review": True,
                      "verified": True, "ci": {"head": HEAD, "success": True}, "evidence": ["accepted:live"]}
        self.runs = 0

    def world(self, root=None, cls=Publication, permissions=None, authorizations=None):
        """A fresh logical run, ledger and transport."""
        self.runs += 1
        run = f"run{self.runs}"
        root = root or self.base / ("root" + str(self.runs))
        root.mkdir(exist_ok=True)
        plan = {"logical_run": run, "pr": self.pr, "instructions": "Publish exactly what was accepted.",
                "authorizations": dict(GRANTS) if authorizations is None else authorizations,
                "options": {"permissions": {key: True for key in PERMISSION_INPUTS} if permissions is None else permissions,
                            "limits": {"ci_reruns_per_head": 3}}}
        ledger = Ledger(root, run, self.pr["url"])
        self.addCleanup(ledger.close)
        provider = Transport()
        publisher = cls(ledger, provider, root, plan, readiness=lambda intent: copy.deepcopy(self.ready),
                        source_validator=lambda source: source.startswith("accepted:"))
        return publisher, provider, ledger

    @staticmethod
    def proposal(kind, payload, ident="op", **extra):
        return {"id": ident, "kind": kind, "head": HEAD, "target": BASE, "payload": payload,
                "sources": ["accepted:report"], **extra}

    @staticmethod
    def audience(prepared):
        return {"fingerprint": prepared["fingerprint"], "suitable": True, "audience": "PR participants",
                "evidence": ["accepted:audience"]}

    @staticmethod
    def state(ledger, ident):
        return ledger.db.execute("SELECT state FROM publication_intents WHERE id=?", (ident,)).fetchone()[0]

    @staticmethod
    def lock(ledger):
        row = ledger.db.execute("SELECT intent FROM publication_lock").fetchone()
        return row[0] if row else None

    @staticmethod
    def events(publisher, ident):
        return [event["event"] for event in publisher.events(ident)]


class HttpKinds(Fixture):
    """Preparation, dispatch and reconciliation for every HTTP write kind."""

    def scenario(self, kind, publisher, provider):
        """Configure the fake for one kind and return its proposal and setup write count."""
        if kind == "reply":
            return self.proposal("reply", {"body": "Fixed with a regression test."}), 0
        if kind in ("reply_inline", "resolve_thread"):
            source = {"id": 15, "body": "Please fix", "user": {"id": 21}}
            provider.ev["inline"] = [copy.deepcopy(source)]
            provider.ev["threads"] = [{"id": "thread15", "isResolved": False, "comments": {"nodes": [{"databaseId": 15}]}}]
            answer = self.proposal("reply_inline", {"body": "Fixed", "comment_id": 15}, "answer",
                                   source={"kind": "inline", "id": 15, "fingerprint": digest(source)})
            if kind == "reply_inline":
                return answer, 0
            prepared = publisher.prepare(answer)
            publisher.execute(prepared["id"], self.audience(prepared))
            return self.proposal("resolve_thread", {"thread_id": "thread15", "reply_operation": "answer"}), 1
        if kind == "edit_pr":
            return self.proposal("edit_pr", {"title": "After", "body": "New description"}), 0
        if kind == "request_review":
            return self.proposal("request_review", {"reviewers": ["reviewer"], "team_reviewers": ["team"]}), 0
        if kind == "ci_rerun":
            return self.proposal("ci_rerun", {"run_id": 14, "attempt": 1, "reason": "Verified transient failure"}), 0
        if kind == "submit_review":
            return self.proposal("submit_review", copy.deepcopy(REVIEW)), 0
        if kind == "merge":
            return self.proposal("merge", {"merge_method": "merge"}), 0
        raise AssertionError(kind)

    def test_every_http_kind_prepares_dispatches_and_reconciles_without_blind_replay(self):
        kinds = ("reply", "reply_inline", "resolve_thread", "edit_pr", "request_review", "ci_rerun",
                 "submit_review", "merge")
        for kind in kinds:
            for fault in (None, "before", "after"):
                with self.subTest(kind=kind, fault=fault):
                    publisher, provider, ledger = self.world()
                    proposal, setup = self.scenario(kind, publisher, provider)
                    prepared = publisher.prepare(proposal)
                    intent = prepared["intent"]
                    # Retained before dispatch: numeric identities, pins, exact payload,
                    # sources and the authority and permission scope relied on.
                    self.assertEqual((intent["pr_id"], intent["actor_id"]), (700, 11))
                    self.assertEqual((intent["head"], intent["lease"], intent["target"]), (HEAD, HEAD, BASE))
                    self.assertEqual(intent["payload"], proposal["payload"])
                    self.assertEqual(intent["sources"], ["accepted:report"])
                    self.assertEqual((intent["action"], intent["permission"]), SUPPORTED[kind])
                    self.assertEqual(intent["authorization"], AUTH)
                    self.assertEqual(prepared["state"], "prepared")
                    self.assertEqual(publisher.reconcile(prepared["id"])["outcome"], "no_effect")
                    self.assertEqual(len(provider.writes), setup)
                    provider.fault = fault
                    if fault is None:
                        receipt = publisher.execute(prepared["id"], self.audience(prepared))
                    else:
                        with self.assertRaisesRegex(OSError, "lost " + fault):
                            publisher.execute(prepared["id"], self.audience(prepared))
                        provider.fault = None
                        self.assertEqual(self.state(ledger, prepared["id"]), "pending")
                        self.assertEqual(self.lock(ledger), prepared["id"])
                        retained = ledger.db.execute("SELECT audience FROM publication_intents WHERE id=?",
                                                     (prepared["id"],)).fetchone()[0]
                        self.assertEqual(retained, canonical(self.audience(prepared)).decode())
                        changes = ledger.db.total_changes
                        observed = publisher.reconcile(prepared["id"])
                        self.assertEqual(ledger.db.total_changes, changes, "reconciliation is read-only")
                        if fault == "before":
                            # Absence never proves non-delivery and never authorizes a resend.
                            self.assertEqual(observed["outcome"], "unknown")
                            for _ in range(2):
                                with self.assertRaises(UncertainEffect):
                                    publisher.execute(prepared["id"], self.audience(prepared))
                            self.assertEqual(self.state(ledger, prepared["id"]), "pending")
                            self.assertEqual(len(provider.writes), setup + 1)
                            self.assertEqual(self.events(publisher, prepared["id"]), ["prepared", "dispatch", "error"])
                            continue
                        self.assertEqual(observed["outcome"], "completed")
                        receipt = publisher.execute(prepared["id"], self.audience(prepared))
                        self.assertEqual(receipt, observed["result"])
                    self.assertEqual(self.state(ledger, prepared["id"]), "applied")
                    self.assertIsNone(self.lock(ledger))
                    self.assertTrue(publisher.validate(prepared["id"], receipt))
                    self.assertEqual(publisher.execute(prepared["id"], self.audience(prepared)), receipt)
                    self.assertEqual(len(provider.writes), setup + 1)

    def test_restart_keeps_original_intent_and_audience_without_replay(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.proposal("submit_review", copy.deepcopy(REVIEW)))
        provider.fault = "before"
        with self.assertRaises(OSError):
            publisher.execute(prepared["id"], self.audience(prepared))
        provider.fault = None
        ledger.close()
        reopened = Ledger(publisher.root, publisher.plan["logical_run"], self.pr["url"])
        self.addCleanup(reopened.close)
        again = Publication(reopened, provider, publisher.root, publisher.plan,
                            source_validator=lambda source: source.startswith("accepted:"))
        replay = again.prepare(self.proposal("submit_review", copy.deepcopy(REVIEW)))
        self.assertEqual((replay["id"], replay["fingerprint"], replay["state"]),
                         (prepared["id"], prepared["fingerprint"], "pending"))
        with self.assertRaises(UncertainEffect):
            again.execute(prepared["id"], self.audience(prepared))
        self.assertEqual(len(provider.writes), 1)

    def test_conclusive_rejection_settles_not_applied_and_needs_fresh_preparation(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.proposal("reply", {"body": "Answer"}))
        provider.rejection = {"status": 403, "reason": "fixture: resource not accessible by integration"}
        with self.assertRaisesRegex(PublicationError, "not_applied"):
            publisher.execute(prepared["id"], self.audience(prepared))
        provider.rejection = None
        self.assertEqual(self.state(ledger, prepared["id"]), "not_applied")
        self.assertIsNone(self.lock(ledger))
        self.assertEqual(publisher.events(prepared["id"])[-1],
                         {"event": "rejected", "value": {"status": 403, "reason": "fixture: resource not accessible by integration"}})
        with self.assertRaisesRegex(PublicationError, "fresh preparation"):
            publisher.execute(prepared["id"], self.audience(prepared))
        self.assertEqual(len(provider.writes), 1)
        fresh = publisher.prepare(self.proposal("reply", {"body": "Answer"}))
        self.assertEqual((fresh["id"], fresh["intent"]["supersedes"]), ("op.r1", "op"))
        self.assertNotEqual(fresh["fingerprint"], prepared["fingerprint"])
        with self.assertRaisesRegex(PublicationError, "audience"):
            publisher.execute(fresh["id"], self.audience(prepared))
        publisher.execute(fresh["id"], self.audience(fresh))
        self.assertEqual(len(provider.writes), 2)
        self.assertEqual(self.state(ledger, prepared["id"]), "not_applied")

    def test_rejection_without_evidence_stays_pending(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.proposal("reply", {"body": "Answer"}))
        provider.rejection = {}
        with self.assertRaisesRegex(UncertainEffect, "without retained evidence"):
            publisher.execute(prepared["id"], self.audience(prepared))
        provider.rejection = None
        self.assertEqual((self.state(ledger, prepared["id"]), self.lock(ledger)), ("pending", prepared["id"]))
        self.assertEqual(publisher.reconcile(prepared["id"])["outcome"], "unknown")
        self.assertEqual(len(provider.writes), 1)

    def test_unavailable_kinds_and_disabled_defaults_deny_before_any_service_call(self):
        publisher, provider, _ = self.world()
        for kind, owner in EXTENSIONS.items():
            with self.subTest(kind=kind), self.assertRaisesRegex(PublicationError, owner):
                publisher.prepare(self.proposal(kind, {"body": "x"}))
        with self.assertRaisesRegex(PublicationError, "APPROVE|approve_pr"):
            publisher.prepare(self.proposal("submit_review", {**REVIEW, "event": "APPROVE"}))
        for permissions, authorizations, message in (
                ({}, None, "permission forbids"),
                (None, {"iterate": AUTH}, "authorization required"),
                (None, {}, "authorization required")):
            denied, provider, _ = self.world(permissions=permissions, authorizations=authorizations)
            for kind in SUPPORTED:
                with self.subTest(kind=kind, message=message), self.assertRaisesRegex(PublicationError, message):
                    denied.prepare(self.proposal(kind, {}))
            self.assertEqual(sum(provider.counts.values()), 0)
        self.assertEqual(sum(provider.counts.values()), 0)

    def test_only_one_unsettled_write_holds_the_publication_lock(self):
        publisher, provider, ledger = self.world()
        first = publisher.prepare(self.proposal("reply", {"body": "First"}, "first"))
        second = publisher.prepare(self.proposal("edit_pr", {"title": "Second"}, "second"))
        provider.fault = "before"
        with self.assertRaises(OSError):
            publisher.execute(first["id"], self.audience(first))
        provider.fault = None
        # A second writer, through its own ledger connection, cannot dispatch
        # while the uncertain original holds the lock.
        other_ledger = Ledger(publisher.root, publisher.plan["logical_run"], self.pr["url"])
        self.addCleanup(other_ledger.close)
        other = Publication(other_ledger, provider, publisher.root, publisher.plan,
                            source_validator=lambda source: source.startswith("accepted:"))
        with self.assertRaisesRegex(UncertainEffect, "lock"):
            other.execute(second["id"], self.audience(second))
        with self.assertRaises(UncertainEffect):
            other.execute(first["id"], self.audience(first))
        self.assertEqual(len(provider.writes), 1)
        self.assertEqual((self.state(ledger, "second"), self.lock(ledger)), ("prepared", "first"))

    def test_duplicate_receipts_fail_safely(self):
        publisher, provider, ledger = self.world()
        provider.ev["comments"] = [{"id": 40, "body": "Original", "user": {"id": 21}},
                                   {"id": 41, "body": "Other", "user": {"id": 22}}]
        sources = [{"kind": "comment", "id": n, "fingerprint": digest(c)} for n, c in ((40, provider.ev["comments"][0]), (41, provider.ev["comments"][1]))]
        first = publisher.prepare(self.proposal("reply", {"body": "Done."}, "first", source=sources[0]))
        receipt = publisher.execute(first["id"], self.audience(first))
        # A different intent whose exact body the first effect already satisfies
        # adopts nothing: one external effect settles only one intent.
        second = publisher.prepare(self.proposal("reply", {"body": "Done."}, "second", source=sources[1]))
        self.assertEqual(second["intent"]["before"]["satisfied_comment_id"], receipt["result"]["id"])
        with self.assertRaisesRegex(PublicationError, "duplicate receipt"):
            publisher.execute(second["id"], self.audience(second))
        self.assertEqual(self.state(ledger, "second"), "prepared")
        self.assertEqual(len(provider.writes), 1)
        self.assertEqual([tuple(row) for row in ledger.db.execute("SELECT effect,intent FROM publication_effects")],
                         [("comment:" + str(receipt["result"]["id"]), "first")])
        changed = copy.deepcopy(receipt)
        changed["result"]["id"] = 999
        with self.assertRaisesRegex(PublicationError, "duplicate receipt"):
            publisher._settle(publisher._load("first"), changed)

    def test_drift_at_dispatch_blocks_without_writes_and_fresh_attempts_are_bounded(self):
        drifts = {
            "head": lambda w: w[1].ev["metadata"]["head"].update(sha=OTHER),
            "target": lambda w: w[1].ev["metadata"]["base"].update(sha=OTHER),
            "actor": lambda w: w[1].ev["viewer"].update(id=99),
            "numeric PR": lambda w: w[1].ev["metadata"].update(id=701),
            "discussion": lambda w: w[1].ev["comments"].append({"id": 5, "body": "new", "user": {"id": 3}}),
            "source": lambda w: setattr(w[0], "source_validator", lambda source: False),
            "permission": lambda w: w[0].plan["options"]["permissions"].update(edit_pr=False),
            "authority": lambda w: w[0].plan["authorizations"].update(
                edit_pr={**AUTH, "request_id": "user-2"}),
            "withdrawn": lambda w: w[0].plan["authorizations"].pop("edit_pr"),
            "field": lambda w: w[1].ev["metadata"].update(title="Edited by someone else"),
        }
        for name, drift in drifts.items():
            with self.subTest(drift=name):
                world = self.world()
                publisher, provider, ledger = world
                prepared = publisher.prepare(self.proposal("edit_pr", {"title": "After"}))
                drift(world)
                with self.assertRaises(PublicationError):
                    publisher.execute(prepared["id"], self.audience(prepared))
                self.assertEqual(self.state(ledger, prepared["id"]), "blocked")
                self.assertEqual(self.events(publisher, prepared["id"]), ["prepared", "blocked"])
                self.assertEqual(publisher.reconcile(prepared["id"])["outcome"], "no_effect")
                self.assertEqual(provider.writes, [])
        # Payload drift under a retained identity is a conflict, not a new intent.
        publisher, provider, ledger = self.world()
        publisher.prepare(self.proposal("edit_pr", {"title": "After"}))
        with self.assertRaisesRegex(PublicationError, "conflicting"):
            publisher.prepare(self.proposal("edit_pr", {"title": "Different"}))
        # Each fresh attempt is prepared again; their number is bounded.
        for attempt in range(FRESH_ATTEMPTS + 1):
            prepared = publisher.prepare(self.proposal("edit_pr", {"title": "After"}))
            provider.ev["metadata"]["id"] = 800 + attempt
            with self.assertRaisesRegex(PublicationError, "numeric PR"):
                publisher.execute(prepared["id"], self.audience(prepared))
            provider.ev["metadata"]["id"] = 700
        with self.assertRaisesRegex(PublicationError, "attempts exhausted"):
            publisher.prepare(self.proposal("edit_pr", {"title": "After"}))
        self.assertEqual(provider.writes, [])

    def test_pending_review_is_preserved_and_blocks_submission(self):
        publisher, provider, ledger = self.world()
        draft = {"id": 77, "user": {"id": 11}, "state": "PENDING", "body": "Unrelated draft", "commit_id": HEAD}
        provider.ev["reviews"] = [copy.deepcopy(draft)]
        with self.assertRaisesRegex(PublicationError, "pending review"):
            publisher.prepare(self.proposal("submit_review", copy.deepcopy(REVIEW)))
        provider.ev["reviews"] = []
        prepared = publisher.prepare(self.proposal("submit_review", copy.deepcopy(REVIEW)))
        provider.ev["reviews"] = [copy.deepcopy(draft)]
        with self.assertRaises(PublicationError):
            publisher.execute(prepared["id"], self.audience(prepared))
        self.assertEqual(self.state(ledger, prepared["id"]), "blocked")
        self.assertEqual(provider.ev["reviews"], [draft])
        self.assertEqual(provider.writes, [])

    def test_merge_needs_readiness_not_completion(self):
        publisher, provider, ledger = self.world()
        prepared = publisher.prepare(self.proposal("merge", {"merge_method": "merge"}))
        self.ready.update(ready=False, complete=True)
        with self.assertRaisesRegex(PublicationError, "incomplete"):
            publisher.execute(prepared["id"], self.audience(prepared))
        # Waiting for readiness is not drift; the intent stays dispatchable.
        self.assertEqual(self.state(ledger, prepared["id"]), "prepared")
        self.assertEqual(provider.writes, [])


class GitKinds(Fixture):
    """Push, rewrite and split publication against disposable branches."""

    def repository(self, name):
        checkout, remote = self.base / (name + " checkout"), self.base / (name + " remote.git")
        checkout.mkdir()
        git(self.base, "init", "--bare", str(remote))
        git(checkout, "init", "--initial-branch=feature")
        (checkout / "code.py").write_text("value = 1\n")
        git(checkout, "add", "code.py")
        git(checkout, "commit", "-m", "Initial")
        initial = git(checkout, "rev-parse", "HEAD")
        git(checkout, "push", str(remote), "HEAD:refs/heads/feature")
        return checkout, remote, initial

    def commit(self, checkout, value):
        (checkout / "code.py").write_text(value)
        git(checkout, "commit", "-am", "Change " + value.strip())
        return git(checkout, "rev-parse", "HEAD")

    def remote_head(self, remote, branch):
        result = subprocess.run(git_fixture.command("--git-dir", str(remote), "rev-parse", "--verify", "-q",
                                                    "refs/heads/" + branch),
                                capture_output=True, env=git_fixture.environment(), timeout=30)
        return result.stdout.decode().strip() or None

    def git_world(self, name, kind):
        checkout, remote, initial = self.repository(name)
        if kind == "push":
            desired = self.commit(checkout, "value = 2\n")
            lease, target, pr_head, branch = initial, initial, initial, "feature"
            payload = {"new_head": desired}
        elif kind == "rewrite":
            published = self.commit(checkout, "value = 2\n")
            git(checkout, "push", str(remote), "HEAD:refs/heads/feature")
            git(checkout, "reset", "--hard", initial)
            desired = self.commit(checkout, "value = 3\n")
            lease, target, pr_head, branch = published, initial, published, "feature"
            payload = {"new_head": desired}
        else:
            desired = self.commit(checkout, "value = 2\n")
            git(checkout, "push", str(remote), "HEAD:refs/heads/feature")
            lease, target, pr_head, branch = desired, initial, desired, "split/fixture"
            payload = {"branch": branch, "new_head": desired, "paths": ["code.py"], "reason": "Accepted split"}
        publisher, provider, ledger = self.world(root=checkout, cls=LocalPublication)
        publisher.remote = remote
        provider.ev["metadata"]["head"]["sha"] = pr_head
        provider.ev["metadata"]["base"]["sha"] = target
        proposal = {"id": kind, "kind": kind, "head": desired, "lease": lease, "target": target,
                    "payload": payload, "sources": ["accepted:report"]}
        return publisher, provider, ledger, proposal, remote, branch, desired

    def test_every_git_kind_prepares_dispatches_and_reconciles_without_blind_replay(self):
        for kind in ("push", "rewrite", "split_push"):
            for fault in (None, "before", "after"):
                with self.subTest(kind=kind, fault=fault):
                    publisher, provider, ledger, proposal, remote, branch, desired = self.git_world(
                        f"{kind} {fault}", kind)
                    before = self.remote_head(remote, branch)
                    prepared = publisher.prepare(proposal)
                    self.assertEqual((prepared["intent"]["action"], prepared["intent"]["permission"]), SUPPORTED[kind])
                    self.assertEqual(prepared["intent"]["lease"], proposal["lease"])
                    publisher.fault = fault
                    if fault is None:
                        receipt = publisher.execute(prepared["id"], self.audience(prepared))
                    else:
                        with self.assertRaisesRegex(OSError, "lost " + fault):
                            publisher.execute(prepared["id"], self.audience(prepared))
                        publisher.fault = None
                        self.assertEqual(self.state(ledger, kind), "pending")
                        observed = publisher.reconcile(prepared["id"])
                        if fault == "before":
                            # An unchanged or absent branch proves nothing.
                            self.assertEqual(observed["outcome"], "unknown")
                            with self.assertRaises(UncertainEffect):
                                publisher.execute(prepared["id"], self.audience(prepared))
                            self.assertEqual(self.remote_head(remote, branch), before)
                            self.assertEqual(publisher.pushes, 1)
                            continue
                        self.assertEqual(observed["outcome"], "completed")
                        receipt = publisher.execute(prepared["id"], self.audience(prepared))
                    self.assertEqual(self.remote_head(remote, branch), desired)
                    self.assertEqual(receipt["result"], {"head": desired, "branch": branch})
                    self.assertEqual(publisher.execute(prepared["id"], self.audience(prepared)), receipt)
                    self.assertEqual(publisher.pushes, 1)
                    self.assertEqual(self.state(ledger, kind), "applied")

    def test_split_draft_pr_follows_its_reconciled_branch_without_blind_replay(self):
        for fault in (None, "before", "after"):
            with self.subTest(fault=fault):
                publisher, provider, ledger, proposal, remote, branch, desired = self.git_world(
                    f"split pr {fault}", "split_push")
                pushed = publisher.prepare(proposal)
                publisher.execute(pushed["id"], self.audience(pushed))
                provider.split_sha = desired
                draft = publisher.prepare({"id": "draft", "kind": "split_pr", "head": desired,
                    "target": proposal["target"], "sources": ["accepted:report"],
                    "payload": {"title": "Split", "body": "Accepted split scope", "head": "o:" + branch,
                                "base": "main", "draft": True, "push_operation": "split_push"}})
                self.assertEqual(draft["intent"]["created_head"], desired)
                provider.fault = fault
                if fault is None:
                    publisher.execute(draft["id"], self.audience(draft))
                else:
                    with self.assertRaises(OSError):
                        publisher.execute(draft["id"], self.audience(draft))
                    provider.fault = None
                    observed = publisher.reconcile(draft["id"])
                    if fault == "before":
                        self.assertEqual(observed["outcome"], "unknown")
                        with self.assertRaises(UncertainEffect):
                            publisher.execute(draft["id"], self.audience(draft))
                        self.assertEqual((self.state(ledger, "draft"), len(provider.writes)), ("pending", 1))
                        continue
                    self.assertEqual(observed["outcome"], "completed")
                    publisher.execute(draft["id"], self.audience(draft))
                self.assertEqual(self.state(ledger, "draft"), "applied")
                publisher.execute(draft["id"], self.audience(draft))
                self.assertEqual((len(provider.writes), publisher.pushes), (1, 1))

    def test_remote_branch_drift_blocks_push_without_writing(self):
        publisher, provider, ledger, proposal, remote, branch, desired = self.git_world("drift", "push")
        prepared = publisher.prepare(proposal)
        other = self.base / "other"
        git(self.base, "clone", "-q", "-b", "feature", str(remote), str(other))
        self.commit(other, "value = 9\n")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/feature")
        moved = self.remote_head(remote, branch)
        with self.assertRaisesRegex(PublicationError, "remote branch changed"):
            publisher.execute(prepared["id"], self.audience(prepared))
        self.assertEqual((self.state(ledger, "push"), self.remote_head(remote, branch)), ("blocked", moved))
        self.assertEqual(getattr(publisher, "pushes", 0), 0)


class NativeSessionsNeverWrite(unittest.TestCase):
    def test_remote_writes_exist_only_in_the_publication_dispatch(self):
        writes = {"POST", "PATCH", "PUT", "DELETE"}
        for path in sorted(HELPERS.rglob("*.py")):
            tree = ast.parse(path.read_text())
            dispatch = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_dispatch":
                    dispatch |= {id(child) for child in ast.walk(node)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                first = node.args[0].value
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                second = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
                third = node.args[2] if len(node.args) > 2 else None
                query = ""
                if isinstance(third, ast.Dict):
                    for key, value in zip(third.keys, third.values):
                        if isinstance(key, ast.Constant) and key.value == "query":
                            query = value.value if isinstance(value, ast.Constant) else ast.unparse(value)
                remote = (name in {"request", "_request"} and first in writes
                          and not (first == "POST" and second == "graphql" and "mutation" not in query)
                          or name in {"_git", "run"} and first == "push")
                if remote:
                    self.assertIn(id(node), dispatch, f"{path.name}:{node.lineno} writes outside the publication dispatch")

    def test_native_session_steps_receive_no_credentials(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            lines = path.read_text().splitlines()
            for number, line in enumerate(lines):
                if line.strip() != "uses: agent/run":
                    continue
                indent = len(lines[number - 1]) - len(lines[number - 1].lstrip())
                step = []
                for following in lines[number + 1:]:
                    current = len(following) - len(following.lstrip())
                    if following.strip() and current <= indent:
                        break
                    step.append(following)
                body = "\n".join(step)
                self.assertNotRegex(body, r"\benv:|secrets\.|GH_TOKEN|GH_ENTERPRISE_TOKEN", f"{path.name}:{number + 1}")


class LedgerMigration(unittest.TestCase):
    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw publication ledger ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "target"
        self.root.mkdir()
        self.path = ledger_path(state_directory(self.root, create=True), "run")
        self.path.parent.mkdir()

    def test_version_two_journal_migrates_to_contract_states(self):
        url = "https://github.com/o/r/pull/7"
        pr = identity(url)
        pending = {"run": "run", "pr": pr, "kind": "reply", "payload": {"body": "x"}}
        applied = {"run": "run", "pr": pr, "kind": "reply", "payload": {"body": "y"}}
        # A version 2 helper reset this dispatched write to prepared with a
        # no-effect proof from absence; it must not become resendable.
        reset = {"run": "run", "pr": pr, "kind": "reply", "payload": {"body": "z"}}
        database = sqlite3.connect(self.path)
        database.executescript("""
            CREATE TABLE groups(id TEXT PRIMARY KEY, pr TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE deadlines(name TEXT PRIMARY KEY, value REAL NOT NULL, started REAL);
            CREATE TABLE publication_intents(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
             value TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '');
            PRAGMA user_version=2;
        """)
        database.execute("INSERT INTO groups VALUES('run',?,1)", (url,))
        database.execute("INSERT INTO publication_intents VALUES('applied',?,?,'completed',?)",
                         (digest(applied), canonical(applied).decode(), canonical({"result": {"id": 9}}).decode()))
        database.execute("INSERT INTO publication_intents VALUES('pending',?,?,'uncertain',?)",
                         (digest(pending), canonical(pending).decode(),
                          canonical({"dispatch_response": {"id": 10}}).decode()))
        database.execute("INSERT INTO publication_intents VALUES('reset',?,?,'prepared',?)",
                         (digest(reset), canonical(reset).decode(), canonical({"no_effect": {"absent": True}}).decode()))
        database.commit()
        database.close()
        with Ledger(self.root, "run", url) as ledger:
            self.assertEqual(ledger.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(dict(tuple(row) for row in ledger.db.execute("SELECT id,state FROM publication_intents")),
                             {"applied": "applied", "pending": "pending", "reset": "pending"})
            self.assertEqual(ledger.db.execute("SELECT receipt FROM publication_intents WHERE id='pending'").fetchone()[0], "")
            self.assertEqual(ledger.db.execute("SELECT intent FROM publication_lock").fetchone()[0], "pending")
            self.assertEqual([tuple(row) for row in ledger.db.execute("SELECT intent,event,value FROM publication_events")],
                             [("pending", "response", '{"id":10}'), ("reset", "no_effect", '{"absent":true}')])
            self.assertEqual([tuple(row) for row in ledger.db.execute("SELECT effect,intent FROM publication_effects")],
                             [("comment:9", "applied")])
            # Neither migrated write reports no_effect or dispatches again.
            provider = FakeGitHub()
            publisher = Publication(ledger, provider, self.root,
                                    {"logical_run": "run", "pr": pr, "authorizations": dict(GRANTS),
                                     "options": {"permissions": {key: True for key in PERMISSION_INPUTS}}},
                                    source_validator=lambda source: True)
            for ident in ("pending", "reset"):
                self.assertEqual(publisher.reconcile(ident)["outcome"], "unknown")
                with self.assertRaises(UncertainEffect):
                    publisher.execute(ident, {})
            self.assertEqual(provider.writes, [])


if __name__ == "__main__":
    unittest.main()
