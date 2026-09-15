# Native support wheels

The release candidate uses two independently named distributions:

| Distribution | Import namespace | Contents |
| --- | --- | --- |
| `meltygui-imgui==2.0.0.post1` | `meltygui_imgui` | pyimgui 2.0.0 rebuilt for Python 3.12 |
| `meltygui-pycuda==2026.1.post1` | `meltygui_pycuda` | Pinned PyCUDA with OpenGL and cuRAND enabled |

Neither installs files into upstream `imgui` or `pycuda`. MeltyGUI imports its
own bindings; child apps can use `from meltygui import imgui`. Use the child-app
migration helper to update direct binding imports.

## First supported target

Linux x86-64, CPython 3.12, glibc 2.28 or newer. Builds use a pinned manylinux
image and pinned build dependencies. auditwheel also certifies older compatible
tags for these artifacts; 2.28 remains the tested build target. Other Python
versions and platforms have not been validated and are not advertised yet.

The PyCUDA wheel bundles cuRAND, with NVIDIA's CUDA 12.1 EULA. It does not bundle
the driver or nvcc. CUDA kernels still compile against the user's toolkit and
are cached; prebuilding the Python extension does not eliminate kernel JIT.
The GL/CPU rendering path remains available without PyCUDA.

## Build

```sh
python3 tools/native_wheels/build.py imgui --output dist/release-native/imgui
python3 tools/native_wheels/build.py pycuda --cuda-root /usr/local/cuda-12.1 \
  --output dist/release-native/pycuda
uv build --no-sources
python3 tools/release.py collect
```

Docker access is required. `--sudo-docker` uses noninteractive sudo where that
is already configured. The tools record upstream revisions, image digest and
artifact checksums. Original licenses and native-library notices ship in the
support wheels and source archives.

Both support packages have source distributions. On machines without a matching
wheel, source builds need a C++ compiler; PyCUDA also needs the CUDA toolkit with
nvcc on PATH. OpenGL remains enabled in source builds. The build pipeline itself
builds its wheels from those source archives.

## Local release-candidate install

```sh
uv venv --python 3.12 /tmp/meltygui-check
uv pip install --python /tmp/meltygui-check/bin/python \
  --find-links dist/release meltygui
/tmp/meltygui-check/bin/python -c 'import meltygui'
```

After PyPI publication, the `--find-links` argument is unnecessary. Optional
Torch/CUDA dependencies use `meltygui[tensor]`; select the appropriate Torch
backend for your project separately. See [publishing](PUBLISHING.md).

The older `tools/build_imgui_wheel.py` and `tools/pycuda_wheels/` recipes are
historical upstream-namespace experiments, not the public release path.
