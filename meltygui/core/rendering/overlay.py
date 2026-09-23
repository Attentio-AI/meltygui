"""Uncached, window-masked render-function overlays.

Callbacks are plain functions accepting any subset of input_value, draw_state
and draw_list as keyword arguments. Draw into the supplied foreground list;
normal widgets/render_func calls belong in the body. Keep callbacks below 0.5ms.
"""
import inspect
import time

import meltygui_imgui as imgui

from meltygui.core.melty import Melty
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.runtime.toggles import Tint
from meltygui.hdr_color import pack_color

# Change the shared budget here, rather than giving individual views exemptions.
OVERLAY_BUDGET_SECONDS = 0.0005
_STATE_KEY = 'render_overlay'


@no_save('callback', 'owner', 'code', 'names', 'error')
class OverlayState(DictConversion):
    def __init__(self):
        super().__init__()
        self.callback = None
        self.owner = None
        self.code = None
        self.names = ()
        self.error = None


def draw_overlay(draw_state):
    """Run once at the owning view's post-body pass, including tile replays.

    A synchronous callback cannot be interrupted at the deadline. Its first
    overrun completes, but its geometry is discarded; subsequent frames show
    the error without calling it.
    Replacing or hotswapping the callback clears the failure automatically.
    """
    callback = (draw_state._kwargs or {}).get('draw_overlay')
    if callback is None:
        draw_state.misc.pop(_STATE_KEY, None)
        return
    if draw_state.closed or draw_state.just_shadow:
        return
    state = draw_state.misc.get(_STATE_KEY)
    if not isinstance(state, OverlayState):
        state = OverlayState()
        draw_state.misc[_STATE_KEY] = state
    draw_state.misc_used.add(_STATE_KEY)
    target = getattr(callback, '__func__', callback)
    owner = getattr(callback, '__self__', None)
    code = getattr(target, '__code__', None)
    if code is None:
        code = getattr(type(target).__call__, '__code__', None)
    if state.callback is not target or state.owner is not owner or state.code is not code:
        state.callback, state.owner, state.code = target, owner, code
        state.error = None
        try:
            if not callable(callback) or getattr(callback, '__render_func__', False):
                raise TypeError('draw_overlay must be a plain callable, not a render_func')
            params = inspect.signature(callback).parameters
            available = {'input_value', 'draw_state', 'draw_list'}
            state.names = tuple(available if any(p.kind == p.VAR_KEYWORD for p in params.values())
                                else available.intersection(params))
            inspect.signature(callback).bind(**dict.fromkeys(state.names))
        except (TypeError, ValueError) as error:
            state.error = str(error)

    draw_list = imgui.get_overlay_draw_list()
    clip = draw_state.abs_clip_rect
    if Melty._overlay_channels_active:
        draw_list.channels_set_current(Melty.overlay_channel_for(draw_state))
    if clip is not None:
        draw_list.push_clip_rect(*clip, True)
    try:
        if state.error is None:
            values = {'input_value': draw_state._raw_input_value,
                      'draw_state': draw_state, 'draw_list': draw_list}
            arguments = {name: values[name] for name in state.names}
            vertex_start = draw_list.vtx_buffer_size
            started = time.perf_counter()
            try:
                callback(**arguments)
            except Exception as error:
                state.error = f'{type(error).__name__}: {error}'
            elapsed = time.perf_counter() - started
            if state.error is None and elapsed > OVERLAY_BUDGET_SECONDS:
                state.error = f'{elapsed * 1000:.3f} ms exceeds 0.5 ms budget'
            if state.error is not None:
                discard_geometry(draw_list, vertex_start)
        if state.error is not None:
            color = pack_color(*Tint.dd_text(draw_state.current_tint), 1.0)
            draw_list.add_text(draw_state._abs_left() + 4,
                               draw_state._abs_top() + draw_state.header_height + 4,
                               color, f'Overlay disabled: {state.error}')
    finally:
        if clip is not None:
            draw_list.pop_clip_rect()
        if Melty._overlay_channels_active:
            draw_list.channels_set_current(Melty.max_layer - 1)


def discard_geometry(draw_list, vertex_start):
    """Degenerate only this callback's vertices, preserving shared list offsets.

    The binding exposes buffers but no truncate operation. Zeroing complete
    vertices uses its published stride, including HDR colors, without relying
    on a C struct layout. Indices/commands remain valid; prior overlays survive.
    """
    import ctypes
    count = draw_list.vtx_buffer_size - vertex_start
    if count > 0:
        ctypes.memset(draw_list.vtx_buffer_data + vertex_start * imgui.VERTEX_SIZE,
                      0, count * imgui.VERTEX_SIZE)


def finish_cached_overlays(cache, ctx):
    """Replay descendants only when the cache skipped their wrapper calls."""
    if ctx.drew_cached:
        tile = cache._tiles.get(ctx.key)
        ctx.overlay_views = tile.overlay_views if tile is not None else ()
        for child in ctx.overlay_views:
            draw_overlay(child)
    if cache._stack:
        cache._stack[-1].overlay_views += ctx.overlay_views
    finish_overlay(ctx.draw_state, cache)


def finish_overlay(draw_state, cache):
    """Uncached children still register for replay by a cached ancestor."""
    draw_overlay(draw_state)
    if (draw_state._kwargs or {}).get('draw_overlay') is not None and cache.enabled and cache._stack:
        cache._stack[-1].overlay_views += (draw_state,)
