# Development

Start with [Contributing](../CONTRIBUTING.md) for the framework design, feature
boundaries and render-function contract. This page covers the environment and
verification workflow. [Architecture gaps](ARCHITECTURE_DEBT.md) records the
remaining migration work and deliberate deferrals.

## Setup

Supported targets are Linux x86-64 with CPython 3.11, 3.12 or 3.13 and a working
OpenGL 4.3 context. MeltyGUI itself is not on PyPI yet, so install it from a
checkout; its native support packages (`meltygui-imgui`, `meltygui-pycuda`) come
from PyPI as prebuilt wheels:

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python -e . --group dev
```

To use MeltyGUI from another project, add the checkout as a path dependency:

```sh
uv add "meltygui[tensor] @ /path/to/meltygui"
```

`meltygui-imgui` supplies a namespaced binding; it does not replace upstream
`imgui`. Low-level app code uses `from meltygui import imgui`.

### Tensor dependencies

```sh
uv pip install --python .venv/bin/python -e '.[tensor]' --group dev
```

The extra declares Torch and the namespaced, GL-enabled PyCUDA build. It does
not pin a Torch CUDA backend: select the one for your project, for example
`torch==2.5.1+cu121` with `--extra-index-url https://download.pytorch.org/whl/cu121`.
Torch older than 2.3 also needs `numpy<2`. CUDA rendering needs an NVIDIA driver
and a CUDA toolkit with nvcc and a compatible host compiler, because kernels
compile at runtime; the PyCUDA wheel's own CUDA 12.1 build does not have to
match the toolkit or Torch's CUDA version. A Torch environment is several GB;
the GL/CPU path without the extra is about 260 MB.

### When no wheel matches

On another platform or Python, pip and uv silently fall back to compiling the
support packages, and only show the build output when it fails.
[Native support wheels](SUPPORT_WHEELS.md#when-no-wheel-matches) describes what
to expect. Use unpublished support wheels with `--find-links <folder>`.

### PyTorch compatibility matrix

`tools/torch_matrix.py` installs a noneditable MeltyGUI wheel next to a range of
Torch releases (one fresh venv each, outside the checkout) and runs the CUDA and
tensor tests against it. It downloads several GB per Torch build:

```sh
python3 tools/torch_matrix.py --list
python3 tools/torch_matrix.py --only py312-torch2.5
python3 tools/torch_matrix.py --window-smoke   # also opens the README example: agent desktop only
```

## Run and check

Run apps with the environment's Python. The native Wayland and GLFW backends
share the same render functions.

```sh
.venv/bin/pytest
.venv/bin/python examples/tensor_live.py
.venv/bin/python examples/voxel_playground.py
```

Use `MELTY_BENCH=1` for a first-frame smoke run; it prints the start-up
phase table (also appended to `~/.cache/<app_id>/startup.log` on every launch)
and exits after the first frame:

```sh
MELTY_BENCH=1 .venv/bin/python examples/tensor_live.py
```

### Start-up

The boot sequence is described at the top of `meltygui/core/runtime/app.py`
and in the `meltygui` package docstring. An app's own imports run before its
first `@glfw_window`; that decoration starts an import thread (the render
libraries — imgui, numpy, OpenGL — and the GL side of the runtime) and opens
the display on the calling thread, so the two overlap. The overlap is only real
while the render graph stays light: `meltygui`, `core_render` and the views
must not import the render libraries or the code-editing stack (libcst) at
module level. The code stack loads with the first code edit — the inputs tab,
a code view, a hotswap. `tests/test_startup_imports.py` pins this; when a
change makes it fail, move the import into the function that needs it or,
for a renderer of a code-stack type, register it by name
(`is_default_for='CodeLine'`). Start-up workers belong in
`meltygui.after_first_frame(fn)`, not in the first frame.

Smoke-run every README example the same way (opens windows; run it from a
reserved agent desktop):

```sh
MELTY_README_SMOKE=1 .venv/bin/pytest tests/test_readme_examples.py
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

- `meltygui/core`: rendering/injection, Melty, modes, shared conversion and
  persistence, events, caches, window lifecycle and backend integration.
- `meltygui/view/<feature>_view.py`: reusable plain render functions.
- `meltygui/model/<feature>_model.py`: value adapters and feature operations.
- `meltygui/state/<feature>_state.py`: explicit feature/view state and helpers.
- `meltygui/examples` and top-level `examples`: sample data, composition and apps.
- `meltygui/core/runtime/extensions.py`: optional application service callbacks.

`views`, `widgets`, `rendering` and `windows` have been removed. All imports use
current paths; update `meltygui_pro` and `melty_code_editor` together with moves.
Saved-session name translation does not make historical paths importable.
Remaining `tensor`, `graphics`, `files`, `models`, `chat`, `completion`, `code`,
`editor` and helper modules are a mixture of feature implementations and adapters
still being classified. Their existing locations are not the template for new
contributions. The text-editor refactor is deferred; see the architecture inventory
before including adjacent work.

Project management, venvs, dependency tools, Git, open-file tabs and the full
code-editor window are maintained in the separate private package. They are
not dependencies of MeltyGUI. The original latent-descent checkout remains a
reference, not a runtime dependency.

See [Building an app](APPS.md) for injected state, render functions and windows.
