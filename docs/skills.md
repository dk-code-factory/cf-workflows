# Supplied skills

A user can have one selected task occurrence performed by their own skill
instead of the baseline adapter. [`cf_workflows/skills.py`](../cf_workflows/skills.py)
holds the suite's side of this: which session loads which admitted skill, and
which engine-observed evidence covers the occurrence. Agent Workflow
[#139](https://github.com/dk-code-factory/agent-workflow/issues/139) owns the
rest: capturing the admitted bytes, loading them as a session-scoped plugin,
hiding host, repository and bundled skills, and recording the provider event
stream. That work is not available yet, so preflight still rejects every plan
that selects a supplied skill (see [What remains blocked](#what-remains-blocked)).

## Selecting a skill

A scope's `skills` array declares the skill as files pinned by digest (see
[admission](admission.md)). A task selection names it by ID:

```json
{"task": "review", "skill": "lint-skill"}
```

The skill record may declare `children`, for example
`[{"agent_type": "Explore", "count": 2}]`. Each entry is a permitted agent type
and the exact number of children of that type the skill must run. Without a
declaration, no child agent is permitted. Host-installed fields (`host`,
`installed_version`, `qualified_name`) reject, so a skill from the user's Codex or
Claude host is never used.

A plan that selects a skill needs semantic assessment ([semantic.md](semantic.md))
and these provider capabilities before any claim:

| Capability | Required when |
| --- | --- |
| `session_scoped_skills` | Any selected occurrence has a skill |
| `observed_skill_evidence` | Any selected occurrence has a skill |
| `observed_child_agents` | A selected skill declares children |

A provider that lacks one rejects structurally. Plans without supplied skills
need none of them.

## Session bindings

`session_plan(admitted)` returns one binding per selected occurrence. It reads
only the admitted snapshot, so changing or removing the skill source after
admission changes nothing. Each binding has:

- `occurrence`: the installed workflow's stable occurrence ID, computed exactly
  as `prepare.py` does;
- `skills`: `[]`, or the one admitted skill with its digest, files and
  `namespace` (`cfw-skill-` plus the first 32 hex digits of its digest);
- `controls`: whether the session gets the skill tool and the agent tool, and
  which agent types are permitted;
- `evidence`: the required invocation, children and terminal outcome;
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
the skills loaded into that session, the provider's events and the session's
terminal outcome. Each loaded skill has a `name`, `sha256` and `locator`. The
admitted skill must be registered under its namespace: as the Claude skill
`<namespace>:<name>`, or as an absolute Codex path ending in `<namespace>/<name>`.
An invocation counts only when its full locator equals that registration, so a
same-named host, repository or other plugin skill never counts. These event
sources are read:

| Provider | Invocation | Children and their parents | Child outcome |
| --- | --- | --- | --- |
| Codex | the engine's `turn/start` `skill` input | `collabAgentToolCall` (`spawnAgent`, `senderThreadId`) | `subAgentActivity` started/completed |
| Claude | `Skill` tool use | `Agent` tool use and `parent_tool_use_id` | `task_started`/`task_notification` |

| Status | Meaning |
| --- | --- |
| `covered` | Every session loaded exactly the admitted skills. The skill was invoked, its latest invocation ran exactly the declared children to completion, and the last session completed. |
| `incomplete` | Evidence is missing or does not match: no invocation, missing, duplicate or unfinished children, a failed last session, or only report material. |
| `failed` | An unadmitted or same-named skill with another digest was loaded, the admitted skill was registered outside its namespace or was missing from a session, another locator was invoked, or an undeclared or nested child started. |
| `reconcile` | The last session was interrupted, its outcome is unknown, or a writing occurrence failed. Its original effects must be reconciled first. |

The result's `reconcile` flag is true whenever reconciliation is needed, even
when a violation in the same session makes the status `failed`.
`attempt_allowed` is false while that flag is set and once the occurrence is
covered.

Records from any other source are ignored, so a report that claims an invocation
or children never establishes coverage. A resumed or checklist session must show
the skill re-registered: Codex needs the extra root again on the new process, and
Claude needs the same `plugins` and `skills`. Without it the occurrence fails
visibly instead of completing without the skill. Readiness keeps its own gate in
[dk-pr-fix](dk-pr-fix.md#budgets-occurrences-and-continuation); coverage of a
skill occurrence never implies it.

## What remains blocked

- [Agent Workflow #139](https://github.com/dk-code-factory/agent-workflow/issues/139):
  admitting the skill bytes into engine state, loading them into one session
  (Codex extra root and `skill` input; Claude local plugin, `skills` allowlist
  and `Skill` tool), discovery isolation, re-registration on resume, and the
  session records. The record shape and capability names here are this
  suite's contract; they must be aligned with #139 when it lands.
- Until then `admission.SUPPLIED_SKILL_EXECUTION` stays false and preflight
  rejects a selected skill after structural checks and before any claim.
- The installed workflow does not carry bindings yet. Its occurrence
  fingerprints do not bind a skill digest, and no YAML step passes a binding to
  the engine. Both need the engine's input form from #139. Budgets and claims
  are keyed by the same occurrence IDs, so they are retained when a skill's
  bytes change.

The tests use fake provider gateways for both providers and a fail-closed fake
engine transport. They do not show that a real provider loads, isolates or
follows a supplied skill; that needs separately authorized live evidence.
