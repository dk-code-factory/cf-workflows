# Structural admission

[`cf_workflows/admission.py`](../cf_workflows/admission.py) implements structural
admission and complete input resolution for the
[configuration contract](contracts/v1/configuration.md) on the portable PR-fix
model. It is a library operation for the future preparation helper (#14); no
installed workflow calls it yet.

## Inputs

`admit()` takes the private request (PR URL, exact instruction, optional
`logical_run`, optional `upstream`, authorizations), the saved configuration
document or `null`, invocation-local inputs, deployment configuration (the parsed
`agents.yml` shape), the admitted role catalog, named resource roots and an
engine transport. The `repository` root is the target checkout.

Effective options start from the declared workflow-input defaults copied from the
pinned portable example, then apply matching personal, repository, upstream and
PR scopes and finally the invocation-local inputs. Every value keeps its source.
`requirements` and `prohibitions` accumulate and are never replaced. Differing
values from two scopes at the same precedence reject. An explicit empty plan
stays empty; `null` is rejected because no declared option gives it a meaning.
Every scope is validated in full, including values that a later scope overrides
and scopes that do not match this target. Selector IDs must be canonical
(lowercase `host/owner/name` or a canonical PR URL), so a scope never silently
fails to apply because of case or formatting. `accounts` hold account logins only.

## Resolution

Admission reads and hashes these inputs:

- Configured resources: normalized relative paths below a named root, with the
  declared SHA-256 and a complete local Markdown link closure.
- Repository guidance: `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` and
  `.github/copilot-instructions.md`, with their transitive local links. Guidance
  may use symlinks that stay inside the repository; each is recorded with its
  resolved path. A symlink that leaves the repository rejects.
- The admitted role catalog: exactly `leaf`, `reviewer`, `verifier`, `fixer` and
  `audience`, with each digest checked.
- Saved and invocation-local prompts and preferences, with their resource references.
- Ordered task and stage selections, including parallel groups and nested
  reviewer groups.

A user-supplied skill is a directory. Its file list and per-file digests must
match exactly, and its content digest is the SHA-256 of the canonical JSON `{path: sha256}`.
Admission hashes the files and checks Markdown links from the entry. It never
loads or runs the skill. Host-installed skill fields reject.

Link closure covers inline links (including balanced parentheses), reference
definitions (not footnotes) and `<...>` targets. It ignores fenced code and code
spans but scans indented lines, so links in nested lists are never dropped. It
resolves a leading `/` against the root. Configured resources, prompt text and their bytes are read once,
so a prompt's text is exactly the content whose digest was verified.

Missing or escaping paths, symlinks outside guidance, digest mismatches, incomplete closures,
legacy qualified selectors, per-task options, cycle hooks, path selectors,
string `settings`, unknown fields or options and wrong types all reject before
the engine is contacted. An authorization for a prohibited action also rejects.
After every local check passes, admission makes one read-only `capabilities()`
call. If that query fails, the result is `preflight: blocked` with structural
status `accepted`, not a structural rejection. Each deployed provider must offer `native_session` and `admitted_roles`,
plus `session_scoped_skills` when a supplied skill is applicable and
`merge_guard` when merge is effectively granted. Admission calls no other
transport operation.

## Result

On success the result holds a deterministic `snapshot` and `snapshot_digest`, the
contract `admission` record (request ID and context, resource and capability
digests) and a bounded `admission_input` (at most 1 MiB). That input lists
configured requirements, effective permissions, prohibitions, capability
bindings, source identities and every applicable custom input. The original
request text is kept byte-for-byte and apart from adapter assignments and
per-task `instructions`. Remote writes default off. A permission is effective
only when it is enabled, explicitly authorized and not prohibited.

Custom inputs are:

- applicable prompts or preferences;
- per-task instructions;
- free-text `requirements` and `prohibitions` (a prohibition that names a
  remote-write action is a structural control instead);
- selected adapters that differ from the baseline;
- supplied skills referenced by a selection.

When any exist, the result is structurally `accepted` and semantically
`blocked`, and preflight rejects. This lasts until semantic assessment (#12) and
supplied-skill execution (#16) land. The bundled validator check runs before the
engine query. The default plan has no custom inputs, so it is admitted with
`semantic: null` and needs no validator session.

## Bootstrap boundary

The configuration validator definition is bundled at
[`cf_workflows/bootstrap/configuration-validator.toml`](../cf_workflows/bootstrap/configuration-validator.toml),
and its SHA-256 is pinned in code. `bootstrap()` admits it structurally only. It
takes no configuration, prompts or skills, and nothing assesses it semantically.
A changed definition rejects. Configuration cannot supply the validator role,
select it for an adapter or target it with a prompt. Validation therefore stops
after one level.

## Not established here

- Running the validator, its verdicts and reconciliation: #12.
- Reusing acceptance: #13.
- Snapshot capture of PR metadata: #14.
- Supplied-skill sessions: #16, which needs Agent Workflow #139.

No real engine reports these capabilities yet; the gating issues are Agent
Workflow #129, #130 and #138. Until they land, real preflight rejects. The tests
use a fail-closed fake transport, and their results do not certify any engine,
provider or host.
