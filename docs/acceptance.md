# Acceptance reuse and configuration writes

[`cf_workflows/acceptance.py`](../cf_workflows/acceptance.py) retains
[semantic acceptance](semantic.md) so that unchanged custom inputs are not
assessed again. [`cf_workflows/writes.py`](../cf_workflows/writes.py) writes
accepted configuration revisions back and recovers interrupted writes. The
configuration entries ([configuration-skills.md](configuration-skills.md)) and
installed `dk-pr-fix` and `dk-pr-review` preflight
([below](#installed-preflight)) use both.

## Acceptance units and bindings

Acceptance is retained per unit, not per proposal. A unit is one custom prompt
or preference, one task instruction, one custom adapter, one supplied skill, or
the free-text policy as a whole. Each unit is bound to digests of:

| Binding | Applies to | Contents |
| --- | --- | --- |
| `content` | the unit | Prompt slot, target and text; instruction text and adapter name; adapter definition; skill digest, files, arguments and declared children |
| `resources` | prompts, custom adapters | Transitive closure of the configured resources the unit references |
| `adapter` | targeted prompts, instructions, skills | The targeted adapter (baseline or custom), its role definition digest and resource closure |
| `plan` | untargeted prompts and preferences, free-text policy | The set of selected adapters, each with its definition, role digest and resource closure |
| `request` | every unit | Exact original instructions and authorizations |
| `policy` | every unit | Requirements, prohibitions and effective permissions |
| `guidance` | every unit | Repository guidance files and symlinks |
| `capabilities` | every unit | Engine version and actual capability bindings |
| `validator` | every unit | Validator role, pinned definition digest and binding scheme |

A unit key is the digest of its kind and bindings. It contains no paths or
scope names, so moving an unchanged prompt to another scope, reordering the
selected tasks or changing unrelated configuration keeps it current. Unrelated
changes include limits, turns, non-matching scopes, unreferenced resources,
unselected adapters and unused roles, the helper image, and the configuration
revision or its `admission` record. Selecting different adapters (for example a
writing task instead of a review) invalidates untargeted text and policy.

Baseline adapter text is bundled control text and is never a unit. A change to
it invalidates only the units bound to that adapter: those that target it, and
untargeted text while it is selected. An adapter override
identical to the baseline is not custom, so it needs no assessment.

## Reuse

`semantic.admit(..., ledger=...)` computes the proposal's units after
structural admission:

- Each accepted session records the full set of units it judged together,
  subjects and context. A unit is reused only if it has its own current
  acceptance and belongs to the earlier set that covers most of the proposal.
  Inputs accepted in separate sessions are never combined unseen: the others
  become subjects judged against that set (reason `combination`).
- When every unit is reused, the proposal is admitted with its original
  `native:` result references. There is no gate read and no session.
- Otherwise only the remaining units are `subjects` of one validator session. Current units are listed under `accepted` with their
  original references, as context the verdict may cite. The result's `reuse`
  record shows the reused, assessed and invalidated units, with the bindings
  that changed for each (`new` or `coupled` when none did).
- An accepted session retains one acceptance per subject. A rejection or
  clarification is never retained as acceptance.

A subset of units judged together is reusable, because removing custom input
cannot create a contradiction. Units accepted after a clarification dialogue are
the exception: they were accepted together with the answers' effects, for example
a supplemental prompt, so each is reusable only while all of them are present.

A `needs_clarification` verdict is retained before its gate opens. If the gate
response is lost and the engine created the gate, the next call finds and
resumes it. If no gate exists, the retained question is asked again without a new
session. A question whose gate was already opened and consumed is assessed afresh
when a revision returns to it.

## Configuration writes

When a dialogue revises the saved configuration (for example by removing a saved
prompt or replacing saved task instructions), `writes.admit()` writes the exact
admitted revision with its `admission` record. No further approval step is
needed. The store offers `read()` and `write(expected_revision, document)` with
compare-and-swap. `FileStore` is the suite's own `configuration.json` with an
exclusive lock and atomic replace. Its revision is recomputed from the content on
every read and under the lock, never taken from the file's `revision` field, so a
hand edit that leaves that field unchanged still counts as another writer.

Each write is an intent in the ledger, recorded before dispatch with its request
ID, evidence references, observed base revision, exact document and content
digest. It is marked `pending` before the store is touched. Before admission reads
the configuration, every `prepared` or `pending` intent is read back:

| Readback | Result |
| --- | --- |
| Intended content | `applied` with a readback receipt; nothing is written again |
| Base revision | The same intent is dispatched again; compare-and-swap on the base prevents a duplicate |
| Anything else | `blocked`: another writer changed the store, so the effect is uncertain. The intent is kept and the current configuration is reassessed |

A lost response is settled by readback in the same call, never by a retry. A
conclusive compare-and-swap rejection settles `not_applied`. At most one intent is
unsettled at a time, and settled intents, including earlier successful siblings,
are never dispatched again. An applied intent keeps its receipt and drops its
copy of the document.

## Installed preflight

`dk_pr_fix.preflight(..., ledger=..., store=...)` (and `dk_pr_review.preflight`)
wires both into the installed workflow's preflight. `writes.open_state()` opens
the suite's own store and ledger at the XDG locations [below](#ledger).

- With `ledger`, `semantic.admit()` reuses current acceptances. A fresh
  preflight whose units are all current calls only `capabilities()`: no gate
  read and no validator task. It is admitted on the original `native:`
  references, and maps and stages the same inputs as the run that was assessed.
  A changed input sends only the units bound to it to the validator, with the
  others as cited context.
- With `store` (which needs `ledger` and replaces the `configuration`
  argument), every unsettled write intent is read back before the saved
  configuration is read. An unavailable store blocks before any gate read or
  task. When a clarification answer revised the saved configuration, the exact
  admitted revision is written back once after the installed-workflow checks
  pass, and the result carries `configuration_write`. When another run's write
  is still unsettled or the ledger fails, the admitted result still returns: the
  write is `deferred` when nothing was prepared, or `pending` for the next
  recovery to read back. A rejected, waiting or blocked preflight writes nothing. The result's `recovered_writes` lists the
  intents settled by readback.
- Preflight writes only the ledger and the store, never the target checkout or
  the state namespace; staging stays a separate `stage_request()` call.

Preset entries over the shared workflow (`custom=False`) still reject custom
inputs before any gate read and take neither argument.

Through `validator_binding.EngineTransport`, every validator task still blocks:
engine `8758775` records no operation receipt
([semantic.md](semantic.md#engine-binding)). A blocked verdict is never
retained, so each fresh preflight runs one task again. The tests that reuse an
`agent-run:` reference feed the binding a hypothetical receipt.

## Ledger

The ledger is a SQLite database at `$XDG_STATE_HOME/cf-workflows/configuration.sqlite3`
(fallback `~/.local/state/cf-workflows`). The configuration file is
`$XDG_CONFIG_HOME/cf-workflows/configuration.json`. Relative XDG values are
ignored. The database holds only:

- `acceptances`: unit key, identity, bindings, coupled units, evidence and request
  IDs, the native result and session references, and any clarification answers;
- `assessments`: the set of units each accepted session judged together;
- `questions`: retained clarification verdicts and their gate IDs;
- `writes`: write intents, their status, dispatch count, receipt and failure.

Writes are serialized by `BEGIN IMMEDIATE` transactions. The schema version is
`PRAGMA user_version = 1`, and a `meta` table names the suite and kind. A file with
another version, suite or schema is refused rather than read or migrated, so no
dk-workflows configuration, state or verdict is ever imported. An `admission`
record inside a saved configuration is not proof of acceptance; only the ledger's
native references are.

`Ledger.backup(path)` takes a consistent online copy. `Ledger.restore(backup, path)`
checks the backup before copying it. A restore preserves acceptances and
unsettled intents, so an uncertain write is still read back rather than reapplied.

## Not established here

- Real engine execution: the engine binding ([semantic.md](semantic.md#engine-binding))
  admits nothing without an operation receipt, and no released Agent Workflow
  contains #129/#130. The tests use scripted sessions, a fake gate store and a
  fake engine port, so reuse through a real engine and live validator sessions
  are not shown.
- Reuse of completed run work, budgets and the logical-run ledger: #15.
- Supplied-skill units are exercised only with supplied-skill execution patched on
  ([skills.md](skills.md), Agent Workflow #139).
- Backup, restore and upgrade of this ledger together with the rest of the
  suite's state: [recovery.md](recovery.md).
