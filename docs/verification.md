# Contracts and inventory verification

Run the standard-library-only verifier from this repository with local source
clones that contain the exact pinned objects:

```sh
python3 scripts/check_baselines.py \
  --dk-workflows /path/to/dk-workflows \
  --agent-workflow /path/to/agent-workflow
python3 -B -m cf_workflows.package
python3 -m unittest discover -s tests -v
```

If necessary, fetch the exact commits into those source clones separately; the
verifier never fetches, checks out, executes or mutates upstream source. It checks
the exact two host manifests and skill directory sets, all 29 distinct inventory
rows and required fields, entry/direct-reference paths and SHA-256 hashes,
portable source existence, exact copied license bytes and local Markdown file
targets. It does not validate remote URLs, Markdown anchors, transitive runtime
resource closure or semantic correctness of the descriptions. Review the latter
against the pinned entries and [contracts](contracts/v1/README.md).
The regression test verifies that missing or extra host-manifest entries reject
before any source-object reads, instead of silently checking a partial host set.
The checker also verifies the [dk-pr-fix extraction](dk-pr-fix.md). Every file of
the pinned `examples/pr-fix/` must be copied or excluded with a reason. Each copied
file must match its recorded upstream digest. `verbatim` files must be identical,
and the YAML roles must equal the canonical conversion of the pinned TOML.
Each consumed upstream correction must come from a descendant of the pin, match
its recorded digest there, and keep its listed definitions identical to that
commit's.

[dk-pr-fix tests](dk-pr-fix.md#verification) (`tests/test_dk_pr_fix.py`, plus the
ported example tests in `tests/dk_pr_fix/`) use disposable repositories, a fresh
state namespace per test, a fail-closed fake engine transport and a fail-closed
fake GitHub transport. They assert invocation counts and run the installed helpers
from a relocated read-only copy against a separate checkout.
`tests/test_dk_pr_fix_continuation.py` counts native dispatches while it replaces
runs, deletes checkpoints and changes configuration, and checks the
[retained budgets, occurrences and continuation](dk-pr-fix.md#budgets-occurrences-and-continuation).
`tests/test_dk_pr_fix_publication.py` injects response loss before and after the
effect of every supported write kind and counts dispatches, as described in
[exact-intent publication](publication.md#verification).
`tests/test_dk_pr_fix_pending_review.py` ports the dk-workflows#260
[pending-review](publication.md#pending-reviews) scenarios on the same
fail-closed transport and counts review submissions.

`tests/test_dk_pr_fix_provisional.py` covers
[provisional analysis](provisional.md#verification) under lagging PR metadata,
with native-dispatch and transport-call counts.
[Current alignment](alignment.md#verification) tests (`tests/test_alignment.py`)
cover dk-workflows#239 and #241 with a disposable repository and ledger and a
fake native-session transport whose unscripted dispatches fail. They count
dispatches per stage, so no settled comparison repeats, and check partial
COMMENT eligibility separately from the alignment gate.
[Structural admission](admission.md) tests (`tests/test_admission.py`) use
disposable Git repositories and resource roots plus a fake engine transport. Any
operation other than the configured `capabilities()` query fails without a live
fallback. Every rejection asserts zero capability queries when it comes from a
local structural check and zero skill, session or publication operations in all
cases. [Semantic assessment](semantic.md) tests (`tests/test_semantic.py`) script
every verdict through fake validator sessions and a durable fake gate store that
outlives helper restarts. They cover interrupted clarification, changed and
revised proposals, attempted prohibition waivers, validator attempts to run or
publish, and a default plan with zero validator sessions. Each asserts its
capability, gate and session counts. [Acceptance reuse](acceptance.md) tests
(`tests/test_acceptance.py`) add a disposable SQLite ledger and the real file
store with injected faults. They change each relevant input in turn and assert
which units are assessed and which are reused with their original evidence, and
that unrelated changes and fresh sessions run no session or gate read. They cover
clarified acceptances, lost gate responses, lost write responses, interruption
before and after a write, another writer, compare-and-swap rejection, a
preserved earlier sibling, backup and restore, and refusal of foreign state.
[Feedback obligation](feedback.md) tests (`tests/test_feedback.py`) cover
dk-workflows#244 with a disposable ledger per test and a fake GitHub transport
whose only operation is a counted `capture()`: rejected bot and style
suggestions, mixed threads, questions for other participants, already answered
and optional sources, edited sources, deleted replies and denied permissions.
[Packaging](packaging.md) tests (`tests/test_package.py`,
`tests/test_prerequisites.py`) check the plugin descriptor and declared resource
closure on read-only relocated copies under paths with spaces, and the runtime
prerequisites against disposable clones with a fake host whose unconfigured
commands fail. `python3 -B -m cf_workflows.package` checks the source plugin
root and prints its inventory status; it builds no package.
[Response stage](responses.md) tests (`tests/test_dk_pr_fix_responses.py`) cover
dk-workflows#255 with the installed helpers and a fake GitHub transport that
counts captures and writes: replies settled before a failing review and kept on
resume, reply response loss, changed comments, disabled resolution, denied
reply authority and custom or empty plans.
[Incoming review and routing](review.md#verification) tests
(`tests/test_dk_pr_review.py`, `tests/test_routing.py`) drive the installed
helpers in review mode and the router with fail-closed fake GitHub and engine
transports. They count native dispatches and identity reads, and cover
dk-workflows#254 report-only pass claims.
[Finding publication](review.md#finding-publication) tests
(`tests/test_dk_pr_review_findings.py`) cover dk-workflows#240 body-only and
mixed findings with summaries disabled, partial `COMMENT` reviews, unchanged
reruns, uncertain submissions, existing pending drafts and conclusive
rejection through the shared publication journal, counting review writes and
native dispatches on a fail-closed fake GitHub.
[Supplied-skill](skills.md) tests (`tests/test_skills.py`) build session bindings
from admitted plans and decide coverage from records of fake Codex and Claude
gateways. They cover missing, duplicate, undeclared and nested children, digest
mismatch, same-named and host skills, report-only claims, resumed and checklist
sessions without the skill, interrupted and failed writers, and occurrence IDs
equal to the installed helper's.
[Configuration entry](configuration-skills.md#verification) tests
(`tests/test_configuration_skills.py`) drive dk-repo-onboard, dk-config and
dk-preferences with a disposable repository, the real configuration store and
ledger, and a fake engine with durable gates and claim-reconciled native sessions.
They count sessions, gates and writes across clarification, lost responses and
concurrent writers.
[Decision](decisions.md#verification) tests (`tests/test_decisions.py`) drive
dk-pr-decisions directly and from a dk-pr-fix plan with the same fake engine and
the fail-closed fake GitHub transport. They count sessions, gates and writes across
conflicting and later instructions, changed sources, clarification, unchanged
reuse and uncertain section publication.
[Prose cleanup](prose.md#verification) tests (`tests/test_prose.py`) drive
dk-plain-english and dk-asd-ste100 with a disposable repository, the real
acceptance and prose ledgers, and a fake engine whose claim-reconciled sessions
edit the checkout. They count sessions across rejected edits, lost responses,
interrupted commits and selective source, preference and reference refreshes.
[Alternatives entry](alternatives.md#verification) tests
(`tests/test_alternatives.py`) drive the four alternatives entries with a
disposable repository and state namespace and a fake engine with durable gates
and claim-reconciled native sessions scripted per stage. They count new sessions
per stage across settled reuse, fresh direct requests, interruptions and
exhausted allowances.
[Feedback entry](feedback-skills.md#verification) tests
(`tests/test_feedback_skills.py`) drive dk-pr-feedback-read, -address and -reply
from their preflight inputs through the installed helpers. They use a paginated
fake behind the real gh capture and fail-closed capture and publication fakes,
and count sessions, pages, commits and writes across resume and composed runs.
[Reviewer entry](reviewer-skills.md#verification) tests
(`tests/test_reviewer_skills.py`) drive dk-pr-reviewers-run and dk-code-simplify
through the installed helpers at capacity one and uncapped, and dk-pr-approvers
with a disposable repository, a fake GitHub transport and a fake engine. They
count native dispatches, sessions and GitHub reads.
[Publication entry](publication-skills.md#verification) tests
(`tests/test_publication_skills.py`) drive dk-pr-publish and dk-pr-review-post
directly and from dk-pr-fix and dk-pr-review plans on a fail-closed fake GitHub.
They count review, edit, request and merge writes and native dispatches across
denial, payload drift, pending drafts, body-only findings, approval and lost
effects.
[Drafting entry](drafting.md#verification) tests (`tests/test_drafting.py`)
drive dk-pr-description, dk-commit-message and dk-issue directly and from
shared plans, with repositories with and without templates. They use the fake
engine and a fail-closed fake GitHub with issue, label and assignee routes. They
count sessions, listings and writes across fabricated claims, unchanged Git
state, metadata validation and uncertain issue creation and PR edits.
These tests do not certify any engine, provider or host. Future runtime
issues must use disposable repositories and explicit fake
GitHub/provider/native-host/configuration-agent transports. Unknown fixture
operations must fail without falling back to network/model execution. Assert
invocation counts for changed-input selective reuse, capacity-one execution,
claim/result loss, interruptions, concurrent writers, uncertain publication and
exhausted budgets. Keep original outputs/claims/effects and absolute deadlines.

The [inventory](inventory.md) is a list of obligations, not a passing acceptance
matrix. Each row eventually needs separate direct and composed scenarios,
recovery/permission-denial cases and actual supported-host capability evidence.
All nine inherited defect mappings in the suite tracker remain required. Real
native-host adherence, live providers, GitHub publication conformance and release
certification stay separate and pending until explicitly authorized and verified.
