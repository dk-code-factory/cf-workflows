# Feedback read, address and reply entries

`cf-workflows:dk-pr-feedback-read`, `cf-workflows:dk-pr-feedback-address` and
`cf-workflows:dk-pr-feedback-reply` are thin entries over the installed
`dk-pr-fix` workflow (#30).
[`cf_workflows/dk_pr_feedback.py`](../cf_workflows/dk_pr_feedback.py) defines
them. Their adapter procedures are the bundled adapters and the installed helpers
in [`prfix/responses.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/responses.py)
and [`prfix/feedback.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/feedback.py).
They add no orchestration and load no skill. They don't run yet (see
[What remains](#what-remains)).

## Entries

Each entry has these parts:

- A preset selection for `workflows/main.yml`, with explicitly empty finalizers
  and reviewers.
- The bundled adapters that selection runs, all with the admitted `leaf` role, and
  their helper operations.
- The entry's original role prompt and options from the pinned baseline. Each
  role prompt matches its inventory digest byte for byte.

| Entry | Preset tasks | Original options | Remote writes |
| --- | --- | --- | --- |
| `dk-pr-feedback-read` | `read-feedback` | none | none |
| `dk-pr-feedback-address` | `read-feedback`, `reconcile-decisions`, `address-feedback` | `additional_validation_commands` | none (a local controlled commit only) |
| `dk-pr-feedback-reply` | `{stage: responses}` | `reply_prompt`, `resolve_answered_threads` | `reply`, `resolve_thread` |

`preflight()` is the shared `dk-pr-fix` preflight. It runs the same structural
admission, installed-closure check, state namespace and staged request. The
preset is applied at invocation precedence, so a configured task selection for
`dk-pr-fix` never changes an entry. Configured permissions, accounts and limits
still apply. A configured replacement of a bundled adapter is custom input, and
the installed workflow rejects it.

An invocation may add only `permissions`, and only for the entry's own actions.
These reject before the engine is contacted:

- tasks, prompts, skills or preferences in the invocation;
- an authorization for another action, such as `push` for the address entry;
- `resolve_thread` while `resolve_answered_threads` is false;
- an unknown option.

The entries use the PR's default logical run, the same one `dk-pr-fix` uses. So
standalone and composed runs share one run ledger, one feedback record and one
publication journal.

Options are honored only where the installed workflow carries them:

- `reply_prompt`: the default from the baseline `config.json` is part of the
  shared `reply-feedback` assignment, so standalone and composed responders both
  apply it. A replacement is custom prompt text and rejects.
- `resolve_answered_threads`: false keeps threads open, because the
  `resolve_thread` authorization rejects and the `resolve_threads` input stays
  false.
- `additional_validation_commands`: only the default `[]` is accepted. Settings
  carry booleans and integers only, so commands have no installed input yet.

## Reading

`read-feedback` occurrences, standalone or composed, follow one contract:

1. The capture reads every page: issue comments, review bodies and inline
   comments through the REST pages, and review threads through the GraphQL
   pages. The gh transport rejects these cases:
   - a short or changing count;
   - a duplicate row across pages;
   - a cursor that does not advance;
   - a PR that changed during the capture.

   The feedback view also rejects an inline comment that has no enumerated
   thread, because that means a truncated thread enumeration.
2. The claim adds `feedback` to the assignment: every current source with its
   stable identity (kind, ID, author ID, thread root), thread, body fingerprint
   and current retained assessment.
3. The report's `deliverables.feedback.assessments` must leave every current
   source assessed. A source with a current assessment may be omitted. A read
   decides no implementation: a request may only be `none` or keep its settled
   decision. Otherwise the report gets a bounded same-session correction.
4. The assessments are retained in the feedback record, with the full
   [obligation rules](feedback.md), only after the report is accepted.

A new or edited source makes the read stale, through the discussion digest, and
leaves that source unassessed. Coverage then lists the read as missing.

## Addressing

`address-feedback` follows the same contract, with two differences:

- A request decided `fixed` needs this report's changes, or an earlier retained
  `fixed` decision for the same request.
- The assessments are retained only after the controlled commit. A report that
  is rejected or never committed leaves no decision behind.

The feedback record keeps these rules for every later task:

- A declined (`rejected`, `intentional`) or `deferred` author request still owes
  an answer (dk-workflows#244).
- A settled implementation decision can't change for the same source version.
  Only an edited source can be decided again.
- A read, a responder or a composed run that tries to change a settled decision
  gets a correction.

A composed run after a standalone address therefore reaches the same occurrence
IDs. Its sessions receive the retained decisions, and a retained fix needs no
new edit or commit. A resumed run of the same execution reuses every current
occurrence and starts no session.

`coverage` returns `feedback` for plans that read or address feedback: every
source with its requests and settlement, `replies_needed` and `unanswered`. These
are the remaining response obligations. The address entry publishes nothing.
Pushing the fix needs [publication](publication.md) through `dk-pr-fix` or
`dk-pr-publish` (#31).

## Replying

`dk-pr-feedback-reply` runs the shared [response stage](responses.md) alone: one
responder occurrence, reply intents, settle, resolution intents and status. The
composed plan runs the same stage twice. Both share the same evidence, the same
exact-intent publication and the same rules for when a thread may be resolved.
A source answered by either run gets no second reply.

The stage completes only when `settled` is true. That needs every owed reply
verified, and no reply or resolution of this run left in one of these states:

- `pending`: the outcome is unknown, so it is never resent;
- `not_applied` or `blocked`, while its source still needs an answer or its
  thread is still open.

A denied permission or authorization reports `blocked`. In every one of these
cases, coverage lists `feedback_responses` as missing, so the reply is never
silently counted as answered. A later fresh attempt that is observed settles it.

## Verification

[`tests/test_feedback_skills.py`](../tests/test_feedback_skills.py) uses
disposable repositories, a fresh state namespace and fail-closed engine and
GitHub transports. Any other operation raises, with no live fallback. The tests
assert native dispatch, capture, page and write counts. They cover:

- **Preflight.** Presets and adapters, the shared logical run, baseline prompt
  digests, the shared reply prompt in `main.yml`, configured and invocation
  overrides, per-entry authority and options.
- **Read.** 252 sources over two REST pages per collection and two GraphQL
  thread pages, through the real gh capture and fake routes. A wrong thread
  count, a duplicate row, drift and a missing thread never complete. A read that
  omits a source or decides an implementation gets a correction. An edited source
  is read again, and its obligation is kept.
- **Address.** Mixed feedback with changed, deferred and rejected suggestions, a
  participant question and an already answered thread. A rejected bot style
  suggestion that declines its answer, or an unsupported `fixed`, gets a
  correction. Remaining obligations are reported. The test also covers a resume
  with zero sessions, and a composed `dk-pr-fix` run that keeps every decision
  and makes no second commit.
- **Reply.** A standalone stage followed by both composed stages with no further
  write. Refused and uncertain resolutions, a refused reply, and denied
  permission or authorization each stay incomplete. A lost reply response is
  reconciled on resume without a resend.

## What remains

- The installed graph, responder, reader and fixer sessions need Agent Workflow
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129) and
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130). No local
  controller or launcher stands in. The fixtures drive only the helpers.
- Plugin skill entries (`SKILL.md` and descriptor rows) wait for that runtime
  ([packaging](packaging.md)). The three inventory rows stay `not_implemented`.
- These features have no installed input yet: a custom `reply_prompt` and
  `additional_validation_commands`. Supplied skills need #16.
- Real GitHub conformance needs separate authorization. One open question is the
  thread root that GitHub reports for replies to thread replies.
