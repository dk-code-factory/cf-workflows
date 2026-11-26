---
name: dk-pr-feedback-address
description: Verify and address actionable feedback on one GitHub pull request with focused, locally committed fixes and per-request dispositions, with the installed cf-workflows dk-pr-fix workflow.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129) in a released engine of the
exact pinned version. Do not copy the workflow into the target repository, and
do not run it with any other engine.

The entry is a preset over the shared `dk-pr-fix` workflow: it runs
`read-feedback`, `reconcile-decisions` and `address-feedback`, with no
finalizers or reviewers, in the same logical run as `dk-pr-fix`. Settled
decisions, deliberate deferrals and response obligations are kept, and a
retained fix is never implemented twice. A fixed decision cites the paths it
changed. Configured `additional_validation_commands` reach the address session
as exact argument arrays; a report that changes files must show every one
passing. They never launch Codex or Claude Code. The fix is committed locally
only; pushing it and replying belong to `dk-pr-fix`, `dk-pr-publish` and
`dk-pr-feedback-reply`.

Check the same prerequisites as `dk-pr-fix` before a run, including the
repository-local commit identity, and report each missing one as guidance.
Never log in, refresh or copy credentials, build or pull images, use an engine
found on PATH or grant write authority.
