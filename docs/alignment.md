# Current alignment over settled architectural analysis

[`cf_workflows/alignment.py`](../cf_workflows/alignment.py) separates a PR's
architectural analysis from its current alignment. This implements the alignment
rules of the [invocation](contracts/v1/invocation.md) and
[state](contracts/v1/state.md) contracts for
[dk-workflows#239](https://github.com/dk-code-factory/dk-workflows/issues/239) and
[dk-workflows#241](https://github.com/dk-code-factory/dk-workflows/issues/241).
Provisional PR identity is [provisional.md](provisional.md).

Like [feedback obligations](feedback.md), this is a library operation that no
installed workflow calls yet. The incoming-review stages (#24) and the
alternatives skills (#27) consume it. Native sessions for these stages need Agent
Workflow #129 and #130.

## Stages

| Stage | Key | Consumes | Settled |
| --- | --- | --- | --- |
| `assessment` | `assessment` | Its own inputs | Once complete |
| `inventory` | `inventory` | The assessment | Once complete |
| `comparison` | `comparison:<decision>` | The assessment and only its own inventory entry | Once complete |
| `selection` | `selection:<decision>` | The assessment and its comparison | Once it has a choice |
| `interactions` | `interactions` | The assessment, inventory and each selection's choice and comparison | Once complete |
| `alignment` | `alignment` | The assessment, interactions, its own inputs and the head | Never |
| `verification` | `verification` | The mismatch alignment and the head | Never |

The first five stages are the architectural analysis. They run once per PR and do
not bind the head. A settled stage whose binding changed is not repeated on a
changed input, guidance, fingerprint or head. `prepare` raises
`AuthorizationRequired` instead. Only a distinct direct user request
(`id`, `source`, `instruction`, `skill: cf-workflows:dk-pr-review-alternatives`,
`origin: user`) permits it, once per stage. A delegated or agent request does
not. A selection without a choice is not settled, so a sourced user choice can
refresh it. An added inventory sibling leaves every unchanged comparison reused.

Alignment and verification judge the current head under current requirements. A
later head or new requirements evidence refreshes them without authorization and
without repeating any architectural stage. An `uncertain` alignment is never
settled. Verification of a `mismatch` must come from a different native session.

## Attempts and allowances

`Ledger.prepare(stage, decision, inputs=..., head=..., operation=..., request=None)`
returns one action:

- `reuse`: an accepted result with the same binding. No session, no allowance.
- `resume`: an interrupted claim with the same binding. The caller continues its
  original engine operation, which already holds its reservation.
- `run`: a new attempt under this engine operation. It reserves one unit of
  `architecture` (default 16) or `alignment` (default 4).

The first reservation fixes the allowance's ceiling for the logical run. A later
run or configuration can only narrow it; only an explicit `grant` adds capacity.
Replaying an operation never consumes twice. When a later head arrives before an
interrupted claim finishes, the claim is `superseded`: it keeps its reservation,
and a late result is retained but never becomes current. `finish` accepts one
result per operation. Every attempt, result and request stays in the history.

## Gate

`gate(ledger, head=..., inputs=..., identities=None, receipts=())` walks the
stages in order and returns one outcome. No outcome sets `clean_review` or
`ready`.

| Outcome | Meaning |
| --- | --- |
| `analysis_required` | The `next` stage is missing, stale or has an interrupted claim (`resume`). `authorization_required` when it is settled |
| `choice_pending`, `interactions_blocked` | Architectural analysis cannot release review |
| `alignment_required` | No alignment matches this head and these inputs: `reason` is `later_head`, `new_evidence` or `changed_analysis`. Earlier alignment and verification are `context` only. Reports the `alignment` allowance |
| `alignment_uncertain` | The current alignment is uncertain. New evidence refreshes it |
| `released` | Aligned, or a mismatch rejected by verification. `release` is true |
| `mismatch_verified` | An independently confirmed mismatch at this head, with its `required_changes` |
| `alternatives_rejected` | A retained receipt rejected exactly the verified changes at this head. Alternatives are complete; ordinary review is not |
| `alignment_blocked` | Verification was uncertain |

A historical rejection receipt is never needed. After a later head the verified
mismatch is context for refreshed alignment, not a gate. A receipt at another
head is ignored. A receipt at this head that differs from the verified changes
rejects. `identities` (the [capture](provisional.md#pinning) record) only marks
the outcome `provisional`. Private analysis may continue.

## Partial COMMENT findings

`partial_comment(finding, verification, snapshot=..., permissions=..., publications=())`
decides whether one finding may be posted in a partial `COMMENT` review. Each
failed check adds a reason:

- `provisional`: the snapshot's identities do not agree. This blocks every finding.
- `other_pr`, `stale_head`: the finding is for another PR or another head.
- `stale_verification`, `unverified`, `not_independent`: the verification is for
  another finding, head or fingerprint, is not `confirmed`, or ran in the
  reviewer's own session.
- `permission`: `post_findings` is not effective.
- `uncertain_publication`, `already_prepared`, `already_applied`: an intent
  covering the finding is still `pending`, or already exists.

Alternatives state is not an input. Unrelated alternatives recovery neither
withholds an eligible finding nor makes the review clean. The result is always
`event: COMMENT`, with `clean_review`, `approve` and `ready` false.

## Ledger

Each logical run has its own record, `runs/<sha256(logical_run)>.alignment.sqlite`
in the [state namespace](dk-pr-fix.md#state-namespace), next to the run ledger.
It holds attempts, direct requests, reservations and grants.
`PRAGMA user_version = 1` and a `meta` table bind the suite, kind, logical run
and PR. Any other version, database or binding is refused.

## Verification

[`tests/test_alignment.py`](../tests/test_alignment.py) uses a disposable Git
repository for real heads, a disposable ledger per test and a fake native-session
transport that counts dispatches per stage. An unscripted dispatch fails. It
covers:

- unchanged reuse across a reopened ledger with no session;
- dk-workflows#239: a verified mismatch at an old head, no rejection receipt,
  then recovery at a later head with one comparison in total;
- an existing receipt reused at its head and refused when it differs;
- dk-workflows#241: an uncertain alignment refreshed by new requirements with
  no architectural session and retained allowances;
- interrupted later-head recovery that resumes its original operation, and a
  superseded claim whose late result stays private;
- changed heads, guidance, delegated and agent requests that never authorize a
  comparison; distinct direct requests that do, with unchanged siblings reused;
- retained, narrowed, exhausted and granted allowances;
- independent verification, pending choices and incompatible interactions;
- partial COMMENT eligibility checked separately from the gate, including the
  provisional block.

## What remains

- Running these stages as engine-managed native sessions needs Agent Workflow
  #129 and #130. [Incoming review](review.md) (#24) does not call the gate yet:
  it needs these native stages in the installed workflow. The alternatives
  skills and the direct manual entry are #27.
- Posting an eligible finding goes through [exact-intent
  publication](publication.md). Incoming review applies the same rules in its
  [finding publication](review.md#finding-publication); it does not call this
  library function.
- Real GitHub conformance and native-session adherence need separate
  authorization. These fixtures do not establish them.
