# Plugin descriptor, resource closure and runtime prerequisites

cf-workflows has one plugin project: the dk-pr-fix installation root,
`cf_workflows/workflows/dk-pr-fix/`. Agent Workflow's shared toolchain builds
the Codex and Claude packages from it. That toolchain is merged on Agent
Workflow's `main` but not released (see
[What remains blocked](#what-remains-blocked)). This repository checks in and
publishes no package, and it has no hand-maintained substitute: no host
manifests, launchers, catalogs or archives. A development check
[builds the packages from the unreleased source](#reproducible-builds-against-unreleased-source)
to show they are reproducible. Nothing here is releasable or certified.

## Descriptor

[`plugin.yaml`](../cf_workflows/workflows/dk-pr-fix/plugin.yaml) uses Agent
Workflow's version 1 descriptor format, as defined in agent-workflow PR #135
(`docs/PLUGIN_AUTHORING.md`):

- `name: cf-workflows` and `version: 0.1.0-dev` are the suite identity from
  [sources.json](../baselines/sources.json). The plugin version is independent of
  the engine version.
- `engine-version: 0.2.2` is the exact engine version that upstream `plugin init`
  pins. That version has no published release (the latest tag is `v0.2.1`), and
  neither has any engine with the plugin toolchain. Replace the pin with the
  first release that ships agent-workflow #130–#133. The pin claims no support.
- `hosts: [codex, claude]`. Host selection is independent of the execution
  provider that `agents.yml` selects.
- `skills` lists the inventory rows with an installed workflow: `dk-pr-fix`
  (`workflows/main.yml`), `dk-pr-review` (its [review-mode preset](review.md)
  `workflows/review.yml`), `dk-pr-iter` (its [routing entry](review.md#installed-entry)
  `workflows/iter.yml`, which runs one of those two), the three [feedback entries](feedback-skills.md)
  (`workflows/feedback-read.yml`, `feedback-address.yml` and
  `feedback-reply.yml`, which fix their task selection over `main.yml`), the
  [publication entries](publication-skills.md) `dk-pr-publish` and
  `dk-pr-review-post` (`workflows/publish.yml` and `workflows/review-post.yml`),
  the [prose presets](prose.md#installed-workflow) `dk-plain-english` and
  `dk-asd-ste100` (`workflows/plain-english.yml` and `workflows/asd-ste100.yml`),
  the [decisions preset](decisions.md#installed-workflow) `dk-pr-decisions`
  (`workflows/decisions.yml`), the
  [reviewer entries](reviewer-skills.md#installed-workflows)
  `dk-pr-reviewers-run`, `dk-code-simplify` and `dk-pr-approvers`
  (`workflows/reviewers-run.yml`, `workflows/code-simplify.yml` and
  `workflows/approvers.yml`), the
  [drafting presets](drafting.md#installed-workflow) `dk-pr-description`,
  `dk-commit-message` and `dk-issue` (`workflows/description.yml`,
  `commit-message.yml` and `issue.yml`), the
  [splitting presets](splitting.md#installed-workflow) `dk-commit-split` and
  `dk-pr-split` (`workflows/commit-split.yml` and `pr-split.yml`), the
  [configuration presets](configuration-skills.md#installed-workflow)
  `dk-repo-onboard`, `dk-config` and `dk-preferences` (`workflows/onboard.yml`,
  `config.yml` and `preferences.yml`, over `workflows/configure.yml`), and the
  [CI entries](ci-skills.md#installed-entries) `dk-ci`, `dk-ci-approve` and
  `dk-ci-flakes` (`workflows/ci.yml`, `ci-approve.yml` and `flakes.yml`).
  Each `SKILL.md`, such as
  [dk-pr-fix's](../cf_workflows/workflows/dk-pr-fix/skills/dk-pr-fix/SKILL.md),
  holds the developer-owned instructions. They state that the plugin is not
  runnable and list the prerequisites below. The shared toolchain adds the
  generated invocation and recovery guidance.
- `resources` declares the files that the workflow closure does not reach:
  `LICENSE`, `agents.yml`, `closure.json` and `request.example.json`.

## Resource closure

[`cf_workflows/package.py`](../cf_workflows/package.py) checks a plugin root.
It writes nothing.

1. It validates the descriptor with the upstream schema 1 rules: exact fields,
   identities and exact semantic versions, unique skills, local workflow paths
   inside a `workflows/` directory, and contained resource paths. It also requires both hosts. The parser accepts
   only the block-style subset this descriptor uses.
2. It collects the declared closure: each `SKILL.md`, whose frontmatter name must
   match its binding; the local reusable-workflow closure from each skill's
   workflow; the helper paths declared under `files:`; the agent definitions
   under `definitions:`, which must be YAML that admission accepts; and the
   declared resources. A called workflow must declare `on.workflow_call`.
   `files:` and `definitions:` must use block form, since the scanner reads no
   flow values. Remote or unsupported references, path traversal, missing
   files, invalid UTF-8 and symlinks reject.
3. The installed files must be exactly that closure. An undeclared file rejects,
   even when `closure.json` is regenerated to include it. So do non-UTF-8 names,
   names that collide after NFC normalization and case folding, Python bytecode
   and special files. A checked-in
   `.codex-plugin`, `.claude-plugin`, `.agents`, `plugin.json`,
   `marketplace.json` or `compatibility.json` rejects, because the toolchain
   generates them. The files must also match `closure.json`.
4. The closure digest is a SHA-256 over each path, content digest and executable
   bit. Location, ownership, timestamps and read-only modes do not change it.

The scanner reads only the reference forms these workflows use and rejects any
other. The engine's own step actions `agent/run` and `input/wait` reference no
plugin file. It is a consistency check of the toolchain's inputs, not a packager. The
upstream `agent-workflow plugin check` is authoritative. The
[build check](#reproducible-builds-against-unreleased-source) runs it from
unreleased source; it is supported only once released.

`python3 -B -m cf_workflows.package` prints the check and the inventory status
as JSON, and exits 1 on a diagnostic. The status:

- rejects a descriptor skill that is not an inventory row, and a plugin name or
  version that differs from the suite identity;
- lists each skill with its inventory parity status (currently
  `not_implemented`);
- reports `releasable: false` with its blockers: the missing engine release,
  the fixture runner's missing container-job support, the non-runnable suite,
  every listed row without implemented parity, and the
  [acceptance matrix](qualification.md#release-readiness) blockers (pending live
  procedures, no verified combination, rows without native evidence). The
  upstream blockers stay listed until a released capability is actually
  consumed here. Editing the inventory cannot remove them.

Adding a skill to the descriptor needs an inventory row and its installed
workflow. Listing a skill does not claim parity or support.

## Runtime prerequisites

[`cf_workflows/prerequisites.py`](../cf_workflows/prerequisites.py)
`check(repository=..., deployment=..., writes=..., ci_timeout_seconds=...)`
checks the prerequisites inherited from the portable example. `deployment` is
the parsed `agents.yml`. Each check is `ok`, `missing` with guidance, or
`blocked`:

| Check | Requirement |
| --- | --- |
| `python` | Host Python 3.11 or newer |
| `git` | Git on the host PATH |
| `repository` | A self-contained clone with a real `.git` directory: not a linked worktree, submodule or link; no object alternates; not shallow |
| `commit_identity` | For writing plans, a repository-local `user.name` and `user.email`; helper containers do not see global Git configuration |
| `deployment` | `agents.yml` passes structural admission, including a real digest-pinned `execution.image` |
| `container_runtime` | The configured runtime (`docker` by default) on PATH |
| `helper_image` | The pinned image is already present locally and contains Python 3.11 or newer, Git, `gh` and `sleep` |
| `run_timeout` | `controller.run-timeout` of at least `2h` and at least the CI window; the engine default `30m` is too short |
| `engine` | Always `blocked`: no verified exact-engine download path exists yet |

The check reports guidance; it never repairs anything. It runs only `git config
--local --get`, `IMAGE image inspect` and, for an image that is already present,
`run --rm --pull=never --network none` to list the image's tools. It never logs
in, reads or refreshes credentials, builds, pulls or loads images, installs an
engine, uses an engine found on PATH, grants write authority or writes to the
checkout or installation. Remote writes stay disabled: [preflight](dk-pr-fix.md)
rejects every authorization and effective permission.

The GitHub credential reaches helpers only through the run's declared
`github_token` secret reference. Provider authentication belongs to the
configured runtime. Setting up either is a manual step.

## Verification

[`tests/test_package.py`](../tests/test_package.py) checks two read-only copies of
the plugin root under paths with spaces and with different timestamps. They
yield the same report and closure digest, and the closure equals `closure.json`
plus the manifest itself. It also covers invalid descriptors, undeclared,
unreachable, missing, linked, colliding and bytecode files, hand-maintained
manifests, skill name mismatch, remote references, helper traversal, flow-form
helper and agent lists, called workflows without `workflow_call`, invalid UTF-8,
Unicode normalization collisions, native agent formats, a manifest mismatch, executable modes, unknown inventory rows,
foreign identities and blockers that inventory edits cannot remove. Its
`PackageBuild` cases drive the [build check](#reproducible-builds-against-unreleased-source)
with a scripted fake toolchain that records every invocation and fails any
unconfigured command. They inject a failed `plugin check`, a non-reproducible
report, undeclared, missing and altered payload files, an entry point without
the developer instructions, a generated descriptor that declares other files, an
incomplete checksum list and an incomplete ZIP, and assert the check and build
counts for each.

[`tests/test_prerequisites.py`](../tests/test_prerequisites.py) uses disposable
repositories under paths with spaces: a complete clone, a linked worktree, a
shallow clone and a clone with alternates. A fake host scripts PATH, image
inspection and the tool listing, and any other command fails without a live
fallback. Each case asserts invocation counts, that nothing in the checkout
changed, and that no command pulls, builds, loads, logs in or authenticates.

The relocated execution smoke test is in [dk-pr-fix verification](dk-pr-fix.md#verification).
It runs the installed helpers from a read-only copy, which now includes the
descriptor, against a separate checkout that contains no workflow definitions.

## Reproducible builds against unreleased source

[`scripts/check_package_build.py`](../scripts/check_package_build.py) is a
development check of the first acceptance criterion, identical packages with no
undeclared resources, against Agent Workflow source that no release contains:

```sh
python3 scripts/check_package_build.py --agent-workflow /path/to/agent-workflow
CF_AGENT_WORKFLOW=/path/to/agent-workflow python3 -m unittest tests.test_package -v
```

It reads the exact `TOOLCHAIN_COMMIT` (`87587758fdb3`, Agent Workflow `main`
with #129–#133, #138 and #139) from a local clone with `git archive`, never
fetching, checking out or changing the clone. It builds `cmd/agent-workflow`
offline (`GOPROXY=off`, read-only modules), so the Go toolchain and module cache
must already be present. Then it:

1. copies the plugin root twice, read-only, under different paths with spaces
   and with different timestamps;
2. runs `plugin check --json` on each copy and requires no diagnostics;
3. runs `plugin build` on each copy, with the targets in different orders
   (`codex,claude` and `claude,codex`) and different umasks;
4. requires `SHA256SUMS`, the ZIP, `build.json` and the whole output trees to be
   byte-identical, the checksums to cover exactly the build output, and the ZIP
   to hold exactly the marketplace tree (the payload plus both host catalogs);
5. requires the runtime payload, minus the generated assets
   (`.codex-plugin/plugin.json`, `.claude-plugin/plugin.json`, `plugin.json`,
   `compatibility.json`, `scripts/agent-workflow` and
   `scripts/engine_bootstrap.py`), to equal `closure.json` and the files it
   lists. Each file ships byte for byte with its executable bit, except
   `plugin.yaml`, which the toolchain regenerates with the same identity and a
   resource list of exactly the payload, and each `SKILL.md`, which must start
   with the developer instructions before the generated guidance.

It prints a JSON summary with `released: false` and the package digest. The
`tests/test_package.py` case that builds the real CLI is skipped unless
`CF_AGENT_WORKFLOW` names the clone. This verifies unreleased upstream source
only. It is not a supported or released toolchain, installs and runs nothing,
and no package is published from it.

## What remains blocked

Agent Workflow
[#130](https://github.com/dk-code-factory/agent-workflow/issues/130) (packaged
execution and shared verified engine setup),
[#131](https://github.com/dk-code-factory/agent-workflow/issues/131)
(`plugin build`), [#132](https://github.com/dk-code-factory/agent-workflow/issues/132)
(`plugin check` and `plugin test`) and
[#133](https://github.com/dk-code-factory/agent-workflow/issues/133) (release
validation) are merged on its `main`, so they are no longer listed as missing
capabilities. Two blockers remain:

- **Engine release.** As of 2026-11-26 no published release contains them (the
  newest tag is `v0.2.1`, which predates them), and the pinned `0.2.2` has no
  verified download. Setup through `engine_bootstrap.py`, release validation
  and supported package claims wait for it. Then replace the pin with that
  release.
- **Container jobs in `plugin test`.** The upstream fixture runner rejects jobs
  with a container ("fixtures do not support job containers"). Every bound
  skill's closure has them (`helper.yml`, `publication.yml`), so `plugin test`
  cannot run the suite (it requires a case for every bound skill), and no
  deterministic packaged smoke execution exists. The three configuration
  presets have no container job, but fixtures for them alone would not make
  the suite pass, so none are added. A real `plugin run` needs Docker, the
  pinned helper image, provider sessions and a released engine, which need
  separate authorization.

Neither needs a change to the descriptor format.
