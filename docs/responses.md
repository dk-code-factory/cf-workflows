# Feedback response stage

[dk-workflows#255](https://github.com/dk-code-factory/dk-workflows/issues/255):
independent review could block finalization after fixes were published, so the
original feedback stayed unanswered. In `cf-workflows:dk-pr-fix`, the
`responses` stage answers feedback after the fix publication and before the
first review stage. A later review failure keeps those replies settled.

## Selection

The default plan selects the stage twice:

```yaml
tasks: [read-feedback, reconcile-decisions, address-feedback, {stage: publication},
        {stage: responses}, assess-scope, describe-pr, {stage: reviews}, {stage: publication}]
finalizers: [inspect-commits, sync-guidance, {stage: responses}, {stage: ci}]
```

`{stage: responses}` takes no other controls. The legacy selector
`dk-workflows:dk-pr-feedback-reply` maps to it. Each stage runs the read-only
`reply-feedback` adapter (kind `reply`, role `leaf`). Its assignment carries the
default `reply_prompt` of [dk-pr-feedback-reply](feedback-skills.md), which also
runs this stage alone. Every response stage of a
plan shares this one responder occurrence. A later stage reuses its current
result, so the finalizer stage reruns the responder only when the discussion or
the diff has changed. A `reply` adapter cannot write and cannot be selected as a
plain task; admission and preparation reject both.

## Responder report

The responder returns its assessments and drafts in
`deliverables.feedback`:

```json
{"assessments": [{"source": "inline:30", "fingerprint": "…", "requests": […]}],
 "replies": [{"source": "inline:30", "body": "Renamed to `count`."}]}
```

The assessments follow the [feedback obligation model](feedback.md). When the
report is accepted, the assessments are retained in the run's feedback record.
The report is rejected, with a bounded same-session correction, in these cases:

- an assessment is invalid or drops an earlier obligation;
- a current source has no assessment;
- a source that still needs an answer has no drafted reply;
- a draft names a source that does not exist.

## Stage steps

[`responses.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/responses.yml)
runs these steps in order, with helpers from
[`prfix/responses.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/responses.py):

1. The responder occurrence runs, or its current result is reused.
2. `publication_list` with `kind: responses` lists exact proposals:
   - It lists nothing until the local head is published and clean, and nothing
     while analysis is provisional. Replies follow the fix publication.
   - It lists one reply for each source in `replies_needed`, and only while the
     `reply` permission is effective. An inline source gets a `reply_inline` in
     its thread, addressed to the thread's root comment because GitHub accepts
     replies only there; a follow-up comment is answered the same way. A
     comment or review body gets a PR comment that links the source's exact
     URL. The URL is prepended unless the draft already links it
     as a whole link, by the same rule `feedback.View.answers` applies. A link
     to `#issuecomment-401` does not link comment 40.
   - Settled sources get nothing, so a reply is never repeated.
   - Each reply intent records the source's kind, ID and body version
     (`source.version`) next to the raw-item digest that pins dispatch. Matching
     uses kind, ID and version, so reactions or metadata changes don't matter.
     If a reply to the same source body already exists, no new reply is listed:
     - A `pending` original is listed first, for read-only reconciliation only.
       It keeps the run's publication lock until it settles.
     - An `applied` original that is not recorded yet is not relisted.
       `responses_settle` records it from its stored receipt.
     - If a recorded reply was later removed, the helper reports a blocker and
       does not answer again.
3. Each proposal goes through [exact-intent publication](publication.md) with
   its own independent audience.
4. `responses_settle` records every applied reply of the run once the reply is
   observed in the source's own location. A reply to an earlier body version
   never settles an edited source.
5. A second `kind: responses` listing proposes `resolve_thread` for threads
   answered by an applied inline reply of this run. The listing proposes it only
   when `resolution()` allows it: `resolve_threads` is enabled and authorized,
   every source is answered, and no participant is still awaited.
6. `responses_status` reports the stage.

When a native report proposes a `reply` or `reply_inline` with a feedback
`source`, the ordinary publication barrier skips it if a response stage is
selected. Those replies belong to the response stage.

## Status and completion

| Status | Meaning |
| --- | --- |
| `complete` | Every source is assessed, and each required request has a verified reply or answer |
| `pending` | Replies are still owed and the `reply` permission is effective |
| `blocked` | Replies are owed, but the `reply_to_feedback` input or the `reply` authorization is missing |
| `incomplete` | A new or changed source has no current assessment |
| `not_selected` | The plan selects no response stage; the status carries a blocker that says so |

`uncertain` lists reply and resolution intents that are still `pending`. Each
one adds a blocker to reconcile it. An applied reply that settle cannot record
also adds a blocker. That happens when the reply no longer answers the source
in its own location. A denied, uncertain or unrecordable action never counts as
a reply.

`refused` lists replies and resolutions whose latest attempt is `not_applied` or
`blocked` while the source still needs an answer or the thread is still open.
Each adds a blocker. `settled` is true only when the status is `complete` and
nothing is uncertain or refused. So an uncertain or refused thread resolution
also keeps the stage open, even after every reply is verified.

With a response stage selected, `coverage` adds `feedback_responses` to
`missing` until `settled` is true. In that case the plan, and so
`completion`, is not complete. `coverage` and `readiness` always return the
`responses` status. Custom and empty plans report `not_selected`, never
`complete`.

A later review failure changes none of this. A replacement run on the same
logical run reopens the same run ledger and feedback record. Settled sources get
no new proposal. A new or edited source needs a fresh responder assessment, and
then only that source is answered.

## Verification

[`tests/test_dk_pr_fix_responses.py`](../tests/test_dk_pr_fix_responses.py) uses
a fresh state namespace and a fake GitHub transport. The transport counts
captures and writes and fails every unmodeled route. The tests cover:

- dk-workflows#255: replies wait for the fix publication, settle and resolve
  before a required review fails, and two resumed stages send no further write;
- loss of the reply response after the effect, which reconciliation settles
  without a resend;
- loss before the effect, which stays `pending` and blocks the run, and is
  never resent;
- an edited comment, which gets exactly one new reply;
- resolution with the permission disabled and with the authorization withdrawn;
- reply authority denied through the permission and through the authorization;
- responder reports that skip a source, omit a draft or decline a rejected bot
  suggestion without an answer;
- unselected and empty plans;
- native reply proposals to feedback in the ordinary barrier;
- a reaction between a reply's publication and settle, after which the original
  is still reconciled and recorded with no resend;
- a draft linking an ID-prefix URL, which still gets the exact source link;
- an applied reply that cannot be recorded, which raises a visible blocker;
- provisional analysis, which withholds every reply;
- the shared responder occurrence, and rejection of a plain responder task.

## Not established here

- The YAML graph, responder sessions and audience sessions need a released
  engine with Agent Workflow
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129) and
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130), which
  are merged on its `main` but unreleased. The stage's claim, listing,
  publication and settle helpers run in containers and need the state namespace
  mounted ([Agent Workflow
  #149](https://github.com/dk-code-factory/agent-workflow/issues/149)), and
  `plugin test` rejects container helper jobs ([Agent Workflow
  #150](https://github.com/dk-code-factory/agent-workflow/issues/150)), so the
  stage has no installed fixture run either. The fixtures drive only the
  helpers. The fix push in the #255 case is simulated: exact-intent push has its
  own fixtures.
- Real GitHub conformance needs separate authorization. One example is whether
  inline replies to a thread reply report the thread root as `in_reply_to_id`.
