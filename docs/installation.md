# Install the development package

Version `0.1.0-dev.1` publishes a generated Codex/Claude marketplace ZIP. The
repository itself is the authoring source, not a marketplace: install from the
release ZIP rather than adding the repository URL to a host.

The package exposes all 29 skill entries. Its workflows remain blocked by the
unreleased exact engine and the other [runtime prerequisites](packaging.md#what-remains-blocked).
Installation does not download an engine, run a workflow, contact a provider or
grant GitHub write authority.

## Codex

With GitHub CLI, `sha256sum`, `unzip` and Codex installed, download and verify
the release assets into a new persistent directory, then extract the marketplace:

```sh
cf_package_dir="$HOME/.local/share/cf-workflows/0.1.0-dev.1"
mkdir -p "$cf_package_dir"
gh release download v0.1.0-dev.1 --repo dk-code-factory/cf-workflows --dir "$cf_package_dir"
(cd "$cf_package_dir" && sha256sum --check SHA256SUMS)
unzip -q "$cf_package_dir/cf-workflows-0.1.0-dev.1.zip" -d "$cf_package_dir"
codex plugin marketplace add "$cf_package_dir/cf-workflows"
codex plugin add cf-workflows@cf-workflows
```

Run each command only after the preceding command succeeds. Keep this directory:
the configured marketplace points to it. `unzip` preserves the launcher's Unix
executable mode. On macOS use `shasum -a 256 --check SHA256SUMS` for verification.
Skills become available on the next turn. Their names use the `cf-workflows:`
prefix, separate from an existing `dk-workflows` installation.

For a new release, use a new versioned directory and repeat verification before
updating the configured marketplace. Do not overwrite an existing extracted
package in place. The ZIP also contains Claude's `.claude-plugin/marketplace.json`
for adding the same extracted marketplace in Claude Code.

## Verify installation

`codex plugin list` should show `cf-workflows@cf-workflows` as installed and
enabled. The plugin contains `skills/`, the full workflow/helper closure, both
host manifests and the shared bootstrap under `scripts/`.

The bootstrap's `check` command is read-only:

```sh
"$cf_package_dir/cf-workflows/plugins/cf-workflows/scripts/agent-workflow" check
```

A missing verified engine is expected for this development version. Do not
substitute an engine from PATH or treat successful host installation as runtime
acceptance. See the [acceptance matrix](qualification.md) for pending evidence.
