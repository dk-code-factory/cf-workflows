"""Structural configuration admission and complete input resolution (contract v1).

Admission reads and hashes declared inputs. It never loads or executes a
user-supplied skill, starts a provider session, runs a target task or performs a
remote write. The only external operation is one read-only capability query on
the supplied engine transport, made after every local structural check passes.
A proposal with custom inputs is structurally accepted but never admitted here:
its preflight is `semantic_required` until cf_workflows.semantic assesses it.
Supplied skills remain rejected at preflight until the engine loads them as
session-scoped plugins (Agent Workflow #139); cf_workflows.skills holds the
per-occurrence bindings and evidence checks that execution will consume.

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
from urllib.parse import unquote, urlsplit

SUITE = 'cf-workflows'
MAX_FILE = 4 << 20
MAX_TEXT = 64 << 10
MAX_CLOSURE = 256
MAX_SKILL_FILES = 512
MAX_SKILL_CHILDREN = 8
MAX_TASKS = 40
MAX_ADMISSION_INPUT = 1 << 20
SHA256 = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'[a-z][a-z0-9-]{0,63}')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
INLINE_LINK = re.compile(r'\]\(\s*(<[^>\n]*>|(?:[^()\s]|\([^()\s]*\))+)(?:\s+(?:"[^"]*"|\'[^\']*\'|\([^)]*\)))?\s*\)')
REFERENCE_DEFINITION = re.compile(r'^ {0,3}\[(?!\^)[^\]]+\]:\s*(<[^>\n]*>|\S+)')  # Not footnotes.
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
CODE_SPAN = re.compile(r'(`+)(?:(?!\1).)+?\1')
REPOSITORY = re.compile(r'[a-z0-9.-]+/[a-z0-9_.-]+/[a-z0-9_.-]+')
HOST = re.compile(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?')

ACTIONS = ('push', 'edit_pr', 'reply', 'resolve_thread', 'request_review', 'create_pr',
           'create_issue', 'edit_issue', 'post_findings', 'approve_fork_ci', 'rerun_ci',
           'approve_pr', 'merge')
PRECEDENCE = ('personal', 'repository', 'upstream', 'pr', 'invocation')
ADMITTED_ROLES = ('leaf', 'reviewer', 'verifier', 'fixer', 'audience')
GUIDANCE = ('AGENTS.md', 'CLAUDE.md', 'CONTRIBUTING.md', '.github/copilot-instructions.md')
STAGES = ('publication', 'responses', 'reviews', 'ci')
KINDS = ('review', 'inspection', 'decisions', 'fix', 'scope', 'description', 'cleanup',
         'synchronization', 'reply', 'task', 'configuration')
# Configuration adapters propose saved changes for cf-workflows:dk-config and dk-preferences only
# (cf_workflows.entries); a configuration helper, never the session, writes the store.
CONFIGURATION_KIND = 'configuration'
# Application modes of a preference: tasks that write implement; review adapters review.
PREFERENCE_MODES = ('implementation', 'review')
SELECTOR_TYPES = ('file', 'directory', 'glob')
# The response stage's read-only adapter; it runs only inside that stage (docs/responses.md).
RESPONDER = 'reply-feedback'
# The simplification reviewer; it never substitutes for ordinary review coverage (docs/review.md).
SIMPLIFIER = 'simplify'
PROMPT_SLOTS = ('task', 'reviewer', 'finalizer', 'assessment', 'selection', 'reconciliation',
                'reply', 'cleanup', 'commit', 'behavior_replacement', 'behavior_addition',
                'completion', 'additional')
LIMIT_BOUNDS = {'review_cycles': (1, 10), 'retries_per_task': (0, 1),
                'ci_repairs_per_cycle': (0, 5), 'ci_reruns_per_head': (0, 3)}
BASE_CAPABILITIES = ('native_session', 'admitted_roles')
SKILL_CAPABILITY = 'session_scoped_skills'
# Engine-observed evidence of the skill invocation and terminal outcome, and of child agents with their parents.
SKILL_EVIDENCE_CAPABILITY = 'observed_skill_evidence'
CHILD_CAPABILITY = 'observed_child_agents'
AGENT_TYPE = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,63}')
MERGE_CAPABILITY = 'merge_guard'
INPUT_CAPABILITY = 'durable_input'  # Engine-level: request-bound clarification gates.
SUPPLIED_SKILL_EXECUTION = False  # Needs session-scoped plugin loading from Agent Workflow #139.

VALIDATOR_ROLE = 'configuration-validator'
BOOTSTRAP_DEFINITION = Path(__file__).resolve().parent / 'bootstrap' / 'configuration-validator.toml'
BOOTSTRAP_SHA256 = 'fcf09ceb74968d05c3f07ea65e94339a2574d8b82aec14a4644a8049f18fd467'

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
    'reply-feedback': {'kind': 'reply', 'writes': False, 'definition': 'leaf', 'assignment':
        'Assess every feedback source against the published changes: quote each request, its addressee, implementation '
        'disposition and response disposition. Draft one exact reply for each source that needs an answer. Do not publish.'},
    'review': {'kind': 'review', 'writes': False, 'definition': 'reviewer', 'assignment':
        'Review the actual changes against the originating issue, documented repository standards and accepted user scope. '
        'Return supported actionable findings or an evidenced no-findings report.', 'checklist': [
            {'id': 'assignment', 'text': 'Inspect the actual diff and originating requirements.'},
            {'id': 'evidence', 'text': 'Verify source evidence and every concrete finding.'},
            {'id': 'scope', 'text': 'Account for repository rules, changed tests and unresolved checks.'}]},
    'simplify': {'kind': 'review', 'writes': False, 'definition': 'reviewer', 'assignment':
        'Review only the PR changes for bounded, behavior-preserving simplifications with a concrete maintainability '
        'benefit. Stay within changed hunks and agreed scope; report broader opportunities as deferred observations. '
        'Each finding states the complexity reduced, its scope and the verification it needs. Do not edit or publish.',
        'checklist': [
            {'id': 'assignment', 'text': 'Inspect the actual changed hunks and agreed scope.'},
            {'id': 'evidence', 'text': 'Support every recommendation with source evidence and a concrete benefit.'},
            {'id': 'scope', 'text': 'Keep behavior and public contracts; defer out-of-scope observations.'}]},
}
# Bundled adapter procedures of the configuration entries (docs/configuration-skills.md). Like the
# baseline adapters they are control text, never custom input, so their default plans need no
# validator session.
ENTRY_ADAPTERS = {
    'onboard-repository': {'kind': 'inspection', 'writes': False, 'definition': 'leaf', 'assignment':
        'Read every discovered instruction source and the saved configuration listed in the assignment. Report each '
        'actionable rule with its source line, scope and authority, repository facts, existing settings with '
        'provenance, conflicts and gaps. Read only: save nothing, run no discovered command, publish nothing.',
        'checklist': [
            {'id': 'assignment', 'text': 'Inspect every listed source or mark it explicitly uninspected.'},
            {'id': 'evidence', 'text': 'Cite the original path and line of every rule and fact.'},
            {'id': 'scope', 'text': 'Keep unselected sources as evidence only; grant no authority.'}]},
    'configure': {'kind': CONFIGURATION_KIND, 'writes': False, 'definition': 'leaf', 'assignment':
        'Explain or change saved cf-workflows configuration exactly as asked. Return an explanation citing '
        'configuration paths, questions for real contradictions or missing intent, or one exact typed operation '
        'list. Do not write the store, grant authority beyond the ask or run pull-request work.',
        'checklist': [
            {'id': 'assignment', 'text': 'Answer or change only what the exact ask requests.'},
            {'id': 'evidence', 'text': 'Cite the effective values and their sources that you relied on.'},
            {'id': 'scope', 'text': 'Ask instead of inventing intent; preserve unrelated settings.'}]},
    'admit-preferences': {'kind': CONFIGURATION_KIND, 'writes': False, 'definition': 'leaf', 'assignment':
        'Turn the exact ask into independently applicable preference records with concrete selectors, conditions '
        'and application modes. Compare overlapping records and governing instructions; ask instead of inventing '
        'intent or precedence. Return one exact typed change set; do not write the store.',
        'checklist': [
            {'id': 'assignment', 'text': 'Keep the strength, conditions and exceptions of the ask.'},
            {'id': 'evidence', 'text': 'Account for every overlapping record and governing instruction.'},
            {'id': 'scope', 'text': 'Never pick a winner between contradictory instructions.'}]},
}
BUNDLED_ADAPTERS = {**BASELINE_ADAPTERS, **ENTRY_ADAPTERS}
DEFAULTS = {
    'tasks': ['read-feedback', 'reconcile-decisions', 'address-feedback', {'stage': 'publication'},
              {'stage': 'responses'}, 'assess-scope', 'describe-pr', {'stage': 'reviews'}, {'stage': 'publication'}],
    'finalizers': ['inspect-commits', 'sync-guidance', {'stage': 'responses'}, {'stage': 'ci'}],
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


class EngineUnavailable(Exception):
    """The engine capability query failed; this is not a structural verdict."""


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


def link_targets(body):
    """Inline and reference-definition Markdown link targets outside code fences and spans."""
    result, fence = [], None
    for line in body.splitlines():
        opening = FENCE.match(line)
        if fence:
            if opening and opening[1][0] == fence[0] and len(opening[1]) >= len(fence):
                fence = None
            continue
        if opening:
            fence = opening[1]
            continue
        # Indented lines are scanned too: nested list items and continuations hold real
        # links, and an extra link in indented code can only reject, never drop a resource.
        line = CODE_SPAN.sub('', line)
        found = INLINE_LINK.findall(line)
        definition = REFERENCE_DEFINITION.match(line)
        if definition:
            found.append(definition[1])
        for link in found:
            link = link[1:-1].strip() if link.startswith('<') else link
            if link and not re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', link) and not link.startswith('#'):
                result.append(unquote(link.split('#')[0].split('?')[0]))
    return [link for link in result if link]


def links(content, rel, path):
    """Local Markdown link targets of a file, normalized below its root; escapes reject.

    A leading / is relative to the root, as repository renderers resolve it.
    """
    try:
        body = content.decode('utf-8')
    except UnicodeDecodeError:
        raise AdmissionError(path, 'Markdown resource is not UTF-8: ' + rel) from None
    result = []
    for target in link_targets(body):
        base = [] if target.startswith('/') else list(PurePosixPath(rel).parent.parts)
        parts = []
        for part in [*base, *target.split('/')]:
            if part in ('', '.'):
                continue
            if part == '..':
                require(parts, path, f'{rel} links outside its root: {target}')
                parts.pop()
            else:
                parts.append(part)
        require(parts, path, f'{rel} has an empty link: {target}')
        result.append('/'.join(parts))
    return result


def inside(root, rel, path):
    """Resolve in-root symlinks for guidance; a target outside root rejects."""
    real = Path(os.path.realpath(root / rel))
    try:
        resolved = real.relative_to(root).as_posix()
    except ValueError:
        raise AdmissionError(path, 'symlink escapes its root: ' + rel) from None
    require(resolved != '.', path, 'link resolves to the root itself: ' + rel)
    return resolved


def follow(root, start, path, allowed=None, aliases=None):
    """Transitive local Markdown closure from start files; returns {path: sha256}.

    With aliases (repository guidance), in-root symlinks are allowed and recorded
    as {link: resolved}; elsewhere every symlink rejects.
    """
    found, pending = {}, list(start)
    while pending:
        rel = pending.pop()
        if rel in found or (aliases and rel in aliases):
            continue
        require(allowed is None or rel in allowed, path, 'incomplete resource closure: ' + rel)
        require(len(found) + len(aliases or ()) < MAX_CLOSURE, path, 'resource closure exceeds bound')
        if aliases is not None:
            require(os.path.lexists(root / rel), path, 'missing resource ' + rel)
            resolved = inside(root, rel, path)
            if resolved != rel:
                aliases[rel] = resolved
                pending.append(resolved)
                continue
            if stat.S_ISDIR(lookup(root, rel, path)[1]):
                continue  # Guidance may link a directory; only files are instruction resources.
        content = read_file(root, rel, path)
        found[rel] = hashlib.sha256(content).hexdigest()
        if rel.endswith('.md'):
            pending.extend(links(content, rel, path))
    return dict(sorted(found.items()))


def resolve_resources(records, roots, path):
    """Verify declared resources; returns their records and the exact bytes that were hashed."""
    require(isinstance(records, list), path, 'must be an array')
    resolved, contents = {}, {}
    for index, record in enumerate(records):
        where = f'{path}[{index}]'
        fields(record, where, ('root', 'path', 'sha256', 'purpose', 'owner'))
        require(record['root'] in roots, where + '.root', 'unknown resource root')
        rel = relative(record['path'], where + '.path')
        require(isinstance(record['sha256'], str) and SHA256.fullmatch(record['sha256']),
                where + '.sha256', 'must be lowercase SHA-256')
        key = record['root'] + ':' + rel
        require(key not in resolved, where, 'duplicate resource ' + key)
        content = read_file(roots[record['root']], rel, where)
        actual = hashlib.sha256(content).hexdigest()
        require(actual == record['sha256'], where + '.sha256', 'resource digest mismatch: ' + key)
        resolved[key] = {'root': record['root'], 'path': rel, 'sha256': actual,
                         'purpose': text(record['purpose'], where + '.purpose', 512),
                         'owner': text(record['owner'], where + '.owner', 512)}
        contents[key] = content
    # Every declared Markdown resource must declare its own local link closure.
    for key, item in resolved.items():
        targets = links(contents[key], item['path'], path) if item['path'].endswith('.md') else []
        for target in targets:
            require(item['root'] + ':' + target in resolved, path,
                    f'incomplete resource closure: {key} references undeclared {target}')
        # Recorded so acceptance can bind each custom input to its exact transitive closure.
        item['links'] = sorted({item['root'] + ':' + target for target in targets})
    return dict(sorted(resolved.items())), contents


def resource_ref(value, resources, path):
    require(isinstance(value, str) and value in resources, path, 'references an undeclared resource')
    return value


def resolve_skill(record, roots, path):
    """Hash a user-supplied skill's declared files; never parse it as instructions or run it."""
    for field in ('host', 'installed_version', 'qualified_name'):
        require(not isinstance(record, dict) or field not in record, path,
                'host-installed skills are never discovered or supported; supply files pinned by digest')
    fields(record, path, ('id', 'name', 'root', 'path', 'sha256', 'files'), ('entry', 'children'))
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
            'entry': entry, 'sha256': tree, 'files': files,
            'children': skill_children(record.get('children', []), path + '.children')}


def skill_children(declared, path):
    """Child agents a supplied skill must run: exact counts of explicitly permitted agent types.

    The default (no declaration) permits no child agents at all.
    """
    require(isinstance(declared, list) and len(declared) <= MAX_SKILL_CHILDREN, path,
            f'must be an array of at most {MAX_SKILL_CHILDREN} child declarations')
    result = {}
    for index, item in enumerate(declared):
        where = f'{path}[{index}]'
        fields(item, where, ('agent_type', 'count'))
        kind = item['agent_type']
        require(isinstance(kind, str) and AGENT_TYPE.fullmatch(kind), where + '.agent_type', 'invalid agent type')
        require(kind not in result, where + '.agent_type', 'duplicate agent type')
        require(type(item['count']) is int and 1 <= item['count'] <= MAX_SKILL_CHILDREN, where + '.count',
                f'must be an integer in 1..{MAX_SKILL_CHILDREN}')
        result[kind] = item['count']
    return [{'agent_type': kind, 'count': count} for kind, count in sorted(result.items())]


def skill_digest(files):
    """Content digest of a supplied skill: canonical JSON of {path: sha256}."""
    return digest(dict(sorted(files.items())))


# -- Roles and bootstrap ----------------------------------------------------------------------

AGENT_LINE = re.compile(r'([a-z][a-z-]*): ("(?:[^"\\\x00-\x1f]|\\.)*")')
AGENT_FIELDS = ('name', 'description', 'instructions', 'model', 'reasoning-effort')


def role_definition(content, path):
    """Admit a role in the engine's canonical YAML agent form: one `key: "JSON string"` line per field.

    Fields follow the shared engine contract (required name, description and
    instructions; optional model and reasoning-effort). Only the exact
    serialization the engine's converter emits is accepted, so the standard
    library reads it without a general YAML parser.
    """
    try:
        body = content.decode('utf-8')
    except UnicodeDecodeError:
        raise AdmissionError(path, 'role definition is not valid UTF-8') from None
    require(body.endswith('\n') and '\r' not in body, path, 'unsupported role definition')
    value = {}
    for line in body[:-1].split('\n'):
        match = AGENT_LINE.fullmatch(line)
        require(match and match[1] in AGENT_FIELDS and match[1] not in value, path, 'unsupported role definition')
        try:
            field = json.loads(match[2])
        except ValueError:
            raise AdmissionError(path, 'unsupported role definition') from None
        require(json.dumps(field, ensure_ascii=False) == match[2], path, 'unsupported role definition')
        value[match[1]] = field
    require(all(isinstance(value.get(key), str) and value[key].strip() for key in ('name', 'description', 'instructions')),
            path, 'unsupported role definition')


def bootstrap_definition(content, path):
    try:
        value = tomllib.loads(content.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise AdmissionError(path, 'bootstrap definition is not valid UTF-8 TOML') from None
    allowed = {'name', 'description', 'developer_instructions', 'model', 'model_reasoning_effort'}
    require(not set(value) - allowed and all(isinstance(value.get(key), str) and value[key].strip()
                                             for key in ('name', 'description', 'developer_instructions')),
            path, 'unsupported bootstrap definition')


def bootstrap():
    """Admit the bundled configuration-validator definition structurally, never semantically.

    This is the bootstrap boundary: its digest is pinned in code, it accepts no
    configuration, prompts or skills, and nothing assesses it, so validation of
    any proposal terminates after one level.
    """
    content = read_file(BOOTSTRAP_DEFINITION.parent, BOOTSTRAP_DEFINITION.name, 'bootstrap')
    actual = hashlib.sha256(content).hexdigest()
    require(actual == BOOTSTRAP_SHA256, 'bootstrap', 'bundled validator definition differs from its pin')
    bootstrap_definition(content, 'bootstrap')
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
    """A request targets a pull request or, with no invented PR, a whole repository."""
    fields(request, 'request', ('instructions',), ('pr', 'repository', 'logical_run', 'authorizations', 'upstream'))
    require(('pr' in request) != ('repository' in request), 'request', 'target exactly one of pr or repository')
    if 'pr' in request:
        pr = pr_identity(request['pr'], 'request.pr')
        repository = pr['repository']
    else:
        pr, repository = None, request['repository']
        require(isinstance(repository, str) and REPOSITORY.fullmatch(repository), 'request.repository',
                'must be a canonical lowercase host/owner/name')
    # The exact original instruction is preserved byte-for-byte, never merged with control text.
    instructions = text(request['instructions'], 'request.instructions')
    logical_run = request.get('logical_run', 'pr_' + digest(pr)[:32] if pr else 'repo_' + digest(repository)[:32])
    require(isinstance(logical_run, str) and IDENT.fullmatch(logical_run), 'request.logical_run', 'invalid identifier')
    upstream = request.get('upstream')
    if upstream is not None:
        require(isinstance(upstream, str) and REPOSITORY.fullmatch(upstream),
                'request.upstream', 'must be a canonical lowercase host/owner/name')
    authorizations = request.get('authorizations', {})
    require(isinstance(authorizations, dict), 'request.authorizations', 'must be an object')
    grants = {}
    for action, value in sorted(authorizations.items()):
        where = 'request.authorizations.' + str(action)
        require(action in ACTIONS, where, 'unknown remote-write action')
        fields(value, where, ('request_id', 'instruction', 'source'))
        grants[action] = {key: text(value[key], f'{where}.{key}') for key in ('request_id', 'instruction', 'source')}
    result = {'pr': pr, 'upstream': upstream, 'original_instructions': instructions,
              'logical_run': logical_run, 'authorizations': grants}
    if pr is None:
        result['repository'] = repository
    return result


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
    for key in ('request_id', 'context_digest', 'resource_digest', 'capability_digest'):
        value = admission[key]
        if value is not None:
            text(value, 'configuration.admission.' + key, 256)
            require(key == 'request_id' or SHA256.fullmatch(value), 'configuration.admission.' + key,
                    'must be lowercase SHA-256 or null')
    require(isinstance(admission['evidence_refs'], list), 'configuration.admission.evidence_refs', 'must be an array')
    for index, value in enumerate(admission['evidence_refs']):
        text(value, f'configuration.admission.evidence_refs[{index}]', 1024)
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
    canonical_id = {'personal': lambda: IDENT.fullmatch(ident) and ident,
                    'repository': lambda: REPOSITORY.fullmatch(ident) and ident,
                    'upstream': lambda: REPOSITORY.fullmatch(ident) and ident,
                    'pr': lambda: pr_identity(ident, path + '.id')['url']}[kind]()
    require(canonical_id == ident, path + '.id',
            'must be a canonical identity (lowercase host/owner/name or canonical PR URL)')
    pr = request['pr']
    return kind, {'personal': True, 'repository': ident == (pr['repository'] if pr else request['repository']),
                  'upstream': ident == request['upstream'], 'pr': pr is not None and ident == pr['url']}[kind]


def check_option(name, value, path, resources):
    require(name in DEFAULTS, path, 'unsupported option')
    require(value is not None, path, 'null has no declared meaning for this option')
    if name in ('tasks', 'finalizers', 'reviewers', 'accounts', 'requirements', 'prohibitions'):
        require(isinstance(value, list), path, 'must be an array')
    if name == 'accounts':
        # Personal accounts are immutable numeric identities scoped to their host; logins can be renamed.
        seen = set()
        for index, item in enumerate(value):
            where = f'{path}[{index}]'
            fields(item, where, ('host', 'id'))
            require(isinstance(item['host'], str) and HOST.fullmatch(item['host']), where + '.host',
                    'must be a canonical lowercase host')
            require(type(item['id']) is int and item['id'] > 0, where + '.id',
                    'must be a positive immutable numeric account ID, not a login')
            require((item['host'], item['id']) not in seen, where, 'duplicate account')
            seen.add((item['host'], item['id']))
    elif name in ('requirements', 'prohibitions'):
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
    elif name in ('tasks', 'finalizers', 'reviewers'):
        for index, group in enumerate(groups(value, path)):
            for call in group:
                if call.get('stage') == 'reviews' and 'reviewers' in call:
                    groups(call['reviewers'], f'{path}[{index}].reviewers')
    elif name == 'adapters':
        for key, adapter in value.items():
            checked = check_adapter(key, adapter, f'{path}.{key}')
            for index, ref in enumerate(checked['resources']):
                resource_ref(ref, resources, f'{path}.{key}.resources[{index}]')
    return value


def check_scope_body(scope, path, resources, invocation=False):
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
        check_option(key, value, f'{path}.options.{key}', resources)
    permissions = scope.get('permissions', {})
    require(isinstance(permissions, dict), path + '.permissions', 'must be an object')
    for key, value in permissions.items():
        require(key in ACTIONS, f'{path}.permissions.{key}', 'unknown remote-write action')
        require(type(value) is bool, f'{path}.permissions.{key}', 'must be boolean')
    for key in ('prompts', 'skills', 'preferences'):
        require(isinstance(scope.get(key, []), list), f'{path}.{key}', 'must be an array')
    return scope


def effective(scopes, request, invocation, resources, defaults=None):
    """Apply matching scopes in precedence order with per-value provenance.

    Every scope is validated in full, whether or not it matches or wins a value.
    Records of non-matching scopes are returned separately for structural checks.
    """
    applicable, inactive = [], []
    locations = {'default': 'default'}
    seen = set()
    for index, scope in enumerate(scopes):
        where = f'configuration.scopes[{index}]'
        check_scope_body(scope, where, resources)
        require(isinstance(scope['id'], str) and IDENT.fullmatch(scope['id']), where + '.id', 'invalid identifier')
        require(scope['id'] not in seen, where + '.id', 'duplicate scope id')
        seen.add(scope['id'])
        kind, applies = matches(scope['selector'], request, where + '.selector')
        if applies:
            applicable.append((PRECEDENCE.index(kind), index, kind, scope['id'], where, scope))
        else:
            inactive.append((f'{kind}:{scope["id"]}', where, scope))
    if invocation is not None:
        check_scope_body(invocation, 'invocation', resources, invocation=True)
        applicable.append((PRECEDENCE.index('invocation'), len(scopes), 'invocation', 'invocation', 'invocation',
                           invocation))
    applicable.sort(key=lambda item: item[:2])
    options = {key: {'value': value, 'source': 'default'} for key, value in {**DEFAULTS, **(defaults or {})}.items()}
    permissions = {action: {'value': False, 'source': 'default'} for action in ACTIONS}
    accumulated = {key: [] for key in ACCUMULATED}
    records = {'prompts': [], 'skills': [], 'preferences': []}
    level = {}
    for rank, _, kind, scope_id, where, scope in applicable:
        source = f'{kind}:{scope_id}'
        locations[source] = where
        for key, value in scope.get('options', {}).items():
            if key in ACCUMULATED:
                accumulated[key].extend({'text': item, 'source': source, 'path': f'{where}.options.{key}[{index}]'}
                                        for index, item in enumerate(value))
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
    return (options, permissions, records, [f'{kind}:{scope_id}' for _, _, kind, scope_id, _, _ in applicable],
            inactive, locations)


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
    require(not (adapter.get('writes') and kind == 'reply'), path + '.writes', 'the response stage cannot write')
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


def build_plan(options, adapters, roles, skills, resources, locations):
    tasks, stages = [], []
    responder = None

    def at(raw, index, n):
        """Exact selection path inside a raw plan: [index] or [index][n] for a parallel group."""
        return f'[{index}][{n}]' if isinstance(raw[index], list) else f'[{index}]'

    def task(call, path, phase, fallback=None):
        name = call['task']
        require(name in adapters, path + '.task', 'unavailable selected adapter: ' + name)
        adapter = adapters[name]
        require(fallback is None or adapter['kind'] == fallback, path, 'reviewer selection must use read-only review adapters')
        require(fallback == 'reply' or adapter['kind'] != 'reply', path + '.task',
                'reply adapters run only inside the response stage; select {"stage": "responses"}')
        for index, ref in enumerate(adapter['resources']):
            resource_ref(ref, resources, f'options.adapters.{name}.resources[{index}]')
        skill = call.get('skill')
        require(skill is None or skill in skills, path + '.skill', 'references an undeclared supplied skill')
        entry = {'id': f'task_{len(tasks) + 1}', 'adapter': name, 'phase': phase, 'kind': adapter['kind'],
                 'writes': adapter['writes'], 'definition': adapter['definition'],
                 'definition_sha256': roles[adapter['definition']]['sha256'],
                 'assignment_sha256': hashlib.sha256(adapter['assignment'].encode()).hexdigest(),
                 'instructions': call.get('instructions'), 'required': call.get('required', True),
                 'skill': skill, 'resources': adapter['resources'], 'path': path}
        tasks.append(entry)
        require(len(tasks) <= MAX_TASKS, path, 'plan exceeds bounded task count')
        return entry

    for phase in ('tasks', 'finalizers'):
        raw = options[phase]['value']
        base = f"{locations[options[phase]['source']]}.options.{phase}"
        for index, group in enumerate(groups(raw, 'options.' + phase)):
            where = f'options.{phase}[{index}]'
            if len(group) > 1:
                require(not any('stage' in call for call in group), where, 'managed stages cannot run in a parallel group')
            stage = group[0].get('stage')
            if stage in ('publication', 'ci'):
                stages.append({'kind': stage, 'phase': phase})
            elif stage == 'responses':
                # Every response stage shares one responder occurrence, so a later stage
                # reuses its current result and only new or changed feedback reruns it.
                if responder is None:
                    responder = task({'task': RESPONDER}, f'{where}.stage', phase, 'reply')
                stages.append({'kind': 'responses', 'phase': phase, 'task': responder['id']})
            elif stage == 'reviews':
                if 'reviewers' in group[0]:
                    reviewers, prefix = group[0]['reviewers'], base + at(raw, index, 0) + '.reviewers'
                else:
                    reviewers = options['reviewers']['value']
                    prefix = f"{locations[options['reviewers']['source']]}.options.reviewers"
                require(reviewers, where, 'selected reviewer stage requires explicit reviewers')
                review_groups = [[task(call, prefix + at(reviewers, number, n), phase, 'review')['id']
                                  for n, call in enumerate(reviewer_group)]
                                 for number, reviewer_group in enumerate(groups(reviewers, where + '.reviewers'))]
                stages.append({'kind': 'reviews', 'phase': phase, 'groups': review_groups})
            else:
                entries = [task(call, base + at(raw, index, n), phase) for n, call in enumerate(group)]
                require(len(entries) == 1 or not any(entry['writes'] for entry in entries), where,
                        'parallel writing tasks share a Git index and are unsupported')
                stages.append({'kind': 'tasks', 'phase': phase, 'groups': [[entry['id'] for entry in entries]]})
    for number, stage in enumerate(stages, 1):
        stage['id'] = f'stage_{number}'
    return tasks, stages


# -- Admission --------------------------------------------------------------------------------

def selector(raw, path):
    """One preference path selector: an exact file, a recursive directory or a root-anchored glob."""
    fields(raw, path, ('type', 'path'))
    require(raw['type'] in SELECTOR_TYPES, path + '.type', 'must be file, directory or glob')
    value = raw['path']
    if raw['type'] == 'directory' and value == '.':
        return {'type': 'directory', 'path': '.'}
    value = relative(value, path + '.path')
    require('\\' not in value, path + '.path', 'backslashes are not path separators')
    if raw['type'] == 'glob':
        require(all(part == '**' or '**' not in part for part in value.split('/')), path + '.path',
                '** must be a whole path component')
    else:
        require(not any(char in value for char in '*?['), path + '.path', 'use a glob selector for patterns')
    return {'type': raw['type'], 'path': value}


def preference_scope(record, where):
    """Explicit modes, selectors and condition of a preference, or None when it declares none.

    A record without these fields keeps its original meaning: every task and path. Omitted modes
    are never defaulted here; the dk-preferences entry writes them explicitly on new records.
    """
    if not {'applies_to', 'include', 'exclude', 'condition'} & set(record):
        return None
    require('applies_to' in record and 'include' in record, where,
            'a scoped preference declares applies_to and include explicitly')
    modes = record['applies_to']
    require(isinstance(modes, list) and modes and all(mode in PREFERENCE_MODES for mode in modes)
            and len(set(modes)) == len(modes), where + '.applies_to', 'must be distinct implementation/review modes')
    scope = {'applies_to': [mode for mode in PREFERENCE_MODES if mode in modes]}
    for key in ('include', 'exclude'):
        raw = record.get(key, [])
        require(isinstance(raw, list) and len(raw) <= 64, f'{where}.{key}', 'must be a bounded array')
        scope[key] = [selector(item, f'{where}.{key}[{index}]') for index, item in enumerate(raw)]
    condition = record.get('condition')
    scope['condition'] = None if condition is None else text(condition, where + '.condition')
    require(scope['include'] or scope['condition'] is not None, where + '.condition',
            'a prompt-only preference (empty include) requires a condition')
    return scope


def task_modes(tasks):
    """Preference modes the selected plan exercises: writers implement, review adapters review."""
    return ({'implementation'} if any(task['writes'] for task in tasks) else set()) | (
        {'review'} if any(task['kind'] == 'review' for task in tasks) else set())


def resolve_prompts(records, adapters, resources, contents, selected, path_kind, modes=frozenset(PREFERENCE_MODES)):
    result = []
    seen = {}
    for where, source, record in records:
        scope = None
        if path_kind == 'preferences':
            fields(record, where, ('id', 'text'), ('resources', 'applies_to', 'include', 'exclude', 'condition'))
            scope = preference_scope(record, where)
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
            content = contents[key]  # The exact bytes whose digest admission verified.
            require(len(content) <= MAX_TEXT, where + '.resource', 'prompt resource exceeds byte bound')
            try:
                body = content.decode('utf-8')
            except UnicodeDecodeError:
                raise AdmissionError(where + '.resource', 'prompt resource is not UTF-8') from None
            refs.extend(item['root'] + ':' + link for link in links(content, item['path'], where + '.resource'))
            refs.append(key)
        else:
            body = text(record['text'], where + '.text')
            require(not link_targets(body), where + '.text', 'inline prompt text cannot link local files; declare a resource instead')
        for ref in refs:
            resource_ref(ref, resources, where + '.resources')
        entry = {'id': record['id'], 'slot': slot, 'target': target, 'source': source, 'path': where,
                 'text': body, 'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
                 'resource': record.get('resource'),
                 'resources': sorted(set(refs)), 'scope': scope,
                 # A scoped preference applies only when the plan exercises one of its modes.
                 'applicable': (target is None or target in selected) and (
                     scope is None or bool(set(scope['applies_to']) & modes))}
        previous = seen.get(record['id'])
        require(previous is None, where + '.id', f'duplicate {path_kind} id also declared at {previous}')
        seen[record['id']] = where
        result.append(entry)
    return result


def admit(*, request, configuration, invocation, deployment, roles, roots, transport, defaults=None):
    """Resolve and structurally admit one proposed run; returns an admission result.

    roots maps resource root names to directories; 'repository' is the target
    checkout used for guidance. transport must provide capabilities(); no other
    transport operation is ever called here. defaults replaces bundled suite
    defaults for an entry point's preset (for example incoming review's plan);
    every configured scope still applies above it.
    """
    try:
        require(defaults is None or isinstance(defaults, dict) and not set(defaults) - set(DEFAULTS)
                and not set(defaults) & set(ACCUMULATED), 'defaults', 'unsupported preset defaults')
        return _admit(request, configuration, invocation, deployment, roles, roots, transport, defaults)
    except EngineUnavailable as error:
        # Local structural checks passed; the run is blocked, not structurally rejected.
        return {'preflight': 'blocked', 'structural': 'accepted', 'semantic': None,
                'error': {'path': 'capabilities', 'message': str(error)}}
    except AdmissionError as error:
        failure = {'path': error.path, 'message': error.message}
    except (TypeError, KeyError, AttributeError, ValueError, OSError, RecursionError) as error:
        # Wrong JSON types, invalid text/numbers and unreadable inputs fail closed as structural rejections.
        failure = {'path': '$', 'message': 'malformed input: ' + type(error).__name__}
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None, 'error': failure}


def _admit(request, configuration, invocation, deployment, roles, roots, transport, defaults=None):
    require(isinstance(roots, dict) and 'repository' in roots, 'roots', 'repository root required')
    for name, path in roots.items():
        require(isinstance(name, str) and Path(path).is_dir(), 'roots.' + str(name), 'must be an existing directory')
    roots = {name: Path(path).resolve() for name, path in roots.items()}
    original = resolve_request(request)
    config = check_configuration(configuration)
    runtime = resolve_deployment(deployment)
    catalog = resolve_roles(roles, roots, 'roles')
    resources, contents = resolve_resources(config['resources'], roots, 'configuration.resources')
    options, permissions, records, scopes, inactive, locations = effective(config['scopes'], original, invocation,
                                                                          resources, defaults)

    adapters = {name: check_adapter(name, value, 'baseline.' + name) for name, value in {**BASELINE_ADAPTERS, **ENTRY_ADAPTERS}.items()}
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
    tasks, stages = build_plan(options, adapters, catalog, skills, resources, locations)
    selected = {entry['adapter'] for entry in tasks}
    prompts = resolve_prompts(records['prompts'], adapters, resources, contents, selected, 'prompts')
    preferences = resolve_prompts(records['preferences'], adapters, resources, contents, selected, 'preferences',
                                  task_modes(tasks))
    # Non-matching scopes do not apply, but their records must still be structurally valid.
    for source, where, scope in inactive:
        names = set(adapters) | set(scope['options'].get('adapters', {}))
        for kind in ('prompts', 'preferences'):
            resolve_prompts([(f'{where}.{kind}[{index}]', source, record) for index, record in enumerate(scope[kind])],
                            names, resources, contents, set(), kind)
        for index, record in enumerate(scope['skills']):
            resolve_skill(record, roots, f'{where}.skills[{index}]')
    aliases = {}
    guidance = {'files': follow(roots['repository'], [name for name in GUIDANCE
                                                     if os.path.lexists(roots['repository'] / name)],
                                'guidance', aliases=aliases),
                'symlinks': dict(sorted(aliases.items()))}

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

    free_text = [item for item in options['requirements']['value'] + options['prohibitions']['value']
                 if item['text'] not in ACTIONS]
    custom = {
        'prompts': [entry['id'] for entry in prompts + preferences if entry['applicable']],
        'policy_text': sorted({item['source'] for item in free_text}),
        'instructions': [entry['id'] for entry in tasks if entry['instructions'] is not None],
        'adapters': sorted(name for name in custom_adapters if name in selected),
        'skills': sorted({entry['skill'] for entry in tasks if entry['skill'] is not None}),
    }
    required = set(BASE_CAPABILITIES)
    if custom['skills']:
        required |= {SKILL_CAPABILITY, SKILL_EVIDENCE_CAPABILITY}
        if any(skills[key]['children'] for key in custom['skills']):
            required.add(CHILD_CAPABILITY)
    if effective_permissions['merge']['effective']:
        required.add(MERGE_CAPABILITY)
    semantic_required = any(custom.values())
    engine_required = {INPUT_CAPABILITY} if semantic_required else set()
    # The bundled validator check is local and structural, so it also precedes the engine query.
    validator = bootstrap() if semantic_required else None

    # Every local structural check has passed; only now consult the engine (read-only).
    try:
        capabilities = transport.capabilities()
    except Exception as error:  # Any transport failure; never mistaken for malformed configuration.
        raise EngineUnavailable('engine capability query failed: ' + type(error).__name__) from error
    fields(capabilities, 'capabilities', ('engine', 'providers'))
    fields(capabilities['engine'], 'capabilities.engine', ('version',), ('capabilities',))
    engine_offered = capabilities['engine'].get('capabilities', [])
    require(isinstance(engine_offered, list) and all(isinstance(item, str) for item in engine_offered),
            'capabilities.engine.capabilities', 'must be an array of strings')
    missing = sorted(engine_required - set(engine_offered))
    require(not missing, 'capabilities.engine', 'unsupported engine capability: ' + ', '.join(missing))
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
    if original['pr'] is None:
        request_record['repository'] = original['repository']
    capability_record = {'engine': engine, 'engine_required': sorted(engine_required),
                         'required': sorted(required), 'bindings': bindings}
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
    admission_input = {
        'request_id': request_id,
        'request': {'original_instructions': original['original_instructions'], 'authorizations': original['authorizations']},
        'requirements': options['requirements']['value'],
        'prohibitions': options['prohibitions']['value'],
        'permissions': effective_permissions,
        'capabilities': capability_record,
        'custom': {
            'prompts': [{**{key: entry[key] for key in ('id', 'slot', 'target', 'source', 'path', 'text', 'resources')},
                         **({'scope': entry['scope']} if entry['scope'] is not None else {})}
                        for entry in prompts + preferences if entry['applicable']],
            'instructions': [{'task': entry['id'], 'adapter': entry['adapter'], 'text': entry['instructions'],
                              'path': entry['path'] + '.instructions'}
                             for entry in tasks if entry['instructions'] is not None],
            'adapters': {name: {**adapters[name],
                                'path': f"{locations[options['adapters']['source']]}.options.adapters.{name}"}
                         for name in custom['adapters']},
            'skills': {key: {**{k: skills[key][k] for k in ('name', 'root', 'path', 'entry', 'sha256', 'files',
                                                            'children', 'source')},
                             'record': skills[key]['path_ref'],
                             'references': [entry['path'] + '.skill' for entry in tasks if entry['skill'] == key]}
                       for key in custom['skills']},
        },
        'sources': {'configuration_revision': config['revision'], 'scopes': scopes,
                    'context_digest': context_digest, 'resource_digest': resource_digest,
                    'capability_digest': capability_digest},
        'validator': validator,
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
    if custom['skills'] and not SUPPLIED_SKILL_EXECUTION:
        result['preflight'] = 'rejected'
        result['error'] = {'path': 'custom.skills', 'message': 'supplied skills need session-scoped execution '
                           '(Agent Workflow #139), which is not available yet'}
    elif semantic_required:
        # Structural success never substitutes for semantic acceptance of custom inputs.
        result['preflight'] = 'semantic_required'
    else:
        result['preflight'] = 'admitted'
    return result
