# Prose cleanup

`cf-workflows:dk-plain-english` and `cf-workflows:dk-asd-ste100` are thin
entries over the shared workflow (#33).
[`cf_workflows/prose.py`](../cf_workflows/prose.py) holds their presets and their
adapter procedure. Like the other library operations, they are not wired into an
installed workflow yet (see [What remains](#what-remains)).

## Entries

| Entry | Adapter | Preset | References |
| --- | --- | --- | --- |
| `dk-plain-english` | `plain-english` | `tasks: []`, `finalizers: [plain-english]`, `reviewers: []` | none |
| `dk-asd-ste100` | `asd-ste100` | `tasks: []`, `finalizers: [asd-ste100]`, `reviewers: []` | optional, by exact digest |

Each adapter is bundled control text in `admission.ENTRY_ADAPTERS`: kind
`cleanup`, the admitted `leaf` role, `writes: true` and no history binding. The
role prompts are the baseline `prompts/default.md` texts, byte for byte; their
digests match [the inventory](../baselines/inventory.json). The baseline entries
have no options, and neither do these.

`preflight()` runs the shared [structural](admission.md) and
[semantic](semantic.md) admission with the saved configuration. A default run
has no custom input, so it runs no validator session and loads no skill. Saved
prompts and implementation preferences are custom inputs and go to the shared
validator, with [acceptance reuse](acceptance.md). An invocation may add only
prompts. Tasks, finalizers, reviewers, permissions, skills, a repository target
and any request authorization reject before the engine is contacted.

## Procedure

`run(admitted, task, ...)` is the helper side of one prose occurrence.

1. **Candidates.** For each file added or modified between the original PR base
   and the checkout head, the pass is planned per file:
   - `reused`: the last accepted unit is current and its output is still the head
     blob. No session runs.
   - `delta`: the unit is current but the file changed later, for example after a
     review fix. Only lines changed since the accepted output are candidates.
   - `full`: no current unit. Lines changed since the PR base are candidates.

   Only changed lines that can hold prose are candidates:
   - documentation (`.md`, `.markdown`, `.rst`, `.txt`, `.adoc`, `.asciidoc`):
     every line outside fenced and indented code blocks;
   - source files with a known comment syntax (`#`, `//` and `/* */`, `--`, `;`,
     by extension): lines that are wholly comment, plus Python docstrings. A small
     lexer follows strings (including multi-line strings and template literals)
     and block comments, so a comment-like line inside a string is code, and a
     line with code before `/*` or after `*/` stays protected. Python uses
     `tokenize`. A file with a construct the lexer cannot follow has no editable
     prose: heredocs, triple-quoted text blocks, C++ and Rust raw strings, YAML
     block scalars, Lua long brackets, and JavaScript or TypeScript regex
     literals (a `/` where an operand starts).

   Other file types, and binary or non-UTF-8 files, are listed as skipped and
   never edited.
2. **Session.** One engine-managed native session gets the exact instructions,
   the role prompt, the bundled assignment and checklist, applicable prompts, the
   preferences selected for each file, the candidate patches, quoted wording and
   reference availability. It may read and edit only the candidate files, with
   `skills: []`, no commit and no publication. A known claim reconciles to its
   original result, so a lost response never starts a second session.
3. **Observation.** The engine must report the claim, the admitted role, digest
   and agent, no loaded skill and only reads and writes of assigned files. Git
   history and the index must be unchanged, and no other file may change.
4. **Edits.** The helper compares each edited file with the head:
   - every changed line lies within two lines of a candidate line, and both the
     old and the new line are prose lines, so code is never edited and a
     comment never becomes code;
   - fenced code blocks are byte-identical;
   - code spans, URLs, link targets, identifier-like terms, numbers, RFC 2119
     keywords, operator characters (`<>=!&|^~%`) and machine directives in the
     changed text are unchanged as a
     multiset;
   - Python files keep the same AST apart from docstrings;
   - wording the user quoted in the instructions (`"..."`, `“...”` or
     `` `...` ``) is kept verbatim.

   The session decides what is prose and checks meaning. The helper only bounds
   the edits; these checks are a safety net, not a semantic proof.
5. **Report.** Changed locations must match the actual edits in every edited
   file. A pass without edits needs a sourced `no_op`. A complete pass cites its
   validation. Ambiguities cite candidate files.
6. **Commit.** A complete pass with edits is committed locally on top of the
   head, through a private index and a ledgered intent. An interrupted commit
   settles from that intent and never commits twice. Nothing is pushed: commits
   reach the PR only through authorized publication stages.

A rejected or incomplete pass restores the checkout and commits nothing. A lost
session response keeps the edits and the claim for reconciliation.

The result is a private report: status, per-file units, changed locations,
validation, ambiguities, reference-verification limits and
`prose_cleanup: {baseline, head, output}` commit IDs. A no-op pass outputs the
head.

## ASD-STE100 limits

The ASD-STE100 pass is best effort. Its report must state
`compliance: "not_claimed"`. The report text, ambiguities and commit message
may mention compliance, conformance or certification only in a fixed disclaimer
(for example "compliance is not claimed" or "does not claim certification").
Any other mention rejects the pass. Without a supplied reference, rule numbers
anywhere in that text and `reference_checks` reject, and the helper
adds an explicit limit naming the unchecked rules and dictionary and the
official starting points. With references (`{id, path, sha256}`, read only when
the bytes match), `reference_checks` must cite a supplied reference. The helper
still records that full compliance is not claimed. `dk-plain-english` takes no
references.

## Refresh and reuse

Units live in a versioned SQLite ledger in the suite's state home
(`prose.ledger_path`), keyed by logical run and a stable occurrence ID (adapter,
phase and task instructions). History is never deleted.

- A file's unit binds its base blob, its accepted output blob and the
  `configuration.resolve()` binding of the implementation preferences that
  select it. A changed preference refreshes only the files it selects.
- A source change refreshes only the changed file, as a delta.
- Instructions, prompts, the role prompt, the assignment, repository guidance and
  references bind every unit. A change there means a full pass. A reference
  that becomes available or changes is never reused under an earlier limited
  result.

## Composition

`occurrences(admitted)` lists the prose finalizers of any admitted shared plan,
in plan order. Ordering, scoped replacement of the whole finalizer list and an
explicit `finalizers: []` all come from admission. Each occurrence runs through
the same `run()`. In dk-workflows plain English is the default fix finalizer.
Here the default plan follows the portable PR-fix example and has no prose
pass, so select it explicitly.

The installed `dk-pr-fix` workflow would run a prose adapter as a generic
writer without these checks. Its preflight therefore rejects prose adapters
until the workflow calls this procedure.

## Verification

[`tests/test_prose.py`](../tests/test_prose.py) uses disposable repositories, the
real acceptance and prose ledgers, and a fail-closed fake engine. Its sessions
edit the checkout once per claim and reconcile on replay. Any unscripted session
or unconfigured operation fails. Each case asserts transport call counts or
session counts. Coverage:

- **Presets.** Baseline prompt digests, finalizer presets, default admission
  with only a capability query, and every expansion rejected.
- **Direct.** Edits only in changed prose, with unrelated, protected and binary
  content unchanged and a local commit. Reuse without a session. Rejection and
  restore for edits outside changed prose, code in non-Python sources,
  comments turned into code, code beside block-comment delimiters,
  comment-like template-literal and text-block content, code after a regex
  literal, indented code blocks, unrecognized file
  types,
  code blocks, code spans, link
  targets, numbers, requirement keywords, Python code, unchanged files and
  quoted wording. Sourced no-ops, incomplete passes, observation violations, a
  lost response and an interrupted commit.
- **ASD-STE100.** Explicit limits without references; rejected compliance and
  certification claims (including a stray negation elsewhere in the sentence,
  the commit message and ambiguities), invented rule numbers and unsupplied
  references; a
  supplied reference that enables checks and refreshes the full pass.
- **Refresh.** A later source change as a single-file delta; changed
  instructions as a full pass; a preference change that refreshes only the
  files it selects, with the validator and acceptance reuse.
- **Composition.** Ordered finalizers, scoped replacement and explicit empty
  lists. Two composed passes in order, then a replacement run with reuse and a
  delta. `dk-pr-fix` preflight rejecting a prose finalizer.

## What remains

- These procedures run as engine-managed native sessions only once Agent Workflow
  [#129](https://github.com/dk-code-factory/agent-workflow/issues/129) and
  [#130](https://github.com/dk-code-factory/agent-workflow/issues/130) land. No
  local controller or launcher stands in for them.
- The installed `dk-pr-fix` graph does not call this procedure yet, so its
  preflight rejects prose finalizers. Plugin `SKILL.md` and descriptor rows wait
  for an installed workflow that runs these entries ([packaging](packaging.md)).
- A user-supplied prose skill, such as an external replacement finalizer, needs
  [#139](https://github.com/dk-code-factory/agent-workflow/issues/139) and #16.
- The edit checks are deterministic proxies. Real session adherence and
  meaning preservation need separately authorized live evidence.
