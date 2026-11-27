# Configuration, preferences and repository onboarding

`cf-workflows:dk-repo-onboard`, `cf-workflows:dk-config` and
`cf-workflows:dk-preferences` are thin entries over the shared workflow
(#26). [`cf_workflows/entries.py`](../cf_workflows/entries.py) defines them and
[`cf_workflows/configuration.py`](../cf_workflows/configuration.py) holds their
adapter procedures. The procedures run on the host. Each native session runs
through the entry's [installed workflow](#installed-workflow), and none of them
has run through a released engine yet (see [What remains](#what-remains)).

## Entries

Each entry has these parts:

- A preset selection for the shared plan: one bundled adapter task, plus
  explicitly empty `finalizers` and `reviewers`.
- The adapter procedure that the task runs: the admitted `leaf` role, a bundled
  assignment and the helper operations in `configuration.py`.
- The entry's original role prompt and options from the pinned baseline.

| Entry | Adapter | Original options | Helper operations | Saves |
| --- | --- | --- | --- | --- |
| `dk-repo-onboard` | `onboard-repository` (inspection) | none | `discover`, `view`, `check_report` | Nothing |
| `dk-config` | `configure` (configuration) | none | `view`, `explain`, `apply_operations`, admit, write, readback | Settings, prompts and prompt resources |
| `dk-preferences` | `admit-preferences` (configuration) | `default_applies_to` | `view`, `apply_operations`, `overlaps`, admit, write, readback, `resolve` | Preferences |

The role prompts are the baseline `prompts/default.md` texts, byte for byte. Their
digests match [the inventory](../baselines/inventory.json). The bundled adapters
are control text in `admission.ENTRY_ADAPTERS`, like the baseline adapters. So an
entry's default plan has no custom input, runs no validator session and loads no
skill. A `dk-config` or `dk-preferences` invocation may add only `prompts`;
these pass the shared [semantic assessment](semantic.md). `dk-repo-onboard`
takes none, because a clarification would open a durable gate. Tasks, finalizers, reviewers, permissions,
skills and unknown entry options reject before the engine is contacted. A
remote-write authorization in the request rejects too: enabling a permission is a
saved setting, not an authorization.

Each entry run may also choose [native controls](#native-controls).

The entry's own run is admitted from bundled defaults and invocation prompts
only. Saved configuration is what the entry inspects or changes, not its governing
input. So an invalid saved document can still be reported and repaired.

A request targets a pull request (`pr`) or a whole repository (`repository`,
canonical `host/owner/name`). A repository target has no invented PR: `pr`
scopes do not match it, and `dk-pr-fix` preflight rejects it.

Composition: a `dk-pr-fix` plan may select `onboard-repository` as a read-only
task. It is admitted like any baseline adapter and passed to the installed
workflow's inputs. The two configuration adapters reject outside their entries.

## Installed workflow

The plugin descriptor lists the three entries. Each is a preset over the shared
session graph [`workflows/configure.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/configure.yml):

| Entry | Workflow | Modes | JSON schema |
| --- | --- | --- | --- |
| `dk-repo-onboard` | `workflows/onboard.yml` | `onboard` | `onboard`: the report sections |
| `dk-config` | `workflows/config.yml` | `explain`, `change` | `explain`; `change`: outcome, summary, operations, questions, resolutions |
| `dk-preferences` | `workflows/preferences.yml` | `change` | `change` |

`configure.yml` has one job per mode. Each job runs one `agent/run` on the
literal `agents/leaf.yaml`, with no `skill` selection, the mode's named schema,
`repair-attempts: 0` and `with.model` from the `model` input. The graph has no
helper container, PR capture, publication stage, gate or secret, and no
request or PR input: the target (a repository or a PR) travels in the typed
assignment, so a repository target needs no invented PR. A preset runs the
shared graph only for its own modes.

The procedure stays on the host: discovery, the configuration view, the report
and proposal checks, the shared structural and semantic admission, and the
configuration write through [`writes.py`](acceptance.md#configuration-writes)
with its readback. So the installed graph carries no second writer or
validator, and the configuration store never enters a helper container. This is
the same split as the installed `dk-pr-fix` preflight, which recovers and writes
back configuration on the host ([acceptance reuse](acceptance.md#installed-preflight)).

### Engine binding

[`configuration_binding.EngineTransport`](../cf_workflows/configuration_binding.py)
implements the procedures' transport over an engine port. It extends the
[validator binding](semantic.md#engine-binding), so `capabilities()`, the
shared validator and the clarification gates (`input_gates`, `open_input_gate`)
use `workflows/assess.yml` as before. `native_session(task)` runs the entry's
preset:

- The preset is chosen by the assignment's entry and mode. Another mode, another
  entry or another role blocks, and so does a `workflows/agents/leaf.yaml` whose
  SHA-256 differs from the admitted definition digest.
- The typed input holds the assignment and the exact text of every readable
  file, read again and checked against its admitted digest, so the session needs
  no file access. An input larger than the engine's task mailbox (1 MiB less a
  64 KiB reserve) blocks before any run.
- The run is correlated by the assignment's claim. A replayed claim returns the
  engine's retained run, so a lost response never starts a second session.
- The `model` control is the step's `with.model`, and the engine must report the
  same model. A per-run `reasoning_effort` has no engine parameter (it is an
  agent definition field), so it blocks before any run.
- Proposal operations arrive as JSON strings, as the leaf role's report contract
  asks for operation objects. The binding decodes them before the procedure
  applies them; a string that is not JSON blocks.

| Observation field | Source |
| --- | --- |
| `session_id` | Observed: the step's opaque `agent` reference |
| `agent` | Observed provider, which must be the assigned agent's |
| `role`, `definition_sha256` | Fixed by the admitted step: the literal leaf definition whose bytes the binding pins |
| `skills_loaded` | Fixed by the step and the engine: no `skill` selection, and managed sessions exclude discovered skills |
| `claim` | The correlation key of the run |
| `native` | Observed `model`, when a model was chosen |
| `operations` | Not recorded by engine `8758775` for native tasks |

Without an operation receipt the binding reports `operations: null`, and the
procedure blocks: *the engine recorded no operation receipt for the entry
session, so its read-only boundary is unverified*. So, with this engine, every
onboarding, explanation and saved change blocks before anything is reported or
written. It needs an engine that records the task's tool operations, as the
validator binding does.

## Per-task preference handoff

Installed `dk-pr-fix` passes accepted preferences to occurrences with their
scope ([semantic assessment](semantic.md#installed-workflow)).
[`prfix/preferences.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/preferences.py)
narrows them per task:

- **Prepare.** A scoped preference reaches only the occurrences of its modes:
  writers implement, review adapters review. A read-only occurrence of another
  kind gets none. An unscoped preference reaches every occurrence, as before.
- **Claim.** The claim adds `preferences` when the occurrence has a scoped
  preference: the mode, the PR paths the capture lists, the selected scoped
  preferences with the captured paths each one selects and whether it needs its
  condition, and a `rule`. A reviewer reviews exactly the captured paths, so it
  gets only the preferences that select one of them. A writer may change paths
  the PR does not change yet, so it gets every scoped preference of its mode;
  one that selects no captured path is listed with no paths, and the rule
  applies each listed preference to any path the writer changes that its scope
  includes and does not exclude. A prompt-only preference (empty `include`) is
  kept as conditional. The `binding` covers only the selected records.

The selectors are a verbatim copy of `configuration.py` (a test keeps them
identical). A test checks that a reviewer's records (id, text, scope, paths,
condition) are the ones `resolve()` selects for the captured paths, and that a
writer's are those plus the other preferences of its mode with no paths.
Prompt ids are unique within an occurrence, so ordering by id needs no
`source`. The captured paths and the occurrence's prompts are both in the claim
fingerprint, so an unchanged occurrence keeps its accepted result, and a
changed preference that the occurrence does not receive leaves the binding
unchanged.

## Sessions

Every procedure builds one typed assignment and calls the engine transport's
`native_session(task)`. The assignment holds the exact ask, the role prompt, the
admitted role and digest, `skills: []`, read-only tools, no writes or
publication, the files the session may read and a claim. The engine's observation
must show that claim, role, digest and agent, no loaded skill and only reads of
the listed files. Anything else blocks the procedure, and nothing is applied. A
known claim is reconciled to its original result, so a lost response never starts
a second session.

## Native controls

Agent Workflow agent definitions take an optional `model` and
`reasoning-effort` (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`;
`docs/AGENT_TASKS.md` at agent-workflow main). The three configuration entries
accept the same two choices per run as `controls`: `onboard()`, `explain()`,
`configure()` and `entries.preflight()` take
`controls={'model': ..., 'reasoning_effort': ...}`, each optional.

- `model` is a provider model identifier (`[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}`),
  never free text. `reasoning_effort` must be one of the engine's values.
- Unknown controls, and controls for any other entry, reject before the engine
  is contacted.
- `claude-sdk` rejects a definition reasoning effort, so `reasoning_effort`
  rejects when the session's agent uses that provider. A model alone is
  accepted.
- The session assignment carries the controls as `native`. The engine's
  observation must report the same `native` value; a session that ran without
  them blocks, and nothing is applied.
- Controls are part of the onboarding and explanation claims and of the saved
  request ID, so the same ask with other controls is another claim. Without
  controls, claims and request IDs are unchanged.

Controls choose how the session runs. They are not saved settings and grant no
authority.

## Onboarding

`onboard()` is read-only. It calls the store's `read()` only. It creates no
configuration, ledger, request or gate, and it returns its report without
persisting it.

1. `discover()` inventories `AGENTS.md` and `CLAUDE.md` in any directory (scoped to
   that directory), `CONTRIBUTING.md` in the root, `.github/` and `docs/`,
   `.github/copilot-instructions.md`, `.github/instructions/*.instructions.md`,
   `.cursorrules` and `.cursor/rules/*`. It also includes every file in the
   guidance closure that admission consumes.
2. A source is `selected` when admission consumes it for every run. Other
   sources are `unselected`, and their rules are evidence only. This suite has no
   saved source-selection override yet.
3. The helper sets each source's status: `inspected`, `unreadable` (too large or
   not UTF-8) or `unsafe` (a symlink that leaves the repository). The session can
   mark a source `uninspected`, but it cannot upgrade a status.
4. `view()` reads the saved configuration for the target: each effective option,
   permission and saved entry option (`entries.<entry>.<option>`) with its source
   and configuration path, the prompts and preferences of matching scopes, and
   the inactive scopes.

`check_report()` then checks the session's report:

- Every discovered source is accounted for.
- Every rule cites an inspected source line, or a saved preference's
  configuration path.
- Only selected sources yield a `selected repository rule`.
- Documented facts cite a source line.
- Configuration entries and conflicts cite known paths.

The report is `complete` only when every source was read and the saved
configuration is valid. Otherwise it is `incomplete` and its gaps say why. A root
guidance symlink that leaves the repository rejects the run, as it does for every
admitted run. The outcome records that nothing was saved and that no authority
was granted. `freshness` binds the target, the discovery inventory, the
configuration view and the admission snapshot.

## Explanation

`explain()` is a transient `dk-config` assignment over the configuration view. It
has no request ID, ledger or gate. Its citations must be existing configuration
paths.

## Saved changes

`configure('dk-config' | 'dk-preferences', ...)` runs these steps:

1. Read back every unsettled configuration write in the shared ledger
   ([writes.py](acceptance.md#configuration-writes)) before reading the store.
2. Bind the exact ask to a request ID. The ID covers the entry, target, logical
   run, instructions, entry options and invocation prompts, never the store
   revision. The same words in another logical run are a new request. If the
   ledger holds an `applied` write for this request and the store still has its
   revision, return the receipt with no session; after any later change the ask
   is assessed again. An uncertain (`blocked`) write is never retried in its run.
3. Read the engine's input gates for the request. A waiting gate returns
   `waiting` with no new session or gate. Answered gates become clarifications.
4. Run one session. Its claim binds the request, base revision and answers. The
   session returns `questions` (one gate is opened), `cancelled`, or a
   `proposal` with an exact typed operation list.
5. `apply_operations()` applies only the entry's operations to a copy of the
   saved document:
   - `dk-config`: `add_scope`, `remove_scope` (empty scopes only),
     `set_option`/`unset_option`, `set_permission`/`unset_permission`,
     `set_prompt`/`remove_prompt`, `set_resource`/`remove_resource` and
     `set_entry_option`/`unset_entry_option`. The last pair changes one
     top-level option of an entry with [saved options](alternatives.md#saved-options)
     in the scope's `entries` (currently `dk-issue-alternatives` and
     `dk-pr-alternatives`). The key and value are checked against that entry's
     option schema when the change is applied.
   - `dk-preferences`: `add_scope`, `set_preference` and `remove_preference`, in
     personal scopes or the target repository's scope only.

   Other operations, unknown scopes and invalid values reject, and nothing is
   written. See [resource-backed prompts](#resource-backed-prompts).
6. Admit the exact revision through the shared [structural](admission.md) and
   [semantic](semantic.md) pipeline, against the bundled default plan, with
   [acceptance reuse](acceptance.md). Custom prompts and preferences therefore get
   the shared validator session. Its clarification gates resume as usual.
7. Write exactly the admitted document through `writes.commit()` with the
   request marker and native result in its `admission.evidence_refs`, then read
   it back. If another writer changed the store, compare-and-swap settles the
   intent as `not_applied`. A later call reads the new revision and assesses it
   with a new claim.

### Resource-backed prompts

A saved prompt can take its text from a file (`resource` instead of `text`), as
admission already allows. `dk-config` can now declare that file:

- `set_resource` takes `{root, path, purpose, owner}` and an optional
  `sha256`. The root must be `repository`, the target checkout. The helper reads
  the file without following symlinks and pins its current SHA-256. A proposed
  `sha256` that differs rejects as a changed file. Paths that leave the
  checkout, symlinks and missing files reject.
- `remove_resource` takes `{root, path}`. It rejects while a saved prompt or
  preference still names the file, so remove or change those in the same
  proposal.

The revision then goes through the same admission: the resource digest is
verified against the same bytes, a Markdown resource must declare its local
link closure, and a prompt that uses it is custom input for the shared
validator, which reads the pinned file. `dk-preferences` cannot declare
resources.

A configuration change never grants an effect: a saved permission stays
non-effective until a request authorizes the action.

## Scoped preferences

A saved preference record may declare `applies_to` (`implementation`,
`review`), `include` and `exclude` selectors and a `condition`. Selectors are
`file` (exact), `directory` (recursive, `.` is the repository) or `glob`
(root-anchored; `*` stays within a path component and `**` must be a whole
component). An empty `include` is prompt-only and requires a condition. A record
that declares none of these fields keeps its original meaning: every task and
path.

- **New records.** `dk-preferences` writes the modes explicitly. Omitted modes
  take the entry's `default_applies_to`.
- **Updates.** An update keeps the saved modes, so a changed default never
  reinterprets a saved record.
- **IDs and duplicates.** New records get a stable `pref-` ID. An exact
  duplicate reuses the saved record.
- **Overlaps.** `overlaps()` finds changed records whose modes and selectors may
  overlap another applicable record. It is conservative: globs compare by their
  literal prefix, and conditions are never evaluated. The proposal must settle
  every overlap as `compatible`, with a reason, or as `resolved` by citing the
  user's recorded answers. Otherwise the helper opens a gate that lists both
  records, and nothing is saved. No precedence is ever invented.

These are the invalidation boundaries:

- **Admission.** A scoped preference is a custom input only when the selected
  plan exercises one of its modes: writers implement, review adapters review. A
  review-only preference adds no validator session to a plan without reviewers.
- **Acceptance.** A preference unit binds its scope. Other units keep their
  acceptance when it changes.
- **Task binding.** `resolve(preferences, mode, paths)` selects records for one
  task's mode and paths and returns matched paths and conditional records. Its
  `binding` covers only the selected records, so changing an unrelated preference
  leaves the task's binding unchanged.

## Manual equivalents

Nothing is imported from dk-workflows: no settings, preferences, source choices,
request history or journals. To carry an old setting over, read it in the old
installation and restate it as a new ask:

- For settings and prompts, ask `dk-config`.
- For preferences, ask `dk-preferences`, with concrete selectors and modes.
- Re-establish each permission deliberately. A saved permission still needs a
  per-request authorization.

Old instruction-source overrides have no equivalent yet. Onboarding reports
which sources admission consumes. An old prompt kept in a file can be restated
as a [resource-backed prompt](#resource-backed-prompts) of the target
repository.

## Verification

[`tests/test_configuration_skills.py`](../tests/test_configuration_skills.py)
uses a disposable repository, the real configuration file store and SQLite
ledger, and a fail-closed fake engine. The engine has durable gates, native
claims that reconcile on replay and scripted validator sessions. Any unscripted
session or unconfigured operation fails. Each case asserts its transport call
counts and, where it matters, the number of distinct sessions.

- **Onboarding.** Fresh discovery saves nothing: no configuration or state
  directory, gate or checkout change. The tests also cover unreadable, unsafe
  and uninspected sources; a session that upgrades a status, governs with
  evidence, cites a line outside its source, writes, runs a command, loads a
  skill or runs another role; existing and invalid configuration; and preset,
  permission, skill and authorization expansion.
- **Presets.** The role prompts and options match the baseline. Invocation
  prompts go through the shared validator. `onboard-repository` composes into a
  `dk-pr-fix` plan, and the configuration adapters and repository targets reject
  there.
- **dk-config.** A transient explanation; an exact write with readback; a
  settled request that runs again with no session; clarification and resume
  with no new session; a lost session response reconciled through its claim; a
  lost write response settled by readback; a concurrent writer; a custom prompt
  accepted and rejected by the shared validator; out-of-entry or invalid
  operations that are never written; a resource-backed prompt pinned, assessed
  by the validator on the pinned bytes and read back, a referenced resource
  that cannot be removed alone, and stale, foreign-root, traversing, linked,
  missing and malformed resource declarations; and saved entry options set,
  shown and cleared (in [`tests/test_alternatives.py`](../tests/test_alternatives.py)).
- **dk-preferences.** Explicit modes, stable IDs and readback; duplicate reuse;
  saved modes kept on update; overlaps that need the user, with invented answers
  rejected; disjoint and compatible additions; the personal and repository scope
  limit; and composed PR admission that consumes and then reuses a saved scoped
  preference.
- **Native controls.** Controls reach the assignment and must match the
  engine observation; they change the onboarding claim and the saved request
  ID; a session that ignores them blocks; unknown, malformed and other-entry
  controls reject; and `claude-sdk` rejects only a reasoning effort.
- **Selection.** Mode, path, exclusion, glob and condition selection; a binding
  that stays unchanged when an unrelated preference changes; conservative
  overlap detection; and malformed scopes and request targets.

[`tests/test_configuration_binding.py`](../tests/test_configuration_binding.py)
runs the same procedures through `configuration_binding.EngineTransport` over a
fake engine port. The port records every installed-workflow run, keeps one run
per correlation key and keeps gate records; an unscripted session or validator
run fails. It covers the descriptor rows, `SKILL.md` files and the preset and
`configure.yml` shapes; onboarding that blocks without an operation receipt and
saves nothing; with a **hypothetical** receipt (engine `8758775` records none),
onboarding from the exact source text, a saved change written once on the host
and settled again with no run, a lost response reconciled to the retained run,
questions through one engine gate resumed with a new claim, and the
`dk-preferences` and explanation presets; the model control and a blocked
reasoning effort; and definition, mode, mailbox, provider and output checks.

`tests/test_dk_pr_fix.py` covers the [handoff](#per-task-preference-handoff):
the verbatim selectors, agreement with `resolve()`, a writer keeping a
preference that selects no captured path while a reviewer drops it, mode
narrowing at prepare, and a writer's claim from a relocated read-only install
that receives both implementation preferences, one with its changed path and
one with none, and no review preference.

## What remains

- These procedures run as engine-managed native sessions only once a released
  engine ships Agent Workflow
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129) and
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130). Both are
  closed on upstream main, but no release contains them. No local controller or
  launcher stands in for either.
- Through engine `8758775` every entry session blocks: the engine records no
  operation receipt for native tasks (see [Engine binding](#engine-binding)).
- A per-run `reasoning_effort` blocks in the binding: the engine takes it only
  from an agent definition. A `model` is passed and must be reported back; no
  provider is shown to honor either.
- `plugin test` fixtures are not added: the upstream fixture runner needs a
  case for every bound skill, and the other skills' container jobs are
  unsupported ([packaging](packaging.md#what-remains-blocked)).
- Saved instruction-source overrides have no equivalent yet. Admission consumes
  the root guidance closure for every run, so an override changes what governs
  all runs and needs its own admission design.
- The per-task handoff selects preferences by the PR's captured paths. A
  writer's preference that selects no captured path is applied by the session
  to the paths it changes; nothing checks that. A condition is passed to the
  session, never evaluated.
- Real configuration-agent and validator adherence needs separate authorization.
