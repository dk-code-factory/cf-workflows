---
name: dk-commit-message
description: Draft a commit message for the uncommitted changes of a GitHub pull request checkout from the diff, repository conventions and recent human commits, without committing.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

The run drafts one message for the checkout's staged, unstaged and untracked
changes, using the repository guidance, the commit template inside the
checkout and recent human commits. A read-only session with the admitted leaf
role and no skills returns the message and its scope. The helper accepts it
only when every changed path is included or excluded with a reason, every
claim cites evidence, the length fallbacks hold, and signers and co-authors
are ones the evidence names. Unchanged inputs reuse the accepted draft without
a session.

The run never stages, commits or publishes, and it accepts no authorization.
Committing the message belongs to a committing procedure.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
