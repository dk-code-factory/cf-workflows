# Invocation contract v1

Back to the [contract index](README.md). `kind` is `invocation`.

| Required field | Shape and invariant |
| --- | --- |
| `schema_version`, `kind`, `suite` | `1`, `invocation`, `cf-workflows` |
| `invocation_id`, `logical_run`, `occurrence_id` | Stable opaque IDs; occurrence distinguishes repeated tasks without resetting run budgets |
| `skill` | Exact `cf-workflows:NAME` inventory entry, never a legacy alias masquerading as native parity |
| `request` | Stable `id`, `source`, exact `original_instructions`, original artifact refs and explicit authorization refs |
| `target` | Canonical host/repository identity, PR/issue when applicable, exact `base`, `head`, `target_ref`; issue-only analysis has no invented PR |
| `host`, `provider` | Separate selected host identity and provider binding with capabilities and evidence mode |
| `inputs`, `resources` | Typed input refs/digests and complete immutable admitted resource refs/digests |
| `configuration` | Effective configuration revision, admission refs and relevant context/resource/capability digests |
| `plan` | Explicit selected tasks/finalizers, ordered stages, reviewer groups, required/optional flags and applicability |
| `permissions` | Effective prohibitions and exact scoped authorization refs, not inferred from plan membership |
| `budget_ref`, `state_ref` | Original retained application budget and recovery record identities |
| `continuation` | `new`, `resume`, `retry` or `checklist`; original claim/session refs when continuing |

Preflight precedes claims, review-cycle consumption and substantive execution.
Verify actual selected host/provider requirements, immutable resources, accepted
configuration and native observation support. Unsupported paths reject; an
otherwise supported native task waits for the attached host. Helpers cannot
start another model session. The native coordinator must invoke the installed
skill with its lifecycle, required independent children and deeper looks in the
same sessions where required. Original terminal outputs and claim-bound host
observations are evidence; a report merely claiming child execution is not.
If a supported host lacks command logs, a declared reported-command evidence
mode must still retain actual child identities and successful native terminal
observations and clearly distinguish reported from host-observed commands.
Missing required evidence leaves the task incomplete.

`resume`, retry and checklist continuation preserve original instructions,
occurrence, session/claim binding, resources and allowances. Successful siblings
remain reusable if their relevant dependencies are unchanged. A renewed explicit
bounded request retains its own source/identity and original exhausted history;
a new session alone cannot replenish budget. Third-party lifecycle completion
runs in finalization, including failures; parent completion waits for children.

Private provisional analysis may use a pinned local snapshot while PR metadata
reconciles, with the mismatch recorded. It cannot satisfy external readiness,
post findings or dispatch publication until current identity reconciles. Current
alignment can refresh against new requirements without a historical rejection
receipt and without repeating settled architectural comparisons. Only a distinct
direct manual alternatives request permits a fresh PR comparison; changed
fingerprints, guidance or head alone do not fabricate such authorization.

For feedback, record implementation disposition, concrete author response
obligation and thread-resolution status independently. Settled applicable replies
after publishing fixes precede the first independent review barrier; reviewer
failure cannot strand an already-supported answer. Questions for other
participants do not invent author obligations. A declined code edit does not
erase a concrete author answer; partial mixed-thread answers do not authorize
resolution of the whole thread.
