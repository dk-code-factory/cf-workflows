---
name: dk-issue-alternatives
description: Compare whole-problem implementation approaches for a GitHub pull request's issue requirements, with the existing PR as one candidate, and settle a choice per questionable decision without editing or publishing.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

Each stage is one read-only native session with the admitted leaf role and no
skills: the shared assessment, an inventory of the whole problem from the issue
requirements, a comparison and a selection per questionable decision, then
their interactions. The helper checks every stage output before it records it:
cited requirement sources and files of the pinned head, rubric weights, helper
totals and the selection policy. A choice the policy cannot accept is asked
through one input gate in manual and semi-manual mode. Settled analysis is
reused across runs and heads; only a direct request for
`dk-pr-review-alternatives` compares settled decisions again.

The issue requirements come from `settings.alternatives.requirements`. A
custom prompt option is rejected; nothing is edited, committed or published.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
