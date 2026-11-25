"""The feedback response stage: settle replies after fixes publish, before review.

dk-workflows#255: independent review could block finalization after fixes were
published, leaving the original feedback unanswered. The response stage runs in
the selected plan after the fix publication and before the first review stage.
Its read-only responder occurrence assesses every feedback source and drafts
replies. This module turns that accepted report into exact reply intents, records
each reply once it is observed, and reports the stage status from the retained
feedback record (``feedback.py``).

Nothing here dispatches a write. Replies and thread resolutions are ordinary
proposals that ``publishing.py`` prepares and dispatches through the exact-intent
boundary, so an uncertain reply is reconciled read-only and never resent.
"""
from __future__ import annotations

import json

from . import feedback
from .core import bounded_json, digest, state_directory, text

REPLY_KINDS = ("reply", "reply_inline")
DRAFT_FIELDS = {"assessments", "replies"}


def responder(plan):
    """The plan's one responder occurrence, or None when no response stage is selected."""
    tasks = [task for task in plan["tasks"] if task["kind"] == "reply"]
    stages = [stage for stage in plan.get("stages", []) if stage["kind"] == "responses"]
    if len(tasks) > 1 or bool(tasks) != bool(stages) or any(stage.get("task") != tasks[0]["id"] for stage in stages):
        raise ValueError("response stages require exactly one shared responder occurrence")
    return tasks[0] if tasks else None


def permissions(plan):
    """Effective admitted permissions in the feedback model: enabled input plus explicit authorization."""
    enabled = plan.get("options", {}).get("permissions", {})
    granted = plan.get("authorizations", {})
    return {"reply": {"effective": enabled.get("reply_to_feedback") is True and "reply" in granted},
            "resolve_thread": {"effective": enabled.get("resolve_threads") is True and "resolve_thread" in granted}}


def drafts(report):
    """The responder deliverable: assessments and one drafted reply body per source."""
    deliverables = report.get("deliverables") if isinstance(report, dict) else None
    value = deliverables.get("feedback") if isinstance(deliverables, dict) else None
    feedback.require(isinstance(value, dict) and set(value) == DRAFT_FIELDS,
                     "responder deliverables.feedback requires exactly assessments and replies")
    feedback.require(isinstance(value["assessments"], list) and isinstance(value["replies"], list),
                     "responder assessments and replies must be arrays")
    bodies = {}
    for reply in value["replies"]:
        feedback.require(isinstance(reply, dict) and set(reply) == {"source", "body"},
                         "a drafted reply requires exactly source and body")
        key = feedback._text(reply["source"], "drafted reply source")
        feedback.require(key not in bodies, "duplicate drafted reply for " + key)
        bodies[key] = feedback._text(reply["body"], "drafted reply body")
    return value["assessments"], bodies


def _record(root, plan, *, read_only):
    path = feedback.ledger_path(state_directory(root), plan["logical_run"])
    if read_only and not path.is_file():
        return feedback.Unrecorded()
    return feedback.Ledger(path, logical_run=plan["logical_run"], pr=plan["pr"], read_only=read_only)


def _observe(provider, plan):
    """One capture: the raw evidence (for exact publication sources) and its normalized view."""
    evidence = provider.capture(plan["pr"])
    return evidence, feedback.View(plan["pr"], evidence)


def accept(root, provider, plan, report):
    """Retain a responder report's assessments at acceptance.

    Raises FeedbackError, which acceptance turns into a bounded same-session
    correction, when an assessment is invalid or drops an obligation, a current
    source stays unassessed, or a source that still needs an answer has no draft.
    """
    assessments, bodies = drafts(report)
    _, view = _observe(provider, plan)
    record = _record(root, plan, read_only=False)
    try:
        for assessment in assessments:
            record.assess(view, assessment)
        result = feedback.stage(record, view, permissions(plan))
    finally:
        record.close()
    feedback.require(not result["unassessed"], "responder left feedback sources unassessed: "
                     + ", ".join(result["unassessed"]))
    missing = [key for key in result["replies_needed"] if key not in bodies]
    feedback.require(not missing, "responder drafted no reply for: " + ", ".join(missing))
    unknown = sorted(set(bodies) - set(view.sources))
    feedback.require(not unknown, "drafted replies name no current feedback source: " + ", ".join(unknown))


def _intents(ledger, plan, states):
    marks = ",".join("?" for _ in states)
    for row in ledger.db.execute(f"SELECT id,value,state,receipt FROM publication_intents WHERE state IN ({marks}) "
                                 "ORDER BY rowid", tuple(states)):
        intent = json.loads(row[1])
        if intent.get("run") == plan["logical_run"] and intent.get("kind") in REPLY_KINDS + ("resolve_thread",):
            yield {"id": row[0], "intent": intent, "state": row[2],
                   "receipt": json.loads(row[3]) if row[3] else None}


def _reply(plan, snapshot, evidence, view, key, body, reference):
    source = view.sources[key]
    collection = feedback.COLLECTIONS[source["kind"]]
    raw = [item for item in evidence[collection] if item.get("id") == source["id"]]
    if source["kind"] == "inline":
        kind, payload = "reply_inline", {"body": body, "comment_id": source["id"]}
    else:
        # A PR comment answers a comment or review body only when it links the exact source.
        url = text(source["url"], "feedback source URL")
        kind, payload = "reply", {"body": body if url in body else url + "\n\n" + body}
    return {"id": "response_" + digest([plan["logical_run"], key, feedback.View.fingerprint(source), payload])[:48],
            "kind": kind, "head": snapshot["head"], "target": snapshot["target"], "payload": payload,
            "sources": [reference],
            "source": {"kind": source["kind"], "id": source["id"], "fingerprint": digest(raw[0])}}


def _dispatched(ledger, plan, source):
    """This run's dispatched reply to the exact source version, if any: pending or applied."""
    for saved in _intents(ledger, plan, ("pending", "applied")):
        intent = saved["intent"]
        if intent["kind"] in REPLY_KINDS and intent.get("source") == source:
            return {"id": saved["id"], **{key: intent.get(key) for key in
                                          ("kind", "head", "lease", "target", "payload", "sources", "source")}}
    return None


def proposals(root, provider, ledger, plan, snapshot):
    """Exact reply and resolution proposals for the current discussion; never a write.

    Only sources that still need an answer get a reply, so settled replies are
    never repeated. A source that already has a dispatched reply to its exact
    version (pending, or applied but not yet recorded) gets no new proposal: the
    retained original goes to ``reconcile``, which publication only reconciles
    read-only, even after the responder report that proposed it went stale.
    """
    from . import app
    task = responder(plan)
    result = {"items": [], "reconcile": [], "blockers": []}
    if task is None:
        return result
    if snapshot["local_head"] != snapshot["head"] or snapshot.get("dirty"):
        result["blockers"].append("publish the accepted fixes before replying to feedback")
        return result
    current = app._current(ledger, snapshot, task)
    if current is None:
        result["blockers"].append("the responder has no current accepted report")
        return result
    row = ledger.db.execute("SELECT report FROM accepted_reports WHERE reference=?", (current["report"],)).fetchone()
    _, bodies = drafts(bounded_json(row["report"]))
    evidence, view = _observe(provider, plan)
    allowed = permissions(plan)
    record = _record(root, plan, read_only=True)
    try:
        status = feedback.stage(record, view, allowed)
        result["blockers"].extend(status["blockers"])
        if status["status"] == "pending":
            for key in status["replies_needed"]:
                if key not in bodies:
                    result["blockers"].append("no drafted reply for " + key)
                    continue
                proposal = _reply(plan, snapshot, evidence, view, key, bodies[key], current["report"])
                original = _dispatched(ledger, plan, proposal["source"])
                if original is None:
                    result["items"].append(proposal)
                elif record.recorded(original["id"]):
                    # Its observed reply was later removed. Never answer again automatically.
                    result["blockers"].append(f"the recorded reply {original['id']} to {key} is no longer observable")
                else:
                    result["reconcile"].append(original)
        # Resolve a thread only after its own answer from this run is applied and
        # feedback.resolution() allows it: authority, fully answered, nobody awaited.
        answered = {}
        for saved in _intents(ledger, plan, ("applied",)):
            if saved["intent"]["kind"] == "reply_inline":
                answered.setdefault(saved["intent"]["payload"]["comment_id"], saved["id"])
        for ident, thread in sorted(view.threads.items()):
            if thread["root"] not in answered:
                continue
            if feedback.resolution(record, view, ident, allowed)["allowed"]:
                result["items"].append({
                    "id": "resolution_" + digest([plan["logical_run"], ident, answered[thread["root"]]])[:48],
                    "kind": "resolve_thread", "head": snapshot["head"], "target": snapshot["target"],
                    "payload": {"thread_id": ident, "reply_operation": answered[thread["root"]]},
                    "sources": [current["report"]]})
    finally:
        record.close()
    return result


def settle(root, provider, ledger, plan, inputs):
    """Record every applied reply of this run once it is observed; then report the stage."""
    if responder(plan) is None:
        return status(root, provider, ledger, plan)
    _, view = _observe(provider, plan)
    record = _record(root, plan, read_only=False)
    try:
        for saved in _intents(ledger, plan, ("applied",)):
            intent, observed = saved["intent"], (saved["receipt"] or {}).get("result") or {}
            source = intent.get("source")
            if intent["kind"] not in REPLY_KINDS or not isinstance(source, dict):
                continue
            key = f"{source['kind']}:{source['id']}"
            reply = {"kind": "inline" if intent["kind"] == "reply_inline" else "comment", "id": observed.get("id")}
            if key not in view.sources:
                continue
            try:
                record.record_reply(view, key, reply, saved["id"])
            except feedback.FeedbackError:
                # An edited source owes a new answer; an unobservable reply never counts.
                continue
    finally:
        record.close()
    return status(root, provider, ledger, plan)


def status(root, provider, ledger, plan, inputs=None):
    """Response-stage status. Read-only; an unselected stage never reports complete."""
    if responder(plan) is None:
        return {"selected": False, "status": "not_selected", "replies_needed": [], "unassessed": [],
                "unanswered": [], "uncertain": [],
                "blockers": ["no response stage is selected; feedback replies are not settled by this plan"]}
    _, view = _observe(provider, plan)
    record = _record(root, plan, read_only=True)
    try:
        result = feedback.stage(record, view, permissions(plan))
    finally:
        record.close()
    uncertain = [saved["id"] for saved in _intents(ledger, plan, ("pending",))]
    blockers = list(result["blockers"]) + ["reply outcome is uncertain; reconcile " + ident for ident in uncertain]
    return {"selected": True, "status": result["status"], "replies_needed": result["replies_needed"],
            "unassessed": result["unassessed"], "unanswered": result["unanswered"], "uncertain": uncertain,
            "blockers": blockers}
