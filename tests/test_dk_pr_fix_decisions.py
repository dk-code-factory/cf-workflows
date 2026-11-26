"""Sourced PR decisions through the installed dk-pr-fix helpers (#28).

Each case uses a fresh state namespace, a disposable run ledger and
reconciliation record, and a fail-closed fake GitHub transport that counts
captures and writes: any route it does not model raises instead of reaching a
live service. The installed helpers run in-process, as the helper containers
would run them for task.yml, reconciliation.yml and the publication stage. The
reconciliation and audience sessions are represented only by the reports a test
feeds them; every claim that would dispatch one is counted. Nothing here starts
an engine, provider or host, runs the YAML graph, opens a real input gate or
certifies GitHub conformance.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
import test_prepare
from prfix import app, decisions, publishing
from prfix.core import Ledger, digest, identity
from prfix.prepare import CHECKLIST
from prfix.publication import PublicationError, original_discussion
from test_publication import BASE, GRANTS, HEAD, FakeGitHub

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, decisions as library  # noqa: E402
from test_dk_pr_fix import FakeTransport, PreflightFixture  # noqa: E402

URL = "https://github.com/o/r/pull/7"
PRODUCER = "agent_" + "1" * 32
SECOND = "agent_" + "3" * 32
AUDIENCE = "agent_" + "2" * 32
USER = {"id": 11, "login": "maintainer", "type": "User"}
MEMBER = {"id": 2, "login": "reviewer", "type": "User"}
BOT = {"id": 4, "login": "lint[bot]", "type": "Bot"}
INSTALL = ROOT / "cf_workflows" / "workflows" / "dk-pr-fix"


def at(minute):
    return f"2026-11-25T10:{minute:02d}:00Z"


class GitHub(FakeGitHub):
    """Counted captures and body writes; the discussion has user, maintainer and bot sources."""

    def __init__(self):
        super().__init__()
        self.counts = Counter()
        self.ev["metadata"]["user"] = copy.deepcopy(USER)
        self.ev["metadata"]["body"] = "Adds the cache.\n\n## Testing\n\nUnit tests."
        self.ev["comments"] = [
            {"id": 50, "user": USER, "author_association": "OWNER", "created_at": at(1),
             "html_url": URL + "#issuecomment-50", "body": "Keep the cache in memory."},
            {"id": 51, "user": MEMBER, "author_association": "MEMBER", "created_at": at(2),
             "html_url": URL + "#issuecomment-51", "body": "Use SQLite for the cache instead."},
            {"id": 52, "user": BOT, "author_association": "NONE", "created_at": at(3),
             "html_url": URL + "#issuecomment-52", "body": "Consider a size limit."},
            {"id": 53, "user": MEMBER, "author_association": "MEMBER", "created_at": at(4),
             "html_url": URL + "#issuecomment-53", "body": "Log cache misses at debug level."},
        ]

    def capture(self, pr):
        self.counts["capture"] += 1
        return super().capture(pr)

    def request(self, method, path, payload=None):
        if method in ("PATCH", "PUT", "POST") and not (path == "graphql" and payload["query"].startswith("query")):
            self.counts["write"] += 1
        return super().request(method, path, payload)


def decision(ident, status, sources, subject=None, **extra):
    return {"id": ident, "subject": subject or ident, "text": "Decision " + ident + ".", "status": status,
            "scope": "this PR", "sources": list(sources), **extra}


def contradiction(ident, involved, sources, resolution=None):
    return {"id": ident, "subject": ident, "decisions": list(involved), "sources": list(sources),
            "question": "Which direction applies to " + ident + "?", "resolution": resolution}


def result(decided=(), contradictions=(), questions=()):
    return {"summary": "Reconciled.", "decisions": list(decided), "contradictions": list(contradictions),
            "questions": list(questions), "language_review": {"passes": 1, "changed": [False], "ambiguities": []}}


class Fixture(unittest.TestCase):
    def setUp(self):
        git_fixture.state(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "source.txt").write_text("cache implementation\n")
        self.pr = identity(URL)
        self.github = GitHub()
        self.plan = {
            "format": "fixture", "mode": "fix", "logical_run": "fixture", "pr": self.pr,
            "instructions": "Add the cache and name the API fetch_all.",
            "options": {"permissions": {}, "accounts": [{"host": "github.com", "id": 11}]},
            "authorizations": {}, "settings": {},
            "tasks": [self.task("task_decisions", "decisions"), self.task("task_describe", "description"),
                      self.task("task_address", "fix", writes=True)],
            "stages": [{"id": "stage_1", "kind": "tasks", "phase": "tasks", "decisions": "task_decisions"},
                       {"id": "stage_2", "kind": "publication", "phase": "tasks"}],
            "definitions": {"audience": "audience-definition"}, "helper_manifest": {},
            "limits": {"retries_per_task": 1, "review_cycles": 5}}
        self.ledger = self.open()
        self.tree = "fixture-tree"
        snapshot = patch.object(app, "_snapshot", side_effect=lambda *args: self.snapshot())
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.sessions = 0

    def open(self):
        """A new connection to the same run ledger, as a replacement run would open it."""
        ledger = Ledger(self.root, "fixture", self.pr["url"])
        self.addCleanup(ledger.close)
        return ledger

    def snapshot(self):
        """The capture fields the helpers bind, with this run's own exact effects removed."""
        ev = copy.deepcopy(self.github.ev)
        meta = ev["metadata"]
        discussion = {"title": meta["title"], "body": meta["body"],
                      **{key: ev[key] for key in ("comments", "reviews", "inline", "threads")}}
        with Ledger(self.root, "fixture", self.pr["url"], read_only=True) as ledger:
            discussion = original_discussion(ledger, self.plan, discussion)
        return {"pr": self.pr, "author_id": 11, "actor_id": 11, "head_repository": "github.com/o/r",
                "head_branch": "feature", "target_branch": "main", "target_tree": "target-tree",
                "base_tree": "base-tree", "local_tree": self.tree, "diff": "diff-" + self.tree,
                "discussion": digest(discussion), "instructions": digest(self.plan["instructions"]),
                # As capture.py: settings digest the run's options (accounts among them) and settings.
                "settings": digest({"options": {key: value for key, value in self.plan["options"].items()
                                                if key not in ("tasks", "finalizers", "reviewers", "limits")},
                                    "settings": self.plan["settings"]}),
                "resource_digests": {}, "definition_digests": {},
                "head": HEAD, "target": BASE, "local_head": HEAD, "dirty": False,
                "identities": {"current": True, "pin": HEAD, "metadata_head": HEAD, "git_head": None,
                               "mismatched": []}}

    @staticmethod
    def task(key, kind, writes=False):
        return {"id": key, "skill": key[5:], "kind": kind, "definition": "leaf", "definition_digest": "leaf-digest",
                "assignment": "Fixture " + kind + " assignment.", "resources": [], "checklist": CHECKLIST,
                "required": True, "writes": writes, "options": {}, "history": False, "head_sensitive": False,
                "cacheable": True}

    def report(self, **values):
        evidence = [{"id": "source", "path": "source.txt",
                     "sha256": hashlib.sha256((self.root / "source.txt").read_bytes()).hexdigest()}]
        report = {"complete": True, "findings": [], "evidence": evidence, "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["source"]}
                                for item in CHECKLIST]}
        report.update(values)
        return report

    def claim(self, task="task_decisions", ledger=None):
        claimed = app.prepare_claim(self.root, self.github, ledger or self.ledger, self.plan, {"task": task})
        if claimed["invoke"] and task == "task_decisions":
            self.sessions += 1  # One reconciliation session dispatched for this claim.
        return claimed

    def submit(self, claimed, output, round_number=0, agent=PRODUCER, ledger=None):
        report = self.report(deliverables={"decisions": output})
        return app.accept(self.root, self.github, ledger or self.ledger, self.plan,
                          {"claim": claimed["claim"], "agent": agent, "report": report, "round": round_number})

    def helper(self, operation, inputs=None, ledger=None):
        return getattr(decisions, operation)(self.root, self.github, ledger or self.ledger, self.plan, inputs or {})

    def baseline(self):
        """Settled request and maintainer decisions, a bot proposal and no contradictions."""
        return result([decision("api-name", "settled", ["instruction:request"]),
                       decision("cache-store", "settled", ["comment:50"]),
                       decision("miss-logging", "settled", ["comment:53"]),
                       decision("size-limit", "proposal", ["comment:52"])])

    def reconciled(self, output=None):
        claimed = self.claim()
        self.assertTrue(claimed["invoke"])
        accepted = self.submit(claimed, output or self.baseline())
        self.assertTrue(accepted["complete"], accepted)
        return claimed, accepted


class Reconcile(Fixture):
    def test_sourced_decisions_and_contradictions_stay_distinct_until_the_user_answers(self):
        claimed = self.claim()
        context = claimed["task_input"]["decisions"]
        authority = {item["id"]: item["authority"] for item in context["sources"]}
        self.assertEqual(authority, {"pr-body": "user", "comment:50": "user", "comment:51": "maintainer",
                                     "comment:52": "bot", "comment:53": "maintainer",
                                     "instruction:request": "user"})
        self.assertEqual(context["subjects"]["full"], True)
        self.assertEqual((context["role_prompt"], context["reconciliation_prompt"]),
                         (decisions.ROLE_PROMPT, decisions.RECONCILIATION_PROMPT))
        # A bot alone cannot settle; the correction stays in the original session.
        bad = result([decision("size-limit", "settled", ["comment:52"])])
        rejected = self.submit(claimed, bad)
        self.assertFalse(rejected["complete"])
        self.assertIn("decisions: result.decisions[0].sources", rejected["feedback"]["contract_error"])
        contested = result(
            [decision("api-name", "settled", ["instruction:request"]),
             decision("cache-memory", "settled", ["comment:50"], subject="cache"),
             decision("cache-sqlite", "settled", ["comment:51"], subject="cache"),
             decision("size-limit", "proposal", ["comment:52"])],
            [contradiction("cache", ["cache-memory", "cache-sqlite"], ["comment:50", "comment:51"])])
        self.assertTrue(self.submit(claimed, contested, round_number=1)["complete"])

        # Distinct groups: the contested pair is neither settled nor handed to a fixer.
        gate = self.helper("gate", {"task": "task_decisions"})
        self.assertEqual((gate["ask"], gate["refresh"], gate["evidence_id"]), (True, True, claimed["claim"]))
        self.assertEqual([item["id"] for item in gate["payload"]["contradictions"]], ["cache"])
        handed = self.claim("task_describe")["task_input"]["decisions"]
        self.assertEqual(handed["dependent_work"], "blocked")
        self.assertEqual([d["id"] for d in handed["commitments"]], ["api-name"])
        self.assertEqual([d["id"] for d in handed["contested"]], ["cache-memory", "cache-sqlite"])
        self.assertEqual([d["id"] for d in handed["proposals"]], ["size-limit"])
        with self.assertRaisesRegex(ValueError, "blocked by unresolved decisions"):
            self.claim("task_address")
        with self.assertRaisesRegex(ValueError, "dependent work is blocked by unresolved decisions: cache"):
            self.helper("status")

        # The engine gate's answer becomes the newest user source of one refresh.
        answer = {"answer": "Keep it in memory.", "answer_id": "delivery-1", "gate_id": "gate-1",
                  "evidence_id": gate["evidence_id"]}
        with self.assertRaisesRegex(decisions.DecisionError, "another reconciliation result"):
            self.helper("answer", {**answer, "evidence_id": "claim_other"})
        self.assertEqual(self.helper("answer", answer)["evidence_id"], claimed["claim"])
        self.helper("answer", answer)  # A replayed delivery is a no-op.
        with self.assertRaisesRegex(decisions.DecisionError, "already holds another answer"):
            self.helper("answer", {**answer, "answer": "Use SQLite."})
        self.assertEqual(self.helper("gate"), {"ask": False, "refresh": True, "evidence_id": "", "prompt": "",
                                               "payload": {}})
        refreshed = self.claim()
        context = refreshed["task_input"]["decisions"]
        self.assertEqual(context["sources"][-1]["id"], "answer:delivery-1")
        self.assertEqual(context["subjects"]["decisions"], ["cache-memory", "cache-sqlite"])
        self.assertEqual(context["subjects"]["contradictions"], ["cache"])
        self.assertEqual([d["id"] for d in context["retained"]["decisions"]], ["api-name", "size-limit"])
        # An older user comment cannot outrank the later maintainer comment.
        stale = result([decision("cache-memory", "settled", ["comment:50"], subject="cache"),
                        decision("cache-sqlite", "excluded", ["comment:50"], subject="cache")],
                       [contradiction("cache", ["cache-memory", "cache-sqlite"], ["comment:50", "comment:51"],
                                      {"direction": "memory", "decision": "cache-memory", "sources": ["comment:50"]})])
        self.assertIn("only a user source at least as recent",
                      self.submit(refreshed, stale)["feedback"]["contract_error"])
        answered = result([decision("cache-memory", "settled", ["comment:50", "answer:delivery-1"], subject="cache"),
                           decision("cache-sqlite", "excluded", ["answer:delivery-1"], subject="cache")],
                          [contradiction("cache", ["cache-memory", "cache-sqlite"], ["comment:50", "comment:51"],
                                         {"direction": "memory", "decision": "cache-memory",
                                          "sources": ["answer:delivery-1"]})])
        self.assertTrue(self.submit(refreshed, answered, round_number=1)["complete"])

        self.assertEqual(self.helper("gate")["ask"], False)
        status = self.helper("status")
        self.assertEqual((status["dependent_work"], status["section"]), ("released", "private"))
        handed = self.claim("task_address")["task_input"]["decisions"]
        self.assertEqual([d["id"] for d in handed["commitments"]], ["api-name", "cache-memory"])
        self.assertEqual([d["id"] for d in handed["exceptions"]], ["cache-sqlite"])
        self.assertEqual([item["id"] for item in handed["unresolved"]], [])
        self.assertEqual(self.sessions, 2, "the first reconciliation and one refresh with the answer")
        self.assertEqual(self.github.counts["write"], 0)

    def test_unchanged_inputs_reuse_and_changed_sources_refresh_only_what_cites_them(self):
        first, _ = self.reconciled()
        # A fix commit and a fresh session: same binding, no session, same report.
        self.tree = "fixed-tree"
        reused = self.claim(ledger=self.open())
        self.assertEqual((reused["invoke"], reused["claim"], reused["report"]), (False, first["claim"], _["report"]))
        self.assertEqual(self.sessions, 1)

        # An edited maintainer comment refreshes only the decision that cites it.
        self.github.ev["comments"][3]["body"] = "Log cache misses at info level."
        claimed = self.claim()
        self.assertTrue(claimed["invoke"])
        context = claimed["task_input"]["decisions"]
        self.assertEqual(context["subjects"]["sources"], ["comment:53"])
        self.assertEqual(context["subjects"]["decisions"], ["miss-logging"])
        self.assertEqual([d["id"] for d in context["retained"]["decisions"]], ["api-name", "cache-store", "size-limit"])
        # An unaffected decision cannot change without a changed source; an affected one must come back.
        rewritten = result([decision("miss-logging", "settled", ["comment:53"]),
                            {**decision("cache-store", "settled", ["comment:50"]), "text": "Rewritten."}])
        self.assertIn("unaffected decision changed", self.submit(claimed, rewritten)["feedback"]["contract_error"])
        omitted = result([])
        self.assertIn("affected decisions must be returned",
                      self.submit(claimed, omitted, round_number=1)["feedback"]["contract_error"])
        self.assertTrue(self.submit(claimed, result([decision("miss-logging", "settled", ["comment:53"])]),
                                    round_number=2)["complete"])

        # A later user comment governs the earlier request: it may revise a retained decision.
        self.github.ev["comments"].append({"id": 54, "user": USER, "author_association": "OWNER", "created_at": at(5),
                                           "html_url": URL + "#issuecomment-54", "body": "Name it load_all instead."})
        claimed = self.claim()
        context = claimed["task_input"]["decisions"]
        order = [item["id"] for item in context["sources"]]
        self.assertLess(order.index("instruction:request"), order.index("comment:54"))
        revised = result([{**decision("api-name", "settled", ["comment:54"]), "text": "Name the API load_all."}])
        self.assertTrue(self.submit(claimed, revised)["complete"])
        handed = decisions.handoff(self.root, self.ledger, self.plan)
        self.assertEqual(next(d for d in handed["commitments"] if d["id"] == "api-name")["sources"], ["comment:54"])
        self.assertEqual(self.sessions, 3)

    def test_changed_accounts_start_a_new_claim_that_refreshes_every_subject(self):
        first, accepted = self.reconciled()
        self.assertFalse(self.claim()["invoke"])
        # User authority comes from accounts: without them the owner's comments are maintainer sources.
        self.plan["options"]["accounts"] = []
        claimed = self.claim()
        self.assertTrue(claimed["invoke"])
        self.assertNotEqual(claimed["claim"], first["claim"])
        context = claimed["task_input"]["decisions"]
        self.assertEqual(context["subjects"]["full"], "governing inputs changed")
        self.assertEqual(context["retained"], {"decisions": [], "contradictions": []})
        self.assertEqual({item["id"]: item["authority"] for item in context["sources"]}["comment:50"], "maintainer")
        self.assertEqual(self.sessions, 2)

    def test_a_discussion_over_the_result_bound_is_reconciled_whole(self):
        self.github.ev["comments"] += [
            {"id": 100 + n, "user": MEMBER, "author_association": "MEMBER", "created_at": at(10 + n % 50),
             "html_url": URL + "#issuecomment-" + str(100 + n), "body": "Note " + str(n) + "."}
            for n in range(decisions.MAX_ITEMS + 4)]
        claimed = self.claim()
        self.assertTrue(claimed["invoke"])
        sources = claimed["task_input"]["decisions"]["sources"]
        self.assertEqual(len(sources), decisions.MAX_ITEMS + 10)
        self.assertTrue(self.submit(claimed, self.baseline())["complete"])

    def test_a_dependent_occurrence_covers_the_reconciliation_it_was_handed(self):
        self.reconciled()
        describe = self.claim("task_describe")
        self.assertEqual([d["id"] for d in describe["task_input"]["decisions"]["commitments"]],
                         ["api-name", "cache-store", "miss-logging"])
        accepted = app.accept(self.root, self.github, self.ledger, self.plan,
                              {"claim": describe["claim"], "agent": SECOND, "report": self.report(), "round": 0})
        self.assertTrue(accepted["complete"])
        self.assertFalse(self.claim("task_describe")["invoke"])
        # A refreshed reconciliation is new input for the description.
        self.github.ev["comments"][3]["body"] = "Do not log cache misses."
        claimed = self.claim()
        self.submit(claimed, result([decision("miss-logging", "settled", ["comment:53"])]))
        self.assertTrue(self.claim("task_describe")["invoke"])

    def test_a_clarification_recorded_during_the_turn_is_never_accepted_over(self):
        claimed = self.claim()
        self.submit(claimed, result([decision("cache-memory", "settled", ["comment:50"]),
                                     decision("cache-sqlite", "settled", ["comment:51"])],
                                    [contradiction("cache", ["cache-memory", "cache-sqlite"],
                                                   ["comment:50", "comment:51"])]))
        gate = self.helper("gate")
        self.helper("answer", {"answer": {"choice": "memory"}, "answer_id": "a1", "gate_id": "g1",
                               "evidence_id": gate["evidence_id"]})
        refreshed = self.claim()
        self.assertEqual(refreshed["task_input"]["decisions"]["sources"][-1]["text"], '{"choice":"memory"}')
        # A second answer lands while the refresh session runs: its report no longer covers the inputs.
        record = decisions.Record(decisions.ledger_path(Path(self.state_dir), "fixture"), logical_run="fixture",
                                  pr=self.pr)
        record.answer(gate_id="g2", answer_id="a2", evidence_id="other-evidence", value="SQLite after all.")
        record.close()
        with self.assertRaisesRegex(ValueError, "recorded during the native turn"):
            self.submit(refreshed, result())


class Publish(Fixture):
    def authorize(self):
        self.plan["options"]["permissions"] = {"edit_pr": True}
        self.plan["authorizations"] = {"edit_pr": GRANTS["edit_pr"]}

    def listed(self, ledger=None):
        return publishing.publication_list(self.root, self.github, ledger or self.ledger, self.plan,
                                           {"kind": "ordinary"})["items"]

    def publish(self, item, ledger=None):
        ledger = ledger or self.ledger
        prepared = publishing.publication_prepare(self.root, self.github, ledger, self.plan, {"proposal": item})
        if not prepared["invoke"]:
            return publishing.publication_recover(self.root, self.github, ledger, self.plan, {"id": prepared["id"]})
        verdict = self.report(audience={"fingerprint": prepared["fingerprint"], "suitable": True,
                                        "audience": "PR participants", "evidence": ["source"]})
        return publishing.publication_execute(self.root, self.github, ledger, self.plan,
                                              {"id": prepared["id"], "agent": AUDIENCE, "report": verdict})

    def test_the_section_is_a_private_draft_without_edit_pr_authority(self):
        self.reconciled()
        self.assertEqual(self.listed(), [])
        self.plan["options"]["permissions"] = {"edit_pr": True}
        self.assertEqual(self.listed(), [], "an enabled input without the user's authorization publishes nothing")
        self.assertEqual(self.github.counts["write"], 0)

    def test_authorized_section_publishes_once_through_the_shared_stage(self):
        self.authorize()
        self.reconciled()
        [item] = self.listed()
        self.assertEqual((item["kind"], set(item["payload"])), ("edit_pr", {"body"}))
        body = item["payload"]["body"]
        self.assertTrue(body.startswith("Adds the cache.\n\n## Testing\n\nUnit tests.\n\n## Decisions\n\n"))
        # Only settled decisions with a public user or maintainer source; the request stays private.
        self.assertIn("<!-- cf-workflows:decision cache-store -->", body)
        self.assertIn("<!-- cf-workflows:decision miss-logging -->", body)
        self.assertIn("(" + "[comment](" + URL + "#issuecomment-50))", body)
        self.assertNotIn("api-name", body)
        self.assertNotIn("size-limit", body)
        # The response is lost after the effect: the original is reconciled, never resent.
        self.github.lose_response = True
        with self.assertRaises((OSError, PublicationError)):
            self.publish(item)
        self.github.lose_response = False
        self.publish(item, self.open())
        self.assertEqual(self.github.counts["write"], 1)
        self.assertEqual(self.github.ev["metadata"]["body"], body)
        # The run's own section edit is not new discussion: no relisting and no new session.
        self.assertEqual(self.listed(self.open()), [])
        self.assertFalse(self.claim(ledger=self.open())["invoke"])
        self.assertEqual(self.sessions, 1)
        self.assertEqual(self.github.counts["write"], 1)

    def test_a_description_body_edit_carries_the_section_in_one_intent(self):
        self.authorize()
        self.reconciled()
        describe = self.claim("task_describe")
        # The native wire form: each proposed operation is a JSON-encoded object.
        operation = json.dumps({"kind": "edit_pr", "head": HEAD, "target": BASE,
                                "payload": {"title": "Cache", "body": "New description."}})
        report = self.report(operations=[operation])
        self.assertTrue(app.accept(self.root, self.github, self.ledger, self.plan,
                                   {"claim": describe["claim"], "agent": SECOND, "report": report,
                                    "round": 0})["complete"])
        [item] = self.listed()
        self.assertEqual(item["payload"]["title"], "Cache")
        self.assertTrue(item["payload"]["body"].startswith("New description.\n\n## Decisions\n\n- "))
        self.assertEqual(len(item["sources"]), 2)
        self.assertTrue(item["id"].startswith("decisions_"))
        self.publish(item)
        self.assertEqual(self.github.counts["write"], 1)
        # The description report is still listed with the same exact intent, which is only reconciled.
        self.assertEqual(self.listed(), [item])
        self.publish(item)
        self.assertEqual(self.github.counts["write"], 1, "the applied intent is reconciled, never resent")

    def test_a_stale_result_never_publishes(self):
        self.authorize()
        self.reconciled()
        self.github.ev["comments"][1]["body"] = "Use SQLite; this is final."
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.github.counts["write"], 0)


class Parity(unittest.TestCase):
    """The installed checks and the library procedure give the same verdicts."""

    def sources(self):
        def source(ident, authority, order):
            return {"id": ident, "kind": "comment", "authority": authority, "order": order, "public": True,
                    "url": URL, "text": ident, "author": None, "sha256": "0" * 64}
        return [source("comment:1", "maintainer", 0), source("comment:2", "bot", 1),
                source("instruction:request", "user", 2), source("comment:3", "user", 3)]

    def test_baseline_prompts_and_options_match_the_library(self):
        self.assertEqual(decisions.OPTIONS, library.OPTIONS)
        self.assertEqual((decisions.ROLE_PROMPT, decisions.LANGUAGE_PROMPT), (library.ROLE_PROMPT,
                                                                             library.LANGUAGE_PROMPT))
        self.assertEqual(decisions.STATUSES, library.STATUSES)

    def test_result_checks_agree(self):
        cases = {
            "settled": result([decision("a", "settled", ["comment:1"])]),
            "bot-settled": result([decision("a", "settled", ["comment:2"])]),
            "maintainer-excludes": result([decision("a", "excluded", ["comment:1"])]),
            "pause-needs-revisit": result([decision("a", "paused", ["comment:3"])]),
            "stale-resolution": result([decision("a", "settled", ["comment:1"]),
                                        decision("b", "settled", ["comment:3"])],
                                       [contradiction("c", ["a", "b"], ["comment:1", "comment:3"],
                                                      {"direction": "a", "decision": "a", "sources": ["comment:1"]})]),
            "user-resolution": result([decision("a", "settled", ["comment:1"]),
                                       decision("b", "settled", ["comment:3"])],
                                      [contradiction("c", ["a", "b"], ["comment:1", "comment:3"],
                                                     {"direction": "b", "decision": "b", "sources": ["comment:3"]})]),
            "language": {**result(), "language_review": {"passes": 2, "changed": [False, False], "ambiguities": []}},
            "extra-field": {**result(), "operations": []},
        }
        arguments = {"sources": self.sources(), "retained": {"decisions": [], "contradictions": []}, "previous": None,
                     "changed": {"comment:1", "comment:2", "instruction:request", "comment:3"},
                     "subjects": {"decisions": [], "contradictions": []}, "passes": 3}
        for name, output in cases.items():
            with self.subTest(case=name):
                try:
                    expected = ("accepted", library.check_result(copy.deepcopy(output), **arguments))
                except admission.AdmissionError as error:
                    expected = ("rejected", error.path, error.message)
                try:
                    observed = ("accepted", decisions.check_result(copy.deepcopy(output), **arguments))
                except decisions.DecisionError as error:
                    observed = ("rejected", error.path, error.message)
                self.assertEqual(observed, expected)

    def test_section_drafts_agree(self):
        sources = self.sources()
        value = {"decisions": [decision("a", "settled", ["comment:1"]),
                               decision("b", "settled", ["instruction:request"]),
                               decision("c", "excluded", ["comment:3"])],
                 "contradictions": [], "questions": []}
        body = ("Intro\r\n\r\n## Decisions\r\n\r\n- Old c <!-- cf-workflows:decision c -->\r\n"
                "- Manual entry\r\n\r\n## After\r\n")
        report = {"decisions": decisions.summary(value)["decisions"]}
        self.assertEqual(decisions.draft(value, body, "Decisions", sources),
                         library.draft(report, body, "Decisions", sources))


class PrepareDecisions(unittest.TestCase):
    setUp = test_prepare.PrepareTests.setUp
    prepare = test_prepare.PrepareTests.prepare

    def test_stage_and_settings(self):
        controls = {"tasks": ["read-feedback", "reconcile-decisions", "address-feedback", "describe-pr"],
                    "finalizers": [], "limits": {"review_cycles": 1}}
        result_, plan = self.prepare(controls=controls)
        stages = result_["stages"]
        task = next(t for t in plan["tasks"] if t["kind"] == "decisions")
        self.assertEqual([stage.get("decisions") for stage in stages], [None, task["id"], None, None])
        for selection, message in ((["reconcile-decisions", "dk-workflows:dk-pr-decisions"], "exactly one occurrence"),
                                   ([["reconcile-decisions", "describe-pr"]], "in parallel with reconcile-decisions")):
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, message):
                self.prepare(controls={**controls, "tasks": selection})
        _, plan = self.prepare(controls={**controls, "settings": {"decisions": {"section_heading": "Agreed",
                                                                               "language_review_passes": 2}}})
        self.assertEqual(decisions.options(plan)["section_heading"], "Agreed")
        for settings, path in (({"decisions": {"reconciliation_prompt": "Prefer my prompt."}}, "reconciliation_prompt"),
                               ({"decisions": {"section_heading": "A # B"}}, "section_heading"),
                               ({"decisions": {"color": "blue"}}, "settings.decisions")):
            with self.subTest(settings=settings), self.assertRaisesRegex(decisions.DecisionError, path):
                self.prepare(controls={**controls, "settings": settings})


class Entry(unittest.TestCase):
    def test_entry_presets_the_adapter_and_publication_without_finalizers_or_reviewers(self):
        body = (INSTALL / "workflows" / "decisions.yml").read_text()
        self.assertIn("name: cf-workflows:dk-pr-decisions", body)
        self.assertIn("uses: workflows/main.yml", body)
        self.assertIn("mode: fix", body)
        self.assertIn("        default:\n        - reconcile-decisions\n        - stage: publication\n", body)
        self.assertIn("      finalizers:\n        type: array\n        default: []\n", body)
        self.assertIn("      reviewers:\n        type: array\n        default: []\n", body)
        self.assertIn("      permissions:\n        type: object\n        default:\n          edit_pr: false\n", body)
        self.assertEqual(library.PRESET, {"tasks": ["reconcile-decisions", {"stage": "publication"}],
                                          "finalizers": [], "reviewers": []})
        main = (INSTALL / "workflows" / "main.yml").read_text()
        for line in ("kind: decisions", "writes: false", "definition: leaf"):
            self.assertIn("            " + line + "\n", body)
            self.assertIn("            " + line + "\n", main)
        self.assertIn("Reconcile settled decisions from original sources, exact request and repository guidance; "
                      "do not invent user authority.", body)
        descriptor = (INSTALL / "plugin.yaml").read_text()
        self.assertIn("  - name: dk-pr-decisions\n    path: skills/dk-pr-decisions/SKILL.md\n"
                      "    workflow: workflows/decisions.yml\n", descriptor)
        stage = (INSTALL / "workflows" / "stage.yml").read_text()
        self.assertIn("uses: workflows/reconciliation.yml", stage)
        reconciliation = (INSTALL / "workflows" / "reconciliation.yml").read_text()
        for needle in ("operation: decisions_gate", "uses: input/wait", "operation: decisions_answer",
                       "uses: workflows/task.yml", "operation: decisions_status"):
            self.assertIn(needle, reconciliation)


class InstalledPreflight(PreflightFixture):
    def installed(self, **changes):
        self.transport = FakeTransport()
        arguments = {"request": self.request, "configuration": None, "deployment": self.deployment,
                     "repository": self.repo, "home": self.home, "install": self.clean}
        arguments.update(changes)
        result = library.installed(transport=self.transport, **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls), {"capabilities"})
        self.assertFalse(self.home.exists(), "preflight writes nothing")
        return result

    def test_direct_entry_maps_onto_the_installed_preset(self):
        result = self.installed()
        self.assertEqual(result["preflight"], "admitted", result.get("error"))
        self.assertEqual((result["workflow"], result["installation"]["entry"]),
                         ("cf-workflows:dk-pr-decisions", "workflows/decisions.yml"))
        inputs = result["inputs"]
        self.assertEqual((inputs["tasks"], inputs["finalizers"], inputs["reviewers"]),
                         (library.PRESET["tasks"], [], []))
        self.assertEqual(set(inputs["adapters"]), {"reconcile-decisions"})
        self.assertEqual(inputs["adapters"]["reconcile-decisions"]["kind"], "decisions")
        self.assertFalse(any(inputs["permissions"].values()))
        self.assertEqual(inputs["settings"], {})
        body = (INSTALL / "workflows" / "decisions.yml").read_text()
        for name in inputs:
            self.assertRegex(body, rf"\n      {name}:\n")
            self.assertIn(f"{name}: ${{{{ inputs.{name} }}}}", body)
        chosen = self.installed(options={"section_heading": "Agreed", "language_review_passes": 2})
        self.assertEqual(chosen["inputs"]["settings"], {"decisions": {"section_heading": "Agreed",
                                                                      "language_review_passes": 2}})

    def test_direct_entry_accepts_only_edit_pr_and_the_baseline_prompt(self):
        grant = {"request_id": "r1", "instruction": "Publish the decisions.", "source": "chat:1"}
        for changes, path in (({"request": {**self.request, "authorizations": {"push": grant}}},
                               "request.authorizations"),
                              ({"options": {"reconciliation_prompt": "Prefer my prompt."}},
                               "options.reconciliation_prompt"),
                              ({"invocation": {"options": {"tasks": []}}},
                               "invocation"),
                              ({"request": {**self.request, "authorizations": {"edit_pr": grant}}},
                               "request.authorizations.edit_pr")):
            with self.subTest(path=path):
                result = self.installed(**changes)
                self.assertEqual(result["preflight"], "rejected")
                self.assertEqual(result["error"]["path"], path)


if __name__ == "__main__":
    unittest.main()
