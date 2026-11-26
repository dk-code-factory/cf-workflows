"""cf-workflows:dk-ci: exact-head CI observation as a thin preset over the shared workflow (#35).

The entry adds no orchestration. Its preset is one `{stage: ci}` task stage of
the installed dk-pr-fix workflow (main.yml, fix mode) with no finalizers or
reviewers and a zero repair allowance, so it observes only: ci-poll.yml calls
the installed `ci` helper operation (prfix/ci.py) until checks settle, and no
repair session exists. It polls at the configured interval, bounds each failed
attempt's classification and reports `no_checks` once an absent-check grace
ends; each window is retained in the ledger. It shares the PR's default
logical run with dk-pr-fix, so a direct and a composed observation share one
retained absolute deadline, one pending-only extension and the per-head rerun
allowance; neither resumes nor a new head renews them. No skill is loaded.

Only current success, verified issue-backed flakes (`known_flakes`) and
verified configured exceptions (`accepted_exceptions`) satisfy CI. Unavailable
or unaffirmed evidence leaves a check failed. Reruns publish through
cf-workflows:dk-pr-publish (`rerun_ci`); this entry takes no authorization.

The original role prompt and options come from the pinned dk-workflows
baseline (MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy

from . import admission, configuration, dk_pr_fix
from .admission import AdmissionError, require

NAME = 'dk-ci'
ENTRY = {
    'skill': 'cf-workflows:dk-ci',
    'role_prompt': 'Check CI for the current PR commit, preserve wait deadlines and classify failures using verified '
                   'flaky tests or exceptions.\n',
    'baseline_prompt': 'claude/skills/dk-ci/prompts/default.md',
    # Supported baseline options (claude/skills/dk-ci/config.json) and their defaults.
    'options': {'timeout_seconds': 1800, 'poll_interval_seconds': 30, 'classification_timeout_seconds': 60,
                'no_checks_grace_seconds': 60, 'pending_extension_seconds': 1800, 'max_pending_extensions': 1},
    'operations': ('ci', 'coverage', 'completion'),
}
PRESET = {'tasks': [{'stage': 'ci'}], 'finalizers': [], 'reviewers': []}
BOUNDS = {'timeout_seconds': (1, 86400), 'pending_extension_seconds': (0, 86400), 'max_pending_extensions': (0, 1),
          **admission.CI_WINDOWS}


def check_options(options):
    """The entry's own baseline options with provenance; unsupported or invalid values reject."""
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    result = {key: {'value': value, 'source': 'default'} for key, value in ENTRY['options'].items()}
    for key, value in options.items():
        path = 'options.' + str(key)
        require(key in ENTRY['options'], path, 'unsupported option for ' + ENTRY['skill'])
        low, high = BOUNDS[key]
        require(type(value) is int and low <= value <= high, path, f'must be an integer in {low}..{high}')
        result[key] = {'value': value, 'source': 'invocation'}
    return result


def configured(request, document):
    """Effective configured ci and limits options for the request's PR; None when admission must report."""
    try:
        original = admission.resolve_request(request)
        where = {'repository': original['pr']['repository'], 'pr': original['pr']['url'],
                 'upstream': original['upstream']}
    except (AdmissionError, TypeError, KeyError, AttributeError, ValueError):
        return None
    current = configuration.view(document, where)
    if not current['valid']:
        return None
    return {key: current['values']['options.' + key] for key in ('ci', 'limits')}


def _rejected(path, message):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}, 'entry': {'name': NAME, 'skill': ENTRY['skill']}}


def preflight(*, request, deployment, repository, transport, configuration=None, invocation=None, options=None,
              roots=None, home=None, install=dk_pr_fix.INSTALL):
    """Admit one direct observation and return the installed workflow's inputs; writes nothing.

    The preset is applied at invocation precedence. Configured CI controls and
    limits stay in force: an option given at invocation sets only its own CI
    control, a default applies only where nothing is configured, and the
    repair allowance is zero.
    """
    try:
        entry_options = check_options(options)
        require(isinstance(request, dict), 'request', 'must be an object')
        require(not request.get('authorizations'), 'request.authorizations',
                'cf-workflows:dk-ci observes only; reruns publish through cf-workflows:dk-pr-publish (rerun_ci)')
        require(invocation is None or invocation == {}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers, limits and permissions')
    except AdmissionError as error:
        return _rejected(error.path, error.message)
    current = configured(request, configuration)
    ci, limits = {}, None
    if current is not None:
        ci = copy.deepcopy(current['ci']['value'])
        limits = {**current['limits']['value'], 'ci_repairs_per_cycle': 0}
    for key, value in entry_options.items():
        if value['source'] == 'invocation' or key not in ci:
            ci[key] = value['value']
    merged = {'options': {**copy.deepcopy(PRESET), 'ci': ci,
                          'limits': limits or {**admission.DEFAULTS['limits'], 'ci_repairs_per_cycle': 0}}}
    result = dk_pr_fix.preflight(request=request, configuration=configuration, invocation=merged,
                                 deployment=deployment, repository=repository, transport=transport, roots=roots,
                                 home=home, install=install, workflow=ENTRY['skill'], entry=dk_pr_fix.ENTRY,
                                 custom=False)
    result['entry'] = {'name': NAME, 'skill': ENTRY['skill'], 'preset': 'observe', 'mode': 'fix',
                       'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    plan = result['snapshot']['plan']
    if ([(stage['kind'], stage['phase']) for stage in plan['stages']] != [('ci', 'tasks')] or plan['tasks']
            or result['inputs']['limits']['ci_repairs_per_cycle'] != 0):
        return {**dk_pr_fix._reject(result, 'plan', ENTRY['skill'] + ' runs exactly its observation preset'),
                'entry': result['entry']}
    result['entry'].update({'role_prompt': ENTRY['role_prompt'], 'operations': list(ENTRY['operations']),
                            'adapters': [], 'definitions': []})
    return result
