---
name: dk-pr-review
description: Review another person's GitHub pull request with engine-managed reviewers and independent finding verification, without fixing it.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

Incoming review reads the PR and never edits, commits, pushes or replies.
Configured reviewers and the simplification reviewer run in separate sessions
with the admitted reviewer role and no skills; a separate verifier session
checks every finding. A missing reviewer or verification is never a clean
review. Finding publication, approval and fork-CI approval are not available in
this release; merge keeps its own explicit grant and current readiness.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
