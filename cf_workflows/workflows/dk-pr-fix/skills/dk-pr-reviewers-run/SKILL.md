---
name: dk-pr-reviewers-run
description: Run a selected, ordered set of GitHub pull request reviewers with independent finding verification, without fixing, posting or approving.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129) from a released engine. Do
not copy the workflow into the target repository, and do not run it with any
other engine.

This entry is incoming review's reviewer stage alone: one reviewer stage and no
other tasks or finalizers. The reviewer selection is required and keeps its
order, explicit parallel groups, repeated occurrences and optional reviewers.
`execution: sequential` or `max_parallel` only caps how many reviewers in a
group run at once; it never drops, merges or reorders one. Each required
occurrence runs in its own session with the admitted reviewer role and no
skills, and a separate verifier session checks every finding. An optional
reviewer that fails leaves the others' original reports as they were.
Per-occurrence provider options, model controls and user-supplied reviewer
skills are rejected.

The run is read-only. It accepts no authorization or permission, and its result
never approves, readies, posts or merges anything. It shares incoming review's
run, so a later `dk-pr-review` reuses every current reviewer and verification.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
