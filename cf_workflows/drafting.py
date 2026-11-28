"""Drafting entries: cf-workflows:dk-pr-description, dk-commit-message and dk-issue (contract v1).

Each entry is a thin preset over the shared workflow: one bundled adapter task
(dk-pr-description runs the baseline `describe-pr`; the others run
`draft-commit-message` and `draft-issue`), followed by the shared publication
stage for the two entries that can write, with explicitly empty finalizers and
reviewers. Its adapter procedure is the admitted `leaf` role, the bundled
assignment and the helper operations here:

- `collect` gathers read-only evidence: the actual PR diff or local staged,
  unstaged and untracked changes, the governing template or the fallback
  sections, repository guidance, recent human commits, verified facts, the
  current title and body, and the labels and assignees verified in the target
  repository. It never changes Git or the remote.
- `draft` runs (or reuses, by binding and claim) one engine-managed native
  session and accepts its draft only when every claim cites known sources,
  validation and reproduction come from evidence of that kind, references and
  closing keywords name known issues, the template or fallback sections are
  followed, quoted user wording is kept exactly and disabled fields and the
  Decisions section are preserved.
- `propose` and `publish` turn an accepted draft into one exact intent of the
  shared publication boundary (`edit_pr`, `create_issue` or `edit_issue`) under
  an effective permission and authorization. dk-commit-message never commits
  or publishes; a committing procedure consumes its `handoff`.

The same procedure serves a composed plan that selects the adapter. Sessions
come from the engine transport; nothing here starts a model, provider or
controller. Original role prompts and options come from the pinned dk-workflows
baseline (MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

from . import admission, configuration, decisions, dk_pr_fix, prose, semantic
from .admission import AdmissionError, digest, fields, require, text

SCHEMA_VERSION = 1
KIND = 'drafting_state'
MAX_SOURCE = 64 << 10
MAX_PATCH = 256 << 10
MAX_ITEMS = 256
HISTORY = 20

# Exact baseline bytes (claude/ and codex/ skills/<entry>/config.json and prompts/default.md; digests in
# baselines/inventory.json).
ENTRIES = {
    'dk-pr-description': {
        'skill': 'cf-workflows:dk-pr-description', 'adapter': 'describe-pr', 'targets': ('pr',),
        'actions': ('edit_pr',),
        'config': '{\n  "$schema": "./config.schema.json",\n  "template": null,\n  "update_title": true,\n  '
                  '"update_body": true,\n  "fallback_sections": [\n    "Issue",\n    "Fix"\n  ]\n}\n',
        'role_prompt': 'Read the complete diff, PR template, linked issue, maintainer conventions and current '
                       'Decisions. Use the configured repository-relative `template` when present; null uses '
                       'repository discovery. Apply `fallback_sections` only when repository formatting leaves a '
                       'choice. Honor `update_title` and `update_body`, preserving each disabled field. Describe the '
                       'resulting behavior and validation. Preserve Decisions and unrelated body sections. Make the '
                       'description match the agreed scope before review.\n',
    },
    'dk-commit-message': {
        'skill': 'cf-workflows:dk-commit-message', 'adapter': 'draft-commit-message', 'targets': ('pr', 'repository'),
        'actions': (),
        'config': '{\n  "$schema": "./config.schema.json",\n  "subject_target_length": 72,\n  '
                  '"body_wrap_length": 72,\n  "max_line_length": 80\n}\n',
        'role_prompt': 'Read the diff, repository guidance, commit template and recent human commits. When supplied '
                       'by cleanup, apply the verified [commit policy](../../../../docs/commit-policy.md) message '
                       'instructions. Explicit policy instructions replace built-in stylistic defaults and '
                       'historical style preferences; required repository conventions and current user constraints '
                       'still apply. Preserve authorship and source records. Return the reviewed message, policy and '
                       'sources. Commit only when requested.\n\nWithout explicit message instructions, match '
                       'repository style and apply the configured `subject_target_length`, `body_wrap_length` and '
                       '`max_line_length` fallbacks and use [the field conventions](../../../../runtime/'
                       'dk-commit-message/references/field-schema.md) when repository formatting rules leave a '
                       'choice open.\n\n',
    },
    'dk-issue': {
        'skill': 'cf-workflows:dk-issue', 'adapter': 'draft-issue', 'targets': ('pr', 'repository'),
        'actions': ('create_issue', 'edit_issue'),
        'config': '{\n  "$schema": "./config.schema.json",\n  "template": null,\n  "labels": [],\n  '
                  '"assignees": [],\n  "fallback_sections": [\n    "Problem",\n    "Expected behavior"\n  ]\n}\n',
        'role_prompt': 'Draft, create or update GitHub issues using repository templates and conventions.\n',
    },
}
for _entry in ENTRIES.values():
    _entry['options'] = {key: value for key, value in json.loads(_entry['config']).items() if key != '$schema'}
BY_ADAPTER = {entry['adapter']: name for name, entry in ENTRIES.items()}
WRITES = {'dk-pr-description': 'publishes only its reviewed title and body (edit_pr)',
          'dk-commit-message': 'drafts a message only; it never stages, commits or publishes',
          'dk-issue': 'writes only the drafted issue (create_issue or edit_issue)'}
# Fact kinds a caller may supply; only validation and reproduction facts back those sections.
FACT_KINDS = ('fact', 'validation', 'reproduction')
PR_TEMPLATES = ('pull_request_template.md', '.github/pull_request_template.md', 'docs/pull_request_template.md')
ISSUE_TEMPLATES = ('issue_template.md', '.github/issue_template.md', 'docs/issue_template.md')

_HEADING = re.compile(r' {0,3}(#{1,6})[ \t]+(.*)')  # The closing # run is stripped by _heading().
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[`~]*[ \t]*$', re.M | re.S)
_CODE = re.compile(r'(`+)(?:(?!\1|\n[ \t]*\n).)+?\1', re.S)  # A code span never crosses a blank line.
_LABEL = re.compile(r'^\s*(?:[-*]\s+)?\**([A-Za-z][A-Za-z ]{0,30}?)\**\s*:', re.M)
VALIDATION = re.compile(r'(?:test(?:s|ing|ed| plan)?|validation|validated|verification|verified|how to test|'
                        r'checks? run)$', re.I)
REPRODUCTION = re.compile(r'(?:steps to reproduce|reproduction(?: steps)?|to reproduce|repro(?:duce)?)$', re.I)
_REF = re.compile(r'(?:(?<![\w/&#])#|(?<![\w/&#-])(?i:GH)-)([1-9][0-9]{0,15})\b')  # Bounded: int() rejects huge digit runs.
_URL_REF = re.compile(r'https://([A-Za-z0-9.-]+)/([\w.-]+)/([\w.-]+)/(?:issues|pull)/([1-9][0-9]*)')
_CLOSING = re.compile(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:#|GH-)([1-9][0-9]{0,15})\b', re.I)
_QUALIFIED_REF = re.compile(r'(?<![\w/.-])[\w.-]+/[\w.-]+#[1-9][0-9]{0,15}\b')
# GitHub also closes an owner/repo#N or issue-URL reference; a draft closes only linked issues, written as #N.
_CLOSING_ELSEWHERE = re.compile(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:[\w.-]+/[\w.-]+#[1-9][0-9]*|'
                                r'https?://[^\s)>\]]+/(?:issues|pull)/[1-9][0-9]*)', re.I)
_CODE_LINK = re.compile(r'https://[A-Za-z0-9.-]+/[\w.-]+/[\w.-]+/(blob|tree)/([^/\s)>\]]+)')
_PATH_LINE = re.compile(r'(?<![\w/.:#-])(?:[\w.-]+/)*[\w-]+\.[A-Za-z0-9]{1,8}:[1-9][0-9]*\b')
_ATTRIBUTION = re.compile(r'generated (?:by|with)\b|\U0001F916|co-authored-by:[^\n]*\b(?:claude|codex|chatgpt|'
                          r'gpt-?\d|copilot|anthropic|openai)\b', re.I)
_TRAILER = re.compile(r'^(signed-off-by|co-authored-by):[ \t]*(.+?)[ \t]*$', re.I | re.M)
_MARKER = re.compile(r'<!-- cf-workflows:decision [a-z0-9][a-z0-9-]{0,63} -->')
_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
_BOT = re.compile(r'\[bot\]|^dependabot|^renovate|noreply@github\.com$', re.I)


class Blocked(configuration.Blocked):
    """The evidence, the session result or a publication precondition cannot be used; nothing is applied."""


def _failure(error):
    if isinstance(error, (AdmissionError, configuration.Blocked)):
        return {'path': error.path, 'message': error.message}
    return {'path': '$', 'message': 'malformed result: ' + type(error).__name__}


def _rejected(path, message):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}}


# -- Entry preset -------------------------------------------------------------------------------

def preset(name):
    """The entry's selection for the shared plan: its adapter, then publication only for a writing entry."""
    entry = ENTRIES[name]
    return {'tasks': [entry['adapter']] + ([{'stage': 'publication'}] if entry['actions'] else []),
            'finalizers': [], 'reviewers': []}


def _names(value, where):
    require(isinstance(value, list) and len(value) <= 100 and len(set(map(str, value))) == len(value),
            where, 'must be a bounded list of distinct names')
    for number, item in enumerate(value):
        text(item, f'{where}[{number}]', 256)
    return value


def check_options(name, options):
    """The entry's baseline options (its config.schema.json) with provenance; unknown options reject."""
    require(name in ENTRIES, 'entry', 'unknown drafting entry')
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    declared = ENTRIES[name]['options']
    result = {key: {'value': copy.deepcopy(value), 'source': 'default'} for key, value in declared.items()}
    for key, value in options.items():
        where = 'options.' + str(key)
        require(key in declared, where, 'unsupported option for ' + ENTRIES[name]['skill'])
        if key == 'template':
            require(value is None or isinstance(value, str), where, 'must be a repository-relative path or null')
            if value is not None:
                admission.relative(value, where)
        elif key in ('update_title', 'update_body'):
            require(type(value) is bool, where, 'must be boolean')
        elif key in ('labels', 'assignees', 'fallback_sections'):
            _names(value, where)
        else:
            require(type(value) is int and 1 <= value <= 1000, where, 'must be a positive integer')
        result[key] = {'value': copy.deepcopy(value), 'source': 'invocation'}
    return result


def preflight(name, *, request, configuration, deployment, repository, transport, invocation=None, options=None,
              roots=None, install=dk_pr_fix.INSTALL, ledger=None):
    """Admit one direct run of a drafting entry through the shared pipeline; writes nothing.

    The preset fixes tasks, finalizers, reviewers and permissions; invocation may
    add only prompts. dk-pr-description accepts only edit_pr, dk-issue only
    create_issue and edit_issue, and dk-commit-message no authorization at all.
    An authorization still needs the configured permission. A default run
    contacts the engine only for capabilities.
    """
    entry = ENTRIES.get(name)
    if entry is None:
        return _rejected('entry', 'unknown drafting entry')
    try:
        entry_options = check_options(name, options)
        require(isinstance(request, dict), 'request', 'must be an object')
        require('repository' not in request or 'repository' in entry['targets'], 'request.pr',
                entry['skill'] + ' describes one pull request')
        authorizations = request.get('authorizations') or {}
        require(isinstance(authorizations, dict) and set(authorizations) <= set(entry['actions']),
                'request.authorizations', entry['skill'] + ' ' + WRITES[name])
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'prompts'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and permissions; only prompts may be added')
    except AdmissionError as error:
        return {**_rejected(error.path, error.message), 'entry': {'name': name, 'skill': entry['skill']}}
    try:
        dk_pr_fix.verify_installation(install)
    except (dk_pr_fix.InstallationError, OSError, ValueError, KeyError) as error:
        return _rejected('installation', str(error) or type(error).__name__)
    roots = dict(roots or {})
    if dk_pr_fix.INSTALL_ROOT in roots or 'repository' in roots:
        return _rejected('roots', 'installation and repository roots are supplied by preflight')
    if not Path(repository).is_dir() or not dk_pr_fix._disjoint(repository, install):
        return _rejected('repository', 'target checkout must be a directory separate from the installation')
    merged = {'options': preset(name), 'prompts': copy.deepcopy(invocation.get('prompts', []))}
    result = semantic.admit(request=copy.deepcopy(request), configuration=copy.deepcopy(configuration),
                            invocation=merged, deployment=deployment, roles=dk_pr_fix.role_catalog(install),
                            roots={**roots, 'repository': repository, dk_pr_fix.INSTALL_ROOT: install},
                            transport=transport, ledger=ledger)
    result['entry'] = {'name': name, 'skill': entry['skill'], 'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    snapshot = result['snapshot']
    for action in sorted(authorizations):
        if not snapshot['permissions'][action]['effective']:
            return {**result, 'preflight': 'rejected', 'error': {
                'path': 'request.authorizations.' + action,
                'message': 'the authorized action is not enabled by accepted configuration; enable it in a scoped '
                           'configuration or remove the authorization'}}
    [task] = snapshot['plan']['tasks']
    expected = ['tasks', 'publication'] if entry['actions'] else ['tasks']
    assert task['adapter'] == entry['adapter'] and [s['kind'] for s in snapshot['plan']['stages']] == expected
    assert not any(value['effective'] for action, value in snapshot['permissions'].items()
                   if action not in entry['actions'])
    result['entry'].update(adapter=entry['adapter'], task=task,
                           publication={action: snapshot['permissions'][action]['effective']
                                        for action in entry['actions']})
    return result


# The installed entry workflows: presets over workflows/main.yml (docs/drafting.md#installed-workflow).
ENTRY_WORKFLOWS = {'dk-pr-description': 'workflows/description.yml',
                   'dk-commit-message': 'workflows/commit-message.yml', 'dk-issue': 'workflows/issue.yml'}


def installed(name, *, request, configuration, deployment, repository, transport, invocation=None, options=None,
              metadata=None, issue=None, roots=None, home=None, install=dk_pr_fix.INSTALL):
    """Admit one run of the installed entry workflow and return its declared inputs; writes nothing.

    The installed preset runs main.yml with the same tasks, finalizers and
    reviewers as preset(name) and only the entry's own permission inputs, which an
    invocation may enable for the requested actions. Entry
    options set by the invocation, and for dk-issue the requested labels and
    assignees (metadata) and an issue number to update, are passed as
    settings.drafting[name], which the installed helper validates again. The
    installed workflow drafts for one pull request and runs no invocation prompts;
    a repository target and prompts use preflight() and draft() instead.
    """
    entry = ENTRIES.get(name)
    if entry is None:
        return _rejected('entry', 'unknown drafting entry')
    try:
        entry_options = check_options(name, options)
        require(isinstance(request, dict), 'request', 'must be an object')
        require('repository' not in request, 'request.repository',
                'the installed entry drafts for one pull request; a repository target runs only through the '
                'library procedure')
        authorizations = request.get('authorizations') or {}
        require(isinstance(authorizations, dict) and set(authorizations) <= set(entry['actions']),
                'request.authorizations', entry['skill'] + ' ' + WRITES[name])
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'permissions'}, 'invocation',
                'the installed entry fixes its preset and runs no prompts yet; only permissions may be added')
        permissions = invocation.get('permissions', {})
        require(isinstance(permissions, dict), 'invocation.permissions', 'must be an object')
        for action in sorted(permissions):
            require(action in entry['actions'], 'invocation.permissions.' + str(action),
                    'only the entry\'s own actions may be enabled')
        require(name == 'dk-issue' or (metadata is None and issue is None), 'metadata',
                'only dk-issue takes requested labels, assignees or an issue number')
        if metadata is not None:
            require(isinstance(metadata, dict) and set(metadata) <= {'labels', 'assignees'}, 'metadata',
                    'only labels and assignees may be requested')
            for key, value in metadata.items():
                _names(value, 'metadata.' + key)
        require(issue is None or (type(issue) is int and issue > 0), 'issue', 'must be an issue number')
    except AdmissionError as error:
        return {**_rejected(error.path, error.message), 'entry': {'name': name, 'skill': entry['skill']}}
    result = dk_pr_fix.preflight(request=request, configuration=configuration,
                                 invocation=copy.deepcopy(invocation) or None, deployment=deployment,
                                 repository=repository, transport=transport, roots=roots,
                                 home=home, install=install, defaults=preset(name), workflow=entry['skill'],
                                 entry=ENTRY_WORKFLOWS[name])
    result['entry'] = {'name': name, 'skill': entry['skill'], 'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    chosen = {key: copy.deepcopy(value['value']) for key, value in entry_options.items()
              if value['source'] == 'invocation'}
    if metadata is not None:
        chosen['metadata'] = copy.deepcopy(metadata)
    if issue is not None:
        chosen['issue'] = issue
    if chosen:
        result['inputs']['settings'] = {**result['inputs']['settings'], 'drafting': {name: chosen}}
    result['inputs']['permissions'] = {action: result['inputs']['permissions'][action] for action in entry['actions']}
    result['entry'].update(adapter=entry['adapter'], publication={
        action: result['inputs']['permissions'][action] for action in entry['actions']})
    return result


def occurrence(name, admitted):
    """The entry's adapter occurrence in an admitted plan: its own preset or a composed shared plan."""
    tasks = [task for task in admitted['snapshot']['plan']['tasks'] if task['adapter'] == ENTRIES[name]['adapter']]
    require(len(tasks) == 1, 'plan.tasks', 'the plan must select ' + ENTRIES[name]['adapter'] + ' exactly once')
    return tasks[0]


def effective(name, admitted, options=None):
    """The governing role behavior, prompt additions and options of this occurrence.

    The entry's own preflight binds its options; a composed caller may pass the
    entry options it was configured with, checked against the same schema.
    """
    entry = admitted.get('entry')
    entry = entry if isinstance(entry, dict) else {}
    own = entry.get('skill') == ENTRIES[name]['skill']
    require(not (own and options is not None), 'options', 'the entry preflight already bound its options')
    checked = entry['options'] if own else check_options(name, options)
    adapter = ENTRIES[name]['adapter']
    applicable = [item for item in admitted['snapshot']['prompts']
                  if item['applicable'] and item['target'] in (None, adapter)]
    replacement = [item for item in applicable if item['slot'] == 'behavior_replacement']
    require(len(replacement) <= 1, 'prompts', 'at most one behavior replacement applies to ' + adapter)
    return {'role_prompt': replacement[0]['text'] if replacement else ENTRIES[name]['role_prompt'],
            'role_source': 'prompt:' + replacement[0]['id'] if replacement else 'default',
            'additions': [{key: item[key] for key in ('id', 'slot', 'text')} for item in applicable
                          if item['slot'] != 'behavior_replacement'],
            'options': {key: value['value'] for key, value in checked.items()}}


# -- Evidence -----------------------------------------------------------------------------------

def git(repository, *args, check=True):
    """Read-only Git: optional locks, filesystem monitors, external diffs and lazy fetches are off."""
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(repository), *args],
                            capture_output=True, timeout=60,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
                                 'GIT_TERMINAL_PROMPT': '0'})
    if check and result.returncode:
        raise Blocked('git', 'git ' + args[0] + ' failed: ' + result.stderr.decode(errors='replace')[:400])
    return result.stdout


def _bounded(data, limit):
    value = data.decode('utf-8', 'replace') if isinstance(data, bytes) else data
    return (value[:limit], True) if len(value) > limit else (value, False)


def _source(ident, kind, body, *, public, url=None, path=None):
    value, truncated = _bounded(body, MAX_SOURCE)
    record = {'id': ident, 'kind': kind, 'text': value, 'public': public, 'url': url,
              'sha256': hashlib.sha256(value.encode()).hexdigest()}
    if path is not None:
        record['path'] = path
    if truncated:
        record['truncated'] = True
    return record


def _strip_code(body):
    return _CODE.sub(' ', _FENCE.sub('\n', body))


def _heading(line):
    """An ATX heading line's title without its closing # run, else None; no backtracking regex."""
    match = _HEADING.fullmatch(line)
    return match[2].rstrip(' \t').rstrip('#').strip() if match else None


def headings(body):
    """Markdown heading titles outside fenced code, in order."""
    found, fence = [], None
    for raw in body.split('\n'):
        line = raw.rstrip('\r')
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            fence = None if fence and stripped.startswith(fence) else (fence or stripped[:3])
            continue
        title = None if fence else _heading(line)
        if title is not None:
            found.append(title)
    return found


def _front_matter(body):
    if body.startswith('---\n'):
        end = body.find('\n---', 4)
        if end != -1:
            return body[end + 4:].lstrip('\n')
    return body


def _form_labels(body):
    """Body field labels of a GitHub issue form; a submitted form renders each as a `### label` heading."""
    labels, inside = [], False
    for line in body.split('\n'):
        if re.match(r'^body\s*:', line):
            inside = True
        elif inside and re.match(r'^\S', line):
            inside = False
        match = re.match(r'^\s+label\s*:\s*(.+?)\s*$', line) if inside else None
        if match:
            labels.append(match[1].strip('\'"'))
    return labels


def _template(root, rel):
    data = admission.read_file(root, rel, 'template', MAX_SOURCE).decode('utf-8', 'replace')
    form = rel.endswith(('.yml', '.yaml'))
    return {'path': rel, 'kind': 'form' if form else 'markdown', 'text': data,
            'sha256': hashlib.sha256(data.encode()).hexdigest(),
            'headings': _form_labels(data) if form else headings(_front_matter(data)),
            'comments': '<!--' in data}


def templates(repository, name, configured):
    """The governing templates: the configured one, else every discovered repository template."""
    root = Path(repository).resolve()
    if configured is not None:
        try:
            return [_template(root, configured)]
        except AdmissionError as error:
            raise Blocked('options.template', error.message) from None
    found, names = [], PR_TEMPLATES if name == 'dk-pr-description' else ISSUE_TEMPLATES
    for directory in (root, root / '.github', root / 'docs'):
        if directory.is_dir() and not directory.is_symlink():
            found.extend(item.relative_to(root).as_posix() for item in sorted(directory.iterdir())
                         if item.is_file() and not item.is_symlink()
                         and item.relative_to(root).as_posix().lower() in names)
    folder = root / '.github' / ('PULL_REQUEST_TEMPLATE' if name == 'dk-pr-description' else 'ISSUE_TEMPLATE')
    if folder.is_dir() and not folder.is_symlink():
        suffixes = ('.md',) if name == 'dk-pr-description' else ('.md', '.yml', '.yaml')
        found.extend(item.relative_to(root).as_posix() for item in sorted(folder.iterdir())
                     if item.is_file() and not item.is_symlink() and item.name.lower().endswith(suffixes)
                     and item.name.lower() not in ('config.yml', 'config.yaml'))
    return [_template(root, rel) for rel in found]


def _guidance(admitted, repository):
    root = Path(repository).resolve()
    result = []
    for rel in sorted(admitted['snapshot']['resources']['guidance']['files']):
        data = admission.read_file(root, rel, 'guidance', MAX_SOURCE)
        result.append(_source('guidance:' + rel, 'guidance', data, public=True, path=rel))
    return result


def _facts(facts):
    result = []
    for number, item in enumerate(facts):
        where = f'facts[{number}]'
        fields(item, where, ('id', 'kind', 'text'), ('url',))
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', str(item['id'])), where + '.id', 'invalid fact id')
        require(item['kind'] in FACT_KINDS, where + '.kind', 'must be one of ' + ', '.join(FACT_KINDS))
        text(item['text'], where + '.text')
        result.append(_source(f"{item['kind']}:{item['id']}", item['kind'], item['text'],
                              public=item.get('url') is not None, url=item.get('url')))
    return result


def _untracked_digest(repository, names):
    """The content of untracked paths (raw Git names): file bytes, link targets, or the kind of anything else."""
    total = hashlib.sha256()
    root = os.fsencode(str(repository))
    for raw in names:
        path = os.path.join(root, raw)
        item = hashlib.sha256()
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                item.update(b'link\0' + os.readlink(path))
            elif stat.S_ISREG(mode):
                with open(path, 'rb') as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        item.update(chunk)
            else:
                item.update(b'other')
        except OSError as error:
            item.update(b'unreadable\0' + type(error).__name__.encode())
        total.update(raw + b'\0' + item.digest())
    return total.hexdigest()


def local_changes(repository):
    """Read-only capture of HEAD, staged and unstaged patches and untracked paths; never refreshes the index.

    content binds the whole patches and the untracked files' content, past the text bounds.
    """
    head = git(repository, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}', check=False).decode().strip() or None
    require(not git(repository, 'ls-files', '--unmerged'), 'changes', 'the index has unresolved conflicts')
    diff = ('diff', '--no-ext-diff', '--no-textconv', '--no-color', '--find-renames')
    staged = git(repository, *diff, '--cached')
    unstaged = git(repository, *diff)
    names = set()
    for args in (('--cached',), ()):
        # Git names are raw bytes: a non-UTF-8 name is shown with replacement characters.
        listed = git(repository, 'diff', '--no-ext-diff', '--name-only', '-z', *args).decode(errors='replace')
        names.update(item for item in listed.split('\x00') if item)
    raw = sorted(item for item in git(repository, 'ls-files', '--others', '--exclude-standard', '-z')
                 .split(b'\x00') if item)
    untracked = sorted(item.decode(errors='replace') for item in raw)
    staged_text, staged_cut = _bounded(staged, MAX_PATCH)
    unstaged_text, unstaged_cut = _bounded(unstaged, MAX_PATCH)
    content = digest({'staged': hashlib.sha256(staged).hexdigest(), 'unstaged': hashlib.sha256(unstaged).hexdigest(),
                      'untracked': _untracked_digest(repository, raw)})
    return {'head': head, 'staged': staged_text, 'unstaged': unstaged_text, 'untracked': untracked,
            'paths': sorted(names | set(untracked)), 'truncated': staged_cut or unstaged_cut, 'content': content}


def _identity(repository):
    name = git(repository, 'config', '--get', 'user.name', check=False).decode().strip()
    email = git(repository, 'config', '--get', 'user.email', check=False).decode().strip()
    return f'{name} <{email}>' if name and email else None


def _history(repository, revision='HEAD', exclude=None):
    """Recent human commits: the style evidence for commit messages."""
    spec = [revision] if exclude is None else [f'{exclude}..{revision}']
    raw = git(repository, 'log', f'-n{HISTORY}', '--no-color', '--format=%H%x1f%an%x1f%ae%x1f%B%x1e', *spec,
              check=False).decode('utf-8', 'replace')
    commits = []
    for record in raw.split('\x1e'):
        parts = record.strip('\n').split('\x1f')
        if len(parts) == 4 and not (_BOT.search(parts[1]) or _BOT.search(parts[2])):
            commits.append({'sha': parts[0], 'message': parts[3].strip()})
    return commits


def _target(snapshot):
    request = snapshot['request']
    return request['pr']['repository'] if request['pr'] else request['repository']


def collect(name, admitted, *, repository, capture=None, github=None, facts=(), issues=(), metadata=None,
            issue=None, policy=None, options=None, decisions_heading=None):
    """Read-only evidence for one draft; changes nothing in Git or on the remote.

    capture is the shared PR capture (dk-pr-description); github is the
    fail-closed provider used to read and verify labels, assignees and an edited
    issue (dk-issue); facts are verified {id, kind, text, url?} records whose
    kind is fact, validation or reproduction; issues are captured linked issues;
    metadata holds explicitly requested labels and assignees, which take
    precedence over the configured defaults; issue is the number of an issue to
    update; policy is verified commit-policy message text from commit cleanup.
    """
    snapshot = admitted['snapshot']
    governing = effective(name, admitted, options)
    values = governing['options']
    request = snapshot['request']
    sources = [_source('instruction:request', 'instruction', request['original_instructions'], public=False)]
    sources += _guidance(admitted, repository)
    sources += _facts(facts)
    evidence = {'entry': name, 'current': None, 'templates': [], 'references': set(), 'linked': set(),
                'urls': set(), 'decisions': None, 'identity': None, 'changes': None, 'metadata': None,
                'number': None, 'policy': policy is not None, 'notes': []}
    if request['pr']:
        evidence['references'].add(request['pr']['number'])
    for item in issues:
        require(type(item.get('number')) is int and isinstance(item.get('body', ''), (str, type(None))),
                'issues', 'every linked issue needs a number')
        if item['number'] in evidence['linked']:
            continue  # The same linked issue captured twice is one source.
        body = f"{item.get('title') or ''}\n\n{item.get('body') or ''}"
        sources.append(_source(f"issue:{item['number']}", 'issue', body, public=True, url=item.get('url')))
        evidence['linked'].add(item['number'])
        evidence['references'].add(item['number'])
    if name == 'dk-pr-description':
        pr = request['pr']
        require(isinstance(capture, dict), 'capture', 'dk-pr-description reads the PR capture')
        meta = capture.get('metadata') or {}
        require(str(meta.get('html_url', '')).lower() == pr['url'] and meta.get('number') == pr['number'],
                'capture.metadata', 'the capture belongs to another pull request')
        head, base = (meta.get('head') or {}).get('sha'), (meta.get('base') or {}).get('sha')
        for sha, label in ((head, 'head'), (base, 'base')):
            present = isinstance(sha, str) and _SHA.fullmatch(sha) and git(
                repository, 'rev-parse', '--verify', '--quiet', sha + '^{commit}', check=False).strip()
            require(present, 'changes', f'the PR {label} commit must be present in the checkout')
        patch, cut = _bounded(git(repository, 'diff', '--no-ext-diff', '--no-textconv', '--no-color',
                                  '--find-renames', f'{base}...{head}'), MAX_PATCH)
        require(patch.strip(), 'changes', 'the pull request has no changes to describe')
        sources.append(_source('diff', 'diff', patch, public=True, url=pr['url'] + '/files'))
        commits = _history(repository, head, exclude=base)
        sources.append(_source('commits', 'commits', '\n\n'.join(item['message'] for item in commits), public=True,
                               url=pr['url'] + '/commits'))
        current = {'title': meta.get('title') or '', 'body': meta.get('body') or ''}
        sources.append(_source('pr:title', 'pr', current['title'], public=True, url=pr['url']))
        sources.append(_source('pr:body', 'pr', current['body'], public=True, url=pr['url']))
        heading = decisions_heading or decisions.OPTIONS['section_heading']
        _, section, _ = decisions.split_section(current['body'], heading)
        evidence.update(current=current, decisions={'heading': heading, 'section': section},
                        changes={'head': head, 'base': base, 'truncated': cut})
        evidence['templates'] = templates(repository, name, values['template'])
    elif name == 'dk-commit-message':
        changes = local_changes(repository)
        require(changes['paths'], 'changes', 'there are no local changes to describe')
        sources.append(_source('diff:staged', 'diff', changes['staged'], public=False))
        sources.append(_source('diff:unstaged', 'diff', changes['unstaged'], public=False))
        sources.append(_source('untracked', 'diff', '\n'.join(changes['untracked']), public=False))
        sources.append(_source('history', 'history', '\n\n'.join(item['message'] for item in _history(repository)),
                               public=False))
        configured = git(repository, 'config', '--get', 'commit.template', check=False).decode().strip()
        if configured:
            try:
                data = admission.read_file(Path(repository).resolve(), admission.relative(configured, 'template'),
                                           'commit.template', MAX_SOURCE)
                sources.append(_source('commit-template', 'template', data, public=False, path=configured))
            except AdmissionError:
                evidence['notes'].append('commit.template outside the checkout was not read: ' + configured)
        if policy is not None:
            sources.append(_source('policy', 'policy', text(policy, 'policy'), public=False))
        evidence.update(changes=changes, identity=_identity(repository))
    else:
        owner, name_ = _target(snapshot).split('/')[1:]
        repo = f'repos/{owner}/{name_}'
        evidence['templates'] = templates(repository, name, values['template'])
        metadata = {} if metadata is None else metadata
        require(isinstance(metadata, dict) and set(metadata) <= {'labels', 'assignees'}, 'metadata',
                'only labels and assignees may be requested')
        chosen = {}
        for key, field in (('labels', 'name'), ('assignees', 'login')):
            explicit = key in metadata
            wanted = _names(metadata[key], 'metadata.' + key) if explicit else list(values[key])
            if issue is not None and not explicit:
                continue  # An update changes labels and assignees only when they are requested.
            if wanted:
                require(github is not None, 'github', 'labels and assignees are verified in the target repository')
                known = {item.get(field) for item in github.pages(f'{repo}/{key}?per_page=100')
                         if isinstance(item, dict)}
                missing = [item for item in wanted if item not in known]
                if missing and explicit:
                    raise Blocked('metadata.' + key, 'requested ' + key + ' do not exist in the target '
                                  'repository: ' + ', '.join(missing))
                if missing:
                    evidence['notes'].append(f'configured {key} not in the target repository were left out: '
                                             + ', '.join(missing))
                wanted = [item for item in wanted if item in known]
            chosen[key] = {'value': wanted, 'source': 'request' if explicit else 'configuration'}
        evidence['metadata'] = chosen
        if issue is not None:
            require(type(issue) is int and issue > 0, 'issue', 'must be an issue number')
            require(github is not None, 'github', 'an issue update reads the current issue')
            current = github.request('GET', f'{repo}/issues/{issue}')
            require(isinstance(current, dict) and current.get('number') == issue and 'pull_request' not in current,
                    'issue', f'#{issue} is not an issue of the target repository')
            evidence['current'] = {'title': current.get('title') or '', 'body': current.get('body') or ''}
            evidence['number'] = issue
            evidence['references'].add(issue)
            body = '{title}\n\n{body}'.format(**evidence['current'])
            # The edited issue may also be linked: its current text replaces the linked capture.
            sources = [item for item in sources if item['id'] != f'issue:{issue}']
            sources.append(_source(f'issue:{issue}', 'issue', body, public=True, url=current.get('html_url')))
    require(len(sources) <= MAX_ITEMS, 'sources', 'evidence exceeds the bounded source count')
    require(len({item['id'] for item in sources}) == len(sources), 'sources', 'duplicate source identity')
    for item in sources:
        # References a draft may make: any issue or PR the evidence itself names.
        evidence['references'].update(int(number) for number in _REF.findall(item['text']))
        evidence['urls'].update(match[0] for match in _URL_REF.finditer(item['text']))
        if item['url']:
            evidence['urls'].add(item['url'])
    evidence['sources'] = sources
    evidence['constraints'] = prose.constraints([request['original_instructions']])
    return evidence


# -- Result checks ------------------------------------------------------------------------------

def _labels(body):
    """Section names a draft uses: Markdown headings and leading `Name:` labels outside code."""
    plain = _FENCE.sub('\n', body)
    return headings(body) + [match[1].strip() for match in _LABEL.finditer(plain)]


def _check_sections(names, cited, where, kept=()):
    """Validation and reproduction sections need cited evidence of that kind; kept sections are the author's."""
    kinds = {source['kind'] for source in cited}
    kept = {label.casefold() for label in kept}
    for label in names:
        if label.casefold() in kept:
            continue
        if VALIDATION.fullmatch(label.strip()) and 'validation' not in kinds:
            raise Blocked(where, f'"{label}" claims validation without cited validation evidence')
        if REPRODUCTION.fullmatch(label.strip()) and 'reproduction' not in kinds:
            raise Blocked(where, f'"{label}" claims reproduction without cited reproduction evidence')


def _settled(section):
    """Section lines without their trailing blank lines, which only the end of the body decides."""
    lines = list(section or [])
    while lines and not lines[-1].strip():
        lines.pop()
    return lines if section is not None else None


def _check_text(body, evidence, where, *, public):
    plain = _strip_code(body)
    for number in _REF.findall(plain):
        if int(number) not in evidence['references']:
            raise Blocked(where, f'#{number} is not named by any evidence')
    for match in _URL_REF.finditer(plain):
        if match[0] not in evidence['urls']:
            raise Blocked(where, match[0] + ' is not named by any evidence')
    texts = '\n'.join(item['text'] for item in evidence['sources'])
    for found in _QUALIFIED_REF.findall(plain):
        if found not in texts:
            raise Blocked(where, found + ' is not named by any evidence')
    for number in _CLOSING.findall(plain):
        if int(number) not in evidence['linked']:
            raise Blocked(where, f'a closing keyword names #{number}, which is not a linked issue')
    existing = (evidence['current'] or {}).get('body') or ''  # Text the author already published.
    for found in _CLOSING_ELSEWHERE.finditer(plain):
        if found[0] not in existing:
            raise Blocked(where, f'"{found[0]}" closes an issue that is not written as a linked #N')
    if _ATTRIBUTION.search(body):
        raise Blocked(where, 'drafts carry no generated-by banner or tool attribution')
    for kind, ref in _CODE_LINK.findall(plain):
        if not _SHA.fullmatch(ref):
            raise Blocked(where, 'code links must be pinned to a full commit SHA, not ' + ref)
    if public:
        for found in _PATH_LINE.finditer(plain):
            if found[0] not in existing:
                raise Blocked(where, found[0] + ' is not usable in a browser; use a SHA-pinned permalink')
        stray = [item for item in re.findall(r'<!--.*?-->', body, re.S)
                 if not _MARKER.fullmatch(item) and item not in existing]
        if stray:
            raise Blocked(where, 'template comments and placeholders are left out of the draft')


def _check_layout(body, output, evidence, governing):
    """The governing template's sections, or the fallback sections, in order; omissions need a reason."""
    candidates = {item['path']: item for item in evidence['templates']}
    chosen = output['template']
    if candidates:
        require(chosen in candidates, 'template', 'the draft must follow one of the repository templates: '
                + ', '.join(sorted(candidates)))
        expected = candidates[chosen]['headings']
    else:
        require(chosen is None, 'template', 'no repository template governs; use the fallback sections')
        expected = governing['options']['fallback_sections']
    omitted = {}
    for number, item in enumerate(_items(output['omitted'], 'omitted', ('section', 'reason'))):
        text(item['reason'], f'omitted[{number}].reason', 1024)
        omitted[text(item['section'], f'omitted[{number}].section', 256).casefold()] = item['reason']
    present = [heading.casefold() for heading in headings(body)]
    positions = []
    for section in expected:
        key = section.casefold()
        if key in present:
            positions.append(present.index(key))
        else:
            require(key in omitted, 'body', f'the "{section}" section is missing and not reported as omitted')
    require(positions == sorted(positions), 'body', 'sections must follow the governing order')


def _items(value, path, required, optional=()):
    require(isinstance(value, list) and len(value) <= MAX_ITEMS, path, 'must be a bounded array')
    for number, item in enumerate(value):
        fields(item, f'{path}[{number}]', required, optional)
    return value


def _check_message(message, evidence, governing):
    lines = message.split('\n')
    require(lines[0].strip() and lines[0] == lines[0].strip(), 'message', 'the subject must be one nonempty line')
    require(len(lines) == 1 or lines[1] == '', 'message', 'a blank line separates the subject from the body')
    require(not any(line.startswith('#') for line in lines), 'message', 'Git strips lines that start with #')
    if not evidence['policy']:  # Explicit policy replaces the stylistic fallbacks.
        values = governing['options']
        require(len(lines[0]) <= values['subject_target_length'], 'message',
                f"the subject exceeds {values['subject_target_length']} characters")
        for line in lines[2:]:
            if _TRAILER.fullmatch(line) or '://' in line or line.startswith((' ', '\t')):
                continue
            require(len(line) <= values['body_wrap_length'], 'message',
                    f"body prose wraps at {values['body_wrap_length']} characters")
        require(all(len(line) <= values['max_line_length'] or '://' in line for line in lines), 'message',
                f"message lines stay within {values['max_line_length']} characters")
    texts = '\n'.join(item['text'] for item in evidence['sources'])
    for kind, who in _TRAILER.findall(message):
        if kind.lower() == 'signed-off-by':
            require(who == evidence['identity'], 'message', 'Signed-off-by must name the configured Git identity')
        else:
            require(who in texts, 'message', 'a co-author must be a person the evidence names: ' + who)


def check_result(name, output, evidence, governing):
    """The accepted draft with its final fields, or Blocked; unsupported claims are never recorded."""
    expected = {'dk-pr-description': ('title', 'body', 'template', 'omitted', 'claims', 'questions', 'summary'),
                'dk-commit-message': ('message', 'scope', 'claims', 'questions', 'summary'),
                'dk-issue': ('title', 'body', 'template', 'omitted', 'claims', 'questions', 'summary')}[name]
    fields(output, 'result', expected)
    require(set(output) == set(expected), 'result', 'unsupported result fields')
    text(output['summary'], 'summary', 4096)
    known = {item['id']: item for item in evidence['sources']}
    cited = []
    for number, claim in enumerate(_items(output['claims'], 'claims', ('text', 'sources'))):
        text(claim['text'], f'claims[{number}].text', 4096)
        require(isinstance(claim['sources'], list) and claim['sources'], f'claims[{number}].sources',
                'every claim cites its evidence')
        for ident in claim['sources']:
            require(ident in known, f'claims[{number}].sources', f'unknown source {ident}')
            cited.append(known[ident])
    require(output['claims'], 'claims', 'a draft states its evidence-backed claims')
    require(isinstance(output['questions'], list) and all(isinstance(item, str) and item.strip()
                                                         for item in output['questions']), 'questions',
            'must be a list of questions')
    values = governing['options']
    if name == 'dk-commit-message':
        message = output['message']
        require(isinstance(message, str) and message.strip(), 'message', 'must be text')
        message = message.strip('\n')
        _check_message(message, evidence, governing)
        # The subject is a summary, not a section: a conventional `test: ...` subject claims nothing.
        _check_sections(_labels(message.partition('\n')[2]), cited, 'message')
        _check_text(message, evidence, 'message', public=False)
        scope = fields(output['scope'], 'scope', ('included', 'excluded'))
        included = _names(scope['included'], 'scope.included')
        excluded = _items(scope['excluded'], 'scope.excluded', ('path', 'reason'))
        for number, item in enumerate(excluded):
            text(item['reason'], f'scope.excluded[{number}].reason', 1024)
        paths = included + [item['path'] for item in excluded]
        require(included, 'scope.included', 'the message describes at least one selected path')
        require(sorted(paths) == evidence['changes']['paths'], 'scope',
                'every changed path is included or excluded with a reason, and no other path')
        final = {'message': message, 'scope': {'included': sorted(included),
                                               'excluded': sorted(excluded, key=lambda item: item['path'])}}
        combined, writable = message, []
    else:
        current = evidence['current']
        title, body = output['title'], output['body']
        fixed = {'title': name == 'dk-pr-description' and not values['update_title'],
                 'body': name == 'dk-pr-description' and not values['update_body']}
        for key, value in (('title', title), ('body', body)):
            if fixed[key]:
                require(value is None, key, f'update_{key} is disabled; the current {key} is kept')
            else:
                require(isinstance(value, str) and value.strip() and '\x00' not in value, key, 'must be text')
        title = current['title'] if fixed['title'] else title
        body = current['body'] if fixed['body'] else body.strip('\n') + '\n'
        require('\n' not in title and len(title) <= 256, 'title', 'must be one bounded line')
        if not fixed['title']:
            _check_text(title, evidence, 'title', public=True)
        if not fixed['body']:
            _check_layout(body, output, evidence, governing)
            _check_sections(_labels(body), cited, 'body', _labels(current['body']) if current else ())
            _check_text(body, evidence, 'body', public=True)
        if evidence['decisions']:
            # dk-pr-decisions owns the bounded section; a description keeps it verbatim and never adds one.
            _, section, _ = decisions.split_section(body, evidence['decisions']['heading'])
            require(_settled(section) == _settled(evidence['decisions']['section']), 'body',
                    'the current Decisions section is kept verbatim; dk-pr-decisions changes it')
        final = {'title': title, 'body': body}
        if name == 'dk-issue':
            final.update(number=evidence['number'],
                         **{key: value['value'] for key, value in evidence['metadata'].items()},
                         metadata={key: value['source'] for key, value in evidence['metadata'].items()})
        combined, writable = title + '\n' + body, [key for key in ('title', 'body') if not fixed[key]]
    for phrase in evidence['constraints']:
        require(phrase in combined, 'constraints', f'the exact requested wording is missing: "{phrase}"')
    # The fields the draft may write and the values it saw: publication refuses a field edited since.
    return {'draft': final, 'fields': writable, 'seen': copy.deepcopy(evidence['current']),
            'template': output.get('template'), 'omitted': output.get('omitted', []),
            'claims': output['claims'], 'questions': output['questions'], 'summary': output['summary']}


# -- Ledger -------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim TEXT NOT NULL UNIQUE,
    entry TEXT NOT NULL,
    binding TEXT NOT NULL,
    current TEXT NOT NULL,
    session TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE rejections (
    claim TEXT PRIMARY KEY,
    entry TEXT NOT NULL,
    binding TEXT NOT NULL,
    session TEXT,
    error TEXT NOT NULL
)
"""


def ledger_path(state_directory, logical_run):
    """The logical run's drafting record next to its run ledger; run text never forms a path."""
    name = hashlib.sha256(text(logical_run, 'logical_run').encode()).hexdigest()
    return Path(state_directory) / 'runs' / (name + '.drafting.sqlite')


class Ledger:
    """Append-only drafts of one logical run and target; history is never rewritten."""

    def __init__(self, path, *, logical_run, target):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not self.path.is_symlink(), 'ledger', 'drafting ledger must not be a symlink')
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        expected = {'suite': admission.SUITE, 'kind': KIND, 'logical_run': logical_run, 'target': target.lower()}
        try:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = self.db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                if version == 0 and tables == 0:
                    for statement in SCHEMA.split(';'):
                        self.db.execute(statement)
                    self.db.executemany('INSERT INTO meta VALUES (?, ?)', sorted(expected.items()))
                    self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                    version = SCHEMA_VERSION
                require(version == SCHEMA_VERSION, 'ledger', f'unsupported drafting ledger version {version}')
                meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
                require(meta == expected, 'ledger', 'drafting ledger belongs to another suite, logical run or target')
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        except (AdmissionError, sqlite3.DatabaseError):
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @staticmethod
    def _record(row):
        if row is None:
            return None
        value = dict(row)
        value['value'] = json.loads(value['value'])
        return value

    def latest(self, entry):
        return self._record(self.db.execute('SELECT * FROM results WHERE entry = ? ORDER BY seq DESC LIMIT 1',
                                            (entry,)).fetchone())

    def claimed(self, claim):
        return self._record(self.db.execute('SELECT * FROM results WHERE claim = ?', (claim,)).fetchone())

    def attempts(self, binding):
        return self.db.execute('SELECT count(*) FROM rejections WHERE binding = ?', (binding,)).fetchone()[0]

    def reject(self, *, claim, entry, binding, session, error):
        """Retain why a claim's result was not accepted; the retained claim is never redispatched."""
        self.db.execute('INSERT OR IGNORE INTO rejections VALUES (?, ?, ?, ?, ?)',
                        (claim, entry, binding, session, admission.canonical(error).decode()))

    def record(self, *, claim, entry, binding, current, session, result_ref, value):
        row = (claim, entry, binding, current, session, result_ref, admission.canonical(value).decode())
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT * FROM results WHERE claim = ?', (claim,)).fetchone()
            if old is None:
                self.db.execute('INSERT INTO results (claim, entry, binding, current, session, result_ref, value) '
                                'VALUES (?, ?, ?, ?, ?, ?, ?)', row)
            elif tuple(old)[1:] != row:
                raise Blocked('ledger', 'the claim already holds another result')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.claimed(claim)


def source_validator(ledger, name):
    """Publication-source check: only the entry's current retained draft is accepted evidence."""
    def valid(source):
        latest = ledger.latest(name)
        return latest is not None and source == 'drafting:' + latest['result_ref']
    return valid


# -- Drafting -----------------------------------------------------------------------------------

def _context(name, admitted, task, governing, evidence):
    snapshot = admitted['snapshot']
    return digest({'entry': name,
                   'task': {key: task[key] for key in ('adapter', 'definition', 'definition_sha256',
                                                        'assignment_sha256', 'instructions')},
                   'governing': governing, 'instructions': snapshot['request']['original_instructions'],
                   'requirements': [item['text'] for item in snapshot['options']['requirements']['value']],
                   'prohibitions': [item['text'] for item in snapshot['options']['prohibitions']['value']],
                   'agent': semantic.validator_agent(admitted),
                   'evidence': {key: evidence[key] for key in ('templates', 'decisions', 'identity', 'metadata',
                                                               'number', 'policy', 'notes')},
                   'changes': evidence['changes']})


def _sessionable(evidence):
    """The evidence the session receives; the private request stays private but governs."""
    return {'sources': copy.deepcopy(evidence['sources']),
            'templates': [{key: item[key] for key in ('path', 'kind', 'text', 'headings')}
                          for item in evidence['templates']],
            'decisions': copy.deepcopy(evidence['decisions']), 'current': copy.deepcopy(evidence['current']),
            'changed_paths': (evidence['changes'] or {}).get('paths'), 'identity': evidence['identity'],
            'metadata': copy.deepcopy(evidence['metadata']), 'number': evidence['number'],
            'notes': list(evidence['notes'])}


def assignment(name, admitted, task, governing, evidence, *, claim):
    """The exact typed assignment of the adapter's engine-managed native session."""
    snapshot = admitted['snapshot']
    agent, provider = semantic.validator_agent(admitted)
    definition = snapshot['adapters'][task['adapter']]
    readable = {f"repository:{item['path']}": item['sha256'] for item in evidence['templates']}
    readable.update({f"repository:{item['path']}": item['sha256'] for item in evidence['sources'] if 'path' in item
                     and item['kind'] == 'guidance'})
    return {
        'schema_version': 1, 'kind': 'drafting_task', 'suite': admission.SUITE, 'skill': ENTRIES[name]['skill'],
        'mode': name, 'claim': claim, 'adapter': task['adapter'], 'task': task['id'], 'role': task['definition'],
        'definition_sha256': task['definition_sha256'], 'assignment': definition['assignment'],
        'checklist': definition['checklist'], 'task_instructions': task['instructions'],
        'role_prompt': governing['role_prompt'], 'additions': governing['additions'],
        'options': governing['options'], 'instructions': snapshot['request']['original_instructions'],
        'constraints': list(evidence['constraints']),
        'requirements': [item['text'] for item in snapshot['options']['requirements']['value']],
        'prohibitions': [item['text'] for item in snapshot['options']['prohibitions']['value']],
        'agent': agent, 'provider': provider, 'skills': [], 'tools': 'read-only', 'writes': False,
        'publication': False, 'readable': readable, 'context': _sessionable(evidence),
    }


def _report(name, row, evidence, *, session):
    value = row['value']
    cited = sorted({ident for claim in value['claims'] for ident in claim['sources']})
    known = {item['id']: item for item in evidence['sources']}
    return {'skill': ENTRIES[name]['skill'], 'status': 'drafted', 'result_ref': row['result_ref'],
            'claim': row['claim'], 'session': session, 'draft': value['draft'], 'fields': value['fields'],
            'seen': value['seen'], 'template': value['template'],
            'omitted': value['omitted'], 'claims': value['claims'], 'questions': value['questions'],
            'summary': value['summary'], 'notes': list(evidence['notes']),
            'sources': [{key: known[ident].get(key) for key in ('id', 'kind', 'public', 'url')}
                        for ident in cited if ident in known]}


def _binding(name, admitted, task, governing, evidence, ledger):
    """(binding, current, latest): what the retained draft must match to stay current."""
    context = _context(name, admitted, task, governing, evidence)
    current = digest(evidence['current'])
    latest = ledger.latest(name)
    if latest is not None and evidence['current'] is not None and all(
            latest['value']['draft'].get(key) == value for key, value in evidence['current'].items()):
        current = latest['current']  # Publishing the draft does not invalidate the draft it came from.
    fixed = {item['id']: item['sha256'] for item in evidence['sources']
             if item['id'] not in ('pr:title', 'pr:body') and not (evidence['number'] and
                                                                  item['id'] == f"issue:{evidence['number']}")}
    return digest({'context': context, 'sources': fixed, 'current': current}), current, latest


def current_report(name, admitted, *, repository, ledger, options=None, **inputs):
    """The retained draft's report while its evidence and governing inputs are unchanged, else None.

    Read-only and sessionless: a committing procedure uses it to refuse a stale
    message instead of drafting again.
    """
    task = occurrence(name, admitted)
    governing = effective(name, admitted, options)
    evidence = collect(name, admitted, repository=repository, options=options, **inputs)
    binding, _, latest = _binding(name, admitted, task, governing, evidence, ledger)
    return _report(name, latest, evidence, session=False) if latest and latest['binding'] == binding else None


def draft(name, admitted, *, repository, ledger, transport, options=None, **inputs):
    """Run (or reuse) one draft of the entry's adapter occurrence and return its private report.

    admitted is the entry's preflight result or a composed shared plan that selects
    the adapter. inputs are those of collect(). transport offers native_session(task).
    Unchanged evidence and governing inputs reuse the retained draft with no
    session, also after its own publication changed the current title or body. A
    lost session response is reconciled through its claim. Writes nothing to Git
    or the remote.
    """
    skill = ENTRIES[name]['skill']
    try:
        require(admitted.get('preflight') == 'admitted', 'admission', 'drafting needs an admitted run')
        task = occurrence(name, admitted)
        governing = effective(name, admitted, options)
    except AdmissionError as error:
        return {'skill': skill, 'status': 'rejected', 'error': _failure(error)}
    try:
        evidence = collect(name, admitted, repository=repository, options=options, **inputs)
    except (AdmissionError, configuration.Blocked) as error:
        return {'skill': skill, 'status': 'blocked', 'error': _failure(error)}
    except Exception as error:
        return {'skill': skill, 'status': 'blocked', 'error': {'path': 'evidence', 'message':
                                                               'evidence unavailable: ' + type(error).__name__}}
    binding, current, latest = _binding(name, admitted, task, governing, evidence, ledger)
    if latest is not None and latest['binding'] == binding:
        return _report(name, latest, evidence, session=False)
    rejected = ledger.attempts(binding)
    allowed = 1 + admitted['snapshot']['limits']['retries_per_task']
    if rejected >= allowed:
        return {'skill': skill, 'status': 'blocked', 'error': {
            'path': 'attempts', 'message': f'{rejected} rejected drafts for unchanged inputs; change the inputs '
                                           'or grant a continuation'}}
    run = admitted['snapshot']['request']['logical_run']
    # The retained draft it supersedes is part of the claim: inputs that return to an earlier state draft anew.
    after = latest['claim'] if latest is not None else None
    claim = 'draft-' + digest({'run': run, 'entry': name, 'binding': binding, 'after': after})[:32] + (
        '' if rejected == 0 else f'-r{rejected}')
    known = ledger.claimed(claim)
    if known is not None:
        return _report(name, known, evidence, session=False)
    task_record = assignment(name, admitted, task, governing, evidence, claim=claim)
    try:
        reference, session_id, output = configuration.run_session(transport, task_record)
    except configuration.Blocked as error:
        if error.path == 'session':  # Lost or failed dispatch: the same claim reconciles it later.
            return {'skill': skill, 'status': 'blocked', 'claim': claim, 'error': _failure(error)}
        session_id, failure = None, _failure(error)
    else:
        try:
            value = check_result(name, output, evidence, governing)
            failure = None
        except (AdmissionError, configuration.Blocked) as error:
            failure = _failure(error)
        except Exception as error:
            failure = {'path': 'result', 'message': 'not accepted: ' + type(error).__name__}
    if failure is not None:
        ledger.reject(claim=claim, entry=name, binding=binding, session=session_id, error=failure)
        return {'skill': skill, 'status': 'blocked', 'claim': claim, 'error': failure}
    row = ledger.record(claim=claim, entry=name, binding=binding, current=current, session=session_id,
                        result_ref=reference, value=value)
    return _report(name, row, evidence, session=True)


def handoff(report):
    """What a composed caller consumes, bound to the accepted draft; splitting.commit() commits it."""
    return {'result_ref': report['result_ref'], **copy.deepcopy(report['draft'])}


# -- Publication --------------------------------------------------------------------------------

def publication_plan(name, admitted):
    """The shared publication plan for this occurrence: only the entry's actions, and only when effective."""
    snapshot = admitted['snapshot']
    request = snapshot['request']
    actions = ENTRIES[name]['actions']
    enabled = {action: snapshot['permissions'][action]['effective'] for action in actions}
    plan = {'logical_run': request['logical_run'], 'instructions': request['original_instructions'],
            'authorizations': {action: request['authorizations'][action] for action in actions if enabled[action]},
            'options': {'permissions': enabled}}
    if request['pr']:
        pr = request['pr']
        host, owner, repo = pr['repository'].split('/')
        plan['pr'] = {'url': pr['url'], 'host': host, 'owner': owner, 'name': repo, 'repository': pr['repository'],
                      'number': pr['number']}
    else:
        host, owner, repo = request['repository'].split('/')
        plan.update(pr=None, repository={'host': host, 'owner': owner, 'name': repo})
    return plan


def _stale(report, live):
    """Fields the draft would write that someone changed after the draft read them."""
    return [key for key in report['fields'] if (live.get(key) or '') != (report['seen'].get(key) or '')]


def propose(name, admitted, report, *, capture=None, current=None):
    """The exact proposal for an accepted draft, or why there is none. Writes nothing.

    capture is the live PR capture (dk-pr-description); current is the live
    issue an update targets (dk-issue). A draft writes only its enabled fields,
    and only while each still holds the value the draft saw; otherwise it is
    stale and must be drafted again from the new text.
    """
    snapshot = admitted['snapshot']
    permissions = snapshot['permissions']
    final = report['draft']
    ident = name.split('-')[-1] + '-' + digest({'run': snapshot['request']['logical_run'],
                                                 'result': report['result_ref']})[:32]
    source = ['drafting:' + report['result_ref']]
    if name == 'dk-commit-message':
        return {'status': 'not_published', 'id': None, 'draft': final}
    if name == 'dk-pr-description':
        if not permissions['edit_pr']['effective']:
            return {'status': 'not_authorized', 'id': ident, 'draft': final}
        meta = capture['metadata']
        changed = _stale(report, meta)
        if changed:
            return {'status': 'stale', 'id': ident, 'draft': final, 'error': {
                'path': 'publication', 'message': 'changed since the draft: ' + ', '.join(changed)}}
        payload = {key: final[key] for key in report['fields'] if final[key] != (meta.get(key) or '')}
        if not payload:
            return {'status': 'unchanged', 'id': ident, 'draft': final}
        return {'status': 'proposed', 'id': ident, 'draft': final, 'proposal': {
            'id': ident, 'kind': 'edit_pr', 'head': meta['head']['sha'], 'target': meta['base']['sha'],
            'payload': payload, 'sources': source}}
    kind = 'edit_issue' if final['number'] else 'create_issue'
    if not permissions[kind]['effective']:
        return {'status': 'not_authorized', 'id': ident, 'draft': final}
    if kind == 'create_issue':
        payload = {key: final[key] for key in ('title', 'body', 'labels', 'assignees')}
    else:
        require(isinstance(current, dict) and current.get('number') == final['number'], 'current',
                'an issue update needs the live issue')
        changed = _stale(report, current)
        if changed:
            return {'status': 'stale', 'id': ident, 'draft': final, 'error': {
                'path': 'publication', 'message': f"#{final['number']} changed since the draft: "
                                                  + ', '.join(changed)}}
        payload = {'number': final['number'], 'title': final['title'], 'body': final['body'],
                   **{key: final[key] for key in ('labels', 'assignees') if key in final}}
    return {'status': 'proposed', 'id': ident, 'draft': final,
            'proposal': {'id': ident, 'kind': kind, 'payload': payload, 'sources': source}}


def publish(name, admitted, report, *, publisher, audience):
    """Publish an accepted draft through the shared exact-intent boundary when authorized.

    publisher is the shared publication helper (prfix.publication.Publication)
    built from publication_plan() with source_validator(ledger, name); audience(intent)
    returns the independent audience assessment of the exact intent. A pending
    intent is only reconciled, never resent; a blocked one is never retried here.
    """
    if report.get('status') != 'drafted' or not report.get('result_ref'):
        return {'status': 'blocked', 'error': {'path': 'report', 'message': 'no accepted draft'}}
    try:
        capture = publisher.provider.capture(publisher.pr) if name == 'dk-pr-description' else None
        number = report['draft'].get('number') if name == 'dk-issue' else None
        current = publisher.provider.request('GET', f'{publisher.repo}/issues/{number}') if number else None
        planned = propose(name, admitted, report, capture=capture, current=current)
    except Exception as error:
        return {'status': 'blocked', 'error': {'path': 'publication', 'message': str(error) or type(error).__name__}}
    retained = decisions._journal_state(publisher, planned['id']) if planned['id'] else None
    if retained in ('pending', 'applied'):
        # A dispatched intent is only reconciled from what the service shows, never sent again.
        try:
            receipt = publisher.execute(planned['id'], None)
        except Exception as error:
            return {'status': decisions._journal_state(publisher, planned['id']), 'draft': planned['draft'],
                    'intent': planned['id'], 'error': {'path': 'publication', 'message': str(error)}}
        return {'status': 'applied', 'draft': planned['draft'], 'intent': planned['id'], 'receipt': receipt}
    if planned['status'] != 'proposed':
        return planned
    intent = None
    try:
        intent = publisher.prepare(planned['proposal'])
        # The intent's baseline is what the service showed at preparation; it must be what the draft saw.
        before = intent['intent']['before']
        changed = [key for key in report['fields'] if key in before
                   and (before[key] or '') != (report['seen'].get(key) or '')]
        if changed:
            return {'status': 'stale', 'draft': planned['draft'], 'intent': intent['id'], 'error': {
                'path': 'publication', 'message': 'changed since the draft: ' + ', '.join(changed)}}
        receipt = publisher.execute(intent['id'], audience(intent))
    except Exception as error:
        # The journal, not the exception type, says whether the write may have happened.
        state = decisions._journal_state(publisher, intent['id']) if intent is not None else 'blocked'
        return {'status': {'prepared': 'blocked'}.get(state, state), 'draft': planned['draft'],
                'intent': intent['id'] if intent is not None else None,
                'error': {'path': 'publication', 'message': str(error) or type(error).__name__}}
    return {'status': 'applied', 'draft': planned['draft'], 'intent': intent['id'], 'receipt': receipt}
