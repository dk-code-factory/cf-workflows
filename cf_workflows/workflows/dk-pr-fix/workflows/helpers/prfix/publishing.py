"""Publication adapters over accepted native reports and application coverage.

Adapted from Agent Workflow's portable PR-fix example (MIT; see LICENSE).

These helpers perform one boundary operation. YAML selects task order, obtains
an independent audience assessment, and schedules each mutation separately.
Native sessions only propose operations; every remote write is an exact intent
dispatched by ``Publication``.
"""
from __future__ import annotations

import copy
import re

from .core import artifact, auth, bounded_json, canonical, digest, read_artifact, text
from .publication import Publication, PublicationError, fork_gate, repository_merge_requirements


_AGENT = re.compile(r"agent_[a-f0-9]{32}")


def _report_row(ledger, reference):
    row = ledger.db.execute("SELECT * FROM accepted_reports WHERE reference=?", (reference,)).fetchone()
    if not row:
        raise PublicationError("publication source is not an accepted original report")
    return dict(row)


def _source(root, provider, ledger, plan, reference, *, snapshot=None, audience_intent=None):
    """Verify immutable artifact bytes and their current application coverage."""
    from . import app
    from .prepare import CHECKLIST
    row = _report_row(ledger, text(reference, "accepted report reference"))
    wrapper = read_artifact(root, reference)
    report = bounded_json(row["report"])
    if wrapper.get("report") != report or wrapper.get("agent") != row["agent"]:
        raise PublicationError("accepted report artifact differs from its retained original")
    if not isinstance(row["agent"], str) or not _AGENT.fullmatch(row["agent"]):
        raise PublicationError("accepted report lacks an opaque native agent identity")
    if row["kind"] == "audience":
        if audience_intent is None:
            raise PublicationError("audience source requires its exact publication intent")
        if row["task"] != "audience:" + audience_intent["id"] or wrapper.get("intent") != audience_intent["id"]:
            raise PublicationError("audience source belongs to another publication")
        if row["fingerprint"] != audience_intent["fingerprint"]:
            raise PublicationError("audience source fingerprint differs")
        if not app.verify_report(root, {"checklist": CHECKLIST}, report):
            raise PublicationError("audience checklist is incomplete")
        verdict = report.get("audience", {})
        if verdict.get("fingerprint") != audience_intent["fingerprint"] or verdict.get("suitable") is not True:
            raise PublicationError("audience verdict does not accept the exact payload")
        return row
    try:
        task = app._task(plan, row["task"])
    except ValueError as exc:
        raise PublicationError("publication source task is outside admitted plan") from exc
    if task["kind"] != row["kind"]:
        raise PublicationError("publication source task kind differs from admitted plan")
    claim = ledger.db.execute("SELECT * FROM claims WHERE id=?", (wrapper.get("claim"),)).fetchone()
    if not claim or claim["task"] != row["task"] or claim["agent"] != row["agent"]:
        raise PublicationError("publication source has no exact original logical claim")
    if claim["state"] != "complete" or claim["report"] != reference or claim["coverage"] != row["fingerprint"]:
        raise PublicationError("publication source is not the accepted final report for its claim")
    snapshot = snapshot if snapshot is not None else app._snapshot(root, provider, plan)
    if row["fingerprint"] != app._fingerprint(snapshot, task, ledger):
        raise PublicationError("publication source no longer covers the current inputs")
    if not app.verify_report(root, task, report):
        raise PublicationError("publication source checklist is incomplete")
    return row


def _publication(root, provider, ledger, plan, *, audience=None):
    def validate_source(reference):
        try:
            _source(root, provider, ledger, plan, reference, audience_intent=audience)
            return True
        except (ValueError, KeyError, TypeError, PublicationError):
            return False
    return Publication(ledger, provider, root, plan,
        readiness=lambda intent: readiness(root, provider, ledger, plan, {"intent": intent}),
        source_validator=validate_source,
        approval=lambda intent: approval(root, provider, ledger, plan, {"intent": intent}),
        fork_ci=lambda intent: fork_ci(root, provider, ledger, plan, {"intent": intent}))


def _current_source(root, provider, ledger, plan, reference, snapshot):
    try:
        _source(root, provider, ledger, plan, reference, snapshot=snapshot)
        return True
    except (ValueError, KeyError, TypeError, PublicationError):
        return False


def publication_list(root, provider, ledger, plan, inputs):
    """List current accepted proposals; never infer authority from selection."""
    from . import app
    snapshot = app._snapshot(root, provider, plan)
    selector = inputs.get("kind", "ordinary")
    if selector not in {"ordinary", "merge", "responses", "all", "findings", "fork_ci"}:
        raise PublicationError("unsupported publication selector")
    if selector == "fork_ci" and plan.get("mode") != "review":
        raise PublicationError("fork workflow approval belongs to incoming review; dk-pr-fix never approves it")
    # Incoming review publishes only its verified findings (and a separately
    # authorized merge or fork approval); native report operations never become its writes.
    if (plan.get("mode") == "review") != (selector == "findings") and selector not in ("merge", "fork_ci"):
        raise PublicationError("verified finding publication belongs to incoming review, which publishes nothing else")
    if not snapshot["identities"]["current"]:
        # Provisional analysis is private: no proposal is listed until the PR
        # metadata reaches the analyzed revision.
        return {"items": [], "provisional": app.provisional(snapshot),
                "blockers": ["publication is withheld while analysis is provisional"]}
    if selector == "fork_ci":
        return _fork_listing(root, provider, ledger, plan)
    if selector == "findings":
        from . import findings
        listed = findings.listing(root, provider, ledger, plan, _publication(root, provider, ledger, plan),
                                  lambda reference, current: _current_source(root, provider, ledger, plan, reference, current))
        # Retained dispatched originals come first, for read-only reconciliation only.
        return {"items": listed["reconcile"] + listed["items"], "withheld": listed["withheld"],
                "blockers": listed["blockers"]}
    if plan.get("mode") == "review":
        return {"items": []}
    from . import responses
    if selector == "responses":
        # Feedback replies and resolutions. Retained dispatched originals come first,
        # for read-only reconciliation only: they hold the lock and are never re-prepared.
        listed = responses.proposals(root, provider, ledger, plan, snapshot)
        return {"items": listed["reconcile"] + listed["items"], "blockers": listed["blockers"]}
    # Replies to feedback sources belong to the selected response stage.
    owned = responses.responder(plan) is not None
    selected, seen = [], {}
    rows = ledger.db.execute("SELECT * FROM accepted_reports ORDER BY rowid").fetchall()
    if selector != "merge" and snapshot["local_head"] != snapshot["head"]:
        # A native writer cannot predict commit-tree's eventual object ID. The
        # controlled commit receipt supplies that identity after acceptance;
        # publication still needs explicit authority and an independent exact
        # audience verdict. Publish it before proposals for the resulting PR.
        permissions = plan.get("options", {}).get("permissions", {})
        authority = plan.get("authorizations", {}).get("push")
        if permissions.get("push_commits") is not True or authority is None:
            return {"items": [], "unpublished": True,
                    "blockers": ["local controlled commits require explicit push authority and configured permission"]}
        auth(authority)
        committed = ledger.db.execute("SELECT * FROM local_commits WHERE head=? AND state='completed' ORDER BY rowid DESC LIMIT 1",
                                      (snapshot["local_head"],)).fetchone()
        if not committed:
            raise PublicationError("unpublished local head has no controlled commit receipt")
        for raw in reversed(rows):
            row = dict(raw)
            if row["kind"] == "audience":
                continue
            wrapper = read_artifact(root, row["reference"])
            if wrapper.get("claim") != committed["id"]:
                continue
            task = app._task(plan, row["task"])
            if not task.get("writes"):
                raise PublicationError("controlled commit source is not an admitted writing task")
            _source(root, provider, ledger, plan, row["reference"], snapshot=snapshot)
            proposal = {"id": "push_" + digest([plan["logical_run"], row["reference"], dict(committed)]),
                        "kind": "push", "head": snapshot["local_head"], "lease": snapshot["head"],
                        "target": snapshot["target"], "payload": {"new_head": snapshot["local_head"]},
                        "sources": [row["reference"]]}
            return {"items": [proposal], "unpublished": True}
        raise PublicationError("controlled commit lacks its accepted native writing report")
    for raw in rows:
        row = dict(raw)
        if row["kind"] == "audience":
            continue
        report = bounded_json(row["report"])
        proposals = report.get("operations", [])
        if not isinstance(proposals, list) or len(proposals) > 64:
            raise PublicationError("native publication proposal list exceeds its bound")
        if not proposals:
            continue
        # A stale report contributes no proposal. Refreshing the owning task is
        # the only way to authorize a payload on changed inputs.
        try:
            _source(root, provider, ledger, plan, row["reference"], snapshot=snapshot)
        except PublicationError:
            continue
        for number, original in enumerate(proposals):
            if not isinstance(original, dict):
                raise PublicationError("native publication proposal requires an object")
            proposal = copy.deepcopy(original)
            if proposal.get("kind") == "approve_pr":
                continue  # Approval comes only from approval_proposal, never from a native report.
            merge = proposal.get("kind") == "merge"
            if selector == "ordinary" and merge or selector == "merge" and not merge:
                continue
            if (selector == "ordinary" and owned and proposal.get("kind") in responses.REPLY_KINDS
                    and proposal.get("source") is not None):
                continue
            sources = proposal.get("sources")
            if sources is None or sources == []:
                proposal["sources"] = [row["reference"]]
            elif not isinstance(sources, list) or not sources:
                raise PublicationError("native publication sources require accepted report references")
            for source in proposal["sources"]:
                _source(root, provider, ledger, plan, source, snapshot=snapshot)
            # IDs bind the original accepted report and proposal coordinates;
            # replaying another graph cannot turn an old intent into new work.
            proposal.setdefault("id", "publication_" + digest([plan["logical_run"], row["reference"], number, original]))
            key = text(proposal["id"], "publication proposal ID")
            fingerprint = digest(proposal)
            if key in seen:
                if seen[key] != fingerprint:
                    raise PublicationError("two accepted reports propose conflicting operation IDs")
                continue
            seen[key] = fingerprint
            selected.append(proposal)
    if selector == "all" and owned:
        for proposal in responses.proposals(root, provider, ledger, plan, snapshot)["items"]:
            if proposal["id"] not in seen:
                seen[proposal["id"]] = digest(proposal)
                selected.append(proposal)
    if selector != "merge":
        # The exact intent of each current authorized draft (describe-pr, draft-issue; drafting.py).
        from . import drafting
        selected = drafting.compose(root, provider, ledger, plan, snapshot, selected)
        # The bounded Decisions section of the current reconciliation, under its own edit_pr authority.
        from . import decisions
        selected = decisions.compose(root, provider, ledger, plan, snapshot, selected)
    return {"items": selected}


def publication_prepare(root, provider, ledger, plan, inputs):
    proposal = inputs.get("proposal")
    if not isinstance(proposal, dict):
        raise PublicationError("publication preparation needs an exact accepted proposal")
    if proposal.get("kind") == "approve_fork_ci":
        return _prepare_fork_ci(root, provider, ledger, plan, proposal)
    # Never accept arbitrary workflow inputs merely because they name an
    # accepted source. Reconstruct the source report's exact original proposal,
    # including the deterministic missing-sources/ID normalization above.
    review = plan.get("mode") == "review"
    listing = publication_list(root, provider, ledger, plan, {"kind": "findings" if review else "all"})
    listed = listing["items"]
    if review and proposal.get("kind") == "submit_review" and _retained_original(root, provider, ledger, plan, proposal):
        # A retained finding submission is never re-created: a dispatched one
        # is only reconciled, and only an identical relisted one is prepared.
        publisher = _publication(root, provider, ledger, plan)
        saved = publisher._attempts(publisher._load(proposal["id"])["id"])[-1]
        if saved["state"] in {"applied", "pending"}:
            from .findings import proposal_of
            if proposal_of(saved) != proposal:
                raise PublicationError("proposal differs from the retained original operation")
            return {**{key: saved[key] for key in ("id", "fingerprint", "intent")}, "invoke": False}
    if listing.get("provisional") and not _retained_original(root, provider, ledger, plan, proposal):
        raise PublicationError("publication is withheld while analysis is provisional")
    if proposal.get("kind") in ("merge", "approve_pr"):
        # Merge and approval come only from their own final helpers, never from a listing.
        final = (merge_proposal if proposal["kind"] == "merge" else approval_proposal)(root, provider, ledger, plan, {})
        if final["invoke"] and proposal == final["proposal"]:
            prepared = _publication(root, provider, ledger, plan).prepare(proposal)
            return {**{key: prepared[key] for key in ("id", "fingerprint", "intent")}, "invoke": prepared["state"] == "prepared"}
    if not any(value == proposal for value in listed):
        # An applied or pending original may outlive source freshness after
        # its own publication. Its immutable ID/payload can be inspected and
        # reconciled, but this path cannot dispatch a replacement proposal.
        ident = proposal.get("id")
        if not isinstance(ident, str):
            raise PublicationError("proposal is outside accepted source reports")
        publisher = _publication(root, provider, ledger, plan)
        # The latest fresh attempt carries the original's exact proposal.
        saved = publisher._attempts(publisher._load(ident)["id"])[-1]
        original = saved["intent"]
        semantic = {key: proposal.get(key, original.get(key)) for key in ("kind", "head", "lease", "target", "payload", "sources", "source")}
        if semantic != {key: original.get(key) for key in semantic}:
            raise PublicationError("proposal differs from the retained original operation")
        if saved["state"] not in {"applied", "pending"}:
            raise PublicationError("publication source changed before dispatch")
        return {**{key: saved[key] for key in ("id", "fingerprint", "intent")}, "invoke": False}
    prepared = _publication(root, provider, ledger, plan).prepare(proposal)
    return {**{key: prepared[key] for key in ("id", "fingerprint", "intent")}, "invoke": prepared["state"] == "prepared"}


def _retained_original(root, provider, ledger, plan, proposal):
    ident = proposal.get("id")
    if not isinstance(ident, str):
        return False
    try:
        _publication(root, provider, ledger, plan)._load(ident)
    except PublicationError:
        return False
    return True


def publication_recover(root, provider, ledger, plan, inputs):
    publisher = _publication(root, provider, ledger, plan)
    saved = publisher._load(text(inputs.get("id"), "original publication operation"))
    _retained_audience(root, ledger, saved)
    observed = publisher.reconcile(saved["id"])
    if observed.get("outcome") != "completed":
        raise PublicationError("original publication remains unresolved; inspect retained evidence before retry")
    return publication_record(root, provider, ledger, plan, {"id": saved["id"], "receipt": observed["result"]})


def publication_execute(root, provider, ledger, plan, inputs):
    from . import app
    from .prepare import CHECKLIST
    publisher = _publication(root, provider, ledger, plan)
    saved = publisher._load(text(inputs.get("id"), "publication operation"))
    if saved["state"] == "prepared":
        # Never dispatch, or block, an intent while analysis is provisional; it
        # stays prepared until the PR metadata reaches the analyzed revision.
        try:
            app.require_current(app._snapshot(root, provider, plan), "publication")
        except app.ProvisionalIdentity as error:
            raise PublicationError(str(error)) from None
    # A replayed execute accepts only its previously retained native audience;
    # the original report is never replaced after a publication side effect.
    agent = text(inputs.get("agent"), "opaque audience agent")
    if not _AGENT.fullmatch(agent):
        raise PublicationError("audience requires an opaque native agent reference")
    report = inputs.get("report")
    if not app.verify_report(root, {"checklist": CHECKLIST}, report):
        raise PublicationError("native audience checklist remains incomplete")
    if report.get("findings") or report.get("blockers"):
        raise PublicationError("native audience reported unresolved payload findings")
    verdict = report.get("audience")
    if not isinstance(verdict, dict) or verdict.get("fingerprint") != saved["fingerprint"] or verdict.get("suitable") is not True:
        raise PublicationError("audience verdict does not accept this exact publication payload")
    identities = app.verify_evidence(root, report)
    evidence = verdict.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(key not in identities for key in evidence):
        raise PublicationError("audience verdict lacks verified source evidence")
    intended = text(verdict.get("audience", "PR participants"), "intended audience")
    producers = {_report_row(ledger, source)["agent"] for source in saved["intent"]["sources"]}
    if agent in producers:
        raise PublicationError("audience assessment must use an independent native agent")
    reference = artifact(root, {"intent": saved["id"], "agent": agent, "report": report})
    existing = ledger.db.execute("SELECT * FROM accepted_reports WHERE task=? ORDER BY rowid DESC LIMIT 1",
                                 ("audience:" + saved["id"],)).fetchone()
    if existing and (existing["reference"] != reference or existing["agent"] != agent or existing["fingerprint"] != saved["fingerprint"]):
        raise PublicationError("retained native audience assessment changed")
    with ledger.transaction():
        ledger.db.execute("INSERT OR IGNORE INTO accepted_reports(reference,task,kind,fingerprint,agent,report) VALUES(?,?,?,?,?,?)",
                          (reference, "audience:" + saved["id"], "audience", saved["fingerprint"], agent, canonical(report).decode()))
    publisher = _publication(root, provider, ledger, plan, audience=saved)
    result = publisher.execute(saved["id"], {"fingerprint": saved["fingerprint"], "suitable": True,
        "audience": intended, "evidence": [reference]})
    return {"id": saved["id"], "receipt": result}


def audience_scope(root, provider, ledger, plan, operation, inputs):
    """Bind ordinary retained task operations to one immutable public intent."""
    from . import app
    from .prepare import CHECKLIST
    saved = _publication(root, provider, ledger, plan)._load(text(inputs["publication"], "audience publication"))
    task = {"id": "publication_audience_" + digest([saved["id"], saved["fingerprint"]]),
        "kind": "publication_audience", "definition": "audience",
        "definition_digest": plan["definitions"]["audience"],
        "implementation": digest(plan["helper_manifest"]),
        "required": True, "writes": False, "cacheable": True, "head_sensitive": True,
        "resources": [], "checklist": CHECKLIST,
        "publication": {"id": saved["id"], "fingerprint": saved["fingerprint"]},
        "assignment": "Review the exact public intent in payload for its audience. Return a source-backed checklist and audience={fingerprint,suitable,evidence}. Do not publish or change the payload."}
    active = {**plan, "tasks": [task]}
    observed = dict(inputs)
    snapshot = app._snapshot(root, provider, plan)
    for source in saved["intent"]["sources"]:
        _source(root, provider, ledger, plan, source, snapshot=snapshot)
    if operation == "claim":
        if inputs.get("task") != "publication_audience":
            raise PublicationError("audience claim requires its declared task")
        observed.update(task=task["id"], payload={"intent": saved["intent"], "fingerprint": saved["fingerprint"]})
    else:
        claim = app._claim(ledger, inputs["claim"])
        assignment = read_artifact(root, claim["assignment"])
        if claim["task"] != task["id"] or assignment["task"] != task:
            raise PublicationError("audience continuation differs from its original publication task")
    return active, observed


def _retained_audience(root, ledger, saved):
    row = ledger.db.execute("SELECT * FROM accepted_reports WHERE task=? AND kind='audience' ORDER BY rowid DESC LIMIT 1",
                            ("audience:" + saved["id"],)).fetchone()
    if not row:
        raise PublicationError("publication has no retained accepted native audience")
    wrapper = read_artifact(root, row["reference"])
    report = bounded_json(row["report"])
    if (wrapper != {"intent": saved["id"], "agent": row["agent"], "report": report}
            or row["fingerprint"] != saved["fingerprint"]
            or report.get("audience", {}).get("fingerprint") != saved["fingerprint"]
            or report.get("audience", {}).get("suitable") is not True):
        raise PublicationError("retained native audience identity changed")
    producers = {_report_row(ledger, source)["agent"] for source in saved["intent"]["sources"]}
    if row["agent"] in producers or not _AGENT.fullmatch(row["agent"]):
        raise PublicationError("retained audience is not independent of the original producers")
    return dict(row)


def publication_record(root, provider, ledger, plan, inputs):
    """Commit a reconciled external receipt in an explicit mutation stage."""
    publisher = _publication(root, provider, ledger, plan)
    saved = publisher._load(text(inputs.get("id"), "publication operation"))
    receipt = inputs.get("receipt")
    _retained_audience(root, ledger, saved)
    if saved["state"] not in {"pending", "applied"}:
        raise PublicationError("only a dispatched publication can record a reconciled receipt")
    if saved["state"] == "applied" and saved["receipt"] != receipt:
        raise PublicationError("duplicate receipt: the applied publication has a different receipt")
    if not publisher.validate(saved["id"], receipt):
        raise PublicationError("reconciled publication receipt does not match the exact current effect")
    publisher._settle(saved, receipt)
    return {"id": saved["id"], "receipt": receipt}


def readiness(root, provider, ledger, plan, inputs=None):
    """Fresh application coverage plus live repository/CI evidence, read-only."""
    from . import app
    from .ci import classify_current, retained_classification_window
    from .registry import Registry
    result = {"complete": False, "ready": False, "clean_review": False, "verified": False,
              "provisional": {"provisional": None},
              "head": "", "target": "", "ci": {"head": "", "success": False}, "evidence": [], "blockers": []}
    try:
        covered = app.coverage(root, provider, ledger, plan, {})
        result.update(complete=bool(covered["complete"]), missing=covered.get("missing", []),
                      withheld=covered.get("withheld", []), unresolved=covered.get("unresolved", []),
                      responses=covered.get("responses"), review=covered.get("review"))
        snapshot = covered["snapshot"]
        result.update(head=snapshot["head"], target=snapshot["target"], provisional=app.provisional(snapshot))
        current_identity = snapshot["identities"]["current"]
        if not current_identity:
            result["blockers"].append("PR metadata has not reached the analyzed revision; analysis is provisional")
        if not covered["complete"]:
            result["blockers"].append("selected task coverage or original finding settlement incomplete")
        reviews = [task for task in plan["tasks"] if task["kind"] == "review"]
        verifications = [task for task in plan["tasks"] if task["kind"] == "verification"]
        if not reviews or not verifications:
            result["blockers"].append("current review and independent finding verification must be selected")
        current = {}
        for task in reviews + verifications:
            claim = app._current(ledger, snapshot, task)
            if not claim:
                result["blockers"].append("current " + task["kind"] + " report missing: " + task["id"])
                continue
            row = _source(root, provider, ledger, plan, claim["report"], snapshot=snapshot)
            current[task["id"]] = row
            result["evidence"].append(row["reference"])
        result["clean_review"] = bool(reviews) and all(t["id"] in current for t in reviews) and not covered["unresolved"]
        if covered.get("review") is not None:
            # Incoming review also needs ordinary (not only simplification) coverage.
            result["clean_review"] = result["clean_review"] and covered["review"]["clean_review"]
        result["verified"] = bool(verifications) and all(t["id"] in current for t in verifications) and not covered["unresolved"]
        if not current_identity:
            # Live CI and repository requirements belong to a reconciled head.
            return result
        if snapshot.get("local_head") != snapshot["head"] or snapshot.get("dirty"):
            result["blockers"].append("merge requires the exact clean published head")
        observed = provider.observe_ci(plan["pr"], snapshot["head"])
        if observed.get("head") != snapshot["head"]:
            raise PublicationError("CI observation belongs to another head")
        # Saved fail-then-pass history counts here too; readiness saves nothing. An
        # attempt whose retained classification window ended stays failed here too.
        registry = Registry(root, read_only=True)
        try:
            classified = classify_current(provider, plan, observed, retained_classification_window(ledger, plan),
                                          registry=registry)
        finally:
            registry.close()
        result["ci"] = {**classified, "success": classified["status"] == "success"}
        result["evidence"].append("sha256:" + digest(observed))
        if not result["ci"]["success"]:
            result["blockers"].append("fresh exact-head CI is not successful")
        after = provider.capture(plan["pr"])
        meta = after["metadata"]
        if meta.get("head", {}).get("sha") != snapshot["head"] or meta.get("base", {}).get("sha") != snapshot["target"]:
            result["blockers"].append("PR head or target changed during readiness verification")
        if meta.get("state") != "open" or meta.get("merged") or meta.get("draft") or meta.get("mergeable_state") != "clean":
            result["blockers"].append("live PR merge requirements are not satisfied")
        requirements = repository_merge_requirements(provider, plan["pr"], meta, snapshot["head"])
        result["evidence"].append("sha256:" + digest(requirements))
        result["ready"] = bool(covered["complete"] and result["clean_review"] and result["verified"] and not result["blockers"])
    except Exception as error:
        result["blockers"].append(str(error))
    return result


def _own(snapshot, plan):
    """The actor's PR, or one by a configured personal account on its host (as in capture)."""
    pr = snapshot["pr"]
    return snapshot["author_id"] == snapshot["actor_id"] or any(
        str(account.get("host", "")).lower() == pr["host"] and account.get("id") == snapshot["author_id"]
        for account in plan.get("options", {}).get("accounts", []))


def approval(root, provider, ledger, plan, inputs=None):
    """Current clean independent incoming review of the exact head, read-only.

    Approval is separate from finding publication and merge: it needs no CI or
    repository merge requirement, but it needs complete required coverage, a
    current ordinary reviewer, a current independent verifier, no confirmed or
    unverified finding, a reconciled (not provisional) head and another
    author's PR. It never reads or implies merge readiness.
    """
    from . import app
    result = {"approve": False, "head": "", "target": "", "evidence": [], "blockers": []}
    try:
        if plan.get("mode") != "review":
            result["blockers"].append("approval belongs to incoming review")
            return result
        covered = app.coverage(root, provider, ledger, plan, {})
        snapshot, review = covered["snapshot"], covered["review"]
        result.update(head=snapshot["head"], target=snapshot["target"])
        if not snapshot["identities"]["current"]:
            result["blockers"].append("PR metadata has not reached the analyzed revision; analysis is provisional")
        if snapshot.get("local_head") != snapshot["head"] or snapshot.get("dirty"):
            result["blockers"].append("approval needs the exact clean published head")
        if _own(snapshot, plan):
            result["blockers"].append("the authenticated account never approves its own PR")
        if not covered["complete"] or not review["clean_review"]:
            result["blockers"].append("approval needs complete clean independent review")
        for task in plan["tasks"]:
            if task["kind"] not in ("review", "verification"):
                continue
            claim = app._current(ledger, snapshot, task)
            if claim:
                result["evidence"].append(_source(root, provider, ledger, plan, claim["report"],
                                                  snapshot=snapshot)["reference"])
        result["approve"] = not result["blockers"] and bool(result["evidence"])
    except Exception as error:
        result["blockers"].append(str(error))
    return result


def approval_proposal(root, provider, ledger, plan, inputs):
    """Construct only the exact-head approval the user separately authorized."""
    authority = plan.get("authorizations", {}).get("approve_pr")
    if authority is None:
        return {"invoke": False, "proposal": {}}
    authority = auth(authority)
    if plan.get("options", {}).get("permissions", {}).get("approve_pr") is not True:
        return {"invoke": False, "proposal": {}, "blockers": ["configured permission forbids approve_pr"]}
    current = approval(root, provider, ledger, plan, {})
    if not current["approve"]:
        return {"invoke": False, "proposal": {}, "blockers": current["blockers"]}
    # The body stays empty: coverage and execution details are private.
    proposal = {"id": "approve_" + digest([plan["logical_run"], current["head"], current["target"], authority]),
                "kind": "approve_pr", "head": current["head"], "lease": current["head"], "target": current["target"],
                "payload": {"body": ""}, "sources": sorted(current["evidence"])}
    return {"invoke": True, "proposal": proposal}


def fork_ci(root, provider, ledger, plan, inputs=None):
    """Complete current incoming review of the exact head for fork workflow approval, read-only.

    Fork approval (#35) only lets the PR's workflows run. It needs what PR
    approval needs (complete current coverage, a current ordinary reviewer and
    independent verifier, no confirmed or unverified finding, a reconciled
    clean head and another author's PR), plus a non-draft PR and no unsettled
    publication. It needs neither CI success nor a human approval, and it reads
    no merge readiness: finding publication, PR approval and merge authority
    never stand in for it.
    """
    current = approval(root, provider, ledger, plan, inputs)
    result = {"ready": False, "head": current["head"], "target": current["target"],
              "evidence": list(current["evidence"]), "blockers": list(current["blockers"])}
    try:
        if ledger.db.execute("SELECT 1 FROM publication_intents WHERE state='pending' LIMIT 1").fetchone():
            result["blockers"].append("an earlier publication is unsettled; reconcile it before fork approval")
        meta = provider.capture(plan["pr"])["metadata"]
        if meta.get("draft"):
            result["blockers"].append("fork approval needs a non-draft PR")
        if meta.get("head", {}).get("sha") != current["head"] or meta.get("base", {}).get("sha") != current["target"]:
            result["blockers"].append("PR head or target changed during fork-approval verification")
    except Exception as error:
        result["blockers"].append(str(error))
    result["ready"] = current["approve"] and not result["blockers"]
    return result


def fork_ci_proposals(root, provider, ledger, plan, inputs=None):
    """The fork approvals the user separately authorized: one per eligible run attempt.

    Every page of the head's ``pull_request`` runs is read. A run is eligible
    only as ``fork_gate`` describes; an ambiguous association blocks them all.
    An empty complete listing with current readiness is the verified no-op.
    """
    authority = plan.get("authorizations", {}).get("approve_fork_ci")
    if authority is None:
        return {"items": [], "blockers": ["fork approval needs its own explicit authorization"]}
    authority = auth(authority)
    if plan.get("options", {}).get("permissions", {}).get("approve_fork_ci") is not True:
        return {"items": [], "blockers": ["configured permission forbids approve_fork_ci"]}
    current = fork_ci(root, provider, ledger, plan, {})
    if not current["ready"]:
        return {"items": [], "blockers": current["blockers"]}
    head, target, pr = current["head"], current["target"], plan["pr"]
    meta = provider.capture(pr)["metadata"]
    branch = meta.get("head", {}).get("ref")
    source = str(meta.get("head", {}).get("repo", {}).get("full_name", "")).lower()
    runs = provider.pages(f"repos/{pr['owner']}/{pr['name']}/actions/runs?event=pull_request&head_sha={head}",
                          key="workflow_runs")
    items, blockers = [], []
    for run in runs:
        reason = fork_gate(run, pr, head, branch, source)
        if reason == "ambiguous pull request association":
            blockers.append(f"workflow run {run.get('id')}: {reason}")
        if reason:
            continue
        ident, attempt = run.get("id"), run.get("run_attempt")
        if any(type(value) is not int or value < 1 for value in (ident, attempt)):
            blockers.append("workflow run identity unavailable")
            continue
        items.append({"id": "fork_ci_" + digest([plan["logical_run"], head, target, ident, attempt, authority]),
                      "kind": "approve_fork_ci", "head": head, "lease": head, "target": target,
                      "payload": {"run_id": ident, "attempt": attempt}, "sources": sorted(current["evidence"])})
    if blockers:
        return {"items": [], "blockers": blockers}
    return {"items": items, "blockers": [], "noop": not items, "runs": len(runs)}


def _fork_proposal(saved):
    intent = saved["intent"]
    return {"id": saved["id"], **{key: intent[key] for key in ("kind", "head", "lease", "target", "payload", "sources")}}


def _fork_listing(root, provider, ledger, plan):
    """Unsettled fork approvals first, for read-only reconciliation only; then current eligible runs."""
    publisher = _publication(root, provider, ledger, plan)
    pending = [publisher._load(row["id"]) for row in
               ledger.db.execute("SELECT id FROM publication_intents WHERE state='pending' ORDER BY rowid")]
    retained = [_fork_proposal(saved) for saved in pending if saved["intent"]["kind"] == "approve_fork_ci"]
    if retained:
        return {"items": retained, "blockers": ["unsettled fork approvals are reconciled before any other"]}
    return fork_ci_proposals(root, provider, ledger, plan, {})


def _prepare_fork_ci(root, provider, ledger, plan, proposal):
    publisher = _publication(root, provider, ledger, plan)
    if _retained_original(root, provider, ledger, plan, proposal):
        saved = publisher._attempts(publisher._load(proposal["id"])["id"])[-1]
        if saved["state"] in {"applied", "pending"}:
            # A dispatched approval is only reconciled; it is never sent again.
            if {key: value for key, value in _fork_proposal(saved).items() if key != "id"} != {
                    key: value for key, value in proposal.items() if key != "id"}:
                raise PublicationError("proposal differs from the retained original operation")
            return {**{key: saved[key] for key in ("id", "fingerprint", "intent")}, "invoke": False}
    current = fork_ci_proposals(root, provider, ledger, plan, {})
    if not any(item == proposal for item in current["items"]):
        raise PublicationError("not a current eligible fork approval: " + "; ".join(current["blockers"] or ["run changed"]))
    prepared = publisher.prepare(proposal)
    return {**{key: prepared[key] for key in ("id", "fingerprint", "intent")}, "invoke": prepared["state"] == "prepared"}


def merge_proposal(root, provider, ledger, plan, inputs):
    """Construct only the final exact-head merge authorized by the user."""
    authority = plan.get("authorizations", {}).get("merge")
    if authority is None:
        return {"invoke": False, "proposal": {}}
    authority = auth(authority)
    if plan.get("options", {}).get("permissions", {}).get("merge") is not True:
        return {"invoke": False, "proposal": {}, "blockers": ["configured permission forbids merge"]}
    current = readiness(root, provider, ledger, plan, {})
    if not current["ready"]:
        return {"invoke": False, "proposal": {}, "blockers": current["blockers"]}
    method = plan.get("options", {}).get("merge_method", "merge")
    if method not in {"merge", "squash"}:
        raise PublicationError("merge receipts support merge/squash only")
    sources = [reference for reference in current["evidence"] if not reference.startswith("sha256:")]
    if not sources:
        raise PublicationError("final merge requires current independent review references")
    proposal = {"id": "merge_" + digest([plan["logical_run"], current["head"], current["target"], authority, method]),
                "kind": "merge", "head": current["head"], "lease": current["head"], "target": current["target"],
                "payload": {"merge_method": method}, "sources": sources}
    return {"invoke": True, "proposal": proposal}


def completed_merge(root, provider, ledger, plan, completion):
    """Validate historical readiness through its exact authorized merge effect."""
    authority = plan.get("authorizations", {}).get("merge")
    if not completion.get("complete") or not completion.get("ready") or authority is None:
        return False
    publisher = _publication(root, provider, ledger, plan)
    for row in ledger.db.execute("SELECT id FROM publication_intents WHERE state IN ('pending','applied')"):
        saved = publisher._load(row["id"])
        intent = saved["intent"]
        if (intent["kind"] != "merge" or intent["head"] != completion.get("head")
                or intent["target"] != completion.get("target") or intent["authorization"] != authority):
            continue
        _retained_audience(root, ledger, saved)
        if publisher.reconcile(saved["id"]).get("outcome") == "completed":
            return True
    return False


def dispatch(operation, root, provider, ledger, plan, inputs):
    handlers = {"publication_list": publication_list, "publication_prepare": publication_prepare,
                "publication_execute": publication_execute, "publication_record": publication_record,
                "publication_recover": publication_recover,
                "readiness": readiness, "merge_proposal": merge_proposal,
                "approval_proposal": approval_proposal}
    if operation not in handlers:
        raise PublicationError("unknown publication helper operation")
    return handlers[operation](root, provider, ledger, plan, inputs)


def reconcile_operation(root, provider, ledger, plan, inputs):
    """Read-only application receipt observation for action-v1 reconciliation."""
    return _publication(root, provider, ledger, plan).reconcile(text(inputs.get("id"), "publication operation"))


def validate_operation(root, provider, ledger, plan, inputs, output):
    """Read-only checkpoint validation; missing effects never authorize replay."""
    if not isinstance(output, dict) or output.get("id") != inputs.get("id"):
        return False
    if "receipt" in inputs and inputs["receipt"] != output.get("receipt"):
        return False
    try:
        publisher = _publication(root, provider, ledger, plan)
        _retained_audience(root, ledger, publisher._load(inputs["id"]))
        return publisher.validate(inputs["id"], output.get("receipt"))
    except (PublicationError, ValueError, KeyError, TypeError):
        return False
