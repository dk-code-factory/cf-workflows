# cf-workflows contracts, version 1

These are normative design contracts for the independent `cf-workflows` suite,
version `0.1.0-dev`. They are not an installed plugin or executable schema validator.
The machine-readable identity lives in [sources.json](../../../baselines/sources.json).
Configuration admission is implemented under issues #11–#13; structural
admission is in [admission.md](../../admission.md), semantic assessment in
[semantic.md](../../semantic.md) and acceptance reuse and configuration writes in
[acceptance.md](../../acceptance.md). Installed execution
and host observation require their actual upstream capabilities. All inventory
rows currently have `not_implemented` status. Neither the historical adapter
aliases nor this documentation establish native skill parity.

## Ownership

| Concern | Owner | Boundary |
| --- | --- | --- |
| Graph sequence, independent readers, serialized writers, review/fix loops, waits and finalizers | Suite YAML | Visible checked-in orchestration; never generated hidden workflow graphs |
| Configuration meaning, PR identity/policy, feedback obligations, findings, budgets, intent/receipt interpretation | Focused suite Python operations | Bounded input/output operations; no second agent loop or helper-owned provider launcher |
| Scheduling, provider sessions, immutable admitted assets, generic execution recovery, host-task transport | Agent Workflow | Consume supported versioned interfaces; no local substitute controller or historical Go PR-fix application |
| Native skill coordinator, required independent children, lifecycle and deeper-look commands | Already-running selected host | Actual native execution and claim-bound observations required; imported prompts alone are insufficient |

Host (`codex` or `claude`) and execution provider are independent selections.
The suite must check the capability requirements of the selected path before
substantive dispatch. Unsupported capabilities reject preflight. A supported
native task with no attached host waits; this is not a completed task.
Uncertain original execution reconciles before any replacement dispatch.
No released engine or real host/provider combination is certified by this PR.

## Public interfaces

Every persisted/public document has an integer `schema_version: 1` and a `kind`
from the applicable contract. JSON uses UTF-8; duplicate object keys and nonfinite
numbers are invalid. Unknown fields reject rather than silently weakening policy.
Identifiers are nonempty opaque strings unless a field specifies otherwise.
SHA-256 resource digests are 64 lowercase hex characters; Git object IDs are
full IDs in the repository's declared object format. Timestamps are UTC RFC 3339.
Paths in admitted resources are normalized relative to their declared immutable
root; escape, symlink escape and missing resources reject admission.
Credentials are referenced by secret handles, never copied into public reports,
configuration evidence, resource hashes or application transcripts.

- [Configuration](configuration.md): complete resolution and structural/semantic admission.
- [Invocation](invocation.md): exact instructions, identities, capability and permission bindings.
- [Report](report.md): coverage, evidence, completion and readiness distinctions.
- [Application state](state.md): selective reuse, retained allowances and uncertain effects.

These contracts specify required data and invariants rather than shipping runtime
JSON Schemas. Downstream schemas/validators must implement these constraints and
explicitly version any incompatible change. Examples are illustrative shapes,
not authorizations or claims of runtime support.

## Compatibility and independent evolution

Suite version, public contract version and exact released-engine version are
separate bindings. Before a package is runnable it must declare a supported exact
released engine, capabilities and package resource closure using the upstream
shared tooling. Source commit pins are provenance, not released-engine pins.
Codex and Claude packages must be generated from one suite descriptor under #36;
hand-maintained launchers are not the distribution contract.

Within schema version 1 a producer must retain required fields and semantics.
A consumer rejects unknown versions or incompatible capability bindings before
execution; it must not reinterpret a document using current defaults. Changes to
accepted representations need a documented converter or a new schema version.
Upgrades are transactional, retain the original record, and preserve all IDs,
consumed budgets, deadlines, admitted old resources, uncertainty and receipts.
An interrupted upgrade resumes or rolls back without dispatching effects.
Read-only inspection may remain available for unsupported old state; execution
requires the original supported resources and engine or an explicit compatible
upgrade. Deleting/replacing an installed plugin must not delete engine-owned
admitted assets or suite recovery state. Backups include both stores by reference
and content as required by the engine recovery contract.

Upstream changes are evaluated explicitly, never automatically synchronized.
Intentional differences are per-row inventory fields and versioned decisions;
none silently waive a baseline completion condition. Adding a skill requires a
new explicit inventory row and direct/composed acceptance evidence. Removing or
renaming one requires a compatibility/migration decision, not an alias count.

## Fresh setup and manual equivalents

The proposed configuration home is `$XDG_CONFIG_HOME/cf-workflows` (fallback
`~/.config/cf-workflows`), with `configuration.json` holding suite settings and
scoped settings/preferences. Application state uses
`$XDG_STATE_HOME/cf-workflows` (fallback `~/.local/state/cf-workflows`), partitioned
by canonical repository identity and logical run ID using collision-resistant
encoded keys. User-supplied paths never concatenate directly into store paths.
Neither store is inside a plugin install or target checkout by default. A future
explicit override must preserve independent namespace and safe-path checks.

Start these stores empty. Do not import dk-workflows settings/preferences,
`.pr-fix-state`, active runs, claims, journals, agent sessions or prior grants.
For manual equivalents, inspect old values read-only, restate desired reviewers,
prompt text, task/finalizer selections, instruction-source choices and options
in a new configuration request, and pass the same admission as any new input.
Re-establish each scoped permission deliberately. Save no implicit default-source
choice. Reuse no old acceptance verdict as proof for the new suite. Keep old
install/state intact for its original engine's recovery; fresh setup does not
resume an old run. No automatic migration command is supplied here.

## Verification boundary

[The inventory guide](../../inventory.md) and
[baseline verification](../../verification.md) define the checks for this PR.
Future runtime fixtures must use disposable repositories and fail-closed fake
GitHub, provider, native-host and configuration-agent transports. Unconfigured
operations fail; there is no live fallback. Assert invocation counts for reuse
and recovery, not only final status. Real native-host adherence, live provider
execution, GitHub publication conformance and release publication require their
own explicit authorization and separate evidence. Deterministic success cannot
promote any of those capabilities to verified support.
