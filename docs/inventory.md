# Complete skill inventory

Every row is a separate acceptance obligation under the `cf-workflows` namespace.
The [acceptance matrix](qualification.md) traces each row to its deterministic
evidence and compatibility status.
All are **not implemented** in this contracts-only revision. The structured
[inventory](../baselines/inventory.json) records each row's inputs, outputs,
permissions, dependencies, completion conditions, intentional differences and
both-host entry/direct-reference resources with exact SHA-256 hashes. Resource
paths refer to the [pinned baseline](baselines.md), not files shipped here.

Shared configuration admission, instruction-source priority, original user
constraints, private reporting, native execution evidence and recovery contracts
apply to every row. A row's permissions describe its maximum task scope, never
an automatic grant. All remote writes default off in this successor, including
upstream paths whose defaults allowed some writes. Direct and composed scenarios
remain pending for every row; a compatibility entry must itself be exercised
and cannot establish implementation of its delegate.

The three configuration rows (#26) have entry presets, adapter procedures and
deterministic direct and composed fixtures ([configuration skills](configuration-skills.md)).
They stay `not_implemented` until they run as engine-managed sessions through an
installed workflow. The same holds for the two prose rows (#33, [prose cleanup](prose.md))
and the four alternatives rows (#27, [alternatives](alternatives.md)).
`dk-pr-decisions` (#28, [decisions](decisions.md)) has an installed preset and
is listed in the plugin descriptor, but it stays `not_implemented` until it runs
as an engine-managed session through a released engine.

The two publication rows (#31, [publication entries](publication-skills.md))
are listed in the plugin descriptor with their own installed entry workflows,
`publish.yml` and `review-post.yml`, like `dk-pr-review`. They also stay
`not_implemented`: no released engine has run those workflows as
engine-managed sessions.

The three feedback rows (#30, [feedback skills](feedback-skills.md)) are
listed in the plugin descriptor with their own installed entry workflows over
`dk-pr-fix`. They stay `not_implemented` until they run as engine-managed
sessions through a released engine.

The three reviewer rows (#29, [reviewer skills](reviewer-skills.md)) are
listed in the plugin descriptor with their own installed workflows
(`reviewers-run.yml`, `code-simplify.yml`, `approvers.yml`). The three CI rows
(#35, [CI entries](ci-skills.md)) are presets over the installed `dk-pr-fix`
workflow with deterministic direct and composed fixtures. All six stay
`not_implemented` for the same reason. So do the two splitting rows
(#34, [commit organization and PR splitting](splitting.md)).

The three drafting rows (#32, [drafting entries](drafting.md#installed-workflow))
are listed in the plugin descriptor with their own installed entry workflows,
`description.yml`, `commit-message.yml` and `issue.yml`, over `dk-pr-fix`. They
stay `not_implemented` until they run as engine-managed sessions through a
released engine.

| Skill | Delivery issue(s) | Required observable result |
| --- | --- | --- |
| `cf-workflows:dk-pr-iter` | #24–25 | Selected fix/review invocation and identity evidence |
| `cf-workflows:dk-pr-fix` | #14–16 | Tested fixes, independent findings, finalizer evidence, completion and readiness |
| `cf-workflows:dk-pr-review` | #24–25 | Discovery, independent verification, dispositions and optional publication |
| `cf-workflows:dk-repo-onboard` | #26 | Complete source/rule/settings inventory with conflicts and gaps |
| `cf-workflows:dk-config` | #26 | Configuration explanation or admitted revision and readback |
| `cf-workflows:dk-preferences` | #26 | Scoped records or removals with provenance and readback |
| `cf-workflows:dk-issue-alternatives` | #27 | Whole-problem comparisons and sourced selection |
| `cf-workflows:dk-pr-alternatives` | #27 | Decision inventory, questionable-decision comparisons and selection |
| `cf-workflows:dk-alternatives-review` | #27 | Delegated comparison or recovered original assignment result |
| `cf-workflows:dk-pr-review-alternatives` | #27 | Fresh requested architecture review with original request retained |
| `cf-workflows:dk-plain-english` | #33 | Edits or sourced no-op, validation and ambiguities |
| `cf-workflows:dk-asd-ste100` | #33 | Best-effort edits, validation and exact reference-verification limits |
| `cf-workflows:dk-code-simplify` | #29 | Bounded behavior-preserving recommendations, deferred observations |
| `cf-workflows:dk-pr-reviewers-run` | #29 | Original reviewer results, coverage, native identities and observations |
| `cf-workflows:dk-pr-approvers` | #29 | Eligible user/team recipients with reasons or exact empty override |
| `cf-workflows:dk-pr-feedback-read` | #30 | Stable complete feedback and separately actionable requests |
| `cf-workflows:dk-pr-feedback-address` | #30 | Tested fixes, per-request dispositions and remaining response obligations |
| `cf-workflows:dk-pr-feedback-reply` | #30 | Bundled attributed answers, publication receipts and optional resolution |
| `cf-workflows:dk-pr-review-post` | #31 | Verified inline findings and body-only metadata findings with receipts |
| `cf-workflows:dk-pr-description` | #32 | Reviewed title/body draft or verified authorized update |
| `cf-workflows:dk-pr-decisions` | #28 | Reconciled structured decisions, open questions and bounded public section |
| `cf-workflows:dk-commit-split` | #34 | Assessed boundaries/messages or safely rewritten series and recovery refs |
| `cf-workflows:dk-pr-split` | #34 | Supported keep/split assessment or verified split PRs |
| `cf-workflows:dk-pr-publish` | #31 | Intent, dispatch/reconciliation receipt or explicit blocker |
| `cf-workflows:dk-ci-approve` | #35 | One verified approval operation per eligible run/attempt or sourced no-op |
| `cf-workflows:dk-ci` | #35 | Current check outcomes, accepted issue-backed flakes/exceptions or blocker |
| `cf-workflows:dk-ci-flakes` | #35 | Named test observations, linked open-issue evidence and limits/gaps |
| `cf-workflows:dk-commit-message` | #32 | Reviewed message with scope, policy and sources |
| `cf-workflows:dk-issue` | #32 | Reviewed draft or verified issue creation/update with receipt |

## Resource and parity rules

The resource list deliberately covers each entry and its direct local Markdown
references on both hosts. It is sufficient to audit this inventory against the
pinned entries; it is **not** a transitive install closure. Packaging must resolve
the complete admitted closure, helpers, catalogs, schemas and licenses before
runtime execution. A missing row, prompt-only wrapper, alias or local adapter
cannot be relabeled as implemented native behavior. Parity status must cite
actual direct/composed evidence and explicit intentional differences, and the
[acceptance checker](qualification.md#checker) enforces this for every row.

The legacy alternatives compatibility entry and the manual alternatives entry
remain distinct rows with different completion and permission boundaries.
Readiness and release qualification remain distinct from selected-plan completion
under the [versioned report contract](contracts/v1/report.md).
