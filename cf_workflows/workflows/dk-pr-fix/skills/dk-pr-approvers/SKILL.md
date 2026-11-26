---
name: dk-pr-approvers
description: Choose GitHub review recipients for a pull request head from a configured override or from repository evidence, without requesting reviews.
---

This plugin version is not runnable. It needs Agent Workflow to run an
installed package against a separate checkout (agent-workflow#130) and
engine-managed native sessions (agent-workflow#129) from a released engine. Do
not copy the workflow into the target repository, and do not run it with any
other engine.

With an exact override (`reviewers` and `team_reviewers`), the entry returns it
unchanged and reads nothing; two empty lists disable review requests. Without
one, helpers collect read-only evidence: the PR at the requested head, its
reviews, CODEOWNERS of the target revision, repository permissions and, only
when those leave no candidate, a bounded search of recent maintainers of the
changed paths. The PR author, bots, deleted accounts, reviewers with only
dismissed reviews and accounts that cannot approve are excluded with reasons.
One session with the admitted leaf role and no skills chooses among the
eligible candidates. Helpers accept only eligible candidates that cite their
own evidence, require the checkout unchanged and read the head again. A failed
or incomplete read returns blockers, never an empty selection.

The result requests no review and approves or merges nothing. Requesting the
selected reviewers belongs to `dk-pr-publish` with its own permission and
explicit authorization.

Check the same prerequisites as `dk-pr-fix` before a run and report each
missing one as guidance. Never log in, refresh or copy credentials, build or
pull images, use an engine found on PATH or grant write authority.
