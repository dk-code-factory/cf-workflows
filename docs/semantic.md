# Semantic assessment

[`cf_workflows/semantic.py`](../cf_workflows/semantic.py) assesses custom inputs
after [structural admission](admission.md) and before their first dispatch. Like
admission, it is a library operation for the future preparation helper (#14); no
installed workflow calls it yet.

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

## Not established here

- Reusing an acceptance in a later run and writing an accepted revision back to
  the configuration store: [acceptance.md](acceptance.md). Without a ledger,
  each call assesses again.
- Supplied-skill execution: preflight still rejects it ([skills.md](skills.md), Agent Workflow #139).
- Real transport bindings for `validator_session`, `input_gates` and
  `open_input_gate`. The engine needs native sessions for the provider (Agent
  Workflow #129 for Claude; #130 for admitted assets), and workflow wiring is #14.

The tests use scripted sessions and a fake gate store. They do not show that a
real provider session follows the validator definition.
