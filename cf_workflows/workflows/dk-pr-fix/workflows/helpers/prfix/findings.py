"""Verified incoming-review findings as one exact review submission (#25).

Authored for cf-workflows; no upstream counterpart. See docs/review.md.

Incoming review never fixes a PR. With ``post_findings`` enabled and
authorized, its independently confirmed findings are published as one
``submit_review`` intent through the shared exact-intent boundary:

- A finding whose own location is a changed line of the PR diff is an inline
  comment on that line. No other line ever stands in for it.
- Every other substantive finding goes in the review body with its actual
  location: a commit message, the PR title or description, or an unchanged
  file line. ``include_summary: false`` removes only the optional overview
  sentence, never a finding. No finding is presented twice.
- Each finding carries its location and evidence links pinned to the reviewed
  head. Coverage gaps, withheld findings and execution details stay in the
  private listing result, never in review text.
- A partial review (incomplete coverage, or no ordinary reviewer) uses
  ``COMMENT``. Publishing findings never establishes a clean review, approval,
  fork-CI approval or merge readiness.
- Findings already carried by a prepared, pending or applied submission of this
  run, by identity or by exact location and text, are not proposed again.
  Retained unsettled submissions are listed for dispatch or read-only
  reconciliation and are never re-created.

Nothing here writes; ``publishing`` prepares and dispatches the listed proposal.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote

from .capture import PUBLICATION_SETTINGS as SETTINGS  # noqa: F401  (publication only; never bind analysis)
from .core import STATE_ENV, digest, read_artifact

LOCATIONS = {"diff": {"path", "line", "side"}, "file": {"path", "line"},
             "commit_message": {"commit"}, "pr_title": set(), "pr_description": set()}
MAX_INLINE = 64
MAX_BODY = 60000
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Workflow state locations and the exact formats of generated execution identities:
# agents (engine), tasks and default logical runs (prepare.py, dk_pr_review.py),
# claims and findings (app.py), and publication intents (publishing.py, findings.py).
# Only these generated forms match, so ordinary code identifiers stay publishable.
_PRIVATE = re.compile(
    r"\.agent-workflow|\.pr-fix-portable|cf-workflows-state|\.owned-by-cf-workflows"
    r"|cf-workflows/repositories/|\b(?:artifacts|requests|preparations)/[0-9a-f]{64}\.json"
    r"|\bagent_[0-9a-f]{32}\b|\btask_[0-9a-f]{24}\b|\b(?:pr|review)_[0-9a-f]{32}\b"
    r"|\b(?:claim|finding|findings|publication|push|merge)_[0-9a-f]{64}\b")


class LocationError(ValueError):
    """A finding names no location that exists in the reviewed PR."""


def _path(value):
    if (not isinstance(value, str) or not value or value.startswith("/") or "\0" in value
            or "\n" in value or "\r" in value or any(part in ("", ".", "..") for part in value.split("/"))):
        raise LocationError("location path must be a repository-relative file path")
    return value


def _line(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LocationError("location line must be a positive integer")
    return value


def location(raw):
    """The canonical form of a reviewer's finding location, or LocationError.

    Native wire reports carry every field, with null for the ones a kind does
    not use; those nulls are dropped here.
    """
    if not isinstance(raw, dict):
        raise LocationError("finding has no location")
    value = {key: item for key, item in raw.items() if item is not None}
    kind = value.get("kind")
    if kind not in LOCATIONS or set(value) != LOCATIONS[kind] | {"kind"}:
        raise LocationError("unsupported finding location")
    if "path" in value:
        _path(value["path"])
    if "line" in value:
        _line(value["line"])
    if kind == "diff" and value["side"] not in ("LEFT", "RIGHT"):
        raise LocationError("diff location side must be LEFT or RIGHT")
    if kind == "commit_message" and not (isinstance(value["commit"], str) and _SHA.fullmatch(value["commit"])):
        raise LocationError("commit location needs a full lowercase commit ID")
    return value


def changed_lines(root, base, head):
    """{(path, side, line)} of every added (RIGHT) and removed (LEFT) line from base to head."""
    from .app import git
    raw = git(root, "-c", "core.quotePath=false", "diff", "-U0", "--no-color", "--no-ext-diff",
              "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", base, head, "--").decode("utf-8", "replace")
    result, old, new, skip = set(), None, None, 0
    for line in raw.split("\n"):
        if skip:
            skip -= 1
            continue
        if line.startswith("diff --git "):
            old = new = None
        elif line.startswith("--- ") or line.startswith("+++ "):
            name = line[4:]
            name = name[:-1] if name.endswith("\t") else name
            # Quoted names (control characters) get no inline anchor.
            path = name[2:] if name.startswith(("a/", "b/")) else None
            if line.startswith("--- "):
                old = path
            else:
                new = path
        else:
            match = _HUNK.match(line)
            if match:
                start, count = int(match[1]), int(match[2] if match[2] is not None else 1)
                added_start, added = int(match[3]), int(match[4] if match[4] is not None else 1)
                if old:
                    result.update((old, "LEFT", n) for n in range(start, start + count))
                if new:
                    result.update((new, "RIGHT", n) for n in range(added_start, added_start + added))
                skip = count + added
    return result


def _lines(root, revision, path):
    """The number of lines of path at revision, or None when it is not a file there."""
    from .app import git
    kind = git(root, "cat-file", "-t", revision + ":" + path, check=False).decode().strip()
    if kind != "blob":
        return None
    data = git(root, "cat-file", "blob", revision + ":" + path)
    return data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)


def _commits(root, base, head):
    from .app import git
    return set(git(root, "rev-list", base + ".." + head).decode().split())


def _web(pr):
    return f"https://{pr['host']}/{pr['owner']}/{pr['name']}"


def _blob(pr, revision, path, line=None):
    return f"{_web(pr)}/blob/{revision}/{quote(path)}" + (f"#L{line}" if line else "")


def placement(root, snapshot, place, changed, commits):
    """Where one located finding is published: ("inline", comment) or ("body", label, link).

    Only a changed line of the PR diff is an inline anchor. Any other real
    location is cited in the body as it is; a location that does not exist in
    the reviewed PR raises LocationError rather than borrowing another one.
    """
    pr, head, base = snapshot["pr"], snapshot["head"], snapshot["base"]
    kind = place["kind"]
    if kind in ("diff", "file"):
        path, line = place["path"], place["line"]
        side = place.get("side", "RIGHT")
        if (path, side, line) in changed:
            return ("inline", {"path": path, "line": line, "side": side})
        revision = head if side == "RIGHT" else base
        size = _lines(root, revision, path)
        if size is None or line > size:
            raise LocationError("location is not a line of the reviewed revision")
        where = "" if side == "RIGHT" else f" before this PR (`{base[:12]}`)"
        return ("body", f"`{path}` line {line}{where}", _blob(pr, revision, path, line))
    if kind == "commit_message":
        if place["commit"] not in commits:
            raise LocationError("commit is not part of the reviewed PR")
        return ("body", f"Commit `{place['commit'][:12]}` message", f"{_web(pr)}/commit/{place['commit']}")
    return ("body", "PR title" if kind == "pr_title" else "PR description", pr["url"])


def key(place, finding_text):
    """Exact presentation identity: the same text at the same location is the same finding."""
    return digest({"location": place, "text": finding_text})


def _private(root, plan, finding_text):
    """Local execution details never enter public review text.

    The checkout and state namespace paths, workflow state references and
    generated identities. A user-chosen logical run name is ordinary text
    ("fix"); only the generated default form matches.
    """
    paths = {str(root)}
    raw = os.environ.get(STATE_ENV, "")
    if raw:
        paths.update({raw, os.path.realpath(raw)})
    return any(path and path in finding_text for path in paths) or _PRIVATE.search(finding_text) is not None


def _evidence(root, snapshot, report, finding):
    pr, head = snapshot["pr"], snapshot["head"]
    paths = {item["id"]: item["path"] for item in report.get("evidence", [])}
    links = []
    for source in finding["sources"]:
        path = paths.get(source)
        if path is None or _lines(root, head, path) is None:
            continue  # Never link a file the reviewed head does not contain.
        link = f"[`{path}`]({_blob(pr, head, path)})"
        if link not in links:
            links.append(link)
    return "Evidence: " + ", ".join(links) if links else ""


def _retained(ledger, plan):
    """This run's finding submissions (every attempt), oldest first."""
    from .publication import _row, _COLUMNS
    result = []
    for row in ledger.db.execute(f"SELECT {_COLUMNS} FROM publication_intents ORDER BY rowid"):
        saved = _row(row)
        intent = saved["intent"]
        source = intent.get("source")
        if (intent.get("kind") != "submit_review" or intent.get("run") != plan["logical_run"]
                or not isinstance(source, dict) or source.get("kind") != "findings"):
            continue
        result.append(saved)
    return result


def proposal_of(saved):
    """The exact proposal a retained intent was prepared from."""
    intent = saved["intent"]
    base = saved["id"].split(".r", 1)[0]
    return {"id": base, **{key: intent[key] for key in ("kind", "head", "lease", "target", "payload", "sources", "source")}}


def listing(root, provider, ledger, plan, publisher, validate_source):
    """Current eligible findings as at most one new submission, plus retained unsettled ones.

    Returns {items, reconcile, withheld, blockers}. ``withheld`` and
    ``blockers`` are private: they never enter a payload.
    """
    from . import app
    covered = app.coverage(root, provider, ledger, plan, {})
    snapshot, review = covered["snapshot"], covered["review"]
    result = {"items": [], "reconcile": [], "withheld": [], "blockers": [], "review": review}
    permissions = plan.get("options", {}).get("permissions", {})
    if permissions.get("post_findings") is not True or plan.get("authorizations", {}).get("post_findings") is None:
        result["blockers"].append("finding publication needs the post_findings permission and an explicit authorization")
        return result
    # Retained submissions reserve their findings. Pending ones are reconciled
    # read-only; prepared ones dispatch only while their sources are current.
    reserved_ids, reserved_keys, seen = set(), set(), set()
    for saved in _retained(ledger, plan):
        base = saved["id"].split(".r", 1)[0]
        latest = publisher._attempts(base)[-1]
        if latest["id"] != saved["id"] or base in seen:
            continue
        seen.add(base)
        state, proposal = latest["state"], proposal_of(latest)
        current = state == "applied" or all(validate_source(source, snapshot) for source in latest["intent"]["sources"])
        if state in ("pending", "applied") or state == "prepared" and current:
            for item in latest["intent"]["source"]["findings"]:
                reserved_ids.add(item["id"])
                reserved_keys.add(item["key"])
        if state == "pending":
            result["reconcile"].append(proposal)
            result["blockers"].append("finding publication " + latest["id"] + " is uncertain; it is only reconciled")
        elif state == "prepared" and current:
            result["items"].append(proposal)
    verifier = next((task for task in plan["tasks"] if task["kind"] == "verification"), None)
    verification = app._current(ledger, snapshot, verifier) if verifier else None
    if verification and not validate_source(verification["report"], snapshot):
        verification = None
    dispositions, verifier_agent = {}, None
    if verification:
        # Only the current independent verification counts; earlier verdicts are history.
        verifier_agent = verification["agent"]
        for item in read_artifact(root, verification["report"])["report"].get("dispositions", []):
            dispositions[item.get("finding_id")] = item.get("status")
    changed = changed_lines(root, snapshot["base"], snapshot["head"])
    commits = _commits(root, snapshot["base"], snapshot["head"])
    inline, body, chosen, sources = [], [], [], set()
    for candidate in app.candidates(root, ledger, plan):
        reasons = []
        task = next((t for t in plan["tasks"] if t["id"] == candidate["task"]), None)
        if task is None or task["kind"] != "review":
            continue  # Captured feedback from others is never republished.
        current = app._current(ledger, snapshot, task)
        report = None
        claim = read_artifact(root, candidate["report"])["claim"]
        if not current or current["id"] != claim or not validate_source(current["report"], snapshot):
            reasons.append("stale")
        else:
            report = read_artifact(root, current["report"])["report"]
            if not any(f.get("id") == candidate["original_id"] and f.get("text") == candidate["text"]
                       and f.get("sources") == candidate["sources"] for f in report.get("findings", [])):
                reasons.append("stale")
        status = dispositions.get(candidate["id"])
        if status == "rejected" or candidate["id"] in reserved_ids:
            continue  # Rejected, or already carried by a retained submission.
        if status != "confirmed":
            reasons.append("unverified")
        if verifier_agent is not None and verifier_agent == candidate["agent"]:
            reasons.append("not_independent")
        if _private(root, plan, candidate["text"]):
            reasons.append("private_details")
        place = placed = None
        try:
            place = location(candidate.get("location"))
            placed = placement(root, snapshot, place, changed, commits)
        except LocationError as error:
            reasons.append("location: " + str(error))
        if not reasons:
            identity = key(place, candidate["text"])
            if identity in reserved_keys:
                continue
            if placed[0] == "inline" and len(inline) >= MAX_INLINE:
                reasons.append("exceeds one review's inline bound; a later run publishes it")
            elif placed[0] == "inline" and any((c["path"], c["line"], c["side"]) ==
                                               (placed[1]["path"], placed[1]["line"], placed[1]["side"]) for c in inline):
                reasons.append("another finding already uses this inline anchor; a later run publishes it")
        if reasons:
            result["withheld"].append({"finding": candidate["id"], "reasons": reasons})
            if status == "confirmed" and "stale" not in reasons:
                # Superseded reports are history; any other confirmed finding left out is a visible gap.
                result["blockers"].append("verified finding " + candidate["id"] + " was not published: "
                                          + "; ".join(reasons))
            continue
        reserved_keys.add(identity)
        evidence = _evidence(root, snapshot, report, candidate)
        if placed[0] == "inline":
            inline.append({**placed[1], "body": candidate["text"] + ("\n\n" + evidence if evidence else "")})
        else:
            _, label, link = placed
            lines = candidate["text"].strip().split("\n")
            entry = f"- **[{label}]({link})**: " + "\n  ".join(lines)
            body.append(entry + ("\n  " + evidence if evidence else ""))
        chosen.append({"id": candidate["id"], "key": identity})
        sources.add(current["report"])
    if not chosen:
        return result
    sources.add(verification["report"])
    parts = []
    if plan.get("settings", {}).get("include_summary") is True:
        parts.append(f"{len(chosen)} independently verified finding{'s' if len(chosen) != 1 else ''}: "
                     f"{len(inline)} inline, {len(body)} below.")
    parts.extend(body)
    text = "\n\n".join(parts)
    if len(text) > MAX_BODY:
        result["blockers"].append("verified findings exceed one review body; nothing new is proposed")
        return result
    # A partial review only comments. Requesting changes needs complete coverage
    # with an ordinary reviewer, and never applies to the actor's own PR.
    complete = review["outcome"] == "findings" and not review["partial"]
    # Like capture: the actor's PR, or one by a configured personal account on its host.
    pr = snapshot["pr"]
    own = snapshot["author_id"] == snapshot["actor_id"] or any(
        str(account.get("host", "")).lower() == pr["host"] and account.get("id") == snapshot["author_id"]
        for account in plan.get("options", {}).get("accounts", []))
    blocking = complete and not own and plan.get("settings", {}).get("request_changes", True) is not False
    payload = {"event": "REQUEST_CHANGES" if blocking else "COMMENT", "body": text, "comments": inline}
    source = {"kind": "findings", "findings": chosen}
    proposal = {"kind": "submit_review", "head": snapshot["head"], "lease": snapshot["head"],
                "target": snapshot["target"], "payload": payload, "sources": sorted(sources), "source": source}
    proposal = {"id": "findings_" + digest([plan["logical_run"], proposal]), **proposal}
    result["items"].append(proposal)
    return result
