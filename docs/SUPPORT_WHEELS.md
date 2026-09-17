# Native support wheels

MeltyGUI depends on two independently named distributions, published on PyPI:

| Distribution | Import namespace | Contents | Repository |
| --- | --- | --- | --- |
| `meltygui-imgui` 2.0.0.post2 | `meltygui_imgui` | pyimgui 2.0.0 | [Attentio-AI/meltygui-imgui](https://github.com/Attentio-AI/meltygui-imgui) |
| `meltygui-pycuda` 2026.1.post2 | `meltygui_pycuda` | Pinned PyCUDA with OpenGL and cuRAND enabled | [Attentio-AI/meltygui-pycuda](https://github.com/Attentio-AI/meltygui-pycuda) |

Neither installs files into upstream `imgui` or `pycuda`. MeltyGUI imports its
own bindings; child apps can use `from meltygui import imgui`. Use the child-app
migration helper to update direct binding imports.

## Supported targets

Linux x86-64, glibc 2.28 or newer, with one wheel each for CPython 3.11, 3.12
and 3.13. Builds use a pinned manylinux image and pinned build dependencies.
auditwheel also certifies older compatible tags for these artifacts; 2.28 remains
the tested build target. The 3.13 ImGui binding is generated with Cython 3.2;
3.11 and 3.12 keep Cython 0.29.

`meltygui-imgui` 2.0.0.post3 adds Windows x86-64 wheels for the same CPythons,
built with MSVC from the same source archive; delvewheel bundles the C++ runtime.
`meltygui-pycuda` has no Windows build, so the `tensor` extra installs torch
alone there and tensors render through the GL/CPU path. Other platforms have
not been validated.

## When no wheel matches

pip and uv never say that no wheel matched: they fall back to the source
archive and compile it, hiding the build output unless it fails.

| Situation | What happens |
| --- | --- |
| Other Python with pip (3.10, 3.14) | pip refuses: `Requires-Python >=3.11,<3.14`. |
| Newer Python with uv | uv ignores the upper bound and compiles from source. |
| Other platform (aarch64, macOS, old glibc) | Source build. |
| Source build, no CUDA toolkit | `meltygui-pycuda` stops at once with a short message naming your platform, the prebuilt targets and `CUDA_ROOT`. |
| Source build, toolkit present | About a minute for PyCUDA, a few minutes for ImGui. PyCUDA links the toolkit's own cuRAND rather than a bundled copy. |
| Wheel installed, no NVIDIA driver | `import meltygui_pycuda.driver` fails with `libcuda.so.1` not found; the GL/CPU path still works. |

Both source builds print a banner with your platform and the prebuilt targets;
installers show it with `-v` or on failure. A source build needs a C++ compiler,
and PyCUDA needs nvcc on PATH or `CUDA_ROOT`. To make a missing wheel an error
rather than a compilation, pass
`--only-binary meltygui-pycuda --only-binary meltygui-imgui`.

The PyCUDA wheel bundles cuRAND, with NVIDIA's CUDA 12.1 EULA. It does not bundle
the driver or nvcc. CUDA kernels still compile against the user's toolkit and
are cached; prebuilding the Python extension does not eliminate kernel JIT.
The GL/CPU rendering path remains available without PyCUDA.

## Build

Each support package is built and released from its own repository:

```sh
python3 tools/build.py                                    # meltygui-imgui
python3 tools/build.py --cuda-root /usr/local/cuda-12.1   # meltygui-pycuda
python3 tools/verify.py dist
```

Docker access is required. `--sudo-docker` uses noninteractive sudo where that
is already configured. The tools record upstream revisions, image digest and
artifact checksums. Every wheel is built from the source archive that users fall
back to. Original licenses and native-library notices ship in the wheels and
source archives.

To collect a MeltyGUI release with locally built support artifacts, copy each
repository's output folder to `dist/release-native/imgui` and
`dist/release-native/pycuda`, then:

```sh
uv build --no-sources
python3 tools/release.py collect
```

## Local release-candidate install

```sh
uv venv --python 3.12 /tmp/meltygui-check
uv pip install --python /tmp/meltygui-check/bin/python \
  --find-links dist/release meltygui
/tmp/meltygui-check/bin/python -I -c 'import meltygui'
```

`--find-links` is only needed for artifacts that are not on PyPI yet. Optional
Torch/CUDA dependencies use `meltygui[tensor]`; select the appropriate Torch
backend for your project separately. See [publishing](PUBLISHING.md).

`tools/native_wheels/`, `tools/build_imgui_wheel.py` and `tools/pycuda_wheels/`
are earlier in-tree recipes, kept for reference; they are not the release path.
