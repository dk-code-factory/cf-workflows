"""Feedback implementation dispositions, response obligations and thread resolution (contract v1).

Three decisions are kept apart for every feedback source:

- implementation: whether the code changed (fixed, intentional, rejected, deferred or none);
- response: whether the PR author owes an answer, and how it is settled (a verified
  reply, an existing verified answer, or a sourced no-reply reason);
- resolution: whether an inline thread may be resolved, which needs a fully answered
  thread and its own authority.

Declining a code change never removes a concrete author request's response
obligation. Bot authorship and style classification have no effect on it. Only a
question for another participant or discussion needing no author reply can be left
unanswered, and each such decision is retained privately with its reason.

The record is a versioned SQLite database per logical run in the suite's state
namespace. Discussion is read from one GitHub capture per observation; nothing here
publishes, and a reply counts only once it is observed in the exact location.

This module is part of the installed helper closure, so it uses only the
standard library and runs inside helper containers. The response stage
(``responses.py``) drives it from the dk-pr-fix workflow.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

SUITE = 'cf-workflows'
SCHEMA_VERSION = 1
KIND = 'feedback_state'
COLLECTIONS = {'comment': 'comments', 'review': 'reviews', 'inline': 'inline'}
ADDRESSEES = ('author', 'participant', 'none')
IMPLEMENTATIONS = ('fixed', 'intentional', 'rejected', 'deferred', 'none')
NO_REPLY = {'participant': 'addressed_to_participant', 'none': 'no_author_reply_needed'}
REQUEST_FIELDS = {'id', 'excerpt', 'addressee', 'participant', 'category', 'implementation', 'instructed', 'response'}
_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
_LOGIN = re.compile(r'^[A-Za-z0-9-]{1,39}(\[bot\])?$')


class FeedbackError(ValueError):
    """Feedback evidence or a proposed disposition is not supported."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition, message):
    if not condition:
        raise FeedbackError(message)


def _text(value, label):
    require(isinstance(value, str) and value.strip() and '\0' not in value, label + ' requires nonempty text')
    return value


def _positive(value, label):
    require(isinstance(value, int) and not isinstance(value, bool) and value > 0, label + ' requires a positive integer')
    return value


# -- Observation -----------------------------------------------------------------------------

def _user(item, label):
    user = item.get('user')
    require(isinstance(user, dict), label + ' has no author')
    login = _text(user.get('login'), label + ' author login')
    return {'id': _positive(user.get('id'), label + ' author ID'), 'login': login,
            'bot': user.get('type') == 'Bot' or login.endswith('[bot]')}


class View:
    """One complete, normalized observation of a PR's discussion."""

    def __init__(self, pr, evidence):
        self.pr = pr
        meta = evidence.get('metadata')
        require(isinstance(meta, dict) and str(meta.get('html_url', '')).lower() == pr['url'].lower()
                and meta.get('number') == pr['number'], 'provider returned a different PR')
        self.author = _user(meta, 'PR')
        viewer = evidence.get('viewer')
        require(isinstance(viewer, dict), 'authenticated actor unavailable')
        self.actor = {'id': _positive(viewer.get('id'), 'authenticated actor'),
                      'login': _text(viewer.get('login'), 'authenticated actor login')}
        self.responders = {self.author['id'], self.actor['id']}
        self.items, roots = {}, {}
        for kind, collection in COLLECTIONS.items():
            values = evidence.get(collection)
            require(isinstance(values, list), 'incomplete discussion: ' + collection)
            for raw in values:
                require(isinstance(raw, dict), 'malformed ' + kind)
                ident = _positive(raw.get('id'), kind + ' ID')
                key = f'{kind}:{ident}'
                require(key not in self.items, 'duplicate discussion item ' + key)
                body = raw.get('body') or ''
                require(isinstance(body, str), key + ' body must be text')
                at = raw.get('submitted_at' if kind == 'review' else 'created_at')
                item = {'key': key, 'kind': kind, 'id': ident, 'url': raw.get('html_url'), 'body': body,
                        'author': _user(raw, key), 'at': at if isinstance(at, str) and at else None}
                if kind == 'inline':
                    parent = raw.get('in_reply_to_id')
                    item['root'] = ident if parent is None else _positive(parent, key + ' parent')
                self.items[key] = item
        threads = evidence.get('threads')
        require(isinstance(threads, list), 'incomplete discussion: threads')
        self.threads = {}
        for thread in threads:
            ident = _text(thread.get('id') if isinstance(thread, dict) else None, 'review thread ID')
            nodes = (thread.get('comments') or {}).get('nodes') or []
            require(len(nodes) == 1, 'review thread ' + ident + ' lacks its first comment')
            root = _positive(nodes[0].get('databaseId'), 'review thread root')
            require(ident not in self.threads and root not in roots, 'duplicate review thread')
            self.threads[ident] = {'id': ident, 'root': root, 'resolved': thread.get('isResolved') is True}
            roots[root] = ident
        for item in self.items.values():
            if item['kind'] == 'inline':
                require(f"inline:{item['root']}" in self.items, item['key'] + ' replies to an unobserved comment')
                item['thread'] = roots.get(item['root'])
        # Sources are feedback by anyone other than the PR author and the publishing actor.
        self.sources = {key: item for key, item in self.items.items()
                        if item['author']['id'] not in self.responders and item['body'].strip()}

    @staticmethod
    def fingerprint(item):
        return digest({'kind': item['kind'], 'id': item['id'], 'body': item['body']})

    def identity(self, item):
        """Stable source identity; logins and thread membership may be renamed or remapped."""
        return {'kind': item['kind'], 'id': item['id'], 'author_id': item['author']['id'], 'root': item.get('root')}

    def answers(self, source, reply):
        """Whether reply is a later answer in the source's own location."""
        if reply['author']['id'] == source['author']['id'] or not (source['at'] and reply['at']):
            return False
        if reply['at'] <= source['at']:
            return False
        if source['kind'] == 'inline':
            return reply['kind'] == 'inline' and reply['root'] == source['root']
        # The source URL must appear as a whole link: #issuecomment-12 is not #issuecomment-123.
        return reply['kind'] == 'comment' and links(reply['body'], source['url'])

    def later(self, source, login):
        """Whether participant login has spoken after source in the same inline thread."""
        return any(item['kind'] == 'inline' and item['root'] == source['root']
                   and item['author']['login'].lower() == login.lower()
                   and item['at'] and source['at'] and item['at'] > source['at']
                   for item in self.items.values())


def links(body, url):
    """Whether body links url as a whole link: #issuecomment-12 is not #issuecomment-123."""
    return (isinstance(url, str) and isinstance(body, str)
            and re.search(re.escape(url) + r'(?![A-Za-z0-9_-])', body) is not None)


def observe(provider, pr):
    """Read the complete discussion with exactly one capture call."""
    return View(pr, provider.capture(pr))


def authorized(permissions, action):
    """An effective admitted permission: enabled, explicitly authorized and not prohibited."""
    entry = (permissions or {}).get(action)
    return isinstance(entry, dict) and entry.get('effective') is True


# -- Assessment ------------------------------------------------------------------------------

def required(request):
    """A concrete author request, or one the user explicitly asked to answer, needs a response."""
    return request['addressee'] == 'author' or request.get('instructed') is True


def _participant(view, source, login):
    _text(login, 'participant')
    require(_LOGIN.fullmatch(login), 'participant requires an account login')
    lowered = login.lower()
    require(lowered not in {view.author['login'].lower(), view.actor['login'].lower(),
                            source['author']['login'].lower()},
            'a question for another participant must name someone other than the PR author and the source author')
    mentioned = re.search(r'(?<![A-Za-z0-9-])@' + re.escape(login) + r'(?![A-Za-z0-9-])', source['body'], re.I)
    spoke = any(item['author']['login'].lower() == lowered and item['at'] and source['at']
                and item['at'] < source['at']
                and (source['kind'] != 'inline' or item.get('root') == source['root'])
                for item in view.items.values())
    require(mentioned or spoke, f'{login} is neither mentioned in nor part of the discussion before {source["key"]}')


def _response(view, source, request):
    response = request.get('response')
    require(isinstance(response, dict), f"{request['id']}: response disposition required")
    disposition = response.get('disposition')
    must = required(request)
    if disposition == 'reply':
        require(set(response) == {'disposition'}, f"{request['id']}: reply takes no other fields")
        require(must, f"{request['id']}: optional discussion stays optional; leave it unanswered with its reason")
    elif disposition == 'already_answered':
        require(set(response) == {'disposition', 'reply', 'justification'},
                f"{request['id']}: already_answered requires reply and justification")
        _text(response['justification'], 'justification')
        reply = response['reply']
        require(isinstance(reply, dict) and set(reply) == {'kind', 'id'} and reply['kind'] in ('comment', 'inline'),
                f"{request['id']}: answer reference requires kind and id")
        answer = view.items.get(f"{reply['kind']}:{_positive(reply['id'], 'answer ID')}")
        require(answer is not None and view.answers(source, answer),
                f"{request['id']}: the cited answer is not a later reply in the source's own location")
    elif disposition == 'no_reply':
        require(not must, f"{request['id']}: a concrete author request needs a reply or an existing answer; "
                          'declining the change, bot authorship or a style category does not remove it')
        require(set(response) == {'disposition', 'reason', 'justification'},
                f"{request['id']}: no_reply requires reason and justification")
        require(response['reason'] == NO_REPLY[request['addressee']],
                f"{request['id']}: no-reply reason must be {NO_REPLY[request['addressee']]}")
        _text(response['justification'], 'justification')
    else:
        raise FeedbackError(f"{request['id']}: unsupported response disposition")


def validate(view, assessment):
    """Check one source's assessment against the current observation; return its canonical form."""
    require(isinstance(assessment, dict) and set(assessment) == {'source', 'fingerprint', 'requests'},
            'assessment requires exactly source, fingerprint and requests')
    source = view.sources.get(assessment['source'])
    require(source is not None, 'assessment names no current feedback source')
    require(assessment['fingerprint'] == View.fingerprint(source), 'source changed after it was assessed')
    requests = assessment['requests']
    require(isinstance(requests, list) and requests, 'every source needs at least one assessed request')
    seen = set()
    for request in requests:
        require(isinstance(request, dict) and not set(request) - REQUEST_FIELDS
                and {'id', 'excerpt', 'addressee', 'implementation', 'response'} <= set(request),
                'request requires id, excerpt, addressee, implementation and response only')
        ident = request['id']
        require(isinstance(ident, str) and _ID.fullmatch(ident) and ident not in seen, 'invalid or duplicate request ID')
        seen.add(ident)
        require(_text(request['excerpt'], 'excerpt') in source['body'], f'{ident}: excerpt is not quoted from the source')
        require(request['addressee'] in ADDRESSEES, f'{ident}: unsupported addressee')
        require(request['implementation'] in IMPLEMENTATIONS, f'{ident}: unsupported implementation disposition')
        require(('participant' in request) == (request['addressee'] == 'participant'),
                f'{ident}: participant is required exactly for a question to another participant')
        if request['addressee'] == 'participant':
            _participant(view, source, request['participant'])
        require(request['implementation'] == 'none' or request['addressee'] == 'author',
                f'{ident}: an implementation decision answers a concrete author request; its addressee is the author')
        require(isinstance(request.get('instructed', False), bool), f'{ident}: instructed must be boolean')
        if 'category' in request:
            _text(request['category'], 'category')
        _response(view, source, request)
    return {'source': source['key'], 'fingerprint': assessment['fingerprint'],
            'requests': sorted(requests, key=lambda request: request['id'])}


# -- Ledger ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (key TEXT PRIMARY KEY, identity TEXT NOT NULL);
CREATE TABLE assessments (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL REFERENCES sources(key),
    fingerprint TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX assessments_source ON assessments (source, fingerprint);
CREATE TABLE replies (
    source TEXT NOT NULL REFERENCES sources(key),
    fingerprint TEXT NOT NULL,
    reply TEXT NOT NULL,
    reference TEXT NOT NULL,
    PRIMARY KEY (source, fingerprint, reply)
)
"""


def ledger_path(state_directory, logical_run):
    """The logical run's feedback record, next to its run ledger; run text never forms a path."""
    name = hashlib.sha256(_text(logical_run, 'logical run').encode()).hexdigest()
    return Path(state_directory) / 'runs' / (name + '.feedback.sqlite')


def dumps(value):
    return canonical(value).decode('utf-8')


class Ledger:
    """Retained feedback decisions for one logical run and PR."""

    def __init__(self, path, *, logical_run, pr, read_only=False):
        self.path = Path(path)
        expected = {'suite': SUITE, 'kind': KIND, 'logical_run': logical_run, 'pr': pr['url'].lower()}
        if read_only:
            # Checkpoint validation and status reads never create or migrate the record.
            require(not self.path.is_symlink() and self.path.is_file(), 'feedback ledger is unavailable')
            self.db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                                      isolation_level=None, timeout=30)
            self.db.row_factory = sqlite3.Row
            try:
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                require(version == SCHEMA_VERSION, f'unsupported feedback ledger version {version}')
                require(dict(self.db.execute('SELECT key, value FROM meta').fetchall()) == expected,
                        'feedback ledger belongs to another logical run or PR')
            except (FeedbackError, sqlite3.DatabaseError) as error:
                self.db.close()
                raise FeedbackError(str(error)) from error
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not self.path.is_symlink(), 'feedback ledger must not be a symlink')
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        try:
            with self.transaction():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                if version == 0 and tables == 0:
                    for statement in SCHEMA.split(';'):
                        self.db.execute(statement)
                    self.db.executemany('INSERT INTO meta VALUES (?, ?)', sorted(expected.items()))
                    self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                require(version in (0, SCHEMA_VERSION), f'unsupported feedback ledger version {version}')
                meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
                require({k: meta.get(k) for k in ('suite', 'kind')} == {k: expected[k] for k in ('suite', 'kind')},
                        'not a cf-workflows feedback ledger')
                require(meta == expected, 'feedback ledger belongs to another logical run or PR')
        except (FeedbackError, sqlite3.DatabaseError) as error:
            self.db.close()
            raise FeedbackError(str(error)) from error

    def close(self):
        self.db.close()

    def transaction(self):
        db = self.db

        class Transaction:
            def __enter__(self):
                db.execute('BEGIN IMMEDIATE')

            def __exit__(self, kind, value, trace):
                db.execute('COMMIT' if kind is None else 'ROLLBACK')
        return Transaction()

    def current(self, source, fingerprint):
        row = self.db.execute('SELECT value FROM assessments WHERE source = ? AND fingerprint = ? ORDER BY seq DESC LIMIT 1',
                              (source, fingerprint)).fetchone()
        return json.loads(row['value']) if row else None

    def history(self, source):
        return [json.loads(row['value']) for row in
                self.db.execute('SELECT value FROM assessments WHERE source = ? ORDER BY seq', (source,))]

    def assess(self, view, assessment):
        """Retain one source's assessment. Repeating it is a no-op.

        A reassessment never drops an earlier obligation whose excerpt the source still
        contains, even after an edit: a required request stays required under its ID, and a
        question for another participant stays addressed to that participant (or becomes
        required). Only an edit that removes the quoted text releases it.
        """
        value = validate(view, assessment)
        source = view.sources[value['source']]
        with self.transaction():
            identity = dumps(view.identity(source))
            old = self.db.execute('SELECT identity FROM sources WHERE key = ?', (source['key'],)).fetchone()
            require(old is None or old['identity'] == identity, 'feedback source identity changed')
            self.db.execute('INSERT OR IGNORE INTO sources VALUES (?, ?)', (source['key'], identity))
            previous = self.current(source['key'], value['fingerprint'])
            if previous == value:
                return value
            now = {request['id']: request for request in value['requests']}
            for earlier in self.history(source['key']):
                for request in earlier['requests']:
                    if request['excerpt'] not in source['body']:
                        continue
                    kept = now.get(request['id'])
                    if required(request):
                        require(kept is not None and required(kept),
                                f"reassessment drops the response obligation of {request['id']}")
                    elif request['addressee'] == 'participant':
                        require(kept is not None and (required(kept) or (
                            kept['addressee'] == 'participant'
                            and kept['participant'].lower() == request['participant'].lower())),
                                f"reassessment drops the question for {request['participant']} in {request['id']}")
            self.db.execute('INSERT INTO assessments (source, fingerprint, value) VALUES (?, ?, ?)',
                            (source['key'], value['fingerprint'], dumps(value)))
        return value

    def record_reply(self, view, source_key, reply, reference):
        """Retain a published reply once it is observed in the source's own location.

        reference is the publication record (for example an exact-intent receipt ID).
        An unobserved reply is never recorded: uncertain dispatch stays with publication.
        """
        source = view.sources.get(source_key)
        require(source is not None, 'reply names no current feedback source')
        fingerprint = View.fingerprint(source)
        assessment = self.current(source_key, fingerprint)
        require(assessment is not None and any(required(r) for r in assessment['requests']),
                'reply names a source with no current response obligation')
        require(isinstance(reply, dict) and set(reply) == {'kind', 'id'}, 'reply requires kind and id')
        item = view.items.get(f"{reply.get('kind')}:{reply.get('id')}")
        require(item is not None, 'reply is not observable; reconcile the original publication before recording it')
        require(item['author']['id'] in view.responders and view.answers(source, item),
                "reply is not the author's later answer in the source's own location")
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO replies VALUES (?, ?, ?, ?)',
                            (source_key, fingerprint, item['key'], dumps(_text(reference, 'reply reference'))))
        return item['key']

    def replies(self, source_key, fingerprint):
        return [row['reply'] for row in self.db.execute(
            'SELECT reply FROM replies WHERE source = ? AND fingerprint = ? ORDER BY rowid', (source_key, fingerprint))]

    def recorded(self, reference):
        """Whether a publication reference was ever recorded as an observed reply."""
        return self.db.execute('SELECT 1 FROM replies WHERE reference = ?', (dumps(reference),)).fetchone() is not None


class Unrecorded:
    """A logical run with no feedback record yet: nothing assessed, nothing replied."""

    def current(self, source, fingerprint):
        return None

    def history(self, source):
        return []

    def replies(self, source_key, fingerprint):
        return []

    def recorded(self, reference):
        return False

    def close(self):
        pass


# -- Stage completion and thread resolution --------------------------------------------------

def _settlement(ledger, view, source):
    """The current settlement of one source, re-verified against the observation."""
    fingerprint = View.fingerprint(source)
    assessment = ledger.current(source['key'], fingerprint)
    record = {'source': source['key'], 'url': source['url'], 'author': source['author'],
              'thread': source.get('thread'), 'fingerprint': fingerprint}
    if assessment is None:
        changed = bool(ledger.history(source['key']))
        return {**record, 'status': 'unassessed', 'reason': 'changed' if changed else 'new'}
    replies = [key for key in ledger.replies(source['key'], fingerprint)
               if key in view.items and view.answers(source, view.items[key])
               and view.items[key]['author']['id'] in view.responders]
    lost = len(replies) < len(ledger.replies(source['key'], fingerprint))
    requests = []
    for request in assessment['requests']:
        response = request['response']
        if response['disposition'] == 'already_answered':
            answer = view.items.get(f"{response['reply']['kind']}:{response['reply']['id']}")
            # A verified reply from the author also settles it, e.g. after the cited answer was deleted.
            settled = bool(replies) or (answer is not None and view.answers(source, answer))
        elif response['disposition'] == 'reply':
            settled = bool(replies)
        else:
            settled = True
        requests.append({'id': request['id'], 'addressee': request['addressee'],
                         'implementation': request['implementation'], 'required': required(request),
                         'disposition': response['disposition'], 'settled': settled})
    open_ = [r['id'] for r in requests if r['required'] and not r['settled']]
    status = 'reply_needed' if open_ else 'settled'
    result = {**record, 'status': status, 'requests': requests, 'replies': replies, 'open': open_}
    if lost and open_:
        result['reason'] = 'reply_unavailable'
    return result


def stage(ledger, view, permissions):
    """Response-stage completion for the current discussion.

    complete: every source is assessed and every required request has a verified reply
    or existing answer. Thread state never counts: a resolved thread still owes its answer.
    """
    sources = {key: _settlement(ledger, view, source) for key, source in sorted(view.sources.items())}
    unassessed = [key for key, value in sources.items() if value['status'] == 'unassessed']
    needed = [key for key, value in sources.items() if value['status'] == 'reply_needed']
    unanswered = []
    for key, value in sources.items():
        if value['status'] == 'unassessed':
            continue
        for request in ledger.current(key, value['fingerprint'])['requests']:
            if request['response']['disposition'] == 'no_reply':
                unanswered.append({'source': key, 'request': request['id'], 'excerpt': request['excerpt'],
                                   'reason': request['response']['reason'],
                                   'justification': request['response']['justification']})
    blockers = []
    if unassessed:
        status = 'incomplete'
        blockers.append('feedback sources lack a current assessment')
    elif needed and not authorized(permissions, 'reply'):
        status = 'blocked'
        blockers.append('required replies lack an effective reply permission and authorization')
    else:
        status = 'pending' if needed else 'complete'
    return {'status': status, 'sources': sources, 'unassessed': unassessed, 'replies_needed': needed,
            'unanswered': unanswered, 'blockers': blockers}


def resolution(ledger, view, thread_id, permissions):
    """Whether an inline thread may be resolved. Never a substitute for an answer."""
    thread = view.threads.get(thread_id)
    require(thread is not None, 'unknown review thread')
    blockers = []
    if not authorized(permissions, 'resolve_thread'):
        blockers.append('thread resolution lacks an effective permission and authorization')
    if thread['resolved']:
        blockers.append('thread is already resolved')
    for key, source in sorted(view.sources.items()):
        if source.get('thread') != thread_id:
            continue
        value = _settlement(ledger, view, source)
        if value['status'] == 'unassessed':
            blockers.append(f'{key} lacks a current assessment')
            continue
        if value['open']:
            blockers.append(f'{key} still needs an answer')
        for request in ledger.current(key, value['fingerprint'])['requests']:
            if request['addressee'] == 'participant' and not view.later(source, request['participant']):
                blockers.append(f"{key} awaits {request['participant']}")
    return {'thread': thread_id, 'allowed': not blockers, 'blockers': blockers}
