---
name: dk-pr-feedback-reply
description: Answer affected GitHub pull request feedback and resolve fully answered inline threads through the shared response stage of the installed cf-workflows dk-pr-fix workflow.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129) in a released engine of the
exact pinned version. Do not copy the workflow into the target repository, and
do not run it with any other engine.

The entry is a preset over the shared `dk-pr-fix` workflow: it runs the shared
response stage alone, with no finalizers or reviewers, in the same logical run
as `dk-pr-fix`, so a source answered by either run gets no second reply. One
responder session with the admitted leaf role and no skills drafts exact
replies with the default reply prompt; publication sends exact intents. Replies
and thread resolution each need an enabled permission and an explicit
authorization; `resolve_answered_threads: false` keeps threads open. A denied,
refused or unknown publication outcome stays incomplete and is never resent
blindly.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
