"""Occurrence-scoped supplied-skill bindings and engine-observed evidence (#16).

A supplied skill replaces the baseline behavior of one selected task occurrence.
This module derives, from an admitted snapshot, what each occurrence's
engine-managed session may load and what the engine must observe before that
occurrence counts as covered. It never loads, runs or discovers a skill, starts
a session or contacts a provider: Agent Workflow #139 owns admission of the
bytes, session-scoped plugin loading, discovery isolation and the event stream.

Names follow what #139 ships: the engine registers an admitted skill as the
plugin `aw-<name>` (Claude invokes `aw-<name>:<name>`) or as the retained root's
`skills/<name>/SKILL.md` (Codex), and a declared child as the agent type
`aw-child-<name>`. Coverage is decided only from records whose source is the
engine. A report from the session itself, however detailed, establishes nothing.
"""
import hashlib
from pathlib import PurePosixPath

from .admission import digest

PROVIDERS = ('codex', 'claude')
CONTINUATIONS = ('new', 'resume', 'retry', 'checklist')
OUTCOMES = ('completed', 'failed', 'interrupted', 'unknown')
RECORD_FIELDS = {'schema_version', 'kind', 'source', 'provider', 'occurrence', 'binding', 'session', 'continuation',
                 'loaded', 'events', 'outcome'}
VERIFICATION = 'finding_verification'
PLUGIN = 'aw-'  # Prefix of the engine-owned plugin an admitted skill is registered in.
CHILD = 'aw-child-'  # Prefix of the engine-registered agent type of a declared child.
CODEX_SKILL_KIND = 'skills.selected_skill_instructions'  # Codex's native skill-consumption fragment.
# Every agent/run step that may carry a selected skill must disable output repair (Agent Workflow #139).
SKILL_OUTPUT = {'format': 'json', 'repair-attempts': 0}


class EvidenceError(ValueError):
    """A malformed engine record; it never counts as evidence."""


def occurrence_id(phase, context, adapter, instructions, ordinals):
    """The installed workflow's stable occurrence ID (prepare.py), so bindings name the same occurrence."""
    key = digest([phase, context, adapter, instructions or '', {}])
    ordinal = ordinals.get(key, 0)
    ordinals[key] = ordinal + 1
    return 'task_' + digest([key, ordinal])[:24]


def qualified(name):
    """The Claude command an admitted skill is invoked as: `aw-<name>:<name>`."""
    return f'{PLUGIN}{name}:{name}'


def locator_matches(provider, locator, name):
    """Claude invokes `aw-<name>:<name>`; Codex reads the retained root's absolute `skills/<name>/SKILL.md`.

    The Codex root is engine-owned and not known here, so a Codex locator is
    checked for shape only; the admitted digest is what identifies the skill.
    """
    if not isinstance(locator, str):
        return False
    if provider == 'claude':
        return locator == qualified(name)
    path = PurePosixPath(locator)
    return (locator.startswith('/') and '..' not in path.parts and str(path) == locator
            and path.parts[-3:] == ('skills', name, 'SKILL.md'))


def selection(skill):
    """The `with.skill` selection Agent Workflow #139 admits for this skill (input `skill`, see docs/skills.md)."""
    return {'input': 'skill', 'arguments': skill['arguments'], 'resources': skill['resources'],
            'evidence': {'invocation': 'required', 'children': [child['name'] for child in skill['children']]},
            'children': [{'name': child['name'], 'prompt': child['prompt'], 'description': child['description'],
                          'tools': child['tools'], 'resources': child['resources'], 'model': child['model'],
                          'max-turns': child['max_turns'], 'max-calls': child['max_calls'],
                          'background': child['background']} for child in skill['children']]}


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
        if task['kind'] == 'reply':
            # Every response stage shares one responder occurrence, independent of its phase.
            ident = occurrence_id('responses', 'responses', task['adapter'], task['instructions'], ordinals)
        else:
            context = 'reviews' if task['id'] in reviewers else 'tasks'
            ident = occurrence_id(task['phase'], context, task['adapter'], task['instructions'], ordinals)
        skill = admitted_skills[task['skill']] if task['skill'] is not None else None
        loaded = [] if skill is None else [{**{key: skill[key] for key in ('id', 'name', 'entry', 'sha256', 'files',
                                                                           'resources', 'arguments')},
                                            'plugin': PLUGIN + skill['name'], 'qualified': qualified(skill['name'])}]
        children = [] if skill is None else skill['children']
        binding = {
            'occurrence': ident, 'task': task['id'], 'adapter': task['adapter'], 'phase': task['phase'],
            'path': task['path'], 'definition': task['definition'], 'writes': task['writes'],
            'skills': loaded,
            # Skill and child-agent tools exist only in a session whose supplied skill needs them.
            'controls': {'skill_tool': skill is not None, 'agent_tool': bool(children),
                         'agent_types': [item['type'] for item in children]},
            'evidence': None if skill is None else {'invocation': skill['name'], 'children': children,
                                                     'outcome': 'completed'},
            'publication': False,
            'findings': 'independent_verification' if task['kind'] == 'review' else None,
        }
        if skill is not None:
            # The engine's `type: skill` input is the admitted directory, resolved against its root.
            binding['input'] = {'directory': {'root': skill['root'], 'path': skill['path']}}
            binding['selection'] = selection(skill)
            binding['output'] = dict(SKILL_OUTPUT)
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
        if not isinstance(item, dict) or set(item) != {'name', 'sha256', 'locator'}:
            raise EvidenceError('loaded skills are {name, sha256, locator} records')
    return raw


def _codex(record, add_invocation, add_child, child_event, fault):
    """Codex app-server: the native skill-consumption fragment; delegation is disabled.

    A `turn/start` skill input is only a request and never evidence. The
    fragment Codex's loader emits as a user message, tagged with skill
    provenance, carries the skill's name, registered path and exact bytes.
    Any `collabAgentToolCall` or `subAgentActivity` item is a delegation the
    profile forbids.
    """
    thread = record['session']
    for event in record['events']:
        params = event.get('params') or {}
        item = params.get('item') or {}
        if event.get('method') != 'item/completed' or not isinstance(item, dict):
            continue
        if item.get('type') in ('collabAgentToolCall', 'subAgentActivity'):
            fault('failed', 'Codex native delegation is disabled; a child agent was started')
            continue
        metadata = item.get('internal_chat_message_metadata_passthrough') or {}
        kinds = metadata.get('content_item_kinds') or []
        if CODEX_SKILL_KIND not in kinds:
            continue
        content = item.get('content') or []
        provenance = [(entry.get('provenance') or {}).get('type') for entry in metadata.get('content_item_metadata') or []]
        turn = params.get('turnId')
        if (params.get('threadId') != thread or not turn or metadata.get('turn_id') != turn
                or item.get('type') != 'message' or item.get('role') != 'user' or not item.get('id')
                or kinds != [CODEX_SKILL_KIND] or provenance != ['skills'] or len(content) != 1
                or content[0].get('type') != 'input_text' or not isinstance(content[0].get('text'), str)):
            fault('failed', 'native skill fragment has mismatched identity or provenance')
            continue
        text = content[0]['text']
        head, separator, rest = text.partition('</name>\n<path>')
        locator, separator2, body = rest.partition('</path>\n')
        if (not separator or not separator2 or not head.startswith('<skill>\n<name>')
                or not body.endswith('\n</skill>')):
            fault('failed', 'native skill fragment is malformed')
            continue
        add_invocation(head[len('<skill>\n<name>'):], locator, body=body[:-len('\n</skill>')].encode('utf-8'))


def _claude(record, add_invocation, add_child, child_event, fault):
    """Claude SDK: a `Skill` call confirmed by its successful tool result; `Agent` calls and task_* messages."""
    tasks, calls = {}, {}
    for event in record['events']:
        kind = event.get('type')
        if kind == 'assistant':
            parent = event.get('parent_tool_use_id')
            for block in (event.get('message') or {}).get('content') or []:
                if not isinstance(block, dict) or block.get('type') != 'tool_use':
                    continue
                arguments = block.get('input') or {}
                if block.get('name') == 'Skill':
                    if parent is not None:
                        fault('failed', 'a child agent invoked a skill')
                    elif set(arguments) - {'skill', 'args'}:
                        fault('failed', 'skill invoked with unsupported options')
                    else:
                        calls[block.get('id')] = arguments
                elif block.get('name') == 'Agent':  # Upstream's bridge counts only Agent as a child call.
                    add_child(block.get('id'), arguments.get('subagent_type'), parent is not None, arguments)
        elif kind == 'user':
            result = event.get('tool_use_result') or {}
            for block in (event.get('message') or {}).get('content') or []:
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                arguments = calls.pop(block.get('tool_use_id'), None)
                # Only the original call's successful result completes an invocation.
                if (arguments is not None and block.get('is_error') is not True and result.get('success') is True
                        and result.get('commandName') == arguments.get('skill')):
                    locator = str(arguments.get('skill', ''))
                    add_invocation(locator.rpartition(':')[2], locator, arguments=arguments.get('args', ''))
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
      before another attempt. The separate `reconcile` flag stays true even
      when a violation makes the status 'failed'.

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
    permitted = {item['type']: 1 for item in expected['children']}  # Each declared child runs exactly once.
    invocations, children, order = [], {}, []

    for record in sessions:
        loaded = {(item['name'], item['sha256']) for item in record['loaded']}
        locators = set()
        for item in record['loaded']:
            if (item['name'], item['sha256']) not in admitted:
                continue
            if locator_matches(record['provider'], item['locator'], item['name']):
                locators.add(item['locator'])
            else:
                fault('failed', f'admitted skill {item["name"]} registered outside its engine-owned plugin: '
                                f'{item["locator"]}')
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

        def add_invocation(name, locator, body=None, arguments=None, record=record, locators=locators):
            skill = binding['skills'][0] if binding['skills'] else None
            if not binding['controls']['skill_tool']:
                fault('failed', 'skill invoked in a session without a supplied skill')
            elif name != expected['invocation'] or locator not in locators:
                # A same-named host, repository or other plugin skill is another skill.
                fault('failed', f'invoked a skill other than the admitted one: {locator}')
            elif body is not None and hashlib.sha256(body).hexdigest() != skill['files'][skill['entry']]:
                fault('failed', 'consumed skill instructions differ from the admitted bytes')
            elif arguments is not None and arguments != skill['arguments']:
                fault('failed', 'skill invoked with arguments other than the admitted ones')
            else:
                invocations.append({'session': record['session'], 'name': name})

        def add_child(ident, agent_type, nested, controls=None, record=record):
            key = (record['session'], ident)
            declared = next((item for item in expected['children'] if item['type'] == agent_type), None)
            if nested:
                fault('failed', 'nested child agents are not permitted')
            elif not binding['controls']['agent_tool']:
                fault('failed', 'child agent started without a declared child')
            elif declared is None:
                fault('failed', f'child agent type not permitted: {agent_type}')
            elif controls is not None and (set(controls) != {'subagent_type', 'prompt', 'description',
                                                             'run_in_background'}
                                           or controls['prompt'] != declared['prompt']
                                           or controls['description'] != declared['description']
                                           or controls['run_in_background'] is not False):
                fault('failed', f'child agent call differs from the declared controls: {agent_type}')
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
    return {'status': status, 'reconcile': bool(reasons['reconcile']),
            'occurrence': binding['occurrence'], 'binding': binding['binding'],
            'reasons': reasons['failed'] + reasons['reconcile'] + reasons['incomplete'],
            'sessions': [item['session'] for item in sessions], 'invocations': len(invocations),
            'children': [{key: children[item][key] for key in ('session', 'id', 'agent_type', 'outcome')}
                         for item in order]}


def attempt_allowed(binding, records):
    """Whether another attempt may be dispatched: never while an earlier session is unreconciled.

    A rule violation in the same session does not lift the reconciliation requirement.
    """
    result = coverage(binding, records)
    return result['status'] != 'covered' and not result['reconcile']
