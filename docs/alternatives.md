# Issue and PR alternatives

`cf-workflows:dk-issue-alternatives`, `cf-workflows:dk-pr-alternatives`, the
`cf-workflows:dk-alternatives-review` compatibility entry and the manual
`cf-workflows:dk-pr-review-alternatives` entry are thin entries over the shared
workflow (#27). [`cf_workflows/entries.py`](../cf_workflows/entries.py) defines
their presets and [`cf_workflows/alternatives.py`](../cf_workflows/alternatives.py)
holds their adapter procedures. The stages, settled analysis, current alignment
and allowances are the ones of [current alignment](alignment.md). Like the other
library operations, nothing installed runs them yet (see [What remains](#what-remains)).

## Entries

Each entry is a preset selection for the shared plan (one bundled adapter task,
explicitly empty `finalizers` and `reviewers`), the adapter procedure that task
runs and the entry's original role prompt and options.

| Entry | Adapter | Scope | Original options | Runs |
| --- | --- | --- | --- | --- |
| `dk-issue-alternatives` | `issue-alternatives` | Whole problem | `assessment_prompt`, `evaluation_prompt`, `scoring`, `selection` | Assessment to interactions |
| `dk-pr-alternatives` | `pr-alternatives` | Branch | The same, configured independently | Assessment to interactions |
| `dk-pr-review-alternatives` | `pr-alternatives` (delegate `dk-pr-alternatives`) | Branch | None; the delegate's options are passed separately | A fresh requested review, current alignment and verification |
| `dk-alternatives-review` | The routed delegate's | Routed | None; the delegate's options are passed separately | The delegate, under this entry's identity |

The role prompts and the seven stage prompts are the baseline texts, byte for
byte. The role prompt digests match [the inventory](../baselines/inventory.json).
The adapters have the `alternatives` kind, are read-only and run the admitted
`leaf` role; verification runs the admitted `verifier` role. A default run has no
custom input, so it runs no validator session and loads no skill. A non-default
prompt option (`assessment_prompt`, `evaluation_prompt`, `scoring` prompts and
`selection.prompt`) becomes an invocation prompt for the adapter and passes the
shared [semantic assessment](semantic.md). Tasks, finalizers, reviewers,
permissions, skills, unknown options and request authorizations reject before
the engine is contacted.

The installed `dk-pr-fix` and `dk-pr-review` workflows reject the alternatives
adapters. A single generic task would conceal the staged procedure.

## Stages

Every stage is one engine-managed native session with an exact typed assignment:
the stage prompt, the pinned target and head, the requirements, the original
instructions, `skills: []`, read-only tools and the files of the pinned head that
it may read. The engine observation must show the claim, role, digest and agent,
no loaded skill and only reads of those files. The helper then checks the output
before the [alignment ledger](alignment.md#ledger) accepts it. Evidence must cite
a requirement source or a file of the pinned head, optionally with a line range.
Past 4096 files, the changed paths and the guidance files stay readable first.

A rejected output or observation closes its attempt: the result never becomes
current, the reservation stays consumed, and the next call starts a new attempt.
A lost transport response keeps the claim open, so the next call resumes it and
the engine returns the original result.

| Stage | Checks |
| --- | --- |
| `assessment` | Purpose, scope, work type, mandatory constraints and rubrics: positive integer weights totaling 100, 0/50/100 anchors per criterion, no change-volume criteria in a feature rubric |
| `inventory` | Decisions with origin, scope, an assessment rubric, `questionable` and evidence. The branch scope covers `introduced` and `inherited` decisions, and an introduced or materially changed decision cites a changed path. The whole-problem scope has `whole_problem` decisions that cite the requirements |
| `comparison` | The decision's rubric, the pinned target revision and complete solution fields. The branch scope retains the current implementation (`pr`) and includes `smallest`. The whole-problem scope includes `smallest` and `alternative`, and a `pr` candidate only when a PR exists |
| `selection` | The selection policy below, against helper totals |
| `interactions` | `compatible`; required changes when incompatible |
| `alignment`, `verification` | The [alignment contract](alignment.md#gate); verification in a different session |

The helper computes each option's total as `sum(weight × rating) / 100` under the
persisted rubric and rejects a stated total that differs.

## Scope

- **Whole problem** (`dk-issue-alternatives`). The analysis starts from issue
  requirements. Without a PR it targets a repository and a pinned target commit.
  It persists nothing: the stages run on a temporary ledger that is removed
  afterwards, and no PR identity is invented. Engine claims alone make a repeated
  or interrupted call reusable. With a PR, the PR is an optional candidate and
  the analysis is kept in its own record (`.whole-problem.alignment.sqlite`).
- **Branch** (`dk-pr-alternatives`). The inventory covers the whole branch: the
  session may read every file of the pinned head, and it receives the paths that
  the branch changed. Only questionable decisions are compared.

With a PR, each scope's record is keyed by the PR, not by the logical run. The
analysis is therefore settled once per PR across the fix, review and manual
routes, sessions, heads and guidance versions.

## Selection

`selection.mode` defaults to `semi-manual`.

| Mode | Behavior |
| --- | --- |
| `automatic` | The session may pick a winner that passes the checks. Otherwise the choice stays pending. Nothing asks the user, and a repeated call starts no session |
| `manual` | The helper refuses an automatic pick. A pending choice opens one engine input gate with the evaluated options |
| `semi-manual` | Like `automatic`, but a pending choice, or an automatic pick that fails the checks, opens the gate |

An automatic pick must be feasible and confident, have no material doubts, and
lead every other feasible option by at least `min_score_gap`. A sole feasible
option substantiates every rejection. `enabled: false` disables automatic picks.
Every settled choice explains why each other option was rejected. A manual
choice must cite the recorded gate answer and match it. Gates are found by their
own identity (the comparison, request and question round), so issue-only runs
without a record find them too. A waiting gate returns `waiting` with no new
session or gate. An answer that names no evaluated feasible option is never
dispatched; the next round asks again and lists the rejected answers. A valid
answer refreshes only that selection.
A choice grants no edit or publication permission.

## Settled analysis and fresh comparison

The procedures reuse a settled stage whose consumed results are unchanged,
whatever its own inputs have become. Changed requirements, guidance,
instructions, options or heads do not repeat it. When a settled stage's consumed
results have changed (for example after an interrupted manual refresh), an
automatic route returns `authorization_required` with the
`cf-workflows:dk-pr-review-alternatives` skill and starts no session.

`manual_review` is the only route to a fresh comparison. It needs the retained
direct user request (`id`, `source`, `instruction`, `skill`, `origin: user`).
Its `instruction` must be the invocation's exact instruction. A downstream call,
an agent origin, another skill or extra fields reject. The request binds the
inventory, comparisons, selections and interactions, so each runs once under
that request. The shared assessment is reused. Replaying the request repeats
nothing, and an interrupted request resumes its open claim and reuses completed
siblings. A new request never renews the retained `architecture` allowance; only
`grant()` adds capacity. The entry then runs current alignment and independent
verification, returns the [gate](alignment.md#gate) outcome and stops. It never
claims ordinary review or readiness.

Current alignment binds the head, the requirements, the instructions, the
guidance and new evidence. A later head or new evidence refreshes alignment (and
verification of a mismatch) without any architectural session. The architectural
allowance is kept per PR, because that analysis is settled per PR. Alignment and
verification keep their allowance per logical run, as in [alignment](alignment.md):
a new logical run of the same PR gets its own alignment count.

An automatic route never supersedes an open attempt of an interrupted direct
request. It returns `authorization_required`, and the request resumes its claim.

## Composition

`run(..., caller=...)` composes an entry into a coordinator's route. No caller
authorizes a fresh comparison.

- `cf-workflows:dk-pr-fix` composes `dk-issue-alternatives` on its PR. After the
  analysis it runs current alignment and returns a `handoff`: the settled
  choices, the alignment and its required changes, which authorized
  implementation tasks apply and test. A retained approach needs no edit.
- `cf-workflows:dk-pr-review` composes `dk-pr-alternatives`. After the analysis
  it runs current alignment, independent verification of a mismatch and the
  alignment gate.

A coordinator composes only its own scope.

## Compatibility entry

`compatibility(route=...)` runs `dk-issue-alternatives` (`whole-problem`) or
`dk-pr-alternatives` (`branch`). The route is explicit, never inferred from the
ask. Every assignment and the result keep this entry's identity, the delegate's
skill and role prompt, the exact original instruction and both role prompt
digests. On a PR the entry reuses the same settled analysis as its delegate, and
it cannot authorize a fresh comparison. A retained dk-workflows assignment
(`legacy`) returns `unsupported` with no session: no earlier state is imported,
and a new comparison never stands in for its original result. A missing or
unknown route rejects before the engine is contacted.

## Verification

[`tests/test_alternatives.py`](../tests/test_alternatives.py) uses a disposable
Git repository with real target, head and later-head commits, a disposable state
namespace and a fail-closed fake engine. The engine has durable gates and native
sessions scripted per stage, and it reconciles a known claim to its original
result. Any unscripted session or unconfigured operation fails. Each case
asserts its transport call counts or new sessions per stage.

- **Presets.** Baseline role and stage prompt digests, options with provenance,
  no skill, rejected authority expansion, custom prompt options through the
  shared validator, and the installed workflow refusing the adapters.
- **Scope.** Issue-only analysis that persists nothing, invents no PR and reuses
  engine claims; separate whole-problem and branch records; each scope's
  inventory and comparison contract; inherited decisions inventoried and compared.
- **Selection.** Automatic mode that never prompts and never chooses over time,
  a refused pick without the score lead, a semi-manual question instead of an
  unqualified pick, manual gates asked once and resumed, answers that name no
  feasible option asked again, issue-only questions asked once, and an invented
  user source.
- **Boundary.** Analysis settled across requirements, guidance, options, heads
  and routes; dk-workflows#241 and #239 alignment refresh with no architectural
  session; independent verification; fresh comparison once per direct request
  with retained allowances and an explicit grant; rejected agent, delegated,
  composed and mismatched requests; interrupted automatic and manual work
  resumed through its claim; an automatic route that does not supersede an
  interrupted direct request; rejected outputs that start a new attempt;
  alignment allowances per logical run; changed paths and guidance readable past
  the bound; exhausted allowances never renewed.
- **Composition.** The fix handoff and its alignment refresh at a later head;
  coordinators limited to their scope.
- **Compatibility.** Both routes under the entry's identity, reuse of the
  delegate's settled analysis, no authorization, and unsupported legacy, route
  and target cases with no transport call.

## What remains

- The stages run as engine-managed native sessions only once Agent Workflow
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129) and
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130) land. The
  installed `dk-pr-fix` and `dk-pr-review` workflows do not run the staged
  procedure yet, so they reject its adapters. No local controller or launcher
  stands in for either.
- Plugin skill entries (`SKILL.md` and descriptor rows) wait for an installed
  workflow that runs them ([packaging](packaging.md)).
- No equivalent yet: saved per-skill alternatives options (entries are admitted
  from bundled defaults and invocation options), sourced
  [decision reconciliation](https://github.com/dk-code-factory/cf-workflows/issues/28)
  inputs, sharing one assessment between the whole-problem and branch records,
  and per-entry native model controls.
- Real native-session adherence needs separate authorization. These fixtures do
  not establish it.
