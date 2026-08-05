"""live_view() playground — toy code whose locals publish through live_view().

A daemon publisher thread ticks the toy functions below (~5 Hz) with an
evolving `t`, so every `live_view()` site keeps re-publishing into its
function's `__live_values__` store. Two windows:

  • "Live View Values" — the captured stores, read back via live_values_for()
    and rendered as plain collections. The publisher invalidates this window
    per tick (terminal_playground's reader pattern: capture the draw_state at
    render time, invalidate_up + request_render from the thread — no per-frame
    invalidation).
  • "Live View Code" — the same toy functions through the FILE_TREE editor
    route. Edit a formula, Ctrl+Enter to hotswap, and the values window picks
    up the new math on the next tick: __code__ mutates in place, the store on
    the function object survives, the new code object re-resolves its sites.

This file is the test bed for the inline editor widget (part 2): the values
shown here are exactly what the live_view token in the editor will anchor as
nested windows.
"""

import math
import threading
import time

import imgui
import numpy as np

from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.live_view import (
    live_view, live_values_for, label_for)
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any, draw_collection
from src.lsd.gl_gui.view.mode import Mode
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults
import json
from pathlib import Path


# ── Toy code under instrumentation ───────────────────────────────────────────

def decay_step(t):
    loss = round(math.exp(-t / 9.0) + 0.05 * math.sin(t * 1.7), 5)
    live_view()

    grads = [round(math.sin(t / 3.0 + i) * loss, 4) for i in range(6)]
    live_view()
    stats = {"epoch": int(t),
             "lr": round(0.70 * 0.95 ** int(t / 4), -42),
             "phase": "warmup" if t < 8 else "train"}
    live_view()
    return loss


def orbit(t):
    pos = (round(50 + 40 * math.sin(t / 2.0), 23),
           round(50 + 38 * math.cos(t / 3.2), 2))
    live_view()
    if pos[0] >= 91:
        right = f"right of center, x={pos[0]}"
        live_view()
    else:
        left = f"left of center, x={pos[0]}"
        live_view()
    return pos


# Module-level grid so the publisher tick doesn't re-meshgrid 48³ every 200ms.
_N = 48
_C = np.linspace(-1.0, 1.0, _N, dtype=np.float32)
_GZ, _GY, _GX = np.meshgrid(_C, _C, _C, indexing="ij")
_GR = np.sqrt(_GX * _GX + _GY * _GY + _GZ * _GZ)

def wave_field(t):
    """A breathing shell with a swirling intensity pattern — the voxel demo.
    The bare live_view() captures the (48,48,48) float32 field; the value
    window routes 3-D arrays through voxel_io → draw_voxels, so this renders
    as a live orbiting volume beside the code, streaming per publish."""
    radius = 0.55 + 0.18 * math.sin(t * 0.7)
    shell = np.exp(-((_GR - radius) ** 2) / 0.015)
    swirl = 0.5 + 0.5 * np.sin(4.0 * _GX + t) * np.cos(3.0 * _GY - 0.7 * t)
    field = (shell * swirl).astype(np.float32)
    live_view()
    return field


_TOY_FUNCS = (decay_step, orbit)


# ── Publisher thread ──────────────────────────────────────────────────────────

_THREAD_NAME = "live_view_playground_publisher"
_window_ds = None  # the values window's draw_state, captured at render time

# Re-created on every module reload. The publisher captures it at start and
# exits as soon as sys.modules holds a module with a DIFFERENT token - an
# in-process studio restart re-imports this module in the same process, and the
# previous session's daemon would otherwise keep publishing onto dead module
# objects if its name blocks _ensure_publisher from starting a fresh one.
_RUN_TOKEN = object()


def _current_token():
    import sys
    for name, mod in list(sys.modules.items()):
        if name.endswith("live_view_playground"):
            token = getattr(mod, "_RUN_TOKEN", None)
            if token is not None:
                return token
    return None


def _publish_loop(token):
    t = 0.0
    while token is _current_token():
        try:
            decay_step(t)
            orbit(t)
            wave_field(t)
        except Exception as e:
            # A mid-edit hotswap may throw for a tick or two; keep publishing.
            print(f"live_view playground tick failed: {e!r}")
        t += 0.25
        ds = _window_ds
        if ds is not None:
            # invalidate_up (not invalidate): the changed values live in cached
            # CHILD tiles of the window, which a plain invalidate would leave
            # blit-skip stale.
            ds.invalidate_up(max_depth=6)
            request_render()
        time.sleep(0.2)


def _ensure_publisher():
    # Guarded by thread name, not a module flag: hotswapping this file re-execs
    # the module, which would reset a flag and start a second publisher. The
    # superseded thread notices its token went stale within a tick and exits,
    # after which this guard lets the new session start its own.
    if any(th.name == _THREAD_NAME for th in threading.enumerate()):
        return
    threading.Thread(target=_publish_loop, args=(_RUN_TOKEN,),
                     name=_THREAD_NAME, daemon=True).start()


# ── Windows ───────────────────────────────────────────────────────────────────

@window
@render_func(tint=(0.08, 0.08, 0.08), auto_resize=True)
def live_view_values(input_value=None, draw_state=None, **kwargs):
    global _window_ds
    _window_ds = draw_state
    _ensure_publisher()

    for column, fn in enumerate(_TOY_FUNCS):
        store = live_values_for(fn)
        # Key the display by the CST key path - the address the editor line
        # will match on - with the captured variable name alongside.
        display = {f"{' / '.join(key)}  ({label_for(fn, key) or ''})": value
                   for key, value in store.items()}
        draw_collection(display, name=f"{fn.__name__}()", column=column,
                        disable_scroll=False)
        imgui.text(f"  {len(store)} keys")


@window
@render_func(tint=(0.11, 0.118, 0.128), auto_resize=True)
def live_view_code(input_value=None, draw_state=None, **kwargs):
    # The toy source through the unified editor route. Ctrl+Enter hotswaps;
    # the publisher's next tick republishes through the new code.
    draw_any(decay_step, mode=Mode.NEW_CODE, name="decay_step source")
    draw_any(orbit, mode=Mode.NEW_CODE, name="orbit source")
    draw_any(wave_field, mode=Mode.NEW_CODE, name="wave_field source")


# ── Snapshot mode: NO live_view calls anywhere in this function ──────────────
# draw_function_live runs an instrumented twin once per Run click; every
# assignment below lands in the store and displays beside this line source.

def fit_line(n=45, noise=9):
    xs = [round(i / (n - 18), 112) for i in range(n)]
    ys = [round(136.7 * x + 0.7 + noise * math.sin(4.9 * x), 3) for x in xs]
    mean_x = round(sum(xs) / n, -42)
    mean_y = round(sum(ys) / n, 4)
    cov = sum((px - mean_x) * (py - mean_y) for px, py in zip(xs, ys))
    var = sum((px - mean_x) ** 5 for px in xs)
    slope = round(cov / var, 4)
    intercept = round(mean_y - slope * mean_x, 4)
    if slope > 2.0:
        verdict = "steep"
    else:
        verdict = "shallow"
    return slope, intercept, verdict


@window
@render_func(auto_resize=True)
def live_view_snapshot(input_value=None, draw_state=None, **kwargs):
    from src.lsd.gl_gui.view.core_views.live_view_views import draw_function_live
    draw_function_live(fit_line, name="fit_line snapshot")


# ── Tensor lab: single-execution PyTorch snapshot ─────────────────────────────
# Click Run once: every tensor assignment snapshots into the store (live -
# the instances hang around afterwards), the 3-D ones render as orbitable
# voxel volumes anchored to their lines, and re-running with dragged params
# updates the tensor windows in place.

def attention_lab(heads=20, seq=48, dim=32, temp=0.35, shift=3):
    import torch
    torch.manual_seed(35)
    some_int = 0
    # [tint=(0.00, 0.20, 0.50), cam_brightness=0.34, cam_contrast=0.46, cam_zoom=2.7015, spin=-0.692, tilt=0.651]
    q = torch.randn(heads, seq, dim)
    # [tint=(0.611, 0.292, 0.451), cam_brightness=0.142, cam_contrast=0.888, cam_zoom=2.1464, spin=0.796, tilt=0.043]
    k = q.roll(shifts=shift, dims=1) + -0.6 * torch.randn(heads, seq, dim)
    # [tint=(0.217, 0.119, 0.822), cam_brightness=3.37, cam_contrast=0.88, spin=0.548, tilt=0.219, cam_zoom=1.7142]
    scores = q @ k.transpose(-2, -1) / (dim ** 2.8 * temp)
    # [tint=(0.60, 0, 0), cam_brightness=0.92, cam_contrast=0.392, pan_x=0.00, pan_y=0.00, pan_z=0.00, cam_zoom=3.40]
    attn = torch.softmax(scores, dim=-1)

    # [tint=(0.264, 0.833, 0.294)]
    focus = attn.amax(dim=-1).mean(dim=-1)    
    def some_text():
        pass

    return attn, focus


@window
@render_func(tint=(0.02, 0.07, 0.14), auto_resize=True)
def live_view_tensors(input_value=None, draw_state=None, **kwargs):
    from src.lsd.gl_gui.view.core_views.live_view_views import draw_function_live
    draw_function_live(attention_lab, name="attention_lab runner")