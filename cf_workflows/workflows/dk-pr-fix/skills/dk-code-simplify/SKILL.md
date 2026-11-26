---
name: dk-code-simplify
description: Review a GitHub pull request's changes for bounded, behavior-preserving simplifications with independent verification, without editing or publishing.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129) from a released engine. Do
not copy the workflow into the target repository, and do not run it with any
other engine.

This entry runs one bundled simplification reviewer in incoming review's
reviewer stage. `priority` (`change-first` or `code-first`) and
`allow_untouched_code` choose which one. Every variant reviews only the PR
changes and agreed scope; with untouched code allowed it may also name directly
related code that a concrete simplification of this PR needs. Broader
opportunities are deferred observations. The reviewer runs with the admitted
reviewer role and no skills, and a separate verifier session decides which
recommendations are confirmed.

Recommendations never make a clean review, and the run never edits, commits,
posts, approves, readies or merges. It accepts no authorization or permission.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
