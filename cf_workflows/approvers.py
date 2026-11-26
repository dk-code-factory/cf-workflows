"""Helper side of cf-workflows:dk-pr-approvers (#29): review recipients, never a request.

The entry is a preset from cf_workflows.entries: one `discover-approvers` task
on the admitted `leaf` role, no finalizers or reviewers, no skill and no
remote-write authorization. `select()` returns the caller's exact override
without any GitHub read or session, an explicit empty override included.
Otherwise it collects complete read-only evidence (the PR, its paginated
reviews, CODEOWNERS of the target revision in the local checkout, repository
permissions and, only when those select nobody, a bounded maintainer search),
lets one engine-managed session choose among the eligible candidates and checks
every choice against that evidence. The head is read again afterwards.

The result never requests a review, approves or merges. `request_payload()`
turns a complete selection into the exact `request_review` intent payload a
separately authorized publisher may use; an empty selection yields none.
"""
import copy
import fnmatch
import re
import subprocess

from . import admission, configuration, entries, semantic
from .admission import AdmissionError, digest, fields, require, text

NAME = 'dk-pr-approvers'
CODEOWNERS = ('.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS')
LOGIN = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})')
SLUG = re.compile(r'[A-Za-z0-9_-]{1,100}')
SHA = re.compile(r'[0-9a-f]{40}')
REVIEWERS = ('write', 'maintain', 'admin')  # Repository permissions whose reviews count as approvals.
MAX_PAGES = 20
MAX_CANDIDATES = 64


class Incomplete(Exception):
    """Evidence is missing or inconsistent; never reported as an empty selection."""


def _names(values, pattern, path):
    require(isinstance(values, list), path, 'must be an array')
    seen = set()
    for index, value in enumerate(values):
        require(isinstance(value, str) and pattern.fullmatch(value), f'{path}[{index}]',
                'a username without @ or a team slug without ORG/')
        require(value.lower() not in seen, f'{path}[{index}]', 'duplicate recipient')
        seen.add(value.lower())
    return list(values)


def check_override(value):
    """The publisher's exact recipient override, or None for discovery; never filtered."""
    if value is None:
        return None
    fields(value, 'override', ('reviewers', 'team_reviewers'))
    return {'reviewers': _names(value['reviewers'], LOGIN, 'override.reviewers'),
            'team_reviewers': _names(value['team_reviewers'], SLUG, 'override.team_reviewers')}


def _git(repository, *args):
    try:
        return subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True,
                              timeout=60).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise Incomplete('git ' + args[0] + ' failed in the target checkout') from None


def _account(value):
    """{id, login} of a human account, or the exclusion reason."""
    if not isinstance(value, dict) or type(value.get('id')) is not int or not isinstance(value.get('login'), str):
        return None, 'deleted or unknown identity'
    if value.get('type') == 'Bot' or value['login'].lower().endswith('[bot]'):
        return None, 'bot account'
    return {'id': value['id'], 'login': value['login']}, None


def _pattern(raw):
    """A CODEOWNERS pattern as a predicate on repository paths (gitignore subset)."""
    anchored = raw.startswith('/') or '/' in raw.rstrip('/')
    directory = raw.endswith('/')
    pattern = raw.strip('/')
    if not pattern or any(char in pattern for char in '\\[!'):
        return None

    def matches(path):
        # The pattern names the file itself or a directory containing it; a trailing / only a directory.
        parts = path.split('/')
        for start in [0] if anchored else range(len(parts)):
            for end in range(start + 1, len(parts) + (0 if directory else 1)):
                if _glob(pattern, '/'.join(parts[start:end])):
                    return True
        return False
    return matches


def _glob(pattern, path):
    if '**' in pattern:
        regex = re.escape(pattern).replace(r'\*\*/', '(?:.*/)?').replace(r'/\*\*', '(?:/.*)?').replace(r'\*\*', '.*')
        regex = regex.replace(r'\*', '[^/]*').replace(r'\?', '[^/]')
        return re.fullmatch(regex, path) is not None
    return len(pattern.split('/')) == len(path.split('/')) and fnmatch.fnmatchcase(path, pattern)


def codeowners(repository, revision, paths, organization):
    """Owners of each changed path at the target revision: the last matching rule wins."""
    present = set(_git(repository, 'ls-tree', '-r', '--name-only', revision, '--', *CODEOWNERS).decode().split('\n'))
    location = next((path for path in CODEOWNERS if path in present), None)
    if location is None:
        return {'location': None, 'owners': {path: [] for path in paths}, 'gaps': []}
    body = _git(repository, 'show', f'{revision}:{location}').decode('utf-8', 'replace')
    rules, gaps = [], []
    for number, line in enumerate(body.splitlines(), 1):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        matcher = _pattern(pattern)
        if matcher is None:
            raise Incomplete(f'{location}:{number} uses CODEOWNERS syntax this helper cannot evaluate')
        parsed = []
        for owner in owners:
            if owner.startswith('@') and '/' in owner:
                org, slug = owner[1:].split('/', 1)
                if org.lower() == organization.lower() and SLUG.fullmatch(slug):
                    parsed.append(('team', slug, f'{location}:{number}'))
                else:
                    gaps.append(f'{location}:{number}: team {owner} belongs to another organization')
            elif owner.startswith('@') and LOGIN.fullmatch(owner[1:]):
                parsed.append(('user', owner[1:], f'{location}:{number}'))
            else:
                gaps.append(f'{location}:{number}: owner {owner} is not a GitHub user or team')
        rules.append((matcher, parsed))
    owners = {}
    for path in paths:
        owners[path] = next((parsed for matcher, parsed in reversed(rules) if matcher(path)), [])
    return {'location': location, 'owners': owners, 'gaps': gaps}


def _pages(read, label):
    items, page = [], 1
    for _ in range(MAX_PAGES):
        response = read(page)
        fields(response, label, ('items', 'next'))
        require(isinstance(response['items'], list), label + '.items', 'must be an array')
        items.extend(response['items'])
        if response['next'] is None:
            return items
        page = response['next']
    raise Incomplete(label + ' pagination exceeds its bound')


def collect(github, repository, pr, head):
    """Complete read-only evidence and the eligible candidates; raises Incomplete."""
    pull = github.pull_request(pr['url'])
    if not isinstance(pull, dict) or pull.get('html_url', '').lower() != pr['url'].lower():
        raise Incomplete('PR identity is unavailable or differs')
    if (pull.get('head') or {}).get('sha') != head:
        raise Incomplete('the PR head is not the requested head')
    base = (pull.get('base') or {}).get('sha')
    if not isinstance(base, str) or not SHA.fullmatch(base):
        raise Incomplete('the target revision is unavailable')
    author, _ = _account(pull.get('user'))
    if author is None:
        raise Incomplete('the PR author has no immutable identity')
    organization = pr['repository'].split('/')[1]
    merge_base = _git(repository, 'merge-base', base, head).decode().strip()
    paths = sorted(filter(None, _git(repository, 'diff', '--name-only', merge_base, head).decode().split('\n')))
    ownership = codeowners(repository, base, paths, organization)
    reviews = _pages(lambda page: github.reviews(pr['url'], page), 'reviews')
    users, teams, excluded = {}, {}, []

    def add(kind, name, source, reason):
        table = users if kind == 'user' else teams
        item = table.setdefault(name.lower(), {'name': name, 'sources': [], 'reasons': []})
        if source not in item['sources']:
            item['sources'].append(source)
            item['reasons'].append(reason)

    def user(value, source, reason):
        account, why = _account(value)
        if account is None:
            excluded.append({'source': source, 'reason': why})
        elif account['id'] == author['id']:
            excluded.append({'source': source, 'login': account['login'], 'reason': 'PR author'})
        else:
            add('user', account['login'], source, reason)

    for value in pull.get('requested_reviewers', []):
        user(value, 'requested', 'review already requested')
    for value in pull.get('requested_teams', []):
        add('team', value['slug'], 'requested', 'team review already requested')
    dismissed = set()
    for number, review in enumerate(reviews):
        state = review.get('state')
        account = (review.get('user') or {}).get('login')
        if state == 'DISMISSED':
            dismissed.add(str(account).lower())
            continue
        if state in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'):
            user(review.get('user'), f'review:{number}', 'reviewed an earlier revision of this PR')
    for path, owned in ownership['owners'].items():
        for kind, name, source in owned:
            if kind == 'user' and name.lower() == author['login'].lower():
                continue
            add(kind, name, source, 'owns ' + path)
    for name in sorted(dismissed - set(users)):
        excluded.append({'source': 'review', 'login': name, 'reason': 'only dismissed reviews'})

    def eligible():
        result = []
        for kind, table, read in (('user', users, github.permission), ('team', teams, github.team_permission)):
            for key in sorted(table):
                item = table[key]
                if 'permission' not in item:
                    item['permission'] = read(pr['repository'], item['name'])
                if item['permission'] in REVIEWERS:
                    result.append({'kind': kind, **item})
                elif not item.get('excluded'):
                    item['excluded'] = True
                    excluded.append({'source': item['sources'][0], kind: item['name'],
                                     'reason': 'cannot approve in the repository: ' + str(item['permission'])})
        return result
    candidates = eligible()
    searched = None
    if not candidates and paths:
        # Ownership does not resolve the selection: a bounded, path-specific maintainer search.
        searched = github.maintainers(pr['repository'], paths)
        fields(searched, 'maintainers', ('items', 'complete'))
        if searched['complete'] is not True:
            raise Incomplete('the maintainer search is incomplete')
        for number, value in enumerate(searched['items'][:MAX_CANDIDATES]):
            user(value, f'maintainers:{number}', str(value.get('reason') or 'recent maintainer of changed paths'))
        candidates = eligible()
    evidence = {'pr': pr['url'], 'head': head, 'target': base, 'author': author, 'paths': paths,
                'codeowners': ownership['location'], 'reviews': len(reviews),
                'maintainer_search': None if searched is None else len(searched['items'])}
    return {'candidates': candidates[:MAX_CANDIDATES], 'excluded': excluded, 'gaps': ownership['gaps'],
            'evidence': evidence, 'digest': digest({'evidence': evidence, 'reviews': reviews,
                                                    'ownership': ownership['owners'], 'candidates': candidates})}


def check_selection(output, candidates):
    """The session's choices, each an eligible evidenced candidate with a reason."""
    fields(output, 'selection', ('reviewers', 'team_reviewers', 'summary'))
    text(output['summary'], 'selection.summary')
    table = {(item['kind'], item['name'].lower()): item for item in candidates}
    result = {'reviewers': [], 'team_reviewers': [], 'reasons': []}
    for key, kind, name in (('reviewers', 'user', 'login'), ('team_reviewers', 'team', 'slug')):
        require(isinstance(output[key], list), 'selection.' + key, 'must be an array')
        for number, item in enumerate(output[key]):
            where = f'selection.{key}[{number}]'
            fields(item, where, (name, 'reason', 'sources'))
            candidate = table.get((kind, str(item[name]).lower()))
            require(candidate is not None, where, 'not an eligible candidate from the collected evidence')
            require(candidate['name'] not in result[key], where, 'duplicate recipient')
            require(isinstance(item['sources'], list) and item['sources']
                    and set(item['sources']) <= set(candidate['sources']), where + '.sources',
                    "cite the candidate's own evidence")
            result[key].append(candidate['name'])
            result['reasons'].append({kind: candidate['name'], 'reason': text(item['reason'], where + '.reason'),
                                      'sources': list(item['sources'])})
    return result


def select(*, request, head, deployment, repository, transport, github, override=None, invocation=None,
           roots=None, install=None):
    """cf-workflows:dk-pr-approvers: an exact override, or evidence-based discovery; requests nothing."""
    extra = {} if install is None else {'install': install}
    try:
        override = check_override(override)
        require(isinstance(head, str) and SHA.fullmatch(head), 'head', 'the exact published head SHA')
    except AdmissionError as error:
        return {'status': 'rejected', 'error': {'path': error.path, 'message': error.message}}
    admitted = entries.preflight(NAME, request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation=invocation, roots=roots, **extra)
    if admitted['preflight'] != 'admitted':
        return {'status': admitted['preflight'], 'admission': admitted}
    pr = admitted['snapshot']['request']['pr']
    if pr is None:
        return {'status': 'rejected', 'error': {'path': 'request.pr', 'message': 'approvers need a pull request'}}
    base = {'pr': pr['url'], 'head': head, 'authority': 'none', 'requested': False}
    if override is not None:
        # The exact override, including two empty lists; no discovery and no fallback.
        return {'status': 'complete', 'complete': True, **base, 'source': 'override', **override,
                'reasons': [], 'evidence': {}, 'blockers': []}
    blocked = {'status': 'incomplete', 'complete': False, **base, 'source': 'automatic', 'reviewers': [],
               'team_reviewers': [], 'reasons': [], 'evidence': {}}
    try:
        found = collect(github, repository, pr, head)
    except Incomplete as error:
        return {**blocked, 'blockers': [str(error)]}
    except Exception as error:  # A failed read is a blocker, never an empty selection.
        return {**blocked, 'blockers': ['evidence read failed: ' + type(error).__name__]}
    selection = {'reviewers': [], 'team_reviewers': [], 'reasons': []}
    references = []
    if found['candidates']:
        entry = admitted['entry']
        agent, provider = semantic.validator_agent(admitted)
        task = {'schema_version': 1, 'kind': 'approvers_entry', 'suite': admission.SUITE, 'skill': entry['skill'],
                'mode': 'select', 'claim': 'approvers-' + digest({'admission': admitted['snapshot_digest'],
                                                                  'evidence': found['digest']})[:32],
                'adapter': entry['adapter'], 'role': entry['definition']['definition'],
                'definition_sha256': entry['task']['definition_sha256'],
                'assignment': entry['definition']['assignment'], 'checklist': entry['definition']['checklist'],
                'role_prompt': entry['role_prompt'], 'instructions': admitted['snapshot']['request']['original_instructions'],
                'prompts': entry['prompts'], 'options': {}, 'agent': agent, 'provider': provider, 'skills': [],
                'tools': 'read-only', 'writes': False, 'publication': False, 'readable': {},
                'context': {'evidence': found['evidence'], 'candidates': found['candidates'],
                            'excluded': found['excluded']}, 'clarifications': []}
        try:
            reference, _, output = configuration.run_session(transport, task)
            selection = check_selection(output, found['candidates'])
        except (AdmissionError, configuration.Blocked) as error:
            return {**blocked, 'blockers': [f'{error.path}: {error.message}']}
        references.append('native:' + reference)
    try:
        current = (github.pull_request(pr['url']).get('head') or {}).get('sha')
    except Exception as error:
        return {**blocked, 'blockers': ['head recheck failed: ' + type(error).__name__]}
    if current != head:
        return {**blocked, 'blockers': ['the PR head changed during discovery']}
    reason = None if found['candidates'] else 'complete evidence supports no eligible recipient'
    return {'status': 'complete', 'complete': True, **base, 'source': 'automatic', **selection,
            'evidence': {**found['evidence'], 'digest': found['digest'], 'excluded': found['excluded'],
                         'gaps': found['gaps'], 'empty_reason': reason},
            'evidence_refs': references, 'blockers': []}


def request_payload(selection, *, pr, head):
    """The exact `request_review` intent payload for a complete selection at the same PR and head.

    None when the selection is empty, so an explicit empty override requests
    nothing. The payload grants nothing: publication keeps its own permission,
    authorization and current-head checks.
    """
    if not selection.get('complete') or selection.get('pr') != pr or selection.get('head') != head:
        raise ValueError('only a complete selection for the same PR and head can be requested')
    payload = {key: list(selection[key]) for key in ('reviewers', 'team_reviewers') if selection[key]}
    return copy.deepcopy(payload) or None
