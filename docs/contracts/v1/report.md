# Report contract v1

Back to the [contract index](README.md). `kind` is `report`.

| Required field | Shape and invariant |
| --- | --- |
| `schema_version`, `kind`, `suite` | `1`, `report`, `cf-workflows` |
| `invocation_id`, `logical_run`, `occurrence_id` | Bind original admitted assignment, not a reconstructed prompt |
| `pins`, `input_digest`, `configuration_ref`, `resource_digest`, `capability_digest` | Exact evidence freshness bindings |
| `status` | `complete`, `incomplete`, `waiting`, `blocked`, `failed` or `rejected` |
| `selected_plan_complete` | Boolean for this selected plan only |
| `readiness` | Object with `ready` boolean, current `head`, gate evidence refs and blockers |
| `provisional` | Boolean plus `reason` and mismatched identity refs when true |
| `coverage` | One record per required/optional selected task, checks, result refs, omitted/incomplete reason |
| `findings`, `dispositions`, `evidence_refs` | Structured supported findings, verification/response decisions and original evidence refs |
| `native_execution_refs`, `publication_refs`, `budget_ref` | Original claim-bound observations, exact intent/receipts and consumed allowance |
| `blockers`, `next_actions` | Private operational reasons and bounded original recovery references |

A completed empty plan does not establish ordinary review or merge readiness.
A provisional report is explicitly pinned private analysis and always has
`readiness.ready:false`. Waiting for a supported host or CI is unfinished work;
unsupported capability rejection differs from an uncertain prior effect.
Readiness is current evidence, never an evergreen cached boolean. Required gaps
keep the corresponding completion condition false; optional omissions must be
reported even if selected-plan completion permits them.

Each finding includes stable source/finding ID, category, exact contextual claim,
trigger/consequence, provenance and verification disposition. Inline publication
requires a real current changed-line anchor with revision-backed proof. A
verified unanchorable metadata finding belongs in the review body even when
optional summaries are disabled. Missing evidence withholds the affected finding.
Partial coverage may publish independently verified findings as COMMENT when
authorized; it must not claim a clean review or APPROVE. Diagnostics and coverage
limitations stay private; never replace a repository answer with a workflow
failure message in public text. Preserve source attribution and original thread
membership for replies. Resolve only actual inline threads with a verified
supported answer and a separate applicable grant.

Store compact statuses and targeted evidence references. Preserve original
native outputs and provenance needed for verification/recovery rather than
rewriting them into duplicate transcripts. Process/token counts are cost proxies;
report complete API dollar cost including later sessions, model and service-tier
pricing only when measured data is available. Unknown cost remains unknown.
