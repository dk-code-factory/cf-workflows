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
authorization accepted is `merge`, with its own permission, the engine's
`merge_guard` and current readiness. `post_findings` is #25. Approval and
fork-CI approval stay denied extension kinds. Capture reads another author's
PR without `maintain_other`, and the plan adds `finding_verification` but no
`finding_fix`.

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

## Verification

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
  coordinator skill with three required passes on the review preset. Report-only
  claims, missing passes and unfinished passes stay `incomplete`.

## What remains

- Running review.yml with real engine-managed sessions needs Agent Workflow
  #129 and #130. These fixtures do not prove session adherence.
- The incoming-review installed entry for `dk-pr-iter` needs a workflow that
  runs routing before the selected entry. Today routing is a library operation
  called before preflight.
- [Alignment](alignment.md) gating of ordinary review runs only as a library.
  Its native stages and the alternatives skills are #27.
- A profile built from an installed code-review-ad package uses custom adapters.
  These need semantic admission (#12/#13) and workflow inputs for accepted
  custom inputs. Supplied reviewer skills need #16 and Agent Workflow #139.
- Finding publication (inline and body-only) is #25. Approval and fork-CI
  approval are #31 and #35.
