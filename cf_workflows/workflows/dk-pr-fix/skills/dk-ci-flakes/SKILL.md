---
name: dk-ci-flakes
description: Scan a GitHub pull request repository's recent failed CI runs and open issues for flaky-test evidence, linking a test only to an exact verified issue declaration, without waiving any failure.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129). Do not copy the workflow
into the target repository, and do not run it with any other engine.

Helpers collect read-only evidence for the PR's repository within the
resolved `lookback_days`, `max_runs` and `max_issues`, or an exact `targets`
narrowing: failed Actions runs, the failed jobs of each run's exact attempt
and their failed-step logs, and open issues with explicit `dk-flaky`
declarations. A test context is linked only when an open issue declares
exactly its workflow, job, test and signature and the signature occurs in its
logs; otherwise it is a candidate. One session with the admitted leaf role and
no skills writes the report, and helpers hold it to that evidence: it cannot
link, unlink, invent or drop a context. Limits and gaps leave the result
uncovered, and a failed read is a blocker, never an empty result.

The result waives no failure and grants no authority. CI acceptance verifies
each failed attempt again. Creating issues, rerunning workflows and changing
exceptions need their own entries and authorizations.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
