"""Installed side of cf-workflows:dk-pr-approvers (#29): review recipients, never a request.

workflows/approvers.yml runs three steps. `approvers_collect` (a retained
preparation operation) returns the caller's exact override with no GitHub read
and no session, an explicit empty override included. Otherwise it collects
complete read-only evidence: the PR at the requested head, its paginated
reviews, CODEOWNERS of the target revision in the checkout, repository
permissions and, only when those select nobody, a bounded path-specific
maintainer search. With no eligible candidate the result is complete and empty
without a session. Otherwise the engine runs one `agent/run` session on the
admitted `leaf` role with no skills, and `approvers_check` accepts only
eligible candidates that cite their own evidence, requires an unchanged
checkout and reads the head again.

Nothing here requests a review, approves, merges or writes the checkout; a
failed or incomplete read is a blocker, never an empty selection. The evidence
and selection checks between the markers are a verbatim copy of
cf_workflows/approvers.py; tests/test_reviewer_skills.py keeps the two identical.
"""
import fnmatch
import re
import subprocess
from urllib.parse import quote

from .core import artifact, digest, identity, read_artifact

MAX_TEXT = 64 << 10
MAX_SEARCH_PATHS = 20  # Changed paths a maintainer search covers; more is an incomplete search.
SEARCH_COMMITS = 10  # Recent commits read per changed path.
SKIPPED = ('skipped', None, '')  # A session status when the select job did not run.
# The baseline role prompt and bundled adapter of the entry (cf_workflows.entries, cf_workflows.admission).
ROLE_PROMPT = ('Resolve GitHub review and re-review recipients for a PR, returning a configured override or '
               'discovering appropriate reviewers from repository and PR evidence. Returns recipients without '
               'requesting reviews.\n\nPrefer appropriate existing requests and previous human reviewers for '
               're-review. Do not select someone solely from dismissed reviews without another supported '
               'reason. Do not assume the authenticated actor is the PR author or exclude them on that '
               'basis.\n\nWhen PR history does not establish suitable recipients, inspect changed paths and '
               'repository ownership rules at the target revision. If ownership does not resolve the '
               'selection, assess relevant recent maintainers from path-specific history or merged PR '
               'reviews, with a bounded targeted search. Avoid unrelated popular contributors and the entire '
               'contributor history.\n')
ASSIGNMENT = ('Select GitHub review recipients for the pinned PR head only from the evidence in the assignment: '
              'existing requests, earlier human reviews, CODEOWNERS and bounded maintainer evidence. Give each '
              'recipient its evidence sources and a reason. Request nothing, approve nothing and change no setting.')
CHECKLIST = [
    {'id': 'assignment', 'text': 'Prefer appropriate existing requests and earlier human reviewers.'},
    {'id': 'evidence', 'text': 'Cite the evidence of every selected recipient.'},
    {'id': 'scope', 'text': 'Select only eligible candidates; grant no authority.'}]
CONTRACT = ('Return JSON {reviewers: [{login, reason, sources}], team_reviewers: [{slug, reason, sources}], '
            'summary}. Choose only candidates from task_input.candidates; sources are that candidate\'s own '
            'evidence sources. An empty selection is valid when no candidate fits. Do not edit files, request '
            'reviews, approve, merge or change settings.')


class AdmissionError(ValueError):
    """Structural rejection naming the offending input path."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


def require(condition, path, message):
    if not condition:
        raise AdmissionError(path, message)


def text(value, path, limit=MAX_TEXT):
    require(isinstance(value, str) and value.strip() and '\0' not in value,
            path, 'must be a nonempty string without NUL')
    try:
        size = len(value.encode('utf-8', 'strict'))
    except UnicodeEncodeError:
        raise AdmissionError(path, 'must be valid Unicode text') from None
    require(size <= limit, path, 'exceeds byte bound')
    return value


def fields(value, path, required, optional=()):
    require(isinstance(value, dict), path, 'must be an object')
    unknown = set(value) - set(required) - set(optional)
    require(not unknown, path, 'unsupported field(s): ' + ', '.join(sorted(map(str, unknown))))
    missing = set(required) - set(value)
    require(not missing, path, 'missing field(s): ' + ', '.join(sorted(missing)))
    return value


# -- Verbatim copy of cf_workflows/approvers.py: evidence and selection checks ------------------

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

    # A literal last segment names the file itself or a directory containing it (a trailing / only
    # a directory). A wildcard last segment without ** matches at exactly its depth, as GitHub
    # documents: `docs/*` owns docs/a.md but not docs/build-app/b.md.
    exact = not directory and '**' not in pattern and any(char in pattern.split('/')[-1] for char in '*?')

    def matches(path):
        parts = path.split('/')
        for start in [0] if anchored else range(len(parts)):
            if exact:
                if _glob(pattern, '/'.join(parts[start:])):
                    return True
                continue
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
<<<<<<< ours
    paths = sorted(filter(None, _git(repository, 'diff', '-z', '--name-only', merge_base, head).decode().split('\0')))
=======
    changed = _git(repository, 'diff', '-z', '--name-only', merge_base, head).decode().split('\0')
    paths = sorted(filter(None, changed))
>>>>>>> theirs
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
                    and all(isinstance(source, str) for source in item['sources'])
                    and set(item['sources']) <= set(candidate['sources']), where + '.sources',
                    "cite the candidate's own evidence")
            result[key].append(candidate['name'])
            result['reasons'].append({kind: candidate['name'], 'reason': text(item['reason'], where + '.reason'),
                                      'sources': list(item['sources'])})
    return result


# -- End of the verbatim copy -----------------------------------------------------------------


class GitHubEvidence:
    """The read-only GitHub reads collect() needs, over the installed gh transport (capture.GitHub)."""

    def __init__(self, transport):
        self.transport = transport
        self.teams = {}

    def _route(self, repository):
        host, owner, name = repository.split('/')
        self.transport.host = host
        return f'repos/{owner}/{name}'

    def _pull(self, url):
        pr = identity(url)
        return self._route(pr['repository']) + '/pulls/' + str(pr['number'])

    def pull_request(self, url):
        return self.transport.request('GET', self._pull(url))

    def reviews(self, url, page):
        items = self.transport.request('GET', f'{self._pull(url)}/reviews?per_page=100&page={page}')
        if not isinstance(items, list):
            raise Incomplete('the reviews response is not a list')
        return {'items': items, 'next': page + 1 if len(items) == 100 else None}

    def permission(self, repository, login):
        value = self.transport.request('GET', f'{self._route(repository)}/collaborators/{login}/permission')
        if not isinstance(value, dict):
            raise Incomplete('the permission response is not an object')
        # role_name distinguishes maintain from write; permission is the legacy level, which a custom
        # role name (based on one of the levels) falls back to.
        role = value.get('role_name')
        return role if role in REVIEWERS else value.get('permission')

    def team_permission(self, repository, slug):
        if repository not in self.teams:
            rows = self.transport.pages(self._route(repository) + '/teams')
            self.teams[repository] = {str(row.get('slug')).lower(): row.get('permission') for row in rows}
        value = self.teams[repository].get(slug.lower(), 'none')
        return {'push': 'write', 'pull': 'read'}.get(value, value)

    def maintainers(self, repository, paths):
        """Authors of recent commits to each changed path; incomplete beyond its bound."""
        if len(paths) > MAX_SEARCH_PATHS:
            return {'items': [], 'complete': False}
        route, items, seen = self._route(repository), [], set()
        for path in paths:
            encoded = quote(path, safe='/-_.~')
            if '%' in encoded:
                return {'items': items, 'complete': False}
            commits = self.transport.request('GET', f'{route}/commits?path={encoded}&per_page={SEARCH_COMMITS}')
            if not isinstance(commits, list):
                raise Incomplete('the commit history response is not a list')
            for commit in commits:
                author = commit.get('author') if isinstance(commit, dict) else None
                login = (author or {}).get('login')
                if isinstance(login, str) and login.lower() not in seen:
                    seen.add(login.lower())
                    items.append({**author, 'reason': 'recent commits to ' + path})
        return {'items': items, 'complete': True}


def evidence_provider(root, provider):
    """The injected evidence reads, or the installed gh transport."""
    if provider is not None:
        return provider
    from .capture import GitHub
    return GitHubEvidence(GitHub(root))


def workspace(root):
    """The checkout's HEAD and status; the selection session must leave both unchanged."""
    head = _git(root, 'rev-parse', 'HEAD').decode().strip()
    status = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--', '.',
                  ':(exclude).agent-workflow', ':(exclude)workflows/.pr-fix-portable')
    return digest([head, status.hex()])


def _base(request, head):
    return {'pr': identity(request['pr'])['url'], 'head': head, 'authority': 'none', 'requested': False}


def _blocked(base, blockers):
    return {'status': 'incomplete', 'complete': False, **base, 'source': 'automatic', 'reviewers': [],
            'team_reviewers': [], 'reasons': [], 'evidence': {}, 'evidence_refs': [], 'blockers': blockers}


def collect_operation(root, provider, request, inputs):
    """approvers_collect: the final override or blocked result, or the evidence and one session's assignment.

    override `{}` selects discovery; `{reviewers, team_reviewers}` is the exact
    override, two empty lists included, and reads nothing.
    """
    head, raw = inputs['head'], inputs['override']
    try:
        require(isinstance(head, str) and SHA.fullmatch(head), 'head', 'the exact published head SHA')
        require(not request.get('authorizations') and not request.get('maintain_other'), 'request.authorizations',
                'approver discovery requests, approves and merges nothing; it takes no authorization')
        override = check_override(None if raw == {} else raw)
    except AdmissionError as error:
        raise ValueError(f'{error.path}: {error.message}') from None
    base = _base(request, head)
    if override is not None:
        # The exact override, including two empty lists; no discovery, read or session.
        return {'session': False, 'evidence': None,
                'result': {'status': 'complete', 'complete': True, **base, 'source': 'override', **override,
                           'reasons': [], 'evidence': {}, 'evidence_refs': [], 'blockers': []}}
    pr = identity(request['pr'])
    try:
        found = collect(provider, root, {'url': pr['url'], 'repository': pr['repository']}, head)
        state = workspace(root)
    except Incomplete as error:
        return {'session': False, 'evidence': None, 'result': _blocked(base, [str(error)])}
    except Exception as error:  # A failed read is a blocker, never an empty selection.
        return {'session': False, 'evidence': None,
                'result': _blocked(base, ['evidence read failed: ' + type(error).__name__])}
    reference = artifact(root, {'kind': 'approvers_evidence', 'base': base, 'found': found, 'workspace': state})
    output = {'session': bool(found['candidates']), 'evidence': reference, 'result': None}
    if found['candidates']:
        output['prompt'] = ROLE_PROMPT + '\n' + ASSIGNMENT + '\n\n' + CONTRACT
        output['task_input'] = {'pr': base['pr'], 'head': head, 'checklist': CHECKLIST,
                                'evidence': found['evidence'], 'candidates': found['candidates'],
                                'excluded': found['excluded']}
    return output


def check_operation(root, provider, inputs):
    """approvers_check: the selection result; a session it cannot accept or a changed head blocks."""
    collected, session = inputs['collected'], inputs['session']
    if not isinstance(collected, dict) or not isinstance(session, dict):
        raise ValueError('approver check requires the collected evidence and the session result')
    if collected.get('evidence') is None:
        if collected.get('session') or session.get('status') not in SKIPPED:
            raise ValueError('no session runs without collected evidence')
        return collected['result']
    record = read_artifact(root, collected['evidence'])
    if record.get('kind') != 'approvers_evidence':
        raise ValueError('approver evidence reference is not approver evidence')
    base, found = record['base'], record['found']
    selection, references = {'reviewers': [], 'team_reviewers': [], 'reasons': []}, []
    if found['candidates']:
        if session.get('status') != 'succeeded' or not isinstance(session.get('output'), dict):
            return _blocked(base, ['the selection session did not complete: ' + str(session.get('status'))])
        try:
            selection = check_selection(session['output'], found['candidates'])
        except AdmissionError as error:
            return _blocked(base, [f'{error.path}: {error.message}'])
        references.append('native:' + str(session.get('agent')))
    elif session.get('status') not in SKIPPED:
        raise ValueError('no session runs without an eligible candidate')
    try:
        if workspace(root) != record['workspace']:
            return _blocked(base, ['the checkout changed during discovery'])
        current = (provider.pull_request(base['pr']).get('head') or {}).get('sha')
    except Exception as error:
        return _blocked(base, ['head recheck failed: ' + type(error).__name__])
    if current != base['head']:
        return _blocked(base, ['the PR head changed during discovery'])
    reason = None if found['candidates'] else 'complete evidence supports no eligible recipient'
    return {'status': 'complete', 'complete': True, **base, 'source': 'automatic', **selection,
            'evidence': {**found['evidence'], 'digest': found['digest'], 'excluded': found['excluded'],
                         'gaps': found['gaps'], 'empty_reason': reason},
            'evidence_refs': references, 'blockers': []}
