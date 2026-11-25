"""Keep disposable Git repositories independent of contributor configuration.

Each isolated test also gets a fresh, empty cf-workflows state namespace outside
its checkout, as the helper containers would have it mounted.
"""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def environment():
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": "", "GIT_TEMPLATE_DIR": ""}


def command(*args):
    return ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "init.templateDir=", *args]


def state(test):
    """Point CF_WORKFLOWS_STATE at a fresh directory for this test only."""
    directory = tempfile.TemporaryDirectory(prefix="cf-workflows-state-")
    test.addCleanup(directory.cleanup)
    test.state_dir = directory.name
    context = patch.dict(os.environ, {"CF_WORKFLOWS_STATE": directory.name})
    context.start()
    test.addCleanup(context.stop)
    return directory.name


def stage_request(test, value):
    """Stage a private request in the state namespace as host-side preflight does."""
    import hashlib
    import json
    from prfix.core import FORMAT, STATE_MARKER, canonical
    base = Path(test.state_dir)
    marker = base / STATE_MARKER
    if not marker.exists():
        marker.write_bytes(canonical({"format": FORMAT}))
    data = json.dumps(value).encode()
    reference = "requests/" + hashlib.sha256(data).hexdigest() + ".json"
    (base / "requests").mkdir(exist_ok=True)
    (base / reference).write_bytes(data)
    return reference


def isolate(test):
    context = patch.dict(os.environ, environment())
    context.start()
    test.addCleanup(context.stop)
    state(test)
