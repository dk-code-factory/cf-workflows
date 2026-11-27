"""Engine binding of the configuration entries' native sessions (docs/configuration-skills.md#engine-binding).

cf_workflows.configuration runs every entry procedure on the host: discovery,
the read-only configuration view, the report and proposal checks, admission and
the single configuration writer (cf_workflows.writes). Its only engine
operation is native_session(task). EngineTransport maps that operation onto the
entry's installed workflow, so the installed graph holds the session and
nothing else:

- dk-repo-onboard runs `workflows/onboard.yml`, dk-config `workflows/config.yml`
  and dk-preferences `workflows/preferences.yml`. Each is a preset over the
  shared `workflows/configure.yml`, which runs one `agent/run` on the admitted
  literal `agents/leaf.yaml` with no `skill` selection, the mode's JSON schema
  and no output repair. There is no helper container, PR capture, publication
  stage or invented PR: the target travels in the typed assignment.
- The typed input carries the assignment and the exact text of every readable
  file, read again and checked against its admitted digest, so the session
  needs no file access. An input larger than the engine's task mailbox blocks
  before any run.
- The run is correlated by the assignment's claim, so a replayed claim returns
  the engine's retained run and never starts a second session.
- The `model` control is the step's `with.model`. The engine must report the
  same model, or the binding blocks. A per-run reasoning effort has no engine
  parameter (it is a definition field), so it blocks before any run.
- Clarification gates and the shared validator use the inherited
  validator_binding transport (`workflows/assess.yml`).

Like the validator binding, the engine records the task's opaque agent
reference, provider and structured output, but no tool operations. Without an
operation receipt the observation reports `operations: None`, and
configuration.run_session blocks the result instead of assuming that the session
only read its files.
"""
import hashlib
import json
from pathlib import Path

from cf_workflows import admission, validator_binding
from cf_workflows.validator_binding import BindingError

INSTALL = validator_binding.INSTALL
SHARED = 'workflows/configure.yml'
# The installed preset of each entry and the session modes it runs.
WORKFLOWS = {'cf-workflows:dk-repo-onboard': ('workflows/onboard.yml', ('onboard',)),
             'cf-workflows:dk-config': ('workflows/config.yml', ('explain', 'change')),
             'cf-workflows:dk-preferences': ('workflows/preferences.yml', ('change',))}
DEFINITION = 'workflows/agents/leaf.yaml'
PROMPTS = {
    'onboard': 'Return the onboarding report for input.assignment as its configuration entry. input.files holds '
               'the exact text of every source you may inspect, keyed as in assignment.readable; do not read, run '
               'or change anything else, and save nothing.',
    'explain': 'Explain the saved configuration in input.assignment.context as its configuration entry, citing only '
               'paths from context.configuration.paths. Do not read, run or change anything.',
    'change': 'Answer the exact ask in input.assignment as its configuration entry: return a proposal with the exact '
              'typed operations from context.operations, concrete questions, or a cancellation. Encode each '
              'operation object as a JSON string. Do not read, run, save or change anything.',
}


def check_definition(task, install=INSTALL):
    """The installed leaf definition is the admitted one: its bytes match the assignment's digest."""
    path = Path(install) / DEFINITION
    if task.get('role') != 'leaf':
        raise BindingError('configuration entries run the admitted leaf role only')
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != task.get(
            'definition_sha256'):
        raise BindingError('installed leaf definition differs from the admitted definition')


def workflow(task):
    """The entry's installed preset for this session mode."""
    if task.get('skill') not in WORKFLOWS or task.get('mode') not in WORKFLOWS[task['skill']][1]:
        raise BindingError('no installed configuration workflow for this entry and mode')
    return WORKFLOWS[task['skill']][0]


def session_inputs(task, roots):
    """The preset's inputs; the whole task must fit the engine's task mailbox."""
    native = task.get('native') or {}
    if 'reasoning_effort' in native:
        raise BindingError('the engine takes no per-run reasoning effort; it is an agent definition field')
    inputs = {'mode': task['mode'], 'prompt': PROMPTS[task['mode']], 'model': native.get('model', ''),
              'input': {'assignment': task, 'files': validator_binding.files(task, roots)}}
    if len(admission.canonical(inputs)) > validator_binding.MAILBOX - validator_binding.MAILBOX_RESERVE:
        raise BindingError('configuration assignment exceeds the engine task mailbox')
    return inputs


def output(task, result):
    """The structured result in the library's shape: proposal operations arrive as JSON strings."""
    if task['mode'] != 'change' or not isinstance(result, dict) or 'operations' not in result:
        return result
    operations = result['operations']
    if not isinstance(operations, list) or not all(isinstance(item, str) for item in operations):
        raise BindingError('proposal operations must be JSON strings')
    try:
        decoded = [json.loads(item) for item in operations]
    except ValueError:
        raise BindingError('a proposal operation is not JSON') from None
    return {**result, 'operations': decoded}


def session(task, outcome):
    """The native session record of one entry run, from what the engine records.

    session_id is the engine's opaque agent reference. Role, definition and the
    empty skill set are fixed by the admitted step (a literal definition whose
    bytes check_definition pinned, no skill selection, and the engine's skill
    discovery isolation), not observed per session. The provider must be the
    assigned agent's, and a chosen model must be the one the engine reports.
    operations comes only from an engine operation receipt; without one it is None.
    """
    if not isinstance(outcome, dict) or outcome.get('status') != 'succeeded':
        raise BindingError('configuration session did not succeed')
    outputs = (outcome.get('outputs') or {}).get(task['mode']) if isinstance(outcome.get('outputs'), dict) else None
    if not isinstance(outputs, dict) or not isinstance(outputs.get('agent'), str) or not outputs['agent']:
        raise BindingError('configuration session returned no agent reference')
    if outputs.get('provider') != task['provider']:
        raise BindingError('configuration session ran on another provider')
    native = task.get('native') or {}
    observed = {}
    if 'model' in native:
        if outputs.get('model') != native['model']:
            raise BindingError('configuration session did not report the chosen model')
        observed['native'] = {'model': outputs['model']}
    result = output(task, outputs.get('structured'))
    reference = 'agent-run:' + admission.digest({'run': outcome.get('run_id'), 'agent': outputs['agent'],
                                                 'result': outputs.get('structured')})
    return {'result_ref': reference, 'output': result,
            'observation': {'session_id': outputs['agent'], 'agent': task['agent'], 'role': task['role'],
                            'definition_sha256': task['definition_sha256'], 'skills_loaded': [],
                            'operations': outputs.get('operations'), 'claim': task['claim'], **observed}}


class EngineTransport(validator_binding.EngineTransport):
    """The configuration-entry transport over an Agent Workflow engine port; see the module docstring."""

    def native_session(self, task):
        target = workflow(task)
        check_definition(task, self.install)
        outcome = self.engine.run(target, session_inputs(task, self.roots), task['claim'])
        return session(task, outcome)
