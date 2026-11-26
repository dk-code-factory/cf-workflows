"""Feedback read, address and reply entries as thin presets over the shared workflow (contract v1).

cf-workflows:dk-pr-feedback-read, dk-pr-feedback-address and dk-pr-feedback-reply add no
orchestration. Each entry is a preset selection for the installed dk-pr-fix workflow
(main.yml), the bundled adapters that selection runs and their helper operations in the
installed helpers (prfix/responses.py and prfix/feedback.py), and the entry's original
options. Preflight is dk-pr-fix preflight: the same structural admission, installed
closure, state namespace and staged request. No skill is loaded.

The entries share the PR's default logical run with dk-pr-fix, so standalone and
composed runs read and settle one feedback record and one publication journal: a
retained decision or reply is never made twice.

Original role prompts and options come from the pinned dk-workflows baseline
(MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy

from . import admission, dk_pr_fix
from .admission import AdmissionError, require

ENTRY = dk_pr_fix.ENTRY
# Exact baseline role prompts (prompts/default.md at the pinned commit; digests in baselines/inventory.json).
ENTRIES = {
    'dk-pr-feedback-read': {
        'skill': 'cf-workflows:dk-pr-feedback-read',
        'preset': {'tasks': ['read-feedback'], 'finalizers': [], 'reviewers': []},
        'role_prompt': 'Use the supplied complete evidence handoff. Without one, use '
                       '`fetch_unresolved_review_threads.py` and `fetch_pr_conversation.py` under '
                       '`runtime/dk-pr-feedback-address/scripts/`. Capture complete, stable snapshots across all '
                       'pages, with source references.\n',
        'baseline_prompt': 'claude/skills/dk-pr-feedback-read/prompts/default.md',
        'options': {}, 'actions': (),
        'operations': ('capture', 'context', 'check_feedback', 'retain_feedback', 'summary'),
    },
    'dk-pr-feedback-address': {
        'skill': 'cf-workflows:dk-pr-feedback-address',
        # Reading and decision reconciliation are the baseline entry's own first steps.
        'preset': {'tasks': ['read-feedback', 'reconcile-decisions', 'address-feedback'], 'finalizers': [],
                   'reviewers': []},
        'role_prompt': 'Read complete, stable feedback from the supplied handoff. Without a handoff, collect it with '
                       'the feedback read helpers. Read the governing instructions, Decisions and code for each item. '
                       'Check its validity and scope. Preserve settled decisions and deferred work. Make focused fixes '
                       'and run relevant checks. Run configured `additional_validation_commands` as argument arrays in '
                       'the affected checkout in addition to relevant checks. Never use them to launch Codex or Claude '
                       'Code.\n',
        'baseline_prompt': 'claude/skills/dk-pr-feedback-address/prompts/default.md',
        'options': {'additional_validation_commands': []}, 'actions': (),
        'operations': ('capture', 'context', 'check_feedback', 'controlled_commit', 'retain_feedback', 'summary'),
    },
    'dk-pr-feedback-reply': {
        'skill': 'cf-workflows:dk-pr-feedback-reply',
        'preset': {'tasks': [{'stage': 'responses'}], 'finalizers': [], 'reviewers': []},
        'role_prompt': 'Reply to affected PR feedback and resolve fully answered inline threads when authorized.\n',
        'baseline_prompt': 'claude/skills/dk-pr-feedback-reply/prompts/default.md',
        'options': {'reply_prompt': admission.REPLY_PROMPT, 'resolve_answered_threads': True},
        'actions': ('reply', 'resolve_thread'),
        'operations': ('responses_accept', 'publication_list', 'publication', 'responses_settle',
                       'responses_status'),
    },
}


def preset(name):
    """The entry's selection for the shared plan; finalizers and reviewers are explicitly empty."""
    return copy.deepcopy(ENTRIES[name]['preset'])


def check_options(name, options):
    """The entry's own options (its baseline schema) with provenance; unknown or unsupported values reject.

    Only values the installed workflow actually honors are accepted: the default reply prompt
    (applied by the shared responder), resolve_answered_threads, and an empty validation list.
    """
    require(name in ENTRIES, 'entry', 'unknown feedback entry')
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    declared = ENTRIES[name]['options']
    result = {key: {'value': copy.deepcopy(value), 'source': 'default'} for key, value in declared.items()}
    for key, value in options.items():
        where = 'options.' + str(key)
        require(key in declared, where, 'unsupported option for ' + ENTRIES[name]['skill'])
        if key == 'resolve_answered_threads':
            require(type(value) is bool, where, 'must be boolean')
        elif key == 'reply_prompt':
            admission.text(value, where)
            # A replacement is custom prompt text: it needs semantic admission and an installed
            # input that carries it to the responder. Neither exists yet.
            require(value == admission.REPLY_PROMPT, where, 'a custom reply prompt is not supported by the installed '
                    'workflow yet; the shared responder applies the default')
        elif key == 'additional_validation_commands':
            require(isinstance(value, list) and all(
                isinstance(command, list) and command
                and all(isinstance(arg, str) and arg.strip() and '\0' not in arg for arg in command)
                for command in value), where, 'must be an array of nonempty argument arrays')
            # Typed command settings have no installed input yet (settings carry booleans and integers only).
            require(not value, where, 'additional validation commands are not supported by the installed workflow '
                    'yet; run them separately')
        result[key] = {'value': copy.deepcopy(value), 'source': 'invocation'}
    return result


def _rejected(path, message):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}}


def _check_plan(name, snapshot):
    """(path, message) when the admitted plan is not exactly the entry's preset, else None."""
    entry = ENTRIES[name]
    expected = [call if isinstance(call, str) else call['stage'] for call in entry['preset']['tasks']]
    stages = snapshot['plan']['stages']
    tasks = {task['id']: task for task in snapshot['plan']['tasks']}
    observed = []
    for stage in stages:
        if stage['kind'] == 'tasks':
            observed += [tasks[key]['adapter'] for group in stage['groups'] for key in group]
        else:
            observed.append(stage['kind'])
    if observed != expected or any(stage['phase'] != 'tasks' for stage in stages):
        return 'plan', f"{entry['skill']} runs exactly its preset {expected}"
    return None


def preflight(name, *, request, deployment, repository, transport, configuration=None, invocation=None,
              options=None, roots=None, home=None, install=dk_pr_fix.INSTALL):
    """Admit one run of a feedback entry and return the installed workflow's inputs; writes nothing.

    invocation may carry only `permissions` for the entry's own remote-write actions
    (reply and resolve_thread for dk-pr-feedback-reply; none otherwise). The preset is
    applied at invocation precedence, so a configured task selection for dk-pr-fix never
    changes it. Configured permissions, accounts and limits still apply.
    """
    entry = ENTRIES.get(name)
    if entry is None:
        return _rejected('entry', 'unknown feedback entry')
    try:
        entry_options = check_options(name, options)
        require(isinstance(request, dict), 'request', 'must be an object')
        authorizations = request.get('authorizations', {})
        require(isinstance(authorizations, dict), 'request.authorizations', 'must be an object')
        for action in sorted(authorizations):
            require(action in entry['actions'], 'request.authorizations.' + str(action),
                    f"{entry['skill']} performs only {list(entry['actions']) or 'no remote writes'}; "
                    'publish fixes with dk-pr-publish or dk-pr-fix')
        if not entry_options.get('resolve_answered_threads', {'value': True})['value']:
            # Without the authorization the resolve_threads input stays false, so threads stay open.
            require('resolve_thread' not in authorizations, 'request.authorizations.resolve_thread',
                    'resolve_answered_threads is false; remove the resolve_thread authorization')
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'permissions'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and adapters; only permissions may be added')
        permissions = invocation.get('permissions', {})
        require(isinstance(permissions, dict), 'invocation.permissions', 'must be an object')
        for action in sorted(permissions):
            require(action in entry['actions'], 'invocation.permissions.' + str(action),
                    'not an action of ' + entry['skill'])
    except AdmissionError as error:
        return _rejected(error.path, error.message)
    merged = {'options': preset(name)}
    if permissions:
        merged['permissions'] = copy.deepcopy(permissions)
    result = dk_pr_fix.preflight(request=request, configuration=configuration, invocation=merged,
                                 deployment=deployment, repository=repository, transport=transport, roots=roots,
                                 home=home, install=install, workflow=entry['skill'], entry=ENTRY)
    result['entry'] = {'name': name, 'skill': entry['skill'], 'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    snapshot = result['snapshot']
    mismatch = _check_plan(name, snapshot)
    if mismatch:
        return {**dk_pr_fix._reject(result, *mismatch), 'entry': result['entry']}
    result['entry'].update({
        'role_prompt': entry['role_prompt'], 'operations': list(entry['operations']),
        'adapters': sorted({task['adapter'] for task in snapshot['plan']['tasks']}),
        'definitions': sorted({task['definition'] for task in snapshot['plan']['tasks']}),
    })
    return result
