---
name: dk-ci-approve
description: Approve waiting fork workflow runs of a GitHub pull request head after complete current incoming review, only under a separate explicit authorization.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

This entry runs `dk-pr-review`'s incoming review and publishes only
`approve_fork_ci`, under its own explicit authorization and the enabled
`approve_fork_ci` permission; a configured permission never supplies the
grant. The installed helper rejects every other action before any session or
effect. Approval needs complete current coverage, an independent verifier, no
confirmed or unverified finding, a reconciled non-draft head of another
author's PR and no unsettled publication. Each eligible run attempt (a
`pull_request` run of the exact head from the PR's fork and branch, waiting in
`action_required` and associated with no other PR) is approved once through
the shared publication journal. An uncertain approval is reconciled read-only
and never sent again. A deployment gate is never approved.

Finding publication, PR approval, merge authority and successful CI never
imply this approval, and it implies none of them. Merge belongs to
`dk-pr-publish`, which can carry a separate `approve_fork_ci` grant beside
`merge`.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
