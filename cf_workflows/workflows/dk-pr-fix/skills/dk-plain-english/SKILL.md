---
name: dk-plain-english
description: Rewrite the prose a GitHub pull request changed in clear technical English, preserving meaning, code and exact wording, with the installed cf-workflows workflow.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

This entry is a preset over the shared `dk-pr-fix` workflow: one
`plain-english` finalizer and no other tasks or reviewers. One session with the
admitted leaf role and no skills edits only prose that the PR added or changed:
documentation outside code blocks, whole comment lines and Python docstrings.
Helpers reject edits outside that prose, changed code blocks, code spans, links,
identifiers, numbers, requirement keywords and machine directives, and changes
to wording the user quoted. An accepted pass becomes one local commit; nothing
is pushed or published. A pass without edits returns a sourced no-op. A rerun
reuses accepted work and refreshes only prose that changed since.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
