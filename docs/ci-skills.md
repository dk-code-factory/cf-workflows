# CI entries

`cf-workflows:dk-ci`, `cf-workflows:dk-ci-flakes` and `cf-workflows:dk-ci-approve`
(#35) are thin wrappers over the shared installed workflow. Each entry is a
preset selection, the admitted adapter procedure that the preset runs (role,
assignment and helper operations) and the entry's baseline options. They add
no orchestration, no second journal and no other effect owner. Every CI write
(a rerun or a fork approval) is an exact intent of the shared
[publication boundary](publication.md). No skill is loaded.

## Presets

| Entry | Preset | Workflow and mode | Logical run | Writes |
| --- | --- | --- | --- | --- |
| `dk-ci` ([`dk_ci.py`](../cf_workflows/dk_ci.py)) | `{stage: ci}` only, no finalizers or reviewers, zero repair allowance | `main.yml`, fix | The PR's default run, shared with dk-pr-fix | None |
| `dk-ci-flakes` ([`ci_flakes.py`](../cf_workflows/ci_flakes.py)) | One `inspect-flakes` task on the `leaf` role (`entries.preset`) | Entry procedure | None (read-only, nothing retained) | None |
| `dk-ci-approve` ([`dk_pr_publish.py`](../cf_workflows/dk_pr_publish.py)) | dk-pr-review's reviewers and verifier, then the `fork_ci` job | `review.yml`, review | The PR's incoming-review run | `approve_fork_ci` |

- **dk-ci** observes only. `ci-poll.yml` calls the installed `ci` helper
  operation ([`prfix/ci.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/ci.py))
  until checks settle. With no repair allowance, `prepare` adds no
  `ci_repair` session, so a direct observation needs no writer identity.
  Reruns publish through dk-pr-publish (`rerun_ci`); dk-ci takes no
  authorization.
- **dk-ci-flakes** collects bounded evidence, runs one read-only report
  session and checks it (`collect`, `check_report`). Like
  `discover-approvers`, the `inspect-flakes` adapter runs only in its entry.
- **dk-ci-approve** is a declared capability of the publication table
  (`CAPABILITIES['approve_fork_ci']`), so it shares the publication entries'
  preflight: an explicit grant, the enabled permission and no other action.

## Exact-head observation

`observe()` reads the PR head, observes that head's checks and classifies
them. A check is accepted only through:

- **success:** `success`, `neutral` or `skipped`;
- **known flakes:** a verified issue-backed flake of the exact failed attempt;
- **accepted exceptions:** a verified configured exception rule.

The result `outcome` uses the baseline names `success`, `known_flakes`,
`accepted_exceptions`, `failure` and `pending`; only the first three satisfy
CI. Readiness and merge observe again at dispatch, so a changed head is never
covered by an earlier result.

**Issue-backed flakes.** For a failed Actions check without configured
classifications, `learn()` reads the exact attempt's run, job and failed-step
logs, takes the tests the logs name and searches open issues of the PR
repository for each one. A candidate needs an explicit declaration of exactly
that workflow path, job name, test and signature, and the signature must occur
in these logs. The PR author controls those logs, so a declaration counts only
from a trusted author: the repository owner, a member or collaborator
(`author_association`), or a configured personal account, and never the PR
author. The issue must also have been opened before the failed run was
created. Without the PR author's identity nothing is learned.

````text
```dk-flaky
{"workflow": ".github/workflows/test.yml", "job": "Test", "test": "TestFlaky",
 "signature": "intermittent gateway timeout", "flaky": true}
```
````

A candidate is never authority. `verify_exceptions()` rereads the run, job,
logs and issue and binds them to the exact head, check, run, job and attempt.
It requires every named failed test and every diagnostic line of every failed
step to be covered, so one flaky test never exempts another test, a compiler
error or a non-test step. Configured classifications and exception rules keep
their existing form; when a check has them, nothing is learned for it.

**Shared registry and fail-then-pass history.** Observation saves named test
outcomes in a registry shared by every PR and logical run of the base
repository ([`prfix/registry.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/registry.py),
`registry/ci-tests.sqlite` in the state namespace). Each row binds the exact
head, workflow path, job, test, run, job ID and attempt:

- **Failures.** `learn()` saves each named failure it reads, with the
  normalized diagnostic lines of the failed-step logs.
- **Passes.** At a head with a saved failure, `observe()` reads the complete
  logs (`job_logs`) of each successful exact attempt of that workflow and job
  and saves the named passes (`--- PASS:` or `PASSED`). An attempt is read
  once per set of saved failures of its job (`reads`), whether or not its logs
  name one; a later saved failure reads it again. An unreadable or mismatched
  job saves nothing and is listed in `registry.gaps`; it never fails the
  observation.

A trusted issue's `dk-flaky` block that names the exact context but does not
affirm it (no `flaky` field) nominates a flake only with saved fail-then-pass
history: a failure of that workflow, job, test and signature, and a later pass
of another exact attempt at the same commit, from any PR of the base
repository. Verification rereads both exact attempts (`actions/runs/{id}/attempts/{n}`
and the job) and their logs, and the issue must still declare the context.
Expired logs, a changed attempt, a denial (`"flaky": false`), prose, another
signature, an untrusted or later declaration, or a caller without the
registry leave the check failed. Readiness uses the same history through a
read-only registry and saves nothing, and applies each attempt's retained
classification window without opening one.

**Unavailable evidence never waives.** Each failed check is verified on its
own. A missing log, a closed, unrelated or ambiguous issue, a failed search,
an unknown line or an exceeded bound leaves that check failed. The check and
the reason are listed in `rejected`, and other checks are unaffected.

## Deadlines and allowances

The [retained window](dk-pr-fix.md#budgets-occurrences-and-continuation) is an
absolute deadline (`ci:<logical run>`) that no resume, head change or larger
timeout moves. dk-ci adds the baseline's pending-only extension:

- After the window ends, one more observation may grant one extension, and
  only while checks are still pending. A failure or success that this
  observation captures is reported as it is and uses no extension. Without an
  available extension, the late observation is a timeout.
- The grant is a durable reservation (`ci-pending-extension:<logical run>`),
  and its window starts where the original ended. A resumed, direct or
  composed observation of the same run never gets a second grant or a later
  end. A later configuration can disable or shorten it, never renew it.
- Repair allowances (per cycle) and rerun allowances (per head, reserved by
  the publisher) stay in the same ledger. dk-ci's zero repair allowance
  reserves nothing, so it never narrows dk-pr-fix's repairs.

Composed dk-pr-fix gets the extension only when configured
(`ci.max_pending_extensions: 1`). Admission and preflight carry
`pending_extension_seconds` and `max_pending_extensions` into the workflow's
`ci` input.

dk-ci also passes the baseline's observation controls. Composed dk-pr-fix
carries each one only when configured:

- **Poll interval** (`poll_interval_seconds`, 1..3600). `observe()` returns
  the validated interval as `poll_interval`, and `ci-poll.yml` sleeps for
  exactly that output between pending observations. Without a configured
  value the composed workflow keeps its original 10 seconds.
- **Classification window** (`classification_timeout_seconds`, 1..3600). Each
  failed exact attempt (check, run, job and attempt) gets a retained window
  (`ci-classification:<logical run>:<attempt digest>`) that opens when the
  attempt is first classified. Once it ends, the check stays failed with
  `classification window exhausted` and no evidence is read, in observation
  and in readiness (`completion`) alike. A proof whose
  window ends while it is verified is discarded, never applied late. A rerun
  attempt is a new attempt with its own window. Without a configured value
  classification is unbounded.
- **Absent-check grace** (`no_checks_grace_seconds`, 0..86400). The grace for
  a head (`ci-no-checks:<logical run>:<head>`) opens at its first observation
  without checks. A head that still reports none when it ends is `no_checks`:
  settled, never CI success and never a repairable failure, so it uses no
  pending-only extension. Checks that appear later are observed as usual.
  Without a configured value, absent checks stay pending until the deadline.

Like the observation window, neither window restarts on resume, a new ledger
connection or a larger configured value.

## Fork workflow approval

`approve_fork_ci` is a supported publication kind under the
[extension contract](publication.md#kinds-and-authority). It has its own
contract action and permission input. Finding publication, PR approval, merge
authority and successful CI never imply it.

- **Readiness** (`publishing.fork_ci`). It needs everything PR approval needs:
  complete current coverage, a current ordinary reviewer and independent
  verifier, no confirmed or unverified finding, a reconciled clean head and
  another author's PR. It also needs a non-draft PR and no unsettled
  publication. It needs neither CI success nor a human approval.
- **Proposals** (`fork_ci_proposals`). It reads every page of the head's
  `pull_request` runs. A run is a fork gate (`publication.fork_gate`) only
  when all of these hold:
  - it is in `action_required`;
  - it is a run of the base repository at the exact head;
  - it comes from the PR's fork repository and branch;
  - it is associated with no other PR.

  An ambiguous association blocks every proposal. Each gate gets one proposal
  `{run_id, attempt}`, whose ID binds the run, head, target, run attempt and
  grant. A complete listing with no gate is the verified no-op.
- **Listing.** The `fork_ci` selector lists unsettled fork approvals first,
  for read-only reconciliation only. A native report never proposes one.
- **Preparation and dispatch.** Both recheck that the exact attempt is still
  gated, has no jobs and no pending deployment. A changed attempt blocks the
  intent. A deployment gate is never approved. Dispatch also rechecks the
  grant, the permission, the own-PR rule and the `fork_ci` callback, then
  sends one `POST actions/runs/{id}/approve`.
- **Reconciliation.** GitHub names no approver, so the effect is the same
  attempt executing jobs: `queued`, `in_progress` or `completed`, not
  `action_required`, with at least one job. An advanced attempt, a run that
  is still gated and a jobless run stay uncertain, and an uncertain approval
  is never resent. The effect key is `fork_ci:<run>:<attempt>`.

dk-pr-fix never approves fork workflows: preflight, `prepare` and the listing
reject `approve_fork_ci` outside review mode. In review mode, `main.yml` runs
the `fork_ci` publication barrier after the cycles and the finding
publication, gated on `fork_ci_authorized`.

## Standalone-to-merge bridge

A merge request (dk-pr-publish's merge preset: `publish.yml` running `main.yml` in review mode) may also carry an
`approve_fork_ci` grant (`dk_pr_publish.BRIDGES`). The grant still needs its
own permission, and merge never implies it. dk-ci-approve itself still takes
no other action. In `main.yml`:

1. `fork_ci` approves each eligible fork run attempt once the review and its
   finding publication settle, as above.
2. `merge_ci` runs once the review is complete, clean and verified and
   `fork_ci` has not failed; an unclean review never waits. It is
   `ci-poll.yml` repeated until checks settle. In review mode the retained
   window and its pending-only extension belong to the exact head
   (`ci:<logical run>:<head>`), so a later merge request after a pushed fix
   gets that head's own window. A later request at a head whose window ended
   observes once: settled checks are reported as they are, and checks still
   pending are a timeout. A timeout fails `merge_ci`, so nothing merges.
3. `merge_readiness` runs the `completion` readiness again after the wait.
   `merge_proposal` needs that fresh readiness, so a failed, pending or
   `no_checks` result never merges.

A merge without the bridge grant also waits for CI before its readiness, but
approves nothing.

## Flaky-test discovery

`ci_flakes.inspect()` targets a PR or a repository and reads, within the
resolved options:

- failed Actions runs created in the lookback window (at most `max_runs`);
- the failed jobs of each run's exact attempt and their failed-step logs,
  which give named failed-test observations;
- open issues (at most `max_issues`) and their explicit `dk-flaky`
  declarations.

`targets` narrows the scan to exact `{workflow, job, test, signature}`
contexts. A context is `linked` only when an open issue declares it exactly
and its signature occurs in an observation's logs. Otherwise it is a
`candidate`. Declarations with no observation in the window are reported
separately.

The report session must account for every context and cite only that
context's observations, and only a verified issue of a linked one. It cannot
change a status. `limits_reached` and `gaps` (missing logs, oversized job
inventories, failures that name no test) make `covered` false. A failed read is
a blocker, never an empty result. The result waives nothing, saves nothing and
grants nothing.

`ci_flakes.saved()` pages the registry's saved observations, optionally of
one exact earlier `{workflow, job, test}` context, through a read-only
registry. It reads nothing from GitHub, starts no session and is marked
`saved: true, verified: false`: saved evidence, never fresh verification.
Follow `next_offset` for further pages.

## Options

| Entry | Option | Default | Mapping |
| --- | --- | --- | --- |
| dk-ci | `timeout_seconds` | `1800` | `ci.timeout_seconds` (1..86400) |
| dk-ci | `pending_extension_seconds` | `1800` | `ci.pending_extension_seconds` (0..86400) |
| dk-ci | `max_pending_extensions` | `1` | `ci.max_pending_extensions` (0 or 1) |
| dk-ci | `poll_interval_seconds` | `30` | `ci.poll_interval_seconds` (1..3600) |
| dk-ci | `classification_timeout_seconds` | `60` | `ci.classification_timeout_seconds` (1..3600) |
| dk-ci | `no_checks_grace_seconds` | `60` | `ci.no_checks_grace_seconds` (0..86400) |
| dk-ci-flakes | `lookback_days` | `30` | Run listing window (1..365) |
| dk-ci-flakes | `max_runs` | `100` | Runs inspected (1..1000) |
| dk-ci-flakes | `max_issues` | `500` | Open issues inspected (1..10000) |
| dk-ci-approve | none | | |

A dk-ci option given at invocation sets only its own CI control. A default
applies only where nothing is configured. Configured classifications,
exceptions and limits stay in force, and the repair allowance is zero.

## Verification

`tests/test_ci_skills.py` uses disposable repositories, fresh state namespaces,
the real SQLite ledger and fail-closed fake GitHub and engine transports. An
unconfigured route or session raises, with no live fallback. GitHub reads,
writes and native sessions are counted. It covers:

- **Presets.** Baseline prompt digests, inputs per preset, configured CI
  controls kept under invocation options, and rejection of authorizations,
  unsupported options and overrides with zero engine calls.
- **Deadlines.** A direct and a composed observation share one window. Resume,
  a changed head and a larger timeout renew nothing. The pending-only
  extension is granted once and only while pending; a settled late capture is
  reported without one. The poll interval comes from the validated plan. The
  absent-check grace and each attempt's classification window are retained
  across resume; a window that ends during verification discards its proof.
  An interrupted wait resumes the same grant, and exhausted windows and
  allowances raise without observing.
- **Flakes.** An exact issue declaration by a trusted author, opened before
  the run, accepts its own attempt only. Declarations by the PR author, an
  outsider or a contributor, opened after the run or without a creation
  time, are never learned. A rerun attempt, a changed head, another test or line in the same step, an
  unrelated or closed issue, a mismatched signature, unavailable logs and a
  failed search leave the check failed and listed in `rejected`, and other
  checks are unaffected. Fail-then-pass history at one commit from another
  PR accepts a failure only with a trusted unaffirmed declaration and both
  attempts reread. A missing, denied, prose-only, untrusted or later issue,
  another signature, an earlier or other-head pass, and expired or changed
  history leave it failed. Saved inspection reads no GitHub state.
- **Fork approval.** One intent per eligible run attempt, published once and
  reconciled on rerun, plus the verified no-op. Rejection cases:
  - no grant, or a grant without the permission;
  - only finding or PR-approval grants, or only merge authority;
  - incomplete or unclean review, or a draft or own PR;
  - an ambiguous association, or a moved head or attempt (blocked, no write);
  - a dk-pr-fix plan.

  A lost response before the effect is never resent. A lost response after
  the effect is reconciled, and an uncertain approval stays pending.
- **Merge bridge.** Merge with a fork-approval grant admits only with both
  grants and permissions. The `main.yml` graph orders `fork_ci`, `merge_ci`,
  `merge_readiness` and `merge_proposal`, and `merge_ci` needs a clean,
  verified review. A pending or failed wait never merges, and the merge after
  success writes once beside the approval. Each head has its own window: a
  later request reports settled checks at an ended head and waits anew for a
  pushed head. Readiness keeps an attempt's exhausted classification window.
- **Flake discovery.** Linked and candidate contexts, targets, limits and
  gaps. A session that links, unlinks, invents or drops a context is rejected.
  Failed reads block.

## What remains

- **Runtime.** The installed graph and its sessions need Agent Workflow #129
  and #130. Both are closed on upstream main but in no release yet (newest
  v0.2.1). No local controller or launcher stands in
  for them. Plugin `SKILL.md` and descriptor rows wait for that runtime, so all
  three inventory rows stay `not_implemented`.
- **Baseline features without an equivalent yet.**
  - The native failure-diagnosis role, and automatic reruns of unrelated
    retryable failures under a `user_retry` grant. Reruns publish through
    dk-pr-publish with the per-head allowance.
- **Live evidence.** GitHub's search, run-listing and fork-approval behavior is
  exercised only against fakes. Live provider adherence and GitHub
  conformance need separate authorization.
