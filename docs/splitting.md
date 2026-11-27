# Commit organization and PR splitting

`cf-workflows:dk-commit-split` and `cf-workflows:dk-pr-split` (#34) are thin
entries over the shared workflow.
[`cf_workflows/splitting.py`](../cf_workflows/splitting.py) holds their presets
and adapter procedures. Both entries are also listed in the plugin descriptor
as presets over the installed dk-pr-fix workflow
([Installed workflow](#installed-workflow)). Neither has run through a released
engine (see [What remains](#what-remains)).

## Entries

| Entry | Preset tasks | Adapter | Accepted authorizations | Options (baseline defaults) |
| --- | --- | --- | --- | --- |
| `dk-commit-split` | `inspect-commits`, then `{stage: publication}` | baseline `inspect-commits` | `push` | `policy` (`null`), `max_passes` (5) |
| `dk-pr-split` | `assess-scope`, then `{stage: publication}` | baseline `assess-scope` | `create_pr`, `push` | `added_lines_threshold` (1000), `evacuate_unrelated` and `split_related` (`automatic`) |

Each entry has three parts:

- A preset selection with explicitly empty `finalizers` and `reviewers`.
- The adapter procedure: the admitted `leaf` role, the baseline read-only
  assignment and the helper operations `collect`, `organize` or `partition`,
  `commit` and `publish`. The default dk-pr-fix plan already selects both
  adapters, so no new adapter is admitted.
- The original role prompt and options from the pinned baseline. `config.json`
  and `prompts/default.md` are kept byte for byte, and their digests match
  [the inventory](../baselines/inventory.json).

A default run is admitted from bundled control text. It runs no validator
session and loads no skill. An invocation may add only `prompts`. Tasks,
finalizers, reviewers, permissions, unknown options, other authorizations and a
repository target reject before the engine is contacted. A prompt `policy`
becomes a `cleanup` prompt for `inspect-commits`, so the shared validator
assesses it. A policy provider (`{"skill": ...}`) rejects, because it needs
supplied-skill execution (#16).

## Authority

Local and remote work have separate grants:

- **Local.** `organize` and `partition` take `rewrite`, an explicit user
  authorization (`request_id`, `instruction`, `source`). Without it they
  assess and verify, and write only to a discarded object directory. No ref,
  index or worktree changes.
- **Remote.** `publish` needs the request authorizations and their configured
  permissions. A local grant never stands in for them, and a remote grant never
  stands in for a local one.
- **Manual mode.** When the mode of a moved kind is `manual`, `partition` also
  needs an `approval` that names the plan digest it reported. `disabled` keeps
  that work in the source PR.

## Evidence

`collect` is read-only. The checked-out branch must be at the PR head, and the
PR base and head must be present. Evidence holds the following:

- the unique merge base and every commit up to the head, with its exact author
  and message, its trailers and its changed paths (merge and root commits
  block);
- the final diff, and the added-line count for the size threshold;
- the configured Git identity and dirty paths.

The actor owns the configured identity's email, plus any `owners` the caller
names. Every commit up to and including the last commit by another author is
fixed. It is never rewritten or moved.

## dk-commit-split

`organize` runs up to `max_passes` passes. Each pass is one read-only session,
reused by its binding with no session when nothing changed. The session must
assess every commit with a reason. It returns either `no_change` or a new
series of commits. Each new commit has a message and units of the form
`{commit, paths}`. The helper accepts a series only when all of these hold:

- every path of every rewritable commit is used exactly once, and changes to a
  path keep their original order;
- each new commit has one author, with the date of its latest source commit;
- each message keeps exactly its source trailers (`Signed-off-by`,
  `Co-authored-by`, ...) and adds no tool attribution. The dk-commit-message
  length fallbacks apply only without a policy.

The helper builds the series through a private index and `commit-tree`. A
commit that is unchanged and keeps its original parent keeps its SHA. A
rebuilt series must reproduce the reviewed final tree exactly. The next pass
assesses the rebuilt series. A pass that changes nothing stops the loop. A
series that still changes at `max_passes` is reported as a concern.

| Outcome | Meaning |
| --- | --- |
| `no_op` | Every commit was assessed, and the series stays. |
| `assessed` | A verified series, with no local grant: nothing was written. |
| `prepared` | A recovery ref keeps the original head, and the branch moved to the verified series by compare-and-swap. |
| `blocked`, `rejected` | Nothing was written, and the reason is given. |

The new final tree equals the old one, so moving the branch ref leaves staged,
unstaged and untracked work exactly as it was.

## dk-pr-split

`partition` runs or reuses one session. The session assesses every changed
path as `core`, `unrelated` or `related`. It returns `keep` or `split`, and a
split names its kind, paths, new branch, draft PR title and body, and commit
message. The helper accepts a split only when all of these hold:

- moved paths have the split's kind, and the kind's mode is not `disabled`;
- each path moves at most once, and core work stays in the source PR;
- moved paths are files or symlinks, not submodules, and each new branch name
  is a valid Git ref name;
- no moved path contains another author's work;
- the body links back to the source PR, with no tool attribution, unpinned
  code links or `path:line`;
- messages keep their source trailers and credit any other owned authors as
  co-authors.

The helper builds each split as one commit on the PR target. It also builds a
source commit on top of the head that returns the moved paths to their
merge-base state. The source history is never rewritten. Verification checks
that every changed path is kept with its final state or moved to exactly one
split that holds it. Together, the source and the splits hold the reviewed
work.

A local split records the recovery ref, the split branches and the source
branch update before any of them is written. Then only the moved paths of the
index and worktree are updated.

These cases block before any write:

- a moved path that is dirty in the checkout;
- an existing local split branch;
- a target that changed a moved path since the merge base.

`partition` has the same outcomes as `organize`, plus `kept`, and `proposed`
for manual mode.

## Recovery

The splitting ledger is a versioned SQLite file per logical run
(`runs/<sha256(logical_run)>.splitting.sqlite`) in the suite's state namespace.
It appends accepted session results, rejected claims and local operations, and
never rewrites them.

- **Lost session response.** It is reconciled through the same claim.
- **Rejected result.** It is retained and never redispatched, up to
  `1 + retries_per_task` attempts per binding.
- **Interrupted local operation.** A later call first reconciles each recorded
  ref: a ref at its new value is done, and a ref at its old value is updated.
  Any other value blocks. For a split, the moved paths are then brought to the
  new source commit. No new session runs.
- **Rerun after the work.** It returns the recorded report while the checkout
  and PR show either the original or the resulting head. A branch reset to the
  original after the work blocks; a new logical run starts over.

## Publication

`publish` sends a prepared result through the shared
[exact-intent publisher](publication.md), with `source_validator(ledger, name)`.
That validator accepts only the latest applied local operation. There is no
second journal.

- **dk-commit-split.** One `rewrite` intent, using the PR head as the exact
  lease. Its plan enables `rewrite_commits` only with the `push` grant and
  permission. The installed dk-pr-fix keeps it false.
- **dk-pr-split.** For each split, `split_push` to the new branch (absence
  lease) and then the draft `split_pr`. After every split is applied, one
  fast-forward `push` of the source commit uses the original head as its
  lease. Both `create_pr` and `push` must be effective.

The shared publisher pushes only a clean checkout at the exact head, so dirty
work holds publication without a write. The first step that is not applied
stops the run, and a rerun continues from it. A pending intent is only
reconciled from the service and is never resent.

## Commit on request

`commit` is the committing procedure for a
[dk-commit-message](drafting.md) draft. It needs an explicit local commit
`authorization`. The draft must still be current
(`drafting.current_report`); otherwise it returns `stale` and writes nothing.

`commit` commits exactly the included paths through a private index. It
updates only their index entries, so excluded and unrelated changes, staged or
not, stay as they were. The commit is dated when it is made and ledgered per
draft. A repeated call with the same draft returns the same commit, and a
later draft in the same run commits again.

## Direct and composed use

`organize` and `partition` accept the entry's own preflight, or any admitted
shared plan that selects the adapter exactly once, and run the same procedure.
A composed caller passes the entry options, checked against the same schema. A
composed commit policy comes from the plan's admitted `cleanup` or `commit`
prompts. A plan without the adapter rejects.

## Installed workflow

`workflows/commit-split.yml` and `workflows/pr-split.yml` call `main.yml` with
the entry preset, `mode: fix` and `entry: dk-commit-split` or `dk-pr-split`.
[`prfix/splitting.py`](../cf_workflows/workflows/dk-pr-fix/workflows/helpers/prfix/splitting.py)
holds the helper side. Its constants, Git plumbing, result checks and builds are
a verbatim copy of the library's, and a test keeps the two identical.

- **Admission.** `splitting.installed(name, ...)` admits a direct run through
  the shared preflight. It returns only the permission inputs the entry's
  intents need: `rewrite_commits` for dk-commit-split under `push`, and
  `create_draft_prs` and `push_commits` for dk-pr-split under `create_pr` and
  `push`. Options set by the invocation, the local `rewrite` grant, further
  `owners` and `validation_commands` go to `settings.splitting.<entry>`. A
  commit policy, invocation prompts and a repository target reject; they run
  only through the library procedure.
- **Plan.** `prepare` requires the exact preset: the adapter, then publication,
  fix mode, only the entry's own authorizations and permission inputs. It marks
  the occurrence and the publication stage with `splitting`. It rejects
  `rewrite_commits` and `settings.splitting` in every other plan. A composed
  dk-pr-fix plan runs `inspect-commits` and `assess-scope` as ordinary
  read-only tasks.
- **Claim and acceptance.** The claim carries the commit and scope evidence of
  the checkout: every commit from the merge base with its author, message,
  trailers, paths and bounded patch, which commits are owned and which are
  fixed, the changed paths and the size. A dk-commit-split claim requires the
  local series to reproduce the reviewed tree; a dk-pr-split claim requires the
  checkout at the PR head. The session returns its result in
  `deliverables.split` and proposes no operations or changes. Acceptance
  applies the library's result checks; a failure is a same-session correction.
- **Build, validate, apply.** The entry's publication stage first runs
  `workflows/splitting.yml`. `split_build` rebuilds the current accepted result
  as objects, under the local grant only. A result that changes nothing, a
  manual-mode split, a missing grant and dk-commit-split's `max_passes` build
  nothing. The operation, with its exact ref updates and recovery ref, is
  recorded in `runs/<run>.split-operations.sqlite` before anything moves.
  `split_validate` then runs once per new intermediate commit (every rebuilt
  commit, or each split commit and the source commit). Each configured command
  runs in a private checkout of that commit's tree. The first failing command
  stops it, and the result is retained, so a replay runs nothing again.
  `split_apply` moves the refs by compare-and-swap only when every commit
  passed. A missing or failed validation fails the step, and nothing is moved
  or listed. A failed operation stays pending for the rest of its logical run:
  each later `split_build` returns it and each `split_apply` fails again, and
  nothing discards it. Only a new logical run assesses and builds again. An
  interrupted apply resumes from the ref values it finds, and a branch moved by
  someone else blocks.
- **Validation is opt-in and trusted.** Without `validation_commands`, the
  series or split is applied with no per-commit validation. The commands run
  the PR's code, including other authors' commits, as the helper in its
  credentialed container. The GitHub token and state-mount variables are
  removed from the command's environment, but this is not isolation: the
  command can still read the helper's credentials (for example from
  `/proc/<ppid>/environ`) and write the state mount, including the retained
  validation results in `runs/<run>.split-operations.sqlite` that gate
  `split_apply`. Configure validation commands only for a PR whose code you
  would run with the helper's GitHub token.
- **Local head.** `capture` accepts the checkout at an applied operation's
  result while the PR shows the head it moved from. The recorded operation
  establishes this, not a caller flag. A dk-commit-split rewrite changes the
  history, so the next cycle runs the next pass on the rebuilt series, until
  `max_passes` passes are applied. Then the last applied pass keeps its
  coverage, and no further session runs. A dk-pr-split keeps the coverage of the assessment it applied, so it is not
  assessed again on its own result.
- **Publication.** The barrier lists only the latest applied operation's
  intents, while the checkout is its result. dk-commit-split lists one
  `rewrite` from the first original head to the final series, once a pass
  changed nothing. After `max_passes` applied passes, each of which changed
  the series, it lists the last applied series at once, with no confirming
  pass, as the library procedure does. The `publication_list` result then
  carries the concern `stopped after max_passes=<n> with the series still
  changing`. The barrier forwards only the items, so the concern appears in
  that helper job's output. dk-pr-split lists each
  `split_push` and draft `split_pr`, then the source `push`.
  `publication_prepare` refuses the source push until every split PR is
  applied. Each intent's source is `splitting:<operation>`, which counts only
  while that operation is the latest applied one and the checkout is its
  result. The audience must be independent of the session that
  produced the result. Without enabled and authorized permissions the local
  result stays unpublished.

## Verification

[`tests/test_splitting.py`](../tests/test_splitting.py) uses a disposable
history with three commits:

- one by another author (Bob), with a `Signed-off-by`;
- a mixed commit and a fixup, both by the actor (Ada);
- a target that moved after the merge base.

It also uses a bare remote, the fail-closed fake engine and the fail-closed
fake GitHub. Unscripted sessions and unconfigured operations fail. Each case
asserts sessions, refs, pushes or writes. It covers:

- **Presets.** Baseline digests, default runs with one capability query,
  rejected expansions, permissions, and a policy prompt assessed once by the
  validator.
- **dk-commit-split.**
  - A no-op that is reused.
  - A tree-preserving reorder that splits the mixed commit and folds the
    fixup. Assessed first with no write, then prepared with zero new
    sessions. Bob's commit, authorship, dates, the recovery ref and dirty
    work are kept.
  - An interrupted ref update reconciled without a session, and a moved branch
    that blocks.
  - Rejected series: a fixed commit, a dropped or duplicated unit, path
    order, mixed authorship and tool trailers.
  - Lost and unscripted sessions.
  - Publication with a missing grant, with dirty work (no push), then exactly
    one push.
  - A composed dk-pr-fix plan.
- **dk-pr-split.**
  - Keep with size reporting.
  - A split built on the target that keeps authorship and dirty work. The
    source and split diffs together equal the reviewed diff.
  - Partial publication failure: a lost draft-PR request stays pending, is
    never resent and is reconciled when it lands. Only then does the source
    push follow.
  - Policy, core-work, other-author, link and branch rejections.
  - Manual approval.
  - Dirty, branch and target collisions.
  - An interrupted checkout update.
  - A composed plan.
- **Commit on request.** Only the included paths are committed. Staged and
  untracked unrelated work is kept, the commit is reused, and a stale draft and
  a missing authorization are refused.

[`tests/test_dk_pr_fix_splitting.py`](../tests/test_dk_pr_fix_splitting.py)
drives the installed helpers in-process, the same way `task.yml`,
`splitting.yml` and the publication stage call them. It uses the same kind of
history, a disposable bare remote, a fresh state namespace and the fail-closed
fake GitHub of the drafting tests, extended with draft-PR routes. It counts
sessions, pushes and writes. It covers:

- **dk-commit-split.** A reorder that is built, validated on both new commits,
  applied with a recovery ref, assessed again by a second pass and then
  published as one rewrite, with Bob's commit and all authorship kept; a relist
  only reconciles. A failing validation command on the intermediate typo-fix
  commit moves and lists nothing, its retained result is not run again, and
  the failed operation stays pending. With `max_passes=1` spent by the one
  applied pass, no second session runs, nothing more is built, and the last
  applied series is listed with its concern. Without a local grant the series
  is only assessed; without push authority it stays local. Same-session corrections for a rewritten fixed commit, a dropped
  unit and a proposed operation. An interrupted apply resumes, and a branch
  moved after the build blocks.
- **dk-pr-split.** A split validated and applied, with the split on the target,
  the source keeping the rest and the moved path updated in the checkout. The
  assessment stays current with no second session. The source push is refused
  until the split PR is applied; a lost draft-PR response is reconciled, never
  resent. Kept and manual-mode results build nothing. Corrections for another
  author's path, a missing back link and lost core work. An existing split
  branch blocks the build.
- **Plans and preflight.** Preset, permission and settings rejections, other
  plans never enabling `rewrite_commits`, the installed preflight against the
  descriptor and entry workflows, and the verbatim procedure.

Nothing starts an engine, provider or host, runs the YAML graph or certifies
GitHub conformance.

## What remains

- **Runtime.** The installed entries are wired and checked structurally with
  Agent Workflow main, which has
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129) and
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130). No
  release contains them yet, and nothing has run the YAML graph. Both inventory
  rows stay `not_implemented`, and native execution stays pending.
- **Installed gaps.** A commit policy, invocation prompts, manual-mode approval
  and dirty-checkout runs use the library procedure only. The installed
  workflow starts from a clean checkout. A composed dk-pr-fix plan does not run
  the splitting procedure.
- **Granularity.** Units and splits are whole paths. A single file's hunks are
  not split, and split PRs are not stacked on each other.
- **Policy providers and supplied skills.** They need #16 and Agent Workflow
  [#139](https://github.com/dk-code-factory/agent-workflow/issues/139).
- **Validation commands.** The installed workflow runs the configured commands
  on every new intermediate commit before any ref moves. The library procedure
  verifies trees, authorship and ownership only. Signed commits are listed,
  and their signatures are not kept on rewritten commits. The commands are not
  isolated from the helper's credentials or state mount (see Build, validate,
  apply); running them in a job without either needs a separate design.
- **Cross-links on the source PR.** Editing the source PR description to link
  the split PRs belongs to dk-pr-description.
- **Live evidence.** Live provider adherence and GitHub publication
  conformance need separate authorization.
