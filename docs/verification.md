# Contracts and inventory verification

Run the standard-library-only verifier from this repository with local source
clones that contain the exact pinned objects:

```sh
python3 scripts/check_baselines.py \
  --dk-workflows /path/to/dk-workflows \
  --agent-workflow /path/to/agent-workflow
python3 -m unittest discover -s tests -v
```

If necessary, fetch the exact commits into those source clones separately; the
verifier never fetches, checks out, executes or mutates upstream source. It checks
the exact two host manifests and skill directory sets, all 29 distinct inventory
rows and required fields, entry/direct-reference paths and SHA-256 hashes,
portable source existence, exact copied license bytes and local Markdown file
targets. It does not validate remote URLs, Markdown anchors, transitive runtime
resource closure or semantic correctness of the descriptions. Review the latter
against the pinned entries and [contracts](contracts/v1/README.md).
The regression test verifies that missing or extra host-manifest entries reject
before any source-object reads, instead of silently checking a partial host set.
The checker also verifies the [dk-pr-fix extraction](dk-pr-fix.md). Every file of
the pinned `examples/pr-fix/` must be copied or excluded with a reason. Each copied
file must match its recorded upstream digest. `verbatim` files must be identical,
and the YAML roles must equal the canonical conversion of the pinned TOML.

[dk-pr-fix tests](dk-pr-fix.md#verification) (`tests/test_dk_pr_fix.py`, plus the
ported example tests in `tests/dk_pr_fix/`) use disposable repositories, a fresh
state namespace per test, a fail-closed fake engine transport and a fail-closed
fake GitHub transport. They assert invocation counts and run the installed helpers
from a relocated read-only copy against a separate checkout.
`tests/test_dk_pr_fix_continuation.py` counts native dispatches while it replaces
runs, deletes checkpoints and changes configuration, and checks the
[retained budgets, occurrences and continuation](dk-pr-fix.md#budgets-occurrences-and-continuation).

[Structural admission](admission.md) tests (`tests/test_admission.py`) use
disposable Git repositories and resource roots plus a fake engine transport. Any
operation other than the configured `capabilities()` query fails without a live
fallback. Every rejection asserts zero capability queries when it comes from a
local structural check and zero skill, session or publication operations in all
cases. [Semantic assessment](semantic.md) tests (`tests/test_semantic.py`) script
every verdict through fake validator sessions and a durable fake gate store that
outlives helper restarts. They cover interrupted clarification, changed and
revised proposals, attempted prohibition waivers, validator attempts to run or
publish, and a default plan with zero validator sessions. Each asserts its
capability, gate and session counts. [Acceptance reuse](acceptance.md) tests
(`tests/test_acceptance.py`) add a disposable SQLite ledger and the real file
store with injected faults. They change each relevant input in turn and assert
which units are assessed and which are reused with their original evidence, and
that unrelated changes and fresh sessions run no session or gate read. They cover
clarified acceptances, lost gate responses, lost write responses, interruption
before and after a write, another writer, compare-and-swap rejection, a
preserved earlier sibling, backup and restore, and refusal of foreign state.
[Feedback obligation](feedback.md) tests (`tests/test_feedback.py`) cover
dk-workflows#244 with a disposable ledger per test and a fake GitHub transport
whose only operation is a counted `capture()`: rejected bot and style
suggestions, mixed threads, questions for other participants, already answered
and optional sources, edited sources, deleted replies and denied permissions.
These tests do not certify any engine, provider or host. Future runtime
issues must use disposable repositories and explicit fake
GitHub/provider/native-host/configuration-agent transports. Unknown fixture
operations must fail without falling back to network/model execution. Assert
invocation counts for changed-input selective reuse, capacity-one execution,
claim/result loss, interruptions, concurrent writers, uncertain publication and
exhausted budgets. Keep original outputs/claims/effects and absolute deadlines.

The [inventory](inventory.md) is a list of obligations, not a passing acceptance
matrix. Each row eventually needs separate direct and composed scenarios,
recovery/permission-denial cases and actual supported-host capability evidence.
All nine inherited defect mappings in the suite tracker remain required. Real
native-host adherence, live providers, GitHub publication conformance and release
certification stay separate and pending until explicitly authorized and verified.
