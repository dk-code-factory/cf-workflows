# cf-workflows

An independent successor to
[dk-workflows](https://github.com/dk-code-factory/dk-workflows), planned for Codex
and Claude on [Agent Workflow](https://github.com/dk-code-factory/agent-workflow).

Version `0.1.0-dev` establishes the suite's contracts and complete 29-skill
inventory. There is no runnable plugin, implemented admission or verified host
compatibility yet. Source pins are provenance, not a certified released engine.

- [Baselines and attribution](docs/baselines.md)
- [All 29 skills and their delivery obligations](docs/inventory.md)
- [Version 1 configuration, invocation, report and state contracts](docs/contracts/v1/README.md)
- [Baseline and cross-reference verification](docs/verification.md)
- [Implementation epic and ordered dependencies](https://github.com/dk-code-factory/cf-workflows/issues/10)

The suite keeps its own release identity and fresh configuration/state namespace.
YAML owns orchestration; focused Python operations own PR policy/evidence; the
engine owns scheduling, sessions, admitted assets and generic recovery. Remote
writes default off. Native skill execution, semantic configuration admission,
installed-resource execution and release qualification remain explicit delivery
obligations, with deterministic and separately authorized live evidence kept apart.
