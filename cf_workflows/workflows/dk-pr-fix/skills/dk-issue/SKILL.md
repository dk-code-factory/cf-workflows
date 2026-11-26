---
name: dk-issue
description: Draft a GitHub issue from the exact request, repository templates and verified labels and assignees, and create or update it only when separately authorized.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

The run drafts one issue in the repository of the selected pull request from
the exact request, the repository guidance and the governing issue template or
issue form (or the configured fallback sections). Requested labels and
assignees must exist in the repository; configured ones that do not are left
out with a note. A read-only session with the admitted leaf role and no skills
returns the draft. The helper accepts it only when every claim cites evidence,
reproduction and validation sections cite evidence of that kind, references
are supported and quoted request wording is kept.

Nothing is written unless `create_issue` (or `edit_issue` for an update) is
enabled and explicitly authorized. Then one exact intent creates or edits the
issue, with an independent audience check. An identical existing issue settles
the creation without a write, and an uncertain creation is reconciled, never
repeated. No other action is accepted.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
