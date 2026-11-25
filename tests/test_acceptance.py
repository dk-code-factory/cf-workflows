"""Acceptance reuse and configuration-write recovery: a disposable SQLite ledger, a real
file store with injected faults, scripted validator sessions and a durable fake gate store."""
from collections import Counter
import contextlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cf_workflows import acceptance, admission, semantic, writes  # noqa: E402
from test_admission import CAPABILITIES  # noqa: E402
from test_semantic import Engine, Semantic, Transport, verdict  # noqa: E402

SAVED = 'configuration.scopes[0].prompts[0]'
LOCAL = 'invocation.prompts[0]'
TASK = 'invocation.options.tasks[0].instructions'


class Ledgered(Semantic):
    def setUp(self):
        super().setUp()
        self.state = self.base / 'state' / 'configuration.sqlite3'
        self.ledger = self.open_ledger()
        self.saved = self.configuration(
            [self.scope('repo', 'repository', 'github.com/owner/repo',
                        prompts=[{'id': 'focus', 'slot': 'additional', 'resource': 'configuration:prompts/review.md'}])],
            [self.resource('prompts/review.md'), self.resource('prompts/policy.md')])
        self.invocation = {'options': {'tasks': [{'task': 'address-feedback', 'instructions': 'Only the flaky test.'}]},
                           'prompts': [{'id': 'local', 'slot': 'additional', 'text': 'Keep the diff small.'}]}

    def open_ledger(self):
        """A fresh process: a new connection to the same durable state."""
        ledger = acceptance.Ledger(self.state)
        self.addCleanup(ledger.close)
        return ledger

    def run_ledgered(self, *sessions, configuration='saved', invocation='default', capabilities=CAPABILITIES):
        self.transport = Transport(self.engine, sessions, capabilities)
        configuration = self.saved if configuration == 'saved' else configuration
        invocation = self.invocation if invocation == 'default' else invocation
        result = semantic.admit(request=copy.deepcopy(self.request), configuration=copy.deepcopy(configuration),
                                invocation=copy.deepcopy(invocation), deployment=self.deployment, roles=self.roles,
                                roots=self.roots, transport=self.transport, ledger=self.ledger)
        self.assertLessEqual(set(self.transport.calls),
                             {'capabilities', 'input_gates', 'validator_session', 'open_input_gate'})
        self.assertLessEqual(self.transport.calls['validator_session'], 1)
        return result

    def accept_all(self):
        result = self.run_ledgered(verdict('accepted'))
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'))
        self.calls(sessions=1)
        return result


class Reuse(Ledgered):
    def test_unchanged_fresh_session_reuses_original_evidence(self):
        first = self.accept_all()
        [task] = self.transport.assignments
        self.assertEqual(task['subjects'], sorted([SAVED, LOCAL, TASK]))
        self.assertEqual(task['accepted'], {})
        # A fresh process with no engine gates: no gate read, no session, the original native reference.
        self.ledger, self.engine = self.open_ledger(), Engine()
        second = self.run_ledgered()
        self.assertEqual((second['preflight'], second['semantic']), ('admitted', 'accepted'))
        self.calls(gate_reads=0)
        self.assertEqual(second['admission']['evidence_refs'], first['admission']['evidence_refs'])
        self.assertIn('native:result-' + task['claim'], second['admission']['evidence_refs'])
        self.assertEqual(second['reuse']['subjects'], [])
        self.assertIsNone(second['assessment']['validator'])
        self.assertEqual(second['snapshot'], first['snapshot'])

    def test_unrelated_changes_keep_every_acceptance(self):
        self.accept_all()
        other = self.scope('other', 'repository', 'github.com/owner/elsewhere',
                           prompts=[{'id': 'x', 'slot': 'additional', 'text': 'Unrelated.'}])
        self.write(self.config_root, 'notes/unused.md', 'Not referenced by any prompt.\n')
        moved = copy.deepcopy(self.saved)
        moved['scopes'].insert(0, self.scope('mine', 'personal', 'me',
                                             prompts=[{'id': 'local', 'slot': 'additional', 'text': 'Keep the diff small.'}]))
        for name, change in [
            ('limits and turns', lambda: self.invocation['options'].update(turns=12, limits={'review_cycles': 2})),
            ('non-matching scope', lambda: self.saved['scopes'].append(other)),
            ('unreferenced resource', lambda: self.saved['resources'].append(self.resource('notes/unused.md'))),
            ('helper image', lambda: self.deployment['execution'].update(image='ghcr.io/fixture/helper@sha256:' + 'b' * 64)),
            ('configuration revision and admission record',
             lambda: self.saved.update(revision='rev-9', admission={**self.saved['admission'], 'semantic': 'accepted'})),
            ('unselected baseline adapter text', lambda: self.enterContext(patch.dict(
                admission.BASELINE_ADAPTERS, {'describe-pr': {**admission.BASELINE_ADAPTERS['describe-pr'],
                                                              'assignment': 'Write a longer description.'}}))),
            ('unused role definition', lambda: self.roles['reviewer'].update(sha256=self.write(
                self.roles_root, 'reviewer.yaml', 'name: "reviewer"\ndescription: "Fixture"\ninstructions: "Look."\n'))),
            ('selected tasks reordered', lambda: self.invocation['options'].update(
                finalizers=['sync-guidance', 'inspect-commits', {'stage': 'ci'}])),
            ('prompt moved to another scope', lambda: (setattr(self, 'saved', moved),
                                                       self.invocation.pop('prompts'))),
        ]:
            with self.subTest(name):
                change()
                result = self.run_ledgered()
                self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'), result.get('error'))
                self.calls(gate_reads=0)

    def test_each_relevant_input_invalidates_only_affected_units(self):
        prompt, local, task = 'prompt:repository:repo:focus', 'prompt:invocation:invocation:local', 'instruction:' + TASK
        everything = sorted([prompt, local, task])
        offered = copy.deepcopy(CAPABILITIES)
        offered['providers']['fixture-provider'].append('merge_guard')

        def resource(stack):
            self.write(self.config_root, 'prompts/policy.md', 'Never weaken or delete tests.\n')
            self.saved['resources'][1] = self.resource('prompts/policy.md')

        def role(stack):
            body = 'name: "leaf"\ndescription: "Fixture role"\ninstructions: "Follow it exactly."\n'
            self.roles['leaf']['sha256'] = self.write(self.roles_root, 'leaf.yaml', body)

        baseline = copy.deepcopy(admission.BASELINE_ADAPTERS)
        baseline['address-feedback']['assignment'] += ' Keep commits focused.'
        # Every selected adapter runs the leaf role: untargeted prompts follow the selected plan,
        # the instruction follows its own adapter.
        selected = {prompt: ['plan'], local: ['plan'], task: ['adapter']}
        for name, change, affected, reason, capabilities in [
            ('prompt text', lambda stack: self.invocation['prompts'][0].update(text='Keep the diff tiny.'), [local], ['content'], None),
            ('linked resource', resource, [prompt], ['resources'], None),
            ('task instruction', lambda stack: self.invocation['options']['tasks'][0].update(instructions='Only tests.'),
             [task], ['content'], None),
            ('baseline adapter text', lambda stack: stack.enter_context(patch.object(admission, 'BASELINE_ADAPTERS',
                                                                                     baseline)),
             everything, selected, None),
            ('role definition', role, everything, selected, None),
            ('guidance', lambda stack: self.write(self.repo, 'docs/guide.md', 'Run every test.\n'), everything, ['guidance'], None),
            ('original request', lambda stack: self.request.update(instructions='Fix both flaky tests.'), everything,
             ['request'], None),
            ('prohibition', lambda stack: self.invocation['options'].update(prohibitions=['merge']), everything,
             ['policy'], None),
            ('actual capabilities', lambda stack: None, everything, ['capabilities'], offered),
            ('validator version', lambda stack: stack.enter_context(patch.object(acceptance, 'SCHEME', 2)), everything,
             ['validator'], None),
        ]:
            with self.subTest(name), contextlib.ExitStack() as stack:
                self.setUp()
                self.accept_all()
                change(stack)
                result = self.run_ledgered(verdict('accepted'), capabilities=capabilities or CAPABILITIES)
                self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'))
                self.calls(sessions=1)
                self.assertEqual(sorted(result['reuse']['subjects']), sorted(affected))
                expected = reason if isinstance(reason, dict) else {identity: reason for identity in affected}
                self.assertEqual(result['reuse']['invalidated'], expected)
                [assigned] = self.transport.assignments
                kept = sorted(set(everything) - set(affected))
                self.assertEqual(sorted(result['reuse']['reused']), kept)
                self.assertEqual(len(assigned['accepted']), len(kept))
                # The reused units keep their first evidence; the new session adds exactly one reference.
                natives = [ref for ref in result['admission']['evidence_refs'] if ref.startswith('native:')]
                self.assertEqual(len(natives), 2 if kept else 1)
                # Unchanged again: the second acceptance is reused in turn.
                self.run_ledgered(capabilities=capabilities or CAPABILITIES)
                self.calls(gate_reads=0)

    def test_new_custom_text_is_assessed_before_dispatch_with_the_rest_as_context(self):
        first = self.accept_all()
        self.invocation['prompts'].append({'id': 'more', 'slot': 'additional', 'text': 'Prefer table-driven tests.'})
        conflict = [{'id': 'c1', 'paths': ['invocation.prompts[1]', LOCAL], 'detail': 'Tables grow the diff.'}]
        result = self.run_ledgered(verdict('rejected', conflicts=conflict))
        self.assertEqual((result['preflight'], result['semantic']), ('rejected', 'rejected'))
        [task] = self.transport.assignments
        self.assertEqual(task['subjects'], ['invocation.prompts[1]'])
        native = [ref for ref in first['admission']['evidence_refs'] if ref.startswith('native:')][0]
        self.assertEqual(task['accepted'], {SAVED: native, LOCAL: native, TASK: native})
        # A rejection is not retained as acceptance: the same proposal is assessed again.
        self.run_ledgered(verdict('accepted'))
        self.calls(sessions=1)

    def test_separately_accepted_inputs_are_assessed_together(self):
        one_line = {'id': 'a', 'slot': 'additional', 'text': 'Keep the diff to one line.'}
        rewrite = {'id': 'b', 'slot': 'additional', 'text': 'Rewrite the entire module from scratch.'}
        for prompts in ([one_line], [rewrite]):
            self.run_ledgered(verdict('accepted'), configuration=None, invocation={'prompts': prompts})
            self.calls(sessions=1)
        conflict = [{'id': 'c1', 'paths': ['invocation.prompts[0]', 'invocation.prompts[1]'], 'detail': 'Contradict.'}]
        result = self.run_ledgered(verdict('needs_clarification', conflicts=conflict), configuration=None,
                                   invocation={'prompts': [one_line, rewrite]})
        self.calls(sessions=1, gates_opened=1)
        [task] = self.transport.assignments
        # The earliest acceptance is context; the other input is judged against it.
        self.assertEqual(task['subjects'], ['invocation.prompts[1]'])
        self.assertEqual(list(task['accepted']), ['invocation.prompts[0]'])
        self.assertEqual(result['reuse']['invalidated'], {'prompt:invocation:invocation:b': ['combination']})
        # The user removes the one-line prompt; the rewrite alone is reused, the pair is never admitted unseen.
        self.engine.answer('gate-1', 'answer-1', {'action': 'remove', 'paths': ['invocation.prompts[0]']})
        self.assertEqual(self.run_ledgered(verdict('accepted'), configuration=None,
                                           invocation={'prompts': [one_line, rewrite]})['semantic'], 'accepted')
        self.calls(sessions=0, capabilities=2)
        self.engine = Engine()
        self.run_ledgered(verdict('rejected', conflicts=conflict), configuration=None,
                          invocation={'prompts': [one_line, rewrite]})
        self.calls(sessions=1)
        # Inputs judged together are reused together, and so is any subset of them.
        both = self.run_ledgered(verdict('accepted'), configuration=None,
                                 invocation={'prompts': [one_line, {**rewrite, 'text': 'Rename one variable.'}]})
        self.calls(sessions=1)
        self.assertEqual(both['semantic'], 'accepted')
        for prompts in ([one_line, {**rewrite, 'text': 'Rename one variable.'}], [one_line]):
            self.run_ledgered(configuration=None, invocation={'prompts': prompts})
            self.calls(gate_reads=0)

    def test_untargeted_text_is_bound_to_the_selected_plan(self):
        invocation = {'prompts': [{'id': 'free', 'slot': 'additional', 'text': 'Feel free to rewrite tests.'}],
                      'options': {'tasks': ['review'], 'finalizers': []}}
        self.run_ledgered(verdict('accepted'), configuration=None, invocation=invocation)
        invocation['options']['tasks'] = ['address-feedback']
        result = self.run_ledgered(verdict('accepted'), configuration=None, invocation=invocation)
        self.calls(sessions=1)
        self.assertEqual(result['reuse']['invalidated'], {'prompt:invocation:invocation:free': ['plan']})

    def test_baseline_control_text_needs_no_assessment(self):
        same = {'review': copy.deepcopy(admission.BASELINE_ADAPTERS['review'])}
        result = self.run_ledgered(configuration=None, invocation={'options': {'adapters': same}})
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', None))
        self.calls(gate_reads=0)
        # Units are custom inputs only; baseline adapters appear only as bindings of units that target them.
        kinds = [unit['kind'] for unit in acceptance.units(self.structural(configuration=self.saved))]
        self.assertEqual(sorted(kinds), ['instruction', 'prompt', 'prompt'])

    def test_free_text_policy_is_one_unit_bound_into_every_other(self):
        self.invocation['options']['requirements'] = ['Every fix needs a regression test.']
        result = self.accept_all()
        self.assertIn('policy', [unit['identity'] for unit in acceptance.units(result)])
        self.assertIn('invocation.options.requirements[0]', self.transport.assignments[0]['subjects'])
        self.invocation['options']['requirements'] = ['Every fix needs two regression tests.']
        self.assertEqual(len(self.run_ledgered(verdict('accepted'))['reuse']['subjects']), 4)


class Clarified(Ledgered):
    def test_clarified_acceptance_is_reused_only_with_its_answers(self):
        question = [{'id': 'u1', 'paths': [LOCAL], 'question': 'Does small forbid new tests?'}]
        self.run_ledgered(verdict('needs_clarification', uncertainties=question))
        self.engine.answer('gate-1', 'answer-1', {'action': 'supplement', 'text': 'New tests are fine.'})
        result = self.run_ledgered(verdict('accepted'))
        self.assertEqual(result['semantic'], 'accepted')
        [task] = self.transport.assignments
        # Nothing was accepted before the dialogue, so every unit is a subject, coupled to the answer.
        self.assertEqual(task['subjects'], sorted([SAVED, LOCAL, TASK, 'invocation.prompts[1]']))
        # The same request replays its answer and reuses that acceptance with no session.
        self.ledger = self.open_ledger()
        replay = self.run_ledgered()
        self.assertEqual(replay['semantic'], 'accepted')
        self.calls(capabilities=2)
        self.assertEqual(replay['admission']['evidence_refs'][-1], result['admission']['evidence_refs'][-1])
        # Without the answer, the prompt it qualified is not accepted on its own.
        self.engine = Engine()
        alone = self.run_ledgered(verdict('accepted'))
        self.calls(sessions=1)
        self.assertEqual(alone['reuse']['invalidated']['prompt:invocation:invocation:local'], ['coupled'])

    def test_lost_gate_response_neither_loses_the_question_nor_repeats_the_session(self):
        question = [{'id': 'u1', 'paths': [LOCAL], 'question': 'How small?'}]
        opened = self.engine.open

        def created_then_lost(request):
            opened(request)
            raise TimeoutError()
        self.engine.open = created_then_lost
        result = self.run_ledgered(verdict('needs_clarification', uncertainties=question))
        self.assertEqual(result['preflight'], 'blocked')
        self.engine.open = opened
        result = self.run_ledgered()
        self.assertEqual((result['preflight'], result['gate']['gate_id']), ('waiting', 'gate-1'))
        self.calls()
        # A gate that was never created: the retained question is asked once, without a session.
        self.engine = Engine()
        self.engine.open = lambda request: (_ for _ in ()).throw(TimeoutError())
        self.invocation['prompts'][0]['text'] = 'Keep it small.'
        self.assertEqual(self.run_ledgered(verdict('needs_clarification', uncertainties=question))['preflight'],
                         'blocked')
        self.engine = Engine()
        result = self.run_ledgered()
        self.assertEqual((result['preflight'], result['assessment']['uncertainties']), ('waiting', question))
        self.calls(gates_opened=1)
        self.engine.answer('gate-1', 'answer-1', {'action': 'replace', 'path': LOCAL + '.text', 'text': 'Under 50 lines.'})
        self.assertEqual(self.run_ledgered(verdict('accepted'))['semantic'], 'accepted')
        self.calls(sessions=1, capabilities=2)


class Isolation(Ledgered):
    def test_no_dk_workflows_configuration_or_state_is_imported(self):
        home = self.base / 'home'
        legacy = [home / '.config' / 'dk-workflows' / 'settings.json', home / '.local' / 'state' / 'dk-workflows' / 'x',
                  self.repo / '.pr-fix-state' / 'run.json']
        for path in legacy:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"semantic": "accepted"}')
            path.chmod(0)
        paths = acceptance.locations({'HOME': str(home), 'XDG_CONFIG_HOME': 'relative/ignored'})
        self.assertEqual(paths, {'configuration': home / '.config' / 'cf-workflows' / 'configuration.json',
                                 'ledger': home / '.local' / 'state' / 'cf-workflows' / 'configuration.sqlite3'})
        # A saved verdict carried in the configuration document is not proof of acceptance.
        self.saved['admission'] = {**self.saved['admission'], 'structural': 'accepted', 'semantic': 'accepted',
                                   'evidence_refs': ['native:dk-workflows-verdict']}
        self.ledger = acceptance.Ledger(paths['ledger'])
        self.addCleanup(self.ledger.close)
        result = self.run_ledgered(verdict('accepted'))
        self.calls(sessions=1)
        self.assertNotIn('native:dk-workflows-verdict', result['admission']['evidence_refs'])
        self.assertEqual(sorted(p.name for p in (home / '.local' / 'state').iterdir()), ['cf-workflows', 'dk-workflows'])
        self.assertEqual([path.stat().st_mode & 0o777 for path in legacy], [0] * len(legacy))

    def test_foreign_or_newer_state_databases_are_refused(self):
        foreign = self.base / 'foreign.sqlite3'
        with sqlite3.connect(foreign) as db:
            db.execute('CREATE TABLE runs (id TEXT)')
        newer = self.base / 'newer.sqlite3'
        with sqlite3.connect(newer) as db:
            db.execute('PRAGMA user_version = 2')
        other = self.base / 'other.sqlite3'
        acceptance.Ledger(other).close()
        with sqlite3.connect(other) as db:
            db.execute("UPDATE meta SET value = 'dk-workflows' WHERE key = 'suite'")
        for path in (foreign, newer, other):
            with self.subTest(path.name), self.assertRaises(acceptance.LedgerError):
                acceptance.Ledger(path)
        with self.assertRaises(acceptance.LedgerError):
            acceptance.Ledger.restore(self.base / 'missing.sqlite3', self.base / 'restored.sqlite3')
        self.assertFalse((self.base / 'missing.sqlite3').exists())


# -- Configuration writes ---------------------------------------------------------------------

class Interrupted(BaseException):
    """The helper process dies; nothing after this point runs."""


class Store:
    """The real file store with one injected fault and operation counts."""

    def __init__(self, path):
        self.inner = writes.FileStore(path)
        self.calls = Counter()
        self.fault = None

    def read(self):
        self.calls['read'] += 1
        if self.fault == 'read':
            raise ConnectionError('store offline')
        return self.inner.read()

    def write(self, expected, document):
        self.calls['write'] += 1
        fault, self.fault = self.fault, None
        if fault == 'interrupt-before':
            raise Interrupted()
        if fault == 'race':  # Another writer lands between our read and our write.
            current = self.inner.read()
            self.inner.write(current['revision'], {**current, 'scopes': []})
        revision = self.inner.write(expected, document)
        if fault == 'interrupt-after':
            raise Interrupted()
        if fault == 'lost-response':
            raise ConnectionError('response lost')
        return revision


class Writes(Ledgered):
    INSTRUCTION = 'configuration.scopes[0].options.tasks[0].instructions'

    def setUp(self):
        super().setUp()
        self.store = Store(self.base / 'config-home' / 'cf-workflows' / 'configuration.json')
        saved = self.configuration([self.scope('repo', 'repository', 'github.com/owner/repo', options={
            'tasks': [{'task': 'address-feedback', 'instructions': 'Rewrite everything.'}]})])
        self.store.inner.write(None, {key: value for key, value in saved.items() if key != 'revision'})
        self.invocation = None

    def admit_stored(self, *sessions):
        self.transport = Transport(self.engine, sessions, CAPABILITIES)
        self.store.calls.clear()
        return writes.admit(request=copy.deepcopy(self.request), invocation=copy.deepcopy(self.invocation),
                            deployment=self.deployment, roles=self.roles, roots=self.roots,
                            transport=self.transport, store=self.store, ledger=self.ledger)

    def clarify(self, fault=None):
        question = [{'id': 'u1', 'paths': [self.INSTRUCTION, 'request.instructions'], 'question': 'Everything?'}]
        first = self.admit_stored(verdict('needs_clarification', uncertainties=question))
        self.assertEqual(first['preflight'], 'waiting')
        self.base_revision = self.store.inner.read()['revision']
        self.engine.answer(self.engine.gates[-1]['gate_id'], 'answer-1',
                           {'action': 'replace', 'path': self.INSTRUCTION, 'text': 'Only the flaky test.'})
        self.store.fault = fault
        return lambda: self.admit_stored(verdict('accepted'))

    def saved_instruction(self):
        return self.store.inner.read()['scopes'][0]['options']['tasks'][0]['instructions']

    def intents(self):
        return self.ledger.writes()

    def test_accepted_revision_is_written_once_with_its_admission_record(self):
        result = self.clarify()()
        self.assertEqual(result['configuration_write']['status'], 'applied')
        self.assertEqual(self.store.calls, Counter(read=1, write=1))
        saved = self.store.inner.read()
        self.assertEqual(self.saved_instruction(), 'Only the flaky test.')
        self.assertEqual(saved['admission'], result['admission'])
        [intent] = self.intents()
        self.assertEqual((intent['status'], intent['dispatches'], intent['base_revision'], intent['document']),
                         ('applied', 1, self.base_revision, None))
        self.assertEqual(intent['receipt'], {'revision': saved['revision'], 'by': 'response'})
        # A fresh session reads the revised configuration and reuses the acceptance: no session, gate or write.
        self.ledger, self.engine = self.open_ledger(), Engine()
        again = self.admit_stored()
        self.assertEqual((again['preflight'], again['semantic']), ('admitted', 'accepted'))
        self.calls(gate_reads=0)
        self.assertEqual(self.store.calls, Counter(read=1))
        self.assertNotIn('configuration_write', again)

    def test_lost_response_is_settled_by_readback(self):
        result = self.clarify('lost-response')()
        self.assertEqual(result['configuration_write']['status'], 'applied')
        self.assertEqual(self.store.calls, Counter(read=2, write=1))
        [intent] = self.intents()
        self.assertEqual((intent['status'], intent['dispatches'], intent['receipt']['by']), ('applied', 1, 'readback'))

    def test_interruption_after_the_write_reconciles_without_reapplying(self):
        run = self.clarify('interrupt-after')
        with self.assertRaises(Interrupted):
            run()
        [intent] = self.intents()
        self.assertEqual((intent['status'], intent['dispatches']), ('pending', 1))
        self.ledger, self.engine = self.open_ledger(), Engine()  # Gates gone: no answer can be replayed.
        result = self.admit_stored()
        self.assertEqual(result['recovered_writes'], [{'intent_id': intent['intent_id'], 'status': 'applied'}])
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'))
        self.calls(gate_reads=0)
        self.assertEqual(self.store.calls['write'], 0)
        self.assertEqual(self.saved_instruction(), 'Only the flaky test.')
        [intent] = self.intents()
        self.assertEqual((intent['status'], intent['dispatches'], intent['receipt']['by']), ('applied', 1, 'readback'))

    def test_interruption_before_the_write_dispatches_the_same_intent_once(self):
        for fault, dispatches in [('interrupt-before', 1), ('prepared', 0)]:
            with self.subTest(fault):
                self.setUp()
                run = self.clarify(fault if fault != 'prepared' else None)
                if fault == 'prepared':
                    self.enterContext(patch.object(self.ledger, 'dispatching', side_effect=Interrupted()))
                with self.assertRaises(Interrupted):
                    run()
                [intent] = self.intents()
                self.assertEqual(intent['dispatches'], dispatches)
                self.assertEqual(self.store.inner.read()['revision'], self.base_revision)
                self.ledger, self.engine = self.open_ledger(), Engine()
                result = self.admit_stored()
                self.assertEqual(result['recovered_writes'], [{'intent_id': intent['intent_id'], 'status': 'applied'}])
                self.assertEqual(self.store.calls['write'], 1)
                self.assertEqual(self.saved_instruction(), 'Only the flaky test.')
                self.assertEqual(result['semantic'], 'accepted')
                self.calls(gate_reads=0)
                self.assertEqual([item['intent_id'] for item in self.intents()], [intent['intent_id']])

    def test_another_writer_leaves_the_uncertain_intent_blocked(self):
        run = self.clarify('interrupt-before')
        with self.assertRaises(Interrupted):
            run()
        current = self.store.inner.read()
        current['scopes'][0]['options']['tasks'][0]['instructions'] = 'Fix the flaky test and its neighbour.'
        self.store.inner.write(current['revision'], {k: v for k, v in current.items() if k != 'revision'})
        self.ledger, self.engine = self.open_ledger(), Engine()
        result = self.admit_stored(verdict('accepted'))
        [intent] = self.intents()
        self.assertEqual(result['recovered_writes'], [{'intent_id': intent['intent_id'], 'status': 'blocked'}])
        self.assertIn('uncertain', intent['failure'])
        self.assertIsNotNone(intent['document'])  # The exact original intent is preserved.
        self.assertEqual(self.store.calls['write'], 0)
        # The other writer's content is reassessed rather than overwritten.
        self.calls(sessions=1)
        self.assertEqual(self.transport.assignments[0]['input']['custom']['instructions'][0]['text'],
                         'Fix the flaky test and its neighbour.')

    def test_compare_and_swap_rejection_is_not_applied(self):
        result = self.clarify('race')()
        self.assertEqual(result['configuration_write']['status'], 'not_applied')
        [intent] = self.intents()
        self.assertEqual((intent['status'], intent['dispatches']), ('not_applied', 1))
        self.assertIn('compare-and-swap', intent['failure'])
        self.assertEqual(self.store.inner.read()['scopes'], [])

    def test_successful_sibling_is_preserved_when_a_later_write_is_interrupted(self):
        self.clarify()()
        [first] = self.intents()
        # A later request: a new local prompt conflicts with the revised saved instruction.
        self.invocation = {'prompts': [{'id': 'wide', 'slot': 'additional', 'text': 'Also fix neighbouring tests.'}]}
        conflict = [{'id': 'c1', 'paths': ['invocation.prompts[0]', self.INSTRUCTION], 'detail': 'Scope differs.'}]
        self.admit_stored(verdict('needs_clarification', conflicts=conflict))
        self.engine.answer(self.engine.gates[-1]['gate_id'], 'answer-2', {'action': 'remove', 'paths': [self.INSTRUCTION]})
        self.store.fault = 'interrupt-after'
        with self.assertRaises(Interrupted):
            self.admit_stored(verdict('accepted'))
        self.ledger, self.engine = self.open_ledger(), Engine()
        result = self.admit_stored()
        first_after, second = self.intents()
        self.assertEqual(first_after, first)
        self.assertEqual((second['status'], second['receipt']['by'], second['base_revision']),
                         ('applied', 'readback', first['receipt']['revision']))
        self.assertEqual(self.store.calls['write'], 0)
        self.assertNotIn('instructions', self.store.inner.read()['scopes'][0]['options']['tasks'][0])
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'))
        self.calls(gate_reads=0)

    def test_backup_and_restore_preserve_acceptance_and_unknown_effects(self):
        run = self.clarify('interrupt-after')
        with self.assertRaises(Interrupted):
            run()
        backup = self.base / 'backup' / 'configuration.sqlite3'
        backup.parent.mkdir()
        self.ledger.backup(backup)
        self.ledger.close()
        os.unlink(self.state)
        self.ledger = acceptance.Ledger.restore(backup, self.state)
        self.addCleanup(self.ledger.close)
        [intent] = self.intents()
        self.assertEqual(intent['status'], 'pending')  # The unknown effect survives the restore.
        self.engine = Engine()
        result = self.admit_stored()
        self.assertEqual(result['recovered_writes'][0]['status'], 'applied')
        self.assertEqual(self.store.calls['write'], 0)
        self.assertEqual(result['semantic'], 'accepted')
        self.calls(gate_reads=0)

    def test_hand_edit_keeping_the_revision_field_is_never_overwritten(self):
        run = self.clarify('interrupt-before')
        with self.assertRaises(Interrupted):
            run()
        path = self.store.inner.path
        raw = json.loads(path.read_text())
        raw['scopes'][0]['options']['prohibitions'] = ['Never edit vendored code.']
        path.write_text(json.dumps(raw))  # The stored revision field is left as it was.
        self.ledger, self.engine = self.open_ledger(), Engine()
        result = self.admit_stored(verdict('accepted'))
        [intent] = self.intents()
        self.assertEqual(result['recovered_writes'], [{'intent_id': intent['intent_id'], 'status': 'blocked'}])
        self.assertEqual(self.store.calls['write'], 0)
        self.assertEqual(json.loads(path.read_text())['scopes'][0]['options']['prohibitions'],
                         ['Never edit vendored code.'])
        # A direct compare-and-swap against the stale field is rejected as well.
        with self.assertRaises(writes.RevisionConflict):
            self.store.inner.write(raw['revision'], {key: value for key, value in raw.items() if key != 'revision'})

    def test_unavailable_store_blocks_before_any_session_or_write(self):
        self.store.fault = 'read'
        result = self.admit_stored()
        self.assertEqual((result['preflight'], result['structural']), ('blocked', 'pending'))
        self.assertEqual(self.transport.calls, Counter())
        self.assertEqual(self.store.calls, Counter(read=1))

    def test_unsettled_write_blocks_another_prepare(self):
        run = self.clarify('interrupt-after')
        with self.assertRaises(Interrupted):
            run()
        with self.assertRaisesRegex(acceptance.LedgerError, 'must be reconciled'):
            self.ledger.prepare(request_id='r', evidence_refs=[], base_revision=None, document={}, target_digest='x')

    def test_file_store_revisions_are_content_derived_and_compare_and_swap(self):
        store = writes.FileStore(self.base / 'plain' / 'configuration.json')
        self.assertIsNone(store.read())
        first = store.write(None, {'a': 1})
        self.assertEqual(first, 'rev_' + writes.content({'a': 1})[:32])
        with self.assertRaises(writes.RevisionConflict):
            store.write(None, {'a': 2})
        self.assertEqual(store.read(), {'a': 1, 'revision': first})
        self.assertEqual(json.loads((self.base / 'plain' / 'configuration.json').read_text())['a'], 1)


if __name__ == '__main__':
    unittest.main()
