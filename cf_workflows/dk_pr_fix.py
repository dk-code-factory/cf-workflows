"""Installed-resource preflight for cf-workflows:dk-pr-fix.

This module prepares one run of the installed YAML workflow. It verifies the
immutable installed closure, admits the configuration structurally against the
installed role catalog, assesses custom inputs semantically (cf_workflows.semantic),
rejects capabilities this release does not provide and maps the admitted options
onto the workflow's declared inputs. It stages the private request in the suite's
state namespace, outside the target checkout.

It never runs the workflow, a target task or a local provider session, and never
writes to the target checkout or the installation. Custom inputs only ask the
engine, through the transport, for one validator task and durable input gates
(cf_workflows.validator_binding). Agent Workflow runs the
workflow; until it can resolve an installed package and mount the state
namespace (Agent Workflow #130), the result is a validated input set only.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from . import admission, semantic

WORKFLOW = 'cf-workflows:dk-pr-fix'
INSTALL = Path(__file__).resolve().parent / 'workflows' / 'dk-pr-fix'
CLOSURE = 'closure.json'
ENTRY = 'workflows/main.yml'
INSTALL_ROOT = 'cf-workflows'
STATE_MOUNT = '/cf-workflows-state'
STATE_MARKER = '.owned-by-cf-workflows-dk-pr-fix'
STATE_FORMAT = 'cf-workflows-dk-pr-fix-v1'
ROLES = {role: f'workflows/agents/{role}.yaml' for role in admission.ADMITTED_ROLES}
# The workflow's permission inputs; each defaults to false.
PERMISSION_INPUTS = ('push_commits', 'rewrite_commits', 'edit_pr', 'reply_to_feedback',
                     'resolve_threads', 'create_draft_prs', 'request_review', 'post_findings',
                     'rerun_ci', 'approve_pr', 'approve_fork_ci', 'merge', 'create_issue', 'edit_issue')
# Contract actions the exact-intent publication path supports, mapped to the
# inputs they enable. History rewrites have no contract action yet, so
# rewrite_commits stays false.
PUBLICATION_ACTIONS = {'push': 'push_commits', 'edit_pr': 'edit_pr', 'reply': 'reply_to_feedback',
                       'resolve_thread': 'resolve_threads', 'create_pr': 'create_draft_prs',
                       'request_review': 'request_review', 'post_findings': 'post_findings',
                       'rerun_ci': 'rerun_ci', 'approve_pr': 'approve_pr', 'approve_fork_ci': 'approve_fork_ci',
                       'merge': 'merge', 'create_issue': 'create_issue', 'edit_issue': 'edit_issue'}
# Actions that only one entry's preset may authorize, with that entry. The installed
# workflow declares their inputs; prepare enables them only under that entry.
OWNED_ACTIONS = {
    'create_issue': ('cf-workflows:dk-issue', 'issue creation belongs to cf-workflows:dk-issue (#32)'),
    'edit_issue': ('cf-workflows:dk-issue', 'issue edits belong to cf-workflows:dk-issue (#32)'),
}


# Bundled adapters whose procedure is an entry's helper side, never a plain workflow task.
# draft-commit-message runs through the installed drafting procedure only under its own
# entry, whose plan alone may start from a checkout with uncommitted changes.
ENTRY_ONLY = {'discover-approvers': None, 'draft-commit-message': 'cf-workflows:dk-commit-message',
              'inspect-flakes': None}
# Incoming review publishes only its verified findings, a separately authorized
# approval of a clean review and separately authorized fork workflow approvals
# (#35); merge keeps its own grant and readiness.
REVIEW_ACTIONS = {'post_findings', 'approve_pr', 'approve_fork_ci', 'merge'}


def _review_plan(snapshot):
    """(path, message) when an admitted plan is not an incoming-review plan, else None."""
    tasks = snapshot['plan']['tasks']
    for task in tasks:
        if task['writes']:
            return task['path'], 'incoming review tasks cannot write'
    for stage in snapshot['plan']['stages']:
        if stage['kind'] in ('publication', 'responses', 'ci'):
            return 'plan.stages', ('incoming review supports task and reviewer stages only; verified findings '
                                   'and fork workflow approvals publish after them')
    if not any(stage['kind'] == 'reviews' for stage in snapshot['plan']['stages']):
        return 'plan.stages', 'incoming review requires a selected reviewer stage'
    return None


class InstallationError(ValueError):
    """The installed closure differs from its manifest."""


def _files(root):
    """Every regular file below root; symlinks, special files and bytecode caches reject.

    Python would load a cached .pyc instead of its verified source, so a cache is
    never part of a verified installation.
    """
    result = {}
    for directory, names, files in os.walk(root):
        names.sort()
        for name in names + sorted(files):
            path = Path(directory) / name
            if name == '__pycache__' or name.endswith(('.pyc', '.pyo')):
                raise InstallationError('installed closure contains Python bytecode: '
                                        + path.relative_to(root).as_posix())
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise InstallationError('installed closure contains a symlink: ' + path.relative_to(root).as_posix())
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise InstallationError('installed closure contains a special file: ' + path.relative_to(root).as_posix())
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def closure_digests(root=INSTALL):
    """{path: sha256} of the installed files, excluding the manifest itself; bytecode rejects."""
    files = _files(Path(root))
    files.pop(CLOSURE, None)
    return dict(sorted(files.items()))


def verify_installation(root=INSTALL):
    """Check the installation against closure.json; returns its closure digest."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise InstallationError('installation root must be a directory')
    manifest = admission.load_json((root / CLOSURE).read_bytes())
    if (not isinstance(manifest, dict) or manifest.get('workflow') != WORKFLOW
            or manifest.get('entry') != ENTRY or not isinstance(manifest.get('files'), dict)):
        raise InstallationError('unsupported installation manifest')
    actual = closure_digests(root)
    if actual != manifest['files']:
        changed = sorted(set(actual) ^ set(manifest['files']) |
                         {path for path in actual if manifest['files'].get(path) not in (None, actual[path])})
        raise InstallationError('installed closure differs from its manifest: ' + ', '.join(changed[:8]))
    return admission.digest(manifest)


def role_catalog(root=INSTALL):
    """The admitted role catalog of an installation, for admission.admit()."""
    files = admission.load_json((Path(root) / CLOSURE).read_bytes())['files']
    return {role: {'root': INSTALL_ROOT, 'path': path, 'sha256': files[path]} for role, path in ROLES.items()}


def state_home():
    raw = os.environ.get('XDG_STATE_HOME', '')
    return Path(raw) if os.path.isabs(raw) else Path.home() / '.local' / 'state'


def state_namespace(home, repository):
    """The suite's fresh state namespace for one canonical repository identity.

    It sits under HOME/cf-workflows, never in a plugin install or target checkout,
    and is keyed by a digest so user-supplied text never forms a path.
    """
    if not isinstance(repository, str) or not admission.REPOSITORY.fullmatch(repository):
        raise ValueError('canonical repository identity required')
    return Path(home) / 'cf-workflows' / 'repositories' / hashlib.sha256(repository.encode()).hexdigest()[:32]


def _disjoint(first, second):
    first, second = Path(first).resolve(), Path(second).resolve()
    return not (first == second or first.is_relative_to(second) or second.is_relative_to(first))


def _reject(result, path, message):
    return {'preflight': 'rejected', 'structural': result.get('structural', 'rejected'),
            'semantic': result.get('semantic'), 'error': {'path': path, 'message': message},
            **{key: result[key] for key in ('admission', 'snapshot', 'snapshot_digest') if key in result}}


def _adapter(record):
    return {key: record[key] for key in ('kind', 'writes', 'assignment', 'definition', 'checklist', 'resources',
                                         'history', 'head_sensitive', 'cacheable')}


def _constraints(result, mode, workflow):
    """(path, message) when an admitted proposal cannot run in the installed workflow, else None.

    It applies to the structural result before any semantic session and again
    to the final admitted proposal.
    """
    snapshot = result['snapshot']
    if snapshot['request']['pr'] is None:
        return 'request.pr', 'dk-pr-fix maintains one pull request; a repository target has none'
    if any(task['kind'] == admission.CONFIGURATION_KIND for task in snapshot['plan']['tasks']):
        # Their proposals are applied only by the configuration entries (cf_workflows.entries).
        return 'options.tasks', ('configuration adapters run only in cf-workflows:dk-config '
                                 'and cf-workflows:dk-preferences')
    # Prose finalizers (plain-english, asd-ste100) run as ordinary task occurrences; the installed
    # helpers bound their sessions to changed prose and check their edits (helpers/prfix/prose.py).
    if any(task['kind'] == admission.ALTERNATIVES_KIND for task in snapshot['plan']['tasks']):
        # A single generic task would conceal the staged procedure (cf_workflows.alternatives).
        return 'options.tasks', ('alternatives adapters run only through their staged procedure; '
                                 'the installed workflow does not run alternatives stages yet')
    entry_only = [task['adapter'] for task in snapshot['plan']['tasks']
                  if task['adapter'] in ENTRY_ONLY and ENTRY_ONLY[task['adapter']] != workflow]
    if entry_only:
        # Its result is meaningful only with the entry's evidence collection and checks
        # (cf_workflows.approvers, cf_workflows.drafting, cf_workflows.ci_flakes).
        return 'options.tasks', (entry_only[0] + ' runs only through its entry procedure; the '
                                 'installed workflow runs it as a generic task')
    # The workflow mounts only the repository and its own installation; a declared resource
    # (and a resource-backed prompt's link closure) would reach no occurrence.
    resourced = [task['adapter'] for task in snapshot['plan']['tasks'] if task['resources']]
    if resourced:
        return (f'options.adapters.{resourced[0]}.resources',
                'the installed workflow does not carry adapter resources yet; remove them or inline the guidance')
    for entry in snapshot['prompts'] + snapshot['preferences']:
        if entry['applicable'] and entry['resources']:
            return (entry['path'] + ('.resource' if entry['resource'] is not None else '.resources'),
                    'the installed workflow does not carry prompt or preference resources yet; '
                    'use inline text without resources')
    replacements =Counter(prompt['target'] for prompt in snapshot['prompts']
                           if prompt['applicable'] and prompt['slot'] == 'behavior_replacement')
    if None in replacements:
        return 'prompts', 'a behavior replacement prompt must target one adapter in the installed workflow'
    if any(count > 1 for count in replacements.values()):
        return 'prompts', 'at most one behavior replacement prompt applies to an adapter'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', snapshot['request']['logical_run']):
        return 'request.logical_run', 'the workflow ledger accepts [A-Za-z0-9_-]{1,100} only'
    # Every remote write needs an enabled permission and an explicit request
    # authorization; the guarded publication helpers recheck both at dispatch.
    permissions = snapshot['permissions']
    if mode == 'review':
        rejected = _review_plan(snapshot)
        if rejected:
            return rejected
    for action in sorted(snapshot['request']['authorizations']):
        if action in OWNED_ACTIONS and OWNED_ACTIONS[action][0] != workflow:
            return 'request.authorizations.' + action, OWNED_ACTIONS[action][1]
        if mode != 'review' and action == 'approve_pr':
            return ('request.authorizations.' + action,
                    'approval belongs to incoming review (dk-pr-review, dk-pr-review-post); '
                    'a maintained PR is never approved by its maintainer run')
        if mode != 'review' and action == 'approve_fork_ci':
            return ('request.authorizations.' + action,
                    'fork workflow approval belongs to incoming review (dk-pr-review, dk-ci-approve); '
                    'dk-pr-fix never approves fork workflows')
        if mode == 'review' and action not in REVIEW_ACTIONS:
            return ('request.authorizations.' + action,
                    'incoming review never writes the PR branch, description or discussion')
        if not permissions[action]['effective']:
            return ('request.authorizations.' + action,
                    'the authorized action is not enabled by accepted configuration; '
                    'enable it in a scoped configuration or remove the authorization')
    return None


def _custom_inputs(snapshot):
    """The accepted custom prompts, preferences and free-text policy, as workflow inputs.

    Per-task instructions travel in the task selections and custom adapters in
    `adapters`; both are already declared inputs.
    """
    prompts = [{**{key: entry[key] for key in ('id', 'slot', 'target', 'text')},
                **({'scope': entry['scope']} if entry['scope'] is not None else {})}
               for entry in snapshot['prompts'] + snapshot['preferences'] if entry['applicable']]
    policy = {key: [item['text'] for item in snapshot['options'][key]['value'] if item['text'] not in admission.ACTIONS]
              for key in admission.ACCUMULATED}
    return prompts, {key: value for key, value in policy.items() if value}


def preflight(*, request, configuration, invocation, deployment, repository, transport,
              roots=None, home=None, install=INSTALL, maintain_other=None, mode='fix', defaults=None,
              workflow=WORKFLOW, entry=ENTRY, custom=True):
    """Admit one run of the installed workflow and return its declared inputs.

    Writes nothing. A rejected or blocked result carries the admission error.
    maintain_other is the explicit request to maintain another author's PR
    ({request_id, instruction, source}); it is staged with the private request.
    mode, defaults, workflow and entry select an entry-point preset over the
    shared workflow, such as incoming review (cf_workflows.dk_pr_review).

    Custom inputs (prompts, preferences, per-task instructions, custom adapters
    and free-text policy) go through semantic assessment (cf_workflows.semantic)
    before anything is mapped: the transport then also runs at most one
    validator task and reads or opens durable input gates (see
    cf_workflows.validator_binding). An entry whose preset workflow does not
    carry them passes custom=False and rejects them before any gate read.
    """
    failure = {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None}
    try:
        closure = verify_installation(install)
    except (InstallationError, OSError, ValueError, KeyError) as error:
        return _reject(failure, 'installation', str(error) or type(error).__name__)
    roots = dict(roots or {})
    if INSTALL_ROOT in roots or 'repository' in roots:
        return _reject(failure, 'roots', f'{INSTALL_ROOT} and repository roots are supplied by preflight')
    if not Path(repository).is_dir() or not _disjoint(repository, install):
        return _reject(failure, 'repository', 'target checkout must be a directory separate from the installation')
    home = Path(home) if home is not None else state_home()
    if maintain_other is not None:
        if (mode != 'fix' or not isinstance(maintain_other, dict)
                or set(maintain_other) != {'request_id', 'instruction', 'source'}
                or not all(isinstance(value, str) and value.strip() for value in maintain_other.values())):
            return _reject(failure, 'maintain_other', 'maintaining another author\'s PR needs an exact '
                           '{request_id, instruction, source} request and the fix workflow')
    result = semantic.admit(request=request, configuration=configuration, invocation=invocation,
                            deployment=deployment, roles=role_catalog(install),
                            roots={**roots, 'repository': repository, INSTALL_ROOT: install}, transport=transport,
                            defaults=defaults,
                            check=lambda admitted: _constraints(admitted, mode, workflow) if custom else (
                                'custom', 'custom configuration inputs are not supported by the installed ' + workflow
                                + ' entry; run them through cf-workflows:dk-pr-fix or cf-workflows:dk-pr-review'))
    if result['preflight'] != 'admitted':
        # Rejected, blocked, or waiting on a durable clarification gate: nothing is mapped or staged.
        return result
    rejected = _constraints(result, mode, workflow)
    if rejected:
        return _reject(result, *rejected)
    snapshot = result['snapshot']
    permissions = snapshot['permissions']
    effective = sorted(action for action, value in permissions.items() if value['effective'])
    namespace = state_namespace(home, snapshot['request']['pr']['repository'])
    for other, name in ((repository, 'repository'), (install, 'installation')):
        if not _disjoint(namespace, other):
            return _reject(result, 'state', 'state namespace must be disjoint from the ' + name)
    options = {key: value['value'] for key, value in snapshot['options'].items()}
    selected = {task['adapter'] for task in snapshot['plan']['tasks']}
    prompts, policy = _custom_inputs(snapshot)
    staged = {'pr': snapshot['request']['pr']['url'],
              'instructions': snapshot['request']['original_instructions'],
              'logical_run': snapshot['request']['logical_run'],
              'authorizations': {action: snapshot['request']['authorizations'][action] for action in effective}}
    if maintain_other is not None:
        staged['maintain_other'] = dict(maintain_other)
    inputs = {
        'tasks': options['tasks'], 'finalizers': options['finalizers'], 'reviewers': options['reviewers'],
        'adapters': {name: _adapter(snapshot['adapters'][name]) for name in sorted(selected)},
        'limits': snapshot['limits'],
        'permissions': {name: name in {PUBLICATION_ACTIONS[action] for action in effective}
                        for name in PERMISSION_INPUTS},
        'ci': {'timeout_seconds': options['ci'].get('timeout_seconds', 1800),
               **{key: options['ci'][key] for key in ('pending_extension_seconds', 'max_pending_extensions',
                                                      *admission.CI_WINDOWS) if key in options['ci']}},
        'merge_method': options['merge_method'], 'accounts': options['accounts'],
        'settings': options['settings'], 'resources': [], 'turns': options['turns'],
    }
    # Declared by main.yml and review.yml; present only for accepted custom inputs, so every
    # other entry's input set and the default plan's are unchanged.
    inputs.update({key: value for key, value in (('prompts', prompts), ('policy', policy)) if value})
    request_bytes = admission.canonical(staged)
    reference = 'requests/' + hashlib.sha256(request_bytes).hexdigest() + '.json'
    inputs['request'] = reference
    return {**result, 'workflow': workflow, 'installation': {'root': str(Path(install).resolve()),
                                                             'entry': entry, 'closure_digest': closure},
            'inputs': inputs,
            'state': {'namespace': str(namespace), 'mount': STATE_MOUNT, 'request': reference,
                      'request_document': staged}}


def _atomic_write(path, data):
    descriptor, name = tempfile.mkstemp(prefix='.staged-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def stage_request(prepared):
    """Write the admitted private request into its state namespace; idempotent.

    Creates the namespace with this suite's ownership marker when absent. An
    existing directory without the marker is adopted only while empty, so prior
    dk-workflows or portable-example state is never imported.
    """
    namespace = Path(prepared['state']['namespace'])
    reference = prepared['state']['request']
    data = admission.canonical(prepared['state']['request_document'])
    if PurePosixPath(reference) != PurePosixPath('requests') / (hashlib.sha256(data).hexdigest() + '.json'):
        raise ValueError('staged request reference differs from its content')
    namespace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if namespace.is_symlink():
        raise ValueError('state namespace must not be a symlink')
    marker = namespace / STATE_MARKER
    expected = admission.canonical({'format': STATE_FORMAT})
    if not os.path.lexists(marker):
        if any(namespace.iterdir()):
            raise ValueError('refusing a nonempty state directory without this suite\'s marker')
        _atomic_write(marker, expected)
    if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != expected:
        raise ValueError('state namespace ownership marker differs')
    requests = namespace / 'requests'
    requests.mkdir(mode=0o700, exist_ok=True)
    if requests.is_symlink():
        raise ValueError('request directory must not be a symlink')
    path = namespace / reference
    if os.path.lexists(path):
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError('staged request conflict')
    else:
        _atomic_write(path, data)
    return reference


def write_closure(root=INSTALL):
    """Regenerate closure.json after an intentional change to the installed files."""
    manifest = {'schema_version': 1, 'kind': 'resource_closure', 'suite': admission.SUITE,
                'workflow': WORKFLOW, 'entry': ENTRY, 'files': closure_digests(root)}
    (Path(root) / CLOSURE).write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    write_closure()
