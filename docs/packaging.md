# Plugin descriptor, resource closure and runtime prerequisites

cf-workflows has one plugin project: the dk-pr-fix installation root,
`cf_workflows/workflows/dk-pr-fix/`. Agent Workflow's shared toolchain builds
the Codex and Claude packages from it. That toolchain is not available yet (see
[What remains blocked](#what-remains-blocked)), so this repository builds no
package. It also has no hand-maintained substitute: no host manifests,
launchers, catalogs or archives. Nothing here is releasable or certified.

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
- `skills` lists only `dk-pr-fix`, the only inventory row with an installed
  workflow. Its [`SKILL.md`](../cf_workflows/workflows/dk-pr-fix/skills/dk-pr-fix/SKILL.md)
  holds the developer-owned instructions. They state that the plugin is not
  runnable and list the prerequisites below. The shared toolchain adds the
  generated invocation and recovery guidance.
- `resources` declares the files that the workflow closure does not reach:
  `LICENSE`, `agents.yml`, `closure.json` and `request.example.json`.

## Resource closure

[`cf_workflows/package.py`](../cf_workflows/package.py) checks a plugin root.
It writes nothing.

1. It validates the descriptor with the upstream schema 1 rules: exact fields,
   identities and exact semantic versions, unique skills, local workflow paths,
   and contained resource paths. It also requires both hosts. The parser accepts
   only the block-style subset this descriptor uses.
2. It collects the declared closure: each `SKILL.md`, whose frontmatter name must
   match its binding; the local reusable-workflow closure from each skill's
   workflow; the helper paths declared under `files:`; the agent definitions
   under `definitions:`, which must be YAML that admission accepts; and the
   declared resources. Remote or unsupported references, path traversal,
   missing files and symlinks reject.
3. The installed files must be exactly that closure. An undeclared file rejects,
   even when `closure.json` is regenerated to include it. So do case-insensitive
   name collisions, Python bytecode and special files. A checked-in
   `.codex-plugin`, `.claude-plugin`, `.agents`, `plugin.json`,
   `marketplace.json` or `compatibility.json` rejects, because the toolchain
   generates them. The files must also match `closure.json`.
4. The closure digest is a SHA-256 over each path, content digest and executable
   bit. Location, ownership, timestamps and read-only modes do not change it.

The scanner reads only the reference forms these workflows use and rejects any
other. It is a consistency check of the toolchain's inputs, not a packager. The
upstream `agent-workflow plugin check` is authoritative once it is released.

`python3 -B -m cf_workflows.package` prints the check and the inventory status
as JSON, and exits 1 on a diagnostic. The status:

- rejects a descriptor skill that is not an inventory row, and a plugin name or
  version that differs from the suite identity;
- lists each skill with its inventory parity status (currently
  `not_implemented`);
- reports `releasable: false` with its blockers: the upstream capabilities, the
  missing engine release, the non-runnable suite and every listed row without
  implemented parity. The upstream blockers stay listed until a released
  capability is actually consumed here. Editing the inventory cannot remove them.

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
manifests, skill name mismatch, remote references, helper traversal, native
agent formats, a manifest mismatch, executable modes, unknown inventory rows,
foreign identities and blockers that inventory edits cannot remove.

[`tests/test_prerequisites.py`](../tests/test_prerequisites.py) uses disposable
repositories under paths with spaces: a complete clone, a linked worktree, a
shallow clone and a clone with alternates. A fake host scripts PATH, image
inspection and the tool listing, and any other command fails without a live
fallback. Each case asserts invocation counts, that nothing in the checkout
changed, and that no command pulls, builds, loads, logs in or authenticates.

The relocated execution smoke test is in [dk-pr-fix verification](dk-pr-fix.md#verification).
It runs the installed helpers from a read-only copy, which now includes the
descriptor, against a separate checkout that contains no workflow definitions.

## What remains blocked

- [agent-workflow #130](https://github.com/dk-code-factory/agent-workflow/issues/130)
  (draft PR #142): running an installed package against a separate checkout, and
  the shared verified exact-engine setup.
- [agent-workflow #131](https://github.com/dk-code-factory/agent-workflow/issues/131):
  `plugin build`. It generates both hosts' manifests, launchers, catalogs and
  byte-identical archives. Byte-identical packages need it.
- [agent-workflow #132](https://github.com/dk-code-factory/agent-workflow/issues/132):
  `plugin check` and `plugin test` with deterministic fixtures for both targets.
- [agent-workflow #133](https://github.com/dk-code-factory/agent-workflow/issues/133):
  release validation of generated artifacts, checksums and compatibility.
- A published engine release of the pinned version, for the verified download.

Installed-package smoke execution through the engine, plugin check/test of
both targets and the archive comparison wait for these. They need no change to
the descriptor format.
