# Sourced PR decisions and reconciliation

`cf-workflows:dk-pr-decisions` is a thin entry over the shared workflow (#28).
[`cf_workflows/decisions.py`](../cf_workflows/decisions.py) holds the preset and
the adapter procedure. Like the other library operations, it is not wired into
an installed workflow yet (see [What remains](#what-remains)).

## Entry

The entry has three parts:

- A preset selection for the shared plan: the bundled `reconcile-decisions`
  task, then the shared `publication` stage, with explicitly empty `finalizers`
  and `reviewers`.
- The adapter procedure that the task runs: the admitted `leaf` role, the
  bundled `reconcile-decisions` assignment and the helper operations
  `collect`, `reconcile`, `handoff`, `draft`, `propose` and `publish`.
- The original role prompt, language-review prompt and options from the pinned
  baseline. `config.json` and `prompts/default.md` are kept byte for byte; their
  digests match [the inventory](../baselines/inventory.json).

| Option | Default | Check |
| --- | --- | --- |
| `reconciliation_prompt` | The baseline prompt | Nonempty text. A changed value becomes a `reconciliation` prompt for the adapter and goes through the shared [semantic assessment](semantic.md). |
| `language_review_passes` | `3` | Integer `1..3`. |
| `section_heading` | `Decisions` | Text without newlines or `#`. |

A default run is admitted from bundled control text, so it runs no validator
session and loads no skill. An invocation may add only `prompts`. Tasks,
finalizers, reviewers, permissions, unknown options and a repository target
reject before the engine is contacted. The only accepted authorization is
`edit_pr`. It must also be enabled by accepted configuration, and a configured
`edit_pr` prohibition rejects it. A `behavior_replacement` prompt for the adapter
replaces the role prompt and suppresses the reconciliation prompt.

Composition: `dk-pr-fix` already selects `reconcile-decisions` by default.
`reconcile` accepts either preflight result and runs the same procedure, so
direct and composed reports have the same shape. A plan without the adapter
rejects. `handoff(report)` is what dependent tasks consume: the settled
commitments, exclusions and pauses, unresolved contradictions, blocking
questions and the result reference they bind.

## Sources

`collect` turns the PR capture, any captured linked issues, the exact request
and the user's recorded gate answers into sources. Each source has a durable
identity (`pr-body`, `comment:<id>`, `review:<id>`, `inline:<id>`,
`issue:<n>`, `issue-comment:<n>:<id>`, `instruction:request`,
`answer:<id>`), its URL when public, the SHA-256 of its exact text and its
discussion order. Pending reviews are skipped. The request and the answers are
private. The request is ordered by when it was issued: the ledger fixes the
fingerprint of each discussion source present at the run's first reconciliation.
The request comes after that unchanged discussion and before any later or
since-edited source, so a later user comment or edit outranks it. Gate answers come last.

The helper assigns authority; the session cannot raise it:

| Authority | Source |
| --- | --- |
| `user` | The request, gate answers, and comments by the configured immutable `accounts` |
| `maintainer` | `OWNER`, `MEMBER` or `COLLABORATOR` association |
| `participant` | Any other person |
| `bot` | A `Bot` account or a `[bot]` login |

The PR body's current decision section is a record, not a source. It is given to
the session, but it cannot settle, restore or revise a decision, and it is not
part of the reuse binding. So publishing the section never invalidates the
result it came from.

## Reconciliation

`reconcile` runs at most one engine-managed native session per call. Its
assignment carries the exact request, the role prompt, the effective
reconciliation prompt, the language-review prompt and limit, configured
requirements and prohibitions, all sources, the published entries, the
subjects, the retained decisions and the previous result. It loads no skill,
uses read-only tools and may not write or publish.

The session returns decisions, contradictions, questions and a language-review
record. The helper accepts the result only when all of these hold:

- Every decision cites known sources. A `settled` decision cites a `user` or
  `maintainer` source; suggestions, bots and the published section alone are
  proposals. Only a `user` source excludes or pauses a decision, and a pause
  keeps its revisit condition.
- A contradiction is resolved only by a `user` source at least as recent as every
  source it involves, including the sources of the decisions it names. An older
  agreement, a maintainer's role or a recommendation never resolves it, and the
  current request outranks earlier discussion.
- A decision that was excluded or paused is settled again only by a user source
  that is later than the exclusion's sources and is itself new or changed. The
  unchanged request or an unrelated new comment cannot revive it.
- An unaffected retained decision or contradiction changes only by citing a
  changed or new source, and every affected one is returned.
- The language review used at most the configured passes and stopped at the
  first unchanged pass.
- The result has no other fields, so it cannot propose operations.

The report keeps `settled`, `proposals`, `excluded`, `paused` and `contested`
decisions apart, with `unresolved` and `resolved` contradictions. A decision
named by an unresolved contradiction is `contested`, never settled. Unresolved
contradictions and blocking questions open one engine input gate per result and
set `dependent_work` to `blocked`. A waiting gate returns with no session. A lost
gate response is reopened from the retained result.

## Reuse and refresh

The ledger is a versioned SQLite file per logical run
(`runs/<sha256(logical_run)>.decisions.sqlite`) in the suite's state namespace.
It appends every accepted result and every rejected claim; nothing is rewritten.

- **Unchanged.** The binding covers the governing inputs and every source
  fingerprint (identity, authority, author and text). An unchanged binding
  reuses the retained result with no session, also in a fresh session.
- **Changed sources.** An edited, new or removed source makes the decisions that
  cite it, the contradictions over them and every unresolved contradiction the
  subjects of one session. The other decisions are retained verbatim.
- **Changed governing inputs.** A changed task, prompt, option other than the
  heading, requirement, prohibition, account list or agent refreshes the whole
  reconciliation, with the previous result as history.
- **Claims.** The claim binds the request and the binding. A lost session
  response reconciles to the original result. A rejected result is retained and
  never redispatched; the next attempt has a fresh claim, up to
  `1 + retries_per_task` attempts per binding.

## Publication

`draft` changes only the bounded section of the PR body. The section is the one
heading that equals `section_heading`, up to the next heading of the same or a
higher level; several such headings block. CRLF bodies are matched without their
carriage returns; text outside the section keeps its original line endings, and
the section uses the body's own. Each settled decision that cites a public user
or maintainer source is rendered with its public source links and a stable
marker. Others are withheld, so a private request cannot reach the body through
a public question. An existing entry of a decision the user excluded or paused,
or one that a settled decision supersedes, is removed. Contested, unmanaged and other entries are kept verbatim, and nothing
outside the section changes.

`publish` uses the shared [exact-intent publication](publication.md) boundary
with an `edit_pr` intent. The plan from `publication_plan` enables `edit_pr`
only when the permission is effective, and `source_validator(ledger)` accepts
only the current retained result. Without that, `propose` returns a private
draft and nothing is written. The intent ID binds the logical run, the result
and the heading. A pending intent is only reconciled from the service, never
sent again; this also holds when the body already shows the effect.

## Verification

[`tests/test_decisions.py`](../tests/test_decisions.py) uses a disposable
repository and state directory, a disposable ledger per logical run, the
fail-closed fake engine of the configuration tests (durable gates,
claim-reconciled native sessions, scripted validator sessions) and the
fail-closed fake GitHub transport of the publication tests. Unscripted sessions
and unconfigured operations fail. Each case asserts transport calls, sessions,
gates or writes. It covers:

- **Preset.** Baseline digests and options; a default run with only a
  capability query; preset, permission, option, authorization and target
  expansion; `edit_pr` without permission or under a prohibition; a custom
  reconciliation prompt accepted and rejected by the shared validator; and a
  behavior replacement.
- **Reconciliation.** Distinct settled, proposed, contested and excluded
  decisions; a waiting gate with no session; a user answer that refreshes only
  the contested subjects; unchanged reuse in a fresh session; a later request
  that governs an earlier agreement; a later or since-edited user comment that
  outranks the request; an excluded agreement that a later maintainer comment, the unchanged
  request or an unrelated comment cannot restore; edited, removed and linked-issue sources;
  a changed requirement; lost session and gate responses; and unsupported
  results, which are never recorded.
- **Publication.** A private draft without authority; a section update that
  preserves the rest of the body, withholds decisions without a public user or
  maintainer source and removes superseded and paused entries; a CRLF body; contested entries kept verbatim; response loss before and
  after the effect with one write; a superseded result and an ambiguous section.
- **Composition.** A `dk-pr-fix` occurrence with the same report and handoff,
  and a plan without the adapter.

## What remains

- The procedure runs as an engine-managed native session only once Agent
  Workflow [#129](https://github.com/dk-code-factory/agent-workflow/issues/129)
  and [#130](https://github.com/dk-code-factory/agent-workflow/issues/130) land.
  The installed workflow does not yet hand decision reports to later tasks or
  route the section intent through its publication stage. No local controller
  or launcher stands in for either.
- Plugin skill entries (`SKILL.md` and descriptor rows) wait for that installed
  workflow ([packaging](packaging.md)). The inventory row stays
  `not_implemented`.
- Advised and selected alternatives (#27) are not yet a source kind.
- The helper checks recency and authority, not meaning. Real native-session
  judgment, a user-supplied skill (#16, Agent Workflow
  [#139](https://github.com/dk-code-factory/agent-workflow/issues/139)) and
  GitHub publication conformance need separate authorization.
