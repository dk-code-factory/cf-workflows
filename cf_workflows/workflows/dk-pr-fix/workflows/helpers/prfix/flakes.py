"""Installed side of cf-workflows:dk-ci-flakes (#35): bounded flaky-test discovery, never a waiver.

workflows/flakes.yml runs three steps. `flakes_collect` (a retained
preparation operation) reads, within the validated `lookback_days`, `max_runs`
and `max_issues` (or an exact `targets` narrowing), the failed Actions runs of
the PR's repository, the failed jobs of each run's exact attempt and their
failed-step logs, and the repository's open issues with their explicit
``dk-flaky`` declarations. A context is `linked` only when an open issue
declares exactly that workflow, job, test and signature and the signature
occurs in an observation's logs; otherwise it is a `candidate`. With no
observed context the result is complete without a session. Otherwise the engine
runs one `agent/run` session on the admitted `leaf` role with no skills, and
`flakes_check` holds its report to the collected evidence: it cannot link,
unlink, invent or drop a context.

Limits reached and gaps make the result uncovered; a failed read is a blocker,
never an empty result. Nothing here waives a failure, saves an observation,
creates an issue, reruns a workflow or changes an exception. The evidence and
report checks between the markers are a verbatim copy of cf_workflows/ci_flakes.py;
tests/test_ci_skills.py keeps the two identical.
"""
import copy
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import quote

from .approvers import AdmissionError, fields, require, text
from .core import artifact, digest, identity, read_artifact

SKIPPED = ('skipped', None, '')  # A session status when the report job did not run.
# The baseline role prompt, bundled adapter and options of the entry (cf_workflows.entries, cf_workflows.admission).
ROLE_PROMPT = ('Actively scan CI history and GitHub issues for flaky-test evidence, or inspect saved '
               'observations for a failed PR check.\n')
ASSIGNMENT = ('Report flaky-test evidence only from the observations and issue evidence in the assignment: each named '
              'failed test with its workflow, job and observations, whether a verified open issue declares exactly that '
              'context, and every limit and gap. Never mark a test linked without that verified issue evidence. Create no '
              'issue, rerun nothing and change no exception.')
CHECKLIST = [
    {'id': 'assignment', 'text': 'Account for every observed test context in the evidence.'},
    {'id': 'evidence', 'text': 'Cite the observations and verified issue of every reported test.'},
    {'id': 'scope', 'text': 'Report limits and gaps; a flake record waives no failure and grants no authority.'}]
OPTIONS = {'lookback_days': 30, 'max_runs': 100, 'max_issues': 500}
RULES = {'lookback_days': range(1, 366), 'max_runs': range(1, 1001), 'max_issues': range(1, 10001)}
CONTRACT = ('Return JSON {summary, tests: [{workflow, job, test, status, observations, issue, note}], gaps}. '
            'Account for every context in task_input.tests exactly once with its own status; cite only that '
            'context\'s observation IDs, and for a linked context one of its verified issues (null for a '
            'candidate). note is optional text or null; gaps lists further evidence gaps as text. Do not edit '
            'files, create issues, rerun workflows or change exceptions.')


# -- Verbatim copy of cf_workflows/ci_flakes.py: evidence and report checks ----------------------

PAGE = 100
MAX_PAGES = 100
NAMED_FAILURE = re.compile(r'^(?:--- FAIL: |FAILED )([^\s(]+)')
TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s')
DECLARATION = re.compile(r'```dk-flaky\s*\n(.*?)\n```', re.S)
CONTEXT = ('workflow', 'job', 'test')
MAX_CONTEXTS = 200


class Incomplete(Exception):
    """A required read is missing or inconsistent; never reported as an empty result."""


def check_targets(value):
    """An explicit narrowing to exact {workflow, job, test, signature} contexts, or None."""
    if value is None:
        return None
    require(isinstance(value, list) and value and len(value) <= MAX_CONTEXTS, 'targets',
            'must be a nonempty bounded array of exact contexts')
    for number, item in enumerate(value):
        fields(item, f'targets[{number}]', (*CONTEXT, 'signature'))
        for key in (*CONTEXT, 'signature'):
            text(item[key], f'targets[{number}].{key}')
    return copy.deepcopy(value)


def _page(github, path, key, label):
    response = github.request('GET', path)
    if not isinstance(response, dict) or not isinstance(response.get(key), list) or any(
            not isinstance(item, dict) for item in response[key]):
        raise Incomplete(label + ' listing is malformed')
    return response


def _failed_tests(logs):
    tests = []
    for line in logs.splitlines():
        match = NAMED_FAILURE.match(TIMESTAMP.sub('', line.split('\t', 2)[-1]).strip())
        if match and match[1] not in tests:
            tests.append(match[1])
    return tests


def declarations(issue):
    """Explicit exact flaky declarations of one open issue."""
    content = str(issue.get('title', '')) + '\n' + str(issue.get('body') or '')
    result = []
    for block in DECLARATION.findall(content):
        try:
            value = json.loads(block)
        except ValueError:
            continue
        if (isinstance(value, dict) and value.get('flaky') is True
                and all(isinstance(value.get(key), str) and value[key].strip() for key in (*CONTEXT, 'signature'))
                and '\n' not in value['signature']):
            result.append({key: value[key] for key in (*CONTEXT, 'signature')})
    return result


def collect(github, target, options, now, targets=None):
    """Complete bounded read-only evidence; raises Incomplete. Limits and gaps are reported, not hidden."""
    days, max_runs, max_issues = (options[key]['value'] for key in ('lookback_days', 'max_runs', 'max_issues'))
    since = datetime.fromtimestamp(now - days * 86400, timezone.utc).date().isoformat()
    prefix = f"repos/{target['owner']}/{target['name']}"
    limits, gaps = [], []
    runs, total = [], None
    for page in range(1, MAX_PAGES + 1):
        response = _page(github, f'{prefix}/actions/runs?status=failure&created={quote(">=" + since)}'
                                 f'&per_page={PAGE}&page={page}', 'workflow_runs', 'workflow run')
        total = response.get('total_count', total)
        runs.extend(response['workflow_runs'])
        if len(response['workflow_runs']) < PAGE or len(runs) >= max_runs:
            break
    else:
        raise Incomplete('workflow run listing exceeds bounded pagination')
    if len(runs) > max_runs or type(total) is int and total > max_runs:
        limits.append('max_runs')
    runs = runs[:max_runs]
    wanted = None if targets is None else {tuple(item[key] for key in CONTEXT) for item in targets}
    observations, logs_by_observation = [], {}
    for run in runs:
        ident, attempt = run.get('id'), run.get('run_attempt')
        if any(type(value) is not int or value < 1 for value in (ident, attempt)):
            raise Incomplete('workflow run identity unavailable')
        workflow = str(run.get('path', '')).split('@', 1)[0]
        jobs = _page(github, f'{prefix}/actions/runs/{ident}/attempts/{attempt}/jobs?per_page={PAGE}', 'jobs', 'job')
        if jobs.get('total_count') != len(jobs['jobs']):
            gaps.append(f'run {ident} attempt {attempt}: job inventory exceeds one page')
            continue
        for job in jobs['jobs']:
            if job.get('conclusion') != 'failure':
                continue
            name = job.get('name')
            if not isinstance(name, str) or not name or type(job.get('id')) is not int:
                gaps.append(f'run {ident} attempt {attempt}: failed job identity unavailable')
                continue
            if wanted is not None and not any(item[:2] == (workflow, name) for item in wanted):
                continue
            try:
                logs = github.failure_logs(target, str(ident), str(job['id']), attempt)
            except Exception as error:  # A missing log is a gap, never a pass.
                gaps.append(f'run {ident} attempt {attempt} job {job["id"]}: failed-step logs unavailable '
                            f'({type(error).__name__})')
                continue
            if not isinstance(logs, str):
                gaps.append(f'run {ident} attempt {attempt} job {job["id"]}: failed-step logs unavailable')
                continue
            tests = _failed_tests(logs)
            if not tests:
                gaps.append(f'run {ident} attempt {attempt} job {job["id"]}: no named failed test '
                            '(unsupported report or non-test failure)')
            for test in tests:
                if wanted is not None and (workflow, name, test) not in wanted:
                    continue
                key = f'{ident}:{attempt}:{job["id"]}:{test}'
                observations.append({'id': key, 'run_id': ident, 'attempt': attempt, 'job_id': job['id'],
                                     'head_sha': run.get('head_sha'), 'workflow': workflow, 'job': name,
                                     'test': test, 'outcome': 'failed', 'logs_sha256': digest(logs)})
                logs_by_observation[key] = logs
    issues = []
    for page in range(1, MAX_PAGES + 1):
        response = github.request('GET', f'{prefix}/issues?state=open&per_page={PAGE}&page={page}')
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise Incomplete('issue listing is malformed')
        issues.extend(item for item in response if not item.get('pull_request'))
        if len(issues) > max_issues or len(issues) == max_issues and len(response) == PAGE:
            limits.append('max_issues')
            break
        if len(response) < PAGE:
            break
    else:
        raise Incomplete('issue listing exceeds bounded pagination')
    issues = issues[:max_issues]
    declared = []
    for issue in issues:
        url, number = issue.get('html_url'), issue.get('number')
        if issue.get('state') != 'open' or not isinstance(url, str) or type(number) is not int:
            continue
        for item in declarations(issue):
            if targets is None or item in targets:
                declared.append({**item, 'issue': url, 'number': number})
    contexts = {}
    for observation in observations:
        key = tuple(observation[field] for field in CONTEXT)
        contexts.setdefault(key, []).append(observation['id'])
    if len(contexts) > MAX_CONTEXTS:
        raise Incomplete('too many failed test contexts for one report; narrow it with targets')
    tests, linked_issues = [], set()
    for key, ids in sorted(contexts.items()):
        links = []
        for item in declared:
            if tuple(item[field] for field in CONTEXT) != key:
                continue
            matching = [ident for ident in ids if item['signature'] in logs_by_observation[ident]]
            if matching:
                links.append({'issue': item['issue'], 'number': item['number'], 'signature': item['signature'],
                              'observations': matching})
                linked_issues.add((item['issue'], key))
        tests.append({**dict(zip(CONTEXT, key)), 'observations': ids,
                      'status': 'linked' if links else 'candidate', 'links': links})
    unobserved = [item for item in declared if (item['issue'], tuple(item[f] for f in CONTEXT)) not in linked_issues]
    evidence = {'repository': target['repository'], 'since': since, 'runs': len(runs), 'issues': len(issues),
                'targets': targets, 'observations': observations, 'tests': tests,
                'declared_unobserved': unobserved, 'limits_reached': limits, 'gaps': gaps}
    return {**evidence, 'digest': digest(evidence)}


def check_report(output, found):
    """The session's report, each test held to the collected evidence."""
    fields(output, 'report', ('summary', 'tests'), ('gaps',))
    text(output['summary'], 'report.summary')
    require(isinstance(output['tests'], list), 'report.tests', 'must be an array')
    table = {tuple(item[key] for key in CONTEXT): item for item in found['tests']}
    seen, result = set(), []
    for number, item in enumerate(output['tests']):
        where = f'report.tests[{number}]'
        fields(item, where, (*CONTEXT, 'status', 'observations'), ('issue', 'note'))
        key = tuple(item[field] for field in CONTEXT)
        evidence = table.get(key)
        require(evidence is not None, where, 'not an observed test context from the collected evidence')
        require(key not in seen, where, 'duplicate test context')
        seen.add(key)
        require(item['status'] == evidence['status'], where + '.status',
                'the status is the verified evidence status; a session cannot link or unlink a test')
        require(isinstance(item['observations'], list) and item['observations']
                and set(item['observations']) <= set(evidence['observations']), where + '.observations',
                "cite the context's own observations")
        issue = item.get('issue')
        if evidence['status'] == 'linked':
            require(issue in {link['issue'] for link in evidence['links']}, where + '.issue',
                    'cite a verified linked issue of this context')
        else:
            require(issue is None, where + '.issue', 'a candidate has no verified issue')
        result.append({**{field: item[field] for field in CONTEXT}, 'status': item['status'],
                       'observations': list(item['observations']), 'issue': issue,
                       'note': text(item['note'], where + '.note') if item.get('note') is not None else None})
    require(seen == set(table), 'report.tests', 'account for every observed test context')
    notes = output.get('gaps', [])
    require(isinstance(notes, list) and all(isinstance(item, str) and item.strip() for item in notes),
            'report.gaps', 'must be an array of text')
    return result, list(notes)


# -- End of the verbatim copy -----------------------------------------------------------------


def options(raw):
    """The entry's options in collect()'s {key: {value}} form; unknown or out-of-range values reject."""
    require(isinstance(raw, dict), 'options', 'must be an object')
    result = {key: {'value': value} for key, value in OPTIONS.items()}
    for key, value in raw.items():
        require(key in OPTIONS, 'options.' + str(key), 'unsupported option for cf-workflows:dk-ci-flakes')
        require(type(value) is int and value in RULES[key], 'options.' + key, 'unsupported value')
        result[key] = {'value': value}
    return result


class GitHubEvidence:
    """The read-only GitHub reads collect() needs, over the installed gh transport (capture.GitHub)."""

    def __init__(self, transport):
        self.transport = transport

    def request(self, method, path, payload=None):
        if method != 'GET':
            raise ValueError('flaky-test discovery only reads')
        return self.transport.request(method, path, payload)

    def failure_logs(self, target, run_id, job_id, attempt):
        return self.transport.failure_logs(target, run_id, job_id, attempt)


def evidence_provider(root, provider):
    """The injected evidence reads, or the installed gh transport."""
    if provider is not None:
        return provider
    from .capture import GitHub
    return GitHubEvidence(GitHub(root))


def _blocked(base, blockers):
    return {'status': 'incomplete', 'complete': False, **base, 'tests': [], 'evidence': {}, 'evidence_refs': [],
            'blockers': blockers}


def collect_operation(root, provider, request, inputs):
    """flakes_collect: the blocked result, or the evidence and one report session's assignment."""
    try:
        require(not request.get('authorizations') and not request.get('maintain_other'), 'request.authorizations',
                'flaky-test discovery creates, reruns and waives nothing; it takes no authorization')
        selected = options(inputs['options'])
        targets = check_targets(inputs['targets'] or None)
    except AdmissionError as error:
        raise ValueError(f'{error.path}: {error.message}') from None
    target = identity(request['pr'])
    base = {'repository': target['repository'], 'authority': 'none', 'waives': False}
    if hasattr(provider, 'transport'):
        provider.transport.host = target['host']
    try:
        found = collect(provider, target, selected, time.time(), targets)
    except Incomplete as error:
        return {'session': False, 'evidence': None, 'result': _blocked(base, [str(error)])}
    except Exception as error:  # A failed read is a blocker, never an empty result.
        return {'session': False, 'evidence': None,
                'result': _blocked(base, ['evidence read failed: ' + type(error).__name__])}
    reference = artifact(root, {'kind': 'flakes_evidence', 'base': base, 'found': found})
    output = {'session': bool(found['tests']), 'evidence': reference, 'result': None}
    if found['tests']:
        output['prompt'] = ROLE_PROMPT + '\n' + ASSIGNMENT + '\n\n' + CONTRACT
        output['task_input'] = {'checklist': CHECKLIST, 'options': {key: value['value'] for key, value in selected.items()},
                                **{key: found[key] for key in ('repository', 'since', 'runs', 'issues', 'observations',
                                                               'tests', 'declared_unobserved', 'limits_reached', 'gaps')}}
    return output


def check_operation(root, inputs):
    """flakes_check: the discovery result; a report it cannot accept blocks, and nothing is ever waived."""
    collected, session = inputs['collected'], inputs['session']
    if not isinstance(collected, dict) or not isinstance(session, dict):
        raise ValueError('flake check requires the collected evidence and the session result')
    if collected.get('evidence') is None:
        if collected.get('session') or session.get('status') not in SKIPPED:
            raise ValueError('no session runs without collected evidence')
        return collected['result']
    record = read_artifact(root, collected['evidence'])
    if record.get('kind') != 'flakes_evidence':
        raise ValueError('flake evidence reference is not flake evidence')
    base, found = record['base'], record['found']
    tests, notes, references = [], [], []
    if found['tests']:
        if session.get('status') != 'succeeded' or not isinstance(session.get('output'), dict):
            return _blocked(base, ['the report session did not complete: ' + str(session.get('status'))])
        try:
            tests, notes = check_report(session['output'], found)
        except AdmissionError as error:
            return _blocked(base, [f'{error.path}: {error.message}'])
        references.append('native:' + str(session.get('agent')))
    elif session.get('status') not in SKIPPED:
        raise ValueError('no session runs without an observed test context')
    covered = not found['limits_reached'] and not found['gaps']
    return {'status': 'complete', 'complete': True, 'covered': covered, **base, 'tests': tests,
            'declared_unobserved': found['declared_unobserved'], 'limits_reached': found['limits_reached'],
            'gaps': found['gaps'], 'notes': notes,
            'evidence': {key: found[key] for key in ('since', 'runs', 'issues', 'digest')},
            'evidence_refs': references, 'blockers': []}
