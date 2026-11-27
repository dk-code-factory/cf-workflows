---
name: dk-ci
description: Check CI for the current head of a GitHub pull request within a retained wait deadline, accepting only success, verified issue-backed flakes or verified configured exceptions.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

This entry observes only. It runs one CI stage of the shared workflow with no
task, finalizer or reviewer and no repair allowance, so no session starts. It
shares the PR's default logical run with `dk-pr-fix`: a direct and a composed
observation share one retained absolute deadline and one pending-only
extension, and a resume or a new head renews neither. Only current success,
verified issue-backed flakes of the exact failed attempt and verified
configured exceptions satisfy CI. Unavailable or unaffirmed evidence leaves a
check failed, and `no_checks` never satisfies CI.

The entry takes no authorization, and the installed helper rejects any
authorization, enabled permission, repair allowance or other stage before
observing. Reruns publish through `dk-pr-publish` (`rerun_ci`), and automatic
reruns of diagnosed unrelated failures belong to a `dk-pr-fix` run that holds
that grant.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
