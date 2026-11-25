# Feedback obligations

[`prfix/feedback.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/feedback.py) keeps three decisions
apart for every PR feedback source, as required by
[dk-workflows#244](https://github.com/dk-code-factory/dk-workflows/issues/244):

| Decision | Values | Owner of the effect |
| --- | --- | --- |
| Implementation | `fixed`, `intentional`, `rejected`, `deferred`, `none` | The fixing task and its commits |
| Response | `reply`, `already_answered`, `no_reply` with a sourced reason | The [response stage](responses.md) through guarded publication (#17) |
| Thread resolution | Allowed or blocked, with reasons | A separately authorized `resolve_thread` publication |

The module is part of the installed helper closure, standard library only, so
helper containers can run it. The [response stage](responses.md) drives it.

## Sources

`observe(provider, pr)` reads the complete discussion with one `capture()` call,
the same shape the dk-pr-fix helpers capture: issue comments, review bodies,
inline comments and review threads. A source is any nonempty comment, review body
or inline comment whose author is neither the PR author nor the authenticated
publishing actor. Bots are sources like anyone else. Each source keeps its
identity (kind, database ID, author ID and inline thread root) and a fingerprint
of its body. Missing collections, a different PR, duplicate items and inline
replies to unobserved comments reject.

## Assessments

An assessment covers one source at its current fingerprint and lists its
requests. Each request quotes an exact excerpt of the source and records its
addressee, its implementation disposition and its response disposition:

- `author`: a concrete concern or request for the PR author. It needs a `reply`
  or an `already_answered` citation. `no_reply` rejects, whatever the
  implementation disposition, the author being a bot or a `category` such as
  `style`. Any implementation decision other than `none` makes the addressee
  `author`, so declining a change cannot reclassify its concern as optional.
- `participant`: a question for another named participant. The login must be
  mentioned in the source or have spoken earlier in the same thread, and must not
  be the PR author or the source's author. It takes `no_reply` with reason
  `addressed_to_participant`.
- `none`: discussion that needs no author reply. It takes `no_reply` with
  reason `no_author_reply_needed`.

Every `no_reply` carries a justification. `instructed: true` records an
explicit user instruction to answer and makes any request required.
`already_answered` cites a later comment by someone other than the source's
author, in the source's own location: the same inline thread, or for comments
and review bodies, a PR comment that links the exact source URL (a link to
`#issuecomment-123` does not answer comment 12).

The ledger keeps every assessment. Repeating an identical assessment adds
nothing. A reassessment may add obligations but never drop one whose excerpt the
source still contains, including after an edit: a required request stays
required under its ID, and a question for another participant stays addressed
to that participant unless it becomes required. Only an edit that removes the
quoted text releases it. An edited source has a new fingerprint and needs a new
assessment; the earlier record and replies remain.

## Response-stage completion

`Ledger.record_reply()` retains a published reply with its publication
reference only after the reply is observed from the PR author or actor, later
than the source, in its own location. An unobserved reply is never recorded:
uncertain dispatch stays with publication reconciliation. One verified reply
covers every required request of its source, including one whose cited answer
was later deleted.

`stage(ledger, view, permissions)` re-verifies every retained reply and answer
against the current observation and returns:

| Status | Meaning |
| --- | --- |
| `complete` | Every source is assessed and every required request has a verified reply or answer |
| `pending` | Required replies remain and the admitted `reply` permission is effective |
| `blocked` | Required replies remain without an effective `reply` permission; the obligations stay |
| `incomplete` | A source is new or changed and lacks a current assessment |

`replies_needed` lists each source once. A deleted reply reopens its source with
reason `reply_unavailable`. `unanswered` lists every `no_reply` request with its
reason and justification for the private report. Thread state never counts: a
thread resolved by anyone still owes its answer.

## Thread resolution

`resolution(ledger, view, thread, permissions)` allows resolving an inline thread
only when the admitted `resolve_thread` permission is effective, the thread is
open, every source in it is assessed and answered, and every participant
question in it has a later reply from that participant. It never settles a
response.

## Ledger

Each logical run has its own record, `runs/<sha256(logical_run)>.feedback.sqlite`
in the [state namespace](dk-pr-fix.md#state-namespace), next to the run ledger.
It holds source identities, assessments and recorded replies.
`PRAGMA user_version = 1` and a `meta` table bind the suite, kind, logical run and
PR. Any other version, database or binding is refused. A read-only open never
creates or changes the record; with no record yet, `Unrecorded` stands in.

## Not established here

- Producing assessments in native sessions: the responder still needs Agent
  Workflow #129 and #130 to run.
- Real GitHub conformance. The tests use a fake transport whose only operation is
  `capture()`.
