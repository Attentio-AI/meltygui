"""Uncached, window-masked render-function overlays.

Callbacks are plain functions accepting any subset of input_value, draw_state
and draw_list as keyword arguments. Draw into the supplied foreground list;
normal widgets/render_func calls belong in the body. Keep callbacks below 0.5ms.
Optional draw_overlay_background callbacks place/paint cached child bodies before
descendant overlays; draw_overlay paints the owning view's foreground afterward.
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
    """Paint the owning view's foreground after its descendants."""
    _run_overlay(draw_state, 'draw_overlay', _STATE_KEY)


def draw_overlay_background(draw_state):
    """Prepare live child bounds and backing pixels before descendant overlays."""
    if (draw_state._kwargs or {}).get('draw_overlay_background') is not None:
        _run_overlay(draw_state, 'draw_overlay_background', 'render_overlay_background')


def _run_overlay(draw_state, option, state_key):
    """Run a bounded callback, retrying failures when its definition changes."""
    callback = (draw_state._kwargs or {}).get(option)
    if callback is None:
        draw_state.misc.pop(state_key, None)
        return
    if draw_state.closed or draw_state.just_shadow:
        return
    state = draw_state.misc.get(state_key)
    if not isinstance(state, OverlayState):
        state = OverlayState()
        draw_state.misc[state_key] = state
    draw_state.misc_used.add(state_key)
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
                raise TypeError(f'{option} must be a plain callable, not a render_func')
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
    """Prepare parents first, then paint descendants and owning foregrounds."""
    draw_overlay_background(ctx.draw_state)
    if ctx.drew_cached:
        tile = cache._tiles.get(ctx.key)
        ctx.overlay_views = tile.overlay_views if tile is not None else ()
        # Recorded postorder during normal drawing. Reverse it for layout so
        # each parent establishes live bounds before a descendant reads them.
        for child in reversed(ctx.overlay_views):
            draw_overlay_background(child)
        for child in ctx.overlay_views:
            draw_overlay(child)
    if cache._stack:
        cache._stack[-1].overlay_views += ctx.overlay_views
    finish_overlay(ctx.draw_state, cache, background_done=True)


def finish_overlay(draw_state, cache, *, background_done=False):
    """Uncached children still register for replay by a cached ancestor."""
    if not background_done:
        draw_overlay_background(draw_state)
    draw_overlay(draw_state)
    options = draw_state._kwargs or {}
    if (any(options.get(name) is not None for name in ('draw_overlay', 'draw_overlay_background'))
            and cache.enabled and cache._stack):
        cache._stack[-1].overlay_views += (draw_state,)


def place_overlay_view(draw_state, rect, clip):
    """Place a cached child whose lightweight layout is owned by an overlay.

    Use the same window-relative placement as normal rendering, without
    invalidating frozen pixels. Geometry writes still refresh geometry caches.
    """
    from meltygui.core.rendering.view_identity import place_in_parent_window
    x, y, width, height = rect
    cursor = imgui.get_cursor_screen_pos()
    silenced = Melty.silence_invalidate
    Melty.silence_invalidate = True
    try:
        imgui.set_cursor_screen_pos((x, y))
        place_in_parent_window(draw_state, parent_window=draw_state.parent_window)
        draw_state.width, draw_state.height = width, height
        draw_state.left, draw_state.top = x, y
        draw_state.clip_rect = clip
        parent = draw_state.parent_window
        draw_state._clip_win_anchor = (parent.abs_left, parent.abs_top) if parent is not None else None
    finally:
        Melty.silence_invalidate = silenced
        imgui.set_cursor_screen_pos(cursor)


def paint_cached_view(draw_state):
    """Paint a child's resident pixels at its live bounds without recapturing.

    An enclosing snapshot can contain empty space where the child's viewport
    used to end. Use the child's own preserved extent when overlay layout
    reveals that space again, rather than replaying the flattened parent there.
    Submit to the current window's main draw list, after its parent replay.
    The foreground overlay is composited AFTER shadows; putting an unshaded
    cache texture there erases the shadows on every cache-served frame.
    The cache retains ownership of the borrowed texture.
    """
    cache = Melty.cache
    tile = cache._tiles.get(draw_state._tile_id) if cache is not None and cache.enabled else None
    if tile is None or not tile.tex or tile.last_clean_frame < 0:
        return False
    from meltygui.core.cache.tile_cache import _tile_alloc
    width, height = tile.content_size or tile.size
    width, height = min(width, draw_state.width), min(height, draw_state.height)
    if width <= 0 or height <= 0:
        return False
    alloc_width, alloc_height = _tile_alloc(tile)
    left, top = draw_state.abs_left, draw_state.abs_top
    draw_list = imgui.get_window_draw_list()
    draw_list.push_clip_rect(*draw_state.abs_clip_rect, True)
    try:
        draw_list.add_image(tile.tex, (left, top), (left + width, top + height),
                            (0, 1), (width / alloc_width, 1 - height / alloc_height))
    finally:
        draw_list.pop_clip_rect()
    return True
