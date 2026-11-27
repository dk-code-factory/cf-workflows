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

The stage checks, totals, selection policy and the walk over the stages are the
installed helpers' prfix/alternatives.py, loaded here in memory, so the library
and the installed workflow (workflows/alternatives.yml) run one implementation.
This module adds admission, the engine transport and the entry procedures.

Stage prompts are the pinned dk-workflows baseline prompts (MIT; see
licenses/dk-workflows-MIT.txt), identical for both skills.
"""
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import types

from . import admission, alignment, configuration, dk_pr_fix, entries, semantic
from .admission import AdmissionError, fields, require
from .configuration import Blocked, entry_options as configuration_entry_options

_SOURCE = (Path(__file__).resolve().parent / 'workflows' / 'dk-pr-fix' / 'workflows' / 'helpers' / 'prfix'
           / 'alternatives.py')


def _procedure():
    # Compiled in memory over the library's alignment records: no bytecode lands in the installed closure.
    module = types.ModuleType('_cf_workflows_alternatives_procedure')
    module.__file__ = str(_SOURCE)
    module._RECORDS = alignment._R
    exec(compile(_SOURCE.read_bytes(), str(_SOURCE), 'exec'), module.__dict__)
    return module


_H = _procedure()
(SCOPES, COMPOSERS, STAGE_PROMPTS, STAGE_PROMPT_SHA256, REFRESHED, ORIGINS, KINDS, WORK_TYPES, VOLUME_CRITERIA,
 MAX_READABLE, ISSUE_ATTEMPTS, MAX_ROUNDS, SOLUTION_FIELDS, SOLUTION_OPTIONAL, DECISION_PREFIX) = (
    _H.SCOPES, _H.COMPOSERS, _H.STAGE_PROMPTS, _H.STAGE_PROMPT_SHA256, _H.REFRESHED, _H.ORIGINS, _H.KINDS,
    _H.WORK_TYPES, _H.VOLUME_CRITERIA, _H.MAX_READABLE, _H.ISSUE_ATTEMPTS, _H.MAX_ROUNDS, _H.SOLUTION_FIELDS,
    _H.SOLUTION_OPTIONAL, _H.DECISION_PREFIX)
(cited, scores, check_assessment, check_inventory, check_comparison, check_selection, check_interactions,
 check_current, Unqualified, StageError) = (
    _H.cited, _H.scores, _H.check_assessment, _H.check_inventory, _H.check_comparison, _H.check_selection,
    _H.check_interactions, _H.check_current, _H.Unqualified, _H.StageError)
# Non-default prompt options are custom input: the shared semantic admission validates them.
PROMPT_OPTIONS = (('assessment_prompt', 'assessment'), ('scoring.profile_selection_prompt', 'assessment'),
                  ('scoring.custom_prompt', 'assessment'), ('evaluation_prompt', 'additional'),
                  ('selection.prompt', 'selection'))


def _failure(error):
    return configuration._failure(error) if isinstance(error, (AdmissionError, Blocked)) else _H.failure(error)


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
    where = 'configuration'  # The input being read, named by a malformed-input rejection.
    try:
        saved = saved_options(configuration, delegate, request)
        where = 'options'
        values, extra = option_prompts(delegate, options, adapter, saved)
        where = 'invocation'
        invocation = {} if invocation is None else invocation
        require(isinstance(invocation, dict) and set(invocation) <= {'prompts'}, 'invocation',
                'the entry preset fixes tasks, finalizers, reviewers and permissions; only prompts may be added')
        prompts = copy.deepcopy(invocation.get('prompts', [])) + extra
    except AdmissionError as error:
        return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
                'error': {'path': error.path, 'message': error.message}}, None
    except (TypeError, KeyError, AttributeError, ValueError) as error:
        return {'preflight': 'rejected', 'structural': 'rejected', 'semantic': None,
                'error': {'path': where, 'message': 'malformed input: ' + type(error).__name__}}, None
    admitted = entries.preflight(name, request=request, deployment=deployment, repository=repository,
                                 transport=transport, invocation={'prompts': prompts} if prompts else None,
                                 options=options if name == delegate else None, roots=roots, install=install,
                                 ledger=acceptance, adapter=adapter, saved=saved if name == delegate else ())
    return admitted, values


# -- Pins and records -------------------------------------------------------------------------------

def pins(repository, *, scope, pr, target, head, priority=()):
    """Verified commits of the checkout; whole-branch readable files and the branch's changed paths."""
    return _H.pins(repository, scope=scope, pr=pr, target=target, head=head, priority=priority)


def selections(state, pr):
    """Advised and selected alternatives of a PR, read-only from both scopes' records."""
    return _H.selections(state, admission.pr_identity(pr, 'pr') if isinstance(pr, str) else pr)


def decision_sources(state, pr):
    """The PR's advised and selected alternatives as advisory sources for cf_workflows.decisions.reconcile."""
    return _H.decision_sources(selections(state, pr))


class _Sessions:
    """The engine transport as the walk's sessions and gates."""

    def __init__(self, transport):
        self.transport = transport

    def run(self, task):
        try:
            return configuration.run_session(self.transport, task)
        except Blocked as error:
            # A transport failure keeps the claim open to resume; a refused session closes it.
            raise _H.Unavailable(_failure(error), rejected=error.__cause__ is None) from None

    def gates(self, request_id):
        try:
            return semantic.check_gates(self.transport.input_gates(request_id), request_id)
        except AdmissionError as error:
            raise _H.Unavailable(_failure(error)) from None
        except Exception as failure:
            raise _H.Unavailable({'path': 'gates', 'message': 'input gates unavailable: '
                                  + type(failure).__name__}) from None

    def open_gate(self, request):
        try:
            opened = self.transport.open_input_gate(copy.deepcopy(request))
            fields(opened, 'gate', ('gate_id',))
            return opened['gate_id']
        except AdmissionError as error:
            raise _H.Unavailable(_failure(error)) from None
        except Exception as failure:
            raise _H.Unavailable({'path': 'gates', 'message': 'input gate failed: '
                                  + type(failure).__name__}) from None


def _pr_ledger(state, scope, pr):
    return _H.pr_ledger(state, scope, pr)


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
        reqs = _H.requirements(requirements, scope)
        reconciled = _H.reconciled(decisions)
        pinned = pins(repository, scope=scope, pr=pr, target=target, head=head,
                      priority=list(snapshot['resources']['guidance']['files']))
        require(through == 'analysis' or pr is not None, 'request.pr', 'current alignment needs a pull request')
    except (AdmissionError, StageError) as error:
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
    entry = admitted['entry']
    agent, provider = semantic.validator_agent(admitted)
    identity = {'entry': entry['skill'], 'skill': entries.ENTRIES[delegate]['skill'], 'adapter': entry['adapter'],
                'role': entry['definition']['definition'], 'definition_sha256': entry['task']['definition_sha256'],
                'verifier_sha256': snapshot['resources']['roles']['verifier']['sha256'],
                'assignment': entry['definition']['assignment'], 'checklist': entry['definition']['checklist'],
                'role_prompt': entries.ENTRIES[delegate]['role_prompt'], 'prompts': entry['prompts'],
                'agent': agent, 'provider': provider}
    with tempfile.TemporaryDirectory(prefix='cfw-alternatives-') as temporary:
        try:
            if pr is None:
                ledger, persisted = _H.issue_ledger(temporary, repository_id), False
            else:
                require(state is not None, 'state', 'PR analysis needs the suite state namespace')
                ledger, persisted = _pr_ledger(state, scope, pr), True
        except (AdmissionError, alignment.AlignmentError) as error:
            return {**base, 'status': 'blocked', 'error': {'path': 'ledger', 'message': str(error)}}
        try:
            walk = _H.Walk(ledger=ledger, scope=scope, pins_=pinned, requirements=reqs, evidence=evidence,
                           sessions=_Sessions(transport), request=direct, identity=identity, values=values,
                           inputs={'instructions': snapshot['request']['original_instructions'],
                                   'guidance': snapshot['resources']['guidance'], 'prompts': entry['prompts']},
                           logical_run=snapshot['request']['logical_run'],
                           # Engine claims never collide across records; issue-only claims stay stable.
                           namespace=str(ledger.path.resolve()) if persisted else f'issue:{repository_id}',
                           persisted=persisted, decisions=reconciled,
                           shared=_H.shared(state, scope, ledger.pr) if persisted else None)
            outcome = walk.run(through, reconciled)
            usage = walk.usage()
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
