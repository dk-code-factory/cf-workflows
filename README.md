# cf-workflows

An independent successor to
[dk-workflows](https://github.com/dk-code-factory/dk-workflows), planned for Codex
and Claude on [Agent Workflow](https://github.com/dk-code-factory/agent-workflow).

Version `0.1.0-dev` establishes the suite's contracts and complete 29-skill
inventory and implements [structural configuration admission](docs/admission.md),
[semantic assessment of custom inputs](docs/semantic.md) and
[acceptance reuse with configuration-write recovery](docs/acceptance.md) against
engine interfaces. It also extracts the portable PR-fix example into the
[installed `cf-workflows:dk-pr-fix` workflow](docs/dk-pr-fix.md), with preflight
and an external state namespace, and keeps its budgets, deadlines, occurrence
identities and explicit continuations in the logical run's ledger.
[Incoming review](docs/review.md) (`cf-workflows:dk-pr-review`) is a review-mode
preset over that workflow that can publish its independently verified findings
inline or in the review body, and `cf-workflows:dk-pr-iter` routing picks fixing or
review from immutable account IDs. One
[plugin descriptor](docs/packaging.md) declares the Codex and Claude package
inputs, with a checked resource closure and read-only runtime prerequisite
checks. There is no runnable plugin, built package or verified host
compatibility yet: running the installed workflow needs Agent Workflow #129,
#130 and the #139 discovery-isolation work, and packaging needs #131–#133 and a
released engine. Source pins are provenance, not a certified released engine.

- [Baselines and attribution](docs/baselines.md)
- [All 29 skills and their delivery obligations](docs/inventory.md)
- [Version 1 configuration, invocation, report and state contracts](docs/contracts/v1/README.md)
- [Installed dk-pr-fix workflow, preflight and state namespace](docs/dk-pr-fix.md)
- [Plugin descriptor, resource closure and runtime prerequisites](docs/packaging.md)
- [Feedback implementation, response obligations and thread resolution](docs/feedback.md)
- [Feedback replies settled before independent review](docs/responses.md)
- [Supplied-skill bindings and engine-observed evidence](docs/skills.md)
- [Exact-intent publication and uncertain-effect reconciliation](docs/publication.md)
- [Provisional analysis while PR metadata reconciles](docs/provisional.md)
- [Current alignment over settled architectural analysis](docs/alignment.md)
- [Incoming review and immutable-identity author routing](docs/review.md)
- [Configuration, preferences and repository onboarding entries](docs/configuration-skills.md)
- [Sourced PR decisions and reconciliation](docs/decisions.md)
- [Baseline and cross-reference verification](docs/verification.md)
- [Implementation epic and ordered dependencies](https://github.com/dk-code-factory/cf-workflows/issues/10)

The suite keeps its own release identity and fresh configuration/state namespace.
YAML owns orchestration; focused Python operations own PR policy/evidence; the
engine owns scheduling, sessions, admitted assets and generic recovery. Remote
writes default off. Native skill execution, semantic configuration admission,
installed-resource execution and release qualification remain explicit delivery
obligations, with deterministic and separately authorized live evidence kept apart.
