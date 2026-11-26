# Acceptance matrix and release evidence

[`baselines/acceptance-matrix.json`](../baselines/acceptance-matrix.json) traces
each of the 29 [inventory](inventory.md) rows to the code that implements it,
its own deterministic scenarios and its compatibility status. It also maps the
nine inherited dk-workflows defects to deterministic tests with merge
provenance, lists the configuration-admission cases, and holds the places for
separately authorized live evidence. Nothing in it is a support claim: no row
is implemented, no host/provider/engine combination is verified and no live
acceptance has been performed.

## What each row records

| Field | Meaning |
| --- | --- |
| `skill` | The exact inventory name. A name that is not an inventory row is rejected as an alias |
| `delivery_issues` | The cf-workflows issues that deliver it; must include the inventory's delivery issue |
| `behavior`, `documentation` | The implementing modules or installed workflow files, and the entry guide |
| `direct` | Scenarios that drive this entry on its own. At least one is cited by no other row |
| `composed` | Scenarios that run it inside a shared `dk-pr-fix`/`dk-pr-review` plan, or show the boundary |
| `recovery` | Recovery (lost responses, interruption, restore) or permission-denial scenarios |
| `composition` | `shared_workflow` (runs in a shared plan), `delegate` (a compatibility entry running its delegate) or `boundary` (the shared plan refuses it, so its composed scenario is that refusal) |
| `status` | `parity` (equals the inventory), `deterministic`, `installed_workflow` (equals the plugin descriptor), `native_execution`, `live_acceptance` |
| `remaining` | What stands between the row and supported behavior |

Intentional differences stay in the inventory row (`intentional_differences`)
and must be non-empty. Each reference is one test method in unittest form,
`module.Class.test_name`, relative to `tests/`.

## Checker

```sh
python3 -B scripts/check_acceptance.py          # static checks
python3 -B scripts/check_acceptance.py --run    # also runs each referenced test on its own
```

[`scripts/check_acceptance.py`](../scripts/check_acceptance.py) uses
[`cf_workflows/qualification.py`](../cf_workflows/qualification.py), the standard
library and local Git only. It rejects:

- a missing, duplicate or extra row, a row named by an alias, and any field
  outside the schema (for example `same_as`), so a row cannot point at another;
- a reference to a module, class, suite or count instead of one test method, a
  row with no direct, composed or recovery scenario, and a row whose direct
  scenarios are all cited by other rows;
- a reference to a test method that does not exist (parsed from the sources,
  not imported);
- a parity status that differs from the inventory, an `installed_workflow` that
  differs from `plugin.yaml`, and a `native_execution` or `live_acceptance`
  status that the recorded evidence does not support;
- a defect list that is not exactly the nine inherited defects, a defect test
  that does not name its defect (`dk-workflows#N` in its body or docstrings, or
  `test_N_` in its name), and a defect test that is absent from every listed
  merge commit or whose merge commit is not an ancestor of `HEAD`;
- an admission-case list that is not exactly the required cases;
- a missing implementing path or guide, and a row table below that differs
  from the matrix.

With `--run`, every referenced test runs once on its own and the report names
each failing row, defect and case. A skipped, erroring, unloadable or not-run
test fails like a failing one. The full suite passing does not count for any
row. The checker never contacts a network.

## Rows

All 29 rows have deterministic direct, composed and recovery or denial
scenarios. None runs as an engine-managed native session yet, so every row's
native execution and live acceptance are `pending` and every parity status is
`not_implemented`.

| Skill | Composition | Native execution | Delivery | Plugin descriptor | Direct / composed / recovery |
| --- | --- | --- | --- | --- | --- |
| `cf-workflows:dk-pr-iter` | shared_workflow | pending | #24, #25 | not listed | 3 / 1 / 2 |
| `cf-workflows:dk-pr-fix` | shared_workflow | pending | #14, #15, #16 | listed | 4 / 2 / 4 |
| `cf-workflows:dk-pr-review` | shared_workflow | pending | #24, #25 | listed | 3 / 2 / 2 |
| `cf-workflows:dk-repo-onboard` | shared_workflow | pending | #26 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-config` | boundary | pending | #26 | not listed | 2 / 1 / 3 |
| `cf-workflows:dk-preferences` | shared_workflow | pending | #26 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-issue-alternatives` | shared_workflow | pending | #27 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-pr-alternatives` | shared_workflow | pending | #27 | not listed | 3 / 1 / 3 |
| `cf-workflows:dk-alternatives-review` | delegate | pending | #27 | not listed | 2 / 1 / 1 |
| `cf-workflows:dk-pr-review-alternatives` | boundary | pending | #27 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-plain-english` | shared_workflow | pending | #33 | not listed | 3 / 2 / 3 |
| `cf-workflows:dk-asd-ste100` | shared_workflow | pending | #33 | not listed | 3 / 1 / 2 |
| `cf-workflows:dk-code-simplify` | shared_workflow | pending | #29 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-pr-reviewers-run` | shared_workflow | pending | #29 | not listed | 3 / 2 / 2 |
| `cf-workflows:dk-pr-approvers` | boundary | pending | #29 | not listed | 3 / 1 / 2 |
| `cf-workflows:dk-pr-feedback-read` | shared_workflow | pending | #30 | listed | 2 / 1 / 2 |
| `cf-workflows:dk-pr-feedback-address` | shared_workflow | pending | #30 | listed | 3 / 1 / 2 |
| `cf-workflows:dk-pr-feedback-reply` | shared_workflow | pending | #30 | listed | 2 / 1 / 2 |
| `cf-workflows:dk-pr-review-post` | shared_workflow | pending | #31 | listed | 4 / 1 / 3 |
| `cf-workflows:dk-pr-description` | shared_workflow | pending | #32 | not listed | 2 / 1 / 3 |
| `cf-workflows:dk-pr-decisions` | shared_workflow | pending | #28 | not listed | 2 / 1 / 3 |
| `cf-workflows:dk-commit-split` | shared_workflow | pending | #34 | not listed | 2 / 1 / 3 |
| `cf-workflows:dk-pr-split` | shared_workflow | pending | #34 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-pr-publish` | shared_workflow | pending | #31 | listed | 3 / 1 / 3 |
| `cf-workflows:dk-ci-approve` | boundary | pending | #35 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-ci` | shared_workflow | pending | #35 | not listed | 3 / 2 / 2 |
| `cf-workflows:dk-ci-flakes` | boundary | pending | #35 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-commit-message` | shared_workflow | pending | #32 | not listed | 2 / 1 / 2 |
| `cf-workflows:dk-issue` | shared_workflow | pending | #32 | not listed | 2 / 1 / 3 |

`dk-pr-fix`, `dk-pr-review`, the three feedback entries, `dk-pr-publish` and
`dk-pr-review-post` are the rows in the plugin descriptor. Listing a row claims
no runtime support: none of them has run through a released engine. The other
22 are library procedures or presets over the shared workflow. The installed
workflow refuses those presets until each one runs as an engine-managed session.

## Inherited defects

| Upstream | Delivery | Deterministic coverage (merge provenance) | Live evidence still required |
| --- | --- | --- | --- |
| dk-workflows#244 | #4, #20, #30 | `test_feedback.RejectedSuggestions` (PR #45) | GitHub publication |
| dk-workflows#255 | #4, #21 | `test_dk_pr_fix_responses.BeforeReview` (PR #52) | GitHub publication, live provider |
| dk-workflows#253 | agent-workflow#138, #5, #19 | `test_dk_pr_fix_publication.MergeGating` (PR #50) | GitHub publication |
| dk-workflows#260 | #5, #18 | `test_dk_pr_fix_pending_review.PendingReviews` (PR #49) | GitHub publication |
| dk-workflows#254 | #14, #24, agent-workflow#139, #16 | `test_dk_pr_review.Helpers` (PR #54), `test_skills.IncomingReviewCoordinator` (PR #46) | live provider, user-supplied skill |
| dk-workflows#259 | #6, #22 | `test_dk_pr_fix_provisional.Provisional` (PR #51) | GitHub publication, live provider |
| dk-workflows#239 | #6, #23 | `test_alignment.CurrentAlignmentTest` (PR #53) | live provider |
| dk-workflows#241 | #6, #23, #27 | `test_alignment.CurrentAlignmentTest` (PR #53), `test_alternatives.Boundary` (PR #59) | live provider |
| dk-workflows#240 | #7, #25 | `test_dk_pr_review_findings.BodyAndInline` (PR #56) | GitHub publication |

The matrix names the exact test methods and the full merge commits. The
coverage is deterministic: fail-closed fake GitHub, engine and provider
transports. For example, the 422 body that GitHub returns for dk-workflows#260
is a fixture, not observed conformance.

## Configuration admission

The matrix lists these cases, each with its own tests: a default plan admitted
structurally with no validator session and no skills; structural rejection
before any engine operation; each semantic verdict; clarification and
reconciliation; prohibitions that no prompt, skill, answer or verdict can
waive; permission denial; unsupported capabilities; supplied skills rejected
until supported; selective reuse of acceptance; configuration-write recovery;
no import of dk-workflows settings or state; the bootstrap validator boundary;
and fail-closed unconfigured fixture operations.

## Verified combinations

**None.** No host, provider and engine combination has been verified, and
`verified_combinations` is empty. Status on 2026-11-26:

- The plugin pins `engine-version: 0.2.2`. Agent Workflow has no published
  GitHub release. Its newest tag, `v0.2.1` (2026-11-14), predates the plugin
  toolchain.
- Agent Workflow #127–#133 (the plugin toolchain, provider-neutral sessions
  and relocated packages), #138 (the portable merge guard) and #139
  (session-scoped supplied skills) are closed upstream. No release contains
  them. The engine release and the fixture runner's missing container-job
  support stay listed as [packaging](packaging.md#what-remains-blocked)
  blockers. A development check builds identical packages from the unreleased
  source; it verifies no combination.
- Agent Workflow #134 (managed provider parity) is open. It gates only the
  capabilities that a selected path needs.

A combination becomes verified only through an accepted `live_provider` record
(below) for that exact host, provider and published engine release, bound to
the current plugin closure digest. Hosts and providers are separate
selections: a verified Codex host with one provider says nothing about another
provider or about Claude.

## Setup, manual configuration and recovery boundaries

- **Setup.** The runtime prerequisites, credentials and helper image are in
  [packaging](packaging.md#runtime-prerequisites). The checks never log in,
  refresh tokens, pull images, install an engine or grant write authority.
  Provider authentication and the GitHub token are manual steps. Remote writes
  stay disabled until a scoped permission and an explicit authorization admit
  them.
- **Manual configuration equivalents.** Start with empty configuration and
  state. Nothing is imported from dk-workflows. Restate reviewers, prompts,
  task and finalizer selections, instruction sources and options in a new
  configuration request that passes normal admission. Grant each scoped
  permission again on purpose. See
  [fresh setup and manual equivalents](contracts/v1/README.md#fresh-setup-and-manual-equivalents)
  and the [configuration entries](configuration-skills.md).
- **Recovery boundaries.** Backups, restores and upgrades of this suite's state
  are in [recovery](recovery.md). An unfinished claim resumes only through its
  original engine execution or an explicit continuation. A restore whose
  original admitted helpers are missing is reported `blocked`, and it never
  resumes with replacement resources. An unsettled publication is reconciled
  read-only and never resent. An old dk-workflows run is never resumed here.
  Engine-side backup and bundle commands are a fixture contract until a
  released engine provides them.

## Live acceptance procedures

Three procedures stay separate from deterministic verification. Each needs its
own explicit authorization, given by the repository owner for that run. The
authorization names the procedure, the hosts and providers, the disposable
target and the permitted effects. A request to implement, test or release does
not authorize them. They are **pending**: none has been authorized or performed,
and this repository contains no record.

Before any procedure runs:

1. A published Agent Workflow release must equal the plugin's `engine-version`.
   Its verified download is the engine, never a build from source or a binary
   found on PATH.
2. Run the deterministic suite, `python3 -B -m cf_workflows.package` and
   `scripts/check_acceptance.py --run` on the exact commit under test.
3. Use a disposable GitHub repository and a fresh state namespace. Grant only
   the scoped permissions the procedure lists.

| Procedure | Scenarios a record must pass |
| --- | --- |
| `user_supplied_skill` | `admitted_by_content_digest`, `loaded_only_into_selected_occurrence`, `engine_observed_invocation`, `declared_children_observed`, `host_installed_skill_never_discovered`, `report_only_claim_rejected`, `default_plan_loads_no_skill` |
| `live_provider` | `engine_managed_native_sessions`, `independent_verification_session`, `same_session_continuation`, `claim_and_result_loss_reconciled`, `capacity_one`, `native_sessions_never_publish`, `semantic_validator_session`, `measured_dollar_cost` |
| `github_publication` | `exact_intent_receipts_per_write_kind`, `lost_response_reconciled_without_resend`, `pending_review_rejection_settles_not_applied`, `outstanding_review_requests_hold_merge`, `body_only_findings_published`, `replies_settled_before_review`, `fork_ci_approval_per_attempt` |

Run `live_provider` once per host and provider pair that is to be listed as
verified.

### Evidence record format

A record is a JSON file committed under `baselines/evidence/` and listed in
the matrix under `live_acceptance.<procedure>.records`. Each listed record must
be accepted. A listed record that is missing or invalid rejects the whole
matrix; it is never ignored.

```json
{
  "schema_version": 1,
  "kind": "acceptance_evidence",
  "suite": "cf-workflows",
  "procedure": "live_provider",
  "authorization": {"granted_by": "", "granted_at": "", "reference": "", "scope": ""},
  "plugin": {"version": "", "closure_digest": ""},
  "engine": {"version": "", "release": "https://github.com/dk-code-factory/agent-workflow/releases/tag/v…"},
  "host": {"name": "codex", "version": ""},
  "provider": {"name": "", "model": "", "service_tier": ""},
  "target": {"repository": "", "disposable": true},
  "recorded_at": "",
  "skill": "dk-pr-fix",
  "scenarios": [{"id": "engine_managed_native_sessions", "result": "passed",
                 "evidence_refs": ["session:…", "claim:…"]}],
  "cost": {"complete": true, "usd": 0.0}
}
```

A record is accepted only when:

- the authorization fields are all present;
- `plugin.closure_digest` equals the current package closure digest;
- `engine.version` equals the pinned engine version, and `engine.release` is
  that version's Agent Workflow release URL;
- the target is disposable;
- it covers exactly one row (`skill`), so one shared run never verifies every
  row it touched;
- every required scenario passed with distinct evidence references of the form
  `session:`, `claim:`, `receipt:`, `intent:`, `run:`, `gate:` or `usage:`
  followed by the original ID (empty or free-text references reject);
- a `live_provider` record has complete measured dollar cost.

Evidence references point to the engine's original session and claim
references and to GitHub receipts. They are not transcripts. An evidence
reference cited for one row cannot be cited for another row, so a separate
record per row must rest on its own observations. A change to the
plugin closure or the engine pin makes earlier records stale. A
`verified_combinations` entry must match an accepted `live_provider` record. A
row's `native_execution` is `verified` only when such a record is for that
row. A row's `live_acceptance` is `recorded` only when all three procedures have
accepted records for it.

## Release readiness

`python3 -B -m cf_workflows.package` reports `releasable: false`. Its blockers
come from three sources:

- the missing engine release and the fixture runner's container-job gap;
- the non-runnable suite and every listed row without parity;
- the matrix: each live procedure without an accepted record, the absence of
  any verified combination, and the rows without native-execution evidence.

Editing the inventory, the descriptor or the matrix status fields cannot remove
these blockers. A changed status fails the checker unless the evidence supports
it, and the upstream blockers stay until a released capability is consumed.

A release follows Agent Workflow's supported pipeline:

1. `plugin build` creates both host packages from this one project.
2. `plugin check` and `plugin test` run with deterministic fixtures.
3. Release validation checks the artifacts, checksums and compatibility against
   the exact released engine.

No hand-built package, launcher or manifest stands in for any of these steps.
A release advertises only the verified combinations and the rows that the
accepted records list. This repository builds no package and publishes no
release.

## What remains

- Every row's native execution through an installed workflow on a released
  engine. Each needs Agent Workflow #129/#130 in a release, and supplied skills
  also need #139.
- All three live procedures, each under its own explicit authorization.
- A published engine release equal to the pin, and the shared toolchain's
  build, check, test and release validation from it. Reproducible packages for
  both hosts are shown only against unreleased source
  ([build check](packaging.md#reproducible-builds-against-unreleased-source)),
  and `plugin test` cannot run the bound skills until the fixture runner
  supports container jobs.
