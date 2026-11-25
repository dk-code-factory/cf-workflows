"""Structural configuration admission and complete input resolution (contract v1).

Admission reads and hashes declared inputs. It never loads or executes a
user-supplied skill, starts a provider session, runs a target task or performs a
remote write. The only external operation is one read-only capability query on
the supplied engine transport, made after every local structural check passes.
Semantic assessment of custom prompts and supplied skills is not implemented
here; until it is, such proposals are rejected at preflight.

Baseline adapter texts and selection rules are adapted from the pinned portable
PR-fix example (Agent Workflow, MIT; see licenses/agent-workflow-MIT.txt).
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
from urllib.parse import urlsplit

SUITE = 'cf-workflows'
MAX_FILE = 4 << 20
MAX_TEXT = 64 << 10
MAX_CLOSURE = 256
MAX_SKILL_FILES = 512
MAX_TASKS = 40
MAX_ADMISSION_INPUT = 1 << 20
SHA256 = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'[a-z][a-z0-9-]{0,63}')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
LINK = re.compile(r'\]\(([^)\s]+)(?:\s+"[^"]*")?\)')

ACTIONS = ('push', 'edit_pr', 'reply', 'resolve_thread', 'request_review', 'create_pr',
           'create_issue', 'edit_issue', 'post_findings', 'approve_fork_ci', 'rerun_ci',
           'approve_pr', 'merge')
PRECEDENCE = ('personal', 'repository', 'upstream', 'pr', 'invocation')
ADMITTED_ROLES = ('leaf', 'reviewer', 'verifier', 'fixer', 'audience')
GUIDANCE = ('AGENTS.md', 'CLAUDE.md', 'CONTRIBUTING.md', '.github/copilot-instructions.md')
STAGES = ('publication', 'reviews', 'ci')
KINDS = ('review', 'inspection', 'decisions', 'fix', 'scope', 'description', 'cleanup',
         'synchronization', 'task')
PROMPT_SLOTS = ('task', 'reviewer', 'finalizer', 'assessment', 'selection', 'reconciliation',
                'reply', 'cleanup', 'commit', 'behavior_replacement', 'behavior_addition',
                'completion', 'additional')
LIMIT_BOUNDS = {'review_cycles': (1, 10), 'retries_per_task': (0, 1),
                'ci_repairs_per_cycle': (0, 5), 'ci_reruns_per_head': (0, 3)}
BASE_CAPABILITIES = ('native_session', 'admitted_roles')
SKILL_CAPABILITY = 'session_scoped_skills'
MERGE_CAPABILITY = 'merge_guard'

VALIDATOR_ROLE = 'configuration-validator'
BOOTSTRAP_DEFINITION = Path(__file__).resolve().parent / 'bootstrap' / 'configuration-validator.toml'
BOOTSTRAP_SHA256 = 'bcc2bd2bc87ede65f0aee2dde86b85b8334e4f7fc961b5e132a38bd548114896'

CHECKLIST = [
    {'id': 'assignment', 'text': 'Perform the exact assigned task against current source and retained original instructions.'},
    {'id': 'evidence', 'text': 'Attach source files with current SHA-256 evidence and preserve every original finding/source identity.'},
    {'id': 'scope', 'text': 'Account for requirements, blockers and all proposed changes or publication operations without expanding user authority.'},
]
BASELINE_ADAPTERS = {
    'read-feedback': {'kind': 'inspection', 'writes': False, 'definition': 'leaf', 'assignment':
        'Capture all actionable inline threads, review bodies and general comments; preserve source identities and obligations.'},
    'reconcile-decisions': {'kind': 'decisions', 'writes': False, 'definition': 'leaf', 'assignment':
        'Reconcile settled decisions from original sources, exact request and repository guidance; do not invent user authority.'},
    'address-feedback': {'kind': 'fix', 'writes': True, 'definition': 'leaf', 'assignment':
        'Verify actionable feedback and implement focused changes. Account for original findings and every changed file; preserve unrelated work.'},
    'assess-scope': {'kind': 'scope', 'writes': False, 'definition': 'leaf', 'assignment':
        'Assess the complete diff against the selected size/scope policies; report concrete boundaries and ownership evidence. '
        'Splitting publication requires separately supplied explicit authority.'},
    'describe-pr': {'kind': 'description', 'writes': False, 'definition': 'leaf', 'assignment':
        'Prepare a repository-native PR title and description from the actual final diff and accepted issue; '
        'propose exact authorized publication operations only.'},
    'inspect-commits': {'kind': 'cleanup', 'writes': False, 'definition': 'leaf', 'assignment':
        'Inspect the entire commit series against selected commit policy and final tree. Report blockers and exact justified '
        'history proposals; do not rewrite Git directly.'},
    'sync-guidance': {'kind': 'synchronization', 'writes': True, 'definition': 'leaf', 'assignment':
        'Synchronize governing repository content only where the exact original request and selected policy require changes; '
        'account for every changed path.'},
    'review': {'kind': 'review', 'writes': False, 'definition': 'reviewer', 'assignment':
        'Review the actual changes against the originating issue, documented repository standards and accepted user scope. '
        'Return supported actionable findings or an evidenced no-findings report.', 'checklist': [
            {'id': 'assignment', 'text': 'Inspect the actual diff and originating requirements.'},
            {'id': 'evidence', 'text': 'Verify source evidence and every concrete finding.'},
            {'id': 'scope', 'text': 'Account for repository rules, changed tests and unresolved checks.'}]},
}
DEFAULTS = {
    'tasks': ['read-feedback', 'reconcile-decisions', 'address-feedback', {'stage': 'publication'},
              'assess-scope', 'describe-pr', {'stage': 'reviews'}, {'stage': 'publication'}],
    'finalizers': ['inspect-commits', 'sync-guidance', {'stage': 'ci'}],
    'reviewers': ['review'],
    'adapters': {},
    'limits': {'review_cycles': 5, 'retries_per_task': 1, 'ci_repairs_per_cycle': 5, 'ci_reruns_per_head': 3},
    'ci': {'timeout_seconds': 1800},
    'merge_method': 'merge',
    'accounts': [],
    'settings': {},
    'turns': 8,
    'requirements': [],
    'prohibitions': [],
}
ACCUMULATED = ('requirements', 'prohibitions')


class AdmissionError(ValueError):
    """Structural rejection naming the offending input path."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition, path, message):
    if not condition:
        raise AdmissionError(path, message)


def text(value, path, limit=MAX_TEXT):
    require(isinstance(value, str) and value.strip() and '\0' not in value,
            path, 'must be a nonempty string without NUL')
    require(len(value.encode('utf-8', 'strict')) <= limit, path, 'exceeds byte bound')
    return value


def fields(value, path, required, optional=()):
    require(isinstance(value, dict), path, 'must be an object')
    unknown = set(value) - set(required) - set(optional)
    require(not unknown, path, 'unsupported field(s): ' + ', '.join(sorted(map(str, unknown))))
    missing = set(required) - set(value)
    require(not missing, path, 'missing field(s): ' + ', '.join(sorted(missing)))
    return value


def load_json(raw):
    """Parse a contract document: duplicate keys and nonfinite numbers reject."""
    def pairs(entries):
        result = {}
        for key, value in entries:
            require(key not in result, '$', 'duplicate JSON key: ' + key)
            result[key] = value
        return result

    def constant(value):
        raise AdmissionError('$', 'nonfinite JSON number: ' + value)
    require(len(raw.encode('utf-8') if isinstance(raw, str) else raw) <= MAX_TEXT * 16,
            '$', 'JSON exceeds byte bound')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


# -- Resource resolution ----------------------------------------------------------------------

def relative(value, path):
    value = text(value, path, 4096)
    pure = PurePosixPath(value)
    require(not pure.is_absolute() and pure.as_posix() == value and value not in ('', '.')
            and not any(part in ('', '.', '..') for part in value.split('/')),
            path, 'must be a normalized relative path inside its root')
    return value


def lookup(root, rel, path):
    """Return (path, mode) below root, rejecting missing entries and any symlink component."""
    current = root
    for part in rel.split('/'):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except (FileNotFoundError, NotADirectoryError):
            raise AdmissionError(path, 'missing resource ' + rel) from None
        require(not stat.S_ISLNK(mode), path, 'resource path traverses a symlink: ' + rel)
    return current, mode


def read_file(root, rel, path, limit=MAX_FILE):
    """Read a bounded regular file below root without following any symlink."""
    current, mode = lookup(root, rel, path)
    require(stat.S_ISREG(mode), path, 'resource is not a regular file: ' + rel)
    descriptor = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), path, 'resource changed type: ' + rel)
        content = stream.read(limit + 1)
    require(len(content) <= limit, path, 'resource exceeds byte bound: ' + rel)
    return content


def links(content, rel, path):
    """Local Markdown link targets of a file, resolved relative to its root; escapes reject."""
    try:
        body = content.decode('utf-8')
    except UnicodeDecodeError:
        raise AdmissionError(path, 'Markdown resource is not UTF-8: ' + rel) from None
    result = []
    for link in LINK.findall(body):
        if '://' in link or link.startswith(('#', 'mailto:')):
            continue
        target = link.split('#')[0]
        if not target:
            continue
        require(not target.startswith('/'), path, f'{rel} links outside its root: {link}')
        parts = []
        for part in [*PurePosixPath(rel).parent.parts, *target.split('/')]:
            if part in ('', '.'):
                continue
            if part == '..':
                require(parts, path, f'{rel} links outside its root: {link}')
                parts.pop()
            else:
                parts.append(part)
        require(parts, path, f'{rel} has an empty link: {link}')
        result.append('/'.join(parts))
    return result


def follow(root, start, path, allowed=None):
    """Transitive local Markdown closure from start files; returns {path: sha256}."""
    found, pending = {}, list(start)
    while pending:
        rel = pending.pop()
        if rel in found:
            continue
        require(allowed is None or rel in allowed, path, 'incomplete resource closure: ' + rel)
        require(len(found) < MAX_CLOSURE, path, 'resource closure exceeds bound')
        if allowed is None and stat.S_ISDIR(lookup(root, rel, path)[1]):
            continue  # Guidance may link a directory; only files are instruction resources.
        content = read_file(root, rel, path)
        found[rel] = hashlib.sha256(content).hexdigest()
        if rel.endswith('.md'):
            pending.extend(links(content, rel, path))
    return dict(sorted(found.items()))


def resolve_resources(records, roots, path):
    require(isinstance(records, list), path, 'must be an array')
    resolved = {}
    for index, record in enumerate(records):
        where = f'{path}[{index}]'
        fields(record, where, ('root', 'path', 'sha256', 'purpose', 'owner'))
        require(record['root'] in roots, where + '.root', 'unknown resource root')
        rel = relative(record['path'], where + '.path')
        require(isinstance(record['sha256'], str) and SHA256.fullmatch(record['sha256']),
                where + '.sha256', 'must be lowercase SHA-256')
        key = record['root'] + ':' + rel
        require(key not in resolved, where, 'duplicate resource ' + key)
        actual = hashlib.sha256(read_file(roots[record['root']], rel, where)).hexdigest()
        require(actual == record['sha256'], where + '.sha256', 'resource digest mismatch: ' + key)
        resolved[key] = {'root': record['root'], 'path': rel, 'sha256': actual,
                         'purpose': text(record['purpose'], where + '.purpose', 512),
                         'owner': text(record['owner'], where + '.owner', 512)}
    # Every declared Markdown resource must declare its own local link closure.
    for key, item in resolved.items():
        if item['path'].endswith('.md'):
            content = read_file(roots[item['root']], item['path'], path)
            for target in links(content, item['path'], path):
                require(item['root'] + ':' + target in resolved, path,
                        f'incomplete resource closure: {key} references undeclared {target}')
    return dict(sorted(resolved.items()))


def resource_ref(value, resources, path):
    require(isinstance(value, str) and value in resources, path, 'references an undeclared resource')
    return value


def resolve_skill(record, roots, path):
    """Hash a user-supplied skill's declared files; never parse it as instructions or run it."""
    for field in ('host', 'installed_version', 'qualified_name'):
        require(not isinstance(record, dict) or field not in record, path,
                'host-installed skills are never discovered or supported; supply files pinned by digest')
    fields(record, path, ('id', 'name', 'root', 'path', 'sha256', 'files'), ('entry',))
    require(isinstance(record['id'], str) and IDENT.fullmatch(record['id']), path + '.id', 'invalid identifier')
    require(isinstance(record['name'], str) and NAME.fullmatch(record['name']), path + '.name',
            'must be an unqualified skill name')
    require(record['root'] in roots, path + '.root', 'unknown skill root')
    base = relative(record['path'], path + '.path')
    root = roots[record['root']]
    current = root
    for part in base.split('/'):
        current = current / part
        require(current.exists() or current.is_symlink(), path + '.path', 'missing skill directory')
        require(not current.is_symlink(), path + '.path', 'skill directory traverses a symlink')
    require(current.is_dir(), path + '.path', 'skill path is not a directory')
    actual = []
    for directory, subdirs, files in os.walk(current, followlinks=False):
        for name in subdirs + files:
            entry = Path(directory) / name
            require(not entry.is_symlink(), path, 'skill contains a symlink: ' + str(entry.relative_to(current)))
        actual.extend((Path(directory) / name).relative_to(current).as_posix() for name in files)
        require(len(actual) <= MAX_SKILL_FILES, path, 'skill has too many files')
    declared = record['files']
    require(isinstance(declared, list) and declared, path + '.files', 'must be a nonempty array')
    expected = {}
    for index, item in enumerate(declared):
        where = f'{path}.files[{index}]'
        fields(item, where, ('path', 'sha256'))
        rel = relative(item['path'], where + '.path')
        require(rel not in expected, where, 'duplicate skill file')
        require(isinstance(item['sha256'], str) and SHA256.fullmatch(item['sha256']), where + '.sha256',
                'must be lowercase SHA-256')
        expected[rel] = item['sha256']
    require(set(actual) == set(expected), path + '.files',
            'declared files differ from the skill directory: ' +
            ', '.join(sorted(set(actual) ^ set(expected))))
    entry = record.get('entry', 'SKILL.md')
    require(entry in expected, path + '.entry', 'entry is not a declared skill file')
    closure = follow(current, [entry], path, allowed=set(expected))
    files = {}
    for rel in sorted(expected):
        files[rel] = closure.get(rel) or hashlib.sha256(read_file(current, rel, path)).hexdigest()
        require(files[rel] == expected[rel], path + '.files', 'skill file digest mismatch: ' + rel)
    tree = skill_digest(files)
    require(record['sha256'] == tree, path + '.sha256', 'skill content digest mismatch')
    return {'id': record['id'], 'name': record['name'], 'root': record['root'], 'path': base,
            'entry': entry, 'sha256': tree, 'files': files}


def skill_digest(files):
    """Content digest of a supplied skill: canonical JSON of {path: sha256}."""
    return digest(dict(sorted(files.items())))


# -- Roles and bootstrap ----------------------------------------------------------------------

def role_definition(content, path):
    try:
        value = tomllib.loads(content.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise AdmissionError(path, 'role definition is not valid UTF-8 TOML') from None
    allowed = {'name', 'description', 'developer_instructions', 'model', 'model_reasoning_effort'}
    require(not set(value) - allowed and all(isinstance(value.get(key), str) and value[key].strip()
                                             for key in ('name', 'description', 'developer_instructions')),
            path, 'unsupported role definition')


def bootstrap():
    """Admit the bundled configuration-validator definition structurally, never semantically.

    This is the bootstrap boundary: its digest is pinned in code, it accepts no
    configuration, prompts or skills, and nothing assesses it, so validation of
    any proposal terminates after one level.
    """
    content = read_file(BOOTSTRAP_DEFINITION.parent, BOOTSTRAP_DEFINITION.name, 'bootstrap')
    actual = hashlib.sha256(content).hexdigest()
    require(actual == BOOTSTRAP_SHA256, 'bootstrap', 'bundled validator definition differs from its pin')
    role_definition(content, 'bootstrap')
    return {'role': VALIDATOR_ROLE, 'sha256': actual, 'structural': 'accepted',
            'semantic': None, 'bootstrap': True, 'skills': [], 'prompts': []}


def resolve_roles(roles, roots, path):
    require(isinstance(roles, dict), path, 'must be an object')
    require(VALIDATOR_ROLE not in roles, path, 'the configuration validator role is bundled and cannot be supplied')
    require(set(roles) == set(ADMITTED_ROLES), path, 'admitted role catalog must be exactly ' + ', '.join(ADMITTED_ROLES))
    resolved = {}
    for name in ADMITTED_ROLES:
        where = f'{path}.{name}'
        fields(roles[name], where, ('root', 'path', 'sha256'))
        require(roles[name]['root'] in roots, where + '.root', 'unknown role root')
        rel = relative(roles[name]['path'], where + '.path')
        content = read_file(roots[roles[name]['root']], rel, where)
        role_definition(content, where)
        actual = hashlib.sha256(content).hexdigest()
        require(actual == roles[name]['sha256'], where + '.sha256', 'role definition digest mismatch')
        resolved[name] = {'root': roles[name]['root'], 'path': rel, 'sha256': actual}
    return resolved


# -- Request, deployment and configuration ---------------------------------------------------

def pr_identity(raw, path):
    parsed = urlsplit(text(raw, path, 2048))
    match = re.fullmatch(r'/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)', parsed.path)
    require(parsed.scheme == 'https' and re.fullmatch(r'[A-Za-z0-9.-]+', parsed.netloc or '')
            and not parsed.query and not parsed.fragment and match
            and match[1] not in ('.', '..') and match[2] not in ('.', '..'),
            path, 'canonical HTTPS pull request URL required')
    host, owner, name = parsed.netloc.lower(), match[1].lower(), match[2].lower()
    repository = '/'.join((host, owner, name))
    return {'url': f'https://{repository}/pull/{int(match[3])}', 'repository': repository,
            'number': int(match[3])}


def resolve_request(request):
    fields(request, 'request', ('pr', 'instructions'), ('logical_run', 'authorizations', 'upstream'))
    pr = pr_identity(request['pr'], 'request.pr')
    # The exact original instruction is preserved byte-for-byte, never merged with control text.
    instructions = text(request['instructions'], 'request.instructions')
    logical_run = request.get('logical_run', 'pr_' + digest(pr)[:32])
    require(isinstance(logical_run, str) and IDENT.fullmatch(logical_run), 'request.logical_run', 'invalid identifier')
    upstream = request.get('upstream')
    if upstream is not None:
        require(isinstance(upstream, str) and re.fullmatch(r'[a-z0-9.-]+/[a-z0-9_.-]+/[a-z0-9_.-]+', upstream),
                'request.upstream', 'must be a canonical lowercase host/owner/name')
    authorizations = request.get('authorizations', {})
    require(isinstance(authorizations, dict), 'request.authorizations', 'must be an object')
    grants = {}
    for action, value in sorted(authorizations.items()):
        where = 'request.authorizations.' + str(action)
        require(action in ACTIONS, where, 'unknown remote-write action')
        fields(value, where, ('request_id', 'instruction', 'source'))
        grants[action] = {key: text(value[key], f'{where}.{key}') for key in ('request_id', 'instruction', 'source')}
    return {'pr': pr, 'upstream': upstream, 'original_instructions': instructions,
            'logical_run': logical_run, 'authorizations': grants}


def resolve_deployment(deployment):
    fields(deployment, 'deployment', ('execution', 'agents'), ('controller',))
    controller = deployment.get('controller', {})
    fields(controller, 'deployment.controller', (), ('run-timeout',))
    if 'run-timeout' in controller:
        require(isinstance(controller['run-timeout'], str) and re.fullmatch(r'[1-9][0-9]*[smh]', controller['run-timeout']),
                'deployment.controller.run-timeout', 'must be a duration such as 2h')
    execution = fields(deployment['execution'], 'deployment.execution', ('mode', 'image'),
                       ('runtime', 'network', 'writable'))
    require(execution['mode'] == 'container', 'deployment.execution.mode', 'helpers run in a container')
    image = execution['image']
    match = re.fullmatch(r'[a-z0-9][a-z0-9._/:-]*@sha256:([0-9a-f]{64})', image if isinstance(image, str) else '')
    require(match and set(match[1]) != {'0'}, 'deployment.execution.image', 'must be pinned by a real sha256 digest')
    require(execution.get('runtime', 'docker') in ('docker', 'podman'), 'deployment.execution.runtime', 'unsupported runtime')
    require(execution.get('network', 'bridge') in ('bridge', 'none'), 'deployment.execution.network', 'unsupported network')
    require(type(execution.get('writable', True)) is bool, 'deployment.execution.writable', 'must be boolean')
    agents = deployment['agents']
    require(isinstance(agents, dict) and agents, 'deployment.agents', 'must be a nonempty object')
    providers = {}
    for name, agent in sorted(agents.items()):
        where = 'deployment.agents.' + str(name)
        require(isinstance(name, str) and NAME.fullmatch(name), where, 'invalid agent name')
        fields(agent, where, ('provider',), ('execution', 'sandbox', 'authentication'))
        providers[name] = text(agent['provider'], where + '.provider', 128)
        fields(agent.get('execution', {}), where + '.execution', (), ('mode',))
        require(agent.get('execution', {}).get('mode', 'local') in ('local', 'container'), where + '.execution.mode',
                'unsupported execution mode')
        require(agent.get('sandbox', 'workspace-write') in ('read-only', 'workspace-write'), where + '.sandbox',
                'unsupported sandbox')
        authentication = fields(agent.get('authentication', {}), where + '.authentication', (), ('mode',))
        require(authentication.get('mode', 'native') == 'native', where + '.authentication.mode',
                'only native provider authentication is supported')
    return {'digest': digest(deployment), 'image': image, 'providers': providers}


def check_configuration(configuration):
    if configuration is None:
        return {'revision': None, 'scopes': [], 'resources': [], 'digest': None}
    fields(configuration, 'configuration', ('schema_version', 'kind', 'suite', 'revision', 'scopes',
                                            'resources', 'admission'))
    require(type(configuration['schema_version']) is int and configuration['schema_version'] == 1,
            'configuration.schema_version', 'unsupported schema version')
    require(configuration['kind'] == 'configuration', 'configuration.kind', 'must be configuration')
    require(configuration['suite'] == SUITE, 'configuration.suite', 'must be ' + SUITE)
    revision = text(configuration['revision'], 'configuration.revision', 256)
    admission = fields(configuration['admission'], 'configuration.admission',
                       ('structural', 'semantic', 'request_id', 'context_digest', 'resource_digest',
                        'capability_digest', 'evidence_refs'))
    require(admission['structural'] in ('pending', 'accepted', 'rejected'), 'configuration.admission.structural',
            'invalid structural status')
    require(admission['semantic'] in (None, 'accepted', 'needs_clarification', 'rejected', 'blocked'),
            'configuration.admission.semantic', 'invalid semantic verdict')
    require(isinstance(configuration['scopes'], list), 'configuration.scopes', 'must be an array')
    return {'revision': revision, 'scopes': configuration['scopes'], 'resources': configuration['resources'],
            'digest': digest(configuration)}


def matches(selector, request, path):
    for unsupported in ('include', 'exclude', 'conditions'):
        require(not isinstance(selector, dict) or unsupported not in selector, path,
                'path selectors and applicability conditions are not supported yet')
    fields(selector, path, ('kind', 'id'))
    kind = selector['kind']
    require(kind in PRECEDENCE[:-1], path + '.kind', 'saved scopes are personal, repository, upstream or pr')
    ident = text(selector['id'], path + '.id', 512)
    return kind, {'personal': True, 'repository': ident == request['pr']['repository'],
                  'upstream': ident == request['upstream'], 'pr': ident == request['pr']['url']}[kind]


def check_option(name, value, path):
    require(name in DEFAULTS, path, 'unsupported option')
    require(value is not None, path, 'null has no declared meaning for this option')
    if name in ('tasks', 'finalizers', 'reviewers', 'accounts', 'requirements', 'prohibitions'):
        require(isinstance(value, list), path, 'must be an array')
    if name in ('accounts', 'requirements', 'prohibitions'):
        for index, item in enumerate(value):
            text(item, f'{path}[{index}]')
    elif name in ('adapters', 'ci', 'limits', 'settings'):
        require(isinstance(value, dict), path, 'must be an object')
    if name == 'limits':
        for key, item in value.items():
            require(key in LIMIT_BOUNDS, f'{path}.{key}', 'unsupported allowance')
            low, high = LIMIT_BOUNDS[key]
            require(type(item) is int and low <= item <= high, f'{path}.{key}', f'must be an integer in {low}..{high}')
    elif name == 'ci':
        fields(value, path, (), ('timeout_seconds',))
        timeout = value.get('timeout_seconds', 1800)
        require(type(timeout) is int and 1 <= timeout <= 86400, path + '.timeout_seconds', 'must be 1..86400')
    elif name == 'settings':
        # String settings could carry instructions; they need a typed declaration first.
        for key, item in value.items():
            require(type(item) in (bool, int), f'{path}.{key}', 'settings support boolean/integer values only')
    elif name == 'merge_method':
        require(value in ('merge', 'squash'), path, 'supports merge/squash only')
    elif name == 'turns':
        require(type(value) is int and 5 <= value <= 1000, path, 'must be an integer in 5..1000')
    return value


def check_scope_body(scope, path, invocation=False):
    keys = ('options', 'permissions', 'prompts', 'skills', 'preferences')
    if invocation:
        fields(scope, path, (), keys)
    else:
        fields(scope, path, ('id', 'selector', *keys))
    options = scope.get('options', {})
    require(isinstance(options, dict), path + '.options', 'must be an object')
    for key in ('before_cycle', 'after_cycle'):
        require(key not in options, f'{path}.options.{key}', 'cycle hooks are unsupported; place tasks explicitly')
    for key, value in options.items():
        check_option(key, value, f'{path}.options.{key}')
    permissions = scope.get('permissions', {})
    require(isinstance(permissions, dict), path + '.permissions', 'must be an object')
    for key, value in permissions.items():
        require(key in ACTIONS, f'{path}.permissions.{key}', 'unknown remote-write action')
        require(type(value) is bool, f'{path}.permissions.{key}', 'must be boolean')
    for key in ('prompts', 'skills', 'preferences'):
        require(isinstance(scope.get(key, []), list), f'{path}.{key}', 'must be an array')
    return scope


def effective(scopes, request, invocation):
    """Apply matching scopes in precedence order with per-value provenance."""
    applicable = []
    seen = set()
    for index, scope in enumerate(scopes):
        where = f'configuration.scopes[{index}]'
        check_scope_body(scope, where)
        require(isinstance(scope['id'], str) and IDENT.fullmatch(scope['id']), where + '.id', 'invalid identifier')
        require(scope['id'] not in seen, where + '.id', 'duplicate scope id')
        seen.add(scope['id'])
        kind, applies = matches(scope['selector'], request, where + '.selector')
        if applies:
            applicable.append((PRECEDENCE.index(kind), index, kind, scope['id'], where, scope))
    if invocation is not None:
        check_scope_body(invocation, 'invocation', invocation=True)
        applicable.append((PRECEDENCE.index('invocation'), len(scopes), 'invocation', 'invocation', 'invocation',
                           invocation))
    applicable.sort(key=lambda item: item[:2])
    options = {key: {'value': value, 'source': 'default'} for key, value in DEFAULTS.items()}
    permissions = {action: {'value': False, 'source': 'default'} for action in ACTIONS}
    accumulated = {key: [] for key in ACCUMULATED}
    records = {'prompts': [], 'skills': [], 'preferences': []}
    level = {}
    for rank, _, kind, scope_id, where, scope in applicable:
        source = f'{kind}:{scope_id}'
        for key, value in scope.get('options', {}).items():
            if key in ACCUMULATED:
                accumulated[key].extend({'text': item, 'source': source} for item in value)
                continue
            previous = level.get(('option', key))
            require(not previous or previous[0] != rank or previous[1] == value, f'{where}.options.{key}',
                    'contradicts ' + str(previous and previous[2]) + ' at the same precedence; resolve before admission')
            level[('option', key)] = (rank, value, source)
            options[key] = {'value': value, 'source': source}
        for key, value in scope.get('permissions', {}).items():
            previous = level.get(('permission', key))
            require(not previous or previous[0] != rank or previous[1] == value, f'{where}.permissions.{key}',
                    'contradicts ' + str(previous and previous[2]) + ' at the same precedence; resolve before admission')
            level[('permission', key)] = (rank, value, source)
            permissions[key] = {'value': value, 'source': source}
        for key in records:
            records[key].extend((f'{where}.{key}[{index}]', source, item)
                                for index, item in enumerate(scope.get(key, [])))
    for key in ACCUMULATED:
        options[key] = {'value': accumulated[key], 'source': 'accumulated'}
    return options, permissions, records, [f'{kind}:{scope_id}' for _, _, kind, scope_id, _, _ in applicable]


# -- Selections -------------------------------------------------------------------------------

def selection(raw, path):
    call = {'task': raw} if isinstance(raw, str) else raw
    require(isinstance(call, dict), path, 'must be a task name or selection object')
    require(len(set(call) & {'task', 'stage'}) == 1, path, 'select exactly one task or stage')
    if 'stage' in call:
        require(call['stage'] in STAGES, path + '.stage', 'unsupported managed stage')
        allowed = {'stage', 'reviewers'} if call['stage'] == 'reviews' else {'stage'}
        require(not set(call) - allowed, path, 'unsupported managed stage controls')
        return call
    name = call['task']
    require(isinstance(name, str), path + '.task', 'must be a string')
    require(':' not in name, path + '.task', 'qualified/legacy selectors do not invoke installed skills; use an adapter name')
    require(NAME.fullmatch(name), path + '.task', 'invalid adapter name')
    require(not set(call) - {'task', 'required', 'instructions', 'skill'}, path,
            'tasks support task/required/instructions/skill only')
    require(type(call.get('required', True)) is bool, path + '.required', 'must be boolean')
    if 'instructions' in call:
        text(call['instructions'], path + '.instructions')
    if 'skill' in call:
        require(isinstance(call['skill'], str) and IDENT.fullmatch(call['skill']), path + '.skill',
                'must reference a supplied skill id')
    return call


def groups(raw, path):
    require(isinstance(raw, list), path, 'selected plan must be an array')
    result = []
    for index, entry in enumerate(raw):
        where = f'{path}[{index}]'
        group = entry if isinstance(entry, list) else [entry]
        require(group and not any(isinstance(item, list) for item in group), where, 'invalid parallel group')
        result.append([selection(item, f'{where}[{n}]' if isinstance(entry, list) else where)
                       for n, item in enumerate(group)])
    return result


def check_adapter(name, adapter, path):
    require(NAME.fullmatch(name), path, 'invalid adapter name')
    fields(adapter, path, ('assignment',), ('kind', 'writes', 'definition', 'checklist', 'resources',
                                             'history', 'head_sensitive', 'cacheable'))
    text(adapter['assignment'], path + '.assignment')
    kind = adapter.get('kind', 'task')
    require(kind in KINDS, path + '.kind', 'unsupported adapter kind')
    role = adapter.get('definition', 'reviewer' if kind == 'review' else 'leaf')
    require(role != VALIDATOR_ROLE, path + '.definition', 'the configuration validator role is reserved for admission')
    require(role in ADMITTED_ROLES, path + '.definition', 'references a role outside the admitted catalog')
    for key in ('writes', 'history', 'head_sensitive', 'cacheable'):
        require(type(adapter.get(key, False)) is bool, f'{path}.{key}', 'must be boolean')
    require(not (adapter.get('writes') and kind == 'review'), path + '.writes', 'reviewers cannot write')
    checklist = adapter.get('checklist', CHECKLIST)
    require(isinstance(checklist, list) and 0 < len(checklist) <= 32, path + '.checklist', 'invalid checklist')
    for index, item in enumerate(checklist):
        fields(item, f'{path}.checklist[{index}]', ('id', 'text'))
        text(item['text'], f'{path}.checklist[{index}].text')
    require(len({item['id'] for item in checklist}) == len(checklist), path + '.checklist', 'duplicate checklist id')
    require(isinstance(adapter.get('resources', []), list), path + '.resources', 'must be an array')
    return {'kind': kind, 'writes': adapter.get('writes', False), 'definition': role,
            'assignment': adapter['assignment'], 'checklist': checklist, 'resources': adapter.get('resources', []),
            'history': adapter.get('history', kind == 'cleanup'), 'head_sensitive': adapter.get('head_sensitive', False),
            'cacheable': adapter.get('cacheable', True)}


def build_plan(options, adapters, roles, skills, resources):
    tasks, stages = [], []

    def task(call, path, phase, fallback=None):
        name = call['task']
        require(name in adapters, path + '.task', 'unavailable selected adapter: ' + name)
        adapter = adapters[name]
        require(fallback is None or adapter['kind'] == fallback, path, 'reviewer selection must use read-only review adapters')
        for index, ref in enumerate(adapter['resources']):
            resource_ref(ref, resources, f'options.adapters.{name}.resources[{index}]')
        skill = call.get('skill')
        require(skill is None or skill in skills, path + '.skill', 'references an undeclared supplied skill')
        entry = {'id': f'task_{len(tasks) + 1}', 'adapter': name, 'phase': phase, 'kind': adapter['kind'],
                 'writes': adapter['writes'], 'definition': adapter['definition'],
                 'definition_sha256': roles[adapter['definition']]['sha256'],
                 'assignment_sha256': hashlib.sha256(adapter['assignment'].encode()).hexdigest(),
                 'instructions': call.get('instructions'), 'required': call.get('required', True),
                 'skill': skill, 'resources': adapter['resources']}
        tasks.append(entry)
        require(len(tasks) <= MAX_TASKS, path, 'plan exceeds bounded task count')
        return entry

    for phase in ('tasks', 'finalizers'):
        for index, group in enumerate(groups(options[phase]['value'], 'options.' + phase)):
            where = f'options.{phase}[{index}]'
            if len(group) > 1:
                require(not any('stage' in call for call in group), where, 'managed stages cannot run in a parallel group')
            stage = group[0].get('stage')
            if stage in ('publication', 'ci'):
                stages.append({'kind': stage, 'phase': phase})
            elif stage == 'reviews':
                reviewers = group[0].get('reviewers', options['reviewers']['value'])
                require(reviewers, where, 'selected reviewer stage requires explicit reviewers')
                review_groups = [[task(call, f'{where}.reviewers', phase, 'review')['id'] for call in reviewer_group]
                                 for reviewer_group in groups(reviewers, where + '.reviewers')]
                stages.append({'kind': 'reviews', 'phase': phase, 'groups': review_groups})
            else:
                entries = [task(call, where, phase) for call in group]
                require(len(entries) == 1 or not any(entry['writes'] for entry in entries), where,
                        'parallel writing tasks share a Git index and are unsupported')
                stages.append({'kind': 'tasks', 'phase': phase, 'groups': [[entry['id'] for entry in entries]]})
    for number, stage in enumerate(stages, 1):
        stage['id'] = f'stage_{number}'
    return tasks, stages


# -- Admission --------------------------------------------------------------------------------

def resolve_prompts(records, adapters, resources, roots, selected, path_kind):
    result = []
    seen = {}
    for where, source, record in records:
        if path_kind == 'preferences':
            fields(record, where, ('id', 'text'), ('resources',))
            slot, target = 'preference', None
        else:
            fields(record, where, ('id', 'slot'), ('text', 'resource', 'target', 'resources'))
            require(record['slot'] in PROMPT_SLOTS, where + '.slot', 'unsupported prompt slot')
            require(('text' in record) != ('resource' in record), where, 'requires exactly one of text or resource')
            slot, target = record['slot'], record.get('target')
            if target is not None:
                require(target != VALIDATOR_ROLE, where + '.target', 'prompts cannot target the configuration validator')
                require(target in adapters, where + '.target', 'targets an unknown adapter')
        require(isinstance(record['id'], str) and IDENT.fullmatch(record['id']), where + '.id', 'invalid identifier')
        refs = record.get('resources', [])
        require(isinstance(refs, list), where + '.resources', 'must be an array')
        refs = [resource_ref(ref, resources, f'{where}.resources[{n}]') for n, ref in enumerate(refs)]
        if 'resource' in record:
            key = resource_ref(record['resource'], resources, where + '.resource')
            item = resources[key]
            content = read_file(roots[item['root']], item['path'], where + '.resource', MAX_TEXT)
            try:
                body = content.decode('utf-8')
            except UnicodeDecodeError:
                raise AdmissionError(where + '.resource', 'prompt resource is not UTF-8') from None
            refs.extend(item['root'] + ':' + link for link in links(content, item['path'], where + '.resource'))
            refs.append(key)
        else:
            body = text(record['text'], where + '.text')
            require(not [link for link in LINK.findall(body) if '://' not in link and not link.startswith('#')],
                    where + '.text', 'inline prompt text cannot link local files; declare a resource instead')
        for ref in refs:
            resource_ref(ref, resources, where + '.resources')
        entry = {'id': record['id'], 'slot': slot, 'target': target, 'source': source, 'path': where,
                 'text': body, 'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
                 'resources': sorted(set(refs)), 'applicable': target is None or target in selected}
        previous = seen.get(record['id'])
        require(previous is None, where + '.id', f'duplicate {path_kind} id also declared at {previous}')
        seen[record['id']] = where
        result.append(entry)
    return result


def admit(*, request, configuration, invocation, deployment, roles, roots, transport):
    """Resolve and structurally admit one proposed run; returns an admission result.

    roots maps resource root names to directories; 'repository' is the target
    checkout used for guidance. transport must provide capabilities(); no other
    transport operation is ever called here.
    """
    try:
        return _admit(request, configuration, invocation, deployment, roles, roots, transport)
    except AdmissionError as error:
        failure = {'path': error.path, 'message': error.message}
    except (TypeError, KeyError, AttributeError) as error:
        # Wrong JSON types fail closed as structural rejections.
        failure = {'path': '$', 'message': 'malformed input: ' + type(error).__name__}
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None, 'error': failure}


def _admit(request, configuration, invocation, deployment, roles, roots, transport):
    require(isinstance(roots, dict) and 'repository' in roots, 'roots', 'repository root required')
    for name, path in roots.items():
        require(isinstance(name, str) and Path(path).is_dir(), 'roots.' + str(name), 'must be an existing directory')
    roots = {name: Path(path).resolve() for name, path in roots.items()}
    original = resolve_request(request)
    config = check_configuration(configuration)
    runtime = resolve_deployment(deployment)
    catalog = resolve_roles(roles, roots, 'roles')
    resources = resolve_resources(config['resources'], roots, 'configuration.resources')
    options, permissions, records, scopes = effective(config['scopes'], original, invocation)

    adapters = {name: check_adapter(name, value, 'baseline.' + name) for name, value in BASELINE_ADAPTERS.items()}
    custom_adapters = []
    for name, value in options['adapters']['value'].items():
        checked = check_adapter(name, value, 'options.adapters.' + str(name))
        if adapters.get(name) != checked:
            custom_adapters.append(name)
        adapters[name] = checked

    limits = {**DEFAULTS['limits'], **options['limits']['value']}
    skills = {}
    for where, source, record in records['skills']:
        skill = resolve_skill(record, roots, where)
        require(skill['id'] not in skills, where + '.id', 'duplicate supplied skill id')
        skills[skill['id']] = {**skill, 'source': source, 'path_ref': where}
    tasks, stages = build_plan(options, adapters, catalog, skills, resources)
    selected = {entry['adapter'] for entry in tasks}
    prompts = resolve_prompts(records['prompts'], adapters, resources, roots, selected, 'prompts')
    preferences = resolve_prompts(records['preferences'], adapters, resources, roots, selected, 'preferences')
    guidance = follow(roots['repository'], [name for name in GUIDANCE if os.path.lexists(roots['repository'] / name)],
                      'guidance')

    prohibited = {item['text'] for item in options['prohibitions']['value'] if item['text'] in ACTIONS}
    for action in original['authorizations']:
        require(action not in prohibited, 'request.authorizations.' + action,
                'authorization contradicts a configured prohibition; resolve before admission')
    effective_permissions = {
        action: {'enabled': permissions[action]['value'], 'source': permissions[action]['source'],
                 'prohibited': action in prohibited, 'authorized': action in original['authorizations'],
                 'effective': (permissions[action]['value'] and action in original['authorizations']
                               and action not in prohibited)}
        for action in ACTIONS}

    custom = {
        'prompts': [entry['id'] for entry in prompts + preferences if entry['applicable']],
        'instructions': [entry['id'] for entry in tasks if entry['instructions'] is not None],
        'adapters': sorted(name for name in custom_adapters if name in selected),
        'skills': sorted({entry['skill'] for entry in tasks if entry['skill'] is not None}),
    }
    required = set(BASE_CAPABILITIES)
    if custom['skills']:
        required.add(SKILL_CAPABILITY)
    if effective_permissions['merge']['effective']:
        required.add(MERGE_CAPABILITY)

    # Every local structural check has passed; only now consult the engine (read-only).
    capabilities = transport.capabilities()
    fields(capabilities, 'capabilities', ('engine', 'providers'))
    fields(capabilities['engine'], 'capabilities.engine', ('version',))
    require(isinstance(capabilities['providers'], dict), 'capabilities.providers', 'must be an object')
    bindings = {}
    for agent, provider in runtime['providers'].items():
        offered = capabilities['providers'].get(provider)
        require(isinstance(offered, list) and all(isinstance(item, str) for item in offered),
                f'deployment.agents.{agent}.provider', 'provider is not available from the engine')
        missing = sorted(required - set(offered))
        require(not missing, f'deployment.agents.{agent}.provider', 'unsupported capability: ' + ', '.join(missing))
        bindings[agent] = {'provider': provider, 'capabilities': sorted(set(offered))}
    engine = text(capabilities['engine']['version'], 'capabilities.engine.version', 128)

    request_record = {'original_instructions': original['original_instructions'], 'logical_run': original['logical_run'],
                      'pr': original['pr'], 'upstream': original['upstream'],
                      'authorizations': original['authorizations']}
    capability_record = {'engine': engine, 'required': sorted(required), 'bindings': bindings}
    resource_record = {'configured': resources, 'guidance': guidance, 'roles': catalog,
                       'skills': {key: {k: v for k, v in skill.items() if k != 'path_ref'} for key, skill in skills.items()}}
    snapshot = {
        'schema_version': 1, 'kind': 'admission_snapshot', 'suite': SUITE,
        'request': request_record,
        'sources': {'configuration_revision': config['revision'], 'configuration_digest': config['digest'],
                    'invocation_digest': digest(invocation) if invocation is not None else None,
                    'deployment_digest': runtime['digest'], 'scopes': scopes},
        'options': {key: value for key, value in options.items() if key != 'adapters'},
        'limits': limits,
        'adapters': {name: {**adapter, 'custom': name in custom_adapters} for name, adapter in sorted(adapters.items())},
        'plan': {'tasks': tasks, 'stages': stages},
        'prompts': prompts, 'preferences': preferences,
        'permissions': effective_permissions,
        'resources': resource_record,
        'capabilities': capability_record,
        'custom': custom,
    }
    request_id = 'req_' + digest({'request': request_record, 'revision': config['revision'],
                                  'configuration': config['digest'], 'invocation': snapshot['sources']['invocation_digest']})[:32]
    context_digest = digest({key: snapshot[key] for key in ('request', 'sources', 'options', 'limits', 'adapters',
                                                            'plan', 'prompts', 'preferences', 'permissions')})
    resource_digest = digest(resource_record)
    capability_digest = digest(capability_record)
    semantic_required = any(custom.values())
    admission_input = {
        'request_id': request_id,
        'request': {'original_instructions': original['original_instructions'], 'authorizations': original['authorizations']},
        'requirements': options['requirements']['value'],
        'prohibitions': options['prohibitions']['value'],
        'permissions': effective_permissions,
        'capabilities': capability_record,
        'custom': {
            'prompts': [{key: entry[key] for key in ('id', 'slot', 'target', 'source', 'path', 'text', 'resources')}
                        for entry in prompts + preferences if entry['applicable']],
            'instructions': [{'task': entry['id'], 'adapter': entry['adapter'], 'text': entry['instructions']}
                             for entry in tasks if entry['instructions'] is not None],
            'adapters': {name: adapters[name] for name in custom['adapters']},
            'skills': {key: {k: skills[key][k] for k in ('name', 'root', 'path', 'entry', 'sha256', 'files', 'source')}
                       for key in custom['skills']},
        },
        'sources': {'configuration_revision': config['revision'], 'scopes': scopes,
                    'context_digest': context_digest, 'resource_digest': resource_digest,
                    'capability_digest': capability_digest},
        'validator': bootstrap() if semantic_required else None,
    }
    require(len(canonical(admission_input)) <= MAX_ADMISSION_INPUT, 'admission_input', 'exceeds byte bound')
    snapshot['admission_input_digest'] = digest(admission_input)
    result = {
        'structural': 'accepted',
        'semantic': 'blocked' if semantic_required else None,
        'admission': {'structural': 'accepted', 'semantic': 'blocked' if semantic_required else None,
                      'request_id': request_id, 'context_digest': context_digest,
                      'resource_digest': resource_digest, 'capability_digest': capability_digest,
                      'evidence_refs': ['snapshot:' + digest(snapshot)]},
        'snapshot': snapshot, 'snapshot_digest': digest(snapshot), 'admission_input': admission_input,
    }
    if semantic_required:
        result['preflight'] = 'rejected'
        result['error'] = {'path': 'custom', 'message': 'custom prompts and supplied skills need semantic '
                           'configuration assessment, which is not available yet'}
    else:
        result['preflight'] = 'admitted'
    return result
