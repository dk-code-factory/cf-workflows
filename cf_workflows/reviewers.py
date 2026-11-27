"""Reviewer coordination and simplification entries over the shared review workflow (#29).

cf-workflows:dk-pr-reviewers-run and cf-workflows:dk-code-simplify add no
orchestration. Each is a preset over the installed workflow in review mode
(workflows/reviewers-run.yml and workflows/code-simplify.yml, each calling
main.yml with its fixed stage): one reviewer stage, no finalizers, the admitted
`reviewer` role for every selected reviewer and the separate `verifier` session
the plan always adds. The installed `prepare` helper rejects any other shape for
either entry. The entry's own options are the
baseline options, checked here with provenance and mapped onto that preset:

- dk-pr-reviewers-run: `reviewers` (ordered selections with explicit parallel
  groups, no default), `execution` and `max_parallel`. Scheduling becomes the
  `max_parallel_reviewers` limit, which caps a group's concurrency and never
  removes or merges an occurrence.
- dk-code-simplify: `priority` and `allow_untouched_code` select one bundled
  simplification reviewer (admission.SIMPLIFIERS).

Both entries are read-only: a request authorization or a permission rejects,
and their results never approve, ready, publish or merge. The default logical
run is incoming review's, so standalone and composed review share occurrence
identities, coverage and retained budgets. Nothing here starts a session.

Original role prompts come from the pinned dk-workflows baseline (MIT; see
licenses/dk-workflows-MIT.txt).
"""
import copy

from . import admission, configuration, dk_pr_fix, dk_pr_review
from .admission import AdmissionError, require

ENTRIES = {
    'dk-pr-reviewers-run': {
        'skill': 'cf-workflows:dk-pr-reviewers-run', 'entry': 'workflows/reviewers-run.yml',
        'role_prompt': 'Run selected PR reviewers on pinned inputs and reuse complete results when inputs are '
                       'unchanged.\n',
        'baseline_prompt': 'claude/skills/dk-pr-reviewers-run/prompts/default.md',
        'options': {'reviewers': [], 'execution': 'parallel', 'max_parallel': None},
    },
    'dk-code-simplify': {
        'skill': 'cf-workflows:dk-code-simplify', 'entry': 'workflows/code-simplify.yml',
        'role_prompt': 'Look for avoidable nesting, redundant conditions or data transformations, confusing names, '
                       'duplicated reasoning and abstractions that add more indirection than they remove. Explain the '
                       'concrete reading, debugging or maintenance burden reduced. Retain useful abstractions, explicit '
                       'control flow and separation of concerns. Fewer lines or personal style alone do not establish '
                       'simplicity.\n',
        'baseline_prompt': 'claude/skills/dk-code-simplify/prompts/default.md',
        'options': {'priority': 'change-first', 'allow_untouched_code': False},
    },
}
# The installed workflow's own inputs; its stage, finalizers and reviewer list are fixed in its job.
SHARED_INPUTS = ('request', 'adapters', 'limits', 'permissions', 'ci', 'merge_method', 'accounts', 'settings',
                 'resources', 'turns')
INPUTS = {'dk-pr-reviewers-run': SHARED_INPUTS + ('reviewers',), 'dk-code-simplify': SHARED_INPUTS + ('simplifier',)}
# The helper operations each reviewer occurrence and the verifier run through (prfix.app).
OPERATIONS = ('prepare', 'claim', 'accept', 'turn_result', 'coverage')
# Selection keys with no supported meaning here. Per-occurrence provider options and native model
# controls need engine support; the admitted role definition carries model and reasoning effort.
UNSUPPORTED_CONTROLS = ('options', 'agent', 'model', 'reasoning_effort', 'reasoning-effort', 'execution',
                        'max_parallel')
MAX_PARALLEL = admission.LIMIT_BOUNDS['max_parallel_reviewers'][1]


def simplifier(priority, allow_untouched_code):
    """The bundled simplification reviewer for dk-code-simplify's options."""
    return {('change-first', False): admission.SIMPLIFIER, ('code-first', False): 'simplify-code-first',
            ('change-first', True): 'simplify-related-code',
            ('code-first', True): 'simplify-code-first-related-code'}[(priority, allow_untouched_code)]


def _reviewers(value, path):
    require(isinstance(value, list), path, 'must be an ordered array of reviewers and parallel groups')
    for index, entry in enumerate(value):
        group = entry if isinstance(entry, list) else [entry]
        for number, call in enumerate(group):
            if isinstance(call, dict):
                where = f'{path}[{index}]' + (f'[{number}]' if isinstance(entry, list) else '')
                for key in UNSUPPORTED_CONTROLS:
                    require(key not in call, f'{where}.{key}',
                            'unsupported reviewer control: per-occurrence provider options and native model controls '
                            'need engine support; the admitted reviewer role carries model and reasoning effort')
    return copy.deepcopy(value)


def check_options(name, options):
    """The entry's own baseline options with provenance; unknown or invalid options reject."""
    require(name in ENTRIES, 'entry', 'unknown reviewer entry')
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    declared = ENTRIES[name]['options']
    result = {key: {'value': copy.deepcopy(value), 'source': 'default'} for key, value in declared.items()}
    for key, value in options.items():
        path = 'options.' + str(key)
        require(key in declared, path, 'unsupported option for ' + ENTRIES[name]['skill'])
        if key == 'reviewers':
            value = _reviewers(value, path)
        elif key == 'execution':
            require(value in ('parallel', 'sequential'), path, 'must be parallel or sequential')
        elif key == 'max_parallel':
            require(value is None or type(value) is int and 1 <= value <= MAX_PARALLEL, path,
                    f'must be null or an integer in 1..{MAX_PARALLEL}')
        elif key == 'priority':
            require(value in ('change-first', 'code-first'), path, 'must be change-first or code-first')
        elif key == 'allow_untouched_code':
            require(type(value) is bool, path, 'must be boolean')
        result[key] = {'value': value, 'source': 'invocation'}
    if name == 'dk-pr-reviewers-run':
        require(not (result['execution']['value'] == 'sequential' and (result['max_parallel']['value'] or 1) != 1),
                'options.max_parallel', 'sequential execution launches one reviewer at a time')
    return result


def _configured_limits(request, configuration_document):
    """Effective configured limits for the request's target; None when admission must report an error."""
    try:
        original = admission.resolve_request(request)
        where = {'repository': original['pr']['repository'], 'pr': original['pr']['url'],
                 'upstream': original['upstream']}
    except (AdmissionError, TypeError, KeyError, AttributeError, ValueError):
        return None
    current = configuration.view(configuration_document, where)
    return current['values']['options.limits']['value'] if current['valid'] else None


def _rejected(path, message, entry=None):
    result = {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
              'error': {'path': path, 'message': message}}
    if entry:
        result['entry'] = entry
    return result


def preflight(name, *, request, configuration=None, invocation=None, deployment, repository, transport,
              options=None, roots=None, home=None, install=dk_pr_fix.INSTALL):
    """Admit one run of dk-pr-reviewers-run or dk-code-simplify; writes nothing.

    invocation is the caller's invocation scope; it may not select tasks,
    finalizers or reviewers (the preset and the entry options do) or enable a
    permission. Returns dk_pr_fix.preflight's result for the entry's installed
    workflow, whose `inputs` are that workflow's declared inputs, plus `entry`:
    the skill, its options with provenance, its role prompt and the adapter
    procedure that runs.
    """
    try:
        entry_options = check_options(name, options)
        require(isinstance(request, dict), 'request', 'must be an object')
        require(not request.get('authorizations'), 'request.authorizations',
                ENTRIES[name]['skill'] + ' is read-only review; it has no publication, approval or merge authority')
        scope = copy.deepcopy({} if invocation is None else invocation)
        require(isinstance(scope, dict), 'invocation', 'must be an object')
        require(not scope.get('permissions'), 'invocation.permissions', 'read-only entry; no permission applies')
        selected = scope.setdefault('options', {})
        require(isinstance(selected, dict), 'invocation.options', 'must be an object')
        for key in ('tasks', 'finalizers', 'reviewers'):
            require(key not in selected, 'invocation.options.' + key,
                    'the entry preset fixes tasks and finalizers; select reviewers with the entry options')
        require(isinstance(selected.get('limits', {}), dict), 'invocation.options.limits', 'must be an object')
    except AdmissionError as error:
        return _rejected(error.path, error.message)
    values = {key: value['value'] for key, value in entry_options.items()}
    if name == 'dk-code-simplify':
        reviewers = [simplifier(values['priority'], values['allow_untouched_code'])]
        selected['tasks'] = [{'stage': 'reviews', 'reviewers': reviewers}]
    else:
        selected['tasks'] = [{'stage': 'reviews'}]
        if entry_options['reviewers']['source'] == 'invocation':
            selected['reviewers'] = values['reviewers']
        if 'invocation' in (entry_options['execution']['source'], entry_options['max_parallel']['source']):
            # Invocation-local scheduling overrides only this limit; the rest stay as configured.
            cap = 1 if values['execution'] == 'sequential' else values['max_parallel'] or 0
            base = _configured_limits(request, configuration) or {}
            selected['limits'] = {**base, **selected.get('limits', {}), 'max_parallel_reviewers': cap}
    selected['finalizers'] = []
    if 'logical_run' not in request:
        try:
            # Standalone and composed incoming review share occurrence identities and coverage.
            request = {**request, 'logical_run': dk_pr_review.logical_run(request.get('pr'))}
        except AdmissionError:
            pass  # Admission reports the malformed PR identity.
    result = dk_pr_fix.preflight(request=request, configuration=configuration, invocation=scope,
                                 deployment=deployment, repository=repository, transport=transport, roots=roots,
                                 home=home, install=install, mode='review',
                                 defaults={'tasks': [{'stage': 'reviews'}], 'finalizers': [], 'reviewers': []},
                                 workflow=ENTRIES[name]['skill'], entry=ENTRIES[name]['entry'], custom=False)
    entry = {'name': name, 'skill': ENTRIES[name]['skill'], 'options': entry_options,
             'role_prompt': ENTRIES[name]['role_prompt']}
    result['entry'] = entry
    if result['preflight'] != 'admitted':
        return result
    snapshot = result['snapshot']
    assert [stage['kind'] for stage in snapshot['plan']['stages']] == ['reviews']
    assert all(task['kind'] == 'review' for task in snapshot['plan']['tasks'])
    assert not any(value['effective'] for value in snapshot['permissions'].values())
    [stage] = snapshot['plan']['stages']
    inputs = result['inputs']
    if name == 'dk-code-simplify':
        assert inputs['tasks'] == selected['tasks'] and inputs['finalizers'] == []
        inputs['simplifier'] = selected['tasks'][0]['reviewers'][0]
    else:
        assert inputs['tasks'] == [{'stage': 'reviews'}] and inputs['finalizers'] == []
    result['inputs'] = {key: inputs[key] for key in INPUTS[name]}
    entry.update({
        'reviewers': [[next(task['adapter'] for task in snapshot['plan']['tasks'] if task['id'] == key)
                       for key in group] for group in stage['groups']],
        'procedure': {'reviewer_role': 'reviewer', 'verifier_role': 'verifier', 'skills': [],
                      'operations': list(OPERATIONS)},
        'max_parallel_reviewers': snapshot['limits']['max_parallel_reviewers'],
        'authority': 'none',
    })
    return result


def result(name, review, candidates, reports):
    """The entry's observable result from the shared review record; grants nothing.

    review is the coverage `review` record of the installed helpers, candidates
    their original findings (each with its `task`) and reports the original
    accepted report of each currently covered occurrence, by occurrence ID.
    """
    require(name in ENTRIES, 'entry', 'unknown reviewer entry')
    status = {}
    for key in ('confirmed', 'rejected', 'unverified'):
        for finding in review['findings'][key]:
            status[finding] = key
    occurrences = [{**item, 'report': copy.deepcopy(reports.get(item['id']))} for item in review['occurrences']]
    common = {'required_missing': list(review['required_missing']),
              'optional_omitted': copy.deepcopy(review['optional_omitted']),
              'occurrences': occurrences, 'approve': False, 'ready': False, 'publication': False,
              'authority': 'none'}
    if name == 'dk-pr-reviewers-run':
        findings = {key: [] for key in ('confirmed', 'rejected', 'unverified')}
        for finding in candidates:
            if finding['id'] in status:
                findings[status[finding['id']]].append(
                    {key: finding[key] for key in ('id', 'original_id', 'task', 'text') if key in finding})
        return {'outcome': review['outcome'], 'complete': review['complete'], 'clean_review': review['clean_review'],
                'findings': findings, **common}
    simplifiers = {item['id'] for item in review['occurrences'] if item['role'] == 'simplification'}
    recommendations = {key: [] for key in ('confirmed', 'rejected', 'unverified')}
    for finding in candidates:
        if finding['task'] in simplifiers and finding['id'] in status:
            recommendations[status[finding['id']]].append(
                {key: finding[key] for key in ('id', 'original_id', 'task', 'text', 'location') if key in finding})
    deferred = [item for key in sorted(simplifiers) if reports.get(key)
                for item in (reports[key].get('deliverables') or {}).get('deferred_observations', [])]
    return {'status': 'complete' if review['complete'] else 'incomplete',
            'recommendations': recommendations['confirmed'], 'rejected': recommendations['rejected'],
            'unverified': recommendations['unverified'], 'deferred_observations': copy.deepcopy(deferred),
            # Simplification never substitutes for ordinary review coverage.
            'clean_review': False, 'replaces_review': False, **common}
