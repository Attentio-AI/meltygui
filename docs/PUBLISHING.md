# Publishing the first release

Artifacts are prepared and locally verified. They have not been uploaded.

## Required account decisions

The public project uses Apache-2.0. The private IDE package must never be
included in either the public Git repository or release artifacts. The local
pre-split wheels have been moved to the private repository's artifact archive.
Publishing is gated by the `MELTYGUI_PUBLISH_ENABLED=true` repository variable
as well as the manual workflow's `publish` input. Enable that only after review.

1. Confirm the GitHub owner. `valine/meltygui` is the proposed repository; the
   local repo has no remote yet. The GitHub connector recognizes `valine`, but
   local `gh` is not authenticated. Authenticate with `gh auth login`, then
   create/push the standalone repo after reviewing the intended public files.
2. In the owner's PyPI account, configure pending trusted publishers for all
   three projects: `meltygui`, `meltygui-imgui`, and `meltygui-pycuda`. Each uses
   the same GitHub owner/repository, workflow `release.yml`, environment `pypi`.
   No long-lived PyPI token is needed for that path.

Official references:
- https://docs.pypi.org/trusted-publishers/adding-a-publisher/
- https://docs.astral.sh/uv/guides/package/

## Release workflow

`.github/workflows/release.yml` is manually dispatched. Leave `publish=false`
for the first CI run; it builds support wheels/source archives, builds MeltyGUI,
checks licenses and metadata, and tests a fresh installation. A publish run uses
PyPI trusted publishing and uploads the native packages before MeltyGUI. The
workflow has been prepared locally but has not run on GitHub yet.

The CUDA job extracts a toolkit from a pinned NVIDIA container. The native
compiler job uses a pinned manylinux image. Neither job needs a GPU; hardware
validation was performed locally and must remain a release check for CUDA changes.

For locally prepared artifacts:

```sh
uv build --no-sources
python3 tools/release.py collect --require-license
uvx --from twine==7.0.0 twine check --strict dist/release/*.whl dist/release/*.tar.gz
```

Only upload the six verified `.whl`/`.tar.gz` artifacts in `dist/release`, not the
entire `dist` tree. The latter also contains older experimental artifacts.
`SHA256.json` records the intended uploads. Never reuse a published version for
changed files; bump the relevant core or support-package version.

## Acceptance after upload

Use a new Python 3.12 environment with no local find-links or extra index:

```sh
uv venv --python 3.12 /tmp/meltygui-from-pypi
uv pip install --python /tmp/meltygui-from-pypi/bin/python meltygui
/tmp/meltygui-from-pypi/bin/python -I -c 'import meltygui; print(meltygui.__file__)'
```

Then launch a child app and check `meltygui[tensor]` on a supported NVIDIA machine.
A local install using `--find-links` validates the artifacts, not public PyPI
availability. Public acceptance remains pending until that final check passes.
