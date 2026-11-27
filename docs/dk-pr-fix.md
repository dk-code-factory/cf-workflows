# Installed dk-pr-fix workflow

`cf-workflows:dk-pr-fix` is Agent Workflow's portable PR-fix example, extracted
into an installed resource closure. It is not runnable yet: the engine cannot
yet run an installed package against a separate checkout or mount the state
namespace (see [What remains blocked](#what-remains-blocked)).

## Installed closure

`cf_workflows/workflows/dk-pr-fix/` is the
installation root:

- `workflows/main.yml` is the entry. Reusable references such as
  `uses: workflows/helper.yml` are relative to the installation root, not the
  target checkout.
- `workflows/helpers/` holds the bounded Python operations. Helper containers
  mount it at `/actions/helpers`; each call performs one operation.
- `workflows/agents/*.yaml` is the admitted role catalog: `leaf`, `reviewer`,
  `verifier`, `fixer` and `audience`.
- `agents.yml` is the deployment template and `request.example.json` is the private
  request shape.
- `closure.json` holds the SHA-256 of every installed file. `LICENSE` is the
  upstream MIT notice.
- `plugin.yaml` and `skills/dk-pr-fix/SKILL.md` are the suite's plugin
  descriptor and skill entry point ([packaging.md](packaging.md)). They have no
  upstream counterpart and are listed as `authored` in the provenance.

The installation is never written. Python would load a cached `.pyc` instead
of its verified source, so any `__pycache__` directory or `.pyc`/`.pyo` file
rejects the installation. The helpers also run with `-B`. To regenerate the
manifest after an intentional change, remove the caches and run
`python3 -B -m cf_workflows.dk_pr_fix`.

## Provenance

[`baselines/dk-pr-fix-closure.json`](../baselines/dk-pr-fix-closure.json) maps each
extracted file and ported test to its path and SHA-256 at the pinned commit
`16ec5eecccd367dfdffcf1b529c51d181bbced49`. Each file is `verbatim`, `adapted` or
`converted`. Every other file of the example is listed as excluded, with a reason:
the copy-into-workspace README and the optional review-profile generator.
Its `corrections` list upstream fixes consumed after the pin. Each names the
upstream commit and the exact definitions taken from it, such as the merge
guard from Agent Workflow #138.
The [baseline checker](verification.md) compares the files with the pinned objects.

The five Codex TOML roles are converted to the engine's canonical YAML form:
`name`, `description`, `instructions` and optional `reasoning-effort`, one
`key: "JSON string"` line each. The checker regenerates the conversion from the
pinned TOML. The output is byte-identical to the upstream conversion in Agent
Workflow PR #135. Instruction text is unchanged.

Adaptations to the example:

- Application state moved out of the checkout (next section).
- The private request is read from the state namespace, not from a
  workspace-relative path.
- Reusable references point into the installation.
- The retry and checklist prompts say that the assignment and the prior report
  are earlier in the same session. The references in `task_input` point into the
  state namespace, which the session cannot read, so they are immutable record
  IDs only.
- The legacy `.pr-fix-state` directory gets no special treatment. In a checkout,
  it is ordinary untracked content.
- Occurrence IDs are stable rather than positional, fingerprints are scoped, and
  allowances, deadlines and continuation follow the rules in
  [Budgets, occurrences and continuation](#budgets-occurrences-and-continuation).
- When PR metadata lags the PR's Git ref, the pinned Git revision is analyzed
  privately and external effects wait for bounded reconciliation: see
  [provisional analysis](provisional.md).
- Publication uses the contract's journal states, per-action authority, a
  retained audience, a lock, unique effects and fresh attempts instead of
  replay, and adds review submission: see [exact-intent publication](publication.md).
- A `responses` stage answers feedback after the fix publication and before
  review, and again as a finalizer: see the [response stage](responses.md).
  Its files (`responses.yml`, `prfix/responses.py` and `prfix/feedback.py`)
  originate here and are listed as `authored` in the provenance.
- A `mode` input selects `fix` (the default) or `review`. Review mode is
  [incoming review](review.md): read-only tasks and reviewers, a separate
  verifier and no `finding_fix`. Its entry `workflows/review.yml` and
  `skills/dk-pr-review/SKILL.md` are `authored`, as are the
  [routing entry](review.md#installed-entry)'s `workflows/iter.yml`,
  `prfix/iteration.py` and `skills/dk-pr-iter/SKILL.md`, the feedback entries'
  `workflows/feedback-*.yml` and `skills/dk-pr-feedback-*/SKILL.md`
  ([feedback entries](feedback-skills.md)) and the
  [publication entries](publication-skills.md) `workflows/publish.yml`,
  `workflows/review-post.yml` and their `SKILL.md` files. A `validation_commands`
  input carries `dk-pr-feedback-address`'s `additional_validation_commands` to the
  address session, and an `entry` input names a publication, drafting or
  splitting entry whose preset `prepare` enforces. `accounts` are `{host, id}`
  numeric identities, and an explicit `maintain_other` request is staged with the
  private request for another author's PR.
- A tasks stage that holds the `reconcile-decisions` occurrence is followed by
  `reconciliation.yml`, which asks once about unresolved decisions and blocks
  dependent work until they settle; the publication stage carries the bounded
  Decisions section under `edit_pr`. See [decisions](decisions.md#installed-workflow).
  `reconciliation.yml`, `prfix/decisions.py`, the preset `workflows/decisions.yml`
  and `skills/dk-pr-decisions/SKILL.md` are `authored`.
- `describe-pr` and `draft-issue` occurrences, and `draft-commit-message` under its
  own entry, run the drafting procedure: their claim carries the evidence, the
  accepted report returns a checked draft and proposes no operations, and the
  publication stage lists the draft's one exact `edit_pr`, `create_issue` or
  `edit_issue` intent under its enabled and authorized permission. See
  [drafting](drafting.md#installed-workflow). `prfix/drafting.py`, the presets
  `workflows/description.yml`, `commit-message.yml` and `issue.yml` and their
  `SKILL.md` files are `authored`.
- Under the `dk-commit-split` and `dk-pr-split` entries, the `inspect-commits` or
  `assess-scope` occurrence runs the splitting procedure: its claim carries the
  commit and scope evidence, and the accepted report returns a checked series or
  split and proposes no operations. The entry's publication stage first runs
  `workflows/splitting.yml`, which builds the result under the explicit local
  grant, validates each new commit and moves the refs, and then lists only its
  exact `rewrite`, or `split_push`, `split_pr` and source `push` intents. A
  composed dk-pr-fix plan runs both adapters as ordinary read-only tasks, and
  `prepare` rejects `rewrite_commits` in every other plan. See
  [splitting](splitting.md#installed-workflow). `prfix/splitting.py`,
  `workflows/splitting.yml`, the presets `workflows/commit-split.yml` and
  `pr-split.yml` and their `SKILL.md` files are `authored`.

## Configuration split

| Input | Source |
| --- | --- |
| Declared workflow inputs | `main.yml` inputs, set by preflight from the admitted options |
| Deployment configuration | Parsed `agents.yml` shape, checked by admission |
| Admitted roles | The installed YAML catalog, by digest |
| Private request | Exact PR URL, instructions and logical run, staged in the state namespace |

[`cf_workflows/dk_pr_fix.py`](../cf_workflows/dk_pr_fix.py) prepares a run. It
runs no target task. Only custom inputs make it ask the engine for one
configuration-validator task and durable input gates ([semantic assessment](semantic.md#installed-workflow)).

1. `preflight()` checks the installation against `closure.json` and checks that
   the target checkout is outside it. On failure it rejects before contacting the
   engine.
2. It runs [structural admission](admission.md) with the installed role catalog.
   Custom prompts and preferences, per-task instructions, custom adapters and
   free-text policy are `semantic_required`. After the installed-workflow limits
   below pass, they go through [semantic assessment](semantic.md): one validator
   task, then a durable gate and re-admission when it asks for clarification.
   Only an accepted proposal is mapped; `waiting`, `rejected` and `blocked` map
   and stage nothing. Preset entries over this workflow reject custom inputs
   before any gate read. Custom adapters, prompts and preferences that declare
   resources reject: the workflow carries no configured resource yet.
   Supplied skills reject until Agent Workflow #139
   ([skills.md](skills.md)). A repository-only request and the configuration
   adapters of `dk-config` and `dk-preferences` reject; the read-only
   `onboard-repository` adapter may be selected ([configuration skills](configuration-skills.md)).
   Prose finalizers (`plain-english`, `asd-ste100`) run as ordinary task
   occurrences whose sessions the installed helpers bound and check
   ([prose cleanup](prose.md#installed-workflow)).
   The staged alternatives adapters reject ([alternatives](alternatives.md)).
   With the suite's configuration ledger, current acceptances are reused with
   no gate read or validator task. With its configuration store, unsettled
   configuration writes are read back before the saved configuration is read,
   and an answered revision of it is written back once
   ([acceptance reuse](acceptance.md#installed-preflight)).
3. A remote write needs an enabled permission and an explicit authorization.
   Preflight maps each such action onto its permission input and stages only
   those authorizations. An authorization without the enabled permission
   rejects, and so do approval and fork-CI approval (incoming review only) and issue
   actions (see [exact-intent publication](publication.md)). Merge also needs the engine's
   `merge_guard` capability ([merge readiness](publication.md#merge-readiness)).
   Every other input is `false`.
4. It maps the effective options onto the declared inputs: tasks, finalizers,
   reviewers, the selected baseline adapters, limits, CI timeout, merge method,
   accounts, settings and turns. Accepted custom prompts and free-text policy
   map onto `prompts` and `policy`, which are absent otherwise. `inputs.request`
   names the staged request.
5. `stage_request()` writes the request as `requests/<sha256>.json` (mode 0600) in
   the state namespace. This is the only write to it; preflight itself writes
   only the configuration ledger and store, and only when they are passed.

## State namespace

Application state lives in
`$XDG_STATE_HOME/cf-workflows/repositories/<sha256(repository)[:32]>` (fallback
`~/.local/state`). It is outside the plugin installation and every target
checkout. Helper containers receive it at `/cf-workflows-state` through
`CF_WORKFLOWS_STATE`. Helpers reject a missing or relative path, a symlink, and
a directory that overlaps the checkout.

Each logical run has its own SQLite ledger, `runs/<sha256(logical_run)>.sqlite`,
which holds claims, submissions, reservations, deadlines, operations, publication
intents and local commits. Run text never forms a path, and runs that differ only
by case stay separate. The repository namespace also holds the writer lease and
the content-addressed `artifacts/`, `preparations/` and `requests/` stores. This
is a deliberate refinement of the contract's per-run partitioning. The lease
fences the shared Git index across runs, so it must be visible to every run.
Content-addressed records are immutable and named by digest, so sharing them
cannot mix two runs' state. The marker `.owned-by-cf-workflows-dk-pr-fix`
(format `cf-workflows-dk-pr-fix-v1`) claims a directory only while it is empty.
So dk-workflows settings, `.pr-fix-state` and other existing state are never
adopted or imported. Back up the namespace together with the engine's recovery
bundle.

## Budgets, occurrences and continuation

All of this lives in the logical run's SQLite ledger. A replacement engine run,
a new session, a changed configuration or lost engine checkpoints and
preparation receipts reopen the same ledger as long as the logical run is the
same. The default logical run is derived from the PR, so it is.

**Occurrences.** Each selected task gets a stable ID derived from its phase
(`tasks` or `finalizers`), its context (task stage or reviewer stage), its
adapter name (legacy aliases resolve to the same name), its instructions and its
options. Repeated identical selections are told apart by their ordinal. Reordering
or inserting other selections keeps every ID, and so keeps its claims and
results. `finding_verification`, `finding_fix` and `ci_repair` keep their fixed IDs.
Claims are the occurrence records: each holds its assignment, rounds, failures
and accepted report, and none is ever deleted.

**Scoped fingerprints.** A completed occurrence is reused while its fingerprint
is unchanged. The fingerprint binds the PR identity, author and actor, target
and local trees, diff, discussion, request and `AGENTS.md` instructions, shared
settings, the task's own resources, role definition, helper implementation and
admitted execution (workflow, roles and policy). History-scoped tasks (commit
inspection) also bind the commit history. Head-sensitive tasks also bind the
heads. CI controls bind only `ci_repair`. Task and finalizer selections, limits,
the merge method and whether an occurrence is required bind nothing. The limit
`max_parallel_reviewers` (0–64; 0 adds no cap) only caps each reviewer group's
concurrency, so changing it reuses every result. So a
governing resource change refreshes only the reviewer that reads it, and a
rewrite that keeps the tree keeps tree-scoped reviews while commit inspection
refreshes. A review round is keyed by the reviewers' current fingerprints, ordered
by occurrence ID, and by each reviewer's count of live claims under them. So
reordering reviewers, refreshing only one of them, or continuing one after a
superseded claim never charges more than one cycle per round.

**Allowances.** Defaults: five review cycles, one eligible native retry per
claim, five CI repairs per cycle, three CI reruns per head and three checklist
follow-ups per claim. Each unit is reserved in one transaction under a stable
operation identity before dispatch. Replaying the same operation after response
loss returns the retained count, even under changed configuration. The first
reservation fixes the allowance's ceiling: a later configuration can narrow it
but never widen it. A zero allowance disables the attempt.

**Deadlines.** The CI window (default 1,800 seconds) is absolute. Its start and
end are recorded on the first observation and kept across polls, head refreshes,
sessions and runs. A larger timeout never moves the end. A smaller timeout ends
the window earlier, measured from the original start. A configured pending-only
extension is granted at most once per run (see [CI entries](ci-skills.md#deadlines-and-allowances)).

**Explicit continuation.** Only an explicit continuation adds capacity. The
`extend` helper operation, or `action.py continue`, records a grant of 1 to 10
units for a named allowance together with the exact user authorization. The
grant is idempotent by request ID, and a different grant under the same ID is
rejected. Prior reservations and results stay. The name `occurrence:<task ID>`
grants a fresh claim for a read-only occurrence whose original claim is
unfinished, for example after a required native failure with no eligible
retry. The original claim becomes `superseded`. Its rounds, failures and
findings are retained, and it can no longer be accepted or resumed. Writers
never qualify, because their unfinished edits share the Git index: they must
resume or settle the original claim. Without a grant, exhausted or failed
mandatory work makes its helper call fail, so the YAML blocks its dependents,
and nothing is deleted.

**Completion and readiness.** `complete` means every required selected
occurrence has current coverage, every original finding is settled and, when the
CI stage is selected, current-head CI succeeded. Retained alternatives analysis
of the PR that does not release review also withholds it
([alignment gate](alignment.md#installed-gate)). It holds for an empty plan and
for plans with only custom tasks or finalizers. `ready` is a separate,
stricter merge gate: it also needs selected current review and independent
verification, a clean published head, fresh CI and live repository
requirements. An interrupted finalizer keeps its unfinished claim. Replaying the
claim returns it, a replacement run cannot redispatch it, and completion stays
false until the original settles.

**Ledger schema.** The ledger records its version in `PRAGMA user_version`
(currently 3). Opening an older ledger migrates it in place without losing
history. A version 1 CI window keeps its absolute end, but a smaller timeout
cannot narrow it because its start is unknown. A newer ledger is refused.

## Backup boundaries

| Data | Owner | Location | Backup and restore |
| --- | --- | --- | --- |
| Application state: ledgers, artifacts, preparations, staged requests, writer lease | cf-workflows | State namespace (`$XDG_STATE_HOME/cf-workflows/repositories/<id>`) | Back up the whole suite state home with [`recovery.backup`](recovery.md), which snapshots every store at one instant while writers continue. Restore it whole into an empty place: a partial restore can replenish allowances or lose evidence. |
| Immutable assets: installed workflow, helpers, roles, `closure.json` | cf-workflows package | Installation root | Reinstall the same version and check it against `closure.json`. Do not modify it. A plan refuses a helper closure that differs from the one it was prepared with. |
| Engine recovery: run snapshots, checkpoints, native sessions | Agent Workflow | Engine state | Back up with the engine's recovery bundle, at the same point in time as the namespace. Losing it keeps budgets, deadlines and results, but an unfinished claim then needs an explicit continuation. A writer's unfinished claim needs the engine run to resume. |

## Verification

[`tests/test_dk_pr_fix.py`](../tests/test_dk_pr_fix.py) covers the following:

- Installed-closure integrity and provenance coverage.
- Reusable references that resolve inside the installation.
- Role parsing by admission and by the helpers.
- No provider, engine or container launcher in the helpers, and the state
  namespace for every helper step.
- Preflight mapping and rejections, with invocation counts on a fail-closed fake
  engine transport.
- Preflight with a disposable configuration ledger and store: fresh-run reuse
  with no validator task, selective invalidation, answered-gate replay, lost
  gate and write responses, and interrupted or raced configuration writes.

[`tests/test_dk_pr_fix_continuation.py`](../tests/test_dk_pr_fix_continuation.py)
drives the installed helpers with a fail-closed fake GitHub transport and counts
native dispatches. It covers reordered and repeated selections, capacity one
against parallel readers, partial parallel failure, zero and exhausted
allowances, and interrupted finalizers. It replaces runs, deletes preparation
checkpoints and changes the configuration and the admitted execution, then
checks the retained counts, deadlines and claims. It also checks scoped refresh,
explicit continuation, completion without readiness and ledger migration.

In one case, the installation is copied to a read-only directory with spaces in
its path. The request is staged, and then `snapshot_request`, `prepare` and the
first `claim` run in a separate interpreter. The helpers are imported only from
that copy, against a separate target checkout with spaces in its path. The case
uses a fake GitHub transport that fails every unconfigured operation. It checks
the following:

- The exact instructions and head/target identities reach the plan and the
  assignment.
- Independent verification is a separate required `verifier` task.
- Capture is called exactly twice.
- The target has no changed or ignored files.
- The installation is unchanged.

The ported example tests in `tests/dk_pr_fix/` run against
the installed helpers, each with a fresh state namespace.

## What remains blocked

These fixtures do not run the YAML graph, start sessions or prove any engine,
provider or host capability.

- [Agent Workflow #129](https://github.com/dk-code-factory/agent-workflow/issues/129):
  engine-managed native sessions through the Codex or Claude SDK.
- [Agent Workflow #130](https://github.com/dk-code-factory/agent-workflow/issues/130):
  running an installed package against a separate checkout, resolving its
  reusable references and admitted assets, and mounting the state namespace.
- [Agent Workflow #139](https://github.com/dk-code-factory/agent-workflow/issues/139)
  discovery-isolation PR: sessions with host and repository skill and plugin
  discovery disabled, and observed evidence of the session's plugin set.
- Every publication kind, merge included, is helper-side only until the
  workflow runs ([exact-intent publication](publication.md#what-remains)).
- Proof that budgets, occurrences and continuation hold across real engine runs
  and native sessions needs #129 and #130. The fixtures exercise only the
  helpers.
- Semantic admission of custom inputs through a real engine: the validator task
  needs an engine operation receipt or a validator-only read-only tool policy,
  which Agent Workflow `8758775` does not provide ([engine binding](semantic.md#engine-binding)).
  Supplied-skill bindings ([skills.md](skills.md)) also need Agent Workflow #139.

No local launcher, controller or second agent loop stands in for these.
