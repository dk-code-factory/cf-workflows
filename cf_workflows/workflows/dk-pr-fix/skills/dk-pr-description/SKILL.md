---
name: dk-pr-description
description: Draft a GitHub pull request title and description from the actual diff, templates and conventions, and update the PR only when separately authorized.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

The run drafts the title and body from the PR's base...head diff and commits,
the current title and body, the repository guidance and the governing pull
request template (or the configured fallback sections). A read-only session
with the admitted leaf role and no skills returns the draft. The helper accepts
it only when every claim cites evidence, validation sections cite validation
evidence, references and code links are supported and pinned, quoted request
wording is kept, disabled fields are kept and the Decisions section is
unchanged. Unchanged inputs reuse the accepted draft without a session.

Nothing is written unless `edit_pr` is enabled and explicitly authorized. Then
only the drafted title and body fields that differ change, as one exact intent
with an independent audience check. A field edited after the draft is never
overwritten. No other action is accepted.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
