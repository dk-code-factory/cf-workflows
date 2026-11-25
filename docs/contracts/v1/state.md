# Application-state contract v1

Back to the [contract index](README.md). `kind` is `application_state`.

| Required field | Shape and invariant |
| --- | --- |
| `schema_version`, `kind`, `suite` | `1`, `application_state`, `cf-workflows` |
| `repository`, `logical_run`, `revision` | Stable canonical identity, original run and compare-and-swap revision |
| `original_request_ref`, `admission_refs` | Original instructions, authorization and accepted input bindings |
| `engine_refs` | Engine run, admitted closure, native claim/session and result references; never a competing scheduler |
| `occurrences` | Stable task keys, selected plan, dependencies, pins, status, original output refs and accepted checklist refs |
| `budgets` | Per-run consumed/reserved review, fix, retry, checklist, CI repair/rerun allowances and absolute deadlines |
| `feedback` | Separate implementation dispositions, concrete response obligations and resolution evidence ([feedback.md](../../feedback.md), [responses.md](../../responses.md)) |
| `decisions`, `alignment` | Original selections/sourced resolutions, current commitments and freshness bindings ([alignment.md](../../alignment.md)) |
| `publications` | Exact-intent journal refs, original request, actor, target/payload digests and uncertainty/receipt status |
| `upgrade` | Original version/ref, target version, transaction status and recovery refs, or null |

The suite retains application evidence; the engine owns durable execution and
immutable admitted assets. No local task scheduler or provider session loop is
introduced. State writes are atomic and serialized or guarded by revision
compare-and-swap. Concurrent stale writers reread before modifying state.
Reserve effect/budget identities durably before dispatch; interruption does not
remove reservations or reset counts. Zero allowances disable attempts. Retain
absolute CI deadlines across polls, head refreshes and later sessions under the
same original wait scope; changed scope needs an explicit recorded decision.

Reuse a completed occurrence only while its consumed instructions, target pins,
configuration, resources, capability requirements and evidence freshness remain
valid. Relevant changes invalidate affected dependents, not successful unrelated
siblings. Preserve original result/history/claim IDs on invalidation; do not
replenish run limits. Head-sensitive review, CI and publication readiness must
be refreshed even when a historical architectural selection stays settled.
Capacity-one scheduling must preserve the same independence and ordering;
parallelism is never required to recover successfully.

## Exact-intent publication

Record immutable intent before dispatch: stable action ID, original request and
authority refs, actor, canonical target, head/base/ref leases where relevant,
exact payload and digest, action kind, and prerequisite evidence. Journal states
are `prepared`, `pending`, `applied`, `not_applied`, `blocked`.
`pending` means dispatch may have occurred; process failure or timeout alone
does not settle it. Reconcile the original intent and effects read-only before
retry. An unchanged branch, absent reviewer, missing response or ambiguous 404
alone proves neither delivery nor non-delivery. Never change action IDs or
redirect targets to bypass uncertainty. Settings changes do not rewrite pending
intent. A conclusive rejection may settle `not_applied`, preserving original
request/failure; a corrected fresh intent is separate, not replay of the terminal
operation. Pending-review rejection must not delete or modify unrelated drafts.

For applied replies later edited/unavailable, preserve the original receipt and
reassess the present answer obligation. A supported `no_replacement` disposition
can settle that obligation; `replacement_required` needs a fresh authorized
linked intent. Neither disposes an uncertain original submission or silently
authorizes thread resolution. Pending review requests remain protected even
when an independently authorized reply proceeds. Every merge rechecks current
identity, review/verification, CI, outstanding user/team requests and repository
requirements immediately before dispatch.

## Minimal retention and recovery

Persist only useful analysis and required recovery/evidence: original outputs,
accepted checklists, source references, exact effects, consumed budgets and
binding digests. Duplicate transcripts, rendered configuration copies and derived
summaries need a demonstrated reuse benefit; they are not default state.
Unchanged fresh-session work should need targeted reads, not repeated agents.
Backups and restores preserve original admitted assets and uncertainty without
importing another suite's settings/state. Plugin replacement/deletion, host
reconnect, partial failure, result/claim loss and interrupted upgrades must
reconcile the original evidence before dispatch; fixtures assert invocation
counts. Unsupported or missing original assets block recovery rather than using
current replacement prompts. Qualification of these requirements belongs to
#37; these contracts alone do not claim recovery is implemented.
