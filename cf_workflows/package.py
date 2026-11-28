"""Deterministic checks of the cf-workflows plugin descriptor and its resource closure.

The plugin project is the dk-pr-fix installation root. Its plugin.yaml uses
Agent Workflow's version 1 descriptor format. Agent Workflow's shared toolchain
(agent-workflow#131-#133) builds, checks and tests the host packages from it;
scripts/check_package_build.py drives that toolchain from unreleased source.
This module builds nothing and generates no host manifest, launcher or catalog.
It checks the inputs that toolchain will consume:

- the descriptor, with the upstream validation rules;
- the declared resource closure: skill entry points, the local reusable-workflow
  closure with its declared helper and agent paths, and the declared resources;
- that the installed files are exactly that closure, with no undeclared,
  missing, linked, colliding or hand-maintained host files;
- that the descriptor lists only inventory rows and keeps its own version.

Its closure scanner reads only the reference forms these workflows use and
rejects any other reference. It is a consistency check; the upstream
`agent-workflow plugin check` is the authoritative one, and it is supported
only once released.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import unicodedata

from . import admission, dk_pr_fix, qualification

ROOT = dk_pr_fix.INSTALL
REPOSITORY = Path(__file__).resolve().parents[1]
DESCRIPTOR = 'plugin.yaml'
HOSTS = ('codex', 'claude')
FIELDS = ('schema-version', 'name', 'version', 'engine-version', 'hosts', 'skills', 'resources')
SKILL_FIELDS = ('name', 'path', 'workflow')
# Host files the shared toolchain generates. A checked-in copy would be hand-maintained.
GENERATED = ('.codex-plugin', '.claude-plugin', '.agents', 'plugin.json', 'marketplace.json', 'compatibility.json')
# Supported-capability prerequisites that are not available yet. Remove an entry
# only when its released capability is actually consumed here. Agent Workflow
# #130-#133 (plugin run, build, check/test and release validation) are merged
# upstream, so the release, the helpers' state mount, the fixture runner's
# container gap and the native-task operation receipt that the three
# configuration entries need remain.
UPSTREAM_BLOCKERS = (
    ('engine release', 'no published engine release contains agent-workflow #130-#133 (packaged run, plugin '
     'build, check, test and release validation), so the pinned version has no verified download'),
    ('helper state mount', 'agent-workflow cannot mount a plugin-private persistent state directory into '
     'container helpers (agent-workflow#149), so no helper of the installed workflow receives '
     + dk_pr_fix.STATE_MOUNT),
    ('plugin test container jobs', 'the agent-workflow fixture runner rejects container jobs (agent-workflow#150), '
     'so plugin test cannot run any bound skill and no deterministic packaged smoke execution exists'),
    ('native operation receipt', 'agent-workflow records no operation receipt or per-task read-only policy for '
     'native agent tasks (agent-workflow#151), so every session of the dk-repo-onboard, dk-config and '
     'dk-preferences entries blocks before anything is reported or written'),
)
IDENTITY = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
                     r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')
PLAIN = re.compile(r'[A-Za-z0-9._/@+-]+')
WORKFLOW = re.compile(r'[A-Za-z0-9._/-]+\.ya?ml')


class PackageError(ValueError):
    """A diagnostic naming the affected file."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


def _scalar(text, where):
    if text.startswith('"'):
        try:
            value = json.loads(text)
        except ValueError:
            raise PackageError(where, 'invalid quoted scalar') from None
        if not isinstance(value, str):
            raise PackageError(where, 'invalid quoted scalar')
        return value
    if re.fullmatch(r'[1-9][0-9]*', text):
        return int(text)
    if not PLAIN.fullmatch(text):
        raise PackageError(where, 'unsupported YAML value ' + json.dumps(text))
    return text


def _flow(text, where):
    if not (text.startswith('[') and text.endswith(']')):
        return _scalar(text, where)
    inner = text[1:-1].strip()
    return [_scalar(item.strip(), where) for item in inner.split(',')] if inner else []


def parse_descriptor(data):
    """Parse the block-style subset of plugin.yaml; anything else rejects.

    Supported: full-line comments, top-level `key: value`, flow lists of plain
    scalars, block lists of scalars and the `skills` list of flat mappings.
    """
    where = DESCRIPTOR
    if len(data) > 1 << 20:
        raise PackageError(where, 'exceeds 1 MiB')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise PackageError(where, 'must be UTF-8') from None
    if '\t' in text or '\r' in text:
        raise PackageError(where, 'tabs and carriage returns are unsupported')
    result, key, item = {}, None, None
    for number, line in enumerate(text.split('\n'), 1):
        at = f'{where}:{number}'
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line.rstrip() != line:
            raise PackageError(at, 'trailing whitespace')
        indent = len(line) - len(line.lstrip(' '))
        body = line.strip()
        if indent == 0:
            match = re.fullmatch(r'([a-z-]+):(?: (.+))?', body)
            if not match or match[1] in result:
                raise PackageError(at, 'unsupported or duplicate top-level entry')
            key, item = match[1], None
            result[key] = _flow(match[2], at) if match[2] is not None else []
            continue
        if key is None or not isinstance(result[key], list):
            raise PackageError(at, 'unexpected indentation')
        if indent == 2 and body.startswith('- '):
            entry = body[2:]
            match = re.fullmatch(r'([a-z-]+): (.+)', entry)
            if match:
                item = {match[1]: _scalar(match[2], at)}
                result[key].append(item)
            else:
                item = None
                result[key].append(_scalar(entry, at))
            continue
        match = re.fullmatch(r'([a-z-]+): (.+)', body)
        if indent == 4 and item is not None and match and match[1] not in item:
            item[match[1]] = _scalar(match[2], at)
            continue
        raise PackageError(at, 'unsupported YAML structure')
    return result


def _exact_version(value):
    match = isinstance(value, str) and VERSION.fullmatch(value)
    if not match:
        return False
    return not any(part.isdigit() and len(part) > 1 and part[0] == '0' for part in (match[4] or '').split('.'))


def local_path(name, where):
    """A literal contained relative path, as the upstream collector requires."""
    if (not isinstance(name, str) or not name or '\\' in name or '\0' in name or '${{' in name
            or name.startswith('/') or '..' in name.split('/')):
        raise PackageError(where, 'requires a literal relative path inside the plugin: ' + json.dumps(name))
    clean = str(PurePosixPath(name))
    if clean == '.':
        raise PackageError(where, 'the whole plugin root cannot be declared')
    return clean


def validate_descriptor(descriptor):
    """The upstream schema 1 rules plus this suite's two-host requirement."""
    where = DESCRIPTOR
    unknown = sorted(set(descriptor) - set(FIELDS))
    missing = [field for field in FIELDS[:-1] if field not in descriptor]
    if unknown or missing:
        raise PackageError(where, 'unknown fields ' + str(unknown) + ', missing fields ' + str(missing))
    if descriptor['schema-version'] != 1:
        raise PackageError(where, 'unsupported schema-version; expected 1')
    if not isinstance(descriptor['name'], str) or not IDENTITY.fullmatch(descriptor['name']):
        raise PackageError(where, 'name must be a lowercase hyphenated identity')
    if not _exact_version(descriptor['version']) or not _exact_version(descriptor['engine-version']):
        raise PackageError(where, 'version and engine-version must be exact semantic versions (without v or ranges)')
    hosts = descriptor['hosts']
    if (not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts)
            or sorted(hosts) != sorted(HOSTS) or len(set(hosts)) != len(hosts)):
        raise PackageError(where, 'hosts must be exactly codex and claude')
    skills = descriptor['skills']
    if not isinstance(skills, list) or not skills:
        raise PackageError(where, 'skills must be nonempty')
    seen, normalized = set(), []
    for skill in skills:
        if not isinstance(skill, dict) or sorted(skill) != sorted(SKILL_FIELDS):
            raise PackageError(where, 'each skill has exactly name, path and workflow')
        name = skill['name']
        if not isinstance(name, str) or not IDENTITY.fullmatch(name):
            raise PackageError(where, 'invalid skill identity ' + json.dumps(name))
        path = local_path(skill['path'], f'{where}: skill {name} path')
        if PurePosixPath(path).name != 'SKILL.md':
            raise PackageError(where, f'skill {name} path must name SKILL.md')
        workflow = workflow_ref(skill['workflow'], f'{where}: skill {name} workflow')
        for key in (('name', name), ('path', path), ('workflow', workflow)):
            if key in seen:
                raise PackageError(where, f'skill {name} duplicates a {key[0]}')
            seen.add(key)
        normalized.append({'name': name, 'path': path, 'workflow': workflow})
    resources = descriptor.get('resources', [])
    if not isinstance(resources, list):
        raise PackageError(where, 'resources must be a list')
    for resource in resources:
        local_path(resource, f'{where}: resource')
    # Accepted spellings (./x, a//b) resolve to the paths the closure and installed files use.
    return {**descriptor, 'skills': normalized}


def workflow_ref(value, where):
    """Upstream CanonicalLocalRef: a local .yml/.yaml path inside a workflows/ directory."""
    if not isinstance(value, str) or any(part in ('', '.') for part in value.removeprefix('./').split('/')):
        raise PackageError(where, 'workflow ref contains an unsafe path component: ' + json.dumps(value))
    path = local_path(value, where)
    if not WORKFLOW.fullmatch(path) or 'workflows' not in path.split('/')[:-1]:
        raise PackageError(where, 'requires a local .yml or .yaml workflow path in a workflows/ directory')
    return path


def _read(root, name):
    """The bytes of a declared regular file; symlinks, directories and special files reject."""
    _check_path(root, name)
    if not (root / name).is_file():
        raise PackageError(name, 'requires a regular file')
    return (root / name).read_bytes()


def _text(root, name):
    try:
        return _read(root, name).decode('utf-8')
    except UnicodeDecodeError:
        raise PackageError(name, 'must be UTF-8') from None


def _reusable(lines):
    """Whether the top-level `on` key declares workflow_call."""
    for number, line in enumerate(lines):
        match = re.fullmatch(r"(?:on|'on'|\"on\"):(.*)", line)
        if match and match[1].strip():
            return re.search(r'\bworkflow_call\b', match[1]) is not None
        if match:
            children = list(_block(lines, number, 0))
            depth = min((len(item) - len(item.lstrip(' ')) for item in children), default=0)
            return any(item[depth:].startswith('workflow_call:') for item in children
                       if len(item) - len(item.lstrip(' ')) == depth)
    return False


def skill_metadata(data, where):
    """SKILL.md frontmatter name and description, followed by instructions."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise PackageError(where, 'must be UTF-8') from None
    match = re.fullmatch(r'---\n(.*?)\n---\n(.*)', text, re.S)
    if not match or not match[2].strip():
        raise PackageError(where, 'requires frontmatter with name and description, then instructions')
    fields = {}
    for line in match[1].split('\n'):
        entry = re.fullmatch(r'([a-z]+): (.+)', line)
        if not entry or entry[1] in fields or entry[1] not in ('name', 'description'):
            raise PackageError(where, 'unsupported frontmatter line ' + json.dumps(line))
        fields[entry[1]] = entry[2]
    if not IDENTITY.fullmatch(fields.get('name', '')) or not fields.get('description', '').strip():
        raise PackageError(where, 'requires name and description')
    return fields


# -- Resource closure -----------------------------------------------------------------------


def _check_path(root, name):
    """Reject symlinks in every component of a contained path."""
    current = root
    for part in name.split('/'):
        current = current / part
        if current.is_symlink():
            raise PackageError(name, 'symlinks are unsupported')
        if not current.exists():
            raise PackageError(name, 'declared resource is missing')


def _expand(root, name, closure):
    """Add a declared file, or every file under a declared directory."""
    _check_path(root, name)
    path = root / name
    if path.is_file():
        closure.add(name)
        return
    if not path.is_dir():
        raise PackageError(name, 'requires a regular file or directory')
    for directory, names, files in os.walk(path):
        names.sort()
        for entry in names + sorted(files):
            child = Path(directory) / entry
            relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                raise PackageError(relative, 'symlinks are unsupported')
            if child.is_file():
                closure.add(relative)


def _block(lines, start, indent):
    """The nonblank value lines of the key at lines[start]: deeper lines, or same-indent list items."""
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip(' '))
        if depth < indent or depth == indent and not line.lstrip().startswith('- '):
            return
        yield line


# Engine-provided step actions a workflow may use; they reference no plugin file.
# input/wait is the durable external input gate (agent-workflow docs/EXTERNAL_INPUT.md).
BUILTIN_STEPS = ('agent/run', 'input/wait')


def _workflow(root, ref, closure, visited, active=()):
    """Add the local reusable-workflow closure of ref and its declared helper and agent paths."""
    if ref in active:
        raise PackageError(ref, 'workflow dependency cycle')
    lines = None
    if ref not in visited:  # Scanned once; every call, even a later one, checks on.workflow_call.
        closure.add(ref)
        lines = _text(root, ref).split('\n')
        visited[ref] = _reusable(lines)
    if active and not visited[ref]:
        raise PackageError(ref, 'a called plugin workflow requires on.workflow_call')
    if lines is None:
        return
    base = PurePosixPath(ref).parent
    for number, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(' '))
        body = line.strip()
        if body.startswith('- '):
            body, indent = body[2:], indent + 2
        match = re.fullmatch(r'(uses|files|definitions|agent):(?: (.*))?', body)
        if not match:
            continue
        key, value, at = match[1], (match[2] or '').strip(), f'{ref}:{number + 1}'
        if key in ('files', 'definitions') and value and not value.startswith('#'):
            # Flow or inline values would hide entries from this scanner; only block form is read.
            raise PackageError(at, key + ' must be a block list or mapping')
        if key == 'uses':
            if value not in BUILTIN_STEPS:
                _workflow(root, workflow_ref(value, at), closure, visited, (*active, ref))
        elif key == 'files':
            for item in _block(lines, number, indent):
                entry = re.fullmatch(r'\s*- (\S+)', item)
                if not entry:
                    raise PackageError(at, 'helper files must be a literal list')
                if '..' in entry[1].split('/'):
                    raise PackageError(at, 'helper path traversal is unsupported')
                _expand(root, local_path(str(base / entry[1]), at), closure)
        elif key == 'definitions':
            for item in _block(lines, number, indent):
                entry = re.fullmatch(r'\s*[a-z-]+: (\S+)', item)
                if not entry:
                    raise PackageError(at, 'agent definitions must be literal paths')
                _agent(root, local_path(str(base / entry[1]), at), closure, at)
        elif value and not value.startswith('${{'):
            _agent(root, local_path(str(base / value), at), closure, at)


def _agent(root, name, closure, where):
    if not name.endswith(('.yaml', '.yml')):
        raise PackageError(where, 'agent definitions must be YAML: ' + name)
    admission.role_definition(_read(root, name), name)
    closure.add(name)


def declared_closure(root=ROOT):
    """(descriptor, sorted declared file paths). Every reference must resolve inside root."""
    root = Path(root)
    descriptor = validate_descriptor(parse_descriptor(_read(root, DESCRIPTOR)))
    closure, visited = {DESCRIPTOR}, {}
    for skill in descriptor['skills']:
        metadata = skill_metadata(_read(root, skill['path']), skill['path'])
        if metadata['name'] != skill['name']:
            raise PackageError(skill['path'], f'name {metadata["name"]!r} does not match binding {skill["name"]!r}')
        closure.add(skill['path'])
        _workflow(root, skill['workflow'], closure, visited)
    for resource in descriptor.get('resources', []):
        _expand(root, local_path(resource, DESCRIPTOR), closure)
    return descriptor, sorted(closure)


def installed_files(root=ROOT):
    """{path: {'sha256', 'executable'}} of every file under root; links, special files and caches reject."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise PackageError('.', 'plugin root must be a directory')
    files, folded = {}, {}
    for directory, names, entries in os.walk(root):
        names.sort()
        for name in names + sorted(entries):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                relative.encode('utf-8')
            except UnicodeEncodeError:
                raise PackageError(repr(relative), 'file names must be UTF-8') from None
            key = unicodedata.normalize('NFC', relative).casefold()
            if key in folded:
                raise PackageError(relative, 'collides with ' + folded[key]
                                   + ' on case-insensitive or normalizing hosts')
            folded[key] = relative
            if name in GENERATED:
                raise PackageError(relative, 'host manifests and catalogs are generated by the shared toolchain, '
                                   'never checked in')
            if name == '__pycache__' or name.endswith(('.pyc', '.pyo')):
                raise PackageError(relative, 'Python bytecode is never part of the package')
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise PackageError(relative, 'symlinks are unsupported')
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise PackageError(relative, 'special files are unsupported')
            files[relative] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'executable': bool(mode & stat.S_IXUSR)}
    return dict(sorted(files.items()))


def check(root=ROOT):
    """Check one plugin root; returns its descriptor and closure digest or raises PackageError.

    The closure digest binds each path, content digest and executable bit, so
    location, ownership, timestamps and read-only modes do not change it.
    """
    root = Path(root)
    files = installed_files(root)
    descriptor, closure = declared_closure(root)
    undeclared = sorted(set(files) - set(closure))
    if undeclared:
        raise PackageError(undeclared[0], 'undeclared resource in the plugin root'
                           + (f' (and {len(undeclared) - 1} more)' if len(undeclared) > 1 else ''))
    try:
        dk_pr_fix.verify_installation(root)
    except (dk_pr_fix.InstallationError, OSError, ValueError, KeyError) as error:
        raise PackageError(dk_pr_fix.CLOSURE, str(error)) from None
    return {'descriptor': descriptor, 'files': files, 'closure_digest': admission.digest(files)}


def inventory_status(descriptor, repository=REPOSITORY, closure_digest=None):
    """Check that the descriptor lists only inventory rows and report what it may claim.

    The plugin version is the suite version from sources.json and is independent
    of the engine version. The acceptance matrix adds its own blockers: live
    procedures without accepted records, no verified host/provider/engine
    combination and rows without native-execution evidence. Live records count
    only for `closure_digest`. The legacy `releasable` field means runtime
    qualification and stays false while any blocker remains. Package publication
    is checked independently by scripts/release_gate.py.
    """
    sources = json.loads((Path(repository) / 'baselines/sources.json').read_text())
    inventory = json.loads((Path(repository) / 'baselines/inventory.json').read_text())
    suite = sources['suite']
    if descriptor['name'] != suite['name'] or descriptor['version'] != suite['version']:
        raise PackageError(DESCRIPTOR, 'plugin name and version must be the suite identity in baselines/sources.json')
    rows = {row['name']: row for row in inventory['skills']}
    listed = []
    for skill in descriptor['skills']:
        row = rows.get(skill['name'])
        if row is None or row['entrypoint'] != f'{suite["name"]}:{skill["name"]}':
            raise PackageError(DESCRIPTOR, f'skill {skill["name"]} is not an inventory row')
        listed.append({'skill': row['entrypoint'], 'parity_status': row['parity_status']})
    blockers = [{'prerequisite': name, 'missing': what} for name, what in UPSTREAM_BLOCKERS]
    if not suite['runnable']:
        blockers.append({'prerequisite': 'cf-workflows', 'missing': 'the suite is not runnable yet'})
    blockers += [{'prerequisite': item['skill'], 'missing': 'parity status ' + item['parity_status']}
                 for item in listed if item['parity_status'] != 'implemented']
    blockers += qualification.release_blockers(
        repository, inventory=inventory, descriptor_skills={skill['name'] for skill in descriptor['skills']},
        closure_digest=closure_digest, engine_version=descriptor['engine-version'])
    return {'plugin': descriptor['name'], 'version': descriptor['version'],
            'engine_version': descriptor['engine-version'], 'hosts': descriptor['hosts'],
            'listed': listed, 'inventory_rows': len(rows), 'releasable': not blockers, 'blockers': blockers}


def report(root=ROOT, repository=REPOSITORY):
    checked = check(root)
    return {'schema_version': 1, 'kind': 'package_check', 'closure_digest': checked['closure_digest'],
            'files': len(checked['files']),
            **inventory_status(checked['descriptor'], repository, closure_digest=checked['closure_digest'])}


if __name__ == '__main__':
    try:
        print(json.dumps(report(), indent=2))
    except PackageError as error:
        print(json.dumps({'schema_version': 1, 'kind': 'package_check', 'error':
                          {'path': error.path, 'message': error.message}}, indent=2))
        sys.exit(1)
