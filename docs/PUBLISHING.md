# Publishing a release

`meltygui` 0.1.0 was published on 2026-09-18 through the workflow below.

## Required account decisions

The public project uses Apache-2.0. The private IDE package must never be
included in either the public Git repository or release artifacts. The local
pre-split wheels have been moved to the private repository's artifact archive.
Publishing is gated by the `MELTYGUI_PUBLISH_ENABLED=true` repository variable
as well as the manual workflow's `publish` input. Enable that only after review.

1. The repository is `Attentio-AI/meltygui`. It needs a `pypi` environment and
   the `MELTYGUI_PUBLISH_ENABLED=true` repository variable.
2. In the owner's PyPI account, configure a pending trusted publisher for
   `meltygui`: owner `Attentio-AI`, repository `meltygui`, workflow
   `release.yml`, environment `pypi`. No long-lived PyPI token is needed.
   `meltygui-imgui` and `meltygui-pycuda` publish from their own repositories
   and are already on PyPI; see [support wheels](SUPPORT_WHEELS.md).

Official references:
- https://docs.pypi.org/trusted-publishers/adding-a-publisher/
- https://docs.astral.sh/uv/guides/package/

## Release workflow

`.github/workflows/release.yml` is manually dispatched. Leave `publish=false`
for the first CI run; it builds MeltyGUI, checks licenses and metadata, and
tests a fresh installation against the support wheels on PyPI. A publish run
uploads MeltyGUI through PyPI trusted publishing. Hardware validation of
`meltygui[tensor]` is performed locally and remains a release check for CUDA changes.

For locally prepared artifacts:

```sh
uv build --no-sources
python3 tools/release.py collect --core-only --require-license
uvx --from twine==7.0.0 twine check --strict dist/release/*.whl dist/release/*.tar.gz
```

Only upload the verified MeltyGUI wheel and source archive in `dist/release`, not the
entire `dist` tree. The latter also contains older experimental artifacts.
`SHA256.json` records the intended uploads. Never reuse a published version for
changed files; bump the relevant core or support-package version.

### From Thermostat

The top **Deployment checklist** has **Prepare deployment** and **Deploy**. Preparation synchronizes
all three package versions and MeltyGUI's native requirements, optionally commits/pushes the release
edits, verifies native builds, and runs MeltyGUI's installation tests with support wheels.
Existing changes must first be reviewed and pushed. Deploy rechecks the tested source commits and
publishes ImGui, PyCUDA, then MeltyGUI, waiting for each native release to reach PyPI.
The `imgui_run_id` and `pycuda_run_id` workflow inputs identify successful native builds. During
verification, their public source commits are checked out and rebuilt into staged wheels; this needs
no credential for cross-repository artifact downloads. Already published native releases and all
publication runs install support wheels from PyPI. GPU execution still needs separate hardware validation.

Individual controls remain available:

Use Deployments → **Raise version** when needed → **Review / commit / push** →
**Verify build** → **Publish**. The review button opens the repository's Version
Control card. The UI distinguishes failed, unverified and verified source;
confirmations carry the displayed version and source commit. The workflow's
optional `expected_version` and `expected_sha` inputs validate that version and
pin checkout to the confirmed source. Manual GitHub runs may leave both blank.

## Acceptance after upload

Use a new environment for each supported Python (3.11, 3.12, 3.13) with no local find-links or extra index:

```sh
uv venv --python 3.12 /tmp/meltygui-from-pypi
uv pip install --python /tmp/meltygui-from-pypi/bin/python meltygui
/tmp/meltygui-from-pypi/bin/python -I -c 'import meltygui; print(meltygui.__file__)'
```

Then launch a child app and check `meltygui[tensor]` on a supported NVIDIA machine.
A local install using `--find-links` validates the artifacts, not public PyPI
availability. 0.1.0 passed the fresh-install check on Linux (3.11-3.13) and Windows (3.12).
