---
name: dk-pr-review-post
description: Post independently verified review findings on another person's GitHub pull request, or approve a complete clean review, through the shared guarded publication journal.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

This entry runs dk-pr-review's incoming review and publishes only
`post_findings` and `approve_pr`, each with its own explicit authorization; a
configured permission never supplies one. The installed helper rejects every
other action before any session or effect. A current review in the PR's
incoming-review run is reused without new reviewer or verifier sessions.
Confirmed findings are posted once: inline only on the changed line they name,
and otherwise in the review body with their actual location and evidence. A
partial review only comments, and posting findings never approves the PR. A
complete clean review of another author's PR is approved once with an empty
body, only under the separate `approve_pr` grant. Merge belongs to
`dk-pr-publish` and fork-CI approval to `dk-ci-approve`.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
