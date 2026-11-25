---
name: dk-pr-fix
description: Maintain one GitHub pull request through bounded fix, independent review and CI cycles with the installed cf-workflows dk-pr-fix workflow.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and a
released engine of the exact pinned version with verified setup. Do not copy
the workflow into the target repository, and do not run it with any other engine.

Check the prerequisites before a run and report each missing one as guidance:

- A self-contained clone with a real `.git` directory: not a linked worktree,
  no object alternates, not shallow.
- A repository-local `user.name` and `user.email` when the plan writes.
- Git, Python 3.11 or newer and the configured container runtime on the host.
- The digest-pinned helper image from `agents.yml`, already present locally,
  containing Python 3.11 or newer, Git, `gh` and `sleep`.
- A `controller.run-timeout` of at least `2h` and at least the CI window.

Never log in, refresh or copy credentials, build or pull images, use an engine
found on PATH or grant write authority. Remote writes stay disabled in this
release. The private request holds the exact PR URL, the original instructions
and an optional logical run. It is staged outside the target checkout.
