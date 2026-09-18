"""View identity: the stable `unique` of a view call, its DrawState, its tile
id and its place in the render tree.

The @render_func wrapper (core_render) builds all four through the functions
here, and so does a view that skips the wrapper (an immediate-mode "fast"
view that still wants its own DrawState): the same inputs give the same
`unique` either way, so a view can move between the two paths and keep its
persisted draw_state.

The order a view runs them in:

    unique, suffix = view_unique(name, func.__name__, key=key)
    draw_state = get_draw_state(unique)
    draw_state._tile_id = view_tile_id(name, unique, draw_state)
    place_in_parent_window(draw_state)     # parent_window + left/top_offset
    link_parent(draw_state)                # _parent + the parent's child index

The wrapper pushes `unique` on Melty.unique_stack and the draw_state on
Melty.draw_state_stack around the body, which is what nests the next level's
identity under this one; a fast view with children does the same.
"""
import zlib

import meltygui_imgui as imgui

from meltygui.core.melty import Melty
from meltygui.state.new_core_model import DrawState
# Imported as a module (not `from ... import DragDrop`) so hotswapping
# drag_drop_core keeps this module on the live class.
import meltygui.core.input.drag_drop_core as _drag_drop


def strhash(s: str) -> int:
    """Stable 32-bit hash of a string."""
    return zlib.crc32(s.encode("utf-8")) & 0xffffffff


def combine(h: int, s: str) -> int:
    """Order-sensitive, stable combine (FNV-style)."""
    return ((h * 16777619) ^ strhash(s)) & 0xffffffff


_UI_ID_MEMO = globals().get("_UI_ID_MEMO", {})    # (suffix, idx) → stable id


def ui_id(suffix=None, idx=0) -> int:
    """Stable UI ID from a suffix string + index. Pure function of
    (suffix, idx) - memoized: every view call builds its suffix string and
    hashed it twice (crc32 + str round trip)."""
    memo_key = (suffix, idx)
    hit = _UI_ID_MEMO.get(memo_key)
    if hit is not None:
        return hit
    h = combine(0, "")
    suffix_int = strhash(str(suffix))

    unique = h if suffix is None else (((h * 16777619) ^ suffix_int) + (idx + 1))
    unique = strhash(str(unique))

    if len(_UI_ID_MEMO) > 65536:
        _UI_ID_MEMO.clear()     # bounded: dynamic names (table-row ids) churn
    _UI_ID_MEMO[memo_key] = unique
    return unique


def _root_window_name():
    """Name of the melty window the current view hashes its identity under."""
    if len(Melty.melty_window_stack) == 0:
        return "Root"
    window = Melty.melty_window_stack[-1]
    # The floating dragged item renders as its own window, so its
    # descendants would hash the item's name here instead of the
    # home window's - fresh uniques, fresh draw_states, expanded/
    # scroll state gone every pickup. Keep the inline identity:
    # while the item floats, its subtree hashes the home window.
    if (_drag_drop.DragDrop.active
            and window is _drag_drop.DragDrop.item_ds
            and window.parent_window is not None):
        return window.parent_window.name
    return window.name


def view_unique(name, func_name, key="", unique_name=None, old_suffix=None):
    """(unique, suffix) of one view call: a stable hash of where it sits -
    the enclosing view's unique (Melty.unique_stack), the enclosing melty
    window, its name / key and the function drawing it. A root-level call
    (Melty.depth == 0) hashes name + key + function only.

    unique_name: hashed in place of nothing - an extra discriminator that
    defaults to `name`. old_suffix: the caller's `suffix` kwarg. An int key
    is the row index, folded in as ui_id's idx."""
    if unique_name is None:
        unique_name = name
    if Melty.depth == 0:
        unique = ui_id(suffix=name + unique_name + str(key) + func_name)
        return unique, f"{unique_name}_{func_name}_{unique}_{key}"

    index = key if isinstance(key, int) else 0
    suffix = Melty.unique_stack[-1] if len(Melty.unique_stack) > 0 else (name or "")
    # Keep original behavior of always appending name (even if empty)
    suffix = f"{old_suffix}_{suffix}_{unique_name}_{key}"
    unique = ui_id(suffix=suffix + unique_name +
                          name + _root_window_name() +
                          str(key) + func_name, idx=index)
    return unique, suffix


_headless_draw_state_registry = globals().get("_headless_draw_state_registry", {})


def get_draw_state(unique: int) -> DrawState:
    """Get or create the DrawState for a view's unique."""
    registry = None
    if Melty.vis is not None:
        registry = Melty.vis.root.draw_state_registry
    elif Melty.draw_state_registry is not None:
        registry = Melty.draw_state_registry

    if registry is None:
        # Headless / background thread - use a module-level fallback
        registry = _headless_draw_state_registry
        if Melty.draw_state_registry is None:
            Melty.draw_state_registry = registry

    if unique not in registry or registry[unique] is None:
        registry[unique] = DrawState()
        registry[unique].unique = unique

    registry[unique].dlt_count = Melty.save_draw_state_for
    return registry[unique]


def view_tile_id(name, unique, draw_state):
    """The view's tile-cache key (also its event-subscription prefix and its
    registered_windows key)."""
    return f"{name}##{strhash(str(unique) + str(draw_state.id))}"


def place_in_parent_window(draw_state, parent_window=None, left=None, view_offset=True):
    """Resolve draw_state.parent_window and stamp left_offset / top_offset
    from the imgui cursor. No-op at the true root (no melty window, no
    enclosing view, no explicit parent). Never call on a deferred re-entry:
    Melty.draw parks the cursor at the window's abs position, which already
    carries window_pos.

    parent_window: an explicit parent makes the view nested even with no
    melty window on the stack - a host that draws its views straight into
    the root imgui window (the hdr-viewer) still gets popovers placed
    relative to their spawner. left: overrides the cursor x. view_offset=
    False: the view is not positioned by the cursor (offset = (left, 0))."""
    enclosing_view = Melty.draw_state_stack[-1] if Melty.draw_state_stack else None
    if not (Melty.melty_window_stack or parent_window is not None
            or enclosing_view is not None):
        return
    if parent_window is None:
        if Melty.melty_window_stack:
            parent_window = Melty.melty_window_stack[-1]
        else:
            # Plain render-function hosts also own positioned children.
            # Their text, hit regions and clipping must use the same
            # cursor origin as the child backgrounds.
            parent_window = enclosing_view.parent_window or enclosing_view
    draw_state.parent_window = parent_window

    # Store left/top_offset as the UNSCROLLED position relative to
    # parent_window's content (cursor pos already reflects ancestor
    # scroll, so add it back). _abs_left subtracts the live ancestor
    # scroll, making abs_left react to mid-frame scroll deltas instead
    # of waiting for this view to re-render with a new cursor pos.
    if not view_offset:
        draw_state.left_offset, draw_state.top_offset = (left if left is not None else 0, 0)
        return
    anc_sx, anc_sy = draw_state._ancestor_scroll()
    cursor_x, cursor_y = imgui.get_cursor_screen_pos()
    if left is None:
        left = cursor_x
    draw_state.left_offset, draw_state.top_offset = (
        left - parent_window.abs_left + anc_sx,
        cursor_y - parent_window.abs_top + anc_sy)


def link_parent(draw_state):
    """Point draw_state._parent at the enclosing view (top of
    Melty.draw_state_stack) and self-register in that parent's child index.

    draw_collection used to be the only view with children (keyed by
    collection idx), so every other container had an empty _children and
    children_in_clip found nothing. Doing it here - at the one place the
    render-tree parent is assigned - populates it for every view. Keyed by
    id(): draw_states are reused from the registry, so a view's id is stable
    across frames, and a re-render always overwrites its own entry. Stale
    _parented entries are removed at read time (children_in_clip drops any
    whose _parent is no longer this DS)."""
    if len(Melty.draw_state_stack) > 0:
        draw_state._parent = Melty.draw_state_stack[-1]
    parent = draw_state._parent
    if parent is not None and parent.id != draw_state.id:
        if id(draw_state) not in parent._view_children:
            parent._view_children[id(draw_state)] = draw_state
