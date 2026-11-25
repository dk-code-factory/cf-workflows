"""Ported portable PR-fix fixtures (Agent Workflow, MIT); modules import their siblings directly."""
from pathlib import Path
import sys

# Keep bytecode caches out of the installed closure these tests import from.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[1] / "cf_workflows" / "workflows" / "dk-pr-fix" / "workflows" / "helpers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
