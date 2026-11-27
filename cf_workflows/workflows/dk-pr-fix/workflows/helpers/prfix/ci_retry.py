"""CI failure diagnosis and bounded automatic reruns (#35).

Authored for cf-workflows; the methodology and role prompt follow dk-workflows'
CI recovery (MIT; see licenses/dk-workflows-MIT.txt in cf-workflows). No helper
here makes a semantic diagnosis or dispatches a request.

A maintained PR (fix mode) whose plan selects the CI stage and carries an
explicit ``rerun_ci`` authorization with the enabled ``rerun_ci`` permission
gets one more fixed occurrence, ``ci_diagnosis`` (prepare.py). It runs on the
admitted ``leaf`` role with the baseline ``failure-diagnosis`` role prompt and
no skill, after an exact-head observation fails (ci-pass.yml ``diagnose``):

- ``context`` (claim) reads, for the latest failed observation of the current
  head, every failed Actions run attempt: the run, its complete job inventory
  (failed jobs that no displayed check names included) and each failed job's
  bounded failed-step logs. Runs that cannot be read are listed as blocked, and
  failed checks that are not Actions jobs as unsupported; both stay failed. The
  handoff carries a ``capture`` fingerprint. When nothing can be diagnosed (no
  readable failed Actions attempt, too many runs or oversized evidence), the
  claim settles the optional occurrence as failed without a session
  (app.py), so recovery is skipped and the failure reaches ``ci_repair``.
- ``check`` (accept) holds the session to that evidence. Every listed run and
  failed job gets exactly one verdict (``unrelated``, ``caused_by_pr`` or
  ``ambiguous``) with a reason and a comparison with the PR's changes. Only an
  unrelated failure may be retryable, and only with excerpts that occur
  verbatim in that job's own failed-step logs. The session proposes no
  operation. A violation is a same-session report-contract correction.
- ``listing`` (publication-barrier.yml with selector ``ci_rerun``) turns the
  current accepted diagnosis into one ``ci_rerun`` proposal per run whose every
  failed job is unrelated and retryable. A PR-caused or ambiguous job keeps its
  whole run for repair; an independent eligible run still proceeds. Each
  proposal cites the diagnosis report, so a newer observation, head or input
  makes it stale before dispatch. Unsettled reruns are listed first, for
  read-only reconciliation only.

The publisher (publication.py) rechecks that the exact attempt is still the
failed one and reserves the retained per-head automatic allowance
(``ci-reruns:<head>``, ``limits.ci_reruns_per_head``) once per exact attempt. A
user's explicit retry of specified failures (``user_retry`` in a dk-pr-publish
``ci_rerun`` proposal) is bound to one exact attempt per run and request
identity and never consumes or resets the automatic allowance.
"""
from __future__ import annotations

from .core import bounded_json, canonical, digest, read_artifact, text

TASK = "ci_diagnosis"
# The baseline role prompt (claude/skills/dk-ci/prompts/failure-diagnosis.md at the pinned
# dk-workflows commit); scripts/check_baselines.py verifies these bytes against that object.
BASELINE_PROMPT = "claude/skills/dk-ci/prompts/failure-diagnosis.md"
ROLE_PROMPT = ("Diagnose each failed workflow and job from original current-head logs, source changes and governing "
               "evidence. Distinguish PR-caused, unrelated retryable and uncertain failures with concrete support.\n")
ROLE_PROMPT_SHA256 = "fb0f987efc56fa2a44e5daa7723e421c94bcfcacd478beccf33be5a71f9a9746"
CONTRACT = (
    "Use only task_input.ci_diagnosis: every failed run attempt, its failed jobs and their failed-step logs at the "
    "current head. Compare each failure with the actual PR changes; an error signature alone is not enough, because a "
    "changed dependency, host name, package source or workflow can cause the same error. Return deliverables as a JSON "
    "object whose only member \"diagnosis\" is {capture, runs: [{run_id, attempt, jobs: [{job_id, verdict, retryable, "
    "reason, change_comparison, log_evidence}]}]}. capture is task_input.ci_diagnosis.capture. Give every listed run "
    "and every failed job exactly one entry. verdict is unrelated, caused_by_pr or ambiguous; only an unrelated failure "
    "may be retryable, and then log_evidence lists exact excerpts of that job's own logs. Explain missing evidence as "
    "ambiguous. Propose no operations; the workflow prepares reruns. Do not edit, commit or publish.")
ASSIGNMENT = ROLE_PROMPT + CONTRACT
VERDICTS = ("unrelated", "caused_by_pr", "ambiguous")
SUCCESS = ("success", "neutral", "skipped")
MAX_RUNS = 20
MAX_JOBS = 100  # One page of an attempt's jobs; a longer inventory blocks that run.
LOG_LINES = 200
LOG_BYTES = 16 << 10
MAX_HANDOFF = 640 << 10
MAX_TEXT = 2000
MAX_EXCERPT = 500
# Evidence failures that block one run or job; anything else propagates.
EVIDENCE_ERRORS = (ValueError, KeyError, TypeError, OSError, RuntimeError)


class Blocked(ValueError):
    """The diagnosis or its evidence cannot be used."""


class Undiagnosable(Blocked):
    """The current failed observation has no failed Actions attempt that can be diagnosed."""


def role(task):
    return isinstance(task, dict) and task.get("id") == TASK


def occurrence(definition_digest):
    """The plan's fixed read-only diagnosis occurrence (prepare.py)."""
    return {"id": TASK, "skill": TASK, "kind": "task", "definition": "leaf",
            "definition_digest": definition_digest, "assignment": ASSIGNMENT,
            "required": False, "writes": False, "options": {}, "resources": [],
            "history": False, "head_sensitive": True, "cacheable": False}


def latest(ledger):
    row = ledger.db.execute("SELECT * FROM ci_observations ORDER BY rowid DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def binding(ledger):
    """The latest observation a diagnosis covers; a newer one makes it stale."""
    row = latest(ledger)
    return None if row is None else digest([row["operation"], row["head"], row["status"], row["result"]])


def _bounded_logs(value):
    if not isinstance(value, str):
        raise ValueError("failed-step logs unavailable")
    lines = value.splitlines()[-LOG_LINES:]
    result = "\n".join(lines)
    encoded = result.encode("utf-8")
    if len(encoded) > LOG_BYTES:
        result = encoded[-LOG_BYTES:].decode("utf-8", errors="ignore")
    return result


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(label + " unavailable")
    return value


def context(root, provider, ledger, plan, snapshot):
    """The read-only diagnosis handoff for the latest failed observation of the current head."""
    observed = latest(ledger)
    head = snapshot["head"]
    if not observed or observed["head"] != head or observed["status"] != "failed":
        raise Blocked("CI diagnosis requires the current exact-head failed observation")
    checks = bounded_json(observed["result"]).get("checks")
    if not isinstance(checks, list):
        raise Blocked("observed CI inventory unavailable")
    pr = plan["pr"]
    prefix = f"repos/{pr['owner']}/{pr['name']}"
    attempts, unsupported = {}, []
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "completed" or check.get("conclusion") in SUCCESS:
            continue
        run_id, attempt = str(check.get("run_id", "")), check.get("attempt")
        if not run_id.isdigit() or int(run_id) < 1 or type(attempt) is not int or attempt < 1:
            unsupported.append({"check_id": str(check.get("id", "")), "name": str(check.get("name", "")),
                                "reason": "not an Actions job; it cannot be rerun"})
            continue
        attempts.setdefault((int(run_id), attempt), []).append(str(check.get("id", "")))
    if len(attempts) > MAX_RUNS:
        raise Undiagnosable("too many failed workflow runs to diagnose at once")
    runs, blocked = [], []
    for (run_id, attempt), named in sorted(attempts.items()):
        try:
            run = provider.request("GET", f"{prefix}/actions/runs/{run_id}")
            if (not isinstance(run, dict) or run.get("id") != run_id or run.get("head_sha") != head
                    or run.get("run_attempt") != attempt or run.get("status") != "completed"
                    or run.get("conclusion") in SUCCESS):
                raise Blocked("workflow run is no longer this failed exact attempt")
            inventory = provider.request("GET", f"{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
            listed = inventory.get("jobs") if isinstance(inventory, dict) else None
            total = inventory.get("total_count") if isinstance(inventory, dict) else None
            if (not isinstance(listed, list) or type(total) is not int or total != len(listed)
                    or total > MAX_JOBS or any(not isinstance(job, dict) for job in listed)):
                raise Blocked("job inventory of the exact attempt is incomplete")
            failed = [job for job in listed if job.get("status") == "completed"
                      and job.get("conclusion") not in SUCCESS]
            if not failed:
                raise Blocked("the failed attempt lists no failed job")
            jobs = []
            for job in sorted(failed, key=lambda item: item.get("id") if type(item.get("id")) is int else 0):
                ident = _positive(job.get("id"), "Actions job ID")
                if job.get("run_id") != run_id or job.get("run_attempt") != attempt or job.get("head_sha") != head:
                    raise Blocked("job does not belong to the exact run attempt")
                entry = {"job_id": ident, "name": text(job.get("name"), "Actions job name"),
                         "conclusion": str(job.get("conclusion")), "logs": None, "gap": None}
                try:
                    entry["logs"] = _bounded_logs(provider.failure_logs(pr, str(run_id), str(ident), attempt))
                except EVIDENCE_ERRORS as error:
                    entry["gap"] = str(error)[:500] or type(error).__name__
                jobs.append(entry)
            runs.append({"run_id": run_id, "attempt": attempt, "workflow": str(run.get("path", "")).split("@", 1)[0],
                         "name": str(run.get("name", "")), "checks": sorted(named), "jobs": jobs})
        except EVIDENCE_ERRORS as error:
            blocked.append({"run_id": run_id, "attempt": attempt, "reason": str(error)[:500] or type(error).__name__})
    if not runs:
        raise Undiagnosable("no failed Actions attempt of the current head can be diagnosed: "
                      + "; ".join(item["reason"] for item in blocked + unsupported))
    handoff = {"head": head, "target": snapshot["target"], "runs": runs, "blocked": blocked,
               "unsupported": unsupported}
    handoff["capture"] = digest(handoff)
    if len(canonical(handoff)) > MAX_HANDOFF:
        raise Undiagnosable("CI diagnosis evidence exceeds its bound")
    return handoff


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT or "\x00" in value:
        raise Blocked(label + " must be bounded nonempty text")
    return value


def check(report, assignment):
    """The validated diagnosis of exactly the handed-off runs and jobs, or Blocked."""
    handoff = assignment.get("ci_diagnosis")
    if not isinstance(handoff, dict):
        raise Blocked("the assignment carries no CI diagnosis evidence")
    if report.get("operations"):
        raise Blocked("a diagnosis proposes no operations; the workflow prepares reruns")
    deliverables = report.get("deliverables")
    if not isinstance(deliverables, dict) or set(deliverables) != {"diagnosis"}:
        raise Blocked("return the diagnosis as deliverables.diagnosis only")
    diagnosis = deliverables["diagnosis"]
    if not isinstance(diagnosis, dict) or set(diagnosis) != {"capture", "runs"}:
        raise Blocked("diagnosis is exactly {capture, runs}")
    if diagnosis["capture"] != handoff["capture"]:
        raise Blocked("diagnosis capture differs from the handed-off evidence")
    expected = {(run["run_id"], run["attempt"]): run for run in handoff["runs"]}
    if not isinstance(diagnosis["runs"], list):
        raise Blocked("diagnosis runs must be an array")
    seen = set()
    for run in diagnosis["runs"]:
        if not isinstance(run, dict) or set(run) != {"run_id", "attempt", "jobs"}:
            raise Blocked("each run is exactly {run_id, attempt, jobs}")
        key = (run["run_id"], run["attempt"])
        if key not in expected or key in seen:
            raise Blocked(f"run {run['run_id']} attempt {run['attempt']} is not one handed-off failed attempt")
        seen.add(key)
        logs = {job["job_id"]: job["logs"] for job in expected[key]["jobs"]}
        if not isinstance(run["jobs"], list):
            raise Blocked("run jobs must be an array")
        named = []
        for job in run["jobs"]:
            if not isinstance(job, dict) or set(job) != {"job_id", "verdict", "retryable", "reason",
                                                          "change_comparison", "log_evidence"}:
                raise Blocked("each job is exactly {job_id, verdict, retryable, reason, change_comparison, "
                              "log_evidence}")
            where = f"run {run['run_id']} job {job['job_id']}"
            if job["job_id"] not in logs or job["job_id"] in named:
                raise Blocked(where + " is not one handed-off failed job")
            named.append(job["job_id"])
            if job["verdict"] not in VERDICTS or type(job["retryable"]) is not bool:
                raise Blocked(where + ": verdict is unrelated, caused_by_pr or ambiguous with boolean retryable")
            _text(job["reason"], where + " reason")
            _text(job["change_comparison"], where + " change_comparison")
            excerpts = job["log_evidence"]
            if not isinstance(excerpts, list) or len(excerpts) > 20:
                raise Blocked(where + ": log_evidence is a bounded array of excerpts")
            if job["retryable"] and job["verdict"] != "unrelated":
                raise Blocked(where + ": only an unrelated failure is retryable")
            if job["retryable"] and logs[job["job_id"]] is None:
                raise Blocked(where + ": its failed-step logs are unavailable, so it cannot be retryable")
            if job["retryable"] and not excerpts:
                raise Blocked(where + ": an unrelated retryable failure needs its own log excerpts")
            for excerpt in excerpts:
                if (not isinstance(excerpt, str) or not excerpt.strip() or len(excerpt) > MAX_EXCERPT
                        or logs[job["job_id"]] is None or excerpt not in logs[job["job_id"]]):
                    raise Blocked(where + ": every excerpt must occur verbatim in that job's own failed-step logs")
        if sorted(named) != sorted(logs):
            raise Blocked(f"run {run['run_id']} attempt {run['attempt']} omits a failed job")
    if seen != set(expected):
        raise Blocked("diagnosis omits a handed-off failed run")
    return diagnosis


def retryable(run):
    return bool(run["jobs"]) and all(job["verdict"] == "unrelated" and job["retryable"] for job in run["jobs"])


def proposals(root, provider, ledger, plan, snapshot):
    """One ci_rerun proposal per run of the current diagnosis whose every failed job is unrelated and retryable."""
    from .app import _current, _task
    from .core import auth
    empty = {"items": [], "rerun": False}
    authority = plan.get("authorizations", {}).get("rerun_ci")
    if authority is None:
        return {**empty, "blockers": ["automatic CI recovery needs its own explicit rerun_ci authorization"]}
    auth(authority)
    if plan.get("options", {}).get("permissions", {}).get("rerun_ci") is not True:
        return {**empty, "blockers": ["configured permission forbids ci_rerun"]}
    try:
        task = _task(plan, TASK)
    except ValueError:
        return {**empty, "blockers": ["the plan has no CI failure diagnosis"]}
    claim = _current(ledger, snapshot, task)
    if claim is None:
        return {**empty, "blockers": ["no current CI failure diagnosis of the latest exact-head observation"]}
    row = ledger.db.execute("SELECT report FROM accepted_reports WHERE reference=?", (claim["report"],)).fetchone()
    if row is None:
        return {**empty, "blockers": ["the CI failure diagnosis is not an accepted report"]}
    handoff = read_artifact(root, claim["assignment"])["ci_diagnosis"]
    diagnosis = bounded_json(row["report"])["deliverables"]["diagnosis"]
    head = snapshot["head"]
    if handoff["head"] != head:
        return {**empty, "blockers": ["the CI failure diagnosis belongs to another head"]}
    limit = plan.get("options", {}).get("limits", {}).get("ci_reruns_per_head", 3)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 3:
        # The publisher (_reserve_rerun) refuses the same value; list no allowance it would refuse.
        return {**empty, "blockers": ["invalid retained per-head CI rerun allowance"]}
    name = "ci-reruns:" + head
    reserved = {value[0] for value in ledger.db.execute("SELECT operation FROM reservations WHERE name=?", (name,))}
    first = ledger.db.execute("SELECT limit_value FROM reservations WHERE name=? ORDER BY rowid LIMIT 1",
                              (name,)).fetchone()
    ceiling = (min(limit, first[0]) if first else limit) + ledger.granted(name)
    items, blockers, pending = [], [], 0
    for run in sorted(diagnosis["runs"], key=lambda value: (value["run_id"], value["attempt"])):
        where = f"workflow run {run['run_id']} attempt {run['attempt']}"
        if not retryable(run):
            caused = any(job["verdict"] == "caused_by_pr" for job in run["jobs"])
            blockers.append(where + (": a PR-caused failure needs repair" if caused else
                                     ": an ambiguous or non-retryable failure stays failed"))
            continue
        key = f"run:{run['run_id']}:{run['attempt']}"
        if key not in reserved and len(reserved) + pending >= ceiling:
            blockers.append(where + ": the automatic rerun allowance of this head is exhausted; "
                                    "a bounded explicit user retry is required")
            continue
        pending += key not in reserved
        reason = "Unrelated retryable failure: " + "; ".join(job["reason"] for job in run["jobs"])
        items.append({"id": "ci_rerun_" + digest([plan["logical_run"], head, run["run_id"], run["attempt"],
                                                  claim["report"]]),
                      "kind": "ci_rerun", "head": head, "lease": head, "target": snapshot["target"],
                      "payload": {"run_id": run["run_id"], "attempt": run["attempt"], "reason": reason[:1000]},
                      "sources": [claim["report"]]})
    blockers += [f"workflow run {item['run_id']} attempt {item['attempt']}: {item['reason']}"
                 for item in handoff["blocked"]]
    blockers += [f"check {item['name'] or item['check_id']}: {item['reason']}" for item in handoff["unsupported"]]
    return {"items": items, "blockers": blockers, "rerun": bool(items)}


def _retained(saved):
    intent = saved["intent"]
    return {"id": saved["id"], **{key: intent[key] for key in ("kind", "head", "lease", "target", "payload", "sources")}}


def listing(root, provider, ledger, plan, snapshot, publisher):
    """Unsettled reruns first, for read-only reconciliation only; then the current eligible runs."""
    if plan.get("mode") != "fix":
        raise ValueError("CI recovery belongs to the maintained PR; incoming review never reruns CI")
    pending = [publisher._load(row["id"]) for row in
               ledger.db.execute("SELECT id FROM publication_intents WHERE state='pending' ORDER BY rowid")]
    retained = [_retained(saved) for saved in pending if saved["intent"]["kind"] == "ci_rerun"]
    if retained:
        return {"items": retained, "rerun": False, "blockers": ["unsettled CI reruns are reconciled before any other"]}
    return proposals(root, provider, ledger, plan, snapshot)


def automatic(proposal):
    """Whether a proposal claims to come from this procedure (its ID prefix) rather than a native report."""
    return (isinstance(proposal, dict) and proposal.get("kind") == "ci_rerun"
            and str(proposal.get("id", "")).startswith("ci_rerun_"))
