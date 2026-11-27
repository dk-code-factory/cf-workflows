"""Runtime prerequisite checks report guidance and never repair, pull, build or log in.

Disposable Git repositories under paths with spaces and a fake host whose
unconfigured commands fail without a live fallback. No container runtime,
image, network or credential is used.
"""
from collections import Counter
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cf_workflows import prerequisites  # noqa: E402

GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TEMPLATE_DIR': ''}
IMAGE = 'registry.example.com/tools@sha256:' + 'a' * 64
DEPLOYMENT = {'controller': {'run-timeout': '2h'},
              'execution': {'mode': 'container', 'image': IMAGE, 'runtime': 'docker'},
              'agents': {'codex': {'provider': 'codex-app-server'}}}
ALL_TOOLS = 'python ok\ngit ok\ngh ok\nsleep ok\n'
BANNED = {'pull', 'build', 'load', 'login', 'logout', 'auth', 'push', 'commit', 'fetch', 'clone', 'credential'}


def git(root, *args):
    subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


class FakeHost:
    """Scripted PATH and commands; Git reads delegate to real Git on the disposable repository."""

    def __init__(self, found=('git', 'docker'), image=True, tools=ALL_TOOLS):
        self.found, self.image, self.tools = set(found), image, tools
        self.calls, self.argv = Counter(), []

    def which(self, name):
        self.calls['which'] += 1
        return '/usr/bin/' + name if name in self.found else None

    def run(self, argv):
        self.argv.append(list(argv))
        if argv[:2] == ['git', '-C'] and argv[3:6] == ['config', '--local', '--get']:
            self.calls['git config'] += 1
            completed = subprocess.run(argv, capture_output=True, env={**os.environ, **GIT_ENV})
            return completed.returncode, completed.stdout.decode()
        if argv == ['docker', 'image', 'inspect', '--format', '{{.Id}}', IMAGE]:
            self.calls['image inspect'] += 1
            return (0, 'sha256:' + 'a' * 64) if self.image else (1, '')
        if argv[:7] == ['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--entrypoint']:
            self.calls['image tools'] += 1
            return 0, self.tools
        raise AssertionError('unconfigured fixture operation: ' + repr(argv))


class Prerequisites(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'target repo' / 'check out'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', '--local', 'user.name', 'Fixture Author')
        git(self.repo, 'config', '--local', 'user.email', 'fixture@example.com')
        (self.repo / 'file.txt').write_text('content\n')
        git(self.repo, 'add', 'file.txt')
        git(self.repo, 'commit', '-q', '-m', 'initial')

    def snapshot(self, root):
        return {path: path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}

    def check(self, host, deployment=DEPLOYMENT, repository=None, **options):
        repository = repository or self.repo
        before = self.snapshot(repository)
        result = prerequisites.check(repository=repository, deployment=deployment, host=host, **options)
        self.assertEqual(self.snapshot(repository), before, 'prerequisite checks must not write')
        for argv in host.argv:
            self.assertFalse(BANNED & set(argv), argv)
            if argv[:2] == ['docker', 'run']:
                self.assertIn('--pull=never', argv)
        return result, {item['name']: item for item in result['checks']}

    def test_complete_host_is_ready_except_the_blocked_engine(self):
        host = FakeHost()
        result, checks = self.check(host)
        self.assertFalse(result['ready'])
        self.assertEqual({name for name, item in checks.items() if item['status'] != 'ok'}, {'engine', 'state_mount'})
        self.assertEqual(checks['engine']['status'], 'blocked')
        self.assertIn('#130', checks['engine']['guidance'])
        self.assertEqual(checks['state_mount']['status'], 'blocked')
        self.assertIn('/cf-workflows-state', checks['state_mount']['detail'])
        self.assertIn('#149', checks['state_mount']['guidance'])
        self.assertEqual(host.calls, Counter({'which': 2, 'git config': 2, 'image inspect': 1, 'image tools': 1}))

    def test_missing_prerequisites_are_guidance_only(self):
        cases = {
            'no git': (FakeHost(found=('docker',)), {}, {'git', 'commit_identity'}),
            'no docker': (FakeHost(found=('git',)), {}, {'container_runtime', 'helper_image'}),
            'image absent': (FakeHost(image=False), {}, {'helper_image'}),
            'image tools': (FakeHost(tools='python missing\ngit ok\ngh missing\nsleep ok\n'), {}, {'helper_image'}),
            'placeholder image': (FakeHost(), {'image': 'registry.example.com/x@sha256:' + '0' * 64},
                                  {'deployment', 'helper_image'}),
            'default timeout': (FakeHost(), {'controller': None}, {'run_timeout'}),
            'short timeout': (FakeHost(), {'controller': {'run-timeout': '90m'}}, {'run_timeout'}),
        }
        for name, (host, change, expected) in cases.items():
            with self.subTest(name):
                deployment = copy.deepcopy(DEPLOYMENT)
                if 'image' in change:
                    deployment['execution']['image'] = change['image']
                if 'controller' in change:
                    if change['controller'] is None:
                        del deployment['controller']
                    else:
                        deployment['controller'] = change['controller']
                result, checks = self.check(host, deployment)
                self.assertEqual({key for key, item in checks.items() if item['status'] == 'missing'}, expected)
                for key in expected:
                    self.assertTrue(checks[key]['guidance'], key)
        _, checks = self.check(FakeHost(tools='python missing\ngit ok\ngh missing\nsleep ok\n'))
        self.assertEqual(checks['helper_image']['detail'], 'image lacks python, gh')
        _, checks = self.check(FakeHost(image=False))
        self.assertIn('never builds or pulls', checks['helper_image']['guidance'])

    def test_run_timeout_must_cover_the_ci_window(self):
        _, checks = self.check(FakeHost(), ci_timeout_seconds=3 * 3600)
        self.assertEqual(checks['run_timeout']['status'], 'missing')
        self.assertIn('at least 3h', checks['run_timeout']['guidance'])

    def test_commit_identity_is_needed_only_for_writing_plans(self):
        git(self.repo, 'config', '--local', '--unset', 'user.email')
        _, checks = self.check(FakeHost())
        self.assertEqual(checks['commit_identity']['status'], 'missing')
        self.assertIn('user.email', checks['commit_identity']['detail'])
        host = FakeHost()
        _, checks = self.check(host, writes=False)
        self.assertEqual(checks['commit_identity']['status'], 'ok')
        self.assertEqual(host.calls['git config'], 0)

    def test_clone_must_be_self_contained_with_a_real_git_directory(self):
        worktree = self.base / 'linked worktree'
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(worktree))
        shallow = self.base / 'shallow clone'
        git(self.base, 'clone', '-q', '--depth', '1', 'file://' + str(self.repo), str(shallow))
        borrowed = self.base / 'borrowed clone'
        git(self.base, 'clone', '-q', '--shared', str(self.repo), str(borrowed))
        for name, repository in (('worktree', worktree), ('shallow', shallow), ('alternates', borrowed)):
            with self.subTest(name):
                host = FakeHost()
                _, checks = self.check(host, repository=repository)
                self.assertEqual(checks['repository']['status'], 'missing')
                self.assertTrue(checks['repository']['guidance'])
                self.assertEqual(checks['commit_identity']['status'], 'missing')
                self.assertEqual(host.calls['git config'], 0)

    def test_unconfigured_host_operation_fails_without_fallback(self):
        deployment = copy.deepcopy(DEPLOYMENT)
        deployment['execution']['runtime'] = 'podman'
        with self.assertRaisesRegex(AssertionError, 'unconfigured fixture operation'):
            prerequisites.check(repository=self.repo, deployment=deployment, host=FakeHost(found=('git', 'podman')))


if __name__ == '__main__':
    unittest.main()
