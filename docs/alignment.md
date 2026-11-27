# Current alignment over settled architectural analysis

[`cf_workflows/alignment.py`](../cf_workflows/alignment.py) separates a PR's
architectural analysis from its current alignment. This implements the alignment
rules of the [invocation](contracts/v1/invocation.md) and
[state](contracts/v1/state.md) contracts for
[dk-workflows#239](https://github.com/dk-code-factory/dk-workflows/issues/239) and
[dk-workflows#241](https://github.com/dk-code-factory/dk-workflows/issues/241).
Provisional PR identity is [provisional.md](provisional.md).

The [alternatives entries](alternatives.md) (#27) drive its stages, as library
procedures and through the installed
[staged procedure](alternatives.md#installed-workflow). The installed
`dk-pr-fix` and `dk-pr-review` workflows also read the result through the
[installed gate](#installed-gate) before review. Native sessions for these
stages need Agent Workflow #129 and #130 in a release.

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
`prepare(..., account=...)` keeps a separate count, `<allowance>:<account>`, with
the same default ceiling; the [alternatives entries](alternatives.md) count
alignment per logical run on a record kept per PR. `adopt('assessment', inputs=..., head=..., origin=...)` records the other scope's
own settled assessment of the same PR as this record's, with no session and no
reservation, when this record has neither a result nor an open claim for it
([shared assessment](alternatives.md#scope)). `abandon(operation)` closes a
claim whose result was rejected: its reservation stays consumed and the next
`prepare` starts a new attempt. A direct request has consumed a stage only when
an attempt under it completed. `gate(..., account=...)` reports that account's
alignment allowance.
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

`inputs=None` is a caller that holds no alignment evidence of its own. The
retained alignment's inputs stand, so only a later head or changed analysis makes
it stale (`later_head`, `changed_analysis`). New requirements reach the gate only
through a caller that supplies `inputs`.

## Installed gate

The gate, the record's reads and its writing `Ledger` are one implementation:
[`prfix/alignment.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/alignment.py)
in the installed helpers. It uses only the standard library.
`cf_workflows/alignment.py` loads it and re-exports them; the installed
alternatives procedure writes the record through the same `Ledger`. The writer
adds an `answers` table for the installed selection gates
([alternatives](alternatives.md#installed-workflow)); readers never need it.

`main.yml` runs the `alignment_gate` helper operation after `prepare` and before
the cycles. Coverage reads the gate again each cycle at the analyzed revision.
The operation reads the PR's branch record, `runs/<sha256(pr_run)>.alignment.sqlite`,
where `pr_run` is `alternatives_<digest(PR URL)[:32]>`, the record the
alternatives entries keep for each PR.

- No record: `no_analysis`. Review is not gated and no record is created.
- A record: it is opened read-only, and the suite, kind, run, PR and version are
  checked. `gate(..., inputs=None, identities=...)` runs at the pinned revision.
  An unreadable, foreign or unsupported record fails the operation, so the
  cycles do not run.
- `gated` is true when the plan selects review and the outcome does not release
  it. Then coverage reports `current_alignment` under `withheld`, not `missing`.
  The run settles without another cycle, but `complete` and `ready` stay false.
  Incoming review stays `partial` and is never `clean`. Its independently
  verified findings are still listed, as a `COMMENT` only. `completion` returns
  this unready result and does not fail, as it does for provisional analysis.
  `readiness` reports the outcome as a blocker.

The gate never starts, repeats or authorizes a stage, and never writes the
record; a plan's alternatives occurrence does that through its staged procedure.
A later head needs only a refreshed alignment from the alternatives entries. It needs no rejection receipt, and settled stages and allowances are
kept. Recovery backs up the `alignment` store with dk-pr-fix, dk-pr-review and
dk-pr-iter runs ([recovery](recovery.md)).

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
It holds attempts, direct requests, reservations and grants, and the answers
to the installed selection gates (an `answers` table the writer adds to a
version 1 record that lacks it).
`PRAGMA user_version = 1` and a `meta` table bind the suite, kind, logical run
and PR. Any other version, database or binding is refused. Whole-problem
analysis (`scope='whole-problem'`) has its own record,
`<name>.whole-problem.alignment.sqlite`, whose `meta` also binds the scope.
Without a PR it binds a repository `subject` instead and invents no PR.

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
  provisional block;
- a caller without alignment evidence (`inputs=None`), and read-only `Records`
  that give the same outcome, refuse a foreign run or PR and create nothing.

[`tests/test_dk_pr_fix_alignment.py`](../tests/test_dk_pr_fix_alignment.py)
drives the installed gate. It writes the retained record with the library
ledger, as the alternatives entries keep it, counts stage sessions, and uses the
fail-closed fake GitHub transport of the incoming-review tests. It covers:

- no record: review is ungated and no record is created;
- dk-workflows#239 through the installed run: a mismatch verified at an earlier
  head with no receipt gates the later head (`later_head`). The verified finding
  still posts as one `COMMENT`, and readiness and merge are withheld. One
  alignment refresh at the new head releases it, with no repeated comparison,
  selection, reviewer or verifier session;
- dk-workflows#241: an uncertain alignment keeps review unclean, and
  `completion` ends unready. New requirements refresh it through the library,
  and only then is the review clean;
- unsettled analysis gates review, and the gate leaves the record's bytes
  unchanged;
- a verified mismatch at the current head keeps a dk-pr-fix run unready;
- provisional identity marks the gate and still withholds findings;
- foreign and unsupported records stop the gate;
- the library and the helpers share one gate.

[`tests/test_dk_pr_review_findings.py`](../tests/test_dk_pr_review_findings.py)
checks the same rule through the installed incoming-review helpers
(`test_partial_findings_publish_after_identity_reconciliation_and_never_while_provisional`):
authorized partial findings analyzed while PR metadata lags are withheld, then
published as one `COMMENT` once the identities agree, with no repeated reviewer
or verifier session and no clean review or readiness.

## What remains

- Running these stages as engine-managed native sessions needs Agent Workflow
  #129 and #130 in a release. The installed alternatives entries and a composed
  `pr-alternatives` or `issue-alternatives` occurrence run them through the
  [staged procedure](alternatives.md#installed-workflow), and the installed gate
  reads the result, but neither has run through a released engine: Agent
  Workflow #129, #130 and #139 are on its `main` but in no release.
- Posting an eligible finding goes through [exact-intent
  publication](publication.md). Incoming review applies the same rules in its
  [finding publication](review.md#finding-publication); it does not call this
  library function. The installed test above covers partial findings through
  identity reconciliation.
- Real GitHub conformance and native-session adherence need separate
  authorization. These fixtures do not establish them.
