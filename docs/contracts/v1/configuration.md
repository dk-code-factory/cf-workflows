# Configuration contract v1

Back to the [contract index](README.md). `kind` is `configuration`. This contract
is the input to admission. [Structural admission](../../admission.md) and
[semantic assessment](../../semantic.md) are implemented as library operations;
acceptance reuse and configuration writes are not.

## Required shape and resolution

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version`, `kind` | integer, string | `1`, `configuration` |
| `suite` | string | Exactly `cf-workflows` |
| `revision` | string | Observed store revision used for compare-and-swap writes |
| `scopes` | ordered array of objects | Each has `id`, `selector`, `options`, `permissions`, `prompts`, `skills`, `preferences` |
| `resources` | array of objects | Each has immutable `root`, normalized `path`, `sha256`, `purpose`, `owner` |
| `admission` | object | `structural`, `semantic`, `request_id`, `context_digest`, `resource_digest`, `capability_digest`, `evidence_refs` |

Selectors identify personal, repository, upstream, PR or invocation scope with
canonical stable identities; path selectors include explicit includes/excludes
and optional applicability conditions. Define option precedence as bundled suite
defaults, personal settings, repository settings, explicit upstream scope when
supplied, PR settings, then invocation-local options. Apply only matching scopes.
Within the same precedence, overlapping contradictory values require resolution,
not arbitrary order. Preserve explicit empty lists and null: an empty selected
plan means no tasks in that slot; null has only the option schema's declared
meaning and never silently restores an omitted task. Original additional user
instructions remain separate from behavior replacement and additions.
Preferences have no automatic priority winner; resolve contradictory applicable
instructions before admission. Omitted preference application modes default only
for new records, never reinterpret existing records after defaults change.

Prompts include custom task text, reviewer text, finalizers, assessment/evaluation,
selection, reconciliation, reply/cleanup/commit policy, behavior replacements and
additions, completion instructions and all other prompt-bearing options. Each
selected skill record identifies its qualified name, installed version, host,
entry resource, full required resource closure, supported options, coordinator,
required child passes, lifecycle and deeper-look commands. Resolve transitive
prompt/skill inputs, not just strings with recognized option names. Missing
resources, unknown options, invalid types, cycles, unsafe paths and incomplete
closures reject structural admission. Retain provenance for every effective value.

## Bootstrap, semantic admission and clarification

Bundled typed read-only discovery/default inspection may run before an accepted
configuration. It grants no substantive prompt execution or remote write. The
bundled configuration agent and its fixed transport contract form the bootstrap
boundary: user-selected replacement instructions cannot validate themselves or
weaken the admission rules. Resolve its actual supported native capability before
claiming a semantic attempt; no helper model launcher substitutes for the host.

Every custom prompt and selected skill passes structural checks and a fresh
native configuration-agent semantic assessment before acceptance or substantive
execution. Assessment consumes the exact original ask, all effective inputs,
permitted/required behavior, explicit prohibitions, relevant repository/host/user
instructions, earlier decisions and capability/resource bindings. Verdict is one
of `accepted`, `needs_clarification`, `rejected`, `blocked`; structural status is
`pending`, `accepted`, `rejected`. The configuration agent must explain concrete
contradictions/uncertainty with source references; acceptance is not a claim that
a model verdict can override prohibitions. Prompt text, selected skills and
validator verdicts cannot grant authority or waive a prohibition.

A durable `request_id` binds the exact ask and proposed changes to their observed
store revision and input/context/resource/capability digests. Retain questions,
answers and original native result references. Clarify through supplemental
instructions, a supported configuration change or rejection. Resume the same
request after interruption; reconcile an existing native claim before dispatch.
Once concrete intent and contradictions are settled, apply the exact admitted
revision without an additional routine approval step. Concurrent/stale revision
requires rereading/reassessment of affected inputs, not blind retry. Reconcile
uncertain writes by readback before a new write.

Reusable acceptance requires all relevant bindings to remain current, including
original instructions and consumed repository sources, effective configuration,
selected installed resources/versions and required capabilities. Refresh only
changed dependencies; preserve completed unaffected work, earlier decisions and
consumed budgets. A different session is neither new authority nor new budget.

## Permissions

All remote-write permissions default false. Separate action keys are `push`,
`edit_pr`, `reply`, `resolve_thread`, `request_review`, `create_pr`, `create_issue`,
`edit_issue`, `post_findings`, `approve_fork_ci`, `rerun_ci`, `approve_pr`, `merge`.
An enabled setting is not a blanket dispatch grant. Each effect also requires an
accepted scoped authorization from an explicit request or saved authorized
policy, binding action, canonical target, actor, payload constraints and head
where applicable. Prohibitions and later narrower instructions remain effective.
An iteration, analysis task, selected finalizer, selection result or cached clean
review grants no additional write scope. Local edits, commits and history rewrites
also require their applicable user authorization, independently of remote writes.
PR approval, CI execution approval, deployment gates, CI success and merge are
separate. Merge requires current clean independent review/verification, exact-head
CI, satisfied repository requirements and no outstanding applicable user/team
review requests, including renewed requests after same-head approval. No setting
or direct merge request bypasses readiness. A separately explicit merge request
is retained across routed workflows under default no-automatic-merge settings;
no repeated confirmation is required merely because the workflow resumed.
