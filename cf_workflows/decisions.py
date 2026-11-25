"""Sourced PR decisions and reconciliation: cf-workflows:dk-pr-decisions (contract v1).

The entry is a thin preset over the shared workflow: the bundled
`reconcile-decisions` adapter task followed by the shared publication stage,
with explicitly empty finalizers and reviewers. Its adapter procedure is the
admitted `leaf` role, the bundled assignment and the helper operations here:

- `collect` turns the PR and linked-issue discussion, the exact request and the
  user's recorded answers into sources with durable identities and authority.
  The helper assigns authority from immutable account IDs and repository
  association; the session cannot raise it.
- `reconcile` runs one engine-managed native session per changed binding and
  checks its result. Settled decisions, proposals, exclusions and unresolved
  contradictions stay distinct. Only a user source resolves a contradiction, and
  never an older one than the user sources it involves. Unchanged inputs reuse
  the retained result without a session; changed sources refresh only the
  decisions and contradictions that cite them.
- `draft`, `propose` and `publish` update only the bounded decision section of
  the PR body through the shared exact-intent publication boundary, and only
  under an effective `edit_pr` permission and authorization.

The same procedure serves a composed dk-pr-fix plan that selects the adapter.
Sessions come from the engine transport; nothing here starts a model, provider
or controller. Original role prompts and options come from the pinned
dk-workflows baseline (MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from . import admission, configuration, dk_pr_fix, semantic
from .admission import AdmissionError, digest, fields, require, text

SKILL = 'cf-workflows:dk-pr-decisions'
ADAPTER = 'reconcile-decisions'
ACTION = 'edit_pr'
PRESET = {'tasks': [ADAPTER, {'stage': 'publication'}], 'finalizers': [], 'reviewers': []}
PROMPT_ID = 'dk-pr-decisions-reconciliation'

# Exact baseline bytes (claude/ and codex/ skills/dk-pr-decisions; digests in baselines/inventory.json).
BASELINE_CONFIG = (
    '{\n  "$schema": "./config.schema.json",\n  "reconciliation_prompt": "Reconcile sourced PR decisions with '
    'advised and selected alternatives before dependent implementation. Interpret the user\'s complete current '
    'instructions by meaning, including revisions, exceptions, conditions, exclusions and directions to reconsider '
    'earlier decisions; do not require special phrases or keywords. Apply explicit user direction within its stated '
    'scope and host instruction priority without asking for confirmation already supplied. Preserve unaffected '
    'decisions and original evidence. If an advised or selected alternative contradicts a settled user decision or '
    'a repository maintainer/admin decision, first look for an applicable sourced user resolution. Reuse that answer '
    'while its subject, scope, conditions and governing sources still apply; ordinary commits, fix cycles and new '
    'sessions alone are not reasons to ask again. Otherwise explain the concrete conflict and ask the user which '
    'direction to follow, blocking only dependent implementation. Do not treat recommendations, scores, recency or '
    'repository role alone as permission to override a decision. Retain the exact user answer or instruction and '
    'source references in the existing private managed result; reference original selections rather than copying '
    'their analysis. Pass effective commitments, scoped exceptions, unresolved conflicts and required changes to the '
    'fixing agent. Refresh affected reconciliation when relevant instructions, decisions, alternatives or conditions '
    'change, preserving unaffected analysis. Selection and reconciliation do not expand edit or publication '
    'permission.",\n  "language_review_passes": 3,\n  "section_heading": "Decisions"\n}\n')
ROLE_PROMPT = ('Read the complete PR discussion, user instructions and current Decisions. Distinguish settled choices '
               'from suggestions, open questions and generated progress. Save exact sources. Ask the user about '
               'important unresolved scope choices. Base proposals on evidence and label them as proposals.\n')
# prompts/language-review.md, the baseline language-review role (sha256 6310fb71...3367).
LANGUAGE_PROMPT = (
    'Write one material choice per entry. Name the component and chosen behavior. Use short sentences and familiar '
    'words. Remove filler, vague qualifiers, unclear pronouns and wording with multiple meanings. Keep the reasons, '
    'scope, conditions, exceptions and compatibility limits needed to understand the choice. Preserve exact source '
    'links and user quotations. Shortening must not remove meaning or make conditional choices unconditional. '
    'Resolve ambiguity from evidence. If evidence is insufficient, report uncertainty without inventing details or '
    'calling the choice settled.\n')
OPTIONS = {key: value for key, value in json.loads(BASELINE_CONFIG).items() if key != '$schema'}

STATUSES = ('settled', 'proposal', 'excluded', 'paused')
GOVERNING = ('user', 'maintainer')
MAINTAINERS = ('OWNER', 'MEMBER', 'COLLABORATOR')
MAX_ITEMS = 256
SCHEMA_VERSION = 1
KIND = 'decisions_state'
_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
_HEADING = re.compile(r' {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*')
_MARKER = re.compile(r'<!-- cf-workflows:decision ([a-z0-9][a-z0-9-]{0,63}) -->')


class Blocked(configuration.Blocked):
    """The session result, its sources or a publication precondition cannot be used; nothing is applied."""


def _failure(error):
    if isinstance(error, (AdmissionError, configuration.Blocked)):
        return {'path': error.path, 'message': error.message}
    return {'path': '$', 'message': 'malformed result: ' + type(error).__name__}


# -- Entry preset -------------------------------------------------------------------------------

def check_options(options):
    """The entry's baseline options with provenance; unknown options reject."""
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    result = {key: {'value': value, 'source': 'default'} for key, value in OPTIONS.items()}
    for key, value in options.items():
        where = 'options.' + str(key)
        require(key in OPTIONS, where, 'unsupported option for ' + SKILL)
        if key == 'reconciliation_prompt':
            text(value, where)
        elif key == 'language_review_passes':
            require(type(value) is int and 1 <= value <= 3, where, 'must be an integer in 1..3')
        else:
            text(value, where, 256)
            require(not set(value) & set('\r\n#'), where, 'heading text without newlines or Markdown markers')
        result[key] = {'value': value, 'source': 'invocation'}
    return result


def _rejected(path, message):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}}


def preflight(*, request, configuration, deployment, repository, transport, invocation=None, options=None,
              roots=None, install=dk_pr_fix.INSTALL, ledger=None):
    """Admit one direct cf-workflows:dk-pr-decisions run through the shared pipeline; writes nothing.

    The preset fixes tasks, finalizers, reviewers and permissions; invocation may
    add only prompts. A changed reconciliation_prompt becomes a `reconciliation`
    prompt for the adapter and goes through the shared semantic assessment. The
    only accepted authorization is edit_pr, which still needs the configured
    permission. A default run contacts the engine only for capabilities.
    """
    try:
        entry_options = check_options(options)
        require(isinstance(request, dict), 'request', 'must be an object')
        require('pr' in request, 'request.pr', SKILL + ' reconciles one pull request')
        authorizations = request.get('authorizations') or {}
        require(isinstance(authorizations, dict) and set(authorizations) <= {ACTION}, 'request.authorizations',
                SKILL + ' publishes only its bounded section (edit_pr)')
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'prompts'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and permissions; only prompts may be added')
    except AdmissionError as error:
        return _rejected(error.path, error.message)
    try:
        dk_pr_fix.verify_installation(install)
    except (dk_pr_fix.InstallationError, OSError, ValueError, KeyError) as error:
        return _rejected('installation', str(error) or type(error).__name__)
    roots = dict(roots or {})
    if dk_pr_fix.INSTALL_ROOT in roots or 'repository' in roots:
        return _rejected('roots', 'installation and repository roots are supplied by preflight')
    if not Path(repository).is_dir() or not dk_pr_fix._disjoint(repository, install):
        return _rejected('repository', 'target checkout must be a directory separate from the installation')
    prompts = copy.deepcopy(invocation.get('prompts', []))
    custom = entry_options['reconciliation_prompt']
    if custom['source'] == 'invocation' and custom['value'] != OPTIONS['reconciliation_prompt']:
        prompts.append({'id': PROMPT_ID, 'slot': 'reconciliation', 'target': ADAPTER, 'text': custom['value']})
    merged = {'options': copy.deepcopy(PRESET), 'prompts': prompts}
    result = semantic.admit(request=copy.deepcopy(request), configuration=copy.deepcopy(configuration),
                            invocation=merged, deployment=deployment, roles=dk_pr_fix.role_catalog(install),
                            roots={**roots, 'repository': repository, dk_pr_fix.INSTALL_ROOT: install},
                            transport=transport, ledger=ledger)
    result['entry'] = {'skill': SKILL, 'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    snapshot = result['snapshot']
    permission = snapshot['permissions'][ACTION]
    if permission['authorized'] and not permission['effective']:
        return {**result, 'preflight': 'rejected', 'error': {
            'path': 'request.authorizations.' + ACTION,
            'message': 'the authorized action is not enabled by accepted configuration; enable it in a scoped '
                       'configuration or remove the authorization'}}
    [task] = snapshot['plan']['tasks']
    assert task['adapter'] == ADAPTER and [s['kind'] for s in snapshot['plan']['stages']] == ['tasks', 'publication']
    assert not any(value['effective'] for name, value in snapshot['permissions'].items() if name != ACTION)
    result['entry'].update(adapter=ADAPTER, task=task, publication=permission['effective'])
    return result


def _task(admitted):
    tasks = [task for task in admitted['snapshot']['plan']['tasks'] if task['adapter'] == ADAPTER]
    require(len(tasks) == 1, 'plan.tasks', 'the plan must select ' + ADAPTER + ' exactly once')
    return tasks[0]


def effective(admitted):
    """The governing role behavior, reconciliation prompt and options of this occurrence."""
    entry = admitted.get('entry') or {}
    values = {key: value['value'] for key, value in (entry.get('options') or check_options(None)).items()}
    applicable = [item for item in admitted['snapshot']['prompts']
                  if item['applicable'] and item['target'] in (None, ADAPTER)]
    replacement = [item for item in applicable if item['slot'] == 'behavior_replacement']
    reconciliation = [item for item in applicable if item['slot'] == 'reconciliation']
    require(len(replacement) <= 1 and len(reconciliation) <= 1, 'prompts',
            'at most one behavior replacement and one reconciliation prompt apply to ' + ADAPTER)
    role = replacement[0]['text'] if replacement else ROLE_PROMPT
    if replacement:  # A complete behavior replacement suppresses the reconciliation prompt.
        prompt, origin = None, None
    elif reconciliation:
        prompt, origin = reconciliation[0]['text'], 'prompt:' + reconciliation[0]['id']
    else:
        prompt, origin = values['reconciliation_prompt'], 'default'
    return {'role_prompt': role, 'reconciliation_prompt': prompt, 'reconciliation_source': origin,
            'additions': [{key: item[key] for key in ('id', 'slot', 'text')} for item in applicable
                          if item['slot'] not in ('behavior_replacement', 'reconciliation')],
            'language_prompt': LANGUAGE_PROMPT, 'language_review_passes': values['language_review_passes'],
            'section_heading': values['section_heading']}


# -- Sources ------------------------------------------------------------------------------------

def split_section(body, heading):
    """(before, section lines or None, after) around the one bounded decision section of a body.

    Lines split on LF; a trailing CR (CRLF bodies from the web editor) is ignored for
    matching, and before/after keep their original line endings.
    """
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
    if len(found) > 1:
        raise Blocked('section', 'the PR body has several "' + heading + '" sections; the bounded section is ambiguous')
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


def _authority(author, association, accounts, host):
    author = author if isinstance(author, dict) else {}
    if author.get('type') == 'Bot' or str(author.get('login', '')).endswith('[bot]'):
        return 'bot'
    if type(author.get('id')) is int and (host, author['id']) in accounts:
        return 'user'
    return 'maintainer' if association in MAINTAINERS else 'participant'


def collect(admitted, capture, *, issues=(), answers=(), baseline=None):
    """Sources with durable identities, helper-assigned authority and discussion order.

    capture is the shared PR capture (metadata, comments, reviews, inline); issues
    are captured linked issues ({number, url, body, user, author_association,
    created_at, comments}); answers are the user's recorded gate answers. baseline
    is the set of discussion identities present when the request was first
    reconciled: the request comes after those and before any later discussion,
    so a later user comment outranks it (None: all discussion precedes it).
    Gate answers come last. The
    current decision section of the PR body is a record, never a source: it cannot
    settle, restore or revise a decision.
    """
    snapshot = admitted['snapshot']
    pr = snapshot['request']['pr']
    heading = effective(admitted)['section_heading']
    host = pr['repository'].split('/', 1)[0]
    accounts = {(item['host'], item['id']) for item in snapshot['options']['accounts']['value']}
    require(isinstance(capture, dict), 'capture', 'must be an object')
    meta = capture.get('metadata') or {}
    require(str(meta.get('html_url', '')).lower() == pr['url'] and meta.get('number') == pr['number'],
            'capture.metadata', 'the capture belongs to another pull request')
    discussion = []

    def add(ident, kind, body, item, url, created):
        if not isinstance(body, str) or not body.strip():
            return
        author = item.get('user') or {}
        discussion.append({'id': ident, 'kind': kind, 'text': body, 'url': url, 'public': True,
                           'author': {'id': author.get('id'), 'login': author.get('login')},
                           'authority': _authority(author, item.get('author_association'), accounts, host),
                           'created_at': created})
    before, section, after = split_section(meta.get('body') or '', heading)
    # Whitespace around the section is not discussion, so publishing it leaves this source unchanged.
    outside = [part.replace('\r\n', '\n').strip('\r\n') for part in (before, after)]
    add('pr-body', 'pr', '\n\n'.join(part for part in outside if part.strip()), meta, pr['url'],
        meta.get('created_at') or '')
    for kind, key in (('comment', 'comments'), ('review', 'reviews'), ('inline', 'inline')):
        for item in capture.get(key) or []:
            require(type(item.get('id')) is int, f'capture.{key}', 'every item needs a numeric ID')
            if kind == 'review' and item.get('state') == 'PENDING':
                continue  # Drafts are not discussion.
            add(f'{kind}:{item["id"]}', kind, item.get('body'), item, item.get('html_url') or pr['url'],
                item.get('created_at') or item.get('submitted_at') or '')
    for issue in issues:
        require(type(issue.get('number')) is int, 'issues', 'every issue needs a number')
        add(f'issue:{issue["number"]}', 'issue', issue.get('body'), issue, issue.get('url'), issue.get('created_at') or '')
        for item in issue.get('comments') or []:
            add(f'issue-comment:{issue["number"]}:{item["id"]}', 'issue-comment', item.get('body'), item,
                item.get('html_url') or issue.get('url'), item.get('created_at') or '')
    discussion.sort(key=lambda item: (item['created_at'], item['id']))
    earlier = [item for item in discussion if baseline is None or item['id'] in baseline]
    later = [item for item in discussion if baseline is not None and item['id'] not in baseline]
    sources = earlier + [{'id': 'instruction:request', 'kind': 'instruction', 'public': False, 'url': None,
                             'text': snapshot['request']['original_instructions'], 'author': None,
                             'authority': 'user', 'created_at': None}] + later
    for answer in answers:
        value = answer['value']
        sources.append({'id': 'answer:' + answer['answer_id'], 'kind': 'answer', 'public': False, 'url': None,
                        'text': value if isinstance(value, str) else admission.canonical(value).decode(),
                        'author': None, 'authority': 'user', 'created_at': None})
    require(len(sources) <= MAX_ITEMS, 'sources', 'discussion exceeds the bounded source count')
    require(len({item['id'] for item in sources}) == len(sources), 'sources', 'duplicate source identity')
    for order, item in enumerate(sources):
        item['order'] = order
        item['sha256'] = hashlib.sha256(item['text'].encode()).hexdigest()
    return sources, {'present': section is not None, 'entries': _entries(section or [])}


def fingerprint(source):
    """Identity, authority and exact text; edits, author or authority changes refresh what cites it."""
    return digest({key: source[key] for key in ('id', 'kind', 'authority', 'author', 'sha256')})


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


# -- Ledger -------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim TEXT NOT NULL UNIQUE,
    context TEXT NOT NULL,
    sources TEXT NOT NULL,
    binding TEXT NOT NULL,
    session TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE rejections (
    claim TEXT PRIMARY KEY,
    binding TEXT NOT NULL,
    session TEXT,
    error TEXT NOT NULL
)
"""


def ledger_path(state_directory, logical_run):
    """The logical run's decision record next to its run ledger; run text never forms a path."""
    name = hashlib.sha256(text(logical_run, 'logical_run').encode()).hexdigest()
    return Path(state_directory) / 'runs' / (name + '.decisions.sqlite')


class Ledger:
    """Append-only reconciliation results of one logical run and PR; history is never rewritten."""

    def __init__(self, path, *, logical_run, pr):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not self.path.is_symlink(), 'ledger', 'decision ledger must not be a symlink')
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        expected = {'suite': admission.SUITE, 'kind': KIND, 'logical_run': logical_run, 'pr': pr.lower()}
        try:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                if version == 0 and tables == 0:
                    for statement in SCHEMA.split(';'):
                        self.db.execute(statement)
                    self.db.executemany('INSERT INTO meta VALUES (?, ?)', sorted(expected.items()))
                    self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                    version = SCHEMA_VERSION
                require(version == SCHEMA_VERSION, 'ledger', f'unsupported decision ledger version {version}')
                meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
                meta.pop('request_baseline', None)
                require(meta == expected, 'ledger', 'decision ledger belongs to another suite, logical run or PR')
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
        for key in ('sources', 'value'):
            value[key] = json.loads(value[key])
        return value

    def latest(self):
        return self._record(self.db.execute('SELECT * FROM results ORDER BY seq DESC LIMIT 1').fetchone())

    def claimed(self, claim):
        return self._record(self.db.execute('SELECT * FROM results WHERE claim = ?', (claim,)).fetchone())

    def history(self):
        return [self._record(row) for row in self.db.execute('SELECT * FROM results ORDER BY seq')]

    def baseline(self, discussion):
        """The discussion identities the request was issued against, fixed at the run's first call."""
        self.db.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)',
                        ('request_baseline', admission.canonical(sorted(discussion)).decode()))
        row = self.db.execute("SELECT value FROM meta WHERE key = 'request_baseline'").fetchone()
        return set(json.loads(row[0]))

    def attempts(self, binding):
        """Rejected attempts of one binding; each later attempt gets a fresh claim."""
        return self.db.execute('SELECT count(*) FROM rejections WHERE binding = ?', (binding,)).fetchone()[0]

    def reject(self, *, claim, binding, session, error):
        """Retain why a claim's result was not accepted; the retained claim is never redispatched."""
        self.db.execute('INSERT OR IGNORE INTO rejections VALUES (?, ?, ?, ?)',
                        (claim, binding, session, admission.canonical(error).decode()))

    def record(self, *, claim, context, sources, binding, session, result_ref, value):
        row = (claim, context, admission.canonical(sources).decode(), binding, session, result_ref,
               admission.canonical(value).decode())
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT * FROM results WHERE claim = ?', (claim,)).fetchone()
            if old is None:
                self.db.execute('INSERT INTO results (claim, context, sources, binding, session, result_ref, value) '
                                'VALUES (?, ?, ?, ?, ?, ?, ?)', row)
            elif tuple(old)[1:] != row:
                raise Blocked('ledger', 'the claim already holds another result')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.claimed(claim)


def source_validator(ledger):
    """Publication-source check: only the current retained result is accepted evidence."""
    def valid(source):
        latest = ledger.latest()
        return latest is not None and source == 'decisions:' + latest['result_ref']
    return valid


# -- Result checks ------------------------------------------------------------------------------

def _items(value, path, required, optional=()):
    require(isinstance(value, list) and len(value) <= MAX_ITEMS, path, 'must be a bounded array')
    for number, item in enumerate(value):
        fields(item, f'{path}[{number}]', required, optional)
    return value


def _ids(value, path, known):
    require(isinstance(value, list) and value and len(set(map(str, value))) == len(value), path,
            'must be a nonempty array of distinct identities')
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
    """Validate one session result and merge it over the retained, unaffected decisions."""
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
    for number, item in enumerate(questions):
        at = f'result.questions[{number}]'
        text(item['question'], at + '.question')
        require(type(item['blocking']) is bool, at + '.blocking', 'must be boolean')
        require(isinstance(item['sources'], list) and set(item['sources']) <= set(known), at + '.sources',
                'cites unknown sources')
    for ident, item in contradictions.items():
        _ids(item['decisions'], f'contradictions.{ident}.decisions', decisions)
    return {'summary': output['summary'], 'decisions': sorted(decisions.values(), key=lambda d: d['id']),
            'contradictions': sorted(contradictions.values(), key=lambda c: c['id']), 'questions': questions,
            'language_review': output['language_review']}


# -- Reconciliation -----------------------------------------------------------------------------

def request_id(admitted):
    snapshot = admitted['snapshot']
    return 'decreq_' + digest({'skill': SKILL, 'logical_run': snapshot['request']['logical_run'],
                               'pr': snapshot['request']['pr']['url'],
                               'instructions': snapshot['request']['original_instructions']})[:32]


def _context(admitted, task, governing):
    snapshot = admitted['snapshot']
    return digest({'task': {key: task[key] for key in ('adapter', 'definition', 'definition_sha256',
                                                        'assignment_sha256', 'instructions')},
                   'governing': {key: governing[key] for key in governing if key != 'section_heading'},
                   'requirements': [item['text'] for item in snapshot['options']['requirements']['value']],
                   'prohibitions': [item['text'] for item in snapshot['options']['prohibitions']['value']],
                   'accounts': snapshot['options']['accounts']['value'],
                   'agent': semantic.validator_agent(admitted)})


def assignment(admitted, task, governing, *, claim, sources, published, subjects, retained, previous, answers):
    """The exact typed assignment of the adapter's engine-managed native session."""
    snapshot = admitted['snapshot']
    agent, provider = semantic.validator_agent(admitted)
    definition = snapshot['adapters'][ADAPTER]
    return {
        'schema_version': 1, 'kind': 'decisions_task', 'suite': admission.SUITE, 'skill': SKILL,
        'mode': 'reconcile', 'claim': claim, 'adapter': ADAPTER, 'task': task['id'], 'role': task['definition'],
        'definition_sha256': task['definition_sha256'], 'assignment': definition['assignment'],
        'checklist': definition['checklist'], 'task_instructions': task['instructions'],
        'role_prompt': governing['role_prompt'], 'reconciliation_prompt': governing['reconciliation_prompt'],
        'additions': governing['additions'], 'language_prompt': governing['language_prompt'],
        'language_review_passes': governing['language_review_passes'],
        'instructions': snapshot['request']['original_instructions'],
        'requirements': [item['text'] for item in snapshot['options']['requirements']['value']],
        'prohibitions': [item['text'] for item in snapshot['options']['prohibitions']['value']],
        'agent': agent, 'provider': provider, 'skills': [], 'tools': 'read-only', 'writes': False,
        'publication': False, 'readable': {},
        'context': {'sources': copy.deepcopy(sources), 'published': copy.deepcopy(published),
                    'subjects': copy.deepcopy(subjects), 'retained': copy.deepcopy(retained),
                    'history': copy.deepcopy(previous)},
        'clarifications': copy.deepcopy(answers),
    }


def _report(row, *, sources, session, refreshed, gate=None, status=None):
    value = row['value']
    unresolved = [item for item in value['contradictions'] if item['resolution'] is None]
    contested = sorted({ident for item in unresolved for ident in item['decisions']})
    blocking = [item for item in value['questions'] if item['blocking']]
    by_status = {name: [d for d in value['decisions'] if d['status'] == name and d['id'] not in contested]
                 for name in STATUSES}
    cited = sorted({i for d in value['decisions'] for i in d['sources']}
                   | {i for c in value['contradictions'] for i in c['sources'] + (c['resolution'] or {}).get('sources', [])})
    known = {item['id']: item for item in sources}
    report = {
        'skill': SKILL, 'status': status or ('waiting' if gate else 'complete' if not unresolved and not blocking
                                             else 'contested'),
        'result_ref': row['result_ref'], 'claim': row['claim'], 'session': session, 'refreshed': refreshed,
        'decisions': {'settled': by_status['settled'], 'proposals': by_status['proposal'],
                      'excluded': by_status['excluded'], 'paused': by_status['paused'],
                      'contested': [d for d in value['decisions'] if d['id'] in contested]},
        'contradictions': {'unresolved': unresolved,
                           'resolved': [item for item in value['contradictions'] if item['resolution'] is not None]},
        'questions': value['questions'], 'language_review': value['language_review'],
        'dependent_work': 'blocked' if unresolved or blocking else 'released',
        'sources': [{key: known[i][key] for key in ('id', 'kind', 'authority', 'url', 'public', 'order')}
                    for i in cited if i in known],
        'summary': value['summary'],
    }
    if gate:
        report['gate'] = gate
    return report


def handoff(report):
    """What dependent tasks consume: effective commitments, exceptions, conflicts, bound to the result."""
    return {'result_ref': report['result_ref'], 'commitments': report['decisions']['settled'],
            'exceptions': report['decisions']['excluded'] + report['decisions']['paused'],
            'unresolved': report['contradictions']['unresolved'],
            'questions': [item for item in report['questions'] if item['blocking']],
            'dependent_work': report['dependent_work']}


def reconcile(admitted, *, capture, ledger, transport, issues=()):
    """Run (or reuse) one reconciliation of the adapter occurrence and return its private report.

    admitted is this entry's preflight result or a dk-pr-fix preflight whose plan
    selects the adapter. transport offers native_session(task), input_gates(request_id)
    and open_input_gate(request). Unchanged sources, answers and governing inputs
    reuse the retained result with no session. A waiting gate returns with no
    session. A lost session response is reconciled through its claim.
    """
    try:
        require(admitted.get('preflight') == 'admitted', 'admission', 'reconciliation needs an admitted run')
        task = _task(admitted)
        governing = effective(admitted)
    except AdmissionError as error:
        return {'skill': SKILL, 'status': 'rejected', 'error': _failure(error)}
    ident = request_id(admitted)
    try:
        gates = semantic.check_gates(transport.input_gates(ident), ident)
    except Exception as failure:
        return {'skill': SKILL, 'status': 'blocked', 'error': {'path': 'gates', 'message': 'input gates unavailable: '
                                                               + type(failure).__name__}}
    waiting = [gate for gate in gates if gate['status'] == 'waiting']
    if waiting:  # Interrupted clarification resumes without a new session or gate.
        return {'skill': SKILL, 'status': 'waiting', 'dependent_work': 'blocked',
                'gate': {key: waiting[0][key] for key in ('gate_id', 'evidence_id', 'status')},
                'questions': waiting[0]['request']['payload']}
    answers = [{'gate_id': gate['gate_id'], 'answer_id': gate['answer']['answer_id'],
                'value': gate['answer']['value']} for gate in gates]
    try:
        sources, published = collect(admitted, capture, issues=issues, answers=answers)
        baseline = ledger.baseline(item['id'] for item in sources if item['kind'] not in ('instruction', 'answer'))
        sources, published = collect(admitted, capture, issues=issues, answers=answers, baseline=baseline)
    except (AdmissionError, configuration.Blocked, TypeError, KeyError, AttributeError) as error:
        return {'skill': SKILL, 'status': 'blocked', 'error': _failure(error)}
    context = _context(admitted, task, governing)
    bound = {item['id']: fingerprint(item) for item in sources}
    binding = digest({'context': context, 'sources': bound})
    latest = ledger.latest()

    def finish(row, session, refreshed):
        unresolved = [c for c in row['value']['contradictions'] if c['resolution'] is None]
        blocking = [q for q in row['value']['questions'] if q['blocking']]
        gate = None
        if unresolved or blocking:
            opened = [g for g in gates if g['evidence_id'] == row['claim']]
            if not opened:  # One gate per result; a lost gate response is reopened from the retained result.
                gate = configuration._gate(transport, ident, row['claim'],
                                           'Choose the direction for these unresolved PR decisions.',
                                           {'contradictions': unresolved, 'questions': blocking,
                                            'result_ref': row['result_ref']})
        return _report(row, sources=sources, session=session, refreshed=refreshed, gate=gate)
    if latest is not None and latest['binding'] == binding:
        return finish(latest, False, [])
    previous = latest['value'] if latest is not None else None
    if latest is not None and latest['context'] == context:
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
    rejected = ledger.attempts(binding)
    allowed = 1 + admitted['snapshot']['limits']['retries_per_task']
    if rejected >= allowed:
        return {'skill': SKILL, 'status': 'blocked', 'dependent_work': 'blocked',
                'error': {'path': 'attempts', 'message': f'{rejected} rejected results for unchanged inputs; '
                          'change the inputs or grant a continuation'}}
    claim = ident + '-' + binding[:16] + ('' if rejected == 0 else f'-r{rejected}')
    known = ledger.claimed(claim)
    if known is not None:
        return finish(known, False, subjects['decisions'])
    task_record = assignment(admitted, task, governing, claim=claim, sources=sources, published=published,
                             subjects=subjects, retained=retained, previous=previous, answers=answers)
    try:
        reference, session_id, output = configuration.run_session(transport, task_record)
    except configuration.Blocked as error:
        if error.path == 'session':  # Lost or failed dispatch: the same claim reconciles it later.
            return {'skill': SKILL, 'status': 'blocked', 'claim': claim, 'error': _failure(error),
                    'dependent_work': 'blocked'}
        session_id, failure = None, _failure(error)
    else:
        try:
            value = check_result(output, sources=sources, retained=retained, previous=previous, changed=changed,
                                 subjects=subjects, passes=governing['language_review_passes'],
                                 published={entry['decision'] for entry in published['entries'] if entry['decision']})
            failure = None
        except (AdmissionError, configuration.Blocked) as error:
            failure = _failure(error)
        except Exception as error:
            failure = {'path': 'result', 'message': 'not accepted: ' + type(error).__name__}
    if failure is not None:
        ledger.reject(claim=claim, binding=binding, session=session_id, error=failure)
        return {'skill': SKILL, 'status': 'blocked', 'claim': claim, 'error': failure, 'dependent_work': 'blocked'}
    row = ledger.record(claim=claim, context=context, sources=bound, binding=binding, session=session_id,
                        result_ref=reference, value=value)
    return finish(row, True, sorted({d['id'] for d in value['decisions']} - {d['id'] for d in retained['decisions']}))


# -- Publication --------------------------------------------------------------------------------

def render(decision, sources):
    """One published entry: the decision text, its public source links and its stable marker."""
    known = {item['id']: item for item in sources}
    links = [f"[{known[i]['kind']}]({known[i]['url']})" for i in decision['sources']
             if i in known and known[i]['public'] and known[i]['url']]
    body = ' '.join(decision['text'].split())
    return f"- {body} ({'; '.join(links)}) <!-- cf-workflows:decision {decision['id']} -->"


def draft(report, body, heading, sources):
    """The PR body with only the bounded decision section changed.

    Settled decisions with a public user or maintainer source are rendered; the
    others are withheld. Existing entries of a decision the user excluded, paused
    or superseded are removed; contested, unmanaged and other
    entries are kept verbatim; nothing outside the section changes.
    """
    before, section, after = split_section(body, heading)
    known = {item['id']: item for item in sources}
    # Published only on a public source that itself carries the decision's authority.
    public = [d for d in report['decisions']['settled']
              if any(i in known and known[i]['public'] and known[i]['authority'] in GOVERNING for i in d['sources'])]
    withheld = [d['id'] for d in report['decisions']['settled'] if d not in public]
    rendered = {d['id']: render(d, sources) for d in public}
    revised = {d['id'] for d in report['decisions']['excluded'] + report['decisions']['paused']} | {
        other for d in report['decisions']['settled'] for other in d.get('supersedes', [])}
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
    eol = '\r\n' if '\r\n' in body else '\n'  # The section follows the body's own line endings.
    if section is None:
        if not lines:
            return {'body': body, 'withheld': withheld, 'removed': [], 'kept': []}
        content = eol.join([f'## {heading}', ''] + lines) + eol
        new = (body.rstrip('\r\n') + eol + eol + content) if body.strip() else content
    else:
        content = eol.join([section[0], ''] + lines) + eol
        new = (before + '\n' if before else '') + content + (eol + after if after else '')
    return {'body': new, 'withheld': withheld, 'removed': removed, 'kept': kept}


def publication_plan(admitted):
    """The shared publication plan for this occurrence: edit_pr only, and only when effective."""
    snapshot = admitted['snapshot']
    pr = snapshot['request']['pr']
    host, owner, name = pr['repository'].split('/')
    effective_ = snapshot['permissions'][ACTION]['effective']
    return {'logical_run': snapshot['request']['logical_run'],
            'pr': {'url': pr['url'], 'host': host, 'owner': owner, 'name': name, 'repository': pr['repository'],
                   'number': pr['number']},
            'instructions': snapshot['request']['original_instructions'],
            'authorizations': {ACTION: snapshot['request']['authorizations'][ACTION]} if effective_ else {},
            'options': {'permissions': {ACTION: effective_}}}


def propose(admitted, report, capture):
    """The exact edit_pr proposal for the section, or why there is none. Writes nothing."""
    governing = effective(admitted)
    sources, _ = collect(admitted, capture)
    meta = capture['metadata']
    body = meta.get('body') or ''
    result = draft(report, body, governing['section_heading'], sources)
    # One intent identity per retained result and heading, whatever the body holds now.
    ident = 'decisions-' + digest({'run': admitted['snapshot']['request']['logical_run'],
                                   'result': report['result_ref'], 'heading': governing['section_heading']})[:32]
    if not admitted['snapshot']['permissions'][ACTION]['effective']:
        return {'status': 'not_authorized', 'id': ident, 'draft': result}
    if result['body'] == body:
        return {'status': 'unchanged', 'id': ident, 'draft': result}
    return {'status': 'proposed', 'id': ident, 'draft': result, 'proposal': {
        'id': ident, 'kind': ACTION, 'head': meta['head']['sha'], 'target': meta['base']['sha'],
        'payload': {'body': result['body']}, 'sources': ['decisions:' + report['result_ref']]}}


def _journal_state(publisher, ident):
    """The intent's journal state from its append-only event history (the shared helper's public read)."""
    try:
        events = {item['event'] for item in publisher.events(ident)}
    except Exception:
        return None  # Never prepared.
    for event, state in (('applied', 'applied'), ('rejected', 'not_applied'), ('blocked', 'blocked'),
                         ('dispatch', 'pending')):
        if event in events:
            return state
    return 'prepared'


def publish(admitted, report, *, publisher, audience):
    """Publish the settled section through the shared exact-intent boundary when authorized.

    publisher is the shared publication helper (prfix.publication.Publication)
    built from publication_plan() with source_validator(ledger); audience(intent)
    returns the independent audience assessment of the exact intent. A pending
    intent is only reconciled, never resent; a blocked one is never retried here.
    """
    if not report.get('result_ref'):
        return {'status': 'blocked', 'error': {'path': 'report', 'message': 'no current reconciliation result'}}
    try:
        planned = propose(admitted, report, publisher.provider.capture(publisher.pr))
    except (AdmissionError, configuration.Blocked) as error:
        return {'status': 'blocked', 'error': _failure(error)}
    retained = _journal_state(publisher, planned['id']) if planned['status'] != 'not_authorized' else None
    if retained in ('pending', 'applied'):
        # A dispatched intent is only reconciled from what the service shows, never sent again.
        try:
            receipt = publisher.execute(planned['id'], None)
        except Exception as error:
            return {'status': _journal_state(publisher, planned['id']), 'draft': planned['draft'],
                    'intent': planned['id'], 'error': {'path': 'publication', 'message': str(error)}}
        return {'status': 'applied', 'draft': planned['draft'], 'intent': planned['id'], 'receipt': receipt}
    if planned['status'] != 'proposed':
        return planned
    intent = None
    try:
        intent = publisher.prepare(planned['proposal'])
        receipt = publisher.execute(intent['id'], audience(intent))
    except Exception as error:
        # The journal, not the exception type, says whether the write may have happened.
        state = _journal_state(publisher, intent['id']) if intent is not None else 'blocked'
        return {'status': {'prepared': 'blocked'}.get(state, state), 'draft': planned['draft'],
                'intent': intent['id'] if intent is not None else None,
                'error': {'path': 'publication', 'message': str(error) or type(error).__name__}}
    return {'status': 'applied', 'draft': planned['draft'], 'intent': intent['id'], 'receipt': receipt}
