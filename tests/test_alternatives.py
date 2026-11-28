"""Alternatives entries: dk-issue-alternatives, dk-pr-alternatives, dk-alternatives-review and
dk-pr-review-alternatives.

Disposable Git repositories for real pins, a disposable state namespace per test and a
fail-closed fake engine with durable gates and claim-reconciled native sessions scripted per
stage. Any unscripted session, gate or other unconfigured operation fails with no live
fallback. These results do not certify any engine, provider or host.
"""
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True  # Never add caches to the installed closure.
from cf_workflows import admission, alignment, alternatives, configuration, dk_pr_fix, entries  # noqa: E402
from test_admission import CAPABILITIES, FakeTransport  # noqa: E402
from test_configuration_skills import Engine  # noqa: E402
from test_semantic import verdict  # noqa: E402

REPOSITORY = 'github.com/owner/repo'
PR = 'https://github.com/Owner/Repo/pull/7'
INVENTORY = {row['name']: row for row in json.loads((ROOT / 'baselines/inventory.json').read_text())['skills']}
ISSUE = [{'source': 'issue:27', 'text': 'Retries must be bounded and observable.'}]
CHANGES = 'Route retries through the shared scheduler.'


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout


def key(task):
    return task['mode'] + (':' + task['decision'] if task['decision'] else '')


class Transport(FakeTransport):
    """Native sessions scripted per stage key; a known claim is reconciled to its original result."""

    def __init__(self, engine, script, validators=(), lose=()):
        super().__init__(CAPABILITIES)
        self.tasks, lost, checks = [], Counter(lose), list(validators)

        def native(task):
            self.tasks.append(copy.deepcopy(task))
            name = key(task)
            if task['claim'] not in engine.claims:
                if name not in script:
                    raise AssertionError('unscripted native session: ' + name)
                output = script[name]
                if isinstance(output, list):
                    if not output:
                        raise AssertionError('unscripted native session: ' + name)
                    output = output.pop(0)
                engine.claims[task['claim']] = respond(task, output)
                engine.sessions[name] += 1
            if lost[name]:  # The session ran, but its response is lost.
                lost[name] -= 1
                raise ConnectionError('response lost')
            return copy.deepcopy(engine.claims[task['claim']])

        def validator(task):
            if not checks:
                raise AssertionError('unscripted validator session')
            return checks.pop(0)(task)
        self.configured.update(
            native_session=native, validator_session=validator, open_input_gate=engine.open,
            input_gates=lambda request_id: copy.deepcopy([g for g in engine.gates if g['request_id'] == request_id]))


def respond(task, output):
    observed = {'session_id': 'session-' + task['claim'], 'agent': task['agent'], 'role': task['role'],
                'definition_sha256': task['definition_sha256'], 'skills_loaded': [],
                'operations': [{'kind': 'read', 'target': name} for name in sorted(task['readable'])[:3]],
                'claim': task['claim']}
    if isinstance(output, tuple):  # (output, observation overrides)
        output, extra = output
        observed.update(extra(task) if callable(extra) else extra)
    return {'result_ref': 'result-' + task['claim'],
            'output': output(task) if callable(output) else copy.deepcopy(output), 'observation': observed}


# -- Stage outputs ----------------------------------------------------------------------------------

RUBRIC = {'id': 'fix', 'scope': 'Retry handling', 'work_type': 'small_fix', 'rationale': 'A focused fix.',
          'evidence': ['issue:27'], 'weights': {'correctness': 70, 'low_loc': 30},
          'anchors': {name: {'0': 'Fails.', '50': 'Partial.', '100': 'Complete.'} for name in ('correctness', 'low_loc')}}


def assessment(**extra):
    return {'purpose': 'Bound retries.', 'scope': 'Retry path', 'work_type': 'small_fix', 'rationale': 'Small fix.',
            'mandatory_constraints': ['Keep the public API.'], 'evidence': ['issue:27', 'git:AGENTS.md:1'],
            'rubrics': [RUBRIC], **extra}


DECISIONS = {
    'retry': {'id': 'retry', 'origin': 'introduced', 'scope': 'src/retry.py', 'rubric': 'fix',
              'decision': 'Unbounded retry loop.', 'questionable': True, 'reason': 'No ceiling.',
              'evidence': ['git:src/retry.py:1']},
    'scheduler': {'id': 'scheduler', 'origin': 'inherited', 'scope': 'src/scheduler.py', 'rubric': 'fix',
                  'decision': 'Ad hoc scheduler inherited from the target.', 'questionable': True,
                  'reason': 'Duplicates the retry timer.', 'evidence': ['git:src/scheduler.py']},
    'timer': {'id': 'timer', 'origin': 'materially_changed', 'scope': 'src/queue.py', 'rubric': 'fix',
              'decision': 'Retry timer kept beside the queue.', 'questionable': True, 'reason': 'Found on review.',
              'evidence': ['git:src/queue.py']},
    'queue': {'id': 'queue', 'origin': 'introduced', 'scope': 'src/queue.py', 'rubric': 'fix',
              'decision': 'In-memory queue.', 'questionable': False, 'reason': 'Adequate.',
              'evidence': ['git:src/queue.py']},
}


def branch_inventory(*names, **changes):
    decisions = [{**DECISIONS[name], **changes.get(name, {})} for name in names or ('retry', 'scheduler', 'queue')]
    return {'coverage': {'introduced': 'Every changed file.', 'inherited': 'The scheduler the branch builds on.'},
            'evidence': ['git:src/retry.py'], 'decisions': decisions}


def whole_inventory(**changes):
    return {'coverage': 'The whole problem stated by issue 27.', 'evidence': ['issue:27'],
            'decisions': [{'id': 'whole', 'origin': 'whole_problem', 'scope': 'Retry handling', 'rubric': 'fix',
                           'decision': 'How to bound retries.', 'questionable': True, 'reason': 'Open problem.',
                           'evidence': ['issue:27'], **changes}]}


def comparison(kinds=('pr', 'smallest', 'alternative'), ratings=None, **extra):
    ratings = ratings or {'pr': 50, 'smallest': 90, 'alternative': 60}

    def build(task):
        solutions = [{'id': kind, 'kind': kind, 'approach': f'The {kind} approach.', 'pros': ['Clear.'], 'cons': [],
                      'handled_edges': ['zero retries'], 'unhandled_edges': [], 'evidence': ['git:src/retry.py'],
                      'complexity': 20, 'intrusiveness': 10, 'feasible': True, 'feasibility_reason': 'Fits.',
                      'material_doubts': [], 'recommended': kind == 'smallest', 'recommendation_reason': 'Lead.',
                      'production_loc': {'min': 1, 'expected': 4, 'max': 9, 'reason': 'Estimate.'},
                      'ratings': {'correctness': ratings[kind], 'low_loc': ratings[kind]},
                      'rating_evidence': {'correctness': 'Tested.', 'low_loc': 'Counted.'}} for kind in kinds]
        return {'rubric': task['context']['rubric']['id'], 'target_revision': task['context']['pins']['target'],
                'alternative_coverage': 'Materially distinct approaches.', 'evidence': ['git:src/retry.py'],
                'solutions': solutions, **extra}
    return build


def auto(choice='smallest', **extra):
    def build(task):
        ids = [item['id'] for item in task['context']['comparison']['solutions']]
        result = {'mode': 'auto', 'choice': choice, 'confident': True, 'sensitivity': 'Low.',
                  'evidence': ['git:src/retry.py']}
        if choice is not None:
            result.update(reason='Clear lead.', rejection_reasons={i: 'Lower total.' for i in ids if i != choice})
        return {**result, **extra}
    return build


def manual(**extra):
    def build(task):
        answer = task['context']['user_choice']
        ids = [item['id'] for item in task['context']['comparison']['solutions']]
        return {'mode': 'manual', 'choice': answer['value'], 'user_source': answer['answer_id'],
                'reason': 'The user chose it.', 'evidence': [answer['answer_id']],
                'rejection_reasons': {i: 'Not chosen.' for i in ids if i != answer['value']}, **extra}
    return build


COMPATIBLE = {'compatible': True, 'reason': 'Selections combine.', 'evidence': ['git:src/retry.py']}


def current(value='aligned'):
    result = {'alignment': value, 'reason': 'Judged against current sources.', 'evidence': ['git:src/retry.py']}
    if value == 'mismatch':
        result['required_changes'] = CHANGES
    return result


def verified(disposition='confirmed'):
    result = {'disposition': disposition, 'reason': 'Independently checked.', 'evidence': ['git:src/retry.py']}
    if disposition == 'confirmed':
        result['required_changes'] = CHANGES
    return result


def branch_script(**overrides):
    script = {'assessment': assessment(), 'inventory': branch_inventory(),
              'comparison:retry': comparison(), 'comparison:scheduler': comparison(),
              'selection:retry': auto(), 'selection:scheduler': auto(), 'interactions': COMPATIBLE}
    script.update(overrides)
    return script


def whole_script(kinds=('pr', 'smallest', 'alternative'), **overrides):
    return {'assessment': assessment(), 'inventory': whole_inventory(), 'comparison:whole': comparison(kinds),
            'selection:whole': auto(), 'interactions': COMPATIBLE, **overrides}


def reconciled(**changes):
    """A cf_workflows.decisions.handoff() value: one settled user commitment."""
    value = {'result_ref': 'decisions-1', 'commitments': [
        {'id': 'bounded-retries', 'subject': 'Retries', 'text': 'Retries stop after five attempts.',
         'status': 'settled', 'scope': 'src/retry.py', 'sources': ['comment-1']}],
        'exceptions': [], 'unresolved': [], 'questions': [], 'dependent_work': 'released'}
    return {**value, **changes}


ANALYSIS = ('assessment', 'inventory', 'comparison:retry', 'comparison:scheduler', 'selection:retry',
            'selection:scheduler', 'interactions')


def direct(ident, instruction='Compare the retry design again.', **extra):
    return {'id': ident, 'source': 'chat:' + ident, 'instruction': instruction,
            'skill': 'cf-workflows:dk-pr-review-alternatives', 'origin': 'user', **extra}


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cfw alternatives ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        git(self.repo, 'config', 'user.name', 'Fixture')
        self.write('AGENTS.md', 'Run unittest before every change.\n')
        self.write('src/scheduler.py', 'def schedule(job):\n    return job\n')
        self.write('src/retry.py', 'def retry(job):\n    return job\n')
        self.target = self.commit('target')
        self.write('src/retry.py', 'def retry(job):\n    while True:\n        job()\n')
        self.write('src/queue.py', 'QUEUE = []\n')
        self.head = self.commit('head')
        self.write('src/queue.py', 'QUEUE = [None]\n')
        self.later = self.commit('later')
        dk_pr_fix.verify_installation()
        self.state = self.base / 'state'
        self.engine = Engine()
        self.deployment = {'controller': {'run-timeout': '2h'},
                           'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
                           'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}

    def write(self, rel, body):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def commit(self, message):
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', message)
        return git(self.repo, 'rev-parse', 'HEAD').strip()

    def transport(self, script, **extra):
        self.fake = Transport(self.engine, script, **extra)
        return self.fake

    def calls(self, **expected):
        self.assertEqual(self.fake.calls, +Counter(expected))

    def new_sessions(self, before):
        return +(self.engine.sessions - before)

    def record(self, key):
        [path] = [p for p in (self.state / 'runs').iterdir() if '.whole-problem.' not in p.name]
        ledger = alignment.Ledger(path, logical_run='alternatives_' + admission.digest(
            admission.pr_identity(PR, 'pr')['url'])[:32], pr=admission.pr_identity(PR, 'pr'))
        try:
            return ledger.latest(key)
        finally:
            ledger.close()

    def pr_run(self, name, script, *, head=None, caller=None, instructions='Compare the retry design.', run=None,
               **extra):
        transport = self.transport(script, **{k: extra.pop(k) for k in ('lose', 'validators') if k in extra})
        extra.setdefault('requirements', ISSUE)
        request = {'pr': PR, 'instructions': instructions, **({'logical_run': run} if run else {})}
        return alternatives.run(name, request=request, deployment=self.deployment,
                                repository=self.repo, transport=transport, target=self.target,
                                head=head or self.head, state=self.state, caller=caller, **extra)

    def manual(self, request, script, *, head=None, instructions=None, **extra):
        transport = self.transport(script, **{k: extra.pop(k) for k in ('lose',) if k in extra})
        return alternatives.manual_review(
            request={'pr': PR, 'instructions': instructions or request['instruction']}, direct=request,
            deployment=self.deployment, repository=self.repo, transport=transport, target=self.target,
            head=head or self.head, state=self.state, requirements=extra.pop('requirements', ISSUE), **extra)


# -- Presets ------------------------------------------------------------------------------------------

class Presets(Fixture):
    def test_role_and_stage_prompts_are_the_baseline_bytes(self):
        for name in ('dk-issue-alternatives', 'dk-pr-alternatives', 'dk-alternatives-review',
                     'dk-pr-review-alternatives'):
            entry = entries.ENTRIES[name]
            [resource] = [r for r in INVENTORY[name]['baseline_resources'] if r['path'] == entry['baseline_prompt']]
            self.assertEqual(hashlib.sha256(entry['role_prompt'].encode()).hexdigest(), resource['sha256'], name)
            self.assertEqual(entries.preset(name), {'tasks': [entry['adapter']], 'finalizers': [], 'reviewers': []})
        for stage, prompt in alternatives.STAGE_PROMPTS.items():
            self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), alternatives.STAGE_PROMPT_SHA256[stage])
        self.assertEqual(set(alternatives.STAGE_PROMPTS), set(alignment.STAGES))

    def test_baseline_options_with_provenance_and_rejections(self):
        options = entries.check_options('dk-pr-alternatives', {'selection': {'mode': 'manual'}})
        self.assertEqual(options['selection'], {'value': {**entries.ALTERNATIVES_OPTIONS['selection'],
                                                          'mode': 'manual'}, 'source': 'invocation'})
        self.assertEqual(options['scoring']['source'], 'default')
        for bad in ({'selection': {'mode': 'sometimes'}}, {'selection': {'min_score_gap': 101}},
                    {'selection': {'enabled': 'yes'}}, {'scoring': {'profile': 'fast'}}, {'unknown': 1},
                    {'assessment_prompt': ''}, {'selection': {'extra': 1}}, {'evaluation_prompt': ' '},
                    {'evaluation_prompt': 'a\0b'}, {'evaluation_prompt': 'x' * (admission.MAX_TEXT + 1)}):
            with self.assertRaises(admission.AdmissionError, msg=bad):
                entries.check_options('dk-issue-alternatives', bad)
        self.assertEqual(entries.check_options('dk-pr-alternatives', {'evaluation_prompt': ''})
                         ['evaluation_prompt']['value'], '')
        self.assertEqual(entries.check_options('dk-alternatives-review', None), {})
        with self.assertRaises(admission.AdmissionError):
            entries.check_options('dk-pr-review-alternatives', {'selection': {'mode': 'manual'}})

    def test_default_run_loads_no_skill_and_rejects_authority_expansion(self):
        result = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual(result['status'], 'complete', result)
        self.calls(capabilities=1, input_gates=2, native_session=7)  # Semi-manual looks for answered questions.
        for task in self.fake.tasks:
            self.assertEqual((task['skills'], task['tools'], task['writes'], task['publication'], task['adapter']),
                             ([], 'read-only', False, False, 'pr-alternatives'))
            self.assertEqual(task['instructions'], 'Compare the retry design.')
            self.assertEqual(task['stage_prompt'], alternatives.STAGE_PROMPTS[task['mode']])
        self.assertEqual((result['ready'], result['clean_review'], result['publication']), (False, False, False))
        grant = {'post_findings': {'request_id': 'r1', 'instruction': 'Post.', 'source': 'chat'}}
        for kwargs in ({'request': {'pr': PR, 'instructions': 'x', 'authorizations': grant}},
                       {'invocation': {'options': {'tasks': ['review']}}},
                       {'options': {'selection': {'mode': 'never'}}}):
            transport = self.transport({})
            arguments = {'request': {'pr': PR, 'instructions': 'Compare.'}, 'deployment': self.deployment,
                         'repository': self.repo, 'transport': transport, 'target': self.target, 'head': self.head,
                         'state': self.state, 'requirements': ISSUE, **kwargs}
            rejected = alternatives.run('dk-pr-alternatives', **arguments)
            self.assertEqual(rejected['status'], 'rejected', kwargs)
            self.calls()

    def test_custom_prompt_options_use_the_shared_semantic_admission(self):
        result = self.pr_run('dk-pr-alternatives', branch_script(), validators=[verdict('accepted')],
                             options={'evaluation_prompt': 'Weigh operability heavily.'})
        self.assertEqual(result['status'], 'complete', result)
        self.calls(capabilities=1, input_gates=3, validator_session=1, native_session=7)
        self.assertEqual(self.fake.tasks[0]['prompts'], [{'id': 'option.evaluation_prompt', 'slot': 'additional',
                                                          'target': 'pr-alternatives',
                                                          'text': 'Weigh operability heavily.'}])
        rejected = self.pr_run('dk-pr-alternatives', {}, validators=[verdict('rejected', conflicts=[
            {'summary': 'Waives a prohibition.', 'paths': lambda task: [task['subjects'][0]['path']]}])],
            options={'selection': {'prompt': 'Always pick the PR.'}})
        self.assertNotEqual(rejected['status'], 'complete')
        self.assertEqual(self.fake.calls['native_session'], 0)

    def test_installed_workflow_composes_only_its_own_scope(self):
        # dk-pr-fix composes whole-problem analysis and dk-pr-review branch analysis, each through the
        # staged procedure (workflows/alternatives.yml); the other scope belongs to the entries.
        for mode, adapter in (('fix', 'pr-alternatives'), ('review', 'issue-alternatives')):
            rejected = dk_pr_fix.preflight(
                request={'pr': PR, 'instructions': 'Fix it.'}, configuration=None, transport=self.transport({}),
                invocation={'options': {'tasks': [adapter, 'address-feedback' if mode == 'fix' else 'read-feedback'],
                                        'finalizers': []}},
                deployment=self.deployment, repository=self.repo, home=self.state, mode=mode)
            self.assertEqual((rejected['preflight'], rejected['error']['path']), ('rejected', 'options.tasks'),
                             rejected)
        admitted = dk_pr_fix.preflight(
            request={'pr': PR, 'instructions': 'Fix it.'}, configuration=None, transport=self.transport({}),
            invocation={'options': {'tasks': ['issue-alternatives', 'address-feedback'], 'finalizers': []}},
            deployment=self.deployment, repository=self.repo, home=self.state)
        self.assertEqual(admitted['preflight'], 'admitted', admitted.get('error'))
        self.assertEqual(admitted['inputs']['adapters']['issue-alternatives']['kind'], 'alternatives')


# -- Whole problem versus branch ------------------------------------------------------------------------

class Scope(Fixture):
    def issue_only(self, script, instructions='Compare approaches for issue 27.', **extra):
        transport = self.transport(script)
        extra.setdefault('requirements', ISSUE)
        return alternatives.run('dk-issue-alternatives', request={'repository': REPOSITORY,
                                                                   'instructions': instructions},
                                deployment=self.deployment, repository=self.repo, transport=transport,
                                target=self.target, state=self.state, **extra)

    def test_issue_only_analysis_persists_nothing_and_invents_no_pr(self):
        script = whole_script(kinds=('smallest', 'alternative'))
        result = self.issue_only(script)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual((result['persisted'], result['pins']['pr'], result['pins']['head']),
                         (False, None, self.target))
        self.assertEqual(result['selections']['whole']['choice'], 'smallest')
        self.assertEqual(result['selections']['whole']['scores'], {'smallest': 90.0, 'alternative': 60.0})
        self.calls(capabilities=1, input_gates=1, native_session=5)
        self.assertFalse(self.state.exists())
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertTrue(all(task['context']['pins']['pr'] is None for task in self.fake.tasks))
        # Interrupted or repeated: engine claims alone make the same call reusable.
        before = copy.copy(self.engine.sessions)
        again = self.issue_only(script)
        self.assertEqual(again['status'], 'complete')
        self.assertEqual(self.new_sessions(before), Counter())
        # A pr candidate without a PR is never accepted.
        invented = self.issue_only(whole_script(), instructions='Compare once more.')
        self.assertEqual((invented['status'], invented['stage']), ('blocked', 'comparison:whole'))
        self.assertIn('pr candidate', invented['error']['message'])
        missing = self.issue_only(script, requirements=[])
        self.assertEqual((missing['status'], missing['error']['path']), ('rejected', 'requirements'))

    def test_issue_only_rejection_is_retried_without_a_record(self):
        script = whole_script(kinds=('smallest', 'alternative'))
        script['inventory'] = [whole_inventory(origin='inherited'), whole_inventory()]
        result = self.issue_only(script)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.engine.sessions['inventory'], 2)
        before = copy.copy(self.engine.sessions)
        self.assertEqual(self.issue_only(script)['status'], 'complete')  # Replays the claims: no session.
        self.assertEqual(self.new_sessions(before), Counter())
        # Every attempt rejected: bounded per stage, and later calls start no session.
        self.state, script['inventory'] = self.base / 'x', [whole_inventory(origin='inherited')] * 3
        blocked = self.issue_only(script, instructions='Compare again for issue 27.')
        self.assertEqual((blocked['status'], blocked['stage']), ('blocked', 'inventory'))
        self.assertEqual(self.engine.sessions['inventory'], 5)
        self.assertEqual(self.issue_only(script, instructions='Compare again for issue 27.')['status'], 'blocked')
        self.assertEqual(self.engine.sessions['inventory'], 5)
        self.assertFalse(self.state.exists())

    def test_issue_only_question_is_asked_once_and_its_answer_is_used(self):
        close = comparison(('smallest', 'alternative'), ratings={'smallest': 70, 'alternative': 65})
        script = whole_script(kinds=('smallest', 'alternative'), **{'comparison:whole': close,
                                                                     'selection:whole': [auto(None), manual()]})
        self.assertEqual(self.issue_only(script)['status'], 'waiting')
        self.assertEqual(self.issue_only(script)['status'], 'waiting')
        self.assertEqual(len(self.engine.gates), 1)
        self.engine.answer(self.engine.gates[0]['gate_id'], 'answer-1', 'alternative')
        done = self.issue_only(script)
        self.assertEqual((done['status'], done['selections']['whole']['choice']), ('complete', 'alternative'), done)
        self.assertEqual((len(self.engine.gates), self.engine.sessions['selection:whole']), (1, 2))
        self.assertFalse(self.state.exists())

    def test_changed_paths_and_guidance_stay_readable_past_the_bound(self):
        with patch.object(alternatives._H, 'MAX_READABLE', 1):
            result = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual(set(self.fake.tasks[0]['readable']), {'git:src/queue.py', 'git:src/retry.py', 'git:AGENTS.md'})
        self.assertEqual(result['gaps'][0]['subject'], 'readable')

    def test_whole_problem_and_branch_scopes_keep_separate_records_and_contracts(self):
        whole = self.pr_run('dk-issue-alternatives', whole_script())
        self.assertEqual((whole['status'], whole['scope'], whole['persisted']), ('complete', 'whole-problem', True))
        branch = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual((branch['status'], branch['scope']), ('complete', 'branch'))
        # The branch record adopts the shared assessment and runs the rest itself.
        self.assertEqual([item['action'] for item in branch['stages']].count('run'), 6)
        self.assertEqual(branch['stages'][0]['action'], 'shared')
        runs = sorted(path.name for path in (self.state / 'runs').iterdir())
        self.assertEqual(len(runs), 2)
        self.assertTrue(any('.whole-problem.' in name for name in runs))
        # Each scope's inventory and comparison contract.
        cases = [
            ('dk-issue-alternatives', {'inventory': whole_inventory(origin='inherited')}, 'inventory'),
            ('dk-issue-alternatives', {'inventory': whole_inventory(evidence=['git:src/retry.py'])}, 'inventory'),
            ('dk-pr-alternatives', {'inventory': {**branch_inventory(), 'coverage': 'Only the diff.'}}, 'inventory'),
            ('dk-pr-alternatives', {'inventory': branch_inventory(retry={'evidence': ['git:src/scheduler.py']})},
             'inventory'),
            ('dk-pr-alternatives', {'inventory': branch_inventory(retry={'origin': 'whole_problem'})}, 'inventory'),
            ('dk-pr-alternatives', {'comparison:retry': comparison(('smallest', 'alternative'))}, 'comparison:retry'),
            ('dk-pr-alternatives', {'comparison:retry': comparison(target_revision=self.head)}, 'comparison:retry'),
        ]
        for name, override, stage in cases:
            self.state = self.base / ('state-' + hashlib.sha256(repr(override).encode()).hexdigest()[:8])
            script = (whole_script() if name == 'dk-issue-alternatives' else branch_script())
            script.update(override)
            result = self.pr_run(name, script)
            self.assertEqual((result['status'], result['stage']), ('blocked', stage), (name, override, result))

    def test_whole_problem_and_branch_records_share_one_assessment(self):
        whole = self.pr_run('dk-issue-alternatives', whole_script())
        self.assertEqual(whole['status'], 'complete', whole)
        before = copy.copy(self.engine.sessions)
        branch = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual(branch['status'], 'complete', branch)
        self.assertEqual(self.new_sessions(before), Counter({key: 1 for key in ANALYSIS[1:]}))
        [origin] = [item['reference'] for item in whole['stages'] if item['key'] == 'assessment']
        shared = branch['stages'][0]
        self.assertEqual((shared['key'], shared['action'], shared['origin']),
                         ('assessment', 'shared', 'whole-problem:' + origin))
        self.assertEqual(self.record('assessment')['result'], assessment())
        self.assertEqual(branch['allowances']['architecture']['used'], 6)  # Adoption reserves nothing.
        inventory = next(task for task in self.fake.tasks if task['mode'] == 'inventory')
        self.assertEqual(inventory['context']['assessment'], assessment())
        # Settled once adopted: later runs reuse it from their own record.
        before = copy.copy(self.engine.sessions)
        again = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual((again['stages'][0]['action'], self.new_sessions(before)), ('reuse', Counter()))
        # Branch first: the whole-problem record adopts the branch assessment.
        self.state = self.base / 'branch first'
        self.pr_run('dk-pr-alternatives', branch_script())
        before = copy.copy(self.engine.sessions)
        whole = self.pr_run('dk-issue-alternatives', whole_script())
        self.assertEqual((whole['status'], whole['stages'][0]['action']), ('complete', 'shared'))
        self.assertTrue(whole['stages'][0]['origin'].startswith('branch:assessment@alt-'))
        self.assertEqual(self.new_sessions(before)['assessment'], 0)
        # An assessment this run cannot cite (other requirements) is not adopted; this scope runs its own.
        self.state = self.base / 'other requirements'
        self.pr_run('dk-issue-alternatives', whole_script())
        ticket = [{'source': 'ticket:9', 'text': 'Retries must be bounded.'}]
        own = assessment(evidence=['ticket:9'], rubrics=[{**RUBRIC, 'evidence': ['ticket:9']}])
        before = copy.copy(self.engine.sessions)
        branch = self.pr_run('dk-pr-alternatives', branch_script(assessment=own), requirements=ticket)
        self.assertEqual((branch['status'], branch['stages'][0]['action']), ('complete', 'run'), branch)
        self.assertEqual(self.new_sessions(before)['assessment'], 1)
        # An assessment made under other assessment options (here the scoring profile) is not adopted.
        self.state = self.base / 'other scoring'
        self.pr_run('dk-issue-alternatives', whole_script(), options={'scoring': {'profile': 'quality'}})
        before = copy.copy(self.engine.sessions)
        branch = self.pr_run('dk-pr-alternatives', branch_script(),
                             options={'scoring': {'profile': 'minimal_change'}})
        self.assertEqual((branch['status'], branch['stages'][0]['action']), ('complete', 'run'), branch)
        self.assertEqual(self.new_sessions(before)['assessment'], 1)

    def test_reconciled_decisions_feed_the_inventory_and_bind_current_alignment(self):
        handoff = reconciled()
        cited = branch_inventory(retry={'evidence': ['git:src/retry.py:2', 'decision:bounded-retries']})
        result = self.pr_run('dk-pr-alternatives', branch_script(inventory=cited, alignment=current()),
                             caller='cf-workflows:dk-pr-review', decisions=handoff)
        self.assertEqual((result['status'], result['decisions']), ('released', 'decisions-1'), result)
        inventory = next(task for task in self.fake.tasks if task['mode'] == 'inventory')
        self.assertEqual(inventory['context']['decisions'], handoff)
        aligned = next(task for task in self.fake.tasks if task['mode'] == 'alignment')
        self.assertEqual(aligned['context']['decisions'], handoff)
        # A new reconciliation refreshes current alignment only; the settled inventory stays.
        before = copy.copy(self.engine.sessions)
        later = reconciled(result_ref='decisions-2', commitments=[*handoff['commitments'], {
            'id': 'log-retries', 'subject': 'Logging', 'text': 'Log each retry.', 'status': 'settled',
            'scope': 'src/retry.py', 'sources': ['comment-2']}])
        again = self.pr_run('dk-pr-alternatives', branch_script(alignment=current()),
                            caller='cf-workflows:dk-pr-review', decisions=later)
        self.assertEqual(again['status'], 'released', again)
        self.assertEqual(self.new_sessions(before), Counter({'alignment': 1}))
        # The inventory may cite only reconciled decisions it was given.
        self.state = self.base / 'unknown decision'
        unknown = branch_inventory(retry={'evidence': ['git:src/retry.py', 'decision:other']})
        blocked = self.pr_run('dk-pr-alternatives', branch_script(inventory=unknown), decisions=handoff)
        self.assertEqual((blocked['status'], blocked['stage']), ('blocked', 'inventory'))
        # A reconciled decision is not a whole-problem requirement.
        self.state = self.base / 'whole problem'
        whole = self.pr_run('dk-issue-alternatives', whole_script(
            inventory=whole_inventory(evidence=['decision:bounded-retries'])), decisions=handoff)
        self.assertEqual((whole['status'], whole['stage']), ('blocked', 'inventory'))
        # Malformed or inconsistent handoffs reject before any session.
        for bad in ({**handoff, 'dependent_work': 'blocked'}, {**handoff, 'commitments': [{'id': 'x'}]},
                    {**handoff, 'unresolved': [{'id': 'c'}]}, {k: v for k, v in handoff.items() if k != 'questions'}):
            self.state = self.base / 'bad'
            rejected = self.pr_run('dk-pr-alternatives', {}, decisions=bad)
            self.assertEqual((rejected['status'], rejected['error']['path'].split('.')[0]), ('rejected', 'decisions'))
            self.assertEqual(self.fake.calls['native_session'], 0)

    def test_inherited_decisions_are_inventoried_and_compared(self):
        result = self.pr_run('dk-pr-alternatives', branch_script())
        inventory = next(task for task in self.fake.tasks if task['mode'] == 'inventory')
        self.assertEqual(inventory['context']['pins']['changed'], ['src/queue.py', 'src/retry.py'])
        self.assertIn('git:src/scheduler.py', inventory['readable'])  # The whole branch, not only the diff.
        self.assertEqual(set(result['selections']), {'retry', 'scheduler'})  # Only questionable decisions.
        compared = {task['decision'] for task in self.fake.tasks if task['mode'] == 'comparison'}
        self.assertEqual(compared, {'retry', 'scheduler'})


# -- Selection ------------------------------------------------------------------------------------------

class Selection(Fixture):
    def test_automatic_mode_never_prompts_and_elapsed_time_never_chooses(self):
        close = comparison(ratings={'pr': 80, 'smallest': 85, 'alternative': 20})
        script = branch_script(**{'comparison:retry': close, 'selection:retry': auto(None)})
        options = {'selection': {'mode': 'automatic'}}
        result = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((result['status'], result['decisions']), ('choice_pending', ['retry']))
        self.assertEqual(self.fake.calls['open_input_gate'] + self.fake.calls['input_gates'], 0)
        before = copy.copy(self.engine.sessions)
        again = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual(again['status'], 'choice_pending')
        self.assertEqual(self.new_sessions(before), Counter())
        # A pick without the required lead settles nothing and stays pending; no later session decides it.
        self.state = self.base / 'other'
        script = branch_script(**{'comparison:retry': close, 'selection:retry': [auto(), auto()]})
        refused = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((refused['status'], refused['decisions']), ('choice_pending', ['retry']), refused)
        self.assertEqual(self.engine.gates, [])
        before = copy.copy(self.engine.sessions)
        again = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((again['status'], self.new_sessions(before)), ('choice_pending', Counter()))
        record = self.record('selection:retry')
        self.assertEqual((record['result']['choice'], record['result']['unqualified']['original']['choice']),
                         (None, 'smallest'))
        self.assertIn('min_score_gap', record['result']['unqualified']['message'])

    def test_manual_mode_asks_instead_of_accepting_an_automatic_pick(self):
        script = branch_script(**{'selection:retry': [auto(), manual()], 'selection:scheduler': [auto(), manual()]})
        options = {'selection': {'mode': 'manual'}}
        result = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((result['status'], sorted(result['decisions'])), ('waiting', ['retry', 'scheduler']))
        self.assertEqual(len(self.engine.gates), 2)
        for gate in self.engine.gates:
            self.engine.answer(gate['gate_id'], 'answer-' + gate['gate_id'], 'alternative')
        done = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual(done['status'], 'complete', done)
        self.assertEqual(self.engine.sessions['selection:retry'], 2)

    def test_an_answer_that_is_not_text_is_asked_again(self):
        script = branch_script(**{'selection:retry': [auto(None), manual()],
                                  'selection:scheduler': [auto(None), manual()]})
        options = {'selection': {'mode': 'manual'}}
        self.pr_run('dk-pr-alternatives', script, options=options)
        self.engine.answer(self.engine.gates[0]['gate_id'], 'answer-list', ['smallest'])
        self.engine.answer(self.engine.gates[1]['gate_id'], 'answer-ok', 'smallest')
        again = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((again['status'], again['decisions'], len(self.engine.gates)), ('waiting', ['retry'], 3))
        self.assertEqual(self.engine.gates[2]['request']['payload']['rejected_answers'], ['answer-list'])

    def test_manual_choice_is_asked_once_resumed_and_sourced(self):
        script = branch_script(**{'selection:retry': [auto(None), manual()],
                                  'selection:scheduler': [auto(None), manual()]})
        options = {'selection': {'mode': 'manual'}}
        result = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((result['status'], sorted(result['decisions'])), ('waiting', ['retry', 'scheduler']))
        self.assertEqual(len(self.engine.gates), 2)
        before = copy.copy(self.engine.sessions)
        waiting = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual(waiting['status'], 'waiting')
        self.assertEqual((self.new_sessions(before), len(self.engine.gates)), (Counter(), 2))
        self.calls(capabilities=1, input_gates=2)
        for gate, choice in zip(self.engine.gates, ('smallest', 'pr')):
            self.engine.answer(gate['gate_id'], 'answer-' + gate['gate_id'], choice)
        done = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual(done['status'], 'complete', done)
        self.assertEqual({k: v['choice'] for k, v in done['selections'].items()},
                         {'retry': 'smallest', 'scheduler': 'pr'})
        self.assertEqual(self.new_sessions(before), Counter({'selection:retry': 1, 'selection:scheduler': 1,
                                                             'interactions': 1}))

    def test_semi_manual_asks_when_an_automatic_pick_does_not_qualify(self):
        close = comparison(ratings={'pr': 80, 'smallest': 85, 'alternative': 20})
        script = branch_script(**{'comparison:retry': close, 'selection:retry': [auto(), manual()]})
        result = self.pr_run('dk-pr-alternatives', script)
        self.assertEqual((result['status'], result['decisions']), ('waiting', ['retry']), result)
        self.assertEqual(len(self.engine.gates), 1)
        before = copy.copy(self.engine.sessions)
        self.assertEqual(self.pr_run('dk-pr-alternatives', script)['status'], 'waiting')
        self.assertEqual((self.new_sessions(before), len(self.engine.gates)), (Counter(), 1))
        self.engine.answer(self.engine.gates[0]['gate_id'], 'answer-1', 'pr')
        done = self.pr_run('dk-pr-alternatives', script)
        self.assertEqual((done['status'], done['selections']['retry']['choice']), ('complete', 'pr'), done)
        self.assertEqual(self.new_sessions(before), Counter({'selection:retry': 1, 'interactions': 1}))

    def test_an_answer_naming_no_feasible_option_is_asked_again_without_a_session(self):
        options = {'selection': {'mode': 'manual'}}
        script = branch_script(**{'selection:retry': [auto(None), manual()],
                                  'selection:scheduler': [auto(None), manual()]})
        self.assertEqual(self.pr_run('dk-pr-alternatives', script, options=options)['status'], 'waiting')
        first = [gate['gate_id'] for gate in self.engine.gates]
        before = copy.copy(self.engine.sessions)
        self.engine.answer(first[0], 'answer-bogus', 'bogus')
        self.engine.answer(first[1], 'answer-ok', 'smallest')
        again = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((again['status'], again['decisions']), ('waiting', ['retry']), again)
        self.assertEqual(len(self.engine.gates), 3)
        self.assertEqual(self.engine.gates[2]['request']['payload']['rejected_answers'], ['answer-bogus'])
        self.assertEqual(self.new_sessions(before), Counter({'selection:scheduler': 1}))
        self.engine.answer(self.engine.gates[2]['gate_id'], 'answer-2', 'alternative')
        done = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual(done['status'], 'complete', done)
        self.assertEqual(done['selections']['retry']['choice'], 'alternative')

    def test_rejected_output_closes_its_attempt_and_the_next_call_starts_a_new_one(self):
        bad = {**branch_inventory(), 'coverage': 'Only the diff.'}
        script = branch_script(inventory=[bad, branch_inventory()])
        rejected = self.pr_run('dk-pr-alternatives', script)
        self.assertEqual((rejected['status'], rejected['stage']), ('blocked', 'inventory'))
        result = self.pr_run('dk-pr-alternatives', script)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.engine.sessions['inventory'], 2)
        self.assertEqual(result['allowances']['architecture']['used'], 8)  # The rejected attempt stays reserved.

    def test_manual_selection_cannot_invent_a_user_source(self):
        script = branch_script(**{'selection:retry': [auto(None), manual(user_source='answer-invented')],
                                  'selection:scheduler': [auto(None), manual()]})
        options = {'selection': {'mode': 'semi-manual', 'enabled': False}}
        self.assertEqual(self.pr_run('dk-pr-alternatives', script, options=options)['status'], 'waiting')
        for gate in self.engine.gates:
            self.engine.answer(gate['gate_id'], 'answer-' + gate['gate_id'], 'smallest')
        result = self.pr_run('dk-pr-alternatives', script, options=options)
        self.assertEqual((result['status'], result['stage']), ('blocked', 'selection:retry'))
        self.assertEqual(result['error']['path'], 'selection.user_source')


# -- Settled analysis, current alignment and explicit fresh comparison -------------------------------------

class Boundary(Fixture):
    def test_analysis_settles_once_per_pr_across_requirements_guidance_heads_and_routes(self):
        first = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual(first['status'], 'complete')
        before = copy.copy(self.engine.sessions)
        self.write('AGENTS.md', 'Prefer the shared scheduler.\n')
        changed = self.pr_run('dk-pr-alternatives', branch_script(), head=self.later,
                              requirements=ISSUE + [{'source': 'ticket:9', 'text': 'Also log retries.'}],
                              options={'selection': {'mode': 'automatic'}}, instructions='Look again.')
        self.assertEqual(changed['status'], 'complete')
        self.assertEqual(self.new_sessions(before), Counter())
        self.assertTrue(all(item['action'] == 'reuse' for item in changed['stages']))
        # The review route reuses the same settled analysis and runs only current alignment.
        review = self.pr_run('dk-pr-alternatives', branch_script(alignment=current()), caller='cf-workflows:dk-pr-review')
        self.assertEqual((review['status'], review['gate']['release']), ('released', True))
        self.assertEqual(self.new_sessions(before), Counter({'alignment': 1}))

    def test_new_requirements_refresh_alignment_without_comparison(self):
        """dk-workflows#241: an uncertain alignment takes in new requirements with no architectural session."""
        script = branch_script(alignment=lambda task: current('mismatch' if task['context']['evidence'] else 'uncertain'),
                               verification=verified())
        first = self.pr_run('dk-pr-alternatives', script, caller='cf-workflows:dk-pr-review')
        self.assertEqual(first['status'], 'alignment_uncertain', first)
        self.assertEqual(first['gate']['allowance']['used'], 1)  # This run's own alignment count.
        before = copy.copy(self.engine.sessions)
        ticket = [{'source': 'ticket:9', 'text': 'Retries go through the scheduler.'}]
        second = self.pr_run('dk-pr-alternatives', script, caller='cf-workflows:dk-pr-review',
                             evidence=ticket)
        self.assertEqual(second['status'], 'mismatch_verified', second)
        self.assertEqual(second['gate']['required_changes'], CHANGES)
        self.assertEqual(self.new_sessions(before), Counter({'alignment': 1, 'verification': 1}))
        self.assertEqual(second['allowances']['architecture']['used'], 7)
        self.assertEqual(second['allowances']['alignment']['used'], 3)
        verification = [task for task in self.fake.tasks if task['mode'] == 'verification'][-1]
        self.assertEqual(verification['role'], 'verifier')
        # A later head refreshes alignment only (dk-workflows#239: no historical receipt needed).
        third = self.pr_run('dk-pr-alternatives', branch_script(alignment=current()), head=self.later,
                            caller='cf-workflows:dk-pr-review', evidence=ticket)
        self.assertEqual(third['status'], 'released', third)
        self.assertEqual(self.new_sessions(before), Counter({'alignment': 2, 'verification': 1}))

    def test_verification_must_be_an_independent_session(self):
        same = (verified(), lambda task: {'session_id': self.alignment_session})
        script = branch_script(alignment=current('mismatch'), verification=same)
        self.alignment_session = None

        original = respond

        def capture(task, output):
            value = original(task, output)
            if task['mode'] == 'alignment':
                self.alignment_session = value['observation']['session_id']
            return value
        with patch(__name__ + '.respond', capture):
            result = self.pr_run('dk-pr-alternatives', script, caller='cf-workflows:dk-pr-review')
        self.assertEqual((result['status'], result['stage']), ('blocked', 'verification'))

    def test_explicit_fresh_comparison_refreshes_requested_analysis_once_per_request(self):
        self.pr_run('dk-pr-alternatives', branch_script())
        before = copy.copy(self.engine.sessions)
        script = branch_script(alignment=current())
        result = self.manual(direct('m1'), script)
        self.assertEqual(result['status'], 'released', result)
        self.assertEqual((result['entry'], result['delegate'], result['request']['id']),
                         ('cf-workflows:dk-pr-review-alternatives', 'cf-workflows:dk-pr-alternatives', 'm1'))
        self.assertEqual((result['ready'], result['clean_review'], result['gate']['ready']), (False, False, False))
        refreshed = self.new_sessions(before)
        self.assertEqual(refreshed, Counter({key: 1 for key in ANALYSIS if key != 'assessment'} | {'alignment': 1}))
        # Replaying the same request repeats nothing; a distinct request refreshes again.
        before = copy.copy(self.engine.sessions)
        self.assertEqual(self.manual(direct('m1'), script)['status'], 'released')
        self.assertEqual(self.new_sessions(before), Counter())
        # A distinct request never renews the retained allowance; only an explicit grant adds capacity.
        again = self.manual(direct('m2', 'Compare it once more.'), script)
        self.assertEqual((again['status'], again['stage']), ('exhausted', 'comparison:scheduler'))
        self.assertEqual(again['allowance'], {'allowance': 'architecture', 'used': 16, 'limit': 16,
                                              'exhausted': True})
        usage = alternatives.grant(state=self.state, pr=PR, request_id='continue-1', allowance='architecture',
                                   amount=3, authorization='Continue the comparison.')
        self.assertEqual((usage['limit'], usage['exhausted']), (19, False))
        again = self.manual(direct('m2', 'Compare it once more.'), script)
        self.assertEqual(again['status'], 'released', again)
        self.assertEqual(self.new_sessions(before), Counter({key: 1 for key in ANALYSIS if key != 'assessment'}
                                                            | {'alignment': 1}))
        self.assertEqual(again['allowances']['architecture']['used'], 7 + 6 + 6)
        # Automatic routes afterwards reuse the refreshed analysis.
        before = copy.copy(self.engine.sessions)
        self.assertEqual(self.pr_run('dk-pr-alternatives', branch_script())['status'], 'complete')
        self.assertEqual(self.new_sessions(before), Counter())

    def test_only_a_distinct_direct_user_request_authorizes_a_fresh_comparison(self):
        for request, instructions in ((direct('a1', origin='agent'), None),
                                      (direct('a2', skill='cf-workflows:dk-pr-alternatives'), None),
                                      ({**direct('a3'), 'extra': 1}, None),
                                      (direct('a4'), 'A different instruction.')):
            result = self.manual(request, {}, instructions=instructions)
            self.assertEqual(result['status'], 'rejected', request)
            self.calls()
        composed = alternatives.manual_review(
            request={'pr': PR, 'instructions': 'Compare.'}, direct=direct('a5', 'Compare.'),
            deployment=self.deployment, repository=self.repo, transport=self.transport({}), target=self.target,
            head=self.head, state=self.state, caller='cf-workflows:dk-pr-review')
        self.assertEqual((composed['status'], composed['error']['path']), ('rejected', 'caller'))
        self.calls()

    def test_a_malformed_direct_request_field_is_rejected_not_raised(self):
        for request in (direct(''), {**direct('a6'), 'id': 7}, {**direct('a7'), 'source': 'nul\0'}):
            result = self.manual(request, {}, instructions='Compare the retry design again.')
            self.assertEqual((result['status'], result['error']['path']), ('rejected', 'direct'), request)
            self.calls()

    def test_an_unreadable_branch_path_is_rejected_not_raised(self):
        (self.repo / os.fsdecode(b'caf\xe9.txt')).write_text('x\n')
        head = self.commit('latin-1 name')
        result = self.pr_run('dk-pr-alternatives', {}, head=head)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['error'], {'path': 'repository', 'message': 'checkout unreadable: UnicodeDecodeError'})
        self.calls(capabilities=1)

    def test_an_unopenable_ledger_is_blocked_not_raised(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text('not a directory')
        result = self.pr_run('dk-pr-alternatives', {})
        self.assertEqual((result['status'], result['error']['path']), ('blocked', 'ledger'))
        self.calls(capabilities=1)
        result = alternatives.run('dk-pr-alternatives', request={'pr': PR, 'instructions': 'Compare.'},
                                  deployment=self.deployment, repository=self.repo, transport=self.transport({}),
                                  target=self.target, head=self.head, state=7, requirements=ISSUE)
        self.assertEqual((result['status'], result['error']['path']), ('blocked', 'ledger'))

    def test_interrupted_fresh_request_resumes_without_repeating_siblings(self):
        self.pr_run('dk-pr-alternatives', branch_script())
        before = copy.copy(self.engine.sessions)
        script = branch_script(alignment=current())
        interrupted = self.manual(direct('m1'), script, lose={'comparison:scheduler': 1})
        self.assertEqual((interrupted['status'], interrupted['stage']), ('blocked', 'comparison:scheduler'))
        # Meanwhile an automatic route sees the changed inventory and refuses to compare.
        automatic = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual((automatic['status'], automatic['skill']),
                         ('authorization_required', 'cf-workflows:dk-pr-review-alternatives'))
        resumed = self.manual(direct('m1'), script)
        self.assertEqual(resumed['status'], 'released', resumed)
        actions = {item['key']: item['action'] for item in resumed['stages']}
        self.assertEqual((actions['inventory'], actions['comparison:retry'], actions['comparison:scheduler']),
                         ('reuse', 'reuse', 'resume'))
        self.assertEqual(self.new_sessions(before), Counter({key: 1 for key in ANALYSIS if key != 'assessment'}
                                                            | {'alignment': 1}))

    def test_interrupted_analysis_resumes_its_claim_without_a_second_session(self):
        lost = self.pr_run('dk-pr-alternatives', branch_script(), lose={'comparison:retry': 1})
        self.assertEqual((lost['status'], lost['stage']), ('blocked', 'comparison:retry'))
        resumed = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual(resumed['status'], 'complete', resumed)
        actions = {item['key']: item['action'] for item in resumed['stages']}
        self.assertEqual((actions['assessment'], actions['comparison:retry']), ('reuse', 'resume'))
        self.assertEqual(self.engine.sessions, Counter({name: 1 for name in ANALYSIS}))
        self.assertEqual(resumed['allowances']['architecture']['used'], 7)

    def test_automatic_route_never_supersedes_an_interrupted_direct_request(self):
        script = branch_script(inventory=[branch_inventory(), branch_inventory('retry', 'scheduler', 'queue', 'timer')],
                               alignment=current(), **{'comparison:timer': comparison(), 'selection:timer': auto()})
        self.pr_run('dk-pr-alternatives', script)
        lost = self.manual(direct('m1'), script, lose={'comparison:timer': 1})
        self.assertEqual((lost['status'], lost['stage']), ('blocked', 'comparison:timer'))
        automatic = self.pr_run('dk-pr-alternatives', script)
        self.assertEqual((automatic['status'], automatic['stage']), ('authorization_required', 'comparison:timer'))
        resumed = self.manual(direct('m1'), script)
        self.assertEqual(resumed['status'], 'released', resumed)
        self.assertEqual(self.engine.sessions['comparison:timer'], 1)

    def test_current_alignment_keeps_its_allowance_per_logical_run(self):
        script = branch_script(alignment=current())
        with patch.dict(alignment.LIMITS, {'alignment': 1}):
            first = self.pr_run('dk-pr-alternatives', script, caller='cf-workflows:dk-pr-review', run='run-a')
            self.assertEqual(first['status'], 'released', first)
            later = self.pr_run('dk-pr-alternatives', script, head=self.later, caller='cf-workflows:dk-pr-review',
                                run='run-a')
            self.assertEqual((later['status'], later['stage']), ('exhausted', 'alignment'))
            before = copy.copy(self.engine.sessions)
            other = self.pr_run('dk-pr-alternatives', script, head=self.later, caller='cf-workflows:dk-pr-review',
                                run='run-b')
        self.assertEqual(other['status'], 'released', other)
        self.assertEqual(self.new_sessions(before), Counter({'alignment': 1}))
        self.assertEqual(other['allowances']['alignment']['used'], 1)
        self.assertEqual(other['allowances']['architecture']['used'], 7)

    def test_retained_allowance_is_never_renewed(self):
        with patch.dict(alignment.LIMITS, {'architecture': 3}):
            result = self.pr_run('dk-pr-alternatives', branch_script())
        self.assertEqual((result['status'], result['stage']), ('exhausted', 'selection:retry'))
        before = copy.copy(self.engine.sessions)
        again = self.pr_run('dk-pr-alternatives', branch_script())  # The default ceiling never resets it.
        self.assertEqual((again['status'], again['allowance']['limit']), ('exhausted', 3))
        self.assertEqual(self.new_sessions(before), Counter())


# -- Composition ------------------------------------------------------------------------------------------

class Composition(Fixture):
    def test_fix_route_hands_settled_choices_and_required_changes_to_implementation(self):
        result = self.pr_run('dk-issue-alternatives', whole_script(alignment=current('mismatch')),
                             caller='cf-workflows:dk-pr-fix')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['handoff'], {'choices': {'whole': 'smallest'}, 'alignment': 'mismatch',
                                             'required_changes': CHANGES, 'tests_required': True})
        self.assertEqual(self.engine.sessions['verification'], 0)
        before = copy.copy(self.engine.sessions)
        again = self.pr_run('dk-issue-alternatives', whole_script(alignment=current()), head=self.later,
                            caller='cf-workflows:dk-pr-fix')
        self.assertEqual(again['handoff']['alignment'], 'aligned')
        self.assertEqual(self.new_sessions(before), Counter({'alignment': 1}))

    def test_unresolved_reconciliation_blocks_the_fix_handoff_but_not_the_analysis(self):
        contested = reconciled(unresolved=[{'id': 'retry-limit', 'subject': 'Retry limit', 'decisions': [
            'bounded-retries'], 'sources': ['comment-1'], 'question': 'Five or ten?', 'resolution': None}],
            dependent_work='blocked')
        result = self.pr_run('dk-issue-alternatives', whole_script(alignment=current('mismatch')),
                             caller='cf-workflows:dk-pr-fix', decisions=contested)
        self.assertEqual(result['status'], 'decisions_blocked', result)
        self.assertNotIn('handoff', result)
        self.assertEqual(result['unresolved'], contested['unresolved'])
        self.assertEqual(result['selections']['whole']['choice'], 'smallest')  # The analysis is settled.
        # The user's answer releases the handoff with no architectural session.
        before = copy.copy(self.engine.sessions)
        released = self.pr_run('dk-issue-alternatives', whole_script(alignment=current('mismatch')),
                               caller='cf-workflows:dk-pr-fix', decisions=reconciled(result_ref='decisions-2'))
        self.assertEqual((released['status'], released['handoff']['required_changes']), ('complete', CHANGES))
        self.assertEqual(self.new_sessions(before), Counter({'alignment': 1}))

    def test_coordinators_compose_only_their_scope(self):
        for name, caller in (('dk-issue-alternatives', 'cf-workflows:dk-pr-review'),
                             ('dk-pr-alternatives', 'cf-workflows:dk-pr-fix'),
                             ('dk-pr-alternatives', 'cf-workflows:dk-pr-iter')):
            result = self.pr_run(name, {}, caller=caller)
            self.assertEqual((result['status'], result['error']['path']), ('rejected', 'caller'))
            self.calls()


# -- Saved entry options ----------------------------------------------------------------------------------

def scope(ident, kind, target, **saved):
    return {'id': ident, 'selector': {'kind': kind, 'id': target}, 'options': {}, 'permissions': {}, 'prompts': [],
            'skills': [], 'preferences': [], 'entries': saved}


def document(*scopes):
    return {**configuration.empty_configuration(), 'revision': 'rev-1', 'scopes': list(scopes)}


PR_URL = admission.pr_identity(PR, 'pr')['url']


class SavedOptions(Fixture):
    def test_saved_entry_options_apply_under_invocation_options_with_provenance(self):
        saved = document(
            scope('repo', 'repository', REPOSITORY,
                  **{'dk-pr-alternatives': {'selection': {'mode': 'automatic', 'min_score_gap': 20}}}),
            scope('this-pr', 'pr', PR_URL, **{'dk-pr-alternatives': {'selection': {'min_score_gap': 5}}}),
            scope('mine', 'personal', 'me', **{'dk-issue-alternatives': {'selection': {'mode': 'manual'}}}),
            scope('elsewhere', 'repository', 'github.com/other/repo',
                  **{'dk-pr-alternatives': {'selection': {'mode': 'manual'}}}))
        request = admission.resolve_request({'pr': PR, 'instructions': 'x'})
        layers = configuration.entry_options(saved, 'dk-pr-alternatives', request)
        self.assertEqual([(source, where) for source, where, _ in layers], [
            ('repository:repo', 'configuration.scopes[0].entries.dk-pr-alternatives'),
            ('pr:this-pr', 'configuration.scopes[1].entries.dk-pr-alternatives')])
        checked = entries.check_options('dk-pr-alternatives', {'selection': {'enabled': False}}, layers)
        self.assertEqual(checked['selection']['value'], {**entries.ALTERNATIVES_OPTIONS['selection'], 'mode': 'automatic',
                                                         'min_score_gap': 5, 'enabled': False})
        self.assertEqual((checked['selection']['source'], checked['selection']['sources']), ('invocation', {
            'selection.enabled': 'invocation', 'selection.min_score_gap': 'pr:this-pr',
            'selection.mode': 'repository:repo'}))
        self.assertEqual(checked['scoring'], {'value': entries.ALTERNATIVES_OPTIONS['scoring'], 'source': 'default'})
        # An empty nested object sets nothing and keeps the lower layer's source.
        empty = entries.check_options('dk-pr-alternatives', {'selection': {}, 'scoring': {}}, layers[:1])
        self.assertEqual((empty['selection']['value']['mode'], empty['selection']['source']),
                         ('automatic', 'repository:repo'))
        self.assertEqual(empty['scoring'], {'value': entries.ALTERNATIVES_OPTIONS['scoring'], 'source': 'default'})
        # The saved automatic mode governs the run: a close pick stays pending and nothing asks the user.
        close = comparison(ratings={'pr': 80, 'smallest': 85, 'alternative': 20})
        result = self.pr_run('dk-pr-alternatives', branch_script(**{'comparison:retry': close,
                                                                    'selection:retry': auto(None)}),
                             configuration=saved)
        self.assertEqual(result['status'], 'choice_pending', result)
        self.assertEqual(self.fake.calls['input_gates'] + self.fake.calls['open_input_gate'], 0)
        self.assertEqual(result['admission']['entry']['options']['selection']['sources']['selection.mode'],
                         'repository:repo')
        selection = next(task for task in self.fake.tasks if task['mode'] == 'selection')
        self.assertEqual((selection['options']['selection']['mode'], selection['context']['policy']['min_score_gap']),
                         ('automatic', 5))
        # Invocation options override saved ones; the issue entry reads only its own saved options.
        self.state = self.base / 'invoked'
        invoked = self.pr_run('dk-pr-alternatives', branch_script(), configuration=saved,
                              options={'selection': {'mode': 'semi-manual'}})
        self.assertEqual(invoked['status'], 'complete', invoked)
        self.assertEqual(self.fake.calls['input_gates'], 2)
        issue = entries.check_options('dk-issue-alternatives', None, configuration.entry_options(
            saved, 'dk-issue-alternatives', request))
        self.assertEqual((issue['selection']['value']['mode'], issue['selection']['source']), ('manual', 'personal:mine'))
        # The compatibility entry passes them as its delegate's.
        self.state = self.base / 'compatibility'
        transport = self.transport(branch_script(**{'comparison:retry': close, 'selection:retry': auto(None)}))
        compat = alternatives.compatibility(route='branch', request={'pr': PR, 'instructions': 'Review alts.'},
                                            deployment=self.deployment, repository=self.repo, transport=transport,
                                            target=self.target, head=self.head, state=self.state,
                                            requirements=ISSUE, configuration=saved)
        self.assertEqual(compat['status'], 'choice_pending', compat)
        self.assertEqual(self.fake.calls['input_gates'], 0)

    def test_invalid_saved_entry_options_reject_before_the_engine(self):
        cases = [
            (document(scope('repo', 'repository', REPOSITORY,
                            **{'dk-pr-alternatives': {'selection': {'mode': 'sometimes'}}})),
             'configuration.scopes[0].entries.dk-pr-alternatives.selection.mode'),
            (document(scope('repo', 'repository', REPOSITORY, **{'dk-alternatives-review': {}})),
             'configuration.scopes[0].entries.dk-alternatives-review'),
            # Not applicable to this PR, but still validated in full.
            (document(scope('other', 'repository', 'github.com/other/repo',
                            **{'dk-pr-alternatives': {'unknown': 1}})),
             'configuration.scopes[0].entries.dk-pr-alternatives.unknown'),
            (document(scope('a', 'repository', REPOSITORY, **{'dk-pr-alternatives': {'selection': {'mode': 'manual'}}}),
                      scope('b', 'repository', REPOSITORY,
                            **{'dk-pr-alternatives': {'selection': {'mode': 'automatic'}}})),
             'configuration.scopes[1].entries.dk-pr-alternatives.selection.mode'),
            ({'kind': 'configuration'}, 'configuration'),
        ]
        for saved, path in cases:
            result = self.pr_run('dk-pr-alternatives', {}, configuration=saved)
            self.assertEqual((result['status'], result['admission']['error']['path']), ('rejected', path), saved)
            self.calls()
        # A malformed invocation is named as such, not as the configuration.
        result = self.pr_run('dk-pr-alternatives', {}, configuration=document(), invocation={'prompts': 'x'})
        self.assertEqual((result['status'], result['admission']['error']['path']), ('rejected', 'invocation'))
        self.calls()
        self.assertFalse(self.state.exists())
        # Two scopes at one precedence that agree, or set different parts, are consistent.
        agreeing = document(
            scope('a', 'repository', REPOSITORY, **{'dk-pr-alternatives': {'selection': {'mode': 'manual'}}}),
            scope('b', 'repository', REPOSITORY, **{'dk-pr-alternatives': {'selection': {'mode': 'manual',
                                                                                         'min_score_gap': 3}}}))
        request = admission.resolve_request({'pr': PR, 'instructions': 'x'})
        layers = configuration.entry_options(agreeing, 'dk-pr-alternatives', request)
        self.assertEqual(entries.check_options('dk-pr-alternatives', None, layers)['selection']['value']['min_score_gap'], 3)
        with self.assertRaises(admission.AdmissionError):
            entries.check_options('dk-config', None, layers)

    def test_dk_config_saves_and_shows_entry_options(self):
        where = {'repository': REPOSITORY, 'pr': None, 'upstream': None}
        options = entries.check_options('dk-config', None)
        saved = document(scope('repo', 'repository', REPOSITORY))
        revised, changes = configuration.apply_operations(saved, [
            {'op': 'set_entry_option', 'scope': 'repo', 'entry': 'dk-pr-alternatives', 'key': 'selection',
             'value': {'mode': 'manual'}}], 'dk-config', where, options)
        self.assertEqual(revised['scopes'][0]['entries'], {'dk-pr-alternatives': {'selection': {'mode': 'manual'}}})
        self.assertEqual(changes['paths'], ['scopes.repo.entries.dk-pr-alternatives.selection'])
        shown = configuration.view(revised, where)
        self.assertEqual(shown['values']['entries.dk-pr-alternatives.selection'], {
            'value': {**entries.ALTERNATIVES_OPTIONS['selection'], 'mode': 'manual'}, 'source': 'repository:repo',
            'path': 'configuration.scopes[0].entries.dk-pr-alternatives.selection'})
        with self.assertRaises(admission.AdmissionError):  # Remove the contents explicitly first.
            configuration.apply_operations(revised, [{'op': 'remove_scope', 'scope': 'repo'}], 'dk-config', where,
                                           options)
        cleared, _ = configuration.apply_operations(revised, [
            {'op': 'unset_entry_option', 'scope': 'repo', 'entry': 'dk-pr-alternatives', 'key': 'selection'},
            {'op': 'remove_scope', 'scope': 'repo'}], 'dk-config', where, options)
        self.assertEqual(cleared['scopes'], [])
        for operation in ({'op': 'set_entry_option', 'scope': 'repo', 'entry': 'dk-ci-flakes', 'key': 'max_runs',
                           'value': 5},
                          {'op': 'unset_entry_option', 'scope': 'repo', 'entry': 'dk-pr-alternatives', 'key': 'x'}):
            with self.assertRaises(admission.AdmissionError, msg=operation):
                configuration.apply_operations(saved, [operation], 'dk-config', where, options)
        # The saved option is checked against the entry's schema when it is set.
        for key, value, path in (('unknown', 1, 'proposal.operations[0].key'),
                                 ('selection', {'mode': 'sometimes'}, 'proposal.operations[0].value.mode'),
                                 ('scoring', {'profile': 'fast'}, 'proposal.operations[0].value.profile')):
            with self.assertRaises(admission.AdmissionError) as raised:
                configuration.apply_operations(saved, [{'op': 'set_entry_option', 'scope': 'repo', 'entry':
                                                        'dk-pr-alternatives', 'key': key, 'value': value}],
                                               'dk-config', where, options)
            self.assertEqual(raised.exception.path, path)
        # dk-preferences cannot save entry options.
        with self.assertRaises(admission.AdmissionError):
            configuration.apply_operations(saved, [{'op': 'set_entry_option', 'scope': 'repo', 'entry':
                                                    'dk-pr-alternatives', 'key': 'selection', 'value': {}}],
                                           'dk-preferences', where, entries.check_options('dk-preferences', None))


# -- Compatibility entry ----------------------------------------------------------------------------------

class Compatibility(Fixture):
    def compat(self, script, route, request=None, **extra):
        transport = self.transport(script)
        return alternatives.compatibility(route=route, request=request or {'pr': PR, 'instructions': 'Review alts.'},
                                          deployment=self.deployment, repository=self.repo, transport=transport,
                                          target=self.target, head=self.head, state=self.state,
                                          requirements=ISSUE, **extra)

    def test_routes_run_the_delegate_under_its_own_identity_with_the_original_instruction(self):
        result = self.compat(whole_script(), 'whole-problem')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual((result['entry'], result['delegate'], result['scope'], result['instructions']),
                         ('cf-workflows:dk-alternatives-review', 'cf-workflows:dk-issue-alternatives',
                          'whole-problem', 'Review alts.'))
        compat = INVENTORY['dk-alternatives-review']['baseline_resources']
        self.assertIn({'path': result['resources']['entry_role_prompt'],
                       'sha256': result['resources']['entry_role_prompt_sha256']}, compat)
        for task in self.fake.tasks:
            self.assertEqual((task['entry'], task['skill'], task['instructions'], task['role_prompt']),
                             ('cf-workflows:dk-alternatives-review', 'cf-workflows:dk-issue-alternatives',
                              'Review alts.', entries.ENTRIES['dk-issue-alternatives']['role_prompt']))
        branch = self.compat(branch_script(), 'branch')
        self.assertEqual((branch['status'], branch['delegate']), ('complete', 'cf-workflows:dk-pr-alternatives'))

    def test_compatibility_reuses_settled_analysis_and_cannot_authorize_a_fresh_one(self):
        self.pr_run('dk-pr-alternatives', branch_script())
        before = copy.copy(self.engine.sessions)
        reused = self.compat(branch_script(), 'branch')
        self.assertEqual(reused['status'], 'complete')
        self.assertEqual(self.new_sessions(before), Counter())
        # After an interrupted manual refresh it reports the authorization the delegate lacks.
        self.manual(direct('m1'), branch_script(alignment=current()), lose={'comparison:scheduler': 1})
        before = copy.copy(self.engine.sessions)
        blocked = self.compat(branch_script(), 'branch')
        self.assertEqual(blocked['status'], 'authorization_required')
        self.assertEqual(self.new_sessions(before), Counter())

    def test_unsupported_routes_and_legacy_assignments_are_never_concealed(self):
        legacy = {'task_key': 'alternatives:dk-pr-fix:comparison', 'claim': 'claim-1'}
        result = self.compat({}, 'whole-problem', legacy=legacy)
        self.assertEqual((result['status'], result['legacy'], result['dispatches']), ('unsupported', legacy, 0))
        self.calls()
        for route in (None, 'legacy', 'architecture', ['branch'], {'branch': True}):
            rejected = self.compat({}, route)
            self.assertEqual((rejected['status'], rejected['error']['path']), ('rejected', 'route'))
            self.calls()
        no_pr = self.compat({}, 'branch', request={'repository': REPOSITORY, 'instructions': 'Review alts.'})
        self.assertEqual((no_pr['status'], no_pr['error']['path']), ('rejected', 'request.pr'))
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
