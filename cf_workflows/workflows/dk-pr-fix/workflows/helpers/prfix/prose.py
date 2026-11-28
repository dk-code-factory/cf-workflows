"""The bounded prose procedure of the plain-english and asd-ste100 finalizers (installed side).

cf-workflows:dk-plain-english and cf-workflows:dk-asd-ste100 are presets over this
workflow: one prose finalizer occurrence that runs through the ordinary task path
(task.yml, native.yml). These helpers add the prose procedure to that path:

- `plan` bounds the native assignment to changed prose: each changed file is
  reused, a delta from the last accepted output, or a full pass from the PR base.
  With no changed prose the occurrence completes without a session.
- `check` runs at acceptance, before the controlled commit: the session changed
  only candidate files, every edit stays within changed prose and keeps protected
  syntax and quoted wording, and the prose report matches the actual edits and
  never claims ASD-STE100 compliance.

The protected-content checks below the marker are a verbatim copy of
cf_workflows/prose.py; tests/test_prose_finalizer.py keeps the two identical.
The engine runs the session (skills: [], the admitted leaf role); nothing here
starts a model, commits outside the controlled Git helper or publishes.
"""
import ast
from collections import Counter
import difflib
import io
from pathlib import Path, PurePosixPath
import re
import subprocess
import tokenize

from .core import GENERATED, digest, read_artifact

SCHEME = 1  # Contract version of the installed procedure; a change refreshes every unit.
MAX_UNITS = 64
MAX_TEXT = 64 << 10


class Blocked(ValueError):
    """The prose report or its edits cannot be accepted; nothing is committed."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


def require(condition, path, message):
    if not condition:
        raise Blocked(path, message)


def text(value, path, limit=MAX_TEXT):
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


# -- Shared with cf_workflows/prose.py (verbatim) ---------------------------------------------

ADJACENT = 2  # Lines next to changed prose that a coherence edit may touch.
MAX_FILE = 1 << 20
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
# Constructs the comment lexer cannot follow; a file containing one has no editable prose.
UNLEXED = {'#': ('<<', '%q', '%Q', '%w', '=begin', '=pod', '"""', "'''"),
           '//': ('R"', '<<<', '@"', 'r#"', '"""', "'''"), '--': ('[[',), ';': ()}
# JavaScript and TypeScript have regex literals: a `/` where an operand starts means the lexer stops.
REGEX_LANGUAGES = ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx')
REGEX_KEYWORDS = ('return', 'typeof', 'instanceof', 'case', 'in', 'of', 'delete', 'void', 'throw', 'new', 'yield',
                  'await', 'do', 'else')
# Operator characters whose count in the changed text must not change.
OPERATORS = '<>=!&|^~%'
RULE_NUMBER = re.compile(r'\b(?:rule|R)\s?\d+(?:\.\d+)+', re.I)


def _lines(data):
    # Lines end at \n only, as the comment lexer and tokenize count them (not at \f, \x85 or U+2028).
    return re.findall(r'[^\n]*\n|[^\n]+\Z', data.decode('utf-8'))


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
            'operators': Counter(char for char in chunk if char in OPERATORS),
            'machine directives': Counter(match.group(0).strip() for match in DIRECTIVE.finditer(chunk))}


def _python_prose(source):
    """0-based Python lines that are wholly a comment token or wholly a docstring; nothing if it does not lex."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        tree = ast.parse(source)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return set()
    code = {line for token in tokens if token.type not in (
        tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)
        for line in range(token.start[0] - 1, token.end[0])}
    lines = {token.start[0] - 1 for token in tokens
             if token.type == tokenize.COMMENT and not token.string.startswith('#!')} - code
    if '\r' in source.replace('\r\n', ''):
        return set()  # A lone CR is a line break to ast but not to the LF-only line indexes.
    rows = source.split('\n')
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                    and not rows[first.lineno - 1][:first.col_offset].strip()
                    and not rows[first.end_lineno - 1][first.end_col_offset:].split('#')[0].strip()):
                lines.update(range(first.lineno - 1, first.end_lineno))
    return lines


def _comment_prose(marker, suffix, text):
    """0-based lines that are wholly comment, from a small lexer over strings and comments.

    Strings may span lines; a line that starts inside a string or holds any code or string
    character is not prose. Files with constructs this lexer cannot follow (heredocs, raw
    strings, YAML block scalars, long brackets) yield no prose at all. Errors only shrink prose.
    """
    if any(opener in text for opener in UNLEXED[marker]) or (
            suffix in ('.yaml', '.yml') and re.search(r'(?m)[:-]\s*[|>][-+0-9]*\s*(?:#.*)?$', text)) or (
            suffix == '.lua' and re.search(r'\[=*\[', text)):
        return set()
    block = ('/*', '*/') if marker == '//' else None
    quotes = ('"', "'", '`') if marker == '//' else ('"', "'")
    result, line, state, code, comment = set(), 0, None, False, False
    previous = ''  # Code before the current position, to tell a regex literal from a division.
    position, size = 0, len(text)
    while position <= size:
        char = text[position] if position < size else '\n'
        if char == '\n':
            if comment and not code:
                result.add(line)
            line, position, previous = line + 1, position + 1, previous[-63:] + ' '
            if state == 'line' or (state == "'" and marker == '//'):
                state = None  # Line comments and C-family character literals end at the line.
            # A line that starts inside a string is string content; inside a block comment, comment.
            code, comment = state in quotes, state == 'block'
            continue
        if state == 'line':
            position += 1
        elif state == 'block':
            comment = True
            if text.startswith(block[1], position):
                state, position = None, position + len(block[1])
            else:
                position += 1
        elif state in quotes:
            code = True
            # Shell single quotes and Go raw strings have no escapes.
            if char == '\\' and not (marker == '#' and state == "'") and not (suffix == '.go' and state == '`'):
                position += 1 if text.startswith('\n', position + 1) else 2  # Line numbers stay exact.
                continue
            if char == state:
                state, previous = None, previous + char
            position += 1
        elif char.isspace():
            previous, position = previous[-63:] + ' ', position + 1
        elif text.startswith(marker, position) and not (marker == '#' and text.startswith('#!', position)):
            state, comment = 'line', True
            position += len(marker)
        elif block and text.startswith(block[0], position):
            state, comment = 'block', True
            position += len(block[0])
        elif char in quotes:
            state, code = char, True
            position += 1
        else:
            if char == '/' and suffix in REGEX_LANGUAGES:
                operand = previous.rstrip()
                word = re.search(r'[A-Za-z_$][\w$]*$', operand)
                if not operand or not re.search(r'[\w$)\]"\'`]$', operand) or (word and word.group(0) in REGEX_KEYWORDS):
                    return set()  # A regex literal: its contents cannot be lexed here.
            previous = (previous + char)[-64:]
            code = True
            position += 2 if char == '\\' and not text.startswith('\n', position + 1) else 1
    return result


def prose_lines(path, lines):
    """0-based lines of a file that may hold prose, or None when its type has no recognized prose syntax.

    Documentation: every line except fenced and indented code blocks. Source: lines that
    are wholly comment (and Python docstrings); a comment next to code on its line stays protected.
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
    if suffix == '.py':
        return _python_prose(''.join(lines))
    return _comment_prose(marker, suffix, ''.join(lines))


def check_edits(path, before, after, allowed, constraints):
    """Edits stay next to changed prose and keep protected syntax, terminology and exact wording."""
    try:
        old, new = _lines(before), _lines(after)
    except UnicodeDecodeError:
        raise Blocked(path, 'edited file is not UTF-8') from None
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    prose_before = prose_lines(path, old) or set()
    allowed = allowed & prose_before
    prose_after = prose_lines(path, new) or set()
    removed, added, touched = [], [], set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            # An edited comment delimiter can turn untouched code into comment or comment into code.
            if any((i1 + k in prose_before) != (j1 + k in prose_after) for k in range(i2 - i1)):
                raise Blocked(path, f'edit turns untouched line {j1 + 1} into prose or code')
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
        require(isinstance(item['path'], str) and item['path'] in actual, where + '.path',
                'reports a location the session did not change')
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
        require(isinstance(item['path'], str) and item['path'] in units
                and type(item['line']) is int and item['line'] >= 1, where,
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
            sources = output['no_op']['sources']
            require(isinstance(sources, list) and all(isinstance(item, str) and item in units for item in sources),
                    'report.no_op.sources', 'cites candidate files')
    if ste:
        # Certification needs evidence this suite cannot hold; the pass never claims it.
        require(output['compliance'] == 'not_claimed', 'report.compliance', 'full ASD-STE100 compliance is never claimed')
        checks = output.get('reference_checks', [])
        require(isinstance(checks, list) and len(checks) <= MAX_ITEMS, 'report.reference_checks', 'must be an array')
        for number, item in enumerate(checks):
            fields(item, f'report.reference_checks[{number}]', ('reference', 'subject'))
            require(isinstance(item['reference'], str) and item['reference'] in references,
                    f'report.reference_checks[{number}].reference',
                    'cites a reference that was not supplied')
            text(item['subject'], f'report.reference_checks[{number}].subject')
        # File paths are names, not report wording: a candidate such as docs/compliance.md claims nothing.
        for value in _texts([output.get(key) for key in ('validation', 'reference_verification_limits',
                                                          'commit_message')] + [
                [item.get('reason') for item in output['changed_locations']],
                (output.get('no_op') or {}).get('reason'),
                [item['text'] for item in output['unresolved_ambiguities']]]):
            require(not CLAIM.search(DISCLAIMER.sub(' ', value)), 'report',
                    'claims ASD-STE100 compliance or certification without evidence')
            require(references or not RULE_NUMBER.search(value), 'report',
                    'cites a rule number without a supplied reference')
    return output


# -- Installed procedure ----------------------------------------------------------------------

def report_contract(name):
    """The prose report contract of an entry's native session."""
    ste = ENTRIES[name]['references']
    return ('Edit only the candidate files of prose.units, and only next to their changed prose lines. Return '
            'deliverables as a JSON object whose only member "prose" is {changed_locations: [{path, start, end, '
            'reason?}], validation: [text], unresolved_ambiguities: [{path, line, text}], '
            'reference_verification_limits: [text]' + (', compliance: "not_claimed"' if ste else '')
            + ', no_op?}. changed_locations must cover every edited file and match the actual edited lines. '
            'With edits, list every changed file in changes and give a commit_message; without edits, give a '
            'sourced no_op {reason, sources: [candidate paths]}. '
            + ('No ASD-STE100 reference is supplied: cite no rule numbers, and mention compliance, conformance or '
               'certification only to say that it is not claimed. ' if ste else '')
            + 'Do not commit, publish, load skills or edit any other file.')


def entry(task):
    """The prose entry name of a plan task, or None when it is not a prose finalizer occurrence."""
    name = BY_ADAPTER.get(task.get('skill'))
    return name if name and task.get('kind') == 'cleanup' and task.get('writes') else None


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError('Git command failed: ' + result.stderr.decode(errors='replace')[:4096])
    return result.stdout


def _blob(root, commit, path):
    """(object id, bytes) of path at commit, or (None, None) when absent."""
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', '--quiet', f'{commit}:{path}'],
                            capture_output=True, timeout=60, check=False)
    oid = result.stdout.decode().strip()
    if result.returncode or not oid:
        return None, None
    return oid, _git(root, 'cat-file', 'blob', oid)


def changed_paths(root, base, head):
    """Added or modified paths from base to head, each with whether it is a regular file at head.

    Symlinks and submodules are never prose candidates; they are listed as skipped.
    """
    raw = _git(root, 'diff', '--no-renames', '--raw', '-z', base, head).decode('utf-8', 'surrogateescape')
    parts = [part for part in raw.split('\0') if part]
    result = {}
    for n in range(0, len(parts) - 1, 2):
        fields_ = parts[n].split()
        if fields_[-1] in ('A', 'M'):
            result[parts[n + 1]] = fields_[1] in ('100644', '100755')
    return dict(sorted(result.items()))


def reference_limits(name):
    """The installed workflow supplies no ASD-STE100 references, so rule and dictionary checks never run."""
    if not ENTRIES[name]['references']:
        return []
    return ['No authoritative ASD-STE100 specification or dictionary was supplied: rule and dictionary checks '
            'were not performed. Official starting points: ' + ', '.join(OFFICIAL) + '.',
            'Best-effort ASD-STE100 editing; full ASD-STE100 compliance is not claimed or certified.']


def contract(plan, task, snapshot):
    """Inputs whose change needs a full pass; per-file sources bind per unit."""
    return digest({'scheme': SCHEME, 'skill': task['skill'], 'assignment': task['assignment'],
                   'checklist': task['checklist'], 'definition': task['definition_digest'],
                   'role_prompt': ENTRIES[BY_ADAPTER[task['skill']]]['role_prompt'],
                   'instructions': plan['instructions'], 'guidance': snapshot['instructions']})


def accepted_units(root, ledger, task):
    """The last accepted unit of each file for this occurrence, from its completed claims in order.

    A unit's output is the file's blob at the claim's controlled commit, or at its
    head when the pass made no edits; a reused unit carries its earlier output.
    """
    units = {}
    for row in ledger.db.execute("SELECT id,assignment FROM claims WHERE task=? AND state='complete' ORDER BY rowid",
                                 (task['id'],)):
        record = read_artifact(root, row['assignment']).get('prose')
        if not record:
            continue
        commit = ledger.db.execute("SELECT head FROM local_commits WHERE id=? AND state='completed'",
                                   (row['id'],)).fetchone()
        output = commit['head'] if commit else record['head']
        for path, unit in record['units'].items():
            if unit['mode'] in ('full', 'delta'):
                units[path] = {'contract': record['contract'], 'base_blob': unit['base_blob'],
                               'output_blob': _blob(root, output, path)[0]}
            elif unit['mode'] == 'reused':
                units[path] = {'contract': record['contract'], 'base_blob': unit['base_blob'],
                               'output_blob': unit['head_blob']}
    return units


def plan(root, ledger, plan_, task, snapshot):
    """The prose record of one occurrence: candidate units and the session's write scope.

    base is the PR base (merge base of target and checkout head); head is the
    checkout head the session edits.
    """
    name = BY_ADAPTER[task['skill']]
    base, head = snapshot['base'], snapshot['local_head']
    bound = contract(plan_, task, snapshot)
    previous = accepted_units(root, ledger, task)
    units, skipped = {}, []
    for path, regular in changed_paths(root, base, head).items():
        if not regular:
            skipped.append({'path': path, 'reason': 'not a regular file'})
            continue
        head_oid, head_bytes = _blob(root, head, path)
        base_oid, _ = _blob(root, base, path)
        if len(head_bytes) > MAX_FILE:
            skipped.append({'path': path, 'reason': 'too large to inspect'})
            continue
        try:
            new = _lines(head_bytes)
        except UnicodeDecodeError:
            skipped.append({'path': path, 'reason': 'binary or not UTF-8'})
            continue
        prose = prose_lines(path, new)
        if prose is None:
            skipped.append({'path': path, 'reason': 'no recognized prose syntax for this file type'})
            continue
        unit = previous.get(path)
        current = unit is not None and unit['contract'] == bound and unit['base_blob'] == base_oid
        item = {'path': path, 'head_blob': head_oid, 'base_blob': base_oid}
        if current and unit['output_blob'] == head_oid:
            units[path] = {**item, 'mode': 'reused'}
            continue
        start = unit['output_blob'] if current else base_oid
        try:
            old = [] if start is None else _lines(_git(root, 'cat-file', 'blob', start))
        except UnicodeDecodeError:
            old = []
        changed = _ranges(old, new) & prose
        if not changed:
            units[path] = {**item, 'mode': 'reused' if current else 'unchanged'}
            continue
        units[path] = {**item, 'mode': 'delta' if current else 'full', 'from_blob': start,
                       'ranges': _spans(changed), 'changed': sorted(changed),
                       'patch': ''.join(difflib.unified_diff(old, new, 'a/' + path, 'b/' + path, n=3))}
    work = sorted(path for path, unit in units.items() if unit['mode'] in ('full', 'delta'))
    if len(work) > MAX_UNITS:
        raise ValueError('too many changed files for one bounded prose pass')
    return {'entry': ENTRIES[name]['skill'], 'adapter': task['skill'], 'scheme': SCHEME, 'contract': bound,
            'base': base, 'head': head, 'units': units, 'skipped': skipped, 'writes': work,
            'role_prompt': ENTRIES[name]['role_prompt'], 'wording': constraints([plan_['instructions']]),
            'adjacent_lines': ADJACENT, 'reference_limits': reference_limits(name), 'skills': [],
            'commit': False, 'publication': False,
            **({'compliance': 'not_claimed'} if ENTRIES[name]['references'] else {})}


def _workspace_changes(root, head):
    """Paths that differ between the workspace and head, untracked files included."""
    tracked = _git(root, 'diff', '--name-only', '--no-renames', '-z', head, '--').decode('utf-8', 'surrogateescape')
    untracked = _git(root, 'ls-files', '--others', '--exclude-standard', '-z').decode('utf-8', 'surrogateescape')
    names = {name for name in (tracked + '\0' + untracked).split('\0') if name}
    return {name for name in names if not PurePosixPath(name).is_relative_to(GENERATED)}


def check(root, record, report):
    """Check a complete native report's edits and prose report against the record; raises Blocked.

    Compares the workspace with the record's head, so a replay after the
    controlled commit sees the same edits.
    """
    name = BY_ADAPTER[record['adapter']]
    work = {path: unit for path, unit in record['units'].items() if unit['mode'] in ('full', 'delta')}
    outside = sorted(_workspace_changes(root, record['head']) - set(work))
    require(not outside, 'changes', 'the session changed files outside its prose candidates: ' + ', '.join(outside[:5]))
    actual = {}
    for path, unit in work.items():
        target = Path(root) / path
        require(target.is_file() and not target.is_symlink(), path, 'the session deleted a candidate file')
        _, before = _blob(root, record['head'], path)
        after = target.read_bytes()
        allowed = {line + offset for line in unit['changed'] for offset in range(-ADJACENT, ADJACENT + 1)}
        actual[path] = check_edits(path, before, after, allowed, record['wording']) if after != before else []
    deliverables = report.get('deliverables')
    require(isinstance(deliverables, dict) and isinstance(deliverables.get('prose'), dict), 'deliverables.prose',
            'a prose pass returns its report in deliverables.prose')
    prose = deliverables['prose']
    require(not {'complete', 'commit_message'} & set(prose), 'deliverables.prose',
            'complete and commit_message belong to the task report')
    output = {**prose, 'complete': report['complete']}
    if report.get('commit_message') is not None:
        output['commit_message'] = report['commit_message']
    check_report(name, output, work, {}, actual)
    require(set(deliverables) == {'prose'}, 'deliverables', 'a prose pass returns only its prose report')
    if ENTRIES[name]['references']:
        # Finding texts are the report's other free text; they cannot claim what the prose report may not.
        for value in _texts([item.get('text') for item in report.get('findings') or [] if isinstance(item, dict)]):
            require(not CLAIM.search(DISCLAIMER.sub(' ', value)), 'report.findings',
                    'claims ASD-STE100 compliance or certification without evidence')
            require(not RULE_NUMBER.search(value), 'report.findings', 'cites a rule number without a supplied reference')
    changes = {item.get('path') for item in report.get('changes') or [] if isinstance(item, dict)}
    require(changes == {path for path, spans in actual.items() if spans}, 'changes',
            'the changed-file manifest must list exactly the edited candidate files')
    return actual


def unchanged(root, ledger, plan_, task, snapshot, record):
    """Whether a session-free completion still describes the current snapshot (coverage recheck).

    Compares content, not commit IDs: an empty commit, a message-only amend or
    a moved merge base with the same trees keeps the settled record valid,
    because its claim fingerprint (and so the cached claim) does not change.
    """
    current = plan(root, ledger, plan_, task, snapshot)
    commits = ('base', 'head')
    return not current['writes'] and _without(current, commits) == _without(record, commits)


def _without(record, keys):
    return {key: value for key, value in record.items() if key not in keys}
