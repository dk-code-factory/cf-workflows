"""The installed current-alignment gate (#23, dk-workflows#239 and #241).

Disposable Git repositories, a fresh state namespace and the fail-closed fake
GitHub transport of the incoming-review fixtures. Retained alternatives
analysis is written with the library ledger, as the alternatives entries keep
it; its native sessions are only results a test hands that ledger, and they are
counted per stage. The installed helpers read the record through the
``alignment_gate`` operation and coverage. Nothing here starts an engine,
provider or host, and nothing certifies native execution or GitHub
publication conformance.
"""
from collections import Counter
from pathlib import Path
import unittest

from test_dk_pr_review_findings import GRANT, INLINE, MERGE, Fixture, git  # also puts the helpers on sys.path

import action
from prfix import alignment as records, app, publishing
from prfix.core import state_directory
from cf_workflows import alignment
from test_dk_pr_review import AGENTS, PR_URL

REQUIREMENTS = {"requirements": "a" * 64}
NEW_REQUIREMENTS = {"requirements": "b" * 64}
CHANGES = "Route retries through the shared scheduler."


class Analysis:
    """The alternatives entries' retained branch record of the fixture PR, with counted sessions."""

    def __init__(self, state, pr_url=PR_URL):
        run = alignment.pr_run(PR_URL)
        self.path = alignment.ledger_path(state, run)
        self.ledger = alignment.Ledger(self.path, logical_run=run, pr={"url": pr_url})
        self.sessions = Counter()
        self.count = 0

    def stage(self, stage, result, head, decision=None, inputs=None):
        self.count += 1
        operation = f"op-{self.count}"
        prepared = self.ledger.prepare(stage, decision, head=head, operation=operation,
                                       inputs={} if stage == "verification" else inputs or REQUIREMENTS)
        if prepared["action"] == "run":
            self.sessions[alignment.key(stage, decision)] += 1
            self.ledger.finish(operation, result, session=f"session-{self.count}")
        return prepared["action"]

    def architecture(self, head, through="interactions"):
        steps = [("assessment", {"purpose": "Bound retries"}, None),
                 ("inventory", {"decisions": [{"id": "retry", "questionable": True}]}, None),
                 ("comparison", {"solutions": [{"id": "retain"}, {"id": "scheduler"}]}, "retry"),
                 ("selection", {"choice": "scheduler"}, "retry"),
                 ("interactions", {"compatible": True, "reason": "One decision."}, None)]
        for stage, result, decision in steps:
            self.stage(stage, result, head, decision)
            if stage == through:
                return

    def align(self, value, head, inputs=None):
        result = {"alignment": value, "reason": "Judged against the current sources."}
        if value == "mismatch":
            result["required_changes"] = CHANGES
        return self.stage("alignment", result, head, inputs=inputs)

    def verify(self, head):
        return self.stage("verification", {"disposition": "confirmed", "reason": "Independently checked.",
                                           "required_changes": CHANGES}, head)

    def architectural(self):
        return {key: count for key, count in self.sessions.items() if not key.startswith(("alignment", "verification"))}


class InstalledGate(Fixture):
    def setUp(self):
        super().setUp()
        self.earlier = git(self.root, "rev-parse", "HEAD~1")
        state_directory(self.root, create=True)  # The suite's namespace, as preflight claims it.
        self.analyses = []
        self.addCleanup(lambda: [item.ledger.close() for item in self.analyses])

    def analysis(self, **kwargs):
        value = Analysis(state_directory(self.root), **kwargs)
        self.analyses.append(value)
        return value

    def gate(self, plan, ledger):
        return action.dispatch("alignment_gate", self.root, self.github, ledger, plan, {})

    def completion(self, plan, ledger):
        return action.dispatch("completion", self.root, self.github, ledger, plan, {})

    def test_no_retained_analysis_leaves_review_ungated_and_creates_no_record(self):
        plan = self.prepare()
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [])
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual((covered["alignment"]["outcome"], covered["alignment"]["gated"]), ("no_analysis", False))
        self.assertEqual((covered["review"]["outcome"], covered["review"]["clean_review"]), ("clean", True))
        self.assertEqual(self.gate(plan, ledger)["outcome"], "no_analysis")
        runs = state_directory(self.root) / "runs"
        self.assertEqual([path.name for path in runs.iterdir() if "alignment" in path.name], [])

    def test_dk_workflows_239_later_head_recovers_without_a_rejection_receipt(self):
        # A mismatch verified at an earlier head; no rejection receipt was ever published.
        analysis = self.analysis()
        analysis.architecture(self.earlier)
        analysis.align("mismatch", self.earlier)
        analysis.verify(self.earlier)
        plan = self.prepare(authorizations={"post_findings": GRANT, "merge": MERGE},
                            permissions={"post_findings": True, "merge": True})
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("inline", INLINE, "diff", path="code.py", line=1, side="RIGHT")])

        gate = self.gate(plan, ledger)
        self.assertEqual((gate["outcome"], gate["reason"], gate["retained"], gate["gated"], gate["release"]),
                         ("alignment_required", "later_head", True, True, False))
        self.assertEqual(gate["next"], {"stage": "alignment", "decision": None})
        self.assertEqual(len(gate["context"]), 2, "the earlier alignment and its verification are context only")
        self.assertFalse(gate["clean_review"] or gate["ready"] or gate["provisional"])
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual((covered["complete"], covered["settled"], covered["withheld"]),
                         (False, True, ["current_alignment"]))
        review = covered["review"]
        self.assertEqual((review["outcome"], review["partial"], review["clean_review"], review["alignment"]),
                         ("findings", True, False, "alignment_required"))

        # The independently verified finding still publishes, as a partial COMMENT only.
        listing, _ = self.barrier(plan, ledger)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual((self.writes(), self.github.posted[0]["event"]), (1, "COMMENT"))
        ready = publishing.readiness(self.root, self.github, ledger, plan)
        self.assertFalse(ready["ready"] or ready["clean_review"])
        self.assertTrue(any("alternatives" in blocker for blocker in ready["blockers"]))
        self.assertFalse(publishing.merge_proposal(self.root, self.github, ledger, plan, {})["invoke"])

        # The alternatives entries refresh only alignment at the later head: no authorization,
        # no repeated comparison or selection, and the allowance keeps counting.
        self.assertEqual(analysis.stage("comparison", {"solutions": [{"id": "x"}]}, self.commit, "retry"), "reuse")
        self.assertEqual(analysis.align("aligned", self.commit), "run")
        self.assertEqual(analysis.architectural(), {"assessment": 1, "inventory": 1, "comparison:retry": 1,
                                                    "selection:retry": 1, "interactions": 1})
        self.assertEqual(analysis.sessions["alignment"], 2)
        gate = self.gate(plan, ledger)
        self.assertEqual((gate["outcome"], gate["release"], gate["gated"]), ("released", True, False))
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual((covered["withheld"], covered["review"]["partial"], covered["review"]["alignment"]),
                         ([], False, "released"))
        self.assertEqual(self.native, Counter({"review": 1, "verification": 1, "audience": 1}),
                         "the gate repeats no reviewer or verifier session")

    def test_dk_workflows_241_new_evidence_refreshes_uncertain_alignment_before_a_clean_review(self):
        analysis = self.analysis()
        analysis.architecture(self.earlier)
        analysis.align("uncertain", self.commit)
        plan = self.prepare()
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [])
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual(covered["alignment"]["outcome"], "alignment_uncertain")
        # The allowance is the logical run's own alignment count, reported, never consumed by the gate.
        self.assertEqual(covered["alignment"]["allowance"]["allowance"],
                         "alignment:run-" + alignment.digest(plan["logical_run"])[:24])
        review = covered["review"]
        self.assertEqual((review["outcome"], review["clean_review"], review["partial"]), ("incomplete", False, True))
        # Analysis is settled and only the alignment is withheld: the run ends, unready.
        done = self.completion(plan, ledger)
        self.assertEqual((done["complete"], done["clean_review"], done["ready"], done["withheld"]),
                         (False, False, False, ["current_alignment"]))

        # New requirements reach alignment through the alternatives entries, not the installed run.
        self.assertEqual(analysis.align("aligned", self.commit, inputs=NEW_REQUIREMENTS), "run")
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual((covered["alignment"]["outcome"], covered["withheld"]), ("released", []))
        self.assertEqual((covered["review"]["outcome"], covered["review"]["clean_review"]), ("clean", True))
        self.assertEqual(sum(analysis.architectural().values()), 5, "no architectural stage repeated")
        self.assertEqual(self.native, Counter({"review": 1, "verification": 1}))

    def test_unsettled_analysis_gates_review_and_the_installed_run_never_runs_it(self):
        analysis = self.analysis()
        analysis.architecture(self.earlier, through="inventory")
        plan = self.prepare()
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [])
        before = analysis.path.read_bytes()
        gate = self.gate(plan, ledger)
        self.assertEqual((gate["outcome"], gate["next"]["stage"], gate["next"]["decision"], gate["gated"]),
                         ("analysis_required", "comparison", "retry", True))
        self.assertFalse(gate["authorization_required"])
        self.assertFalse(app.coverage(self.root, self.github, ledger, plan, {})["review"]["clean_review"])
        self.assertEqual(analysis.path.read_bytes(), before, "the gate reads the record and writes nothing")
        self.assertEqual(sum(analysis.sessions.values()), 2)

    def test_verified_mismatch_at_the_current_head_keeps_a_fix_run_unready(self):
        analysis = self.analysis()
        analysis.architecture(self.earlier)
        analysis.align("mismatch", self.commit)
        analysis.verify(self.commit)
        self.github.evidence["metadata"]["user"] = dict(self.github.evidence["viewer"])  # The actor's own PR.
        plan = self.prepare(mode="fix", authorizations={})
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [])
        fix = next(task for task in plan["tasks"] if task["kind"] == "fix")
        self.execute(plan, ledger, fix["skill"], AGENTS[2])
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual((covered["alignment"]["outcome"], covered["alignment"]["required_changes"]),
                         ("mismatch_verified", CHANGES))
        self.assertEqual((covered["complete"], covered["settled"], covered["withheld"]),
                         (False, True, ["current_alignment"]))
        done = self.completion(plan, ledger)
        self.assertFalse(done["complete"] or done["ready"] or done["clean_review"])

    def test_provisional_identity_marks_the_gate_and_still_withholds_findings(self):
        analysis = self.analysis()
        analysis.architecture(self.earlier)
        analysis.align("aligned", self.commit)
        self.github.evidence["metadata"]["head"]["sha"] = self.earlier
        plan = self.prepare()
        ledger = self.ledger(plan)
        self.reviewed(plan, ledger, [self.finding("inline", INLINE, "diff", path="code.py", line=1, side="RIGHT")])
        gate = self.gate(plan, ledger)
        self.assertEqual((gate["outcome"], gate["provisional"], gate["head"]), ("released", True, self.commit))
        self.assertEqual(self.listed(plan, ledger)["items"], [])
        self.assertEqual(self.writes(), 0)

    def test_foreign_or_unsupported_records_stop_the_gate(self):
        plan = self.prepare()
        ledger = self.ledger(plan)
        analysis = self.analysis(pr_url="https://github.com/owner/repo/pull/8")
        analysis.architecture(self.earlier, through="assessment")
        with self.assertRaisesRegex(ValueError, "another logical run or PR"):
            self.gate(plan, ledger)
        with self.assertRaisesRegex(ValueError, "unreadable"):
            app.coverage(self.root, self.github, ledger, plan, {})
        analysis.ledger.close()
        analysis.path.unlink()
        own = self.analysis()
        own.architecture(self.earlier, through="assessment")
        own.ledger.db.execute("PRAGMA user_version = 2")
        with self.assertRaisesRegex(ValueError, "unsupported alignment ledger version 2"):
            self.gate(plan, ledger)

    def test_library_and_installed_helpers_share_one_gate(self):
        self.assertEqual(Path(alignment.gate.__code__.co_filename).resolve(),
                         Path(records.gate.__code__.co_filename).resolve())
        self.assertEqual(alignment.gate.__code__.co_code, records.gate.__code__.co_code)
        self.assertTrue(issubclass(alignment.Ledger, alignment.Records))
        with self.assertRaises(records.AlignmentError):
            records.Records.open(state_directory(self.root) / "runs" / "missing.alignment.sqlite",
                                 logical_run="x", pr_url=PR_URL)
        self.assertFalse((state_directory(self.root) / "runs" / "missing.alignment.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
