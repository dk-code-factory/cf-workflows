"""Run installed dk-pr-fix helper operations in a separate interpreter with a fake GitHub transport.

Usage: python3 -I -B dk_pr_fix_driver.py < spec.json

The spec names the installed helpers directory, the target checkout, the fixture
evidence and the action-v1 steps. Helpers are imported only from that directory.
The fake transport answers capture() from fixture evidence; every other operation
fails without a live fallback. Prints the results and invocation counts as JSON.
"""
from collections import Counter
import copy
import json
import sys

sys.dont_write_bytecode = True


class FakeGitHub:
    def __init__(self, pr, evidence):
        self.pr, self.evidence = pr, evidence
        self.last_capture = None
        self.read_only = False
        self.calls = Counter()

    def capture(self, identity):
        self.calls['capture'] += 1
        if identity.get('url') != self.pr:
            raise AssertionError('capture for an unexpected PR')
        self.last_capture = copy.deepcopy(self.evidence)
        return self.last_capture

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)

        def unconfigured(*args, **kwargs):
            self.calls[name] += 1
            raise AssertionError('unconfigured fixture operation: ' + name)
        return unconfigured


def main():
    spec = json.loads(sys.stdin.read())
    sys.path.insert(0, spec['helpers'])
    import action
    provider = FakeGitHub(spec['pr'], spec['evidence'])
    results = {}
    for step in spec['steps']:
        payload = step['input']
        for key, source in step.get('refs', {}).items():
            value = results
            for part in source:
                value = value[part]
            payload[key] = value
        request = {'protocol': 'action-v1', 'phase': step.get('phase', 'execute'), 'input': payload,
                   'context': {'operation_key': step['name'], 'execution_fingerprint': spec['execution']}}
        results[step['name']] = action.handle(step['operation'], request, spec['root'], provider)
    print(json.dumps({'results': results, 'calls': dict(provider.calls),
                      'modules': sorted({module.__file__ for name, module in sys.modules.items()
                                         if name == 'action' or name.startswith('prfix')})}))


if __name__ == '__main__':
    main()
