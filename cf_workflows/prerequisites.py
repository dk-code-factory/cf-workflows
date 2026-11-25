"""Read-only runtime prerequisite checks for the installed dk-pr-fix workflow.

These are the prerequisites inherited from Agent Workflow's portable PR-fix
example. Every missing one is reported as guidance; nothing is repaired. The
checks never log in, read or refresh credentials, build, pull or load images,
install an engine, use an engine found on PATH, or write to the checkout or the
installation. The only container command runs the already-present helper image
with `--pull=never` and no network to list its tools.

The verified exact-engine download is Agent Workflow's shared setup
(agent-workflow#130). It is not available yet, so the engine check always
reports it as blocked rather than substituting a local engine.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import admission

MINIMUM_RUN_TIMEOUT = 2 * 3600
DEFAULT_RUN_TIMEOUT = '30m'  # The engine's default controller.run-timeout.
UNITS = {'s': 1, 'm': 60, 'h': 3600}
# Lists the image's tools; any failure is reported per tool, never repaired.
TOOLS = ('python3 -c "import sys; sys.exit(sys.version_info < (3, 11))" '
         '&& echo "python ok" || echo "python missing"; '
         'for tool in git gh sleep; do command -v "$tool" >/dev/null && echo "$tool ok" || echo "$tool missing"; done')
IMAGE_TOOLS = ('python', 'git', 'gh', 'sleep')


class SystemHost:
    """The real host: PATH lookup and bounded commands without a shell or stdin."""

    def which(self, name):
        return shutil.which(name)

    def run(self, argv):
        try:
            completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            return 127, str(error)
        return completed.returncode, completed.stdout.decode(errors='replace')


def _item(name, status, detail, guidance=''):
    return {'name': name, 'status': status, 'detail': detail, 'guidance': '' if status == 'ok' else guidance}


def seconds(duration):
    match = isinstance(duration, str) and re.fullmatch(r'([1-9][0-9]*)([smh])', duration)
    return int(match[1]) * UNITS[match[2]] if match else None


def repository_layout(repository):
    """A self-contained clone with a real .git directory; filesystem reads only."""
    root = Path(repository)
    git = root / '.git'
    if not root.is_dir():
        return _item('repository', 'missing', 'target checkout is not a directory', 'Clone the repository first.')
    if git.is_symlink() or not git.is_dir():
        return _item('repository', 'missing', '.git is not a real directory (linked worktree, submodule or link)',
                     'Use an independent clone; container workspace mounts reject a .git file.')
    if (git / 'objects' / 'info' / 'alternates').exists():
        return _item('repository', 'missing', 'the clone borrows objects through alternates',
                     'Helper containers do not mount alternates; reclone, or add --dissociate to --reference.')
    if (git / 'shallow').exists():
        return _item('repository', 'missing', 'the clone is shallow',
                     'Fetch the full history, for example: git fetch --unshallow.')
    return _item('repository', 'ok', 'self-contained clone with a real .git directory')


def check(*, repository, deployment, host=None, writes=True, ci_timeout_seconds=1800):
    """Return {'ready', 'checks'}; each check is ok, missing (with guidance) or blocked.

    `deployment` is the parsed agents.yml. `writes` says whether the selected
    plan has writing tasks, which need a repository-local commit identity.
    """
    host = host or SystemHost()
    checks = [_item('python', 'ok' if sys.version_info >= (3, 11) else 'missing',
                    'host Python ' + '.'.join(map(str, sys.version_info[:3])), 'Install Python 3.11 or newer.')]
    git = host.which('git')
    checks.append(_item('git', 'ok' if git else 'missing', 'host Git ' + ('found' if git else 'not found on PATH'),
                        'Install Git.'))
    layout = repository_layout(repository)
    checks.append(layout)
    if not writes:
        checks.append(_item('commit_identity', 'ok', 'read-only plan needs no commit identity'))
    elif not git or layout['status'] != 'ok':
        checks.append(_item('commit_identity', 'missing', 'not checked: needs Git and a valid clone',
                            'Fix the Git and repository checks first.'))
    else:
        absent = [key for key in ('user.name', 'user.email')
                  if host.run(['git', '-C', str(repository), 'config', '--local', '--get', key])[0] != 0]
        checks.append(_item('commit_identity', 'missing' if absent else 'ok',
                            'missing repository-local ' + ', '.join(absent) if absent else
                            'repository-local user.name and user.email are set',
                            'Writing plans need them; helper containers do not see global Git configuration: '
                            "git config --local user.name 'Your Name'; "
                            "git config --local user.email 'you@example.com'."))
    checks += _deployment(deployment, host, ci_timeout_seconds)
    checks.append(_item('engine', 'blocked', 'no verified exact-engine download path is available',
                        'Agent Workflow #130 provides the shared verified setup and the pinned engine has no '
                        'release yet. Do not substitute an engine from PATH or a local build.'))
    return {'schema_version': 1, 'kind': 'prerequisites',
            'ready': all(item['status'] == 'ok' for item in checks), 'checks': checks}


def _deployment(deployment, host, ci_timeout_seconds):
    checks = []
    try:
        image = admission.resolve_deployment(deployment)['image']
        error = None
    except (admission.AdmissionError, KeyError, TypeError, AttributeError) as failure:
        image, error = None, str(failure)
    checks.append(_item('deployment', 'missing' if error else 'ok', error or 'agents.yml is structurally valid',
                        'Fix agents.yml; execution.image must be pinned by a real sha256 digest.'))
    execution = deployment.get('execution', {}) if isinstance(deployment, dict) else {}
    runtime = execution.get('runtime', 'docker') if isinstance(execution, dict) else 'docker'
    found = isinstance(runtime, str) and runtime in ('docker', 'podman') and host.which(runtime)
    checks.append(_item('container_runtime', 'ok' if found else 'missing',
                        f'{runtime} ' + ('found' if found else 'not found on PATH'),
                        f'Install {runtime} and make it usable by this user.'))
    if not (found and image):
        checks.append(_item('helper_image', 'missing', 'not checked: needs a valid deployment and container runtime',
                            'Fix the deployment and container runtime checks first.'))
    elif host.run([runtime, 'image', 'inspect', '--format', '{{.Id}}', image])[0] != 0:
        checks.append(_item('helper_image', 'missing', image + ' is not present locally',
                            'Build or load it yourself, then pin its digest in agents.yml. '
                            'Setup never builds or pulls images.'))
    else:
        code, output = host.run([runtime, 'run', '--rm', '--pull=never', '--network', 'none',
                                 '--entrypoint', 'sh', image, '-c', TOOLS])
        absent = [tool for tool in IMAGE_TOOLS if f'{tool} ok' not in output.split('\n')]
        checks.append(_item('helper_image', 'missing' if code or absent else 'ok',
                            'image lacks ' + ', '.join(absent) if absent else
                            ('image tool listing failed' if code else
                             'present locally with Python 3.11+, Git, gh and sleep'),
                            'Use an image with Python 3.11 or newer, Git, gh and sleep.'))
    controller = deployment.get('controller', {}) if isinstance(deployment, dict) else {}
    configured = controller.get('run-timeout') if isinstance(controller, dict) else None
    needed = max(MINIMUM_RUN_TIMEOUT, ci_timeout_seconds)
    actual = seconds(configured or DEFAULT_RUN_TIMEOUT)
    checks.append(_item('run_timeout', 'ok' if actual and actual >= needed else 'missing',
                        f'controller.run-timeout is {configured or DEFAULT_RUN_TIMEOUT + " (engine default)"}',
                        'Set controller.run-timeout in agents.yml to at least '
                        + (f'{needed // 3600}h' if needed % 3600 == 0 else f'{needed}s')
                        + ' for the complete plan, including the CI window.'))
    return checks
