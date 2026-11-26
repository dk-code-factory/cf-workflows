"""Semantic configuration assessment of custom inputs (contract v1).

A structurally accepted proposal with custom prompts, policy text, per-task
instructions, custom adapters or supplied skills is assessed by the bundled
configuration validator before its first dispatch. The validator runs as an
engine-managed native session with the fixed admitted validator role and no
skills loaded. It may read the proposal and the declared files of a supplied
skill; it never runs a skill, edits files or publishes.

This module prepares the typed assignment, checks the engine-observed session
and its verdict, and binds clarification to durable engine input gates. Each
call starts at most one validator session and opens at most one gate. Verdicts,
user answers and gate payloads are data: they cannot grant authority, enable a
capability or change a requirement or prohibition. The default plan has no
custom inputs and never reaches this stage.

With a ledger (cf_workflows.acceptance), each unit of custom input keeps its
acceptance while its bindings stay current. A proposal whose every unit is
current is admitted on its original native evidence without any gate read or
session. Otherwise only the stale or new units are subjects of the session; the
reused units are context it may cite. Clarification verdicts are retained before
their gate opens, so a lost gate response does not repeat the session.
"""
import copy
import hashlib
import re

from cf_workflows import acceptance, admission
from cf_workflows.admission import AdmissionError, fields, require, text

VERDICTS = ('accepted', 'needs_clarification', 'rejected', 'blocked')
PREFLIGHT = {'accepted': 'admitted', 'needs_clarification': 'waiting', 'rejected': 'rejected', 'blocked': 'blocked'}
ANSWERS = ('supplement', 'remove', 'replace', 'reject')
MAX_ITEMS = 64
PATH = re.compile(r'[^.\[\]]+(?:\.[^.\[\]]+|\[(?:0|[1-9][0-9]*)\])*')
PATH_TOKEN = re.compile(r'([^.\[\]]+)|\[([0-9]+)\]')


# -- Assignment ------------------------------------------------------------------------------

def evidence_id(admitted):
    """Identity of the exact proposal being assessed: its bounded admission input."""
    return 'cfg_' + admitted['snapshot']['admission_input_digest']


def readable(admitted):
    """Files the validator may read, with the digests admission verified."""
    resources = admitted['snapshot']['resources']
    files = {key: item['sha256'] for key, item in resources['configured'].items()}
    files.update({'repository:' + rel: value for rel, value in resources['guidance']['files'].items()})
    for key, skill in admitted['admission_input']['custom']['skills'].items():
        files.update({f'skill:{key}/{rel}': value for rel, value in skill['files'].items()})
    return dict(sorted(files.items()))


def locations(admitted):
    """Every concrete path a verdict may cite, mapped to its kind.

    Only custom-input paths are editable by a clarification answer; requirements,
    prohibitions, permissions and the original request never are.
    """
    data = admitted['admission_input']
    index = {'request.instructions': 'request'}
    index.update({'request.authorizations.' + action: 'authorization' for action in data['request']['authorizations']})
    index.update({'permissions.' + action: 'permission' for action in admission.ACTIONS})
    index.update({item['path']: 'requirement' for item in data['requirements']})
    index.update({item['path']: 'prohibition' for item in data['prohibitions']})
    custom = data['custom']
    text_prompts = {entry['path'] for entry in admitted['snapshot']['prompts'] + admitted['snapshot']['preferences']
                    if entry['resource'] is None}
    for entry in custom['prompts']:
        index[entry['path']] = 'prompt'
        if entry['path'] in text_prompts:
            index[entry['path'] + '.text'] = 'prompt_text'
    index.update({entry['path']: 'instruction' for entry in custom['instructions']})
    # Removing an override restores the baseline adapter; a new adapter name has nothing to fall back to.
    index.update({adapter['path']: 'adapter' if name in admission.BUNDLED_ADAPTERS else 'custom_adapter'
                  for name, adapter in custom['adapters'].items()})
    for skill in custom['skills'].values():
        index[skill['record']] = 'skill'
        index.update({ref: 'skill_reference' for ref in skill['references']})
    index.update({key: 'resource' for key in readable(admitted)})
    return dict(sorted(index.items()))


EDITS = {'prompt': ('remove',), 'prompt_text': ('replace',), 'instruction': ('remove', 'replace'),
         'adapter': ('remove',), 'skill_reference': ('remove',)}


def editable(admitted):
    return {path: list(EDITS[kind]) for path, kind in locations(admitted).items() if kind in EDITS}


def validator_agent(admitted):
    bindings = admitted['snapshot']['capabilities']['bindings']
    agent = min(bindings)
    return agent, bindings[agent]['provider']


def assignment(admitted, request_id, clarifications, split):
    """The exact typed assignment for one engine-managed validator session.

    Only the subjects need a verdict; accepted units are current context with
    their original native evidence, which a conflict may still cite.
    """
    identity = evidence_id(admitted)
    agent, provider = validator_agent(admitted)
    return {
        'schema_version': 1, 'kind': 'configuration_assessment', 'suite': admission.SUITE,
        'request_id': request_id, 'evidence_id': identity, 'claim': 'cfg-assess-' + identity[4:36],
        'role': admission.VALIDATOR_ROLE, 'definition_sha256': admission.BOOTSTRAP_SHA256,
        'agent': agent, 'provider': provider,
        'skills': [], 'tools': 'read-only', 'publication': False,
        'readable': readable(admitted), 'paths': sorted(locations(admitted)),
        'input': admitted['admission_input'],
        'subjects': sorted(path for unit in split['subjects'] for path in unit['paths']),
        'accepted': {path: 'native:' + unit['record']['result_ref'] for unit in split['reused'] for path in unit['paths']},
        # User answers are request context for the validator, never execution evidence.
        'clarifications': copy.deepcopy(clarifications),
        'output': {'verdicts': list(VERDICTS),
                   'fields': ['verdict', 'summary', 'conflicts', 'uncertainties', 'resolved']},
    }


# -- Session and verdict checks ----------------------------------------------------------------

def check_items(items, where, keys, index, answers):
    require(isinstance(items, list) and len(items) <= MAX_ITEMS, where, 'must be a bounded array')
    for number, item in enumerate(items):
        at = f'{where}[{number}]'
        fields(item, at, keys)
        require(isinstance(item['id'], str) and admission.IDENT.fullmatch(item['id']), at + '.id', 'invalid identifier')
        paths = item['paths']
        require(isinstance(paths, list) and paths, at + '.paths', 'must name at least one concrete path')
        for path in paths:
            require(isinstance(path, str) and path in index, at + '.paths', 'unknown configuration or source path: '
                    + str(path))
        for key in set(keys) - {'id', 'paths', 'answers'}:
            text(item[key], f'{at}.{key}', 4096)
        if 'answers' in keys:
            require(isinstance(item['answers'], list) and item['answers']
                    and all(answer in answers for answer in item['answers']), at + '.answers',
                    'a resolution must cite recorded clarification answers')
    return items


def check_verdict(output, index, answers):
    """Validate a validator verdict; unknown fields (grants, waivers, capabilities) reject it."""
    fields(output, 'validator.output', ('verdict', 'summary', 'conflicts', 'uncertainties', 'resolved'))
    verdict = output['verdict']
    require(verdict in VERDICTS, 'validator.output.verdict', 'unknown verdict')
    text(output['summary'], 'validator.output.summary', 4096)
    check_items(output['conflicts'], 'validator.output.conflicts', ('id', 'paths', 'detail'), index, answers)
    check_items(output['uncertainties'], 'validator.output.uncertainties', ('id', 'paths', 'question'), index, answers)
    check_items(output['resolved'], 'validator.output.resolved', ('id', 'paths', 'detail', 'answers'), index, answers)
    ids = [item['id'] for key in ('conflicts', 'uncertainties', 'resolved') for item in output[key]]
    require(len(ids) == len(set(ids)), 'validator.output', 'duplicate item id')
    open_items = output['conflicts'] + output['uncertainties']
    require(verdict != 'accepted' or not open_items, 'validator.output',
            'acceptance with open conflicts or uncertainties is not acceptance')
    require(verdict != 'needs_clarification' or open_items, 'validator.output',
            'clarification requires a concrete conflict or uncertainty')
    require(verdict != 'rejected' or output['conflicts'], 'validator.output', 'rejection requires a concrete conflict')
    return copy.deepcopy(output)


def check_session(session, task, answers):
    """Check the engine-observed session, then its verdict. Any other operation discards the verdict."""
    fields(session, 'validator', ('result_ref', 'output', 'observation'))
    reference = text(session['result_ref'], 'validator.result_ref', 512)
    observed = fields(session['observation'], 'validator.observation',
                      ('session_id', 'agent', 'role', 'definition_sha256', 'skills_loaded', 'operations'))
    text(observed['session_id'], 'validator.observation.session_id', 512)
    require(observed['role'] == admission.VALIDATOR_ROLE and observed['definition_sha256'] == admission.BOOTSTRAP_SHA256,
            'validator.observation', 'session did not run the fixed bundled validator role')
    require(observed['agent'] == task['agent'], 'validator.observation.agent', 'session ran on another agent')
    require(observed['skills_loaded'] == [], 'validator.observation.skills_loaded', 'the validator loads no skills')
    # An engine that records no operations for the session leaves its read-only boundary unverified.
    require(observed['operations'] is not None, 'validator.observation.operations',
            'the engine recorded no operation receipt for the validator session, so its read-only boundary '
            'is unverified')
    require(isinstance(observed['operations'], list), 'validator.observation.operations', 'must be an array')
    for number, operation in enumerate(observed['operations']):
        at = f'validator.observation.operations[{number}]'
        fields(operation, at, ('kind', 'target'))
        require(operation['kind'] == 'read' and operation['target'] in task['readable'], at,
                f"validator attempted {operation['kind']!s} {operation['target']!s}; it may only read the proposal")
    verdict = check_verdict(session['output'], set(task['paths']), answers)
    return reference, observed['session_id'], verdict


# -- Durable clarification -------------------------------------------------------------------

def check_gates(raw, request_id):
    require(isinstance(raw, list), 'gates', 'must be an array')
    result = []
    for number, gate in enumerate(raw):
        at = f'gates[{number}]'
        fields(gate, at, ('gate_id', 'request_id', 'evidence_id', 'status', 'request', 'answer'))
        require(gate['request_id'] == request_id, at + '.request_id', 'gate belongs to another request')
        require(gate['status'] in ('waiting', 'answered'), at + '.status', 'unsupported gate status')
        require((gate['answer'] is None) == (gate['status'] == 'waiting'), at + '.answer', 'answer/status mismatch')
        if gate['answer'] is not None:
            fields(gate['answer'], at + '.answer', ('answer_id', 'value'))
            text(gate['answer']['answer_id'], at + '.answer.answer_id', 256)
        fields(gate['request'], at + '.request', ('request_id', 'evidence_id', 'prompt', 'payload'))
        require(gate['request']['request_id'] == request_id and gate['request']['evidence_id'] == gate['evidence_id'],
                at + '.request', 'gate request does not match its pinned identity')
        result.append(gate)
    return result


def parse(path):
    require(PATH.fullmatch(path), 'answer.path', 'malformed path: ' + path)
    return [int(match[2]) if match[2] is not None else match[1] for match in PATH_TOKEN.finditer(path)]


def apply_edit(proposal, path, edit):
    tokens = parse(path)
    require(tokens[0] in ('configuration', 'invocation'), 'answer.path', 'not a proposal path: ' + path)
    parent = proposal
    for token in tokens[:-1]:
        parent = parent[token]
    last = tokens[-1]
    if edit is None:
        del parent[last]
    else:
        require(isinstance(parent[last], str), 'answer.path', 'replace targets text only: ' + path)
        parent[last] = edit


def reconcile(proposal, admitted, gate):
    """Apply one answer to the exact proposal; returns the revised proposal, or None for rejection.

    Answers may supplement instructions or remove/replace custom inputs only.
    They never edit requirements, prohibitions, permissions or the original request.
    """
    value = gate['answer']['value']
    require(isinstance(value, dict) and value.get('action') in ANSWERS, 'answer.action',
            'must be one of ' + ', '.join(ANSWERS))
    action = value['action']
    allowed = editable(admitted)
    revised = copy.deepcopy(proposal)
    if action == 'reject':
        fields(value, 'answer', ('action',), ('reason',))
        if 'reason' in value:
            text(value['reason'], 'answer.reason', 4096)
        return None
    if action == 'supplement':
        fields(value, 'answer', ('action', 'text'))
        body = text(value['text'], 'answer.text')
        invocation = revised['invocation'] if revised['invocation'] is not None else {}
        invocation.setdefault('prompts', []).append(
            {'id': 'clarification-' + hashlib.sha256(gate['answer']['answer_id'].encode()).hexdigest()[:16],
             'slot': 'additional', 'text': body})
        revised['invocation'] = invocation
        return revised
    if action == 'remove':
        fields(value, 'answer', ('action', 'paths'))
        paths = value['paths']
        require(isinstance(paths, list) and paths and len(set(map(str, paths))) == len(paths), 'answer.paths',
                'must be a nonempty array of distinct paths')
    else:
        fields(value, 'answer', ('action', 'path', 'text'))
        paths = [value['path']]
        text(value['text'], 'answer.text')
    for path in paths:
        require(isinstance(path, str) and action in allowed.get(path, ()), 'answer.paths',
                f'{action} is not a supported change for {path}; requirements, prohibitions, permissions and '
                'the original request are never edited by a clarification answer')
    # Later list elements first, so earlier indices stay valid.
    for path in sorted(paths, key=lambda item: [(isinstance(t, int), t) for t in parse(item)], reverse=True):
        apply_edit(revised, path, value['text'] if action == 'replace' else None)
    return revised


# -- Operation -------------------------------------------------------------------------------

def outcome(admitted, proposal, request_id, verdict, clarifications, validator=None, gate=None, error=None,
            split=None):
    result = copy.deepcopy(admitted)
    result.pop('error', None)
    semantic = verdict['verdict'] if verdict else 'blocked'
    result['semantic'] = semantic
    result['preflight'] = PREFLIGHT[semantic]
    record = result['admission']
    record['semantic'] = semantic
    if split is not None and semantic == 'accepted':
        # Reused units keep their original native evidence, never a fresh copy.
        reused = sorted({'native:' + unit['record']['result_ref'] for unit in split['reused']})
        record['evidence_refs'] = record['evidence_refs'] + reused
    if validator:
        # Only the engine-observed validator result is evidence; answers stay in clarifications.
        record['evidence_refs'] = record['evidence_refs'] + ['native:' + validator['result_ref']]
    if split is not None:
        result['reuse'] = {
            'reused': {unit['identity']: unit['record']['result_ref'] for unit in split['reused']},
            'subjects': [unit['identity'] for unit in split['subjects']],
            'invalidated': split['invalidated']}
    result['assessment'] = {
        'request_id': request_id, 'evidence_id': evidence_id(admitted), 'validator': validator,
        **({key: verdict[key] for key in ('summary', 'conflicts', 'uncertainties', 'resolved')} if verdict else
           {'summary': None, 'conflicts': [], 'uncertainties': [], 'resolved': []}),
    }
    result['clarifications'] = copy.deepcopy(clarifications)
    result['proposal'] = copy.deepcopy(proposal)
    if gate is not None:
        result['gate'] = gate
    if error is not None:
        result['error'] = error
    return result


def ask(admitted, proposal, request_id, verdict, validator, clarifications, transport, retry=None, ledger=None,
        split=None):
    """Open one durable input gate pinned to this exact proposal; the gate holds the verdict."""
    identity = evidence_id(admitted)
    payload = {'verdict': verdict, 'validator': validator, 'editable': editable(admitted),
               'actions': list(ANSWERS), 'retry': retry}
    request = {'request_id': request_id, 'evidence_id': identity, 'payload': payload,
               'prompt': 'Resolve the configuration conflicts and uncertainties in the payload: supplement the '
                         'instructions, change or remove a custom input, or reject the proposal.'}
    try:
        opened = transport.open_input_gate(copy.deepcopy(request))
        fields(opened, 'gate', ('gate_id',))
        gate_id = text(opened['gate_id'], 'gate.gate_id', 256)
    except Exception as failure:  # The verdict stands, but nothing durable asks the user yet.
        error = {'path': 'gate', 'message': 'input gate could not be opened: ' + type(failure).__name__}
        return outcome(admitted, proposal, request_id, None, clarifications, validator, error=error, split=split)
    if ledger is not None and retry is None:
        ledger.asked(identity, gate_id)
    gate = {'gate_id': gate_id, 'evidence_id': identity, 'status': 'waiting'}
    return outcome(admitted, proposal, request_id, verdict, clarifications, validator, gate=gate,
                   error=retry and {'path': 'answer', 'message': retry}, split=split)


REUSED = {'verdict': 'accepted', 'summary': 'Every custom input has a current acceptance; its original evidence is reused.',
          'conflicts': [], 'uncertainties': [], 'resolved': []}


def assess(admitted, proposal, request_id, clarifications, transport, ledger=None):
    """Run one engine-managed validator session for the exact proposal and interpret it.

    With a ledger, current acceptances are reused, a retained unasked question is
    asked without a new session, and an acceptance is retained per subject unit.
    """
    split = acceptance.plan(admitted, ledger)
    if not split['subjects']:
        return outcome(admitted, proposal, request_id, REUSED, clarifications, split=split)
    answers = {item['answer_id'] for item in clarifications}
    identity = evidence_id(admitted)
    stored = ledger.question(identity) if ledger is not None else None
    if stored is not None and stored['gate_id'] is None:
        # The session ran but its gate never opened: ask the retained question, run nothing new.
        try:
            verdict = check_verdict(stored['verdict'], set(locations(admitted)), answers)
        except (AdmissionError, TypeError, KeyError):
            verdict = None
        if verdict is not None:
            return ask(admitted, proposal, request_id, verdict, stored['validator'], clarifications, transport,
                       ledger=ledger, split=split)
    task = assignment(admitted, request_id, clarifications, split)
    try:
        session = transport.validator_session(copy.deepcopy(task))
    except Exception as failure:
        error = {'path': 'validator', 'message': 'validator session failed: ' + type(failure).__name__}
        return outcome(admitted, proposal, request_id, None, clarifications, error=error, split=split)
    try:
        reference, session_id, verdict = check_session(session, task, answers)
    except (AdmissionError, TypeError, KeyError, AttributeError, ValueError) as failure:
        message = failure.message if isinstance(failure, AdmissionError) else 'malformed session result'
        path = failure.path if isinstance(failure, AdmissionError) else 'validator'
        return outcome(admitted, proposal, request_id, None, clarifications, error={'path': path, 'message': message},
                       split=split)
    validator = {'role': task['role'], 'definition_sha256': task['definition_sha256'], 'agent': task['agent'],
                 'session_id': session_id, 'result_ref': reference, 'claim': task['claim']}
    if verdict['verdict'] == 'needs_clarification':
        if ledger is not None:
            ledger.ask(identity, request_id, verdict, validator)
        return ask(admitted, proposal, request_id, verdict, validator, clarifications, transport, ledger=ledger,
                   split=split)
    if verdict['verdict'] == 'accepted' and ledger is not None:
        ledger.accept(split['subjects'], split['reused'], evidence_id=identity, request_id=request_id, validator=validator,
                      clarifications=clarifications)
    return outcome(admitted, proposal, request_id, verdict, clarifications, validator, split=split)


def admit(*, request, configuration, invocation, deployment, roles, roots, transport, ledger=None, defaults=None,
          check=None):
    """Structural admission followed, for custom inputs only, by semantic assessment.

    transport offers capabilities(), validator_session(assignment), input_gates(request_id)
    and open_input_gate(request). The default plan calls only capabilities().
    ledger, an acceptance.Ledger, enables reuse of current acceptances; a fully
    current proposal also calls only capabilities(). defaults is admission's
    preset defaults. check(admitted) returns (path, message) when a structurally
    admitted proposal cannot run where it is headed (such as the installed
    workflow); it applies before any gate read or session, and to every revision.
    """
    fixed = {'request': request, 'deployment': deployment, 'roles': roles, 'roots': roots}

    def structural(proposal):
        result = admission.admit(**copy.deepcopy(fixed | proposal), transport=transport, defaults=defaults)
        failure = check(result) if check is not None and result['preflight'] == 'semantic_required' else None
        if failure:
            result = {**result, 'preflight': 'rejected', 'error': {'path': failure[0], 'message': failure[1]}}
        return result
    proposal = {'configuration': copy.deepcopy(configuration), 'invocation': copy.deepcopy(invocation)}
    admitted = structural(proposal)
    if admitted['preflight'] != 'semantic_required':
        return admitted  # Default plan, structural rejection, blocked engine or unsupported supplied skill.
    request_id = admitted['admission']['request_id']
    if ledger is not None:
        split = acceptance.plan(admitted, ledger)
        if not split['subjects']:
            return outcome(admitted, proposal, request_id, REUSED, [], split=split)
    try:
        gates = check_gates(transport.input_gates(request_id), request_id)
    except Exception as failure:
        message = failure.message if isinstance(failure, AdmissionError) else type(failure).__name__
        return outcome(admitted, proposal, request_id, None, [],
                       error={'path': 'gates', 'message': 'input gates unavailable: ' + message})
    clarifications = []
    cursor = 0  # Gates are consumed once each, in creation order; every round consumes one.
    while True:
        identity = evidence_id(admitted)
        position = next((number for number in range(cursor, len(gates))
                         if gates[number]['evidence_id'] == identity), None)
        if position is None:
            # Nothing unconsumed asks about this proposal, including one that a revision returned to.
            return assess(admitted, proposal, request_id, clarifications, transport, ledger)
        gate, cursor = gates[position], position + 1
        later = any(item['evidence_id'] == identity for item in gates[cursor:])
        payload = gate['request']['payload']
        answers = {item['answer_id'] for item in clarifications}
        try:
            verdict = check_verdict(payload['verdict'], set(locations(admitted)), answers)
        except (AdmissionError, TypeError, KeyError) as failure:
            return outcome(admitted, proposal, request_id, None, clarifications,
                           error={'path': f"gates.{gate['gate_id']}", 'message': 'gate verdict invalid: ' + str(failure)})
        validator = payload.get('validator')
        if gate['status'] == 'waiting':  # Interrupted clarification resumes without a new session or gate.
            return outcome(admitted, proposal, request_id, verdict, clarifications, validator,
                           gate={'gate_id': gate['gate_id'], 'evidence_id': identity, 'status': 'waiting'})
        try:
            revised = reconcile(proposal, admitted, gate)
            retried = structural(revised) if revised is not None else None
        except (AdmissionError, TypeError, KeyError, IndexError) as failure:
            message = failure.message if isinstance(failure, AdmissionError) else 'path does not exist'
            retried = {'preflight': 'rejected', 'error': {'path': 'answer', 'message': message}}
        if retried is not None and retried['preflight'] == 'rejected':
            # Not applied: the proposal and verdict stand, and the user is asked again exactly once.
            if later:
                continue
            error = retried['error']
            return ask(admitted, proposal, request_id, verdict, validator, clarifications, transport,
                       retry=f"answer {gate['answer']['answer_id']} was not applied: {error['path']}: {error['message']}")
        clarifications.append({'gate_id': gate['gate_id'], 'evidence_id': identity,
                               'answer_id': gate['answer']['answer_id'], 'value': gate['answer']['value'],
                               'conflicts': verdict['conflicts'], 'uncertainties': verdict['uncertainties']})
        if revised is None:
            rejected = {**verdict, 'verdict': 'rejected', 'summary': 'The user rejected the proposal.'}
            return outcome(admitted, proposal, request_id, rejected, clarifications, validator)
        proposal, admitted = revised, retried
        if admitted['preflight'] != 'semantic_required':
            # Removing every custom input leaves the default path; a failed engine query stays blocked.
            return {**admitted, 'clarifications': clarifications, 'proposal': copy.deepcopy(proposal)}
