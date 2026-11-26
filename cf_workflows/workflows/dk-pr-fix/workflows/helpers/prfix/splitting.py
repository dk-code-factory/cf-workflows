"""The splitting procedure of inspect-commits and assess-scope (installed side).

cf-workflows:dk-commit-split and dk-pr-split are presets over this workflow
(workflows/commit-split.yml, pr-split.yml). Their adapter occurrence runs
through the ordinary task path (task.yml, native.yml) as one engine-managed,
read-only session with the admitted leaf role and no skills. prepare marks it
with `splitting: <entry>`; a composed dk-pr-fix plan runs both adapters as
ordinary read-only tasks. These helpers add the procedure to that path:

- `context` collects read-only evidence for the claim (assignment['splitting']):
  every commit from the merge base with its author, message, trailers, paths
  and bounded patch, the final diff, the added-line count, the owned authors
  and the commits that are never rewritten.
- `check` runs at acceptance: the report proposes no operations and returns its
  result in deliverables.split, which passes the library's result checks.
- In the publication stage (workflows/splitting.yml), `build` verifies and
  builds the accepted series or split as objects under the explicit local
  `rewrite` grant, `validate` runs the configured validation commands on each
  new intermediate commit in a private checkout, and `apply` moves the refs by
  ledgered compare-and-swap only when every commit passed.
- `listing` gives the exact ordered publication intents of an applied operation
  (`rewrite`, or `split_push`, `split_pr` and the source `push`) under enabled
  and explicitly authorized permissions, for the shared publication stage.

The constants, Git plumbing, result checks and builds between the markers are a
verbatim copy of cf_workflows/splitting.py; tests/test_dk_pr_fix_splitting.py
keeps the two identical. Nothing here starts a model or writes to the remote.
"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from . import drafting
from .core import auth, canonical, digest, ledger_path, read_artifact, state_directory

KIND = 'splitting_context'
STORE_KIND = 'split_operations_state'
STORE_VERSION = 1
MAX_TEXT = 64 << 10
# Validation commands: bounded as dk-pr-feedback-address's (prepare.VALIDATION_LIMITS; admission rejects provider
# CLIs), each with a bounded runtime and retained output.
VALIDATION_TIMEOUT = 600
VALIDATION_OUTPUT = 4096
# Never handed to a validation command: the helper's GitHub credentials and state mount.
PRIVATE_ENV = ('GH_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_TOKEN', 'CF_WORKFLOWS_STATE')

# The baseline role prompts, adapters, actions and options of each entry (cf_workflows/splitting.py ENTRIES).
ENTRIES = {
    'dk-commit-split': {
        'adapter': 'inspect-commits', 'actions': ('push',), 'options': {'policy': None, 'max_passes': 5},
        'permissions': {'rewrite': 'rewrite_commits'},
        'role_prompt': 'Review and organize all PR commits while preserving the reviewed final tree, authorship and '
                       'logical commit boundaries.\n',
    },
    'dk-pr-split': {
        'adapter': 'assess-scope', 'actions': ('create_pr', 'push'),
        'options': {'added_lines_threshold': 1000, 'evacuate_unrelated': 'automatic', 'split_related': 'automatic'},
        'permissions': {'split_push': 'create_draft_prs', 'split_pr': 'create_draft_prs', 'push': 'push_commits'},
        'role_prompt': 'Move unrelated work or split large changes into separate PRs under saved size, scope and '
                       'publication policies.\n',
    },
}
CONTRACT = (
    'Return the result in deliverables.split and propose no operations or changes: the helper verifies it, '
    'builds it only under a separate local grant and publishes it only under separate authority. Do not write, '
    'commit or publish. For inspect-commits the result is {outcome: no_change | reorganize, assessments: '
    '[{commit, reason}] for every commit, series: [{message, units: [{commit, paths}], reason}], summary, '
    'questions}: units use every path of every rewritable commit exactly once and keep the order of changes to a '
    'path, a new commit has one author and keeps exactly its source trailers, and commits marked fixed are never '
    'rewritten. For assess-scope it is {outcome: keep | split, assessments: [{path, scope: core | unrelated | '
    'related, reason}] for every changed path, splits: [{id, kind, paths, branch, title, body, message, reason}], '
    'source_message, summary, questions}: core work stays, a moved path has its split\'s kind and no other '
    'author\'s work, the body links back to the source pull request and messages add no tool attribution.')


class Blocked(ValueError):
    """The evidence, a result or a Git precondition cannot be used; nothing is applied."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


AdmissionError = Blocked


def require(condition, path, message):
    if not condition:
        raise Blocked(path, message)


def text(value, path, limit=MAX_TEXT):
    require(isinstance(value, str) and value.strip() and '\0' not in value,
            path, 'must be a nonempty string without NUL')
    try:
        size = len(value.encode('utf-8', 'strict'))
    except UnicodeEncodeError:
        raise Blocked(path, 'must be valid Unicode text') from None
    require(size <= limit, path, 'exceeds byte bound')
    return value


def fields(value, path, required, optional=()):
    require(isinstance(value, dict), path, 'must be an object')
    unknown = set(value) - set(required) - set(optional)
    require(not unknown, path, 'unsupported field(s): ' + ', '.join(sorted(map(str, unknown))))
    missing = set(required) - set(value)
    require(not missing, path, 'missing field(s): ' + ', '.join(sorted(missing)))
    return value


# -- Verbatim copy of cf_workflows/splitting.py (constants, Git plumbing, result checks, builds) --

MAX_COMMITS = 100
MAX_PATHS = 1000
MAX_PATCH = 256 << 10
MODES = ('automatic', 'manual', 'disabled')
SCOPES = ('core', 'unrelated', 'related')
MODE_OPTION = {'unrelated': 'evacuate_unrelated', 'related': 'split_related'}
RECOVERY = 'refs/cf-workflows/recovery/'
_TRAILER = re.compile(r'^[A-Za-z][A-Za-z-]*-by:[ \t]*\S.*$', re.M)
_AUTHOR = re.compile(r'(.*) <([^<>]*)> ([0-9]+ [+-][0-9]{4})')
_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
_BRANCH = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}')


def git(repository, *args, data=None, env=None, check=True):
    """Git with filesystem monitors, hooks, external diffs and lazy fetches off; env adds variables."""
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
                             '--literal-pathspecs', '-C', str(repository), *args],
                            input=data, capture_output=True, timeout=60,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
                                 'GIT_TERMINAL_PROMPT': '0', **(env or {})})
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


def _patch(repository, records, env):
    """Bounded per-commit patches for the session; a cut is flagged, never silent."""
    remaining, result = MAX_PATCH, {}
    for record in records:
        patch = git(repository, 'show', '--format=', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames',
                    record['sha'], env=env).decode('utf-8', 'replace')
        result[record['sha']] = {'patch': patch[:max(remaining, 0)], 'truncated': len(patch) > remaining}
        remaining -= len(patch)
    return result


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
        require(isinstance(branch, str) and _BRANCH.fullmatch(branch) and '..' not in branch
                and not branch.endswith(('/', '.', '.lock')) and branch not in branches
                and branch not in (evidence['pr']['head_ref'], evidence['pr']['base_ref']), where + '.branch',
                'a new distinct branch name')
        branches.add(branch)
        require(isinstance(item['paths'], list) and item['paths'], where + '.paths', 'name the moved paths')
        for path in item['paths']:
            require(path in changes and path not in moved, where + '.paths', f'{path} is not a changed path or moves twice')
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


def plan_digest(value):
    """The concrete split a manual-mode approval names."""
    return digest({key: value[key] for key in ('splits', 'source_message')})


def _sync(repository, value):
    """Bring only the moved paths of the index and worktree to the new source commit."""
    final = _states(repository, value['final'], value['moved'])
    original = _states(repository, value['original'], value['moved'])
    for path in value['moved']:
        file = Path(repository) / path
        present = file.is_file() or file.is_symlink()
        blob = git(repository, 'hash-object', '--', path).decode().strip() if present else None
        if blob not in ((original.get(path) or [None, None])[1], (final.get(path) or [None, None])[1]):
            raise Blocked('collision', f'{path} changed in the checkout during the split; reconcile it')
    keep = [path for path in value['moved'] if path in final]
    if keep:
        git(repository, 'checkout', '-q', value['final'], '--', *keep)
    for path in value['moved']:
        if path not in final:
            git(repository, 'rm', '-q', '--cached', '--ignore-unmatch', '--', path)
            (Path(repository) / path).unlink(missing_ok=True)

# -- End of the verbatim copy -------------------------------------------------------------------

def role(task):
    """The splitting entry whose procedure this occurrence runs, or None (a composed plan's ordinary task)."""
    name = task.get('splitting')
    return name if name in ENTRIES and task.get('skill') == ENTRIES[name]['adapter'] else None


def occurrence(plan):
    """The plan's splitting occurrence, or None."""
    tasks = [task for task in plan.get('tasks', []) if role(task)]
    require(len(tasks) <= 1, 'plan.tasks', 'a splitting entry runs its adapter exactly once')
    return tasks[0] if tasks else None


def _commands(raw, where):
    from .prepare import validation_commands
    try:
        commands = validation_commands(raw)
    except ValueError as error:
        raise Blocked(where, str(error)) from None
    return commands


def settings(plan, name):
    """(options, extra) of an entry from ``settings.splitting[name]``; baseline values by default.

    Options follow the entry's config.schema.json. A commit policy runs only
    through the library procedure, which admits it as a validated prompt. extra
    holds the explicit local ``rewrite`` grant, further owned author emails
    (``owners``) and the ``validation_commands`` every new intermediate commit runs.
    """
    raw = (plan.get('settings') or {}).get('splitting', {})
    require(isinstance(raw, dict) and set(raw) <= set(ENTRIES), 'settings.splitting',
            'supports ' + ', '.join(sorted(ENTRIES)) + ' only')
    given = raw.get(name, {})
    where = 'settings.splitting.' + name
    declared = ENTRIES[name]['options']
    allowed = (set(declared) - {'policy'}) | {'rewrite', 'owners', 'validation_commands'}
    require(isinstance(given, dict) and set(given) <= allowed, where,
            'supports the entry options, rewrite, owners and validation_commands only; a commit policy runs '
            'only through the library procedure')
    values = copy.deepcopy(declared)
    extra = {'rewrite': None, 'owners': [], 'validation_commands': []}
    for key, value in given.items():
        path = where + '.' + key
        if key == 'max_passes':
            require(type(value) is int and 1 <= value <= 5, path, 'must be an integer from 1 to 5')
        elif key == 'added_lines_threshold':
            require(type(value) is int and 1 <= value <= 10 ** 7, path, 'must be a positive integer')
        elif key in MODE_OPTION.values():
            require(value in MODES, path, 'must be one of ' + ', '.join(MODES))
        elif key == 'rewrite':
            try:
                value = auth(value)
            except (ValueError, TypeError, AttributeError):
                raise Blocked(path, 'a local rewrite needs an explicit {request_id, instruction, source} grant') from None
        elif key == 'owners':
            require(isinstance(value, list) and len(value) <= 100 and all(
                isinstance(item, str) and '@' in item and item.strip() == item and len(item) <= 256
                for item in value), path, 'must be a list of author email addresses')
        else:
            value = _commands(value, path)
        if key in declared:
            values[key] = copy.deepcopy(value)
        else:
            extra[key] = copy.deepcopy(value)
    return values, extra


def _policy(task):
    """The commit policy of the occurrence: its applicable cleanup or commit prompts."""
    texts = [item['text'] for item in task.get('prompts', []) if item.get('slot') in ('cleanup', 'commit')]
    return '\n\n'.join(texts) or None


# -- Operation store ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE operations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    entry TEXT NOT NULL,
    claim TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('built', 'applying', 'applied')),
    value TEXT NOT NULL
);
CREATE TABLE validations (
    operation TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('passed', 'failed')),
    result TEXT NOT NULL,
    PRIMARY KEY (operation, commit_id)
)
"""


def store_path(root, logical_run, create=False):
    """The logical run's split-operation record next to its run ledger; run text never forms a path."""
    directory = state_directory(root, create=create, missing_ok=not create)
    if directory is None:
        return None
    path = ledger_path(directory, logical_run)
    return path.with_name(path.name[:-len('.sqlite')] + '.split-operations.sqlite')


class Store:
    """Built, applying and applied local operations of one logical run, and their validations.

    A read-only store over a missing file is empty; nothing is created until a build.
    """

    def __init__(self, root, plan, write=False):
        self.db = None
        path = store_path(root, plan['logical_run'], create=write)
        if path is None or not write and not path.is_file():
            return
        require(not path.is_symlink(), 'store', 'split-operation store must not be a symlink')
        expected = {'suite': 'cf-workflows', 'kind': STORE_KIND, 'logical_run': plan['logical_run'],
                    'target': plan['pr']['url']}
        if write:
            path.parent.mkdir(mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path.as_uri() + ('?mode=rwc' if write else '?mode=ro'), uri=True,
                                  isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        try:
            if write:
                os.chmod(path, 0o600)
                with self.transaction():
                    version = self.db.execute('PRAGMA user_version').fetchone()[0]
                    tables = self.db.execute(
                        "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                    if version == 0 and tables == 0:
                        for statement in SCHEMA.split(';'):
                            self.db.execute(statement)
                        self.db.executemany('INSERT INTO meta VALUES (?, ?)', sorted(expected.items()))
                        self.db.execute(f'PRAGMA user_version = {STORE_VERSION}')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            require(version == STORE_VERSION, 'store', f'unsupported split-operation store version {version}')
            meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
            require(meta == expected, 'store', 'split-operation store belongs to another suite, logical run or PR')
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        if self.db is not None:
            self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    @staticmethod
    def _load(row):
        if row is None:
            return None
        value = dict(row)
        value['value'] = json.loads(value['value'])
        return value

    def get(self, ident):
        if self.db is None:
            return None
        return self._load(self.db.execute('SELECT * FROM operations WHERE id = ?', (ident,)).fetchone())

    def by_claim(self, claim):
        if self.db is None:
            return None
        return self._load(self.db.execute('SELECT * FROM operations WHERE claim = ?', (claim,)).fetchone())

    def latest(self, entry, states):
        if self.db is None:
            return None
        marks = ', '.join('?' * len(states))
        return self._load(self.db.execute(f'SELECT * FROM operations WHERE entry = ? AND state IN ({marks}) '
                                          'ORDER BY seq DESC LIMIT 1', (entry, *states)).fetchone())

    def ending_at(self, head):
        """The latest moved operation whose result is head."""
        if self.db is None:
            return None
        for row in self.db.execute("SELECT * FROM operations WHERE state IN ('applying', 'applied') "
                                   'ORDER BY seq DESC'):
            value = self._load(row)
            if value['value']['final'] == head:
                return value
        return None

    def applied(self, entry):
        if self.db is None:
            return 0
        return self.db.execute("SELECT count(*) FROM operations WHERE entry = ? AND state = 'applied'",
                               (entry,)).fetchone()[0]

    def build(self, ident, entry, claim, value):
        """Record the exact objects and ref updates before anything is validated or moved."""
        with self.transaction():
            if self.db.execute('SELECT 1 FROM operations WHERE id = ?', (ident,)).fetchone() is None:
                self.db.execute("INSERT INTO operations (id, entry, claim, state, value) VALUES (?, ?, ?, 'built', ?)",
                                (ident, entry, claim, canonical(value).decode()))
        return self.get(ident)

    def advance(self, ident, old, new):
        with self.transaction():
            self.db.execute('UPDATE operations SET state = ? WHERE id = ? AND state = ?', (new, ident, old))
        return self.get(ident)

    def validation(self, operation, commit):
        if self.db is None:
            return None
        row = self.db.execute('SELECT * FROM validations WHERE operation = ? AND commit_id = ?',
                              (operation, commit)).fetchone()
        return None if row is None else {'state': row['state'], 'result': json.loads(row['result'])}

    def validated(self, operation, commit, state, result):
        """Retain one commit's validation once; a replay keeps the original result."""
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO validations VALUES (?, ?, ?, ?)',
                            (operation, commit, state, canonical(result).decode()))
        return self.validation(operation, commit)


def origins(root, plan, head):
    """The revisions an applied local operation of this run moved the checkout from, latest first.

    capture accepts a checkout at head while the PR shows one of them: the
    recorded operation, not a caller flag, establishes the local head.
    """
    result = []
    with Store(root, plan) as store:
        while len(result) < 2 * MAX_COMMITS:
            operation = store.ending_at(head)
            if operation is None or operation['value']['original'] in result:
                break
            head = operation['value']['original']
            result.append(head)
    return result


def binding(ledger, task, snapshot):
    """dk-pr-split's coverage while its own applied split is the checkout, else None.

    The split is the result of the accepted assessment it records, so the
    occurrence stays current and is never assessed again on its own result.
    """
    if role(task) != 'dk-pr-split' or ledger is None:
        return None
    path = store_path(ledger.root, ledger.logical_run)
    if path is None or not path.is_file():
        return None
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)
    try:
        for (raw,) in db.execute("SELECT value FROM operations WHERE entry = 'dk-pr-split' "
                                     "AND state IN ('applying', 'applied') ORDER BY seq DESC"):
            value = json.loads(raw)
            if value['final'] == snapshot['local_head'] and value['task'] == task['id']:
                return value['coverage']
    finally:
        db.close()
    return None


# -- Claim and acceptance -----------------------------------------------------------------------

def context(root, provider, plan, task, snapshot):
    """The splitting context of a new claim (assignment['splitting']); reads only, writes nothing."""
    name = role(task)
    values, extra = settings(plan, name)
    local, head = snapshot['local_head'], snapshot['head']
    require(_sha(root, 'HEAD') == local, 'checkout', 'the checkout moved during the capture')
    branch = git(root, 'symbolic-ref', '-q', 'HEAD', check=False).decode().strip()
    require(branch.startswith('refs/heads/'), 'checkout', 'a branch must be checked out')
    if name == 'dk-pr-split':
        require(local == head, 'checkout', 'dk-pr-split assesses the published PR head')
    base = snapshot['base']
    records = series(root, base, local)
    require(records, 'commits', 'the pull request has no commits')
    identity = _identity(root)
    owned = sorted({identity['email'].lower(), *(item.lower() for item in extra['owners'])})
    changes = _diff(root, base, local)
    require(len(changes) <= MAX_PATHS, 'changes', f'more than {MAX_PATHS} changed paths')
    added = 0
    for line in git(root, 'diff', '--numstat', '--no-renames', '--no-ext-diff', base, local).decode(
            'utf-8', 'surrogateescape').splitlines():
        count = line.split('\t', 1)[0]
        added += int(count) if count.isdigit() else 0
    tree = _sha(root, local + '^{tree}')
    if name == 'dk-commit-split':
        # Every pass keeps the reviewed final tree; a later pass assesses the rebuilt series.
        require(tree == snapshot['head_tree'], 'tree', 'the local series no longer reproduces the reviewed tree')
    with Store(root, plan) as store:
        number = store.applied(name) + 1
    policy = _policy(task)
    evidence = {'pr': {'url': plan['pr']['url'], 'head': local, 'base': snapshot['target'],
                       'head_ref': snapshot['head_branch'], 'base_ref': snapshot['target_branch'],
                       'head_repository': snapshot['head_repository'].split('/', 1)[1]},
                'published': head, 'branch': branch, 'merge_base': base, 'records': records, 'identity': identity,
                'owners': owned, 'changes': changes, 'added_lines': added, 'tree': tree, 'dirty': []}
    fixed = _fixed(records, set(owned))
    patches = _patch(root, records, {})
    threshold = values.get('added_lines_threshold')
    session = {
        'pr': evidence['pr'], 'merge_base': base, 'pass': number,
        'commits': [{'sha': item['sha'], 'author': item['author'], 'message': item['message'],
                     'owned': _owned(item, set(owned)), 'fixed': index < fixed, 'paths': sorted(item['paths']),
                     **patches[item['sha']]} for index, item in enumerate(records)],
        'changes': sorted(changes), 'added_lines': added, 'policy': policy,
        **({'size': {'added_lines': added, 'threshold': threshold, 'oversized': added > threshold}}
           if threshold is not None else {}),
    }
    return {'schema_version': 1, 'kind': KIND, 'entry': name, 'adapter': ENTRIES[name]['adapter'],
            'role_prompt': ENTRIES[name]['role_prompt'], 'options': values, 'policy': policy, 'contract': CONTRACT,
            'skills': [], 'tools': 'read-only', 'writes': False, 'publication': False, 'context': session,
            'evidence': evidence}


def check(root, plan, report, assignment):
    """The accepted result of a report against the context its claim carried, or Blocked."""
    ctx = assignment.get('splitting')
    require(isinstance(ctx, dict) and ctx.get('kind') == KIND, 'assignment', 'the claim carries no splitting context')
    require(not report.get('operations'), 'operations', 'a splitting result proposes no operations; the helper '
            'derives the exact intents and publishes them only under separate authority')
    require(not report.get('changes'), 'changes', 'the assessment is read-only; the helper builds the result')
    deliverables = report.get('deliverables')
    require(isinstance(deliverables, dict) and set(deliverables) == {'split'}, 'deliverables',
            'the report returns only its result in deliverables.split')
    governing = {'options': ctx['options'], 'policy': ctx['policy']}
    evidence = ctx['evidence']
    try:
        if ctx['entry'] == 'dk-commit-split':
            return check_commits(deliverables['split'], evidence, evidence['records'], governing)
        return check_split(deliverables['split'], evidence, governing)
    except Blocked:
        raise
    except (TypeError, AttributeError, KeyError, ValueError) as error:
        raise Blocked('result', 'malformed result: ' + type(error).__name__) from None


# -- Build, validate and apply (workflows/splitting.yml) ----------------------------------------

def _pending(store, operation):
    value = operation['value']
    items = [{'commit': commit} for commit in value['validate']] if value['commands'] else []
    return {'operation': operation['id'], 'state': operation['state'], 'items': items,
            'commands': len(value['commands'])}


def build(root, provider, ledger, plan, inputs):
    """Verify and build the current accepted result as objects; moves no ref, index or worktree.

    Returns the operation and the new intermediate commits to validate. A
    result that changes nothing, a manual-mode split, a missing local grant and
    an exhausted pass allowance build nothing and say why.
    """
    from . import app
    task = occurrence(plan)
    if task is None:
        return {'operation': None, 'items': [], 'status': 'not_splitting'}
    name = role(task)
    values, extra = settings(plan, name)
    with Store(root, plan, write=True) as store:
        pending = store.latest(name, ('built', 'applying'))
        if pending is not None:
            return {**_pending(store, pending), 'status': 'pending'}
        snapshot = app._snapshot(root, provider, plan)
        current = app._current(ledger, snapshot, task)
        if current is None or not current['agent']:
            return {'operation': None, 'items': [], 'status': 'no_current_result'}
        done = store.by_claim(current['id'])
        if done is not None:
            return {'operation': done['id'], 'items': [], 'status': done['state']}
        assignment = read_artifact(root, current['assignment'])
        report = read_artifact(root, current['report'])['report']
        try:
            value = check(root, plan, report, assignment)
        except Blocked as error:
            raise ValueError('splitting result no longer passes its checks: ' + str(error)) from None
        evidence = assignment['splitting']['evidence']
        status = None
        if value['outcome'] in ('no_change', 'keep'):
            status = 'no_op' if name == 'dk-commit-split' else 'kept'
        elif name == 'dk-pr-split' and any(values[MODE_OPTION[split['kind']]] == 'manual' for split in value['splits']):
            status = 'proposed'  # A manual-mode approval names a plan digest and runs through the library procedure.
        elif extra['rewrite'] is None:
            status = 'assessed'
        elif name == 'dk-commit-split' and store.applied(name) >= values['max_passes']:
            status = 'exhausted'
        if status is not None:
            return {'operation': None, 'items': [], 'status': status}
        original = evidence['pr']['head']
        if _sha(root, 'HEAD') != original or git(root, 'symbolic-ref', '-q', 'HEAD').decode().strip() != evidence['branch']:
            raise ValueError('the checkout moved since the assessment; reconcile it first')
        report = {'skill': 'cf-workflows:' + name, 'claim': current['id'], 'report': current['report'],
                  'pr': evidence['pr']['url'], 'reviewed': {'head': evidence['published'], 'tree': evidence['tree']},
                  'target': evidence['pr']['base'], 'base_ref': evidence['pr']['base_ref'],
                  'head_repository': evidence['pr']['head_repository'], 'merge_base': evidence['merge_base'],
                  'branch': evidence['branch'], 'original': original, 'authorization': extra['rewrite'],
                  'outcome': value['outcome'], 'summary': value['summary']}
        try:
            with _objects(root, True) as env:
                if name == 'dk-commit-split':
                    records, final = build_series(root, evidence, evidence['records'], value, env)
                    if final == original:
                        return {'operation': None, 'items': [], 'status': 'no_op'}
                    known = {item['sha'] for item in evidence['records']}
                    created = [item['sha'] for item in records if item['sha'] not in known]
                    moved, refs = [], []
                    report.update(final={'head': final, 'tree': _sha(root, final + '^{tree}')},
                                  commits=[{'sha': item['sha'], 'author': item['author'],
                                            'subject': item['message'].split('\n', 1)[0]} for item in records],
                                  fixed=[item['sha'] for item in evidence['records'][
                                      :_fixed(evidence['records'], set(evidence['owners']))]])
                    if report['final']['tree'] != evidence['tree']:
                        raise Blocked('series', 'the final tree differs from the reviewed tree')
                else:
                    built = build_split(root, evidence, value, env)
                    final, moved = built['source'], built['moved']
                    created = [split['commit'] for split in built['splits']] + [final]
                    refs = [{'ref': 'refs/heads/' + split['branch'], 'old': None, 'new': split['commit']}
                            for split in built['splits']]
                    report.update(plan=plan_digest(value), moved=moved,
                                  splits=[{**split, 'commit': item['commit']}
                                          for split, item in zip(value['splits'], built['splits'])],
                                  source={'head': final, 'message': value['source_message']})
                    taken = [item['ref'] for item in refs if _ref(root, item['ref'])]
                    if taken:
                        raise Blocked('collision', 'split branches already exist locally: ' + ', '.join(taken))
        except Blocked as error:
            raise ValueError('splitting build blocked: ' + str(error)) from None
        ident = name.replace('dk-', '') + '-' + digest({'run': plan['logical_run'], 'claim': current['id'],
                                                         'from': original, 'to': final})[:32]
        recovery = RECOVERY + ident
        report.update(operation=ident, recovery=recovery)
        operation = {'entry': name, 'task': task['id'], 'claim': current['id'], 'coverage': current['coverage'],
                     'branch': evidence['branch'], 'original': original, 'final': final, 'moved': moved,
                     'refs': [{'ref': recovery, 'old': None, 'new': original}, *refs,
                              {'ref': evidence['branch'], 'old': original, 'new': final}],
                     'validate': created, 'commands': extra['validation_commands'], 'report': report}
        return {**_pending(store, store.build(ident, name, current['id'], operation)), 'status': 'built'}


def _checkout(root, commit, directory):
    """A private checkout of commit's tree; the repository's refs, index and worktree stay as they are."""
    with tempfile.TemporaryDirectory(prefix='cf-index-') as scratch:
        env = {'GIT_INDEX_FILE': str(Path(scratch) / 'index')}
        git(root, 'read-tree', commit, env=env)
        git(root, 'checkout-index', '-a', '-f', '--prefix=' + str(directory) + '/', env=env)


def _run(argv, directory):
    try:
        env = {key: value for key, value in os.environ.items() if key not in PRIVATE_ENV}
        result = subprocess.run(argv, cwd=directory, capture_output=True, timeout=VALIDATION_TIMEOUT,
                                stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return {'argv': argv, 'exit': None, 'output': f'timed out after {VALIDATION_TIMEOUT} seconds'}
    except OSError as error:
        return {'argv': argv, 'exit': None, 'output': 'could not start: ' + type(error).__name__}
    output = (result.stdout + result.stderr).decode('utf-8', 'replace')
    return {'argv': argv, 'exit': result.returncode, 'output': output[-VALIDATION_OUTPUT:]}


def validate(root, provider, ledger, plan, inputs):
    """Run the validation commands on one new intermediate commit of a built operation, once.

    Each command runs in a fresh private checkout of that commit; the first
    failing command stops it. The retained result gates apply().
    """
    task = occurrence(plan)
    require(task is not None, 'plan', 'not a splitting plan')
    with Store(root, plan, write=True) as store:
        operation = store.get(inputs.get('operation'))
        if operation is None or operation['entry'] != role(task):
            raise ValueError('validation needs a built splitting operation of this run')
        commit = inputs.get('commit')
        if commit not in operation['value']['validate']:
            raise ValueError('validation runs only on the operation\'s new intermediate commits')
        known = store.validation(operation['id'], commit)
        if known is not None:
            return {'commit': commit, **known}
        if operation['state'] != 'built':
            raise ValueError('the operation moved its refs without this validation')
        results = []
        with tempfile.TemporaryDirectory(prefix='cf-split-validate-') as scratch:
            directory = Path(scratch) / 'tree'
            directory.mkdir()
            _checkout(root, commit, directory)
            for argv in operation['value']['commands']:
                results.append(_run(list(argv), directory))
                if results[-1]['exit'] != 0:
                    break
        state = 'passed' if all(item['exit'] == 0 for item in results) else 'failed'
        return {'commit': commit, **store.validated(operation['id'], commit, state, {'commands': results})}


def _update(root, operation):
    """The recorded ref updates by compare-and-swap; a ref already at its new value is settled."""
    value = operation['value']
    for item in value['refs']:
        current = _ref(root, item['ref'])
        if current == item['new']:
            continue
        if current != item['old']:
            raise Blocked('refs', f"{item['ref']} moved to {current}; reconcile it before continuing")
        git(root, 'update-ref', '-m', 'cf-workflows ' + value['entry'], item['ref'], item['new'],
            item['old'] or '0' * len(item['new']))
    if value['moved']:
        _sync(root, value)


def apply(root, provider, ledger, plan, inputs):
    """Move the refs of the built operation once every new intermediate commit passed validation.

    A failed or missing validation leaves every ref, the index and the worktree
    as they are and fails this step. An interrupted apply resumes from the ref
    values it finds.
    """
    task = occurrence(plan)
    if task is None:
        return {'status': 'not_splitting'}
    name = role(task)
    with Store(root, plan, write=True) as store:
        operation = store.latest(name, ('built', 'applying'))
        if operation is None:
            latest = store.latest(name, ('applied',))
            return {'status': 'nothing' if latest is None else 'applied',
                    'operation': None if latest is None else latest['id']}
        value = operation['value']
        if operation['state'] == 'built':
            if value['commands']:
                failed = []
                for commit in value['validate']:
                    known = store.validation(operation['id'], commit)
                    if known is None:
                        raise ValueError(f'intermediate commit {commit} has no validation result; nothing was moved')
                    if known['state'] != 'passed':
                        last = known['result']['commands'][-1]
                        failed.append(f"{commit}: {' '.join(last['argv'])} exited {last['exit']}")
                if failed:
                    raise ValueError('validation failed on intermediate commits; nothing was moved: '
                                     + '; '.join(failed))
            if _sha(root, 'HEAD') != value['original']:
                raise ValueError('the checkout moved since the build; reconcile it first')
            if value['moved']:
                dirty = sorted(set(_dirty(root)) & set(value['moved']))
                if dirty:
                    raise ValueError('moved paths have uncommitted changes: ' + ', '.join(dirty[:5]))
            operation = store.advance(operation['id'], 'built', 'applying')
        try:
            _update(root, operation)
        except Blocked as error:
            raise ValueError('splitting apply blocked: ' + str(error)) from None
        operation = store.advance(operation['id'], 'applying', 'applied')
        return {'status': 'prepared', 'operation': operation['id'], 'report': operation['value']['report']}


# -- Publication --------------------------------------------------------------------------------

def _permitted(plan, name):
    """The permissions an entry's intents need that are not enabled and explicitly authorized."""
    enabled = (plan.get('options') or {}).get('permissions', {})
    authorized = plan.get('authorizations') or {}
    needs = sorted(set(ENTRIES[name]['permissions'].values()))
    missing = [setting for setting in needs if enabled.get(setting) is not True]
    missing += [action for action in ENTRIES[name]['actions'] if action not in authorized]
    return missing


def _chain(store, operation):
    """The first revision of the run's chain of applied operations ending at this one."""
    original = operation['value']['original']
    while True:
        earlier = store.ending_at(original)
        if earlier is None or earlier['id'] == operation['id']:
            return original
        operation, original = earlier, earlier['value']['original']


def proposals(plan, store, operation):
    """The exact ordered publication intents of an applied operation; IDs bind the run and the result."""
    value, report = operation['value'], operation['value']['report']
    run, source = plan['logical_run'], ['splitting:' + operation['id']]
    target = report['target']
    if value['entry'] == 'dk-commit-split':
        lease = _chain(store, operation)
        ident = 'rewrite-' + digest({'run': run, 'from': lease, 'to': value['final']})[:32]
        return [{'id': ident, 'kind': 'rewrite', 'head': value['final'], 'lease': lease, 'target': target,
                 'payload': {'new_head': value['final']}, 'sources': source}]
    head, owner = value['original'], report['head_repository'].split('/')[0]

    def ident(kind, part=''):
        return f'{kind}-' + digest({'run': run, 'operation': operation['id'], 'part': part})[:32]
    result = []
    for split in report['splits']:
        push = ident('split-push', split['id'])
        result.append({'id': push, 'kind': 'split_push', 'head': split['commit'], 'lease': head, 'target': target,
                       'payload': {'branch': split['branch'], 'new_head': split['commit'], 'paths': split['paths'],
                                   'reason': split['reason']}, 'sources': source})
        result.append({'id': ident('split-pr', split['id']), 'kind': 'split_pr', 'head': head, 'target': target,
                       'payload': {'title': split['title'], 'body': split['body'],
                                   'head': owner + ':' + split['branch'], 'base': report['base_ref'], 'draft': True,
                                   'push_operation': push}, 'sources': source})
    result.append({'id': ident('split-source'), 'kind': 'push', 'head': value['final'], 'lease': head,
                   'target': target, 'payload': {'new_head': value['final']}, 'sources': source})
    return result


def listing(root, provider, ledger, plan, snapshot):
    """The publication listing of a splitting plan, or None for any other plan; writes nothing.

    Only the latest applied operation, while the checkout or PR is its result,
    lists its intents: a dk-commit-split series once its occurrence is current
    on it (the last pass changed nothing), a dk-pr-split at once. Without
    enabled and authorized permissions the local result stays unpublished.
    """
    from . import app
    task = occurrence(plan)
    if task is None:
        return None
    name = role(task)
    with Store(root, plan) as store:
        operation = store.latest(name, ('applied',))
        if operation is None or operation['value']['final'] != snapshot['local_head']:
            return {'items': []}
        if app._current(ledger, snapshot, task) is None:
            return {'items': [], 'unpublished': True,
                    'blockers': ['the rebuilt series is assessed by the next pass before it is published']}
        missing = _permitted(plan, name)
        if missing:
            return {'items': [], 'unpublished': True,
                    'blockers': ['the local result needs explicit authority and configured permission: '
                                 + ', '.join(missing)]}
        return {'items': proposals(plan, store, operation)}


def source(root, ledger, plan, reference, snapshot):
    """The accepted report row behind a ``splitting:<operation>`` source, or ValueError.

    Only the latest applied operation of the plan's entry counts, while the
    checkout is its result.
    """
    task = occurrence(plan)
    require(task is not None and isinstance(reference, str) and reference.startswith('splitting:'), 'source',
            'not a splitting source of this plan')
    with Store(root, plan) as store:
        operation = store.get(reference[len('splitting:'):])
        latest = store.latest(role(task), ('applied',))
    require(operation is not None and latest is not None and operation['id'] == latest['id'], 'source',
            'only the latest applied splitting operation is a publication source')
    if snapshot is not None:
        require(snapshot['local_head'] == operation['value']['final'], 'source',
                'the checkout is no longer the applied splitting result')
    row = ledger.db.execute('SELECT * FROM accepted_reports WHERE reference = ?',
                            (operation['value']['report']['report'],)).fetchone()
    require(row is not None, 'source', 'the splitting operation lost its accepted report')
    return dict(row)


def ready(root, plan, proposal, publisher):
    """The dk-pr-split source push needs every split PR applied first; otherwise ValueError."""
    sources = proposal.get('sources') or []
    task = occurrence(plan)
    if task is None or role(task) != 'dk-pr-split' or proposal.get('kind') != 'push' or not sources:
        return
    with Store(root, plan) as store:
        operation = store.get(str(sources[0])[len('splitting:'):])
        if operation is None:
            return
        pending = [item['id'] for item in proposals(plan, store, operation) if item['kind'] == 'split_pr']
    for ident in pending:
        try:
            state = publisher._load(ident)['state']
        except Exception:
            state = None
        if state != 'applied':
            raise Blocked('publication', 'the source pull request keeps its moved work until every split pull '
                          'request is applied')


def report_row(ledger, reference):
    """The accepted report row that produced a ``splitting:<operation>`` source (its producer's identity)."""
    path = store_path(ledger.root, ledger.logical_run)
    require(path is not None and path.is_file(), 'source', 'no splitting operation is recorded for this run')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)
    try:
        row = db.execute('SELECT value FROM operations WHERE id = ?', (reference[len('splitting:'):],)).fetchone()
    finally:
        db.close()
    require(row is not None, 'source', 'unknown splitting operation')
    accepted = ledger.db.execute('SELECT * FROM accepted_reports WHERE reference = ?',
                                 (json.loads(row[0])['report']['report'],)).fetchone()
    require(accepted is not None, 'source', 'the splitting operation lost its accepted report')
    return dict(accepted)
