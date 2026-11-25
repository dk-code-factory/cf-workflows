# Baseline provenance and licensing

The machine-readable [sources](../baselines/sources.json) pin:

| Source | Exact commit | Inspected scope |
| --- | --- | --- |
| dk-workflows 5.17.0 | `3d9a6e7b6fc837082be3bbd295d23116ff7f71fa` | Both host manifests, all 29 skill entries and their direct local references |
| Agent Workflow portable PR-fix | `16ec5eecccd367dfdffcf1b529c51d181bbced49` | `examples/pr-fix/README.md`, workflow graph and `docs/PR_FIX.md` recovery/execution contract |

The [pinned Claude manifest](https://github.com/dk-code-factory/dk-workflows/blob/3d9a6e7b6fc837082be3bbd295d23116ff7f71fa/.claude-plugin/plugin.json)
and [pinned Codex manifest](https://github.com/dk-code-factory/dk-workflows/blob/3d9a6e7b6fc837082be3bbd295d23116ff7f71fa/.codex-plugin/plugin.json)
identify version 5.17.0. A README version is not the version authority.
The [portable entry guide](https://github.com/dk-code-factory/agent-workflow/blob/16ec5eecccd367dfdffcf1b529c51d181bbced49/examples/pr-fix/README.md)
describes local adapter assignments, including qualified legacy names. Those do
not invoke installed plugin skills. Its YAML/Python separation, original request
snapshots, bounded cycles, same-session continuation, independent verification,
publication intent and recovery are the extraction baseline, not proof of parity
with the whole suite. Do not copy the old `.pr-fix-state` namespace.

The pinned portable source has the known merge-request guard gap
(dk-workflows#253). The extraction consumes the actual upstream correction from
[Agent Workflow #138](https://github.com/dk-code-factory/agent-workflow/issues/138),
merged as [`2406f002`](https://github.com/dk-code-factory/agent-workflow/commit/2406f002319528a65de0091bd3b73c366ecc27db).
[dk-pr-fix-closure.json](../baselines/dk-pr-fix-closure.json) lists it under
`corrections`, and the checker requires the guard to be that commit's exact
definitions. The rest of the extraction stays at the pin.
Native transport and observed dispatch need [#136](https://github.com/dk-code-factory/agent-workflow/issues/136)
and [#137](https://github.com/dk-code-factory/agent-workflow/issues/137).
The shared packaging toolchain needs its own supported capabilities before
runtime integration. Open dependencies cannot be bypassed by local substitute
launchers or controllers. See the [delivery tracker](https://github.com/dk-code-factory/cf-workflows/issues/10).

## Notices

The contracts PR copied only license texts and paraphrased metadata. The
[dk-pr-fix extraction](dk-pr-fix.md) copies and adapts the portable example's
workflow closure and tests. Its per-file provenance is in
[dk-pr-fix-closure.json](../baselines/dk-pr-fix-closure.json), and the installed
closure carries the upstream MIT `LICENSE`. Preserve the following notices in
distributions containing copied or adapted substantial portions:

- [dk-workflows MIT notice](../licenses/dk-workflows-MIT.txt): Copyright 2026 Dmitry Kropachev.
- [Agent Workflow MIT notice](../licenses/agent-workflow-MIT.txt): Copyright 2026 PicoClaw contributors.
- [architecture-designer MIT notice](../licenses/architecture-designer-MIT.txt): retained upstream notice for adapted alternatives behavior.

Upstream alternatives attribution points to
[Jeff Allan's architecture-designer](https://github.com/jeffallan/claude-skills/blob/078a25cc5bfd8011459be7b91d004e900c1d02d3/skills/architecture-designer/SKILL.md).
Future extraction must audit the full copied resource closure and keep its
original notices and attribution, including further third-party notices. The
direct-reference inventory is not that transitive licensing/package audit.
Independent versioning does not remove attribution obligations. Evaluate future
upstream changes explicitly; these pins never imply automatic synchronization.
