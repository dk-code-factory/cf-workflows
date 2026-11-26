---
name: dk-asd-ste100
description: Apply best-effort ASD-STE100 guidance to the prose a GitHub pull request changed, with explicit verification limits and no compliance claim, using the installed cf-workflows workflow.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

This entry is a preset over the shared `dk-pr-fix` workflow: one `asd-ste100`
finalizer and no other tasks or reviewers. It edits changed PR prose under the
same bounds as `dk-plain-english`: one session with the admitted leaf role and
no skills, edits only in changed prose, protected syntax and quoted wording
unchanged, and one local commit that is never pushed or published.

The installed workflow supplies no ASD-STE100 specification or dictionary, so
rule and dictionary checks do not run. Every report states that limit and that
full ASD-STE100 compliance is not claimed. A report that claims compliance,
conformance or certification, or cites a rule number, is rejected.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
