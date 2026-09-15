# GL-enabled PyCUDA: first wheel

Build and verification tools for `pycuda==2026.1+melty.gl1`.
The local version identifies Melty's OpenGL-enabled variant. This remains the
`pycuda` distribution and imports as `pycuda`; do not install another distribution
that writes the same module beside it.

## Build

From the meltygui checkout:

```sh
uv run --no-project --python 3.12 tools/pycuda_wheels/build.py \
  --cuda-root /usr/local/cuda-12.1 --output dist/pycuda-gl
```

The recipe checks out upstream commit
`19bed9035edb9990b2928e297125d8e9d470130b` with its pinned submodules. It enables GL,
adds the local version, pins setuptools/wheel/NumPy build dependencies, and avoids
ambient `~/.aksetup-defaults.py` and global configuration. CUDA is discovered from
the toolkit placed first on PATH; no machine-specific toolkit path is stored in
the source archive. Build products and environment details are recorded in
`build-manifest.json`, including SHA-256 checksums.

`uv build` creates the source archive and builds the wheel from that archive.
`auditwheel repair` bundles cuRAND, leaving the NVIDIA driver external. The
original wheel is kept under `raw/`; install the repaired wheel from the parent
folder. No installation into the editor or studio environment happens here.

### First artifact's compatibility

- CPython 3.12, Linux x86_64, GL enabled, cuRAND enabled.
- Built with CUDA 12.1 and GCC 13 on Linux Mint 22.
- Audited platform: **manylinux_2_39_x86_64**, with a recent platform C++ runtime.
- The NVIDIA driver remains a system dependency. CUDA kernel source compilation
  additionally needs `nvcc` and a compatible host compiler.
- This is the first local artifact, not a broadly portable/public release. Build
  the release matrix on a chosen older manylinux baseline before publishing;
  finish NVIDIA redistributable notices for bundled cuRAND at that point.

## Install and verify the wheel

```sh
uv venv --python 3.12 /tmp/pycuda-wheel-check
uv pip install --python /tmp/pycuda-wheel-check/bin/python \
  dist/pycuda-gl/pycuda-2026.1+melty.gl1-cp312-cp312-manylinux_2_39_x86_64.whl \
  numpy==2.2.6 PyOpenGL==3.1.10
PATH=/usr/local/cuda-12.1/bin:$PATH CUDAHOSTCXX=/usr/bin/g++-11 \
  /tmp/pycuda-wheel-check/bin/python tools/pycuda_wheels/verify.py
```

Verification imports from the fresh environment, compiles and executes a small
kernel on every visible CUDA GPU, and performs an actual CUDA-to-OpenGL buffer
roundtrip through an EGL device display. It does not open or control a desktop.
The kernel compiler is explicitly GCC 11 because CUDA 12.1 does not support the
host's GCC 13 for nvcc compilation. This does not change the system compiler.

## Exercise the source fallback

```sh
uv venv --python 3.12 /tmp/pycuda-source-check
PATH=/usr/local/cuda-12.1/bin:$PATH uv pip install \
  --python /tmp/pycuda-source-check/bin/python --no-cache --no-binary pycuda \
  dist/pycuda-gl/pycuda-2026.1+melty.gl1.tar.gz numpy==2.2.6 PyOpenGL==3.1.10
PATH=/usr/local/cuda-12.1/bin:$PATH CUDAHOSTCXX=/usr/bin/g++-11 \
  /tmp/pycuda-source-check/bin/python tools/pycuda_wheels/verify.py
```

The source fallback requires Python/C++ development headers, CUDA headers and
libraries, and GL headers. It preserves the GL build without an editable checkout
or a manual `configure.py` step. Upstream prints its generic missing-config
warning, but this variant's defaults already enable GL.

Later, publish the repaired wheels and this source archive on the same explicit
package index so uv can choose by compatibility. A direct wheel URL alone cannot
provide automatic fallback. IDE setup/repair actions should consume that same
package source; no startup package rebuilding is added by these tools.
