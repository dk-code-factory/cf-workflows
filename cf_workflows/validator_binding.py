"""Engine binding of the semantic-assessment transport (docs/semantic.md#engine-binding).

cf_workflows.semantic asks a transport for capabilities(), one
validator_session(assignment), input_gates(request_id) and
open_input_gate(request). EngineTransport maps them onto Agent Workflow
primitives through the installed workflow `workflows/assess.yml`:

- validator_session runs its `validate` job: one `agent/run` native task on the
  literal bundled definition `workflows/validator/configuration-validator.yaml`,
  with no `skill` selection, a fixed JSON verdict schema and no output repair. The
  typed input carries the assignment and the exact bytes of every readable file,
  so the validator needs no file access.
- open_input_gate runs its `clarify` job: one `input/wait` gate whose evidence ID
  is the proposal's evidence ID and whose payload carries the request ID.
- input_gates reads the engine's durable gate records (`agent-workflow inputs`)
  for the request and maps pending, accepted and consumed gates.

The engine is a host port, never a local launcher: run(workflow, inputs, key)
admits one run of an installed workflow under a correlation key and returns its
status, run ID and outputs; inputs(key) returns the ExternalInputGate records of
the runs under that key; capabilities() reports the engine contract list and
the providers' capabilities. Only `durable-external-input-v1` maps to a suite
name. Engine main's per-runtime ProviderCapabilities record (sessions, resume,
interrupt, structured_output, managed_policy) maps to no suite provider
capability, so against it admission rejects every provider; see
UNMAPPED_PROVIDER_CAPABILITIES and docs/admission.md#engine-capability-vocabulary.

The engine records the task's opaque agent reference, provider and structured
output. It does not record which tools the session used: native tasks share the
deployment's one runtime role and its tool policy, and only supplied-skill
invocations get observed receipts. A session result without an operation receipt
therefore reports `operations: None`, and semantic.check_session blocks it
instead of assuming a read-only session.
"""
import hashlib
import json
from pathlib import Path

from cf_workflows import admission

INSTALL = Path(__file__).resolve().parent / 'workflows' / 'dk-pr-fix'
ASSESSMENT = 'workflows/assess.yml'
DEFINITION = 'workflows/validator/configuration-validator.yaml'
# Engine contract names mapped to the suite's engine-level capability names.
ENGINE_CAPABILITIES = {'durable-external-input-v1': admission.INPUT_CAPABILITY}
# The fields of Agent Workflow's per-runtime ProviderCapabilities record (pkg/workflows/context.go
# at 8758775, reported by `agent-workflow doctor`). None of them is a suite provider capability.
ENGINE_PROVIDER_FIELDS = ('sessions', 'resume', 'interrupt', 'structured_output', 'managed_policy')
# Every provider capability structural admission can require, with why no engine field stands for it.
UNMAPPED_PROVIDER_CAPABILITIES = {
    'native_session': 'no engine field admits the suite\'s native task contract; `sessions` reports '
                      'conversation-session support only, and no released engine contains #129/#130',
    'admitted_roles': 'native tasks share the deployment\'s one runtime role and its tool policy; no '
                      'per-task role or read-only policy is reported (agent-workflow #151)',
    admission.MERGE_CAPABILITY: 'the #138 repository merge guard is a helper check, not a provider '
                                'capability the engine reports',
    **{profile: 'the #139 skill profile is enforced when a skill selection is admitted, not reported; '
                'selections in reusable workflows reject (agent-workflow #152)'
       for profile in (admission.CLAUDE_SKILL_PROFILE, admission.CLAUDE_CHILD_PROFILE,
                       admission.CODEX_SKILL_PROFILE)},
}
# Agent Workflow's task mailbox bound for the prompt, structured-output instructions and typed input.
MAILBOX = 1 << 20
MAILBOX_RESERVE = 64 << 10
GATE_STATES = {'pending': 'waiting', 'accepted': 'answered', 'consumed': 'answered'}
SKIPPED_STATES = ('canceled', 'replaced')  # Invalidated questions carry no usable answer.
PROMPT = ('Assess the configuration proposal in input.assignment as the bundled configuration validator. '
          'input.files holds the exact bytes of every file you may consider, keyed as in '
          'assignment.readable; do not read, run or change anything else. Return one verdict for the '
          'assignment subjects, citing only paths from assignment.paths.')


class BindingError(RuntimeError):
    """The engine result cannot stand for the requested transport operation."""


def converted_definition(source):
    """The engine's canonical YAML form of the bundled Codex TOML validator definition."""
    import tomllib
    value = tomllib.loads(source.decode('utf-8'))
    pairs = [('name', value['name']), ('description', value['description']),
             ('instructions', value['developer_instructions'])]
    if 'model_reasoning_effort' in value:
        pairs.append(('reasoning-effort', value['model_reasoning_effort']))
    return ''.join(f'{key}: {json.dumps(text, ensure_ascii=False)}\n' for key, text in pairs).encode()


def check_definition(install=INSTALL):
    """The installed YAML definition is exactly the conversion of the pinned bootstrap; returns its digest."""
    admission.bootstrap()  # Pinned digest and structural shape of the bundled TOML.
    expected = converted_definition(admission.BOOTSTRAP_DEFINITION.read_bytes())
    path = Path(install) / DEFINITION
    if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
        raise BindingError('installed validator definition is not the conversion of the pinned bootstrap')
    return hashlib.sha256(expected).hexdigest()


def resolve(key, task, roots):
    """The local file of one readable key: a configured resource, repository guidance or a skill file."""
    kind, _, rest = key.partition(':')
    if kind == 'skill':
        skill_id, _, rel = rest.partition('/')
        skill = task['input']['custom']['skills'][skill_id]
        return roots[skill['root']], skill['path'] + '/' + rel
    return roots[kind], rest


def files(task, roots):
    """{key: text} for every readable file, read again and checked against its admitted digest."""
    result = {}
    for key, expected in task['readable'].items():
        root, rel = resolve(key, task, roots)
        content = admission.read_file(Path(root), admission.relative(rel, 'readable.' + key), 'readable.' + key)
        if hashlib.sha256(content).hexdigest() != expected:
            raise BindingError('readable file changed since admission: ' + key)
        try:
            result[key] = content.decode('utf-8')
        except UnicodeDecodeError:
            raise BindingError('readable file is not UTF-8 text: ' + key) from None
    return result


def validate_inputs(task, roots):
    """The `validate` job inputs; the whole task must fit the engine's task mailbox."""
    inputs = {'mode': 'validate', 'prompt': PROMPT, 'input': {'assignment': task, 'files': files(task, roots)}}
    if len(admission.canonical(inputs)) > MAILBOX - MAILBOX_RESERVE:
        raise BindingError('validator assignment exceeds the engine task mailbox')
    return inputs


def session(task, outcome):
    """The semantic session record of one `validate` run, from what the engine records.

    session_id is the engine's opaque agent reference. Role, definition and the
    empty skill set are fixed by the admitted step (a literal definition whose
    bytes check_definition pinned, no skill selection, and the engine's skill
    discovery isolation), not observed per session. The provider is observed and
    must be the assigned agent's. operations comes only from an engine operation
    receipt; without one it is None.
    """
    if not isinstance(outcome, dict) or outcome.get('status') != 'succeeded':
        raise BindingError('validator task did not succeed')
    outputs = outcome.get('outputs')
    if not isinstance(outputs, dict) or not isinstance(outputs.get('agent'), str) or not outputs['agent']:
        raise BindingError('validator task returned no agent reference')
    if outputs.get('provider') != task['provider']:
        raise BindingError('validator task ran on another provider')
    reference = 'agent-run:' + admission.digest({'run': outcome.get('run_id'), 'agent': outputs['agent'],
                                                 'verdict': outputs.get('verdict')})
    return {'result_ref': reference, 'output': outputs.get('verdict'),
            'observation': {'session_id': outputs['agent'], 'agent': task['agent'],
                            'role': admission.VALIDATOR_ROLE,
                            'definition_sha256': admission.BOOTSTRAP_SHA256,
                            'skills_loaded': [], 'operations': outputs.get('operations')}}


def gate(record, request_id):
    """One semantic gate from an engine ExternalInputGate record, or None when it was invalidated."""
    state = record.get('state')
    if state in SKIPPED_STATES:
        return None
    if state not in GATE_STATES:
        raise BindingError('unsupported engine gate state: ' + str(state))
    payload = record.get('payload')
    if not isinstance(payload, dict) or set(payload) != {'request_id', 'assessment'}:
        raise BindingError('engine gate payload is not an assessment question')
    evidence = record.get('evidence_id')
    answer = record.get('answer')
    if GATE_STATES[state] == 'answered':
        if not isinstance(answer, dict) or answer.get('evidence_id') != evidence:
            raise BindingError('engine answer is not pinned to its gate evidence')
        answer = {'answer_id': answer.get('answer_id'), 'value': answer.get('value')}
    else:
        answer = None
    return {'gate_id': record.get('gate_id'), 'request_id': payload['request_id'], 'evidence_id': evidence,
            'status': GATE_STATES[state], 'answer': answer,
            'request': {'request_id': payload['request_id'], 'evidence_id': evidence,
                        'prompt': record.get('prompt'), 'payload': payload['assessment']}}


def providers(offered):
    """Suite capability lists per provider; an engine ProviderCapabilities record maps to none.

    A list is taken as the suite names the port reports. A record in the engine's own
    shape names no suite capability (UNMAPPED_PROVIDER_CAPABILITIES), so admission
    rejects the provider as lacking each required one instead of as unavailable.
    """
    if not isinstance(offered, dict):
        return offered
    result = {}
    for provider, value in offered.items():
        if isinstance(value, dict):
            if not (set(value) <= set(ENGINE_PROVIDER_FIELDS) and all(isinstance(v, bool) for v in value.values())):
                raise BindingError(f'provider {provider} capabilities are not an engine ProviderCapabilities record')
            value = []
        result[provider] = value
    return result


class EngineTransport:
    """The semantic transport over an Agent Workflow engine port; see the module docstring."""

    def __init__(self, engine, roots, install=INSTALL):
        self.engine, self.roots, self.install = engine, dict(roots), Path(install)

    def capabilities(self):
        raw = self.engine.capabilities()
        offered = raw['capabilities']
        if not isinstance(offered, list):
            raise BindingError('engine capabilities must be a list')
        return {'engine': {'version': raw['version'],
                           'capabilities': sorted({ENGINE_CAPABILITIES[name] for name in offered
                                                   if name in ENGINE_CAPABILITIES})},
                'providers': providers(raw['providers'])}

    def validator_session(self, task):
        check_definition(self.install)
        outcome = self.engine.run(ASSESSMENT, validate_inputs(task, self.roots), task['claim'])
        return session(task, outcome)

    def input_gates(self, request_id):
        result = []
        for record in self.engine.inputs(request_id):
            mapped = gate(record, request_id)
            if mapped is not None:
                result.append(mapped)
        return result

    def open_input_gate(self, request):
        inputs = {'mode': 'clarify', 'prompt': request['prompt'], 'evidence_id': request['evidence_id'],
                  'payload': {'request_id': request['request_id'], 'assessment': request['payload']}}
        outcome = self.engine.run(ASSESSMENT, inputs, request['request_id'])
        if not isinstance(outcome, dict) or outcome.get('status') != 'waiting':
            raise BindingError('clarification run did not wait for input')
        opened = [record for record in self.engine.inputs(request['request_id'])
                  if record.get('origin_run_id') == outcome.get('run_id') and record.get('state') == 'pending'
                  and record.get('evidence_id') == request['evidence_id']]
        if len(opened) != 1:
            raise BindingError('clarification run did not open exactly one gate')
        return {'gate_id': opened[0]['gate_id']}
