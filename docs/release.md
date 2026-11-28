# CI and releases

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push to
`main`, every pull request and on demand. The suite needs only the standard
library; a few tests also need PyYAML from
[`requirements-test.txt`](../requirements-test.txt). Its jobs:

| Job | What it runs |
| --- | --- |
| `unit` (Python 3.11 and 3.13) | `python3 -B -m cf_workflows.package`, `scripts/check_acceptance.py` and the whole unit suite |
| `acceptance` | `scripts/check_acceptance.py --run`: every test the acceptance matrix references, on its own |
| `baselines` | `scripts/check_baselines.py` against fresh clones of dk-workflows and Agent Workflow |
| `engine` | `scripts/check_package_build.py` and the `CF_AGENT_WORKFLOW` tests against the pinned, unreleased Agent Workflow `TOOLCHAIN_COMMIT` |

The `engine` job downloads the toolchain's Go modules in a separate step, so the
build check itself still builds offline (`GOPROXY=off`). It verifies unreleased
upstream source only, as described in
[packaging](packaging.md#reproducible-builds-against-unreleased-source).
For version tags it retains the verified output as a tar archive so CI artifact
transport preserves executable modes. The release job consumes that same build.

## Releasing

The release version is the plugin version in
[`plugin.yaml`](../cf_workflows/workflows/dk-pr-fix/plugin.yaml), which must
equal the suite version in [`sources.json`](../baselines/sources.json). To
release, push the tag `v<version>` on a commit of `main`:

```sh
git tag -a v0.1.0-dev.1 -m 'cf-workflows 0.1.0-dev.1'
git push origin v0.1.0-dev.1
```

[`.github/workflows/release.yml`](../.github/workflows/release.yml) runs the
whole CI on the tag, then
[`scripts/release_gate.py`](../scripts/release_gate.py):

- rejects a tag that is not `v` plus the plugin version, a tag that does not
  point at the checked-out commit and a failing package check;
- for a final version (no prerelease part), rejects the release unless the
  package check reports `releasable: true`, and lists the blockers;
- for a prerelease version (such as `0.1.0-dev.1`), allows a **development
  package**: a GitHub prerelease, never marked latest, whose notes distinguish
  installation from execution, say the suite is not runnable and list every
  open release blocker.

The assets are the tagged source (`git archive`), package check report,
dual-host marketplace ZIP, toolchain build report and `SHA256SUMS`. The release
gate rechecks the build's checksums, archive and payload against the tagged
plugin closure before writing any asset. It refuses a nonempty output directory
so an older package cannot leak into the release.

The ZIP is installable through [these steps](installation.md). Its exact engine
pin still has no released download, and workflow execution has additional
[blockers](packaging.md#what-remains-blocked). A development package claims no
runtime support; the final-version gate remains unchanged. The gate can also
assemble a source-only snapshot when invoked without `--package-dir`, but the
release workflow requires the verified build artifact and always supplies it.

To start the next version, change the version in `plugin.yaml` and
`sources.json` and in the places that quote it (README, packaging guide,
contracts README and tests). `closure.json` digests `plugin.yaml`, so
regenerate it with `python3 -B -m cf_workflows.dk_pr_fix`.
