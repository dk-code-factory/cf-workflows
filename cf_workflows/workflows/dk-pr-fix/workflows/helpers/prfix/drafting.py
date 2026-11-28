"""The drafting procedure of describe-pr, draft-commit-message and draft-issue (installed side).

cf-workflows:dk-pr-description, dk-commit-message and dk-issue are presets over
this workflow (workflows/description.yml, commit-message.yml, issue.yml), and a
composed plan may select describe-pr or draft-issue. Each occurrence runs
through the ordinary task path (task.yml, native.yml) as one engine-managed,
read-only session with the admitted leaf role and no skills. These helpers add
the drafting procedure to that path:

- `context` collects read-only evidence for the claim (assignment['drafting']):
  the PR's base...head diff and commits, or the local staged, unstaged and
  untracked changes; the governing templates or fallback sections; repository
  guidance; recent human commits; the current title and body and its Decisions
  section; and the labels and assignees verified in the target repository.
- `check` runs at acceptance: the report proposes no operations and returns its
  draft in deliverables.draft, which passes the library's result checks.
- `compose` lists the exact publication intent of a current accepted draft
  (edit_pr, create_issue or edit_issue) under an enabled and explicitly
  authorized permission, for the shared publication stage. dk-commit-message
  never publishes or commits.

The constants and result checks between the markers are a verbatim copy of
cf_workflows/drafting.py; tests/test_dk_pr_fix_drafting.py keeps the two
identical. Nothing here starts a model or writes to Git or the remote.
"""
import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

from . import decisions
from .core import GENERATED, digest, read_artifact

MAX_SOURCE = 64 << 10
MAX_PATCH = 256 << 10
MAX_ITEMS = 256
HISTORY = 20
PR_TEMPLATES = ('pull_request_template.md', '.github/pull_request_template.md', 'docs/pull_request_template.md')
ISSUE_TEMPLATES = ('issue_template.md', '.github/issue_template.md', 'docs/issue_template.md')
KIND = 'drafting_context'

# The baseline role prompts and options of each entry (cf_workflows/drafting.py ENTRIES).
ENTRIES = {
    'dk-pr-description': {
        'adapter': 'describe-pr', 'actions': ('edit_pr',),
        'options': {'template': None, 'update_title': True, 'update_body': True,
                    'fallback_sections': ['Issue', 'Fix']},
        'role_prompt': 'Read the complete diff, PR template, linked issue, maintainer conventions and current '
                       'Decisions. Use the configured repository-relative `template` when present; null uses '
                       'repository discovery. Apply `fallback_sections` only when repository formatting leaves a '
                       'choice. Honor `update_title` and `update_body`, preserving each disabled field. Describe the '
                       'resulting behavior and validation. Preserve Decisions and unrelated body sections. Make the '
                       'description match the agreed scope before review.\n',
    },
    'dk-commit-message': {
        'adapter': 'draft-commit-message', 'actions': (),
        'options': {'subject_target_length': 72, 'body_wrap_length': 72, 'max_line_length': 80},
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
        'adapter': 'draft-issue', 'actions': ('create_issue', 'edit_issue'),
        'options': {'template': None, 'labels': [], 'assignees': [], 'fallback_sections': ['Problem',
                                                                                           'Expected behavior']},
        'role_prompt': 'Draft, create or update GitHub issues using repository templates and conventions.\n',
    },
}
BY_ADAPTER = {entry['adapter']: name for name, entry in ENTRIES.items()}
# Legacy selector names of the baseline adapters (prepare.TASK_ALIASES).
ALIASES = {'dk-workflows:dk-pr-description': 'describe-pr'}
CONTRACT = (
    'Return the draft in deliverables.draft and propose no operations: the helper derives the one exact '
    'publication intent from the accepted draft and publishes it only under separate authority. Do not write, '
    'commit or publish. A description or issue draft is {title, body, template, omitted: [{section, reason}], '
    'claims: [{text, sources}], questions, summary}; a disabled title or body is null. A commit message draft is '
    '{message, scope: {included, excluded: [{path, reason}]}, claims, questions, summary}. Every claim cites '
    'evidence source IDs from drafting.evidence.sources. A validation or testing section cites a validation '
    'source and a reproduction section a reproduction source. Name only issues and URLs the evidence names, pin '
    'code links to a full commit SHA, use no path:line reference, template comment or tool attribution, follow '
    'the governing template (name it in template) or the fallback sections, keep every quoted phrase of the '
    'request verbatim and keep the current Decisions section unchanged.')


class Blocked(ValueError):
    """The evidence or the draft cannot be used; nothing is published."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


def require(condition, path, message):
    if not condition:
        raise Blocked(path, message)


def text(value, path, limit=MAX_SOURCE):
    require(isinstance(value, str) and value.strip() and '\0' not in value,
            path, 'must be a nonempty string without NUL')
    try:
        size = len(value.encode('utf-8', 'strict'))
    except UnicodeEncodeError:
        raise Blocked(path, 'must be valid Unicode text') from None
    require(size <= limit, path, 'exceeds byte bound')
    return value


def fields(value, path, required, optional=()):
    require(isinstance(value, dict), path, 'must be an object')
    unknown = set(value) - set(required) - set(optional)
    require(not unknown, path, 'unsupported field(s): ' + ', '.join(sorted(map(str, unknown))))
    missing = set(required) - set(value)
    require(not missing, path, 'missing field(s): ' + ', '.join(sorted(missing)))
    return value


# -- Verbatim copy of cf_workflows/drafting.py (constants and result checks) -------------------

_HEADING = re.compile(r' {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[`~]*[ \t]*$', re.M | re.S)
_CODE = re.compile(r'(`+)(?:(?!\1).)+?\1', re.S)
_LABEL = re.compile(r'^\s*(?:[-*]\s+)?\**([A-Za-z][A-Za-z ]{0,30}?)\**\s*:', re.M)
VALIDATION = re.compile(r'(?:test(?:s|ing|ed| plan)?|validation|validated|verification|verified|how to test|'
                        r'checks? run)$', re.I)
REPRODUCTION = re.compile(r'(?:steps to reproduce|reproduction(?: steps)?|to reproduce|repro(?:duce)?)$', re.I)
_REF = re.compile(r'(?<![\w/&#])#([1-9][0-9]{0,15})\b')
_URL_REF = re.compile(r'https://([A-Za-z0-9.-]+)/([\w.-]+)/([\w.-]+)/(?:issues|pull)/([1-9][0-9]*)')
_CLOSING = re.compile(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#([1-9][0-9]{0,15})\b', re.I)
_CODE_LINK = re.compile(r'https://[A-Za-z0-9.-]+/[\w.-]+/[\w.-]+/(blob|tree)/([^/\s)>\]]+)')
_PATH_LINE = re.compile(r'(?<![\w/.:#-])(?:[\w.-]+/)*[\w-]+\.[A-Za-z0-9]{1,8}:[1-9][0-9]*\b')
_ATTRIBUTION = re.compile(r'generated (?:by|with)\b|\U0001F916|co-authored-by:[^\n]*\b(?:claude|codex|chatgpt|'
                          r'gpt-?\d|copilot|anthropic|openai)\b', re.I)
_TRAILER = re.compile(r'^(signed-off-by|co-authored-by):[ \t]*(.+?)[ \t]*$', re.I | re.M)
_MARKER = re.compile(r'<!-- cf-workflows:decision [a-z0-9][a-z0-9-]{0,63} -->')
_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
_BOT = re.compile(r'\[bot\]|^dependabot|^renovate|noreply@github\.com$', re.I)


def _names(value, where):
    require(isinstance(value, list) and len(value) <= 100 and len(set(map(str, value))) == len(value),
            where, 'must be a bounded list of distinct names')
    for number, item in enumerate(value):
        text(item, f'{where}[{number}]', 256)
    return value


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


def headings(body):
    """Markdown heading titles outside fenced code, in order."""
    found, fence = [], None
    for raw in body.split('\n'):
        line = raw.rstrip('\r')
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            fence = None if fence and stripped.startswith(fence) else (fence or stripped[:3])
            continue
        match = None if fence else _HEADING.fullmatch(line)
        if match:
            found.append(match[2].strip())
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
    for number in _CLOSING.findall(plain):
        if int(number) not in evidence['linked']:
            raise Blocked(where, f'a closing keyword names #{number}, which is not a linked issue')
    if _ATTRIBUTION.search(body):
        raise Blocked(where, 'drafts carry no generated-by banner or tool attribution')
    for kind, ref in _CODE_LINK.findall(plain):
        if not _SHA.fullmatch(ref):
            raise Blocked(where, 'code links must be pinned to a full commit SHA, not ' + ref)
    if public:
        existing = (evidence['current'] or {}).get('body') or ''  # Text the author already published.
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

# -- End of the verbatim copy -------------------------------------------------------------------

def role(task):
    """The drafting entry whose adapter this occurrence runs, or None."""
    skill = task.get('skill')
    return BY_ADAPTER.get(ALIASES.get(skill, skill))


def _relative(value, path):
    value = text(value, path, 4096)
    parts = value.split('/')
    require(not value.startswith('/') and not any(part in ('', '.', '..') for part in parts),
            path, 'must be a normalized relative path inside the repository')
    return value


def settings(plan, name):
    """(options, requested) of an occurrence from ``settings.drafting[name]``; baseline values by default.

    Options follow the entry's config.schema.json. dk-issue also takes the
    explicitly requested labels and assignees (``metadata``) and the number of an
    issue to update (``issue``).
    """
    raw = (plan.get('settings') or {}).get('drafting', {})
    require(isinstance(raw, dict) and set(raw) <= set(ENTRIES), 'settings.drafting',
            'supports ' + ', '.join(sorted(ENTRIES)) + ' only')
    given = raw.get(name, {})
    where = 'settings.drafting.' + name
    declared = ENTRIES[name]['options']
    extra = ('metadata', 'issue') if name == 'dk-issue' else ()
    require(isinstance(given, dict) and set(given) <= set(declared) | set(extra), where,
            'supports the entry options' + (', metadata and issue' if extra else '') + ' only')
    values = copy.deepcopy(declared)
    for key, value in given.items():
        path = where + '.' + key
        if key == 'template':
            require(value is None or isinstance(value, str), path, 'must be a repository-relative path or null')
            if value is not None:
                _relative(value, path)
        elif key in ('update_title', 'update_body'):
            require(type(value) is bool, path, 'must be boolean')
        elif key in ('labels', 'assignees', 'fallback_sections'):
            _names(value, path)
        elif key in declared:
            require(type(value) is int and 1 <= value <= 1000, path, 'must be a positive integer')
        if key in declared:
            values[key] = copy.deepcopy(value)
    requested = {'metadata': None, 'issue': None}
    if 'metadata' in given:
        metadata = given['metadata']
        require(isinstance(metadata, dict) and set(metadata) <= {'labels', 'assignees'}, where + '.metadata',
                'only labels and assignees may be requested')
        for key, value in metadata.items():
            _names(value, where + '.metadata.' + key)
        requested['metadata'] = copy.deepcopy(metadata)
    if 'issue' in given:
        require(type(given['issue']) is int and given['issue'] > 0, where + '.issue', 'must be an issue number')
        requested['issue'] = given['issue']
    return values, requested


def _read(root, rel, path):
    """A bounded regular file below root; no component may be a symlink."""
    current = Path(root)
    for part in rel.split('/'):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except (FileNotFoundError, NotADirectoryError):
            raise Blocked(path, 'missing: ' + rel) from None
        require(not stat.S_ISLNK(mode), path, 'must not traverse a symlink: ' + rel)
    require(stat.S_ISREG(mode), path, 'not a regular file: ' + rel)
    descriptor = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        content = stream.read(MAX_SOURCE + 1)
    require(len(content) <= MAX_SOURCE, path, 'exceeds byte bound: ' + rel)
    return content


def _template(root, rel):
    data = _read(root, rel, 'template').decode('utf-8', 'replace')
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
        except Blocked as error:
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


def _guidance(root, paths):
    """AGENTS.md at the root and above every changed path: the instruction files capture binds."""
    found = {'AGENTS.md'}
    for changed in paths or ():
        parts = changed.split('/')[:-1]
        found.update('/'.join(parts[:depth] + ['AGENTS.md']) for depth in range(1, len(parts) + 1))
    result = []
    for rel in sorted(found):
        path = Path(root) / rel
        if path.is_file() and not path.is_symlink():
            result.append(_source('guidance:' + rel, 'guidance', _read(root, rel, 'guidance'), public=True,
                                  path=rel))
    return result


def _repo(plan):
    return 'repos/' + plan['pr']['owner'] + '/' + plan['pr']['name']


def context(root, provider, plan, task, snapshot):
    """The drafting context of a new claim (assignment['drafting']); reads only, writes nothing."""
    name = role(task)
    values, requested = settings(plan, name)
    pr = plan['pr']
    request = plan['instructions']
    sources = [_source('instruction:request', 'instruction', request, public=False)]
    sources += _guidance(root, snapshot.get('paths'))
    evidence = {'entry': name, 'current': None, 'templates': [], 'references': {pr['number']}, 'linked': set(),
                'urls': set(), 'decisions': None, 'identity': None, 'changes': None, 'metadata': None,
                'number': None, 'policy': False, 'notes': []}
    if name == 'dk-pr-description':
        meta = provider.capture(pr).get('metadata') or {}
        require(str(meta.get('html_url', '')).lower() == pr['url'] and meta.get('number') == pr['number'],
                'capture.metadata', 'the capture belongs to another pull request')
        # The described head is the checkout's: unpublished controlled commits are part of the PR's changes.
        base = (meta.get('base') or {}).get('sha')
        head = git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
        present = isinstance(base, str) and _SHA.fullmatch(base) and git(
            root, 'rev-parse', '--verify', '--quiet', base + '^{commit}', check=False).strip()
        require(present, 'changes', 'the PR base commit must be present in the checkout')
        patch, cut = _bounded(git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-color',
                                  '--find-renames', f'{base}...{head}'), MAX_PATCH)
        require(patch.strip(), 'changes', 'the pull request has no changes to describe')
        sources.append(_source('diff', 'diff', patch, public=True, url=pr['url'] + '/files'))
        commits = _history(root, head, exclude=base)
        sources.append(_source('commits', 'commits', '\n\n'.join(item['message'] for item in commits), public=True,
                               url=pr['url'] + '/commits'))
        current = {'title': meta.get('title') or '', 'body': meta.get('body') or ''}
        # Issues the published body already closes stay linked; a draft may keep their closing keywords.
        evidence['linked'].update(int(number) for number in _CLOSING.findall(_strip_code(current['body'])))
        sources.append(_source('pr:title', 'pr', current['title'], public=True, url=pr['url']))
        sources.append(_source('pr:body', 'pr', current['body'], public=True, url=pr['url']))
        heading = decisions.options(plan)['section_heading']
        _, section, _ = decisions.split_section(current['body'], heading)
        evidence.update(current=current, decisions={'heading': heading, 'section': section},
                        changes={'head': head, 'base': base, 'truncated': cut})
        evidence['templates'] = templates(root, name, values['template'])
    elif name == 'dk-commit-message':
        changes = local_changes(root)
        # The engine's own generated directory is never part of the described changes.
        owned = GENERATED.rstrip('/') + '/'
        for key in ('untracked', 'paths'):
            changes[key] = [path for path in changes[key] if not path.startswith(owned)]
        require(changes['paths'], 'changes', 'there are no local changes to describe')
        sources.append(_source('diff:staged', 'diff', changes['staged'], public=False))
        sources.append(_source('diff:unstaged', 'diff', changes['unstaged'], public=False))
        sources.append(_source('untracked', 'diff', '\n'.join(changes['untracked']), public=False))
        sources.append(_source('history', 'history', '\n\n'.join(item['message'] for item in _history(root)),
                               public=False))
        configured = git(root, 'config', '--get', 'commit.template', check=False).decode().strip()
        if configured:
            try:
                data = _read(root, _relative(configured, 'commit.template'), 'commit.template')
                sources.append(_source('commit-template', 'template', data, public=False, path=configured))
            except Blocked:
                evidence['notes'].append('commit.template outside the checkout was not read: ' + configured)
        evidence.update(changes=changes, identity=_identity(root))
    else:
        repo = _repo(plan)
        evidence['templates'] = templates(root, name, values['template'])
        metadata = requested['metadata'] or {}
        issue = requested['issue']
        chosen = {}
        for key, field in (('labels', 'name'), ('assignees', 'login')):
            explicit = key in metadata
            wanted = list(metadata[key]) if explicit else list(values[key])
            if issue is not None and not explicit:
                continue  # An update changes labels and assignees only when they are requested.
            if wanted:
                known = {item.get(field) for item in provider.pages(f'{repo}/{key}?per_page=100')
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
            current = provider.request('GET', f'{repo}/issues/{issue}')
            require(isinstance(current, dict) and current.get('number') == issue and 'pull_request' not in current,
                    'issue', f'#{issue} is not an issue of the target repository')
            evidence['current'] = {'title': current.get('title') or '', 'body': current.get('body') or ''}
            evidence['number'] = issue
            evidence['references'].add(issue)
            body = '{title}\n\n{body}'.format(**evidence['current'])
            sources.append(_source(f'issue:{issue}', 'issue', body, public=True, url=current.get('html_url')))
    require(len(sources) <= MAX_ITEMS, 'sources', 'evidence exceeds the bounded source count')
    require(len({item['id'] for item in sources}) == len(sources), 'sources', 'duplicate source identity')
    for item in sources:
        evidence['references'].update(int(number) for number in _REF.findall(item['text']))
        evidence['urls'].update(match[0] for match in _URL_REF.finditer(item['text']))
        if item['url']:
            evidence['urls'].add(item['url'])
    evidence['sources'] = sources
    from .prose import constraints
    evidence['constraints'] = constraints([request])
    for key in ('references', 'linked', 'urls'):
        evidence[key] = sorted(evidence[key])
    return {'schema_version': 1, 'kind': KIND, 'entry': name, 'adapter': ENTRIES[name]['adapter'],
            'role_prompt': ENTRIES[name]['role_prompt'], 'options': values, 'contract': CONTRACT,
            'skills': [], 'tools': 'read-only', 'writes': False, 'publication': False, 'evidence': evidence}


def _evidence(ctx):
    evidence = copy.deepcopy(ctx['evidence'])
    for key in ('references', 'linked', 'urls'):
        evidence[key] = set(evidence[key])
    return evidence


def check(root, plan, report, assignment):
    """The accepted draft of a report against the context its claim carried, or Blocked."""
    ctx = assignment.get('drafting')
    require(isinstance(ctx, dict) and ctx.get('kind') == KIND, 'assignment', 'the claim carries no drafting context')
    require(not report.get('operations'), 'operations', 'a draft proposes no operations; the helper derives the '
            'exact intent and publishes it only under separate authority')
    require(not report.get('changes'), 'changes', 'drafting is read-only')
    deliverables = report.get('deliverables')
    require(isinstance(deliverables, dict) and set(deliverables) == {'draft'}, 'deliverables',
            'the report returns only its draft in deliverables.draft')
    try:
        return check_result(ctx['entry'], deliverables['draft'], _evidence(ctx), {'options': ctx['options']})
    except Blocked:
        raise
    except (TypeError, AttributeError, KeyError, ValueError) as error:
        raise Blocked('result', 'malformed draft: ' + type(error).__name__) from None


def current(root, ledger, plan, task, snapshot):
    """(claim row, accepted draft) of the occurrence's current accepted report, or None."""
    from . import app
    row = app._current(ledger, snapshot, task)
    if row is None or not row['agent']:
        return None
    assignment = read_artifact(root, row['assignment'])
    report = read_artifact(root, row['report'])['report']
    return row, check(root, plan, report, assignment)


def permitted(plan, action):
    """An enabled permission input plus an explicit authorization of the action."""
    enabled = (plan.get('options') or {}).get('permissions', {}).get(action) is True
    return enabled and action in (plan.get('authorizations') or {})


def _stale(value, live):
    """Fields the draft would write that someone changed after the draft read them."""
    return [key for key in value['fields'] if (live.get(key) or '') != (value['seen'].get(key) or '')]


def proposal(root, provider, ledger, plan, task, snapshot):
    """The exact intent of the occurrence's current draft, with its status; writes nothing."""
    name = role(task)
    if name not in ('dk-pr-description', 'dk-issue'):
        return {'status': 'not_published'}
    accepted = current(root, ledger, plan, task, snapshot)
    if accepted is None:
        return {'status': 'no_current_draft'}
    row, value = accepted
    final = value['draft']
    source = row['report']
    ident = {'dk-pr-description': 'description_', 'dk-issue': 'issue_'}[name] + digest(
        {'run': plan['logical_run'], 'result': source})[:48]
    if name == 'dk-pr-description':
        if not permitted(plan, 'edit_pr'):
            return {'status': 'not_authorized', 'draft': final}
        live = provider.capture(plan['pr']).get('metadata') or {}
        payload = {key: final[key] for key in value['fields'] if final[key] != (live.get(key) or '')}
        if not payload:
            return {'status': 'unchanged', 'draft': final}
        if _stale(value, live):
            return {'status': 'stale', 'draft': final}
        return {'status': 'proposed', 'draft': final, 'proposal': {
            'id': ident, 'kind': 'edit_pr', 'head': snapshot['head'], 'lease': snapshot['head'],
            'target': snapshot['target'], 'payload': payload, 'sources': [source]}}
    kind = 'edit_issue' if final['number'] else 'create_issue'
    if not permitted(plan, kind):
        return {'status': 'not_authorized', 'draft': final}
    if kind == 'create_issue':
        payload = {key: final[key] for key in ('title', 'body', 'labels', 'assignees')}
    else:
        live = provider.request('GET', f"{_repo(plan)}/issues/{final['number']}")
        require(isinstance(live, dict) and live.get('number') == final['number'], 'issue',
                'an issue update needs the live issue')
        if all((live.get(key) or '') == final[key] for key in ('title', 'body')) and not any(
                key in final for key in ('labels', 'assignees')):
            return {'status': 'unchanged', 'draft': final}
        if _stale(value, live):
            return {'status': 'stale', 'draft': final}
        payload = {'number': final['number'], 'title': final['title'], 'body': final['body'],
                   **{key: final[key] for key in ('labels', 'assignees') if key in final}}
    return {'status': 'proposed', 'draft': final,
            'proposal': {'id': ident, 'kind': kind, 'payload': payload, 'sources': [source]}}


def compose(root, provider, ledger, plan, snapshot, listed):
    """Add the exact intent of every current authorized draft to the listed proposals.

    A stale draft, a draft whose fields already hold the drafted text, and a draft
    without an enabled and authorized permission list nothing. The intent ID binds
    the logical run and the accepted report, so a retained intent is only
    reconciled. dk-pr-decisions' section is composed into a description body edit
    afterwards (decisions.compose).
    """
    result = list(listed)
    seen = {item.get('id') for item in result if isinstance(item, dict)}
    for task in plan.get('tasks', []):
        if role(task) not in ('dk-pr-description', 'dk-issue'):
            continue
        try:
            planned = proposal(root, provider, ledger, plan, task, snapshot)
        except Blocked:
            continue  # A draft that no longer passes its checks never publishes; refreshing it is the only way.
        if planned['status'] == 'proposed' and planned['proposal']['id'] not in seen:
            seen.add(planned['proposal']['id'])
            result.append(planned['proposal'])
    return result
