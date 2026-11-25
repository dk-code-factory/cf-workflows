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

This documentation-only PR has no runtime transports or fixture operations.
No fake runtime execution is presented as implemented admission or host support.
Future runtime issues must use disposable repositories and explicit fake
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
