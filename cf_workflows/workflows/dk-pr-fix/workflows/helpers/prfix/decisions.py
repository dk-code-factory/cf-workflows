"""Sourced PR decisions in the installed workflow (#28): cf-workflows:dk-pr-decisions.

The ``reconcile-decisions`` adapter (task kind ``decisions``) runs as an
ordinary task occurrence, one engine-managed native session per changed
binding. This module is its helper side. It ports the procedure of the library
module ``cf_workflows/decisions.py`` so helper containers run it with the
standard library only:

- ``context`` collects the sources (PR body outside the decision section,
  comments, reviews, inline comments, the exact request and recorded answers)
  with durable identities and helper-assigned authority, and the retained,
  affected and changed decisions. Claiming the occurrence puts it in the
  session assignment.
- ``binding`` is the occurrence's claim fingerprint: the discussion, request,
  repository guidance, settings and recorded answers, but not the diff, so
  fix commits alone never start another session.
- ``check`` validates the session's ``deliverables.decisions`` (sourced
  decisions, user-only resolution of contradictions, refresh only through
  changed sources) and ``retain`` records the merged result.
- ``handoff`` is what dependent fix and description occurrences receive.
- ``gate``, ``answer`` and ``status`` are the clarification boundary that
  ``reconciliation.yml`` drives with the engine's ``input/wait``: unresolved
  contradictions and blocking questions ask once per result and block
  dependent work until a later reconciliation settles them.
- ``compose`` routes the bounded Decisions section through the shared
  publication stage as an exact ``edit_pr`` intent, only under an enabled and
  authorized ``edit_pr``.

Nothing here starts a session, writes Git or publishes. Original role prompts
and options come from the pinned dk-workflows baseline (MIT; see
licenses/dk-workflows-MIT.txt).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .capture import MAX_PAGES
from .core import canonical, digest, state_directory

SUITE = 'cf-workflows'
KIND = 'decisions'
ACTION = 'edit_pr'
# Fix and description occurrences consume the handoff (the baseline's "fixing agent").
DEPENDENT = ('fix', 'description')
# The baseline options (dk-workflows skills/dk-pr-decisions/config.json; cf_workflows/decisions.py).
RECONCILIATION_PROMPT = (
    'Reconcile sourced PR decisions with advised and selected alternatives before dependent implementation. '
    'Interpret the user\'s complete current instructions by meaning, including revisions, exceptions, conditions, '
    'exclusions and directions to reconsider earlier decisions; do not require special phrases or keywords. Apply '
    'explicit user direction within its stated scope and host instruction priority without asking for confirmation '
    'already supplied. Preserve unaffected decisions and original evidence. If an advised or selected alternative '
    'contradicts a settled user decision or a repository maintainer/admin decision, first look for an applicable '
    'sourced user resolution. Reuse that answer while its subject, scope, conditions and governing sources still '
    'apply; ordinary commits, fix cycles and new sessions alone are not reasons to ask again. Otherwise explain the '
    'concrete conflict and ask the user which direction to follow, blocking only dependent implementation. Do not '
    'treat recommendations, scores, recency or repository role alone as permission to override a decision. Retain '
    'the exact user answer or instruction and source references in the existing private managed result; reference '
    'original selections rather than copying their analysis. Pass effective commitments, scoped exceptions, '
    'unresolved conflicts and required changes to the fixing agent. Refresh affected reconciliation when relevant '
    'instructions, decisions, alternatives or conditions change, preserving unaffected analysis. Selection and '
    'reconciliation do not expand edit or publication permission.')
ROLE_PROMPT = ('Read the complete PR discussion, user instructions and current Decisions. Distinguish settled choices '
               'from suggestions, open questions and generated progress. Save exact sources. Ask the user about '
               'important unresolved scope choices. Base proposals on evidence and label them as proposals.\n')
LANGUAGE_PROMPT = (
    'Write one material choice per entry. Name the component and chosen behavior. Use short sentences and familiar '
    'words. Remove filler, vague qualifiers, unclear pronouns and wording with multiple meanings. Keep the reasons, '
    'scope, conditions, exceptions and compatibility limits needed to understand the choice. Preserve exact source '
    'links and user quotations. Shortening must not remove meaning or make conditional choices unconditional. '
    'Resolve ambiguity from evidence. If evidence is insufficient, report uncertainty without inventing details or '
    'calling the choice settled.\n')
OPTIONS = {'reconciliation_prompt': RECONCILIATION_PROMPT, 'language_review_passes': 3, 'section_heading': 'Decisions'}

STATUSES = ('settled', 'proposal', 'excluded', 'paused')
GOVERNING = ('user', 'maintainer')
MAINTAINERS = ('OWNER', 'MEMBER', 'COLLABORATOR')
MAX_ITEMS = 256
# Every source the run captured is reconciled: capture reads at most MAX_PAGES pages of 100 comments, reviews
# and inline comments each (capture.py), plus the PR body and the request, with MAX_ITEMS left for answers.
MAX_SOURCES = 3 * MAX_PAGES * 100 + 2 + MAX_ITEMS
MAX_TEXT = 64 << 10
SCHEMA_VERSION = 1
LEDGER_KIND = 'decisions_reconciliation'
CONTRACT = (
    'Return deliverables.decisions as exactly {summary, decisions, contradictions, questions, language_review}. '
    'Each decision is {id, subject, text, status, scope, sources} with optional conditions, revisit and supersedes; '
    'status is settled, proposal, excluded or paused, and sources name context.sources IDs. Each contradiction is '
    '{id, subject, decisions, sources, question, resolution}; resolution is null or {direction, sources} with an '
    'optional decision. Each question is {id, question, sources, blocking}. language_review is {passes, changed, '
    'ambiguities}. Return every decision and contradiction named in context.subjects; keep the others in '
    'context.retained unchanged unless a changed source applies. Propose no operations: the helper drafts the '
    'bounded section and publishes it only under separate authority.')
_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
_HEADING = re.compile(r' {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*')
_MARKER = re.compile(r'<!-- cf-workflows:decision ([a-z0-9][a-z0-9-]{0,63}) -->')


class DecisionError(ValueError):
    """A reconciliation result, its sources or an answer cannot be used; nothing is recorded."""

    def __init__(self, path, message):
        super().__init__(path + ': ' + message)
        self.path, self.message = path, message


def require(condition, path, message):
    if not condition:
        raise DecisionError(path, message)


def text(value, path, limit=MAX_TEXT):
    require(isinstance(value, str) and value.strip() and '\0' not in value, path,
            'must be a nonempty string without NUL')
    try:
        size = len(value.encode('utf-8', 'strict'))
    except UnicodeEncodeError:
        raise DecisionError(path, 'must be valid Unicode text') from None
    require(size <= limit, path, 'exceeds byte bound')
    return value


def fields(value, path, required, optional=()):
    require(isinstance(value, dict), path, 'must be an object')
    unknown = set(value) - set(required) - set(optional)
    require(not unknown, path, 'unsupported field(s): ' + ', '.join(sorted(map(str, unknown))))
    missing = set(required) - set(value)
    require(not missing, path, 'missing field(s): ' + ', '.join(sorted(missing)))
    return value


# -- Plan ---------------------------------------------------------------------------------------

def role(task):
    return task.get('kind') == KIND


def occurrence(plan):
    """The plan's one decisions occurrence, or None."""
    tasks = [task for task in plan.get('tasks', []) if role(task)]
    if len(tasks) > 1:
        raise ValueError('a plan reconciles decisions in exactly one occurrence')
    return tasks[0] if tasks else None


def options(plan):
    """The entry options from ``settings.decisions``; the baseline values by default.

    A custom reconciliation prompt needs the shared semantic validator, which the
    installed workflow does not run, so only the baseline prompt is accepted here.
    """
    raw = (plan.get('settings') or {}).get('decisions', {})
    require(isinstance(raw, dict) and set(raw) <= set(OPTIONS), 'settings.decisions',
            'supports reconciliation_prompt, language_review_passes and section_heading only')
    value = {**OPTIONS, **raw}
    require(value['reconciliation_prompt'] == RECONCILIATION_PROMPT, 'settings.decisions.reconciliation_prompt',
            'a custom reconciliation prompt needs the shared semantic validator; the installed workflow runs '
            'the baseline prompt only')
    require(type(value['language_review_passes']) is int and 1 <= value['language_review_passes'] <= 3,
            'settings.decisions.language_review_passes', 'must be an integer in 1..3')
    text(value['section_heading'], 'settings.decisions.section_heading', 256)
    require(not set(value['section_heading']) & set('\r\n#'), 'settings.decisions.section_heading',
            'heading text without newlines or Markdown markers')
    return value


def permitted(plan):
    """Whether the section may be published: enabled edit_pr plus explicit authorization."""
    enabled = (plan.get('options') or {}).get('permissions', {}).get(ACTION) is True
    return enabled and ACTION in (plan.get('authorizations') or {})


# -- Sources ------------------------------------------------------------------------------------

def split_section(body, heading):
    """(before, section lines or None, after) around the one bounded decision section of a body."""
    lines = body.split('\n')
    found = []
    fence = None
    for number, raw in enumerate(lines):
        line = raw.rstrip('\r')
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            fence = None if fence and stripped.startswith(fence) else (fence or stripped[:3])
            continue
        match = None if fence else _HEADING.fullmatch(line)
        if match and match[2] == heading:
            found.append((number, len(match[1])))
    if not found:
        return body, None, ''
    require(len(found) == 1, 'section', 'the PR body has several "' + heading + '" sections; the bounded section '
            'is ambiguous')
    start, level = found[0]
    end = len(lines)
    fence = None
    for number in range(start + 1, len(lines)):
        line = lines[number].rstrip('\r')
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            fence = None if fence and stripped.startswith(fence) else (fence or stripped[:3])
            continue
        match = None if fence else _HEADING.fullmatch(line)
        if match and len(match[1]) <= level:
            end = number
            break
    return '\n'.join(lines[:start]), [line.rstrip('\r') for line in lines[start:end]], '\n'.join(lines[end:])


def _entries(section):
    """Published entries of the section: (decision ID or None, exact lines)."""
    entries, current = [], None
    for line in section[1:]:
        if line.startswith(('- ', '* ')):
            current = [line]
            entries.append(current)
        elif current is not None and line.startswith(('  ', '\t')) and line.strip():
            current.append(line)
        else:
            current = None
            entries.append([line])
    result = []
    for lines in entries:
        found = _MARKER.search('\n'.join(lines)) if lines[0].startswith(('- ', '* ')) else None
        result.append({'decision': found[1] if found else None, 'lines': lines})
    return result


def _authority(author, association, accounts, host):
    author = author if isinstance(author, dict) else {}
    if author.get('type') == 'Bot' or str(author.get('login', '')).endswith('[bot]'):
        return 'bot'
    if type(author.get('id')) is int and (host, author['id']) in accounts:
        return 'user'
    return 'maintainer' if association in MAINTAINERS else 'participant'


def fingerprint(source):
    """Identity, authority and exact text; edits, author or authority changes refresh what cites it."""
    return digest({key: source[key] for key in ('id', 'kind', 'authority', 'author', 'sha256')})


def collect(plan, discussion, *, heading, answers=(), baseline=None, alternatives=()):
    """Sources with durable identities, helper-assigned authority and discussion order.

    discussion is the PR discussion with this run's own exact effects removed
    (``publication.original_discussion``): title, body, comments, reviews,
    inline comments. baseline is the {identity: fingerprint} discussion present
    at the run's first reconciliation: the request comes after it and before any
    later or since-edited source. Advised and selected alternatives of the PR
    (alternatives.decision_sources: advisory authority) follow, and recorded
    answers come last, so only a later user source resolves a contradiction with
    an alternative. The current decision section of the PR body is a record,
    never a source.
    """
    pr = plan['pr']
    host = pr['host']
    accounts = {(item['host'], item['id']) for item in (plan.get('options') or {}).get('accounts', [])}
    items = []

    def add(ident, kind, body, item, url, created):
        if not isinstance(body, str) or not body.strip():
            return
        author = item.get('user') or {}
        items.append({'id': ident, 'kind': kind, 'text': body, 'url': url, 'public': True,
                      'author': {'id': author.get('id'), 'login': author.get('login')},
                      'authority': _authority(author, item.get('author_association'), accounts, host),
                      'created_at': created})
    meta = discussion.get('metadata') or {}
    before, section, after = split_section(discussion.get('body') or '', heading)
    # Whitespace around the section is not discussion, so publishing it leaves this source unchanged.
    outside = [part.replace('\r\n', '\n').strip('\r\n') for part in (before, after)]
    add('pr-body', 'pr', '\n\n'.join(part for part in outside if part.strip()), meta, pr['url'],
        meta.get('created_at') or '')
    for kind, key in (('comment', 'comments'), ('review', 'reviews'), ('inline', 'inline')):
        for item in discussion.get(key) or []:
            require(isinstance(item, dict) and type(item.get('id')) is int, 'discussion.' + key,
                    'every item needs a numeric ID')
            if kind == 'review' and item.get('state') == 'PENDING':
                continue  # Drafts are not discussion.
            add(f'{kind}:{item["id"]}', kind, item.get('body'), item, item.get('html_url') or pr['url'],
                item.get('created_at') or item.get('submitted_at') or '')
    for item in items:
        item['sha256'] = hashlib.sha256(item['text'].encode()).hexdigest()
    items.sort(key=lambda item: (item['created_at'], item['id']))
    earlier = [item for item in items if baseline is None or baseline.get(item['id']) == fingerprint(item)]
    later = [item for item in items if baseline is not None and baseline.get(item['id']) != fingerprint(item)]
    sources = earlier + [{'id': 'instruction:request', 'kind': 'instruction', 'public': False, 'url': None,
                          'text': plan['instructions'], 'author': None, 'authority': 'user',
                          'created_at': None}] + later + [dict(item) for item in alternatives]
    for answer in answers:
        value = answer['value']
        sources.append({'id': 'answer:' + answer['answer_id'], 'kind': 'answer', 'public': False, 'url': None,
                        'text': value if isinstance(value, str) else canonical(value).decode(),
                        'author': None, 'authority': 'user', 'created_at': None})
    require(len(sources) <= MAX_SOURCES, 'sources', 'discussion exceeds the bounded source count')
    require(len({item['id'] for item in sources}) == len(sources), 'sources', 'duplicate source identity')
    for order, item in enumerate(sources):
        item['order'] = order
        item['sha256'] = hashlib.sha256(item['text'].encode()).hexdigest()
    return sources, {'present': section is not None, 'entries': _entries(section or [])}


# -- Record -------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim TEXT NOT NULL UNIQUE,
    report TEXT NOT NULL,
    context TEXT NOT NULL,
    sources TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE answers (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id TEXT NOT NULL UNIQUE,
    answer_id TEXT NOT NULL UNIQUE,
    evidence_id TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL
)
"""


def ledger_path(directory, logical_run):
    """The logical run's reconciliation record next to its run ledger; run text never forms a path."""
    name = hashlib.sha256(text(logical_run, 'logical run').encode()).hexdigest()
    return Path(directory) / 'runs' / (name + '.reconciliation.sqlite')


def _dumps(value):
    return canonical(value).decode('utf-8')


class Record:
    """Append-only reconciliation results and clarification answers of one logical run and PR."""

    def __init__(self, path, *, logical_run, pr, read_only=False):
        self.path = Path(path)
        expected = {'suite': SUITE, 'kind': LEDGER_KIND, 'logical_run': logical_run, 'pr': pr['url'].lower()}
        if read_only:
            require(not self.path.is_symlink() and self.path.is_file(), 'record',
                    'reconciliation record is unavailable')
            self.db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, isolation_level=None,
                                      timeout=30)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            require(not self.path.is_symlink(), 'record', 'reconciliation record must not be a symlink')
            self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        try:
            if not read_only:
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    version = self.db.execute('PRAGMA user_version').fetchone()[0]
                    tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                                             ).fetchone()[0]
                    if version == 0 and tables == 0:
                        for statement in SCHEMA.split(';'):
                            self.db.execute(statement)
                        self.db.executemany('INSERT INTO meta VALUES (?, ?)', sorted(expected.items()))
                        self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                    self.db.execute('COMMIT')
                except BaseException:
                    self.db.execute('ROLLBACK')
                    raise
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            require(version == SCHEMA_VERSION, 'record', f'unsupported reconciliation record version {version}')
            meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
            meta.pop('request_baseline', None)
            require(meta == expected, 'record', 'reconciliation record belongs to another suite, logical run or PR')
        except (DecisionError, sqlite3.DatabaseError):
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def baseline(self, discussion):
        """The discussion {identity: fingerprint} the request was issued against, fixed at the run's first claim."""
        self.db.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('request_baseline', _dumps(dict(discussion))))
        return json.loads(self.db.execute("SELECT value FROM meta WHERE key = 'request_baseline'").fetchone()[0])

    def retained_baseline(self):
        row = self.db.execute("SELECT value FROM meta WHERE key = 'request_baseline'").fetchone()
        return json.loads(row[0]) if row else None

    def answers(self):
        return [{'gate_id': row['gate_id'], 'answer_id': row['answer_id'], 'evidence_id': row['evidence_id'],
                 'value': json.loads(row['value'])} for row in self.db.execute('SELECT * FROM answers ORDER BY seq')]

    def answer(self, *, gate_id, answer_id, evidence_id, value):
        """Retain one accepted answer; a replay of the same delivery is a no-op, any other reuse conflicts."""
        row = (gate_id, answer_id, evidence_id, _dumps(value))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT gate_id, answer_id, evidence_id, value FROM answers WHERE gate_id = ? '
                                  'OR answer_id = ? OR evidence_id = ?', (gate_id, answer_id, evidence_id)).fetchall()
            if old:
                require(len(old) == 1 and tuple(old[0]) == row, 'answer',
                        'the gate, answer or evidence identity already holds another answer')
            else:
                self.db.execute('INSERT INTO answers (gate_id, answer_id, evidence_id, value) VALUES (?, ?, ?, ?)', row)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def result(self, claim):
        row = self.db.execute('SELECT * FROM results WHERE claim = ?', (claim,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ('sources', 'value'):
            value[key] = json.loads(value[key])
        return value

    def claims(self):
        return [row[0] for row in self.db.execute('SELECT claim FROM results ORDER BY seq DESC')]

    def record(self, *, claim, report, context, sources, value):
        row = (claim, report, context, _dumps(sources), _dumps(value))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT claim, report, context, sources, value FROM results WHERE claim = ?',
                                  (claim,)).fetchone()
            if old is None:
                self.db.execute('INSERT INTO results (claim, report, context, sources, value) VALUES (?, ?, ?, ?, ?)',
                                row)
            else:
                require(tuple(old) == row, 'record', 'the claim already holds another result')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise


class _Unrecorded:
    """A logical run with no reconciliation record yet."""

    def baseline(self, discussion):
        return None

    def retained_baseline(self):
        return None

    def answers(self):
        return []

    def result(self, claim):
        return None

    def claims(self):
        return []

    def close(self):
        pass


def _open(root, plan, *, read_only):
    directory = state_directory(root, missing_ok=read_only)
    path = ledger_path(directory, plan['logical_run']) if directory is not None else None
    if read_only and (path is None or not path.is_file()):
        return _Unrecorded()
    return Record(path, logical_run=plan['logical_run'], pr=plan['pr'], read_only=read_only)


def _answered(ledger, plan):
    """The answers recorded for this run; reading never creates the record."""
    if ledger is None:
        return []
    record = _open(ledger.root, plan, read_only=True)
    try:
        return record.answers()
    finally:
        record.close()


def binding(snapshot, task, ledger=None, plan=None):
    """The occurrence's claim fingerprint.

    It binds the discussion (this run's own exact effects removed), request and
    repository guidance, settings (the capture's digest of the run's options,
    which include the accounts that assign user authority), the PR identity,
    the agent definition and recorded answers. The diff and head are not bound: a fix
    commit alone reuses the retained reconciliation in any later session.
    """
    keys = ('pr', 'author_id', 'actor_id', 'head_repository', 'head_branch', 'target_branch',
            'discussion', 'instructions', 'settings')
    selected = {key: snapshot[key] for key in keys}
    resources = task.get('resources', [])
    if any(path not in snapshot.get('resource_digests', {}) for path in resources):
        raise ValueError('task resource is absent from captured resource identities')
    selected['resources'] = {path: snapshot.get('resource_digests', {}).get(path) for path in resources}
    selected['definition'] = task.get('definition_digest') or snapshot.get('definition_digests', {}).get(
        task.get('definition'))
    answers = _answered(ledger, plan or {'logical_run': ledger.logical_run, 'pr': snapshot['pr']}) if ledger else []
    value = {'snapshot': selected, 'task': {key: value for key, value in task.items() if key != 'required'},
             'answers': [[item['answer_id'], item['evidence_id'], item['value']] for item in answers]}
    advised = _alternatives(ledger.root, snapshot['pr'], task) if ledger else []
    if advised:
        # A new or refreshed choice of the PR's alternatives analysis reconciles again (decision_sources),
        # except the choices of a composed alternatives occurrence that consumes this reconciliation.
        value['alternatives'] = [fingerprint({**item, 'sha256': hashlib.sha256(item['text'].encode()).hexdigest()})
                                 for item in advised]
    return digest(value)


def _alternatives(root, pr, task=None):
    """The PR's advised and selected alternatives as advisory sources; none before any analysis.

    A plan's alternatives occurrence that consumes this reconciliation (prepare.py:
    ``alternatives_consumer``, its record's scope) made its choices against it, so that
    record is not a source: taking it back would make each binding stale the other.
    """
    from . import alternatives
    directory = state_directory(root, missing_ok=True)
    if directory is None:
        return []
    consumer = (task or {}).get('alternatives_consumer')
    return alternatives.decision_sources([item for item in alternatives.selections(directory, pr)
                                          if item['scope'] != consumer])


def reference(ledger):
    """The latest accepted reconciliation report reference, bound by dependent fingerprints; None before one."""
    row = ledger.db.execute("SELECT reference FROM accepted_reports WHERE kind=? ORDER BY rowid DESC LIMIT 1",
                            (KIND,)).fetchone()
    return row[0] if row else None


# -- Session context ----------------------------------------------------------------------------

def _discussion(ledger, plan, evidence):
    from .publication import original_discussion
    meta = evidence.get('metadata') or {}
    require(str(meta.get('html_url', '')).lower() == plan['pr']['url'].lower() and meta.get('number') ==
            plan['pr']['number'], 'capture', 'the capture belongs to another pull request')
    discussion = {'title': meta.get('title'), 'body': meta.get('body'),
                  **{key: evidence.get(key, []) for key in ('comments', 'reviews', 'inline', 'threads')}}
    discussion = original_discussion(ledger, plan, discussion)
    discussion['metadata'] = meta
    return discussion


def _governing(task, values, plan):
    """What a changed value refreshes in full; the heading only shapes the published section."""
    return digest({'task': {key: task.get(key) for key in ('skill', 'definition', 'definition_digest', 'assignment')},
                   'options': {key: value for key, value in values.items() if key != 'section_heading'},
                   'accounts': (plan.get('options') or {}).get('accounts', [])})


def _current(root, ledger, plan, snapshot=None, provider=None):
    """(task, claim row, retained result) of the occurrence's current claim, or (task, None, None)."""
    from . import app
    task = occurrence(plan)
    if task is None:
        return None, None, None
    snapshot = snapshot if snapshot is not None else app._snapshot(root, provider, plan)
    claim = app._current(ledger, snapshot, task)
    if claim is None:
        return task, None, None
    record = _open(root, plan, read_only=True)
    try:
        row = record.result(claim['id'])
    finally:
        record.close()
    if row is None or row['report'] != claim['report']:
        return task, claim, None
    return task, claim, row


def _latest(root, ledger, plan):
    """The newest retained result whose claim completed with that exact report."""
    record = _open(root, plan, read_only=True)
    try:
        for claim in record.claims():
            row = ledger.db.execute("SELECT state, report FROM claims WHERE id=?", (claim,)).fetchone()
            result = record.result(claim)
            if row and row['state'] == 'complete' and row['report'] == result['report']:
                return result
    finally:
        record.close()
    return None


def context(root, provider, ledger, plan, task):
    """The reconciliation context of a new claim (assignment['decisions']).

    Unchanged governing inputs refresh only the decisions and contradictions that
    cite a changed, new or removed source, plus every unresolved contradiction;
    the others are retained verbatim. A changed task, option or account list
    refreshes everything with the previous result as history.
    """
    values = options(plan)
    discussion = _discussion(ledger, plan, provider.capture(plan['pr']))
    record = _open(root, plan, read_only=False)
    try:
        answers = record.answers()
        heading = values['section_heading']
        advised = _alternatives(root, plan['pr'], task)
        sources, _ = collect(plan, discussion, heading=heading, answers=answers, alternatives=advised)
        baseline = record.baseline({item['id']: fingerprint(item) for item in sources
                                    if item['kind'] not in ('instruction', 'answer', 'alternatives')})
    finally:
        record.close()
    sources, published = collect(plan, discussion, heading=heading, answers=answers, baseline=baseline,
                                 alternatives=advised)
    governing = _governing(task, values, plan)
    bound = {item['id']: fingerprint(item) for item in sources}
    latest = _latest(root, ledger, plan)
    previous = latest['value'] if latest is not None else None
    if latest is not None and latest['context'] == governing:
        changed = {key for key in set(bound) | set(latest['sources']) if bound.get(key) != latest['sources'].get(key)}
        affected = {d['id'] for d in previous['decisions'] if set(d['sources']) & changed}
        conflicts = {c['id'] for c in previous['contradictions']
                     if c['resolution'] is None or set(c['decisions']) & affected
                     or set(c['sources'] + c['resolution']['sources']) & changed}
        involved = {d for c in previous['contradictions'] if c['id'] in conflicts for d in c['decisions']}
        retained = {'decisions': [d for d in previous['decisions'] if d['id'] not in affected | involved],
                    'contradictions': [c for c in previous['contradictions'] if c['id'] not in conflicts]}
        subjects = {'sources': sorted(changed & set(bound)), 'removed': sorted(changed - set(bound)),
                    'decisions': sorted(affected | involved), 'contradictions': sorted(conflicts)}
    else:  # First analysis, or governing inputs changed: everything is a subject.
        changed = set(bound)
        retained = {'decisions': [], 'contradictions': []}
        subjects = {'sources': sorted(bound), 'removed': [], 'decisions': [], 'contradictions': [],
                    'full': True if latest is None else 'governing inputs changed'}
    return {'schema_version': 1, 'kind': 'decisions_context', 'skill': 'cf-workflows:dk-pr-decisions',
            'role_prompt': ROLE_PROMPT, 'reconciliation_prompt': values['reconciliation_prompt'],
            'language_prompt': LANGUAGE_PROMPT, 'language_review_passes': values['language_review_passes'],
            'section_heading': heading, 'contract': CONTRACT, 'governing': governing,
            'sources': sources, 'bound': bound, 'published': published, 'changed': sorted(changed),
            'subjects': subjects, 'retained': retained, 'history': previous,
            'previous_report': latest['report'] if latest is not None else None,
            'answers': [{key: item[key] for key in ('answer_id', 'evidence_id', 'value')} for item in answers]}


# -- Result checks ------------------------------------------------------------------------------

def _items(value, path, required, optional=()):
    require(isinstance(value, list) and len(value) <= MAX_ITEMS, path, 'must be a bounded array')
    for number, item in enumerate(value):
        fields(item, f'{path}[{number}]', required, optional)
    return value


def _ids(value, path, known):
    require(isinstance(value, list) and value and all(isinstance(item, str) for item in value)
            and len(set(value)) == len(value), path, 'must be a nonempty array of distinct identities')
    unknown = [item for item in value if item not in known]
    require(not unknown, path, 'unknown identity: ' + ', '.join(sorted(map(str, unknown))))
    return value


def _language(value, limit):
    fields(value, 'result.language_review', ('passes', 'changed', 'ambiguities'))
    passes, changed = value['passes'], value['changed']
    require(type(passes) is int and 1 <= passes <= limit, 'result.language_review.passes',
            f'must be 1..{limit} (the configured language_review_passes)')
    require(isinstance(changed, list) and len(changed) == passes and all(type(item) is bool for item in changed),
            'result.language_review.changed', 'one boolean per pass')
    require(all(changed[:-1]), 'result.language_review.changed', 'review stops at the first unchanged pass')
    require(passes == limit or not changed[-1], 'result.language_review.changed',
            'stopping before the limit needs an unchanged last pass')
    require(isinstance(value['ambiguities'], list) and len(value['ambiguities']) <= MAX_ITEMS,
            'result.language_review.ambiguities', 'must be a bounded array')
    for number, item in enumerate(value['ambiguities']):
        text(item, f'result.language_review.ambiguities[{number}]')
    return value


def check_result(output, *, sources, retained, previous, changed, subjects, passes, published=()):
    """Validate one session result and merge it over the retained, unaffected decisions.

    The same checks as ``cf_workflows.decisions.check_result``; tests hold the two to the same verdicts.
    """
    known = {item['id']: item for item in sources}
    fields(output, 'result', ('summary', 'decisions', 'contradictions', 'questions', 'language_review'))
    text(output['summary'], 'result.summary')
    _language(output['language_review'], passes)
    retained_decisions = {item['id']: item for item in retained['decisions']}
    earlier = {item['id']: item for item in (previous or {}).get('decisions', [])}
    decisions = dict(retained_decisions)
    seen = set()
    for number, item in enumerate(_items(output['decisions'], 'result.decisions',
                                         ('id', 'subject', 'text', 'status', 'scope', 'sources'),
                                         ('conditions', 'revisit', 'supersedes'))):
        at = f'result.decisions[{number}]'
        require(isinstance(item['id'], str) and _ID.fullmatch(item['id']), at + '.id', 'invalid decision ID')
        require(item['id'] not in seen, at + '.id', 'duplicate decision ID')
        seen.add(item['id'])
        for key in ('subject', 'text', 'scope') + tuple(k for k in ('conditions', 'revisit') if k in item):
            text(item[key], f'{at}.{key}')
        require(item['status'] in STATUSES, at + '.status', 'settled, proposal, excluded or paused')
        supersedes = item.get('supersedes', [])
        require(isinstance(supersedes, list) and all(isinstance(other, str) for other in supersedes),
                at + '.supersedes', 'must be an array of decision IDs')
        cited = _ids(item['sources'], at + '.sources', known)
        authorities = {known[ident]['authority'] for ident in cited}
        if item['status'] == 'settled':
            require(authorities & set(GOVERNING), at + '.sources', 'a settled decision cites a user or maintainer '
                    'source; suggestions, bots and the published section alone are proposals')
        if item['status'] in ('excluded', 'paused'):
            require('user' in authorities, at + '.sources', 'only the user excludes or pauses a decision')
        require(item['status'] != 'paused' or 'revisit' in item, at + '.revisit', 'a pause keeps its revisit condition')
        if item['id'] in retained_decisions and item != retained_decisions[item['id']]:
            require(set(cited) & changed, at, 'an unaffected decision changed without a changed or new source')
        old = earlier.get(item['id'])
        if old is not None and old['status'] in ('excluded', 'paused') and item['status'] == 'settled':
            # Revised or excluded agreements return only through a later explicit user source.
            floor = max(known[i]['order'] if i in known else -1 for i in old['sources'])
            require(any(known[i]['authority'] == 'user' and known[i]['order'] > floor and i in changed for i in cited),
                    at + '.sources', 'an excluded or paused decision is restored only by a later user source that '
                    'is itself new or changed')
        decisions[item['id']] = item
    missing = [ident for ident in subjects['decisions'] if ident not in seen]
    require(not missing, 'result.decisions', 'affected decisions must be returned: ' + ', '.join(missing))
    for ident, item in decisions.items():
        for number, other in enumerate(item.get('supersedes', [])):
            require(other in decisions or other in earlier or other in published,
                    f'result.decisions.{ident}.supersedes[{number}]', 'supersedes an unknown decision')
    contradictions = {item['id']: item for item in retained['contradictions']}
    seen = set()
    for number, item in enumerate(_items(output['contradictions'], 'result.contradictions',
                                         ('id', 'subject', 'decisions', 'sources', 'question', 'resolution'))):
        at = f'result.contradictions[{number}]'
        require(isinstance(item['id'], str) and _ID.fullmatch(item['id']) and item['id'] not in seen, at + '.id',
                'invalid or duplicate contradiction ID')
        seen.add(item['id'])
        text(item['subject'], at + '.subject')
        text(item['question'], at + '.question')
        involved = _ids(item['decisions'], at + '.decisions', decisions)
        cited = _ids(item['sources'], at + '.sources', known)
        if item['id'] in contradictions and item != contradictions[item['id']]:
            require(set(cited) & changed, at, 'an unaffected contradiction changed without a changed or new source')
        resolution = item['resolution']
        if resolution is not None:
            fields(resolution, at + '.resolution', ('direction', 'sources'), ('decision',))
            text(resolution['direction'], at + '.resolution.direction')
            require(resolution.get('decision') is None or resolution['decision'] in involved,
                    at + '.resolution.decision', 'resolves toward one of the contradicting decisions')
            answered = _ids(resolution['sources'], at + '.resolution.sources', known)
            # The newest word must be the user's: no stale agreement, role or recommendation outranks it.
            floor = max(known[i]['order'] for i in set(cited).union(*(decisions[d]['sources'] for d in involved)))
            require(any(known[i]['authority'] == 'user' and known[i]['order'] >= floor for i in answered),
                    at + '.resolution.sources', 'only a user source at least as recent as every involved source '
                    'resolves a contradiction')
        contradictions[item['id']] = item
    missing = [ident for ident in subjects['contradictions'] if ident not in seen]
    require(not missing, 'result.contradictions', 'affected or unresolved contradictions must be returned: '
            + ', '.join(missing))
    questions = _items(output['questions'], 'result.questions', ('id', 'question', 'sources', 'blocking'))
    seen = set()
    for number, item in enumerate(questions):
        at = f'result.questions[{number}]'
        require(text(item['id'], at + '.id', 256) not in seen, at + '.id', 'duplicate question ID')
        seen.add(item['id'])
        text(item['question'], at + '.question')
        require(type(item['blocking']) is bool, at + '.blocking', 'must be boolean')
        require(isinstance(item['sources'], list) and all(isinstance(i, str) for i in item['sources'])
                and set(item['sources']) <= set(known), at + '.sources', 'cites unknown sources')
    for ident, item in contradictions.items():
        _ids(item['decisions'], f'contradictions.{ident}.decisions', decisions)
    return {'summary': output['summary'], 'decisions': sorted(decisions.values(), key=lambda d: d['id']),
            'contradictions': sorted(contradictions.values(), key=lambda c: c['id']), 'questions': questions,
            'language_review': output['language_review']}


def check(root, ledger, plan, report, assignment):
    """Validate an accepted session report against the context its claim carried; returns the merged result."""
    ctx = assignment.get('decisions')
    require(isinstance(ctx, dict) and ctx.get('kind') == 'decisions_context', 'assignment',
            'the claim carries no reconciliation context')
    require(not report.get('operations'), 'result', 'a reconciliation proposes no operations; the helper drafts '
            'the bounded section and publishes it only under separate authority')
    deliverables = report.get('deliverables')
    require(isinstance(deliverables, dict) and 'decisions' in deliverables, 'deliverables',
            'the report returns its result in deliverables.decisions')
    current = [{key: item[key] for key in ('answer_id', 'evidence_id', 'value')} for item in _answered(ledger, plan)]
    if current != ctx['answers']:
        raise ValueError('a clarification answer was recorded during the native turn; reconcile again')
    return check_result(deliverables['decisions'], sources=ctx['sources'], retained=ctx['retained'],
                        previous=ctx['history'], changed=set(ctx['changed']), subjects=ctx['subjects'],
                        passes=ctx['language_review_passes'],
                        published={entry['decision'] for entry in ctx['published']['entries'] if entry['decision']})


def retain(root, plan, claim, assignment, value, report):
    """Record the accepted result under its claim; replaying the same acceptance is a no-op."""
    ctx = assignment['decisions']
    record = _open(root, plan, read_only=False)
    try:
        record.record(claim=claim, report=report, context=ctx['governing'], sources=ctx['bound'], value=value)
    finally:
        record.close()


# -- Handoff and clarification ------------------------------------------------------------------

def summary(value):
    """The result's distinct groups: settled, proposals, excluded, paused, contested; unresolved and resolved."""
    unresolved = [item for item in value['contradictions'] if item['resolution'] is None]
    contested = sorted({ident for item in unresolved for ident in item['decisions']})
    blocking = [item for item in value['questions'] if item['blocking']]
    groups = {name: [d for d in value['decisions'] if d['status'] == name and d['id'] not in contested]
              for name in STATUSES}
    return {'decisions': {'settled': groups['settled'], 'proposals': groups['proposal'],
                          'excluded': groups['excluded'], 'paused': groups['paused'],
                          'contested': [d for d in value['decisions'] if d['id'] in contested]},
            'contradictions': {'unresolved': unresolved,
                               'resolved': [item for item in value['contradictions']
                                            if item['resolution'] is not None]},
            'questions': value['questions'], 'blocking': blocking,
            'dependent_work': 'blocked' if unresolved or blocking else 'released'}


def handoff(root, ledger, plan):
    """What dependent occurrences consume: commitments, exceptions and conflicts bound to the accepted result."""
    if occurrence(plan) is None:
        return None
    latest = _latest(root, ledger, plan)
    if latest is None:
        return None
    grouped = summary(latest['value'])
    return {'result': latest['report'], 'commitments': grouped['decisions']['settled'],
            'exceptions': grouped['decisions']['excluded'] + grouped['decisions']['paused'],
            'proposals': grouped['decisions']['proposals'], 'contested': grouped['decisions']['contested'],
            'unresolved': grouped['contradictions']['unresolved'], 'questions': grouped['blocking'],
            'dependent_work': grouped['dependent_work']}


def gate(root, provider, ledger, plan, inputs=None):
    """Whether the current result needs the user: ``ask`` opens one input gate, ``refresh`` reconciles again.

    A result with unresolved contradictions or blocking questions asks once; its
    claim is the gate's evidence identity. After an answer is recorded, the
    occurrence's binding changes, so there is no current claim and the stage
    reconciles again with the answer as the newest user source.
    """
    task, claim, row = _current(root, ledger, plan, provider=provider)
    if task is None or (inputs or {}).get('task', task['id']) != task['id']:
        raise ValueError('the gate belongs to the plan\'s one decisions occurrence')
    if claim is None:
        return {'ask': False, 'refresh': True, 'evidence_id': '', 'prompt': '', 'payload': {}}
    if row is None:
        raise ValueError('the current decisions claim has no retained reconciliation result')
    grouped = summary(row['value'])
    if grouped['dependent_work'] == 'released':
        return {'ask': False, 'refresh': False, 'evidence_id': claim['id'], 'prompt': '', 'payload': {}}
    return {'ask': True, 'refresh': True, 'evidence_id': claim['id'],
            'prompt': 'Choose the direction for these unresolved PR decisions.',
            'payload': {'contradictions': grouped['contradictions']['unresolved'], 'questions': grouped['blocking'],
                        'result': row['report']}}


def answer(root, provider, ledger, plan, inputs):
    """Retain the user's answer to the current result's gate; it becomes a user source of the next result.

    Replaying the recorded delivery is a no-op, although the answer already
    changed the binding so the answered result is no longer current.
    """
    evidence = text(inputs.get('evidence_id'), 'answer.evidence_id', 256)
    delivery = {'gate_id': inputs.get('gate_id'), 'answer_id': inputs.get('answer_id'), 'evidence_id': evidence,
                'value': inputs.get('answer')}
    for recorded in _answered(ledger, plan):
        if recorded == delivery:
            return {'recorded': True, 'evidence_id': evidence}
        require(all(recorded[key] != delivery[key] for key in ('gate_id', 'answer_id', 'evidence_id')), 'answer',
                'the gate, answer or evidence identity already holds another answer')
    task, claim, row = _current(root, ledger, plan, provider=provider)
    if task is None or claim is None or row is None:
        raise ValueError('no current reconciliation result is waiting for an answer')
    require(evidence == claim['id'], 'answer.evidence_id', 'the answer belongs to another reconciliation result')
    require(summary(row['value'])['dependent_work'] == 'blocked', 'answer', 'the current result needs no answer')
    require(inputs.get('answer') is not None, 'answer', 'requires one non-null JSON value')
    record = _open(root, plan, read_only=False)
    try:
        record.answer(gate_id=text(inputs.get('gate_id'), 'answer.gate_id', 256),
                      answer_id=text(inputs.get('answer_id'), 'answer.answer_id', 256),
                      evidence_id=evidence, value=inputs['answer'])
    finally:
        record.close()
    return {'recorded': True, 'evidence_id': evidence}


def status(root, provider, ledger, plan, inputs=None):
    """The current result for dependent work; unresolved contradictions or questions block it."""
    from . import app
    snapshot = app._snapshot(root, provider, plan)
    task, claim, row = _current(root, ledger, plan, snapshot=snapshot)
    if task is None or row is None:
        raise ValueError('no current reconciliation result; dependent work stays blocked')
    grouped = summary(row['value'])
    if grouped['dependent_work'] == 'blocked':
        names = [item['id'] for item in grouped['contradictions']['unresolved'] + grouped['blocking']]
        raise ValueError('dependent work is blocked by unresolved decisions: ' + ', '.join(names))
    return {'result': row['report'], 'dependent_work': 'released',
            'section': 'authorized' if permitted(plan) else 'private',
            'counts': {name: len(values) for name, values in grouped['decisions'].items()}}


# -- Publication --------------------------------------------------------------------------------

def render(decision, sources):
    """One published entry: the decision text, its public source links and its stable marker."""
    known = {item['id']: item for item in sources}
    links = [f"[{known[i]['kind']}]({known[i]['url']})" for i in decision['sources']
             if i in known and known[i]['public'] and known[i]['url']]
    # An HTML comment opener in the text could forge another decision's marker or hide the entry.
    body = ' '.join(decision['text'].split()).replace('<!--', '&lt;!--')
    return f"- {body} ({'; '.join(links)}) <!-- cf-workflows:decision {decision['id']} -->"


def draft(value, body, heading, sources):
    """The PR body with only the bounded decision section changed.

    Settled decisions with a public user or maintainer source are rendered; the
    others are withheld. Existing entries of a decision the user excluded, paused
    or superseded are removed; contested, unmanaged and other entries are kept
    verbatim; nothing outside the section changes.
    """
    grouped = summary(value)['decisions']
    before, section, after = split_section(body, heading)
    known = {item['id']: item for item in sources}
    public = [d for d in grouped['settled']
              if any(i in known and known[i]['public'] and known[i]['authority'] in GOVERNING for i in d['sources'])]
    withheld = [d['id'] for d in grouped['settled'] if d not in public]
    rendered = {d['id']: render(d, sources) for d in public}
    revised = {d['id'] for d in grouped['excluded'] + grouped['paused']} | {
        other for d in grouped['settled'] for other in d.get('supersedes', [])}
    lines, removed, kept, placed = [], [], [], set()
    for entry in _entries(section or []):
        ident = entry['decision']
        if ident in rendered and ident not in placed:
            lines.append(rendered[ident])
            placed.add(ident)
        elif ident is not None and ident in revised and ident not in rendered:
            removed.append(ident)
        else:
            if ident is not None:
                kept.append(ident)
            lines.extend(entry['lines'])
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    lines.extend(rendered[d['id']] for d in public if d['id'] not in placed)
    eol = '\r\n' if '\r\n' in body else '\n'
    if section is None:
        if not lines:
            return {'body': body, 'withheld': withheld, 'removed': [], 'kept': []}
        content = eol.join([f'## {heading}', ''] + lines) + eol
        new = (body.rstrip('\r\n') + eol + eol + content) if body.strip() else content
    else:
        content = eol.join([section[0], ''] + lines) + eol
        new = (before + '\n' if before else '') + content + (eol + after if after else '')
    return {'body': new, 'withheld': withheld, 'removed': removed, 'kept': kept}


def compose(root, provider, ledger, plan, snapshot, listed):
    """Route the bounded Decisions section through the listed publication proposals.

    Only under an enabled and authorized edit_pr, and only from the current
    accepted result. A listed body edit from another accepted report (such as
    describe-pr) carries the section, so one intent writes the body; otherwise
    one edit_pr intent changes only the section of the current body. Nothing is
    listed when the section is already current. The intent ID binds the logical
    run, the result, the heading and the body it edits, so a retained intent is
    only reconciled.
    """
    task = occurrence(plan)
    if task is None or not permitted(plan):
        return listed
    _, claim, row = _current(root, ledger, plan, snapshot=snapshot)
    if row is None:
        return listed  # A stale or missing result never publishes; refreshing the occurrence is the only way.
    heading = options(plan)['section_heading']
    evidence = provider.capture(plan['pr'])
    discussion = _discussion(ledger, plan, evidence)
    record = _open(root, plan, read_only=True)
    try:
        baseline = record.retained_baseline()
        answers = record.answers()
    finally:
        record.close()
    sources, _ = collect(plan, discussion, heading=heading, answers=answers, baseline=baseline)
    source = row['report']
    result, composed = [], False
    for proposal in listed:
        payload = proposal.get('payload') if isinstance(proposal, dict) else None
        if (proposal.get('kind') == ACTION and isinstance(payload, dict) and isinstance(payload.get('body'), str)
                and source not in proposal.get('sources', [])):
            drafted = draft(row['value'], payload['body'], heading, sources)['body']
            if drafted != payload['body']:
                proposal = {**proposal, 'payload': {**payload, 'body': drafted},
                            'sources': list(proposal['sources']) + [source],
                            'id': 'decisions_' + digest([proposal['id'], source, heading])[:48]}
            composed = True
        result.append(proposal)
    if composed:
        return result
    body = (evidence.get('metadata') or {}).get('body') or ''
    drafted = draft(row['value'], body, heading, sources)['body']
    if drafted == body:
        return result
    # The live body is bound too: an edit by someone else after a retained intent is a new intent.
    result.append({'id': 'decisions_' + digest({'run': plan['logical_run'], 'result': source, 'heading': heading,
                                                'body': hashlib.sha256(body.encode()).hexdigest()})[:48],
                   'kind': ACTION, 'head': snapshot['head'], 'lease': snapshot['head'], 'target': snapshot['target'],
                   'payload': {'body': drafted}, 'sources': [source]})
    return result
