"""Adapter procedures of the configuration entries (contract v1).

Each procedure is the helper side of one preset task from cf_workflows.entries:
it builds the native assignment, checks the engine-observed session and its
output, and performs the bounded deterministic operations of the entry.

- `onboard` (cf-workflows:dk-repo-onboard) discovers instruction sources and reads
  the saved configuration read-only. It creates no configuration, ledger, request
  or gate, and its report is returned, never persisted.
- `explain` (cf-workflows:dk-config inspection) is a transient assignment: no
  request ID, no ledger and no gate.
- `configure` (cf-workflows:dk-config and dk-preferences saved changes) binds the
  exact ask to a durable request ID, clarifies through engine input gates, admits
  the exact proposed revision through the shared structural and semantic
  admission and writes it through the shared intent ledger (cf_workflows.writes).
  There is no second writer or validator.
- `resolve` selects scoped preferences for one task's mode and paths.

Sessions come from the engine transport (`native_session`); no helper starts a
model or provider. Fixtures use fail-closed fakes.
"""
import copy
import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath

from . import admission, dk_pr_fix, entries, semantic, writes
from .admission import AdmissionError, digest, fields, require, text

AGENT_FILES = ('AGENTS.md', 'CLAUDE.md')  # In any directory, scoped to that directory.
ROOT_SOURCES = {'CONTRIBUTING.md': 'human', '.github/CONTRIBUTING.md': 'human', 'docs/CONTRIBUTING.md': 'human',
                '.github/copilot-instructions.md': 'automation', '.cursorrules': 'automation'}
SOURCE_DIRECTORIES = {'.github/instructions': '.instructions.md', '.cursor/rules': ''}
MAX_WALK = 100_000
MAX_ITEMS = 512
RESOURCE_ROOT = 'repository'  # dk-config declares files of the target checkout only.
AUTHORITIES = ('selected repository rule', 'evidence only', 'saved preference')
SECTIONS = ('sources', 'rules', 'facts', 'configuration', 'conflicts', 'gaps', 'outcome')
OUTCOMES = ('proposal', 'questions', 'cancelled')
SCOPE_KEYS = ('options', 'permissions', 'prompts', 'skills', 'preferences')


class Blocked(Exception):
    """The session result or its observation cannot be used; nothing is applied."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


def _failure(error):
    if isinstance(error, (AdmissionError, Blocked)):
        return {'path': error.path, 'message': error.message}
    return {'path': '$', 'message': 'malformed result: ' + type(error).__name__}


# -- Discovery (dk-repo-onboard) --------------------------------------------------------------

def _candidate(rel):
    """(kind, scope) of an instruction-source path, or None."""
    name = rel.rsplit('/', 1)[-1]
    parent = rel.rsplit('/', 1)[0] if '/' in rel else '.'
    if name in AGENT_FILES:
        return 'automation', parent
    if rel in ROOT_SOURCES:
        return ROOT_SOURCES[rel], '.'
    for directory, suffix in SOURCE_DIRECTORIES.items():
        if parent == directory and name.endswith(suffix):
            return 'automation', '.'
    return None


def discover(repository):
    """Read-only inventory of the checkout's instruction sources; reads files, writes nothing.

    `selected` sources are the guidance closure that admission consumes for every
    run. Other discovered sources are inventoried as evidence only: no source
    selection override exists in this suite yet.
    """
    root = Path(repository).resolve()
    gaps, found, walked = [], {}, 0
    for directory, names, files in os.walk(root):
        names[:] = sorted(name for name in names if name != '.git')
        for name in sorted(files) + [name for name in names if os.path.islink(os.path.join(directory, name))]:
            walked += 1
            rel = (Path(directory) / name).relative_to(root).as_posix()
            if _candidate(rel):
                found[rel] = _candidate(rel)
        if walked > MAX_WALK:
            gaps.append({'subject': 'discovery', 'impact': f'stopped after {MAX_WALK} entries; sources may be missing'})
            break
    aliases, selected = {}, {}
    try:
        selected = admission.follow(root, [name for name in admission.GUIDANCE if os.path.lexists(root / name)],
                                    'guidance', aliases=aliases)
    except AdmissionError as error:
        gaps.append({'subject': 'guidance', 'impact': 'admission rejects the repository guidance: ' + error.message})
    for rel in selected:
        found.setdefault(rel, ('linked', '.'))
    sources = []
    for rel, (kind, scope) in sorted(found.items()):
        entry = {'path': rel, 'kind': kind, 'scope': scope,
                 'selection': 'selected' if rel in selected or rel in aliases else 'unselected',
                 'status': 'inspected', 'reason': None, 'sha256': None, 'lines': 0, 'resolved': rel}
        path = root / rel
        try:
            if path.is_symlink():
                target = Path(os.path.realpath(path))
                if not target.is_relative_to(root) or not target.is_file():
                    entry.update(status='unsafe', reason='symlink leaves the repository or has no file target')
                    sources.append(entry)
                    continue
                entry['resolved'] = target.relative_to(root).as_posix()
                path = target
            data = path.read_bytes()
            if len(data) > admission.MAX_FILE:
                entry.update(status='unreadable', reason='exceeds the size bound')
            else:
                entry.update(sha256=hashlib.sha256(data).hexdigest(), lines=len(data.decode('utf-8').splitlines()))
        except UnicodeDecodeError:
            entry.update(status='unreadable', reason='not UTF-8', sha256=None)
        except OSError as error:
            entry.update(status='unreadable', reason=type(error).__name__)
        sources.append(entry)
    return {'sources': sources, 'gaps': gaps, 'digest': digest({'sources': sources, 'gaps': gaps})}


# -- Read-only configuration view --------------------------------------------------------------

def target(snapshot):
    request = snapshot['request']
    pr = request['pr']
    return {'repository': pr['repository'] if pr else request['repository'], 'pr': pr['url'] if pr else None,
            'upstream': request['upstream']}


def view(document, where):
    """Effective saved values for a target with provenance; never writes or reconciles."""
    try:
        config = admission.check_configuration(document)
        resources = {}
        for record in config['resources']:
            fields(record, 'configuration.resources', ('root', 'path', 'sha256', 'purpose', 'owner'))
            resources[record['root'] + ':' + record['path']] = record
        request = {'pr': admission.pr_identity(where['pr'], 'target.pr') if where['pr'] else None,
                   'repository': where['repository'], 'upstream': where['upstream']}
        options, permissions, records, scopes, inactive, locations = admission.effective(
            config['scopes'], request, None, resources)
        saved = {name: entries.check_options(name, None, entry_options(document, name, request))
                 for name in entries.SAVED_OPTIONS}
    except (AdmissionError, TypeError, KeyError, AttributeError, ValueError) as error:
        return {'revision': document.get('revision') if isinstance(document, dict) else None, 'valid': False,
                'error': _failure(error), 'paths': []}

    def at(source):
        return locations.get(source, source)
    values = {'options.' + key: {'value': value['value'], 'source': value['source'],
                                 'path': None if value['source'] in ('default', 'accumulated')
                                 else f"{at(value['source'])}.options.{key}"}
              for key, value in options.items()}
    values.update({'permissions.' + key: {'value': value['value'], 'source': value['source'],
                                          'path': None if value['source'] == 'default'
                                          else f"{at(value['source'])}.permissions.{key}"}
                   for key, value in permissions.items()})
    for name, checked in saved.items():
        for key, value in checked.items():
            if value['source'] != 'default':
                values[f'entries.{name}.{key}'] = {'value': value['value'], 'source': value['source'],
                                                   'path': f"{at(value['source'])}.entries.{name}.{key}"}
    entries_ = {kind: [{'path': where_, 'source': source, 'record': copy.deepcopy(record)}
                       for where_, source, record in records[kind]] for kind in ('prompts', 'preferences', 'skills')}
    paths = sorted(set(values) | {item['path'] for kind in entries_.values() for item in kind}
                   | set(locations.values()) - {'default'} | {where_ for _, where_, _ in inactive})
    return {'revision': config['revision'], 'valid': True, 'values': values, **entries_,
            'scopes': scopes, 'inactive': [source for source, _, _ in inactive], 'paths': paths}


def entry_options(document, name, request):
    """The applicable saved option layers of one entry, lowest precedence first.

    request is a resolved request (admission.resolve_request) or a target with the
    same `pr`, `repository` and `upstream` keys. Each layer is (source, path,
    options) for entries.check_options, which applies invocation options over them.
    Every scope is validated in full first. Two scopes at the same precedence that
    set different values for the same option reject, as for workflow options.
    """
    require(name in entries.SAVED_OPTIONS, 'entry', 'no saved options for ' + name)
    config = admission.check_configuration(document)
    resources = {}
    for record in config['resources']:
        fields(record, 'configuration.resources', ('root', 'path', 'sha256', 'purpose', 'owner'))
        resources[record['root'] + ':' + record['path']] = record
    admission.effective(config['scopes'], request, None, resources)
    applicable = []
    for index, scope in enumerate(config['scopes']):
        where = f'configuration.scopes[{index}]'
        kind, applies = admission.matches(scope['selector'], request, where + '.selector')
        if applies and name in scope.get('entries', {}):
            applicable.append((admission.PRECEDENCE.index(kind), index, f"{kind}:{scope['id']}",
                               f'{where}.entries.{name}', scope['entries'][name]))
    applicable.sort(key=lambda item: item[:2])
    def flat(value, prefix=''):
        if isinstance(value, dict) and value:
            return {leaf: item for key, child in value.items() for leaf, item in flat(child, f'{prefix}.{key}').items()}
        return {prefix: value}
    for number, first in enumerate(applicable):
        for second in applicable[number + 1:]:
            if first[0] != second[0]:
                continue
            ours, theirs = flat(first[4]), flat(second[4])
            for leaf in sorted(set(ours) & set(theirs)):
                require(ours[leaf] == theirs[leaf], second[3] + leaf,
                        f'contradicts {first[2]} at the same precedence; resolve before admission')
    return [(source, where, copy.deepcopy(options)) for _, _, source, where, options in applicable]


# -- Native sessions ---------------------------------------------------------------------------

def assignment(admitted, *, mode, claim, context, readable, clarifications=()):
    """The exact typed assignment for the entry's engine-managed native session."""
    entry, snapshot = admitted['entry'], admitted['snapshot']
    agent, provider = semantic.validator_agent(admitted)
    return {
        'schema_version': 1, 'kind': 'configuration_entry', 'suite': admission.SUITE,
        'skill': entry['skill'], 'mode': mode, 'claim': claim,
        'adapter': entry['adapter'], 'role': entry['definition']['definition'],
        'definition_sha256': entry['task']['definition_sha256'], 'assignment': entry['definition']['assignment'],
        'checklist': entry['definition']['checklist'], 'role_prompt': entry['role_prompt'],
        'instructions': snapshot['request']['original_instructions'], 'prompts': entry['prompts'],
        'options': {key: value['value'] for key, value in entry['options'].items()},
        'agent': agent, 'provider': provider, 'skills': [], 'tools': 'read-only', 'writes': False,
        'publication': False, 'readable': dict(sorted(readable.items())), 'context': copy.deepcopy(context),
        'clarifications': copy.deepcopy(list(clarifications)),
        # Admitted native controls (entries.check_controls); empty runs the definition as installed.
        'native': copy.deepcopy(entry.get('controls', {})),
    }


def run_session(transport, task):
    """Dispatch (or reconcile, by claim) one native session and check what the engine observed."""
    try:
        session = transport.native_session(copy.deepcopy(task))
    except Exception as failure:
        raise Blocked('session', 'native session failed: ' + type(failure).__name__) from failure
    try:
        fields(session, 'session', ('result_ref', 'output', 'observation'))
        reference = text(session['result_ref'], 'session.result_ref', 512)
        observed = fields(session['observation'], 'session.observation',
                          ('session_id', 'agent', 'role', 'definition_sha256', 'skills_loaded', 'operations', 'claim'),
                          ('native',))
        text(observed['session_id'], 'session.observation.session_id', 512)
    except AdmissionError as error:
        raise Blocked(error.path, error.message) from None
    if observed['claim'] != task['claim']:
        raise Blocked('session.observation.claim', 'session belongs to another claim')
    if (observed['role'], observed['definition_sha256'], observed['agent']) != (
            task['role'], task['definition_sha256'], task['agent']):
        raise Blocked('session.observation', 'session did not run the admitted role on the assigned agent')
    if observed.get('native', {}) != task.get('native', {}):
        raise Blocked('session.observation.native', 'session did not run with the admitted model and reasoning effort')
    if observed['skills_loaded'] != []:
        raise Blocked('session.observation.skills_loaded', 'entry sessions load no skills')
    if not isinstance(observed['operations'], list):
        raise Blocked('session.observation.operations', 'must be an array')
    for number, operation in enumerate(observed['operations']):
        if (not isinstance(operation, dict) or set(operation) != {'kind', 'target'} or operation['kind'] != 'read'
                or operation['target'] not in task['readable']):
            raise Blocked(f'session.observation.operations[{number}]',
                          'the session may only read the files of its assignment')
    return reference, observed['session_id'], session['output']


def _items(value, path, required, optional=()):
    require(isinstance(value, list) and len(value) <= MAX_ITEMS, path, 'must be a bounded array')
    for number, item in enumerate(value):
        fields(item, f'{path}[{number}]', required, optional)
    return value


# -- Onboarding --------------------------------------------------------------------------------

def check_report(output, inventory, current):
    """Merge the native report with helper-owned discovery; the session cannot upgrade a status."""
    fields(output, 'report', SECTIONS)
    known = {item['path']: item for item in inventory['sources']}
    reported = {}
    for number, item in enumerate(_items(output['sources'], 'report.sources', ('path', 'status'), ('reason',))):
        where = f'report.sources[{number}]'
        require(item['path'] in known and item['path'] not in reported, where + '.path', 'unknown or repeated source')
        require(item['status'] in ('inspected', 'uninspected'), where + '.status', 'must be inspected or uninspected')
        require(item['status'] == 'uninspected' or known[item['path']]['status'] == 'inspected', where + '.status',
                'an unreadable or unsafe source cannot be reported as inspected')
        reported[item['path']] = item
    require(set(reported) == set(known), 'report.sources', 'every discovered source must be accounted for')
    inspected = {path for path, item in reported.items() if item['status'] == 'inspected'}
    preferences = {item['path'] for item in current.get('preferences', [])}
    for number, rule in enumerate(_items(output['rules'], 'report.rules',
                                         ('text', 'source', 'line', 'scope', 'authority'))):
        where = f'report.rules[{number}]'
        text(rule['text'], where + '.text')
        require(rule['authority'] in AUTHORITIES, where + '.authority', 'unknown authority')
        if rule['authority'] == 'saved preference':
            require(rule['source'] in preferences and rule['line'] is None, where + '.source',
                    'a saved preference cites its configuration path')
            continue
        require(rule['source'] in inspected, where + '.source', 'rules cite an inspected source')
        require(type(rule['line']) is int and 1 <= rule['line'] <= known[rule['source']]['lines'],
                where + '.line', 'line outside the source')
        selected = known[rule['source']]['selection'] == 'selected'
        require(rule['authority'] == ('selected repository rule' if selected else 'evidence only'),
                where + '.authority', 'only selected sources govern; others are evidence only')
    for number, fact in enumerate(_items(output['facts'], 'report.facts', ('fact', 'source', 'line', 'documented'))):
        where = f'report.facts[{number}]'
        text(fact['fact'], where + '.fact')
        require(type(fact['documented']) is bool, where + '.documented', 'must be boolean')
        require(not fact['documented'] or (fact['source'] in inspected and type(fact['line']) is int
                                           and 1 <= fact['line'] <= known[fact['source']]['lines']),
                where, 'a documented fact cites an inspected source line')
    paths = set(current.get('paths', []))
    for number, item in enumerate(_items(output['configuration'], 'report.configuration', ('path', 'status'))):
        require(item['path'] in paths, f'report.configuration[{number}].path', 'not a configuration path')
        require(item['status'] in ('active', 'overridden', 'inactive'), f'report.configuration[{number}].status',
                'must be active, overridden or inactive')
    citable = set(known) | paths
    for number, item in enumerate(_items(output['conflicts'], 'report.conflicts', ('summary', 'paths', 'resolution'))):
        where = f'report.conflicts[{number}]'
        text(item['summary'], where + '.summary')
        require(isinstance(item['paths'], list) and len(set(item['paths'])) >= 2 and set(item['paths']) <= citable,
                where + '.paths', 'a conflict cites both of its sources')
        require(item['resolution'] is None or isinstance(item['resolution'], str), where + '.resolution',
                'null or the settled resolution')
    for number, item in enumerate(_items(output['gaps'], 'report.gaps', ('subject', 'impact'))):
        text(item['impact'], f'report.gaps[{number}].impact')
    text(output['outcome'], 'report.outcome')
    sources = [{**item, 'reported': reported[item['path']]['status'],
                'reason': item['reason'] or reported[item['path']].get('reason')} for item in inventory['sources']]
    gaps = inventory['gaps'] + output['gaps'] + [
        {'subject': item['path'], 'impact': f"{item['status']}: {item['reason']}"}
        for item in inventory['sources'] if item['status'] != 'inspected'] + [
        {'subject': path, 'impact': 'not inspected by the session'}
        for path in sorted(set(known) - inspected) if known[path]['status'] == 'inspected']
    if not current['valid']:
        gaps.append({'subject': 'configuration', 'impact': 'saved configuration is invalid: '
                     + current['error']['path'] + ': ' + current['error']['message']})
    # Complete only when every discovered source was read by the helper and the session and the
    # saved configuration was readable; reported gaps of other kinds are kept but do not decide it.
    complete = inspected == set(known) and not inventory['gaps'] and current['valid']
    return {'status': 'complete' if complete else 'incomplete', 'sources': sources, 'rules': output['rules'],
            'facts': output['facts'], 'configuration': output['configuration'], 'conflicts': output['conflicts'],
            'gaps': gaps, 'outcome': output['outcome']}


def _read(store):
    try:
        return store.read(), None
    except Exception as failure:
        return None, {'subject': 'configuration', 'impact': 'configuration store unreadable: ' + type(failure).__name__}


def onboard(*, request, store, deployment, repository, transport, invocation=None, roots=None, install=None,
            controls=None):
    """cf-workflows:dk-repo-onboard: one read-only native session over current discovery.

    Reads the configuration store with read() only, creates no ledger, request or
    gate, writes nothing and returns the report without persisting it.
    """
    extra = {} if install is None else {'install': install}
    admitted = entries.preflight('dk-repo-onboard', request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation=invocation, roots=roots, controls=controls, **extra)
    if admitted['preflight'] != 'admitted':
        return {'status': admitted['preflight'], 'admission': admitted}
    where = target(admitted['snapshot'])
    inventory = discover(repository)
    document, unreadable = _read(store)
    current = view(document, where)
    if unreadable:
        inventory = {**inventory, 'gaps': inventory['gaps'] + [unreadable]}
    readable = {'repository:' + item['resolved']: item['sha256'] for item in inventory['sources']
                if item['status'] == 'inspected'}
    freshness = digest({'target': where, 'inventory': inventory['digest'], 'configuration': current,
                        'admission': admitted['snapshot_digest'], **_native(admitted)})
    task = assignment(admitted, mode='onboard', claim='cfg-onboard-' + freshness[:32], readable=readable,
                      context={'repository': {**where, 'root': str(Path(repository).resolve())},
                               'inventory': inventory, 'configuration': current})
    try:
        reference, session_id, output = run_session(transport, task)
        report = check_report(output, inventory, current)
    except (AdmissionError, Blocked, TypeError, KeyError, AttributeError) as error:
        return {'status': 'blocked', 'error': _failure(error), 'admission': admitted}
    report['repository'] = {**where, 'root': str(Path(repository).resolve()), 'status': report['status']}
    report['source_choice'] = {'mode': 'guidance-closure', 'governing': sorted(
        item['path'] for item in inventory['sources'] if item['selection'] == 'selected'),
        'note': 'Admission consumes the root guidance files and their local link closure; no override is saved.'}
    report['outcome'] = {'text': report['outcome'], 'saved': [], 'authority': 'none'}
    return {'status': report['status'], 'report': report, 'freshness': freshness,
            'evidence_refs': ['native:' + reference], 'session_id': session_id, 'admission': admitted}


# -- Explanation (dk-config inspection) --------------------------------------------------------

def explain(*, request, store, deployment, repository, transport, invocation=None, roots=None, install=None,
            controls=None):
    """cf-workflows:dk-config inspection: a transient assignment with no request ID or history."""
    extra = {} if install is None else {'install': install}
    admitted = entries.preflight('dk-config', request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation=invocation, roots=roots, controls=controls, **extra)
    if admitted['preflight'] != 'admitted':
        return {'status': admitted['preflight'], 'admission': admitted}
    document, unreadable = _read(store)
    if unreadable:
        return {'status': 'blocked', 'error': {'path': 'configuration_store', 'message': unreadable['impact']}}
    current = view(document, target(admitted['snapshot']))
    task = assignment(admitted, mode='explain', readable={},
                      claim='cfg-explain-' + digest({'admission': admitted['snapshot_digest'], 'view': current,
                                                     **_native(admitted)})[:32],
                      context={'configuration': current})
    try:
        reference, _, output = run_session(transport, task)
        fields(output, 'explanation', ('explanation', 'citations'))
        text(output['explanation'], 'explanation.explanation')
        require(isinstance(output['citations'], list) and set(output['citations']) <= set(current['paths']),
                'explanation.citations', 'cite configuration paths that exist')
    except (AdmissionError, Blocked) as error:
        return {'status': 'blocked', 'error': _failure(error)}
    return {'status': 'complete', 'explanation': output['explanation'], 'citations': output['citations'],
            'configuration': current, 'evidence_refs': ['native:' + reference]}


# -- Preference selectors ----------------------------------------------------------------------

def _glob(pattern, path):
    parts, names = pattern.split('/'), path.split('/')

    def match(i, j):
        if i == len(parts):
            return j == len(names)
        if parts[i] == '**':
            return any(match(i + 1, k) for k in range(j, len(names) + 1))
        return j < len(names) and fnmatch.fnmatchcase(names[j], parts[i]) and match(i + 1, j + 1)
    return match(0, 0)


def selects(item, path):
    if item['type'] == 'file':
        return path == item['path']
    if item['type'] == 'directory':
        return item['path'] == '.' or path == item['path'] or path.startswith(item['path'] + '/')
    return _glob(item['path'], path)


def _prefix(item):
    """Literal leading components of a selector (all of them unless it is a glob)."""
    if item['path'] == '.':
        return []
    parts = item['path'].split('/')
    if item['type'] == 'glob':
        parts = parts[:next((n for n, part in enumerate(parts) if any(c in part for c in '*?[')), len(parts))]
    return parts


def may_overlap(first, second):
    """Conservative: could two preference scopes select a common path? Conditions are not evaluated."""
    if first is None or second is None:
        return True
    if not set(first['applies_to']) & set(second['applies_to']):
        return False
    if not first['include'] or not second['include']:
        return True  # A prompt-only scope depends on its condition.
    for a in first['include']:
        for b in second['include']:
            if a['type'] == 'file' and b['type'] == 'file':
                if a['path'] == b['path']:
                    return True
                continue
            x, y = _prefix(a), _prefix(b)
            n = min(len(x), len(y))
            if x[:n] == y[:n] and not (a['type'] == 'file' and len(x) < len(y)) and not (
                    b['type'] == 'file' and len(y) < len(x)):
                return True
    return False


def resolve(preferences, mode, paths):
    """Preferences for one task: its mode and its complete paths. Selection carries no priority.

    The binding covers only the selected records and their matched paths, so a
    change to an unrelated preference leaves the task's binding unchanged.
    """
    require(mode in admission.PREFERENCE_MODES, 'mode', 'must be implementation or review')
    require(isinstance(paths, list), 'paths', 'must be an array')
    paths = sorted({admission.relative(path, f'paths[{n}]') for n, path in enumerate(paths)})
    selected = []
    for entry in preferences:
        scope = entry['scope']
        if scope is None:
            matched, conditional = paths, False
        else:
            if mode not in scope['applies_to']:
                continue
            matched = [path for path in paths if any(selects(item, path) for item in scope['include'])
                       and not any(selects(item, path) for item in scope['exclude'])]
            if scope['include'] and not matched:
                continue
            conditional = scope['condition'] is not None
        selected.append({'id': entry['id'], 'source': entry['source'], 'text': entry['text'], 'scope': scope,
                         'paths': matched, 'requires_condition': conditional})
    selected.sort(key=lambda item: (item['id'], item['source']))
    return {'mode': mode, 'paths': paths, 'records': selected,
            'binding': digest({'mode': mode, 'records': selected})}


# -- Saved changes (dk-config, dk-preferences) -------------------------------------------------

def empty_configuration():
    return {'schema_version': 1, 'kind': 'configuration', 'suite': admission.SUITE, 'revision': 'rev_none',
            'scopes': [], 'resources': [],
            'admission': {'structural': 'pending', 'semantic': None, 'request_id': None, 'context_digest': None,
                          'resource_digest': None, 'capability_digest': None, 'evidence_refs': []}}


def _scope(document, scope_id, path):
    found = [scope for scope in document['scopes'] if isinstance(scope, dict) and scope.get('id') == scope_id]
    require(found, path + '.scope', 'unknown scope ' + str(scope_id))
    return found[0]


def _preference(record, path, previous, defaults):
    """Normalize one proposed preference; omitted modes default only for a new record."""
    fields(record, path, ('text', 'include'), ('id', 'applies_to', 'exclude', 'condition', 'resources'))
    normalized = {'text': text(record['text'], path + '.text')}
    if 'applies_to' in record:
        normalized['applies_to'] = record['applies_to']
    elif previous is not None:
        normalized['applies_to'] = (previous.get('applies_to') or list(admission.PREFERENCE_MODES))
    else:
        normalized['applies_to'] = list(defaults)
    normalized['include'] = record['include']
    normalized['exclude'] = record.get('exclude', [])
    normalized['condition'] = record.get('condition')
    if record.get('resources'):
        normalized['resources'] = record['resources']
    scope = admission.preference_scope(normalized, path)
    return {**{key: value for key, value in normalized.items() if key == 'resources'}, 'text': normalized['text'],
            **scope}


def _same(first, second):
    return {k: v for k, v in first.items() if k != 'id'} == {k: v for k, v in second.items() if k != 'id'}


def apply_operations(document, operations, name, where, options, repository=None):
    """Apply the exact typed operation list of a proposal to a copy of the saved document.

    Returns (revised, changes). Only the named paths change; the result still
    passes full structural and semantic admission before anything is written.
    `set_resource` declares a file of the target checkout (`repository`) pinned to
    its current bytes, so a prompt can be resource-backed.
    """
    domains = entries.ENTRIES[name]['domains']
    revised = copy.deepcopy(document) if document is not None else empty_configuration()
    revised.pop('revision', None)
    revised['revision'] = document['revision'] if document is not None else 'rev_none'
    require(isinstance(operations, list) and 0 < len(operations) <= 64, 'proposal.operations',
            'a proposal is a nonempty bounded operation list')
    changes = {'upserted': {}, 'removed': [], 'reused': {}, 'paths': []}
    for number, operation in enumerate(operations):
        at = f'proposal.operations[{number}]'
        require(isinstance(operation, dict) and operation.get('op') in domains, at + '.op',
                'operation outside this entry: ' + str(isinstance(operation, dict) and operation.get('op')))
        kind = operation['op']
        if kind == 'add_scope':
            fields(operation, at, ('op', 'scope', 'selector'))
            require(isinstance(operation['scope'], str) and admission.IDENT.fullmatch(operation['scope'])
                    and not any(scope.get('id') == operation['scope'] for scope in revised['scopes']),
                    at + '.scope', 'new scope needs a new identifier')
            selector = operation['selector']
            require(isinstance(selector, dict), at + '.selector', 'must be an object')
            if name == 'dk-preferences':
                require(selector.get('kind') == 'personal'
                        or selector == {'kind': 'repository', 'id': where['repository']},
                        at + '.selector', 'preferences are personal or for this repository')
            revised['scopes'].append({'id': operation['scope'], 'selector': copy.deepcopy(selector),
                                      **{key: {} if key in ('options', 'permissions') else [] for key in SCOPE_KEYS}})
            changes['paths'].append('scopes.' + operation['scope'])
            continue
        if kind in ('set_resource', 'remove_resource'):
            fields(operation, at, ('op', 'resource'))
            record = operation['resource']
            fields(record, at + '.resource', ('root', 'path') + (('purpose', 'owner') if kind == 'set_resource' else ()),
                   ('sha256',) if kind == 'set_resource' else ())
            require(record['root'] == RESOURCE_ROOT, at + '.resource.root',
                    'dk-config declares resources of the target repository only')
            rel = admission.relative(record['path'], at + '.resource.path')
            key = RESOURCE_ROOT + ':' + rel
            existing = [item for item in revised['resources']
                        if isinstance(item, dict) and item.get('root') == RESOURCE_ROOT and item.get('path') == rel]
            if kind == 'remove_resource':
                require(existing, at + '.resource', 'unknown resource ' + key)
                require(key not in _resource_refs(revised), at + '.resource',
                        'resource is still referenced; remove or change its prompts and preferences first')
            else:
                require(repository is not None, at, 'resource declarations need the target checkout')
                actual = hashlib.sha256(admission.read_file(Path(repository), rel, at + '.resource.path')).hexdigest()
                require(record.get('sha256', actual) == actual, at + '.resource.sha256',
                        'resource changed since the proposal: ' + key)
            for item in existing:
                revised['resources'].remove(item)
            if kind == 'set_resource':
                # Pinned to the bytes read now; admission verifies the same digest before the write.
                revised['resources'].append({'root': RESOURCE_ROOT, 'path': rel, 'sha256': actual,
                                             'purpose': text(record['purpose'], at + '.resource.purpose', 512),
                                             'owner': text(record['owner'], at + '.resource.owner', 512)})
            changes['paths'].append('resources.' + key)
            continue
        if kind == 'remove_scope':
            fields(operation, at, ('op', 'scope'))
            scope = _scope(revised, operation['scope'], at)
            require(not any(scope.get(key) for key in (*SCOPE_KEYS, 'entries')), at,
                    'remove the scope contents explicitly first')
            revised['scopes'].remove(scope)
            changes['paths'].append('scopes.' + operation['scope'])
            continue
        scope = _scope(revised, operation.get('scope'), at)
        if name == 'dk-preferences':
            selector = scope.get('selector', {})
            require(selector.get('kind') == 'personal' or selector == {'kind': 'repository', 'id': where['repository']},
                    at + '.scope', 'preferences are personal or for this repository')
        if kind in ('set_option', 'unset_option', 'set_permission', 'unset_permission'):
            section, key = ('options', 'key') if 'option' in kind else ('permissions', 'action')
            fields(operation, at, ('op', 'scope', key, *(('value',) if kind.startswith('set') else ())))
            require(isinstance(operation[key], str), at + '.' + key, 'must be a string')
            if kind.startswith('set'):
                scope[section][operation[key]] = copy.deepcopy(operation['value'])
            else:
                require(operation[key] in scope[section], at + '.' + key, 'not set in this scope')
                del scope[section][operation[key]]
            changes['paths'].append(f"scopes.{scope['id']}.{section}.{operation[key]}")
        elif kind in ('set_entry_option', 'unset_entry_option'):
            # One top-level option of an entry with saved options, checked against the entry's schema
            # here as one saved layer, like the scope body check on write.
            fields(operation, at, ('op', 'scope', 'entry', 'key', *(('value',) if kind.startswith('set') else ())))
            require(operation['entry'] in entries.SAVED_OPTIONS, at + '.entry', 'no saved options for this entry')
            require(isinstance(operation['key'], str), at + '.key', 'must be a string')
            if kind.startswith('set'):
                require(operation['key'] in entries.ENTRIES[operation['entry']]['options'], at + '.key',
                        'unsupported option for ' + entries.ENTRIES[operation['entry']]['skill'])
                try:
                    entries.check_options(operation['entry'], {operation['key']: operation['value']})
                except AdmissionError as error:
                    prefix = 'options.' + operation['key']
                    raise AdmissionError(at + '.value' + error.path[len(prefix):], error.message) from None
            saved = scope.setdefault('entries', {})
            if kind.startswith('set'):
                saved.setdefault(operation['entry'], {})[operation['key']] = copy.deepcopy(operation['value'])
            else:
                require(operation['key'] in saved.get(operation['entry'], {}), at + '.key', 'not set in this scope')
                del saved[operation['entry']][operation['key']]
                if not saved[operation['entry']]:
                    del saved[operation['entry']]
                if not saved:
                    del scope['entries']
            changes['paths'].append(f"scopes.{scope['id']}.entries.{operation['entry']}.{operation['key']}")
        elif kind in ('set_prompt', 'remove_prompt'):
            fields(operation, at, ('op', 'scope', 'record') if kind == 'set_prompt' else ('op', 'scope', 'id'))
            ident = operation['record'].get('id') if kind == 'set_prompt' and isinstance(operation['record'], dict) \
                else operation.get('id')
            existing = [item for item in scope['prompts'] if isinstance(item, dict) and item.get('id') == ident]
            require(kind == 'set_prompt' or existing, at + '.id', 'unknown prompt')
            for item in existing:
                scope['prompts'].remove(item)
            if kind == 'set_prompt':
                scope['prompts'].append(copy.deepcopy(operation['record']))
            changes['paths'].append(f"scopes.{scope['id']}.prompts.{ident}")
        elif kind == 'remove_preference':
            fields(operation, at, ('op', 'scope', 'id'))
            existing = [item for item in scope['preferences'] if item.get('id') == operation['id']]
            require(existing, at + '.id', 'unknown preference')
            scope['preferences'].remove(existing[0])
            changes['removed'].append(operation['id'])
            changes['paths'].append(f"scopes.{scope['id']}.preferences.{operation['id']}")
        elif kind == 'set_preference':
            fields(operation, at, ('op', 'scope', 'record'))
            record = operation['record']
            require(isinstance(record, dict), at + '.record', 'must be an object')
            previous = None
            if 'id' in record:
                found = [item for item in scope['preferences'] if item.get('id') == record['id']]
                require(found, at + '.record.id', 'updates name an existing preference in this scope')
                previous = found[0]
            normalized = _preference(record, at + '.record', previous, options['default_applies_to']['value'])
            duplicate = next((item for item in scope['preferences']
                              if _same(item, normalized) and item is not previous), None)
            if previous is not None:
                scope['preferences'].remove(previous)
            if duplicate is not None:
                # An exact duplicate reuses the saved record; an update that duplicates one is removed.
                changes['reused'][f'operations[{number}]'] = duplicate['id']
                if previous is not None:
                    changes['removed'].append(previous['id'])
                continue
            ident = previous['id'] if previous is not None else 'pref-' + digest(
                {'scope': scope['id'], 'record': normalized})[:16]
            scope['preferences'].append({'id': ident, **normalized})
            changes['upserted'][f'operations[{number}]'] = ident
            changes['paths'].append(f"scopes.{scope['id']}.preferences.{ident}")
    return revised, changes


def _resource_refs(document):
    """Every resource key a saved prompt or preference names."""
    refs = set()
    for scope in document['scopes']:
        for kind in ('prompts', 'preferences'):
            for record in scope.get(kind, []):
                if isinstance(record, dict):
                    refs.update(item for item in record.get('resources', []) if isinstance(item, str))
                    if isinstance(record.get('resource'), str):
                        refs.add(record['resource'])
    return refs


def overlaps(revised, changes, where):
    """Pairs of changed and other applicable preferences whose scopes and modes may overlap."""
    request = {'pr': None, 'repository': where['repository'], 'upstream': None}
    records = []
    for scope in revised['scopes']:
        kind = scope['selector']['kind']
        if kind == 'personal' or (kind == 'repository' and admission.matches(scope['selector'], request, 'scope')[1]):
            records.extend((scope['id'], item) for item in scope['preferences'])
    changed = {ident: ref for ref, ident in changes['upserted'].items()}
    found, seen = [], set()
    for first_scope, first in records:
        if first['id'] not in changed:
            continue
        for second_scope, second in records:
            pair = frozenset((first['id'], second['id']))
            if second['id'] == first['id'] or pair in seen:
                continue
            seen.add(pair)
            if may_overlap(admission.preference_scope(first, 'record'), admission.preference_scope(second, 'record')):
                found.append({'change': changed[first['id']], 'with': changed.get(second['id'], second['id']),
                              'records': [{'scope': first_scope, **first}, {'scope': second_scope, **second}]})
    return found


def check_resolutions(raw, found, answers):
    """Every overlap is compatible or resolved by the user's recorded answers; no invented winner."""
    raw = _items(raw, 'proposal.resolutions', ('change', 'with', 'outcome', 'reason'), ('answers',))
    settled = {}
    for number, item in enumerate(raw):
        where = f'proposal.resolutions[{number}]'
        text(item['reason'], where + '.reason')
        require(item['outcome'] in ('compatible', 'resolved'), where + '.outcome', 'compatible or resolved')
        cited = item.get('answers', [])
        require(item['outcome'] == 'compatible' or (isinstance(cited, list) and cited and set(cited) <= answers),
                where + '.answers', 'a resolved contradiction cites the user answers that settled it')
        settled[(item['change'], item['with'])] = item
    return [pair for pair in found if (pair['change'], pair['with']) not in settled
            and (pair['with'], pair['change']) not in settled]


def _gate(transport, request_id, claim, prompt, payload):
    request = {'request_id': request_id, 'evidence_id': claim, 'prompt': prompt, 'payload': payload}
    opened = transport.open_input_gate(copy.deepcopy(request))
    fields(opened, 'gate', ('gate_id',))
    return {'gate_id': text(opened['gate_id'], 'gate.gate_id', 256), 'evidence_id': claim, 'status': 'waiting'}


def request_id(admitted):
    """Durable identity of the exact ask in its logical run; it never depends on store revisions.

    The same words asked in another logical run are a new request, never settled by an earlier one.
    """
    entry, snapshot = admitted['entry'], admitted['snapshot']
    return 'cfgreq_' + digest({'skill': entry['skill'], 'target': target(snapshot),
                               'logical_run': snapshot['request']['logical_run'],
                               'instructions': snapshot['request']['original_instructions'],
                               'options': {key: value['value'] for key, value in entry['options'].items()},
                               'prompts': entry['prompts'], **_native(admitted)})[:32]


def _native(admitted):
    """Native controls bound into a claim or request ID; absent when none were chosen, so IDs stay stable."""
    controls = admitted['entry'].get('controls', {})
    return {'native': controls} if controls else {}


def configure(name, *, request, store, ledger, deployment, repository, transport, options=None, invocation=None,
              roots=None, install=None, controls=None):
    """Saved change through cf-workflows:dk-config or dk-preferences.

    Reconciles earlier writes first, resumes the same request through its gates and
    native claim, admits the exact proposed revision through the shared pipeline and
    writes exactly that revision once. transport offers capabilities(),
    native_session(task), input_gates(request_id), open_input_gate(request) and
    validator_session(task); store and ledger are the shared writes.FileStore and
    acceptance.Ledger.
    """
    require(name in ('dk-config', 'dk-preferences'), 'entry', 'saved changes use dk-config or dk-preferences')
    install = dk_pr_fix.INSTALL if install is None else install
    try:
        recovered = writes.reconcile(store, ledger)
        document = store.read()
    except Exception as failure:
        return {'status': 'blocked', 'error': {'path': 'configuration_store',
                                               'message': 'configuration store unavailable: ' + type(failure).__name__}}
    admitted = entries.preflight(name, request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation=invocation, options=options, roots=roots,
                                 ledger=ledger, install=install, controls=controls)
    if admitted['preflight'] != 'admitted':
        return {'status': admitted['preflight'], 'admission': admitted, 'recovered_writes': recovered}
    where, ident = target(admitted['snapshot']), request_id(admitted)
    base = {'request_id': ident, 'recovered_writes': recovered}
    marker = 'request:' + ident
    settled = [intent for intent in ledger.writes(('applied', 'blocked')) if marker in intent['evidence_refs']]
    current_revision = document['revision'] if document is not None else None
    for intent in settled[-1:]:
        # An uncertain write of this request is never retried. An applied one is settled only while the
        # store still holds its revision; after any later change the same ask is assessed again.
        if intent['status'] == 'blocked' or (intent['receipt'] or {}).get('revision') == current_revision:
            return {**base, 'status': intent['status'], 'write': {'intent_id': intent['intent_id'],
                                                                   'status': intent['status'],
                                                                   'receipt': intent['receipt'],
                                                                   'failure': intent['failure']},
                    'configuration': view(document, where)}
    if ledger.writes(('prepared', 'pending')):
        return {**base, 'status': 'pending', 'error': {'path': 'writes', 'message': 'an earlier write is uncertain'}}
    try:
        gates = semantic.check_gates(transport.input_gates(ident), ident)
    except Exception as failure:
        return {**base, 'status': 'blocked', 'error': {'path': 'gates', 'message': 'input gates unavailable: '
                                                       + type(failure).__name__}}
    waiting = [gate for gate in gates if gate['status'] == 'waiting']
    if waiting:  # Interrupted clarification resumes without a new session or gate.
        return {**base, 'status': 'waiting',
                'gate': {key: waiting[0][key] for key in ('gate_id', 'evidence_id', 'status')},
                'questions': waiting[0]['request']['payload']}
    clarifications = [{'gate_id': gate['gate_id'], 'answer_id': gate['answer']['answer_id'],
                       'value': gate['answer']['value'], 'payload': gate['request']['payload']} for gate in gates]
    current = view(document, where)
    context = {'configuration': copy.deepcopy(document), 'view': current,
               'operations': list(entries.ENTRIES[name]['domains']), 'base_revision': document and document['revision']}
    claim = ident + '-' + digest({'base': context['base_revision'],
                                  'answers': [item['answer_id'] for item in clarifications]})[:16]
    task = assignment(admitted, mode='change', claim=claim, context=context, readable={},
                      clarifications=clarifications)
    try:
        reference, _, output = run_session(transport, task)
        fields(output, 'proposal', ('outcome', 'summary'), ('operations', 'questions', 'resolutions'))
        require(output['outcome'] in OUTCOMES, 'proposal.outcome', 'proposal, questions or cancelled')
        text(output['summary'], 'proposal.summary')
        if output['outcome'] == 'questions':
            questions = _items(output['questions'], 'proposal.questions', ('question', 'paths'))
            require(questions, 'proposal.questions', 'clarification needs a concrete question')
            gate = _gate(transport, ident, claim, 'Answer the configuration questions in the payload.',
                         {'questions': questions, 'summary': output['summary'], 'result_ref': reference})
            return {**base, 'status': 'waiting', 'gate': gate, 'questions': questions}
        if output['outcome'] == 'cancelled':
            return {**base, 'status': 'cancelled', 'summary': output['summary'],
                    'evidence_refs': ['native:' + reference]}
        revised, changes = apply_operations(document, output.get('operations'), name, where,
                                            admitted['entry']['options'], repository=repository)
        if name == 'dk-preferences':
            open_pairs = check_resolutions(output.get('resolutions', []), overlaps(revised, changes, where),
                                           {item['answer_id'] for item in clarifications})
            if open_pairs:
                gate = _gate(transport, ident, claim, 'These preferences may contradict each other. Revise, replace, '
                             'narrow or cancel the new preference, or confirm they are compatible.',
                             {'overlaps': open_pairs, 'summary': output['summary'], 'result_ref': reference})
                return {**base, 'status': 'waiting', 'gate': gate, 'overlaps': open_pairs}
    except (AdmissionError, Blocked) as error:
        return {**base, 'status': 'blocked' if isinstance(error, Blocked) else 'rejected', 'error': _failure(error)}
    except Exception as failure:
        return {**base, 'status': 'blocked',
                'error': {'path': 'proposal', 'message': 'not applied: ' + type(failure).__name__}}
    proposal = {'operations': output['operations'], 'digest': digest({'base': context['base_revision'],
                                                                      'operations': output['operations']}),
                'changes': changes}
    if writes.content(revised) == writes.content(document):
        return {**base, 'status': 'unchanged', 'proposal': proposal, 'evidence_refs': ['native:' + reference]}
    # The exact revision is admitted against the bundled default plan, which exercises every
    # preference mode, through the shared structural and semantic pipeline.
    plan = {key: admission.DEFAULTS[key] for key in ('tasks', 'finalizers', 'reviewers')}
    result = semantic.admit(request={k: v for k, v in request.items() if k != 'authorizations'},
                            configuration=copy.deepcopy(revised), invocation={'options': plan}, deployment=deployment,
                            roles=dk_pr_fix.role_catalog(install), transport=transport, ledger=ledger,
                            roots={**dict(roots or {}), 'repository': repository, dk_pr_fix.INSTALL_ROOT: install})
    outcome = {**base, 'proposal': proposal, 'assessment': {key: result.get(key) for key in
                                                            ('preflight', 'semantic', 'gate', 'error', 'reuse')}}
    if result['preflight'] != 'admitted':
        status = {'waiting': 'waiting', 'rejected': 'rejected'}.get(result['preflight'], 'blocked')
        return {**outcome, 'status': status}
    final = result.get('proposal', {}).get('configuration', revised)
    # Exactly the admitted document, never a recomputed or merged one.
    require(admission.digest(final) == result['snapshot']['sources']['configuration_digest'], 'proposal',
            'the admitted revision differs from the document to write')
    result['admission']['evidence_refs'] = sorted(set(result['admission']['evidence_refs'])
                                                  | {marker, 'native:' + reference})
    write = writes.commit(store, ledger, document, final, result)
    readback = store.read() if write['status'] == 'applied' else None
    outcome.update(status=write['status'], write=write, evidence_refs=result['admission']['evidence_refs'])
    if readback is not None:
        outcome['readback'] = readback_records(readback, changes, where)
    return outcome


def readback_records(document, changes, where):
    """The persisted state of every changed record and setting, read back after the write."""
    current = view(document, where)
    preferences = {}
    for scope in document['scopes']:
        for item in scope.get('preferences', []):
            preferences[item['id']] = {'scope': scope['id'], 'owner': scope['selector'], **item}
    return {'revision': document['revision'], 'paths': changes['paths'],
            'preferences': [preferences[ident] for ident in sorted(set(changes['upserted'].values())
                                                                   | set(changes['reused'].values()))
                            if ident in preferences],
            'removed': sorted(ident for ident in changes['removed'] if ident not in preferences),
            'values': {key: value for key, value in current.get('values', {}).items() if value['path'] is not None}}
