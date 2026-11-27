"""Publication, review-posting and fork-CI approval entries as thin presets (contract v1).

cf-workflows:dk-pr-publish, cf-workflows:dk-pr-review-post and
cf-workflows:dk-ci-approve add no orchestration and no second publication journal. Each is a preset selection for the installed
workflow, the admitted adapter procedure that selection runs (roles, assignments
and the helper operations in the installed prfix/publishing.py, findings.py and
publication.py) and the entry's original options. Preflight is the shared
dk-pr-fix or dk-pr-review preflight. No skill is loaded.

- dk-pr-publish, changes preset (publish.yml running main.yml in fix mode):
  the bundled ``prepare-publication`` leaf session turns the exact request
  into proposed operations, then the shared publication stage prepares exact
  intents, obtains an independent audience verdict, rechecks live state and
  reconciles uncertain effects. It shares the PR's default logical run with
  dk-pr-fix.
- dk-pr-publish, merge preset (publish.yml running main.yml in review mode):
  merge needs current independent review, so a merge request runs the
  incoming-review preset and the shared merge readiness (current head, CI,
  pending reviews and requests). It shares the PR's incoming-review logical
  run, so current review is reused.
  The standalone-to-merge bridge (#35, main.yml merge_ci): after the review
  completes, the merge preset first runs any separately authorized fork
  workflow approvals (an ``approve_fork_ci`` grant beside ``merge``, with its
  own permission), then waits for exact-head CI within the retained window and
  checks readiness again before the merge.
- dk-pr-review-post (review-post.yml running main.yml in review mode):
  verified findings inline or in the body, and a separately authorized
  approval of a clean review.
- dk-ci-approve (ci-approve.yml running main.yml in review mode, #35): one exact fork workflow
  approval per eligible run attempt of the reviewed head, after complete
  current incoming review (main.yml fork_ci job, publishing.fork_ci). Finding
  publication, PR approval and merge authority never imply it.

Every public action has one declared capability below. Undeclared actions, and
actions owned by another entry or issue, reject before any effect. A configured
permission never supplies a missing authorization. The installed helpers repeat
the routing of dk-pr-publish, dk-pr-review-post and dk-ci-approve inside the package
(prfix/prepare.py ENTRY_ACTIONS, the entry_preset operation and prepare's
`entry` control), so a direct run of their installed workflow rejects the same
actions before any session or effect.

Original role prompts and options come from the pinned dk-workflows baseline
(MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy

from . import admission, dk_pr_fix, dk_pr_review
from .admission import AdmissionError, require

CHANGES = {'tasks': ['prepare-publication', {'stage': 'publication'}], 'finalizers': [], 'reviewers': []}
REVIEW = copy.deepcopy(dk_pr_review.DEFAULTS)
# Installed entry workflows (plugin.yaml). publish.yml routes its two presets; the merge
# path takes dk-pr-review's selections under the review_ input names.
PUBLISH = 'workflows/publish.yml'
REVIEW_POST = 'workflows/review-post.yml'
CI_APPROVE = 'workflows/ci-approve.yml'
MERGE_INPUTS = {'tasks': 'review_tasks', 'reviewers': 'review_reviewers', 'adapters': 'review_adapters'}
PRESETS = {
    'changes': {'mode': 'fix', 'workflow': PUBLISH, 'options': CHANGES},
    'merge': {'mode': 'review', 'workflow': PUBLISH, 'options': REVIEW},
    'review-post': {'mode': 'review', 'workflow': REVIEW_POST, 'options': REVIEW},
    'fork-ci': {'mode': 'review', 'workflow': CI_APPROVE, 'options': REVIEW},
}
# Every public action: its entry and preset, the publication kinds it dispatches and its workflow input.
CAPABILITIES = {
    'push': {'entry': 'dk-pr-publish', 'preset': 'changes', 'kinds': ('push',), 'permission': 'push_commits'},
    'edit_pr': {'entry': 'dk-pr-publish', 'preset': 'changes', 'kinds': ('edit_pr',), 'permission': 'edit_pr'},
    'reply': {'entry': 'dk-pr-publish', 'preset': 'changes', 'kinds': ('reply', 'reply_inline'),
              'permission': 'reply_to_feedback'},
    'resolve_thread': {'entry': 'dk-pr-publish', 'preset': 'changes', 'kinds': ('resolve_thread',),
                       'permission': 'resolve_threads'},
    'request_review': {'entry': 'dk-pr-publish', 'preset': 'changes', 'kinds': ('request_review',),
                       'permission': 'request_review'},
    'create_pr': {'entry': 'dk-pr-publish', 'preset': 'changes', 'kinds': ('split_push', 'split_pr'),
                  'permission': 'create_draft_prs'},
    'rerun_ci': {'entry': 'dk-pr-publish', 'preset': 'changes', 'kinds': ('ci_rerun',), 'permission': 'rerun_ci'},
    'merge': {'entry': 'dk-pr-publish', 'preset': 'merge', 'kinds': ('merge',), 'permission': 'merge'},
    'post_findings': {'entry': 'dk-pr-review-post', 'preset': 'review-post', 'kinds': ('submit_review',),
                      'permission': 'post_findings'},
    'approve_pr': {'entry': 'dk-pr-review-post', 'preset': 'review-post', 'kinds': ('approve_pr',),
                   'permission': 'approve_pr'},
    'approve_fork_ci': {'entry': 'dk-ci-approve', 'preset': 'fork-ci', 'kinds': ('approve_fork_ci',),
                        'permission': 'approve_fork_ci'},
}
# Exact baseline role prompts (prompts/default.md at the pinned commit; digests in baselines/inventory.json).
ENTRIES = {
    'dk-pr-publish': {
        'skill': 'cf-workflows:dk-pr-publish',
        'role_prompt': 'Publish prepared PR changes, replies, reviews and authorized merges with current checks '
                       'and recovery records.\n',
        'baseline_prompt': 'claude/skills/dk-pr-publish/prompts/default.md',
        'options': {'default_merge_method': 'squash', 'review_requests': None},
        'operations': ('publication_list', 'publication_prepare', 'publication_execute', 'publication_record',
                       'publication_recover', 'readiness', 'merge_proposal'),
    },
    'dk-pr-review-post': {
        'skill': 'cf-workflows:dk-pr-review-post',
        'role_prompt': 'Publish verified inline PR findings when authorized; report workflow blockers privately.\n',
        'baseline_prompt': 'claude/skills/dk-pr-review-post/prompts/default.md',
        'options': {'include_summary': False, 'blocking_event': 'REQUEST_CHANGES', 'nonblocking_event': 'COMMENT'},
        'operations': ('publication_list', 'publication_prepare', 'publication_execute', 'publication_record',
                       'publication_recover', 'approval_proposal'),
    },
    'dk-ci-approve': {
        'skill': 'cf-workflows:dk-ci-approve',
        'role_prompt': 'Approve fork workflow runs after complete incoming review, with current evidence and '
                       'journaled recovery.\n',
        'baseline_prompt': 'claude/skills/dk-ci-approve/prompts/default.md',
        'options': {},
        'operations': ('publication_list', 'publication_prepare', 'publication_execute', 'publication_record',
                       'publication_recover', 'fork_ci'),
    },
}
OWNERS = {action: message for action, (_, message) in dk_pr_fix.OWNED_ACTIONS.items()}
# The standalone-to-merge bridge: actions of another entry an entry may carry beside a
# required action of its own, each with its own explicit grant and permission (#35).
BRIDGES = {'dk-pr-publish': {'approve_fork_ci': 'merge'}}


def actions(name):
    return tuple(action for action, record in CAPABILITIES.items() if record['entry'] == name)


def check_options(name, options):
    """The entry's own options (its baseline schema) with provenance; unknown or unsupported values reject."""
    require(name in ENTRIES, 'entry', 'unknown publication entry')
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    declared = ENTRIES[name]['options']
    result = {key: {'value': copy.deepcopy(value), 'source': 'default'} for key, value in declared.items()}
    for key, value in options.items():
        where = 'options.' + str(key)
        require(key in declared, where, 'unsupported option for ' + ENTRIES[name]['skill'])
        if key == 'default_merge_method':
            require(value in ('squash', 'merge'), where, 'supports squash/merge only')
        elif key == 'review_requests':
            empty = isinstance(value, dict) and value == {'reviewers': [], 'team_reviewers': []}
            # Automatic finalization requests need dk-pr-approvers (#29); explicit
            # recipients are a request_review operation of the exact request.
            require(value is None or empty, where, 'automatic review requests are not supported yet; request '
                    'explicit reviewers with a request_review authorization')
        elif key == 'include_summary':
            require(type(value) is bool, where, 'must be boolean')
        elif key == 'blocking_event':
            require(value in ('REQUEST_CHANGES', 'COMMENT'), where, 'supports REQUEST_CHANGES or COMMENT; '
                    'approval is the separate approve_pr action')
        elif key == 'nonblocking_event':
            require(value == 'COMMENT', where, 'nonblocking and partial reviews always COMMENT')
        result[key] = {'value': copy.deepcopy(value), 'source': 'invocation'}
    return result


def select(name, authorizations):
    """The preset for an entry's requested actions; undeclared or mixed actions reject before effects."""
    require(isinstance(authorizations, dict), 'request.authorizations', 'must be an object')
    require(authorizations, 'request.authorizations', ENTRIES[name]['skill'] + ' needs an explicit authorization '
            'for each action it publishes; a configured permission never supplies one')
    presets = set()
    bridged = BRIDGES.get(name, {})
    for action in sorted(authorizations):
        where = 'request.authorizations.' + str(action)
        require(action not in OWNERS, where, OWNERS.get(action, ''))
        record = CAPABILITIES.get(action)
        require(record is not None, where, 'no declared publication capability')
        if action in bridged and bridged[action] in authorizations:
            continue  # Runs inside the bridging action's preset; never implied by it.
        require(record['entry'] == name, where, f"{action} belongs to cf-workflows:{record['entry']}")
        presets.add(record['preset'])
    require(len(presets) == 1, 'request.authorizations', 'merge needs current independent review of the published '
            'head; publish changes and merge in separate invocations')
    return presets.pop()


def _rejected(path, message):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}}


def preflight(name, *, request, deployment, repository, transport, configuration=None, invocation=None,
              options=None, roots=None, home=None, install=dk_pr_fix.INSTALL, maintain_other=None):
    """Admit one direct invocation and return the installed workflow's inputs; writes nothing.

    invocation may carry only `permissions` for the requested actions. The preset
    is applied at invocation precedence, so a configured selection never changes
    it; configured permissions, accounts and limits still apply.
    """
    entry = ENTRIES.get(name)
    if entry is None:
        return _rejected('entry', 'unknown publication entry')
    try:
        entry_options = check_options(name, options)
        require(isinstance(request, dict), 'request', 'must be an object')
        authorizations = request.get('authorizations', {})
        selected = select(name, authorizations)
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'permissions'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers, adapters and options; only permissions '
                'may be added')
        permissions = invocation.get('permissions', {})
        require(isinstance(permissions, dict), 'invocation.permissions', 'must be an object')
        for action in sorted(permissions):
            require(action in authorizations, 'invocation.permissions.' + str(action),
                    'only the requested actions may be enabled')
        require(maintain_other is None or selected == 'changes', 'maintain_other',
                'only the changes preset maintains the PR')
    except AdmissionError as error:
        return {**_rejected(error.path, error.message), 'entry': {'name': name, 'skill': entry['skill']}}
    preset = PRESETS[selected]
    merged = {'options': copy.deepcopy(preset['options'])}
    if selected == 'merge':
        merged['options']['merge_method'] = entry_options['default_merge_method']['value']
    given = {}
    if entry_options.get('include_summary', {}).get('source') == 'invocation':
        given['include_summary'] = entry_options['include_summary']['value']
    if entry_options.get('blocking_event', {}).get('source') == 'invocation':
        given['request_changes'] = entry_options['blocking_event']['value'] == 'REQUEST_CHANGES'
    if permissions:
        merged['permissions'] = copy.deepcopy(permissions)
    arguments = dict(request=request, configuration=configuration, invocation=merged, deployment=deployment,
                     repository=repository, transport=transport, roots=roots, home=home, install=install,
                     custom=False)
    if preset['mode'] == 'review':
        result = dk_pr_review.preflight(**arguments)
        configured = result.get('snapshot', {}).get('options', {}).get('settings', {}).get('value')
        if result.get('preflight') == 'admitted' and given and {**configured, **given} != configured:
            # Review-post options set only the publication settings they name; every other
            # configured setting is kept, so analysis bindings do not change. A second
            # admission binds the merged settings at invocation precedence.
            arguments['invocation'] = {**merged, 'options': {**merged['options'],
                                                             'settings': {**configured, **given}}}
            result = dk_pr_review.preflight(**arguments)
        result['workflow'] = entry['skill'] if result.get('preflight') == 'admitted' else result.get('workflow')
        if result.get('preflight') == 'admitted':
            result['installation']['entry'] = preset['workflow']
            if selected == 'merge':
                result['inputs'] = {MERGE_INPUTS.get(key, key): value for key, value in result['inputs'].items()}
    else:
        result = dk_pr_fix.preflight(**arguments, maintain_other=maintain_other, workflow=entry['skill'],
                                     entry=preset['workflow'])
    result['entry'] = {'name': name, 'skill': entry['skill'], 'preset': selected, 'mode': preset['mode'],
                       'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    snapshot = result['snapshot']
    expected = {'changes': ['tasks', 'publication'], 'merge': ['tasks', 'reviews'],
                'review-post': ['tasks', 'reviews'], 'fork-ci': ['tasks', 'reviews']}[selected]
    if ([stage['kind'] for stage in snapshot['plan']['stages']] != expected
            or any(stage['phase'] != 'tasks' for stage in snapshot['plan']['stages'])):
        return {**dk_pr_fix._reject(result, 'plan', f"{entry['skill']} runs exactly its {selected} preset"),
                'entry': result['entry']}
    assert not snapshot['plan']['tasks'] or all(not task['writes'] for task in snapshot['plan']['tasks'])
    result['entry'].update({
        'role_prompt': entry['role_prompt'], 'operations': list(entry['operations']),
        'adapters': sorted({task['adapter'] for task in snapshot['plan']['tasks']}),
        'definitions': sorted({task['definition'] for task in snapshot['plan']['tasks']} | {'audience'}),
        'capabilities': {action: dict(CAPABILITIES[action], kinds=list(CAPABILITIES[action]['kinds']))
                         for action in sorted(authorizations)},
    })
    return result
