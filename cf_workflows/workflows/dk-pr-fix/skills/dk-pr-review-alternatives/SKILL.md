---
name: dk-pr-review-alternatives
description: Run a fresh, directly requested alternatives review of a GitHub pull request branch, then current alignment, independent verification of a mismatch and the alignment gate, and stop without ordinary review or readiness.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

Only a direct user invocation of this skill compares settled decisions again.
The staged request is that direct request: the inventory, comparisons,
selections and interactions run once under it, and replaying it repeats
nothing. The shared assessment is reused, and retained allowances are never
renewed. Current alignment and an independent verification of a mismatch run
next, and the run reports the alignment gate's outcome. It never claims a clean
review, approval or readiness, and nothing is edited or published.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
