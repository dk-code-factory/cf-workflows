"""Commit organization and PR splitting: cf-workflows:dk-commit-split and dk-pr-split (contract v1).

Each entry is a thin preset over the shared workflow: one baseline adapter task
(dk-commit-split runs `inspect-commits`, dk-pr-split runs `assess-scope`), then
the shared publication stage, with explicitly empty finalizers and reviewers.
Its adapter procedure is the admitted `leaf` role, the baseline read-only
assignment and the helper operations here:

- `collect` reads the PR's commits, authors, changed paths and the local
  checkout. It never changes Git.
- `organize` (dk-commit-split) runs or reuses up to `max_passes` read-only
  native sessions. Each assesses every commit and may propose a new series of
  path-level units; the helper accepts it only when every unit is used once,
  each path keeps its order, each new commit has one author and keeps its
  source trailers, and the rebuilt final tree equals the reviewed tree.
- `partition` (dk-pr-split) runs or reuses one session that assesses every
  changed path as core, unrelated or related work and may propose split PRs.
  The helper accepts it only when core work stays, every moved path belongs to
  exactly one split of an enabled mode, no other author's work moves, and the
  source and split trees together equal the reviewed tree.
- With an explicit local authorization, the helper writes the series through
  Git plumbing and ledgered compare-and-swap ref updates, with a recovery ref.
  Unrelated staged, unstaged and untracked work is never touched.
- `commit` commits a current dk-commit-message draft (#32) when asked to.
- `publish` sends the result through the shared exact-intent publisher
  (`rewrite`, `split_push`, `split_pr`, `push`) under separate grants. Local
  organization never implies a remote write.

Sessions come from the engine transport; nothing here starts a model, provider
or controller. Original role prompts and options come from the pinned
dk-workflows baseline (MIT; see licenses/dk-workflows-MIT.txt).
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from . import admission, configuration, decisions, dk_pr_feedback, dk_pr_fix, drafting, semantic
from .admission import AdmissionError, digest, fields, require, text

SCHEMA_VERSION = 1
KIND = 'splitting_state'
MAX_COMMITS = 100
MAX_PATHS = 1000
MAX_PATCH = 256 << 10
MODES = ('automatic', 'manual', 'disabled')
SCOPES = ('core', 'unrelated', 'related')
MODE_OPTION = {'unrelated': 'evacuate_unrelated', 'related': 'split_related'}
RECOVERY = 'refs/cf-workflows/recovery/'

# Exact baseline bytes (claude/ and codex/ skills/<entry>/config.json and prompts/default.md; digests in
# baselines/inventory.json).
ENTRIES = {
    'dk-commit-split': {
        'skill': 'cf-workflows:dk-commit-split', 'adapter': 'inspect-commits', 'actions': ('push',),
        'config': '{\n  "$schema": "./config.schema.json",\n  "policy": null,\n  "max_passes": 5\n}\n',
        'role_prompt': 'Review and organize all PR commits while preserving the reviewed final tree, authorship and '
                       'logical commit boundaries.\n',
    },
    'dk-pr-split': {
        'skill': 'cf-workflows:dk-pr-split', 'adapter': 'assess-scope', 'actions': ('create_pr', 'push'),
        'config': '{\n  "$schema": "./config.schema.json",\n  "added_lines_threshold": 1000,\n  '
                  '"evacuate_unrelated": "automatic",\n  "split_related": "automatic"\n}\n',
        'role_prompt': 'Move unrelated work or split large changes into separate PRs under saved size, scope and '
                       'publication policies.\n',
    },
}
for _entry in ENTRIES.values():
    _entry['options'] = {key: value for key, value in json.loads(_entry['config']).items() if key != '$schema'}
BY_ADAPTER = {entry['adapter']: name for name, entry in ENTRIES.items()}
POLICY_PROMPT = 'commit-policy'
_TRAILER = re.compile(r'^[A-Za-z][A-Za-z-]*-by:[ \t]*\S.*$', re.M)
_AUTHOR = re.compile(r'(.*) <([^<>]*)> ([0-9]+ [+-][0-9]{4})')
_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
_BRANCH = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}')


class Blocked(configuration.Blocked):
    """The checkout, a session result or a Git precondition cannot be used; nothing is applied."""


def _failure(error):
    if isinstance(error, (AdmissionError, configuration.Blocked)):
        return {'path': error.path, 'message': error.message}
    return {'path': '$', 'message': 'malformed result: ' + type(error).__name__}


def _rejected(path, message):
    return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
            'error': {'path': path, 'message': message}}


def _grant(value, where):
    """An explicit user authorization record, as for remote actions: request ID, instruction and source."""
    fields(value, where, ('request_id', 'instruction', 'source'))
    return {key: text(value[key], f'{where}.{key}') for key in ('request_id', 'instruction', 'source')}


# -- Entry preset -------------------------------------------------------------------------------

def preset(name):
    """The entry's selection for the shared plan: its baseline adapter, then the publication stage."""
    return {'tasks': [ENTRIES[name]['adapter'], {'stage': 'publication'}], 'finalizers': [], 'reviewers': []}


def check_options(name, options):
    """The entry's baseline options (its config.schema.json) with provenance; unknown options reject."""
    require(name in ENTRIES, 'entry', 'unknown splitting entry')
    options = {} if options is None else options
    require(isinstance(options, dict), 'options', 'must be an object')
    declared = ENTRIES[name]['options']
    result = {key: {'value': copy.deepcopy(value), 'source': 'default'} for key, value in declared.items()}
    for key, value in options.items():
        where = 'options.' + str(key)
        require(key in declared, where, 'unsupported option for ' + ENTRIES[name]['skill'])
        if key == 'policy':
            require(value is None or isinstance(value, dict), where, 'must be null or a policy object')
            if isinstance(value, dict):
                require('skill' not in value, where, 'policy providers run a supplied skill, which needs '
                        'session-scoped execution (#16, Agent Workflow #139); use a prompt policy')
                fields(value, where, ('prompt',))
                text(value['prompt'], where + '.prompt')
        elif key == 'max_passes':
            require(type(value) is int and 1 <= value <= 5, where, 'must be an integer from 1 to 5')
        elif key == 'added_lines_threshold':
            require(type(value) is int and 1 <= value <= 10 ** 7, where, 'must be a positive integer')
        else:
            require(value in MODES, where, 'must be one of ' + ', '.join(MODES))
        result[key] = {'value': copy.deepcopy(value), 'source': 'invocation'}
    return result


def preflight(name, *, request, configuration, deployment, repository, transport, invocation=None, options=None,
              roots=None, install=dk_pr_fix.INSTALL, ledger=None):
    """Admit one direct run of a splitting entry through the shared pipeline; writes nothing.

    The preset fixes tasks, finalizers, reviewers and permissions; invocation
    may add only prompts. dk-commit-split accepts only a push authorization and
    dk-pr-split create_pr and push; each still needs the configured permission.
    A prompt `policy` is admitted as a cleanup prompt for the adapter, so the
    shared validator assesses it. A default run contacts the engine only for
    capabilities. Local rewrites need their own authorization at `organize`
    or `partition`; no remote grant stands in for it.
    """
    entry = ENTRIES.get(name)
    if entry is None:
        return _rejected('entry', 'unknown splitting entry')
    try:
        entry_options = check_options(name, options)
        require(isinstance(request, dict) and 'pr' in request, 'request.pr', entry['skill'] + ' works on one pull request')
        authorizations = request.get('authorizations') or {}
        require(isinstance(authorizations, dict) and set(authorizations) <= set(entry['actions']),
                'request.authorizations', entry['skill'] + ' publishes only ' + ' and '.join(entry['actions']))
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'prompts'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and permissions; only prompts may be added')
    except AdmissionError as error:
        return {**_rejected(error.path, error.message), 'entry': {'name': name, 'skill': entry['skill']}}
    try:
        dk_pr_fix.verify_installation(install)
    except (dk_pr_fix.InstallationError, OSError, ValueError, KeyError) as error:
        return _rejected('installation', str(error) or type(error).__name__)
    roots = dict(roots or {})
    if dk_pr_fix.INSTALL_ROOT in roots or 'repository' in roots:
        return _rejected('roots', 'installation and repository roots are supplied by preflight')
    if not Path(repository).is_dir() or not dk_pr_fix._disjoint(repository, install):
        return _rejected('repository', 'target checkout must be a directory separate from the installation')
    prompts = copy.deepcopy(invocation.get('prompts', []))
    policy = entry_options.get('policy', {}).get('value')
    if policy is not None:
        prompts.append({'id': POLICY_PROMPT, 'slot': 'cleanup', 'target': entry['adapter'], 'text': policy['prompt']})
    merged = {'options': preset(name), 'prompts': prompts}
    result = semantic.admit(request=copy.deepcopy(request), configuration=copy.deepcopy(configuration),
                            invocation=merged, deployment=deployment, roles=dk_pr_fix.role_catalog(install),
                            roots={**roots, 'repository': repository, dk_pr_fix.INSTALL_ROOT: install},
                            transport=transport, ledger=ledger)
    result['entry'] = {'name': name, 'skill': entry['skill'], 'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    snapshot = result['snapshot']
    for action in sorted(authorizations):
        if not snapshot['permissions'][action]['effective']:
            return {**result, 'preflight': 'rejected', 'error': {
                'path': 'request.authorizations.' + action,
                'message': 'the authorized action is not enabled by accepted configuration; enable it in a scoped '
                           'configuration or remove the authorization'}}
    [task] = snapshot['plan']['tasks']
    assert task['adapter'] == entry['adapter'] and not task['writes']
    assert [stage['kind'] for stage in snapshot['plan']['stages']] == ['tasks', 'publication']
    assert not any(value['effective'] for action, value in snapshot['permissions'].items()
                   if action not in entry['actions'])
    result['entry'].update(adapter=entry['adapter'], task=task,
                           publication={action: snapshot['permissions'][action]['effective']
                                        for action in entry['actions']})
    return result


# The installed entry workflows: presets over workflows/main.yml (docs/splitting.md#installed-workflow).
ENTRY_WORKFLOWS = {'dk-commit-split': 'workflows/commit-split.yml', 'dk-pr-split': 'workflows/pr-split.yml'}
# The installed permission inputs each entry's intents need, by contract action.
INSTALLED_PERMISSIONS = {'dk-commit-split': {'push': 'rewrite_commits'},
                         'dk-pr-split': {'create_pr': 'create_draft_prs', 'push': 'push_commits'}}


def installed(name, *, request, configuration, deployment, repository, transport, invocation=None, options=None,
              rewrite=None, owners=(), validation_commands=(), roots=None, home=None, install=dk_pr_fix.INSTALL):
    """Admit one run of the installed entry workflow and return its declared inputs; writes nothing.

    The installed preset runs main.yml with the same tasks, finalizers and
    reviewers as preset(name). Its only permission inputs are the ones its
    intents need (rewrite_commits for dk-commit-split; create_draft_prs and
    push_commits for dk-pr-split), enabled for the requested and configured
    actions. Entry options set by the invocation, the explicit local `rewrite`
    grant, further `owners` and the `validation_commands` each new intermediate
    commit runs are passed as settings.splitting[name], which the installed
    helper validates again. A commit policy and invocation prompts need the
    shared validator and run only through preflight() and organize() or
    partition().
    """
    entry = ENTRIES.get(name)
    if entry is None:
        return _rejected('entry', 'unknown splitting entry')
    try:
        entry_options = check_options(name, options)
        require(entry_options.get('policy', {}).get('value') is None, 'options.policy',
                'a commit policy is assessed by the shared validator; run it through the library procedure')
        require(isinstance(request, dict) and 'pr' in request, 'request.pr', entry['skill'] + ' works on one pull request')
        authorizations = request.get('authorizations') or {}
        require(isinstance(authorizations, dict) and set(authorizations) <= set(entry['actions']),
                'request.authorizations', entry['skill'] + ' publishes only ' + ' and '.join(entry['actions']))
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'permissions'}, 'invocation',
                'the installed entry fixes its preset and runs no prompts yet; only permissions may be added')
        permissions = invocation.get('permissions', {})
        require(isinstance(permissions, dict), 'invocation.permissions', 'must be an object')
        for action in sorted(permissions):
            require(action in entry['actions'], 'invocation.permissions.' + str(action),
                    'only the entry\'s own actions may be enabled')
        grant = None if rewrite is None else _grant(rewrite, 'rewrite')
        owners = list(owners)
        for number, item in enumerate(owners):
            require(isinstance(item, str) and '@' in item and item.strip() == item and len(item) <= 256,
                    f'owners[{number}]', 'must be an email address')
        commands = dk_pr_feedback.check_options(
            'dk-pr-feedback-address', {'additional_validation_commands': list(validation_commands)}
        )['additional_validation_commands']['value']
    except AdmissionError as error:
        if error.path.startswith('options.additional_validation_commands'):
            error = AdmissionError('validation_commands' + error.path[len('options.additional_validation_commands'):],
                                   error.message)
        return {**_rejected(error.path, error.message), 'entry': {'name': name, 'skill': entry['skill']}}
    result = dk_pr_fix.preflight(request=request, configuration=configuration,
                                 invocation=copy.deepcopy(invocation) or None, deployment=deployment,
                                 repository=repository, transport=transport, roots=roots, home=home, install=install,
                                 defaults=preset(name), workflow=entry['skill'], entry=ENTRY_WORKFLOWS[name])
    result['entry'] = {'name': name, 'skill': entry['skill'], 'options': entry_options}
    if result['preflight'] != 'admitted':
        return result
    chosen = {key: copy.deepcopy(value['value']) for key, value in entry_options.items()
              if value['source'] == 'invocation'}
    for key, value in (('rewrite', grant), ('owners', owners), ('validation_commands', commands)):
        if value:
            chosen[key] = copy.deepcopy(value)
    if chosen:
        result['inputs']['settings'] = {**result['inputs']['settings'], 'splitting': {name: chosen}}
    effective = {action for action in entry['actions'] if result['snapshot']['permissions'][action]['effective']}
    mapping = INSTALLED_PERMISSIONS[name]
    result['inputs']['permissions'] = {setting: action in effective for action, setting in mapping.items()}
    result['entry'].update(adapter=entry['adapter'], publication={action: action in effective
                                                                   for action in entry['actions']})
    return result


def occurrence(name, admitted):
    """The entry's adapter occurrence in an admitted plan: its own preset or a composed shared plan."""
    tasks = [task for task in admitted['snapshot']['plan']['tasks'] if task['adapter'] == ENTRIES[name]['adapter']]
    require(len(tasks) == 1, 'plan.tasks', 'the plan must select ' + ENTRIES[name]['adapter'] + ' exactly once')
    return tasks[0]


def effective(name, admitted, options=None):
    """The governing role behavior, prompt additions, policy text and options of this occurrence.

    The entry's own preflight binds its options; a composed caller may pass the
    entry options it was configured with, checked against the same schema. The
    commit policy is every applicable `cleanup` or `commit` prompt of the adapter.
    """
    entry = admitted.get('entry')
    entry = entry if isinstance(entry, dict) else {}
    own = entry.get('skill') == ENTRIES[name]['skill']
    require(not (own and options is not None), 'options', 'the entry preflight already bound its options')
    checked = entry['options'] if own else check_options(name, options)
    adapter = ENTRIES[name]['adapter']
    applicable = [item for item in admitted['snapshot']['prompts']
                  if item['applicable'] and item['target'] in (None, adapter)]
    replacement = [item for item in applicable if item['slot'] == 'behavior_replacement']
    require(len(replacement) <= 1, 'prompts', 'at most one behavior replacement applies to ' + adapter)
    policy = [item['text'] for item in applicable if item['slot'] in ('cleanup', 'commit')]
    values = {key: value['value'] for key, value in checked.items()}
    if not own and isinstance(values.get('policy'), dict):
        raise AdmissionError('options.policy', 'a composed plan admits its commit policy as a cleanup prompt')
    return {'role_prompt': replacement[0]['text'] if replacement else ENTRIES[name]['role_prompt'],
            'role_source': 'prompt:' + replacement[0]['id'] if replacement else 'default',
            'additions': [{key: item[key] for key in ('id', 'slot', 'text')} for item in applicable
                          if item['slot'] != 'behavior_replacement'],
            'policy': '\n\n'.join(policy) or None, 'options': values}


# -- Git ----------------------------------------------------------------------------------------

def git(repository, *args, data=None, env=None, check=True):
    """Git with filesystem monitors, hooks, external diffs and lazy fetches off; env adds variables."""
    try:
        result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
                                 '--literal-pathspecs', '-C', str(repository), *args],
                                input=data, capture_output=True, timeout=60,
                                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
                                     'GIT_TERMINAL_PROMPT': '0', **(env or {})})
    except subprocess.TimeoutExpired:
        raise Blocked('git', 'git ' + args[0] + ' timed out after 60 seconds') from None
    except OSError as error:
        raise Blocked('git', 'git ' + args[0] + ' could not start: ' + type(error).__name__) from None
    if check and result.returncode:
        raise Blocked('git', 'git ' + args[0] + ' failed: ' + result.stderr.decode(errors='replace')[:400])
    return result.stdout


def _sha(repository, revision, env=None):
    value = git(repository, 'rev-parse', '--verify', '--quiet', revision, env=env, check=False).decode().strip()
    return value if re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', value) else None


@contextmanager
def _objects(repository, write):
    """Where new objects go: the repository when writing, else a discarded directory over its objects."""
    if write:
        yield {}
        return
    common = git(repository, 'rev-parse', '--git-common-dir').decode().strip()
    objects = (Path(repository) / common if not os.path.isabs(common) else Path(common)).resolve() / 'objects'
    with tempfile.TemporaryDirectory(prefix='cf-splitting-') as scratch:
        yield {'GIT_OBJECT_DIRECTORY': scratch, 'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(objects)}


def _state(raw_mode, oid):
    return None if set(oid) == {'0'} else [raw_mode, oid]


def _diff(repository, old, new, env=None):
    """{path: [old state, new state]} of a tree diff; a state is [mode, oid] or None."""
    raw = git(repository, 'diff-tree', '-r', '-z', '--no-renames', '--no-commit-id', '--raw', old, new, env=env)
    parts = raw.decode('utf-8', 'surrogateescape').split('\0')
    result = {}
    for header, path in zip(parts[0::2], parts[1::2]):
        if header:
            old_mode, new_mode, old_oid, new_oid, _ = header.lstrip(':').split(' ')
            result[path] = [_state(old_mode, old_oid), _state(new_mode, new_oid)]
    return result


def _states(repository, treeish, paths, env=None):
    """{path: [mode, oid]} for the paths present in treeish."""
    if not paths:
        return {}
    raw = git(repository, 'ls-tree', '-r', '-z', '--full-tree', treeish, '--', *sorted(paths), env=env)
    result = {}
    for entry in raw.decode('utf-8', 'surrogateescape').split('\0'):
        if entry:
            meta, path = entry.split('\t', 1)
            mode, _, oid = meta.split(' ')
            result[path] = [mode, oid]
    return result


def _tree(repository, parent_tree, states, env):
    """A new tree: parent_tree with each path set to its state (None removes it), via a private index."""
    with tempfile.TemporaryDirectory(prefix='cf-index-') as scratch:
        local = {**env, 'GIT_INDEX_FILE': str(Path(scratch) / 'index')}
        git(repository, 'read-tree', parent_tree, env=local)
        zero = '0' * len(parent_tree)
        lines = ''.join(f'0 {zero}\t{path}\0' if state is None else f'{state[0]} {state[1]}\t{path}\0'
                        for path, state in sorted(states.items()))
        if lines:
            git(repository, 'update-index', '-z', '--index-info', data=lines.encode('utf-8', 'surrogateescape'),
                env=local)
        return git(repository, 'write-tree', env=local).decode().strip()


def _identity(repository):
    name = git(repository, 'config', '--get', 'user.name', check=False).decode().strip()
    email = git(repository, 'config', '--get', 'user.email', check=False).decode().strip()
    require(name and email, 'identity', 'a configured Git user.name and user.email commit the result')
    return {'name': name, 'email': email}


def _commit(repository, tree, parent, message, author, committer, env):
    """commit-tree with an exact author; the committer is the configured identity at the author date."""
    local = {**env, 'GIT_AUTHOR_NAME': author['name'], 'GIT_AUTHOR_EMAIL': author['email'],
             'GIT_AUTHOR_DATE': author['date'], 'GIT_COMMITTER_NAME': committer['name'],
             'GIT_COMMITTER_EMAIL': committer['email'], 'GIT_COMMITTER_DATE': author['date']}
    return git(repository, 'commit-tree', tree, '-p', parent, data=(message.rstrip('\n') + '\n').encode(),
               env=local).decode().strip()


def _record(repository, sha, env=None):
    """One commit: parents, exact author, message, trailers and changed paths."""
    raw = git(repository, 'cat-file', 'commit', sha, env=env).decode('utf-8', 'surrogateescape')
    header, _, message = raw.partition('\n\n')
    parents, author, signed = [], None, False
    for line in header.split('\n'):
        key, _, value = line.partition(' ')
        if key == 'parent':
            parents.append(value)
        elif key == 'author':
            match = _AUTHOR.fullmatch(value)
            require(match, 'commits', 'unparsable author of ' + sha)
            author = {'name': match[1], 'email': match[2], 'date': match[3]}
        elif key == 'gpgsig':
            signed = True
    require(len(parents) == 1, 'commits', 'merge and root commits are not reorganized: ' + sha)
    return {'sha': sha, 'parent': parents[0], 'author': author, 'message': message, 'signed': signed,
            'trailers': _TRAILER.findall(message), 'paths': _diff(repository, parents[0], sha, env)}


def series(repository, base, head, env=None):
    """The commits from base (exclusive) to head, oldest first."""
    listed = git(repository, 'rev-list', '--reverse', '--topo-order', f'{base}..{head}', env=env).decode().split()
    require(len(listed) <= MAX_COMMITS, 'commits', f'more than {MAX_COMMITS} commits')
    return [_record(repository, sha, env) for sha in listed]


def _dirty(repository):
    raw = git(repository, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames')
    return sorted(entry[3:] for entry in raw.decode('utf-8', 'surrogateescape').split('\0') if entry)


def _ref(repository, ref):
    return _sha(repository, ref) if ref == 'HEAD' else (
        git(repository, 'rev-parse', '--verify', '--quiet', ref, check=False).decode().strip() or None)


def _owned(record, owners):
    return record['author']['email'].lower() in owners


def _fixed(records, owners):
    """Commits up to and including the last one another author owns are never rewritten."""
    foreign = [number for number, record in enumerate(records) if not _owned(record, owners)]
    return foreign[-1] + 1 if foreign else 0


# -- Evidence -----------------------------------------------------------------------------------

def collect(name, admitted, *, repository, capture, owners=(), options=None):
    """Read-only evidence: the PR pins, the checked-out branch, every commit and the final diff.

    owners are further author emails the actor owns besides the configured Git
    identity. The checked-out branch must be at the PR head.
    """
    snapshot = admitted['snapshot']
    pr = snapshot['request']['pr']
    require(pr is not None, 'request.pr', ENTRIES[name]['skill'] + ' works on one pull request')
    governing = effective(name, admitted, options)
    require(isinstance(capture, dict), 'capture', 'the shared PR capture is required')
    meta = capture.get('metadata') or {}
    require(str(meta.get('html_url', '')).lower() == pr['url'] and meta.get('number') == pr['number'],
            'capture.metadata', 'the capture belongs to another pull request')
    head, base = (meta.get('head') or {}).get('sha'), (meta.get('base') or {}).get('sha')
    for sha, label in ((head, 'head'), (base, 'base')):
        require(isinstance(sha, str) and _sha(repository, sha + '^{commit}') == sha, 'checkout',
                f'the PR {label} commit must be present in the checkout')
    branch = git(repository, 'symbolic-ref', '-q', 'HEAD', check=False).decode().strip()
    require(branch.startswith('refs/heads/'), 'checkout', 'a branch must be checked out')
    if _sha(repository, 'HEAD') != head:
        raise Blocked('checkout', 'the checked-out branch is not at the PR head; reconcile it first')
    bases = git(repository, 'merge-base', '--all', base, head).decode().split()
    require(len(bases) == 1, 'history', 'the PR needs one unique merge base')
    merge_base = bases[0]
    records = series(repository, merge_base, head)
    require(records, 'commits', 'the pull request has no commits')
    identity = _identity(repository)
    for number, item in enumerate(owners):
        require(isinstance(item, str) and '@' in item, f'owners[{number}]', 'must be an email address')
    owned = {identity['email'].lower(), *(item.lower() for item in owners)}
    changes = _diff(repository, merge_base, head)
    require(len(changes) <= MAX_PATHS, 'changes', f'more than {MAX_PATHS} changed paths')
    added = 0
    for line in git(repository, 'diff', '--numstat', '--no-renames', '--no-ext-diff', merge_base, head).decode(
            'utf-8', 'surrogateescape').splitlines():
        count = line.split('\t', 1)[0]
        added += int(count) if count.isdigit() else 0
    return {'entry': name, 'governing': governing,
            'pr': {'url': pr['url'], 'head': head, 'base': base, 'head_ref': (meta.get('head') or {}).get('ref'),
                   'base_ref': (meta.get('base') or {}).get('ref'),
                   'head_repository': str(((meta.get('head') or {}).get('repo') or {}).get('full_name', '')).lower()},
            'branch': branch, 'merge_base': merge_base, 'records': records, 'identity': identity,
            'owners': sorted(owned), 'changes': changes, 'added_lines': added,
            'tree': _sha(repository, head + '^{tree}'), 'dirty': _dirty(repository)}


def _patch(repository, records, env):
    """Bounded per-commit patches for the session; a cut is flagged, never silent."""
    remaining, result = MAX_PATCH, {}
    for record in records:
        patch = git(repository, 'show', '--format=', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames',
                    record['sha'], env=env).decode('utf-8', 'replace')
        result[record['sha']] = {'patch': patch[:max(remaining, 0)], 'truncated': len(patch) > remaining}
        remaining -= len(patch)
    return result


def _context(name, admitted, task, governing, evidence):
    snapshot = admitted['snapshot']
    return digest({'entry': name,
                   'task': {key: task[key] for key in ('adapter', 'definition', 'definition_sha256',
                                                        'assignment_sha256', 'instructions')},
                   'governing': governing, 'instructions': snapshot['request']['original_instructions'],
                   'requirements': [item['text'] for item in snapshot['options']['requirements']['value']],
                   'prohibitions': [item['text'] for item in snapshot['options']['prohibitions']['value']],
                   'guidance': snapshot['resources']['guidance'], 'agent': semantic.validator_agent(admitted),
                   'pr': evidence['pr'], 'owners': evidence['owners'], 'identity': evidence['identity']})


def assignment(name, admitted, task, governing, evidence, records, *, claim, number, env):
    """The exact typed assignment of the adapter's engine-managed read-only native session."""
    snapshot = admitted['snapshot']
    agent, provider = semantic.validator_agent(admitted)
    definition = snapshot['adapters'][task['adapter']]
    root = Path(evidence['repository']) if 'repository' in evidence else None
    readable = {}
    for rel in sorted(snapshot['resources']['guidance']['files']):
        readable['repository:' + rel] = snapshot['resources']['guidance']['files'][rel]
    patches = _patch(root, records, env) if root else {}
    fixed = _fixed(records, set(evidence['owners']))
    context = {
        'pr': evidence['pr'], 'merge_base': evidence['merge_base'], 'pass': number,
        'commits': [{'sha': item['sha'], 'author': item['author'], 'message': item['message'],
                     'owned': _owned(item, set(evidence['owners'])), 'fixed': index < fixed,
                     'paths': sorted(item['paths']), **patches.get(item['sha'], {})}
                    for index, item in enumerate(records)],
        'changes': sorted(evidence['changes']), 'added_lines': evidence['added_lines'],
        'policy': governing['policy'],
    }
    return {
        'schema_version': 1, 'kind': 'splitting_task', 'suite': admission.SUITE, 'skill': ENTRIES[name]['skill'],
        'mode': name, 'claim': claim, 'adapter': task['adapter'], 'task': task['id'], 'role': task['definition'],
        'definition_sha256': task['definition_sha256'], 'assignment': definition['assignment'],
        'checklist': definition['checklist'], 'task_instructions': task['instructions'],
        'role_prompt': governing['role_prompt'], 'additions': governing['additions'],
        'options': governing['options'], 'instructions': snapshot['request']['original_instructions'],
        'requirements': [item['text'] for item in snapshot['options']['requirements']['value']],
        'prohibitions': [item['text'] for item in snapshot['options']['prohibitions']['value']],
        'agent': agent, 'provider': provider, 'skills': [], 'tools': 'read-only', 'writes': False,
        'publication': False, 'readable': readable, 'context': context,
    }


# -- Result checks ------------------------------------------------------------------------------

def _items(value, path, required, optional=()):
    require(isinstance(value, list) and len(value) <= MAX_PATHS, path, 'must be a bounded array')
    for number, item in enumerate(value):
        fields(item, f'{path}[{number}]', required, optional)
    return value


def check_message(message, where, *, policy, required=()):
    """A commit message: subject line, blank separator, no comment lines or tool attribution.

    The dk-commit-message length fallbacks apply only without commit-policy
    text. Trailers are exactly the source trailers (`required`); none is invented.
    """
    require(isinstance(message, str) and message.strip() and '\x00' not in message, where, 'must be text')
    lines = message.strip('\n').split('\n')
    require(lines[0].strip() and lines[0] == lines[0].strip(), where, 'the subject must be one nonempty line')
    require(len(lines) == 1 or lines[1] == '', where, 'a blank line separates the subject from the body')
    require(not any(line.startswith('#') for line in lines), where, 'Git strips lines that start with #')
    require(not drafting._ATTRIBUTION.search(message), where, 'messages carry no tool attribution')
    if not policy:
        limits = drafting.ENTRIES['dk-commit-message']['options']
        require(len(lines[0]) <= limits['subject_target_length'], where,
                f"the subject exceeds {limits['subject_target_length']} characters")
        require(all(len(line) <= limits['max_line_length'] or '://' in line or _TRAILER.fullmatch(line)
                    for line in lines), where, f"message lines stay within {limits['max_line_length']} characters")
    trailers = set(_TRAILER.findall(message))
    missing = sorted(set(required) - trailers)
    require(not missing, where, 'source trailers must be kept: ' + '; '.join(missing))
    invented = sorted(trailers - set(required))
    require(not invented, where, 'trailers must come from the source commits: ' + '; '.join(invented))
    return message.strip('\n') + '\n'


def _common(output, expected, outcomes):
    fields(output, 'result', expected)
    require(set(output) == set(expected), 'result', 'unsupported result fields')
    require(output['outcome'] in outcomes, 'outcome', 'must be one of ' + ', '.join(outcomes))
    text(output['summary'], 'summary', 4096)
    require(isinstance(output['questions'], list) and all(isinstance(item, str) and item.strip()
                                                         for item in output['questions']), 'questions',
            'must be a list of questions')


def check_commits(output, evidence, records, governing):
    """The accepted assessment of one pass, or AdmissionError; units must cover the rewritable range."""
    _common(output, ('outcome', 'assessments', 'series', 'summary', 'questions'), ('no_change', 'reorganize'))
    shas = [item['sha'] for item in records]
    assessed = {}
    for number, item in enumerate(_items(output['assessments'], 'assessments', ('commit', 'reason'))):
        text(item['reason'], f'assessments[{number}].reason', 2048)
        require(item['commit'] in shas and item['commit'] not in assessed, f'assessments[{number}].commit',
                'assess each commit of the series exactly once')
        assessed[item['commit']] = item['reason']
    require(set(assessed) == set(shas), 'assessments', 'every commit is assessed, including unchanged ones')
    if output['outcome'] == 'no_change':
        require(output['series'] == [], 'series', 'an unchanged series proposes no commits')
        return {'outcome': 'no_change', 'assessments': output['assessments'], 'series': [],
                'summary': output['summary'], 'questions': output['questions']}
    owners = set(evidence['owners'])
    fixed = _fixed(records, owners)
    index = {sha: number for number, sha in enumerate(shas)}
    units, groups = {}, []
    for number, group in enumerate(_items(output['series'], 'series', ('message', 'units', 'reason'))):
        where = f'series[{number}]'
        text(group['reason'], where + '.reason', 2048)
        sources = []
        for n, unit in enumerate(_items(group['units'], where + '.units', ('commit', 'paths'))):
            at = f'{where}.units[{n}]'
            require(unit['commit'] in index, at + '.commit', 'unknown commit')
            require(index[unit['commit']] >= fixed, at + '.commit',
                    'commits up to the last one by another author are never rewritten')
            record = records[index[unit['commit']]]
            require(isinstance(unit['paths'], list) and unit['paths'], at + '.paths', 'name the moved paths')
            for path in unit['paths']:
                require(path in record['paths'], at + '.paths', f'{path} is not changed by {unit["commit"]}')
                require((unit['commit'], path) not in units, at + '.paths', f'{path} of {unit["commit"]} is used twice')
                units[(unit['commit'], path)] = number
            sources.append(record)
        require(sources, where + '.units', 'a commit needs units')
        authors = {(item['author']['name'], item['author']['email']) for item in sources}
        require(len(authors) == 1, where + '.units', 'one new commit keeps one author; split mixed authorship')
        required = sorted({line for item in sources for line in item['trailers']})
        message = check_message(group['message'], where + '.message', policy=governing['policy'], required=required)
        groups.append({'message': message, 'units': [{'commit': unit['commit'], 'paths': sorted(unit['paths'])}
                                                     for unit in group['units']], 'reason': group['reason']})
    every = {(item['sha'], path) for item in records[fixed:] for path in item['paths']}
    require(set(units) == every, 'series', 'every path of every rewritable commit is placed exactly once')
    for path in {path for _, path in every}:
        order = sorted((index[sha], units[(sha, p)]) for sha, p in units if p == path)
        require([group for _, group in order] == sorted(group for _, group in order), 'series',
                f'changes to {path} must keep their original order')
    return {'outcome': 'reorganize', 'assessments': output['assessments'], 'series': groups,
            'summary': output['summary'], 'questions': output['questions']}


def _public(value, where, url):
    require('\x00' not in value and not drafting._ATTRIBUTION.search(value), where, 'no tool attribution')
    for found in drafting._PATH_LINE.finditer(drafting._strip_code(value)):
        raise Blocked(where, found[0] + ' is not usable in a browser; use a SHA-pinned permalink')
    for kind, ref in drafting._CODE_LINK.findall(value):
        require(drafting._SHA.fullmatch(ref), where, 'code links must be pinned to a full commit SHA')
    return value


def check_split(output, evidence, governing):
    """The accepted scope assessment, or AdmissionError; core work stays and nothing is dropped."""
    _common(output, ('outcome', 'assessments', 'splits', 'source_message', 'summary', 'questions'), ('keep', 'split'))
    changes = evidence['changes']
    scopes = {}
    for number, item in enumerate(_items(output['assessments'], 'assessments', ('path', 'scope', 'reason'))):
        where = f'assessments[{number}]'
        require(item['path'] in changes and item['path'] not in scopes, where + '.path',
                'assess each changed path exactly once')
        require(item['scope'] in SCOPES, where + '.scope', 'must be one of ' + ', '.join(SCOPES))
        text(item['reason'], where + '.reason', 2048)
        scopes[item['path']] = item['scope']
    require(set(scopes) == set(changes), 'assessments', 'every changed path is assessed')
    if output['outcome'] == 'keep':
        require(output['splits'] == [] and output['source_message'] is None, 'splits', 'keeping proposes no split')
        return {'outcome': 'keep', 'assessments': output['assessments'], 'splits': [], 'source_message': None,
                'summary': output['summary'], 'questions': output['questions']}
    owners, records = set(evidence['owners']), evidence['records']
    options, policy, url = governing['options'], governing['policy'], evidence['pr']['url']
    moved, ids, branches, splits = {}, set(), set(), []
    for number, item in enumerate(_items(output['splits'], 'splits', (
            'id', 'kind', 'paths', 'branch', 'title', 'body', 'message', 'reason'))):
        where = f'splits[{number}]'
        require(isinstance(item['id'], str) and _SLUG.fullmatch(item['id']) and item['id'] not in ids,
                where + '.id', 'a distinct lowercase slug')
        ids.add(item['id'])
        require(item['kind'] in ('unrelated', 'related'), where + '.kind', 'splits move unrelated or related work')
        require(options[MODE_OPTION[item['kind']]] != 'disabled', where + '.kind',
                MODE_OPTION[item['kind']] + ' is disabled for this pull request; that work stays')
        branch = item['branch']
        # Git's ref rules: no empty, dot-led or .lock component, and no trailing dot.
        require(isinstance(branch, str) and _BRANCH.fullmatch(branch) and '..' not in branch
                and not branch.endswith(('/', '.', '.lock')) and branch not in branches
                and all(part and not part.startswith('.') and not part.endswith('.lock') for part in branch.split('/'))
                and branch not in (evidence['pr']['head_ref'], evidence['pr']['base_ref']), where + '.branch',
                'a new distinct branch name')
        branches.add(branch)
        require(isinstance(item['paths'], list) and item['paths'], where + '.paths', 'name the moved paths')
        for path in item['paths']:
            require(path in changes and path not in moved, where + '.paths', f'{path} is not a changed path or moves twice')
            require(all(state is None or state[0] != '160000' for state in changes[path]), where + '.paths',
                    f'{path} is a submodule; splits move files')
            require(scopes[path] == item['kind'], where + '.paths', f'{path} is assessed as {scopes[path]} work')
            touching = [record for record in records if path in record['paths']]
            require(all(_owned(record, owners) for record in touching), where + '.paths',
                    f'{path} includes another author\'s work; it is never moved without them')
            moved[path] = item['id']
        title = text(item['title'], where + '.title', 256)
        require('\n' not in title, where + '.title', 'one line')
        body = _public(text(item['body'], where + '.body'), where + '.body', url)
        require(url in body, where + '.body', 'a split PR links back to its source pull request')
        sources = [record for record in records if set(record['paths']) & set(item['paths'])]
        authors = []
        for record in reversed(sources):
            key = (record['author']['name'], record['author']['email'])
            if key not in authors:
                authors.append(key)
        required = sorted({line for record in sources for line in record['trailers']}
                          | {f'Co-authored-by: {name} <{email}>' for name, email in authors[1:]})
        splits.append({'id': item['id'], 'kind': item['kind'], 'paths': sorted(item['paths']), 'branch': branch,
                       'title': _public(title, where + '.title', url), 'body': body, 'reason':
                       text(item['reason'], where + '.reason', 2048),
                       'message': check_message(item['message'], where + '.message', policy=policy, required=required)})
    require(splits, 'splits', 'a split proposes at least one split pull request')
    require(any(scope == 'core' for scope in scopes.values()) and set(changes) - set(moved), 'splits',
            'the source pull request keeps its core work; no size-only excuse discards required work')
    source = check_message(output['source_message'], 'source_message', policy=policy)
    return {'outcome': 'split', 'assessments': output['assessments'], 'splits': splits, 'source_message': source,
            'summary': output['summary'], 'questions': output['questions']}


# -- Builds -------------------------------------------------------------------------------------

def build_series(repository, evidence, records, value, env):
    """Commit objects of an accepted reorganization; returns (records of the new series, head).

    A group equal to one original commit on its original parent keeps that commit.
    """
    fixed = _fixed(records, set(evidence['owners']))
    index = {item['sha']: number for number, item in enumerate(records)}
    parent = records[fixed - 1]['sha'] if fixed else evidence['merge_base']
    previous = _sha(repository, parent + '^{tree}', env)
    committer = evidence['identity']
    for number, group in enumerate(value['series']):
        units = sorted(group['units'], key=lambda unit: index[unit['commit']])
        sources = [records[index[unit['commit']]] for unit in units]
        original = sources[0]
        if (len(units) == 1 and units[0]['paths'] == sorted(original['paths']) and original['parent'] == parent
                and group['message'].rstrip('\n') == original['message'].rstrip('\n')):
            parent, previous = original['sha'], _sha(repository, original['sha'] + '^{tree}', env)
            continue
        states = {}
        for unit, record in zip(units, sources):
            for path in unit['paths']:
                states[path] = record['paths'][path][1]  # The latest unit of a path wins.
        tree = _tree(repository, previous, states, env)
        if tree == previous:
            raise Blocked(f'series[{number}]', 'the new commit would be empty')
        parent = _commit(repository, tree, parent, group['message'], sources[-1]['author'], committer, env)
        previous = tree
    if _sha(repository, parent + '^{tree}', env) != evidence['tree']:
        raise Blocked('series', 'the rebuilt series does not reproduce the reviewed final tree')
    return series(repository, evidence['merge_base'], parent, env), parent


def build_split(repository, evidence, value, env):
    """Split commits on the PR target and the source commit without the moved work; verified against the tree."""
    pr, base, head = evidence['pr'], evidence['merge_base'], evidence['pr']['head']
    moved = sorted(path for split in value['splits'] for path in split['paths'])
    at_base, at_target = _states(repository, base, moved, env), _states(repository, pr['base'], moved, env)
    collisions = [path for path in moved if at_base.get(path) != at_target.get(path)]
    if collisions:
        raise Blocked('collision', 'the target changed moved paths since the merge base: ' + ', '.join(collisions[:5]))
    final = _states(repository, head, list(evidence['changes']), env)
    target_tree = _sha(repository, pr['base'] + '^{tree}', env)
    splits = []
    for split in value['splits']:
        touching = [record for record in evidence['records'] if set(record['paths']) & set(split['paths'])]
        tree = _tree(repository, target_tree, {path: final.get(path) for path in split['paths']}, env)
        commit = _commit(repository, tree, pr['base'], split['message'], touching[-1]['author'],
                         evidence['identity'], env)
        splits.append({'id': split['id'], 'branch': split['branch'], 'commit': commit, 'paths': split['paths']})
    author = {**evidence['identity'], 'date': evidence['records'][-1]['author']['date']}
    source_tree = _tree(repository, evidence['tree'], {path: at_base.get(path) for path in moved}, env)
    source = _commit(repository, source_tree, head, value['source_message'], author, evidence['identity'], env)
    # Every changed path is kept with its final state or moved to exactly one split that has it.
    kept = _states(repository, source, list(evidence['changes']), env)
    for path in evidence['changes']:
        expected = at_base.get(path) if path in moved else final.get(path)
        require(kept.get(path) == expected, 'verification', f'{path} is not preserved in the source pull request')
    for item in splits:
        changed = _diff(repository, pr['base'], item['commit'], env)
        require(set(changed) == set(item['paths']) and all(changed[path][1] == final.get(path) for path in changed),
                'verification', 'split ' + item['id'] + ' does not hold exactly its moved work')
    return {'splits': splits, 'source': source, 'moved': moved}


# -- Ledger -------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim TEXT NOT NULL UNIQUE,
    entry TEXT NOT NULL,
    binding TEXT NOT NULL,
    session TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE rejections (
    claim TEXT PRIMARY KEY,
    entry TEXT NOT NULL,
    binding TEXT NOT NULL,
    session TEXT,
    error TEXT NOT NULL
);
CREATE TABLE operations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    entry TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('applying', 'applied')),
    value TEXT NOT NULL,
    report TEXT
)
"""


def ledger_path(state_directory, logical_run):
    """The logical run's splitting record next to its run ledger; run text never forms a path."""
    name = hashlib.sha256(text(logical_run, 'logical_run').encode()).hexdigest()
    return Path(state_directory) / 'runs' / (name + '.splitting.sqlite')


class Ledger:
    """Append-only session results and ledgered local Git operations of one logical run and PR."""

    def __init__(self, path, *, logical_run, target):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not self.path.is_symlink(), 'ledger', 'splitting ledger must not be a symlink')
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        expected = {'suite': admission.SUITE, 'kind': KIND, 'logical_run': logical_run, 'target': target.lower()}
        try:
            with self.transaction():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = self.db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                if version == 0 and tables == 0:
                    for statement in SCHEMA.split(';'):
                        self.db.execute(statement)
                    self.db.executemany('INSERT INTO meta VALUES (?, ?)', sorted(expected.items()))
                    self.db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                    version = SCHEMA_VERSION
                require(version == SCHEMA_VERSION, 'ledger', f'unsupported splitting ledger version {version}')
                meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
                require(meta == expected, 'ledger', 'splitting ledger belongs to another suite, logical run or target')
        except (AdmissionError, sqlite3.DatabaseError):
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()

    @staticmethod
    def _load(row, *keys):
        if row is None:
            return None
        value = dict(row)
        for key in keys:
            value[key] = json.loads(value[key]) if value[key] is not None else None
        return value

    def result(self, binding):
        return self._load(self.db.execute('SELECT * FROM results WHERE binding = ? ORDER BY seq DESC LIMIT 1',
                                          (binding,)).fetchone(), 'value')

    def claimed(self, claim):
        return self._load(self.db.execute('SELECT * FROM results WHERE claim = ?', (claim,)).fetchone(), 'value')

    def attempts(self, binding):
        return self.db.execute('SELECT count(*) FROM rejections WHERE binding = ?', (binding,)).fetchone()[0]

    def reject(self, *, claim, entry, binding, session, error):
        """Retain why a claim's result was not accepted; the retained claim is never redispatched."""
        self.db.execute('INSERT OR IGNORE INTO rejections VALUES (?, ?, ?, ?, ?)',
                        (claim, entry, binding, session, admission.canonical(error).decode()))

    def record(self, *, claim, entry, binding, session, result_ref, value):
        row = (claim, entry, binding, session, result_ref, admission.canonical(value).decode())
        with self.transaction():
            old = self.db.execute('SELECT * FROM results WHERE claim = ?', (claim,)).fetchone()
            if old is None:
                self.db.execute('INSERT INTO results (claim, entry, binding, session, result_ref, value) '
                                'VALUES (?, ?, ?, ?, ?, ?)', row)
            elif tuple(old)[1:] != row:
                raise Blocked('ledger', 'the claim already holds another result')
        return self.claimed(claim)

    def operation(self, ident):
        return self._load(self.db.execute('SELECT * FROM operations WHERE id = ?', (ident,)).fetchone(),
                          'value', 'report')

    def latest(self, entry):
        return self._load(self.db.execute('SELECT * FROM operations WHERE entry = ? ORDER BY seq DESC LIMIT 1',
                                          (entry,)).fetchone(), 'value', 'report')

    def operations(self, entry):
        return [self._load(row, 'value', 'report') for row in self.db.execute(
            'SELECT * FROM operations WHERE entry = ? ORDER BY seq DESC', (entry,))]

    def begin(self, ident, entry, value):
        """Record the exact ref updates before any of them happens."""
        with self.transaction():
            if self.db.execute('SELECT 1 FROM operations WHERE id = ?', (ident,)).fetchone() is None:
                self.db.execute("INSERT INTO operations (id, entry, state, value) VALUES (?, ?, 'applying', ?)",
                                (ident, entry, admission.canonical(value).decode()))
        return self.operation(ident)

    def settle(self, ident, report):
        with self.transaction():
            self.db.execute("UPDATE operations SET state = 'applied', report = ? WHERE id = ? AND state = 'applying'",
                            (admission.canonical(report).decode(), ident))
        return self.operation(ident)


def source_validator(ledger, name):
    """Publication-source check: only the entry's latest applied local operation is accepted evidence."""
    def valid(source):
        latest = ledger.latest(name)
        return latest is not None and latest['state'] == 'applied' and source == 'splitting:' + latest['id']
    return valid


# -- Sessions -----------------------------------------------------------------------------------

def _session(name, admitted, task, governing, evidence, records, *, number, binding, ledger, transport, check, env):
    """(row, session) of an accepted result for binding, reusing it with no session; or raises Blocked."""
    known = ledger.result(binding)
    if known is not None:
        return known, False
    rejected = ledger.attempts(binding)
    allowed = 1 + admitted['snapshot']['limits']['retries_per_task']
    if rejected >= allowed:
        raise Blocked('attempts', f'{rejected} rejected results for unchanged inputs; change the inputs or grant '
                                  'a continuation')
    run = admitted['snapshot']['request']['logical_run']
    claim = 'split-' + digest({'run': run, 'entry': name, 'binding': binding})[:32] + (
        '' if rejected == 0 else f'-r{rejected}')
    known = ledger.claimed(claim)
    if known is not None:
        return known, False
    native = assignment(name, admitted, task, governing, evidence, records, claim=claim, number=number, env=env)
    try:
        reference, session_id, output = configuration.run_session(transport, native)
    except configuration.Blocked as error:
        if error.path == 'session':  # Lost or failed dispatch: the same claim reconciles it later.
            raise Blocked('session', error.message) from None
        session_id, failure = None, _failure(error)
    else:
        try:
            value, failure = check(output), None
        except (AdmissionError, configuration.Blocked) as error:
            failure = _failure(error)
        except Exception as error:
            failure = {'path': 'result', 'message': 'not accepted: ' + type(error).__name__}
    if failure is not None:
        ledger.reject(claim=claim, entry=name, binding=binding, session=session_id, error=failure)
        raise Blocked(failure['path'], failure['message'])
    return ledger.record(claim=claim, entry=name, binding=binding, session=session_id, result_ref=reference,
                         value=value), True


def _prepare(name, admitted, options):
    require(admitted.get('preflight') == 'admitted', 'admission', 'splitting needs an admitted run')
    return occurrence(name, admitted), effective(name, admitted, options)


def _resume(name, ledger, repository, capture):
    """The latest local operation when the checkout or PR still shows it; reconciles an interrupted one.

    Returns None when the checkout moved on to unrelated history.
    """
    latest = ledger.latest(name)
    if latest is None:
        return None
    value = latest['value']
    pins = {value['original'], value['final']}
    local = _sha(repository, 'HEAD')
    remote = ((capture.get('metadata') or {}).get('head') or {}).get('sha')
    if local not in pins or remote not in pins:
        return None
    if latest['state'] == 'applied' and local == value['original']:
        raise Blocked('checkout', 'the branch was reset to its original head after the recorded rewrite; restore '
                                  f"{value['branch']} to {value['final']} or start a new logical run")
    if latest['state'] == 'applying':
        _apply(repository, ledger, latest['id'], value)
        latest = ledger.settle(latest['id'], latest['value']['report'])
    return {**latest['report'], 'reused': True}


def _apply(repository, ledger, ident, value):
    """Apply the recorded ref updates by compare-and-swap; a ref already at its value is settled."""
    ledger.begin(ident, value['entry'], value)
    for item in value['refs']:
        current = _ref(repository, item['ref'])
        if current == item['new']:
            continue
        if current != item['old']:
            raise Blocked('refs', f"{item['ref']} moved to {current}; reconcile it before continuing")
        git(repository, 'update-ref', '-m', 'cf-workflows ' + value['entry'], item['ref'], item['new'],
            item['old'] or '0' * len(item['new']))
    if value.get('moved'):
        _sync(repository, value)


def _sync(repository, value):
    """Bring only the moved paths of the index and worktree to the new source commit."""
    final = _states(repository, value['final'], value['moved'])
    original = _states(repository, value['original'], value['moved'])
    for path in value['moved']:
        file = Path(repository) / path
        if file.is_symlink():  # The tree holds the link text; hash-object on the path follows the link.
            blob = git(repository, 'hash-object', '--stdin', data=os.fsencode(os.readlink(file))).decode().strip()
        else:
            blob = git(repository, 'hash-object', '--', path).decode().strip() if file.is_file() else None
        if blob not in ((original.get(path) or [None, None])[1], (final.get(path) or [None, None])[1]):
            raise Blocked('collision', f'{path} changed in the checkout during the split; reconcile it')
    keep = [path for path in value['moved'] if path in final]
    if keep:
        git(repository, 'checkout', '-q', value['final'], '--', *keep)
    for path in value['moved']:
        if path not in final:
            git(repository, 'rm', '-q', '--cached', '--ignore-unmatch', '--', path)
            (Path(repository) / path).unlink(missing_ok=True)


def _local(local, where):
    return None if local is None else _grant(local, where)


def _header(name, evidence):
    return {'skill': ENTRIES[name]['skill'], 'pr': evidence['pr']['url'],
            'reviewed': {'head': evidence['pr']['head'], 'tree': evidence['tree']},
            'target': evidence['pr']['base'], 'base_ref': evidence['pr']['base_ref'],
            'head_repository': evidence['pr']['head_repository'], 'merge_base': evidence['merge_base'],
            'branch': evidence['branch']}


# -- dk-commit-split ----------------------------------------------------------------------------

def organize(admitted, *, repository, capture, ledger, transport, rewrite=None, owners=(), options=None):
    """Assess every commit and, with a local `rewrite` authorization, reorganize the series.

    admitted is the entry's preflight or a composed plan selecting `inspect-commits`.
    Outcomes: `no_op` (every commit assessed, nothing to change), `assessed` (a
    verified series without local authority: nothing written), `prepared` (the
    branch now points at the verified series; a recovery ref keeps the original),
    `blocked` and `rejected`. Writes no index, worktree or remote.
    """
    name = 'dk-commit-split'
    skill = ENTRIES[name]['skill']
    try:
        task, governing = _prepare(name, admitted, options)
        grant = _local(rewrite, 'rewrite')
    except AdmissionError as error:
        return {'skill': skill, 'status': 'rejected', 'error': _failure(error)}
    try:
        resumed = _resume(name, ledger, repository, capture)
        if resumed is not None:
            return resumed
        evidence = collect(name, admitted, repository=repository, capture=capture, owners=owners, options=options)
        evidence['repository'] = str(repository)
        context = _context(name, admitted, task, governing, evidence)
        report = _header(name, evidence)
        with _objects(repository, grant is not None) as env:
            records, head, passes = evidence['records'], evidence['pr']['head'], []
            for number in range(1, governing['options']['max_passes'] + 1):
                binding = digest({'context': context, 'series': [item['sha'] for item in records], 'pass': number})
                row, ran = _session(name, admitted, task, governing, evidence, records, number=number,
                                    binding=binding, ledger=ledger, transport=transport, env=env,
                                    check=lambda output, records=records: check_commits(output, evidence, records,
                                                                                        governing))
                value = row['value']
                step = {'pass': number, 'claim': row['claim'], 'session': ran, 'result_ref': row['result_ref'],
                        'input': head, 'outcome': value['outcome'], 'assessments': value['assessments'],
                        'summary': value['summary'], 'questions': value['questions']}
                passes.append(step)
                if value['outcome'] == 'no_change':
                    break
                new_records, new_head = build_series(repository, evidence, records, value, env)
                step.update(output=new_head, series=[{'commit': item['sha'], 'units': group['units'],
                                                      'reason': group['reason']}
                                                     for item, group in zip(new_records[-len(value['series']):],
                                                                            value['series'])])
                if new_head == head:
                    step['outcome'] = 'no_change'
                    break
                records, head = new_records, new_head
            concerns = [] if passes[-1]['outcome'] == 'no_change' else [
                f"stopped after max_passes={governing['options']['max_passes']} with the series still changing"]
            report.update(passes=passes, concerns=concerns,
                          final={'head': head, 'tree': _sha(repository, head + '^{tree}', env)},
                          commits=[{'sha': item['sha'], 'author': item['author'],
                                    'subject': item['message'].split('\n', 1)[0]} for item in records],
                          fixed=[item['sha'] for item in evidence['records'][:_fixed(evidence['records'],
                                                                                      set(evidence['owners']))]],
                          signed=[item['sha'] for item in evidence['records'] if item['signed']])
            if report['final']['tree'] != evidence['tree']:
                raise Blocked('series', 'the final tree differs from the reviewed tree')
            if head == evidence['pr']['head']:
                return {**report, 'status': 'no_op'}
            if grant is None:
                return {**report, 'status': 'assessed', 'note': 'nothing was written; a local rewrite needs its '
                                                               'own authorization'}
            ident = 'commit-split-' + digest({'run': admitted['snapshot']['request']['logical_run'],
                                              'from': evidence['pr']['head'], 'to': head})[:32]
            recovery = RECOVERY + ident
            report.update(status='prepared', operation=ident, recovery=recovery, authorization=grant,
                          mapping=passes, dirty=evidence['dirty'])
            value = {'entry': name, 'branch': evidence['branch'], 'original': evidence['pr']['head'], 'final': head,
                     'refs': [{'ref': recovery, 'old': None, 'new': evidence['pr']['head']},
                              {'ref': evidence['branch'], 'old': evidence['pr']['head'], 'new': head}],
                     'report': report}
            _apply(repository, ledger, ident, value)
            ledger.settle(ident, report)
            return report
    except (AdmissionError, configuration.Blocked) as error:
        return {'skill': skill, 'status': 'blocked', 'error': _failure(error)}


# -- dk-pr-split --------------------------------------------------------------------------------

def plan_digest(value):
    """The concrete split a manual-mode approval names."""
    return digest({key: value[key] for key in ('splits', 'source_message')})


def partition(admitted, *, repository, capture, ledger, transport, rewrite=None, approval=None, owners=(),
              options=None):
    """Assess the PR's scope and, with a local `rewrite` authorization, prepare the split locally.

    admitted is the entry's preflight or a composed plan selecting `assess-scope`.
    Outcomes: `kept` (every path assessed; the work stays), `assessed` (a verified
    split without local authority), `proposed` (a manual-mode split waiting for an
    `approval` naming its plan digest), `prepared` (split branches on the target,
    a source commit without the moved work, a recovery ref, and only the moved
    paths of the checkout updated), `blocked` and `rejected`. Writes no remote.
    """
    name = 'dk-pr-split'
    skill = ENTRIES[name]['skill']
    try:
        task, governing = _prepare(name, admitted, options)
        grant = _local(rewrite, 'rewrite')
    except AdmissionError as error:
        return {'skill': skill, 'status': 'rejected', 'error': _failure(error)}
    try:
        resumed = _resume(name, ledger, repository, capture)
        if resumed is not None:
            return resumed
        evidence = collect(name, admitted, repository=repository, capture=capture, owners=owners, options=options)
        evidence['repository'] = str(repository)
        context = _context(name, admitted, task, governing, evidence)
        threshold = governing['options']['added_lines_threshold']
        report = {**_header(name, evidence), 'size': {'added_lines': evidence['added_lines'], 'threshold': threshold,
                                                      'oversized': evidence['added_lines'] > threshold},
                  'modes': {key: governing['options'][key] for key in MODE_OPTION.values()}}
        binding = digest({'context': context, 'series': [item['sha'] for item in evidence['records']],
                          'size': report['size']})
        with _objects(repository, grant is not None) as env:
            row, ran = _session(name, admitted, task, governing, evidence, evidence['records'], number=1,
                                binding=binding, ledger=ledger, transport=transport, env=env,
                                check=lambda output: check_split(output, evidence, governing))
            value = row['value']
            report.update(claim=row['claim'], session=ran, result_ref=row['result_ref'], outcome=value['outcome'],
                          assessments=value['assessments'], summary=value['summary'], questions=value['questions'])
            if value['outcome'] == 'keep':
                return {**report, 'status': 'kept'}
            built = build_split(repository, evidence, value, env)
            plan = plan_digest(value)
            report.update(plan=plan, splits=[{**split, **{key: item[key] for key in ('commit',)}}
                                             for split, item in zip(value['splits'], built['splits'])],
                          source={'head': built['source'], 'message': value['source_message']},
                          moved=built['moved'])
            manual = sorted({split['kind'] for split in value['splits']
                             if governing['options'][MODE_OPTION[split['kind']]] == 'manual'})
            if manual:
                try:
                    approved = _grant({key: item for key, item in (approval or {}).items() if key != 'plan'},
                                      'approval') if approval is not None else None
                except AdmissionError as error:
                    return {'skill': skill, 'status': 'rejected', 'error': _failure(error)}
                if approved is None or approval.get('plan') != plan:
                    return {**report, 'status': 'proposed', 'note': 'manual mode for ' + ', '.join(manual)
                            + ' work needs an approval naming this plan digest; nothing was written'}
                report['approval'] = {**approved, 'plan': plan}
            if grant is None:
                return {**report, 'status': 'assessed', 'note': 'nothing was written; a local split needs its own '
                                                               'authorization'}
            dirty = sorted(set(evidence['dirty']) & set(built['moved']))
            if dirty:
                raise Blocked('collision', 'moved paths have uncommitted changes: ' + ', '.join(dirty[:5]))
            taken = [split['branch'] for split in built['splits'] if _ref(repository, 'refs/heads/' + split['branch'])]
            if taken:
                raise Blocked('collision', 'split branches already exist locally: ' + ', '.join(taken))
            ident = 'pr-split-' + digest({'run': admitted['snapshot']['request']['logical_run'],
                                          'from': evidence['pr']['head'], 'plan': plan})[:32]
            recovery = RECOVERY + ident
            report.update(status='prepared', operation=ident, recovery=recovery, authorization=grant,
                          dirty=evidence['dirty'])
            refs = [{'ref': recovery, 'old': None, 'new': evidence['pr']['head']}]
            refs += [{'ref': 'refs/heads/' + split['branch'], 'old': None, 'new': split['commit']}
                     for split in built['splits']]
            refs.append({'ref': evidence['branch'], 'old': evidence['pr']['head'], 'new': built['source']})
            op = {'entry': name, 'branch': evidence['branch'], 'original': evidence['pr']['head'],
                  'final': built['source'], 'refs': refs, 'moved': built['moved'], 'report': report}
            _apply(repository, ledger, ident, op)
            ledger.settle(ident, report)
            return report
    except (AdmissionError, configuration.Blocked) as error:
        return {'skill': skill, 'status': 'blocked', 'error': _failure(error)}


# -- Commit on request --------------------------------------------------------------------------

def commit(admitted, report, *, repository, drafting_ledger, ledger, authorization, options=None):
    """Commit exactly the included paths of a current dk-commit-message draft when asked to.

    authorization is the user's explicit local commit request. The draft must be
    the retained one with unchanged evidence; otherwise it is `stale` and nothing
    is written. Excluded and unrelated changes stay as they are, staged or not.
    """
    try:
        grant = _grant(authorization, 'authorization')
        require(report.get('status') == 'drafted' and report.get('result_ref'), 'report', 'no accepted draft')
        include = report['draft']['scope']['included']
    except (AdmissionError, KeyError, TypeError) as error:
        return {'status': 'rejected', 'error': _failure(error) if isinstance(error, AdmissionError)
                else {'path': 'report', 'message': 'not a commit-message report'}}
    try:
        head = _sha(repository, 'HEAD')
        require(head, 'checkout', 'a commit to build on is required')
        ident = 'commit-' + digest({'run': admitted['snapshot']['request']['logical_run'],
                                    'draft': report['result_ref'], 'head': head})[:32]
        # Only this draft's own operation is reused: one that made HEAD, or an interrupted one from HEAD.
        for row in ledger.operations('dk-commit-message'):
            if row['value']['report']['draft'] != report['result_ref']:
                continue
            if row['value']['final'] == head or row['value']['original'] == head and row['state'] == 'applying':
                _apply(repository, ledger, row['id'], row['value'])
                _index(repository, row['value'])
                return ledger.settle(row['id'], row['value']['report'])['report'] | {'reused': True}
        current = drafting.current_report('dk-commit-message', admitted, repository=repository,
                                          ledger=drafting_ledger, options=options)
        if current is None or current['result_ref'] != report['result_ref']:
            return {'status': 'stale', 'error': {'path': 'report', 'message': 'the changes or governing inputs '
                                                                              'differ from the draft; draft again'}}
        branch = git(repository, 'symbolic-ref', '-q', 'HEAD', check=False).decode().strip() or 'HEAD'
        with tempfile.TemporaryDirectory(prefix='cf-index-') as scratch:
            env = {'GIT_INDEX_FILE': str(Path(scratch) / 'index')}
            git(repository, 'read-tree', head, env=env)
            git(repository, 'update-index', '--add', '--remove', '--', *include, env=env)
            tree = git(repository, 'write-tree', env=env).decode().strip()
        changed = set(_diff(repository, head, tree))
        require(changed == set(include), 'scope', 'the commit would differ from the included paths')
        identity = _identity(repository)
        when = f'{int(time.time())} +0000'  # Recorded with the operation; a resume reuses its commit.
        new = _commit(repository, tree, head, report['draft']['message'], {**identity, 'date': when}, identity, {})
        result = {'skill': 'cf-workflows:dk-commit-message', 'status': 'committed', 'commit': new, 'parent': head,
                  'included': sorted(include), 'excluded': report['draft']['scope']['excluded'],
                  'authorization': grant, 'draft': report['result_ref']}
        value = {'entry': 'dk-commit-message', 'branch': branch, 'original': head, 'final': new, 'include': include,
                 'refs': [{'ref': branch, 'old': head, 'new': new}], 'report': result}
        _apply(repository, ledger, ident, value)
        _index(repository, value)
        ledger.settle(ident, result)
        return result
    except (AdmissionError, configuration.Blocked) as error:
        return {'status': 'blocked', 'error': _failure(error)}


def _index(repository, value):
    """The committed paths' index entries follow the commit; other staged entries are kept."""
    git(repository, 'reset', '-q', value['final'], '--', *value['include'])


# -- Publication --------------------------------------------------------------------------------

def publication_plan(name, admitted):
    """The shared publication plan: only the entry's actions, and only when effective.

    A rewritten series publishes as the `rewrite` kind under `push`; split PRs as
    `split_push` and `split_pr` under `create_pr`, then the source as `push`.
    """
    snapshot = admitted['snapshot']
    request = snapshot['request']
    enabled = {action: snapshot['permissions'][action]['effective'] for action in ENTRIES[name]['actions']}
    permissions = ({'rewrite_commits': enabled['push']} if name == 'dk-commit-split' else
                   {'create_draft_prs': enabled['create_pr'], 'push_commits': enabled['push']})
    pr = request['pr']
    host, owner, repo = pr['repository'].split('/')
    return {'logical_run': request['logical_run'], 'instructions': request['original_instructions'],
            'authorizations': {action: request['authorizations'][action] for action in enabled if enabled[action]},
            'options': {'permissions': permissions},
            'pr': {'url': pr['url'], 'host': host, 'owner': owner, 'name': repo, 'repository': pr['repository'],
                   'number': pr['number']}}


def proposals(name, admitted, report):
    """The exact ordered publication proposals of a prepared result; IDs bind the run and operation."""
    run = admitted['snapshot']['request']['logical_run']
    source = ['splitting:' + report['operation']]
    head, target = report['reviewed']['head'], report['target']

    def ident(kind, part=''):
        return f'{kind}-' + digest({'run': run, 'operation': report['operation'], 'part': part})[:32]
    if name == 'dk-commit-split':
        new = report['final']['head']
        return [{'id': ident('rewrite'), 'kind': 'rewrite', 'head': new, 'lease': head, 'target': target,
                 'payload': {'new_head': new}, 'sources': source}]
    owner = report['head_repository'].split('/')[0]
    result = []
    for split in report['splits']:
        push = ident('split-push', split['id'])
        result.append({'id': push, 'kind': 'split_push', 'head': split['commit'], 'lease': head, 'target': target,
                       'payload': {'branch': split['branch'], 'new_head': split['commit'], 'paths': split['paths'],
                                   'reason': split['reason']}, 'sources': source})
        result.append({'id': ident('split-pr', split['id']), 'kind': 'split_pr', 'head': head, 'target': target,
                       'payload': {'title': split['title'], 'body': split['body'], 'head': owner + ':' + split['branch'],
                                   'base': report['base_ref'], 'draft': True, 'push_operation': push},
                       'sources': source})
    new = report['source']['head']
    result.append({'id': ident('split-source'), 'kind': 'push', 'head': new, 'lease': head, 'target': target,
                   'payload': {'new_head': new}, 'sources': source})
    return result


def publish(name, admitted, report, *, publisher, audience):
    """Publish a prepared result through the shared exact-intent boundary, in order, when authorized.

    publisher is the shared publication helper (prfix.publication.Publication)
    built from publication_plan() with source_validator(ledger, name); audience(intent)
    returns the independent audience assessment of the exact intent. Split PRs
    publish before the source loses their work, and the source push needs every
    split applied. A pending intent is only reconciled, never resent; the first
    step that is not applied stops the run, and a rerun continues from it.
    """
    if report.get('status') != 'prepared':
        return {'status': 'nothing' if report.get('status') in ('no_op', 'kept') else 'blocked',
                'error': None if report.get('status') in ('no_op', 'kept') else
                {'path': 'report', 'message': 'only a locally prepared result is published'}}
    enabled = publication_plan(name, admitted)['options']['permissions']
    if not all(enabled.values()):
        return {'status': 'not_authorized', 'needs': sorted(key for key, value in enabled.items() if not value)}
    steps = []
    for proposal in proposals(name, admitted, report):
        retained = decisions._journal_state(publisher, proposal['id'])
        try:
            if retained in ('pending', 'applied'):
                # A dispatched intent is only reconciled from what the service shows, never sent again.
                receipt = publisher.execute(proposal['id'], None)
            else:
                intent = publisher.prepare(proposal)
                receipt = publisher.execute(intent['id'], audience(intent))
        except Exception as error:
            state = decisions._journal_state(publisher, proposal['id'])
            steps.append({'id': proposal['id'], 'kind': proposal['kind'],
                          'status': {'prepared': 'blocked', None: 'blocked'}.get(state, state),
                          'error': {'path': 'publication', 'message': str(error) or type(error).__name__}})
            return {'status': steps[-1]['status'], 'steps': steps}
        steps.append({'id': proposal['id'], 'kind': proposal['kind'], 'status': 'applied', 'receipt': receipt})
    return {'status': 'applied', 'steps': steps}
