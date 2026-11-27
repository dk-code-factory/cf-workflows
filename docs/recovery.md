# Upgrades, backup and restore

[`cf_workflows/recovery.py`](../cf_workflows/recovery.py) upgrades, backs up and
restores the suite's retained state. Like admission, it is a library operation:
no installed workflow or command calls it yet.

## What is retained

The suite state home is `$XDG_STATE_HOME/cf-workflows` (fallback
`~/.local/state/cf-workflows`). The saved configuration is
`$XDG_CONFIG_HOME/cf-workflows/configuration.json`.

| Store | Path under the state home | Rows ([inventory](inventory.md)) |
| --- | --- | --- |
| `configuration` | `configuration.sqlite3` | dk-config, dk-preferences, prose entries |
| `prose` | `prose/<id>.sqlite3` | dk-plain-english, dk-asd-ste100 |
| `run` | `repositories/<id>/runs/<run>.sqlite` | dk-pr-fix, dk-pr-review, dk-pr-iter and every row that runs over the shared workflow or publishes |
| `feedback` | `…/runs/<run>.feedback.sqlite` | dk-pr-fix, the feedback entries |
| `decisions` | `…/runs/<run>.decisions.sqlite` | dk-pr-decisions (library procedure) |
| `reconciliation` | `…/runs/<run>.reconciliation.sqlite` | dk-pr-decisions, dk-pr-fix, dk-pr-iter (installed workflow) |
| `alignment` | `…/runs/<run>[.whole-problem].alignment.sqlite` | the four alternatives entries; dk-pr-fix, dk-pr-review, dk-pr-iter (read by the installed gate) |
| `drafting` | `…/runs/<run>.drafting.sqlite` | dk-pr-description, dk-commit-message, dk-issue, dk-commit-split |
| `splitting` | `…/runs/<run>.splitting.sqlite` | dk-commit-split, dk-pr-split (library procedure) |
| `split_operations` | `…/runs/<run>.split-operations.sqlite` | dk-commit-split, dk-pr-split (installed workflow) |
| `writer` | `repositories/<id>/workspace-writer.sqlite` | writing runs |

Each namespace also holds its ownership marker and the content-addressed
`artifacts/` and `requests/`, and the `preparations/` checkpoints.
dk-repo-onboard, dk-pr-approvers and dk-ci-flakes keep no state of their own.
`recovery.ROWS` records this for every row.

`recovery.STORES` states why each table of each store is retained: identity,
recovery, evidence or reusable analysis. `audit()` refuses any other file, any
table without a stated purpose, links and special files. So a transcript, a
rendered configuration copy, a derived summary or another suite's state (for
example dk-workflows `.pr-fix-state` or settings) never enters a backup
unnoticed. SQLite sidecars, files left by an interrupted atomic write and
pre-upgrade backups are skipped.

## Backup

`backup(home, destination, engine=..., configuration=...)` writes a new
directory:

1. With an engine adapter, it first takes the engine's database backup and
   recovery bundle, which needs the controller stopped
   (`agent-workflow stop`, `backup`, `bundle`). Their digests go into the
   manifest. Without one, the backup holds this suite's state only. An unfinished
   claim then needs its engine run or an explicit continuation, as
   [dk-pr-fix backup boundaries](dk-pr-fix.md#backup-boundaries) describe.
2. It snapshots every SQLite store at one instant while writers may continue.
   It opens a read transaction on each store and keeps the set only if no store
   committed between the first and the last (`PRAGMA data_version` on separate
   probe connections) and no store appeared. Otherwise it retries, up to
   `attempts` times, and then fails. Committed WAL records are included, and an
   uncommitted transaction is not. Stores are opened read-write (never
   created) so SQLite rolls back the hot journal a writer killed mid-commit
   leaves in a rollback-journal store; only reads are issued. Each copy is a
   self-contained file.
3. It copies the marker, the upgrade journal, the checkpoints and the requests,
   and then the artifacts they reference. Each content-addressed file must match
   its name.
4. It copies the saved configuration under its writer lock. The document must
   be a cf-workflows configuration with schema version 1.
5. It writes `manifest.json` last, with each file's kind, schema version and
   SHA-256. A backup without a manifest is incomplete. A failed backup removes
   its directory.

Backups are private: they hold exact requests, authorizations and evidence.

## Restore

`restore(backup, home, engine=..., configuration=..., install=...)` first
verifies everything read-only: the manifest's format and suite, every digest,
no extra or missing file, and each store's integrity, foreign keys, identity,
justified tables and schema version. A store newer than this release refuses;
install the newer release instead of downgrading. It never merges:

- the suite state home must be empty or absent;
- the saved configuration is written only where none exists or an identical one
  does;
- a backup that holds the engine bundle needs the engine adapter, which restores
  its database and bundle first.

## Engine binding

`recovery.Engine(cli, workspace)` is the engine adapter bound to the
`agent-workflow` CLI of one engine workspace. It uses the maintenance commands
of Agent Workflow `main` at
[`8758775`](https://github.com/dk-code-factory/agent-workflow/blob/87587758fdb3633db744aa54d0380f31774fb1e3/cmd/agent-workflow/maintenance.go).
No release has them yet.

| Adapter call | Engine side |
| --- | --- |
| `status()` | The controller is `stopped` only when the workspace's `.agent-workflow/owner.lock` is free. `backup`, `bundle` and `restore` take the same lock. The version is from `agent-workflow version`. |
| `stop()` | `agent-workflow stop`, then it waits until the lock is released. |
| `backup(path)`, `bundle(path)` | `agent-workflow backup PATH`, `agent-workflow bundle PATH`. |
| `retained(bundle)` | It reads the bundle's `assets/<origin>/<id>/` trees. Each tree must hold exactly the files its `bundle.json` lists, with their sizes, digests and the modes the engine materializes (`0444`, or `0555` when executable). A tree's `helpers/` files give the same helper-manifest digest that an admitted plan records. |
| `restore(database, bundle)` | It checks the whole bundle first, and then puts its `workspaces/`, `providers/`, `invocations/` and `assets/` trees back. Then it runs `agent-workflow restore DATABASE`. Every entry keeps its exact permission bits from the bundle, because the engine verifies the modes of a retained asset tree (`0444`/`0555` files, `0555` directories) on resume, on use and on the next `bundle`; only setuid, setgid and sticky bits are dropped. It refuses an existing entry at a bundle path, before it changes anything. If extraction or `agent-workflow restore` fails, it removes every entry it created, so a retry starts again. |
| `resume(run, retry_uncertain=...)` | `agent-workflow resume --detach`. This restarts the controller. |

A bundle entry outside these trees refuses. So do a duplicate entry, a path
that escapes, and a link that is outside `workspaces/` or points out of it. An
absolute link refuses too, even one that points inside `workspaces/`: the engine
exports such links verbatim, but they name the old state path, so this fails
closed instead of restoring a link to another place. The engine itself restores the database. It puts
every unfinished run into `needs_attention` with `restore_uncertainty`. The
controller then refuses to resume that run until `retry_uncertain=True`
acknowledges that an uncertain effect can repeat. The engine checks a retained
asset tree's identity again when it uses the tree.

The state is then staged next to its final place and renamed into it. The
report lists, per logical run:

- unfinished original claims, unsettled publication intents and `applying`
  local Git operations;
- for each retained admitted plan, whether its helpers are `installed`,
  `retained` by the engine bundle, or `missing`.

A run with unfinished claims whose original helpers are missing is `blocked`.
Its evidence is still restored, but it is never resumed with a replacement
plugin's resources. The installed helpers already refuse a plan whose helper
closure differs ("prepared helper closure differs before dispatch"). Stores
that need a migration are listed under `upgrade`.

After a restore, nothing is replayed. An unsettled publication is reconciled
read-only and never resent. An unfinished claim resumes only through its
original engine execution (the engine quarantines it as `needs_attention`), or
by an explicit continuation. Completed siblings, consumed allowances, ceilings,
grants and absolute deadlines are the backed-up values, so an unchanged fresh
session reuses completed work without a native session.

## Upgrade

`upgrade(home, install=..., configuration=...)` moves every store to the
installed release's schema:

1. Before changing anything, it checks every store and the saved configuration.
   A newer or unsupported version, a foreign store or a non-cf-workflows
   configuration refuses the whole upgrade.
2. It records `upgrade.json` (`prepared`): the previous and target release
   (descriptor version, closure digest and helper digest), each store's version
   and the backup location.
3. It takes one pre-upgrade backup under `upgrades/<id>/`, without the engine.
4. It marks `migrating` and migrates each store. Logical-run ledgers are opened
   by the target installation's own helpers in a separate interpreter, which
   migrate versions 1 and 2 to 3 in one transaction. The other stores are at
   their only version, 1.
5. It marks `applied`.

An interrupted upgrade resumes from its journal. It reuses a complete pre-upgrade
backup and never replaces it, and a store that is already migrated is left
alone. A different target release cannot take over an unfinished upgrade.
Backups are refused while an upgrade is in progress. The saved configuration is
never rewritten. The suite's settings and state are never imported from
dk-workflows.

## Cost evidence

`cost(proxies, usage)` keeps fixture counts (sessions, processes, tokens) apart
from provider-reported dollar cost. `usage` takes the engine's per-run summaries
(`run_id`, nullable `cost_usd`, `known_cost_usd`, `complete`) for the original
run and every later session. The measured total is a sum only when every run
reported a complete cost. Otherwise it is null, with the known subtotal shown.
Nothing is priced or estimated. No fixture produces measured cost.

## Verification

[`tests/test_recovery.py`](../tests/test_recovery.py) uses disposable
repositories, a fresh state home per case and fail-closed fakes. A fake engine
adapter backs up and restores the fixture engine's durable state (claims, gates,
sessions) and retains the admitted helper files. Fake Codex and Claude host
adapters record native dispatches. The cases cover the following:

- **Catalog.** The table catalog equals the tables each real store creates. An
  extra table, a foreign file or a link rejects. Every inventory row names its
  stores, and every store has a recovery case.
- **Backup and restore.** A concurrent writer commits a parent to one ledger and
  its child to another between snapshots. The snapshot is retaken, and with the
  check disabled the copies disagree. A writer that never pauses fails the
  backup without leaving a partial one. Committed WAL records are captured, and a
  writer killed mid-transaction is not; the hot journal of a rollback-journal
  writer killed mid-commit is rolled back by backup and upgrade. Tampered, extra, incomplete, foreign and
  newer backups and nonempty targets reject before any change. The saved
  configuration is restored only into an empty place, and dk-workflows settings
  reject. The engine bundle needs a stopped controller and is restored together.
- **Partial recovery (both hosts, capacity one).** One sibling is complete, one
  has a lost result, and there are a review, a deadline, a continuation grant and
  a publication whose response was lost before its effect. After the backup,
  every local file and engine record is lost, and then restored. A fresh session
  reuses the completed siblings without a dispatch. A fresh claim cannot replace
  the quarantined original, which settles from its retained session. The
  publication is reconciled `unknown` and never resent. Allowances, grants and
  deadlines are unchanged, and the native-session count stays 3.
- **Changed resources after restore.** Only the occurrence bound to a changed
  resource refreshes, and the retained ceiling is never replenished by a new
  configuration or execution.
- **Plugin replacement and deletion.** The replacement's helpers refuse the
  original plan in a separate interpreter. The engine-retained original helpers
  resume it and reuse its completed sibling. Without retained assets, the run is
  reported `blocked`, and its evidence is still restored.
- **Upgrade.** Version 1 and 2 ledgers and a current store migrate under a
  journal interrupted after the first store. The resumed upgrade keeps the
  retained backup byte-identical. Windows, allowances and uncertain publications
  survive. A newer store or a foreign or newer configuration refuses with nothing
  changed. A restored pre-upgrade backup reports its needed migrations.
- **Entry stores.** Each entry store is exercised through its own entry's
  harness across a backup, a total loss and a restore: an accepted prose pass and
  draft are reused with no session; a lost decision response settles to its
  original claim; the installed reconciliation record keeps its result and
  request baseline, so a fresh session reuses it and an edit refreshes only what
  it touches; an interrupted commit rewrite reconciles its original refs; a
  settled alternatives stage is reused while an open one resumes its original
  operation; and an uncertain configuration write is settled by readback.
- **Cost.** Proxies stay apart from reported cost, a later session's run is
  counted once, and unknown cost stays null.
- **Engine binding.** `recovery.Engine` runs against a fail-closed fake of the
  8758775 maintenance CLI. Synthetic bundles use the engine's own layout (both
  retained-asset formats). The installed helpers in a tree map to the installed
  helper digest. Tampered, unlisted, wrongly-moded, orphaned, stray, foreign,
  duplicate, escaping and linked entries refuse before any change. A restored
  tree keeps the engine's exact modes (`0444`/`0555` files, `0555` directories
  under `files/`, `0700` above). A failed `agent-workflow restore` removes the
  extracted entries, and a retry then succeeds. A held owner lock refuses backup and
  restore, and `stop()` waits for its release. Backup, total loss and restore
  call `version`, `backup`, `bundle` and `restore` once each. A second restore
  over restored engine state refuses. Resume needs the acknowledgement. After a
  plugin replacement, a bundle tree with the original helpers reports the run
  `retained` and resumes it from the restored tree. A tree with only the
  replacement's helpers reports it `blocked`.
- **Engine integration (opt-in).** With `CF_AGENT_WORKFLOW` set to a local
  Agent Workflow clone, a test builds the CLI from the pinned commit, as
  [`scripts/check_package_build.py`](../scripts/check_package_build.py) does. It
  runs a fixture workflow on the host: one command step, then an external-input
  gate. The test backs up the suite and the engine, deletes both, and restores
  them. The restored run is quarantined. A resume without the acknowledgement is
  refused, and the acknowledged resume restarts the controller. After the gate
  is answered, the run succeeds. The completed step does not run again. No
  provider, container or network is used.

## What remains

- No released engine has the maintenance commands or retained assets used here
  (Agent Workflow #130 and the release that ships it; the plugin pins `0.2.2`,
  which has no release). `recovery.Engine` is bound to unreleased `main`
  (`8758775`). The opt-in integration test runs only on a source build, so no
  engine version is supported or verified.
- The bundles with retained asset trees are synthetic: the tests build them in
  the layout and modes of `pkg/workflows/bundle.go` at `8758775`. No bundle that
  the real engine exported with an asset tree has been read or restored; the
  opt-in integration bundle has none.
- No command or installed workflow calls these operations yet. No provider
  session or host restart has been exercised. The installed dk-pr-fix workflow
  has not been recovered on the engine either: its helpers run as `action-v1`
  steps in container jobs, and `agent-workflow plugin test` at `8758775` refuses
  container jobs. So per-row recovery on the engine waits for a released engine
  that can run the installed workflow.
- Live dollar cost is not measured. It needs authorized live runs whose engine
  usage reports cost.
- The entry-store cases cover each store's recovery path once through one
  entry's harness. They are not separate end-to-end recovery scenarios for each
  of the 29 rows. The [acceptance matrix](qualification.md) lists each row's
  own recovery or permission-denial scenarios.
