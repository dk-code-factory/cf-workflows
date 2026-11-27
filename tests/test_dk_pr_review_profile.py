"""A code-review-ad pass profile admitted through installed dk-pr-review preflight (#24).

The profile is three custom reviewer adapters (Toolkit, Standards, Spec) in one
parallel group. It goes through the installed review preflight's semantic
assessment with the suite's own acceptance ledger, then through the installed
helpers in review mode. Disposable repositories, a fresh state namespace and
fail-closed fakes: the engine transport answers only the capability query, one
scripted validator task and gate reads; the GitHub transport answers only
capture. Native sessions are only the reports a test hands the helpers. Nothing
here starts an engine, provider or host, so none of it certifies session
adherence or a real code-review-ad package.
"""
from collections import Counter
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

import dk_pr_fix  # noqa: F401  (puts the installed helpers and ported fixtures on sys.path)
from prfix import app
from prfix.core import read_artifact
from prfix.prepare import prepare

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import dk_pr_review, writes  # noqa: E402
from cf_workflows import dk_pr_fix as fix_preflight  # noqa: E402
from test_dk_pr_fix import CAPABILITIES, FakeTransport, clean_install  # noqa: E402
import test_dk_pr_review as incoming  # noqa: E402
from test_semantic import Engine, verdict  # noqa: E402

AGENTS, PASSES, PR_URL = incoming.AGENTS, incoming.PASSES, incoming.PR_URL
NAMES = ["code-review-ad-" + name for name in PASSES]
DEPLOYMENT = {'controller': {'run-timeout': '2h'},
              'execution': {'mode': 'container', 'image': 'ghcr.io/fixture/helper@sha256:' + 'a' * 64},
              'agents': {'codex': {'provider': 'fixture-provider', 'sandbox': 'workspace-write'}}}


def profile(**changed):
    """The pass profile as custom invocation options: one required reviewer occurrence per pass."""
    adapters = {name: {'kind': 'review', 'definition': 'reviewer', 'writes': False,
                       'assignment': changed.get(name, 'Execute only the ' + name[len('code-review-ad-'):]
                                                 + ' pass of code-review-ad. Do not coordinate or run other passes.')}
                for name in NAMES}
    return {'options': {'tasks': [{'stage': 'reviews'}], 'reviewers': [list(NAMES)], 'adapters': adapters}}


class PassProfile(incoming.Fixture):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix='cfw review profile ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.install = clean_install(self.base / 'clean install')
        self.home = self.base / 'state home'
        self.xdg = {'HOME': str(self.base / 'user home'), 'XDG_STATE_HOME': str(self.base / 'xdg state'),
                    'XDG_CONFIG_HOME': str(self.base / 'xdg config')}
        self.engine = Engine()
        self.reopen()

    def reopen(self):
        """A fresh process: a new connection to the same durable acceptance ledger."""
        _, self.acceptances = writes.open_state(self.xdg)
        self.addCleanup(self.acceptances.close)

    def admit(self, *sessions, invocation=None, **changes):
        before = Counter(self.transport.calls)
        self.engine_transport = engine = FakeTransport(CAPABILITIES)
        script = list(sessions)
        self.assignments = []

        def session(task):
            self.assignments.append(task)
            if not script:
                raise AssertionError('unscripted validator session')
            return script.pop(0)(task)
        engine.configured.update(validator_session=session, open_input_gate=self.engine.open,
                                 input_gates=lambda request_id: copy.deepcopy(
                                     [gate for gate in self.engine.gates if gate['request_id'] == request_id]))
        arguments = {'request': {'pr': PR_URL, 'instructions': 'Review this PR.'}, 'configuration': None,
                     'invocation': profile() if invocation is None else invocation, 'deployment': DEPLOYMENT,
                     'repository': self.root, **changes}
        result = dk_pr_review.preflight(transport=engine, home=self.home, install=self.install,
                                        ledger=self.acceptances, **copy.deepcopy(arguments))
        self.assertLessEqual(set(engine.calls), {'capabilities', 'input_gates', 'validator_session', 'open_input_gate'})
        self.assertFalse(self.home.exists(), 'preflight stages nothing')
        self.assertEqual(self.transport.calls, before, 'preflight reads nothing from GitHub')
        return result

    def plan(self, admitted):
        """The installed helpers' plan from exactly the inputs review.yml passes to main.yml."""
        controls = {key: value for key, value in admitted['inputs'].items() if key != 'request'}
        result = prepare(self.root, admitted['state']['request_document'], {**controls, 'mode': 'review'},
                         'exec-1', self.transport, self.definitions)
        return read_artifact(self.root, result['plan'])

    def reports(self, plan, ledger):
        snapshot = app._snapshot(self.root, self.transport, plan)
        return {task['skill']: read_artifact(self.root, current['report'])['report']
                for task in plan['tasks'] if (current := app._current(ledger, snapshot, task))}

    def test_profile_is_assessed_once_and_reaches_review_yml_as_declared_inputs(self):
        result = self.admit(verdict('accepted'))
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'), result.get('error'))
        self.assertEqual((result['workflow'], result['installation']['entry']),
                         ('cf-workflows:dk-pr-review', 'workflows/review.yml'))
        self.assertEqual(self.engine_transport.calls, Counter(capabilities=1, input_gates=1, validator_session=1))
        [task] = self.assignments
        self.assertEqual(sorted(task['subjects']), sorted('invocation.options.adapters.' + name for name in NAMES))
        inputs = result['inputs']
        self.assertEqual((inputs['tasks'], inputs['finalizers'], inputs['reviewers']),
                         ([{'stage': 'reviews'}], [], [NAMES]))
        self.assertEqual(set(inputs['adapters']), set(NAMES))
        for name in NAMES:
            self.assertEqual(inputs['adapters'][name]['assignment'],
                             profile()['options']['adapters'][name]['assignment'])
            self.assertEqual((inputs['adapters'][name]['definition'], inputs['adapters'][name]['writes']),
                             ('reviewer', False))
        # Acceptance grants nothing, and adapters alone add no prompt or policy input.
        self.assertFalse(any(inputs['permissions'].values()))
        self.assertEqual(result['state']['request_document']['authorizations'], {})
        self.assertFalse({'prompts', 'policy'} & set(inputs))
        self.assertEqual(result['state']['request_document']['logical_run'], dk_pr_review.logical_run(PR_URL))
        body = (fix_preflight.INSTALL / 'workflows' / 'review.yml').read_text()
        for name in inputs:
            self.assertIn(f'{name}: ${{{{ inputs.{name} }}}}', body)

    def test_unchanged_profile_reuses_its_acceptance_and_a_changed_pass_is_assessed_alone(self):
        first = self.admit(verdict('accepted'))
        [task] = self.assignments
        self.reopen()
        self.engine = Engine()  # No gate survives: reuse needs none.
        again = self.admit()
        self.assertEqual((again['preflight'], again['semantic']), ('admitted', 'accepted'), again.get('error'))
        self.assertEqual(self.engine_transport.calls, Counter(capabilities=1))
        self.assertEqual(set(again['reuse']['reused'].values()), {'result-' + task['claim']})
        self.assertEqual(again['inputs'], first['inputs'])
        # Only the changed pass goes back to the validator; its siblings keep their original evidence.
        changed = profile(**{'code-review-ad-spec': 'Execute only the spec pass of code-review-ad against the '
                                                    'linked issue. Do not coordinate or run other passes.'})
        result = self.admit(verdict('accepted'), invocation=changed)
        self.assertEqual((result['preflight'], result['semantic']), ('admitted', 'accepted'), result.get('error'))
        self.assertEqual(self.engine_transport.calls['validator_session'], 1)
        self.assertEqual(list(result['reuse']['invalidated']), ['adapter:code-review-ad-spec'])
        self.assertEqual(self.assignments[-1]['subjects'], ['invocation.options.adapters.code-review-ad-spec'])
        self.assertEqual(set(result['reuse']['reused']),
                         {'adapter:code-review-ad-toolkit', 'adapter:code-review-ad-standards'})

    def test_an_unaccepted_profile_maps_nothing(self):
        conflict = [{'id': 'c1', 'paths': ['invocation.options.adapters.code-review-ad-toolkit'],
                     'detail': 'Asks the reviewer to edit source.'}]
        for response, outcome in ((verdict('rejected', conflicts=conflict), 'rejected'),
                                  (verdict('accepted', operations=None, skills_loaded=['code-review-ad']),
                                   'blocked')):
            with self.subTest(outcome=outcome):
                self.engine = Engine()
                result = self.admit(response)
                self.assertEqual(result['preflight'], outcome)
                self.assertNotIn('inputs', result)
                self.assertEqual(self.engine_transport.calls['validator_session'], 1)

    def test_snapshotted_package_resources_reject_before_any_gate_read_or_session(self):
        # The upstream generator's form pins the package's pass briefs as adapter resources. The installed
        # workflow mounts no configured resource, so the profile rejects instead of running without them.
        package = self.base / 'code-review-ad'
        (package / 'passes').mkdir(parents=True)
        files = {'passes/' + name + '.md': ('The ' + name + ' pass brief.\n').encode() for name in PASSES}
        for rel, body in files.items():
            (package / rel).write_bytes(body)
        configuration = {'schema_version': 1, 'kind': 'configuration', 'suite': 'cf-workflows', 'revision': 'rev-1',
                         'scopes': [], 'resources': [{'root': 'configuration', 'path': rel,
                                                      'sha256': hashlib.sha256(body).hexdigest(),
                                                      'purpose': 'Pass brief.', 'owner': 'maintainers'}
                                                     for rel, body in files.items()],
                         'admission': {'structural': 'pending', 'semantic': None, 'request_id': None,
                                       'context_digest': None, 'resource_digest': None,
                                       'capability_digest': None, 'evidence_refs': []}}
        options = profile()['options']
        resourced = {name: {**adapter, 'resources': ['configuration:passes/' + name[len('code-review-ad-'):] + '.md']}
                     for name, adapter in options['adapters'].items()}
        result = self.admit(invocation={'options': {**options, 'adapters': resourced}},
                            configuration=configuration, roots={'configuration': package})
        self.assertEqual((result['preflight'], result['error']['path']),
                         ('rejected', 'options.adapters.code-review-ad-toolkit.resources'), result)
        self.assertNotIn('inputs', result)
        self.assertEqual(self.engine_transport.calls, Counter(capabilities=1))

    def test_each_admitted_pass_is_its_own_session_and_keeps_its_original_report(self):
        plan = self.plan(self.admit(verdict('accepted')))
        reviewers = [task for task in plan['tasks'] if task['kind'] == 'review']
        self.assertEqual([task['skill'] for task in reviewers], NAMES)
        self.assertTrue(all(task['required'] and task['definition'] == 'reviewer' and not task['writes']
                            for task in reviewers))
        self.assertEqual(self.task(plan, 'finding_verification')['definition'], 'verifier')
        self.assertFalse(any(task['writes'] or task['kind'] == 'fix' for task in plan['tasks']))
        ledger = self.ledger(plan)
        originals = {name: {'pass': name, 'original': 'Original ' + name + ' pass output.'} for name in PASSES}
        finding = self.finding('toolkit-1')
        self.execute(plan, ledger, 'code-review-ad-toolkit', AGENTS[0],
                     self.report(findings=[finding], deliverables={'report': originals['toolkit']}))
        self.execute(plan, ledger, 'code-review-ad-standards', AGENTS[1],
                     self.report(deliverables={'report': originals['standards']}))
        # Two passes and no verifier: missing work never becomes a clean review.
        review = self.coverage(plan, ledger)['review']
        self.assertEqual((review['outcome'], review['clean_review'], review['partial']), ('incomplete', False, True))
        self.assertEqual(sorted(review['required_missing']),
                         sorted([self.task(plan, 'code-review-ad-spec')['id'], 'finding_verification']))
        self.execute(plan, ledger, 'code-review-ad-spec', AGENTS[2],
                     self.report(deliverables={'report': originals['spec']}))
        self.execute(plan, ledger, 'finding_verification', AGENTS[3])
        review = self.coverage(plan, ledger)['review']
        self.assertEqual((review['outcome'], review['complete'], review['clean_review']), ('findings', True, False))
        self.assertEqual(len(review['findings']['confirmed']), 1)
        self.assertEqual({item['adapter']: item['agent'] for item in review['occurrences']},
                         {**dict(zip(NAMES, AGENTS)), 'finding_verification': AGENTS[3]})
        reports = self.reports(plan, ledger)
        for name in PASSES:
            self.assertEqual(reports['code-review-ad-' + name]['deliverables'], {'report': originals[name]})
        self.assertEqual(reports['code-review-ad-toolkit']['findings'], [finding])
        self.assertEqual(self.native, 4)
        # An unchanged rerun from a fresh preflight reuses the profile's acceptance and every pass.
        ledger.close()
        self.reopen()
        rerun = self.plan(self.admit())
        self.assertEqual(self.engine_transport.calls, Counter(capabilities=1))
        reopened = self.ledger(rerun)
        for skill in (*NAMES, 'finding_verification'):
            self.assertFalse(self.execute(rerun, reopened, skill, AGENTS[5])['invoke'])
        self.assertEqual(self.native, 4)
        self.assertEqual(self.reports(rerun, reopened), reports)
        self.assertEqual(set(self.transport.calls), {'capture'})

    def test_the_native_task_session_loads_no_skill(self):
        # Each pass runs as an ordinary reviewer session: native.yml selects an admitted definition only.
        body = (fix_preflight.INSTALL / 'workflows' / 'native.yml').read_text()
        self.assertIn('uses: agent/run', body)
        self.assertNotRegex(body, r'\n\s+skill:')


if __name__ == '__main__':
    unittest.main()
