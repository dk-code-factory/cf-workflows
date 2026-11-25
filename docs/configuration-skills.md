# Configuration, preferences and repository onboarding

`cf-workflows:dk-repo-onboard`, `cf-workflows:dk-config` and
`cf-workflows:dk-preferences` are thin entries over the shared workflow
(#26). [`cf_workflows/entries.py`](../cf_workflows/entries.py) defines them and
[`cf_workflows/configuration.py`](../cf_workflows/configuration.py) holds their
adapter procedures. Like the other library operations, they are not wired into an
installed workflow yet (see [What remains](#what-remains)).

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
| `dk-config` | `configure` (configuration) | none | `view`, `explain`, `apply_operations`, admit, write, readback | Settings and prompts |
| `dk-preferences` | `admit-preferences` (configuration) | `default_applies_to` | `view`, `apply_operations`, `overlaps`, admit, write, readback, `resolve` | Preferences |

The role prompts are the baseline `prompts/default.md` texts, byte for byte. Their
digests match [the inventory](../baselines/inventory.json). The bundled adapters
are control text in `admission.ENTRY_ADAPTERS`, like the baseline adapters. So an
entry's default plan has no custom input, runs no validator session and loads no
skill. An invocation may add only `prompts`; these pass the shared
[semantic assessment](semantic.md). Tasks, finalizers, reviewers, permissions,
skills and unknown entry options reject before the engine is contacted. A
remote-write authorization in the request rejects too: enabling a permission is a
saved setting, not an authorization.

The entry's own run is admitted from bundled defaults and invocation prompts
only. Saved configuration is what the entry inspects or changes, not its governing
input. So an invalid saved document can still be reported and repaired.

A request targets a pull request (`pr`) or a whole repository (`repository`,
canonical `host/owner/name`). A repository target has no invented PR: `pr`
scopes do not match it, and `dk-pr-fix` preflight rejects it.

Composition: a `dk-pr-fix` plan may select `onboard-repository` as a read-only
task. It is admitted like any baseline adapter and passed to the installed
workflow's inputs. The two configuration adapters reject outside their entries.

## Sessions

Every procedure builds one typed assignment and calls the engine transport's
`native_session(task)`. The assignment holds the exact ask, the role prompt, the
admitted role and digest, `skills: []`, read-only tools, no writes or
publication, the files the session may read and a claim. The engine's observation
must show that claim, role, digest and agent, no loaded skill and only reads of
the listed files. Anything else blocks the procedure, and nothing is applied. A
known claim is reconciled to its original result, so a lost response never starts
a second session.

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
4. `view()` reads the saved configuration for the target: each effective option
   and permission with its source and configuration path, the prompts and
   preferences of matching scopes, and the inactive scopes.

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
2. Bind the exact ask to a request ID. The ID covers the entry, target,
   instructions, entry options and invocation prompts, never the store revision.
   If the ledger already holds an `applied` write for this request, return its
   receipt with no session. An uncertain (`blocked`) write is never retried.
3. Read the engine's input gates for the request. A waiting gate returns
   `waiting` with no new session or gate. Answered gates become clarifications.
4. Run one session. Its claim binds the request, base revision and answers. The
   session returns `questions` (one gate is opened), `cancelled`, or a
   `proposal` with an exact typed operation list.
5. `apply_operations()` applies only the entry's operations to a copy of the
   saved document:
   - `dk-config`: `add_scope`, `remove_scope` (empty scopes only),
     `set_option`/`unset_option`, `set_permission`/`unset_permission` and
     `set_prompt`/`remove_prompt`.
   - `dk-preferences`: `add_scope`, `set_preference` and `remove_preference`, in
     personal scopes or the target repository's scope only.

   Other operations, unknown scopes and invalid values reject, and nothing is
   written.
6. Admit the exact revision through the shared [structural](admission.md) and
   [semantic](semantic.md) pipeline, against the bundled default plan, with
   [acceptance reuse](acceptance.md). Custom prompts and preferences therefore get
   the shared validator session. Its clarification gates resume as usual.
7. Write exactly the admitted document through `writes.commit()` with the
   request marker and native result in its `admission.evidence_refs`, then read
   it back. If another writer changed the store, compare-and-swap settles the
   intent as `not_applied`. A later call reads the new revision and assesses it
   with a new claim.

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
which sources admission consumes.

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
  accepted and rejected by the shared validator; and out-of-entry or invalid
  operations that are never written.
- **dk-preferences.** Explicit modes, stable IDs and readback; duplicate reuse;
  saved modes kept on update; overlaps that need the user, with invented answers
  rejected; disjoint and compatible additions; the personal and repository scope
  limit; and composed PR admission that consumes and then reuses a saved scoped
  preference.
- **Selection.** Mode, path, exclusion, glob and condition selection; a binding
  that stays unchanged when an unrelated preference changes; conservative
  overlap detection; and malformed scopes and request targets.

## What remains

- These procedures run as engine-managed native sessions only once Agent Workflow
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129) and
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130) land. The
  installed graph is PR-targeted, so running a repository-targeted entry through
  it also needs workflow inputs for a repository target. No local controller or
  launcher stands in for either.
- Plugin skill entries (`SKILL.md` and descriptor rows) wait for an installed
  workflow that runs them ([packaging](packaging.md)).
- These features have no equivalent yet: saved instruction-source overrides,
  resource-backed prompt edits through `dk-config`, per-task preference handoffs
  in the installed `dk-pr-fix` workflow (which still rejects custom inputs), and
  native model controls per entry.
- Real configuration-agent and validator adherence needs separate authorization.
