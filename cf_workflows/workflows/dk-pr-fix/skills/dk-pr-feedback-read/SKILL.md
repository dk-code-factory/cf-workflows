---
name: dk-pr-feedback-read
description: Read every inline thread, review body and general comment of one GitHub pull request into stable, separately actionable requests with the installed cf-workflows dk-pr-fix workflow.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129) in a released engine of the
exact pinned version. Do not copy the workflow into the target repository, and
do not run it with any other engine.

The entry is a preset over the shared `dk-pr-fix` workflow: it runs only the
`read-feedback` task, with no finalizers or reviewers, in the same logical run
as `dk-pr-fix`. A reader session with the admitted leaf role and no skills
captures every page of the discussion, assesses every current source with its
stable identity and keeps every response obligation. It decides no
implementation and performs no remote write.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
