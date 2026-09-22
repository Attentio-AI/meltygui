#!/usr/bin/env python3
"""Every route a volume (or its image) takes to the screen, side by side.

draw_voxels / draw_voxels_cuda / draw_voxels_opengl are called DIRECTLY on
plain tensors: no live view, no RenderHost, no project process. One inline
view per case; the line above each shows the route it ACTUALLY took, read
back from the view's gl_state after the call, so a silent fallback (interop ->
pinned host, interop -> CPU upload) is visible.

    case                      tensor on        march     what crosses
    cuda / peer image         another GPU      CUDA      image, peer copy into a GL buffer
    cuda / device image       display GPU      CUDA      image, device copy into a GL buffer
    cuda / pinned image       another GPU      CUDA      image, pinned host + glTexSubImage2D
    opengl / cuda-gl interop  display GPU      OpenGL    volume, device copy into a 3-D texture
    opengl / host upload      CPU              OpenGL    volume, glTexImage3D from client memory
    auto                      another GPU      (picked)  draw_voxels chooses: must be the CUDA march
    opengl / cross-gpu        another GPU      OpenGL    the WHOLE volume (setting, off by default:
                                                         docs/TENSOR_RENDERING_REQUIREMENTS.md forbids it)

Device indices are this process's CUDA ordinals (torch and PyCUDA agree inside
one process); they are NOT nvidia-smi's. The display GPU is whatever
cuGLGetDevices reports for the window's GL context.

    python -m meltygui.examples.voxel_transfer_paths
"""
import math

import meltygui_imgui as imgui
from meltygui import draw_voxels, draw_voxels_cuda, draw_voxels_opengl, glfw_window
from meltygui.core.core_render import render_func
from meltygui.core.runtime.toggles import Toggles

SETTINGS = {
    'size': 96,                 # volume edge, voxels
    'animate': False,           # in-place writes: bumps _version, re-uploads / re-bakes every frame
    'cross_gpu_opengl': False,  # the forbidden route, for reproducing what it does
}

# Module level so a hotswap re-exec keeps the tensors (and their identity,
# which is the views' cache key).
cases = []
built_for = None
frame = 0


def _fill(volume, grid, phase):
    """A breathing torus written IN PLACE, so the tensor keeps its identity."""
    import torch
    z, y, x = grid
    ring = torch.sqrt(x * x + y * y) - 0.55 - 0.1 * math.sin(phase)
    volume.copy_(torch.exp(-(ring * ring + z * z) * 40.0))


def _make(device, size):
    import torch
    axis = torch.linspace(-1.0, 1.0, size, device=device)
    grid = torch.meshgrid(axis, axis, axis, indexing='ij')
    volume = torch.empty(size, size, size, dtype=torch.float16, device=device)
    _fill(volume, grid, 0.0)
    return volume, grid


def _display_device():
    """CUDA ordinal behind this window's GL context, or None."""
    try:
        from meltygui.core.graphics.cuda_interop_core import gl_devices
        devices = gl_devices()
        return int(devices[0]) if devices else None
    except Exception:
        return None


def _build(size, cross_gpu):
    import torch
    count = torch.cuda.device_count() if torch.cuda.is_available() else 0
    display = _display_device()
    # Prefer the GPU with the most memory as "the tensor's GPU" (the H100 here).
    others = sorted((d for d in range(count) if d != display),
                    key=lambda d: -torch.cuda.get_device_properties(d).total_memory)
    other = others[0] if others else None
    sources = {}

    def source(device):
        if device not in sources:
            sources[device] = _make(device, size)
        return sources[device]

    def where(device):
        return f"cuda:{device} {torch.cuda.get_device_name(device)}"

    built = []

    def case(name, view, device, interop=True, expect=()):
        volume, grid = source('cpu' if device is None else f'cuda:{device}')
        built.append(dict(name=name, view=view, volume=volume, grid=grid, interop=interop,
                          where='cpu' if device is None else where(device),
                          expect=expect, route='not drawn yet'))

    if other is not None:
        case('cuda / peer image', draw_voxels_cuda, other, expect=('cuda_view', 'cuda_image_interop'))
        case('cuda / pinned image', draw_voxels_cuda, other, interop=False,
             expect=('cuda_view', 'cuda_image', 'cuda_host'))
        case('auto', draw_voxels, other, expect=('cuda_view',))
        if cross_gpu:
            case('opengl / cross-gpu (forbidden)', draw_voxels_opengl, other, expect=('volume_cuda',))
    if display is not None:
        case('cuda / device image', draw_voxels_cuda, display, expect=('cuda_view', 'cuda_image_interop'))
        case('opengl / cuda-gl interop', draw_voxels_opengl, display, expect=('volume_cuda',))
    case('opengl / host upload', draw_voxels_opengl, None, expect=('volume',))
    return built, display


COLUMNS = 3
VIEW_SIZE = (480, 400)
ROUTE_KEYS = ('cuda_view', 'cuda_image_interop', 'cuda_image', 'cuda_host', 'volume_cuda', 'volume')


def _route(state, expect):
    """The resources the view holds after this frame, i.e. the route it took."""
    misc = getattr(state, 'misc', None) or {}
    resources = misc.get('gl_state')
    if resources is None:
        return 'no gl_state'
    held = [key for key in ROUTE_KEYS if resources.peek(key) is not None]
    text = ' + '.join(held) or 'nothing uploaded'
    missing = [key for key in expect if key not in held]
    if missing:
        text += f"   UNEXPECTED: no {', '.join(missing)}"
    error = getattr(misc.get('voxel_state'), 'cuda_error', None)
    if error:
        text += f"   {error.splitlines()[0]}"
    return text


@glfw_window(name='Voxel transfer paths', app_id='meltygui-voxel-transfer-paths',
             width=1500, height=980, settings=SETTINGS)
@render_func(use_cache=False)
def voxel_transfer_paths(input_value=None, draw_state=None):
    global cases, built_for, frame
    try:
        import torch  # noqa: F401
    except ImportError:
        imgui.text("needs torch: pip install meltygui[tensor]")
        return False, input_value
    wanted = (int(SETTINGS['size']), bool(SETTINGS['cross_gpu_opengl']))
    if built_for is None or built_for[:2] != wanted:
        cases, display = _build(*wanted)
        built_for = wanted + (display,)
    frame += 1
    display = built_for[2]
    imgui.text("display GPU: " + ("none reachable from CUDA (every image rides the pinned host buffer)"
                                  if display is None else f"cuda:{display}"))

    if SETTINGS['animate']:
        # One write per SOURCE tensor; cases share them per device.
        for volume, grid in {id(e['volume']): (e['volume'], e['grid']) for e in cases}.values():
            _fill(volume, grid, frame * 0.05)

    previous = Toggles.Voxels.cuda_image_interop
    try:
        for i, entry in enumerate(cases):
            if i % COLUMNS:
                imgui.same_line()
            imgui.begin_group()
            imgui.text(f"{entry['name']}  -  {entry['where']}")
            if 'UNEXPECTED' in entry['route'] or 'failed' in entry['route']:
                imgui.text_colored(entry['route'], 1.0, 0.45, 0.40, 1.0)
            else:
                imgui.text(entry['route'])
            # The views are drawn INLINE (not as_window: a Melty window is drawn
            # in the deferred layer pass, after this loop). The toggle is read
            # inside the view, so flipping it around the call runs both image
            # routes in the same frame.
            Toggles.Voxels.cuda_image_interop = entry['interop']
            _, _, state = draw_voxels(
                entry['volume'], view_func=entry['view'], name=entry['name'],
                width=VIEW_SIZE[0], height=VIEW_SIZE[1],
                dim_names=('z', 'y', 'x'), x_dim=2, y_dim=1, z_dim=0,
                step_size=0.004, max_steps=512,
                # animate: the view's cache would otherwise skip the re-render
                use_cache=not SETTINGS['animate'], return_extras=True)
            entry['route'] = _route(state, entry['expect'])
            imgui.end_group()
    finally:
        Toggles.Voxels.cuda_image_interop = previous
    return False, input_value
