"""The staged alternatives procedure (#27).

Authored for cf-workflows; no upstream counterpart. See docs/alternatives.md.

This is the one implementation of the alternatives stages: the stage output
checks, weighted totals, the selection policy and the walk over the fixed
stages of the alignment record (prfix/alignment.py). The library module
``cf_workflows/alternatives.py`` loads this file and drives the walk with its
engine transport. The installed workflow drives it one native stage at a time
(workflows/alternatives.yml): ``next`` returns the next stage assignment or
selection gate, ``accept`` checks a native result and records it, ``answer``
records a gate answer, and ``status`` completes the occurrence once the
procedure has settled.

The walk never starts a model, provider or controller. Sessions and gates come
from the caller: the library's engine transport, or the installed workflow's
``agent/run`` and ``input/wait`` steps. Nothing here edits, commits or
publishes. It uses only the standard library; stage prompts are the pinned
dk-workflows baseline prompts (MIT; see licenses/dk-workflows-MIT.txt).
"""
import copy
import json
from pathlib import Path
import re
import subprocess

try:
    from . import alignment as records
except ImportError:  # Loaded in memory by the library, which supplies the same records module.
    records = globals()['_RECORDS']

digest = records.digest

SCOPES = {'dk-issue-alternatives': 'whole-problem', 'dk-pr-alternatives': 'branch'}
ADAPTERS = {'dk-issue-alternatives': 'issue-alternatives', 'dk-pr-alternatives': 'pr-alternatives'}
COMPOSERS = {'cf-workflows:dk-pr-fix': 'whole-problem', 'cf-workflows:dk-pr-review': 'branch'}
# Baseline claude/skills/dk-pr-alternatives/prompts/<stage>.md (identical for dk-issue-alternatives).
STAGE_PROMPTS = {
    'assessment': 'Assess purpose and scoped evaluation rubrics from requirements and repository evidence. Return the '
                  'shared assessment contract; references replace copied metadata or discussion.\n',
    'inventory': 'Inventory architectural decisions throughout the branch including inherited decisions, labeling '
                 'origin inherited, introduced or materially_changed. Judge questionability using the applicable '
                 'persisted rubric and evaluation guidance, without a predefined defect checklist. In fix mode '
                 'inventory the whole implementation problem from issue requirements, with the PR as an optional '
                 'candidate. Return coverage, decisions and evidence.\n',
    'comparison': 'Compare materially distinct approaches for the assigned decision using its persisted rubric '
                  'consistently for every option. Include retaining the current implementation when a PR exists. '
                  'Estimate every option against the same pinned target revision, counting work already on the PR '
                  'branch. Return the comparison contract and target_revision. Do not select or edit.\n',
    'selection': 'Use helper totals and the shared selection policy. Apply feasibility, confidence, sensitivity, '
                 'unresolved-doubt and score-gap checks. Manual choices require an explicit sourced user choice of '
                 'an evaluated feasible option. Selection grants no edit or publication permission.\n',
    'interactions': 'Check interactions between every settled approach and the retained nonquestionable decisions. '
                    'Return compatible:boolean, reason, evidence and required_changes for incompatibility. Do not '
                    'silently alter settled choices. Unresolved interactions block dependent work.\n',
    'alignment': 'Compare the pinned implementation against the settled approaches and interaction analysis. Return '
                 'aligned, mismatch or uncertain with reason, evidence and required_changes for mismatch. In fix mode '
                 'hand off selected changes to authorized implementation tasks and relevant tests; retaining current '
                 'code needs no edit. In review mode report recommendations without edits.\n',
    'verification': 'Independently verify the material design mismatch against the selected decision and current '
                    'pinned implementation. Return confirmed, rejected or uncertain with evidence and required '
                    'changes.\n',
}
STAGE_PROMPT_SHA256 = {
    'assessment': 'b109948cf2ea65dfbe188984923568a6d8cb9e70ad757e7076a1dd4193cb2436',
    'inventory': '7c04c2b3033907ddc4e3f5176aa465596db7fea54bc1d907ffda5b9587a826fe',
    'comparison': '1c62bb573f7fa634da5583307dbcf32760ed896df5fa50ba9cfd85f2e479c97b',
    'selection': '7e033528a0c63216b30aa896f9881575f301c5ea071220583e3df245dac99395',
    'interactions': 'ff147e2458c68a4a649053d3934928634afe8c325b545ded04597551b8efd197',
    'alignment': 'e21c94d22ae60ee8a02506d0a7894fd71accf76b8269fa0ececb430f5223ba0a',
    'verification': '09816061a6bd6b4ac83e4e181934a915c7322811efb600613f58840c75031709',
}
# A direct request refreshes these stages; the shared assessment is reused.
REFRESHED = ('inventory', 'comparison', 'selection', 'interactions')
ORIGINS = {'branch': ('inherited', 'introduced', 'materially_changed'), 'whole-problem': ('whole_problem',)}
KINDS = ('pr', 'smallest', 'alternative')
WORK_TYPES = ('small_fix', 'feature', 'mixed')
VOLUME_CRITERIA = ('low_loc', 'low_intrusiveness')
MAX_READABLE = 4096
MAX_TEXT = 64 << 10
ISSUE_ATTEMPTS = 3  # Attempts per stage of an issue-only run, which keeps no record of rejections.
MAX_ROUNDS = 8  # Questions about one choice after answers that named no feasible option.
SOLUTION_FIELDS = ('id', 'kind', 'approach', 'pros', 'cons', 'handled_edges', 'unhandled_edges', 'evidence',
                   'complexity', 'intrusiveness', 'feasible', 'feasibility_reason', 'material_doubts',
                   'recommended', 'recommendation_reason', 'production_loc', 'ratings', 'rating_evidence')
SOLUTION_OPTIONAL = ('components', 'complexity_reason', 'intrusiveness_reason', 'total')
_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
# Reconciled decisions (decisions handoff) are citable as `decision:<id>`.
DECISION_PREFIX = 'decision:'
_DECISION_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


class StageError(ValueError):
    """A rejected input or stage output, naming the offending path."""

    def __init__(self, path, message):
        super().__init__(f'{path}: {message}')
        self.path, self.message = path, message


class Unqualified(StageError):
    """An automatic pick that the selection policy cannot accept; semi-manual asks the user instead."""


class Unavailable(Exception):
    """A session or gate the caller could not supply. rejected: the session ran and was refused."""

    def __init__(self, failure, rejected=False):
        super().__init__(failure['message'])
        self.failure, self.rejected = failure, rejected


def require(condition, path, message):
    if not condition:
        raise StageError(path, message)


def text(value, path, limit=MAX_TEXT):
    require(isinstance(value, str) and value.strip() and '\0' not in value,
            path, 'must be a nonempty string without NUL')
    try:
        size = len(value.encode('utf-8', 'strict'))
    except UnicodeEncodeError:
        raise StageError(path, 'must be valid Unicode text') from None
    require(size <= limit, path, 'exceeds byte bound')
    return value


def fields(value, path, required, optional=()):
    require(isinstance(value, dict), path, 'must be an object')
    unknown = set(value) - set(required) - set(optional)
    require(not unknown, path, 'unsupported field(s): ' + ', '.join(sorted(map(str, unknown))))
    missing = set(required) - set(value)
    require(not missing, path, 'missing field(s): ' + ', '.join(sorted(missing)))
    return value


def failure(error):
    """The {path, message} of a rejection; any other error names only its type."""
    if hasattr(error, 'path') and hasattr(error, 'message'):
        return {'path': error.path, 'message': error.message}
    return {'path': '$', 'message': 'malformed result: ' + type(error).__name__}


# -- Pins and inputs --------------------------------------------------------------------------------

def _git(repository, *args):
    return subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True, text=True).stdout


def pins(repository, *, scope, pr, target, head, priority=()):
    """Verified commits of the checkout; whole-branch readable files and the branch's changed paths.

    Past the bound, the changed paths and the priority paths (guidance) stay readable first.
    """
    require(isinstance(target, str) and _SHA.fullmatch(target), 'target', 'requires a full commit ID')
    require(scope != 'branch' or pr is not None, 'request.pr', 'branch analysis needs a pull request')
    if pr is None:
        require(head is None, 'head', 'issue-only analysis has no PR head; it is pinned to the target')
        head = target
    require(isinstance(head, str) and _SHA.fullmatch(head), 'head', 'requires a full commit ID')
    for label, commit in (('target', target), ('head', head)):
        try:
            require(_git(repository, 'cat-file', '-t', commit).strip() == 'commit', label, 'not a commit')
        except subprocess.CalledProcessError:
            raise StageError(label, 'commit is not present in the checkout') from None
    try:
        _git(repository, 'merge-base', '--is-ancestor', target, head)
    except subprocess.CalledProcessError:
        raise StageError('target', 'the target revision is not an ancestor of the head') from None
    listing = _git(repository, 'ls-tree', '-r', '-z', head).split('\0')
    readable, gaps = {}, []
    for line in filter(None, listing):
        meta, path = line.split('\t', 1)
        mode, kind, blob = meta.split()
        if kind == 'blob' and mode != '120000':
            readable['git:' + path] = blob
    changed = sorted(filter(None, _git(repository, 'diff', '--name-only', '-z', target + '...' + head).split('\0')))
    if len(readable) > MAX_READABLE:
        gaps.append({'subject': 'readable', 'impact': f'only {MAX_READABLE} files are listed: the changed '
                                                      'paths and guidance first, then the rest in path order'})
        first = [name for name in dict.fromkeys('git:' + path for path in [*changed, *priority]) if name in readable]
        rest = [name for name in sorted(readable) if name not in set(first)]
        readable = {name: readable[name] for name in (first + rest)[:max(MAX_READABLE, len(first))]}
    return {'target': target, 'head': head, 'pr': pr['url'] if pr else None, 'changed': changed,
            'readable': readable, 'gaps': gaps}


def requirements(raw, scope):
    raw = [] if raw is None else raw
    require(isinstance(raw, list) and len(raw) <= 64, 'requirements', 'must be a bounded array')
    seen = set()
    for number, item in enumerate(raw):
        fields(item, f'requirements[{number}]', ('source', 'text'))
        text(item['source'], f'requirements[{number}].source', 512)
        require(not item['source'].startswith(('git:', DECISION_PREFIX)), f'requirements[{number}].source',
                'git: and decision: name files and reconciled decisions')
        text(item['text'], f'requirements[{number}].text')
        require(item['source'] not in seen, f'requirements[{number}].source', 'duplicate requirement source')
        seen.add(item['source'])
    require(scope != 'whole-problem' or raw, 'requirements', 'whole-problem analysis starts from issue requirements')
    return copy.deepcopy(raw)


def reconciled(value):
    """Sourced decision reconciliation for the inventory: a decisions handoff value, or None.

    Settled commitments and user exceptions become citable as `decision:<id>`. Unresolved
    contradictions and blocking questions keep dependent implementation blocked.
    """
    if value is None:
        return None
    fields(value, 'decisions', ('result_ref', 'commitments', 'exceptions', 'unresolved', 'questions',
                                'dependent_work'))
    text(value['result_ref'], 'decisions.result_ref', 512)
    seen = set()
    for group in ('commitments', 'exceptions'):
        for number, item in enumerate(_list(value[group], 'decisions.' + group)):
            where = f'decisions.{group}[{number}]'
            require(isinstance(item, dict) and isinstance(item.get('id'), str) and _DECISION_ID.fullmatch(item['id'])
                    and item['id'] not in seen, where + '.id', 'invalid or duplicate decision ID')
            seen.add(item['id'])
            require(isinstance(item.get('sources'), list) and item['sources'], where + '.sources',
                    'a reconciled decision keeps its sources')
    for group in ('unresolved', 'questions'):
        _list(value[group], 'decisions.' + group)
    require(value['dependent_work'] == ('blocked' if value['unresolved'] or value['questions'] else 'released'),
            'decisions.dependent_work', 'blocked exactly while contradictions or blocking questions are unresolved')
    return copy.deepcopy(value)


# -- Stage output checks ------------------------------------------------------------------------

def _list(value, path, nonempty=False):
    require(isinstance(value, list) and len(value) <= 256 and (value or not nonempty), path,
            'must be a nonempty bounded array' if nonempty else 'must be a bounded array')
    return value


_LINES = re.compile(r'L?[1-9][0-9]*(?:-L?[1-9][0-9]*)?')


def cited(item, citable):
    """The requirement source or `git:<path>` file an evidence item cites (optionally `:<lines>`), or None."""
    if item in citable:
        return item
    base, _, lines = item.rpartition(':')
    return base if base in citable and _LINES.fullmatch(lines) else None


def _evidence(value, path, citable):
    """Evidence cites requirement sources or files of the pinned head."""
    for number, item in enumerate(_list(value, path, nonempty=True)):
        text(item, f'{path}[{number}]', 1024)
        require(cited(item, citable) is not None, f'{path}[{number}]',
                'cites neither a requirement source nor a file of the pinned head')
    return value


def _percent(value, path):
    require(type(value) is int and 0 <= value <= 100, path, 'must be an integer from 0 to 100')
    return value


def check_assessment(output, citable):
    fields(output, 'assessment', ('purpose', 'scope', 'work_type', 'rationale', 'mandatory_constraints',
                                  'evidence', 'rubrics'))
    for key in ('purpose', 'scope', 'rationale'):
        text(output[key], 'assessment.' + key)
    require(output['work_type'] in WORK_TYPES, 'assessment.work_type', 'small_fix, feature or mixed')
    for number, item in enumerate(_list(output['mandatory_constraints'], 'assessment.mandatory_constraints')):
        text(item, f'assessment.mandatory_constraints[{number}]')
    _evidence(output['evidence'], 'assessment.evidence', citable)
    ids = set()
    for number, rubric in enumerate(_list(output['rubrics'], 'assessment.rubrics', nonempty=True)):
        where = f'assessment.rubrics[{number}]'
        fields(rubric, where, ('id', 'scope', 'work_type', 'rationale', 'evidence', 'weights', 'anchors'))
        require(isinstance(rubric['id'], str) and _IDENT.fullmatch(rubric['id']) and rubric['id'] not in ids,
                where + '.id', 'unique stable rubric ID')
        ids.add(rubric['id'])
        text(rubric['scope'], where + '.scope')
        text(rubric['rationale'], where + '.rationale')
        require(rubric['work_type'] in ('small_fix', 'feature'), where + '.work_type', 'small_fix or feature')
        _evidence(rubric['evidence'], where + '.evidence', citable)
        weights = rubric['weights']
        require(isinstance(weights, dict) and weights and all(type(w) is int and w > 0 for w in weights.values())
                and sum(weights.values()) == 100, where + '.weights', 'positive integer percentages totaling 100')
        require(rubric['work_type'] != 'feature' or not set(weights) & set(VOLUME_CRITERIA), where + '.weights',
                'feature rubrics exclude change-volume ranking')
        require(isinstance(rubric['anchors'], dict) and set(rubric['anchors']) == set(weights), where + '.anchors',
                'anchors cover exactly the weighted criteria')
        for criterion, anchor in rubric['anchors'].items():
            fields(anchor, f'{where}.anchors.{criterion}', ('0', '50', '100'))
            for level in ('0', '50', '100'):
                text(anchor[level], f'{where}.anchors.{criterion}.{level}')
    return output


def check_inventory(output, *, scope, assessment, pins_, citable):
    fields(output, 'inventory', ('coverage', 'evidence', 'decisions'))
    if scope == 'branch':
        # The whole branch: decisions inherited from the target as well as introduced ones.
        fields(output['coverage'], 'inventory.coverage', ('introduced', 'inherited'))
        for key in ('introduced', 'inherited'):
            text(output['coverage'][key], 'inventory.coverage.' + key)
    else:
        text(output['coverage'], 'inventory.coverage')
    _evidence(output['evidence'], 'inventory.evidence', citable)
    rubrics = {rubric['id'] for rubric in assessment['rubrics']}
    changed = set(pins_['changed'])
    for number, item in enumerate(_list(output['decisions'], 'inventory.decisions', nonempty=True)):
        where = f'inventory.decisions[{number}]'
        fields(item, where, ('id', 'origin', 'scope', 'rubric', 'decision', 'questionable', 'reason', 'evidence'))
        require(item['origin'] in ORIGINS[scope], where + '.origin', 'one of ' + ', '.join(ORIGINS[scope]))
        require(item['rubric'] in rubrics, where + '.rubric', 'cites a rubric of the shared assessment')
        for key in ('scope', 'decision', 'reason'):
            text(item[key], f'{where}.{key}')
        require(type(item['questionable']) is bool, where + '.questionable', 'must be boolean')
        _evidence(item['evidence'], where + '.evidence', citable)
        if item['origin'] in ('introduced', 'materially_changed'):
            require(any(cited(ref, citable).removeprefix('git:') in changed for ref in item['evidence']),
                    where + '.evidence', 'a branch decision cites a path the branch changed')
        if scope == 'whole-problem':
            require(any(not cited(ref, citable).startswith(('git:', DECISION_PREFIX)) for ref in item['evidence']),
                    where + '.evidence',
                    'a whole-problem decision cites the issue requirements')
    return output


def scores(comparison, rubric):
    """Weighted totals: sum(weight x rating) / 100 under the persisted rubric, the same for every option."""
    return {solution['id']: sum(rubric['weights'][c] * solution['ratings'][c] for c in rubric['weights']) / 100
            for solution in comparison['solutions']}


def check_comparison(output, *, scope, decision, rubric, target, has_pr, citable):
    fields(output, 'comparison', ('rubric', 'target_revision', 'alternative_coverage', 'evidence', 'solutions'))
    require(output['rubric'] == decision['rubric'], 'comparison.rubric', 'uses the decision\'s persisted rubric')
    require(output['target_revision'] == target, 'comparison.target_revision',
            'every option is estimated against the pinned target revision')
    text(output['alternative_coverage'], 'comparison.alternative_coverage')
    _evidence(output['evidence'], 'comparison.evidence', citable)
    solutions = _list(output['solutions'], 'comparison.solutions', nonempty=True)
    ids, kinds = set(), []
    for number, item in enumerate(solutions):
        where = f'comparison.solutions[{number}]'
        fields(item, where, SOLUTION_FIELDS, SOLUTION_OPTIONAL)
        require(isinstance(item['id'], str) and _IDENT.fullmatch(item['id']) and item['id'] not in ids,
                where + '.id', 'unique stable option ID')
        ids.add(item['id'])
        require(item['kind'] in KINDS, where + '.kind', 'pr, smallest or alternative')
        kinds.append(item['kind'])
        for key in ('approach', 'feasibility_reason', 'recommendation_reason'):
            text(item[key], f'{where}.{key}')
        for key in ('pros', 'cons', 'handled_edges', 'unhandled_edges', 'material_doubts'):
            for n, entry in enumerate(_list(item[key], f'{where}.{key}')):
                text(entry, f'{where}.{key}[{n}]')
        _evidence(item['evidence'], where + '.evidence', citable)
        for key in ('complexity', 'intrusiveness'):
            _percent(item[key], f'{where}.{key}')
        for key in ('feasible', 'recommended'):
            require(type(item[key]) is bool, f'{where}.{key}', 'must be boolean')
        loc = fields(item['production_loc'], where + '.production_loc', ('min', 'expected', 'max', 'reason'))
        require(all(type(loc[k]) is int and loc[k] >= 0 for k in ('min', 'expected', 'max'))
                and loc['min'] <= loc['expected'] <= loc['max'], where + '.production_loc',
                'ordered nonnegative integer line counts')
        text(loc['reason'], where + '.production_loc.reason')
        require(isinstance(item['ratings'], dict) and set(item['ratings']) == set(rubric['weights']),
                where + '.ratings', 'rates exactly the persisted rubric criteria')
        for criterion, rating in item['ratings'].items():
            _percent(rating, f'{where}.ratings.{criterion}')
        require(isinstance(item['rating_evidence'], dict) and set(item['rating_evidence']) == set(rubric['weights']),
                where + '.rating_evidence', 'supports every rated criterion')
    if scope == 'branch':
        require(kinds.count('pr') == 1 and 'smallest' in kinds, 'comparison.solutions',
                'branch comparisons retain the current implementation (pr) and include the smallest approach')
    else:
        require(kinds.count('pr') == (1 if has_pr else 0), 'comparison.solutions',
                'the existing PR is the only optional pr candidate; without a PR none is invented')
        require('smallest' in kinds and 'alternative' in kinds, 'comparison.solutions',
                'whole-problem comparisons include the smallest and a materially distinct alternative')
    totals = scores(output, rubric)
    for number, item in enumerate(solutions):
        require('total' not in item or item['total'] == totals[item['id']], f'comparison.solutions[{number}].total',
                'differs from the weighted total')
    return output


def check_selection(output, *, comparison, totals, policy, answer, citable):
    """The shared selection policy; returns the accepted selection with its helper totals."""
    by_id = {item['id']: item for item in comparison['solutions']}
    mode = output.get('mode') if isinstance(output, dict) else None
    require(mode in ('auto', 'manual'), 'selection.mode', 'auto or manual')
    require(answer is None or mode == 'manual', 'selection.mode', 'a recorded user answer settles the choice manually')
    if mode == 'manual':
        fields(output, 'selection', ('mode', 'choice', 'user_source', 'reason', 'evidence', 'rejection_reasons'))
        require(answer is not None and output['user_source'] == answer['answer_id'], 'selection.user_source',
                'a manual choice cites the recorded user answer')
        require(output['choice'] == answer['value'], 'selection.choice', 'differs from the user\'s choice')
    else:
        fields(output, 'selection', ('mode', 'choice', 'confident', 'sensitivity', 'evidence'),
               ('reason', 'rejection_reasons', 'rejected_alternatives'))
        require(type(output['confident']) is bool, 'selection.confident', 'must be boolean')
        text(output['sensitivity'], 'selection.sensitivity')
    _evidence(output['evidence'], 'selection.evidence', citable | ({answer['answer_id']} if answer else set()))
    choice = output['choice']
    if choice is None:
        require(mode == 'auto', 'selection.choice', 'a manual selection names its choice')
        return output
    require(choice in by_id and by_id[choice]['feasible'], 'selection.choice', 'an evaluated feasible option')
    text(output.get('reason'), 'selection.reason')
    reasons = output.get('rejection_reasons')
    require(isinstance(reasons, dict) and set(reasons) == set(by_id) - {choice}, 'selection.rejection_reasons',
            'explains the rejection of every unchosen option')
    for key, reason in reasons.items():
        text(reason, 'selection.rejection_reasons.' + key)
    if mode == 'auto':
        def qualify(condition, message):
            if not condition:
                raise Unqualified('selection.choice', message)
        qualify(policy['mode'] != 'manual' and policy['enabled'], 'automatic picking is disabled; a choice needs '
                'the user')
        qualify(output['confident'] and not by_id[choice]['material_doubts'],
                'a confident winner without unresolved material doubts')
        others = [totals[key] for key, item in by_id.items() if key != choice and item['feasible']]
        qualify(all(totals[choice] - other >= policy['min_score_gap'] for other in others),
                'the winner needs a unique lead of at least min_score_gap')
        if not others:
            rejected = output.get('rejected_alternatives')
            require(isinstance(rejected, dict) and set(rejected) == set(by_id) - {choice},
                    'selection.rejected_alternatives', 'a sole feasible option substantiates every rejection')
    return output


def check_interactions(output, citable):
    fields(output, 'interactions', ('compatible', 'reason', 'evidence'), ('required_changes',))
    require(type(output['compatible']) is bool, 'interactions.compatible', 'must be boolean')
    text(output['reason'], 'interactions.reason')
    _evidence(output['evidence'], 'interactions.evidence', citable)
    require(output['compatible'] or 'required_changes' in output, 'interactions.required_changes',
            'incompatible selections state the required changes')
    if 'required_changes' in output:
        text(output['required_changes'], 'interactions.required_changes')
    return output


def check_current(stage, output, citable):
    """Alignment and verification: the alignment contract plus cited evidence."""
    key = 'alignment' if stage == 'alignment' else 'disposition'
    fields(output, stage, (key, 'reason', 'evidence'), ('required_changes',))
    try:
        records._validate(stage, output)
    except records.AlignmentError as error:
        raise StageError(stage, str(error)) from None
    _evidence(output['evidence'], stage + '.evidence', citable)
    return output


# -- The stage walk -------------------------------------------------------------------------------

class Stop(Exception):
    def __init__(self, status, **detail):
        super().__init__(status)
        self.status, self.detail = status, detail


class Walk:
    """One call's walk over the fixed stages; every decision is the record's.

    sessions supplies the native sessions and gates: run(task) returns (reference,
    session, output); gates(request_id) returns the gate's records; open_gate(request)
    opens one gate and returns its ID. Each raises Unavailable when it cannot supply
    one; anything else it raises (the installed workflow's dispatch and question)
    ends the walk at that point. identity carries the entry's assignment fields;
    inputs carries the instructions, guidance and prompts the stages bind.
    """

    def __init__(self, *, ledger, scope, pins_, requirements, evidence, sessions, request, identity, values,
                 inputs, logical_run, namespace, persisted=True, decisions=None, shared=None):
        self.ledger, self.scope, self.pins, self.requirements = ledger, scope, pins_, requirements
        self.sessions, self.request, self.identity, self.values = sessions, request, identity, values
        self.namespace, self.persisted = namespace, persisted
        # shared() reads the other scope's settled assessment of the same PR, or None.
        self.decisions, self.shared = decisions, shared
        self.instructions = inputs['instructions']
        self.citable = set(pins_['readable']) | {item['source'] for item in requirements} | {
            DECISION_PREFIX + item['id'] for group in ('commitments', 'exceptions')
            for item in (decisions or {}).get(group, [])}
        self.evidence = copy.deepcopy(evidence or [])
        self.digests = {
            'requirements': digest(requirements), 'instructions': digest(inputs['instructions']),
            'guidance': digest(inputs['guidance']), 'prompts': digest(inputs['prompts']),
            'assessment': digest({k: values[k] for k in ('assessment_prompt', 'scoring')}),
            'evaluation': digest(values['evaluation_prompt']), 'policy': digest(values['selection']),
            'evidence': digest(self.evidence)}
        if decisions is not None:
            self.digests['decisions'] = digest(decisions)
        self.stages, self.dispatches, self.selections, self.gates = [], 0, {}, []
        # Architectural stages are settled per PR; current alignment keeps its own allowance per logical run.
        self.account = 'run-' + digest(logical_run)[:24]
        self.pending_status = 'choice_pending'

    # Inputs that bind each stage. Settled stages are reused whatever these become.
    def inputs(self, stage, decision=None, answer=None):
        d = self.digests
        value = {
            'assessment': {k: d[k] for k in ('requirements', 'instructions', 'guidance', 'prompts', 'assessment',
                                             'evaluation')},
            'inventory': {'evaluation': d['evaluation'], 'target': self.pins['target']},
            'comparison': {'evaluation': d['evaluation'], 'target': self.pins['target']},
            'selection': {'policy': d['policy'], 'answer': answer['answer_id'] if answer else ''},
            'interactions': {},
            'alignment': {k: d[k] for k in ('requirements', 'instructions', 'guidance', 'evidence')},
            'verification': {},
        }[stage]
        if self.decisions is not None and stage in ('inventory', 'alignment'):
            # Reconciled decisions bind the inventory (settled like the rest) and current alignment.
            value['decisions'] = d['decisions']
        if self.request is not None and stage in REFRESHED:
            value['request'] = self.request['id']
        return value

    def reusable(self, stage, decision=None):
        """A settled architectural result whose consumed results are unchanged; never for a requested refresh."""
        if self.request is not None and stage in REFRESHED:
            return None
        latest = self.ledger.latest(records.key(stage, decision))
        if (latest is None or stage not in records.SETTLED or not records.Ledger._settled(stage, latest)
                or latest['dependencies'] != self.ledger.dependencies(stage, decision)):
            return None
        return latest

    def stage(self, stage, decision=None, *, context, check, answer=None):
        stage_key = records.key(stage, decision)
        found = self.reusable(stage, decision)
        if found is not None:
            self.stages.append({'key': stage_key, 'action': 'reuse', 'reference': found['reference']})
            return found['result']
        shared = self.adopt(stage, check)
        if shared is not None:
            return shared
        while True:
            try:
                return self.attempt(stage, decision, stage_key, context=context, check=check, answer=answer)
            except Stop as stop:
                # Without a record nothing remembers a rejection across calls: an issue-only run walks
                # its claims (replays cost no session) and retries a rejected attempt in the same call,
                # up to ISSUE_ATTEMPTS attempts per stage. A PR record retries on the next call.
                if (self.persisted or not stop.detail.get('rejected')
                        or len(self.ledger.history(stage_key)) >= ISSUE_ATTEMPTS):
                    raise

    def adopt(self, stage, check):
        """The other scope's settled assessment of this PR, adopted when this record has none.

        It must be bound to this run's assessment inputs (the origin's binding equals this
        record's) and pass this walk's own checks (for example, cite files of this pinned
        head); otherwise this scope runs its own assessment.
        """
        if (stage not in records.SHARED or self.shared is None or self.ledger.latest(stage) is not None
                or self.ledger.open_claim(stage) is not None):
            return None
        origin = self.shared()
        if origin is None:
            return None
        # Only an assessment of this run's own inputs (requirements, instructions, guidance, prompts,
        # assessment and scoring options, evaluation prompt) is adopted; the binding digests them.
        _, binding = self.ledger.binding(stage, inputs=self.inputs(stage), head=self.pins['head'])
        if origin['binding'] != binding:
            return None
        try:
            check(copy.deepcopy(origin['result']))
        except (StageError, TypeError, KeyError, AttributeError, ValueError):
            return None
        record = self.ledger.adopt(stage, inputs=self.inputs(stage), head=self.pins['head'], origin=origin)
        self.stages.append({'key': stage, 'action': 'shared', 'reference': record['reference'],
                            'origin': origin['scope'] + ':' + origin['reference']})
        return record['result']

    def attempt(self, stage, decision, stage_key, *, context, check, answer):
        inputs = self.inputs(stage, decision, answer)
        head = self.pins['head']
        _, binding = self.ledger.binding(stage, decision, inputs=inputs, head=head)
        # A new attempt gets a new engine operation; a replay finds its claim or result first.
        operation = 'alt-' + digest({'namespace': self.namespace, 'binding': binding,
                                     'attempt': len(self.ledger.history(stage_key))})[:40]
        request = self.request if self.request is not None and stage in REFRESHED else None
        account = self.account if stage in records.CURRENT else None
        claim = self.ledger.open_claim(stage_key)
        if claim is not None and claim['request'] is not None and request is None:
            # An interrupted direct request owns this attempt; an automatic route never supersedes it.
            raise Stop('authorization_required', stage=stage_key, skill=records.DIRECT_SKILL,
                       message=f'{stage_key} belongs to interrupted direct request {claim["request"]}; '
                               f'resume it through {records.DIRECT_SKILL}')
        try:
            action = self.ledger.prepare(stage, decision, inputs=inputs, head=head, operation=operation,
                                         request=request, account=account)
        except records.AuthorizationRequired as error:
            raise Stop('authorization_required', stage=stage_key, skill=records.DIRECT_SKILL,
                       message=str(error)) from None
        except records.AllowanceExhausted as error:
            raise Stop('exhausted', stage=stage_key,
                       allowance=self.ledger.usage(records.allowance(stage, account)),
                       message=str(error)) from None
        self.stages.append({'key': stage_key, 'action': action['action'], 'reference': action['reference']})
        if action['action'] == 'reuse':
            return action['result']
        task = self.assignment(stage, decision, action['operation'], context)
        try:
            reference, session_id, output = self.sessions.run(task)
        except Unavailable as error:
            if error.rejected:
                self.ledger.abandon(action['operation'])
                raise Stop('blocked', stage=stage_key, error=error.failure, rejected=True) from None
            # The transport failed: the session may have run, so its claim stays open to resume.
            raise Stop('blocked', stage=stage_key, error=error.failure) from None
        try:
            try:
                check(output)
                accepted = output
            except Unqualified as error:
                # An automatic pick the policy cannot accept settles nothing: the attempt records no
                # choice and keeps the session's original output. Automatic mode stays pending;
                # manual and semi-manual ask the user.
                accepted = {'mode': 'auto', 'choice': None, 'unqualified': {
                    'path': error.path, 'message': error.message, 'original': copy.deepcopy(output)}}
            record = self.ledger.finish(action['operation'], accepted, session=session_id)
        except (StageError, records.AlignmentError, TypeError, KeyError, AttributeError, ValueError) as error:
            # A rejected result never becomes current, and the next call starts a new attempt
            # instead of resuming it; the attempt's reservation stays consumed.
            self.ledger.abandon(action['operation'])
            reason = ({'path': stage_key, 'message': str(error)} if isinstance(error, records.AlignmentError)
                      else failure(error))
            raise Stop('blocked', stage=stage_key, error=reason, rejected=True) from None
        self.dispatches += 1
        self.stages[-1].update(evidence='native:' + reference, session=session_id)
        return record['result']

    def assignment(self, stage, decision, claim, context):
        identity = self.identity
        verifier = stage == 'verification'
        return {
            'schema_version': 1, 'kind': 'alternatives_stage', 'suite': records.SUITE,
            'entry': identity['entry'], 'skill': identity['skill'], 'scope': self.scope,
            'mode': stage, 'decision': decision, 'claim': claim, 'adapter': identity['adapter'],
            'role': 'verifier' if verifier else identity['role'],
            'definition_sha256': identity['verifier_sha256'] if verifier else identity['definition_sha256'],
            'assignment': identity['assignment'], 'checklist': identity['checklist'],
            'role_prompt': identity['role_prompt'], 'stage_prompt': STAGE_PROMPTS[stage],
            'instructions': self.instructions, 'prompts': identity['prompts'], 'options': copy.deepcopy(self.values),
            'agent': identity['agent'], 'provider': identity['provider'], 'skills': [], 'tools': 'read-only',
            'writes': False, 'publication': False, 'readable': dict(sorted(self.pins['readable'].items())),
            'context': {'pins': {k: self.pins[k] for k in ('target', 'head', 'pr', 'changed')},
                        'requirements': self.requirements, **copy.deepcopy(context)},
        }

    # -- Stages ----------------------------------------------------------------------------------

    def analysis(self):
        """assessment, inventory, a comparison and selection per questionable decision, interactions."""
        assessment = self.stage('assessment', context={}, check=lambda out: check_assessment(out, self.citable))
        context = {'assessment': assessment}
        if self.decisions is not None:
            context['decisions'] = copy.deepcopy(self.decisions)
        inventory = self.stage('inventory', context=context, check=lambda out: check_inventory(
            out, scope=self.scope, assessment=assessment, pins_=self.pins, citable=self.citable))
        rubrics = {rubric['id']: rubric for rubric in assessment['rubrics']}
        pending = []
        for decision in [item for item in inventory['decisions'] if item['questionable']]:
            rubric = rubrics[decision['rubric']]
            comparison = self.stage('comparison', decision['id'], context={
                'assessment': assessment, 'decision': decision, 'rubric': rubric},
                check=lambda out, d=decision, r=rubric: check_comparison(
                    out, scope=self.scope, decision=d, rubric=r, target=self.pins['target'],
                    has_pr=self.pins['pr'] is not None, citable=self.citable))
            totals = scores(comparison, rubric)
            selection = self.select(decision, comparison, totals)
            self.selections[decision['id']] = {'choice': selection['choice'], 'mode': selection['mode'],
                                               'scores': totals}
            if selection['choice'] is None:
                pending.append(decision['id'])
        if pending:
            raise Stop(self.pending_status, decisions=pending, gates=self.gates)
        return self.stage('interactions', context={'assessment': assessment, 'inventory': inventory,
                                                   'selections': self.selections},
                          check=lambda out: check_interactions(out, self.citable))

    def gate_id(self, comparison_ref, round_):
        return 'altchoice_' + digest({'namespace': self.namespace, 'comparison': comparison_ref, 'round': round_,
                                      'request': self.request['id'] if self.request else None})[:32]

    def select(self, decision, comparison, totals):
        """Sourced selection; manual and semi-manual ask the user through an engine gate, automatic never."""
        stage_key = records.key('selection', decision['id'])
        policy = self.values['selection']
        found = self.reusable('selection', decision['id'])
        if found is not None:
            self.stages.append({'key': stage_key, 'action': 'reuse', 'reference': found['reference']})
            return found['result']
        comparison_ref = self.ledger.latest(records.key('comparison', decision['id']))['reference']
        answer, round_, rejected = None, 0, []
        if policy['mode'] != 'automatic':
            # Gates are found by their own identity, so issue-only runs without a record find them too.
            feasible = {item['id'] for item in comparison['solutions'] if item['feasible']}
            while True:
                gates = self.read_gates(self.gate_id(comparison_ref, round_))
                if any(gate['status'] == 'waiting' for gate in gates):
                    # An open question resumes without a new session or gate.
                    self.pending_status = 'waiting'
                    self.gates.append({'decision': decision['id'], 'request_id': self.gate_id(comparison_ref, round_),
                                       'status': 'waiting'})
                    return {'choice': None, 'mode': 'manual'}
                answered = [gate['answer'] for gate in gates if gate['status'] == 'answered']
                if not answered:
                    break
                value = answered[-1]['value']
                if isinstance(value, str) and value in feasible:
                    answer = answered[-1]
                    break
                # Not an evaluated feasible option: ask again in a new round, never dispatch it.
                rejected.append(answered[-1]['answer_id'])
                round_ += 1
                if round_ > MAX_ROUNDS:
                    raise Stop('blocked', stage=stage_key, error={'path': 'gates', 'message':
                                                                  'too many answers that name no feasible option'})
        result = self.stage('selection', decision['id'], answer=answer, context={
            'comparison': comparison, 'scores': totals, 'policy': policy,
            'user_choice': copy.deepcopy(answer)},
            check=lambda out: check_selection(out, comparison=comparison, totals=totals, policy=policy,
                                              answer=answer, citable=self.citable))
        if result['choice'] is None and policy['mode'] != 'automatic':
            request = {'request_id': self.gate_id(comparison_ref, round_), 'evidence_id': self.stages[-1]['reference'],
                       'prompt': 'Choose one evaluated feasible option for this decision.',
                       'payload': {'decision': decision['id'], 'scores': totals, 'rejected_answers': rejected,
                                   'options': [{'id': item['id'], 'kind': item['kind'], 'approach': item['approach'],
                                                'feasible': item['feasible']} for item in comparison['solutions']]}}
            try:
                gate = self.sessions.open_gate(copy.deepcopy(request))
            except Unavailable as error:
                raise Stop('blocked', stage=stage_key, error=error.failure) from None
            self.pending_status = 'waiting'
            self.gates.append({'decision': decision['id'], 'request_id': request['request_id'],
                               'gate_id': gate, 'status': 'waiting'})
        return result

    def read_gates(self, gate_id):
        try:
            return self.sessions.gates(gate_id)
        except Unavailable as error:
            raise Stop('blocked', stage='gates', error=error.failure) from None

    def current(self, verify):
        """Current alignment at the pinned head under current requirements; verification on a mismatch."""
        settled = {key: self.ledger.latest(key) for key in ('assessment', 'interactions')}
        context = {'assessment': settled['assessment']['result'], 'interactions': settled['interactions']['result'],
                   'selections': self.selections, 'evidence': self.evidence,
                   'references': [item['reference'] for item in self.stages if item['key'] != 'alignment']}
        if self.decisions is not None:
            context['decisions'] = copy.deepcopy(self.decisions)
        earlier = self.ledger.latest('alignment')
        if earlier is not None:
            context['earlier'] = {'reference': earlier['reference'], 'head': earlier['head'],
                                  'result': earlier['result']}
        result = self.stage('alignment', context=context, check=lambda out: check_current('alignment', out,
                                                                                            self.citable))
        if verify and result['alignment'] == 'mismatch':
            self.stage('verification', context={'alignment': result, 'selections': self.selections},
                       check=lambda out: check_current('verification', out, self.citable))
        return result

    def run(self, through, reconciled_=None):
        """The walk through `analysis`, `alignment` (the fix handoff) or `verification` (the gate)."""
        outcome = {'status': 'complete'}
        try:
            interactions = self.analysis()
            outcome['interactions'] = interactions
            if not interactions['compatible']:
                raise Stop('interactions_blocked', required_changes=interactions['required_changes'])
            if through != 'analysis':
                current = self.current(verify=through == 'verification')
                outcome['alignment'] = current
                if through == 'verification':
                    outcome['gate'] = records.gate(self.ledger, head=self.pins['head'],
                                                   inputs=self.inputs('alignment'), account=self.account)
                    outcome['status'] = outcome['gate']['outcome']
                elif reconciled_ is not None and reconciled_['dependent_work'] == 'blocked':
                    # Unresolved reconciliation blocks dependent implementation, not the analysis.
                    outcome.update(status='decisions_blocked', unresolved=reconciled_['unresolved'],
                                   questions=reconciled_['questions'])
                else:
                    # The fix route hands settled choices and required changes to authorized tasks.
                    outcome['handoff'] = {
                        'choices': {key: value['choice'] for key, value in self.selections.items()},
                        'alignment': current['alignment'],
                        'required_changes': current.get('required_changes'),
                        'tests_required': current['alignment'] == 'mismatch'}
        except Stop as stop:
            outcome = {'status': stop.status, **stop.detail}
        return outcome

    def usage(self):
        return {'architecture': self.ledger.usage('architecture'),
                'alignment': self.ledger.usage(records.allowance('alignment', self.account))}


# -- Records --------------------------------------------------------------------------------------

def pr_ledger(state, scope, pr):
    """A PR's record of one scope: once per PR across routes, runs and sessions."""
    run = records.pr_run(pr['url'])
    return records.Ledger(records.ledger_path(state, run, scope), logical_run=run, pr=pr, scope=scope)


def issue_ledger(directory, repository):
    """Issue-only analysis: a disposable record, removed after the call; no PR identity."""
    return records.Ledger(Path(directory) / 'issue.alignment.sqlite', logical_run='issue',
                          subject='repository:' + repository, scope='whole-problem')


def shared(state, scope, pr):
    """A reader of the other scope's own settled assessment of the same PR; it never creates a record."""
    other = next(item for item in records.SCOPES if item != scope)
    run = records.pr_run(pr['url'])
    path = records.ledger_path(state, run, other)

    def read():
        if not path.is_file():
            return None
        try:
            ledger = records.Ledger(path, logical_run=run, pr=pr, scope=other)
        except records.AlignmentError:
            return None  # An unreadable sibling shares nothing; this scope runs its own assessment.
        try:
            found = ledger.latest('assessment')
        finally:
            ledger.close()
        if found is None or found['operation'].startswith(records.SHARED_PREFIX):
            return None  # Adopted from this record, which then has it already.
        return {'scope': other, 'operation': found['operation'], 'session': found['session'],
                'binding': found['binding'], 'reference': found['reference'], 'result': found['result']}
    return read


def selections(state, pr):
    """Advised and selected alternatives of a PR, read-only from both scopes' records.

    For each settled comparison: its recommended options and, once chosen, the
    selected option with its approach. These are advisory decision sources
    (decisions.py): they never settle or resolve a decision on their own.
    """
    result = []
    run = records.pr_run(pr['url'])
    for scope in records.SCOPES:
        path = records.ledger_path(state, run, scope)
        if not path.is_file():
            continue
        try:
            with records.Records.open(path, logical_run=run, pr_url=pr['url'], scope=scope) as ledger:
                try:
                    questionable = ledger.questionable()
                except records.AlignmentError:
                    continue  # No settled inventory yet: nothing has been advised.
                for decision in questionable:
                    comparison = ledger.latest(records.key('comparison', decision))
                    if comparison is None or comparison['dependencies'] != ledger.dependencies('comparison', decision):
                        continue
                    options = {item['id']: item for item in comparison['result']['solutions']}
                    chosen = ledger.latest(records.key('selection', decision))
                    choice = None
                    if (chosen is not None and chosen['result']['choice'] is not None
                            and chosen['dependencies']['comparison'] == comparison['reference']):
                        choice = chosen['result']['choice']
                    result.append({'scope': scope, 'decision': decision, 'comparison': comparison['reference'],
                                   'advised': sorted(key for key, item in options.items() if item['recommended']),
                                   'selected': choice,
                                   'approach': options[choice]['approach'] if choice in options else None,
                                   'reference': chosen['reference'] if choice is not None else None})
        except records.AlignmentError as error:
            raise ValueError('retained alternatives analysis is unreadable: ' + str(error)) from error
    return result


def decision_sources(selected):
    """Advised and selected alternatives as decision sources (kind ``alternatives``).

    Their authority is ``advisory``: a recommendation or a selection never settles,
    excludes or resolves a decision by itself, so a contradiction with a settled
    user or maintainer decision is asked. The text binds the comparison and
    selection references, so a refreshed choice refreshes only what cites it.
    """
    sources = []
    for item in selected:
        body = {key: item[key] for key in ('scope', 'decision', 'advised', 'selected', 'approach', 'comparison',
                                           'reference')}
        sources.append({'id': 'alternatives:' + item['scope'] + ':' + item['decision'], 'kind': 'alternatives',
                        'public': False, 'url': None, 'text': records.canonical(body).decode('utf-8'),
                        'author': None, 'authority': 'advisory', 'created_at': None})
    return sources


# -- Installed procedure (workflows/alternatives.yml) ----------------------------------------------

KIND = 'alternatives'
# Default options of both entries (their baseline config.json; cf_workflows.entries.ALTERNATIVES_OPTIONS).
_ASSESSMENT = ('Assess purpose, requirements and repository evidence before comparing approaches. For small fixes '
               'favor focused implementation changes and relevant edge-case coverage. For features exclude code-change '
               'volume from ranking and assess design and maintainability through actual consequences. For mixed work '
               'identify scopes and assign applicable rubrics. Select criteria, positive percentage weights totaling '
               '100 and explicit 0/50/100 anchors, rationale, mandatory constraints and original source references. '
               'Judge architectural decisions with that rubric and supplied evaluation guidance, without a predefined '
               'defect checklist.')
OPTIONS = {
    'assessment_prompt': _ASSESSMENT,
    'evaluation_prompt': '',
    'scoring': {
        'profile': 'auto',
        'profile_selection_prompt': 'Assess purpose and scope to select criteria, percentage weights and scoring '
                                    'anchors with an evidence-backed rationale. Favor focused changes and relevant '
                                    'edge coverage for small fixes; exclude change volume when ranking feature '
                                    'approaches. Assign applicable rubrics to mixed scopes.',
        'custom_prompt': None,
    },
    'selection': {
        'enabled': True,
        'prompt': 'Assess confidence and sensitivity to LOC and complexity estimates. Accept a clear winner only with '
                  'supported feasibility, adequate score lead and no unresolved material doubts. For a sole feasible '
                  'option substantiate rejection of every alternative.',
        'min_score_gap': 10,
        'mode': 'semi-manual',
    },
}
PROFILES = ('auto', 'minimal_change_edge_coverage', 'quality', 'minimal_change')
MODES = ('automatic', 'manual', 'semi-manual')
# Baseline role prompts (prompts/default.md; digests in baselines/inventory.json).
ROLE_PROMPTS = {
    'dk-issue-alternatives': 'Compare whole-problem implementation approaches from issue requirements, optionally '
                             'including an existing PR.\n',
    'dk-pr-alternatives': 'Inventory branch architecture including inherited decisions, then compare questionable '
                          'decisions under the shared PR rubric.\n',
    'dk-alternatives-review': 'Compatibility entry for dk-issue-alternatives and retained whole-problem managed '
                              'assignments.\n',
    'dk-pr-review-alternatives': 'Manual entry point for alternatives-only PR review. Run and record a fresh review '
                                 'on demand, including after an earlier completed run.\n',
}
# Each installed entry: its delegate per route and the depth it runs to.
ENTRIES = {
    'dk-issue-alternatives': {'whole-problem': 'dk-issue-alternatives'},
    'dk-pr-alternatives': {'branch': 'dk-pr-alternatives'},
    'dk-alternatives-review': {'whole-problem': 'dk-issue-alternatives', 'branch': 'dk-pr-alternatives'},
    'dk-pr-review-alternatives': {'branch': 'dk-pr-alternatives'},
}
# Outcomes that settle the occurrence; any other stops its stage.
SETTLED = {'analysis': ('complete',), 'alignment': ('complete',),
           'verification': ('released', 'mismatch_verified', 'alignment_uncertain', 'alignment_blocked')}
# The native stage report (workflows/alternatives.yml): the stage's output as one JSON object.
NATIVE_CONTRACT = ('Return {"stage": <the assignment mode>, "result": <the stage output as one JSON-encoded object>}. '
                   'The result follows the stage prompt and cites only the requirement sources, reconciled '
                   '`decision:` entries and `git:<path>[:<lines>]` files of the assignment. Read only; do not edit, '
                   'commit, publish or load skills.')


def role(task):
    return task.get('kind') == KIND


def occurrence(plan):
    """The plan's one alternatives occurrence, or None."""
    tasks = [task for task in plan.get('tasks', []) if role(task)]
    if len(tasks) > 1:
        raise ValueError('a plan runs the alternatives procedure in exactly one occurrence')
    return tasks[0] if tasks else None


def _merge(declared, value, path):
    if isinstance(declared, dict):
        require(isinstance(value, dict) and not set(value) - set(declared), path,
                'supports ' + ', '.join(sorted(declared)) + ' only')
        return {key: _merge(declared[key], value[key], path + '.' + key) if key in value else copy.deepcopy(item)
                for key, item in declared.items()}
    return copy.deepcopy(value)


def settings(raw, *, entry, mode, adapter, request):
    """The installed occurrence's validated settings (``settings.alternatives``).

    entry is the direct entry ('' for a coordinator's composed occurrence). Prompt options keep
    their baseline text: a custom prompt needs the shared semantic validator, which the installed
    workflow does not run. A retained legacy assignment is not imported.
    """
    raw = {} if raw is None else raw
    require(isinstance(raw, dict) and set(raw) <= {'route', 'requirements', 'options', 'legacy'},
            'settings.alternatives', 'supports route, requirements and options only')
    require('legacy' not in raw, 'settings.alternatives.legacy', 'retained dk-workflows assignments are not '
            'imported; resume them in their original installation')
    if entry:
        routes = ENTRIES[entry]
        if entry == 'dk-alternatives-review':
            require(raw.get('route') in routes, 'settings.alternatives.route',
                    'the compatibility entry needs an explicit route: whole-problem or branch')
            route = raw['route']
        else:
            require('route' not in raw, 'settings.alternatives.route', 'only the compatibility entry takes a route')
            route = next(iter(routes))
        delegate = routes[route]
        through = 'verification' if entry == 'dk-pr-review-alternatives' else 'analysis'
        caller = None
    else:
        require('route' not in raw, 'settings.alternatives.route', 'only the compatibility entry takes a route')
        delegate = next(name for name, value in ADAPTERS.items() if value == adapter)
        # dk-pr-fix composes whole-problem analysis on its PR; dk-pr-review composes branch analysis.
        caller = 'cf-workflows:dk-pr-fix' if mode == 'fix' else 'cf-workflows:dk-pr-review'
        require(COMPOSERS[caller] == SCOPES[delegate], 'options.tasks',
                caller + ' composes only ' + ('issue-alternatives' if mode == 'fix' else 'pr-alternatives'))
        through = 'alignment' if mode == 'fix' else 'verification'
    require(ADAPTERS[delegate] == adapter, 'options.tasks', 'cf-workflows:' + delegate + ' runs the '
            + ADAPTERS[delegate] + ' adapter')
    values = _merge(OPTIONS, raw.get('options', {}), 'settings.alternatives.options')
    for dotted in ('assessment_prompt', 'evaluation_prompt', 'scoring.profile_selection_prompt',
                   'scoring.custom_prompt', 'selection.prompt'):
        first, _, second = dotted.partition('.')
        value, default = (values[first], OPTIONS[first]) if not second else (values[first][second],
                                                                            OPTIONS[first][second])
        require(value == default, 'settings.alternatives.options.' + dotted,
                'a custom prompt needs the shared semantic validator; the installed workflow runs the baseline '
                'prompts only')
    require(values['scoring']['profile'] in PROFILES, 'settings.alternatives.options.scoring.profile',
            'one of ' + ', '.join(PROFILES))
    selection = values['selection']
    require(type(selection['enabled']) is bool, 'settings.alternatives.options.selection.enabled', 'must be boolean')
    require(selection['mode'] in MODES, 'settings.alternatives.options.selection.mode', 'one of ' + ', '.join(MODES))
    require(type(selection['min_score_gap']) is int and 0 <= selection['min_score_gap'] <= 100,
            'settings.alternatives.options.selection.min_score_gap', 'an integer from 0 to 100')
    scope = SCOPES[delegate]
    reqs = requirements(raw.get('requirements'), scope)
    direct = None
    if entry == 'dk-pr-review-alternatives':
        # The staged request is the distinct direct user invocation of this entry; replaying it repeats nothing.
        ident = 'request-' + digest(request)[:32]
        direct = {'id': ident, 'source': 'staged-request:' + ident, 'instruction': request['instructions'],
                  'skill': records.DIRECT_SKILL, 'origin': 'user'}
    return {'entry': entry or None, 'delegate': delegate, 'scope': scope, 'through': through, 'caller': caller,
            'values': values, 'requirements': reqs, 'direct': direct}


class Dispatch(Exception):
    """The walk needs this native stage session."""

    def __init__(self, task):
        super().__init__(task['claim'])
        self.task = task


class Ask(Exception):
    """The walk needs the user's choice through one engine input gate."""

    def __init__(self, request):
        super().__init__(request['request_id'])
        self.request = request


class _Installed:
    """Sessions of the installed workflow: a delivered native result, or the next dispatch or question."""

    def __init__(self, ledger, delivered=None):
        self.ledger, self.delivered, self.consumed = ledger, delivered, False

    def run(self, task):
        if self.delivered is None or self.delivered['claim'] != task['claim']:
            raise Dispatch(task)
        self.consumed = True
        output = self.delivered['output']
        if output is None:
            raise Unavailable(self.delivered['failure'], rejected=True)
        if self.delivered['stage'] != task['mode']:
            raise Unavailable({'path': 'report.stage', 'message': 'the report names another stage than its claim'},
                              rejected=True)
        return self.delivered['agent'], self.delivered['agent'], output

    def gates(self, request_id):
        return self.ledger.answers(request_id)

    def open_gate(self, request):
        raise Ask(request)


def _handoff(root, ledger, plan):
    """The decisions handoff in the shape the inventory consumes, or None without a decisions occurrence."""
    from . import decisions
    handed = decisions.handoff(root, ledger, plan)
    if handed is None:
        return None
    return {'result_ref': handed['result'], **{key: handed[key] for key in (
        'commitments', 'exceptions', 'unresolved', 'questions', 'dependent_work')}}


def _walk(root, provider, ledger, plan, task, delivered=None):
    """The occurrence's walk at the current snapshot. Returns (walk, record, setting, outcome-or-step)."""
    from . import app
    from .core import state_directory
    setting = task['alternatives']
    snapshot = app._snapshot(root, provider, plan)
    try:
        handed = reconciled(_handoff(root, ledger, plan))
        pinned = pins(root, scope=setting['scope'], pr=plan['pr'], target=snapshot['base'], head=snapshot['head'],
                      priority=['AGENTS.md'])
    except StageError as error:
        raise ValueError('alternatives inputs: ' + str(error)) from None
    state = state_directory(root)
    record = pr_ledger(state, setting['scope'], plan['pr'])
    identity = {'entry': 'cf-workflows:' + (setting['entry'] or setting['delegate']),
                'skill': 'cf-workflows:' + setting['delegate'], 'adapter': task['skill'], 'role': task['definition'],
                'definition_sha256': task['definition_digest'],
                'verifier_sha256': plan['definitions']['verifier'], 'assignment': task['assignment'],
                'checklist': task['checklist'], 'role_prompt': ROLE_PROMPTS[setting['delegate']],
                'prompts': task.get('prompts', []), 'agent': task['definition'], 'provider': 'engine'}
    sessions = _Installed(record, delivered)
    walk = Walk(ledger=record, scope=setting['scope'], pins_=pinned, requirements=setting['requirements'],
                evidence=[], sessions=sessions, request=setting['direct'], identity=identity,
                values=setting['values'], logical_run=plan['logical_run'], namespace=str(record.path.resolve()),
                inputs={'instructions': plan['instructions'], 'guidance': snapshot['instructions'],
                        'prompts': task.get('prompts', [])},
                decisions=handed, shared=shared(state, setting['scope'], plan['pr']))
    try:
        outcome = walk.run(setting['through'], handed)
    except Dispatch as needed:
        outcome = {'status': 'dispatch', 'task': needed.task}
    except Ask as asked:
        outcome = {'status': 'ask', 'request': asked.request}
    except BaseException:
        record.close()
        raise
    if delivered is not None and not sessions.consumed:
        record.close()
        raise ValueError('the native result belongs to a claim this procedure is not waiting for')
    return walk, record, outcome


def _summary(walk, outcome):
    return {key: value for key, value in outcome.items() if key not in ('task', 'request')} | {
        'stages': walk.stages, 'dispatches': walk.dispatches, 'selections': walk.selections}


def next_step(root, provider, ledger, plan, inputs):
    """The next native stage, selection gate or the settled outcome of the occurrence."""
    task = _task(plan, inputs)
    walk, record, outcome = _walk(root, provider, ledger, plan, task)
    try:
        usage = walk.usage()
    finally:
        record.close()
    base = {'invoke': False, 'ask': False, 'settled': False, 'claim': '', 'definition': '', 'prompt': '',
            'task_input': {}, 'evidence_id': '', 'payload': {}}
    if outcome['status'] == 'dispatch':
        assignment = outcome['task']
        prompt = (assignment['stage_prompt'] + '\n' + task['assignment'] + '\n' + NATIVE_CONTRACT)
        return {**base, 'invoke': True, 'claim': assignment['claim'], 'stage': assignment['mode'],
                'decision': assignment['decision'] or '', 'definition': assignment['role'], 'prompt': prompt,
                'task_input': assignment}
    if outcome['status'] == 'ask':
        request = outcome['request']
        return {**base, 'ask': True, 'evidence_id': request['request_id'], 'prompt': request['prompt'],
                'payload': {**request['payload'], 'selection': request['evidence_id']}}
    return {**base, 'settled': True, 'status': outcome['status'], 'outcome': _summary(walk, outcome),
            'allowances': usage}


def _task(plan, inputs):
    task = occurrence(plan)
    if task is None or inputs.get('task') != task['id']:
        raise ValueError('the alternatives procedure belongs to the plan\'s one alternatives occurrence')
    return task


def _decode(inputs):
    """The delivered native stage result: (claim, agent, output or None, failure)."""
    claim = text(inputs.get('claim'), 'accept.claim', 256)
    status = inputs.get('status')
    agent = inputs.get('agent')
    report = inputs.get('report')
    if status != 'succeeded' or not isinstance(agent, str) or not agent or not isinstance(report, dict):
        return claim, agent if isinstance(agent, str) and agent else 'unobserved:' + claim, None, {
            'path': 'session', 'message': 'native stage session ended without a result: ' + str(status)}
    try:
        fields(report, 'report', ('stage', 'result'))
        output = json.loads(text(report['result'], 'report.result', 1 << 20))
        require(isinstance(output, dict), 'report.result', 'must encode one JSON object')
    except (StageError, ValueError, RecursionError) as error:
        return claim, agent, None, failure(error) if isinstance(error, StageError) else {
            'path': 'report.result', 'message': 'not JSON: ' + type(error).__name__}
    return claim, agent, (report['stage'], output), None


def accept(root, provider, ledger, plan, inputs):
    """Check one native stage result and record it; a rejected result stops this run's procedure.

    The result belongs to the claim the procedure is waiting for. Its session is the
    engine's agent reference, so verification is independent of the alignment it checks.
    A rejection closes the attempt (its reservation stays consumed); the next run starts
    a new one.
    """
    task = _task(plan, inputs)
    claim, agent, decoded, reason = _decode(inputs)
    stage, output = decoded if decoded is not None else (None, None)
    delivered = {'claim': claim, 'agent': agent, 'stage': stage, 'output': output, 'failure': reason}
    _, record, outcome = _walk(root, provider, ledger, plan, task, delivered)
    record.close()
    rejected = outcome['status'] == 'blocked' and outcome.get('rejected')
    return {'accepted': not rejected, 'stop': bool(rejected), 'claim': claim,
            **({'error': outcome['error'], 'stage': outcome['stage']} if rejected else {})}


def answer(root, provider, ledger, plan, inputs):
    """Record the user's answer to the selection gate the procedure is asking; replay is a no-op."""
    task = _task(plan, inputs)
    request_id = text(inputs.get('evidence_id'), 'answer.evidence_id', 256)
    walk, record, outcome = _walk(root, provider, ledger, plan, task)
    try:
        recorded = record.answers(request_id)
        if not recorded:
            require(outcome['status'] == 'ask' and outcome['request']['request_id'] == request_id,
                    'answer.evidence_id', 'the procedure is not asking this question')
        require(inputs.get('answer') is not None, 'answer', 'requires one JSON value')
        record.answer(request_id, gate_id=text(inputs.get('gate_id'), 'answer.gate_id', 256),
                      answer_id=text(inputs.get('answer_id'), 'answer.answer_id', 256), value=inputs['answer'])
    except records.AlignmentError as error:
        raise ValueError(str(error)) from None
    finally:
        record.close()
    return {'recorded': True, 'evidence_id': request_id}


def binding(ledger, snapshot, task):
    """The occurrence's coverage: the settled state of its record at the analyzed head.

    It also binds the consumed decisions handoff, so a refreshed reconciliation walks
    the procedure again (only current alignment binds it once the inventory settled).
    """
    from . import decisions
    from .core import state_directory
    if ledger is None:
        raise ValueError('alternatives coverage needs the run ledger')
    setting = task['alternatives']
    state = state_directory(ledger.root, missing_ok=True)
    stages = []
    if state is not None:
        run = records.pr_run(snapshot['pr']['url'])
        path = records.ledger_path(state, run, setting['scope'])
        if path.is_file():
            with records.Records.open(path, logical_run=run, pr_url=snapshot['pr']['url'],
                                      scope=setting['scope']) as record:
                stages = [row[0] for row in record.db.execute(
                    "SELECT key || '@' || operation FROM attempts WHERE state = 'complete' ORDER BY seq")]
    return digest({'task': {key: value for key, value in task.items() if key != 'required'},
                   'head': snapshot['head'], 'base': snapshot['base'], 'instructions': snapshot['instructions'],
                   'decisions': decisions.reference(ledger), 'stages': stages})


def status(root, provider, ledger, plan, inputs):
    """Complete the occurrence once its procedure settled; anything else stops the stage.

    The occurrence completes with a helper record and no further session, like a
    settled prose occurrence. Its coverage binds the record's settled stages and the
    analyzed head, so a later head walks the procedure again: settled analysis is
    reused and only current alignment refreshes.
    """
    from . import app
    from .core import FORMAT, artifact
    task = _task(plan, inputs)
    snapshot = app._snapshot(root, provider, plan)
    walk, record, outcome = _walk(root, provider, ledger, plan, task)
    record.close()
    setting = task['alternatives']
    summary = _summary(walk, outcome)
    if outcome['status'] not in SETTLED[setting['through']]:
        detail = outcome.get('error') or outcome.get('message') or outcome.get('stage') or outcome.get('decisions')
        raise ValueError('the alternatives procedure did not settle: ' + outcome['status']
                         + (' (' + json.dumps(detail, sort_keys=True) + ')' if detail else ''))
    coverage = app._fingerprint(snapshot, task, ledger)
    current = app._current(ledger, snapshot, task)
    if current is not None:
        return {'complete': True, 'claim': current['id'], 'report': current['report'], 'status': outcome['status'],
                'outcome': summary}
    key = 'claim_' + digest([plan['logical_run'], task['id'], coverage])
    assignment = artifact(root, {'format': FORMAT, 'claim': key, 'task': task, 'snapshot': snapshot,
                                 'instructions': plan['instructions'], 'alternatives': setting})
    reference = artifact(root, {'claim': key, 'agent': '', 'round': 0, 'report': None,
                                'alternatives': summary})
    with ledger.transaction():
        if not ledger.db.execute('SELECT 1 FROM claims WHERE id=?', (key,)).fetchone():
            ledger.db.execute('INSERT INTO claims(id,task,fingerprint,state,assignment,report,coverage) '
                              'VALUES(?,?,?,?,?,?,?)', (key, task['id'], coverage, 'complete', assignment,
                                                        reference, coverage))
    return {'complete': True, 'claim': key, 'report': reference, 'status': outcome['status'], 'outcome': summary}


def handed(root, ledger, plan, snapshot):
    """What a composed dk-pr-fix run hands its fix occurrences: the settled choices and alignment, or None."""
    from . import app
    from .core import read_artifact
    task = occurrence(plan)
    if task is None or task['alternatives']['through'] != 'alignment':
        return None
    current = app._current(ledger, snapshot, task)
    if current is None:
        return None
    value = read_artifact(root, current['report'])['alternatives']
    return {'report': current['report'], 'handoff': value.get('handoff'), 'selections': value['selections']}


def reference(ledger, task, snapshot):
    """The alternatives report a composed fix occurrence is handed; None unless the procedure's coverage is current.

    Like handed(): a claim whose coverage the current head, guidance, reconciliation or
    record stages no longer match is never bound, so a fix fingerprint never binds a stale report.
    """
    from .core import read_artifact
    row = ledger.db.execute("SELECT report,assignment,coverage FROM claims WHERE task=? AND state='complete' "
                            "ORDER BY rowid DESC LIMIT 1", (task["alternatives_handoff"],)).fetchone()
    if row is None:
        return None
    occurrence_ = read_artifact(ledger.root, row[1])['task']
    return row[0] if row[2] == binding(ledger, snapshot, occurrence_) else None
