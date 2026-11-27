"""One-shot CI observation and exact-attempt exception verification.

Adapted from Agent Workflow's portable PR-fix example (MIT; see LICENSE).

Polling, repair selection and repetition live in YAML. Unknown failures remain
blocking; an agent's classification is a candidate, never authority by itself.

Issue-backed flakes (#35) are learned only from a failed check's own exact
attempt: the tests its failed-step logs name, and open issues of the PR
repository that declare exactly that workflow, job, test and signature. Only
a trusted author's declaration counts (repository owner, member or
collaborator, or a configured personal account; never the PR author), and only
one opened before the failed run. Each
failed check is verified on its own. Evidence that is unavailable, ambiguous or
does not affirm the exact attempt leaves that check failed and is reported in
``rejected``; it never waives a failure, and one check's evidence never exempts
another. The observation window is an absolute retained deadline with at most
one pending-only extension. A configured classification timeout and absent-check
grace are retained windows too; none of them restarts on resume.

Observed named outcomes go to the shared cross-PR registry (prfix/registry.py):
failures that learning reads, and passes of later successful attempts at a head
with a saved failure. A trusted open issue that declares an exact context
without affirming it (``dk-flaky`` without ``"flaky": true``) nominates a flake
only with saved fail-then-pass history at one commit, and verification rereads
both exact attempts before the failure is accepted.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import re
import time
from urllib.parse import quote, urlsplit

from .core import bounded_json, digest, text
from .registry import MAX_LINE, MAX_LINES, Registry


SUCCESS = {"success", "neutral", "skipped"}
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s")
NAMED_FAILURE = re.compile(r"^(?:--- FAIL: |FAILED )([^\s(]+)")
NAMED_PASS = re.compile(r"^(?:--- PASS: |PASSED )([^\s(]+)")
GO_SUMMARY = re.compile(r"^(?:FAIL(?:\s+\S+(?:\s+[0-9.]+s)?)?|PASS|ok\s+\S+\s+(?:[0-9.]+s|\(cached\))|\?\s+\S+\s+\[no test files\]|exit status [1-9][0-9]*)$")
MAKE_FAILURE = re.compile(r"^(?:make(?:\[[0-9]+\])?: \*\*\* \[[^\]]+: test\] Error [1-9][0-9]*|(?:##\[error\])?(?:Error: )?Process completed with exit code [1-9][0-9]*\.)$")
GO_LOCATION = re.compile(r"^\s+[^\s]+\.go:[0-9]+:\s*")
# Bounds of automatic flake learning for one failed check (baseline dk-ci-flakes max_issues).
LEARN_TESTS = 20
LEARN_ISSUES = 500
# Issue authors whose flaky declarations a PR's own CI may learn from (GitHub author_association).
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
# Observation controls with their bounds (baseline dk-ci options; docs/ci-skills.md).
WINDOWS = {"poll_interval_seconds": (1, 3600), "classification_timeout_seconds": (1, 3600),
           "no_checks_grace_seconds": (0, 86400)}
# Evidence failures that leave a check failed; anything else (a programming or fixture error) propagates.
EVIDENCE_ERRORS = (ValueError, KeyError, TypeError, OSError, RuntimeError)


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(label + " requires a positive integer")
    return value


def _tuple(value):
    return tuple(str(value.get(k, "")) for k in ("head", "check_id", "run_id", "job_id", "attempt"))


def _configured_object(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("unsupported CI " + label + " fields")
    for key in required:
        text(value.get(key), "CI " + label + " " + key)


def _configured_classification(value):
    common = {"check_id", "run_id", "job_id", "attempt", "kind", "workflow", "job"}
    _configured_object(value, common | {"test", "signature", "issue", "rule"},
                       ("check_id", "kind", "workflow", "job"), "classification")
    for key in ("run_id", "job_id"):
        ident = value.get(key)
        if type(ident) not in {str, int} or not str(ident).isdigit() or int(ident) < 1:
            raise ValueError("CI classification requires an exact positive " + key)
    _integer(value.get("attempt"), "CI classification attempt")
    if value["kind"] == "flake":
        if "rule" in value:
            raise ValueError("CI flake cannot declare an exception rule")
        for key in ("test", "signature", "issue"):
            text(value.get(key), "CI flake " + key)
        link = urlsplit(value["issue"])
        if link.scheme != "https" or not link.netloc or link.query or link.fragment:
            raise ValueError("CI flake requires a canonical HTTPS issue URL")
    elif value["kind"] == "exception":
        text(value.get("rule"), "CI exception rule")
        if "issue" in value:
            raise ValueError("CI exception cannot declare a flake issue")
        for key in ("test", "signature"):
            if key in value:
                text(value[key], "CI exception " + key)
    else:
        raise ValueError("unsupported CI classification kind")


def _configured_rule(value):
    _configured_object(value, {"id", "kind", "workflow", "job", "reason", "test", "signature"},
                       ("id", "kind", "workflow", "reason"), "exception")
    if "job" in value:
        text(value["job"], "CI exception job")
    if value["kind"] == "test":
        text(value.get("test"), "CI test exception test")
        if "signature" in value:
            text(value["signature"], "CI test exception signature")
    elif value["kind"] != "pipeline" or {"test", "signature"} & value.keys():
        raise ValueError("unsupported CI exception kind or test controls")


def configuration(options):
    """Validate declared controls before admission or any CI observation."""
    selected = options.get("ci", {})
    if not isinstance(selected, dict) or set(selected) - {"timeout_seconds", "classifications", "exceptions",
                                                          "pending_extension_seconds", "max_pending_extensions",
                                                          *WINDOWS}:
        raise ValueError("unsupported CI controls; expected timeout_seconds/pending_extension_seconds/"
                         "max_pending_extensions/poll_interval_seconds/classification_timeout_seconds/"
                         "no_checks_grace_seconds/classifications/exceptions object")
    for key, (low, high) in WINDOWS.items():
        if key in selected:
            value = selected[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"CI {key} must be an integer in {low}..{high}")
    for values, key in ((selected, "timeout_seconds"), (options, "ci_timeout_seconds")):
        if key in values:
            timeout = values[key]
            if type(timeout) is not int or not 1 <= timeout <= 86400:
                raise ValueError("CI timeout must be an integer in 1..86400 seconds")
    if "pending_extension_seconds" in selected:
        value = selected["pending_extension_seconds"]
        if type(value) is not int or not 0 <= value <= 86400:
            raise ValueError("CI pending extension must be an integer in 0..86400 seconds")
    if "max_pending_extensions" in selected:
        value = selected["max_pending_extensions"]
        if type(value) is not int or value not in (0, 1):
            raise ValueError("CI max_pending_extensions must be 0 or 1")
    for values, classification_key, exception_key in (
            (selected, "classifications", "exceptions"), (options, "ci_classifications", "ci_exceptions")):
        if classification_key in values:
            candidates = values[classification_key]
            if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
                raise ValueError("CI classifications must be an array of objects")
            for candidate in candidates:
                _configured_classification(candidate)
        if exception_key in values:
            rules = values[exception_key]
            if isinstance(rules, dict):
                if set(rules) != {"entries"}:
                    raise ValueError("CI exception wrapper requires only entries")
                rules = rules["entries"]
            if not isinstance(rules, list) or any(not isinstance(item, dict) for item in rules):
                raise ValueError("CI exceptions must contain an array of objects")
            seen = set()
            for rule in rules:
                _configured_rule(rule)
                if rule["id"] in seen:
                    raise ValueError("duplicate CI exception ID")
                seen.add(rule["id"])
    return _selection(options)


def _selection(options):
    """Resolve admitted controls, including retained legacy option spellings."""
    selected = options.get("ci", {})
    rules = selected.get("exceptions", options.get("ci_exceptions", []))
    return {"timeout_seconds": selected.get("timeout_seconds", options.get("ci_timeout_seconds", 1800)),
            # Off unless configured; cf-workflows:dk-ci passes its baseline one-extension default.
            "pending_extension_seconds": selected.get("pending_extension_seconds", 1800),
            "max_pending_extensions": selected.get("max_pending_extensions", 0),
            # The composed default keeps the original 10-second poll; dk-ci passes its baseline 30.
            "poll_interval_seconds": selected.get("poll_interval_seconds", 10),
            # Unbounded classification and no absent-check grace unless configured: absent checks
            # then stay pending until the retained deadline. dk-ci passes its baseline values.
            "classification_timeout_seconds": selected.get("classification_timeout_seconds"),
            "no_checks_grace_seconds": selected.get("no_checks_grace_seconds"),
            "classifications": selected.get("classifications", options.get("ci_classifications", [])),
            "exceptions": rules.get("entries", []) if isinstance(rules, dict) else rules}


def classify(observation, exceptions=None):
    """Classify an exact inventory using independently verified proof objects.

    ``exceptions`` must come from ``verify_exceptions`` at this boundary, rather
    than from a leaf's JSON. Proofs retain the precise source and complete failed
    step inventory; callers persist that original evidence for checkpoint use.
    """
    head = text(observation.get("head"), "CI head")
    checks = observation.get("checks")
    if not isinstance(checks, list):
        raise ValueError("CI check inventory must be an array")
    proofs = {_tuple(proof): proof for proof in (exceptions or [])
              if isinstance(proof, dict) and proof.get("verified") is True
              and proof.get("exempt") is True and proof.get("evidence")}
    seen = set()
    pending, failed = not checks, False
    used = []
    for check in checks:
        check_id = text(check.get("id"), "CI check ID")
        if check_id in seen:
            raise ValueError("duplicate CI check identity")
        seen.add(check_id)
        if check.get("status") != "completed":
            pending = True
            continue
        if check.get("conclusion") in SUCCESS:
            continue
        pin = {"head": head, "check_id": check_id, "run_id": check.get("run_id", ""),
               "job_id": check.get("job_id", ""), "attempt": check.get("attempt", 0)}
        proof = proofs.get(_tuple(pin))
        if proof is None:
            failed = True
        else:
            used.append(proof)
    status = "failed" if failed else "pending" if pending else "success"
    outcome = {"failed": "failure", "pending": "pending"}.get(status, "success")
    if status == "success" and used:
        # Baseline result names: only success, known_flakes and accepted_exceptions satisfy CI.
        flakes = all(c.get("kind") == "flake" for proof in used for c in proof.get("classifications", []))
        outcome = "known_flakes" if flakes else "accepted_exceptions"
    return {"head": head, "status": status, "outcome": outcome, "checks": checks,
            "evidence": observation.get("evidence"), "classifications": used}


def _test_command(line):
    return (not any(c in line for c in (";", "|", "&", "`", "$("))
            and (line == "make test" or line.startswith("go test ")))


def _test_inventory(log, candidates):
    signatures, tests = set(), set()
    for candidate in candidates:
        test = text(candidate.get("test"), "test exception name")
        tests.add(test)
        signature = candidate.get("signature")
        if signature:
            if any(c in signature for c in ("\r", "\n")):
                raise ValueError("test exception signature must name one diagnostic line")
            signatures.add(signature.strip())
    failures = 0
    preamble = False
    for number, original in enumerate(log.splitlines(), 1):
        line = TIMESTAMP.sub("", original)
        value = line.strip()
        if not value:
            continue
        if value.startswith("##[group]Run "):
            if not _test_command(value[len("##[group]Run "):]):
                raise ValueError("unsupported failed-step command envelope")
            preamble = True
            continue
        if value == "##[endgroup]":
            preamble = False
            continue
        if preamble and (value.startswith("shell: ") or value == "env:"
                         or re.match(r"^\s+[A-Za-z_][A-Za-z0-9_]*: ", line)):
            continue
        if _test_command(value) or MAKE_FAILURE.fullmatch(value):
            continue
        match = NAMED_FAILURE.match(value)
        if match:
            if match[1] not in tests:
                raise ValueError("uncovered failing test: " + match[1])
            failures += 1
            continue
        if GO_SUMMARY.fullmatch(value) or value.startswith(("=== RUN   ", "=== PAUSE ",
                "=== CONT  ", "=== NAME  ", "--- PASS: ", "--- SKIP: ")):
            continue
        if value in signatures or GO_LOCATION.sub("", line).strip() in signatures:
            continue
        raise ValueError("unclassified failed-step log line " + str(number))
    if not failures:
        raise ValueError("test exception cannot exempt an unknown failure")


def _failed_logs(log, job):
    expected = []
    for step in job.get("steps", []):
        if step.get("status") != "completed":
            raise ValueError("CI exception requires complete exact-attempt steps")
        if step.get("conclusion") in SUCCESS:
            continue
        name = text(step.get("name"), "failed step name")
        _integer(step.get("number"), "failed step number")
        if name in expected:
            raise ValueError("ambiguous failed step names")
        expected.append(name)
    if not expected:
        raise ValueError("failed step metadata is unavailable")
    parts = defaultdict(list)
    for line in log.splitlines():
        if not line.strip():
            continue
        row = line.split("\t", 2)
        if len(row) != 3 or row[0] != job["name"] or row[1] not in expected:
            raise ValueError("failed-step log identity is missing or mismatched")
        parts[row[1]].append(row[2])
    if any(not parts[name] for name in expected):
        raise ValueError("failed-step log inventory is incomplete")
    return ["\n".join(parts[name]) for name in expected]


def _affirmative_issue(issue, candidate):
    if issue.get("state") != "open" or issue.get("pull_request"):
        return False
    content = issue.get("title", "") + "\n" + issue.get("body", "")
    keys = ("workflow", "job", "test", "signature")
    if any(not isinstance(candidate.get(k), str) or not candidate[k].strip() for k in keys):
        return False
    for block in re.findall(r"```dk-flaky\s*\n(.*?)\n```", content, re.S):
        try:
            declaration = bounded_json(block)
        except (ValueError, TypeError):
            continue
        if not isinstance(declaration, dict):
            continue
        if all(declaration.get(k) == candidate[k] for k in keys):
            return declaration.get("flaky") is True
    if any("`" + candidate[k] + "`" not in content for k in keys):
        return False
    return (not re.search(r"\b(?:not|never|no|non)[ -]?(?:a )?(?:flaky|intermittent)", content, re.I)
            and not re.search(r"\b(?:maybe|possibly|suspect|could|might|whether|is it)\b|\?", content, re.I)
            and bool(re.search(r"^\s*(?:confirmed flaky test|known flaky test|this test is flaky)[.: ]", content, re.I | re.M)))


def _matching_issue(issue, candidate):
    """An open issue that declares exactly this context without affirming or denying it."""
    if issue.get("state") != "open" or issue.get("pull_request"):
        return False
    keys = ("workflow", "job", "test", "signature")
    return any(all(declared.get(k) == candidate[k] for k in keys) for declared in _declarations(issue, False))


def _verify_history(provider, pr, candidate, registry):
    """Reread the saved fail-then-pass of the candidate's context; its evidence, or ValueError."""
    if registry is None:
        raise ValueError("fail-then-pass evidence is unavailable without the shared registry")
    context = {key: candidate[key] for key in ("workflow", "job", "test")}
    saved = registry.history(pr["repository"], context, candidate["signature"])
    if saved is None or saved != candidate.get("history"):
        raise ValueError("saved fail-then-pass history differs or is unavailable")
    prefix = "repos/" + pr["owner"] + "/" + pr["name"]
    evidence = {}
    for side, conclusion in (("failure", "failure"), ("pass", "success")):
        item = saved[side]
        # The exact attempt, not the run's latest one: a pass is often a later attempt of the same run.
        run = provider.request("GET", f'{prefix}/actions/runs/{item["run_id"]}/attempts/{item["attempt"]}')
        job = provider.request("GET", prefix + "/actions/jobs/" + item["job_id"])
        if (str(run.get("id")) != item["run_id"] or str(job.get("id")) != item["job_id"]
                or run.get("run_attempt") != item["attempt"] or run.get("head_sha") != item["head"] or job.get("head_sha") != item["head"]
                or str(run.get("path", "")).split("@", 1)[0] != item["workflow"] or job.get("name") != item["job"]
                or job.get("run_attempt") != item["attempt"] or str(job.get("run_id")) != item["run_id"]
                or job.get("status") != "completed" or job.get("conclusion") != conclusion):
            raise ValueError("saved " + side + " no longer binds its exact head/run/job/attempt")
        if side == "failure":
            logs = provider.failure_logs(pr, item["run_id"], item["job_id"], item["attempt"])
            failed, _, lines = _lines(logs)
            if item["test"] not in failed or candidate["signature"] not in lines:
                raise ValueError("saved failure logs no longer name this test and signature")
        else:
            logs = provider.job_logs(pr, item["run_id"], item["job_id"], item["attempt"])
            if item["test"] not in _lines(logs)[1]:
                raise ValueError("saved pass logs no longer name this test as passed")
        evidence[side] = {**item, "sha256": digest({"run": run, "job": job, "logs": logs})}
    return {"kind": "same-commit-fail-pass", **evidence}


def verify_exceptions(provider, pr, observation, candidates, rules=None, registry=None):
    """Read exact run/job/log/issue evidence before permitting any exemption."""
    head = observation["head"]
    rule_map = {rule["id"]: rule for rule in (rules or [])}
    grouped = defaultdict(list)
    checks = {check["id"]: check for check in observation["checks"]}
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("check_id") not in checks:
            raise ValueError("classification names an unknown CI check")
        grouped[candidate["check_id"]].append(candidate)
    proofs = []
    prefix = "repos/" + pr["owner"] + "/" + pr["name"]
    for check_id, group in grouped.items():
        check = checks[check_id]
        if check.get("status") != "completed" or check.get("conclusion") in SUCCESS:
            raise ValueError("exceptions cannot replace pending or successful checks")
        first = group[0]
        for key in ("run_id", "job_id"):
            if not str(first.get(key, "")).isdigit() or int(first[key]) < 1:
                raise ValueError("exact Actions run/job required")
        attempt = _integer(first.get("attempt"), "Actions attempt")
        if any(str(first.get(k)) != str(check.get(k)) for k in ("run_id", "job_id", "attempt")):
            raise ValueError("classification differs from observed Actions attempt")
        run = provider.request("GET", prefix + "/actions/runs/" + str(first["run_id"]))
        job = provider.request("GET", prefix + "/actions/jobs/" + str(first["job_id"]))
        if (str(run.get("id")) != str(first["run_id"])
                or str(job.get("id")) != str(first["job_id"])
                or run.get("head_sha") != head or job.get("head_sha") != head
                or run.get("run_attempt") != attempt
                or str(job.get("run_id")) != str(first["run_id"])
                or job.get("run_attempt") != attempt or job.get("conclusion") != "failure"
                or job.get("status") != "completed"
                or not str(job.get("check_run_url", "")).endswith("/check-runs/" + check_id)):
            raise ValueError("CI exception evidence does not bind exact failed head/check/run/job/attempt")
        logs = provider.failure_logs(pr, first["run_id"], first["job_id"], attempt)
        workflow = run.get("path", "").split("@", 1)[0]
        evidence = [{"kind": "actions", "sha256": digest({"run": run, "job": job, "logs": logs})}]
        pipeline = False
        for candidate in group:
            if (any(str(candidate.get(k)) != str(first.get(k)) for k in ("check_id", "run_id", "job_id", "attempt"))
                    or candidate.get("workflow") != workflow or candidate.get("job") != job.get("name")):
                raise ValueError("classification scope differs from exact workflow/job")
            kind = candidate.get("kind")
            if kind == "exception":
                rule = rule_map.get(candidate.get("rule"))
                if (not rule or rule.get("workflow") != workflow
                        or rule.get("job") and rule["job"] != job.get("name")
                        or not rule.get("reason")):
                    raise ValueError("CI exception is absent from scoped settings")
                if rule.get("kind") == "pipeline":
                    pipeline = True
                    continue
                if (rule.get("kind") != "test" or rule.get("test") != candidate.get("test")
                        or rule.get("signature") != candidate.get("signature")):
                    raise ValueError("test exemption differs from saved policy")
            elif kind == "flake":
                link = urlsplit(text(candidate.get("issue"), "flake issue URL"))
                parts = link.path.strip("/").split("/")
                if (link.scheme != "https" or link.netloc != pr["host"] or link.query or link.fragment
                        or "%" in link.path or len(parts) != 4 or parts[:2] != [pr["owner"], pr["name"]]
                        or parts[2] != "issues" or not parts[3].isdigit() or int(parts[3]) < 1):
                    raise ValueError("flake requires a canonical issue in the PR repository")
                issue = provider.request("GET", prefix + "/issues/" + parts[3])
                if issue.get("html_url", "").lower() != candidate["issue"].lower():
                    raise ValueError("issue does not affirm this exact test/signature as flaky")
                if "history" in candidate:
                    # A declaration without affirmation counts only with reread fail-then-pass history.
                    if not _matching_issue(issue, candidate):
                        raise ValueError("issue does not declare this exact test/signature")
                    evidence.append(_verify_history(provider, pr, candidate, registry))
                elif not _affirmative_issue(issue, candidate):
                    raise ValueError("issue does not affirm this exact test/signature as flaky")
                evidence.append({"kind": "open-flake-issue", "url": candidate["issue"], "sha256": digest(issue)})
            else:
                raise ValueError("unsupported CI classification kind")
            test = text(candidate.get("test"), "classified test")
            signature = candidate.get("signature")
            if test not in logs or signature and signature not in logs:
                raise ValueError("classified test/signature is absent from exact logs")
        if not pipeline:
            for part in _failed_logs(logs, job):
                _test_inventory(part, group)
        proofs.append({"head": head, "check_id": check_id, "run_id": str(first["run_id"]),
                       "job_id": str(first["job_id"]), "attempt": attempt, "verified": True,
                       "exempt": True, "classifications": group, "evidence": evidence})
    return proofs


def _timestamp(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("deadline"))
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("retained CI deadline is invalid")
    return value


def _declarations(issue, affirmed=True):
    """Exact ``dk-flaky`` declarations; with ``affirmed=False`` those that name a context without affirming it."""
    content = str(issue.get("title", "")) + "\n" + str(issue.get("body") or "")
    for block in re.findall(r"```dk-flaky\s*\n(.*?)\n```", content, re.S):
        try:
            value = bounded_json(block)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and ("flaky" not in value if not affirmed else value.get("flaky") is True):
            yield value


def _lines(logs):
    """Named outcomes and normalized diagnostic lines of ``job\\tstep\\tline`` logs."""
    failed, passed, lines = [], [], set()
    for raw in logs.splitlines():
        line = TIMESTAMP.sub("", raw.split("\t", 2)[-1])
        value = line.strip()
        for pattern, names in ((NAMED_FAILURE, failed), (NAMED_PASS, passed)):
            match = pattern.match(value)
            if match and match[1] not in names:
                names.append(match[1])
        for item in (value, GO_LOCATION.sub("", line).strip()):
            if item and len(item) <= MAX_LINE and len(lines) < MAX_LINES:
                lines.add(item)
    return failed, passed, sorted(lines)


def _when(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def trusted_declarer(issue, trust, run):
    """Whether ``issue`` may declare a flake for the PR's failed ``run``.

    The PR's logs are under the PR author's control, so a declaration counts
    only from a trusted author other than the PR author, made before the
    failed run was created. Missing identities or timestamps never count.
    """
    user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
    author = user.get("id")
    if type(author) is not int or author == trust["author"]:
        return False
    if issue.get("author_association") not in TRUSTED_ASSOCIATIONS and author not in trust["accounts"]:
        return False
    opened, started = _when(issue.get("created_at")), _when(run.get("created_at"))
    return opened is not None and started is not None and opened < started


def _trust(plan):
    author = plan.get("snapshot", {}).get("author_id")
    if type(author) is not int or author < 1:
        raise ValueError("PR author identity unavailable for flake learning")
    host = plan["pr"].get("host", "github.com")
    accounts = {item.get("id") for item in plan.get("options", {}).get("accounts", [])
                if isinstance(item, dict) and str(item.get("host", "")).lower() == host}
    return {"author": author, "accounts": accounts}


def learn(provider, pr, check, trust, registry=None):
    """Issue-backed flake candidates for one failed Actions check, from its exact attempt only.

    Reads the attempt's run, job and failed-step logs, takes the tests those
    logs name and looks up open issues of the PR repository that mention each
    test. A candidate needs an explicit ``dk-flaky`` declaration of exactly this
    workflow, job and test whose signature occurs in these logs, by a trusted
    author before the run (``trusted_declarer``). With a ``registry`` the
    failures are saved, and a trusted declaration of the exact context that does
    not affirm it nominates a candidate only with saved fail-then-pass history
    of that context and signature at one commit. Candidates are never
    authority: ``verify_exceptions`` rereads everything and requires every
    failed test and diagnostic line to be covered.
    """
    for key in ("run_id", "job_id"):
        if not str(check.get(key, "")).isdigit() or int(check[key]) < 1:
            return []  # Not an Actions check: nothing to learn from.
    attempt = _integer(check.get("attempt"), "Actions attempt")
    prefix = "repos/" + pr["owner"] + "/" + pr["name"]
    run = provider.request("GET", prefix + "/actions/runs/" + str(check["run_id"]))
    job = provider.request("GET", prefix + "/actions/jobs/" + str(check["job_id"]))
    workflow, name = str(run.get("path", "")).split("@", 1)[0], job.get("name")
    if not workflow or not isinstance(name, str) or not name:
        raise ValueError("exact workflow/job identity unavailable for flake learning")
    logs = provider.failure_logs(pr, str(check["run_id"]), str(check["job_id"]), attempt)
    tests, _, lines = _lines(logs)
    if len(tests) > LEARN_TESTS:
        raise ValueError("too many failed tests to learn flakes from one check")
    if registry is not None and tests and run.get("head_sha") == job.get("head_sha") and job.get("conclusion") == "failure":
        try:
            registry.record(pr["repository"], pr["url"], [
                {"head": run.get("head_sha"), "workflow": workflow, "job": name, "test": test, "outcome": "fail",
                 "run_id": str(check["run_id"]), "job_id": str(check["job_id"]), "attempt": attempt,
                 "completed": job.get("completed_at"), "lines": lines, "logs_sha256": digest(logs)}
                for test in tests])
        except ValueError:
            pass  # Incomplete attempt metadata is not saved; classification is unaffected.
    candidates = []
    for test in tests:
        query = quote(f'repo:{pr["owner"]}/{pr["name"]} is:issue is:open "{test}"', safe="")
        issues = provider.pages("search/issues?q=" + query, key="items")
        if len(issues) > LEARN_ISSUES:
            raise ValueError("flake issue lookup exceeds its bound")
        matched, historical = [], []
        for issue in issues:
            url = str(issue.get("html_url", ""))
            if (issue.get("pull_request") or issue.get("state") != "open" or not url
                    or not trusted_declarer(issue, trust, run)):
                continue
            number = str(issue.get("number", "")).zfill(20)
            for affirmed, found in ((True, matched), (False, historical)):
                for declared in _declarations(issue, affirmed):
                    signature = declared.get("signature")
                    if (declared.get("workflow") == workflow and declared.get("job") == name
                            and declared.get("test") == test and isinstance(signature, str) and signature.strip()
                            and "\n" not in signature and signature in logs):
                        found.append((number, url, signature))
        candidate = {"check_id": check["id"], "run_id": str(check["run_id"]), "job_id": str(check["job_id"]),
                     "attempt": attempt, "kind": "flake", "workflow": workflow, "job": name, "test": test}
        if matched:
            _, url, signature = min(matched)
            candidates.append({**candidate, "signature": signature, "issue": url})
            continue
        for _, url, signature in sorted(historical):
            history = None if registry is None else registry.history(
                pr["repository"], {"workflow": workflow, "job": name, "test": test}, signature)
            if history is not None:
                candidates.append({**candidate, "signature": signature, "issue": url, "history": history})
                break
    return candidates


def record_passes(provider, pr, observation, registry):
    """Save named passes of successful Actions checks at a head with a saved failure.

    Only a completed successful job of the exact head whose workflow and job
    have a saved failure there is read, once per set of saved failures of
    that job (a later saved failure reads it again). Missing or unreadable
    logs save no pass and are returned as gaps; they never fail the
    observation.
    """
    head = observation["head"]
    failures = registry.failed_at(pr["repository"], head)
    names = {row["job"] for row in failures}
    prefix = "repos/" + pr["owner"] + "/" + pr["name"]
    saved, gaps = 0, []
    for check in observation.get("checks", []):
        if (check.get("status") != "completed" or check.get("conclusion") != "success"
                or check.get("name") not in names or not str(check.get("run_id", "")).isdigit()
                or not str(check.get("job_id", "")).isdigit() or type(check.get("attempt")) is not int
                or check["attempt"] < 1):
            continue
        read = digest(sorted({(row["workflow"], row["test"]) for row in failures if row["job"] == check["name"]}))
        if registry.was_read(pr["repository"], check["run_id"], check["job_id"], check["attempt"], read):
            continue
        try:
            run = provider.request("GET", f'{prefix}/actions/runs/{check["run_id"]}/attempts/{check["attempt"]}')
            job = provider.request("GET", prefix + "/actions/jobs/" + str(check["job_id"]))
            workflow = str(run.get("path", "")).split("@", 1)[0]
            if (run.get("run_attempt") != check["attempt"] or run.get("head_sha") != head or job.get("head_sha") != head or job.get("status") != "completed"
                    or job.get("conclusion") != "success" or job.get("run_attempt") != check["attempt"]
                    or job.get("name") != check["name"]):
                raise ValueError("successful job does not bind the exact head and attempt")
            wanted = {row["test"] for row in failures if (row["workflow"], row["job"]) == (workflow, job["name"])}
            if not wanted:
                registry.mark_read(pr["repository"], check["run_id"], check["job_id"], check["attempt"], read)
                continue
            logs = provider.job_logs(pr, str(check["run_id"]), str(check["job_id"]), check["attempt"])
            _, passed, _ = _lines(logs)
            saved += registry.record(pr["repository"], pr["url"], [
                {"head": head, "workflow": workflow, "job": job["name"], "test": test, "outcome": "pass",
                 "run_id": str(check["run_id"]), "job_id": str(check["job_id"]), "attempt": check["attempt"],
                 "completed": job.get("completed_at"), "lines": [], "logs_sha256": digest(logs)}
                for test in passed if test in wanted])
            registry.mark_read(pr["repository"], check["run_id"], check["job_id"], check["attempt"], read)
        except EVIDENCE_ERRORS as error:
            gaps.append({"check_id": str(check.get("id", "")), "reason": str(error)[:500]})
    return {"recorded": saved, "gaps": gaps}


def classify_current(provider, plan, observation, window=None, registry=None):
    """Classify one supplied observation with freshly verified exception proofs.

    The caller owns capture freshness and any retained observation deadline.
    Preparation validates options; readiness can also read older admitted plans.
    Each failed check is verified separately from configured classifications
    and issue-backed flakes learned from its exact attempt. A check whose
    evidence is unavailable or incomplete stays failed and is reported in
    ``rejected``; it never fails or exempts another check.

    ``window(check)`` gives the retained absolute end of that failed attempt's
    classification window, or None for no bound. A check whose window has
    ended before its evidence is read, or ends while it is verified, stays
    failed: its proofs are discarded, never applied late.

    ``registry`` is the shared cross-PR registry: learning saves the failures
    it reads there (unless it is read-only) and uses its fail-then-pass history.
    """
    selected = _selection(plan.get("options", {}))
    configured = selected["classifications"]
    checks = observation.get("checks")
    checks = checks if isinstance(checks, list) else []
    failed = [check for check in checks if isinstance(check, dict) and check.get("status") == "completed"
              and check.get("conclusion") not in SUCCESS]
    known = {check.get("id") for check in failed}
    proofs, rejected = [], []
    for candidate in configured:
        if not isinstance(candidate, dict) or candidate.get("check_id") not in known:
            rejected.append({"check_id": str(candidate.get("check_id", "")) if isinstance(candidate, dict) else "",
                             "reason": "classification names no failed check of this observation"})
    for check in failed:
        group = [candidate for candidate in configured
                 if isinstance(candidate, dict) and candidate.get("check_id") == check.get("id")]
        end = None if window is None else window(check)
        if end is not None and time.time() >= end:
            rejected.append({"check_id": str(check.get("id", "")), "reason": "classification window exhausted"})
            continue
        try:
            # Configured candidates are explicit; learning is the default for a check without them.
            group = group or learn(provider, plan["pr"], check, _trust(plan), registry)
            verified = (verify_exceptions(provider, plan["pr"], observation, group, selected["exceptions"], registry)
                        if group else [])
        except EVIDENCE_ERRORS as error:
            rejected.append({"check_id": str(check.get("id", "")), "reason": str(error)[:500]})
            continue
        if verified and end is not None and time.time() >= end:
            rejected.append({"check_id": str(check.get("id", "")),
                             "reason": "classification window ended during verification"})
            continue
        proofs.extend(verified)
    result = classify(observation, proofs)
    result["rejected"] = rejected
    return result


def _extension(ledger, run, selected, original, create):
    """The retained end of the one pending-only extension, or None.

    The grant is a durable reservation, so a resumed, direct or composed
    observation of the same logical run never receives a second one. Its
    window starts where the original ended. Configuration can disable or
    shorten it later, never lengthen or renew it.
    """
    seconds, allowed = selected["pending_extension_seconds"], selected["max_pending_extensions"]
    if not allowed or not seconds:
        return None
    name = "ci-pending-extension:" + run
    granted = ledger.db.execute("SELECT 1 FROM reservations WHERE name=? LIMIT 1", (name,)).fetchone()
    if not granted:
        if not create:
            return None
        try:
            ledger.reserve(name, "pending-extension", allowed)
        except ValueError:
            return None
    return _timestamp(ledger.deadline(name, seconds, start=original))


def _classification_window(ledger, run, seconds, read_only=False):
    """Retained classification windows, one per failed exact attempt; None when unbounded.

    A read-only window (readiness) opens nothing: it applies an attempt's
    retained end when observation opened one, and is unbounded for an attempt
    whose window was never opened, as a first classification would be.
    """
    if seconds is None:
        return None

    def window(check):
        key = ":".join(str(check.get(k, "")) for k in ("id", "run_id", "job_id", "attempt"))
        name = "ci-classification:" + run + ":" + digest(key)
        if not read_only:
            return _timestamp(ledger.deadline(name, seconds))
        row = ledger.db.execute("SELECT value,started FROM deadlines WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return _timestamp(row[0] if row[1] is None else min(row[0], row[1] + seconds))
    return window


def retained_classification_window(ledger, plan):
    """The plan's classification windows as observation retained them, opening none (readiness)."""
    seconds = _selection(plan.get("options", {}))["classification_timeout_seconds"]
    return _classification_window(ledger, plan["logical_run"], seconds, read_only=True)


def _no_checks(ledger, run, head, grace):
    """Whether the absent-check grace for this head has ended; it starts at the first empty observation."""
    if grace is None:
        return False
    if grace == 0:
        return True
    return time.time() >= _timestamp(ledger.deadline("ci-no-checks:" + run + ":" + head, grace))


def observe(provider, plan, ledger, operation):
    """Observe once. A new process or head never silently renews the clock.

    After the retained window ends, one more observation may decide the
    pending-only extension: it is granted only while checks are still pending,
    at most once per logical run, and never after a failure or success.

    With a configured absent-check grace, a head that still reports no checks
    when its retained grace ends is ``no_checks``: settled, not satisfied and
    not a repairable failure. A configured classification timeout bounds each
    failed attempt's classification by a retained window (see
    ``classify_current``). Neither restarts on resume.

    Review mode (the merge bridge, main.yml merge_ci) keys the observation
    window and its extension by the exact head, so a later merge request of a
    pushed fix gets that head's own window. Its late observation of a head
    whose window ended reports settled checks as they are and is a timeout
    while they are still pending; the wait itself never renews.
    """
    selected = configuration(plan.get("options", {}))
    timeout = selected["timeout_seconds"]
    text(operation, "CI observation operation")
    run = plan["logical_run"]
    review = plan.get("mode") == "review"
    head = None
    if review:
        # A merge request's wait belongs to the exact head it waits for.
        head = provider.capture(plan["pr"])["metadata"]["head"]["sha"]
    window = run if head is None else run + ":" + text(head, "CI head")
    original = _timestamp(ledger.deadline("ci:" + window, timeout))
    extended = _extension(ledger, window, selected, original, create=False)
    deadline = original if extended is None else extended
    eligible = bool(extended is None and selected["max_pending_extensions"] and selected["pending_extension_seconds"])
    if time.time() >= deadline and not eligible and not review:
        raise TimeoutError("retained CI deadline exhausted")
    if head is None:
        head = provider.capture(plan["pr"])["metadata"]["head"]["sha"]
    observation = provider.observe_ci(plan["pr"], head)
    if observation.get("head") != head:
        raise ValueError("CI observer returned another head")
    registry = Registry(ledger.root)
    try:
        # Passes first: a later attempt that passed at this head completes saved history.
        saved = record_passes(provider, plan["pr"], observation, registry)
        result = classify_current(provider, plan, observation,
                                  _classification_window(ledger, run, selected["classification_timeout_seconds"]),
                                  registry)
    finally:
        registry.close()
    result["registry"] = saved
    if (result["status"] == "pending" and not result["checks"]
            and _no_checks(ledger, run, head, selected["no_checks_grace_seconds"])):
        result.update(status="no_checks", outcome="no_checks")
    if time.time() >= deadline:
        if not eligible and (result["status"] == "pending" or not review):
            raise TimeoutError("retained CI deadline exhausted during observation")
        if result["status"] == "pending":
            extended = _extension(ledger, window, selected, original, create=True)
            if extended is None or time.time() >= extended:
                raise TimeoutError("retained CI deadline and pending extension exhausted")
            deadline = extended
        # A settled post-window capture is reported as it is; it consumes no extension.
    result.update(deadline=deadline, original_deadline=original, extended=extended is not None,
                  operation=operation, ready=result["status"] == "success", pending=result["status"] == "pending",
                  failed=result["status"] == "failed", repair=False,
                  # The delay before the next observation (ci-poll.yml), as an argv string.
                  poll_interval=str(selected["poll_interval_seconds"]))
    return result


def reserve_repair(ledger, cycle, operation, limit=5):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 5:
        raise ValueError("CI repair allowance must be in 0..5")
    return ledger.reserve("ci-repair:" + text(cycle, "review cycle"), text(operation, "repair operation"), limit)


def reserve_rerun(ledger, head, operation, limit=3):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 3:
        raise ValueError("CI rerun allowance must be in 0..3")
    return ledger.reserve("ci-reruns:" + text(head, "CI head"), text(operation, "rerun operation"), limit)
