"""Author-aware routing for cf-workflows:dk-pr-iter.

The PR author and the authenticated actor are compared by immutable numeric
account IDs on the PR's host, together with the configured personal accounts
(admission option `accounts`, `{host, id}` records). Logins, remotes, commit
authorship and write access never decide ownership, so a renamed login keeps
its route and a reused login never inherits one. Missing or inconsistent
identity evidence blocks routing instead of guessing.

The routed request keeps the exact original instructions and authorizations.
Routing grants nothing: an analysis-only request selects read-only incoming
review and prohibits every remote write, and an explicit request to review your
own PR never permits approving it. route() performs only two read-only
GitHub reads on the supplied transport; nothing is staged, run or published.

preflight() is the installed entry (workflows/iter.yml): it routes, admits the
routed request through the selected entry's preflight and returns iter.yml's
inputs, including the route and the two IDs it compared. The installed
`iter_route` helper (prfix/iteration.py) checks them again before either entry runs.
"""
from . import admission, dk_pr_fix, dk_pr_review

ROUTES = {'fix': 'cf-workflows:dk-pr-fix', 'review': 'cf-workflows:dk-pr-review'}
WORKFLOW = 'cf-workflows:dk-pr-iter'
ENTRY = 'workflows/iter.yml'


class RoutingBlocked(ValueError):
    """Routing cannot be decided from the available evidence or request."""


def _require(condition, message):
    if not condition:
        raise RoutingBlocked(message)


def _account(value, label):
    _require(isinstance(value, dict) and type(value.get('id')) is int and value['id'] > 0
             and isinstance(value.get('login'), str) and value['login'].strip(),
             label + ' identity is missing an immutable numeric ID or login')
    return {'id': value['id'], 'login': value['login']}


def _explicit(value):
    if value is None:
        return None
    _require(isinstance(value, dict) and set(value) == {'route', 'request_id', 'instruction', 'source'},
             'an explicit route needs exactly route, request_id, instruction and source')
    _require(value['route'] in ROUTES, 'an explicit route is fix or review')
    _require(all(isinstance(value[key], str) and value[key].strip() for key in ('request_id', 'instruction', 'source')),
             'an explicit route needs its exact request identity')
    return value


def check_accounts(value):
    """Validate configured personal accounts: canonical host and positive numeric ID, never a login."""
    try:
        admission.check_option('accounts', value, 'accounts', {})
    except admission.AdmissionError as error:
        raise RoutingBlocked(str(error)) from None
    return value


def route(*, request, accounts=(), transport, explicit=None, analysis_only=False):
    """Select the fix or incoming-review entry point for one PR; read-only.

    request is the private request (`pr`, `instructions`, optional `logical_run`,
    `authorizations`, `upstream`). transport must provide `pull_request(pr)` and
    `viewer(host)`; no other operation is called. explicit is a direct user
    request to fix or review ({route, request_id, instruction, source}).
    Returns a routed record, or {'status': 'blocked', 'reason'} without a route.
    """
    try:
        return _route(request, accounts, transport, explicit, analysis_only)
    except (RoutingBlocked, admission.AdmissionError) as error:
        return {'status': 'blocked', 'route': None, 'skill': None, 'reason': str(error)}


def invocation(routed, base=None):
    """The routed invocation scope: base options plus the prohibitions routing added.

    Prohibitions accumulate in admission, so a narrower routed request is never
    weakened by the caller's own invocation scope.
    """
    scope = {key: value for key, value in (base or {}).items()}
    added = ((routed.get('invocation') or {}).get('options') or {}).get('prohibitions', [])
    if added:
        options = dict(scope.get('options', {}))
        options['prohibitions'] = list(options.get('prohibitions', [])) + [item for item in added
                                                                          if item not in options.get('prohibitions', [])]
        scope['options'] = options
    return scope or None


invocation_scope = invocation


def _route(request, configured, transport, explicit, analysis_only):
    _require(isinstance(request, dict) and type(analysis_only) is bool, 'malformed routing request')
    original = admission.resolve_request(request)
    pr = original['pr']
    host = pr['repository'].split('/')[0]
    configured = check_accounts(list(configured) if isinstance(configured, (list, tuple)) else configured)
    explicit = _explicit(explicit)
    try:
        pull = transport.pull_request(pr['url'])
        viewer = transport.viewer(host)
    except RoutingBlocked:
        raise
    except Exception as error:  # Any read failure; never a guessed route.
        raise RoutingBlocked('PR author or authenticated account identity is unavailable: '
                             + type(error).__name__) from None
    _require(isinstance(pull, dict) and isinstance(pull.get('html_url'), str)
             and pull.get('number') == pr['number'], 'PR identity is unavailable or inconsistent')
    try:
        same = admission.pr_identity(pull['html_url'], 'pull.html_url')['url'] == pr['url']
    except admission.AdmissionError:
        same = False
    _require(same, 'PR identity is unavailable or inconsistent')
    author = _account(pull.get('user'), 'PR author')
    actor = _account(viewer, 'authenticated account')
    matched = 'actor' if author['id'] == actor['id'] else None
    for item in configured:
        if matched is None and item['host'] == host and item['id'] == author['id']:
            matched = 'account'
    ownership = 'mine' if matched else 'others'

    authorizations = dict(original['authorizations'])
    prohibitions, withheld = [], []
    if analysis_only:
        _require(explicit is None or explicit['route'] == 'review',
                 'an analysis-only request cannot also ask to fix the PR; resolve the request first')
        selected, basis = 'review', 'analysis_only'
        # The narrower request wins over every saved or requested write.
        withheld = sorted(authorizations)
        authorizations = {}
        prohibitions = list(admission.ACTIONS)
    elif explicit is not None:
        selected, basis = explicit['route'], 'explicit'
    else:
        selected, basis = ('fix' if ownership == 'mine' else 'review'), 'identity'
    if selected == 'review' and ownership == 'mine' and 'approve_pr' not in prohibitions:
        # Reviewing your own PR never approves it.
        _require('approve_pr' not in authorizations, 'the authenticated account never approves its own PR')
        prohibitions.append('approve_pr')
    maintain_other = None
    if selected == 'fix' and ownership == 'others':
        # Only a direct user request maintains another author's PR; it grants no remote write.
        maintain_other = {key: explicit[key] for key in ('request_id', 'instruction', 'source')}
    routed = {key: request[key] for key in ('pr', 'instructions', 'logical_run', 'upstream') if key in request}
    if authorizations:
        routed['authorizations'] = authorizations
    return {'status': 'routed', 'route': selected, 'skill': ROUTES[selected], 'basis': basis,
            'pr': pr['url'], 'host': host, 'author': author, 'actor': actor, 'ownership': ownership,
            'identity': {'matched': matched, 'compared': 'numeric_id'},
            'request': routed, 'invocation': {'options': {'prohibitions': prohibitions}} if prohibitions else None,
            'maintain_other': maintain_other, 'withheld': withheld}


def _owned(routed, accounts):
    author = routed['author']['id']
    return author == routed['actor']['id'] or any(
        item['host'] == routed['host'] and item['id'] == author for item in accounts)


def preflight(*, request, github, transport, deployment, repository, accounts=(), explicit=None,
              analysis_only=False, configuration=None, invocation=None, roots=None, home=None,
              install=dk_pr_fix.INSTALL, ledger=None, store=None):
    """Route one PR and admit it on the selected entry; returns workflows/iter.yml's inputs.

    github provides the two routing reads (see route()); transport is the engine
    transport of the selected entry's preflight. accounts are the personal
    accounts routing compares; they must give the same ownership as the admitted
    `accounts` option, which the installed helper uses. The routed request,
    its added prohibitions and an explicit maintain_other request pass to
    dk_pr_fix.preflight or dk_pr_review.preflight unchanged. Writes nothing
    outside their ledger and configuration store.
    """
    routed = route(request=request, accounts=accounts, transport=github, explicit=explicit,
                   analysis_only=analysis_only)
    if routed['status'] != 'routed':
        return {'preflight': 'blocked', 'structural': None, 'semantic': None, 'routing': routed,
                'error': {'path': 'routing', 'message': routed['reason']}}
    arguments = {'request': routed['request'], 'configuration': configuration,
                 'invocation': invocation_scope(routed, invocation), 'deployment': deployment,
                 'repository': repository, 'transport': transport, 'roots': roots, 'home': home,
                 'install': install, 'ledger': ledger, 'store': store}
    if routed['route'] == 'fix':
        result = dk_pr_fix.preflight(maintain_other=routed['maintain_other'], **arguments)
    else:
        result = dk_pr_review.preflight(**arguments)
    result = {**result, 'routing': routed}
    if result['preflight'] != 'admitted':
        return result
    if _owned(routed, result['inputs']['accounts']) != (routed['ownership'] == 'mine'):
        # Nothing is staged; an admitted configuration write-back stays recorded.
        rejected = dk_pr_fix._reject(result, 'accounts', 'routing compared other personal accounts than the '
                                     'admitted accounts option; route with the admitted accounts')
        return {**rejected, 'routing': routed,
                **{key: result[key] for key in ('configuration_write',) if key in result}}
    inputs = {**result['inputs'], 'route': routed['route'],
              'identity': {'author': routed['author']['id'], 'actor': routed['actor']['id']}}
    return {**result, 'workflow': WORKFLOW, 'entry_workflow': result['workflow'], 'inputs': inputs,
            'installation': {**result['installation'], 'entry': ENTRY}}
