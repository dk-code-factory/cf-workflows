# PR descriptions, commit messages and issues

`cf-workflows:dk-pr-description`, `cf-workflows:dk-commit-message` and
`cf-workflows:dk-issue` (#32) are thin entries over the shared workflow.
[`cf_workflows/drafting.py`](../cf_workflows/drafting.py) holds their presets
and adapter procedure. Like the other library operations, it is not wired into
an installed workflow yet (see [What remains](#what-remains)).

## Entries

| Entry | Preset tasks | Adapter | Target | Accepted authorizations |
| --- | --- | --- | --- | --- |
| `dk-pr-description` | `describe-pr`, then `{stage: publication}` | baseline `describe-pr` | PR | `edit_pr` |
| `dk-commit-message` | `draft-commit-message` | bundled entry adapter | PR or repository | none |
| `dk-issue` | `draft-issue`, then `{stage: publication}` | bundled entry adapter | PR or repository | `create_issue`, `edit_issue` |

Each entry has three parts:

- A preset selection with explicitly empty `finalizers` and `reviewers`.
- The adapter procedure that the task runs: the admitted `leaf` role, the
  bundled read-only assignment and the helper operations `collect`, `draft`,
  `propose` and `publish`.
- The original role prompt and options from the pinned baseline. `config.json`
  and `prompts/default.md` are kept byte for byte; their digests match
  [the inventory](../baselines/inventory.json).

| Entry | Options (baseline defaults) |
| --- | --- |
| `dk-pr-description` | `template` (`null`), `update_title` (`true`), `update_body` (`true`), `fallback_sections` (`Issue`, `Fix`) |
| `dk-commit-message` | `subject_target_length` (72), `body_wrap_length` (72), `max_line_length` (80) |
| `dk-issue` | `template` (`null`), `labels` (`[]`), `assignees` (`[]`), `fallback_sections` (`Problem`, `Expected behavior`) |

A default run is admitted from bundled control text. It runs no validator
session and loads no skill. An invocation may add only `prompts`. Tasks,
finalizers, reviewers, permissions, unknown options, other authorizations and a
repository target for a description reject before the engine is contacted. An
accepted authorization also needs the configured permission.

The installed dk-pr-fix rejects `draft-commit-message` and `draft-issue` tasks,
because its graph would run them as generic tasks without these checks. It also
rejects issue authorizations, because it declares no issue input.

## Evidence

`collect` is read-only. It never changes Git, the index or the remote:

- **Description.** The PR's base and head must be present in the checkout.
  Evidence is the `base...head` diff, the PR commits, the current title and
  body, captured linked issues and the bounded Decisions section of the body.
- **Commit message.** Git runs with optional locks, filesystem monitors,
  external diffs and lazy fetches disabled. Evidence is HEAD, the staged and
  unstaged patches, untracked paths, the configured Git identity, recent human
  commits, a commit template inside the checkout and verified commit-policy
  text from commit cleanup. Unresolved conflicts or no changes block.
- **Issue.** Evidence is the exact ask, verified facts and, for an update, the
  current issue. Labels and assignees are verified against the repository
  listings. Requested ones take precedence over the configured defaults. A
  missing requested name blocks before any session. A missing configured name
  is left out with a note. An update changes labels and assignees only when
  they are requested.
- **All entries.** Every entry also gets the repository guidance and the
  governing templates. A configured `template` must exist. Otherwise the entry
  discovers `pull_request_template.md` (repository root, `.github/` or
  `docs/`) or `.github/PULL_REQUEST_TEMPLATE/*.md` for a description. For an
  issue it discovers `issue_template.md` or the Markdown templates and issue
  forms in `.github/ISSUE_TEMPLATE/`. An issue form's body labels are its
  sections. Callers supply facts as `{id, kind, text, url?}` with kind `fact`,
  `validation` or `reproduction`.

## Drafting

`draft` runs at most one engine-managed native session per call, through the
claim of its binding. The session gets the exact request, the role prompt, the
options, the evidence and the exact wording constraints. It uses read-only
tools, loads no skill and may not write or publish. The helper accepts a draft
only when all of these hold:

- Every claim cites known sources. A validation or testing section, or a
  `Tested:` line, cites a `validation` fact. A reproduction section cites a
  `reproduction` fact. Sections that are already in the current body belong
  to its author.
- Every `#N` and issue or PR URL is named by the evidence. A closing keyword
  names a linked issue. Code links are pinned to a full commit SHA.
- Public text has no `path:line` reference, no template comment and no
  generated-by banner or tool attribution.
- The draft follows the governing template, or the fallback sections when no
  template governs, in order. A left-out section is reported with a reason.
  When several templates exist, the draft names the one it follows.
- Every phrase that the request quotes appears verbatim.
- A disabled `update_title` or `update_body` field is not drafted, and the
  current value is kept. The current Decisions section is kept verbatim, and a
  description never adds one: `dk-pr-decisions` owns it.
- A commit message accounts for every changed path as included or excluded
  with a reason, and it names no other path. It applies the length fallbacks
  only without commit-policy text. It has no `#` lines. Its `Signed-off-by`
  names the configured Git identity, and every co-author is named by the
  evidence.

The ledger is a versioned SQLite file per logical run
(`runs/<sha256(logical_run)>.drafting.sqlite`) in the suite's state namespace.
It appends accepted drafts and rejected claims and never rewrites them.

- **Unchanged inputs** reuse the retained draft with no session, also in a
  fresh session. When the current title and body equal the retained draft
  because it was published, the draft is still current.
- **A lost session response** reconciles through the same claim.
- **A rejected draft** is retained and never redispatched. The next attempt
  gets a fresh claim, up to `1 + retries_per_task` attempts per binding.

## Publication

`propose` turns an accepted draft into one exact proposal. `publish` sends it
through the shared [exact-intent publication](publication.md) boundary with
`source_validator(ledger, name)`, which accepts only the current retained draft.
Without an effective permission, `propose` returns a private draft and nothing
is written.

- **Description.** One `edit_pr` with only the enabled fields that differ from
  the current PR. It is bound to the current head and target.
- **Issue.** One `create_issue` with the title, body and verified labels and
  assignees. With an issue number it is one `edit_issue` instead.
- **Commit message.** Never published or committed. `handoff(report)` gives
  the message and scope to the committing procedure that consumes it.

A draft records the fields it may write and the title and body it saw. A
description writes only its enabled fields that differ, so a disabled field is
never reverted. When a field the draft would write changed after the draft,
`propose` returns `stale` and nothing is prepared. After preparation, the
intent's baseline must also equal what the draft saw; otherwise `publish`
returns `stale` and the intent is never dispatched. Drafting again reads the
new text as evidence. After dispatch begins, the shared publisher refuses any
field changed since preparation.

The intent ID binds the logical run and the draft. A pending intent is only
reconciled from the service and never sent again. An applied one returns its
receipt with no write. Issue kinds share the journal, lock, audience and
receipts of every PR kind, so a PR run that both describes its PR and files a
follow-up issue keeps both intents in one journal. See
[issue kinds](publication.md#kinds-and-authority).

## Direct and composed use

`draft` accepts the entry's own preflight or any admitted shared plan that
selects the adapter exactly once, and runs the same procedure. The default
dk-pr-fix plan already selects `describe-pr`. A composed caller may pass the
entry options, checked against the same schema. A plan without the adapter
rejects.

## Verification

[`tests/test_drafting.py`](../tests/test_drafting.py) uses disposable
repositories with and without PR templates, issue templates and issue forms. It
also uses a disposable drafting and run ledger per logical run, the fail-closed
fake engine of the configuration tests and the fail-closed fake GitHub of the
publication tests, with issue, label and assignee routes. Unscripted sessions
and unconfigured operations fail. Each case asserts sessions, transport calls or
writes. It covers:

- **Presets.** Baseline digests, presets, default runs with only a capability
  query, rejection of expanded presets, options and authorizations with zero
  engine calls, permissions, and the installed dk-pr-fix refusals.
- **Description.** A private fallback draft and unchanged reuse, template
  sections, fabricated and supported validation, disabled title, quoted
  wording, the kept Decisions section, an authorized edit with response loss
  before and after the effect and one write, reuse after publication, edits
  after the draft (stale, no write), a disabled title changed after the draft
  (kept), and a dk-pr-fix occurrence.
- **Unsupported claims.** Unknown and closing references, unpinned links,
  `path:line`, banners, template comments and unknown sources reject. The
  author's existing sections are kept.
- **Commit message.** Index, HEAD, refs and status are unchanged. Every path is
  accounted for. The cases also cover subject length, policy text, tool
  co-authors, signers, testing claims, unknown references, a lost session
  response and a composed occurrence.
- **Issue.** Verified, dropped and missing labels and assignees with listing
  counts, no live fallback, issue forms and reproduction evidence, creation with
  response loss before and after the effect, an identical existing issue, a
  label removed after preparation, an update with a concurrent edit, edits
  between the draft and preparation or inside preparation (stale, never
  dispatched, then re-drafted and published once), and one
  journal for a composed PR run. At unit level it covers issue payloads,
  permissions, repository plans and duplicate receipts.

## What remains

- The procedure runs as an engine-managed native session only once Agent
  Workflow [#129](https://github.com/dk-code-factory/agent-workflow/issues/129)
  and [#130](https://github.com/dk-code-factory/agent-workflow/issues/130) land.
  The installed workflow does not yet run these procedures or route their
  intents through its publication stage. No local controller or launcher
  stands in.
- Plugin skill entries (`SKILL.md` and descriptor rows) wait for that installed
  workflow ([packaging](packaging.md)). All three inventory rows stay
  `not_implemented`.
- dk-commit-message drafts only. The baseline's "commit only when requested"
  is [`splitting.commit`](splitting.md#commit-on-request) (#34). It commits a
  draft only while `current_report` shows the draft is still current.
- The helper checks citations, references, sections, wording and metadata, not
  meaning. Real native-session judgment, user-supplied skills (#16, Agent
  Workflow [#139](https://github.com/dk-code-factory/agent-workflow/issues/139))
  and GitHub publication conformance need separate authorization.
