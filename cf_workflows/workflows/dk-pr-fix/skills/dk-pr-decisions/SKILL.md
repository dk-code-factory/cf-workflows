---
name: dk-pr-decisions
description: Reconcile sourced decisions on a GitHub pull request, ask about unresolved contradictions, and publish only the bounded Decisions section when separately authorized.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

The run reconciles decisions from the PR body, comments, reviews and inline
comments, the exact request and the user's recorded answers. A read-only
session with the admitted leaf role and no skills returns the decisions; the
helper assigns each source's authority and accepts only sourced results.
Settled decisions, proposals, exclusions, pauses and contested decisions stay
apart. Only a user source at least as recent as every involved source resolves
a contradiction. Unresolved contradictions and blocking questions open one
input gate and block dependent work until a later reconciliation settles them.
Unchanged inputs reuse the retained result without a session.

Nothing is written unless `edit_pr` is enabled and explicitly authorized. Then
only the bounded Decisions section of the PR body changes, as one exact intent
with an independent audience check. No other action is accepted.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
