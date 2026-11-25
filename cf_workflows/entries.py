"""Configuration entry points as thin presets over the shared workflow (contract v1).

cf-workflows:dk-repo-onboard, dk-config and dk-preferences add no orchestration.
Each entry is a preset selection for the shared plan (one bundled adapter task,
explicitly empty finalizers and reviewers), the adapter procedure that task runs
(admitted role, bundled assignment and the helper operations in
cf_workflows.configuration) and the entry's original options. Preflight runs the
same structural and semantic admission as every other run. No skill is loaded,
and no entry accepts a remote-write authorization.

An entry's own run is admitted from bundled defaults and invocation-local prompts.
Saved configuration is its subject, not its governing input, so an invalid saved
document can still be inspected and repaired.

Original role prompts and options come from the pinned dk-workflows baseline
(MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy
from pathlib import Path

from . import admission, dk_pr_fix, semantic
from .admission import AdmissionError, require

# Exact baseline role prompts (prompts/default.md at the pinned commit; digests in baselines/inventory.json).
ENTRIES = {
    'dk-repo-onboard': {
        'skill': 'cf-workflows:dk-repo-onboard', 'adapter': 'onboard-repository', 'saves': False,
        'role_prompt': 'Onboard a repository by discovering instruction sources and reporting all discovered rules, '
                       'settings and gaps in a structured inventory. Use for requested repository onboarding or an '
                       'explicit discovery refresh.\n',
        'baseline_prompt': 'claude/skills/dk-repo-onboard/prompts/default.md',
        'options': {}, 'operations': ('discover', 'view', 'check_report'), 'domains': (),
    },
    'dk-config': {
        'skill': 'cf-workflows:dk-config', 'adapter': 'configure', 'saves': True,
        'role_prompt': 'Change, inspect and explain saved dk-workflows settings, including instruction-source '
                       'choices. Use dk-repo-onboard for requested repository discovery; ordinary workflows read '
                       'settings directly.\n',
        'baseline_prompt': 'claude/skills/dk-config/prompts/default.md',
        'options': {}, 'operations': ('view', 'explain', 'apply_operations', 'admit', 'write', 'readback'),
        'domains': ('add_scope', 'remove_scope', 'set_option', 'unset_option', 'set_permission',
                    'unset_permission', 'set_prompt', 'remove_prompt'),
    },
    'dk-preferences': {
        'skill': 'cf-workflows:dk-preferences', 'adapter': 'admit-preferences', 'saves': True,
        'role_prompt': 'Add, update, list, inspect or remove personal and repository preferences for implementation '
                       'and review, with concrete scopes and contradiction checks before saving.\n',
        'baseline_prompt': 'claude/skills/dk-preferences/prompts/default.md',
        'options': {'default_applies_to': ['implementation', 'review']},
        'operations': ('view', 'prepare', 'overlaps', 'apply_operations', 'admit', 'write', 'readback', 'resolve'),
        'domains': ('add_scope', 'set_preference', 'remove_preference'),
    },
}


def preset(name):
    """The entry's selection for the shared plan: one adapter task and explicitly empty slots."""
    return {'tasks': [ENTRIES[name]['adapter']], 'finalizers': [], 'reviewers': []}


def check_options(name, options):
    """The entry's own options (its baseline schema) with provenance; unknown options reject."""
    require(name in ENTRIES, 'entry', 'unknown configuration entry')
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    declared = ENTRIES[name]['options']
    result = {key: {'value': copy.deepcopy(value), 'source': 'default'} for key, value in declared.items()}
    for key, value in options.items():
        require(key in declared, 'options.' + str(key), 'unsupported option for ' + ENTRIES[name]['skill'])
        if key == 'default_applies_to':
            require(isinstance(value, list) and value and len(set(map(str, value))) == len(value)
                    and all(mode in admission.PREFERENCE_MODES for mode in value),
                    'options.default_applies_to', 'must be distinct implementation/review modes')
            value = [mode for mode in admission.PREFERENCE_MODES if mode in value]
        result[key] = {'value': value, 'source': 'invocation'}
    return result


def _rejected(path, message, **extra):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}, **extra}


def preflight(name, *, request, deployment, repository, transport, invocation=None, options=None, roots=None,
              install=dk_pr_fix.INSTALL, ledger=None):
    """Admit one run of a configuration entry through the shared admission pipeline.

    invocation may carry only `prompts`: the task, finalizer and reviewer selection
    is the entry's preset. Writes nothing; transport sees the same operations as
    semantic.admit (only capabilities() for the default plan).
    """
    try:
        entry_options = check_options(name, options)
        require(isinstance(request, dict), 'request', 'must be an object')
        require(not request.get('authorizations'), 'request.authorizations',
                'configuration entries perform no remote write; a permission is a saved setting, not an authorization')
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'prompts'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and permissions; only prompts may be added')
        # Discovery is read-only and leaves no history: a custom prompt would need a semantic
        # assessment whose clarification opens a durable gate, so onboarding takes none.
        require(name != 'dk-repo-onboard' or not invocation.get('prompts'), 'invocation.prompts',
                'dk-repo-onboard takes no custom prompts; it creates no request history')
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
    merged = {'options': preset(name), 'prompts': copy.deepcopy(invocation.get('prompts', []))}
    result = semantic.admit(request=copy.deepcopy(request), configuration=None, invocation=merged,
                            deployment=deployment, roles=dk_pr_fix.role_catalog(install),
                            roots={**roots, 'repository': repository, dk_pr_fix.INSTALL_ROOT: install},
                            transport=transport, ledger=ledger)
    entry = ENTRIES[name]
    result['entry'] = {'name': name, 'skill': entry['skill'], 'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    snapshot = result['snapshot']
    [task] = snapshot['plan']['tasks']
    assert task['adapter'] == entry['adapter'] and [stage['kind'] for stage in snapshot['plan']['stages']] == ['tasks']
    assert not any(value['effective'] for value in snapshot['permissions'].values())
    result['entry'].update({
        'adapter': entry['adapter'], 'task': task, 'definition': snapshot['adapters'][entry['adapter']],
        'role_prompt': entry['role_prompt'], 'operations': list(entry['operations']),
        'domains': list(entry['domains']), 'saves': entry['saves'],
        # Applicable custom prompts reach the session after semantic acceptance, apart from the ask.
        'prompts': [{key: item[key] for key in ('id', 'slot', 'target', 'text')}
                    for item in snapshot['prompts'] if item['applicable']],
    })
    return result
