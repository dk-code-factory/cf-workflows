"""Feedback read, address and reply entries over the shared workflow (#30).

Disposable Git repositories, a fresh state namespace and fail-closed fakes: the
engine transport answers only the capability query, and the GitHub transports
answer only capture (and, for replies, the modeled publication routes); any
other operation raises with no live fallback. A paginated transport drives the
real gh-backed capture through fake REST pages and GraphQL thread pages. Native
sessions are only the reports a test hands the installed helpers. Counts of
native dispatches, captures, pages and writes are asserted. Nothing here starts
an engine, provider or host or contacts GitHub, so none of it certifies session
adherence or GitHub conformance.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import yaml

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
import git_fixture
from prfix import app, capture, feedback, publication, responses
from prfix.core import Ledger, read_artifact
from prfix.prepare import CHECKLIST, prepare
from prfix.publication import PublicationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import admission, dk_pr_feedback  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import FakeTransport, PreflightFixture  # noqa: E402
import test_dk_pr_fix_responses as replies  # noqa: E402

PR_URL = "https://github.com/owner/repo/pull/7"
AGENTS = ["agent_" + str(n) * 32 for n in range(1, 10)]
INVENTORY = json.loads((ROOT / "baselines" / "inventory.json").read_text())
DEPLOYMENT = {"execution": {"mode": "container", "image": "ghcr.io/fixture/helper@sha256:" + "a" * 64},
              "agents": {"codex": {"provider": "fixture-provider", "sandbox": "workspace-write"}}}
OWNER = {"id": 1, "login": "owner", "type": "User"}
REVIEWER = {"id": 2, "login": "reviewer", "type": "User"}
BOT = {"id": 4, "login": "lint[bot]", "type": "Bot"}
GRANT = {"request_id": "reply-1", "instruction": "Answer the review feedback.", "source": "chat:1"}


def workflow(path):
    return yaml.safe_load((fix_preflight.INSTALL / path).read_text())


def entry_controls(name, inputs):
    """What the installed entry workflow hands main.yml: its fixed selection and main.yml's adapters."""
    entry = workflow(dk_pr_feedback.ENTRIES[name]["entry"])
    adapters = workflow("workflows/main.yml")["on"]["workflow_call"]["inputs"]["adapters"]["default"]
    fixed = {key: value for key, value in entry["jobs"]["feedback"]["with"].items()
             if not (isinstance(value, str) and value.startswith("${{"))}
    return {**{key: value for key, value in inputs.items() if key != "request"}, **fixed, "adapters": adapters}


def git(root, *args):
    return subprocess.run(git_fixture.command(*args), cwd=root, capture_output=True, check=True,
                          env=git_fixture.environment(), timeout=30).stdout.decode().strip()


def at(minute):
    return f"2026-11-25T10:{minute:02d}:00Z"


def thread(ident, root):
    return {"id": ident, "isResolved": False, "isOutdated": False,
            "comments": {"nodes": [{"id": "N" + str(root), "databaseId": root}]}}


def discussion():
    """Mixed feedback: an inline request, a bot style suggestion, a participant question,
    a review body and an inline question the PR author already answered in its thread."""
    return {
        "comments": [
            {"id": 40, "user": BOT, "created_at": at(2), "html_url": PR_URL + "#issuecomment-40",
             "body": "Consider replacing the magic number 2 with a named constant."},
            {"id": 41, "user": REVIEWER, "created_at": at(3), "html_url": PR_URL + "#issuecomment-41",
             "body": "@bob can you confirm the release date?"}],
        "reviews": [{"id": 50, "user": REVIEWER, "submitted_at": at(4), "state": "COMMENTED",
                     "html_url": PR_URL + "#pullrequestreview-50",
                     "body": "Please move the migration into a follow-up PR."}],
        "inline": [
            {"id": 30, "user": REVIEWER, "created_at": at(1), "html_url": PR_URL + "#discussion_r30",
             "in_reply_to_id": None, "body": "Please rename `value` to `count`."},
            {"id": 60, "user": REVIEWER, "created_at": at(5), "html_url": PR_URL + "#discussion_r60",
             "in_reply_to_id": None, "body": "Why is this a module constant?"},
            {"id": 61, "user": OWNER, "created_at": at(6), "html_url": PR_URL + "#discussion_r61",
             "in_reply_to_id": 60, "body": "It is shared by both callers."}],
        "threads": [thread("T30", 30), thread("T60", 60)]}


def request(ident, excerpt, implementation="none", addressee="author", response=None, **extra):
    if implementation == "fixed":
        extra.setdefault("paths", ["code.py"])
    return {"id": ident, "excerpt": excerpt, "addressee": addressee, "implementation": implementation,
            "response": response or {"disposition": "reply"}, **extra}


def assessment(view, key, *requests):
    return {"source": key, "fingerprint": feedback.View.fingerprint(view.sources[key]), "requests": list(requests)}


def assessments(view, rename="none", constant="none", migration="none"):
    return [
        assessment(view, "inline:30", request("rename", "rename `value` to `count`", rename)),
        # dk-workflows#244: a declined bot style suggestion keeps its author's obligation.
        assessment(view, "comment:40", request("constant", "named constant", constant, category="style")),
        assessment(view, "comment:41", request("date", "confirm the release date", addressee="participant",
                                               participant="bob", response={
                                                   "disposition": "no_reply", "reason": "addressed_to_participant",
                                                   "justification": "Asked of bob, not the PR author."})),
        assessment(view, "review:50", request("migration", "move the migration into a follow-up PR", migration)),
        assessment(view, "inline:60", request("why", "Why is this a module constant?", response={
            "disposition": "already_answered", "reply": {"kind": "inline", "id": 61},
            "justification": "The PR author answered in the thread."})),
    ]


class Transport:
    """Fake GitHub with a mutable discussion: capture only; everything else fails closed."""

    def __init__(self, head, target):
        self.calls = Counter()
        self.last_capture = None
        self.read_only = False
        self.evidence = {
            "metadata": {"html_url": PR_URL, "number": 7, "state": "open", "merged": False, "draft": False,
                         "title": "Fix", "body": "Fix a problem", "updated_at": "now", "user": OWNER,
                         "head": {"sha": head, "ref": "fix", "repo": {"full_name": "owner/repo"}},
                         "base": {"sha": target, "ref": "main", "repo": {"full_name": "owner/repo"}}},
            "viewer": {"id": 1, "login": "owner"}, **discussion()}

    def capture(self, pr):
        self.calls["capture"] += 1
        if pr["url"] != PR_URL:
            raise AssertionError("capture for an unexpected PR")
        self.last_capture = copy.deepcopy(self.evidence)
        return copy.deepcopy(self.last_capture)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def unconfigured(*args, **kwargs):
            self.calls[name] += 1
            raise AssertionError("unconfigured fixture operation: " + name)
        return unconfigured


class PagedGitHub(capture.GitHub):
    """The real gh-backed capture over fake REST pages and GraphQL thread pages.

    Only the routes capture reads are modeled; any other route raises. Each call is
    counted by method and route.
    """

    def __init__(self, root, evidence):
        super().__init__(root)
        self.evidence = evidence
        self.calls = Counter()
        self.thread_total = None
        self.drift = False
        self.duplicate = False

    def request(self, method, path, payload=None):
        parts = urlsplit(path)
        route, query = parts.path, dict(parse_qsl(parts.query))
        self.calls[(method, route)] += 1
        base = "repos/owner/repo"
        rows = {base + "/issues/7/comments": "comments", base + "/pulls/7/reviews": "reviews",
                base + "/pulls/7/comments": "inline"}
        if method == "GET" and route == base + "/pulls/7":
            metadata = copy.deepcopy(self.evidence["metadata"])
            if self.drift and self.calls[(method, route)] % 2 == 0:
                metadata["updated_at"] = "later"
            return metadata
        if method == "GET" and route == "user":
            return copy.deepcopy(self.evidence["viewer"])
        if method == "GET" and route in rows:
            page = int(query["page"])
            items = self.evidence[rows[route]]
            batch = items[(page - 1) * 100:page * 100]
            if self.duplicate and page == 2 and batch:
                batch = [items[0], *batch[1:]]
            return copy.deepcopy(batch)
        if method == "POST" and route == "graphql" and payload["query"].startswith("query"):
            threads = self.evidence["threads"]
            start = 0 if payload["variables"]["cursor"] is None else int(payload["variables"]["cursor"])
            nodes = threads[start:start + 100]
            more = start + 100 < len(threads)
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "totalCount": self.thread_total or len(threads), "nodes": copy.deepcopy(nodes),
                "pageInfo": {"hasNextPage": more, "endCursor": str(start + 100) if more else None}}}}}}
        raise AssertionError(f"unconfigured fixture route: {method} {route}")


def paged_discussion(head, target):
    """250 feedback sources over two REST pages each and two GraphQL thread pages."""
    comments = [{"id": 1000 + n, "user": BOT if n % 3 == 0 else REVIEWER, "created_at": at(n % 50),
                 "html_url": f"{PR_URL}#issuecomment-{1000 + n}", "body": f"Please check item {n}."}
                for n in range(150)]
    inline = [{"id": 2000 + n, "user": REVIEWER, "created_at": at(n % 50), "in_reply_to_id": None,
               "html_url": f"{PR_URL}#discussion_r{2000 + n}", "body": f"Rename local {n}."} for n in range(101)]
    reviews = [{"id": 3000, "user": REVIEWER, "submitted_at": at(55), "state": "COMMENTED",
                "html_url": PR_URL + "#pullrequestreview-3000", "body": "Overall, add a changelog entry."}]
    return {"metadata": {"html_url": PR_URL, "number": 7, "state": "open", "merged": False, "draft": False,
                         "title": "Fix", "body": "Fix a problem", "updated_at": "now", "user": OWNER,
                         "head": {"sha": head, "ref": "fix", "repo": {"full_name": "owner/repo", "id": 9}},
                         "base": {"sha": target, "ref": "main", "repo": {"full_name": "owner/repo", "id": 9}}},
            "viewer": {"id": 1, "login": "owner"}, "comments": comments, "reviews": reviews, "inline": inline,
            "threads": [thread(f"T{item['id']}", item["id"]) for item in inline]}


class EntryPreflight(PreflightFixture):
    def entry(self, name, **changes):
        self.transport = changes.pop("transport", None) or FakeTransport()
        arguments = {"request": self.request, "deployment": self.deployment, "repository": self.repo,
                     "home": self.home, **changes}
        result = dk_pr_feedback.preflight(name, transport=self.transport, install=self.clean,
                                          **copy.deepcopy(arguments))
        self.assertLessEqual(set(self.transport.calls), {"capabilities"})
        self.assertFalse(self.home.exists(), "preflight writes nothing")
        return result

    def rejected(self, result, path, fragment):
        self.assertEqual(result["preflight"], "rejected", result)
        self.assertEqual(result["error"]["path"], path, result)
        self.assertIn(fragment, result["error"]["message"])

    def test_presets_select_bundled_adapters_on_the_shared_logical_run(self):
        fix = self.preflight()
        expected = {"dk-pr-feedback-read": {"read-feedback"},
                    "dk-pr-feedback-address": {"read-feedback", "reconcile-decisions", "address-feedback"},
                    "dk-pr-feedback-reply": {"reply-feedback"}}
        for name, adapters in expected.items():
            with self.subTest(name=name):
                result = self.entry(name)
                self.assertEqual(result["preflight"], "admitted", result)
                self.assertEqual(self.transport.calls, Counter(capabilities=1))
                inputs = result["inputs"]
                # The entry workflow fixes the selection and adapters; preflight hands it only its inputs.
                self.assertEqual(set(inputs), set(dk_pr_feedback.ENTRIES[name]["inputs"]))
                self.assertEqual({key: result["snapshot"]["options"][key]["value"]
                                  for key in ("tasks", "finalizers", "reviewers")}, dk_pr_feedback.preset(name))
                self.assertEqual(result["entry"]["adapters"], sorted(adapters))
                for adapter in adapters:
                    self.assertEqual(result["snapshot"]["adapters"][adapter]["assignment"],
                                     admission.BASELINE_ADAPTERS[adapter]["assignment"])
                self.assertFalse(any(inputs["permissions"].values()))
                self.assertEqual((result["workflow"], result["installation"]["entry"]),
                                 (dk_pr_feedback.ENTRIES[name]["skill"], dk_pr_feedback.ENTRIES[name]["entry"]))
                # Standalone and composed runs share one logical run, feedback record and journal.
                self.assertEqual(result["state"]["request_document"]["logical_run"],
                                 fix["state"]["request_document"]["logical_run"])
                self.assertEqual(result["state"]["namespace"], fix["state"]["namespace"])
                snapshot = result["snapshot"]
                self.assertFalse(any(snapshot["custom"].values()))
                self.assertTrue(all(task["skill"] is None and task["definition"] == "leaf"
                                    for task in snapshot["plan"]["tasks"]))
                self.assertEqual(result["entry"]["adapters"], sorted(adapters))

    def test_role_prompts_and_reply_prompt_match_the_baseline(self):
        digests = {resource["path"]: resource["sha256"] for row in INVENTORY["skills"]
                   for resource in row["baseline_resources"]}
        for name, entry in dk_pr_feedback.ENTRIES.items():
            digest = hashlib.sha256(entry["role_prompt"].encode()).hexdigest()
            self.assertEqual(digest, digests[entry["baseline_prompt"]], name)
            self.assertEqual(digest, digests[entry["baseline_prompt"].replace("claude/", "codex/", 1)], name)
        # The default reply prompt lives in the shared responder, so composed runs apply it too.
        assignment = admission.BASELINE_ADAPTERS["reply-feedback"]["assignment"]
        self.assertTrue(assignment.endswith("Reply prompt: " + admission.REPLY_PROMPT))
        main = (fix_preflight.INSTALL / "workflows" / "main.yml").read_text()
        self.assertIn("assignment: " + json.dumps(assignment), main)
        composed = self.preflight()["inputs"]["adapters"]["reply-feedback"]["assignment"]
        self.assertEqual(composed, self.entry("dk-pr-feedback-reply")["snapshot"]["adapters"]["reply-feedback"]
                         ["assignment"])

    def test_the_preset_is_fixed_and_configuration_cannot_replace_it(self):
        for invocation in ({"options": {"tasks": ["describe-pr"]}}, {"prompts": []}, {"skills": []},
                           {"preferences": []}):
            with self.subTest(invocation=invocation):
                self.rejected(self.entry("dk-pr-feedback-read", invocation=invocation), "invocation", "preset")
                self.assertEqual(self.transport.calls, Counter())
        scope = {"id": "repo", "selector": {"kind": "repository", "id": "github.com/owner/repo"},
                 "options": {"tasks": ["describe-pr"], "finalizers": ["inspect-commits"], "reviewers": ["simplify"]},
                 "permissions": {}, "prompts": [], "skills": [], "preferences": []}
        configuration = {"schema_version": 1, "kind": "configuration", "suite": "cf-workflows", "revision": "rev-1",
                         "scopes": [scope], "resources": [],
                         "admission": {"structural": "pending", "semantic": None, "request_id": None,
                                       "context_digest": None, "resource_digest": None, "capability_digest": None,
                                       "evidence_refs": []}}
        self.assertEqual(self.preflight(configuration=configuration)["inputs"]["tasks"], ["describe-pr"])
        result = self.entry("dk-pr-feedback-read", configuration=configuration)
        self.assertEqual(result["preflight"], "admitted", result)
        self.assertEqual(tuple(result["snapshot"]["options"][key]["value"] for key in ("tasks", "finalizers",
                                                                                      "reviewers")),
                         (["read-feedback"], [], []))
        self.assertNotIn("tasks", result["inputs"])
        # A configured replacement of the bundled adapter is custom input the installed workflow cannot run.
        scope["options"] = {"adapters": {"read-feedback": {"kind": "inspection", "assignment": "Skim it."}}}
        self.rejected(self.entry("dk-pr-feedback-read", configuration=configuration), "custom", "not supported")

    def test_each_entry_accepts_only_its_own_remote_writes(self):
        for name in ("dk-pr-feedback-read", "dk-pr-feedback-address"):
            for action in ("push", "reply", "resolve_thread"):
                with self.subTest(name=name, action=action):
                    request = {**self.request, "authorizations": {action: GRANT}}
                    self.rejected(self.entry(name, request=request), "request.authorizations." + action,
                                  "no remote writes")
                    self.rejected(self.entry(name, invocation={"permissions": {action: True}}),
                                  "invocation.permissions." + action, "not an action")
                    self.assertEqual(self.transport.calls, Counter())
        request = {**self.request, "authorizations": {"push": GRANT}}
        self.rejected(self.entry("dk-pr-feedback-reply", request=request), "request.authorizations.push",
                      "only ['reply', 'resolve_thread']")
        request = {**self.request, "authorizations": {"reply": GRANT, "resolve_thread": GRANT}}
        # An authorization without an enabled permission is rejected by the shared preflight.
        self.rejected(self.entry("dk-pr-feedback-reply", request=request), "request.authorizations.reply",
                      "not enabled")
        result = self.entry("dk-pr-feedback-reply", request=request,
                            invocation={"permissions": {"reply": True, "resolve_thread": True}})
        self.assertEqual(result["preflight"], "admitted", result)
        self.assertEqual({key for key, value in result["inputs"]["permissions"].items() if value},
                         {"reply_to_feedback", "resolve_threads"})
        self.assertEqual(set(result["state"]["request_document"]["authorizations"]), {"reply", "resolve_thread"})
        self.rejected(self.entry("dk-pr-feedback-reply", request=request, options={"resolve_answered_threads": False},
                                 invocation={"permissions": {"reply": True, "resolve_thread": True}}),
                      "request.authorizations.resolve_thread", "resolve_answered_threads is false")
        only_reply = {**self.request, "authorizations": {"reply": GRANT}}
        result = self.entry("dk-pr-feedback-reply", request=only_reply, options={"resolve_answered_threads": False},
                            invocation={"permissions": {"reply": True}})
        self.assertEqual({key for key, value in result["inputs"]["permissions"].items() if value},
                         {"reply_to_feedback"})

    def test_entry_workflows_fix_the_preset_and_declare_only_preflight_inputs(self):
        descriptor = (fix_preflight.INSTALL / "plugin.yaml").read_text()
        main = workflow("workflows/main.yml")["on"]["workflow_call"]["inputs"]
        for name, entry in dk_pr_feedback.ENTRIES.items():
            with self.subTest(name=name):
                definition = workflow(entry["entry"])
                self.assertEqual(definition["name"], entry["skill"])
                declared = definition["on"]["workflow_call"]["inputs"]
                self.assertEqual(set(declared), set(entry["inputs"]))
                self.assertEqual(set(self.entry(name)["inputs"]), set(entry["inputs"]))
                self.assertTrue(declared["request"]["required"])
                for key, value in declared.items():
                    self.assertEqual(value["type"], main[key]["type"], key)
                (job,) = definition["jobs"].values()
                self.assertEqual(job["uses"], "workflows/main.yml")
                passed = {key: value for key, value in job["with"].items()
                          if isinstance(value, str) and value.startswith("${{")}
                self.assertEqual(passed, {key: "${{ inputs.%s }}" % key for key in entry["inputs"]})
                # The selection is fixed in the installed workflow; adapters and mode are main.yml's.
                fixed = {key: value for key, value in job["with"].items() if key not in passed}
                self.assertEqual(fixed, dk_pr_feedback.preset(name))
                skill = name.removeprefix("cf-workflows:")
                self.assertIn(f"  - name: {skill}\n    path: skills/{skill}/SKILL.md\n"
                              f"    workflow: {entry['entry']}\n", descriptor)
                text = (fix_preflight.INSTALL / "skills" / skill / "SKILL.md").read_text()
                self.assertTrue(text.startswith(f"---\nname: {skill}\ndescription: "))
                self.assertIn("This plugin version is not runnable.", text)

    def test_options_follow_the_baseline_schemas_and_installed_support(self):
        self.assertEqual(self.entry("dk-pr-feedback-reply", options={"reply_prompt": admission.REPLY_PROMPT})
                         ["entry"]["options"]["reply_prompt"]["source"], "invocation")
        result = self.entry("dk-pr-feedback-address", options={"additional_validation_commands": []})
        self.assertEqual((result["preflight"], result["inputs"]["validation_commands"]), ("admitted", []))
        commands = [["make", "check"], ["python3", "-m", "pytest", "-q", "tests/a b.py"]]
        result = self.entry("dk-pr-feedback-address", options={"additional_validation_commands": commands})
        self.assertEqual(result["preflight"], "admitted", result)
        self.assertEqual(result["inputs"]["validation_commands"], commands)
        self.assertEqual(result["entry"]["options"]["additional_validation_commands"],
                         {"value": commands, "source": "invocation"})
        # Only the address entry workflow declares the input.
        self.assertNotIn("validation_commands", self.entry("dk-pr-feedback-read")["inputs"])
        for name, options, path, fragment in (
                ("dk-pr-feedback-read", {"additional_validation_commands": []}, "options.additional_validation_commands",
                 "unsupported option"),
                ("dk-pr-feedback-reply", {"reply_prompt": "Be terse."}, "options.reply_prompt", "custom reply prompt"),
                ("dk-pr-feedback-reply", {"reply_prompt": " "}, "options.reply_prompt", "nonempty"),
                ("dk-pr-feedback-reply", {"resolve_answered_threads": "yes"}, "options.resolve_answered_threads",
                 "boolean"),
                ("dk-pr-feedback-address", {"additional_validation_commands": [[]]},
                 "options.additional_validation_commands", "nonempty argument strings"),
                ("dk-pr-feedback-address", {"additional_validation_commands": ["make check"]},
                 "options.additional_validation_commands", "nonempty argument strings"),
                ("dk-pr-feedback-address", {"additional_validation_commands": [["make", " "]]},
                 "options.additional_validation_commands", "nonempty argument strings"),
                ("dk-pr-feedback-address", {"additional_validation_commands": [["make"]] * 33},
                 "options.additional_validation_commands", "at most 32"),
                ("dk-pr-feedback-address", {"additional_validation_commands": [["x" * 70000]]},
                 "options.additional_validation_commands", "64 KiB"),
                ("dk-pr-feedback-address", {"additional_validation_commands": [["make"], ["/usr/bin/codex", "exec"]]},
                 "options.additional_validation_commands[1]", "never launch Codex or Claude Code"),
                ("dk-pr-feedback-address", {"additional_validation_commands": [["claude", "-p", "check"]]},
                 "options.additional_validation_commands[0]", "never launch Codex or Claude Code"),
                ("dk-pr-feedback-reply", [], "options", "object")):
            with self.subTest(name=name, options=options):
                self.rejected(self.entry(name, options=options), path, fragment)
                self.assertEqual(self.transport.calls, Counter())
        self.rejected(self.entry("dk-pr-feedback-other"), "entry", "unknown")
        self.rejected(self.entry("dk-pr-feedback-read", request={"repository": "github.com/owner/repo",
                                                                  "instructions": "Read it."}),
                      "request.pr", "one pull request")


class EntryRuns(unittest.TestCase):
    """The entries' inputs prepared and claimed by the installed helpers on one repository."""

    def setUp(self):
        git_fixture.isolate(self)
        temp = tempfile.TemporaryDirectory(prefix="cfw feedback entries ")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "target"
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
        git(self.root, "commit", "-am", "Change value")
        self.github = Transport(git(self.root, "rev-parse", "HEAD"), self.target)
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])
        self.native = 0

    def preflight(self, name=None, instructions="Address the feedback on PR 7.", options=None):
        engine = FakeTransport()
        arguments = {"request": {"pr": PR_URL, "instructions": instructions}, "deployment": DEPLOYMENT,
                     "repository": self.root, "transport": engine, "home": self.base / "home"}
        if name:
            result = dk_pr_feedback.preflight(name, options=options, **arguments)
        else:
            result = fix_preflight.preflight(configuration=None, invocation=None, **arguments)
        self.assertEqual(result["preflight"], "admitted", result)
        self.assertEqual(engine.calls, Counter(capabilities=1))
        return result

    def plan(self, prepared, provider=None):
        name = prepared["entry"]["name"] if "entry" in prepared else None
        controls = (entry_controls(name, prepared["inputs"]) if name else
                    {key: value for key, value in prepared["inputs"].items() if key != "request"})
        result = prepare(self.root, prepared["state"]["request_document"], controls, "exec-1",
                         provider or self.github)
        return read_artifact(self.root, result["plan"])

    def ledger(self, plan):
        ledger = Ledger(self.root, plan["logical_run"], plan["pr"]["url"])
        self.ledgers.append(ledger)
        return ledger

    @staticmethod
    def task(plan, adapter):
        return next(task for task in plan["tasks"] if task["skill"] == adapter)

    def report(self, **values):
        evidence = {"id": "source", "path": "AGENTS.md", "sha256": app.source_file(self.root, "AGENTS.md")}
        result = {"complete": True, "findings": [], "evidence": [evidence], "blockers": [],
                  "checklist": [{"id": item["id"], "status": "completed", "evidence": ["source"]} for item in CHECKLIST]}
        result.update(values)
        return result

    def feedback(self, value):
        return self.report(deliverables={"feedback": {"assessments": value}})

    def decided(self):
        """A reconciliation with nothing to decide (prfix/decisions.py accepts only a sourced result)."""
        return self.report(deliverables={"decisions": {
            "summary": "No decisions in this discussion.", "decisions": [], "contradictions": [], "questions": [],
            "language_review": {"passes": 1, "changed": [False], "ambiguities": []}}})

    def claim(self, plan, ledger, adapter, provider=None):
        claimed = app.prepare_claim(self.root, provider or self.github, ledger, plan,
                                    {"task": self.task(plan, adapter)["id"]})
        self.native += bool(claimed["invoke"])
        return claimed

    def accept(self, plan, ledger, claimed, report, round_number=0, provider=None):
        return app.accept(self.root, provider or self.github, ledger, plan,
                          {"claim": claimed["claim"], "agent": AGENTS[0], "report": report, "round": round_number})

    def view(self, plan):
        return feedback.View(plan["pr"], copy.deepcopy(self.github.evidence))

    def change(self, path, content):
        before = hashlib.sha256(git(self.root, "show", "HEAD:" + path).encode() + b"\n").hexdigest()
        (self.root / path).write_text(content)
        return [{"path": path, "before": before, "after": app.source_file(self.root, path)}]

    def commits(self):
        return int(git(self.root, "rev-list", "--count", self.target + "..HEAD"))

    def test_address_keeps_obligations_and_decisions_and_never_implements_twice(self):
        plan = self.plan(self.preflight("dk-pr-feedback-address"))
        self.assertEqual([task["skill"] for task in plan["tasks"]],
                         ["read-feedback", "reconcile-decisions", "address-feedback"])
        ledger = self.ledger(plan)
        view = self.view(plan)

        read = self.claim(plan, ledger, "read-feedback")
        sources = read["task_input"]["feedback"]["sources"]
        # The author's own answer (inline:61) is not feedback; every other item is, with its thread.
        self.assertEqual([item["source"] for item in sources],
                         ["comment:40", "comment:41", "inline:30", "inline:60", "review:50"])
        self.assertEqual({item["source"]: item["thread"] for item in sources if item["thread"]},
                         {"inline:30": "T30", "inline:60": "T60"})
        # An incomplete read and a read that decides an implementation each get a bounded correction.
        for round_number, value, error in (
                (0, assessments(view)[:-1], "unassessed: inline:60"),
                (1, assessments(view, constant="rejected"), "decides no implementation")):
            result = self.accept(plan, ledger, read, self.feedback(value), round_number)
            self.assertFalse(result["complete"])
            self.assertIn(error, result["feedback"]["contract_error"])
        self.assertTrue(self.accept(plan, ledger, read, self.feedback(assessments(view)), 2)["complete"])
        self.assertTrue(self.accept(plan, ledger, self.claim(plan, ledger, "reconcile-decisions"),
                                    self.decided())["complete"])

        address = self.claim(plan, ledger, "address-feedback")
        retained = {item["source"]: item["current"] for item in address["task_input"]["feedback"]["sources"]}
        self.assertEqual(retained["comment:40"]["requests"][0]["implementation"], "none")
        changes = self.change("code.py", "COUNT = 2\n")
        decided = assessments(view, rename="fixed", constant="rejected", migration="deferred")
        declined = copy.deepcopy(decided)
        declined[1]["requests"][0]["response"] = {"disposition": "no_reply", "reason": "no_author_reply_needed",
                                                  "justification": "Style suggestion from a bot."}
        fix = {"changes": changes, "commit_message": "Rename value to count"}
        for round_number, report, error in (
                (0, {**self.feedback(declined), **fix}, "concrete author request"),
                (1, self.feedback(decided), "must cite paths")):
            result = self.accept(plan, ledger, address, report, round_number)
            self.assertIn(error, result["feedback"]["contract_error"])
        self.assertEqual(self.commits(), 1, "a rejected report commits nothing")
        self.assertTrue(self.accept(plan, ledger, address, {**self.feedback(decided), **fix}, 2)["complete"])
        self.assertEqual(self.commits(), 2)
        # The commit changes the diff, so the shared workflow's next cycle reads again; every
        # source is already assessed, so that read adds no assessment and the fix stays current.
        # The reconciliation binds no diff, so the commit alone reuses it without a session.
        self.assertFalse(app.coverage(self.root, self.github, ledger, plan, {})["complete"])
        self.assertTrue(self.accept(plan, ledger, self.claim(plan, ledger, "read-feedback"), self.report())["complete"])
        self.assertFalse(self.claim(plan, ledger, "reconcile-decisions")["invoke"])
        self.assertFalse(self.claim(plan, ledger, "address-feedback")["invoke"])

        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual((covered["complete"], covered["missing"]), (True, []))
        self.assertEqual(covered["responses"]["status"], "not_selected")
        # Declined and deferred author requests still owe answers; the participant question
        # and the already answered thread do not.
        self.assertEqual(covered["feedback"]["replies_needed"], ["comment:40", "inline:30", "review:50"])
        self.assertEqual([item["source"] for item in covered["feedback"]["unanswered"]], ["comment:41"])
        self.assertEqual(covered["feedback"]["sources"]["comment:40"]["requests"][0]["implementation"], "rejected")
        self.assertEqual(self.native, 4)
        self.assertEqual(set(self.github.calls), {"capture"}, "the address entry publishes nothing")

        # A resumed run of the same execution reuses every occurrence: no session, no commit.
        resumed = self.ledger(plan)
        for adapter in ("read-feedback", "reconcile-decisions", "address-feedback"):
            self.assertFalse(self.claim(plan, resumed, adapter)["invoke"])
        self.assertEqual((self.native, self.commits()), (4, 2))

        # The fix is published; a composed dk-pr-fix run then reaches the same occurrences.
        self.github.evidence["metadata"]["head"]["sha"] = git(self.root, "rev-parse", "HEAD")
        composed = self.plan(self.preflight(instructions="Fix PR 7 and finish it."))
        for adapter in ("read-feedback", "address-feedback"):
            self.assertEqual(self.task(composed, adapter)["id"], self.task(plan, adapter)["id"])
        ledger = self.ledger(composed)
        read = self.claim(composed, ledger, "read-feedback")
        self.assertEqual({item["source"]: item["current"]["requests"][0]["implementation"]
                          for item in read["task_input"]["feedback"]["sources"]}["review:50"], "deferred")
        result = self.accept(composed, ledger, read, self.feedback(assessments(view)))
        self.assertIn("settled implementation decision of rename (fixed)", result["feedback"]["contract_error"])
        # Every source is already assessed, so the read needs no new assessment.
        self.assertTrue(self.accept(composed, ledger, read, self.report(), 1)["complete"])
        address = self.claim(composed, ledger, "address-feedback")
        flipped = assessments(view, rename="fixed", constant="rejected", migration="fixed")
        result = self.accept(composed, ledger, address, self.feedback(flipped))
        self.assertIn("settled implementation decision of migration", result["feedback"]["contract_error"])
        # The retained fix needs no new edit, so nothing is implemented or committed twice.
        self.assertTrue(self.accept(composed, ledger, address, self.feedback(decided), 1)["complete"])
        self.assertEqual((self.native, self.commits()), (6, 2))
        self.assertEqual(set(self.github.calls), {"capture"})

    def addressed(self):
        """The address preset through a read and reconciliation, with its fix claim open."""
        plan = self.plan(self.preflight("dk-pr-feedback-address"))
        ledger, view = self.ledger(plan), self.view(plan)
        self.assertTrue(self.accept(plan, ledger, self.claim(plan, ledger, "read-feedback"),
                                    self.feedback(assessments(view)))["complete"])
        self.assertTrue(self.accept(plan, ledger, self.claim(plan, ledger, "reconcile-decisions"),
                                    self.decided())["complete"])
        return plan, ledger, view, self.claim(plan, ledger, "address-feedback")

    def test_a_source_arriving_during_acceptance_is_left_for_the_next_read(self):
        # Review cycle 1: retention records exactly the validated assessments, never a later observation.
        plan, ledger, view, address = self.addressed()
        fix = {"changes": self.change("code.py", "COUNT = 2\n"), "commit_message": "Rename value to count"}
        original = responses.retain_feedback

        def late(*args):
            self.github.evidence["comments"].append({"id": 99, "user": REVIEWER, "created_at": at(9),
                                                     "html_url": PR_URL + "#issuecomment-99", "body": "Add a test."})
            return original(*args)
        with patch.object(responses, "retain_feedback", late):
            result = self.accept(plan, ledger, address, {**self.feedback(assessments(view, rename="fixed")), **fix})
        self.assertTrue(result["complete"])
        self.assertEqual(self.commits(), 2)
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual(covered["feedback"]["sources"]["inline:30"]["requests"][0]["implementation"], "fixed")
        self.assertEqual(covered["feedback"]["unassessed"], ["comment:99"])

    def test_a_fixed_decision_needs_its_cited_paths_changed_and_intact(self):
        plan, ledger, view, address = self.addressed()
        before = git(self.root, "rev-parse", "HEAD")
        fix = {"changes": self.change("code.py", "COUNT = 2\n"), "commit_message": "Rename value to count"}
        # Review cycle 1: one changed file backs no fix cited elsewhere, nor a fix that cites nothing.
        elsewhere = assessments(view, rename="fixed", migration="fixed")
        elsewhere[3]["requests"][0]["paths"] = ["migrations/0002.py"]
        uncited = assessments(view, rename="fixed")
        del uncited[0]["requests"][0]["paths"]
        for round_number, value in enumerate((elsewhere, uncited)):
            result = self.accept(plan, ledger, address, {**self.feedback(value), **fix}, round_number)
            self.assertIn("must cite paths", result["feedback"]["contract_error"])
        self.assertTrue(self.accept(plan, ledger, address, {**self.feedback(assessments(view, rename="fixed")), **fix},
                                    2)["complete"])
        self.assertEqual(app.feedback_fixes(self.root, ledger), {("inline:30", "rename"): {"code.py"}})
        # Review cycle 1: the fix is lost to a reset; a rerun must implement it again, not claim it.
        git(self.root, "reset", "--hard", before)
        self.assertEqual(app.feedback_fixes(self.root, ledger), {})
        for adapter in ("read-feedback", "reconcile-decisions"):
            claimed = self.claim(plan, ledger, adapter)
            if claimed["invoke"]:
                self.accept(plan, ledger, claimed,
                            self.decided() if adapter == "reconcile-decisions" else self.report())
        address = self.claim(plan, ledger, "address-feedback")
        self.assertTrue(address["invoke"])
        self.assertEqual(address["task_input"]["feedback"]["fixes"], [])
        lost = [{"source": "inline:30", "request": "rename"}]
        self.assertEqual(address["task_input"]["feedback"]["unbacked"], lost)
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertFalse(covered["complete"])
        self.assertEqual(covered["feedback"]["unbacked"], lost)
        # Review cycle 2: omitting the source whose fix was lost is not a way around it.
        result = self.accept(plan, ledger, address, self.report())
        self.assertIn("lost their controlled fix", result["feedback"]["contract_error"])
        self.assertIn("inline:30", result["feedback"]["contract_error"])
        result = self.accept(plan, ledger, address, self.feedback(assessments(view, rename="fixed")), 1)
        self.assertIn("must cite paths", result["feedback"]["contract_error"])
        fix = {"changes": self.change("code.py", "COUNT = 2\n"), "commit_message": "Rename value to count again"}
        self.assertTrue(self.accept(plan, ledger, address, {**self.feedback(assessments(view, rename="fixed")), **fix},
                                    2)["complete"])
        self.assertEqual(self.commits(), 2)
        self.assertEqual(app.coverage(self.root, self.github, ledger, plan, {})["feedback"]["unbacked"], [])

    def test_a_later_commit_to_the_fixed_file_keeps_the_fix_and_a_revert_drops_it(self):
        # Review cycle 3: further edits to a fixed path keep the fix backed; restoring its
        # pre-fix bytes does not.
        plan, ledger, view, address = self.addressed()
        fix = {"changes": self.change("code.py", "COUNT = 2\n"), "commit_message": "Rename value to count"}
        self.assertTrue(self.accept(plan, ledger, address, {**self.feedback(assessments(view, rename="fixed")),
                                                            **fix})["complete"])
        (self.root / "code.py").write_text("COUNT = 2\n# documented\n")
        git(self.root, "commit", "-qam", "Document COUNT")
        self.github.evidence["metadata"]["head"]["sha"] = git(self.root, "rev-parse", "HEAD")
        self.assertEqual(app.feedback_fixes(self.root, ledger), {("inline:30", "rename"): {"code.py"}})
        for adapter in ("read-feedback", "reconcile-decisions"):
            claimed = self.claim(plan, ledger, adapter)
            if claimed["invoke"]:
                self.accept(plan, ledger, claimed,
                            self.decided() if adapter == "reconcile-decisions" else self.report())
        address = self.claim(plan, ledger, "address-feedback")
        self.assertEqual(address["task_input"]["feedback"]["unbacked"], [])
        self.assertTrue(self.accept(plan, ledger, address, self.report())["complete"])
        self.assertEqual(app.coverage(self.root, self.github, ledger, plan, {})["feedback"]["unbacked"], [])
        (self.root / "code.py").write_text("value = 2\n")
        git(self.root, "commit", "-qam", "Revert the rename")
        self.github.evidence["metadata"]["head"]["sha"] = git(self.root, "rev-parse", "HEAD")
        self.assertEqual(app.feedback_fixes(self.root, ledger), {})
        self.assertEqual(app.coverage(self.root, self.github, ledger, plan, {})["feedback"]["unbacked"],
                         [{"source": "inline:30", "request": "rename"}])

    def test_an_edited_source_is_assessed_again_and_keeps_its_obligation(self):
        plan = self.plan(self.preflight("dk-pr-feedback-read"))
        self.assertEqual([task["skill"] for task in plan["tasks"]], ["read-feedback"])
        ledger = self.ledger(plan)
        read = self.claim(plan, ledger, "read-feedback")
        self.assertTrue(self.accept(plan, ledger, read, self.feedback(assessments(self.view(plan))))["complete"])
        self.assertTrue(app.coverage(self.root, self.github, ledger, plan, {})["complete"])
        self.github.evidence["reviews"][0]["body"] += " Keep this PR small."
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertFalse(covered["complete"])
        self.assertEqual(covered["feedback"]["unassessed"], ["review:50"])
        read = self.claim(plan, ledger, "read-feedback")
        self.assertEqual([item["source"] for item in read["task_input"]["feedback"]["sources"] if item["changed"]],
                         ["review:50"])
        view = self.view(plan)
        dropped = [assessment(view, "review:50", request(
            "migration", "move the migration into a follow-up PR", addressee="none", response={
                "disposition": "no_reply", "reason": "no_author_reply_needed", "justification": "Edited."}))]
        result = self.accept(plan, ledger, read, self.feedback(dropped))
        self.assertIn("drops the response obligation of migration", result["feedback"]["contract_error"])
        self.assertTrue(self.accept(plan, ledger, read, self.feedback(assessments(view)[3:4]), 1)["complete"])
        self.assertTrue(app.coverage(self.root, self.github, ledger, plan, {})["complete"])
        self.assertEqual(self.native, 2)

    def test_entry_workflows_plan_exactly_what_preflight_admitted(self):
        # The entry workflow runs main.yml's bundled adapters, not the preflight's copies:
        # both must yield the same occurrences, assignments and stages.
        for name in dk_pr_feedback.ENTRIES:
            with self.subTest(name=name):
                prepared = self.preflight(name)
                admitted = {key: fix_preflight._adapter(prepared["snapshot"]["adapters"][key])
                            for key in prepared["entry"]["adapters"]}
                direct = {**{key: value for key, value in prepared["inputs"].items() if key != "request"},
                          **dk_pr_feedback.preset(name), "adapters": admitted}
                expected = read_artifact(self.root, prepare(self.root, prepared["state"]["request_document"], direct,
                                                            "exec-1", self.github)["plan"])
                plan = self.plan(prepared)
                self.assertEqual((plan["tasks"], plan["stages"], plan["options"]),
                                 (expected["tasks"], expected["stages"], expected["options"]))
                self.assertNotIn("validation_commands", plan)

    def test_validation_commands_reach_only_the_address_session_and_must_pass(self):
        commands = [["python3", "-c", "print('ok')"], ["make", "check"]]
        plan = self.plan(self.preflight("dk-pr-feedback-address", options={"additional_validation_commands": commands}))
        self.assertEqual(plan["validation_commands"], commands)
        ledger, view = self.ledger(plan), self.view(plan)
        read = self.claim(plan, ledger, "read-feedback")
        self.assertNotIn("validation_commands", read["task_input"])
        self.assertTrue(self.accept(plan, ledger, read, self.feedback(assessments(view)))["complete"])
        reconcile = self.claim(plan, ledger, "reconcile-decisions")
        self.assertNotIn("validation_commands", reconcile["task_input"])
        self.accept(plan, ledger, reconcile, self.report())
        address = self.claim(plan, ledger, "address-feedback")
        self.assertEqual(address["task_input"]["validation_commands"],
                         {"commands": commands, "contract": app.VALIDATION_CONTRACT})
        decided = assessments(view, rename="fixed")
        fix = {"changes": self.change("code.py", "COUNT = 2\n"), "commit_message": "Rename value to count"}

        def report(results):
            value = self.feedback(decided)
            if results is not None:
                value["deliverables"]["validation_commands"] = results
            return {**value, **fix}
        passed = [{"argv": command, "exit_code": 0} for command in commands]
        for round_number, results, error in (
                (0, None, "each of the 2 configured validation commands"),
                (1, [passed[0], {"argv": ["make", "test"], "exit_code": 0}], "command 2 must run exactly"),
                (2, [passed[0], {"argv": commands[1], "exit_code": 2}], "command 2 failed with exit code 2")):
            result = self.accept(plan, ledger, address, report(results), round_number)
            self.assertFalse(result["complete"])
            self.assertIn(error, result["feedback"]["contract_error"])
        self.assertEqual(self.commits(), 1, "a rejected report commits nothing")
        # A same-session correction that runs every command exactly and passes is accepted and committed.
        self.assertTrue(self.accept(plan, ledger, address, report(passed), 3)["complete"])
        self.assertEqual(self.commits(), 2)
        self.assertEqual(self.native, 3, "one read, one reconciliation and one corrected address session")

    def test_validation_commands_are_bounded_by_the_installed_helper(self):
        prepared = self.preflight("dk-pr-feedback-address")
        controls = entry_controls("dk-pr-feedback-address", prepared["inputs"])
        for commands, error in (([["make", ""]], "nonempty argument"), ("make check", "nonempty argument"),
                                ([["make"]] * 33, "at most 32")):
            with self.subTest(commands=commands):
                with self.assertRaisesRegex(ValueError, error):
                    prepare(self.root, prepared["state"]["request_document"],
                            {**controls, "validation_commands": commands}, "exec-1", self.github)
        # Incoming review addresses no feedback.
        review = {**controls, "tasks": ["read-feedback", {"stage": "reviews"}], "reviewers": ["review"],
                  "mode": "review", "validation_commands": [["make", "check"]]}
        with self.assertRaisesRegex(ValueError, "incoming review"):
            prepare(self.root, prepared["state"]["request_document"], review, "exec-1", self.github)
        self.assertEqual(self.github.calls["capture"], 0, "rejected inputs capture nothing")
        # A composed dk-pr-fix plan without commands gives its address session none.
        plan = self.plan(self.preflight())
        self.assertNotIn("validation_commands", plan)

    def test_the_reply_preset_prepares_one_shared_responder_stage(self):
        plan = self.plan(self.preflight("dk-pr-feedback-reply"))
        self.assertEqual([(task["skill"], task["kind"], task["writes"]) for task in plan["tasks"]],
                         [("reply-feedback", "reply", False)])
        self.assertEqual([(stage["kind"], stage["task"]) for stage in plan["stages"]],
                         [("responses", plan["tasks"][0]["id"])])
        self.assertIn("Reply prompt: " + admission.REPLY_PROMPT, plan["tasks"][0]["assignment"])

    def test_read_captures_every_page_and_truncation_never_completes(self):
        evidence = paged_discussion(git(self.root, "rev-parse", "HEAD"), self.target)
        paged = PagedGitHub(self.root, evidence)
        plan = self.plan(self.preflight("dk-pr-feedback-read"), provider=paged)
        base = "repos/owner/repo"
        self.assertEqual(paged.calls, Counter({
            ("GET", base + "/pulls/7"): 2, ("GET", "user"): 1, ("GET", base + "/issues/7/comments"): 2,
            ("GET", base + "/pulls/7/reviews"): 1, ("GET", base + "/pulls/7/comments"): 2, ("POST", "graphql"): 2}))
        view = feedback.View(plan["pr"], paged.capture(plan["pr"]))
        self.assertEqual(len(view.sources), 252)
        self.assertEqual(len(view.threads), 101)
        value = [assessment(view, key, request("r1", source["body"])) for key, source in sorted(view.sources.items())]
        ledger = self.ledger(plan)
        read = self.claim(plan, ledger, "read-feedback", paged)
        self.assertEqual(len(read["task_input"]["feedback"]["sources"]), 252)
        result = self.accept(plan, ledger, read, self.feedback(value[1:]), provider=paged)
        self.assertIn("unassessed: comment:1000", result["feedback"]["contract_error"])
        self.assertTrue(self.accept(plan, ledger, read, self.feedback(value), 1, provider=paged)["complete"])
        covered = app.coverage(self.root, paged, ledger, plan, {})
        self.assertTrue(covered["complete"])
        self.assertEqual(len(covered["feedback"]["replies_needed"]), 252)

        # Truncated, duplicated or drifting observations never become complete feedback.
        cases = {"thread count": ("thread_total", 102, "incomplete review thread enumeration"),
                 "duplicate row": ("duplicate", True, "duplicate GitHub row"),
                 "drift": ("drift", True, "PR changed during capture")}
        for label, (attribute, value_, message) in cases.items():
            with self.subTest(label):
                broken = PagedGitHub(self.root, copy.deepcopy(evidence))
                setattr(broken, attribute, value_)
                with self.assertRaisesRegex(ValueError, message):
                    broken.capture(plan["pr"])
                with self.assertRaisesRegex(ValueError, message):
                    app.prepare_claim(self.root, broken, self.ledger(plan), plan,
                                      {"task": self.task(plan, "read-feedback")["id"]})
        missing = copy.deepcopy(evidence)
        missing["threads"].pop()
        with self.assertRaisesRegex(feedback.FeedbackError, "no enumerated review thread"):
            feedback.View(plan["pr"], PagedGitHub(self.root, missing).capture(plan["pr"]))
        with self.assertRaisesRegex(AssertionError, "unconfigured fixture route"):
            paged.pull_ref(plan["pr"])
        self.assertEqual(self.native, 1)


class ReplyEntry(replies.Fixture):
    """dk-pr-feedback-reply: the shared response stage alone, then composed, on one logical run."""

    def setUp(self):
        super().setUp()
        self.composed = copy.deepcopy(self.plan)
        # The reply preset: one responder occurrence in one response stage.
        self.plan["tasks"] = [self.task("responder", "reply")]
        self.plan["stages"] = [{"id": "stage_1", "kind": "responses", "phase": "tasks", "task": "responder"}]
        self.refuse = set()
        self.lose = set()
        github, test = self.github, self

        original = github.request

        def request(method, path, payload=None):
            kind = ("resolve" if path == "graphql" and payload["query"].startswith("mutation")
                    else "reply" if method == "POST" and path.endswith(("/comments", "/replies")) else None)
            if kind in test.refuse:
                github.counts["write"] += 1
                raise publication.Rejected({"status": 403, "message": "Resource not accessible by integration"})
            if kind in test.lose:
                github.counts["write"] += 1
                github.calls.append((method, path, copy.deepcopy(payload)))
                raise OSError("fixture response lost before effect")
            return original(method, path, payload)
        github.request = request

    def run_stage(self, plan=None, ledger=None):
        """responses.yml; a refused or uncertain publication fails its step and the stage continues."""
        plan, ledger = plan or self.plan, ledger or self.ledger
        for _ in range(2):
            for item in publishing_list(self, plan, ledger):
                try:
                    self.publish_in(plan, item, ledger)
                except (PublicationError, OSError):
                    pass
            responses.settle(self.root, self.github, ledger, plan, {})
        return responses.status(self.root, self.github, ledger, plan)

    def publish_in(self, plan, item, ledger):
        saved, self.plan = self.plan, plan
        try:
            return self.publish(item, ledger)
        finally:
            self.plan = saved

    def covered(self, plan=None):
        return app.coverage(self.root, self.github, self.ledger, plan or self.plan, {})

    def with_answered_thread(self):
        self.github.ev["inline"] += [
            {"id": 60, "user": replies.REVIEWER, "created_at": at(4), "html_url": PR_URL + "#discussion_r60",
             "in_reply_to_id": None, "body": "Why is this a module constant?"},
            {"id": 61, "user": replies.MAINTAINER, "created_at": at(5), "html_url": PR_URL + "#discussion_r61",
             "in_reply_to_id": 60, "body": "It is shared by both callers."}]
        self.github.ev["threads"].append(thread("T60", 60))
        value = self.drafts()
        view = self.view()
        value["assessments"].append(assessment(view, "inline:60", request("why", "module constant", response={
            "disposition": "already_answered", "reply": {"kind": "inline", "id": 61},
            "justification": "Answered in the thread."})))
        return value

    def test_standalone_replies_are_never_repeated_by_a_composed_run(self):
        self.respond(self.with_answered_thread())
        result = self.run_stage()
        self.assertTrue(result["settled"], result)
        writes = [(method, path.rsplit("/", 2)[-2:]) for method, path, _ in self.github.writes]
        self.assertEqual(writes, [("POST", ["7", "comments"]), ("POST", ["30", "replies"]), ("POST", ["graphql"])])
        # The already answered thread gets no reply and is not resolved by this run.
        self.assertFalse(next(t for t in self.github.ev["threads"] if t["id"] == "T60")["isResolved"])
        self.assertEqual(self.covered()["missing"], [])
        # dk-pr-fix on the same logical run: both of its response stages find everything settled.
        for _ in range(2):
            self.assertTrue(self.run_stage(self.composed, self.open())["settled"])
        self.assertEqual(self.github.counts["write"], 3)
        self.assertNotIn("feedback_responses", self.covered(self.composed)["missing"])

    def test_refused_and_uncertain_resolution_keep_the_entry_incomplete(self):
        for mode in ("refuse", "lose"):
            with self.subTest(mode=mode):
                self.setUp()
                getattr(self, mode).add("resolve")
                self.respond()
                result = self.run_stage()
                # Every reply is verified, but the resolution is not observed: never silently complete.
                self.assertEqual((result["status"], result["settled"]), ("complete", False), result)
                self.assertEqual(len(result["refused" if mode == "refuse" else "uncertain"]), 1, result)
                self.assertIn("feedback_responses", self.covered()["missing"])
                self.assertFalse(self.github.ev["threads"][0]["isResolved"])
                if mode == "lose":
                    # Absence proves nothing: the original stays pending and is never resent.
                    self.assertFalse(self.run_stage()["settled"])
                    self.assertEqual(self.github.counts["write"], 3)
                    continue
                self.refuse.clear()
                result = self.run_stage()
                self.assertTrue(result["settled"], result)
                self.assertTrue(self.github.ev["threads"][0]["isResolved"])
                self.assertEqual(self.covered()["missing"], [])
                self.assertEqual(self.github.counts["write"], 4, "two replies, one refused and one fresh resolution")

    def test_refused_reply_and_denied_authority_never_count_as_answers(self):
        self.refuse.add("reply")
        self.respond()
        result = self.run_stage()
        self.assertEqual((result["status"], result["settled"]), ("pending", False))
        self.assertEqual(result["replies_needed"], ["comment:40", "inline:30"])
        self.assertTrue(any("was not_applied" in blocker for blocker in result["blockers"]), result)
        self.assertIn("feedback_responses", self.covered()["missing"])
        self.assertFalse(self.github.ev["threads"][0]["isResolved"], "an unanswered thread is never resolved")
        self.refuse.clear()
        self.assertTrue(self.run_stage()["settled"])

        for denied in ("permission", "authorization"):
            with self.subTest(denied=denied):
                self.setUp()
                if denied == "permission":
                    self.plan["options"]["permissions"]["reply_to_feedback"] = False
                else:
                    self.plan["authorizations"].pop("reply")
                self.respond()
                result = self.run_stage()
                self.assertEqual((result["status"], result["settled"]), ("blocked", False))
                self.assertEqual(self.github.counts["write"], 0)
                self.assertIn("feedback_responses", self.covered()["missing"])

    def test_lost_reply_response_is_reconciled_on_resume_without_a_resend(self):
        self.respond()
        self.github.fault = "after"
        with self.assertRaises(OSError):
            for item in publishing_list(self, self.plan, self.ledger):
                self.publish(item)
        self.github.fault = None
        self.assertEqual(self.github.counts["write"], 1)
        self.assertFalse(responses.status(self.root, self.github, self.ledger, self.plan)["settled"])
        result = self.run_stage(ledger=self.open())
        self.assertTrue(result["settled"], result)
        self.assertEqual(self.github.counts["write"], 3)
        self.assertEqual(len([item for item in self.github.ev["comments"] if item["user"]["id"] == 11]), 1)


def publishing_list(fixture, plan, ledger):
    from prfix import publishing
    return publishing.dispatch("publication_list", fixture.root, fixture.github, ledger, plan,
                               {"kind": "responses"})["items"]


if __name__ == "__main__":
    unittest.main()
