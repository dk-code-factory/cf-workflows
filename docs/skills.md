# Supplied skills

A user can have one selected task occurrence performed by their own skill
instead of the baseline adapter. [`cf_workflows/skills.py`](../cf_workflows/skills.py)
holds the suite's side of this: which session loads which admitted skill, and
which engine-observed evidence covers the occurrence. Agent Workflow
[#139](https://github.com/dk-code-factory/agent-workflow/issues/139) owns the
rest: capturing the admitted bytes, loading them into an engine-owned plugin,
hiding host, repository and bundled skills, and retaining the provider events.
Its documented profile ([`docs/AGENT_TASKS.md`](https://github.com/dk-code-factory/agent-workflow/blob/87587758fdb3633db744aa54d0380f31774fb1e3/docs/AGENT_TASKS.md))
is on Agent Workflow main but in no release, and it admits a selection only in
an entry workflow. Preflight therefore still rejects every plan that selects a
supplied skill (see [What remains blocked](#what-remains-blocked)).

Admission accepts only the shapes that profile admits, so nothing passes here
that the engine would reject later.

## Selecting a skill

A scope's `skills` array declares the skill as files pinned by digest (see
[admission](admission.md)). A task selection names it by ID:

```json
{"task": "review", "skill": "lint-skill"}
```

The skill directory must match the upstream `{directory: /absolute/path}`
skill input:

- It contains no symlinks. Its only skill entry is a top-level `SKILL.md`; a
  nested `SKILL.md` or any other `entry` rejects.
- `SKILL.md` frontmatter has exactly `name` and `description`, each a
  single-line YAML string. Authority-changing metadata such as `allowed-tools`
  or `model` rejects, and so do block scalars, flow collections and plain
  values that YAML resolves to a boolean, number or null. Quote such values.
- The frontmatter `name` equals the record's `name`: lowercase words joined by
  single hyphens, at most 64 bytes.
- `SKILL.md` is valid UTF-8 and at most 64 KiB after Go's JSON string escaping.
  `<`, `>` and `&` count six bytes each.
- Every declared file other than `SKILL.md` becomes a selected resource, at
  most 128 of them.

Admission does not accept the upstream `{package: {root, digest, skill}}` form;
a skill is always a directory pinned by its file digests.

The record may add literal `arguments`, a string of at most 64 KiB without NUL
or `${{` expressions. It may also declare `children`: at most one required
child in Agent Workflow's constrained Claude profile.

```json
{"name": "review", "prompt": "Review the retained reference and return the full result.",
 "description": "Review retained reference", "resources": ["refs/rules.md"]}
```

`name` follows the skill-name pattern in at most 48 bytes. `prompt` and
`description` are literal text, and the description is at most 512 bytes.
`resources` lists distinct declared files other than `SKILL.md`, at most 32 of
them. The profile fixes the rest, so a declaration that sets it rejects:
`tools: [Read]`, `model: inherit`, `max-turns: 2`, `max-calls: 1` and
`background: false`. Without a declaration, no child agent is permitted.
Host-installed fields (`host`, `installed_version`, `qualified_name`) reject, so
a skill from the user's Codex or Claude host is never used.

## Runtime profiles

A plan that selects a skill needs semantic assessment ([semantic.md](semantic.md)).
Every deployed runtime may run the selected occurrence, so each one must use a
provider that #139 supports and offer that provider's profile before any claim.
The profile names are the ones upstream pins in `pkg/clirunner/skill.go`:

| Runtime provider | Required profile | Rejects |
| --- | --- | --- |
| `claude-sdk` | `claude-sdk@0.3.322/local-skill-v1` | |
| `claude-sdk`, skill declares a child | `claude-sdk@0.3.322/sonnet46-one-read-child-v1` | |
| `codex-app-server` | `codex-app-server@0.167.1/local-skill-no-delegation-v1` | children, nonempty `arguments`, a `sandbox` other than `read-only` |
| any other provider | none | every supplied skill |

A provider outside the table, a runtime with `execution.mode: container`, a
Codex child, Codex arguments or a Codex `sandbox` other than `read-only` (unset
is allowed) reject before the engine is queried. A missing profile rejects after the one capability query.
The required profiles are recorded per runtime in the snapshot as
`capabilities.skill_profiles`. Plans without supplied skills need none of this.

Some upstream controls cannot be checked here, because the installed workflow
does not choose them yet or the deployment cannot express them. These include
the pinned model (`claude-sonnet-4-6` for the child profile), no reasoning-effort
override and the Claude role ceiling (only the `Read` tool and no MCP grants).
The engine enforces them when it admits the step.

## Session bindings

`session_plan(admitted)` returns one binding per selected occurrence. It reads
only the admitted snapshot, so changing or removing the skill source after
admission changes nothing. Each binding has:

- `occurrence`: the installed workflow's stable occurrence ID, computed exactly
  as `prepare.py` does;
- `skills`: `[]`, or the one admitted skill with its digest, files, resources
  and arguments. It also names the engine-owned `plugin` (`aw-<name>`) and the
  `qualified` Claude command (`aw-<name>:<name>`);
- `controls`: whether the session gets the skill tool and the agent tool, and
  the permitted child agent type (`aw-child-<name>`);
- `evidence`: the required invocation, child and terminal outcome;
- for a selected occurrence only: `input`, the admitted directory for the
  `type: skill` workflow input, as `{directory: {root, path}}` relative to a
  named root (it is resolved to the upstream absolute `{directory: /abs}` when
  the workflow is wired); `selection`, the `with.skill` block (`input`,
  `arguments`, `resources`, `evidence: {invocation: required, children}`,
  `children`); and `output`, which is `{format: json, repair-attempts: 0}`
  because #139 rejects output repairs for a selected skill;
- `publication: false`: a skill never publishes. Effects go through the helper
  intents;
- `binding`: the digest of all of the above.

Only the selected occurrence loads its skill. Sibling bindings are identical to
the same plan without the skill, so their completed work stays reusable. When a
skill performs a review, `verification` names the separate baseline verifier
session (`finding_verification`, no skills) that must verify its findings.

## Evidence and coverage

`coverage(binding, records)` decides an occurrence from the engine's records of
its sessions: the original one, then every `resume`, `retry` or `checklist`
continuation. Each record (`kind: session_observation`, `source: engine`) carries
the skills loaded into that session, the provider's original events and the
session's terminal outcome. This record envelope is this suite's contract; the
events inside it are the native ones #139 retains. Each loaded skill has a
`name`, `sha256` and `locator`. The admitted skill must be registered in its
engine-owned plugin: as the Claude command `aw-<name>:<name>`, or as an absolute
Codex path ending in `skills/<name>/SKILL.md` under the retained root. The
engine owns that root, so a Codex locator is checked by shape and the admitted
digest identifies the skill. An invocation counts only when its locator equals
that registration, so a same-named host, repository or other plugin skill never
counts. These event sources are read:

| Provider | Invocation | Children |
| --- | --- | --- |
| Claude | `Skill` tool use with only `skill` and `args`, completed by its own successful tool result (`success`, matching `commandName`); `args` must equal the admitted arguments | `Agent`/`Task` tool use whose `subagent_type`, `prompt`, `description` and `run_in_background: false` equal the declaration, `parent_tool_use_id`, then `task_started`/`task_notification` |
| Codex | the native `skills.selected_skill_instructions` user fragment on the session's thread and turn, with `skills` provenance and the exact admitted `SKILL.md` bytes. A `turn/start` `skill` input is only a request. | none: any `collabAgentToolCall` or `subAgentActivity` fails, because the profile disables delegation |

| Status | Meaning |
| --- | --- |
| `covered` | Every session loaded exactly the admitted skills. The skill was invoked, its latest invocation ran the declared child once to completion, and the last session completed. |
| `incomplete` | Evidence is missing or does not match: no invocation (including a Claude call without its successful result), a missing, duplicate or unfinished child, a failed last session, or only report material. |
| `failed` | An unadmitted or same-named skill with another digest was loaded, the admitted skill was registered outside its plugin or was missing from a session, another locator, other arguments or other instruction bytes were invoked, a malformed Codex fragment was seen, or an undeclared, differently controlled, nested or Codex child started. |
| `reconcile` | The last session was interrupted, its outcome is unknown, or a writing occurrence failed. Its original effects must be reconciled first. |

The result's `reconcile` flag is true whenever reconciliation is needed, even
when a violation in the same session makes the status `failed`.
`attempt_allowed` is false while that flag is set and once the occurrence is
covered.

Records from any other source are ignored, so a report that claims an invocation
or children never establishes coverage. A resumed or checklist session must show
the skill re-registered. Codex needs the retained root registered again on the
new process; it emits no new fragment, so the original invocation still counts.
Claude needs the same plugin. Without it the occurrence fails visibly instead of
completing without the skill. Readiness keeps its own gate in
[dk-pr-fix](dk-pr-fix.md#budgets-occurrences-and-continuation); coverage of a
skill occurrence never implies it.

## What remains blocked

- The installed workflow cannot carry a selection yet. #139 admits `with.skill`
  only in the entry workflow and only on one literal agent definition. It
  rejects selections in reusable workflows ("skill selections in reusable
  workflows are not certified"). dk-pr-fix dispatches each occurrence through
  the reusable `native.yml` with `agent: ${{ inputs.agent }}`. Wiring the
  binding, and binding the skill digest in the occurrence fingerprint, needs
  that upstream capability first. Until then
  `admission.SUPPLIED_SKILL_EXECUTION` stays false, and preflight rejects a
  selected skill after structural checks and before any claim. Budgets and
  claims are keyed by the same occurrence IDs, so they are retained when a
  skill's bytes change.
- No released engine contains #139. The latest tag is v0.2.1, and the plugin
  pins the unreleased 0.2.2.
- The suite asks for at most one child. A coordinator with several required
  passes needs a broader upstream child profile.

The tests use fake provider gateways for both providers and a fail-closed fake
engine transport. They do not show that a real provider loads, isolates or
follows a supplied skill; that needs separately authorized live evidence.
