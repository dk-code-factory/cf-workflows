---
name: dk-pr-publish
description: Publish exactly authorized changes to one GitHub pull request, or merge it after current independent review, through the shared guarded publication journal.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

Every action needs its own explicit authorization in the private request; a
configured permission never supplies one. Before any session or effect, the
installed helper routes the request to one preset or rejects it:

- `push`, `edit_pr`, `reply`, `resolve_thread`, `request_review`, `create_pr`
  and `rerun_ci` run the changes preset. A read-only session proposes the
  exact operations, and the shared publication stage prepares exact intents,
  obtains an independent audience verdict, rechecks live state and reconciles
  uncertain effects.
- `merge` alone runs incoming review and the shared merge readiness: current
  independent review of the exact head, successful exact-head CI and no
  pending review or review request. It cannot be combined with other actions.
- Finding publication and approval belong to `dk-pr-review-post`, fork-CI
  approval to `dk-ci-approve` and issue writes to `dk-issue`; they reject here.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
