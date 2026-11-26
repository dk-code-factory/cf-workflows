---
name: dk-pr-iter
description: Iterate on a GitHub pull request by routing it, from verified account IDs, to fixing your own PR or to incoming review of another author's PR.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129) from a released engine. Do
not copy the workflow into the target repository, and do not run it with any
other engine.

The PR is yours when its author's immutable numeric account ID is the
authenticated account's, or a configured personal account on the PR's host.
Logins, remotes, commit authorship and write access never decide it. Your PR
runs `dk-pr-fix`; another author's PR runs `dk-pr-review`. An explicit request
to fix or review selects that route, and only an explicit fix request maintains
another author's PR. An analysis-only request always runs read-only review with
every remote write prohibited. Missing or inconsistent identity evidence blocks
instead of guessing.

The routed request keeps the exact original instructions and authorizations,
and routing adds none. Reviewing your own PR never approves it. Before either
entry runs, the workflow reads both identities again and stops when the token's
account or the PR author changed.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
