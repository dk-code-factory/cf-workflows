"""The 29-row acceptance matrix, inherited-defect coverage and release evidence (#38).

`baselines/acceptance-matrix.json` traces every inventory row to its
implementing code, its own deterministic direct and composed scenarios, its
recovery or permission-denial scenarios and its honest compatibility status. It
maps the nine inherited dk-workflows defects to deterministic tests with merge
provenance, lists the configuration-admission cases, and records separately
authorized live acceptance and verified host/provider/engine combinations.

This module validates that document. It is standard-library only and runs
nothing: test references are resolved by parsing the test sources, and Git
provenance is checked through a caller-supplied `git` function. A missing row,
an alias, an aggregate reference (a module, a class, a whole suite or a count)
and a dangling test reference all reject. A live-evidence record counts only
when it is complete, authorized and bound to the current plugin closure and the
exact pinned engine release; deterministic success never promotes a capability.
"""
import ast
import json
import math
from pathlib import Path
import re

SUITE = 'cf-workflows'
MATRIX = 'baselines/acceptance-matrix.json'
HOSTS = ('codex', 'claude')

# The nine inherited upstream defects of the suite tracker (#10, "Known defects").
DEFECTS = (244, 255, 253, 260, 254, 259, 239, 241, 240)

# Configuration-admission cases the parent issues (#2, #9, #10) require.
ADMISSION_CASES = (
    'default_plan_structural_only',
    'default_plan_loads_no_skills',
    'structural_rejection_before_engine',
    'semantic_verdicts',
    'clarification_and_reconciliation',
    'prohibitions_cannot_be_waived',
    'permission_denial',
    'unsupported_capability',
    'supplied_skills_rejected_until_supported',
    'selective_acceptance_reuse',
    'configuration_write_recovery',
    'no_import_of_prior_settings_or_state',
    'bootstrap_boundary',
    'unconfigured_fixture_fails_closed',
)

# Separately authorized live procedures and the scenarios each record must pass.
PROCEDURES = {
    'user_supplied_skill': (
        'admitted_by_content_digest',
        'loaded_only_into_selected_occurrence',
        'engine_observed_invocation',
        'declared_children_observed',
        'host_installed_skill_never_discovered',
        'report_only_claim_rejected',
        'default_plan_loads_no_skill',
    ),
    'live_provider': (
        'engine_managed_native_sessions',
        'independent_verification_session',
        'same_session_continuation',
        'claim_and_result_loss_reconciled',
        'capacity_one',
        'native_sessions_never_publish',
        'semantic_validator_session',
        'measured_dollar_cost',
    ),
    'github_publication': (
        'exact_intent_receipts_per_write_kind',
        'lost_response_reconciled_without_resend',
        'pending_review_rejection_settles_not_applied',
        'outstanding_review_requests_hold_merge',
        'body_only_findings_published',
        'replies_settled_before_review',
        'fork_ci_approval_per_attempt',
    ),
}

ROW_KEYS = {'skill', 'delivery_issues', 'behavior', 'documentation', 'composition', 'direct', 'composed',
            'recovery', 'status', 'remaining'}
STATUS_KEYS = {'parity', 'deterministic', 'installed_workflow', 'native_execution', 'live_acceptance'}
COMPOSITIONS = ('shared_workflow', 'delegate', 'boundary')
DEFECT_KEYS = {'upstream', 'required_behavior', 'delivery', 'provenance', 'tests', 'live_acceptance'}
TEST_ID = re.compile(r'(?:[a-z_][a-z0-9_]*\.)*test_[a-z0-9_]+\.[A-Z][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+')
SHA = re.compile(r'[0-9a-f]{40}')
# An evidence reference names one original engine or GitHub observation, never free text.
EVIDENCE_REF = re.compile(r'(?:session|claim|receipt|intent|run|gate|usage):[A-Za-z0-9][A-Za-z0-9._:/#-]*')
RELEASE_URL = re.compile(r'https://github\.com/dk-code-factory/agent-workflow/releases/tag/v?([0-9][^/\s]*)')


# Open Agent Workflow issues that block an installed row, each with the feature of the row's installed
# workflow graph that needs it. A row's `remaining` must cite each one its graph has ("Agent Workflow
# #N"), and may cite the state-mount and container-job blockers only when its graph has them.
ENGINE_BLOCKERS = {
    149: 'a helper step uses the state namespace, which no engine mounts into container helpers',
    150: 'a job runs in a container, which plugin test fixtures reject',
    151: 'its configuration session needs an operation receipt that the engine does not record for native tasks',
    152: 'its occurrences run through the reusable native.yml, where supplied skill selections reject',
}
GRAPH_ONLY_BLOCKERS = (149, 150)
NATIVE_WORKFLOW = 'workflows/native.yml'
CONFIGURE_WORKFLOW = 'workflows/configure.yml'
USES = re.compile(r'^\s*(?:- )?uses: (workflows/[A-Za-z0-9_./-]+\.ya?ml)\s*$', re.M)
STATE_ENV = re.compile(r'^\s*CF_WORKFLOWS_STATE:', re.M)
CONTAINER = re.compile(r'^\s+container:', re.M)
CITED = re.compile(r'Agent Workflow #([0-9]+)')
# A release gap is stated as what must ship, not only as "never ran on a released engine".
RELEASE_GAP = re.compile(r'after a released engine ships|no release contains')


class MatrixError(ValueError):
    """The matrix, a test reference or an evidence record cannot be accepted."""


def require(condition, message):
    if not condition:
        raise MatrixError(message)


def _keys(value, allowed, where, optional=()):
    require(isinstance(value, dict), f'{where}: must be an object')
    extra = set(value) - set(allowed)
    missing = set(allowed) - set(optional) - set(value)
    require(not extra, f'{where}: unexpected field(s) {sorted(extra)}; aliases and aggregates are not evidence')
    require(not missing, f'{where}: missing field(s) {sorted(missing)}')


def _refs(value, where, nonempty=True):
    require(isinstance(value, list), f'{where}: must be a list of test IDs')
    require(bool(value) or not nonempty, f'{where}: needs at least one scenario')
    for ref in value:
        require(isinstance(ref, str) and TEST_ID.fullmatch(ref),
                f'{where}: {ref!r} is not a single test method (module.Class.test_name); '
                'modules, classes, suites and counts are aggregates')
    require(len(set(value)) == len(value), f'{where}: duplicate test reference')
    return value


def index_tests(tests_root):
    """Map each test method ID (as unittest discovery names it) to its source and docstrings."""
    tests_root = Path(tests_root)
    found = {}
    for path in sorted(tests_root.rglob('test_*.py')):
        relative = path.relative_to(tests_root).with_suffix('')
        module = '.'.join(relative.parts)
        source = path.read_text()
        tree = ast.parse(source)
        module_doc = ast.get_docstring(tree) or ''
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            class_doc = ast.get_docstring(node) or ''
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith('test'):
                    found[f'{module}.{node.name}.{item.name}'] = {
                        'path': str(Path('tests') / path.relative_to(tests_root)), 'class': node.name,
                        'name': item.name, 'source': ast.get_source_segment(source, item),
                        'class_doc': class_doc, 'module_doc': module_doc}
    return found


def _marked(test, number):
    """The test names the defect it reproduces in its own name, body, class or module docstring."""
    pattern = re.compile(rf'dk-workflows#{number}(?![0-9])|dk_workflows_{number}(?![0-9])')
    return (re.match(rf'test_{number}_', test['name']) is not None
            or any(pattern.search(text) for text in (test['source'], test['class_doc'], test['module_doc'])))


def load(repository):
    return json.loads((Path(repository) / MATRIX).read_text())


def validate(matrix, inventory, *, descriptor_skills, tests=None, records=None, closure_digest=None,
             engine_version=None):
    """Check the matrix shape against the inventory; with `tests`, resolve every reference.

    `descriptor_skills` are the names the plugin descriptor lists (installed
    workflows). `records` maps a record path to its parsed document and is used
    to decide which live procedures and combinations are actually evidenced.
    Returns the accepted evidence summary.
    """
    _keys(matrix, {'schema_version', 'kind', 'suite', 'rows', 'defects', 'configuration_admission',
                   'live_acceptance', 'verified_combinations'}, 'matrix')
    require((matrix['schema_version'], matrix['kind'], matrix['suite']) == (1, 'acceptance_matrix', SUITE),
            'matrix: not a version 1 cf-workflows acceptance matrix')
    rows = {row['name']: row for row in inventory['skills']}
    require(len(rows) == len(inventory['skills']) == 29, 'inventory must contain exactly 29 distinct rows')
    require(isinstance(matrix['rows'], list), 'matrix.rows: must be a list')
    seen = []
    for index, row in enumerate(matrix['rows']):
        where = f'rows[{index}]'
        _keys(row, ROW_KEYS, where)
        name = row['skill']
        require(name in rows, f'{where}: {name!r} is not an inventory row (aliases are not rows)')
        require(name not in seen, f'{where}: {name} appears twice')
        seen.append(name)
    missing = [name for name in rows if name not in seen]
    require(not missing, f'matrix: missing inventory row(s) {missing}')

    accepted = accepted_evidence(matrix, records or {}, inventory=inventory, closure_digest=closure_digest,
                                 engine_version=engine_version)
    direct_owner = {}
    for row in matrix['rows']:
        name, where = row['skill'], f'row {row["skill"]}'
        inventory_row = rows[name]
        require(isinstance(row['delivery_issues'], list) and row['delivery_issues']
                and all(isinstance(n, int) for n in row['delivery_issues'])
                and inventory_row['delivery_issue'] in row['delivery_issues'],
                f'{where}: delivery issues must include the inventory delivery issue')
        require(isinstance(row['behavior'], list) and row['behavior']
                and all(isinstance(p, str) and p for p in row['behavior']), f'{where}: behavior paths required')
        require(isinstance(row['documentation'], str) and row['documentation'].startswith('docs/'),
                f'{where}: documentation must be a docs/ path')
        require(row['composition'] in COMPOSITIONS, f'{where}: composition must be one of {COMPOSITIONS}')
        for kind in ('direct', 'composed', 'recovery'):
            _refs(row[kind], f'{where}.{kind}')
        for ref in row['direct']:
            direct_owner.setdefault(ref, []).append(name)
        require(bool(inventory_row['intentional_differences']), f'{where}: inventory has no intentional differences')
        status = row['status']
        _keys(status, STATUS_KEYS, f'{where}.status')
        require(status['parity'] == inventory_row['parity_status'], f'{where}: parity differs from the inventory')
        require(status['deterministic'] == 'fixture_verified', f'{where}: deterministic status must be fixture_verified')
        require(status['installed_workflow'] is (name in descriptor_skills),
                f'{where}: installed_workflow must match the plugin descriptor')
        native = name in accepted['native_rows']
        require(status['native_execution'] == ('verified' if native else 'pending'),
                f'{where}: native_execution must be {"verified" if native else "pending"}; '
                'only an accepted live-provider record on a verified combination can verify it')
        live = all(name in accepted['procedures'][p] for p in PROCEDURES)
        require(status['live_acceptance'] == ('recorded' if live else 'pending'),
                f'{where}: live_acceptance must be {"recorded" if live else "pending"}')
        if status['parity'] == 'implemented':
            require(native and status['installed_workflow'], f'{where}: implemented parity needs native evidence')
        else:
            require(isinstance(row['remaining'], str) and row['remaining'].strip(),
                    f'{where}: unfinished rows must state what remains')
    for row in matrix['rows']:
        own = [ref for ref in row['direct'] if direct_owner[ref] == [row['skill']]]
        require(own, f'row {row["skill"]}: every direct scenario is shared with another row; '
                'a row needs at least one direct scenario of its own')

    _check_defects(matrix['defects'])
    _check_admission(matrix['configuration_admission'])
    if tests is not None:
        check_references(matrix, tests)
    return accepted


def _check_defects(defects):
    require(isinstance(defects, list), 'matrix.defects: must be a list')
    numbers = []
    for index, defect in enumerate(defects):
        where = f'defects[{index}]'
        _keys(defect, DEFECT_KEYS, where)
        match = re.fullmatch(r'dk-code-factory/dk-workflows#([0-9]+)', str(defect['upstream']))
        require(match is not None, f'{where}: upstream must be dk-code-factory/dk-workflows#N')
        number = int(match.group(1))
        numbers.append(number)
        require(isinstance(defect['required_behavior'], str) and defect['required_behavior'].strip(),
                f'{where}: required behavior missing')
        require(isinstance(defect['delivery'], list) and defect['delivery'], f'{where}: delivery issues missing')
        require(isinstance(defect['provenance'], list) and defect['provenance'], f'{where}: provenance missing')
        for item in defect['provenance']:
            _keys(item, {'pr', 'merge_commit'}, f'{where}.provenance')
            require(isinstance(item['pr'], int) and SHA.fullmatch(str(item['merge_commit'])),
                    f'{where}: provenance needs a PR number and a full merge commit')
        _refs(defect['tests'], f'{where}.tests')
        require(isinstance(defect['live_acceptance'], list) and defect['live_acceptance']
                and set(defect['live_acceptance']) <= set(PROCEDURES),
                f'{where}: live_acceptance must name the live procedures that remain separately required')
    require(sorted(numbers) == sorted(DEFECTS) and len(numbers) == len(set(numbers)),
            f'matrix.defects: must map exactly the nine inherited defects {sorted(DEFECTS)}')


def _check_admission(cases):
    require(isinstance(cases, list), 'matrix.configuration_admission: must be a list')
    names = []
    for index, case in enumerate(cases):
        _keys(case, {'case', 'tests'}, f'configuration_admission[{index}]')
        names.append(case['case'])
        _refs(case['tests'], f'configuration_admission[{case["case"]}]')
    require(sorted(names) == sorted(ADMISSION_CASES) and len(names) == len(set(names)),
            f'matrix.configuration_admission: must list exactly {sorted(ADMISSION_CASES)}')


def references(matrix):
    """Every referenced test ID with the (owner, role) pairs that cite it."""
    cited = {}
    for row in matrix['rows']:
        for kind in ('direct', 'composed', 'recovery'):
            for ref in row[kind]:
                cited.setdefault(ref, []).append((row['skill'], kind))
    for defect in matrix['defects']:
        for ref in defect['tests']:
            cited.setdefault(ref, []).append((defect['upstream'], 'defect'))
    for case in matrix['configuration_admission']:
        for ref in case['tests']:
            cited.setdefault(ref, []).append(('admission:' + case['case'], 'case'))
    return cited


def check_references(matrix, tests):
    """Every reference names an existing test method; defect tests name their defect."""
    dangling = sorted(ref for ref in references(matrix) if ref not in tests)
    require(not dangling, f'dangling test reference(s): {dangling}')
    for defect in matrix['defects']:
        number = int(defect['upstream'].rsplit('#', 1)[1])
        for ref in defect['tests']:
            require(_marked(tests[ref], number),
                    f'{defect["upstream"]}: {ref} does not name dk-workflows#{number} in its name, body or docstrings')


def check_paths(matrix, repository):
    """Each row's implementing paths and documentation exist in this repository."""
    root = Path(repository).resolve()
    for row in matrix['rows']:
        for path in [*row['behavior'], row['documentation']]:
            require(not Path(path).is_absolute() and '..' not in Path(path).parts,
                    f'row {row["skill"]}: {path} is not a path inside this repository')
            require((root / path).exists() and (root / path).resolve().is_relative_to(root),
                    f'row {row["skill"]}: {path} does not exist')


def workflow_graph(root, workflow):
    """The installed workflow files `workflow` reaches through local `uses:` calls, itself included."""
    root, seen, pending = Path(root), set(), [workflow]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(USES.findall((root / current).read_text()))
    return seen


def engine_blockers(root, workflow):
    """The ENGINE_BLOCKERS issue numbers the installed graph of `workflow` needs."""
    graph = workflow_graph(root, workflow)
    texts = [(Path(root) / path).read_text() for path in graph]
    found = set()
    if any(STATE_ENV.search(text) for text in texts):
        found.add(149)
    if any(CONTAINER.search(text) for text in texts):
        found.add(150)
    if CONFIGURE_WORKFLOW in graph:
        found.add(151)
    if NATIVE_WORKFLOW in graph:
        found.add(152)
    return found


def check_engine_blockers(matrix, root, workflows):
    """Each installed unfinished row's `remaining` names the release gap and exactly its graph's engine blockers.

    `workflows` maps a descriptor skill name to its workflow path under `root`.
    A cited state-mount or container-job blocker that the row's graph does not
    have rejects too, so a row cannot blame an issue that does not affect it.
    """
    for row in matrix['rows']:
        workflow = workflows.get(row['skill'])
        if workflow is None or row['status']['parity'] == 'implemented':  # Implemented rows have no gap.
            continue
        where, text = f'row {row["skill"]}', row['remaining']
        require(RELEASE_GAP.search(text) is not None,
                f'{where}: remaining must name the missing engine release')
        needed = engine_blockers(root, workflow)
        cited = {int(number) for number in CITED.findall(text)}
        missing = sorted(needed - cited)
        require(not missing, f'{where}: remaining must cite '
                + '; '.join(f'Agent Workflow #{n} ({ENGINE_BLOCKERS[n]})' for n in missing))
        stray = sorted((cited & set(GRAPH_ONLY_BLOCKERS)) - needed)
        require(not stray, f'{where}: remaining cites Agent Workflow '
                + ', '.join(f'#{n}' for n in stray) + ', which its installed workflow graph does not need')


def _defines(text, cls, name):
    """Whether the top-level class `cls` in `text` has a method `name` in its own body."""
    match = re.search(rf'^class {re.escape(cls)}\b.*$', text, re.M)
    if match is None:
        return False
    body = text[match.end():]
    end = re.search(r'^\S', body, re.M)  # The next nonblank line in column zero ends the class.
    body = body[:end.start()] if end else body
    return re.search(rf'^\s+def {re.escape(name)}\(', body, re.M) is not None


def check_provenance(matrix, tests, git):
    """Each defect test exists in a listed merge commit that is an ancestor of HEAD.

    `git(*args)` returns stdout bytes and raises on failure; `git('merge-base',
    '--is-ancestor', a, b)` must raise when `a` is not an ancestor of `b`.
    """
    for defect in matrix['defects']:
        commits = [item['merge_commit'] for item in defect['provenance']]
        for commit in commits:
            try:
                git('cat-file', '-e', commit + '^{commit}')
                git('merge-base', '--is-ancestor', commit, 'HEAD')
            except Exception:  # noqa: BLE001 - any git failure means unproven provenance
                raise MatrixError(f'{defect["upstream"]}: merge commit {commit} is not in this history') from None
        for ref in defect['tests']:
            test = tests[ref]
            found = False
            for commit in commits:
                try:
                    text = git('show', f'{commit}:{test["path"]}').decode()
                except Exception:  # noqa: BLE001
                    continue
                if _defines(text, test['class'], test['name']):
                    found = True
                    break
            require(found, f'{defect["upstream"]}: {ref} is not present in any listed merge commit')


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_record(record, procedure, *, inventory, closure_digest, engine_version):
    """A live acceptance record that is complete, authorized and bound to this exact release input."""
    where = f'{procedure} record'
    _keys(record, {'schema_version', 'kind', 'suite', 'procedure', 'authorization', 'plugin', 'engine', 'host',
                   'provider', 'target', 'recorded_at', 'skill', 'scenarios', 'cost'}, where)
    require((record['schema_version'], record['kind'], record['suite']) == (1, 'acceptance_evidence', SUITE),
            f'{where}: not a version 1 cf-workflows acceptance evidence record')
    require(record['procedure'] == procedure, f'{where}: procedure mismatch')
    authorization = record['authorization']
    _keys(authorization, {'granted_by', 'granted_at', 'reference', 'scope'}, f'{where}.authorization')
    require(all(isinstance(v, str) and v.strip() for v in authorization.values()),
            f'{where}: explicit authorization (who, when, where, scope) is required')
    _keys(record['plugin'], {'version', 'closure_digest'}, f'{where}.plugin')
    require(_text(record['plugin']['version']), f'{where}: plugin version required')
    require(closure_digest is not None and record['plugin']['closure_digest'] == closure_digest,
            f'{where}: recorded for a different plugin closure')
    _keys(record['engine'], {'version', 'release'}, f'{where}.engine')
    release = RELEASE_URL.fullmatch(str(record['engine']['release']))
    require(record['engine']['version'] == engine_version and release is not None
            and release.group(1) == engine_version,
            f'{where}: must name the exact pinned engine version and its published release')
    _keys(record['host'], {'name', 'version'}, f'{where}.host')
    require(record['host']['name'] in HOSTS and _text(record['host']['version']), f'{where}: unknown host')
    _keys(record['provider'], {'name', 'model', 'service_tier'}, f'{where}.provider')
    require(all(isinstance(v, str) and v.strip() for v in record['provider'].values()), f'{where}: provider identity')
    _keys(record['target'], {'repository', 'disposable'}, f'{where}.target')
    require(record['target']['disposable'] is True, f'{where}: live acceptance runs only on a disposable target')
    require(_text(record['target']['repository']), f'{where}: target repository required')
    require(_text(record['recorded_at']), f'{where}: recorded_at required')
    names = {row['name'] for row in inventory['skills']}
    # One record covers one row: a shared run cannot verify every row it touched at once.
    require(isinstance(record['skill'], str) and record['skill'] in names,
            f'{where}: skill must be exactly one inventory row')
    scenarios = record['scenarios']
    require(isinstance(scenarios, list), f'{where}: scenarios must be a list')
    require(all(isinstance(item, dict) and isinstance(item.get('id'), str) for item in scenarios),
            f'{where}: each scenario needs a string id')
    ids = [item['id'] for item in scenarios]
    require(sorted(ids) == sorted(PROCEDURES[procedure]) and len(ids) == len(scenarios),
            f'{where}: must record exactly the scenarios {list(PROCEDURES[procedure])}')
    for item in scenarios:
        _keys(item, {'id', 'result', 'evidence_refs'}, f'{where}.scenarios[{item["id"]}]')
        refs = item['evidence_refs']
        require(item['result'] == 'passed' and isinstance(refs, list) and refs,
                f'{where}: scenario {item["id"]} needs a passing result with evidence references')
        require(all(isinstance(ref, str) and EVIDENCE_REF.fullmatch(ref) for ref in refs)
                and len(set(refs)) == len(refs),
                f'{where}: scenario {item["id"]} evidence references must be distinct session, claim, '
                'receipt, intent, run, gate or usage IDs')
    _keys(record['cost'], {'complete', 'usd'}, f'{where}.cost')
    if procedure == 'live_provider':
        usd = record['cost']['usd']
        require(record['cost']['complete'] is True and isinstance(usd, (int, float)) and not isinstance(usd, bool)
                and math.isfinite(usd) and usd >= 0,
                f'{where}: a live-provider record needs complete measured dollar cost')
    return record


def accepted_evidence(matrix, records, *, inventory, closure_digest=None, engine_version=None):
    """Which procedures, combinations and rows the recorded live evidence actually covers.

    A record path that is listed but absent or invalid rejects the matrix; it is
    never silently ignored.
    """
    live = matrix['live_acceptance']
    _keys(live, set(PROCEDURES), 'matrix.live_acceptance')
    procedures = {name: set() for name in PROCEDURES}
    valid = {}
    cited = {}
    for name in PROCEDURES:
        entry = live[name]
        _keys(entry, {'status', 'records'}, f'live_acceptance.{name}')
        require(isinstance(entry['records'], list), f'live_acceptance.{name}.records: must be a list')
        require(entry['status'] == ('recorded' if entry['records'] else 'pending_authorization'),
                f'live_acceptance.{name}: status must be pending_authorization until a record is listed')
        for path in entry['records']:
            require(isinstance(path, str) and path in records, f'live_acceptance.{name}: record {path} is missing')
            record = validate_record(records[path], name, inventory=inventory,
                                     closure_digest=closure_digest, engine_version=engine_version)
            valid[path] = record
            procedures[name].add(record['skill'])
            for item in record['scenarios']:
                for ref in item['evidence_refs']:
                    owner = cited.setdefault(ref, record['skill'])
                    require(owner == record['skill'],
                            f'live_acceptance.{name}: evidence {ref} is cited for both {owner} and '
                            f'{record["skill"]}; each row needs its own observations')
    combinations = matrix['verified_combinations']
    require(isinstance(combinations, list), 'matrix.verified_combinations: must be a list')
    native_rows = set()
    for index, combination in enumerate(combinations):
        where = f'verified_combinations[{index}]'
        _keys(combination, {'host', 'provider', 'engine_version', 'record'}, where)
        record = valid.get(combination['record']) if isinstance(combination['record'], str) else None
        require(record is not None and record['procedure'] == 'live_provider',
                f'{where}: needs an accepted live_provider record')
        require((combination['host'], combination['provider'], combination['engine_version'])
                == (record['host']['name'], record['provider']['name'], record['engine']['version']),
                f'{where}: differs from its record')
        native_rows.add(record['skill'])
    return {'procedures': procedures, 'combinations': len(combinations), 'native_rows': native_rows}


def read_records(matrix, repository):
    """Parse every listed record file; a missing file is left out so validation names it."""
    records = {}
    live = matrix.get('live_acceptance') if isinstance(matrix, dict) else None
    for entry in live.values() if isinstance(live, dict) else []:
        listed = entry.get('records') if isinstance(entry, dict) else None
        for path in listed if isinstance(listed, list) else []:
            if isinstance(path, str) and (Path(repository) / path).is_file():
                records[path] = json.loads((Path(repository) / path).read_text())
    return records


def release_blockers(repository, *, inventory, descriptor_skills, closure_digest, engine_version):
    """Blockers derived from the matrix: pending live procedures, no verified combination, unverified rows."""
    try:
        matrix = load(repository)
        records = read_records(matrix, repository)
        accepted = validate(matrix, inventory, descriptor_skills=descriptor_skills, records=records,
                            closure_digest=closure_digest, engine_version=engine_version)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [{'prerequisite': 'acceptance matrix', 'missing': f'no valid acceptance matrix: {error}'}]
    blockers = [{'prerequisite': 'live acceptance: ' + name,
                 'missing': 'separately authorized evidence is pending'}
                for name in PROCEDURES if not accepted['procedures'][name]]
    if not accepted['combinations']:
        blockers.append({'prerequisite': 'compatibility',
                         'missing': 'no verified host/provider/engine combination'})
    unverified = [row['skill'] for row in matrix['rows'] if row['skill'] not in accepted['native_rows']]
    if unverified:
        blockers.append({'prerequisite': 'acceptance matrix',
                         'missing': f'{len(unverified)} of {len(matrix["rows"])} rows lack native-execution '
                                    'evidence on a verified combination'})
    return blockers
