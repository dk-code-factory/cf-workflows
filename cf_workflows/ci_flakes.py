"""Helper side of cf-workflows:dk-ci-flakes (#35): bounded flaky-test discovery, never a waiver.

The entry is a preset from cf_workflows.entries: one `inspect-flakes` task on the
admitted `leaf` role, no finalizers or reviewers, no skill and no remote-write
authorization. `inspect()` reads, within the resolved `lookback_days`,
`max_runs` and `max_issues` (or an explicit `targets` narrowing):

- failed Actions runs of the repository, their failed jobs of the exact attempt
  and those jobs' failed-step logs, giving named failed-test observations;
- open issues of the repository and their explicit ``dk-flaky`` declarations.

A test context (workflow, job, test) is `linked` only when an open issue
declares exactly that context and its signature occurs in the logs of an
observation of it; otherwise it stays a `candidate`. One engine-managed session
writes the report, and `check_report()` holds it to that evidence: it cannot
link a test, cite an observation or drop a context the helper did not verify.
Limits reached and missing evidence are reported, never treated as coverage.

The result waives nothing. CI acceptance (prfix/ci.py) verifies the exact failed
attempt again on every observation, and creating issues, rerunning workflows or
changing exceptions still needs its own authorization.

The entry is also installed (workflows/flakes.yml, `installed_preflight()`):
there the helpers (helpers/prfix/flakes.py, whose evidence and report checks
are a verbatim copy of the ones below) collect the evidence for the PR's
repository and check the report, and the engine runs the report session as an
`agent/run` step.

`saved()` pages the observations CI observation saved in the shared cross-PR
registry (prfix/registry.py), optionally of one exact earlier context, with no
GitHub read and no session. It is saved evidence, never fresh verification.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

from . import admission, configuration, dk_pr_fix, entries, semantic
from .admission import AdmissionError, digest, fields, require, text

NAME = 'dk-ci-flakes'
PAGE = 100
MAX_PAGES = 100
NAMED_FAILURE = re.compile(r'^(?:--- FAIL: |FAILED )([^\s(]+)')  # The named failures prfix/ci.py accepts.
TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s')
DECLARATION = re.compile(r'```dk-flaky\s*\n(.*?)\n```', re.S)
CONTEXT = ('workflow', 'job', 'test')
MAX_CONTEXTS = 200


class Incomplete(Exception):
    """A required read is missing or inconsistent; never reported as an empty result."""


def _target(snapshot):
    request = snapshot['request']
    repository = request['pr']['repository'] if request['pr'] else request['repository']
    host, owner, name = repository.split('/')
    return {'host': host, 'owner': owner, 'name': name, 'repository': repository}


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
        require(all(isinstance(item[field], str) for field in CONTEXT), where, 'workflow, job and test are text')
        key = tuple(item[field] for field in CONTEXT)
        evidence = table.get(key)
        require(evidence is not None, where, 'not an observed test context from the collected evidence')
        require(key not in seen, where, 'duplicate test context')
        seen.add(key)
        require(item['status'] == evidence['status'], where + '.status',
                'the status is the verified evidence status; a session cannot link or unlink a test')
        require(isinstance(item['observations'], list) and item['observations']
                and all(isinstance(ident, str) for ident in item['observations'])
                and set(item['observations']) <= set(evidence['observations']), where + '.observations',
                "cite the context's own observations")
        issue = item.get('issue')
        if evidence['status'] == 'linked':
            require(isinstance(issue, str) and issue in {link['issue'] for link in evidence['links']}, where + '.issue',
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


def inspect(*, request, deployment, repository, transport, github, now, options=None, targets=None,
            invocation=None, roots=None, install=None):
    """cf-workflows:dk-ci-flakes: bounded evidence, one report session; waives and writes nothing."""
    extra = {} if install is None else {'install': install}
    try:
        targets = check_targets(targets)
    except AdmissionError as error:
        return {'status': 'rejected', 'error': {'path': error.path, 'message': error.message}}
    admitted = entries.preflight(NAME, request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation=invocation, options=options, roots=roots, **extra)
    if admitted['preflight'] != 'admitted':
        return {'status': admitted['preflight'], 'admission': admitted}
    target = _target(admitted['snapshot'])
    base = {'repository': target['repository'], 'authority': 'none', 'waives': False}
    blocked = {'status': 'incomplete', 'complete': False, **base, 'tests': [], 'evidence': {}}
    try:
        found = collect(github, target, admitted['entry']['options'], now, targets)
    except Incomplete as error:
        return {**blocked, 'blockers': [str(error)]}
    except Exception as error:  # A failed read is a blocker, never an empty result.
        return {**blocked, 'blockers': ['evidence read failed: ' + type(error).__name__]}
    tests, notes, references = [], [], []
    if found['tests']:
        entry = admitted['entry']
        agent, provider = semantic.validator_agent(admitted)
        task = {'schema_version': 1, 'kind': 'ci_flakes_entry', 'suite': admission.SUITE, 'skill': entry['skill'],
                'mode': 'inspect', 'claim': 'flakes-' + digest({'admission': admitted['snapshot_digest'],
                                                                'evidence': found['digest']})[:32],
                'adapter': entry['adapter'], 'role': entry['definition']['definition'],
                'definition_sha256': entry['task']['definition_sha256'],
                'assignment': entry['definition']['assignment'], 'checklist': entry['definition']['checklist'],
                'role_prompt': entry['role_prompt'],
                'instructions': admitted['snapshot']['request']['original_instructions'],
                'prompts': entry['prompts'], 'options': {key: value['value'] for key, value in entry['options'].items()},
                'agent': agent, 'provider': provider, 'skills': [], 'tools': 'read-only', 'writes': False,
                'publication': False, 'readable': {},
                'context': {key: found[key] for key in ('repository', 'since', 'runs', 'issues', 'observations',
                                                        'tests', 'declared_unobserved', 'limits_reached', 'gaps')},
                'clarifications': []}
        try:
            reference, _, output = configuration.run_session(transport, task)
            tests, notes = check_report(output, found)
        except (AdmissionError, configuration.Blocked) as error:
            return {**blocked, 'blockers': [f'{error.path}: {error.message}']}
        references.append('native:' + reference)
    covered = not found['limits_reached'] and not found['gaps']
    return {'status': 'complete', 'complete': True, 'covered': covered, **base, 'tests': tests,
            'declared_unobserved': found['declared_unobserved'], 'limits_reached': found['limits_reached'],
            'gaps': found['gaps'], 'notes': notes,
            'evidence': {key: found[key] for key in ('since', 'runs', 'issues', 'digest')},
            'evidence_refs': references, 'blockers': []}


def saved(registry, repository, *, context=None, offset=0, limit=20):
    """Saved named outcomes of earlier checks from the shared CI registry; reads nothing else.

    ``registry`` is the installed helper's prfix.registry.Registry, opened read-only
    on the mounted state namespace. Follow ``next_offset`` for further pages.
    """
    require(isinstance(repository, str) and re.fullmatch(r'[a-z0-9.-]+/[a-z0-9_.-]+/[a-z0-9_.-]+',
                                                         repository.lower()), 'repository',
            'must be HOST/OWNER/NAME')
    if context is not None:
        fields(context, 'context', CONTEXT)
        for key in CONTEXT:
            text(context[key], 'context.' + key)
    require(type(offset) is int and offset >= 0, 'offset', 'must be a nonnegative integer')
    require(type(limit) is int and 1 <= limit <= 100, 'limit', 'must be an integer in 1..100')
    page = registry.page(repository.lower(), offset, limit, context)
    return {'status': 'complete', 'saved': True, 'verified': False, 'authority': 'none', 'waives': False,
            'repository': repository.lower(), 'context': copy.deepcopy(context), **page}


WORKFLOW = 'workflows/flakes.yml'


def installed_preflight(*, request, deployment, repository, transport, options=None, targets=None, invocation=None,
                        roots=None, home=None, install=dk_pr_fix.INSTALL):
    """Admit one run of the installed flakes workflow and return its declared inputs; writes nothing.

    The admission is inspect()'s. The installed run targets the PR's repository,
    so a request without a PR rejects. Custom prompts reject, since the installed
    session does not consume semantically admitted text yet.
    """
    try:
        checked = check_targets(targets)
        require(invocation is None or isinstance(invocation, dict) and not invocation.get('prompts'),
                'invocation.prompts', 'the installed flake report session takes no custom prompts yet')
    except AdmissionError as error:
        return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
                'error': {'path': error.path, 'message': error.message}}
    admitted = entries.preflight(NAME, request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation=invocation, options=options, roots=roots,
                                 install=install)
    if admitted['preflight'] != 'admitted':
        return admitted
    snapshot = admitted['snapshot']
    pr = snapshot['request']['pr']
    if pr is None:
        return {**admitted, 'preflight': 'rejected',
                'error': {'path': 'request.pr', 'message': 'the installed entry scans a pull request\'s repository; '
                          'run a repository scan through inspect()'}}
    home = Path(home) if home is not None else dk_pr_fix.state_home()
    namespace = dk_pr_fix.state_namespace(home, pr['repository'])
    for other, name in ((repository, 'repository'), (install, 'installation')):
        if not dk_pr_fix._disjoint(namespace, other):
            return {**admitted, 'preflight': 'rejected',
                    'error': {'path': 'state', 'message': 'state namespace must be disjoint from the ' + name}}
    staged = {'pr': pr['url'], 'instructions': snapshot['request']['original_instructions'],
              'logical_run': snapshot['request']['logical_run']}
    reference = 'requests/' + hashlib.sha256(admission.canonical(staged)).hexdigest() + '.json'
    return {**admitted, 'workflow': entries.ENTRIES[NAME]['skill'],
            'installation': {'root': str(Path(install).resolve()), 'entry': WORKFLOW,
                             'closure_digest': dk_pr_fix.verify_installation(install)},
            'inputs': {'request': reference,
                       'options': {key: value['value'] for key, value in admitted['entry']['options'].items()},
                       'targets': [] if checked is None else checked,
                       'turns': snapshot['options']['turns']['value']},
            'state': {'namespace': str(namespace), 'mount': dk_pr_fix.STATE_MOUNT, 'request': reference,
                      'request_document': staged}}
