---
name: dk-alternatives-review
description: Compatibility entry for dk-issue-alternatives and dk-pr-alternatives on a GitHub pull request; runs the explicitly routed procedure under this entry's identity and does not import retained dk-workflows assignments.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

The route is explicit: `settings.alternatives.route` is `whole-problem`
(the `issue-alternatives` adapter) or `branch` (`pr-alternatives`). It is
never inferred from the request. The run keeps this entry's identity, the exact
original instruction and both role prompts, and reuses the delegate's settled
analysis of the PR. It cannot authorize a fresh comparison. A retained
dk-workflows assignment is reported as unsupported before any session.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
