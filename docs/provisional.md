# Provisional analysis while PR metadata reconciles

GitHub's PR metadata can report a different head from the PR's Git ref
(dk-workflows#259). [`cf-workflows:dk-pr-fix`](dk-pr-fix.md) then analyzes the
pinned Git revision privately and withholds every external effect until the
metadata reaches that revision. This implements the provisional rules of the
[invocation](contracts/v1/invocation.md) and [report](contracts/v1/report.md)
contracts for PR identity. Architectural alignment recovery is #23.

## Pinning

Every capture ([`prfix/capture.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/capture.py))
chooses the analyzed revision, `snapshot.head`:

1. The checkout is at the metadata head, or builds on it through retained
   controlled commits: the metadata head. The PR's Git ref is not read.
2. Otherwise the transport reads `GET repos/<owner>/<name>/git/ref/pull/<n>/head`.
   If that ref differs from the metadata head and the checkout is at it, or
   builds on it through retained controlled commits, the checkout is pinned to
   the Git ref. This is the only accepted metadata lag.
3. Anything else is an error, as before: a snapshot or ref for another PR, a
   corrupt or missing ref, a ref that agrees with the metadata while the
   checkout differs, a checkout at neither revision, incomplete metadata or a
   PR that changed during capture.

"Builds on" means the revision is one of the controlled commits' recorded
parents, from the local head back. A lagging metadata head that is merely an
older ancestor of a local commit does not qualify.

Each snapshot records `identities`: `current`, the `pin`, the observed
`metadata_head` and `git_head`, `mismatched` and a `reason`. Every claim's
assignment keeps the snapshot it was analyzed under, so the actual metadata
identity of provisional work is retained.

## Separate identity, alignment and readiness

| Concept | Where | Provisional value |
| --- | --- | --- |
| Analysis identity | `snapshot.head`, trees, diff, discussion and the occurrence fingerprints | The pinned revision |
| Current alignment | `snapshot.identities.current`; `provisional` in `coverage`, `alignment` and `readiness` | `false`, with the mismatched heads |
| External readiness | `readiness.ready` | Always `false` |

Fingerprints never bind `identities`. So when the metadata reaches the pinned
revision, every result whose other inputs are unchanged is reused with no new
session. A changed tree refreshes tree-scoped work, a reworded commit with the
same tree refreshes only head-sensitive work, and changed resources or
discussion refresh only the occurrences that bind them.

While analysis is provisional:

- Read-only and writing tasks run. A writer's commit stays local.
- `publication_list` lists nothing and reports `provisional`.
  `publication_prepare` refuses a new intent. `publication_execute` refuses a
  `prepared` intent and leaves it `prepared`; it is neither sent nor blocked.
  Reconciling an already `pending` intent stays read-only and allowed.
- `readiness` reports a blocker and skips live CI and repository checks, so
  `merge_proposal` never invokes. Approval and fork-CI approval stay extension
  kinds that deny (#31, #35).
- The CI stage asks `alignment` first and neither observes CI nor starts the
  retained CI window. `coverage` reports `current_head_ci` as `withheld`, so
  `complete` stays false, and a CI repair claim refuses.

## Bounded reconciliation

`coverage` returns `complete` (selected-plan coverage), `current`,
`provisional`, `reconciliation`, `waiting`, `withheld` (work that needs a
reconciled head, such as current-head CI) and `settled`. After a provisional
cycle, `cycle.yml` runs the `alignment` operation with `wait: true` and sleeps
10 seconds between observations. Each wait reserves one unit of the allowance
`identity-reconciliation:<pin>` (5 per pinned revision) in the logical run's
ledger under the engine operation's identity. So a lost response replays the
retained result, an interrupted wait stays reserved and its operation stays
`unknown`, and a new run, session or configuration continues from the retained
count. Only an explicit continuation for that name (`extend`) adds waits.

`main.yml` repeats cycles until `settled`: every analysis occurrence and finding
settled, and either current or out of waits. Withheld work does not keep the
loop going. The next cycle after reconciliation reuses the analysis and
publishes. An exhausted allowance ends waiting and cycling. The run then ends
provisionally: `provisional` is true and `ready` is false; `complete` is true
only when nothing was withheld, so a selected CI stage keeps it false. A later run
whose snapshot agrees proceeds normally and reuses the same results.

## Verification

[`tests/test_dk_pr_fix_provisional.py`](../tests/test_dk_pr_fix_provisional.py)
uses a disposable repository, a fresh state namespace and a fake GitHub
transport that models only capture and the PR's Git ref. It counts native
dispatches and transport calls and covers:

- lagging metadata with private analysis, retained metadata identities, and no
  listing, preparation, dispatch, readiness, merge, CI or service write;
- reconciliation to the same revision with full reuse, then publication;
  renewed lag after preparation leaves the intent `prepared`;
- a provisional writer whose push is proposed only after reconciliation;
- a changed tree, a same-tree new revision, a changed resource and new
  discussion, each refreshing only affected occurrences;
- cross-PR, corrupt, missing and inconsistent snapshots and Git refs, at
  preparation and mid-run;
- replayed, interrupted, replacement-run, exhausted and continued waits.
- a selected CI stage withheld while provisional, which settles the run once
  waits are exhausted, ends it provisionally and becomes missing work after
  reconciliation.

## What remains

Running the workflow needs Agent Workflow #129 and #130, as for the rest of
[dk-pr-fix](dk-pr-fix.md#what-remains-blocked). These fixtures exercise the
helpers only; they do not run the YAML graph or establish GitHub conformance.
