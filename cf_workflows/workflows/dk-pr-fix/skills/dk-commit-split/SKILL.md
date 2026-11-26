---
name: dk-commit-split
description: Assess and organize all commits of a GitHub pull request into logical commits while keeping the reviewed final tree, authorship and source trailers, and rewrite or publish the series only when separately authorized.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

Each pass assesses every commit from the merge base in a read-only session with
the admitted leaf role and no skills. The session returns either no change or a
new series of path-level units. The helper accepts a series only when every
path of every rewritable commit is used once in its original order, each new
commit has one author and keeps exactly its source trailers, commits up to the
last one by another author stay as they are, and the rebuilt final tree equals
the reviewed tree. Unchanged inputs reuse the accepted result without a session.

The branch moves only with an explicit local `rewrite` grant. The helper then
builds the series, runs the configured validation commands on every new
commit in a private checkout, and moves the branch by compare-and-swap, with a
recovery ref, only when every commit passed. The next pass assesses the
rebuilt series, up to `max_passes`. The rewrite is pushed, with the original
head as the lease, only when `push` is explicitly authorized and
`rewrite_commits` is enabled.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
