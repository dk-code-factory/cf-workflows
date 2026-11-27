"""Retained, replay-safe preparation operations. No agent or remote mutations.

Adapted from Agent Workflow's portable PR-fix example (MIT; see LICENSE). The
private request is read from the mounted state namespace, never the checkout.
"""
import hashlib
import os
import re

from .core import (STATE_ENV, STATE_MARKER, artifact, atomic_json, bounded_json, digest,
                   read_artifact, state_directory, text)

REQUEST = re.compile(r"requests/[a-f0-9]{64}\.json")


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {"pr", "logical_run", "instructions", "authorizations", "maintain_other"}:
        raise ValueError("request supports PR identity, instructions, logical_run and authorizations only; move orchestration to YAML inputs")
    from .core import identity, auth
    identity(request.get("pr"))
    text(request.get("instructions"), "exact original instruction")
    for value in request.get("authorizations", {}).values():
        auth(value)
    if "maintain_other" in request:
        auth(request["maintain_other"])
    return request


def handle(operation, request, root, provider):
    phase = request["phase"]
    inputs = request.get("input")
    context = request.get("context", {})
    key = text(context.get("operation_key"), "engine operation identity")
    execution = text(context.get("execution_fingerprint"), "admitted execution fingerprint")
    if not isinstance(inputs, dict):
        raise ValueError("preparation input must be an object")
    ident = digest([operation, key])
    fingerprint = digest([operation, inputs, execution])
    response = {"protocol": "action-v1", "phase": phase}
    # Read-only phases must neither initialize state nor read a mutable request
    # again after its first snapshot. The engine pins inputs and helper bundles.
    saved = None
    base = None
    raw = os.environ.get(STATE_ENV, "")
    if raw and os.path.lexists(os.path.join(raw, STATE_MARKER)):
        base = state_directory(root)
        directory = base / "preparations"
        if directory.is_symlink():
            raise ValueError("preparation directory must not be a symlink")
        receipt = directory / (ident + ".json")
        if receipt.is_symlink():
            raise ValueError("preparation receipt must not be a symlink")
        if receipt.exists():
            saved = bounded_json(receipt.read_bytes())
            if saved.get("input") != fingerprint:
                raise ValueError("preparation identity conflict")
            value = read_artifact(root, saved["artifact"])
            if value.get("input") != fingerprint:
                raise ValueError("preparation evidence differs")
            saved = value["result"]
    if phase == "bindings":
        return {**response, "capabilities": ["bindings", "execute", "validate_checkpoint", "reconcile"],
                "read_only_phases": ["bindings", "validate_checkpoint", "reconcile"],
                "bindings": {"input": fingerprint, "execution_fingerprint": execution}}
    if phase == "reconcile":
        if saved is not None:
            return {**response, "outcome": "completed", "proof": {"preparation": ident}, "result": saved}
        # Only immutable local artifacts/ledger initialization can precede the
        # atomic receipt. Repeating reads cannot dispatch or publish effects.
        return {**response, "outcome": "no_effect", "proof": {"no_dispatch_or_publication": ident}}
    if phase == "validate_checkpoint":
        return {**response, "valid": saved is not None and saved == request.get("checkpoint", {}).get("result")}
    if saved is not None:
        return {**response, "result": saved}
    base = state_directory(root, create=True)
    if operation == "snapshot_request":
        if set(inputs) != {"request"}:
            raise ValueError("snapshot requires request path")
        output = {"snapshot": _snapshot(root, base, inputs["request"], ident, fingerprint)}
    elif operation == "entry_preset":
        # A direct publication entry's routing (publish.yml): its declared preset for the
        # exact staged request, or a rejection before any session or effect.
        if set(inputs) != {"request", "entry"}:
            raise ValueError("entry preset requires request path and entry")
        from .prepare import ENTRY_PRESETS, entry_preset
        request = read_artifact(root, _snapshot(root, base, inputs["request"], ident, fingerprint))
        preset = entry_preset(text(inputs["entry"], "entry"), request.get("authorizations", {}))
        output = {"preset": preset, "mode": ENTRY_PRESETS[preset][0]}
    elif operation == "iter_route":
        # cf-workflows:dk-pr-iter (iter.yml): the route preflight chose, confirmed against
        # current identities and the exact staged request before either entry runs.
        if set(inputs) != {"request", "route", "identity", "accounts", "permissions"}:
            raise ValueError("iter route requires request, route, identity, accounts and permissions")
        from .iteration import identity_provider, route_operation
        request = validate_request(read_artifact(root, _snapshot(root, base, inputs["request"], ident, fingerprint)))
        output = route_operation(identity_provider(root, provider), request, inputs)
    elif operation == "prepare":
        if set(inputs) != {"snapshot", "controls"}:
            raise ValueError("prepare requires immutable snapshot and YAML controls")
        from .prepare import prepare
        snapshot = validate_request(read_artifact(root, inputs["snapshot"]))
        output = prepare(root, snapshot, inputs["controls"], execution, provider)
    elif operation == "approvers_collect":
        # cf-workflows:dk-pr-approvers (approvers.yml): read-only evidence, no request or approval.
        if set(inputs) != {"snapshot", "head", "override"}:
            raise ValueError("approver collection requires snapshot, head and override")
        from .approvers import collect_operation, evidence_provider
        request = validate_request(read_artifact(root, inputs["snapshot"]))
        output = collect_operation(root, evidence_provider(root, provider), request, inputs)
    elif operation == "approvers_check":
        if set(inputs) != {"collected", "session"}:
            raise ValueError("approver check requires collected evidence and the session result")
        from .approvers import check_operation, evidence_provider
        output = check_operation(root, evidence_provider(root, provider), inputs)
    elif operation == "flakes_collect":
        # cf-workflows:dk-ci-flakes (flakes.yml): read-only evidence; it waives, saves and writes nothing.
        if set(inputs) != {"snapshot", "options", "targets"}:
            raise ValueError("flake collection requires snapshot, options and targets")
        from .flakes import collect_operation, evidence_provider
        request = validate_request(read_artifact(root, inputs["snapshot"]))
        output = collect_operation(root, evidence_provider(root, provider), request, inputs)
    elif operation == "flakes_check":
        if set(inputs) != {"collected", "session"}:
            raise ValueError("flake check requires collected evidence and the session result")
        from .flakes import check_operation
        output = check_operation(root, inputs)
    else:
        raise ValueError("unknown preparation operation")
    result = {"output": output, "evidence": {"preparation": ident, "input": fingerprint}}
    reference = artifact(root, {"input": fingerprint, "result": result})
    atomic_json(base / "preparations" / (ident + ".json"), {"input": fingerprint, "artifact": reference})
    return {**response, "result": result}


def _snapshot(root, base, relative, ident, fingerprint):
    """The retained snapshot reference of a staged request; the first complete read wins."""
    relative = text(relative, "request path")
    if not REQUEST.fullmatch(relative):
        raise ValueError("request must be a staged requests/<sha256>.json in the state namespace")
    path = base / relative
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("request must not traverse symlinks")
    # Retain the first complete read before subsequent result bookkeeping.
    # A lost receipt cannot silently replace the exact original request.
    staged = base / "preparations" / (ident + ".request.json")
    if staged.is_symlink():
        raise ValueError("request snapshot receipt must not be a symlink")
    if staged.exists():
        retained = bounded_json(staged.read_bytes())
        if retained.get("input") != fingerprint:
            raise ValueError("request snapshot identity conflict")
        reference = retained["snapshot"]
        validate_request(read_artifact(root, reference))
    else:
        if not path.is_file() or path.stat().st_size > 1 << 20:
            raise ValueError("request must be a bounded regular JSON file")
        data = path.read_bytes()
        if path.name != hashlib.sha256(data).hexdigest() + ".json":
            raise ValueError("staged request differs from its content name")
        snapshot = validate_request(bounded_json(data))
        reference = artifact(root, snapshot)
        atomic_json(staged, {"input": fingerprint, "snapshot": reference})
    return reference
