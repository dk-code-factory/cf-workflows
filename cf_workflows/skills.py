"""Occurrence-scoped supplied-skill bindings and engine-observed evidence (#16).

A supplied skill replaces the baseline behavior of one selected task occurrence.
This module derives, from an admitted snapshot, what each occurrence's
engine-managed session may load and what the engine must observe before that
occurrence counts as covered. It never loads, runs or discovers a skill, starts
a session or contacts a provider: Agent Workflow #139 owns admission of the
bytes, session-scoped plugin loading, discovery isolation and the event stream.

Coverage is decided only from records whose source is the engine. A report from
the session itself, however detailed, establishes nothing.
"""
from .admission import digest

PROVIDERS = ('codex', 'claude')
CONTINUATIONS = ('new', 'resume', 'retry', 'checklist')
OUTCOMES = ('completed', 'failed', 'interrupted', 'unknown')
RECORD_FIELDS = {'schema_version', 'kind', 'source', 'provider', 'occurrence', 'binding', 'session', 'continuation',
                 'loaded', 'events', 'outcome'}
VERIFICATION = 'finding_verification'


class EvidenceError(ValueError):
    """A malformed engine record; it never counts as evidence."""


def occurrence_id(phase, context, adapter, instructions, ordinals):
    """The installed workflow's stable occurrence ID (prepare.py), so bindings name the same occurrence."""
    key = digest([phase, context, adapter, instructions or '', {}])
    ordinal = ordinals.get(key, 0)
    ordinals[key] = ordinal + 1
    return 'task_' + digest([key, ordinal])[:24]


def session_plan(admitted):
    """Per-occurrence session bindings for a structurally accepted admission result.

    Uses only the admitted snapshot, so a skill source changed or removed after
    admission does not alter any binding: the admitted digest stays pinned.
    """
    if admitted.get('structural') != 'accepted':
        raise ValueError('session bindings need a structurally accepted admission')
    snapshot = admitted['snapshot']
    reviewers = {task for stage in snapshot['plan']['stages'] if stage['kind'] == 'reviews'
                 for group in stage['groups'] for task in group}
    admitted_skills = snapshot['resources']['skills']
    ordinals, occurrences = {}, []
    for task in snapshot['plan']['tasks']:
        context = 'reviews' if task['id'] in reviewers else 'tasks'
        ident = occurrence_id(task['phase'], context, task['adapter'], task['instructions'], ordinals)
        skill = admitted_skills[task['skill']] if task['skill'] is not None else None
        loaded = [] if skill is None else [{key: skill[key] for key in ('id', 'name', 'entry', 'sha256', 'files')}]
        children = [] if skill is None else skill['children']
        binding = {
            'occurrence': ident, 'task': task['id'], 'adapter': task['adapter'], 'phase': task['phase'],
            'path': task['path'], 'definition': task['definition'], 'writes': task['writes'],
            'skills': loaded,
            # Skill and child-agent tools exist only in a session whose supplied skill needs them.
            'controls': {'skill_tool': skill is not None, 'agent_tool': bool(children),
                         'agent_types': [item['agent_type'] for item in children]},
            'evidence': None if skill is None else {'invocation': skill['name'], 'children': children,
                                                     'outcome': 'completed'},
            'publication': False,
            'findings': 'independent_verification' if task['kind'] == 'review' else None,
        }
        binding['binding'] = digest(binding)
        occurrences.append(binding)
    with_skill = [item['occurrence'] for item in occurrences if item['skills']]
    reviewed = [item['occurrence'] for item in occurrences if item['skills'] and item['findings']]
    # Findings of a supplied reviewer still go to the separate baseline verifier session.
    verification = ({'occurrence': VERIFICATION, 'definition': 'verifier', 'skills': [],
                     'controls': {'skill_tool': False, 'agent_tool': False, 'agent_types': []},
                     'sources': reviewed} if reviewed else None)
    return {'occurrences': occurrences, 'with_skills': with_skill, 'verification': verification}


# -- Engine records ---------------------------------------------------------------------------

def _record(raw, binding):
    if not isinstance(raw, dict) or raw.get('source') != 'engine':
        return None  # Report-only material: never evidence.
    if set(raw) != RECORD_FIELDS or raw['schema_version'] != 1 or raw['kind'] != 'session_observation':
        raise EvidenceError('unsupported engine session record')
    if raw['occurrence'] != binding['occurrence'] or raw['binding'] != binding['binding']:
        raise EvidenceError('engine record belongs to another occurrence or binding')
    if raw['provider'] not in PROVIDERS or raw['continuation'] not in CONTINUATIONS or raw['outcome'] not in OUTCOMES:
        raise EvidenceError('unsupported provider, continuation or outcome')
    if not isinstance(raw['session'], str) or not raw['session']:
        raise EvidenceError('engine record needs a session ID')
    if not isinstance(raw['loaded'], list) or not isinstance(raw['events'], list):
        raise EvidenceError('loaded skills and events must be arrays')
    for item in raw['loaded']:
        if not isinstance(item, dict) or set(item) != {'name', 'sha256'}:
            raise EvidenceError('loaded skills are {name, sha256} records')
    return raw


def _codex(record, add_invocation, add_child, child_event, fault):
    """Codex app-server: the engine's turn/start skill input, collabAgentToolCall and subAgentActivity items."""
    thread = record['session']
    for event in record['events']:
        method = event.get('method')
        params = event.get('params') or {}
        if method == 'turn/start':
            for item in params.get('input', []):
                if isinstance(item, dict) and item.get('type') == 'skill':
                    add_invocation(item.get('name'))
            continue
        item = params.get('item') or {}
        if method != 'item/completed' or not isinstance(item, dict):
            continue
        if item.get('type') == 'collabAgentToolCall' and item.get('tool') == 'spawnAgent':
            nested = item.get('senderThreadId') != thread
            for child in item.get('receiverThreadIds') or []:
                add_child(child, item.get('agentType'), nested)
                if item.get('status') == 'failed':
                    child_event(child, 'failed')
        elif item.get('type') == 'subAgentActivity':
            child_event(item.get('agentThreadId'), {'started': 'started', 'completed': 'completed'}.get(item.get('kind')))


def _claude(record, add_invocation, add_child, child_event, fault):
    """Claude SDK: Skill and Agent tool_use blocks, parent_tool_use_id and task_* system messages."""
    tasks = {}
    for event in record['events']:
        kind = event.get('type')
        if kind == 'assistant':
            parent = event.get('parent_tool_use_id')
            for block in (event.get('message') or {}).get('content') or []:
                if not isinstance(block, dict) or block.get('type') != 'tool_use':
                    continue
                if block.get('name') == 'Skill':
                    if parent is not None:
                        fault('failed', 'a child agent invoked a skill')
                    # The engine wraps the admitted bytes as plugin:<name>; only the skill name is policy.
                    add_invocation(str((block.get('input') or {}).get('skill', '')).rpartition(':')[2])
                elif block.get('name') in ('Agent', 'Task'):
                    add_child(block.get('id'), (block.get('input') or {}).get('subagent_type'), parent is not None)
        elif kind == 'system' and event.get('subtype') == 'task_started':
            tasks[event.get('task_id')] = event.get('tool_use_id')
            child_event(event.get('tool_use_id'), 'started')
        elif kind == 'system' and event.get('subtype') == 'task_notification':
            status = event.get('status')
            child_event(tasks.get(event.get('task_id')), status if status in ('completed', 'failed') else None)


def coverage(binding, records):
    """Decide one occurrence's coverage from the engine's records of its sessions, in order.

    records holds the original session and every resume, retry or checklist
    continuation. Returns {'status', 'reasons', ...} where status is:

    - 'covered': every session loaded exactly the admitted skills, the admitted
      skill was invoked, its latest invocation ran exactly the declared children
      to completion, and the last session completed;
    - 'incomplete': evidence is missing or does not match the declaration;
    - 'failed': isolation or permission was violated, or the admitted skill was
      unavailable in a session; the occurrence never completes without it;
    - 'reconcile': the last session was interrupted, its outcome is unknown, or
      a writing occurrence failed; its original effects must be reconciled
      before another attempt.

    A session's own report is not an input: it can never establish coverage.
    """
    reasons = {'failed': [], 'reconcile': [], 'incomplete': []}

    def fault(level, message):
        if message not in reasons[level]:
            reasons[level].append(message)

    sessions, ignored = [], 0
    for raw in records:
        try:
            record = _record(raw, binding)
        except EvidenceError as error:
            fault('incomplete', 'malformed engine record: ' + str(error))
            continue
        if record is None:
            ignored += 1
        else:
            sessions.append(record)
    if ignored:
        fault('incomplete', 'report-only evidence is ignored')
    if not sessions:
        fault('incomplete', 'no engine-observed session')
    elif sessions[0]['continuation'] != 'new' or any(item['continuation'] == 'new' for item in sessions[1:]):
        fault('incomplete', 'session chain must start with one new session')

    admitted = {(item['name'], item['sha256']) for item in binding['skills']}
    names = {item['name'] for item in binding['skills']}
    expected = binding['evidence'] or {'invocation': None, 'children': []}
    permitted = {item['agent_type']: item['count'] for item in expected['children']}
    invocations, children, order = [], {}, []

    for record in sessions:
        loaded = {(item['name'], item['sha256']) for item in record['loaded']}
        for name, sha256 in sorted(loaded - admitted):
            if name in names:
                fault('failed', f'skill {name} loaded with digest {sha256}, not the admitted digest')
            else:
                fault('failed', f'unadmitted skill loaded: {name}')
        for name, _ in sorted(admitted - loaded):
            if record['continuation'] == 'new':
                fault('failed', f'admitted skill {name} was not loaded into session {record["session"]}')
            else:
                fault('failed', f'admitted skill {name} is unavailable in {record["continuation"]} '
                                f'session {record["session"]}')

        def add_invocation(name, record=record):
            if not binding['controls']['skill_tool']:
                fault('failed', 'skill invoked in a session without a supplied skill')
            elif name != expected['invocation']:
                fault('failed', f'invoked a skill other than the admitted one: {name}')
            else:
                invocations.append({'session': record['session'], 'name': name})

        def add_child(ident, agent_type, nested, record=record):
            key = (record['session'], ident)
            if nested:
                fault('failed', 'nested child agents are not permitted')
            elif not binding['controls']['agent_tool']:
                fault('failed', 'child agent started without a declared child')
            elif agent_type not in permitted:
                fault('failed', f'child agent type not permitted: {agent_type}')
            elif not invocations:
                fault('incomplete', 'child agent started before the admitted skill was invoked')
            elif key in children:
                fault('incomplete', f'duplicate child record: {ident}')
            else:
                children[key] = {'session': record['session'], 'id': ident, 'agent_type': agent_type,
                                 'invocation': len(invocations) - 1, 'started': False, 'outcome': None}
                order.append(key)

        def child_event(ident, state, record=record):
            child = children.get((record['session'], ident))
            if child is None or state is None:
                return
            if state == 'started':
                child['started'] = True
            elif child['outcome'] is None:
                child['outcome'] = state

        try:
            (_codex if record['provider'] == 'codex' else _claude)(record, add_invocation, add_child, child_event,
                                                                   fault)
        except (AttributeError, TypeError) as error:
            fault('incomplete', 'malformed provider events: ' + type(error).__name__)

    if sessions:
        last = sessions[-1]['outcome']
        if last in ('interrupted', 'unknown') or (last == 'failed' and binding['writes']):
            # The session may have changed files or started effects; its original effects come first.
            fault('reconcile', f'last session outcome is {last}; reconcile its effects before another attempt')
        elif last == 'failed':
            fault('incomplete', 'last session failed')
    if binding['evidence'] is not None:
        if not invocations:
            fault('incomplete', 'no engine-observed invocation of the admitted skill')
        else:
            latest = len(invocations) - 1
            counts = {}
            for key in order:
                child = children[key]
                if child['invocation'] != latest:
                    continue
                counts[child['agent_type']] = counts.get(child['agent_type'], 0) + 1
                if not child['started'] or child['outcome'] != 'completed':
                    fault('incomplete', f'child {child["id"]} has no completed terminal outcome')
            for agent_type, count in sorted(permitted.items()):
                seen = counts.get(agent_type, 0)
                if seen < count:
                    fault('incomplete', f'missing {agent_type} children: {seen} of {count}')
                elif seen > count:
                    fault('incomplete', f'duplicate {agent_type} children: {seen} of {count}')
    status = next((level for level in ('failed', 'reconcile', 'incomplete') if reasons[level]), 'covered')
    return {'status': status, 'occurrence': binding['occurrence'], 'binding': binding['binding'],
            'reasons': reasons['failed'] + reasons['reconcile'] + reasons['incomplete'],
            'sessions': [item['session'] for item in sessions], 'invocations': len(invocations),
            'children': [{key: children[item][key] for key in ('session', 'id', 'agent_type', 'outcome')}
                         for item in order]}


def attempt_allowed(binding, records):
    """Whether another attempt may be dispatched: never while an earlier session is unreconciled."""
    return coverage(binding, records)['status'] not in ('reconcile', 'covered')
