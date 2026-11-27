# Publication and review-posting entries

`cf-workflows:dk-pr-publish` and `cf-workflows:dk-pr-review-post` (#31) are thin
wrappers over the shared installed workflow
([`cf_workflows/dk_pr_publish.py`](../cf_workflows/dk_pr_publish.py)). Each
entry is a preset selection, the admitted adapter procedure that the preset
runs (roles, assignments and helper operations) and the entry's baseline
options. They add no orchestration, no second publication journal and no
other effect owner: every write is an exact intent of the shared
[publication boundary](publication.md). No skill is loaded.

Both entries are listed in the plugin descriptor
([`plugin.yaml`](../cf_workflows/workflows/dk-pr-fix/plugin.yaml)), each with
its own `SKILL.md` and installed entry workflow, the same way `dk-pr-review`
is exposed as `review.yml`:

- [`publish.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/publish.yml)
  first runs the installed `entry_preset` helper on the staged request. That
  helper returns the request's one preset or rejects it. Then exactly one job
  runs `main.yml`: `changes` in fix mode or `merge` in review mode. The merge
  path takes dk-pr-review's selections as `review_tasks`, `review_reviewers`
  and `review_adapters`.
- [`review-post.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/review-post.yml)
  runs `main.yml` in review mode, which publishes only through the shared
  `findings` and `approval` jobs.

Their `workflow_call` defaults carry the presets, and both pass an `entry`
input to `main.yml`.

## Presets

| Entry | Requested actions | Preset | Workflow and mode | Logical run |
| --- | --- | --- | --- | --- |
| `dk-pr-publish` | `push`, `edit_pr`, `reply`, `resolve_thread`, `request_review`, `create_pr`, `rerun_ci` | `prepare-publication`, then `{stage: publication}`; no finalizers or reviewers | `publish.yml` (`changes` job), fix | The PR's default run, shared with dk-pr-fix |
| `dk-pr-publish` | `merge` only | dk-pr-review's reviewers and verifier, then the merge job | `publish.yml` (`merge` job), review | The PR's incoming-review run |
| `dk-pr-review-post` | `post_findings`, `approve_pr` | dk-pr-review's reviewers and verifier, then the findings and approval jobs | `review-post.yml`, review | The PR's incoming-review run |

The adapter procedures:

- **`prepare-publication`** is a bundled leaf adapter (`admission.ENTRY_ADAPTERS`).
  Its read-only session turns the exact request into proposed operations with
  exact payloads. The helpers `publication_list`, `publication_prepare`,
  `publication_execute`, `publication_record` and `publication_recover` publish
  them, after an independent `audience` session assesses each exact intent.
  The ordinary listing never lists a native `merge` or `approve_pr` proposal.
- **Merge** comes only from `merge_proposal` under the shared readiness: current
  review and independent verification of the exact clean head, successful
  exact-head CI, a clean PR and a complete enumeration with no pending user or
  team review request. Merge needs current review, so a merge request runs the
  incoming-review preset. A current review in that run is reused without a
  session, because review-mode permissions do not bind analysis
  (`capture.py`). A merge cannot be combined with other actions in one call,
  except a separately authorized fork workflow approval through the
  [standalone-to-merge bridge](ci-skills.md#standalone-to-merge-bridge).
- **CI reruns** (`rerun_ci`) are the user's explicit retries: a proposal may
  carry `user_retry`, equal to the retained `rerun_ci` authorization, which
  binds one exact attempt per run and request identity and never consumes the
  automatic per-head allowance
  ([failure diagnosis and reruns](ci-skills.md#failure-diagnosis-and-reruns)).
- **Review posting** is [finding publication](review.md#finding-publication)
  (`findings.py`): verified findings inline only on the changed line they
  name, every other substantive finding in the body, summary suppression, and
  `COMMENT` for every partial review.
- **Approval** comes only from `approval_proposal` (see [approval](#approval)).

Preflight is the shared dk-pr-fix or dk-pr-review preflight. The preset is
applied at invocation precedence, so a configured task selection never changes
it. An invocation may add only `permissions`, and only for the requested
actions. Prompts, skills, other options and selection overrides reject. A
default run makes one capability query and starts no validator session.

## Declared capabilities

`CAPABILITIES` declares each public action once: its entry, preset,
publication kinds and workflow permission input. Each supported publication
kind except `rewrite` (which has no contract action) belongs to exactly one
action. Before the engine is contacted, preflight rejects all of the
following:

- a call with no authorization: a configured permission never supplies one;
- an action of the other entry: findings and approval belong to
  dk-pr-review-post, merge and branch or PR writes to dk-pr-publish;
- `approve_fork_ci`, which belongs to `dk-ci-approve` ([CI entries](ci-skills.md));
- `create_issue` and `edit_issue` (written only by [dk-issue](drafting.md), #32),
  and any undeclared action;
- `merge` together with any other action.

An authorization without the enabled permission rejects, as in dk-pr-fix. A
permission enabled by configuration but not authorized stays false.

The installed helpers repeat this routing inside the package, so a direct run
of `publish.yml`, `review-post.yml` or `ci-approve.yml` that skips host preflight
still rejects before any session or effect. `prfix/prepare.py` holds
`ENTRY_ACTIONS` (each action's entry and preset, including dk-ci-approve's
`approve_fork_ci`), `ENTRY_OWNERS` (issue writes), `ENTRY_BRIDGES` (the merge
preset's separate `approve_fork_ci` grant) and `ENTRY_PRESETS` (each preset's
mode, stage kinds and leading adapter).
These checks run inside the package:

- **`entry_preset`.** The `admit` job of `publish.yml` runs it on the staged
  request. It rejects a missing authorization, an action of the other entry or
  of another owner, an undeclared action, and `merge` mixed with other actions.
  Only a successful result starts a preset job, and the result is retained, so
  a lost response is answered without a second read.
- **`prepare`.** It runs again with the `entry` control. It applies the same
  rules and also rejects the following: a preset in the wrong mode, any other
  stage shape or leading adapter, a finalizer, and a writing task.

## Approval

`approve_pr` is a supported publication kind under the
[extension contract](publication.md#kinds-and-authority). It has its own
contract action and permission input, and it is never implied by
`post_findings`, merge or a complete plan.

- **Proposal.** `approval_proposal` constructs it in review mode only, with an
  explicit authorization and the `approve_pr` permission. All of these must
  hold: complete required coverage, a current ordinary reviewer, a current
  independent verifier, no confirmed or unverified finding, a reconciled head,
  the exact clean published head, and another author's PR. The PR must not be
  authored by the actor or by a configured personal account on its host. The
  payload is `{body: ""}`, and the ID binds the run, head, target and grant.
- **Dispatch.** One `POST pulls/N/reviews` with event `APPROVE`. It rechecks the
  grant, the permission, the own-PR rule and the `approval` callback for the
  exact head and target, and it checks for pending reviews the same way as a
  review submission. An actor-owned draft refuses preparation, blocks
  dispatch and settles conclusive 422 rejection as `not_applied`.
- **Reconciliation.** It needs the actor's `APPROVED` review with the exact
  body, the exact head and no comments. The effect key is the review ID.

dk-pr-fix never approves: preflight and `prepare` reject `approve_pr` outside
review mode. Approval establishes no merge readiness.

## Options

| Entry | Option | Default | Mapping |
| --- | --- | --- | --- |
| dk-pr-publish | `default_merge_method` | `squash` | `merge_method` of the merge preset |
| dk-pr-publish | `review_requests` | `null` | `null` or two empty lists only; automatic finalization requests need dk-pr-approvers (#29) |
| dk-pr-review-post | `include_summary` | `false` | `settings.include_summary` |
| dk-pr-review-post | `blocking_event` | `REQUEST_CHANGES` | `settings.request_changes` (`COMMENT` sets it false) |
| dk-pr-review-post | `nonblocking_event` | `COMMENT` | `COMMENT` only; partial reviews always comment |

A review-post option given at invocation sets only the setting it maps to.
Every other configured setting is kept, so review analysis stays current. When
the result differs from the configured settings, preflight admits the merged
settings in a second capability query. Neither option can request `APPROVE`.

## Direct and composed use

Both forms use the same journal, intents, audience verdicts, receipts and
uncertainty states:

- A direct dk-pr-publish run and a composed dk-pr-fix run share the logical run.
  An exact effect that already exists settles a later intent without a second
  write.
- dk-pr-review-post shares dk-pr-review's run. With the same instruction, a
  direct post reuses the composed review without a reviewer or verifier
  session. A composed rerun that is authorized to post finds the findings
  already reserved by the applied submission.

## Verification

`tests/test_publication_skills.py` uses a disposable repository, a fresh state
namespace and a fail-closed fake GitHub. The fake answers only capture, PR
edits, review requests, review creation and approval, review-comment reads, CI,
the merge requirements query, merge and merge-commit reads. Native and audience
dispatches and writes are counted. It covers:

- **Presets.** Inputs per preset, baseline prompt digests, the capability
  table against `SUPPORTED` and `ACTIONS`, and rejection of misrouted,
  unsupported, mixed and unauthorized actions and preset overrides with zero
  engine calls. Configured permissions never supply authority, and dk-pr-fix
  rejects approval.
- **Installed entries.** Each admitted preset's inputs are declared by its
  installed entry workflow and passed to the right `main.yml` job with its mode
  and `entry`. The installed defaults are the preset, and the descriptor rows
  name the entry workflows. The installed `ENTRY_ACTIONS` table agrees with
  `CAPABILITIES`. Each entry runs exactly its preset. The installed
  `entry_preset` and `prepare` reject foreign, undeclared, owned-elsewhere,
  unauthorized and mixed actions, wrong modes and preset overrides, with no
  write and no session. The `admit` helper routes a staged request once,
  answers a lost response from its retained result, and retains nothing for a
  rejected request.
- **dk-pr-publish changes.**
  - Exact intents, audience and receipts, published once and reconciled on
    rerun.
  - Native merge and approval proposals are never listed.
  - A denied action has no write.
  - Payload drift blocks without a write, and only a refreshed session
    proposes again.
  - A lost response before the effect stays pending and is never resent. A
    lost response after the effect is reconciled.
  - The composed dk-pr-fix publication settles the existing effect with no
    second write.
- **dk-pr-review-post.**
  - Direct posting after a composed review needs no new reviewer or verifier
    session, puts the body-only finding in the body with the summary
    suppressed, and comments when `blocking_event` is `COMMENT`. A composed
    rerun adds nothing.
  - A partial review comments and is never approved.
  - A clean review is approved once with an empty body.
  - Approval needs its own grant and never applies to the actor's own PR.
  - An existing draft refuses approval before any intent. A draft after
    preparation blocks the approval. A lost response before the effect is never
    resent, and one after the effect is reconciled.
  - At unit level, the approval kind is tested with lost responses, own-PR and
    configured-account refusal, missing or foreign evidence and its own
    permission and grant.
- **Direct merge.**
  - Pending user and team requests hold merge before and after preparation.
  - A native merge proposal never becomes a write.
  - A moved head withholds publication.
  - A lost merge response is reconciled without a resend.

## What remains

- **Runtime.** Both entries are in the descriptor, and `plugin check` from
  Agent Workflow main accepts it. Running their installed workflows with
  engine-managed sessions needs a released engine that contains Agent Workflow
  #129, #130 and #139. Those issues are closed upstream, but no release ships
  them yet, and the plugin pins the unreleased engine 0.2.2. No local
  controller or launcher stands in for them, so both inventory rows stay
  `not_implemented` and their native execution stays `pending`.
- **Baseline features without an equivalent.**
  - Historical finding follow-ups: reusing an equivalent open thread, and
    follow-up publication for bot, human and earlier-run reports.
  - The baseline's `inspect-reply`/`dispose-reply` and
    `inspect-review-request`/`dispose-review-request` dispositions.
  - Automatic finalization review requests (dk-pr-approvers, #29).
- **Local commits.** A direct dk-pr-publish does not publish local controlled
  commits made by another plan, because their source belongs to that plan.
  Resuming that dk-pr-fix plan with push authority publishes them.
- **Owned elsewhere.** Fork-CI approval is the `dk-ci-approve` entry
  ([CI entries](ci-skills.md)), and issue writes belong to
  [dk-issue](drafting.md) (#32).
- **Live evidence.** Live provider adherence and GitHub publication conformance
  need separate authorization. For example, these fixtures do not prove that
  GitHub accepts an `APPROVE` with an empty body.
