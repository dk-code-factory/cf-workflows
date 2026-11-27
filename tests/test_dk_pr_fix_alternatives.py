"""The staged alternatives procedure through the installed dk-pr-fix helpers (#27).

Disposable Git repositories, a fresh state namespace and the fail-closed fake
GitHub transport of the incoming-review fixtures. The installed helpers run
in-process, as the helper containers would run them for alternatives.yml and
alternatives-stage.yml: ``alternatives_next`` names the next native stage or
selection gate, a test hands ``alternatives_accept`` the report that stage's
native session would return, ``alternatives_answer`` records a gate answer, and
``alternatives_status`` completes the occurrence. Every dispatch is counted per
stage. Nothing here starts an engine, provider or host, runs the YAML graph,
opens a real input gate or certifies native-session adherence.
"""
from collections import Counter
import copy
import json
from pathlib import Path
import unittest

from test_dk_pr_review_findings import Fixture, git  # also puts the helpers on sys.path

import action
from prfix import alignment as records, alternatives, app, decisions
from prfix.core import Ledger, read_artifact, state_directory
from prfix.prepare import prepare
from cf_workflows import admission, alternatives as library
from test_dk_pr_review import PR_URL, adapters

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'cf_workflows' / 'workflows' / 'dk-pr-fix'
ISSUE = [{'source': 'issue:27', 'text': 'Values must stay bounded and observable.'}]
CHANGES = 'Route the value through the shared helper.'
RUBRIC = {'id': 'fix', 'scope': 'Value handling', 'work_type': 'small_fix', 'rationale': 'A focused fix.',
          'evidence': ['git:AGENTS.md'], 'weights': {'correctness': 70, 'low_loc': 30},
          'anchors': {name: {'0': 'Fails.', '50': 'Partial.', '100': 'Complete.'} for name in ('correctness', 'low_loc')}}


def assessment(task):
    return {'purpose': 'Bound the value.', 'scope': 'code.py', 'work_type': 'small_fix', 'rationale': 'Small.',
            'mandatory_constraints': ['Keep the module API.'], 'evidence': ['git:AGENTS.md:1'], 'rubrics': [RUBRIC]}


def inventory(task):
    if task['scope'] == 'whole-problem':
        return {'coverage': 'The whole problem of issue 27.', 'evidence': ['issue:27'],
                'decisions': [{'id': 'whole', 'origin': 'whole_problem', 'scope': 'code.py', 'rubric': 'fix',
                               'decision': 'How to bound the value.', 'questionable': True, 'reason': 'Open.',
                               'evidence': ['issue:27']}]}
    return {'coverage': {'introduced': 'Every changed file.', 'inherited': 'The repository rules.'},
            'evidence': ['git:code.py'],
            'decisions': [{'id': 'value', 'origin': 'introduced', 'scope': 'code.py', 'rubric': 'fix',
                           'decision': 'An inline constant.', 'questionable': True, 'reason': 'No bound.',
                           'evidence': ['git:code.py:1']},
                          {'id': 'rules', 'origin': 'inherited', 'scope': 'AGENTS.md', 'rubric': 'fix',
                           'decision': 'Imperative commit subjects.', 'questionable': False, 'reason': 'Fine.',
                           'evidence': ['git:AGENTS.md']}]}


def comparison(task, ratings=None):
    ratings = ratings or {'pr': 50, 'smallest': 90, 'alternative': 60}
    kinds = ('pr', 'smallest', 'alternative')
    solutions = [{'id': kind, 'kind': kind, 'approach': f'The {kind} approach.', 'pros': ['Clear.'], 'cons': [],
                  'handled_edges': ['zero'], 'unhandled_edges': [], 'evidence': ['git:code.py'],
                  'complexity': 20, 'intrusiveness': 10, 'feasible': True, 'feasibility_reason': 'Fits.',
                  'material_doubts': [], 'recommended': kind == 'smallest', 'recommendation_reason': 'Lead.',
                  'production_loc': {'min': 1, 'expected': 2, 'max': 4, 'reason': 'Estimate.'},
                  'ratings': {'correctness': ratings[kind], 'low_loc': ratings[kind]},
                  'rating_evidence': {'correctness': 'Read.', 'low_loc': 'Counted.'}} for kind in kinds]
    return {'rubric': 'fix', 'target_revision': task['context']['pins']['target'],
            'alternative_coverage': 'Materially distinct approaches.', 'evidence': ['git:code.py'],
            'solutions': solutions}


def auto(task, choice='smallest'):
    ids = [item['id'] for item in task['context']['comparison']['solutions']]
    return {'mode': 'auto', 'choice': choice, 'confident': True, 'sensitivity': 'Low.', 'evidence': ['git:code.py'],
            'reason': 'Clear lead.', 'rejection_reasons': {i: 'Lower total.' for i in ids if i != choice}}


def manual(task):
    answer = task['context']['user_choice']
    ids = [item['id'] for item in task['context']['comparison']['solutions']]
    return {'mode': 'manual', 'choice': answer['value'], 'user_source': answer['answer_id'],
            'reason': 'The user chose it.', 'evidence': [answer['answer_id']],
            'rejection_reasons': {i: 'Not chosen.' for i in ids if i != answer['value']}}


def interactions(task):
    return {'compatible': True, 'reason': 'The selections combine.', 'evidence': ['git:code.py']}


def current(value):
    def build(task):
        result = {'alignment': value, 'reason': 'Judged against the pinned head.', 'evidence': ['git:code.py']}
        if value == 'mismatch':
            result['required_changes'] = CHANGES
        return result
    return build


def verified(task):
    return {'disposition': 'confirmed', 'reason': 'Independently checked.', 'evidence': ['git:code.py'],
            'required_changes': CHANGES}


SCRIPT = {'assessment': assessment, 'inventory': inventory, 'comparison': comparison, 'selection': auto,
          'interactions': interactions, 'alignment': current('aligned'), 'verification': verified}


class Installed(Fixture):
    def setUp(self):
        super().setUp()
        state_directory(self.root, create=True)
        self.sessions = Counter()
        self.agents = 0

    def plan(self, tasks, *, entry='', mode=None, settings=None, instructions='Compare the value design.',
             run='alternatives_fixture', permissions=None):
        mode = mode or ('review' if entry else 'fix')  # The entries run read-only in review mode.
        bundled = {name: dict(admission.BUNDLED_ADAPTERS[name]) for name in ('issue-alternatives', 'pr-alternatives')}
        controls = {'tasks': tasks, 'finalizers': [], 'reviewers': ['review'], 'accounts': [],
                    'adapters': {**adapters(), **bundled}, 'settings': settings or {}, 'resources': [], 'turns': 8,
                    'mode': mode, 'entry': entry, 'permissions': permissions or {}}
        request = {'pr': PR_URL, 'instructions': instructions, 'logical_run': run}
        if mode == 'fix':  # A composed dk-pr-fix run maintains the fixture contributor's PR.
            request['maintain_other'] = {'request_id': 'm1', 'instruction': instructions, 'source': 'chat:m1'}
        result = prepare(self.root, request, controls, 'exec-1', self.github, self.definitions)
        return read_artifact(self.root, result['plan'])

    def occurrence(self, plan):
        return alternatives.occurrence(plan)['id']

    def call(self, operation, plan, ledger, **inputs):
        return action.dispatch(operation, self.root, self.github, ledger, plan,
                               {'plan': 'fixture', 'task': self.occurrence(plan), **inputs})

    def deliver(self, plan, ledger, step, output, *, stage=None, status='succeeded'):
        self.agents += 1
        report = None if output is None else {'stage': stage or step['stage'], 'result': json.dumps(output)}
        return self.call('alternatives_accept', plan, ledger, claim=step['claim'], status=status,
                         agent='agent_' + format(self.agents, '032x'), report=report)

    def drive(self, plan, ledger, script=None, answers=(), limit=40):
        """The alternatives.yml loop: dispatch, accept or answer until the procedure settles or waits."""
        script, answers = {**SCRIPT, **(script or {})}, list(answers)
        for _ in range(limit):
            step = self.call('alternatives_next', plan, ledger)
            if step['settled']:
                return step
            if step['ask']:
                if not answers:
                    return step
                value = answers.pop(0)
                self.call('alternatives_answer', plan, ledger, evidence_id=step['evidence_id'], answer=value,
                          answer_id='answer-' + step['evidence_id'], gate_id='gate-' + step['evidence_id'])
                continue
            task = step['task_input']
            name = task['mode'] + (':' + task['decision'] if task['decision'] else '')
            self.sessions[name] += 1
            builder = script.get(name, script[task['mode']])
            accepted = self.deliver(plan, ledger, step, builder(task))
            if accepted['stop']:
                return accepted
        raise AssertionError('the procedure did not settle')

    def status(self, plan, ledger):
        return self.call('alternatives_status', plan, ledger)

    def record(self, scope='branch'):
        run = records.pr_run(PR_URL)
        return records.Records.open(records.ledger_path(state_directory(self.root), run, scope), logical_run=run,
                                    pr_url=PR_URL, scope=scope)


class Entries(Installed):
    def test_descriptor_lists_the_four_entries_over_main_yml(self):
        descriptor = (INSTALL / 'plugin.yaml').read_text()
        for name, workflow in (('dk-issue-alternatives', 'issue-alternatives'), ('dk-pr-alternatives',
                               'pr-alternatives'), ('dk-alternatives-review', 'alternatives-review'),
                               ('dk-pr-review-alternatives', 'pr-review-alternatives')):
            self.assertIn(f'  - name: {name}\n    path: skills/{name}/SKILL.md\n    workflow: workflows/{workflow}.yml\n',
                          descriptor)
            body = (INSTALL / 'workflows' / (workflow + '.yml')).read_text()
            self.assertIn('uses: workflows/main.yml', body)
            self.assertIn('entry: ' + name, body)
            self.assertIn('permissions:\n        type: object\n        default: {}', body)
            skill = (INSTALL / 'skills' / name / 'SKILL.md').read_text()
            self.assertTrue(skill.startswith('---\nname: ' + name + '\n'), name)
        stage = (INSTALL / 'workflows' / 'stage.yml').read_text()
        self.assertIn("uses: workflows/alternatives.yml", stage)
        self.assertIn("if: ${{ inputs.stage.kind == 'alternatives' }}", stage)

    def test_direct_branch_entry_runs_each_stage_as_its_own_checked_native_session(self):
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives')
        self.assertEqual([stage['kind'] for stage in plan['stages']], ['alternatives'])
        ledger = self.ledger(plan)
        step = self.call('alternatives_next', plan, ledger)
        task = step['task_input']
        self.assertEqual((step['definition'], task['mode'], task['skills'], task['tools'], task['writes'],
                          task['publication'], task['entry'], task['skill'], task['adapter']),
                         ('leaf', 'assessment', [], 'read-only', False, False, 'cf-workflows:dk-pr-alternatives',
                          'cf-workflows:dk-pr-alternatives', 'pr-alternatives'))
        self.assertEqual(task['stage_prompt'], library.STAGE_PROMPTS['assessment'])
        self.assertEqual(task['role_prompt'], alternatives.ROLE_PROMPTS['dk-pr-alternatives'])
        self.assertIn('git:code.py', task['readable'])
        self.assertEqual(task['context']['pins']['changed'], ['code.py', 'notes file.md'])
        # The generic task path refuses the occurrence: it never runs as one generic session.
        with self.assertRaisesRegex(ValueError, 'staged procedure'):
            app.prepare_claim(self.root, self.github, ledger, plan, {'task': self.occurrence(plan)})

        settled = self.drive(plan, ledger)
        self.assertEqual((settled['status'], settled['outcome']['selections']['value']['choice']),
                         ('complete', 'smallest'))
        self.assertEqual(self.sessions, Counter({'assessment': 1, 'inventory': 1, 'comparison:value': 1,
                                                 'selection:value': 1, 'interactions': 1}))
        done = self.status(plan, ledger)
        self.assertTrue(done['complete'])
        covered = app.coverage(self.root, self.github, ledger, plan, {})
        self.assertEqual(covered['missing'], [])
        # A later run of the same PR reuses the settled analysis: no session, no gate.
        again = self.plan(['pr-alternatives'], entry='dk-pr-alternatives', run='another_run',
                          instructions='Compare it again.')
        before = sum(self.sessions.values())
        self.assertEqual(self.drive(again, self.ledger(again))['status'], 'complete')
        self.assertEqual(sum(self.sessions.values()), before)

    def test_whole_problem_entry_keeps_its_own_record_and_needs_issue_requirements(self):
        with self.assertRaisesRegex(ValueError, 'issue requirements'):
            self.plan(['issue-alternatives'], entry='dk-issue-alternatives')
        with self.assertRaisesRegex(ValueError, 'runs the issue-alternatives adapter'):
            self.plan(['pr-alternatives'], entry='dk-issue-alternatives',
                      settings={'alternatives': {'requirements': ISSUE}})
        plan = self.plan(['issue-alternatives'], entry='dk-issue-alternatives',
                         settings={'alternatives': {'requirements': ISSUE}})
        ledger = self.ledger(plan)
        settled = self.drive(plan, ledger)
        self.assertEqual(settled['status'], 'complete', settled)
        self.assertEqual(self.sessions['comparison:whole'], 1)
        with self.record('whole-problem') as record:
            self.assertEqual(record.latest('selection:whole')['result']['choice'], 'smallest')
        self.assertFalse(records.ledger_path(state_directory(self.root), records.pr_run(PR_URL)).exists())

    def test_entries_reject_authority_custom_prompts_and_other_tasks_before_any_session(self):
        cases = (
            ({'tasks': ['pr-alternatives', 'read-feedback']}, 'no other task'),
            ({'tasks': ['pr-alternatives'], 'permissions': {'post_findings': True}}, 'no other task'),
            ({'tasks': ['pr-alternatives'], 'mode': 'fix'}, 'review mode only'),
            ({'tasks': ['pr-alternatives'], 'settings': {'alternatives': {'options': {'evaluation_prompt': 'Be strict.'}}}},
             'semantic validator'),
            ({'tasks': ['pr-alternatives'], 'settings': {'alternatives': {'options': {'selection': {'mode': 'often'}}}}},
             'selection.mode'),
            ({'tasks': ['pr-alternatives'], 'settings': {'alternatives': {'route': 'branch'}}}, 'compatibility entry'),
        )
        for kwargs, message in cases:
            with self.assertRaisesRegex(ValueError, message, msg=kwargs):
                self.plan(kwargs.pop('tasks'), entry='dk-pr-alternatives', **kwargs)
        with self.assertRaisesRegex(ValueError, 'one alternatives occurrence'):
            self.plan(['read-feedback'], settings={'alternatives': {}})
        self.assertEqual(self.sessions, Counter())


class Selection(Installed):
    def test_semi_manual_question_is_asked_once_and_its_answer_settles_the_choice(self):
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives')
        ledger = self.ledger(plan)
        # The automatic pick lacks the score gap: it records no choice and asks the user instead.
        close = {'comparison': lambda task: comparison(task, {'pr': 85, 'smallest': 90, 'alternative': 60})}
        asked = self.drive(plan, ledger, close)
        self.assertTrue(asked['ask'])
        self.assertEqual(asked['payload']['decision'], 'value')
        self.assertEqual({item['id'] for item in asked['payload']['options']}, {'pr', 'smallest', 'alternative'})
        self.assertEqual(self.drive(plan, ledger, close)['evidence_id'], asked['evidence_id'],
                         'an unanswered question is the same durable gate')
        self.assertEqual(self.sessions['selection:value'], 1)
        # An answer that names no feasible option is never dispatched; the next round asks again.
        again = self.drive(plan, ledger, close, answers=['retain everything'])
        self.assertTrue(again['ask'])
        self.assertNotEqual(again['evidence_id'], asked['evidence_id'])
        self.assertEqual(again['payload']['rejected_answers'], ['answer-' + asked['evidence_id']])
        settled = self.drive(plan, ledger, {**close, 'selection': manual}, answers=['pr'])
        self.assertEqual((settled['status'], settled['outcome']['selections']['value']),
                         ('complete', {'choice': 'pr', 'mode': 'manual', 'scores': {'pr': 85.0, 'smallest': 90.0,
                                                                                    'alternative': 60.0}}))
        self.assertEqual(self.sessions['selection:value'], 2)
        # Replaying the recorded delivery is a no-op; a question nobody asks is refused.
        replay = self.call('alternatives_answer', plan, ledger, evidence_id=again['evidence_id'], answer='pr',
                           answer_id='answer-' + again['evidence_id'], gate_id='gate-' + again['evidence_id'])
        self.assertTrue(replay['recorded'])
        with self.assertRaisesRegex(ValueError, 'not asking'):
            self.call('alternatives_answer', plan, ledger, evidence_id='altchoice_unknown', answer='pr',
                      answer_id='answer-x', gate_id='gate-x')

    def test_automatic_mode_never_asks(self):
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives',
                         settings={'alternatives': {'options': {'selection': {'mode': 'automatic'}}}})
        ledger = self.ledger(plan)
        close = {'comparison': lambda task: comparison(task, {'pr': 85, 'smallest': 90, 'alternative': 60})}
        pending = self.drive(plan, ledger, close)
        self.assertEqual((pending['status'], pending['outcome']['decisions']), ('choice_pending', ['value']))
        with self.assertRaisesRegex(ValueError, 'did not settle: choice_pending'):
            self.status(plan, ledger)
        self.drive(plan, ledger, close)
        self.assertEqual(self.sessions['selection:value'], 1, 'a pending choice is never settled by repetition')


class Boundary(Installed):
    def settle(self):
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives')
        ledger = self.ledger(plan)
        self.assertEqual(self.drive(plan, ledger)['status'], 'complete')
        return plan, ledger

    def test_rejected_output_closes_its_attempt_and_the_next_run_starts_a_new_one(self):
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives')
        ledger = self.ledger(plan)
        first = self.call('alternatives_next', plan, ledger)
        bad = assessment(first['task_input'])
        bad['evidence'] = ['git:missing.py']
        stopped = self.deliver(plan, ledger, first, bad)
        self.assertEqual((stopped['accepted'], stopped['stop'], stopped['error']['path']),
                         (False, True, 'assessment.evidence[0]'))
        second = self.call('alternatives_next', plan, ledger)
        self.assertNotEqual(second['claim'], first['claim'])
        # A report naming another stage, or a session without a result, is rejected the same way.
        self.assertTrue(self.deliver(plan, ledger, second, assessment(second['task_input']), stage='inventory')['stop'])
        third = self.call('alternatives_next', plan, ledger)
        self.assertTrue(self.deliver(plan, ledger, third, None, status='failed')['stop'])
        with self.record() as record:
            self.assertEqual([item['state'] for item in record.history('assessment')], ['superseded'] * 3)
            self.assertEqual(record.usage('architecture')['used'], 3)
        with self.assertRaisesRegex(ValueError, 'not waiting for'):
            self.deliver(plan, ledger, first, assessment(first['task_input']))

    def test_interrupted_stage_resumes_its_open_claim(self):
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives')
        ledger = self.ledger(plan)
        first = self.call('alternatives_next', plan, ledger)
        # The native session's result was lost before acceptance: the same claim is dispatched again.
        resumed = self.call('alternatives_next', plan, ledger)
        self.assertEqual(resumed['claim'], first['claim'])
        self.assertTrue(self.deliver(plan, ledger, resumed, assessment(resumed['task_input']))['accepted'])
        with self.record() as record:
            # One assessment attempt; the next stage's claim is already open for the next step.
            self.assertEqual([item['state'] for item in record.history('assessment')], ['complete'])
            self.assertEqual(record.open_claim('inventory') is not None, True)

    def test_manual_entry_refreshes_once_per_direct_request_and_stops_at_the_gate(self):
        self.settle()
        before = Counter(self.sessions)
        instruction = 'Compare the value design again.'
        manual_plan = self.plan(['pr-alternatives'], entry='dk-pr-review-alternatives', instructions=instruction,
                                run='manual_run', settings={'alternatives': {'requirements': ISSUE}})
        direct = manual_plan['tasks'][0]['alternatives']['direct']
        self.assertEqual((direct['instruction'], direct['skill'], direct['origin']),
                         (instruction, 'cf-workflows:dk-pr-review-alternatives', 'user'))
        ledger = self.ledger(manual_plan)
        settled = self.drive(manual_plan, ledger, {'alignment': current('mismatch')})
        self.assertEqual(settled['status'], 'mismatch_verified')
        self.assertEqual(self.sessions - before, Counter({'inventory': 1, 'comparison:value': 1, 'selection:value': 1,
                                                          'interactions': 1, 'alignment': 1, 'verification': 1}),
                         'the shared assessment is reused; everything else runs once under the request')
        done = self.status(manual_plan, ledger)
        self.assertEqual((done['status'], done['outcome']['gate']['clean_review'], done['outcome']['gate']['ready']),
                         ('mismatch_verified', False, False))
        # Replaying the same staged request repeats nothing.
        replay = self.plan(['pr-alternatives'], entry='dk-pr-review-alternatives', instructions=instruction,
                           run='manual_run', settings={'alternatives': {'requirements': ISSUE}})
        count = sum(self.sessions.values())
        self.assertEqual(self.drive(replay, self.ledger(replay))['status'], 'mismatch_verified')
        self.assertEqual(sum(self.sessions.values()), count)

    def test_new_requirements_refresh_only_current_alignment_in_composed_review(self):
        self.settle()
        before = Counter(self.sessions)
        tasks = ['pr-alternatives', {'stage': 'reviews'}]
        review = self.plan(tasks, mode='review', settings={'alternatives': {'requirements': ISSUE}}, run='r1')
        self.assertEqual(review['tasks'][0]['alternatives']['through'], 'verification')
        ledger = self.ledger(review)
        self.assertEqual(self.drive(review, ledger)['status'], 'released')
        self.assertEqual(self.sessions - before, Counter({'alignment': 1}))
        # New requirements: current alignment refreshes; no comparison, selection or authorization.
        changed = self.plan(tasks, mode='review', run='r1', settings={'alternatives': {'requirements': ISSUE + [
            {'source': 'comment:9', 'text': 'Also log the bound.'}]}})
        self.assertEqual(self.drive(changed, self.ledger(changed))['status'], 'released')
        self.assertEqual(self.sessions - before, Counter({'alignment': 2}))
        gate = action.dispatch('alignment_gate', self.root, self.github, ledger, changed, {})
        self.assertEqual((gate['outcome'], gate['gated']), ('released', False))

    def test_an_automatic_route_never_repeats_settled_analysis_under_changed_requirements(self):
        self.settle()
        before = Counter(self.sessions)
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives', run='other',
                         settings={'alternatives': {'requirements': ISSUE}})
        self.assertEqual(self.drive(plan, self.ledger(plan))['status'], 'complete')
        self.assertEqual(self.sessions, before)


class Compatibility(Installed):
    def test_routes_run_the_delegate_under_the_entry_identity_and_legacy_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'explicit route'):
            self.plan(['pr-alternatives'], entry='dk-alternatives-review')
        with self.assertRaisesRegex(ValueError, 'runs the issue-alternatives adapter'):
            self.plan(['pr-alternatives'], entry='dk-alternatives-review',
                      settings={'alternatives': {'route': 'whole-problem', 'requirements': ISSUE}})
        with self.assertRaisesRegex(ValueError, 'not imported'):
            self.plan(['pr-alternatives'], entry='dk-alternatives-review',
                      settings={'alternatives': {'route': 'branch', 'legacy': {'id': 'old-1'}}})
        plan = self.plan(['pr-alternatives'], entry='dk-alternatives-review',
                         settings={'alternatives': {'route': 'branch'}})
        ledger = self.ledger(plan)
        step = self.call('alternatives_next', plan, ledger)
        self.assertEqual((step['task_input']['entry'], step['task_input']['skill']),
                         ('cf-workflows:dk-alternatives-review', 'cf-workflows:dk-pr-alternatives'))
        self.assertEqual(plan['instructions'], step['task_input']['instructions'])
        self.assertEqual(self.drive(plan, ledger)['status'], 'complete')
        # The delegate's own entry reuses what the compatibility route settled.
        before = sum(self.sessions.values())
        delegate = self.plan(['pr-alternatives'], entry='dk-pr-alternatives', run='delegate')
        self.assertEqual(self.drive(delegate, self.ledger(delegate))['status'], 'complete')
        self.assertEqual(sum(self.sessions.values()), before)


class Composition(Installed):
    def test_fix_route_hands_settled_choices_to_its_fix_occurrences(self):
        tasks = ['issue-alternatives', 'address-feedback']
        settings = {'alternatives': {'requirements': ISSUE}}
        plan = self.plan(tasks, settings=settings)
        address = next(task for task in plan['tasks'] if task['skill'] == 'address-feedback')
        self.assertEqual(address['alternatives_handoff'], self.occurrence(plan))
        self.assertEqual(plan['tasks'][0]['alternatives']['through'], 'alignment')
        ledger = self.ledger(plan)
        with self.assertRaisesRegex(ValueError, 'settled alternatives analysis first'):
            app.prepare_claim(self.root, self.github, ledger, plan, {'task': address['id']})
        settled = self.drive(plan, ledger, {'alignment': current('mismatch')})
        self.assertEqual(settled['outcome']['handoff'], {'choices': {'whole': 'smallest'}, 'alignment': 'mismatch',
                                                         'required_changes': CHANGES, 'tests_required': True})
        self.assertEqual(self.sessions['verification'], 0, 'the fix route hands off; it does not verify')
        self.status(plan, ledger)
        claim = app.prepare_claim(self.root, self.github, ledger, plan, {'task': address['id']})
        handed = claim['task_input']['alternatives']
        self.assertEqual((handed['handoff']['required_changes'], handed['selections']['whole']['choice']),
                         (CHANGES, 'smallest'))

    def test_coordinators_compose_only_their_scope(self):
        with self.assertRaisesRegex(ValueError, 'composes only issue-alternatives'):
            self.plan(['pr-alternatives', 'address-feedback'])
        with self.assertRaisesRegex(ValueError, 'composes only pr-alternatives'):
            self.plan(['issue-alternatives', {'stage': 'reviews'}], mode='review',
                      settings={'alternatives': {'requirements': ISSUE}})
        with self.assertRaisesRegex(ValueError, 'alone in its stage'):
            self.plan([['pr-alternatives', 'read-feedback'], {'stage': 'reviews'}], mode='review')


class Sources(Installed):
    def test_advised_and_selected_alternatives_are_advisory_decision_sources(self):
        plan = self.plan(['pr-alternatives'], entry='dk-pr-alternatives')
        ledger = self.ledger(plan)
        self.assertEqual(decisions._alternatives(self.root, plan['pr']), [])
        self.drive(plan, ledger)
        [source] = decisions._alternatives(self.root, plan['pr'])
        self.assertEqual((source['id'], source['kind'], source['authority'], source['public']),
                         ('alternatives:branch:value', 'alternatives', 'advisory', False))
        body = json.loads(source['text'])
        self.assertEqual((body['selected'], body['advised'], body['approach']),
                         ('smallest', ['smallest'], 'The smallest approach.'))
        # They follow the discussion and the request, and come before recorded answers.
        sources, _ = decisions.collect(plan, {'metadata': {}, 'comments': []}, heading='Decisions',
                                       answers=[{'answer_id': 'a1', 'value': 'Keep it.'}],
                                       alternatives=[source])
        self.assertEqual([item['kind'] for item in sources], ['instruction', 'alternatives', 'answer'])
        known = {item['id']: item for item in sources}
        # A settled decision cannot rest on an alternative alone: it is advisory, never governing.
        self.assertNotIn(known['alternatives:branch:value']['authority'], decisions.GOVERNING)
        # The library collects the same advisory source.
        self.assertEqual(library.decision_sources(state_directory(self.root), PR_URL), [source])

    def test_library_and_installed_helpers_share_one_procedure(self):
        helper = str(INSTALL / 'workflows' / 'helpers' / 'prfix' / 'alternatives.py')
        for name in ('check_assessment', 'check_inventory', 'check_comparison', 'check_selection', 'scores'):
            self.assertEqual(getattr(library, name).__code__.co_filename, helper, name)
        self.assertEqual(library._H.Walk.run.__code__.co_filename, helper)
        self.assertEqual(library.STAGE_PROMPTS, alternatives.STAGE_PROMPTS)
        self.assertEqual(alternatives.OPTIONS, __import__('cf_workflows.entries').entries.ALTERNATIVES_OPTIONS)
        for name, prompt in alternatives.ROLE_PROMPTS.items():
            self.assertEqual(prompt, __import__('cf_workflows.entries').entries.ENTRIES[name]['role_prompt'])


if __name__ == '__main__':
    unittest.main()
