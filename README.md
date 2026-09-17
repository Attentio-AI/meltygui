# MeltyGUI

Low latency tensor visualization for PyTorch.

MeltyGUI is an open source data visualization toolkit for PyTorch, designed to render massive tensors in-place on the GPU at an interactive framerate. The purpose of this project is to make PyTorch code easier to understand and faster to debug. Along with its core tensor rendering components, MeltyGUI is bundled with a Blender inspired UI framework designed to make interacting with high dimensional tensors feel fast and intuitive. 

MeltyGUI is capable of rendering tensors exceeding 64GB in size. The primary challenge with visualizing data this large is moving it around. MeltyGUI skips the data movement entirely, and ray-marches tensors directly from CUDA memory.

<img width="2410" height="2018" alt="Image" src="https://github.com/user-attachments/assets/82ffae2c-ec0a-456c-b781-5428c54009f8" />

## Getting started

MeltyGUI is not yet published to PyPI. For now, install from a checkout using
[the setup guide](docs/DEVELOPMENT.md#setup).

The current setup targets Linux x86-64 with Python 3.11, 3.12 or 3.13 and a
working OpenGL 4.3 context. CUDA tensor rendering also requires PyTorch, an
NVIDIA GPU, a CUDA toolkit, and MeltyGUI's GL-enabled CUDA binding. The native
bindings install as prebuilt wheels on those targets; elsewhere installers
compile them, see [when no wheel matches](docs/SUPPORT_WHEELS.md#when-no-wheel-matches).

## Visualize a tensor

Create a small volume on the GPU and tag `draw_voxels` as an OS window with
`glfw_window`:

```python
import torch
import meltygui

axis = torch.linspace(-1.5, 1.5, 40, device="cuda:0")
x, y, z = torch.meshgrid(axis, axis, axis, indexing="ij")
volume = torch.exp(-4 * ((torch.sqrt(x * x + y * y) - 0.85) ** 2 + z * z))

meltygui.glfw_window(
    meltygui.draw_voxels, name="Torus", value=volume, width=850, height=700
)
```

Save this as `tensor_demo.py` and run it with the Python from your configured
environment:

```sh
.venv/bin/python tensor_demo.py
```

`glfw_window` turns a view into a native window: `name` is the window title,
`width` / `height` its content size, `value` the view's input, and every other
keyword argument goes to the view. The window loop starts automatically after
the module finishes defining its windows. The CUDA renderer reads the volume on
its own GPU and transfers the rendered 2D image for display.

To compose several views in one window, decorate your own function instead. It
runs each frame, so create the tensor outside it to avoid reallocating the
volume on every frame:

```python
@meltygui.glfw_window(name="CUDA tensor", width=850, height=700)
def draw_frame():
    # Interactive widget, rendered in a loop. This function will be called wheneven
    # the rendered image is invalidated
    meltygui.draw_voxels(volume, name="Torus", width=800, height=630)
```

`draw_voxels` chooses CUDA for CUDA tensors and OpenGL for CPU tensors, NumPy
arrays and GL textures. Select a renderer explicitly with the framework's
`view_func` override (or call either renderer directly):

```python
meltygui.draw_voxels(volume, view_func=meltygui.draw_voxels_opengl)
meltygui.draw_voxels(volume, view_func=meltygui.draw_voxels_cuda)
```

`draw_voxels_opengl` accepts tensors directly, including CUDA tensors; it slices
and uploads the displayed volume to a GL texture. `draw_voxels_cuda` requires a
CUDA tensor and reads it in place. Both expose the same camera, mapping and
shading controls. Backend selection no longer uses the obsolete `cuda_march`
parameter.

See [the tensor and live-code example](examples/tensor_live.py) for a tensor
viewer alongside an editable function view.

## Build an app

Use the bundled UI framework to add controls, editors, and additional windows.
[Building an app](docs/APPS.md) covers persistent view state, render functions,
and window lifecycle.

For framework design, feature ownership and render-function patterns, see
[Contributing](CONTRIBUTING.md). [Development](docs/DEVELOPMENT.md) covers setup,
checks and packaging; [Architecture gaps](docs/ARCHITECTURE_DEBT.md) tracks the
remaining migration work.

## License

MeltyGUI is licensed under [Apache 2.0](LICENSE). Bundled third-party code and
assets retain their own licenses. The separate commercial IDE is not part of
this distribution.
