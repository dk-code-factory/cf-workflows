---
name: dk-repo-onboard
description: Discover a repository's instruction sources and report the rules, saved settings and gaps they establish, read-only and without saving anything.
---

This plugin version is not runnable. It needs a released engine of the exact
pinned version with engine-managed native sessions (agent-workflow#129) and
verified setup (agent-workflow#130). Do not copy the workflow into the target
repository, and do not run it with any other engine.

The target is a whole repository (`host/owner/name`) or one pull request; a
repository target has no pull request. The host-side procedure inventories
`AGENTS.md`, `CLAUDE.md`, contributing guides and agent instruction files, and
reads the saved configuration read-only. One session with the admitted leaf
role and no skills then reports every discovered source, the rules it states
(only sources that admission consumes govern; the others are evidence), the
documented facts, the effective saved settings and the conflicts and gaps. The
host accepts the report only when every rule and fact cites a line of an
inspected source or a saved configuration path.

Onboarding saves nothing: no settings, request history, gate or checkout
change, and it grants no execution or publication authority. It takes no
custom prompts. A `model` control may choose the session's model.

Never log in, refresh or copy credentials, use an engine found on PATH or grant
write authority.
