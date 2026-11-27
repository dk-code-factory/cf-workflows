"""Exact-intent publication: one guarded external write at a time.

Adapted from Agent Workflow's portable PR-fix example (MIT; see LICENSE).

Every write is an immutable intent in the logical run's SQLite ledger before
dispatch. The intent pins the numeric actor and PR identity, head, target,
branch and repository leases, the exact payload, accepted sources, the user
authorization and configured permission it relies on, and its pre-dispatch
baseline. Dispatch rechecks all of them, retains the independent audience
assessment and takes the run's publication lock in the same transaction that
moves the intent to ``pending``. Journal states follow the state contract:

- ``prepared``: never dispatched.
- ``pending``: dispatch may have happened; the intent holds the lock.
- ``applied``: the exact effect was observed and its receipt retained.
- ``not_applied``: the transport conclusively reported a refusal without effect.
- ``blocked``: a dispatch-time recheck found drift; it was never dispatched.

Only read-only reconciliation that observes the exact effect, or the original
dispatch's own conclusive rejection, settles a pending intent. Missing,
unchanged or ambiguous observations keep it pending and never authorize replay. A ``not_applied`` or ``blocked`` intent is never resent; a
later attempt is a fresh intent with its own preparation and audience.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Callable

from .capture import MAX_PAGES, HTTPFailure
from .core import auth, canonical, digest, text


class PublicationError(RuntimeError):
    pass


class UncertainEffect(PublicationError):
    pass


class Drift(PublicationError):
    """A retained binding no longer holds at dispatch; the intent is blocked."""


class PendingReview(Drift):
    """The actor owns a pending review on the PR. It is never submitted,
    discarded, overwritten or adopted; the submission waits for a fresh attempt."""

    def __init__(self, reviews):
        self.reviews = reviews
        super().__init__(
            f"the actor has pending review(s) {reviews}; submit or delete them on GitHub, "
            "then prepare a fresh attempt. This helper never changes a pending review")


class Rejected(Exception):
    """Raised by a transport only when the service conclusively refused a write
    before any effect. Timeouts, malformed or generic errors and absence are
    never conclusive. The GitHub transport raises ``HTTPFailure`` instead, and
    only ``pending_review_rejection`` classifies one as conclusive."""

    def __init__(self, evidence):
        super().__init__("service conclusively rejected the write")
        self.evidence = evidence


# GitHub's exact refusal of a review creation while the actor owns a draft.
PENDING_REVIEW_ERROR = "User can only have one pending review per pull request"
_FAILURE_BODY = {"message", "errors", "documentation_url", "status"}
REJECTED_BY_PENDING_REVIEW = (
    "GitHub refused the review because the actor has a pending review; it settles not_applied "
    "and the draft is untouched. Submit or delete the draft, then prepare a fresh attempt")


def failure_record(error):
    """The retained form of an original dispatch failure (no headers or stderr)."""
    record = {"type": type(error).__name__}
    if isinstance(error, HTTPFailure):
        record.update(status=error.status, body=copy.deepcopy(error.body))
    return record


def pending_review_rejection(intent, failure):
    """Rejection evidence when ``failure`` conclusively refused a review submission
    because the actor already owns a pending review, otherwise ``None``.

    Only the original dispatch's HTTP 422 carrying exactly GitHub's
    pending-review error is conclusive. Timeouts, transport errors, other or
    additional 422 errors, malformed bodies and any current draft observation
    are not.
    """
    if intent.get("kind") not in REVIEW_KINDS or not isinstance(failure, dict):
        return None
    body = failure.get("body")
    if (failure.get("type") != "HTTPFailure" or failure.get("status") != 422
            or not isinstance(body, dict) or not set(body) <= _FAILURE_BODY
            or body.get("message") != "Unprocessable Entity"
            or body.get("errors") != [PENDING_REVIEW_ERROR]
            or body.get("status", "422") != "422"):
        return None
    return {"classifier": "pending_review_conflict", "intent": digest(intent),
            "status": 422, "body": copy.deepcopy(body)}


# The merge guard below (_review_identity, _pending_reviewer and
# repository_merge_requirements) is Agent Workflow #138's correction, consumed
# unchanged from its merge commit; scripts/check_baselines.py verifies that.
def _review_identity(value):
    if not isinstance(value, str) or not value or any(c.isspace() or c == "\x00" for c in value):
        raise PublicationError("repository review request identity unavailable")
    return value


def _pending_reviewer(node):
    if not isinstance(node, dict) or not isinstance(node.get("requestedReviewer"), dict):
        raise PublicationError("repository review request identity unavailable")
    _review_identity(node.get("id"))
    reviewer = node["requestedReviewer"]
    _review_identity(reviewer.get("id"))
    kind = reviewer.get("__typename")
    if kind == "User":
        return "user @" + _review_identity(reviewer.get("login"))
    if kind == "Team" and isinstance(reviewer.get("organization"), dict):
        return "team @" + _review_identity(reviewer["organization"].get("login")) + "/" + _review_identity(reviewer.get("slug"))
    raise PublicationError("repository review request type unavailable or unsupported")


def repository_merge_requirements(provider, pr, metadata, head):
    """Read complete current requests; earlier approvals never discharge them."""
    if metadata.get("mergeable") is not True or metadata.get("mergeable_state") != "clean" or metadata.get("draft"):
        raise PublicationError("repository merge requirements are not currently satisfied")
    query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){
      repository(owner:$owner,name:$name){pullRequest(number:$number){
        headRefOid isDraft mergeable mergeStateStatus reviewDecision
        reviewRequests(first:100,after:$cursor){totalCount pageInfo{hasNextPage endCursor}
          nodes{id requestedReviewer{__typename ... on User{id login}
            ... on Team{id slug organization{login}}}}}}}}"""
    cursor, baseline, total = None, None, None
    cursors, request_ids, reviewer_ids = set(), set(), set()
    requests, pending = [], []
    fields = ("headRefOid", "isDraft", "mergeable", "mergeStateStatus", "reviewDecision")
    for _ in range(MAX_PAGES):
        response = provider.request("POST", "graphql", {"query": query, "variables": {
            "owner": pr["owner"], "name": pr["name"], "number": pr["number"], "cursor": cursor}})
        if not isinstance(response, dict) or response.get("errors"):
            raise PublicationError("repository merge requirements unavailable")
        actual = response
        for key in ("data", "repository", "pullRequest"):
            actual = actual.get(key) if isinstance(actual, dict) else None
        if not isinstance(actual, dict) or any(key not in actual for key in fields):
            raise PublicationError("repository merge requirements incomplete")
        state = {key: actual[key] for key in fields}
        if baseline is None:
            baseline = state
        if (state != baseline or actual["headRefOid"] != head or actual["isDraft"] is not False
                or actual["mergeable"] != "MERGEABLE" or actual["mergeStateStatus"] != "CLEAN"):
            raise PublicationError("repository merge requirements changed or remain unsatisfied")
        connection = actual.get("reviewRequests")
        if not isinstance(connection, dict):
            raise PublicationError("repository review requests unavailable")
        count, nodes, page = connection.get("totalCount"), connection.get("nodes"), connection.get("pageInfo")
        if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                or not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(page, dict)
                or type(page.get("hasNextPage")) is not bool or "endCursor" not in page):
            raise PublicationError("repository review requests incomplete")
        if total is None:
            total = count
        if count != total:
            raise PublicationError("repository review requests changed during enumeration")
        for node in nodes:
            label = _pending_reviewer(node)
            reviewer = node["requestedReviewer"]
            identity = (reviewer["__typename"], reviewer["id"])
            if node["id"] in request_ids or identity in reviewer_ids:
                raise PublicationError("repository review requests repeated during enumeration")
            request_ids.add(node["id"])
            reviewer_ids.add(identity)
            requests.append(node)
            pending.append(label)
        if len(requests) > total:
            raise PublicationError("repository review requests incomplete")
        end = page["endCursor"]
        if end is not None:
            _review_identity(end)
        if not page["hasNextPage"]:
            if len(requests) != total:
                raise PublicationError("repository review requests incomplete")
            break
        if not nodes or not end or end in cursors or len(requests) >= total:
            raise PublicationError("repository review requests pagination incomplete")
        cursors.add(end)
        cursor = end
    else:
        raise PublicationError("repository review requests exceed bounded pagination")
    if pending:
        raise PublicationError("outstanding repository review requests: " + ", ".join(pending))
    # GitHub returns explicit null when no repository review decision applies.
    # An omitted/unknown value cannot establish readiness; REVIEW_REQUIRED holds
    # even when no individual reviewer has been requested.
    if baseline["reviewDecision"] not in ("APPROVED", None):
        raise PublicationError("repository merge requirements review decision unsatisfied or unavailable")
    return {**baseline, "reviewRequests": {"totalCount": total, "nodes": requests}}


# Supported kinds: (contract remote-write action, workflow permission input).
SUPPORTED = {
    "push": ("push", "push_commits"), "rewrite": ("push", "rewrite_commits"),
    "edit_pr": ("edit_pr", "edit_pr"),
    "reply": ("reply", "reply_to_feedback"), "reply_inline": ("reply", "reply_to_feedback"),
    "resolve_thread": ("resolve_thread", "resolve_threads"),
    "split_push": ("create_pr", "create_draft_prs"), "split_pr": ("create_pr", "create_draft_prs"),
    "request_review": ("request_review", "request_review"),
    "ci_rerun": ("rerun_ci", "rerun_ci"),
    "submit_review": ("post_findings", "post_findings"),
    # A review approval (#31): its own action and permission, never implied by
    # finding publication or merge. The ``approval`` callback must show current
    # clean independent review of the exact head, and the actor never approves
    # its own PR.
    "approve_pr": ("approve_pr", "approve_pr"),
    # Fork workflow execution approval (#35): its own action and permission,
    # never implied by finding publication, PR approval or merge. The
    # ``fork_ci`` callback must show complete current incoming review of the
    # exact head, and the run attempt must still wait for fork approval.
    "approve_fork_ci": ("approve_fork_ci", "approve_fork_ci"),
    "merge": ("merge", "merge"),
}
# Extension contract: these kinds are reserved for their owning PRs. Each needs
# its own payload, dispatch, read-only observation, effect key and fixtures
# before it is added to SUPPORTED; until then preparation denies it.
EXTENSIONS = {}
# Issue kinds (#32): repository writes of cf-workflows:dk-issue, each its own
# contract action and permission. They bind the numeric repository, actor and
# issue rather than a PR head. The installed workflow declares their inputs, and
# prepare enables them only for the cf-workflows:dk-issue entry.
ISSUE_KINDS = {"create_issue": ("create_issue", "create_issue"), "edit_issue": ("edit_issue", "edit_issue")}
ISSUE_FIELDS = ("title", "body", "labels", "assignees")
# Run states after a fork approval took effect: the same attempt executes jobs.
FORK_EXECUTING = ("queued", "in_progress", "completed")
# Kinds that create a pull request review; a pending review owned by the actor holds both.
REVIEW_KINDS = {"submit_review", "approve_pr"}
KINDS = set(SUPPORTED)
SCOPES = {**SUPPORTED, **ISSUE_KINDS}
PERMISSIONS = {kind: setting for kind, (_, setting) in SCOPES.items()}
PERMISSION_INPUTS = sorted({setting for _, setting in SCOPES.values()})
STATES = ("prepared", "pending", "applied", "not_applied", "blocked")
TERMINAL = {"not_applied", "blocked"}
FRESH_ATTEMPTS = 3
REVIEW_EVENTS = {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
APPROVED = "APPROVED"
_COLUMNS = "id,fingerprint,value,state,receipt,audience"
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,240}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _sha(value, label):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PublicationError(f"{label} requires a full lowercase object ID")
    return value


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublicationError(f"{label} requires a positive integer")
    return value


def _strings(value, label):
    if not isinstance(value, list) or not value or any(
        not isinstance(v, str) or not v.strip() or "\x00" in v for v in value
    ):
        raise PublicationError(f"{label} requires nonempty text references")
    return value


def _branch(value):
    if not isinstance(value, str) or not _BRANCH.fullmatch(value):
        raise PublicationError("invalid exact branch")
    if ".." in value or "//" in value or "@{" in value or value.endswith(("/", ".", ".lock")):
        raise PublicationError("invalid exact branch")
    return value


def _json(raw):
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def _row(row):
    if row is None:
        raise PublicationError("unknown publication intent")
    return {"id": row[0], "fingerprint": row[1], "intent": _json(row[2]), "state": row[3],
            "receipt": _json(row[4]) if row[4] else None, "audience": _json(row[5]) if row[5] else None}


def _discussion(ev):
    return digest({key: ev.get(key, []) for key in ("comments", "reviews", "inline", "threads")})


def effect_key(intent, receipt):
    """The unique external object an applied receipt names, or None.

    One external effect can settle only one intent; a second intent that
    observes the same object is a duplicate receipt and stays unsettled.
    """
    kind, result = intent["kind"], (receipt or {}).get("result")
    if not isinstance(result, dict):
        return None
    if kind in ("push", "rewrite", "split_push"):
        return f"ref:{intent.get('head_repository')}:{result.get('branch')}:{result.get('head')}"
    if kind in ("reply", "reply_inline"):
        return ("comment:" if kind == "reply" else "review_comment:") + str(result.get("id"))
    if kind == "split_pr":
        return "pull:" + str(result.get("number"))
    if kind in REVIEW_KINDS:
        return "review:" + str(result.get("review", {}).get("id"))
    if kind == "merge":
        return "merge:" + str(result.get("commit", {}).get("sha"))
    if kind == "create_issue":
        return f"issue:{intent['repository']}:{result.get('number')}"
    if kind == "ci_rerun":
        # The rerun creates the next attempt of the exact run.
        return f"run:{result.get('id', intent['payload']['run_id'])}:{result.get('run_attempt')}"
    if kind == "approve_fork_ci":
        # The approval lets this exact attempt execute; it creates no new attempt.
        return f"fork_ci:{intent['payload']['run_id']}:{intent['payload']['attempt']}"
    return None


def fork_gate(run, pr, head, branch, head_repository):
    """Why ``run`` is not a fork approval gate of this exact PR head, or None.

    A gate is a ``pull_request`` run of the base repository at the exact head,
    from the PR's fork repository and branch, in ``action_required``, with no
    association to any other PR. Runs from the base repository, earlier heads
    and other events are not gates. The caller checks the attempt's jobs and
    pending deployments separately.
    """
    if not isinstance(run, dict):
        return "workflow run unavailable"
    base = f"{pr['owner']}/{pr['name']}".lower()
    source = str((run.get("head_repository") or {}).get("full_name", "")).lower()
    if run.get("event") != "pull_request" or run.get("head_sha") != head:
        return "not a pull_request run of the exact head"
    if "action_required" not in (run.get("status"), run.get("conclusion")):
        return "not waiting for fork approval"
    if str((run.get("repository") or {}).get("full_name", "")).lower() != base:
        return "run belongs to another repository"
    if source == base or source != head_repository or run.get("head_branch") != branch:
        return "run is not from this PR's fork repository and branch"
    associated = run.get("pull_requests")
    if not isinstance(associated, list) or any(not isinstance(item, dict) for item in associated) or any(
            item.get("number") != pr["number"] for item in associated):
        return "ambiguous pull request association"
    return None


def reply_identity(item):
    """The fields that make a posted reply this exact effect.

    Reactions, updated_at and other metadata on our own reply change over time
    without changing the effect, so receipts compare only these.
    """
    if not isinstance(item, dict):
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    return {"id": item.get("id"), "body": item.get("body"), "user": user.get("id"),
            "in_reply_to_id": item.get("in_reply_to_id")}


def _pr_field(key, value):
    """Comparable form of an edited PR field: GitHub reports a cleared body as null."""
    return (value or "") if key == "body" else value


def original_discussion(ledger, plan, discussion):
    """Remove only exact, reconciled effects created by this logical run."""
    result = copy.deepcopy(discussion)
    for row in ledger.db.execute(f"SELECT {_COLUMNS} FROM publication_intents WHERE state='applied' ORDER BY rowid DESC"):
        saved = _row(row)
        intent, receipt = saved["intent"], saved["receipt"]
        if (saved["fingerprint"] != digest(intent) or intent.get("run") != plan["logical_run"]
                or intent.get("pr") != plan["pr"] or not isinstance(receipt, dict)
                or receipt.get("id") != saved["id"] or receipt.get("fingerprint") != saved["fingerprint"]
                or receipt.get("actor_id") != intent.get("actor_id")):
            raise PublicationError("retained discussion effect identity differs")
        kind, payload, observed = intent["kind"], intent["payload"], receipt.get("result")
        if kind in {"reply", "reply_inline"} and not intent["before"].get("satisfied_comment_id"):
            key = "comments" if kind == "reply" else "inline"
            if (isinstance(observed, dict) and observed.get("body") == payload["body"]
                    and observed.get("user", {}).get("id") == intent["actor_id"]):
                # Reactions and updated_at on our own reply do not make it someone else's.
                result[key] = [item for item in result[key] if reply_identity(item) != reply_identity(observed)]
        elif kind == "edit_pr" and observed == payload and all(
                _pr_field(key, result.get(key)) == value for key, value in payload.items()):
            result.update(intent["before"])
        elif kind == "resolve_thread" and isinstance(observed, dict):
            before = intent["before"].get("thread")
            if before is not None:
                result["threads"] = [copy.deepcopy(before) if thread == observed else thread for thread in result["threads"]]
        elif kind in REVIEW_KINDS and isinstance(observed, dict):
            review = observed.get("review")
            created = {c.get("id") for c in observed.get("comments", [])}
            result["reviews"] = [item for item in result["reviews"] if item != review]
            result["inline"] = [item for item in result["inline"]
                                if not (item.get("id") in created and item.get("user", {}).get("id") == intent["actor_id"])]
    return result


class Publication:
    """Application-owned publication policy with a narrow fakeable transport.

    ``readiness`` must recapture accepted application review/verification and CI
    for the supplied exact intent. No callback means merge is unavailable.
    ``approval`` must recapture current clean independent review of the exact
    intent head; no callback means approval is unavailable.
    ``fork_ci`` must recapture complete current incoming review of the exact
    intent head for fork workflow approval; no callback means it is unavailable.
    ``source_validator`` validates original accepted report references, including
    their current artifact bytes. Python never reads the engine database.
    """

    def __init__(self, ledger, provider, root: Path, plan: dict,
                 readiness: Callable | None = None, source_validator: Callable | None = None,
                 approval: Callable | None = None, fork_ci: Callable | None = None):
        self.ledger = ledger
        self.provider = provider
        self.root = Path(root)
        self.plan = plan
        self.readiness = readiness
        self.approval = approval
        self.fork_ci = fork_ci
        self.source_validator = source_validator
        # A repository plan (no PR) publishes issue kinds only.
        self.pr = plan.get("pr")
        target = self.pr or plan["repository"]
        self.repo = f"repos/{target['owner']}/{target['name']}"
        self.repository = f"{target.get('host', 'github.com')}/{target['owner']}/{target['name']}".lower()
        if self.pr is not None:
            self.pr_path = f"{self.repo}/pulls/{self.pr['number']}"
            self.issue_path = f"{self.repo}/issues/{self.pr['number']}"

    def _request(self, method, path, payload=None):
        return self.provider.request(method, path, payload)

    def _descends(self, commit, ancestor):
        """Read-only: whether ``commit`` strictly follows ``ancestor`` in the repository history."""
        if not (isinstance(commit, str) and _SHA.fullmatch(commit)):
            return False
        compared = self._request("GET", f"{self.repo}/compare/{ancestor}...{commit}")
        return isinstance(compared, dict) and compared.get("status") == "ahead" and compared.get("behind_by") == 0

    def _capture(self):
        ev = self.provider.capture(self.pr)
        meta = ev.get("metadata", {})
        if meta.get("html_url", "").lower() != self.pr["url"].lower() or meta.get("number") != self.pr["number"]:
            raise PublicationError("provider returned a different PR")
        viewer = ev.get("viewer", {})
        if not isinstance(viewer, dict):
            viewer = {"login": viewer, "id": ev.get("viewer_id")}
            ev = dict(ev, viewer=viewer)
        _positive(viewer.get("id"), "authenticated actor")
        text(viewer.get("login"), "authenticated actor login")
        return ev

    @staticmethod
    def _kind(kind):
        if not isinstance(kind, str):
            raise PublicationError("unsupported publication operation")
        if kind in EXTENSIONS:
            raise PublicationError(f"{kind} is an extension kind owned by {EXTENSIONS[kind]}; this release denies it")
        if kind not in SCOPES:
            raise PublicationError("unsupported publication operation")
        return kind

    def _authorization(self, kind):
        # Authority is per contract action. An iteration or another action's
        # grant never stands in for it.
        action = SCOPES[kind][0]
        value = self.plan.get("authorizations", {}).get(action)
        if value is None:
            raise PublicationError(f"explicit user authorization required for {action}")
        auth(value)
        return value

    def _permission(self, kind):
        # Every permission defaults to false, including merge.
        permissions = self.plan.get("options", {}).get("permissions", {})
        if permissions.get(PERMISSIONS[kind]) is not True:
            raise PublicationError(f"configured permission forbids {kind}")

    def _select(self, ident):
        return self.ledger.db.execute(f"SELECT {_COLUMNS} FROM publication_intents WHERE id=?", (ident,)).fetchone()

    def _load(self, ident):
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise PublicationError("invalid publication operation ID")
        row = self._select(ident)
        if row is None:
            alias = self.ledger.db.execute(
                "SELECT canonical_id FROM publication_aliases WHERE id=?", (ident,)
            ).fetchone()
            if alias:
                row = self._select(alias[0])
        value = _row(row)
        if value["fingerprint"] != digest(value["intent"]):
            raise PublicationError("retained publication intent changed")
        if value["intent"].get("run") != self.plan["logical_run"] or value["intent"].get("pr") != self.pr:
            raise PublicationError("publication intent belongs to another run or PR")
        if value["intent"].get("kind") in ISSUE_KINDS and value["intent"].get("repository") != self.repository:
            raise PublicationError("publication intent belongs to another repository")
        response = self.ledger.db.execute(
            "SELECT value FROM publication_events WHERE intent=? AND event='response' ORDER BY seq DESC LIMIT 1",
            (value["id"],)).fetchone()
        value["response"] = _json(response[0]) if response else None
        return value

    def events(self, ident):
        """The retained, append-only history of one intent."""
        saved = self._load(ident)
        return [{"event": row[0], "value": _json(row[1])} for row in self.ledger.db.execute(
            "SELECT event,value FROM publication_events WHERE intent=? ORDER BY seq", (saved["id"],))]

    def _event(self, ident, event, value):
        # Called inside a ledger transaction.
        seq = self.ledger.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM publication_events WHERE intent=?",
                                     (ident,)).fetchone()[0]
        self.ledger.db.execute("INSERT INTO publication_events(intent,seq,event,value) VALUES(?,?,?,?)",
                               (ident, seq, event, canonical(value).decode()))

    @staticmethod
    def _pending_reviews(actor, ev):
        return [r for r in ev.get("reviews", []) if r.get("state") == "PENDING"
                and r.get("user", {}).get("id") == actor]

    def _live_binding(self, intent, ev, *, discussion=True):
        meta = ev["metadata"]
        if ev["viewer"]["id"] != intent["actor_id"]:
            raise Drift("authenticated actor changed")
        if meta.get("id") != intent["pr_id"]:
            raise Drift("numeric PR identity changed")
        if (meta.get("state") != "open" or meta.get("merged")
                or meta.get("draft") and intent["kind"] in ("merge", "approve_fork_ci")):
            raise Drift("publication requires the expected open PR")
        head, base = meta.get("head", {}), meta.get("base", {})
        expected = (intent["lease"], intent["target"], intent["branch"], intent["head_repository"])
        actual = (head.get("sha"), base.get("sha"), head.get("ref"),
                  str(head.get("repo", {}).get("full_name", "")).lower())
        if actual != expected:
            raise Drift("PR head, target, branch or repository lease changed")
        if intent["kind"] in REVIEW_KINDS:
            pending = self._pending_reviews(intent["actor_id"], ev)
            if pending:
                # Never discard, submit, overwrite or adopt another draft.
                raise PendingReview(sorted(r.get("id") for r in pending))
        if discussion and _discussion(ev) != intent["discussion"]:
            raise Drift("incoming discussion changed after preparation")
        if intent["kind"] == "reply_inline":
            destination = intent["payload"]["comment_id"]
            if sum(1 for c in ev.get("inline", []) if c.get("id") == destination) != 1:
                raise Drift("inline destination is not present in the exact PR")
        source = intent.get("source")
        if source and source.get("kind") == "findings":
            # Verified incoming-review findings (findings.py), not a feedback source.
            if intent["kind"] != "submit_review":
                raise PublicationError("only a review submission carries verified findings")
        elif source:
            kind = source.get("kind")
            key = {"comment": "comments", "inline": "inline", "review": "reviews"}.get(kind)
            if key is None:
                raise PublicationError("invalid original feedback kind")
            matches = [c for c in ev.get(key, []) if c.get("id") == source.get("id")]
            if len(matches) != 1 or digest(matches[0]) != source.get("fingerprint"):
                raise Drift("original feedback changed or disappeared")
            # A follow-up inline comment is answered at its thread root, the only reply target.
            if intent["kind"] == "reply_inline" and intent["payload"]["comment_id"] not in (
                    source["id"], matches[0].get("in_reply_to_id")):
                raise PublicationError("inline destination differs from original feedback")

    def _payload(self, kind, payload):
        if not isinstance(payload, dict):
            raise PublicationError("publication payload requires an object")
        exact = {
            "push": {"new_head"}, "rewrite": {"new_head"},
            "reply": {"body"}, "reply_inline": {"body", "comment_id"},
            "resolve_thread": {"thread_id", "reply_operation"},
            "ci_rerun": {"run_id", "attempt", "reason"},
            "approve_fork_ci": {"run_id", "attempt"},
            "merge": {"merge_method"},
            "split_push": {"branch", "new_head", "paths", "reason"},
            "split_pr": {"title", "body", "head", "base", "draft", "push_operation"},
            "submit_review": {"event", "body", "comments"},
            "approve_pr": {"body"},
        }
        if kind in exact:
            expected = exact[kind]
            # A user's explicit retry of specified failures (#35) names its own authorization.
            optional = {"split_pr": {"head_repo"}, "ci_rerun": {"user_retry"}}.get(kind, set())
            if not (set(payload) == expected or optional and set(payload) == expected | optional):
                raise PublicationError(f"{kind} requires exactly {sorted(expected)}")
        if kind == "edit_pr" and (not payload or not set(payload) <= {"title", "body"}):
            raise PublicationError("PR edits accept title/body only")
        if kind == "request_review" and (not payload or not set(payload) <= {"reviewers", "team_reviewers"}):
            raise PublicationError("review request requires explicit user/team lists")
        if kind in ISSUE_KINDS:
            return self._issue_payload(kind, payload)
        for key in ("body", "title", "reason"):
            if key in payload:
                if kind == "submit_review" and key == "body" and payload["comments"] and payload[key] == "":
                    # Inline findings need no body; an empty body is never padded with prose.
                    continue
                if kind == "approve_pr" and key == "body":
                    # An approval carries no operational prose; an empty body is the default.
                    if not isinstance(payload[key], str) or "\x00" in payload[key] or len(payload[key]) > 65536:
                        raise PublicationError("approval body must be bounded text without NUL")
                    continue
                if kind == "edit_pr" and key == "body":
                    if not isinstance(payload[key], str) or "\x00" in payload[key]:
                        raise PublicationError("PR body must be text without NUL")
                    payload[key].encode("utf-8", "strict")
                else:
                    text(payload[key], key)
        if kind in ("push", "rewrite", "split_push"):
            _sha(payload["new_head"], "published head")
        if kind == "reply_inline":
            _positive(payload["comment_id"], "inline comment ID")
        if kind == "resolve_thread":
            text(payload["thread_id"], "thread ID")
            text(payload["reply_operation"], "answered inline operation")
        if kind == "request_review":
            if not any(payload.values()):
                raise PublicationError("empty reviewer selection")
            for key, values in payload.items():
                _strings(values, key)
                if len(set(values)) != len(values) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in values):
                    raise PublicationError("invalid or duplicate reviewer name")
        if kind in ("ci_rerun", "approve_fork_ci"):
            _positive(payload["run_id"], "Actions run ID")
            _positive(payload["attempt"], "Actions attempt")
        if kind == "ci_rerun" and "user_retry" in payload:
            try:
                auth(payload["user_retry"])
            except ValueError as error:
                raise PublicationError("user_retry needs the exact request_id, instruction and source") from error
        if kind == "merge" and payload["merge_method"] not in ("merge", "squash"):
            raise PublicationError("merge receipt supports merge/squash with exact target parents")
        if kind == "split_push":
            _branch(payload["branch"])
            _strings(payload["paths"], "accepted split paths")
            if any(v.startswith("/") or ".." in v.split("/") or "\x00" in v for v in payload["paths"]):
                raise PublicationError("split paths must stay in the checkout")
        if kind == "split_pr":
            if payload["draft"] is not True:
                raise PublicationError("split PR must be a draft")
            _branch(payload["base"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_][A-Za-z0-9_./-]*", payload["head"]):
                raise PublicationError("split PR head requires exact owner:branch")
            _branch(payload["head"].split(":", 1)[1])
            text(payload["push_operation"], "reconciled split push")
            if "head_repo" in payload and not re.fullmatch(r"[A-Za-z0-9_.-]+", payload["head_repo"]):
                raise PublicationError("invalid exact split head repository")
        if kind == "submit_review":
            if payload["event"] not in REVIEW_EVENTS:
                raise PublicationError("review submission supports COMMENT and REQUEST_CHANGES; approval is the approve_pr extension")
            comments = payload["comments"]
            if not isinstance(comments, list) or len(comments) > 64:
                raise PublicationError("review comments require a bounded list")
            anchors = set()
            for comment in comments:
                if not isinstance(comment, dict) or set(comment) != {"path", "line", "side", "body"}:
                    raise PublicationError("review comment requires exactly path, line, side and body")
                path = text(comment["path"], "review comment path")
                if path.startswith("/") or ".." in path.split("/"):
                    raise PublicationError("review comment path must stay in the checkout")
                _positive(comment["line"], "review comment line")
                if comment["side"] not in ("LEFT", "RIGHT"):
                    raise PublicationError("review comment side must be LEFT or RIGHT")
                text(comment["body"], "review comment body")
                anchors.add((path, comment["line"], comment["side"]))
            if len(anchors) != len(comments):
                raise PublicationError("duplicate review comment anchor")
            if len(payload["body"]) > 65536 or any(len(comment["body"]) > 65536 for comment in comments):
                raise PublicationError("review text exceeds GitHub's body bound")
        return copy.deepcopy(payload)

    def _attempts(self, base):
        """The original intent and its fresh attempts, oldest first."""
        rows = []
        for number in range(FRESH_ATTEMPTS + 1):
            ident = base if number == 0 else f"{base}.r{number}"
            if not _ID.fullmatch(ident):
                raise PublicationError("invalid publication operation ID")
            row = self._select(ident)
            if row is None:
                break
            rows.append(self._load(ident))
        return rows

    def prepare(self, proposal):
        if not isinstance(proposal, dict):
            raise PublicationError("publication proposal requires an object")
        unknown = set(proposal) - {"id", "kind", "head", "lease", "target", "payload", "sources", "source"}
        if unknown:
            raise PublicationError(f"unsupported publication proposal fields: {sorted(unknown)}")
        kind = self._kind(proposal.get("kind"))
        authority = self._authorization(kind)
        self._permission(kind)
        payload = self._payload(kind, proposal.get("payload", {}))
        sources = _strings(proposal.get("sources"), "original report sources")
        if self.source_validator is None:
            raise PublicationError("accepted publication-source validation unavailable")
        for source in sources:
            if self.source_validator(source) is not True:
                raise PublicationError("publication source is not current accepted evidence")
        if kind in ISSUE_KINDS:
            return self._prepare_issue(proposal, kind, authority, payload, sources)
        if self.pr is None:
            raise PublicationError("a repository plan publishes issue kinds only")
        head = _sha(proposal.get("head"), "intent head")
        lease = _sha(proposal.get("lease", head), "head lease")
        target = _sha(proposal.get("target"), "target")
        semantic = {"kind": kind, "head": head, "lease": lease, "target": target,
                    "payload": payload, "sources": list(sources), "source": proposal.get("source")}
        requested_id = proposal.get("id")
        ident, supersedes = requested_id, None
        if requested_id is not None:
            if not isinstance(requested_id, str) or not _ID.fullmatch(requested_id):
                raise PublicationError("invalid publication operation ID")
            # A retained effect is immutable even after a push or merge changes
            # the live head. Return its identity; execute/checkpoint validation
            # verifies the effect without preparing a replacement publication.
            alias = self.ledger.db.execute(
                "SELECT canonical_id,fingerprint FROM publication_aliases WHERE id=?", (requested_id,)
            ).fetchone()
            if alias and alias[1] != digest(semantic):
                raise PublicationError("conflicting publication operation identity")
            attempts = self._attempts(alias[0] if alias else requested_id)
            if attempts:
                original = attempts[0]["intent"]
                if not alias and {key: original.get(key) for key in semantic} != semantic:
                    raise PublicationError("conflicting publication operation identity")
                latest = attempts[-1]
                if latest["state"] not in TERMINAL:
                    if latest["intent"]["authorization"] != authority:
                        raise PublicationError("retained publication authority changed")
                    return self._public(latest)
                # A terminal intent is never resent. A later attempt is a fresh
                # intent with its own preparation, baseline and audience.
                if len(attempts) > FRESH_ATTEMPTS:
                    raise PublicationError("fresh publication attempts exhausted for this proposal")
                ident = f"{attempts[0]['id']}.r{len(attempts)}"
                supersedes = latest["id"]
        ev = self._capture()
        meta = ev["metadata"]
        action, setting = SUPPORTED[kind]
        intent = {
            "run": self.plan["logical_run"], "pr": self.pr, "kind": kind,
            "pr_id": _positive(meta.get("id"), "numeric PR identity"),
            "head": head, "lease": lease, "target": target,
            "branch": _branch(meta.get("head", {}).get("ref")),
            "head_repository": str(meta.get("head", {}).get("repo", {}).get("full_name", "")).lower(),
            "actor_id": ev["viewer"]["id"], "payload": payload,
            "authorization": authority, "action": action, "permission": setting,
            "sources": list(sources), "discussion": _discussion(ev), "before": {},
        }
        if proposal.get("source") is not None:
            intent["source"] = copy.deepcopy(proposal["source"])
        if supersedes is not None:
            intent["supersedes"] = supersedes
        if kind not in ("push", "rewrite", "split_push") and head != lease:
            raise PublicationError("non-Git publication requires the current published head")
        self._live_binding(intent, ev)
        if kind in ("push", "rewrite") and payload["new_head"] != head:
            raise PublicationError("push intent head must equal its exact desired head")
        if kind in ("reply", "reply_inline"):
            key = "inline" if kind == "reply_inline" else "comments"
            intent["before"]["comment_ids"] = [_positive(c.get("id"), "comment ID") for c in ev.get(key, [])]
            matches = self._matching_replies(intent, ev)
            if len(matches) > 1:
                raise PublicationError("multiple identical replies require explicit recovery")
            if matches:
                intent["before"]["satisfied_comment_id"] = matches[0]["id"]
        elif kind == "edit_pr":
            intent["before"] = {key: meta.get(key) for key in payload}
        elif kind == "resolve_thread":
            threads = [thread for thread in ev.get("threads", []) if thread.get("id") == payload["thread_id"]]
            if len(threads) == 1:
                intent["before"]["thread"] = copy.deepcopy(threads[0])
        elif kind in REVIEW_KINDS:
            intent["before"]["review_ids"] = [_positive(r.get("id"), "review ID") for r in ev.get("reviews", [])]
            if kind == "approve_pr":
                self._not_own(ev)
        elif kind == "approve_fork_ci":
            self._not_own(ev)
            intent["before"]["run"] = self._fork_run(intent)
        elif kind == "ci_rerun" and "user_retry" in payload:
            self._check_user_retry(intent, authority)
        elif kind == "split_pr":
            pushed = self._load(payload["push_operation"])
            if pushed["state"] != "applied" or pushed["intent"]["kind"] != "split_push" or not self.validate(pushed["id"], pushed["receipt"]):
                raise PublicationError("split PR requires its current reconciled branch publication")
            split = pushed["intent"]
            owner = split["head_repository"].split("/", 1)[0]
            if payload["head"] != owner + ":" + split["payload"]["branch"]:
                raise PublicationError("split PR head differs from its reconciled push")
            head_name = split["head_repository"].split("/", 1)[1]
            if "head_repo" in payload and payload["head_repo"] != head_name:
                raise PublicationError("split head_repo differs from its pinned source repository")
            if owner == self.pr["owner"] and head_name != self.pr["name"] and payload.get("head_repo") != head_name:
                raise PublicationError("same-owner fork split requires exact head_repo")
            if payload["base"] != meta.get("base", {}).get("ref"):
                raise PublicationError("split PR target branch differs")
            intent["created_head"] = split["payload"]["new_head"]
            prs = self._pages(f"{self.repo}/pulls?state=all&per_page=100")
            intent["before"]["pr_numbers"] = [_positive(v.get("number"), "PR baseline number") for v in prs]
            if self.pr["number"] not in intent["before"]["pr_numbers"]:
                raise PublicationError("split PR baseline omitted the source PR")
        ident = ident or digest(intent)
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise PublicationError("invalid publication operation ID")
        fingerprint = digest(intent)
        with self.ledger.transaction():
            existing = self._select(ident)
            alias = self.ledger.db.execute(
                "SELECT canonical_id,fingerprint FROM publication_aliases WHERE id=?", (ident,)
            ).fetchone()
            if existing:
                saved = _row(existing)
                # Replaying an already prepared operation uses its original
                # baseline. No observation is allowed to rewrite that intent.
                self._assert_replay(saved["intent"], intent)
                return self._public(saved)
            if alias:
                saved = self._load(alias[0])
                self._assert_replay(saved["intent"], intent)
                return self._public(saved)
            if kind in ("reply", "reply_inline") and supersedes is None:
                rows = self.ledger.db.execute(
                    f"SELECT {_COLUMNS} FROM publication_intents ORDER BY rowid"
                ).fetchall()
                key = self._reply_key(intent)
                for row in rows:
                    saved = _row(row)
                    if (saved["state"] not in TERMINAL and saved["intent"].get("kind") in ("reply", "reply_inline")
                            and self._reply_key(saved["intent"]) == key):
                        self.ledger.db.execute(
                            "INSERT INTO publication_aliases(id,canonical_id,fingerprint) VALUES(?,?,?)",
                            (ident, saved["id"], digest(semantic)),
                        )
                        return self._public(saved)
            self.ledger.db.execute(
                "INSERT INTO publication_intents(id,fingerprint,value,state,receipt) VALUES(?,?,?,'prepared','')",
                (ident, fingerprint, canonical(intent).decode()),
            )
            self._event(ident, "prepared", {"fingerprint": fingerprint, "supersedes": supersedes})
        return {"id": ident, "fingerprint": fingerprint, "intent": intent, "state": "prepared"}

    @staticmethod
    def _assert_replay(saved, current):
        left, right = copy.deepcopy(saved), copy.deepcopy(current)
        for value in (left, right):
            value.pop("discussion", None)
            value.pop("before", None)
        if left != right:
            raise PublicationError("conflicting publication operation identity")

    @staticmethod
    def _public(value):
        return {key: value[key] for key in ("id", "fingerprint", "intent", "state")}

    @staticmethod
    def _reply_key(intent):
        return digest({key: intent.get(key) for key in ("run", "pr", "kind", "actor_id", "payload", "authorization", "source")})

    def _matching_replies(self, intent, ev):
        key = "inline" if intent["kind"] == "reply_inline" else "comments"
        payload = intent["payload"]
        return [c for c in ev.get(key, [])
                if isinstance(c.get("id"), int) and not isinstance(c.get("id"), bool)
                and c["id"] > 0 and c.get("body") == payload["body"]
                and c.get("user", {}).get("id") == intent["actor_id"]
                and (intent["kind"] != "reply_inline" or c.get("in_reply_to_id") == payload["comment_id"])]

    def _pages(self, path):
        # The transport owns complete API pagination, not an application loop.
        pages = getattr(self.provider, "pages", None)
        if pages is None:
            raise PublicationError("complete provider pagination is unavailable")
        result = pages(path)
        if not isinstance(result, list):
            raise PublicationError("invalid complete API inventory")
        return result

    def _git(self, *args, input=None):
        from .capture import run
        try:
            output = run(self.root, ["git", "-c", "credential.helper=", "-c",
                "credential.helper=!gh auth git-credential", "-c", "credential.interactive=false", *args], input)
        except RuntimeError as error:
            raise PublicationError("Git publication command failed") from error
        return output.decode("utf-8", "strict").strip()

    def _git_remote(self, intent):
        # Never trust a mutable local remote URL for publication.
        repo = intent["head_repository"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise PublicationError("invalid pinned head repository")
        return f"https://{self.pr['host']}/{repo}.git"

    def _git_observation(self, intent):
        branch = intent["payload"].get("branch", intent["branch"])
        ref = "refs/heads/" + _branch(branch)
        value = self._git("ls-remote", "--refs", self._git_remote(intent), ref)
        if not value:
            return None
        parts = value.split()
        if len(parts) != 2 or parts[1] != ref:
            raise PublicationError("ambiguous remote branch observation")
        return _sha(parts[0], "observed remote head")

    def _check_git(self, intent):
        kind, payload = intent["kind"], intent["payload"]
        wanted = payload["new_head"]
        if self._git("rev-parse", "--verify", wanted + "^{commit}") != wanted:
            raise PublicationError("exact publication commit unavailable")
        if kind in ("push", "rewrite"):
            if self._git("rev-parse", "HEAD") != wanted:
                raise PublicationError("checked local head changed before push")
            from .capture import _owned_exclusions
            exclusions = _owned_exclusions(self.root, self.plan)
            if self._git("status", "--porcelain=v1", "--untracked-files=all", "--", ".", *exclusions):
                raise PublicationError("publish only the clean accepted local head")
            current = self._git_observation(intent)
            if current != intent["lease"]:
                raise Drift("remote branch changed before push")
            if kind == "push":
                # merge-base --is-ancestor returns status one for a rewrite.
                self._git("merge-base", "--is-ancestor", intent["lease"], wanted)
        else:
            if payload["branch"] == intent["branch"] or self._git_observation(intent) is not None:
                raise Drift("split publication requires a new branch absence lease")
            changed = self._git("diff", "--name-only", "--no-renames", "-z", intent["target"], wanted)
            paths = [p for p in changed.split("\x00") if p]
            if not set(paths) <= set(payload["paths"]):
                raise PublicationError("split commit contains paths outside its accepted boundary")

    def _check_resolution(self, intent, ev):
        payload = intent["payload"]
        reply = self._load(payload["reply_operation"])
        if reply["state"] != "applied" or reply["intent"]["kind"] != "reply_inline":
            raise PublicationError("thread resolution requires a completed inline answer")
        receipt = reply["receipt"]
        if not receipt or not self._validate_receipt(reply, receipt, ev):
            raise PublicationError("completed inline answer is unavailable")
        threads = [t for t in ev.get("threads", []) if t.get("id") == payload["thread_id"]]
        if len(threads) != 1:
            raise PublicationError("selected review thread unavailable")
        thread = threads[0]
        comments = thread.get("comments", {})
        comments = comments.get("nodes", []) if isinstance(comments, dict) else comments
        root_id = reply["intent"]["payload"]["comment_id"]
        if not any(c.get("databaseId", c.get("id")) == root_id for c in comments):
            raise PublicationError("inline answer belongs to a different thread")
        return thread

    def _not_own(self, ev):
        """The actor never approves its own PR, nor one by a configured personal account."""
        author = ev["metadata"].get("user", {}).get("id")
        _positive(author, "PR author")
        accounts = self.plan.get("options", {}).get("accounts", [])
        if author == ev["viewer"]["id"] or any(
                str(account.get("host", "")).lower() == self.pr.get("host", "github.com")
                and account.get("id") == author for account in accounts):
            raise PublicationError("the authenticated account never approves its own PR")

    def _approval_gate(self, intent, ev):
        self._not_own(ev)
        if self.approval is None:
            raise PublicationError("current clean-review verification for approval unavailable")
        evidence = self.approval(intent)
        # Finding publication, merge readiness and a selected plan never imply approval.
        if not isinstance(evidence, dict) or evidence.get("approve") is not True:
            raise PublicationError("approval needs current complete clean independent review")
        if evidence.get("head") != intent["head"] or evidence.get("target") != intent["target"]:
            raise PublicationError("approval evidence belongs to another head or target")
        _strings(evidence.get("evidence"), "approval review evidence")

    @staticmethod
    def _user_retry(intent):
        """The one-shot slot of an explicit user retry: one exact attempt per run and request identity."""
        payload = intent["payload"]
        return (f"ci-user-retry:{payload['user_retry']['request_id']}:{payload['run_id']}",
                f"{intent['head']}:{payload['attempt']}")

    def _check_user_retry(self, intent, authority):
        if intent["payload"]["user_retry"] != authority:
            raise PublicationError("user_retry must be the retained explicit rerun_ci authorization")
        name, key = self._user_retry(intent)
        bound = self.ledger.db.execute("SELECT operation FROM reservations WHERE name=? AND operation<>? LIMIT 1",
                                       (name, key)).fetchone()
        if bound:
            raise PublicationError("this user retry request already binds another exact attempt of the run")

    def _reserve_rerun(self, intent):
        """Reserve the rerun's allowance once per exact attempt, before dispatch.

        An automatic rerun uses the retained per-head allowance; resuming or freshly
        preparing the same exact attempt never consumes another slot. An explicit user
        retry uses its own one-shot slot and leaves the automatic allowance untouched.
        """
        payload = intent["payload"]
        if "user_retry" in payload:
            self._check_user_retry(intent, intent["authorization"])
            name, key = self._user_retry(intent)
            try:
                self.ledger.reserve(name, key, 1)
            except ValueError as error:
                raise PublicationError("this user retry request already binds another exact attempt of the run") from error
            return
        limit = self.plan.get("options", {}).get("limits", {}).get("ci_reruns_per_head", 3)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 3:
            raise PublicationError("invalid retained per-head CI rerun allowance")
        try:
            self.ledger.reserve("ci-reruns:" + intent["head"], f"run:{payload['run_id']}:{payload['attempt']}", limit)
        except ValueError as error:
            raise PublicationError("retained per-head CI rerun allowance exhausted or changed; a bounded explicit "
                                   "user retry is required") from error

    def _fork_run(self, intent):
        """The exact attempt still waits for fork approval of this PR head, with no jobs or deployment gate."""
        payload = intent["payload"]
        path = f"{self.repo}/actions/runs/{payload['run_id']}"
        run = self._request("GET", path)
        if not isinstance(run, dict) or run.get("id") != payload["run_id"] or run.get("run_attempt") != payload["attempt"]:
            raise Drift("the workflow run attempt changed")
        reason = fork_gate(run, self.pr, intent["head"], intent["branch"], intent["head_repository"])
        if reason:
            raise Drift("fork approval no longer applies: " + reason)
        jobs = self._request("GET", f"{path}/attempts/{payload['attempt']}/jobs?per_page=100")
        if not isinstance(jobs, dict) or jobs.get("total_count") != 0:
            raise Drift("the run attempt has jobs; it is not waiting for fork approval")
        if self._request("GET", path + "/pending_deployments") != []:
            raise PublicationError("deployment gates are never approved")
        return {"id": run["id"], "attempt": run["run_attempt"], "workflow": str(run.get("path", ""))}

    def _fork_gate(self, intent, ev):
        self._not_own(ev)
        if self.fork_ci is None:
            raise PublicationError("current incoming-review verification for fork approval unavailable")
        evidence = self.fork_ci(intent)
        # Finding publication, PR approval, merge authority and successful CI never imply it.
        if not isinstance(evidence, dict) or evidence.get("ready") is not True:
            raise PublicationError("fork approval needs complete current incoming review")
        if evidence.get("head") != intent["head"] or evidence.get("target") != intent["target"]:
            raise PublicationError("fork approval evidence belongs to another head or target")
        _strings(evidence.get("evidence"), "fork approval review evidence")
        self._fork_run(intent)

    def _merge_gate(self, intent, ev):
        if self.readiness is None:
            raise PublicationError("live application merge-readiness verification unavailable")
        evidence = self.readiness(intent)
        # Completion of the selected plan never implies readiness.
        if not isinstance(evidence, dict) or evidence.get("ready") is not True:
            raise PublicationError("selected application work or live PR requirements incomplete")
        if evidence.get("head") != intent["head"] or evidence.get("target") != intent["target"]:
            raise PublicationError("merge-readiness evidence belongs to another head or target")
        if evidence.get("clean_review") is not True or evidence.get("verified") is not True:
            raise PublicationError("current clean review and independent verification required")
        ci = evidence.get("ci", {})
        if ci.get("head") != intent["head"] or ci.get("success") is not True:
            raise PublicationError("merge requires fresh exact-head successful CI")
        _strings(evidence.get("evidence"), "live readiness evidence")
        # Last live read before the lock and the write: requests or CI that
        # changed after preparation or readiness hold here, and the intent
        # stays prepared rather than blocked.
        repository_merge_requirements(self.provider, self.pr, ev["metadata"], intent["head"])

    def _gate(self, intent, ev, audience):
        kind = intent["kind"]
        try:
            authority = self._authorization(kind)
        except (PublicationError, ValueError) as error:
            raise Drift("retained user authority was withdrawn") from error
        if authority != intent["authorization"]:
            raise Drift("retained user authority changed")
        if SCOPES.get(kind) != (intent["action"], intent["permission"]):
            raise Drift("retained permission scope differs from this release")
        try:
            self._permission(kind)
        except PublicationError as error:
            raise Drift("configured permission no longer allows this write") from error
        if not isinstance(audience, dict) or audience.get("suitable") is not True or audience.get("fingerprint") != digest(intent):
            raise PublicationError("accepted audience review must name the exact payload fingerprint")
        _strings(audience.get("evidence"), "audience review evidence")
        text(audience.get("audience"), "intended audience")
        if self.source_validator is None:
            raise PublicationError("accepted publication-source validation unavailable")
        for source in intent["sources"]:
            if self.source_validator(source) is not True:
                raise Drift("publication source is stale or unaccepted")
        for source in audience["evidence"]:
            if self.source_validator(source) is not True:
                raise PublicationError("audience evidence is stale or unaccepted")
        if kind in ISSUE_KINDS:
            self._issue_binding(intent, ev)
            return
        self._live_binding(intent, ev)
        if kind in ("push", "rewrite", "split_push"):
            self._check_git(intent)
        elif kind == "resolve_thread":
            self._check_resolution(intent, ev)
        elif kind == "edit_pr":
            # Never overwrite a field someone else changed after preparation.
            meta = ev["metadata"]
            if any(_pr_field(key, meta.get(key)) not in (_pr_field(key, value), intent["payload"][key])
                   for key, value in intent["before"].items()):
                raise Drift("PR field changed after preparation")
        elif kind == "split_pr":
            pushed = self._load(intent["payload"]["push_operation"])
            if pushed["state"] != "applied" or not self.validate(pushed["id"], pushed["receipt"]):
                raise PublicationError("split branch changed before draft PR dispatch")
        elif kind == "ci_rerun":
            payload = intent["payload"]
            run = self._request("GET", f"{self.repo}/actions/runs/{payload['run_id']}")
            if run.get("head_sha") != intent["head"] or run.get("run_attempt") != payload["attempt"] or run.get("status") != "completed" or run.get("conclusion") in ("success", "neutral", "skipped"):
                raise PublicationError("rerun diagnosis no longer matches the failed exact attempt")
        elif kind == "merge":
            self._merge_gate(intent, ev)
        elif kind == "approve_pr":
            self._approval_gate(intent, ev)
        elif kind == "approve_fork_ci":
            self._fork_gate(intent, ev)

    def execute(self, ident, audience):
        saved = self._load(ident)
        state = saved["state"]
        if state == "applied":
            if not self.validate(saved["id"], saved["receipt"]):
                raise PublicationError("retained publication receipt no longer validates")
            return saved["receipt"]
        if state == "pending":
            # The original dispatch's own conclusive rejection settles it
            # before any observation, so it never adopts a later receipt.
            evidence = self._retained_rejection(saved)
            if evidence is not None:
                self._not_applied(saved, evidence)
                raise PublicationError(REJECTED_BY_PENDING_REVIEW)
            # Read-only reconciliation only. Absence or an unchanged service
            # never authorizes sending the original again.
            observed = self.reconcile(saved["id"])
            if observed["outcome"] == "completed":
                self._settle(saved, observed["result"])
                return observed["result"]
            raise UncertainEffect("publication may have taken effect; it stays pending until reconciliation observes it")
        if state in TERMINAL:
            raise PublicationError(f"publication intent is {state}; a later attempt needs fresh preparation")
        intent = saved["intent"]
        ev = self._current(intent)
        try:
            self._gate(intent, ev, audience)
        except Drift as error:
            self._block(saved, str(error))
            raise
        # Exact pre-existing effects do not dispatch again. This is particularly
        # useful for distinct IDs proposing the same original reply.
        observed = self._observe(saved, ev)
        if observed["outcome"] == "completed":
            self._settle(saved, observed["result"])
            return observed["result"]
        if intent["kind"] == "ci_rerun":
            self._reserve_rerun(intent)
        with self.ledger.transaction():
            holder = self.ledger.db.execute("SELECT intent FROM publication_lock WHERE singleton=1").fetchone()
            # Every pending intent blocks dispatch, even one that does not hold
            # the lock row (a migrated ledger can hold several).
            other = self.ledger.db.execute(
                "SELECT id FROM publication_intents WHERE state='pending' AND id<>? LIMIT 1", (saved["id"],)).fetchone()
            if holder and holder[0] != saved["id"] or other:
                raise UncertainEffect("publication lock is held by an unsettled intent; reconcile it first")
            cursor = self.ledger.db.execute(
                "UPDATE publication_intents SET state='pending',audience=? WHERE id=? AND state='prepared'",
                (canonical(audience).decode(), saved["id"]))
            if cursor.rowcount != 1:
                raise UncertainEffect("publication acquired by another invocation")
            self.ledger.db.execute("INSERT OR REPLACE INTO publication_lock(singleton,intent) VALUES(1,?)", (saved["id"],))
            self._event(saved["id"], "dispatch", {"audience": audience})
        try:
            response = self._dispatch(intent)
        except Rejected as rejection:
            self._not_applied(saved, rejection.evidence)
            raise PublicationError("the service conclusively rejected the write; it settles not_applied") from rejection
        except BaseException as error:
            # Retain the original failure before classifying it, so a restart
            # can still settle a conclusive rejection from it.
            failure = failure_record(error)
            try:
                with self.ledger.transaction():
                    self._event(saved["id"], "error", failure)
            except Exception:
                pass
            evidence = pending_review_rejection(intent, failure)
            if evidence is None:
                raise  # The intent stays pending.
            self._not_applied(saved, evidence)
            raise PublicationError(REJECTED_BY_PENDING_REVIEW) from error
        # Keep the exact acknowledged response before observation. If observation
        # is temporarily unavailable, the next reconciliation can use it.
        with self.ledger.transaction():
            self._event(saved["id"], "response", response)
        observed = self.reconcile(saved["id"])
        if observed["outcome"] != "completed":
            raise UncertainEffect("publication response retained; its effect is not yet verified")
        self._settle(saved, observed["result"])
        return observed["result"]

    def _retained_rejection(self, saved):
        """Conclusive rejection evidence retained from the original dispatch.

        Read-only. Needs exactly one dispatch, no acknowledged response and
        exactly one retained failure that the classifier accepts.
        """
        if saved["intent"].get("kind") not in REVIEW_KINDS:
            return None
        events = self.events(saved["id"])
        names = [event["event"] for event in events]
        if names.count("dispatch") != 1 or "response" in names or names.count("error") != 1:
            return None
        failure = next(event["value"] for event in events if event["event"] == "error")
        return pending_review_rejection(saved["intent"], failure)

    def _settle(self, saved, receipt):
        """Record the exact applied receipt once and release the lock."""
        key = effect_key(saved["intent"], receipt)
        with self.ledger.transaction():
            if key is not None:
                owner = self.ledger.db.execute("SELECT intent FROM publication_effects WHERE effect=?", (key,)).fetchone()
                if owner and owner[0] != saved["id"]:
                    raise PublicationError("duplicate receipt: this effect already settles another intent")
                self.ledger.db.execute("INSERT OR IGNORE INTO publication_effects(effect,intent) VALUES(?,?)", (key, saved["id"]))
            row = self.ledger.db.execute("SELECT state,receipt FROM publication_intents WHERE id=? AND fingerprint=?",
                                         (saved["id"], saved["fingerprint"])).fetchone()
            if row is None or row[0] not in ("prepared", "pending", "applied"):
                raise PublicationError("publication intent changed before receipt commit")
            encoded = canonical(receipt).decode()
            if row[0] == "applied":
                if row[1] != encoded:
                    raise PublicationError("duplicate receipt: the intent already has a different receipt")
                return
            self.ledger.db.execute("UPDATE publication_intents SET state='applied',receipt=? WHERE id=?",
                                   (encoded, saved["id"]))
            self._release(saved["id"])
            self._event(saved["id"], "applied", {"effect": key})

    def _release(self, ident):
        """Release the lock, handing it to any other pending intent."""
        self.ledger.db.execute("DELETE FROM publication_lock WHERE intent=?", (ident,))
        self.ledger.db.execute(
            "INSERT OR IGNORE INTO publication_lock(singleton,intent) "
            "SELECT 1,id FROM publication_intents WHERE state='pending' AND id<>? ORDER BY rowid LIMIT 1", (ident,))

    def _block(self, saved, reason):
        with self.ledger.transaction():
            changed = self.ledger.db.execute(
                "UPDATE publication_intents SET state='blocked' WHERE id=? AND state='prepared'", (saved["id"],))
            if changed.rowcount == 1:
                self._event(saved["id"], "blocked", {"reason": reason})

    def _not_applied(self, saved, evidence):
        if not isinstance(evidence, dict) or not evidence:
            raise UncertainEffect("rejection without retained evidence stays pending")
        with self.ledger.transaction():
            changed = self.ledger.db.execute(
                "UPDATE publication_intents SET state='not_applied' WHERE id=? AND state='pending'", (saved["id"],))
            if changed.rowcount != 1:
                raise UncertainEffect("publication state changed before settling its rejection")
            self._release(saved["id"])
            self._event(saved["id"], "rejected", evidence)

    def _dispatch(self, intent):
        kind, payload = intent["kind"], copy.deepcopy(intent["payload"])
        if kind in ("push", "rewrite", "split_push"):
            branch = payload.get("branch", intent["branch"])
            lease = "" if kind == "split_push" else intent["lease"]
            self._git("push", "--porcelain", f"--force-with-lease=refs/heads/{branch}:{lease}",
                      self._git_remote(intent), f"{payload['new_head']}:refs/heads/{branch}")
            return {"branch": branch, "head": payload["new_head"]}
        if kind == "edit_pr":
            return self._request("PATCH", self.pr_path, payload)
        if kind == "reply":
            return self._request("POST", self.issue_path + "/comments", payload)
        if kind == "reply_inline":
            return self._request("POST", f"{self.pr_path}/comments/{payload['comment_id']}/replies", {"body": payload["body"]})
        if kind == "resolve_thread":
            result = self._request("POST", "graphql", {"query": "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}", "variables": {"id": payload["thread_id"]}})
            if result.get("errors"):
                raise UncertainEffect("thread mutation returned GraphQL errors")
            return result
        if kind == "request_review":
            return self._request("POST", self.pr_path + "/requested_reviewers", payload)
        if kind == "ci_rerun":
            return self._request("POST", f"{self.repo}/actions/runs/{payload['run_id']}/rerun-failed-jobs")
        if kind == "approve_fork_ci":
            return self._request("POST", f"{self.repo}/actions/runs/{payload['run_id']}/approve")
        if kind == "submit_review":
            return self._request("POST", self.pr_path + "/reviews", {
                "commit_id": intent["head"], "event": payload["event"], "body": payload["body"],
                "comments": payload["comments"]})
        if kind == "approve_pr":
            return self._request("POST", self.pr_path + "/reviews", {
                "commit_id": intent["head"], "event": "APPROVE", "body": payload["body"], "comments": []})
        if kind == "merge":
            response = self._request("PUT", self.pr_path + "/merge", {**payload, "sha": intent["head"]})
            if response.get("merged") is not True:
                raise UncertainEffect("merge response did not confirm a merge")
            return response
        if kind == "split_pr":
            payload.pop("push_operation")
            return self._request("POST", self.repo + "/pulls", payload)
        if kind == "create_issue":
            return self._request("POST", self.repo + "/issues", payload)
        if kind == "edit_issue":
            number = payload.pop("number")
            return self._request("PATCH", f"{self.repo}/issues/{number}", payload)
        raise PublicationError("unsupported publication dispatch")

    def _receipt(self, saved, result):
        intent = saved["intent"]
        receipt = {"id": saved["id"], "fingerprint": saved["fingerprint"], "run": intent["run"],
                   "kind": intent["kind"], "pr": self.pr["url"] if self.pr else None, "head": intent.get("head"),
                   "actor_id": intent["actor_id"], "result": result,
                   "evidence": ["read-only exact service reconciliation"]}
        if intent["kind"] in ISSUE_KINDS:
            receipt["repository"] = intent["repository"]
        return receipt

    @staticmethod
    def _known(saved):
        """The acknowledged response, or the retained receipt's result."""
        if saved.get("response"):
            return saved["response"]
        return (saved.get("receipt") or {}).get("result") or {}

    def _observe(self, saved, ev):
        """Read-only: ``completed`` with an exact receipt, otherwise ``unknown``.

        Absence, an unchanged branch or an unrequested reviewer never proves
        that a dispatched write did not happen, so there is no ``no_effect``.
        """
        intent = saved["intent"]
        if intent["kind"] in ISSUE_KINDS:
            return self._observe_issue(saved, ev)
        kind, payload, meta = intent["kind"], intent["payload"], ev["metadata"]
        if ev["viewer"]["id"] != intent["actor_id"]:
            return {"outcome": "unknown", "proof": {"reason": "authenticated actor differs"}}
        found, result = False, None
        if kind in ("push", "rewrite", "split_push"):
            current = self._git_observation(intent)
            found = current == payload["new_head"]
            result = {"head": current, "branch": payload.get("branch", intent["branch"])}
        elif kind == "edit_pr":
            result = {k: _pr_field(k, meta.get(k)) for k in payload}
            found = result == payload
        elif kind in ("reply", "reply_inline"):
            wanted = intent["before"].get("satisfied_comment_id")
            known = self._known(saved)
            if known:
                wanted = _positive(known.get("id"), "acknowledged reply ID")
            matches = self._matching_replies(intent, ev)
            if wanted:
                matches = [v for v in matches if v["id"] == wanted]
            else:
                matches = [v for v in matches if v["id"] not in intent["before"]["comment_ids"]]
            if len(matches) == 1:
                found, result = True, matches[0]
            # Missing and ambiguous creations remain unknown.
        elif kind == "resolve_thread":
            thread = self._check_resolution(intent, ev)
            found = thread.get("isResolved") is True
            result = thread
        elif kind == "request_review":
            result = self._request("GET", self.pr_path + "/requested_reviewers")
            # GitHub matches logins and slugs case-insensitively and reports their canonical case.
            actual = {key: {v[name].lower() for v in result.get(field, []) if isinstance(v.get(name), str)}
                      for key, field, name in (("reviewers", "users", "login"), ("team_reviewers", "teams", "slug"))}
            found = all({v.lower() for v in values} <= actual[key] for key, values in payload.items())
            # Missing requests may have been satisfied, withdrawn or dismissed.
        elif kind == "ci_rerun":
            run = self._request("GET", f"{self.repo}/actions/runs/{payload['run_id']}")
            found = run.get("head_sha") == intent["head"] and run.get("run_attempt") == payload["attempt"] + 1
            # Same-attempt state can be a delayed request; no automatic replay.
            # Only stable fields: the rerun's status and timings move on after it is queued.
            result = {key: run.get(key) for key in ("id", "head_sha", "run_attempt")}
        elif kind == "approve_fork_ci":
            # GitHub names no approver: the same attempt executing jobs is the effect.
            # An advanced attempt, a still-gated or jobless run stays unknown.
            path = f"{self.repo}/actions/runs/{payload['run_id']}"
            run = self._request("GET", path)
            result = {"run": run}
            if (isinstance(run, dict) and run.get("id") == payload["run_id"]
                    and run.get("run_attempt") == payload["attempt"] and run.get("head_sha") == intent["head"]
                    and run.get("status") in FORK_EXECUTING and run.get("conclusion") != "action_required"):
                jobs = self._request("GET", f"{path}/attempts/{payload['attempt']}/jobs?per_page=100")
                count = jobs.get("total_count") if isinstance(jobs, dict) else None
                found = type(count) is int and count > 0
                # Only stable fields: the run's status and job count move on after approval.
                result = {"run": {key: run.get(key) for key in ("id", "run_attempt", "head_sha", "path")},
                          "executing": found}
        elif kind in REVIEW_KINDS:
            known = self._known(saved)
            wanted = known.get("review", known).get("id") if known else None
            state = APPROVED if kind == "approve_pr" else REVIEW_EVENTS[payload["event"]]
            matches = [r for r in ev.get("reviews", [])
                       if isinstance(r.get("id"), int) and not isinstance(r.get("id"), bool)
                       and r.get("user", {}).get("id") == intent["actor_id"] and r.get("state") == state
                       and r.get("body") == payload["body"] and r.get("commit_id") == intent["head"]]
            if wanted is not None:
                matches = [r for r in matches if r["id"] == wanted]
            else:
                matches = [r for r in matches if r["id"] not in intent["before"]["review_ids"]]
            if len(matches) == 1:
                review = matches[0]
                comments = self._request("GET", f"{self.pr_path}/reviews/{review['id']}/comments?per_page=100")
                if isinstance(comments, list) and len(comments) < 100:
                    observed = sorted((c.get("path"), c.get("line"), c.get("side"), c.get("body")) for c in comments)
                    expected = sorted((c["path"], c["line"], c["side"], c["body"]) for c in payload.get("comments", []))
                    found = observed == expected and all(c.get("user", {}).get("id") == intent["actor_id"] for c in comments)
                    result = {"review": review, "comments": comments}
        elif kind == "merge":
            merged = meta.get("merged") is True and meta.get("state") == "closed"
            merge_sha = meta.get("merge_commit_sha")
            if merged and meta.get("head", {}).get("sha") == intent["head"] and meta.get("merged_by", {}).get("id") == intent["actor_id"] and isinstance(merge_sha, str) and _SHA.fullmatch(merge_sha):
                commit = self._request("GET", f"{self.repo}/git/commits/{merge_sha}")
                parents = [p.get("sha") for p in commit.get("parents", [])]
                wanted = [parents[0] if parents else None] + ([intent["head"]] if payload["merge_method"] == "merge" else [])
                # The PUT pins only the head: a base that advanced before the merge is its first parent.
                found = commit.get("sha") == merge_sha and parents == wanted and (
                    parents[0] == intent["target"] or self._descends(parents[0], intent["target"]))
                # Only stable fields: later comments, labels or edits change the PR metadata.
                result = {"pr": {"number": meta.get("number"), "state": meta.get("state"), "merged": True,
                                 "merge_commit_sha": merge_sha, "head": {"sha": intent["head"]},
                                 "merged_by": {"id": intent["actor_id"]}}, "commit": commit}
        elif kind == "split_pr":
            wanted_number = self._known(saved).get("number")
            matches = []
            for pr in self._pages(f"{self.repo}/pulls?state=all&per_page=100"):
                if wanted_number is not None and pr.get("number") != wanted_number:
                    continue
                if wanted_number is None and pr.get("number") in intent["before"]["pr_numbers"]:
                    continue
                matches.append(pr)
            owner, branch = payload["head"].split(":", 1)
            matches = [v for v in matches if v.get("user", {}).get("id") == intent["actor_id"]
                       and v.get("state") == "open" and v.get("draft") is True
                       and v.get("title") == payload["title"] and v.get("body") == payload["body"]
                       and v.get("head", {}).get("sha") == intent["created_head"]
                       and v.get("head", {}).get("ref") == branch
                       and v.get("head", {}).get("repo", {}).get("full_name", "").lower() == intent["head_repository"]
                       and intent["head_repository"].split("/", 1)[0] == owner.lower()
                       and v.get("base", {}).get("sha") == intent["target"]
                       and v.get("base", {}).get("ref") == payload["base"]
                       and v.get("base", {}).get("repo", {}).get("full_name", "").lower() == f"{self.pr['owner']}/{self.pr['name']}".lower()]
            if len(matches) == 1:
                # Only the matched fields: comments and updated_at change the PR object.
                pr = matches[0]
                found, result = True, {
                    "number": pr.get("number"), "state": pr["state"], "draft": True, "title": pr["title"],
                    "body": pr["body"], "user": {"id": intent["actor_id"]},
                    **{side: {"sha": pr[side]["sha"], "ref": pr[side]["ref"],
                              "repo": {"full_name": pr[side]["repo"]["full_name"]}} for side in ("head", "base")}}
        proof = {"intent": saved["fingerprint"], "observation": digest(result), "kind": kind}
        if found:
            return {"outcome": "completed", "proof": proof, "result": self._receipt(saved, result)}
        return {"outcome": "unknown", "proof": proof}

    def reconcile(self, ident):
        """Read-only: return an observation without advancing local state.

        ``no_effect`` comes only from the journal: an intent that was never
        dispatched, was blocked before dispatch, was conclusively rejected, or
        retains its original dispatch's conclusive rejection.
        """
        saved = self._load(ident)
        if saved["state"] == "prepared" or saved["state"] in TERMINAL:
            return {"outcome": "no_effect", "proof": {"intent": saved["fingerprint"], "state": saved["state"]}}
        evidence = self._retained_rejection(saved) if saved["state"] == "pending" else None
        if evidence is not None:
            # From the original rejection, never from current absence.
            return {"outcome": "no_effect", "proof": {"intent": saved["fingerprint"], "state": "pending",
                                                      "rejection": evidence}}
        try:
            return self._observe(saved, self._current(saved["intent"]))
        except Exception as error:
            return {"outcome": "unknown", "proof": {"intent": saved["fingerprint"], "reason": str(error)}}

    def _validate_receipt(self, saved, receipt, ev):
        expected = self._receipt(saved, receipt.get("result"))
        if receipt != expected:
            return False
        observed = self._observe(dict(saved, response=receipt.get("result", {})), ev)
        if observed.get("outcome") != "completed":
            return False
        if saved["intent"]["kind"] in ("reply", "reply_inline"):
            # The same reply, even after reactions or metadata updates on it.
            return ({k: v for k, v in observed["result"].items() if k != "result"}
                    == {k: v for k, v in receipt.items() if k != "result"}
                    and reply_identity(observed["result"].get("result")) == reply_identity(receipt.get("result")))
        return observed.get("result") == receipt

    def validate(self, ident, receipt):
        """Read-only current exact-effect validation for action checkpoints."""
        try:
            saved = self._load(ident)
            return isinstance(receipt, dict) and self._validate_receipt(saved, receipt, self._current(saved["intent"]))
        except Exception:
            return False

    # -- Issue kinds (#32) ------------------------------------------------------------------------
    # Same journal, lock, audience, receipts and uncertainty states as every PR
    # kind. An intent binds the numeric repository, actor and (for an edit) issue,
    # the exact payload and its verified labels and assignees.

    def _current(self, intent):
        """The live read-only state an intent's binding and observation use."""
        if intent["kind"] in ISSUE_KINDS:
            return self._issue_capture(intent["kind"], intent["payload"])
        return self._capture()

    def _issue_capture(self, kind, payload):
        viewer = self._request("GET", "user")
        repository = self._request("GET", self.repo)
        if not isinstance(viewer, dict) or not isinstance(repository, dict):
            raise PublicationError("issue publication state unavailable")
        _positive(viewer.get("id"), "authenticated actor")
        if not isinstance(viewer.get("login"), str) or not _LOGIN.fullmatch(viewer["login"]):
            raise PublicationError("authenticated actor login unavailable")
        _positive(repository.get("id"), "numeric repository identity")
        ev = {"viewer": {"id": viewer["id"], "login": viewer["login"]}, "repository": {"id": repository["id"]}}
        if kind == "edit_issue":
            number = payload["number"]
            issue = self._request("GET", f"{self.repo}/issues/{number}")
            if (not isinstance(issue, dict) or issue.get("number") != number or "pull_request" in issue
                    or not isinstance(issue.get("title"), str)):
                raise PublicationError(f"issue {number} is unavailable or is a pull request")
            _positive(issue.get("id"), "numeric issue identity")
            ev["issue"] = issue
        return ev

    @staticmethod
    def _issue_value(key, value):
        """Comparable form of an issue field: label and assignee names sorted, an absent body empty."""
        if key == "labels":
            return sorted(item.get("name") if isinstance(item, dict) else item for item in value or [])
        if key == "assignees":
            return sorted(item.get("login") if isinstance(item, dict) else item for item in value or [])
        if key == "body":
            return value or ""
        return value

    def _issue_matches(self, issue, payload):
        return all(self._issue_value(key, issue.get(key)) == self._issue_value(key, payload[key])
                   for key in ISSUE_FIELDS if key in payload)

    def _issue_payload(self, kind, payload):
        allowed = set(ISSUE_FIELDS)
        if kind == "create_issue" and set(payload) != allowed:
            raise PublicationError(f"create_issue requires exactly {sorted(allowed)}")
        if kind == "edit_issue":
            if "number" not in payload or not set(payload) - {"number"} or not set(payload) <= allowed | {"number"}:
                raise PublicationError("edit_issue requires number and at least one of " + ", ".join(ISSUE_FIELDS))
            _positive(payload["number"], "issue number")
        if "title" in payload:
            text(payload["title"], "issue title")
            if len(payload["title"]) > 256 or "\n" in payload["title"]:
                raise PublicationError("issue title must be one bounded line")
        if "body" in payload and (not isinstance(payload["body"], str) or "\x00" in payload["body"]
                                  or len(payload["body"]) > 65536):
            raise PublicationError("issue body must be bounded text without NUL")
        for key in ("labels", "assignees"):
            if key in payload:
                values = payload[key]
                if (not isinstance(values, list) or len(values) > (10 if key == "assignees" else 100)
                        or len(set(map(str, values))) != len(values)):
                    raise PublicationError(f"issue {key} require a bounded list of distinct names")
                for value in values:
                    text(value, f"issue {key[:-1]}")
                    if key == "assignees" and not _LOGIN.fullmatch(value):
                        raise PublicationError("invalid issue assignee login")
        return copy.deepcopy(payload)

    def _issue_metadata(self, payload, error):
        """Every requested label and assignee exists in the target repository now."""
        for key, field in (("labels", "name"), ("assignees", "login")):
            wanted = payload.get(key) or []
            if wanted:
                known = {item.get(field) for item in self._pages(f"{self.repo}/{key}?per_page=100")
                         if isinstance(item, dict)}
                missing = sorted(set(wanted) - known)
                if missing:
                    raise error(f"issue {key} unavailable in the target repository: {missing}")

    def _own_issues(self, login):
        """The actor's issues in the repository, pull requests excluded."""
        result = []
        for issue in self._pages(f"{self.repo}/issues?state=all&creator={login}&per_page=100"):
            if isinstance(issue, dict) and "pull_request" not in issue:
                _positive(issue.get("number"), "issue number")
                if (issue.get("user") or {}).get("login") != login:
                    raise PublicationError("issue listing returned another creator")
                result.append(issue)
        return result

    def _prepare_issue(self, proposal, kind, authority, payload, sources):
        if any(key in proposal for key in ("head", "lease", "target")):
            raise PublicationError("issue writes bind the repository, not a PR head")
        semantic = {"kind": kind, "payload": payload, "sources": list(sources), "source": proposal.get("source")}
        ident, supersedes = proposal.get("id"), None
        if ident is not None:
            if not isinstance(ident, str) or not _ID.fullmatch(ident):
                raise PublicationError("invalid publication operation ID")
            attempts = self._attempts(ident)
            if attempts:
                if {key: attempts[0]["intent"].get(key) for key in semantic} != semantic:
                    raise PublicationError("conflicting publication operation identity")
                latest = attempts[-1]
                if latest["state"] not in TERMINAL:
                    if latest["intent"]["authorization"] != authority:
                        raise PublicationError("retained publication authority changed")
                    return self._public(latest)
                # A terminal intent is never resent; a later attempt is a fresh intent.
                if len(attempts) > FRESH_ATTEMPTS:
                    raise PublicationError("fresh publication attempts exhausted for this proposal")
                ident, supersedes = f"{attempts[0]['id']}.r{len(attempts)}", latest["id"]
        ev = self._issue_capture(kind, payload)
        action, setting = ISSUE_KINDS[kind]
        intent = {"run": self.plan["logical_run"], "pr": self.pr, "repository": self.repository, "kind": kind,
                  "repository_id": ev["repository"]["id"], "actor_id": ev["viewer"]["id"],
                  "actor_login": ev["viewer"]["login"], "payload": payload, "authorization": authority,
                  "action": action, "permission": setting, "sources": list(sources), "before": {}}
        if proposal.get("source") is not None:
            intent["source"] = copy.deepcopy(proposal["source"])
        if supersedes is not None:
            intent["supersedes"] = supersedes
        self._issue_metadata(payload, PublicationError)
        if kind == "edit_issue":
            issue = ev["issue"]
            intent["issue_id"] = issue["id"]
            intent["before"] = {key: self._issue_value(key, issue.get(key)) for key in ISSUE_FIELDS if key in payload}
        else:
            own = self._own_issues(ev["viewer"]["login"])
            intent["before"]["issue_numbers"] = sorted(issue["number"] for issue in own)
            # An identical open issue by the actor is this effect; it settles without a second write.
            same = [issue for issue in own if issue.get("state") == "open" and self._issue_matches(issue, payload)]
            if len(same) > 1:
                raise PublicationError("multiple identical issues require explicit recovery")
            if same:
                intent["before"]["satisfied_issue"] = same[0]["number"]
        ident = ident or digest(intent)
        fingerprint = digest(intent)
        with self.ledger.transaction():
            existing = self._select(ident)
            if existing:
                saved = _row(existing)
                self._assert_replay(saved["intent"], intent)
                return self._public(saved)
            self.ledger.db.execute(
                "INSERT INTO publication_intents(id,fingerprint,value,state,receipt) VALUES(?,?,?,'prepared','')",
                (ident, fingerprint, canonical(intent).decode()))
            self._event(ident, "prepared", {"fingerprint": fingerprint, "supersedes": supersedes})
        return {"id": ident, "fingerprint": fingerprint, "intent": intent, "state": "prepared"}

    def _issue_binding(self, intent, ev):
        if ev["viewer"]["id"] != intent["actor_id"]:
            raise Drift("authenticated actor changed")
        if ev["repository"]["id"] != intent["repository_id"]:
            raise Drift("numeric repository identity changed")
        self._issue_metadata(intent["payload"], Drift)
        if intent["kind"] == "edit_issue":
            issue = ev["issue"]
            if issue["id"] != intent["issue_id"]:
                raise Drift("numeric issue identity changed")
            # Never overwrite a field someone else changed after preparation.
            payload = intent["payload"]
            if any(self._issue_value(key, issue.get(key)) not in (value, self._issue_value(key, payload[key]))
                   for key, value in intent["before"].items()):
                raise Drift("issue field changed after preparation")

    def _observe_issue(self, saved, ev):
        """Read-only: ``completed`` with an exact receipt, otherwise ``unknown``.

        A missing issue never proves that a creation did not happen.
        """
        intent = saved["intent"]
        kind, payload = intent["kind"], intent["payload"]
        found, result = False, None
        if ev["viewer"]["id"] != intent["actor_id"] or ev["repository"]["id"] != intent["repository_id"]:
            return {"outcome": "unknown", "proof": {"reason": "authenticated actor or repository differs"}}
        if kind == "edit_issue":
            issue = ev["issue"]
            found = issue["id"] == intent["issue_id"] and self._issue_matches(issue, payload)
            result = {"number": issue["number"], "id": issue["id"],
                      **{key: self._issue_value(key, issue.get(key)) for key in ISSUE_FIELDS if key in payload}}
        else:
            known = self._known(saved)
            candidates = self._own_issues(intent["actor_login"])
            if known:
                # The acknowledged creation names its issue; a retained receipt names it by identity.
                wanted = _positive(known.get("number"), "acknowledged issue number")
                exact = "title" not in known or self._issue_matches(known, payload)
                candidates = [issue for issue in candidates if exact and issue["number"] == wanted]
            elif intent["before"].get("satisfied_issue"):
                candidates = [issue for issue in candidates if issue["number"] == intent["before"]["satisfied_issue"]
                              and self._issue_matches(issue, payload)]
            else:
                candidates = [issue for issue in candidates if issue["number"] not in intent["before"]["issue_numbers"]
                              and self._issue_matches(issue, payload)]
            if len(candidates) == 1:
                issue = candidates[0]
                found = True
                result = {"number": issue["number"], "id": issue.get("id"), "html_url": issue.get("html_url")}
            # Missing and ambiguous creations remain unknown.
        proof = {"intent": saved["fingerprint"], "observation": digest(result), "kind": kind}
        if found:
            return {"outcome": "completed", "proof": proof, "result": self._receipt(saved, result)}
        return {"outcome": "unknown", "proof": proof}
