"""The installed prose finalizer: dk-plain-english and dk-asd-ste100 through the shared task path.

Disposable repositories, the real run ledger and the controlled Git helper.
Native sessions are scripted edits to the checkout followed by the report the
engine would return; nothing starts a model, provider or engine. These results
do not certify any engine, provider or host.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, prose as library  # noqa: E402
from prfix import app, prose  # noqa: E402
from prfix.core import Ledger, read_artifact  # noqa: E402
from prepared_fixture import prepare  # noqa: E402
from test_publication import FakeGitHub  # noqa: E402

EDITOR = "agent_" + "4" * 32
OTHER = "agent_" + "5" * 32
GUIDE_BASE = "# Guide\n\nIntro paragraph that stays.\n\nBackground paragraph that stays.\n\nTail paragraph that stays.\n"
LONG = "In order to make use of the tool, it is necessary that `make setup` is run by the user before builds."
SHORT = "Before builds, the user runs `make setup` to use the tool."
SETUP = ("## Setup\n\n" + LONG + "\nSee [install](install.md) for details; it takes 5 minutes.\n\n"
         "```sh\nmake setup --fast\n```\n\n")
GUIDE_HEAD = GUIDE_BASE.replace("Tail paragraph", SETUP + "Tail paragraph")


def git(root, *args):
    result = subprocess.run(git_fixture.command(*args), cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=git_fixture.environment(), timeout=15, check=True)
    return result.stdout.decode().strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Fixture(unittest.TestCase):
    adapter = "plain-english"

    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cf prose finalizer ")
        self.addCleanup(temp.cleanup)
        self.root = root = Path(temp.name)
        git(root, "init", "--initial-branch=main")
        git(root, "config", "user.email", "fixture@example.invalid")
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "commit.gpgsign", "false")
        self.write("AGENTS.md", "Keep repository terminology.\n")
        self.write("docs/guide.md", GUIDE_BASE)
        self.write("code.py", "value = 1\n")
        git(root, "add", ".")
        git(root, "commit", "-m", "Initial")
        self.target = git(root, "rev-parse", "HEAD")
        git(root, "switch", "-c", "feature")
        self.write("docs/guide.md", GUIDE_HEAD)
        self.write("code.py", "value = 2\n")
        self.write("logo.bin", b"\x00\xff\xfe")
        (root / "notes.md").symlink_to("docs/guide.md")
        roles = {}
        for key in ("leaf", "reviewer", "verifier", "fixer", "audience"):
            roles[key] = "roles/" + key + ".yaml"
            self.write(roles[key], f'name: "{key}"\ndescription: "Fixture"\ninstructions: "Follow the assignment."\n')
        git(root, "add", ".")
        git(root, "commit", "-m", "PR change")
        self.head = git(root, "rev-parse", "HEAD")
        self.provider = FakeGitHub()
        self.provider.ev["metadata"].update(user={"id": 11, "login": "maintainer"}, updated_at="now")
        self.provider.ev["metadata"]["head"]["sha"] = self.head
        self.provider.ev["metadata"]["base"]["sha"] = self.target
        self.request = {"pr": "https://github.com/o/r/pull/7", "instructions": "Clean up the changed prose.",
                        "image": "ghcr.io/fixture/python-git-gh@sha256:" + "a" * 64, "roles": roles,
                        "options": {"tasks": [], "finalizers": [self.adapter], "reviewers": []},
                        "adapters": {self.adapter: copy.deepcopy(admission.ENTRY_ADAPTERS[self.adapter])}}
        self.replan()

    def replan(self, **changes):
        request = {**copy.deepcopy(self.request), **changes}
        self.plan = read_artifact(self.root, prepare(self.root, request, self.provider)["plan"])
        if getattr(self, "ledger", None) is None:
            self.ledger = Ledger(self.root, self.plan["logical_run"], self.plan["pr"]["url"])
            self.addCleanup(self.ledger.close)
        [self.task] = [task for task in self.plan["tasks"] if task["skill"] == self.adapter]

    def write(self, rel, body):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())

    def claim(self):
        return app.prepare_claim(self.root, self.provider, self.ledger, self.plan, {"task": self.task["id"]})

    def accept(self, claim, report, agent=EDITOR, round=0):
        return app.accept(self.root, self.provider, self.ledger, self.plan,
                          {"claim": claim["claim"], "agent": agent, "report": report, "round": round})

    def edit(self, rel, old, new):
        body = (self.root / rel).read_text()
        self.assertIn(old, body)
        self.write(rel, body.replace(old, new, 1))

    def report(self, claim, edits=(), prose_report=None, **values):
        """The native task report: evidence, checklist, changes manifest and deliverables.prose."""
        head = claim["task_input"]["prose"]["head"]
        changes, locations = [], []
        for rel in sorted({rel for rel, _ in edits}):
            before = git(self.root, "show", f"{head}:{rel}").encode() + b"\n"
            changes.append({"path": rel, "before": hashlib.sha256(before).hexdigest(), "after": sha(self.root / rel)})
        for rel, needle in edits:
            lines = (self.root / rel).read_text().splitlines()
            locations += [{"path": rel, "start": n, "end": n, "reason": "clearer"}
                          for n, line in enumerate(lines, 1) if needle in line]
        body = {"changed_locations": locations, "validation": ["Meaning and protected syntax checked."],
                "unresolved_ambiguities": [], "reference_verification_limits": []}
        if not edits:
            body["no_op"] = {"reason": "The changed prose is already clear.",
                             "sources": claim["task_input"]["prose"]["writes"]}
        if self.adapter == "asd-ste100":
            body["compliance"] = "not_claimed"
        if prose_report:
            body = prose_report(body)
        evidence = [{"id": "guide", "path": "docs/guide.md", "sha256": sha(self.root / "docs/guide.md")}]
        result = {"complete": True, "findings": [], "evidence": evidence, "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["guide"]}
                                for item in self.task["checklist"]],
                  "dispositions": [], "changes": changes, "operations": [], "audience": None,
                  "commit_message": "Clarify the setup prose" if edits else None,
                  "deliverables": json.dumps({"prose": body})}
        result.update(values)
        return result

    def fresh(self):
        """A new logical run over the original PR head, with an empty state namespace."""
        git(self.root, "reset", "-q", "--hard", self.head)
        self.ledger.close()
        self.ledger = None
        git_fixture.state(self)
        self.replan()
        return self.claim()

    def pushed(self):
        """The PR head now includes the local commits (as after an authorized push)."""
        self.provider.ev["metadata"]["head"]["sha"] = git(self.root, "rev-parse", "HEAD")

    def commits(self):
        return int(git(self.root, "rev-list", "--count", "HEAD"))

    def clean(self):
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=all"), "")


class Candidates(Fixture):
    def test_assignment_bounds_the_session_to_changed_prose(self):
        claim = self.claim()
        self.assertTrue(claim["invoke"])
        record = claim["task_input"]["prose"]
        self.assertEqual(record["units"]["docs/guide.md"]["mode"], "full")
        self.assertEqual(record["units"]["code.py"]["mode"], "unchanged")
        self.assertEqual({item["path"]: item["reason"] for item in record["skipped"]},
                         {"logo.bin": "binary or not UTF-8", "notes.md": "not a regular file"})
        self.assertEqual(record["writes"], ["docs/guide.md"])
        self.assertEqual((record["skills"], record["commit"], record["publication"]), ([], False, False))
        self.assertEqual(record["role_prompt"], library.ENTRIES["dk-plain-english"]["role_prompt"])
        guide = GUIDE_HEAD.splitlines()
        changed = {guide[line] for line in record["units"]["docs/guide.md"]["changed"]}
        self.assertIn(LONG, changed)
        self.assertNotIn("make setup --fast", changed)  # Code blocks are never candidates.
        self.assertNotIn("Intro paragraph that stays.", changed)
        contract = claim["task_input"]["prose_contract"]
        self.assertIn('only member "prose"', contract)
        self.assertNotIn("{", contract.replace("{changed", "").replace("{path", "").replace("{reason", ""))
        self.assertNotIn("compliance", contract)
        self.assertEqual(claim["definition"], "leaf")


class Direct(Fixture):
    def test_edits_within_changed_prose_commit_through_the_controlled_helper(self):
        claim = self.claim()
        self.edit("docs/guide.md", LONG, SHORT)
        commits = self.commits()
        accepted = self.accept(claim, self.report(claim, [("docs/guide.md", SHORT)]))
        self.assertTrue(accepted["complete"], accepted)
        self.assertEqual(self.commits(), commits + 1)
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "Clarify the setup prose")
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD"), "docs/guide.md")
        self.assertIn(SHORT, (self.root / "docs/guide.md").read_text())
        self.assertIn("make setup --fast", (self.root / "docs/guide.md").read_text())
        self.clean()
        self.assertEqual(self.ledger.db.execute("SELECT state FROM local_commits").fetchone()[0], "completed")
        # A replayed acceptance (lost response) settles from the retained commit; no second commit.
        again = self.accept(claim, self.report(claim, [("docs/guide.md", SHORT)]))
        self.assertEqual((again["complete"], again["report"]), (True, accepted["report"]))
        self.assertEqual(self.commits(), commits + 1)
        coverage = app.coverage(self.root, self.provider, self.ledger, self.plan, {})
        self.assertNotIn(self.task["id"], coverage["missing"])
        # A rerun over the same head reuses the accepted pass without a session.
        self.assertFalse(self.claim()["invoke"])

    def test_sourced_no_op_commits_nothing(self):
        claim = self.claim()
        commits = self.commits()
        accepted = self.accept(claim, self.report(claim))
        self.assertTrue(accepted["complete"], accepted)
        self.assertEqual(self.commits(), commits)
        missing = self.accept(self.claim_again_after_change(), self.report(claim, prose_report=lambda body: {
            key: value for key, value in body.items() if key != "no_op"}))
        self.assertFalse(missing["complete"])
        self.assertIn("sourced no-op", missing["feedback"]["contract_error"])

    def claim_again_after_change(self):
        self.edit("docs/guide.md", "it takes 5 minutes.", "it takes 5 minutes to finish.")
        git(self.root, "commit", "-qam", "Later source change")
        self.pushed()
        self.replan()
        claim = self.claim()
        self.assertTrue(claim["invoke"])
        return claim

    def test_out_of_bounds_edits_and_mismatched_reports_get_a_bounded_correction(self):
        cases = [
            ("unchanged prose", lambda: self.edit("docs/guide.md", "Intro paragraph that", "The intro paragraph"),
             [("docs/guide.md", "The intro")], None, "outside the changed prose"),
            ("code block", lambda: self.edit("docs/guide.md", "make setup --fast", "make setup"),
             [("docs/guide.md", "make setup")], None, "outside the changed prose"),
            ("link target", lambda: self.edit("docs/guide.md", "[install](install.md)", "[install](setup.md)"),
             [("docs/guide.md", "setup.md")], None, "changed: install.md, setup.md"),
            ("number", lambda: self.edit("docs/guide.md", "5 minutes", "five minutes"),
             [("docs/guide.md", "five minutes")], None, "changed: 5"),
            ("unlisted file", lambda: self.edit("code.py", "value = 2", "value = 3"),
             [], None, "outside its prose candidates"),
            ("unreported edit", lambda: self.edit("docs/guide.md", LONG, SHORT),
             [], None, "every edited file must be reported"),
        ]
        for label, mutate, edits, prose_report, message in cases:
            with self.subTest(label):
                claim = self.fresh()
                mutate()
                report = self.report(claim, edits, prose_report)
                if label == "unreported edit":
                    report["deliverables"] = json.dumps({"prose": {
                        "changed_locations": [], "validation": ["Checked."], "unresolved_ambiguities": [],
                        "reference_verification_limits": [], "no_op": {"reason": "None.", "sources": []}}})
                commits = self.commits()
                result = self.accept(claim, report)
                self.assertFalse(result["complete"])
                self.assertIn("prose cleanup", result["feedback"]["contract_error"])
                self.assertIn(message, result["feedback"]["contract_error"])
                self.assertEqual(self.commits(), commits)

    def test_quoted_user_wording_is_kept_verbatim(self):
        self.replan(instructions='Clean up the prose but keep "it is necessary that" as written.')
        claim = self.claim()
        self.assertEqual(claim["task_input"]["prose"]["wording"], ["it is necessary that"])
        self.edit("docs/guide.md", LONG, SHORT)
        result = self.accept(claim, self.report(claim, [("docs/guide.md", SHORT)]))
        self.assertFalse(result["complete"])
        self.assertIn("exact wording", result["feedback"]["contract_error"])

    def test_no_changed_prose_completes_without_a_session(self):
        git(self.root, "checkout", "-q", self.target, "--", "docs/guide.md")
        git(self.root, "commit", "-qm", "Drop the prose change")
        self.pushed()
        self.replan()
        claim = self.claim()
        self.assertEqual((claim["invoke"], claim["complete"]), (False, True))
        wrapper = read_artifact(self.root, claim["report"])
        self.assertEqual((wrapper["report"], wrapper["prose"]["status"]), (None, "reused"))
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        coverage = app.coverage(self.root, self.provider, self.ledger, self.plan, {})
        self.assertNotIn(self.task["id"], coverage["missing"])
        self.assertFalse(self.claim()["invoke"])

    def test_session_free_coverage_survives_a_new_head_with_the_same_tree(self):
        git(self.root, "checkout", "-q", self.target, "--", "docs/guide.md")
        git(self.root, "commit", "-qm", "Drop the prose change")
        self.pushed()
        self.replan()
        settled = self.claim()
        self.assertFalse(settled["invoke"])
        # An empty commit moves the head but not the tree; the settled record still holds.
        git(self.root, "commit", "-q", "--allow-empty", "-m", "Empty")
        self.pushed()
        self.replan()
        self.assertEqual(self.claim()["invoke"], False)
        coverage = app.coverage(self.root, self.provider, self.ledger, self.plan, {})
        self.assertNotIn(self.task["id"], coverage["missing"])


class Refresh(Fixture):
    def test_a_later_source_change_refreshes_only_its_lines_as_a_delta(self):
        claim = self.claim()
        self.edit("docs/guide.md", LONG, SHORT)
        self.assertTrue(self.accept(claim, self.report(claim, [("docs/guide.md", SHORT)]))["complete"])
        # A later review fix adds a sentence; only that change is a candidate.
        self.edit("docs/guide.md", "Tail paragraph that stays.", "Tail paragraph that stays.\nA newly added note here.")
        git(self.root, "commit", "-qam", "Review fix")
        self.pushed()
        self.replan()
        claim = self.claim()
        unit = claim["task_input"]["prose"]["units"]["docs/guide.md"]
        self.assertEqual(unit["mode"], "delta")
        lines = (self.root / "docs/guide.md").read_text().splitlines()
        self.assertEqual([lines[n] for n in unit["changed"]], ["A newly added note here."])
        self.assertNotIn("-" + LONG, unit["patch"])

    def test_changed_instructions_refresh_the_whole_pass(self):
        claim = self.claim()
        self.edit("docs/guide.md", LONG, SHORT)
        self.assertTrue(self.accept(claim, self.report(claim, [("docs/guide.md", SHORT)]))["complete"])
        self.pushed()
        self.replan(instructions="Clean up the changed prose for new readers.")
        claim = self.claim()
        self.assertEqual(claim["task_input"]["prose"]["units"]["docs/guide.md"]["mode"], "full")


class SimplifiedTechnicalEnglish(Fixture):
    adapter = "asd-ste100"

    def test_limits_are_explicit_and_compliance_is_never_claimed(self):
        claim = self.claim()
        record = claim["task_input"]["prose"]
        self.assertEqual(record["compliance"], "not_claimed")
        self.assertIn('compliance: "not_claimed"', claim["task_input"]["prose_contract"])
        self.assertTrue(any("rule and dictionary checks were not performed" in item
                            for item in record["reference_limits"]))
        cases = [
            ("claim", lambda body: {**body, "validation": ["The text is now STE compliant."]}, None, "compliance"),
            ("negation elsewhere", lambda body: {**body, "validation": ["Not reviewed; it is certified."]}, None,
             "compliance"),
            ("compliance field", lambda body: {**body, "compliance": "claimed"}, None, "never claimed"),
            ("rule number", lambda body: {**body, "validation": ["Applied rule 1.2."]}, None, "rule number"),
            ("unsupplied reference", lambda body: {**body, "reference_checks": [{"reference": "spec", "subject": "x"}]},
             None, "not supplied"),
            ("commit message", None, {"commit_message": "Make the guide ASD-STE100 conformant"}, "compliance"),
            ("finding text", None, {"findings": [{"id": "F1", "text": "The guide is certified STE text.",
                                                  "sources": ["guide"], "location": None}]}, "compliance"),
            ("other deliverable", None, {"summary": "Rule 2.1 applied."}, "only its prose report"),
            ("disclaimed finding", None, {"findings": [{"id": "F1", "text": "Compliance is not claimed.",
                                                        "sources": ["guide"], "location": None}]}, None),
        ]
        for label, change, values, message in cases:
            with self.subTest(label):
                claim = self.fresh()
                self.edit("docs/guide.md", LONG, SHORT)
                values = dict(values or {})
                extra = {"summary": values.pop("summary")} if "summary" in values else {}
                report = self.report(claim, [("docs/guide.md", SHORT)], change, **values)
                report["deliverables"] = json.dumps({**json.loads(report["deliverables"]), **extra})
                result = self.accept(claim, report)
                if message is None:
                    self.assertTrue(result["complete"], result)
                    continue
                self.assertFalse(result["complete"])
                self.assertIn(message, result["feedback"]["contract_error"])
        claim = self.fresh()
        self.edit("docs/guide.md", LONG, SHORT)
        disclaimed = self.report(claim, [("docs/guide.md", SHORT)], lambda body: {
            **body, "reference_verification_limits": ["Full ASD-STE100 compliance is not claimed."]})
        self.assertTrue(self.accept(claim, disclaimed)["complete"])


class Composed(Fixture):
    def test_composed_prose_finalizers_run_in_order_on_each_others_output(self):
        adapters = {name: copy.deepcopy(admission.ENTRY_ADAPTERS[name]) for name in admission.PROSE_ADAPTERS}
        self.request["options"]["finalizers"] = ["plain-english", "asd-ste100"]
        self.request["adapters"] = adapters
        self.plan = read_artifact(self.root, prepare(self.root, self.request, self.provider)["plan"])
        self.ledger = Ledger(self.root, self.plan["logical_run"], self.plan["pr"]["url"])
        self.addCleanup(self.ledger.close)
        plain, ste = [task for task in self.plan["tasks"] if task["skill"] in admission.PROSE_ADAPTERS]
        self.assertEqual((plain["skill"], ste["skill"]), ("plain-english", "asd-ste100"))
        self.task, self.adapter = plain, "plain-english"
        first = self.claim()
        self.edit("docs/guide.md", LONG, SHORT)
        self.assertTrue(self.accept(first, self.report(first, [("docs/guide.md", SHORT)]))["complete"])
        output = git(self.root, "rev-parse", "HEAD")
        self.task, self.adapter = ste, "asd-ste100"
        second = self.claim()
        record = second["task_input"]["prose"]
        self.assertEqual((record["head"], record["units"]["docs/guide.md"]["mode"]), (output, "full"))
        self.assertIn("+" + SHORT, record["units"]["docs/guide.md"]["patch"])
        self.edit("docs/guide.md", "to use the tool.", "so that the tool works.")
        accepted = self.accept(second, self.report(second, [("docs/guide.md", "so that the tool works.")]),
                               agent=OTHER)
        self.assertTrue(accepted["complete"], accepted)
        self.assertEqual(git(self.root, "log", "-3", "--format=%s").splitlines(),
                         ["Clarify the setup prose", "Clarify the setup prose", "PR change"])
        # The sibling's commit changed the tree the earlier occurrence covered, so the next cycle reruns it.
        coverage = app.coverage(self.root, self.provider, self.ledger, self.plan, {})
        self.assertEqual([key for key in coverage["missing"] if key in (plain["id"], ste["id"])], [plain["id"]])
        # Each occurrence keeps its own units: the earlier one now sees only its sibling's later edit.
        self.task, self.adapter = plain, "plain-english"
        rerun = self.claim()
        unit = rerun["task_input"]["prose"]["units"]["docs/guide.md"]
        self.assertEqual(unit["mode"], "delta")
        self.assertIn("+Before builds, the user runs `make setup` so that the tool works.", unit["patch"])
        self.assertNotIn("+" + SHORT + "\n", unit["patch"])
        self.assertTrue(self.accept(rerun, self.report(rerun))["complete"])  # A sourced no-op settles the cycle.
        coverage = app.coverage(self.root, self.provider, self.ledger, self.plan, {})
        self.assertFalse({plain["id"], ste["id"]} & set(coverage["missing"]))


class Shared(unittest.TestCase):
    NAMES = ("ADJACENT", "MAX_FILE", "MAX_ITEMS", "OFFICIAL", "ENTRIES", "BY_ADAPTER", "FENCE", "CODE_SPAN", "URL",
             "LINK", "IDENTIFIER", "PLAIN", "NUMBER", "KEYWORD", "DIRECTIVE", "QUOTED", "CLAIM", "_SUBJECT",
             "DISCLAIMER", "DOCUMENTATION", "COMMENTS", "UNLEXED", "REGEX_LANGUAGES", "REGEX_KEYWORDS", "OPERATORS",
             "RULE_NUMBER", "_lines", "_ranges", "_spans", "_python_skeleton", "_tokens", "_python_prose",
             "_comment_prose", "prose_lines", "check_edits", "constraints", "_texts", "check_report")

    @staticmethod
    def definitions(path):
        source = Path(path).read_text()
        result = {}
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                result[node.name] = ast.get_source_segment(source, node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                result[node.targets[0].id] = ast.get_source_segment(source, node)
        return result

    @staticmethod
    def ste_report(**changes):
        report = {"complete": True, "changed_locations": [{"path": "docs/compliance.md", "start": 1, "end": 1,
                                                           "reason": "Shorter sentence."}],
                  "validation": ["Read the result."], "unresolved_ambiguities": [],
                  "reference_verification_limits": ["No reference was supplied."], "compliance": "not_claimed",
                  "commit_message": "Shorten a sentence"}
        report.update(changes)
        return report

    def test_report_paths_are_names_not_compliance_claims(self):
        actual = {"docs/compliance.md": [(1, 1)]}
        units = {"docs/compliance.md": {}, "R1.2.md": {}}
        self.assertTrue(prose.check_report("dk-asd-ste100", self.ste_report(), units, [], actual)["complete"])
        ambiguity = [{"path": "R1.2.md", "line": 1, "text": "Unclear."}]
        prose.check_report("dk-asd-ste100", self.ste_report(unresolved_ambiguities=ambiguity), units, [], actual)
        claimed = self.ste_report(validation=["The text is compliant."])
        with self.assertRaisesRegex(prose.Blocked, "compliance"):
            prose.check_report("dk-asd-ste100", claimed, units, [], actual)

    def test_untyped_report_identities_are_rejected(self):
        actual, units = {"docs/a.md": [(1, 1)]}, {"docs/a.md": {}}
        location = {"path": "docs/a.md", "start": 1, "end": 1}
        cases = {
            "report.changed_locations[0].path": self.ste_report(changed_locations=[{**location, "path": []}]),
            "report.unresolved_ambiguities[0]": self.ste_report(
                changed_locations=[location], unresolved_ambiguities=[{"path": {}, "line": 1, "text": "Unclear."}]),
            "report.reference_checks[0].reference": self.ste_report(
                changed_locations=[location], reference_checks=[{"reference": ["r"], "subject": "Wording."}]),
        }
        for path, report in cases.items():
            with self.subTest(path), self.assertRaises(prose.Blocked) as raised:
                prose.check_report("dk-asd-ste100", report, units, ["r"], actual)
            self.assertIn(path, str(raised.exception))
        no_op = {"complete": True, "changed_locations": [], "validation": ["Read it."], "unresolved_ambiguities": [],
                 "reference_verification_limits": [], "no_op": {"reason": "Already plain.", "sources": [["docs/a.md"]]}}
        with self.assertRaisesRegex(prose.Blocked, "report.no_op.sources"):
            prose.check_report("dk-plain-english", no_op, units, [], {"docs/a.md": []})

    def test_line_numbers_count_newlines_only(self):
        for text, comment in (("x = 1\n\f\n# A comment.\ny = 2\n", 2), ("s = 'a\u2028b'\n# A comment.\ny = 2\n", 1)):
            with self.subTest(text=text):
                lines = prose._lines(text.encode())
                self.assertEqual(len(lines), text.count("\n"))
                self.assertEqual(prose.prose_lines("a.py", lines), {comment})
                self.assertEqual(prose.prose_lines("a.sh", lines), {comment})

    def test_installed_checks_are_the_library_checks_verbatim(self):
        installed = self.definitions(prose.__file__)
        original = self.definitions(library.__file__)
        for name in self.NAMES:
            with self.subTest(name):
                self.assertEqual(installed[name], original[name])
        self.assertEqual(set(prose.BY_ADAPTER), set(admission.PROSE_ADAPTERS))


if __name__ == "__main__":
    unittest.main()
