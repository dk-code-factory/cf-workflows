"""Prose cleanup entries and their adapter procedure (contract v1).

cf-workflows:dk-plain-english and cf-workflows:dk-asd-ste100 add no orchestration.
Each entry is a preset selection for the shared plan (one bundled prose finalizer,
explicitly empty tasks and reviewers) plus the adapter procedure below: the
admitted `leaf` role, a bundled assignment and these helper operations.

- `candidates` bounds the pass to text the PR changed: base to head for a
  first pass, or the previous accepted output to head when only sources changed.
- `run` dispatches (or reconciles, by claim) one engine-managed native session,
  checks what the engine observed and the actual edits, then records a local
  commit through a ledgered intent. It never pushes or publishes.
- `occurrences` lists the prose finalizers of any admitted shared plan, in order,
  so a composed run uses the same procedure.

Sessions come from the engine transport (`native_session`); no helper starts a
model or provider. Original role prompts come from the pinned dk-workflows
baseline (MIT; see licenses/dk-workflows-MIT.txt).
"""
import ast
from collections import Counter
import copy
import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess

from . import admission, configuration, dk_pr_fix, semantic
from .admission import AdmissionError, digest, fields, require, text

SCHEME = 1  # Contract version of the procedure; a change refreshes every unit.
SCHEMA_VERSION = 1
KIND = 'prose_state'
ADJACENT = 2  # Lines next to changed prose that a coherence edit may touch.
MAX_FILE = 1 << 20
MAX_UNITS = 64
MAX_ITEMS = 256
OFFICIAL = ('https://www.asd-ste100.org/', 'https://www.asd-ste100.org/about_STE.html')

# Exact baseline role prompts (prompts/default.md at the pinned commit; digests in baselines/inventory.json).
ENTRIES = {
    'dk-plain-english': {
        'skill': 'cf-workflows:dk-plain-english', 'adapter': 'plain-english', 'references': False,
        'role_prompt': 'Favor short sentences, familiar words, active voice and explicit references. Name the actor '
                       'when the source establishes it. Split long sentences without losing logical connections. '
                       'Keep necessary technical terms and precise modal verbs. Do not invent an actor, strengthen a '
                       'recommendation into a requirement, remove a qualifier or simplify a technical distinction '
                       'away.\n',
        'baseline_prompt': 'claude/skills/dk-plain-english/prompts/default.md',
    },
    'dk-asd-ste100': {
        'skill': 'cf-workflows:dk-asd-ste100', 'adapter': 'asd-ste100', 'references': True,
        'role_prompt': 'Use concise, independently written guidance: keep terminology consistent, separate actions '
                       'into clear steps, make conditions explicit and prefer direct constructions with clear '
                       'subjects. Consult authoritative rules and dictionary entries when available, including their '
                       'approved meanings and parts of speech. Preserve required technical nouns and verbs. Do not '
                       'replace domain terminology merely to shorten it.\n',
        'baseline_prompt': 'claude/skills/dk-asd-ste100/prompts/default.md',
    },
}
BY_ADAPTER = {entry['adapter']: name for name, entry in ENTRIES.items()}
assert set(BY_ADAPTER) == set(admission.PROSE_ADAPTERS)

# Protected syntax compared before and after the edits.
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[`~]*[ \t]*$', re.M | re.S)
CODE_SPAN = re.compile(r'(`+)(?:(?!\1).)+?\1')
URL = re.compile(r'[a-z][a-z0-9+.-]*://[^\s<>()\]]+', re.I)
LINK = re.compile(r'\]\(([^)\s]+)')
IDENTIFIER = re.compile(r'(?<![\w-])(?:--?[A-Za-z][\w-]*|[A-Za-z_]\w*(?:[./:]\w+)+|[a-z]+[A-Z]\w*|'
                        r'[A-Z][a-z]+[A-Z]\w*|[A-Z]{2,}\w*|\w*_\w*|\w*\d\w*)')
PLAIN = {'e.g', 'i.e', 'etc', 'vs'}  # Ordinary abbreviations, not terminology.
NUMBER = re.compile(r'\d+(?:[.,]\d+)*')
KEYWORD = re.compile(r'\b(?:MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|REQUIRED|RECOMMENDED|MAY|OPTIONAL)\b')
DIRECTIVE = re.compile(r'(?:noqa|type:\s*ignore|pragma|pylint:|eslint-|NOLINT|nolint|@ts-|fmt:|prettier-ignore|'
                       r'isort:|mypy:|rubocop:|clang-format|SPDX-License-Identifier)[^\n]*', re.I)
QUOTED = re.compile(r'"([^"\n]{2,200})"|“([^”\n]{2,200})”|`([^`\n]{2,200})`')
CLAIM = re.compile(r'\b(?:complian\w*|complies|certif\w*|conform\w*)\b', re.I)
# The only accepted mentions: fixed disclaimers. Any other compliance or certification wording rejects.
_SUBJECT = r'(?:full\s+)?(?:ASD-STE100\s+|STE\s+)?(?:compliance|conformance|certification)'
DISCLAIMER = re.compile(
    rf'\b{_SUBJECT}\s+(?:is|was|has\s+been)\s+not\s+(?:claimed|certified|verified)'
    r'(?:\s+(?:or|and|nor)\s+(?:claimed|certified|verified))?\b'
    rf'|\b(?:does|do|did)\s+not\s+claim\s+{_SUBJECT}\b'
    rf'|\bno\s+{_SUBJECT}\s+(?:is\s+)?claimed\b', re.I)

# Where changed lines can be prose. Documentation files: every line outside code blocks.
# Source files: whole comment lines only (and Python docstrings). Other files are never edited.
DOCUMENTATION = ('.md', '.markdown', '.rst', '.txt', '.adoc', '.asciidoc')
COMMENTS = {
    '#': ('.py', '.sh', '.bash', '.zsh', '.rb', '.pl', '.pm', '.yaml', '.yml', '.toml', '.r', '.cmake', '.ps1'),
    '//': ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.java', '.c', '.h', '.cc', '.cpp', '.hpp', '.cs', '.go',
           '.rs', '.swift', '.kt', '.kts', '.scala', '.dart', '.php', '.proto'),
    '--': ('.sql', '.lua', '.hs'),
    ';': ('.clj', '.lisp', '.el'),
}
RULE_NUMBER = re.compile(r'\b(?:rule|R)\s?\d+(?:\.\d+)+', re.I)


class Blocked(Exception):
    """The session result, its observation or its edits cannot be accepted; nothing is committed."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


def _failure(error):
    if isinstance(error, (AdmissionError, Blocked)):
        return {'path': error.path, 'message': error.message}
    return {'path': '$', 'message': 'malformed result: ' + type(error).__name__}


def git(repository, *args, data=None, check=True, env=None):
    result = subprocess.run(['git', '-C', str(repository), *args], input=data, capture_output=True, timeout=60,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', **(env or {})})
    if check and result.returncode:
        raise Blocked('git', 'git ' + args[0] + ' failed: ' + result.stderr.decode(errors='replace')[:400])
    return result.stdout


def _ancestor(repository, first, second):
    return subprocess.run(['git', '-C', str(repository), 'merge-base', '--is-ancestor', first, second],
                          capture_output=True, timeout=60).returncode == 0


def _commit_id(repository, revision, path='checkout'):
    value = git(repository, 'rev-parse', '--verify', '--quiet', revision + '^{commit}', check=False).decode().strip()
    require(re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', value or ''), path, 'must name an existing commit')
    return value


def _blob(repository, commit, path):
    """(object id, bytes) of path at commit, or (None, None) when absent."""
    oid = git(repository, 'rev-parse', '--verify', '--quiet', f'{commit}:{path}', check=False).decode().strip()
    if not oid:
        return None, None
    return oid, git(repository, 'cat-file', 'blob', oid)


def _object(repository, oid):
    return b'' if oid is None else git(repository, 'cat-file', 'blob', oid)


def _lines(data):
    return data.decode('utf-8').splitlines(keepends=True)


# -- Entry admission ---------------------------------------------------------------------------

def preset(name):
    """The entry's selection for the shared plan: one prose finalizer and explicitly empty slots."""
    return {'tasks': [], 'finalizers': [ENTRIES[name]['adapter']], 'reviewers': []}


def _rejected(path, message):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}}


def preflight(name, *, request, configuration, deployment, repository, transport, invocation=None, roots=None,
              install=dk_pr_fix.INSTALL, ledger=None):
    """Admit one direct prose pass through the shared structural and semantic admission.

    The preset fixes tasks, finalizers and reviewers; invocation may add only prompts.
    Saved configuration still governs, so its prompts and implementation preferences
    are custom inputs assessed by the shared validator (ledger enables reuse).
    """
    try:
        require(name in ENTRIES, 'entry', 'unknown prose entry')
        require(isinstance(request, dict) and 'pr' in request, 'request.pr', 'prose cleanup edits changed PR prose')
        require(not request.get('authorizations'), 'request.authorizations',
                'prose cleanup commits locally only; publication stays with authorized publication stages')
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'prompts'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and permissions; only prompts may be added')
        dk_pr_fix.verify_installation(install)
    except AdmissionError as error:
        return _rejected(error.path, error.message)
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
    result['entry'] = {'name': name, 'skill': ENTRIES[name]['skill']}
    if result['preflight'] == 'admitted':
        snapshot = result['snapshot']
        [task] = snapshot['plan']['tasks']
        assert task['adapter'] == ENTRIES[name]['adapter'] and task['phase'] == 'finalizers'
        assert not any(value['effective'] for value in snapshot['permissions'].values())
    return result


def occurrences(admitted):
    """Prose finalizer (or task) occurrences of an admitted shared plan, in plan order.

    Scoped finalizer lists already replace inherited lists and an explicit empty
    list selects nothing; this only reads the admitted plan.
    """
    snapshot = admitted['snapshot']
    return [task for task in snapshot['plan']['tasks'] if task['adapter'] in BY_ADAPTER]


# -- Candidates --------------------------------------------------------------------------------

def _ranges(old, new):
    """New-side line numbers (0-based) changed from old to new, and the opcodes."""
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    changed = set()
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in ('replace', 'insert'):
            changed.update(range(j1, j2))
    return changed


def _spans(lines):
    """Sorted inclusive 1-based [start, end] spans of a set of 0-based line numbers."""
    spans = []
    for line in sorted(lines):
        if spans and spans[-1][1] == line:
            spans[-1][1] = line + 1
        else:
            spans.append([line + 1, line + 1])
    return spans


def changed_paths(repository, base, head):
    raw = git(repository, 'diff', '--no-renames', '--name-status', '-z', base, head).decode('utf-8', 'surrogateescape')
    parts = [part for part in raw.split('\0') if part]
    return sorted(parts[n + 1] for n in range(0, len(parts), 2) if parts[n] in ('A', 'M'))


def candidates(repository, base, head, units, contract, bindings):
    """Plan the pass per changed path: reuse, delta from the accepted output, or full from base.

    units are the latest accepted unit records of this occurrence by path. A unit is
    reused only while its contract, base blob and preference binding are current and
    its accepted output is still the head blob.
    """
    plan, skipped = {}, []
    for path in changed_paths(repository, base, head):
        head_oid, head_bytes = _blob(repository, head, path)
        base_oid, _ = _blob(repository, base, path)
        if len(head_bytes) > MAX_FILE:
            skipped.append({'path': path, 'reason': 'too large to inspect'})
            continue
        try:
            new = _lines(head_bytes)
        except UnicodeDecodeError:
            # Binary or non-UTF-8 content: a summary only, never treated as prose.
            skipped.append({'path': path, 'reason': 'binary or not UTF-8'})
            continue
        prose = prose_lines(path, new)
        if prose is None:
            skipped.append({'path': path, 'reason': 'no recognized prose syntax for this file type'})
            continue
        unit = units.get(path)
        current = (unit is not None and unit['contract'] == contract and unit['base_blob'] == base_oid
                   and unit['binding'] == bindings[path])
        if current and unit['output_blob'] == head_oid:
            plan[path] = {'path': path, 'mode': 'reused', 'head_blob': head_oid, 'base_blob': base_oid,
                          'binding': bindings[path], 'claim': unit['claim']}
            continue
        start = unit['output_blob'] if current else base_oid
        try:
            old = _lines(_object(repository, start))
        except UnicodeDecodeError:
            old = []
        changed = _ranges(old, new) & prose
        if not changed:
            # No changed prose (only code, or only deletions since the accepted output).
            plan[path] = {'path': path, 'mode': 'reused' if current else 'unchanged', 'head_blob': head_oid,
                          'base_blob': base_oid, 'binding': bindings[path], 'claim': unit['claim'] if current else None}
            continue
        patch = ''.join(difflib.unified_diff(old, new, 'a/' + path, 'b/' + path, n=3))
        plan[path] = {'path': path, 'mode': 'delta' if current else 'full', 'from_blob': start,
                      'head_blob': head_oid, 'base_blob': base_oid, 'binding': bindings[path],
                      'ranges': _spans(changed), 'patch': patch, 'changed': sorted(changed)}
    require(sum(1 for item in plan.values() if item['mode'] in ('full', 'delta')) <= MAX_UNITS, 'candidates',
            'too many changed files for one bounded pass')
    return plan, skipped


# -- Protected content -------------------------------------------------------------------------

def _python_skeleton(source):
    """AST dump without docstrings, or None when source is not Python."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)
                and isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))):
            body[0] = ast.Pass()
    return ast.dump(tree, include_attributes=False)


def _tokens(chunk):
    return {'identifiers': Counter(token for token in IDENTIFIER.findall(CODE_SPAN.sub(' ', chunk))
                                   if token.lower() not in PLAIN),
            'code spans': Counter(match.group(0) for match in CODE_SPAN.finditer(chunk)),
            'URLs': Counter(URL.findall(chunk)), 'link targets': Counter(LINK.findall(chunk)),
            'numbers': Counter(NUMBER.findall(CODE_SPAN.sub(' ', chunk))),
            'requirement keywords': Counter(KEYWORD.findall(chunk)),
            'machine directives': Counter(match.group(0).strip() for match in DIRECTIVE.finditer(chunk))}


def _docstrings(source):
    """0-based lines of Python module, class and function docstrings."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                lines.update(range(first.lineno - 1, first.end_lineno))
    return lines


def prose_lines(path, lines):
    """0-based lines of a file that may hold prose, or None when its type has no recognized prose syntax.

    Documentation: every line except fenced and indented code blocks. Source: whole
    comment lines (and Python docstrings); trailing comments on code lines stay protected.
    """
    suffix = PurePosixPath(path).suffix.lower()
    result = set()
    if suffix in DOCUMENTATION:
        fence = None
        for number, line in enumerate(lines):
            match = re.match(r' {0,3}(`{3,}|~{3,})', line)
            if fence:
                if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                    fence = None
                continue
            if match:
                fence = match.group(1)
            elif not re.match(r'(?: {4}|\t)', line):  # Indented lines are code blocks or literal blocks.
                result.add(number)
        return result
    marker = next((marker for marker, suffixes in COMMENTS.items() if suffix in suffixes), None)
    if marker is None:
        return None
    block = False
    for number, line in enumerate(lines):
        stripped = line.strip()
        if marker == '//' and (block or stripped.startswith('/*')):
            result.add(number)
            block = '*/' not in stripped[2 if not block else 0:]
        elif stripped.startswith(marker) and not (marker == '#' and stripped.startswith('#!')):
            result.add(number)
    if suffix == '.py':
        result |= _docstrings(''.join(lines))
    return result


def check_edits(path, before, after, allowed, constraints):
    """Edits stay next to changed prose and keep protected syntax, terminology and exact wording."""
    try:
        old, new = _lines(before), _lines(after)
    except UnicodeDecodeError:
        raise Blocked(path, 'edited file is not UTF-8') from None
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    allowed = allowed & (prose_lines(path, old) or set())
    prose_after = prose_lines(path, new) or set()
    removed, added, touched = [], [], set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue
        inside = (all(line in allowed for line in range(i1, i2)) if i2 > i1
                  else i1 - 1 in allowed or i1 in allowed)  # An insertion sits next to changed prose.
        if not inside:
            raise Blocked(path, f'edit at line {i1 + 1} is outside the changed prose')
        if not all(line in prose_after for line in range(j1, j2)):
            raise Blocked(path, f'edit at line {j1 + 1} turns prose into code or code into prose')
        removed.append(''.join(old[i1:i2]))
        added.append(''.join(new[j1:j2]))
        touched.update(range(j1, j2))
    before_text, after_text = ''.join(old), ''.join(new)
    if [match.group(0) for match in FENCE.finditer(before_text)] != [
            match.group(0) for match in FENCE.finditer(after_text)]:
        raise Blocked(path, 'code blocks changed')
    first, second = _tokens('\n'.join(removed)), _tokens('\n'.join(added))
    for kind in first:
        if first[kind] != second[kind]:
            lost = sorted((first[kind] - second[kind]) + (second[kind] - first[kind]))[:5]
            raise Blocked(path, f'{kind} changed: ' + ', '.join(map(str, lost)))
    if path.endswith('.py'):
        skeleton = _python_skeleton(before_text)
        if skeleton is not None and skeleton != _python_skeleton(after_text):
            raise Blocked(path, 'Python code changed; only comments and docstrings may change')
    for phrase in constraints:
        if before_text.count(phrase) > after_text.count(phrase):
            raise Blocked(path, 'exact wording from the request was changed: ' + phrase)
    return _spans(touched)


def constraints(instructions):
    """Exact wording the user quoted: these phrases are never rewritten."""
    found = []
    for text_ in instructions:
        for match in QUOTED.finditer(text_ or ''):
            phrase = next(group for group in match.groups() if group)
            if phrase not in found:
                found.append(phrase)
    return found


# -- References (dk-asd-ste100) ----------------------------------------------------------------

def check_references(name, references):
    """Read supplied authoritative references by exact digest; absence is recorded, not fatal."""
    references = [] if references is None else references
    require(isinstance(references, list) and len(references) <= 16, 'references', 'must be a bounded array')
    require(not references or ENTRIES[name]['references'], 'references',
            ENTRIES[name]['skill'] + ' consumes no reference sources')
    result = {}
    for number, item in enumerate(references):
        where = f'references[{number}]'
        fields(item, where, ('id', 'path', 'sha256'), ('title',))
        require(isinstance(item['id'], str) and admission.IDENT.fullmatch(item['id']) and item['id'] not in result,
                where + '.id', 'invalid or repeated reference id')
        path = Path(text(item['path'], where + '.path', 4096))
        require(path.is_absolute() and path.is_file() and not path.is_symlink(), where + '.path',
                'must be an absolute regular file')
        data = path.read_bytes()
        require(len(data) <= MAX_FILE and hashlib.sha256(data).hexdigest() == item['sha256'], where + '.sha256',
                'reference bytes differ from the supplied digest')
        result[item['id']] = {'path': str(path), 'sha256': item['sha256'], 'title': item.get('title')}
    limits = []
    if ENTRIES[name]['references']:
        if not result:
            limits.append('No authoritative ASD-STE100 specification or dictionary was supplied: rule and '
                          'dictionary checks were not performed. Official starting points: ' + ', '.join(OFFICIAL)
                          + '.')
        limits.append('Best-effort ASD-STE100 editing; full ASD-STE100 compliance is not claimed or certified.')
    return result, limits


# -- Native report -----------------------------------------------------------------------------

def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def check_report(name, output, units, references, actual):
    """Check the native report against the helper-observed edits and reference availability."""
    ste = ENTRIES[name]['references']
    optional = ('commit_message', 'no_op') + (('reference_checks',) if ste else ())
    required = ('complete', 'changed_locations', 'validation', 'unresolved_ambiguities',
                'reference_verification_limits') + (('compliance',) if ste else ())
    fields(output, 'report', required, optional)
    require(type(output['complete']) is bool, 'report.complete', 'must be boolean')
    for key in ('validation', 'reference_verification_limits'):
        require(isinstance(output[key], list) and len(output[key]) <= MAX_ITEMS, 'report.' + key,
                'must be a bounded array')
        for number, item in enumerate(output[key]):
            text(item, f'report.{key}[{number}]')
    require(output['validation'] or not output['complete'], 'report.validation', 'a complete pass cites its validation')
    reported = {}
    require(isinstance(output['changed_locations'], list) and len(output['changed_locations']) <= MAX_ITEMS,
            'report.changed_locations', 'must be a bounded array')
    for number, item in enumerate(output['changed_locations']):
        where = f'report.changed_locations[{number}]'
        fields(item, where, ('path', 'start', 'end'), ('reason',))
        require(item['path'] in actual, where + '.path', 'reports a location the session did not change')
        require(type(item['start']) is int and type(item['end']) is int and 1 <= item['start'] <= item['end'],
                where, 'invalid line span')
        require(any(item['start'] <= end and start <= item['end'] for start, end in actual[item['path']]), where,
                'does not match an actual edit')
        reported.setdefault(item['path'], []).append(item)
    require(set(reported) == {path for path, spans in actual.items() if spans}, 'report.changed_locations',
            'every edited file must be reported')
    require(isinstance(output['unresolved_ambiguities'], list) and len(output['unresolved_ambiguities']) <= MAX_ITEMS,
            'report.unresolved_ambiguities', 'must be a bounded array')
    for number, item in enumerate(output['unresolved_ambiguities']):
        where = f'report.unresolved_ambiguities[{number}]'
        fields(item, where, ('path', 'line', 'text'))
        text(item['text'], where + '.text')
        require(item['path'] in units and type(item['line']) is int and item['line'] >= 1, where,
                'cites a line of a candidate file')
    if any(actual.values()):
        message = text(output.get('commit_message'), 'report.commit_message', 16384)
        require(message.strip(), 'report.commit_message', 'edits need a commit message')
    else:
        require(output['complete'] is False or isinstance(output.get('no_op'), dict), 'report.no_op',
                'a pass without edits returns a sourced no-op assessment')
        if output.get('no_op') is not None:
            fields(output['no_op'], 'report.no_op', ('reason', 'sources'))
            text(output['no_op']['reason'], 'report.no_op.reason')
            require(isinstance(output['no_op']['sources'], list) and set(output['no_op']['sources']) <= set(units),
                    'report.no_op.sources', 'cites candidate files')
    if ste:
        # Certification needs evidence this suite cannot hold; the pass never claims it.
        require(output['compliance'] == 'not_claimed', 'report.compliance', 'full ASD-STE100 compliance is never claimed')
        checks = output.get('reference_checks', [])
        require(isinstance(checks, list) and len(checks) <= MAX_ITEMS, 'report.reference_checks', 'must be an array')
        for number, item in enumerate(checks):
            fields(item, f'report.reference_checks[{number}]', ('reference', 'subject'))
            require(item['reference'] in references, f'report.reference_checks[{number}].reference',
                    'cites a reference that was not supplied')
            text(item['subject'], f'report.reference_checks[{number}].subject')
        for value in _texts({key: output.get(key) for key in (
                'validation', 'reference_verification_limits', 'changed_locations', 'no_op',
                'unresolved_ambiguities', 'commit_message')}):
            require(not CLAIM.search(DISCLAIMER.sub(' ', value)), 'report',
                    'claims ASD-STE100 compliance or certification without evidence')
            require(references or not RULE_NUMBER.search(value), 'report',
                    'cites a rule number without a supplied reference')
    return output


# -- Ledger ------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE passes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim TEXT NOT NULL UNIQUE,
    logical_run TEXT NOT NULL,
    adapter TEXT NOT NULL,
    occurrence TEXT NOT NULL,
    contract TEXT NOT NULL,
    head TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('dispatched', 'committing', 'accepted', 'rejected', 'incomplete')),
    task TEXT NOT NULL,
    commit_intent TEXT,
    result TEXT
);
CREATE TABLE units (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim TEXT NOT NULL,
    logical_run TEXT NOT NULL,
    occurrence TEXT NOT NULL,
    path TEXT NOT NULL,
    contract TEXT NOT NULL,
    base_blob TEXT,
    binding TEXT NOT NULL,
    output_blob TEXT NOT NULL
);
"""


class Ledger:
    """Versioned SQLite ledger of prose passes and accepted units; history is never deleted."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('BEGIN IMMEDIATE')
        try:
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
            if version == 0 and tables == 0:
                for statement in filter(str.strip, SCHEMA.split(';')):
                    self.db.execute(statement)
                self.db.executemany('INSERT INTO meta VALUES (?, ?)', [('suite', admission.SUITE), ('kind', KIND)])
                self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
            elif version != SCHEMA_VERSION:
                raise ValueError(f'unsupported prose ledger version {version}')
            if dict(self.db.execute('SELECT key, value FROM meta').fetchall()) != {'suite': admission.SUITE,
                                                                                   'kind': KIND}:
                raise ValueError('not a cf-workflows prose ledger')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def write(self, statement, values):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute(statement, values)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def pass_(self, claim):
        return self.db.execute('SELECT * FROM passes WHERE claim=?', (claim,)).fetchone()

    def units(self, logical_run, occurrence):
        result = {}
        for row in self.db.execute('SELECT * FROM units WHERE logical_run=? AND occurrence=? ORDER BY seq',
                                   (logical_run, occurrence)):
            result[row['path']] = dict(row)
        return result

    def accept(self, claim, result, units):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.pass_(claim)
            self.db.executemany('INSERT INTO units (claim, logical_run, occurrence, path, contract, base_blob, binding, '
                                'output_blob) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                [(claim, row['logical_run'], row['occurrence'], unit['path'], row['contract'],
                                  unit['base_blob'], unit['binding'], unit['output_blob']) for unit in units])
            self.db.execute("UPDATE passes SET state='accepted', result=? WHERE claim=?",
                            (admission.canonical(result).decode(), claim))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise


def ledger_path(home, repository):
    """The repository's prose ledger in the suite's state home; a digest, so request text never forms a path.

    Passes and units are keyed by logical run and occurrence inside it.
    """
    return Path(home) / 'cf-workflows' / 'prose' / (hashlib.sha256(repository.encode()).hexdigest()[:32] + '.sqlite3')


# -- Procedure ---------------------------------------------------------------------------------

def occurrence_id(task):
    """Stable across reordering: adapter, phase and instructions, never the plan position."""
    return 'prose_' + digest([task['adapter'], task['phase'], task['instructions']])[:24]


def contract(admitted, task, references):
    """Inputs that require a full pass when they change; per-file sources and preferences bind per unit."""
    snapshot = admitted['snapshot']
    adapter = snapshot['adapters'][task['adapter']]
    return digest({'scheme': SCHEME, 'adapter': task['adapter'], 'assignment': adapter['assignment'],
                   'checklist': adapter['checklist'], 'definition': task['definition_sha256'],
                   'role_prompt': ENTRIES[BY_ADAPTER[task['adapter']]]['role_prompt'],
                   'instructions': [snapshot['request']['original_instructions'], task['instructions']],
                   'prompts': sorted(item['sha256'] for item in snapshot['prompts'] if item['applicable']
                                     and item['target'] in (None, task['adapter'])),
                   'guidance': snapshot['resources']['guidance'],
                   'references': {key: value['sha256'] for key, value in references.items()}})


def _status(repository):
    raw = git(repository, 'status', '--porcelain=v1', '-z', '--untracked-files=all').decode('utf-8', 'surrogateescape')
    return {entry[3:]: entry[:2] for entry in raw.split('\0') if entry}


def _restore(repository, head):
    """Undo the session's uncommitted edits; the checkout was clean before dispatch."""
    tracked = [path for path, code in _status(repository).items() if code != '??']
    if tracked:
        git(repository, '--literal-pathspecs', 'checkout', '-q', head, '--', *tracked)
    for path, code in _status(repository).items():
        if code == '??':
            (Path(repository) / path).unlink()


def _commit(repository, ledger, claim, message, paths, parent):
    """A ledgered local commit of exactly paths; recovery settles from HEAD, never commits twice."""
    row = ledger.pass_(claim)
    if row['commit_intent']:
        intent = admission.load_json(row['commit_intent'])
        head = _commit_id(repository, 'HEAD')
        if head == intent['commit']:
            git(repository, 'reset', '-q', '--mixed', intent['commit'])
            return intent['commit']
        if head != intent['parent']:
            raise Blocked('commit', 'HEAD moved away from the recorded commit intent; reconcile manually')
        git(repository, 'update-ref', 'HEAD', intent['commit'], intent['parent'])
        git(repository, 'reset', '-q', '--mixed', intent['commit'])
        return intent['commit']
    # A private index: the checkout's own index and unrelated state are never staged.
    index = Path(git(repository, 'rev-parse', '--absolute-git-dir').decode().strip()) / ('cf-prose-' + claim)
    try:
        env = {'GIT_INDEX_FILE': str(index)}
        git(repository, 'read-tree', parent, env=env)
        git(repository, '--literal-pathspecs', 'update-index', '--', *paths, env=env)
        tree = git(repository, 'write-tree', env=env).decode().strip()
    finally:
        index.unlink(missing_ok=True)
    changed = set(git(repository, 'diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z',
                      parent, tree).decode().split('\0')) - {''}
    if changed != set(paths):
        raise Blocked('commit', 'commit tree differs from the accepted edits')
    commit = git(repository, 'commit-tree', tree, '-p', parent, data=message.encode()).decode().strip()
    ledger.write("UPDATE passes SET state='committing', commit_intent=? WHERE claim=?",
                 (admission.canonical({'parent': parent, 'tree': tree, 'commit': commit}).decode(), claim))
    git(repository, 'update-ref', 'HEAD', commit, parent)
    git(repository, 'reset', '-q', '--mixed', commit)
    return commit


def assignment(admitted, task, *, claim, plan, skipped, references, limits, wording, bindings, base, head,
               repository, mode):
    snapshot = admitted['snapshot']
    adapter = snapshot['adapters'][task['adapter']]
    name = BY_ADAPTER[task['adapter']]
    agent, provider = semantic.validator_agent(admitted)
    work = [item for item in plan.values() if item['mode'] in ('full', 'delta')]
    return {
        'schema_version': 1, 'kind': 'prose_cleanup', 'suite': admission.SUITE, 'skill': ENTRIES[name]['skill'],
        'mode': mode, 'claim': claim, 'adapter': task['adapter'], 'role': task['definition'],
        'definition_sha256': task['definition_sha256'], 'assignment': adapter['assignment'],
        'checklist': adapter['checklist'], 'role_prompt': ENTRIES[name]['role_prompt'],
        'instructions': snapshot['request']['original_instructions'], 'task_instructions': task['instructions'],
        'prompts': [{key: item[key] for key in ('id', 'slot', 'target', 'text')} for item in snapshot['prompts']
                    if item['applicable'] and item['target'] in (None, task['adapter'])],
        'preferences': {item['path']: bindings[item['path']]['records'] for item in work},
        'agent': agent, 'provider': provider, 'skills': [], 'tools': 'edit',
        'writes': ['repository:' + item['path'] for item in work], 'commit': False, 'publication': False,
        'readable': dict(sorted({**{'repository:' + item['path']: item['head_blob'] for item in work},
                                 **{'reference:' + key: value['sha256'] for key, value in references.items()}}.items())),
        'context': {'repository': str(Path(repository).resolve()), 'base': base, 'head': head,
                    'units': [{key: item[key] for key in ('path', 'mode', 'ranges', 'patch')} for item in work],
                    'skipped': skipped, 'wording': wording, 'adjacent_lines': ADJACENT,
                    'references': {key: {k: value[k] for k in ('sha256', 'title')} for key, value in references.items()},
                    'reference_limits': limits},
    }


def _observe(session, task):
    try:
        fields(session, 'session', ('result_ref', 'output', 'observation'))
        reference = text(session['result_ref'], 'session.result_ref', 512)
        observed = fields(session['observation'], 'session.observation',
                          ('session_id', 'agent', 'role', 'definition_sha256', 'skills_loaded', 'operations', 'claim'))
        text(observed['session_id'], 'session.observation.session_id', 512)
    except AdmissionError as error:
        raise Blocked(error.path, error.message) from None
    if observed['claim'] != task['claim']:
        raise Blocked('session.observation.claim', 'session belongs to another claim')
    if (observed['role'], observed['definition_sha256'], observed['agent']) != (
            task['role'], task['definition_sha256'], task['agent']):
        raise Blocked('session.observation', 'session did not run the admitted role on the assigned agent')
    if observed['skills_loaded'] != []:
        raise Blocked('session.observation.skills_loaded', 'prose entries load no skills')
    allowed = {'read': set(task['readable']), 'write': set(task['writes'])}
    if not isinstance(observed['operations'], list):
        raise Blocked('session.observation.operations', 'must be an array')
    for number, operation in enumerate(observed['operations']):
        if (not isinstance(operation, dict) or set(operation) != {'kind', 'target'}
                or operation['target'] not in allowed.get(operation['kind'], ())):
            raise Blocked(f'session.observation.operations[{number}]',
                          'the session may only read and edit the files of its assignment')
    return reference, observed['session_id'], session['output']


def run(admitted, task, *, repository, base, transport, ledger, references=None, mode='direct'):
    """The adapter procedure of one prose occurrence: candidates, one native session, checks and a local commit.

    Returns a private report. `reused` dispatches no session; `blocked` and
    `incomplete` restore the candidate files and commit nothing.
    """
    snapshot = admitted['snapshot']
    name = BY_ADAPTER[task['adapter']]
    pr = snapshot['request']['pr']
    require(pr is not None, 'request.pr', 'prose cleanup edits changed PR prose')
    references, limits = check_references(name, references)
    base, head = _commit_id(repository, base, 'base'), _commit_id(repository, 'HEAD')
    require(_ancestor(repository, base, head), 'base', 'the original PR base must be an ancestor of the checkout head')
    logical_run, occurrence = snapshot['request']['logical_run'], occurrence_id(task)
    bound = contract(admitted, task, references)
    preferences = [item for item in snapshot['preferences'] if item['applicable']]
    paths = changed_paths(repository, base, head)
    bindings = {path: configuration.resolve(preferences, 'implementation', [path]) for path in paths}
    plan, skipped = candidates(repository, base, head, ledger.units(logical_run, occurrence), bound,
                               {path: value['binding'] for path, value in bindings.items()})
    work = {path: item for path, item in plan.items() if item['mode'] in ('full', 'delta')}
    wording = constraints([snapshot['request']['original_instructions'], task['instructions']])
    report = {'entry': ENTRIES[name]['skill'], 'adapter': task['adapter'], 'occurrence': occurrence,
              'units': {path: item['mode'] for path, item in sorted(plan.items())}, 'skipped': skipped,
              'reference_verification_limits': list(limits), 'wording': wording,
              **({'compliance': 'not_claimed'} if ENTRIES[name]['references'] else {})}
    if not work:
        return {**report, 'status': 'reused', 'prose_cleanup': {'baseline': base, 'head': head, 'output': head},
                'changed_locations': [], 'validation': ['No changed prose since the accepted pass.']}
    claim = 'prose-' + digest({'occurrence': occurrence, 'contract': bound, 'head': head,
                               'units': {path: [item['from_blob'], item['head_blob'], item['binding']]
                                         for path, item in work.items()}})[:32]
    native = assignment(admitted, task, claim=claim, plan=plan, skipped=skipped, references=references,
                        limits=limits, wording=wording, bindings=bindings, base=base, head=head,
                        repository=repository, mode=mode)
    row = ledger.pass_(claim)
    if row is None:
        require(not _status(repository), 'checkout', 'the checkout must be clean before a prose pass')
        ledger.write('INSERT INTO passes (claim, logical_run, adapter, occurrence, contract, head, state, task) '
                     "VALUES (?, ?, ?, ?, ?, ?, 'dispatched', ?)",
                     (claim, logical_run, task['adapter'], occurrence, bound, head,
                      admission.canonical(native).decode()))
    elif row['state'] in ('accepted', 'rejected', 'incomplete'):
        return admission.load_json(row['result'])
    try:
        # A known claim is reconciled by the engine to its original result: no second session.
        try:
            session = transport.native_session(copy.deepcopy(native))
        except Exception as failure:
            raise Blocked('session', 'native session failed: ' + type(failure).__name__) from failure
        result_ref, session_id, output = _observe(session, native)
        if row is not None and row['state'] == 'committing':
            commit = _commit(repository, ledger, claim, None, None, head)
            actual = admission.load_json(row['result'] or '{}').get('actual')
        else:
            status = _status(repository)
            require(_commit_id(repository, 'HEAD') == head, 'checkout', 'the session changed Git history')
            outside = sorted(set(status) - set(work))
            if outside:
                raise Blocked('checkout', 'the session changed files outside its assignment: ' + ', '.join(outside[:5]))
            require(all(code[0] == ' ' for code in status.values()), 'checkout', 'the session staged changes')
            actual = {}
            for path, item in work.items():
                after = (Path(repository) / path).read_bytes() if (Path(repository) / path).is_file() else None
                if after is None:
                    raise Blocked(path, 'the session deleted a candidate file')
                _, before = _blob(repository, head, path)
                allowed = {line + offset for line in item['changed'] for offset in range(-ADJACENT, ADJACENT + 1)}
                actual[path] = check_edits(path, before, after, allowed, wording) if path in status else []
            check_report(name, output, work, references, actual)
            commit = head
            if output['complete'] and any(actual.values()):
                ledger.write('UPDATE passes SET result=? WHERE claim=?',
                             (admission.canonical({'actual': actual}).decode(), claim))
                commit = _commit(repository, ledger, claim, output['commit_message'],
                                 sorted(path for path, spans in actual.items() if spans), head)
    except (AdmissionError, Blocked, TypeError, KeyError, AttributeError) as error:
        result = {**report, 'status': 'blocked', 'error': _failure(error), 'claim': claim}
        if isinstance(error, Blocked) and error.path in ('session', 'commit'):
            # A lost response or an unsettled commit: keep the edits and the claim for reconciliation.
            return result
        _restore(repository, head)
        ledger.write("UPDATE passes SET state='rejected', result=? WHERE claim=?",
                     (admission.canonical(result).decode(), claim))
        return result
    result = {**report, 'evidence_refs': ['native:' + result_ref], 'session_id': session_id,
              'changed_locations': output['changed_locations'],
              'validation': output['validation'] + [
                  'Helper: edits stay within changed prose; code blocks, code spans, links, identifiers, numbers, '
                  'requirement keywords, directives and quoted wording are unchanged.'],
              'unresolved_ambiguities': output['unresolved_ambiguities'],
              'reference_verification_limits': limits + [item for item in output['reference_verification_limits']
                                                         if item not in limits],
              **({'reference_checks': output.get('reference_checks', [])} if ENTRIES[name]['references'] else {}),
              **({'no_op': output['no_op']} if output.get('no_op') else {})}
    if not output['complete']:
        _restore(repository, head)
        result['status'] = 'incomplete'
        ledger.write("UPDATE passes SET state='incomplete', result=? WHERE claim=?",
                     (admission.canonical(result).decode(), claim))
        return result
    result.update(status='complete', commit=commit if commit != head else None,
                  prose_cleanup={'baseline': base, 'head': head, 'output': commit})
    units = [{'path': path, 'base_blob': item['base_blob'], 'binding': item['binding'],
              'output_blob': _blob(repository, commit, path)[0]} for path, item in work.items()]
    ledger.accept(claim, result, units)
    return result


def cleanup(name, *, request, configuration, deployment, repository, transport, ledger, base, invocation=None,
            references=None, roots=None, install=dk_pr_fix.INSTALL, acceptance_ledger=None):
    """A direct cf-workflows:dk-plain-english or dk-asd-ste100 pass: preflight, then the adapter procedure."""
    admitted = preflight(name, request=request, configuration=configuration, deployment=deployment,
                         repository=repository, transport=transport, invocation=invocation, roots=roots,
                         install=install, ledger=acceptance_ledger)
    if admitted['preflight'] != 'admitted':
        return {'status': admitted['preflight'], 'admission': admitted}
    [task] = occurrences(admitted)
    try:
        return {**run(admitted, task, repository=repository, base=base, transport=transport, ledger=ledger,
                      references=references), 'admission': admitted}
    except AdmissionError as error:
        return {'status': 'rejected', 'error': _failure(error), 'admission': admitted}
