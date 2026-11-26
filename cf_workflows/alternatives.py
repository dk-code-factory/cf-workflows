"""Adapter procedures of the alternatives entries (contract v1).

Each procedure is the helper side of one preset task from cf_workflows.entries. It
adds no orchestration of its own: it walks the fixed alternatives stages of
cf_workflows.alignment, builds each stage's native assignment, checks the
engine-observed session and its output, computes weighted totals and selection
checks, and records the result in the alignment ledger.

- `run('dk-issue-alternatives')`: whole-problem scope. Compares implementation
  approaches for the problem stated by the issue requirements; an existing PR is
  an optional candidate. Without a PR nothing is persisted and no PR identity is
  invented: the stages run on a temporary ledger that is removed afterwards, and
  engine claims alone make an interrupted call reusable.
- `run('dk-pr-alternatives')`: branch scope. Inventories the architecture of the
  whole PR branch, including inherited decisions, and compares only questionable
  decisions.
- `manual_review` (cf-workflows:dk-pr-review-alternatives): the only route to a
  fresh comparison of settled analysis. It needs a distinct direct user request,
  refreshes the inventory and comparisons under that request, reuses the shared
  assessment and stops after alternatives.
- `compatibility` (cf-workflows:dk-alternatives-review): runs the routed delegate
  under its own entry identity, keeps the original instruction and both role
  prompts, and reports retained legacy assignments as unsupported instead of
  starting new work.

With a PR, analysis is settled once per PR across routes, sessions, heads and
guidance versions. Changed requirements refresh current alignment only. Sessions
come from the engine transport (`native_session`); no helper starts a model or
provider. Nothing here edits, commits or publishes.

Stage prompts are the pinned dk-workflows baseline prompts (MIT; see
licenses/dk-workflows-MIT.txt), identical for both skills.
"""
import copy
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

from . import admission, alignment, configuration, dk_pr_fix, entries, semantic
from .admission import AdmissionError, digest, fields, require, text
from .configuration import Blocked, entry_options as configuration_entry_options

SCOPES = {'dk-issue-alternatives': 'whole-problem', 'dk-pr-alternatives': 'branch'}
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
# Non-default prompt options are custom input: the shared semantic admission validates them.
PROMPT_OPTIONS = (('assessment_prompt', 'assessment'), ('scoring.profile_selection_prompt', 'assessment'),
                  ('scoring.custom_prompt', 'assessment'), ('evaluation_prompt', 'additional'),
                  ('selection.prompt', 'selection'))
# A direct request refreshes these stages; the shared assessment is reused.
REFRESHED = ('inventory', 'comparison', 'selection', 'interactions')
ORIGINS = {'branch': ('inherited', 'introduced', 'materially_changed'), 'whole-problem': ('whole_problem',)}
KINDS = ('pr', 'smallest', 'alternative')
WORK_TYPES = ('small_fix', 'feature', 'mixed')
VOLUME_CRITERIA = ('low_loc', 'low_intrusiveness')
MAX_READABLE = 4096
ISSUE_ATTEMPTS = 3  # Attempts per stage of an issue-only run, which keeps no record of rejections.
MAX_ROUNDS = 8  # Questions about one choice after answers that named no feasible option.
SOLUTION_FIELDS = ('id', 'kind', 'approach', 'pros', 'cons', 'handled_edges', 'unhandled_edges', 'evidence',
                   'complexity', 'intrusiveness', 'feasible', 'feasibility_reason', 'material_doubts',
                   'recommended', 'recommendation_reason', 'production_loc', 'ratings', 'rating_evidence')
SOLUTION_OPTIONAL = ('components', 'complexity_reason', 'intrusiveness_reason', 'total')
_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
# Reconciled decisions (cf_workflows.decisions.handoff) are citable as `decision:<id>`.
DECISION_PREFIX = 'decision:'
_DECISION_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


def _failure(error):
    return configuration._failure(error)


# -- Options and admission --------------------------------------------------------------------

def _value(options, dotted):
    for part in dotted.split('.'):
        options = options[part]
    return options


def option_prompts(name, options, adapter, saved=()):
    """Invocation prompts for non-default prompt options; defaults are bundled control text."""
    values = {key: value['value'] for key, value in entries.check_options(name, options, saved).items()}
    defaults = entries.ALTERNATIVES_OPTIONS
    prompts = []
    for dotted, slot in PROMPT_OPTIONS:
        value = _value(values, dotted)
        if value != _value(defaults, dotted) and value not in (None, ''):
            prompts.append({'id': 'option.' + dotted, 'slot': slot, 'target': adapter, 'text': value})
    require(values['scoring']['profile'] != 'custom' or values['scoring']['custom_prompt'] is not None,
            'options.scoring.custom_prompt', 'a custom scoring profile requires its prompt')
    return values, prompts


def saved_options(configuration, delegate, request):
    """The delegate's applicable saved option layers (cf_workflows.configuration.entry_options)."""
    if configuration is None:
        return []
    return configuration_entry_options(configuration, delegate, admission.resolve_request(request))


def admit(name, *, delegate, request, deployment, repository, transport, options, invocation, acceptance, roots,
          install, configuration=None):
    """Shared structural and semantic admission of an alternatives run under the invoked entry's identity.

    Saved options of the delegate apply under the invocation options. A compatibility or
    manual entry passes them as the delegate's, like its invocation options.
    """
    adapter = entries.ENTRIES[delegate]['adapter']
    try:
        saved = saved_options(configuration, delegate, request)
        values, extra = option_prompts(delegate, options, adapter, saved)
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'prompts'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and permissions; only prompts may be added')
        prompts = copy.deepcopy(invocation.get('prompts', [])) + extra
    except AdmissionError as error:
        return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
                'error': {'path': error.path, 'message': error.message}}, None
    except (TypeError, KeyError, AttributeError, ValueError) as error:
        return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
                'error': {'path': 'configuration', 'message': 'malformed input: ' + type(error).__name__}}, None
    admitted = entries.preflight(name, request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation={'prompts': prompts} if prompts else None,
                                 options=options if name == delegate else None, roots=roots, install=install,
                                 ledger=acceptance, adapter=adapter, saved=saved if name == delegate else ())
    return admitted, values


# -- Pins ---------------------------------------------------------------------------------------

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
            raise AdmissionError(label, 'commit is not present in the checkout') from None
    try:
        _git(repository, 'merge-base', '--is-ancestor', target, head)
    except subprocess.CalledProcessError:
        raise AdmissionError('target', 'the target revision is not an ancestor of the head') from None
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


def _requirements(raw, scope):
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


def _decisions(value):
    """Sourced decision reconciliation for the inventory: a cf_workflows.decisions.handoff() value.

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
        require(isinstance(rubric['id'], str) and admission.IDENT.fullmatch(rubric['id']) and rubric['id'] not in ids,
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
        require(isinstance(item['id'], str) and admission.IDENT.fullmatch(item['id']) and item['id'] not in ids,
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


class Unqualified(AdmissionError):
    """An automatic pick that the selection policy cannot accept; semi-manual asks the user instead."""


def check_selection(output, *, comparison, totals, policy, answer, citable):
    """The shared selection policy; returns the accepted selection with its helper totals."""
    by_id = {item['id']: item for item in comparison['solutions']}
    mode = output.get('mode') if isinstance(output, dict) else None
    require(mode in ('auto', 'manual'), 'selection.mode', 'auto or manual')
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
        alignment._validate(stage, output)
    except alignment.AlignmentError as error:
        raise AdmissionError(stage, str(error)) from None
    _evidence(output['evidence'], stage + '.evidence', citable)
    return output


# -- The stage walk -------------------------------------------------------------------------------

class _Stop(Exception):
    def __init__(self, status, **detail):
        super().__init__(status)
        self.status, self.detail = status, detail


class _Walk:
    """One call's walk over the fixed stages; every decision is the ledger's."""

    def __init__(self, *, admitted, values, ledger, scope, pins_, requirements, evidence, transport, request,
                 delegate, namespace, persisted=True, decisions=None, shared=None):
        self.admitted, self.values, self.ledger, self.scope = admitted, values, ledger, scope
        self.pins, self.requirements, self.transport, self.request = pins_, requirements, transport, request
        self.delegate, self.namespace, self.persisted = entries.ENTRIES[delegate], namespace, persisted
        # shared() reads the other scope's settled assessment of the same PR, or None.
        self.decisions, self.shared = decisions, shared
        snapshot = admitted['snapshot']
        self.citable = set(pins_['readable']) | {item['source'] for item in requirements} | {
            DECISION_PREFIX + item['id'] for group in ('commitments', 'exceptions')
            for item in (decisions or {}).get(group, [])}
        self.evidence = copy.deepcopy(evidence or [])
        self.digests = {
            'requirements': digest(requirements), 'instructions': digest(snapshot['request']['original_instructions']),
            'guidance': digest(snapshot['resources']['guidance']), 'prompts': digest(admitted['entry']['prompts']),
            'assessment': digest({k: values[k] for k in ('assessment_prompt', 'scoring')}),
            'evaluation': digest(values['evaluation_prompt']), 'policy': digest(values['selection']),
            'evidence': digest(self.evidence)}
        if decisions is not None:
            self.digests['decisions'] = digest(decisions)
        self.stages, self.dispatches, self.selections, self.gates = [], 0, {}, []
        # Architectural stages are settled per PR; current alignment keeps its own allowance per logical run.
        self.account = 'run-' + digest(snapshot['request']['logical_run'])[:24]
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
        latest = self.ledger.latest(alignment.key(stage, decision))
        if (latest is None or stage not in alignment.SETTLED or not alignment.Ledger._settled(stage, latest)
                or latest['dependencies'] != self.ledger.dependencies(stage, decision)):
            return None
        return latest

    def stage(self, stage, decision=None, *, context, check, answer=None):
        stage_key = alignment.key(stage, decision)
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
            except _Stop as stop:
                # Without a record nothing remembers a rejection across calls: an issue-only run walks
                # its claims (replays cost no session) and retries a rejected attempt in the same call,
                # up to ISSUE_ATTEMPTS attempts per stage. A PR record retries on the next call.
                if (self.persisted or not stop.detail.get('rejected')
                        or len(self.ledger.history(stage_key)) >= ISSUE_ATTEMPTS):
                    raise

    def adopt(self, stage, check):
        """The other scope's settled assessment of this PR, adopted when this record has none.

        It must pass this walk's own checks (for example, cite files of this pinned head);
        otherwise this scope runs its own assessment.
        """
        if (stage not in alignment.SHARED or self.shared is None or self.ledger.latest(stage) is not None
                or self.ledger.open_claim(stage) is not None):
            return None
        origin = self.shared()
        if origin is None:
            return None
        try:
            check(copy.deepcopy(origin['result']))
        except (AdmissionError, TypeError, KeyError, AttributeError, ValueError):
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
        account = self.account if stage in alignment.CURRENT else None
        claim = self.ledger.open_claim(stage_key)
        if claim is not None and claim['request'] is not None and request is None:
            # An interrupted direct request owns this attempt; an automatic route never supersedes it.
            raise _Stop('authorization_required', stage=stage_key, skill=alignment.DIRECT_SKILL,
                        message=f'{stage_key} belongs to interrupted direct request {claim["request"]}; '
                                f'resume it through {alignment.DIRECT_SKILL}')
        try:
            action = self.ledger.prepare(stage, decision, inputs=inputs, head=head, operation=operation,
                                         request=request, account=account)
        except alignment.AuthorizationRequired as error:
            raise _Stop('authorization_required', stage=stage_key, skill=alignment.DIRECT_SKILL,
                        message=str(error)) from None
        except alignment.AllowanceExhausted as error:
            raise _Stop('exhausted', stage=stage_key,
                        allowance=self.ledger.usage(alignment.allowance(stage, account)),
                        message=str(error)) from None
        self.stages.append({'key': stage_key, 'action': action['action'], 'reference': action['reference']})
        if action['action'] == 'reuse':
            return action['result']
        task = self.assignment(stage, decision, action['operation'], context)
        try:
            reference, session_id, output = configuration.run_session(self.transport, task)
        except Blocked as error:
            if error.__cause__ is not None:
                # The transport failed: the session may have run, so its claim stays open to resume.
                raise _Stop('blocked', stage=stage_key, error=_failure(error)) from None
            self.ledger.abandon(action['operation'])
            raise _Stop('blocked', stage=stage_key, error=_failure(error), rejected=True) from None
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
        except (AdmissionError, alignment.AlignmentError, TypeError, KeyError, AttributeError,
                ValueError) as error:
            # A rejected result never becomes current, and the next call starts a new attempt
            # instead of resuming it; the attempt's reservation stays consumed.
            self.ledger.abandon(action['operation'])
            failure = ({'path': stage_key, 'message': str(error)} if isinstance(error, alignment.AlignmentError)
                       else _failure(error))
            raise _Stop('blocked', stage=stage_key, error=failure, rejected=True) from None
        self.dispatches += 1
        self.stages[-1].update(evidence='native:' + reference, session=session_id)
        return record['result']

    def assignment(self, stage, decision, claim, context):
        entry, snapshot = self.admitted['entry'], self.admitted['snapshot']
        agent, provider = semantic.validator_agent(self.admitted)
        role = 'verifier' if stage == 'verification' else entry['definition']['definition']
        sha = (snapshot['resources']['roles'][role]['sha256'] if stage == 'verification'
               else entry['task']['definition_sha256'])
        return {
            'schema_version': 1, 'kind': 'alternatives_stage', 'suite': admission.SUITE,
            'entry': entry['skill'], 'skill': self.delegate['skill'], 'scope': self.scope,
            'mode': stage, 'decision': decision, 'claim': claim, 'adapter': entry['adapter'], 'role': role,
            'definition_sha256': sha, 'assignment': entry['definition']['assignment'],
            'checklist': entry['definition']['checklist'], 'role_prompt': self.delegate['role_prompt'],
            'stage_prompt': STAGE_PROMPTS[stage], 'instructions': snapshot['request']['original_instructions'],
            'prompts': entry['prompts'], 'options': copy.deepcopy(self.values),
            'agent': agent, 'provider': provider, 'skills': [], 'tools': 'read-only', 'writes': False,
            'publication': False, 'readable': dict(sorted(self.pins['readable'].items())),
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
            raise _Stop(self.pending_status, decisions=pending, gates=self.gates)
        return self.stage('interactions', context={'assessment': assessment, 'inventory': inventory,
                                                   'selections': self.selections},
                          check=lambda out: check_interactions(out, self.citable))

    def select(self, decision, comparison, totals):
        """Sourced selection; manual and semi-manual ask the user through an engine gate, automatic never."""
        stage_key = alignment.key('selection', decision['id'])
        policy = self.values['selection']
        found = self.reusable('selection', decision['id'])
        if found is not None:
            self.stages.append({'key': stage_key, 'action': 'reuse', 'reference': found['reference']})
            return found['result']
        comparison_ref = self.ledger.latest(alignment.key('comparison', decision['id']))['reference']

        def gate_id(round_):
            return 'altchoice_' + digest({'namespace': self.namespace, 'comparison': comparison_ref, 'round': round_,
                                          'request': self.request['id'] if self.request else None})[:32]
        answer, round_, rejected = None, 0, []
        if policy['mode'] != 'automatic':
            # Gates are found by their own identity, so issue-only runs without a record find them too.
            feasible = {item['id'] for item in comparison['solutions'] if item['feasible']}
            while True:
                gates = self.read_gates(gate_id(round_))
                if any(gate['status'] == 'waiting' for gate in gates):
                    # An open question resumes without a new session or gate.
                    self.pending_status = 'waiting'
                    self.gates.append({'decision': decision['id'], 'request_id': gate_id(round_),
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
                    raise _Stop('blocked', stage=stage_key, error={'path': 'gates', 'message':
                                                                   'too many answers that name no feasible option'})
        result = self.stage('selection', decision['id'], answer=answer, context={
            'comparison': comparison, 'scores': totals, 'policy': policy,
            'user_choice': copy.deepcopy(answer)},
            check=lambda out: check_selection(out, comparison=comparison, totals=totals, policy=policy,
                                              answer=answer, citable=self.citable))
        if result['choice'] is None and policy['mode'] != 'automatic':
            request = {'request_id': gate_id(round_), 'evidence_id': self.stages[-1]['reference'],
                       'prompt': 'Choose one evaluated feasible option for this decision.',
                       'payload': {'decision': decision['id'], 'scores': totals, 'rejected_answers': rejected,
                                   'options': [{'id': item['id'], 'kind': item['kind'], 'approach': item['approach'],
                                                'feasible': item['feasible']} for item in comparison['solutions']]}}
            try:
                opened = self.transport.open_input_gate(copy.deepcopy(request))
                fields(opened, 'gate', ('gate_id',))
            except AdmissionError as error:
                raise _Stop('blocked', stage=stage_key, error=_failure(error)) from None
            except Exception as failure:
                raise _Stop('blocked', stage=stage_key, error={'path': 'gates', 'message': 'input gate failed: '
                                                               + type(failure).__name__}) from None
            self.pending_status = 'waiting'
            self.gates.append({'decision': decision['id'], 'request_id': gate_id(round_),
                               'gate_id': opened['gate_id'], 'status': 'waiting'})
        return result

    def read_gates(self, gate_id):
        try:
            return semantic.check_gates(self.transport.input_gates(gate_id), gate_id)
        except AdmissionError as error:
            raise _Stop('blocked', stage='gates', error=_failure(error)) from None
        except Exception as failure:
            raise _Stop('blocked', stage='gates', error={'path': 'gates', 'message': 'input gates unavailable: '
                                                         + type(failure).__name__}) from None

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


# -- Entry procedures -------------------------------------------------------------------------------

def _ledger(state, scope, pr, repository_id, temporary):
    if pr is None:
        # Issue-only analysis: a disposable ledger, removed after the call; no PR identity.
        path = Path(temporary) / 'issue.alignment.sqlite'
        return alignment.Ledger(path, logical_run='issue', subject='repository:' + repository_id,
                                scope='whole-problem'), False
    require(state is not None, 'state', 'PR analysis needs the suite state namespace')
    return _pr_ledger(state, scope, pr), True


def _pr_ledger(state, scope, pr):
    run = 'alternatives_' + digest(pr['url'])[:32]  # Once per PR across routes, runs and sessions.
    return alignment.Ledger(alignment.ledger_path(state, run, scope), logical_run=run, pr=pr, scope=scope)


def _shared(state, scope, pr):
    """A reader of the other scope's own settled assessment of the same PR; it never creates a record."""
    other = next(item for item in alignment.SCOPES if item != scope)
    run = 'alternatives_' + digest(pr['url'])[:32]
    path = alignment.ledger_path(state, run, other)

    def read():
        if not path.is_file():
            return None
        try:
            ledger = alignment.Ledger(path, logical_run=run, pr=pr, scope=other)
        except alignment.AlignmentError:
            return None  # An unreadable sibling shares nothing; this scope runs its own assessment.
        try:
            found = ledger.latest('assessment')
        finally:
            ledger.close()
        if found is None or found['operation'].startswith(alignment.SHARED_PREFIX):
            return None  # Adopted from this record, which then has it already.
        return {'scope': other, 'operation': found['operation'], 'session': found['session'],
                'reference': found['reference'], 'result': found['result']}
    return read


def grant(*, state, pr, request_id, allowance, amount, authorization, scope='branch'):
    """An explicit continuation adds capacity to a PR's retained allowance; a replay adds nothing."""
    ledger = _pr_ledger(state, scope, admission.pr_identity(pr, 'pr'))
    try:
        ledger.grant(request_id, allowance, amount, authorization)
        return ledger.usage(allowance)
    finally:
        ledger.close()


def _execute(name, *, delegate, scope, request, deployment, repository, transport, state, target, head,
             requirements, evidence, options, invocation, acceptance, roots, install, caller, direct, through,
             decisions=None, configuration=None):
    admitted, values = admit(name, delegate=delegate, request=request, deployment=deployment,
                             repository=repository, transport=transport, options=options, invocation=invocation,
                             acceptance=acceptance, roots=roots, install=install, configuration=configuration)
    if admitted['preflight'] != 'admitted':
        return {'status': admitted['preflight'], 'admission': admitted}
    snapshot = admitted['snapshot']
    pr = snapshot['request']['pr']
    repository_id = pr['repository'] if pr else snapshot['request']['repository']
    try:
        reqs = _requirements(requirements, scope)
        reconciled = _decisions(decisions)
        pinned = pins(repository, scope=scope, pr=pr, target=target, head=head,
                      priority=list(snapshot['resources']['guidance']['files']))
        require(through == 'analysis' or pr is not None, 'request.pr', 'current alignment needs a pull request')
    except AdmissionError as error:
        return {'status': 'rejected', 'error': _failure(error), 'admission': admitted}
    except (subprocess.CalledProcessError, OSError) as error:
        return {'status': 'rejected', 'error': {'path': 'repository', 'message': 'checkout unreadable: '
                                                + type(error).__name__}, 'admission': admitted}
    base = {'entry': entries.ENTRIES[name]['skill'], 'delegate': entries.ENTRIES[delegate]['skill'], 'scope': scope,
            'caller': caller, 'instructions': snapshot['request']['original_instructions'],
            'request': copy.deepcopy(direct), 'pins': {k: pinned[k] for k in ('target', 'head', 'pr')},
            'decisions': reconciled['result_ref'] if reconciled else None,
            'gaps': pinned['gaps'],
            'resources': {'entry_role_prompt': entries.ENTRIES[name]['baseline_prompt'],
                          'entry_role_prompt_sha256': hashlib.sha256(
                              entries.ENTRIES[name]['role_prompt'].encode()).hexdigest(),
                          'role_prompt': entries.ENTRIES[delegate]['baseline_prompt'],
                          'stage_prompts': dict(STAGE_PROMPT_SHA256)},
            'ready': False, 'clean_review': False, 'publication': False}
    with tempfile.TemporaryDirectory(prefix='cfw-alternatives-') as temporary:
        try:
            ledger, persisted = _ledger(state, scope, pr, repository_id, temporary)
        except (AdmissionError, alignment.AlignmentError) as error:
            return {**base, 'status': 'blocked', 'error': {'path': 'ledger', 'message': str(error)}}
        try:
            walk = _Walk(admitted=admitted, values=values, ledger=ledger, scope=scope, pins_=pinned,
                         requirements=reqs, evidence=evidence, transport=transport, request=direct,
                         delegate=delegate,
                         # Engine claims never collide across records; issue-only claims stay stable.
                         namespace=str(ledger.path.resolve()) if persisted else f'issue:{repository_id}',
                         persisted=persisted, decisions=reconciled,
                         shared=_shared(state, scope, ledger.pr) if persisted else None)
            outcome = {'status': 'complete'}
            try:
                interactions = walk.analysis()
                outcome['interactions'] = interactions
                if not interactions['compatible']:
                    raise _Stop('interactions_blocked', required_changes=interactions['required_changes'])
                if through != 'analysis':
                    current = walk.current(verify=through == 'verification')
                    outcome['alignment'] = current
                    if through == 'verification':
                        outcome['gate'] = alignment.gate(ledger, head=pinned['head'],
                                                         inputs=walk.inputs('alignment'), account=walk.account)
                        outcome['status'] = outcome['gate']['outcome']
                    elif reconciled is not None and reconciled['dependent_work'] == 'blocked':
                        # Unresolved reconciliation blocks dependent implementation, not the analysis.
                        outcome.update(status='decisions_blocked', unresolved=reconciled['unresolved'],
                                       questions=reconciled['questions'])
                    else:
                        # The fix route hands settled choices and required changes to authorized tasks.
                        outcome['handoff'] = {
                            'choices': {key: value['choice'] for key, value in walk.selections.items()},
                            'alignment': current['alignment'],
                            'required_changes': current.get('required_changes'),
                            'tests_required': current['alignment'] == 'mismatch'}
            except _Stop as stop:
                outcome = {'status': stop.status, **stop.detail}
            usage = {'architecture': ledger.usage('architecture'),
                     'alignment': ledger.usage(alignment.allowance('alignment', walk.account))}
        finally:
            ledger.close()
    return {**base, **outcome, 'persisted': persisted, 'stages': walk.stages, 'dispatches': walk.dispatches,
            'selections': walk.selections, 'allowances': usage, 'admission': admitted}


def run(name, *, request, deployment, repository, transport, target, head=None, requirements=None, evidence=None,
        state=None, options=None, invocation=None, acceptance=None, roots=None, install=None, caller=None,
        decisions=None, configuration=None):
    """cf-workflows:dk-issue-alternatives or dk-pr-alternatives, directly or composed by a coordinator.

    caller None runs the analysis through interactions. cf-workflows:dk-pr-fix
    composes whole-problem analysis with current alignment and returns the handoff
    for authorized implementation tasks; cf-workflows:dk-pr-review composes branch
    analysis with current alignment, independent verification and the alignment
    gate. No caller authorizes a fresh comparison of settled analysis.

    decisions is the sourced reconciliation handoff (cf_workflows.decisions.handoff)
    the inventory consumes; configuration is the saved configuration document whose
    applicable `entries` options apply under options.
    """
    require(name in SCOPES, 'entry', 'not an alternatives skill')
    if caller is not None and COMPOSERS.get(caller) != SCOPES[name]:
        return {'status': 'rejected', 'error': {'path': 'caller', 'message': 'not a coordinator of ' + name}}
    through = {None: 'analysis', 'cf-workflows:dk-pr-fix': 'alignment',
               'cf-workflows:dk-pr-review': 'verification'}[caller]
    return _execute(name, delegate=name, scope=SCOPES[name], request=request, deployment=deployment,
                    repository=repository, transport=transport, state=state, target=target, head=head,
                    requirements=requirements, evidence=evidence, options=options, invocation=invocation,
                    acceptance=acceptance, roots=roots, install=install or dk_pr_fix.INSTALL, caller=caller,
                    direct=None, through=through, decisions=decisions, configuration=configuration)


def manual_review(*, request, direct, deployment, repository, transport, target, head, state, requirements=None,
                  evidence=None, alternatives_options=None, invocation=None, acceptance=None, roots=None,
                  install=None, caller=None, decisions=None, configuration=None):
    """cf-workflows:dk-pr-review-alternatives: a fresh requested architecture review, then stop.

    direct is the retained direct user request {id, source, instruction, skill,
    origin}; its instruction is this invocation's exact instruction. It refreshes
    the inventory and every questionable comparison once under that request,
    reuses the shared assessment, resumes an interrupted request without repeating
    completed siblings, and never claims ordinary review or readiness.
    """
    if caller is not None:
        return {'status': 'rejected', 'error': {'path': 'caller', 'message': 'a downstream call is not a distinct '
                                                'direct user invocation of ' + alignment.DIRECT_SKILL}}
    try:
        alignment.direct_request(direct)
        require(isinstance(request, dict) and direct['instruction'] == request.get('instructions'),
                'request.instructions', 'the direct request binds this invocation\'s exact instruction')
    except alignment.AuthorizationRequired as error:
        return {'status': 'rejected', 'error': {'path': 'direct', 'message': str(error)}}
    except AdmissionError as error:
        return {'status': 'rejected', 'error': _failure(error)}
    return _execute('dk-pr-review-alternatives', delegate='dk-pr-alternatives', scope='branch', request=request,
                    deployment=deployment, repository=repository, transport=transport, state=state, target=target,
                    head=head, requirements=requirements, evidence=evidence, options=alternatives_options,
                    invocation=invocation, acceptance=acceptance, roots=roots,
                    install=install or dk_pr_fix.INSTALL, caller=None, direct=copy.deepcopy(direct),
                    through='verification', decisions=decisions, configuration=configuration)


def compatibility(*, route, request, deployment, repository, transport, target, head=None, requirements=None,
                  evidence=None, state=None, alternatives_options=None, invocation=None, acceptance=None, roots=None,
                  install=None, legacy=None, decisions=None, configuration=None):
    """cf-workflows:dk-alternatives-review: the explicit route to a delegate under this entry's identity.

    route is 'whole-problem' (dk-issue-alternatives) or 'branch'
    (dk-pr-alternatives); it is never inferred. A retained legacy assignment
    (legacy) is reported as unsupported: no dk-workflows state is imported, and
    a new comparison never substitutes for its original result.
    """
    if legacy is not None:
        return {'status': 'unsupported', 'entry': entries.ENTRIES['dk-alternatives-review']['skill'],
                'legacy': copy.deepcopy(legacy), 'dispatches': 0,
                'error': {'path': 'legacy', 'message': 'retained dk-workflows assignments are not imported; resume '
                                                       'them in their original installation'}}
    delegates = entries.ENTRIES['dk-alternatives-review']['delegates']
    if route not in delegates:
        return {'status': 'rejected', 'error': {'path': 'route', 'message': 'route must be whole-problem or branch'}}
    return _execute('dk-alternatives-review', delegate=delegates[route], scope=route, request=request,
                    deployment=deployment, repository=repository, transport=transport, state=state, target=target,
                    head=head, requirements=requirements, evidence=evidence, options=alternatives_options,
                    invocation=invocation, acceptance=acceptance, roots=roots,
                    install=install or dk_pr_fix.INSTALL, caller=None, direct=None, through='analysis',
                    decisions=decisions, configuration=configuration)
