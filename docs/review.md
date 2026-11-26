# Incoming review and author routing

`cf-workflows:dk-pr-review` reviews another author's PR without fixing it.
`cf-workflows:dk-pr-iter` decides, from verified identities, whether a PR goes
to fixing or to incoming review. Neither is runnable yet: like
[dk-pr-fix](dk-pr-fix.md), the installed workflow needs Agent Workflow #129 and
#130 (see [What remains](#what-remains)).

## Routing

[`cf_workflows/routing.py`](../cf_workflows/routing.py) reads the PR and the
authenticated account once each through the supplied transport
(`pull_request(pr)`, `viewer(host)`). It compares immutable numeric account IDs
on the PR's host:

- The PR is yours when its author's ID is the actor's ID, or a configured
  personal account on that host. Logins, remotes, commit authorship and write
  access never count, so a renamed login keeps its route and a reused login
  never inherits one.
- Personal accounts are the admission option `accounts`: `{host, id}` records
  with a canonical lowercase host and a positive integer ID. A login, a string
  ID or a duplicate rejects.
- A missing, non-numeric or zero ID, a failed read, or a PR that is not the
  requested one returns `blocked` with no route. Routing never guesses.

| Request | Route |
| --- | --- |
| Your PR | `fix` (`cf-workflows:dk-pr-fix`) |
| Another author's PR | `review` (`cf-workflows:dk-pr-review`) |
| Explicit `{route, request_id, instruction, source}` | That route. Fixing another author's PR stages the request as `maintain_other` |
| Analysis only | `review`, with every remote-write action prohibited and every authorization withheld. An explicit fix in the same request blocks |

The routed request keeps the exact original instructions, logical run and
authorizations. Routing adds no authority. Reviewing your own PR adds an
`approve_pr` prohibition and blocks an approval grant. `routing.invocation()`
merges the routed prohibitions into the caller's invocation scope. Prohibitions
accumulate in admission, so a narrower request always wins, and restoring a
withheld grant contradicts it and rejects.

## Incoming review

[`cf_workflows/dk_pr_review.py`](../cf_workflows/dk_pr_review.py) admits the
review preset through the same preflight as dk-pr-fix, with these defaults
below every configured scope:

- tasks `read-feedback`, then the reviewer stage;
- no finalizers;
- reviewers `review` and the simplification reviewer `simplify` in parallel.

Its entry, [`workflows/review.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/review.yml),
calls `main.yml` with `mode: review`. The default logical run is
`review_<digest>`, separate from the fix run of the same PR.

Preflight and the `prepare` helper reject a writing task, the publication,
response and CI stages, and a plan without a reviewer stage. The only
authorizations accepted are `post_findings` (see
[Finding publication](#finding-publication)), `approve_pr` (see
[approval](publication-skills.md#approval)) and `merge`, each with its own
permission. Merge also needs the engine's `merge_guard` and current readiness.
Fork-CI approval stays a denied extension kind. These permissions never bind
analysis in review mode, so a later dk-pr-review-post or merge with the same
instruction reuses current review. Capture reads
another author's PR without `maintain_other`, and the plan adds
`finding_verification` but no `finding_fix`.

The standalone entries `cf-workflows:dk-pr-reviewers-run` and
`cf-workflows:dk-code-simplify` are presets over the same review mode and logical
run ([reviewer skills](reviewer-skills.md)). Every bundled simplification
reviewer (`admission.SIMPLIFIERS`) counts as simplification, never as ordinary
coverage.

Each reviewer and the verifier is an engine-managed session with the admitted
`reviewer` or `verifier` role and no skills. A profile such as the
code-review-ad passes (Toolkit, Standards, Spec) is a parallel group of
reviewer adapters, one occurrence per pass. Every original report is retained
by the existing claim ledger.

## Outcomes

`coverage` returns a `review` record. `readiness` copies it.

| Field | Meaning |
| --- | --- |
| `occurrences` | Each reviewer and the verifier: `ordinary`, `simplification` or `verification`, `required`, and `covered`, `failed` or `missing` |
| `required_missing` | Required occurrences without current coverage |
| `optional_omitted` | Optional occurrences that failed or are missing, reported separately |
| `findings` | Candidate IDs that are `confirmed`, `rejected` or `unverified` |
| `complete` | Every required occurrence is current and every finding has a verification disposition |
| `clean_review` | `complete`, at least one current ordinary reviewer and no confirmed finding |
| `outcome` | `clean`, `findings` or `incomplete` |

These rules come from dk-workflows#254:

- A session that already reported for one occurrence cannot be accepted for
  another. A coordinator-style report that describes sibling passes covers
  only its own occurrence.
- Verification is required even with empty findings, and it must run in its
  own session.
- Simplification never substitutes for an ordinary reviewer.
- A confirmed finding settles the analysis but blocks `clean_review`. It is
  never fixed here.

`approve` and `ready` are always false in this record. `readiness` stays the
merge gate. It also needs every selected reviewer current, fresh exact-head CI
and the live repository requirements. So incomplete coverage never reaches
`merge_proposal`.

An unchanged rerun reuses each occurrence while its fingerprint holds, with no
new session, across a reopened ledger and a new plan artifact. A new head or
tree makes that coverage stale, so the run is `incomplete` until the affected
occurrences run again. Supplied reviewer skills keep the [skills](skills.md)
rules: only the selected occurrence loads the skill, its findings go to the
separate verifier, and coverage comes only from engine records.

## Finding publication

With `post_findings` enabled by accepted configuration and explicitly
authorized, `main.yml`'s `findings` job runs the publication barrier with the
`findings` selector after the review cycles.
[`prfix/findings.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/findings.py)
lists at most one new `submit_review` proposal, and the shared
[exact-intent publication](publication.md) path prepares it, obtains its
independent audience verdict, dispatches it and reconciles it. This covers
dk-workflows#240.

Each reviewer finding names its own location (the assignment's
`location_contract` and the native report schema):

| Location | Published |
| --- | --- |
| `{kind: diff, path, line, side}` on an added (`RIGHT`) or removed (`LEFT`) line of the PR diff | Inline on that line |
| `{kind: file, path, line}` on an added line | Inline on that line |
| Any other existing line of the head (or, for `LEFT`, the base) | Review body, as `path` line N with a pinned link |
| `{kind: commit_message, commit}` for a commit of the PR | Review body, with the commit link |
| `{kind: pr_title}` or `{kind: pr_description}` | Review body, with the PR link |

The changed lines come from `git diff -U0 base..head` of the reviewed
revision, which is GitHub's PR diff. No finding is ever moved to a nearby
changed line. A location that does not exist in the reviewed PR (a missing
line or file, a commit outside the PR) and a finding with no location are
withheld, with the reason in the private listing result. One submission holds
at most 64 inline comments and one finding per anchor; the others wait for a
later run.

A finding is published only when all of these hold:

- it comes from a review occurrence (captured feedback from others is never
  republished) whose current accepted report still contains it;
- the current independent verification confirms it, from a session other than
  the reviewer's;
- its text names no local execution detail: the checkout or state namespace
  path (mounted, configured or resolved), a `cf-workflows/repositories/` path,
  an `artifacts/`, `requests/` or `preparations/` state reference, or a
  generated identity in its exact format (`agent_` + 32 hex, `task_` + 24 hex,
  default `pr_`/`review_` + 32 hex runs, and `claim_`, `finding_`, `findings_`,
  `publication_`, `push_` or `merge_` + 64 hex). A user-chosen run name and
  ordinary code identifiers such as `group_2` or `merge_20240115` do not count;
- analysis is not [provisional](provisional.md);
- no prepared, pending or applied submission of this run already carries it,
  by finding identity or by exact location and text.

The payload is built only from those findings. Inline comments hold the
finding text and evidence links. The body holds one entry per body finding:
its location label and link, its text and its evidence links, all pinned to
the reviewed head. The boolean `settings` option `include_summary: true` adds
one overview sentence (counts only); `false`, the default, removes only that sentence, so a
body-only review keeps every finding. A review with only inline findings and
no summary has an empty body. Coverage gaps, withheld findings, operational
errors and other diagnostics stay in the listing's private `withheld` and
`blockers` fields. A confirmed finding left out for any reason other than a
superseded report also adds a blocker, so it is never silently omitted.

The event is `REQUEST_CHANGES` only when the review outcome is `findings`
(complete coverage with an ordinary reviewer), the PR is not the user's own
(authored by the actor or by a configured personal account on the PR's host,
as in [routing](#routing)) and the setting `request_changes` is not false. Otherwise, including every
partial review, it is `COMMENT`. Both are boolean `settings` options. Publishing findings never changes the review
record: `clean_review`, `approve` and `ready` stay false, merge keeps its own
authorization and readiness, and approval and [fork-CI approval](ci-skills.md#fork-workflow-approval) keep their own grants.
`include_summary` and `request_changes` shape only the review text, so changing
them never makes review coverage stale.

Recovery is the shared publication journal. The proposal ID is a digest of the
run and the exact proposal, so an unchanged rerun relists the same submission
or, once it is applied, nothing. A pending (uncertain) submission is listed
only for read-only reconciliation, and its findings are not proposed again. A
blocked or `not_applied` submission frees its findings, and an identical
relisting becomes a fresh attempt. An actor-owned pending review refuses
preparation and is never touched ([pending reviews](publication.md#pending-reviews)).
Native reviewer reports cannot propose review writes in review mode: the
publication helpers list only verified findings and the final merge there.

## Verification

- [`tests/test_dk_pr_review_findings.py`](../tests/test_dk_pr_review_findings.py)
  drives the installed helpers on a disposable repository with a fake GitHub
  transport that answers only capture, review creation and review-comment
  reads, and counts writes and native dispatches. It covers dk-workflows#240
  body-only findings with summaries disabled, mixed inline, body and withheld
  findings without duplication or re-anchoring, the exact changed-line map,
  partial `COMMENT` reviews that establish no readiness, own-PR and configured
  events, missing permission or authorization, native review proposals,
  unverified and rejected findings, unchanged reruns and a new head, response
  loss before and after the effect, an existing pending draft, a draft after
  preparation, conclusive pending-review rejection and provisional analysis.
  For #23 it also checks a partial review analyzed while PR metadata lags: no
  finding is listed or prepared while provisional, the reconciled run reuses
  the reviewer and verifier results with no new session, a renewed lag before
  dispatch withholds everything again, and the eventual `COMMENT` establishes
  no clean review, approval or readiness.
- [`tests/test_routing.py`](../tests/test_routing.py) uses a fake GitHub
  transport that answers only the two identity reads, at most once each. Any
  other operation fails. Cases cover own and other PRs, multi-host accounts,
  renamed and reused logins, ten missing or inconsistent identity cases,
  analysis-only and explicit routes. Routed requests are then admitted by the
  real preflights.
- [`tests/test_dk_pr_review.py`](../tests/test_dk_pr_review.py) drives the
  installed helpers in review mode. It uses a disposable repository and a fake
  GitHub transport that answers only capture, and it counts native dispatches.
  Cases: clean review only after a separate verifier, simplification-only
  plans, report-only pass claims (dk-workflows#254), confirmed and rejected
  findings, optional versus required failures, reuse and head refresh, and
  merge withheld without coverage. The review preflight cases cover the preset,
  scope overrides and rejected writes and publications.
- `tests/test_skills.py` (`IncomingReviewCoordinator`) checks a supplied
  coordinator skill with its one required child pass on the review preset
  (Agent Workflow #139 admits exactly one required child, on Claude only).
  Report-only claims, missing passes and unfinished passes stay `incomplete`.

## What remains

- Running review.yml with real engine-managed sessions needs Agent Workflow
  #129 and #130. These fixtures do not prove session adherence.
- The incoming-review installed entry for `dk-pr-iter` needs a workflow that
  runs routing before the selected entry. Today routing is a library operation
  called before preflight.
- [Alignment](alignment.md) gating of ordinary review runs only as a library.
  The [alternatives entries](alternatives.md) drive its stages as library
  procedures; the installed review workflow rejects their adapters.
- A profile built from an installed code-review-ad package uses custom adapters.
  `dk-pr-review` preflight routes them through semantic assessment and maps them
  once accepted, but through a real engine the validator task blocks until the
  engine records its operations ([engine binding](semantic.md#engine-binding)).
  The profile itself is #13. Supplied reviewer skills need #16 and Agent Workflow #139.
- Finding publication runs through the installed graph only once Agent
  Workflow #129 and #130 land. The fixtures prove the helper and ledger
  behavior, not GitHub's acceptance of these payloads (for example an empty
  body with inline comments); that conformance needs separate authorization.
- The standalone `cf-workflows:dk-pr-review-post` entry and approval are in
  [publication entries](publication-skills.md). Historical finding follow-ups
  (reusing an earlier equivalent thread) remain open there. Fork-CI approval is
  in [CI entries](ci-skills.md).
