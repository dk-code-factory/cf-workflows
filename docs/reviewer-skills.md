# Reviewer coordination, simplification and approver discovery

`cf-workflows:dk-pr-reviewers-run`, `cf-workflows:dk-code-simplify` and
`cf-workflows:dk-pr-approvers` are thin entries over the shared workflow (#29).
[`cf_workflows/reviewers.py`](../cf_workflows/reviewers.py) holds the two review
entries and [`cf_workflows/approvers.py`](../cf_workflows/approvers.py) the
approver procedure. Each has its own [installed workflow](#installed-workflows)
listed in the plugin descriptor. None of them is runnable yet (see
[What remains](#what-remains)).

Each entry is a preset selection plus its own adapter procedure. No entry adds
orchestration or loads a skill, and none accepts an authorization. The role
prompts are the baseline `prompts/default.md` texts, byte for byte, and their
digests match [the inventory](../baselines/inventory.json).

| Entry | Preset | Adapter procedure | Original options |
| --- | --- | --- | --- |
| `dk-pr-reviewers-run` | review mode: `tasks: [{stage: reviews}]`, no finalizers | selected admitted review adapters on the `reviewer` role, plus the separate `verifier` session | `reviewers` (no default), `execution`, `max_parallel` |
| `dk-code-simplify` | review mode: one reviewer stage with one simplification reviewer | the selected simplification adapter on the `reviewer` role, plus the `verifier` session | `priority`, `allow_untouched_code` |
| `dk-pr-approvers` | one `discover-approvers` task on the `leaf` role, no finalizers or reviewers | helper evidence collection, one session that chooses among eligible candidates, helper checks | none |

## Reviewer runs

`reviewers.preflight('dk-pr-reviewers-run', ...)` admits the preset through the
shared `dk_pr_fix.preflight` in review mode and returns the inputs of
`workflows/reviewers-run.yml` ([installed workflows](#installed-workflows)).
These rules apply:

- **Selection.** `reviewers` keeps its order, explicit parallel groups, repeated
  occurrences and `required: false` selections exactly. Without an invocation
  value, the configured `reviewers` option applies. With none, or with an
  explicit `[]`, the run rejects before the engine is contacted: reviewers must be
  configured.
- **Unsupported controls.** A selection that carries `options`, `agent`, `model`,
  `reasoning_effort`, `execution` or `max_parallel` rejects. Per-occurrence
  provider options and native model controls need engine support. The model and
  reasoning effort come from the admitted role definition. Instructions, custom
  adapters and supplied skills keep the shared rejections (#16, Agent Workflow
  #139).
- **Scheduling.** `execution: sequential` sets the limit
  `max_parallel_reviewers` to 1. `max_parallel: N` sets it to N (1–64), and
  `null` sets 0 (no configured cap). The other configured limits stay. `prepare`
  caps each reviewer group's `concurrency` at the limit. It never removes,
  merges or reorders an occurrence, and limits bind no analysis, so changing the
  schedule reuses every result.
- **No authority.** A request authorization, an invocation permission, or an
  invocation that selects tasks, finalizers or reviewers rejects.
- **Shared run.** The default logical run is incoming review's
  `review_<digest>`. The reviewer stage sits in the same phase as in
  `dk-pr-review`, so occurrence IDs, the review contract, coverage and retained
  budgets are shared. A later composed review reuses every current reviewer and
  verifier.

Coverage is the installed helpers' review record ([incoming review](review.md#outcomes)).
Every required occurrence needs its own engine-managed session, verification runs
in a separate session even with no findings, and a report that claims sibling
passes covers only its own occurrence. `reviewers.result()` maps that record to
the entry's result:

- the outcome and its required and optional gaps;
- every occurrence with its native agent and its original accepted report (or
  `null`);
- the finding dispositions;
- `approve`, `ready` and `publication`, always false.

An optional sibling that failed or is missing is listed in `optional_omitted`, and
the reports of the completed siblings stay as they were accepted.

## Simplification

`dk-code-simplify` runs one bundled simplification reviewer, chosen from its
options. The default (`change-first`, untouched code not allowed) is the
composed `simplify` reviewer. Each other combination is its own bundled control
text, so it needs no validator session:

| `priority` | `allow_untouched_code` | Adapter |
| --- | --- | --- |
| `change-first` | `false` | `simplify` |
| `code-first` | `false` | `simplify-code-first` |
| `change-first` | `true` | `simplify-related-code` |
| `code-first` | `true` | `simplify-code-first-related-code` |

Every variant reviews only the PR changes. It stays within the changed hunks and
agreed scope; with untouched code allowed, it may also include directly related
code or call sites that a concrete simplification of this PR needs. Broader
opportunities go to deferred observations, and the reviewer does not edit or
publish. A configured reviewer list never widens this entry, and a composed plan
may select any variant by name. A policy change refreshes only the
simplification occurrence.

All four adapters are `admission.SIMPLIFIERS` and count as simplification in the
installed helpers. A simplification-only run never has ordinary coverage, so it
is never a clean review. `reviewers.result('dk-code-simplify', ...)` returns:

- the confirmed recommendations;
- the rejected and unverified ones;
- the reviewer's `deliverables.deferred_observations`;
- `clean_review` and `replaces_review`, always false.

The independent verifier decides which recommendations are confirmed.

## Approvers

`approvers.select(request, head, override, ...)` admits the entry preset through
`entries.preflight` (one capability query). It then works in one of two ways.

**Override.** With the caller's exact `override` (`{reviewers, team_reviewers}`,
usernames without `@`, team slugs without `ORG/`), it returns that selection
unfiltered as `source: override`. It reads nothing from GitHub and runs no
session. An explicit empty override returns two empty lists and disables
discovery. A malformed or duplicate override rejects.

**Discovery.** Without an override, it collects complete read-only evidence:

- the PR, which must still be at the requested head;
- every page of its reviews (bounded);
- the changed paths and the CODEOWNERS of the target revision from the local
  checkout (`.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS`; the
  last matching rule wins, and a wildcard last segment such as `docs/*` matches
  only at its own depth);
- the repository permission of each candidate.

Candidates are existing requested users and teams, earlier non-dismissed human
reviewers and code owners of changed paths. The PR author (by numeric ID), bots,
deleted identities, reviewers with only dismissed reviews and accounts without
write, maintain or admin permission are excluded with reasons. Owners that are
teams of another organization or email addresses are reported as gaps. The authenticated actor is never excluded for being the
actor. Only when this leaves no candidate does the helper call one bounded
`maintainers(repository, paths)` search.

With no eligible candidate, the result is complete and empty, with the reason
and no session. Otherwise one engine-managed `leaf` session (no skills, no
writes) chooses recipients from the candidates. The helper accepts only eligible
candidates that cite their own evidence and give a reason. The head is read
again afterwards.

The result has these fields: `complete`, `pr`, `head`, `source`, `reviewers`,
`team_reviewers`, `reasons`, `evidence` and `blockers`. A failed or unbounded
read, an incomplete search, a changed head, CODEOWNERS syntax the helper cannot
evaluate or an unacceptable choice returns `complete: false` with blockers and
no recipients. Missing evidence is never an empty selection.

`request_payload(selection, pr, head)` converts a complete selection for the same
PR and head into the exact `request_review` intent payload. An empty selection
returns `None`, so nothing is requested. The payload grants nothing:
[publication](publication.md) keeps its own permission, authorization and
current-head checks. The `discover-approvers` adapter is rejected in `dk-pr-fix`
plans, because its result depends on this procedure.

## Installed workflows

[`plugin.yaml`](../cf_workflows/workflows/dk-pr-fix/plugin.yaml) binds each entry
to its own workflow in the installed `dk-pr-fix` root, with a `SKILL.md` that
states the plugin is not runnable. Preflight returns exactly the inputs each
workflow declares.

| Entry | Workflow | Declared inputs | Fixed in the workflow |
| --- | --- | --- | --- |
| `dk-pr-reviewers-run` | [`reviewers-run.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/reviewers-run.yml) | `request`, `reviewers` (required), `adapters`, `limits`, `permissions`, `ci`, `merge_method`, `accounts`, `settings`, `resources`, `turns` | `tasks: [{stage: reviews}]`, `finalizers: []`, `mode: review`, `entry: dk-pr-reviewers-run` |
| `dk-code-simplify` | [`code-simplify.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/code-simplify.yml) | `request`, `simplifier` (default `simplify`), and the same shared inputs | one reviewer stage with `[simplifier]`, `finalizers: []`, `reviewers: []`, `mode: review`, `entry: dk-code-simplify` |
| `dk-pr-approvers` | [`approvers.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/approvers.yml) | `request`, `head`, `override` (`{}` discovers), `turns` | the three jobs below |

Both reviewer workflows call `main.yml`. Its installed `prepare` helper
([`prepare.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/prepare.py))
repeats the preset: review mode, exactly one reviewer stage with only review
tasks, no finalizer, no authorization and no enabled permission, and for
`dk-code-simplify` exactly one bundled simplifier. Any other shape rejects
before a session. The check never changes the plan, so occurrence identities,
the review contract and coverage stay incoming review's.

`approvers.yml` runs the approver procedure without a library controller:

1. `approvers_collect`, a retained helper operation
   ([`approvers.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/approvers.py)),
   returns the exact override with no GitHub read and no session, an explicit
   empty override included. Otherwise it collects the same evidence as
   `approvers.select()` through read-only `gh api` routes and records it with
   the checkout state. A staged authorization rejects.
2. Only with an eligible candidate, one `agent/run` step on the admitted `leaf`
   definition with no skills chooses recipients under a fixed output schema.
3. `approvers_check` accepts only eligible candidates that cite their own
   evidence, requires the checkout unchanged and reads the head again. A failed
   session, a rejected choice, a changed checkout or a moved head returns
   `complete: false` with blockers and no recipients.

The evidence and selection checks in the installed helper are a verbatim copy of
`cf_workflows/approvers.py`, and a test keeps them identical. The installed
maintainer search reads recent commits for at most 20 changed paths; more paths,
or a path that needs URL escaping, is an incomplete search and blocks.
`approvers.installed_preflight()` admits the entry as `select()` does and also
rejects custom prompts, which the installed session does not consume yet.

## Authority

Neither simplification nor approver discovery can authorize publication,
approval or a merge. The entries reject every authorization. Their results are
read-only records: `approve`, `ready` and `publication` stay false, and merge
readiness stays the [merge gate](publication.md).

## Verification

[`tests/test_reviewer_skills.py`](../tests/test_reviewer_skills.py) covers these
cases.

- **Preflight.** On a relocated installation with a fail-closed fake engine:
  baseline prompt digests; empty, configured, ordered, grouped, repeated and
  optional reviewer selections; scheduling with configured limits kept;
  unsupported controls, authorizations and preset overrides rejected with zero
  capability queries; simplification variants; direct and composed inputs that
  match; and `discover-approvers` rejected in a fix plan.
- **Installed helpers.** In review mode, on a disposable repository and a fake
  GitHub that answers only capture, with native dispatches counted:
  - direct then composed runs that share every reviewer and verifier session;
  - concurrency 1, 2 and uncapped, with the same occurrences;
  - at capacity one, a required pass missing until it runs, and a failed
    optional sibling whose completed siblings keep their original reports
    across a reopened ledger with no new session;
  - repeated occurrences that need distinct sessions;
  - simplification recommendations and deferred observations that never make a
    clean review, readiness or a merge proposal;
  - a policy change that refreshes only the simplifier.
- **Approvers.** On a disposable repository with a fake GitHub that counts each
  read and a fake engine with scripted sessions: empty and non-empty overrides
  with zero GitHub reads and sessions; evidence-based selection with
  exclusions; out-of-evidence choices; the maintainer search; pagination;
  failed, unconfigured and changed-head reads; unsupported CODEOWNERS syntax;
  and rejected authorizations.
- **Installed bindings.** The descriptor rows and `SKILL.md` files; each entry
  workflow declares exactly the inputs its preflight returns and fixes its
  preset over `main.yml`; the installed `prepare` helper keeps the plan of each
  reviewer preset identical to incoming review's and rejects other stages,
  finalizers, permissions, authorizations, modes and non-simplifier reviewers.
- **Installed approvers.** Through the action-v1 entry point on the same
  disposable repository and fake GitHub: the verbatim copy; overrides with zero
  reads and no session; accepted, foreign and failed selections; a session that
  writes the checkout and a moved head; an empty result without a session;
  replay, checkpoint validation and reconciliation with no new read; a staged
  authorization; the read-only `gh api` routes and the search bound; and
  `installed_preflight()` inputs and rejections.

## What remains

- Agent Workflow
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129),
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130) and
  [#139](https://github.com/dk-code-factory/agent-workflow/issues/139) are on
  its `main`, and `agent-workflow plugin check` and `validate` built from that
  source accept the three workflows. No release contains them (the newest tag is
  `v0.2.1`), so none of the workflows has run as engine-managed sessions, the
  `0.2.2` pin stays ([packaging](packaging.md#what-remains-blocked)) and the
  three inventory rows stay `not_implemented`. No local controller or launcher
  stands in for the engine.
- Reviewer skills supplied by the user still reject: #16 has no runtime path for
  them through these workflows yet. Per-occurrence provider options and native
  model controls reject until the engine offers them (Agent Workflow
  [#134](https://github.com/dk-code-factory/agent-workflow/issues/134)).
- Requesting the selected reviewers through the publisher is
  `cf-workflows:dk-pr-publish` (#31).
- Real provider-session adherence and GitHub conformance need separate
  authorization; these fixtures do not establish them.
