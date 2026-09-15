# Development

## Setup

The first release target is Linux x86-64, Python 3.12, with a working OpenGL 4.3
context. Build the native support wheels with [the native build guide](SUPPORT_WHEELS.md),
or use the prepared artifacts under `dist/release`:

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python --find-links dist/release -e . --group dev
```

The `meltygui-imgui` dependency supplies a namespaced Python 3.12 binding; it does
not replace upstream `imgui`. Low-level app code uses `from meltygui import imgui`.

### Tensor dependencies

```sh
uv pip install --python .venv/bin/python --find-links dist/release -e '.[tensor]' --group dev
```

The extra declares Torch and the namespaced, GL-enabled PyCUDA build. Select the
appropriate Torch CUDA backend for your project. CUDA rendering needs an NVIDIA
driver and CUDA toolkit with nvcc and a compatible host compiler. Both native
support packages also ship source distributions for local compilation.

## Run and check

Run apps with the environment's Python. The native Wayland and GLFW backends
share the same render functions.

```sh
.venv/bin/pytest
.venv/bin/python examples/tensor_live.py
```

Use `MELTY_BENCH=1` for a first-frame smoke run:

```sh
MELTY_BENCH=1 .venv/bin/python examples/tensor_live.py
```

For UI verification, follow the desktop reservation instructions in
`/home/lukas/AGENTS.md` when working on Lukas's machine.

## Packaging and release

Build and test a noneditable wheel outside the checkout before release:

```sh
.venv/bin/python -m build
```

See [Native dependency wheels](SUPPORT_WHEELS.md#local-release-candidate-install) for
installing the resulting wheel into a separate environment. Run validation
from outside the repository to ensure imports resolve to the installed package.

MeltyGUI is licensed under Apache 2.0 and is not yet published to PyPI.
The public distribution excludes the separate proprietary IDE package.
See [Publishing](PUBLISHING.md) for account setup and release verification.

## Project structure

- `meltygui/rendering`, `state`, `windows`, `widgets`, `views`: UI toolkit and state.
- `meltygui/tensor`, `graphics`: tensor rendering and shaders.
- `meltygui/files`: file browsing and selection.
- `meltygui/editor`: text editing, source preview and live inspection.
- `meltygui/code`: source parsing, hotswap, symbol definitions and usage.
- `meltygui/extensions.py`: optional application service callbacks.

Project management, venvs, dependency tools, Git, open-file tabs and the full
code-editor window are maintained in the separate private package. They are
not dependencies of MeltyGUI. The original latent-descent checkout remains a
reference, not a runtime dependency.

See [Building an app](APPS.md) for injected state, render functions and windows.
