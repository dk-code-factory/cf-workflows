# Semantic assessment

[`cf_workflows/semantic.py`](../cf_workflows/semantic.py) assesses custom inputs
after [structural admission](admission.md) and before their first dispatch.
`dk-pr-fix` and `dk-pr-review` preflight run it ([installed workflow](#installed-workflow)),
and [`cf_workflows/validator_binding.py`](../cf_workflows/validator_binding.py)
binds its transport to Agent Workflow ([engine binding](#engine-binding)).

## Routing

`semantic.admit()` takes the same inputs as `admission.admit()` and runs it first.
Only a result with preflight `semantic_required` goes further. The default plan
has no custom inputs, so it returns after structural admission with
`semantic: null` and makes no gate read or validator session. Structural
rejections, a blocked engine query and supplied skills (rejected until Agent
Workflow #139) also return unchanged.

A proposal with custom inputs also needs the engine-level `durable_input`
capability for clarification gates. Without it, structural admission rejects
the proposal before any session.

## Validator session

Each call starts at most one engine-managed native session through the
transport's `validator_session(assignment)`. The assignment fixes:

- the bundled `configuration-validator` role and its pinned definition digest;
- `skills: []`, read-only tools and no publication;
- the deployment agent that runs it (the first agent by name);
- the exact `admission_input` and the earlier clarification answers;
- the files it may read, with their admitted digests: configured resources,
  repository guidance and a supplied skill's declared files;
- every path a verdict may cite;
- the `subjects` that need a verdict and the `accepted` units reused as context,
  with their original result references ([acceptance.md](acceptance.md));
- a claim derived from the proposal's evidence ID, so the engine can reconcile an
  interrupted session.

The engine's observation must show that role and digest, the assigned agent, no
loaded skills, and only reads of the listed files. Any other operation discards
the verdict and blocks preflight. Examples are running a command or a skill,
writing, publishing, or reading anything else.

## Verdicts

The validator returns `verdict`, `summary`, `conflicts`, `uncertainties` and
`resolved`. Every item names at least one known path: a custom input, a
requirement or prohibition, a permission, the request or a readable file.
Unknown fields reject the output, so a verdict cannot carry waivers, permissions
or capabilities.

| Verdict | Requires | Preflight |
| --- | --- | --- |
| `accepted` | No open conflicts or uncertainties; each resolved item cites recorded answers | `admitted` |
| `needs_clarification` | At least one conflict or uncertainty | `waiting` on a durable gate |
| `rejected` | At least one conflict | `rejected` |
| `blocked` | A reason in `summary` | `blocked` |

`needs_clarification` is the contract's name for the issue's
"reconciliation-required". A verdict that breaks these rules blocks preflight,
as does a transport or session failure. Neither is a structural rejection.

Acceptance changes only `semantic` and adds the native result reference to
`evidence_refs`. The admitted snapshot, including permissions, prohibitions and
capability requirements, stays exactly the structural one. Acceptance cannot
supply a missing capability such as `merge_guard`; structural admission rejects
that before any session.

## Clarification

The gate follows the engine's durable external input model. The helper opens
one gate through `open_input_gate(request)`, bound to the original
`request_id` and pinned to the proposal's evidence ID. The gate payload holds
the full verdict, the validator reference and the editable paths.

On every call the helper reads `input_gates(request_id)` and replays the gates
in creation order. Each gate is consumed at most once. The next gate for the
current proposal is the first unconsumed gate pinned to its evidence ID:

- A waiting gate returns `waiting` again, with no new session or gate.
- `supplement` adds the answer text as an invocation-local `additional` prompt.
- `remove` deletes custom prompts, preferences, task instructions, skill
  references or overrides of baseline adapters. A newly named adapter can't be
  removed this way, because its selections would have nothing to run.
- `replace` rewrites inline prompt text or task instructions.
- `reject` settles the proposal as `rejected`.

Answers can never edit requirements, prohibitions, permissions or the original
request. The revised proposal is admitted structurally again. If the answer
can't be applied, or the revision fails structural admission, the proposal and
verdict stand. One new gate then asks again with the reason, and later replays
reuse that gate instead of opening another. Otherwise the exact revised proposal
gets a fresh validator session. If a revision returns to an earlier proposal,
its consumed gates are not reused, so it is assessed afresh. Dialogues have no
round limit; each answered round costs one read-only capability query on replay.
Removing every custom input returns to the default path without a session.

A changed proposal has a different `request_id` and evidence ID, so earlier
gates and answers do not apply to it. Answers are recorded in `clarifications`,
apart from `evidence_refs`. The result also carries the exact `proposal` that was
assessed.

## Installed workflow

[`dk_pr_fix.preflight()`](../cf_workflows/dk_pr_fix.py) calls `semantic.admit()`
instead of structural admission alone. Before any gate read or validator task,
the installed-workflow limits apply to the structural result and to every
revision: a repository-only target, configuration, alternatives and entry-only
adapters, an untargeted or repeated `behavior_replacement`, adapters, prompts
or preferences that declare resources (or a resource-backed prompt), and
unsupported authorizations reject. The workflow mounts no configured resource,
so such an input would reach its occurrence without the files it names. Only
an `admitted` result is mapped and staged. A
`waiting` result carries its gate and maps nothing; the next preflight call
replays the answered gate and admits the revision again.

After acceptance the custom inputs reach the shared workflow through declared
inputs of `main.yml` and `review.yml`:

| Custom input | Workflow input | In the plan |
| --- | --- | --- |
| Per-task instructions | `tasks`, `finalizers`, `reviewers` selections | Appended to the adapter assignment |
| Custom adapters without resources | `adapters` | The adapter definition |
| Inline prompts and preferences without resources | `prompts`: `{id, slot, target, text}` plus a preference's `scope` | `prompts` of each targeted occurrence; a `behavior_replacement` replaces its adapter's assignment |
| Free-text requirements and prohibitions | `policy`: `{requirements, prohibitions}` | `policy` of every occurrence |

An untargeted prompt reaches every occurrence. The `task`, `reviewer`,
`finalizer` and `reply` slots narrow it to task-phase non-review occurrences,
reviewers, finalizers and the responder; other slots apply to any occurrence.
The helpers pass a preference's scope as data and do not narrow it to matching
paths. Action names in `prohibitions` stay permission denials and are not
policy text. `prompts` and `policy` are present only for accepted custom inputs,
so the default plan's inputs, occurrences and fingerprints are unchanged.

Preset entries over the shared workflow (feedback, publication, prose, CI and
reviewer entries) pass `custom=False`: their entry workflows declare no prompt
inputs, so custom inputs reject before any gate read. Preflight passes no
acceptance ledger, so each preflight assesses again.

## Engine binding

`validator_binding.EngineTransport` implements the transport over an engine
port with the installed workflow [`workflows/assess.yml`](../cf_workflows/workflows/dk-pr-fix/workflows/assess.yml):

| Transport operation | Engine primitive |
| --- | --- |
| `capabilities()` | The engine's contract list; `durable-external-input-v1` maps to `durable_input` |
| `validator_session(assignment)` | `assess.yml` `validate`: one `agent/run` on the literal `workflows/validator/configuration-validator.yaml`, no `skill`, the verdict schema and `repair-attempts: 0`, correlated by the assignment's claim |
| `open_input_gate(request)` | `assess.yml` `clarify`: one `input/wait` with the proposal's evidence ID; the payload holds the request ID and the semantic payload |
| `input_gates(request_id)` | The engine's `ExternalInputGate` records for the request: `pending` is waiting, `accepted` and `consumed` are answered, `canceled` and `replaced` are skipped |

The validator definition is the engine's canonical YAML conversion of the
pinned bootstrap TOML; any other bytes block the task. The task input carries
the assignment and the exact text of every readable file, read again and
checked against its admitted digest, so the validator needs no file access. An
input larger than the engine's 1 MiB task mailbox (less a 64 KiB reserve)
blocks before any run. An answer whose evidence ID differs from its gate blocks.

The observation fields `check_session` requires compare with what Agent
Workflow `8758775` records as follows:

| Field | Source |
| --- | --- |
| `session_id` | Observed: the task's opaque `agent` reference |
| `agent` | Observed provider, which must be the assigned agent's |
| `role`, `definition_sha256` | Fixed by the admitted step: a literal definition whose bytes the binding pins |
| `skills_loaded` | Fixed by the step and the engine: no `skill` selection, and managed sessions exclude discovered skills |
| `operations` | Not recorded. Native tasks share the deployment's one runtime role and its tools, and only supplied-skill invocations get observed receipts |

Without an operation receipt the binding reports `operations: null`, and
`check_session` blocks the verdict: *the engine recorded no operation receipt for
the validator session, so its read-only boundary is unverified*. So with this
engine every custom input blocks at preflight. Admission needs an engine that
records the task's tool operations, or that runs the validator under its own
read-only tool policy.

## Not established here

- Reusing an acceptance in a later run and writing an accepted revision back to
  the configuration store: [acceptance.md](acceptance.md). Without a ledger,
  each call assesses again.
- Supplied-skill execution: preflight still rejects it ([skills.md](skills.md), Agent Workflow #139).
- A real engine port. The binding runs nothing itself, and running an installed
  workflow needs Agent Workflow #130 in a released engine. The validator's
  read-only boundary also needs an engine operation receipt (above).

The tests use scripted sessions, a fake gate store and a fake engine port. They
do not show that a real engine or provider session follows the validator
definition.
