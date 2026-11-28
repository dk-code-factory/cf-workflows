"""Supporting entry points as thin presets over the shared workflow (contract v1).

Configuration (cf-workflows:dk-repo-onboard, dk-config, dk-preferences) and
alternatives (dk-issue-alternatives, dk-pr-alternatives, the dk-alternatives-review
compatibility entry and the manual dk-pr-review-alternatives) add no orchestration.
Each entry is a preset selection for the shared plan (one bundled adapter task,
explicitly empty finalizers and reviewers), the adapter procedure that task runs
(admitted role, bundled assignment and the helper operations in
cf_workflows.configuration or cf_workflows.alternatives) and the entry's original options. Preflight runs the
same structural and semantic admission as every other run. No skill is loaded,
and no entry accepts a remote-write authorization.

An entry's own run is admitted from bundled defaults and invocation-local prompts.
The alternatives entries also apply their saved entry options (SAVED_OPTIONS).
Otherwise saved configuration is its subject, not its governing input, so an invalid saved
document can still be inspected and repaired.

Original role prompts and options come from the pinned dk-workflows baseline
(MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy
from pathlib import Path
import re

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
                    'unset_permission', 'set_prompt', 'remove_prompt', 'set_resource', 'remove_resource',
                    'set_entry_option', 'unset_entry_option'),
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
    # Read-only approver discovery (#29); its helper side is cf_workflows.approvers.
    'dk-pr-approvers': {
        'skill': 'cf-workflows:dk-pr-approvers', 'adapter': 'discover-approvers', 'saves': False,
        'role_prompt': 'Resolve GitHub review and re-review recipients for a PR, returning a configured override or '
                       'discovering appropriate reviewers from repository and PR evidence. Returns recipients without '
                       'requesting reviews.\n\nPrefer appropriate existing requests and previous human reviewers for '
                       're-review. Do not select someone solely from dismissed reviews without another supported '
                       'reason. Do not assume the authenticated actor is the PR author or exclude them on that '
                       'basis.\n\nWhen PR history does not establish suitable recipients, inspect changed paths and '
                       'repository ownership rules at the target revision. If ownership does not resolve the '
                       'selection, assess relevant recent maintainers from path-specific history or merged PR '
                       'reviews, with a bounded targeted search. Avoid unrelated popular contributors and the entire '
                       'contributor history.\n',
        'baseline_prompt': 'claude/skills/dk-pr-approvers/prompts/default.md',
        'options': {}, 'operations': ('override', 'collect', 'codeowners', 'check_selection', 'recheck_head'),
        'domains': (),
    },
    # Read-only flaky-test discovery (#35); its helper side is cf_workflows.ci_flakes.
    'dk-ci-flakes': {
        'skill': 'cf-workflows:dk-ci-flakes', 'adapter': 'inspect-flakes', 'saves': False,
        'role_prompt': 'Actively scan CI history and GitHub issues for flaky-test evidence, or inspect saved '
                       'observations for a failed PR check.\n',
        'baseline_prompt': 'claude/skills/dk-ci-flakes/prompts/default.md',
        'options': {'lookback_days': 30, 'max_runs': 100, 'max_issues': 500},
        'rules': {'lookback_days': range(1, 366), 'max_runs': range(1, 1001), 'max_issues': range(1, 10001)},
        'operations': ('collect', 'link', 'check_report'), 'domains': (),
    },
}

# Options of both alternatives skills (their baseline config.json defaults, identical for issue and
# PR). Non-default prompt text is custom input for the shared semantic admission (cf_workflows.alternatives).
_ASSESSMENT = ('Assess purpose, requirements and repository evidence before comparing approaches. For small fixes '
               'favor focused implementation changes and relevant edge-case coverage. For features exclude code-change '
               'volume from ranking and assess design and maintainability through actual consequences. For mixed work '
               'identify scopes and assign applicable rubrics. Select criteria, positive percentage weights totaling '
               '100 and explicit 0/50/100 anchors, rationale, mandatory constraints and original source references. '
               'Judge architectural decisions with that rubric and supplied evaluation guidance, without a predefined '
               'defect checklist.')
ALTERNATIVES_OPTIONS = {
    'assessment_prompt': _ASSESSMENT,
    'evaluation_prompt': '',
    'scoring': {
        'profile': 'auto',
        'profile_selection_prompt': 'Assess purpose and scope to select criteria, percentage weights and scoring '
                                    'anchors with an evidence-backed rationale. Favor focused changes and relevant '
                                    'edge coverage for small fixes; exclude change volume when ranking feature '
                                    'approaches. Assign applicable rubrics to mixed scopes.',
        'custom_prompt': None,
    },
    'selection': {
        'enabled': True,
        'prompt': 'Assess confidence and sensitivity to LOC and complexity estimates. Accept a clear winner only with '
                  'supported feasibility, adequate score lead and no unresolved material doubts. For a sole feasible '
                  'option substantiate rejection of every alternative.',
        'min_score_gap': 10,
        'mode': 'semi-manual',
    },
}
ALTERNATIVES_RULES = {
    'scoring.profile': ('auto', 'minimal_change_edge_coverage', 'quality', 'minimal_change', 'custom'),
    'selection.mode': ('automatic', 'manual', 'semi-manual'),
    'selection.min_score_gap': range(0, 101),
}
ENTRIES.update({
    'dk-issue-alternatives': {
        'skill': 'cf-workflows:dk-issue-alternatives', 'adapter': 'issue-alternatives', 'saves': False,
        'role_prompt': 'Compare whole-problem implementation approaches from issue requirements, optionally including '
                       'an existing PR.\n',
        'baseline_prompt': 'claude/skills/dk-issue-alternatives/prompts/default.md',
        'options': ALTERNATIVES_OPTIONS, 'rules': ALTERNATIVES_RULES,
        'operations': ('pins', 'prepare', 'scores', 'check_stage', 'finish', 'gate'), 'domains': (),
    },
    'dk-pr-alternatives': {
        'skill': 'cf-workflows:dk-pr-alternatives', 'adapter': 'pr-alternatives', 'saves': False,
        'role_prompt': 'Inventory branch architecture including inherited decisions, then compare questionable '
                       'decisions under the shared PR rubric.\n',
        'baseline_prompt': 'claude/skills/dk-pr-alternatives/prompts/default.md',
        'options': ALTERNATIVES_OPTIONS, 'rules': ALTERNATIVES_RULES,
        'operations': ('pins', 'prepare', 'scores', 'check_stage', 'finish', 'gate'), 'domains': (),
    },
    # The compatibility and manual entries run a delegate's adapter under their own identity and
    # options (none); the delegate's options are passed separately and checked as the delegate's.
    'dk-alternatives-review': {
        'skill': 'cf-workflows:dk-alternatives-review', 'adapter': 'issue-alternatives', 'saves': False,
        'role_prompt': 'Compatibility entry for dk-issue-alternatives and retained whole-problem managed '
                       'assignments.\n',
        'baseline_prompt': 'claude/skills/dk-alternatives-review/prompts/default.md',
        'options': {}, 'operations': ('route',), 'domains': (),
        'delegates': {'whole-problem': 'dk-issue-alternatives', 'branch': 'dk-pr-alternatives'},
    },
    'dk-pr-review-alternatives': {
        'skill': 'cf-workflows:dk-pr-review-alternatives', 'adapter': 'pr-alternatives', 'saves': False,
        'role_prompt': 'Manual entry point for alternatives-only PR review. Run and record a fresh review on demand, '
                       'including after an earlier completed run.\n',
        'baseline_prompt': 'claude/skills/dk-pr-review-alternatives/prompts/default.md',
        'options': {}, 'operations': ('direct_request',), 'domains': (),
        'delegates': {'branch': 'dk-pr-alternatives'},
    },
})


# Native controls (#26): the engine's optional agent-definition settings, chosen per entry run.
# Only the configuration entries pass them to their session; other entries reject them.
NATIVE_CONTROL_ENTRIES = ('dk-repo-onboard', 'dk-config', 'dk-preferences')
REASONING_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}')
# Providers that reject a definition's reasoning-effort (Agent Workflow docs/AGENT_TASKS.md).
NO_REASONING_EFFORT = ('claude-sdk',)


def check_controls(name, controls):
    """An entry run's native controls: a model and a reasoning effort, each optional and validated."""
    require(name in ENTRIES, 'entry', 'unknown entry')
    controls = {} if controls is None else controls
    require(isinstance(controls, dict), 'controls', 'must be an object')
    require(not controls or name in NATIVE_CONTROL_ENTRIES, 'controls',
            ENTRIES[name]['skill'] + ' takes no native controls')
    fields_ = set(controls) - {'model', 'reasoning_effort'}
    require(not fields_, 'controls', 'unsupported native control(s): ' + ', '.join(sorted(map(str, fields_))))
    if 'model' in controls:
        require(isinstance(controls['model'], str) and MODEL.fullmatch(controls['model']), 'controls.model',
                'must be a provider model identifier')
    if 'reasoning_effort' in controls:
        require(controls['reasoning_effort'] in REASONING_EFFORTS, 'controls.reasoning_effort',
                'must be one of ' + ', '.join(REASONING_EFFORTS))
    return dict(sorted(controls.items()))


# Entries whose own options may be saved per configuration scope (`entries`; see
# cf_workflows.configuration.entry_options). Saved values apply under invocation options.
SAVED_OPTIONS = ('dk-issue-alternatives', 'dk-pr-alternatives')


def preset(name, adapter=None):
    """The entry's selection for the shared plan: one adapter task and explicitly empty slots."""
    return {'tasks': [adapter or ENTRIES[name]['adapter']], 'finalizers': [], 'reviewers': []}


def _option(declared, value, path, rules, where):
    """One option checked against its declared default's shape; nested objects merge over the defaults."""
    if isinstance(declared, dict):
        require(isinstance(value, dict), path, 'must be an object')
        merged = copy.deepcopy(declared)
        for key, item in value.items():
            require(key in declared, path + '.' + str(key), 'unsupported option for ' + where)
            merged[key] = _option(declared[key], item, path + '.' + key, rules, where)
        return merged
    rule = rules.get(path.split('.', 1)[1])
    if rule is not None:
        require(value in rule and type(value) is type(declared), path, 'unsupported value')
    elif declared is None or isinstance(declared, str):
        # A nullable prompt takes text; only an option whose default is empty may be empty.
        require(isinstance(value, str) or (declared is None and value is None), path, 'must be text')
        if isinstance(value, str) and (declared != '' or value != ''):
            admission.text(value, path)
    else:
        require(type(value) is type(declared), path, 'must be ' + type(declared).__name__)
    return value


def _leaves(value, prefix):
    if not isinstance(value, dict):
        return [prefix]
    return [leaf for key, item in value.items() for leaf in _leaves(item, prefix + '.' + key)]


def _layer(declared, current, value, path, rules, where):
    """One option layer over the value below it; nested objects merge key by key."""
    if isinstance(declared, dict):
        require(isinstance(value, dict), path, 'must be an object')
        merged = copy.deepcopy(current)
        for key, item in value.items():
            require(key in declared, path + '.' + str(key), 'unsupported option for ' + where)
            merged[key] = _layer(declared[key], current[key], item, path + '.' + key, rules, where)
        return merged
    return _option(declared, value, path, rules, where)


def check_options(name, options, saved=()):
    """The entry's own options (its baseline schema) with provenance; unknown options reject.

    saved holds the applicable saved layers of an entry in SAVED_OPTIONS as (source, path,
    options), lowest precedence first (cf_workflows.configuration.entry_options); invocation
    options apply over them. A nested option merges key by key; when more than one layer set
    parts of it, `sources` names the layer of each part that was set.
    """
    require(name in ENTRIES, 'entry', 'unknown entry')
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    require(not saved or name in SAVED_OPTIONS, 'configuration', ENTRIES[name]['skill'] + ' has no saved options')
    declared = ENTRIES[name]['options']
    result = {key: {'value': copy.deepcopy(value), 'source': 'default'} for key, value in declared.items()}
    parts = {key: {} for key in declared}
    for source, where, layer in [*saved, ('invocation', 'options', options)]:
        try:
            require(isinstance(layer, dict), 'options', 'must be an object')
            for key, value in layer.items():
                require(key in declared, 'options.' + str(key), 'unsupported option for ' + ENTRIES[name]['skill'])
                if key == 'default_applies_to':
                    require(isinstance(value, list) and value and len(set(map(str, value))) == len(value)
                            and all(mode in admission.PREFERENCE_MODES for mode in value),
                            'options.default_applies_to', 'must be distinct implementation/review modes')
                    value = [mode for mode in admission.PREFERENCE_MODES if mode in value]
                else:
                    value = _layer(declared[key], result[key]['value'], value, 'options.' + key,
                                   ENTRIES[name].get('rules', {}), ENTRIES[name]['skill'])
                leaves = _leaves(layer[key], key)
                # An empty nested object sets nothing, so it does not take over the option's source.
                result[key] = {'value': value, 'source': source if leaves else result[key]['source']}
                parts[key].update({leaf: source for leaf in leaves})
        except AdmissionError as error:
            raise AdmissionError(where + error.path[len('options'):], error.message) from None
    for key, leaves in parts.items():
        if len(set(leaves.values())) > 1:
            result[key]['sources'] = dict(sorted(leaves.items()))
    return result


def _rejected(path, message, **extra):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}, **extra}


def preflight(name, *, request, deployment, repository, transport, invocation=None, options=None, roots=None,
              install=dk_pr_fix.INSTALL, ledger=None, adapter=None, controls=None, saved=()):
    """Admit one run of an entry through the shared admission pipeline.

    invocation may carry only `prompts`: the task, finalizer and reviewer selection
    is the entry's preset. controls carries the configuration entries' native
    model and reasoning-effort choices for their session. Writes nothing; transport sees the same operations as
    semantic.admit (only capabilities() for the default plan).
    """
    try:
        entry_options = check_options(name, options, saved)
        native = check_controls(name, controls)
        require(isinstance(request, dict), 'request', 'must be an object')
        require(not request.get('authorizations'), 'request.authorizations',
                'supporting entries perform no remote write; a permission is a saved setting, not an authorization')
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
    # A compatibility or manual entry runs its delegate's adapter under its own identity.
    selected = adapter or ENTRIES[name]['adapter']
    delegates = {ENTRIES[delegate]['adapter'] for delegate in ENTRIES[name].get('delegates', {}).values()}
    if selected not in {ENTRIES[name]['adapter']} | delegates:
        return _rejected('adapter', 'not an adapter of ' + ENTRIES[name]['skill'])
    merged = {'options': preset(name, selected), 'prompts': copy.deepcopy(invocation.get('prompts', []))}
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
    assert task['adapter'] == selected and [stage['kind'] for stage in snapshot['plan']['stages']] == ['tasks']
    assert not any(value['effective'] for value in snapshot['permissions'].values())
    if 'reasoning_effort' in native:
        # The entry session runs on the same agent as the shared validator (configuration.assignment).
        _, provider = semantic.validator_agent(result)
        if provider in NO_REASONING_EFFORT:
            return {**_rejected('controls.reasoning_effort', provider + ' rejects a definition reasoning '
                                'effort; omit it for this provider'), 'entry': result['entry']}
    result['entry'].update({
        'adapter': selected, 'task': task, 'definition': snapshot['adapters'][selected],
        'role_prompt': entry['role_prompt'], 'operations': list(entry['operations']),
        'domains': list(entry['domains']), 'saves': entry['saves'], 'controls': native,
        # Applicable custom prompts reach the session after semantic acceptance, apart from the ask.
        'prompts': [{key: item[key] for key in ('id', 'slot', 'target', 'text')}
                    for item in snapshot['prompts'] if item['applicable']],
    })
    return result
