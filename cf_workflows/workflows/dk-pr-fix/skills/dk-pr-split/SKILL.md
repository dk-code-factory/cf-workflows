---
name: dk-pr-split
description: Assess the scope of a GitHub pull request and move unrelated or related work into separate draft pull requests without dropping or rewriting any change, only when separately authorized.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

One read-only session with the admitted leaf role and no skills assesses every
changed path as core, unrelated or related work against the size threshold and
the configured modes. The helper accepts a split only when core work stays,
every moved path belongs to exactly one split of an enabled mode, no other
author's work moves, each split body links back to the source pull request,
and the source and split trees together hold every reviewed change. A
manual-mode split is only proposed. Unchanged inputs reuse the accepted result
without a session.

The local branches move only with an explicit local `rewrite` grant. The helper
then builds each split on the target and a source commit without the moved
work, runs the configured validation commands on every new commit in a private
checkout, and moves the refs, with a recovery ref, only when every commit
passed. With `create_pr` and `push` explicitly authorized and enabled, it
pushes each split branch and opens its draft pull request, and only then pushes
the source. A lost response is reconciled and never resent.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
