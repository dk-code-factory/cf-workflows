# Installed dk-pr-fix workflow

`cf-workflows:dk-pr-fix` is Agent Workflow's portable PR-fix example, extracted
into an installed resource closure. It is not runnable yet: the engine cannot
yet run an installed package against a separate checkout or mount the state
namespace (see [What remains blocked](#what-remains-blocked)).

## Installed closure

`cf_workflows/workflows/dk-pr-fix/` is the
installation root:

- `workflows/main.yml` is the entry. Reusable references such as
  `uses: workflows/helper.yml` are relative to the installation root, not the
  target checkout.
- `workflows/helpers/` holds the bounded Python operations. Helper containers
  mount it at `/actions/helpers`; each call performs one operation.
- `workflows/agents/*.yaml` is the admitted role catalog: `leaf`, `reviewer`,
  `verifier`, `fixer` and `audience`.
- `agents.yml` is the deployment template and `request.example.json` is the private
  request shape.
- `closure.json` holds the SHA-256 of every installed file. `LICENSE` is the
  upstream MIT notice.

The installation is never written. Python would load a cached `.pyc` instead
of its verified source, so any `__pycache__` directory or `.pyc`/`.pyo` file
rejects the installation. The helpers also run with `-B`. To regenerate the
manifest after an intentional change, remove the caches and run
`python3 -B -m cf_workflows.dk_pr_fix`.

## Provenance

[`baselines/dk-pr-fix-closure.json`](../baselines/dk-pr-fix-closure.json) maps each
extracted file and ported test to its path and SHA-256 at the pinned commit
`16ec5eecccd367dfdffcf1b529c51d181bbced49`. Each file is `verbatim`, `adapted` or
`converted`. Every other file of the example is listed as excluded, with a reason:
the copy-into-workspace README and the optional review-profile generator.
The [baseline checker](verification.md) compares the files with the pinned objects.

The five Codex TOML roles are converted to the engine's canonical YAML form:
`name`, `description`, `instructions` and optional `reasoning-effort`, one
`key: "JSON string"` line each. The checker regenerates the conversion from the
pinned TOML. The output is byte-identical to the upstream conversion in Agent
Workflow PR #135. Instruction text is unchanged.

Adaptations to the example:

- Application state moved out of the checkout (next section).
- The private request is read from the state namespace, not from a
  workspace-relative path.
- Reusable references point into the installation.
- The retry and checklist prompts say that the assignment and the prior report
  are earlier in the same session. The references in `task_input` point into the
  state namespace, which the session cannot read, so they are immutable record
  IDs only.
- The legacy `.pr-fix-state` directory gets no special treatment. In a checkout,
  it is ordinary untracked content.

## Configuration split

| Input | Source |
| --- | --- |
| Declared workflow inputs | `main.yml` inputs, set by preflight from the admitted options |
| Deployment configuration | Parsed `agents.yml` shape, checked by admission |
| Admitted roles | The installed YAML catalog, by digest |
| Private request | Exact PR URL, instructions and logical run, staged in the state namespace |

[`cf_workflows/dk_pr_fix.py`](../cf_workflows/dk_pr_fix.py) prepares a run. It
starts no session and runs nothing.

1. `preflight()` checks the installation against `closure.json` and checks that
   the target checkout is outside it. On failure it rejects before contacting the
   engine.
2. It runs [structural admission](admission.md) with the installed role catalog.
   Custom prompts, per-task instructions, custom adapters and free-text policy
   reject at preflight. Admission reports them as `semantic_required`, but the
   installed workflow has no inputs that carry them yet, so running without them
   would silently drop them. Supplied skills reject until #16.
3. Remote-write authorizations and effective permissions reject. Publication and
   merge stay disabled until the guarded path in #17 is available. Every
   permission input is `false`.
4. It maps the effective options onto the declared inputs: tasks, finalizers,
   reviewers, the selected baseline adapters, limits, CI timeout, merge method,
   accounts, settings and turns. `inputs.request` names the staged request.
5. `stage_request()` writes the request as `requests/<sha256>.json` (mode 0600) in
   the state namespace. This is the only write.

## State namespace

Application state lives in
`$XDG_STATE_HOME/cf-workflows/repositories/<sha256(repository)[:32]>` (fallback
`~/.local/state`). It is outside the plugin installation and every target
checkout. Helper containers receive it at `/cf-workflows-state` through
`CF_WORKFLOWS_STATE`. Helpers reject a missing or relative path, a symlink, and
a directory that overlaps the checkout.

Each logical run has its own SQLite ledger, `runs/<sha256(logical_run)>.sqlite`,
which holds claims, submissions, reservations, deadlines, operations, publication
intents and local commits. Run text never forms a path, and runs that differ only
by case stay separate. The repository namespace also holds the writer lease and
the content-addressed `artifacts/`, `preparations/` and `requests/` stores. This
is a deliberate refinement of the contract's per-run partitioning. The lease
fences the shared Git index across runs, so it must be visible to every run.
Content-addressed records are immutable and named by digest, so sharing them
cannot mix two runs' state. The marker `.owned-by-cf-workflows-dk-pr-fix`
(format `cf-workflows-dk-pr-fix-v1`) claims a directory only while it is empty.
So dk-workflows settings, `.pr-fix-state` and other existing state are never
adopted or imported. Back up the namespace together with the engine's recovery
bundle.

## Verification

[`tests/test_dk_pr_fix.py`](../tests/test_dk_pr_fix.py) covers the following:

- Installed-closure integrity and provenance coverage.
- Reusable references that resolve inside the installation.
- Role parsing by admission and by the helpers.
- No provider, engine or container launcher in the helpers, and the state
  namespace for every helper step.
- Preflight mapping and rejections, with invocation counts on a fail-closed fake
  engine transport.

In one case, the installation is copied to a read-only directory with spaces in
its path. The request is staged, and then `snapshot_request`, `prepare` and the
first `claim` run in a separate interpreter. The helpers are imported only from
that copy, against a separate target checkout with spaces in its path. The case
uses a fake GitHub transport that fails every unconfigured operation. It checks
the following:

- The exact instructions and head/target identities reach the plan and the
  assignment.
- Independent verification is a separate required `verifier` task.
- Capture is called exactly twice.
- The target has no changed or ignored files.
- The installation is unchanged.

The ported example tests in `tests/dk_pr_fix/` run against
the installed helpers, each with a fresh state namespace.

## What remains blocked

These fixtures do not run the YAML graph, start sessions or prove any engine,
provider or host capability.

- [Agent Workflow #129](https://github.com/dk-code-factory/agent-workflow/issues/129):
  engine-managed native sessions through the Codex or Claude SDK.
- [Agent Workflow #130](https://github.com/dk-code-factory/agent-workflow/issues/130):
  running an installed package against a separate checkout, resolving its
  reusable references and admitted assets, and mounting the state namespace.
- [Agent Workflow #139](https://github.com/dk-code-factory/agent-workflow/issues/139)
  discovery-isolation PR: sessions with host and repository skill and plugin
  discovery disabled, and observed evidence of the session's plugin set.
- [Agent Workflow #138](https://github.com/dk-code-factory/agent-workflow/issues/138)
  and #17: merge and publication.
- #15: retained budgets, occurrence identity and continuation across runs.
- Workflow inputs for semantically admitted custom inputs (#12), and #16 for
  supplied skills.

No local launcher, controller or second agent loop stands in for these.
