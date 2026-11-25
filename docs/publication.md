# Exact-intent publication

Every remote write of [`cf-workflows:dk-pr-fix`](dk-pr-fix.md) goes through one
boundary: [`prfix/publication.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/publication.py).
Native sessions, including sessions that load user-supplied skills, only propose
operations in their reports. They receive no GitHub credentials, and no other
helper sends a write. This implements the exact-intent part of the
[application-state contract](contracts/v1/state.md#exact-intent-publication).

## Kinds and authority

| Kind | Contract action | Workflow permission input |
| --- | --- | --- |
| `push`, `rewrite` | `push` | `push_commits`, `rewrite_commits` |
| `edit_pr` | `edit_pr` | `edit_pr` |
| `reply`, `reply_inline` | `reply` | `reply_to_feedback` |
| `resolve_thread` | `resolve_thread` | `resolve_threads` |
| `split_push`, `split_pr` | `create_pr` | `create_draft_prs` |
| `request_review` | `request_review` | `request_review` |
| `ci_rerun` | `rerun_ci` | `rerun_ci` |
| `submit_review` (`COMMENT` or `REQUEST_CHANGES`) | `post_findings` | `post_findings` |
| `merge` | `merge` | `merge` |

A write needs both the permission input and an explicit request authorization
for its contract action. Every permission defaults to false, and an iteration
or another action's grant never stands in.

Preflight enables a permission input only when the action is enabled by
accepted configuration and explicitly authorized. An authorization without the
enabled permission rejects instead of being dropped. `rewrite_commits` stays
false because history rewrites have no contract action yet. These actions reject
at preflight, and their kinds deny at preparation:

| Action | Owner |
| --- | --- |
| `approve_pr` | #31 |
| `create_issue`, `edit_issue` | #32 |
| `approve_fork_ci` | #35 |

**Extension contract.** A new kind needs all of the following before it is
supported: its contract action and permission input, an exact payload schema,
one dispatch call, a read-only observation that recognizes only the exact
effect, an effect key when it creates a unique object, and preparation,
dispatch and reconciliation fixtures with response loss before and after the
effect. Until then it stays in `EXTENSIONS` and denies.

## Intents

Preparation records an immutable intent in the logical run's SQLite ledger. The
intent holds:

- the numeric actor ID and numeric PR ID;
- head, lease, target, branch and head repository pins;
- the exact payload and the accepted source reports;
- the authorization and the contract action and permission input it relies on;
- the pre-dispatch baseline, such as existing comment, review or PR numbers.

The ID is stable. Preparing the same ID again returns the original intent, and
any change to the kind, pins, payload or sources is a conflict. An existing
actor-owned pending review refuses a review submission before any intent is
recorded (see [pending reviews](#pending-reviews)).

## Dispatch

Dispatch rechecks everything against the current plan and the live service:
authority, permission, sources, actor, PR identity, pins, incoming discussion,
remote branch lease, and the PR fields an edit would overwrite. Drift moves the
intent to `blocked` without a write. A merge also needs current readiness;
completing the selected plan never implies it (see [Merge readiness](#merge-readiness)).

The independent audience assessment of the exact intent fingerprint is
retained in the same transaction that takes the run's publication lock and
moves the intent to `pending`. Only then is the write sent.

## Merge readiness

Merge needs its own `merge` permission and authorization, and admission requires
the engine's `merge_guard` capability. Readiness, when the merge is proposed and
again at dispatch, needs all of the following:

- complete selected work, with current review and independent verification of
  the exact clean published head;
- successful CI for that exact head;
- an open, clean, mergeable, non-draft PR at the pinned head and target;
- a review decision of `APPROVED` or explicit `null`;
- a complete, paginated enumeration of review requests with none outstanding.

The repository check is Agent Workflow #138's corrected guard, consumed
unchanged (dk-workflows#253). Any pending user or team request holds merge,
including a request renewed after an approval of the same head, whatever the
aggregate decision. The hold names the pending users and teams and never
withdraws a request. A missing, malformed, unsupported or partial enumeration,
or an omitted or unknown decision, also holds.

At dispatch, the readiness callback recaptures coverage and CI, and the
repository read follows it immediately before the lock and the write. A
request, CI change or missing readiness found there keeps the intent `prepared`
with no write, so it can dispatch once requirements hold again. Head, target,
authority or permission drift blocks it. A request added after that final read
is not visible to the helper; the merge call still pins the head.

A merge whose response is lost stays `pending`. Reconciliation only looks for
the exact merge commit. An open PR, restarted helpers, a lost checkpoint and
unavailable request data never cause a second merge request.

## Journal

| State | Meaning |
| --- | --- |
| `prepared` | Never dispatched. Reconciliation reports `no_effect`. |
| `pending` | Dispatch may have happened. The intent holds the lock. |
| `applied` | The exact effect was observed. Its receipt is retained. |
| `not_applied` | The transport reported a conclusive refusal, with evidence, before any effect. |
| `blocked` | A dispatch-time recheck found drift. Never dispatched. |

Each intent also has an append-only event history: preparation, dispatch with
its audience, the acknowledged response, errors, rejection evidence, blocking
and the receipt. A lost response keeps the original intent and its acknowledged
response, if any.

## Reconciliation

Reconciliation is read-only. It settles a `pending` intent only when it
observes the exact effect: the comment with the acknowledged ID, the branch at
the exact head, the review with its exact inline comments, the merge commit
with the exact parents, and so on. An unchanged branch, a missing comment or
reviewer, a timeout or a generic error keeps the intent `pending`. Executing a
`pending` intent again only reconciles; it never sends the write again.

While an intent is `pending`, no other write of the run can dispatch. There is
no automatic override.

`not_applied` needs conclusive evidence from the original dispatch: a
transport that raises `Rejected` with evidence, or the
[pending-review rejection](#pending-reviews). Every other real failure stays
`pending`.

A `not_applied` or `blocked` intent is never resent. Preparing its ID again
creates a fresh attempt, `<id>.r<n>`. The attempt records which intent it
supersedes, takes a new baseline and needs its own audience assessment. At most
three fresh attempts follow an original.

## Pending reviews

GitHub allows one pending (draft) review per actor and PR. A pending review
owned by the authenticated actor is never submitted, deleted, overwritten or
adopted. Another account's draft is not the actor's pending review.

- **Preflight.** Preparing a `submit_review` captures the PR first. An
  actor-owned pending review refuses with `PendingReview`, which names the
  review IDs and the recovery step: submit or delete the draft on GitHub, then
  prepare again. No intent is recorded.
- **Dispatch.** The dispatch recheck repeats the check. A draft that appeared
  after preparation blocks the intent without a write.
- **Rejection.** A draft can still appear between the recheck and the request.
  For writes, the GitHub transport runs `gh api --include` and raises
  `HTTPFailure` with the exact status and JSON body; headers and standard
  error are dropped. Dispatch retains that failure in an `error` event before
  classifying it. Only HTTP 422 whose body is exactly `message: Unprocessable
  Entity` with `errors: ["User can only have one pending review per pull
  request"]` (and optionally `status: "422"` and `documentation_url`) is
  conclusive. It settles the intent `not_applied` with a `rejected` event that
  holds the classifier, the intent fingerprint and the body.
- **Uncertain.** Timeouts, transport errors, other or additional 422 errors,
  malformed bodies, other statuses, and the presence or absence of a draft now
  keep the intent `pending`. Reconciliation then needs the exact review, and
  nothing is resent.
- **Resumed recovery.** If the process stops after the failure was retained but
  before settlement, reconciliation (still read-only) reports `no_effect` from
  that retained original evidence, and executing the intent settles it
  `not_applied`. This needs exactly one dispatch, no acknowledged response and
  exactly one retained failure. It is decided before any observation, so the
  rejected intent never adopts a review that exists later, even one with the
  same text.
- **Later attempts.** A `not_applied` intent is never resent and never gets a
  receipt. Preparing its ID again refuses while the draft remains. Afterwards
  it creates a fresh attempt with the original, unchanged payload, a new
  baseline and its own audience assessment.

## Receipts

A receipt binds the intent ID, fingerprint, run, kind, PR, head, actor and the
observed result. An effect that creates a unique object (a comment, review,
branch head, draft PR, merge commit or CI attempt) can settle only one intent.
A second intent that observes the same object stays unsettled. Recording a
different receipt for an applied intent is refused.

## Ledger

Ledger schema version 3 adds the `audience` column, `publication_events`,
`publication_effects` and the single-row `publication_lock`. Opening a version 2
ledger migrates it in place: `uncertain` becomes `pending` and takes the lock,
`completed` becomes `applied`, and a retained acknowledged response moves into
the event history. Version 2 reset some dispatched writes to `prepared` with a
no-effect proof from absence. Such a write was sent, so it also becomes
`pending`, and its proof is kept only as history. A migrated ledger can hold
several pending intents. Dispatch refuses while any of them is pending, and
settling the lock holder hands the lock to the next one.

The lock belongs to one logical run. Two logical runs on the same PR do not
share it. For them, the head lease, `--force-with-lease`, the discussion
baseline and the unique-effect check remain the guards.

## Verification

[`tests/test_dk_pr_fix_publication.py`](../tests/test_dk_pr_fix_publication.py)
uses a fresh state namespace and a fake GitHub transport that fails every
unmodeled route. Git kinds push to disposable bare repositories. For every
supported kind, it prepares, dispatches and reconciles with no fault, with
response loss before the effect and with response loss after it. It asserts the
dispatch count, the journal state, the lock and that reconciliation does not
write to the ledger. It also covers:

- each drift class blocking without a write;
- payload conflicts and bounded fresh attempts;
- conclusive rejection, and rejection without evidence;
- pending reviews, in
  [`tests/test_dk_pr_fix_pending_review.py`](../tests/test_dk_pr_fix_pending_review.py),
  which ports the dk-workflows#260 scenarios. Its fake review endpoint refuses
  like GitHub while the actor owns a draft. It covers preflight, a draft
  created before the dispatch recheck and one created after it, eleven
  ambiguous failures with a draft present, a timeout with no draft, conclusive
  rejection after the draft is gone, resumed recovery in a fresh process next
  to an identical later review, a fresh attempt that cannot reuse the original
  identity or receipt, and the transport's status and body parsing;
- duplicate receipts;
- a second writer refused by the lock;
- a restart that keeps the intent and its audience;
- preserved pending reviews;
- merge without readiness;
- merge readiness (`MergeGating`): renewed same-head user and team requests
  under `APPROVED`, `null`, empty and `REVIEW_REQUIRED` decisions; missing,
  malformed and unavailable enumeration; incomplete review, verification and
  CI; head, target, authority and permission drift; requests and CI changes
  after preparation, including a request that lands after the readiness
  recapture; no-request paths; and lost merge responses with a lost
  checkpoint. Each asserts zero or one writes and the number of request reads;
- unavailable kinds and disabled defaults denied with no service call;
- the version 2 ledger migration;
- writes only inside the publication dispatch and no credentials in native-session steps.

[`tests/test_dk_pr_fix.py`](../tests/test_dk_pr_fix.py) covers the preflight
mapping of permissions and authorizations. The ported example tests carry the
upstream #138 fixtures for the guard itself.

## What remains

- Running the workflow, and so real dispatch through the engine, needs Agent
  Workflow #129 and #130.
- The pending-review classifier matches the exact 422 body reported
  in dk-workflows#260. Live conformance of that body needs separate
  authorization.
- Approval, issue and fork-CI kinds: #31, #32 and #35.
- While analysis is [provisional](provisional.md), nothing is listed, prepared
  or dispatched.
- GitHub publication conformance needs separate authorization; these fixtures
  do not establish it.
