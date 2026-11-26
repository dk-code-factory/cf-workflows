"""Validate YAML selections and prepare immutable application evidence.

No workflow/configuration files, agent dispatch, publication or execution loops
belong here. The checked-in YAML owns all orchestration.

Adapted from Agent Workflow's portable PR-fix example (MIT; see LICENSE).
"""
import hashlib
import json
import os
from pathlib import Path
import re

from .core import (FORMAT, Ledger, artifact, auth, digest,
                   identity, state_directory, text)

CHECKLIST = [
    {"id": "assignment", "text": "Perform the exact assigned task against current source and retained original instructions."},
    {"id": "evidence", "text": "Attach source files with current SHA-256 evidence and preserve every original finding/source identity."},
    {"id": "scope", "text": "Account for requirements, blockers and all proposed changes or publication operations without expanding user authority."},
]

# Legacy names remain selectors, not installed plugin invocations.
TASK_ALIASES = {
    "dk-workflows:dk-pr-feedback-read": "read-feedback",
    "dk-workflows:dk-pr-decisions": "reconcile-decisions",
    "dk-workflows:dk-pr-feedback-address": "address-feedback",
    "dk-workflows:dk-pr-split": "assess-scope",
    "dk-workflows:dk-pr-description": "describe-pr",
    "dk-workflows:dk-commit-split": "inspect-commits",
    "dk-workflows:dk-preferences": "sync-guidance",
    "local:review": "review",
}
ADAPTER_ALIASES = {**TASK_ALIASES, **{name: legacy for legacy, name in TASK_ALIASES.items()}}
STAGE_ALIASES = {"dk-workflows:dk-pr-publish": "publication", "dk-workflows:dk-ci": "ci",
                 "dk-workflows:dk-pr-reviewers-run": "reviews",
                 "dk-workflows:dk-pr-feedback-reply": "responses"}
# The response stage's read-only adapter. It runs only inside that stage, and
# every response stage of a plan shares its one occurrence (see responses.py).
RESPONDER = "reply-feedback"
# Plan modes. "review" is incoming review of a PR (cf-workflows:dk-pr-review):
# read-only tasks and reviewers, independent verification and no finding fixes.
MODES = ("fix", "review")


def _selection(raw):
    call = {"task": raw} if isinstance(raw, str) else raw
    if not isinstance(call, dict) or len(set(call) & {"task", "skill", "stage"}) != 1:
        raise ValueError("select exactly one task or stage")
    if "skill" in call:
        name = call["skill"]
        if isinstance(name, str) and name in STAGE_ALIASES:
            allowed = {"reviewers"} if STAGE_ALIASES[name] == "reviews" else set()
            options = call.get("options", {})
            if (set(call) - {"skill", "options", "required", "instructions"}
                    or not isinstance(options, dict) or set(options) - allowed
                    or call.get("required", True) is not True or call.get("instructions", "") != ""):
                raise ValueError("unsupported managed stage controls")
            call = {"stage": STAGE_ALIASES[name], **options}
        else:
            call = {"task": name, **{key: value for key, value in call.items() if key != "skill"}}
    elif "task" in call and isinstance(call["task"], str) and call["task"] in STAGE_ALIASES:
        # Bare legacy strings were historically accepted alongside skill objects.
        return _selection({"skill": call["task"], **{key: value for key, value in call.items() if key != "task"}})
    if "stage" in call:
        stage = call["stage"]
        if not isinstance(stage, str) or stage not in {"publication", "responses", "reviews", "ci"}:
            raise ValueError("unsupported managed stage")
        if set(call) - ({"stage", "reviewers"} if stage == "reviews" else {"stage"}):
            raise ValueError("unsupported managed stage controls")
    elif set(call) - {"task", "options", "required", "instructions"}:
        raise ValueError("portable tasks support task/options/required/instructions only")
    return call


def _groups(raw):
    if not isinstance(raw, list):
        raise ValueError("selected plan must be an array")
    result = []
    for entry in raw:
        group = entry if isinstance(entry, list) else [entry]
        if not group or any(isinstance(item, list) for item in group):
            raise ValueError("invalid parallel group")
        result.append([_selection(item) for item in group])
    return result


def _definition(root, raw):
    path = Path(text(raw, "native definition"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("native definitions must be workspace-relative")
    path = root / path
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("native definition must not traverse symlinks")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1 << 20:
        raise ValueError("native definition must be a bounded regular file")
    data = path.read_bytes()
    agent_definition(data)
    return data


def agent_definition(data):
    """Parse the canonical YAML agent definition form: one `key: "JSON string"` line per field.

    This is the engine's shared YAML contract (required name, description and
    instructions; optional model and reasoning-effort) restricted to the exact
    serialization its converter produces, so the standard library can read it.
    """
    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("unsupported native definition") from None
    if not body.endswith("\n") or "\r" in body:
        raise ValueError("unsupported native definition")
    value = {}
    for line in body[:-1].split("\n"):
        match = re.fullmatch(r'([a-z][a-z-]*): ("(?:[^"\\\x00-\x1f]|\\.)*")', line)
        if not match or match[1] in value:
            raise ValueError("unsupported native definition")
        try:
            field = json.loads(match[2])
        except ValueError:
            raise ValueError("unsupported native definition") from None
        if json.dumps(field, ensure_ascii=False) != match[2]:
            raise ValueError("unsupported native definition")
        value[match[1]] = field
    if (set(value) - {"name", "description", "instructions", "model", "reasoning-effort"}
            or not all(isinstance(value.get(key), str) and value[key].strip()
                       for key in ("name", "description", "instructions"))):
        raise ValueError("unsupported native definition")
    return value


def _helper_manifest(source):
    result = {}
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("helper bundle must not contain symlinks")
        if path.is_file():
            if path.stat().st_size > 4 << 20:
                raise ValueError("helper file too large")
            result[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if len(result) > 4096:
        raise ValueError("helper bundle has too many files")
    return result


def _validate_commit_identity(root):
    from .capture import run
    # Helpers have a private HOME and receive no host Git identity overrides.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_CONFIG", "GIT_AUTHOR", "GIT_COMMITTER")) and key != "EMAIL"}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        for key in ("user.name", "user.email"):
            text(run(root, ["git", "config", "--local", "--get", key],
                     env=env, limit=4096).decode("utf-8"), key)
        for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
            run(root, ["git", "-c", "user.useConfigOnly=true", "var", variable],
                env=env, limit=4096)
    except (RuntimeError, ValueError, UnicodeError) as error:
        raise ValueError("writing tasks require valid repository-local Git user.name and user.email; "
                         "configure both before preparation") from error



def prepare(root, request, controls, execution_fingerprint, provider=None, definitions=None):
    root = Path(root).resolve(strict=True)
    allowed = {"pr", "logical_run", "instructions", "authorizations", "maintain_other"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("request supports PR identity, instructions, logical_run and authorizations only; move orchestration to YAML inputs")
    pr = identity(request["pr"])
    instructions = text(request.get("instructions"), "exact original instruction")
    execution_fingerprint = text(execution_fingerprint, "admitted execution fingerprint")
    if not isinstance(controls, dict) or set(controls) - {"tasks", "finalizers", "reviewers", "limits", "permissions", "accounts", "ci", "merge_method", "settings", "resources", "adapters", "turns", "mode"}:
        raise ValueError("unsupported YAML preparation input")
    mode = controls.get("mode", "fix")
    if mode not in MODES:
        raise ValueError("unsupported plan mode")
    turns = controls.get("turns", 8)
    if type(turns) is not int or not 5 <= turns <= 2147483647:
        raise ValueError("turn allowance must support at least five retained turns")
    source = Path(__file__).resolve().parent.parent
    definitions = definitions or {role: _definition(source.parent, "agents/" + role + ".yaml")
                                  for role in ("leaf", "reviewer", "verifier", "fixer", "audience")}
    if set(definitions) != {"leaf", "reviewer", "verifier", "fixer", "audience"}:
        raise ValueError("five explicit native definitions required")
    options = {key: value for key, value in controls.items()
               if key not in {"settings", "resources", "adapters", "turns", "mode"}}
    allowed_options = {"tasks", "finalizers", "reviewers", "limits", "permissions", "accounts", "ci", "merge_method",
                       "ci_timeout_seconds", "ci_classifications", "ci_exceptions", "before_cycle", "after_cycle"}
    if set(options) - allowed_options:
        raise ValueError("unsupported portable option: " + ", ".join(sorted(set(options) - allowed_options)))
    from .ci import configuration
    configuration(options)
    from .publication import PERMISSION_INPUTS
    permissions = options.get("permissions", {})
    if (not isinstance(permissions, dict) or set(permissions) - set(PERMISSION_INPUTS)
            or any(type(value) is not bool for value in permissions.values())):
        raise ValueError("permissions accept only the declared boolean publication inputs")
    accounts = options.get("accounts", [])
    # Personal accounts are immutable host-scoped numeric identities, never logins.
    if (not isinstance(accounts, list) or any(not isinstance(item, dict) or set(item) != {"host", "id"}
            or not isinstance(item["host"], str) or type(item["id"]) is not int or item["id"] <= 0
            for item in accounts)):
        raise ValueError("accounts are {host, id} immutable numeric identities")
    if mode == "review" and any(permissions.get(name) for name in PERMISSION_INPUTS
                                if name not in ("merge", "post_findings", "approve_pr", "approve_fork_ci")):
        raise ValueError("incoming review enables no branch or feedback publication input")
    if mode != "review" and permissions.get("approve_pr"):
        raise ValueError("approval belongs to incoming review; a maintained PR is never approved by its maintainer run")
    if mode != "review" and permissions.get("approve_fork_ci"):
        raise ValueError("fork workflow approval belongs to incoming review; dk-pr-fix never approves it")
    if options.get("merge_method", "merge") not in ("merge", "squash"):
        raise ValueError("merge_method supports merge/squash only")
    limits = {"review_cycles": 5, "retries_per_task": 1, "ci_repairs_per_cycle": 5, "ci_reruns_per_head": 3,
              "max_parallel_reviewers": 0, **options.get("limits", {})}
    # max_parallel_reviewers only schedules: it caps each reviewer group's concurrency (0: no cap)
    # and never removes, merges or reorders an occurrence.
    bounds = {"review_cycles": (1, 10), "retries_per_task": (0, 1), "ci_repairs_per_cycle": (0, 5), "ci_reruns_per_head": (0, 3),
              "max_parallel_reviewers": (0, 64)}
    if set(limits) != set(bounds) or any(type(limits[key]) is not int or not low <= limits[key] <= high for key, (low, high) in bounds.items()):
        raise ValueError("unsupported retained allowance")
    authorizations = {key: auth(value) for key, value in request.get("authorizations", {}).items()}
    if request.get("maintain_other"):
        authorizations["maintain_other"] = auth(request["maintain_other"])
    logical_run = request.get("logical_run", "pr_" + digest(pr)[:32])
    state_directory(root, create=True)
    ledger = Ledger(root, logical_run, pr["url"])
    ledger.close()
    adapters = controls.get("adapters", {})
    tasks, stages = [], []
    if mode == "review" and request.get("maintain_other"):
        raise ValueError("incoming review never maintains the PR")
    occurrences = {}

    def occurrence(phase, context, name, call):
        # A stable identity survives reordering, unrelated insertions and new
        # runs. Repeated identical selections are told apart by their ordinal.
        key = digest([phase, context, TASK_ALIASES.get(name, name),
                      call.get("instructions", ""), call.get("options", {})])
        ordinal = occurrences.get(key, 0)
        occurrences[key] = ordinal + 1
        return "task_" + digest([key, ordinal])[:24]

    def task_call(call, phase, context, fallback=None):
        if "stage" in call:
            raise ValueError("reviewer selection must use read-only review adapters")
        name = text(call.get("task"), "selected task")
        adapter_name = name if name in adapters else ADAPTER_ALIASES.get(name, name)
        if adapter_name in adapters:
            adapter = adapters[adapter_name]
            if not isinstance(adapter, dict) or set(adapter) - {"kind", "writes", "assignment", "definition", "checklist", "resources", "history", "head_sensitive", "cacheable"}:
                raise ValueError("unsupported adapter fields")
        else:
            raise ValueError("unavailable selected adapter: " + name)
        kind = adapter.get("kind", fallback or "task")
        if kind not in {"review", "inspection", "decisions", "fix", "scope", "description", "cleanup", "synchronization", "reply", "task"}:
            raise ValueError("unsupported adapter kind")
        if (kind == "reply") != (context == "responses"):
            raise ValueError("reply adapters run only inside the response stage")
        role = adapter.get("definition", "reviewer" if kind == "review" else "leaf")
        if role not in definitions:
            raise ValueError("adapter references undeclared native definition")
        task = {"id": occurrence(phase, context, name, call), "skill": name, "kind": kind,
                "definition": role, "definition_digest": hashlib.sha256(definitions[role]).hexdigest(),
                "assignment": text(adapter.get("assignment"), "adapter assignment") + "\n" + call.get("instructions", ""),
                "required": call.get("required", True), "writes": bool(adapter.get("writes", False)),
                "checklist": adapter.get("checklist", CHECKLIST), "options": call.get("options", {}),
                "resources": adapter.get("resources", []), "history": bool(adapter.get("history", kind == "cleanup")),
                "head_sensitive": bool(adapter.get("head_sensitive", False)), "cacheable": bool(adapter.get("cacheable", True))}
        if type(task["required"]) is not bool or not isinstance(task["options"], dict):
            raise ValueError("invalid task controls")
        if not task["checklist"] or len(task["checklist"]) > 32 or len({item["id"] for item in task["checklist"]}) != len(task["checklist"]):
            raise ValueError("invalid task checklist")
        if task["writes"] and kind == "review":
            raise ValueError("reviewers cannot write")
        if task["writes"] and kind == "reply":
            raise ValueError("the response stage cannot write")
        if task["writes"] and mode == "review":
            raise ValueError("incoming review tasks cannot write")
        tasks.append(task)
        return task["id"]

    responder = []
    selections = [("tasks", options.get("tasks", [])),
                  ("finalizers", options.get("finalizers", []))]
    for phase, selected in selections:
        for group in _groups(selected):
            if len(group) > 1 and any("stage" in call for call in group):
                raise ValueError("managed publication/review/CI stages cannot be in a parallel group")
            stage = group[0].get("stage")
            if mode == "review" and stage in {"publication", "responses", "ci"}:
                # Verified findings publish after the cycles (main.yml findings job); feedback
                # replies and CI approval (#35) are not incoming-review stages.
                raise ValueError("incoming review supports task and reviewer stages only")
            if stage in {"publication", "ci"}:
                stages.append({"kind": stage, "phase": phase})
                continue
            if stage == "responses":
                if not responder:
                    responder.append(task_call({"task": RESPONDER}, "responses", "responses", "reply"))
                stages.append({"kind": "responses", "phase": phase, "task": responder[0]})
                continue
            if stage == "reviews":
                reviewers = group[0].get("reviewers", options.get("reviewers", []))
                if not reviewers:
                    raise ValueError("selected reviewer stage requires explicit reviewers")
                groups = []
                for reviewer_group in _groups(reviewers):
                    keys = [task_call(call, phase, "reviews", "review") for call in reviewer_group]
                    if any(next(t for t in tasks if t["id"] == key)["kind"] != "review" for key in keys):
                        raise ValueError("reviewer selection must use read-only review adapters")
                    cap = limits["max_parallel_reviewers"] or len(keys)
                    groups.append({"id": "group_" + str(len(groups) + 1),
                                   "tasks": [{"id": key} for key in keys], "concurrency": min(len(keys), cap)})
                stages.append({"kind": "reviews", "groups": groups, "review": True, "fix": mode == "fix", "phase": phase})
                continue
            keys = [task_call(call, phase, "tasks") for call in group]
            if len(keys) > 1 and any(next(t for t in tasks if t["id"] == key)["writes"] for key in keys):
                raise ValueError("parallel writing tasks share a Git index and are unsupported")
            stages.append({"kind": "tasks", "phase": phase,
                           "groups": [{"id": "group_1", "tasks": [{"id": key} for key in keys], "concurrency": len(keys)}],
                           "review": any(next(t for t in tasks if t["id"] == key)["kind"] == "review" for key in keys),
                           "fix": mode == "fix"})

    if mode == "review" and not any(stage["kind"] == "reviews" for stage in stages):
        raise ValueError("incoming review requires a selected reviewer stage")
    if any(task["kind"] == "review" for task in tasks):
        # Incoming review verifies findings independently and never fixes them.
        for key, kind, role, assignment in [
            ("finding_verification", "verification", "verifier", "Independently verify every retained original finding. Preserve exact finding_id and report source; return dispositions confirmed/rejected with concrete source evidence and reason. Do not edit or publish."),
            *([] if mode == "review" else [("finding_fix", "fix", "fixer", "Address independently confirmed findings. Return every finding_id disposition fixed/accepted/deferred/rejected, current evidence IDs and exact changed-file hashes/commit_message. Preserve original report/source references in deliverables. Accepted/deferred dispositions require retained exact user authority. Do not publish or commit.")])]:
            tasks.append({"id": key, "skill": key, "kind": kind, "definition": role,
                "definition_digest": hashlib.sha256(definitions[role]).hexdigest(), "assignment": assignment,
                "required": True, "writes": kind == "fix", "checklist": CHECKLIST,
                "options": {}, "resources": [], "history": False, "head_sensitive": False, "cacheable": True})
    if any(stage["kind"] == "ci" for stage in stages) and limits["ci_repairs_per_cycle"] > 0:
        # Without a repair allowance (cf-workflows:dk-ci observes only) no repair session exists.
        tasks.append({"id": "ci_repair", "skill": "ci_repair", "kind": "fix", "definition": "fixer",
            "definition_digest": hashlib.sha256(definitions["fixer"]).hexdigest(),
            "assignment": "Repair only the exact evidenced failed CI head/run/job/attempt in this assignment. Inspect original logs and relevant tests. Return source evidence, complete changed-file manifest and native commit message. Do not publish or commit; do not reinterpret unknown failures as flakes.",
            "required": False, "writes": True, "checklist": CHECKLIST, "options": {}, "resources": [],
            "history": False, "head_sensitive": True, "cacheable": False})
    plan = {"format": FORMAT, "mode": mode, "logical_run": logical_run, "pr": pr, "instructions": instructions,
            "options": options, "settings": controls.get("settings", {}),
            "runtime": {"execution_fingerprint": execution_fingerprint}, "execution_fingerprint": execution_fingerprint, "turns": turns,
            "authorizations": authorizations, "limits": limits, "tasks": tasks, "stages": stages,
            "resources": {}, "definitions": {key: hashlib.sha256(value).hexdigest() for key, value in definitions.items()},
            # Ordered by stable occurrence, so reordering reviewers keeps the round.
            "review_contract": digest(sorted((task for task in tasks if task["kind"] in {"review", "verification"}),
                                             key=lambda task: task["id"]))}
    source = Path(__file__).resolve().parent.parent
    plan["helper_manifest"] = _helper_manifest(source)
    for task in tasks:
        task["implementation"] = digest(plan["helper_manifest"])
        task["runtime_fingerprint"] = execution_fingerprint
    if options.get("before_cycle") or options.get("after_cycle"):
        raise ValueError("portable cycle hooks require explicit task placement; legacy hook stores are not imported")
    from .capture import resource_digests
    plan["resources"] = resource_digests(root, controls.get("resources", []))
    if any(name not in plan["resources"] for task in tasks for name in task["resources"]):
        raise ValueError("adapter resource is undeclared")
    for number, stage in enumerate(stages, 1):
        stage["id"] = "stage_" + str(number)
    # Bound selected plan size separately from the runtime worker budget.
    # Conditional CI repairs and publication audiences do not reserve workers here.
    selection_weight_per_cycle = sum(sum(len(group["tasks"]) for group in stage["groups"]) + (2 if stage["review"] else 0)
                           if stage["kind"] in {"tasks", "reviews"} else
                           1 if stage["kind"] == "ci" else 0 for stage in stages) + len(responder)
    selection_weight = selection_weight_per_cycle * limits["review_cycles"] + (1 if "merge" in authorizations else 0)
    if len(tasks) > 40 or selection_weight > 64:
        raise ValueError("portable plan exceeds bounded selection size")
    if any(task["writes"] for task in tasks):
        _validate_commit_identity(root)
    from .capture import GitHub, capture
    provider = provider or GitHub(root)
    plan["snapshot"] = capture(root, provider, plan, clean=True)
    plan["fingerprint"] = digest(plan)
    plan_reference = artifact(root, plan)
    return {"logical_run": logical_run, "plan": plan_reference, "stages": stages,
            "limits": limits, "turns": turns, "merge_authorized": "merge" in authorizations,
            # Incoming review's verified findings; publication still rechecks both at dispatch.
            "findings_authorized": mode == "review" and "post_findings" in authorizations
                                   and permissions.get("post_findings") is True,
            # A separately authorized approval of a current clean review; approval_proposal rechecks it.
            "approval_authorized": mode == "review" and "approve_pr" in authorizations
                                   and permissions.get("approve_pr") is True,
            # A separately authorized fork workflow approval (#35); never implied by the two above or merge.
            "fork_ci_authorized": mode == "review" and "approve_fork_ci" in authorizations
                                  and permissions.get("approve_fork_ci") is True}
