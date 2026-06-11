import inspect
import sys
import threading
import traceback
import types
from collections import deque, defaultdict
from collections.abc import MutableMapping
from enum import Enum
from inspect import Parameter
from math import sqrt
from pathlib import Path
from types import NoneType
from typing import Any

import OpenGL.GL as gl
import glfw
import numpy
import torch
from imgui.core import _DrawList

from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.global_style import GlobalStyle
from src.lsd.gl_gui.melty import Melty, CollectionAction, ManagedWindow, SearchTerm
from src.lsd.gl_gui.model.core_model.draw_state import ZoomState, TileMode, DrawState, TabState, DropDownState
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles, Tint, mix
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, push_style_var, \
    pop_style_var, end, begin
from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace, request_render
from src.lsd.gl_gui.view.core_conversion.bubbling import _BubblingDict, _DeepPath
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import Comment, GeneralParse, UsageRef, CallParse, \
    SymbolUsage, cst_module_to_dict, dict_to_cst_module
from src.lsd.gl_gui.view.core_conversion.new_codecs import CallSite
from src.lsd.gl_gui.view.core_conversion.new_converters import code_file_io, convert_in_and_out_value, \
    cst_module_to_string, string_to_cst_module, code_hosts_for, host_code_state, \
    recompile_button, recompile_status, run_recompile
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.codec_register import registry as FILE_CODECS
from src.lsd.gl_gui.view.core_views.core_render import render_func, render_func_kwarg_names
from src.lsd.gl_gui.view.core_views.core_undo import UndoManager
# Module import (not "from ... import DragDrop`) so hotswaps rebind cleanly.
from src.lsd.gl_gui.view.core_views import drag_drop as _drag_drop
from src.lsd.gl_gui.view.core_views.cst_proxy import *
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import hotkey, tint, Core
from src.lsd.gl_gui.view.core_views.decoration.invalidation_decoration import live
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.folders_proxy import FolderProxy
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_header_end, draw_footer, render_search, \
    annotation_item_type
from src.lsd.gl_gui.view.core_views.inspect_utils import set_fn_defaults
from src.lsd.gl_gui.view.core_views.tensor_views import draw_tensor
from src.lsd.gl_gui.view.core_views.text_editor import draw_text, _scroll_into_view
from src.shader_library.shader_manager.texture_manager import PendingTexture
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


@render_func(use_cache=True, show_bg=True, width=20, height=22, tile_mode=TileMode.MAX,
             auto_resize=False, just_shadow=True, selectable=False, no_cursor=True, temp=True)
def empty(input_val):
    pass


@render_func(is_default_for=(types.FrameType), use_cache=True, tint=(0.6, 0.2, 0.0),
             header_same_line=False, show_bg=False, align_header=False, closed=False,
             shadow=True, selectable=False, wrap=False, with_header=draw_header,
             indent_size=5, searchable=True, shaodw=False, bg_offset=3)
def draw_frame(input_value: types.FrameType, draw_state, **kwargs):
    file_name_truncated = Path(input_value.f_code.co_filename).name
    imgui.text(f"{file_name_truncated}:{input_value.f_lineno} in {input_value.f_code.co_name}")

    # Jump-to-error: open the frame's source file at the failing line. Same
    # threaded open_in_intellij pattern the jump-to-caller button uses.
    if button(f"{file_name_truncated}:{input_value.f_lineno}",
              height=30, value=0.4, saturation=1.5, name="jump_to_frame")[0]:
        from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
        threading.Thread(
            target=open_in_intellij,
            args=(str(input_value.f_code.co_filename),),
            kwargs={"line_number": input_value.f_lineno},
            daemon=True).start()
    # Loop over the frame's local variables, which are the most relevant to debugging.

    draw_text("Locals", name="Locals", show_header=False,
              font=Font.JETBRAINS_MONO_40, bg_offset=3)

    for var_name, var_value in input_value.f_locals.items():
        # Display the variable name and its value.
        imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] + 40, imgui.get_cursor_screen_pos()[1]))
        imgui.begin_group()
        draw_any(var_value, with_header=draw_header, show_header=True, name=var_name, mode=Modes.READ_ONLY)
        imgui.end_group()


@render_func(is_default_for=types.ModuleType, use_cache=True,
             show_bg=True, with_header=draw_header, with_footer=draw_footer)
def draw_module(input_value: types.ModuleType, draw_state, **kwargs):
    imgui.text(f"Module: {input_value.__name__}")


@render_func(is_default_for=(type), tint=(0.93, 0.56, 0.23, 0.308), use_cache=True,
             header_single_line=True, show_name=True, temp=True, is_tree=False, shadow=False,
             show_bg=True, with_header=draw_header)
def draw_type_name(input_value, **kwargs):
    try:
        imgui.text(f"{input_value.__name__}")
    except Exception as e:
        imgui.text(f"Error displaying type: {e}")


def _collection_match_keys(input_value, keys, excluded, show_excluded):
    """The (index, lowercased key string) pairs draw_collection renders and
    searches, in key order — the basis for both counting key matches and
    resolving which key holds the current match, without rendering. `index` is
    the position in `keys`, so it lines up with the render loop. Mirrors the
    loop's key-string derivation and skip filters."""
    out = []
    parent_cls_name = input_value.__class__.__name__
    excl_attrs = getattr(type(input_value), "__excluded_attrs__", None)
    for idx, key in enumerate(keys):
        if isinstance(key, (float, Enum, NoneType)):
            key_str = parent_cls_name
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)
        if str(key).split("##")[0] in excluded:
            continue
        if (not show_excluded and excl_attrs is not None
                and not Toggles.show_excluded and str(key) in excl_attrs):
            continue
        if not show_excluded and (key_str.startswith("_") or key_str.endswith("_")):
            continue
        out.append((idx, key_str.lower()))
    return out


def _fuzzy_substring_distance(q, k):
    """Min edit distance between `q` and any substring of `k` (the k-differences
    DP: row 0 is all zeros so the match may start anywhere in k). Damerau/OSA, so
    an adjacent transposition — the most common typo — costs 1, not 2. Both
    lowercase."""
    m, n = len(q), len(k)
    if m == 0:
        return 0
    prev2 = None
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        qi = q[i - 1]
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if qi == k[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and j > 1
                    and qi == k[j - 2] and q[i - 2] == k[j - 1]):
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        prev2 = prev
        prev = cur
    return min(prev)


def _fuzzy_key_match(q, k):
    """Does query `q` match candidate `k` (both lowercase), tolerating a few
    typos? Exact substring first (fast, also covers short queries); for longer
    queries fall back to approximate substring matching with a small edit budget
    that scales with length (~1 typo per 4 chars). The single predicate the key
    match's count, current index and highlight all share, so they stay in sync."""
    if not q:
        return False
    if q in k:
        return True
    if len(q) < 4:
        return False
    return _fuzzy_substring_distance(q, k) <= max(1, len(q) // 4)


def global_search_results(root, q, exclude=None, max_depth=30, limit=60):
    """Walk the live draw_state tree under `root` (draw_main's, via the same
    DrawState.descendants used elsewhere) and return the nodes whose display
    name matches `q`, best-first — the global-search result list.

    Exact substring hits rank ahead of fuzzy (typo) ones, and within a tier
    shorter names first, so the limit trims the long fuzzy tail rather than good
    matches. Skips shadow/blank nodes, dedupes by label, and skips the `exclude`
    subtree (the search window itself, so it doesn't match its own query)."""
    exclude_ids = set()
    if exclude is not None:
        exclude_ids = {id(exclude)} | {id(d) for d in exclude.descendants(max_depth=max_depth)}
    tol = max(1, len(q) // 4)
    scored = []
    seen = set()
    for ds in root.descendants(max_depth=max_depth):
        if getattr(ds, 'just_shadow', False) or id(ds) in exclude_ids:
            continue
        name = getattr(ds, 'name', None)
        if not name:
            continue
        label = str(name).split("##")[0].strip()
        if not label or not any(c.isalnum() for c in label):
            continue
        low = label.lower()
        if low in seen:
            continue
        if q in low:
            dist = 0
        elif len(q) >= 4:
            dist = _fuzzy_substring_distance(q, low)
            if dist > tol:
                continue
        else:
            continue
        seen.add(low)
        scored.append((dist, len(label), label, ds))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [(label, ds) for _, _, label, ds in scored[:limit]]


def go_to_search_result(ds, win=None):
    """Jump to a global-search result.

    If the result names a top-level window — i.e. it's a Dock/window-manager
    entry — launch that window directly (open + raise) instead of just scrolling
    the Dock to its row. Otherwise open the result's owning window and scroll the
    result into view (via the editor's _scroll_into_view, which walks up to the
    real scroll container) and focus it."""
    name = getattr(ds, 'name', None)
    target = Core.melty.find_window(name) if name else None
    if target is not None and target is not win:
        target.closed = False
        # Summon it (move + raise) to where the search is, so it comes to you
        # instead of opening at its old, maybe off-screen, spot.
        gs = Core.melty.find_window("GlobalSearch")
        if gs is not None and gs.abs_left is not None:
            Core.melty.summon_window(target, gs.abs_left, gs.abs_top)
        else:
            Core.melty.move_window_to_front(target)
        Core.melty.focused_ds = target
        # Sole-select it so it shows Melty's selection outline.
        Core.melty.selected = {target}
        Core.melty.last_selected = target
        request_render()
        return

    if win is not None:
        win.closed = False
        Core.melty.move_window_to_front(win)
    Core.melty.focused_ds = ds
    Core.melty.selected = {ds}
    Core.melty.last_selected = ds
    if ds.abs_top is not None and ds.height is not None:
        _scroll_into_view(ds, ds.abs_top, ds.abs_top + ds.height)
    request_render()


def _dismiss_global_search():
    """Close the GlobalSearch window and release the box's text focus — called
    after a result is activated (clicked or Enter), so picking a result also
    dismisses the search. Also clears the query, so the next open starts fresh
    (Esc, which doesn't call this, leaves the query for resuming)."""
    win = Core.melty.find_window("GlobalSearch")
    if win is not None:
        win.closed = True
    GlobalSearch.query = ""
    GlobalSearch._last_query = None
    GlobalSearch.selected = 0
    Core.melty.clear_focus()
    request_render()


def _draw_state_tint(ds):
    """The colour a draw_state renders with — its live render tint if it has
    one, else its declared tint. Used to colour search-result rows so they read
    at a glance."""
    if ds is None:
        return None
    return getattr(ds, 'current_tint', None) or getattr(ds, 'tint', None)


def _owning_window(ds, root):
    """The top-level window a result belongs to: walk up _parent until the next
    step would be `root` (draw_main), so we stop on root's direct child."""
    node = ds
    parent = getattr(node, '_parent', None)
    while parent is not None and parent is not root and parent is not node:
        node = parent
        parent = getattr(node, '_parent', None)
    return node


def group_results_by_window(results, root):
    """Group ranked results by their owning window, preserving order — so the
    window of the best match comes first and rows stay rank-ordered within it.
    The grouping pass in the dock-sort spirit, but for read-only display."""
    groups = {}
    for label, ds in results:
        groups.setdefault(_owning_window(ds, root), []).append((label, ds))
    return groups


def search_activate_target(node):
    """The draw_state Ctrl+Enter should 'click' for the current search match
    `node` (melty.search_current_node). When the match is one of a collection's
    keys, that's the child at the current key; for a leaf content match it's the
    node itself."""
    if node is None:
        return None
    key = getattr(node, '_search_current_key', None)
    if key is not None:
        child = node._children.get(key)
        if child is not None:
            return child
    return node


@render_func(use_cache=True, is_default_for=SymbolUsage)
def draw_symbol_usage(input_value):
    imgui.text(str(input_value))


@render_func(is_default_for=(dict, MutableMapping, defaultdict, tuple, list, GeneralParse, CallParse, _BubblingDict, _DeepPath), use_cache=True,
             header_same_line=False, show_bg=True, show_instance_vars=False, align_header=False,
             manual_content_height=True, shadow=True, selectable=False,
             wrap=False, with_header=draw_header, indent_size=2, searchable=True)
def draw_collection(input_value, draw_state, depth, style_manager, meta, icon=None,
                    mode=None, keys=None, get_attr=None, set_attr=None, show_excluded=False,
                    child_kwargs=None, show_bg=False, show_search=True, align_header=False, wrap=False,
                    on_collapse=False, search_text="", return_item=False, close_triggers_delete=False,
                    on_expand=False, show_add_delete=False, show_add_types=None, item_spacing_y=3, show_system=False,
                    included=None, horizontal=False, show_indices=False, excluded=None, annotation=None, **kwargs):
    """
    Universal collection renderer

    show_add_types={"Display Name": TypeA, ...} draws a second + button in the
    header that instantiates the chosen type (rendered by draw_header; the
    value just rides the kwargs through). Several entries get a chevron
    dropdown to pick from; a single entry binds the + directly with no
    chevron. A bare list of types is accepted and keyed by __name__.
    """
    if excluded is None:
        excluded = set()

    if included is None:
        included = set()

    if child_kwargs is None:
        child_kwargs = {}
    changed = False
    if hasattr(input_value, 'children') and isinstance(input_value.children, (list, dict, defaultdict,
                                                                              types.MappingProxyType, deque)):
        input_value = input_value.children

    if show_bg and draw_state.total_z_offset < 0:
        imgui.dummy(1, 3)
    else:
        imgui.dummy(1, 1)

    # --- configure per collection type ---
    collection = input_value
    # When the value *is* a class (e.g. an @window-registered class drawn
    # directly), its per-attribute `{field}_meta` overrides live on the class
    # itself, not on its metaclass. Use the class as parent_type so get_child_meta
    # can find them; otherwise fall back to the instance's class.
    parent_type = input_value if isinstance(input_value, type) else input_value.__class__
    if isinstance(input_value, (str, int, float, bool, Enum, NoneType)):
        imgui.text("No view for type: " + str(type(input_value)))
        return False, input_value
    if keys is None:
        if isinstance(input_value,
                      (dict, list, tuple, set, defaultdict, MutableMapping, types.MappingProxyType, _DeepPath, deque)):
            apply_change = True
            parent_type = input_value.__class__
            if isinstance(input_value, types.MappingProxyType):
                keys = input_value.keys()
            elif isinstance(input_value, (dict, defaultdict, MutableMapping, types.MappingProxyType)):
                keys = input_value.keys()
                collection = input_value
            else:
                keys = range(len(input_value))
                collection = list(input_value)

        elif hasattr(input_value, "__dict__") and depth < Core.melty.max_depth:
            if hasattr(type(input_value), "__field_defaults__") and hasattr(input_value, 'to_dict'):
                type(input_value).__field_defaults__.update(input_value.__dict__)
                # __field_defaults__ accumulates keys from every instance, so a
                # field deleted from THIS instance lingers there. Skip keys the
                # instance no longer resolves (neither set nor the class default)
                # so deleting a field removes its row instead of leaving a ghost.
                keys = [k for k in type(input_value).__field_defaults__
                        if k in input_value.__dict__ or hasattr(input_value, k)]
            else:
                if input_value is None or input_value.__dict__ is None:
                    return False, input_value
                keys = input_value.__dict__.keys()
            collection = input_value.__dict__
            use_tint = False
            apply_change = True
        else:
            imgui.text("No view for type: " + str(type(input_value)))
            return False, input_value

        keys = list(keys)[:]

    # --- per-key type annotations ---
    # Class-level annotations (walking the MRO) name a type per attribute when
    # rendering an object's __dict__; a typed container annotation
    # (Dict[str, Lora] / List[Lora]) covers every key otherwise. Each child
    # inherits its annotation so its own add button can instantiate the right
    # item type (replaces the old meta.field_type path).
    parent_annotations = {}
    for _klass in reversed(getattr(parent_type, "__mro__", ())):
        _anns = _klass.__dict__.get("__annotations__")
        if _anns:
            parent_annotations.update(_anns)
    item_annotation = annotation_item_type(annotation)

    # --- search (key matching) ---
    # Same dual resolution as the text editor: a forwarded SearchTerm carries
    # the shared cross-view session, or the owner's own session when this
    # collection hosts the find UI. We claim one slot per matching key, in
    # visual order interleaved with the children (claimed below), so the
    # combined next/prev sequence reads top-to-bottom. The current key is
    # latched so incidental repaints don't shift the highlight.
    _search_term = search_text or (draw_state.search_text if draw_state.search_active else "")
    if isinstance(_search_term, SearchTerm):
        search_session = _search_term
    elif draw_state.search_active and draw_state._search_session is not None:
        search_session = draw_state._search_session
    else:
        search_session = None
    search_q = str(_search_term).lower() if (search_session is not None and _search_term) else ""
    search_current_y = None  # screen-Y of the current key's row (for scroll)
    search_current_h = None
    # On a full-search frame (term change / nav) render every row — even ones
    # the off-screen optimization would skip — so the row holding the current
    # match is reached and can scroll into view.
    _search_full_render = search_session is not None and search_session.scroll_to

    # Stash a matcher so the search owner's tree walk (DrawState.descendants /
    # melty.search_walk) can count this collection's key matches without
    # rendering. It counts only this collection's own keys (the whole key list,
    # not just the rows the loop below draws); child collections / text editors
    # are separate tree nodes with their own matchers, so the walk sums them
    # without double-counting. Keys are re-derived lazily on call (only on
    # counting frames), so an unsearched render pays nothing for it.
    def _search_matcher(term, session, _iv=input_value, _keys=keys,
                        _excl=excluded, _se=show_excluded):
        q = str(term).lower()
        if not q:
            return
        mk = _collection_match_keys(_iv, _keys, _excl, _se)
        session.claim(sum(1 for _, k in mk if _fuzzy_key_match(q, k)))

    draw_state._search_matcher = _search_matcher

    # The owner's pre-body walk picked the global-current match and stashed its
    # local index on us (_search_active_local) when one of OUR keys holds it —
    # the same walk that produced the count, so selection and count agree.
    # Resolve that ordinal (over our matching keys, in key order) to the key
    # index the loop should highlight + scroll to. None when the current match
    # lives in a child instead (that child carries its own mark).
    _current_key_idx = None
    if search_q and draw_state._search_active_local is not None:
        _matching = [i for (i, k) in
                     _collection_match_keys(input_value, keys, excluded, show_excluded)
                     if _fuzzy_key_match(search_q, k)]
        if 0 <= draw_state._search_active_local < len(_matching):
            _current_key_idx = _matching[draw_state._search_active_local]

    # --- unified loop ---
    drew_any = False

    start_cursor = imgui.get_cursor_pos()[1]
    rect = Core.melty.get_clip_rect()

    premature_break = False

    Core.melty.collection_index_stack.append(0)
    this_collection = len(Core.melty.collection_index_stack) - 1

    max_items = 5000
    start_index = 0
    end_index = min(len(keys) - 1, max_items)

    scroll_offset = draw_state.scroll_offset
    true_left = draw_state.left - scroll_offset[0]
    true_top = draw_state.top - scroll_offset[1]

    # Remove excluded from keys
    item_to_return = None
    # Keys of children whose close (X) was clicked this frame — collected during the
    # loop and removed from the collection AFTER it (never mutate keys mid-iteration).
    to_delete = set()
    # Whether any row was row-skipped this pass - a skipped pass reconstructs
    # the layout from cached relative_pos/height caches, so its accuracy is only
    # as good as those caches; a skip-free pass measured everything for real.
    rows_skipped = False

    for idx in range(start_index, end_index + 1):
        key = keys[idx]
        relative_pos = imgui.get_cursor_screen_pos()
        relative_pos = (relative_pos[0] - true_left,
                        relative_pos[1] - true_top + item_spacing_y)

        child_draw_state = draw_state._children.get(idx, None)

        # A child currently being drag-and-dropped renders as a floating
        # window (kwargs injected below) - never row-skip it, its stale
        # relative_pos no longer says where it is.
        is_dragged = _drag_drop.DragDrop.is_dragged_child(draw_state, key)

        # ----- off-screen detection -----
        # Is this row scrolled outside the viewport? When not searching, skip it
        # entirely up front (the perf early-out). On a search-counting frame keep
        # `clipped` to decide below: reuse a cached match count (no render) or
        # render to (re)count.
        clipped = False
        if (not horizontal and not is_dragged and child_draw_state is not None
                and child_draw_state.relative_pos is not None
                and not Core.melty.frame_count <= 2
                and (not draw_state.invalid_content_height or imgui.is_mouse_down(0)
                     or imgui.is_mouse_down(1) or imgui.is_mouse_down(2))):
            _spy = true_top + child_draw_state.relative_pos[1] - child_draw_state.header_height
            _bottom = _spy + child_draw_state.height + child_draw_state.header_height
            clipped = (_bottom + child_draw_state.height < rect[1] or _spy > rect[3])

        if clipped and not _search_full_render:
            rows_skipped = True
            imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0],
                                         (true_top + child_draw_state.relative_pos[1] +
                                          child_draw_state.height)))
            continue

        Core.melty.collection_index_stack[this_collection] = idx
        item = None
        if get_attr is None:
            if isinstance(collection, dict) and key not in collection:
                # A declared attr deleted from the instance __dict__ still
                # resolves through the class default - use that instead of
                # degrading to a "Key not found" ghost row.
                if hasattr(input_value, "__dict__") and hasattr(input_value, str(key)):
                    item = getattr(input_value, str(key), None)
                else:
                    imgui.text("Key not found: " + str(key))
                    continue
            elif hasattr(input_value, "__dict__") and hasattr(input_value, str(key)):
                item = getattr(input_value, str(key), None)
            else:
                item = collection[key]
        else:
            try:
                item = get_attr(input_value, key)
            except Exception as e:
                imgui.text(f"Error getting key {key}")

        # visual separator (object extras)
        if key is None and item is None:
            seperator(Core.melty.spacing[1])
            continue
        # apply global skip to all types
        if isinstance(key, (float, Enum, NoneType)):
            key_str = f"{input_value.__class__.__name__}"
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)


        if not show_excluded and hasattr(type(input_value), "__excluded_attrs__"):
            if not Toggles.show_excluded:
                if key_str in type(input_value).__excluded_attrs__ and key_str not in included:
                    continue
        display_name = None

        if key_str in excluded and key_str not in included:
            continue

        if not show_excluded and (not show_system and (key_str.startswith("_") or key_str.endswith("_"))):
            if key_str not in included:
                continue

        # ----- SEARCH (key match) -----
        # Whether this key is the global-current match was decided by the
        # owner's pre-body walk (resolved to _current_key_idx above); we just
        # flag it and record its row Y so the post-loop block scrolls to it.
        key_is_match = bool(search_q) and _fuzzy_key_match(search_q, key_str.lower())
        key_is_current = key_is_match and idx == _current_key_idx
        if key_is_current:
            search_current_y = imgui.get_cursor_screen_pos()[1]

        prev_tint = None
        try:
            if isinstance(collection, FolderProxy):
                codec = FILE_CODECS.for_name(key)
                if codec is not None and hasattr(codec, 'tint'):
                    prev_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*codec.tint)

            y_offset = Core.melty.collection_spacing
            if show_indices:
                display_name = f"{str(idx)}"

            item_kwargs = {
                'type_collection': kwargs.get("real_type", type(input_value)),
                'real_type': type(item),
                'return_extras': True,
                'key': key,
                'on_collapse': on_collapse,
                'on_expand': on_expand,
                'collection': input_value,
                'name': key_str,
                'display_name': display_name,
                'parent_show_add_delete': show_add_delete,
                'show_add_delete': show_add_delete,
                'with_header_end': draw_header_end if show_add_delete else None,
                'annotation': parent_annotations.get(key, item_annotation),
                'y_offset': y_offset,
                'mode': mode,
                'wrap':wrap,
                'search_match': key_is_match,
                'search_current': key_is_current,
            }

            # Folders: a dict child of a typed-add collection (show_add_types)
            # inherits the same type choices, so nested folders keep the
            # [+ <type> v] affordance all the way down.
            if show_add_types and isinstance(item, dict):
                item_kwargs['show_add_types'] = show_add_types

            # Per-field overrides: a `# [tint=...]` comment above a primitive
            # field is stored on the parent as __overrides__['__<field>__'].
            # Feed those into the child's kwargs (the child has no dict of its
            # own to carry them). Skip dunder keys defensively.
            if isinstance(input_value, dict):
                _parent_ov = input_value.get("__overrides__")
                if isinstance(_parent_ov, dict):
                    _field_ov = _parent_ov.get(f"__{key}__")
                    if isinstance(_field_ov, dict):
                        for _ok, _ov in _field_ov.items():
                            if not (isinstance(_ok, str) and _ok.startswith("__")):
                                item_kwargs[_ok] = _ov

            item_kwargs = item_kwargs | child_kwargs
            if is_dragged:
                # Detach the dragged child to a floating closable window,
                # pinned to its pre-pickup size (the wrapper glues the
                # window_pos to the cursor each dispatch).
                item_kwargs.update(_drag_drop.DragDrop.dragged_item_kwargs())
            if isinstance(input_value, (list, tuple, set)) or horizontal:
                item_kwargs['align_header'] = False

            if horizontal and child_draw_state is not None:
                rect = Core.melty.get_clip_rect()
                right_edge = rect[2]
                space_left = right_edge - (imgui.get_cursor_screen_pos()[0] + child_draw_state.width)

                if len(child_draw_state._children) > 0 and child_draw_state.height > 50:
                    imgui.dummy(0, 0)

                elif space_left < 0:
                    imgui.new_line()
                    imgui.dummy(0, item_spacing_y)

            item_return = draw_any(item, **item_kwargs)

            if len(item_return) == 3:
                item_changed, out_val, returned_ds = item_return
            else:
                item_changed, out_val, returned_ds = item_return[0], item_return[1], None

            if returned_ds is not None:
                draw_state._children[idx] = returned_ds
                returned_ds._collection_draw_state = draw_state
                returned_ds.relative_pos = relative_pos
                # Child window closed via its X (closable + closed) -> queue its key
                # for removal from the collection (applied after the loop).
                if returned_ds.closable and returned_ds.closed:
                    if close_triggers_delete:
                        to_delete.add(key)
                if key_is_current:
                    search_current_h = returned_ds.header_height
                if is_dragged:
                    # The child deferred to a floating window and drew nothing
                    # inline - hold its slot open with a placeholder so the
                    # remaining layout doesn't shift. (The horizontal branch
                    # must not run: returned_ds.abs_* is the floating window.)
                    imgui.set_cursor_screen_pos((returned_ds.abs_left + returned_ds.width, returned_ds.abs_top))
                    _drag_drop.DragDrop.draw_placeholder(horizontal, item_spacing_y,
                                                         style_manager=style_manager,
                                                         draw_bg=draw_bg)
                elif horizontal:
                    imgui.same_line(spacing=0)
                    imgui.set_cursor_screen_pos((returned_ds.abs_left + returned_ds.width, returned_ds.abs_top))
                else:
                    imgui.dummy(0, item_spacing_y)

            if isinstance(out_val, CollectionAction):
                # perform the move; this should mutate the plain dicts you attached
                result = Core.melty.to_apply(out_val)
                item_changed, out_val = False, None

            if set_attr is not None and item_changed:
                try:
                    set_attr(input_value, key, out_val)
                except Exception as e:
                    print(f"Error setting key {key} to value {out_val}: {e}")
            else:
                if "return_item" not in item_kwargs:
                    if item_changed and apply_change and key is not None:
                        if isinstance(input_value, (dict, defaultdict, MutableMapping, types.MappingProxyType)):
                            input_value[key] = out_val
                        elif isinstance(input_value, list):
                            input_value[key] = out_val
                        elif isinstance(input_value, deque):
                            input_value[key] = out_val
                        elif isinstance(input_value, tuple):
                            temp = list(input_value)
                            temp[key] = out_val
                            input_value = parent_type(temp)
                        else:
                            setattr(input_value, key_str, out_val)

            changed |= item_changed
            if item_changed and return_item:
                item_to_return = out_val

        except Exception as e:
            print(f"Error rendering field '{key_str}' of {type(input_value).__name__}: {e}")
            print_stack_trace(exception=e)

        finally:
            if prev_tint is not None:
                style_manager.set_imgui_tint(*prev_tint)

    Core.melty.collection_index_stack.pop()

    # Apply removals for children closed via their X this frame (collected above, so the
    # collection is never mutated mid-iteration). Only dict-like / list collections are
    # safely key-deletable here; tuples/sets/object-__dict__ are left untouched. Sets
    # `changed` so the edit propagates to the owner (e.g. a RenderHost io_function).
    if to_delete:
        print(f"Deleting keys {to_delete} {input_value.__class__.__name__}")
        if isinstance(input_value, (dict, defaultdict, MutableMapping, _BubblingDict)):
            for _k in to_delete:
                print(f"_k in to_delete Deleting key {_k}")
                if _k in input_value:
                    print(f"del input_value[_k found in, deleting")
                    del input_value[_k]
                    changed = True


        elif isinstance(input_value, list):
            for _i in sorted((k for k in to_delete if isinstance(k, int)), reverse=True):
                if 0 <= _i < len(input_value):
                    del input_value[_i]
                    changed = True


    # When navigation just happened, scroll the current key into view.
    # draw_collection disables its own scroll, so _scroll_into_view walks up to
    # the real scroll container. (A current match inside a child is scrolled by
    # that child itself.) _current_key_idx came from the owner's walk.
    if search_session is not None and search_session.scroll_to:
        draw_state._search_current_key = _current_key_idx
        if search_current_y is not None:
            h = search_current_h or imgui.get_text_line_height()
            _scroll_into_view(draw_state, search_current_y, search_current_y + h)

    # Prune stale child slots - indices outside the current key range, left
    # over from deletions/cross-collection edits. Their draw_states keep old
    # geometry that ghost-walks (skip-advance, drop slots) would trip over.
    if draw_state._children:
        for _stale_idx in [i for i in draw_state._children
                           if isinstance(i, int) and i > end_index]:
            del draw_state._children[_stale_idx]

    end_pos = imgui.get_cursor_pos()[1]
    content_height = (end_pos - start_cursor)
    imgui.dummy(1, 0)

    # Commit the measurement when it's trustworthy: a skip-free pass measured
    # every row for real (commit even mid-drag - that's what lets a collection
    # update DURING a drag instead of storming after it); a pass with
    # skips reconstructed the layout from caches, so only commit it in the
    # old steady-state conditions (no buttons held).
    measured_fully = not rows_skipped and not premature_break
    if measured_fully or (not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1)
                          and not imgui.is_mouse_down(2) and not premature_break):
        draw_state.content_height = snap_int(content_height)
        draw_state.invalid_content_height = False

    draw_state.premature_break = premature_break
    if return_item:
        if changed:
            return changed, item_to_return
        else:
            return False, input_value

    return changed, input_value


def main_header(input_value, name, **kwargs):
    imgui.text("Main Header")


@render_func(is_default_for=(property))
def draw_property(input_value: property, draw_state, **kwargs):
    imgui.text_colored(f"Property: {input_value.fget.__name__}", 1.0, 0.5, 0.0, 1.0)
    # value = input_value.fget(input_value)
    # draw_any(value, name="Value", show_bg=True, draw_state=draw_state)



@render_func(is_lens_for=(type), skip_draw=True)
def type_lens(input_value, view_func, child_kwargs, **kwargs):
    changed, value = view_func(**child_kwargs)
    if changed:
        for k, v in value.items():
            if hasattr(input_value, k):
                if k.startswith("_"):
                    continue
                try:
                    setattr(input_value, k, v)
                except Exception as e:
                    pass

    return changed, value


@render_func(show_bg=True, align_header=False, use_cache=True, shadow=False,
             with_header=draw_header)
def draw_type(input_value: type, **kwargs):
    try:
        class_vars = {**{k: getattr(input_value, k) for k in vars(input_value)}}

        changed, new_dict = draw_collection(class_vars, real_type=input_value, disable_scroll=True,
                                            name=f"Class: {input_value.__name__}")

        if changed:
            for k, v in new_dict.items():
                if k.startswith("_"):
                    continue
                try:
                    imgui.text(f"Setting attribute {k} to value {v} on class {input_value.__name__}")
                    setattr(input_value, k, v)
                except Exception as e:
                    imgui.text(f"Error setting attribute {k} on class {input_value.__name__}: {e}")
    except Exception as e:
        imgui.text(f"Error rendering type {input_value}: {e}")


@render_func(show_bg=True, use_cache=True, selectable=False, header_single_line=False, align_header=False,
             with_header=None, bg_offset=3, auto_resize=True, temp=True)
def draw_global_search(input_value, draw_state=None, **kwargs):
    """Renders the GlobalSearch window: the search box plus the matching nodes
    from the draw_state tree draw_main registered on us. Results are recomputed
    only when the query changes (the walk is the expensive part)."""
    # Expose our own window draw_state + honour a focus request from draw_main's
    # Ctrl+Shift+F shortcut (one-shot: grab the box's text focus this frame).
    input_value.window_ds = draw_state
    _focus = input_value._focus_requested
    input_value._focus_requested = False
    # return_extras gives the box's draw_state so we can tell when it holds text
    # focus (and thus when our arrow/enter result-navigation should be live).
    box = draw_text(input_value.query, name="Search", show_name=False, searchable=False, header_same_line=False,
                    font=Font.JETBRAINS_MONO_50, request_focus=_focus, is_tree=False, align_header=False,
                    return_extras=True, tint=(1, 1, 1))
    changed, new_query = box[0], box[1]
    box_ds = box[2] if len(box) > 2 else None
    if changed:
        input_value.query = new_query

    q = (input_value.query or "").strip().lower()
    if q != input_value._last_query:
        input_value._last_query = q
        input_value.results = (global_search_results(input_value.root, q, exclude=draw_state)
                               if input_value.root is not None and len(q) >= 2 else [])
        input_value.selected = 0  # reset highlight to the top match on a new query

    # Flatten the grouped results to their on-screen order — what the highlight
    # moves through and what each index below refers to.
    groups = group_results_by_window(input_value.results, input_value.root)
    flat = [(label, ds, win) for win, items in groups.items() for (label, ds) in items]
    n = len(flat)
    input_value.selected = (input_value.selected % n) if n else 0

    # While the box holds text focus (single-line, so Up/Down/Enter don't touch
    # it): Up/Down move the highlight one result, Ctrl+Up/Down jump between window
    # sections (group starts), and Enter launches the highlighted result.
    if n and box_ds is not None and Core.melty.text_focused_ds is box_ds:
        # Flat indices where each window group begins (for section jumps).
        starts = [i for i in range(n) if i == 0 or flat[i][2] is not flat[i - 1][2]]

        def _group_idx():  # index into `starts` of the group holding `selected`
            gi = 0
            for j, s in enumerate(starts):
                if s <= input_value.selected:
                    gi = j
            return gi

        downs = [m for k, m in Core.melty.frame_key_events if k == glfw.KEY_DOWN]
        ups = [m for k, m in Core.melty.frame_key_events if k == glfw.KEY_UP]
        enters = [k for k, _ in Core.melty.frame_key_events if k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)]
        if downs:
            if any(m & glfw.MOD_CONTROL for m in downs):
                input_value.selected = starts[(_group_idx() + 1) % len(starts)]
            else:
                input_value.selected = (input_value.selected + 1) % n
            request_render()
        elif ups:
            if any(m & glfw.MOD_CONTROL for m in ups):
                input_value.selected = starts[(_group_idx() - 1) % len(starts)]
            else:
                input_value.selected = (input_value.selected - 1) % n
            request_render()
        if enters:
            _, _ds, _win = flat[input_value.selected]
            go_to_search_result(_ds, _win)
            _dismiss_global_search()

    w = (draw_state.content_width - 10) if draw_state and draw_state.content_width else 200
    # Group by owning window; header + rows are coloured by the window's real
    # tint (from its ManagedWindow), and the highlighted row stands out. A row
    # that names a window (a Dock entry) is tinted by that window, not its
    # container, so it matches the window it launches.
    idx = 0
    for win, items in groups.items():
        win_label = str(getattr(win, 'name', '') or '').split("##")[0] or "?"

        # text(win_label, height=26, indent_size=10, text_color=Core.melty.window_tint(getattr(win, 'name', None)),
        #      wrap=True, name=f"gsg_{idx}", width=w, font=Font.DEJAVU_SANS_22)
        for label, ds in items:
            sel = (idx == input_value.selected)
            entry = Core.melty.find_window(getattr(ds, 'name', None))
            tint = Melty.window_tint(getattr(ds, 'name', None) if entry is not None
                                     else getattr(win, 'name', None))
            if tint is None:
                tint = ds.tint

            if button(label, text_align="left", name=f"gsr_{idx}", width=w, height=24, show_bg=False, use_cache=False,
                      color=tint, tint_value=-0.6, factor=0.8, shadow=False, z_offset=0, saturation=1.0,
                      search_match=sel, search_current=sel, rounding=0)[0]:
                go_to_search_result(ds, win)
                _dismiss_global_search()
            imgui.dummy(0,00)
            idx += 1
    return False, input_value


@window(view_func=draw_global_search, mode=Modes.WINDOW_AUTO_FIT)
@defaults(tint=(0.15076258778572083, 0.2957677, 0.4697674512863159))
class GlobalSearch:
    query = ""
    root = None  # draw_main's draw_state, registered each frame
    window_ds = None  # this window's own draw_state (for the show shortcut)
    _focus_requested = False
    _last_query = None
    results = []  # cached [(label, draw_state)] for the current query
    selected = 0  # index (in on-screen order) of the arrow-key highlight


@render_func()
def class_to_var_dict(input_value: type, changed, draw_state, **kwargs):
    class_vars = {**{k: getattr(input_value, k) for k in vars(input_value)}}
    class_vars["__original__"] = input_value

    return changed, class_vars


@render_func()
def var_dict_to_class(input_value, changed, **kwargs):
    original_class = input_value.get("__original__", None)
    if original_class is None:
        imgui.text_colored("Error: No original class found in dict", 1.0, 0.0, 0.0, 1.0)
        return False, input_value

    if changed:
        for k, v in input_value.items():
            if k.startswith("_"):
                continue
            try:
                imgui.text(f"Setting attribute {k} to value {v} on class {original_class.__name__}")
                setattr(original_class, k, v)
            except Exception as e:
                imgui.text(f"Error setting attribute {k} on class {original_class.__name__}: {e}")

    return False, input_value


some_float = [0.0]
cst_dict = {}
test_code = None
selected_tabs = ["Alpha"]


@render_func(show_bg=True, with_header=draw_header)
def test_columns():
    draw_str("Column 1", name="col1", column=0)
    draw_int(123, name="col2", column=1)
    draw_float(0.5, name="col3", column=2)
    draw_float(0.5, name="test_5", column=5)
    draw_float(0.5, name="test_5_b", column=5)

    for i in range(10):
        draw_float(0.4, name=f"float_{i}", column=2)


@render_func(use_cache=False, show_bg=False, disable_scroll=True, shadow=False, selectable=False)
def draw_with_modes(input_value, modes, tab_state: TabState = None, search_text="", draw_state=None, unique=0):
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [modes[0]]
    imgui.dummy(0, 5)
    tint_value = 0.0
    tint_saturation = 0.688
    tab_changed, new_tabs = draw_tab_bar(input_value=tab_state.selected_tabs,
                                         tab_height=30, show_bg=False, bg_offset=1,
                                         name=f"tab_bar{unique}", wrap=True,
                                         collection=modes, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    imgui.dummy(0, 2)
    changed = False
    value = input_value
    for idx, mode in enumerate(tab_state.selected_tabs):
        mode_changed, value = draw_any(input_value, name=f"Mode: {mode} {unique}", mode=mode, selectable=False,
                                       show_name=False,
                                       with_header=None, show_header=False, disable_scroll=False,
                                       indent_size=0, show_bg=False, use_cache=True, shadow=False, column=idx)
        changed |= mode_changed

    return changed, value


@render_func
def draw_draw_state(input_value, **kwargs):
    pass


@render_func(use_cache=False, shadow=False, show_bg=False, disable_scroll=False, selectable=False)
def run_chain(input_value, chain=None, draw_state=None, route=None,
              s_key_pressed=False, enter_key_pressed=False, unique=None, debug=False, **kwargs):
    """Debug render function: executes a chain step by step with imgui output.

    Shows function name, changed flag, output type, and a value preview
    at each stage.  Color coded: green=changed, gray=cached, yellow=pending.
    """
    if chain is None:
        imgui.text("No chain provided")
        return False, input_value

    value = input_value
    changed = False

    if debug:
        imgui.text(f"Chain: {len(chain)} nodes")
        imgui.text(f"Input: {type(input_value).__name__}")
        imgui.separator()

    cache_tree = draw_state._chain_stack
    cache_tree.begin()

    mode_cache = chain[0][1].get("mode_cache", False) if isinstance(chain[0], dict) else False
    if mode_cache:
        value = cache_tree.step(changed, value)

    to_route = {}

    for i, func in enumerate(chain):
        if isinstance(func, tuple):
            func, func_kwargs = func[0], func[1]
        else:
            func_kwargs = {}

        if debug:
            if not changed:
                name = getattr(func, '__name__', repr(func))
                imgui.text(f"  [{i}] {name} — (no change)")

        func_kwargs['name'] = f"{func.__name__}{i}{kwargs.get('name', f'')}{unique}"
        func_kwargs['shadow'] = False
        func_kwargs['changed'] = changed
        func_kwargs['show_header'] = False
        func_kwargs['s_key_pressed'] = s_key_pressed
        func_kwargs['enter_key_pressed'] = enter_key_pressed
        func_kwargs['draw'] = True
        func_kwargs['real_type'] = type(input_value)
        for arg_name, arg_val in to_route.values():
            func_kwargs[arg_name] = arg_val

        next_cached = cache_tree.peek()
        if isinstance(value, str):
            imgui.text(f"  [{i}] {func.__name__} — str: '{value[:30]}'")

        imgui.begin_group()
        changed, value = func(input_value=value, reference=next_cached, **func_kwargs)
        imgui.end_group()
        #
        # if not changed:
        #     value = None

        if isinstance(value, Pending):
            changed = False
            value = None

        mode_cache = func_kwargs.get('mode_cache', True)
        if mode_cache:
            value = cache_tree.step(changed, value)

        if route is not None:
            if func in route:
                arg_name = route[func]
                to_route[arg_name] = arg_name, value

    cache_tree.end()

    return changed, value


some_test_tensor = torch.randn(3, 3)

# Nested sample data for the recursive dropdown demo.
dropdown_demo_data = {
    "small": 12,
    "medium": 16,
    "large": 24,
    "color": {
        "rgb": {"red": (1.0, 0.0, 0.0), "green": (0.0, 1.0, 0.0)},
        "named": {"steel": "#4682b4", "teal": "#008080"},
    },
    "alignment": ["left", "center", "right"],
}

drop_down_selection = None

@render_func(use_cache=False, show_bg=True)
def draw_hosts():
    for host in list(Core.melty.render_hosts.values()):
        host.draw()


@render_func(use_cache=False, show_bg=True, selectable=False,
             show_tint=True, bg_offset=-1, with_header=draw_header)
def draw_main(input_value, vis, search_text="", draw_state=None, **kwargs):
    global test_obj
    global cst_dict
    global test_code
    from src.lsd.gl_gui.view.mode import Mode

    # Register the root draw_state so GlobalSearch can walk the whole UI tree.
    GlobalSearch.root = draw_state

    # Ctrl+Shift+F: reveal the GlobalSearch window and focus its box. A
    # non_blocking root handler — so it survives a window blocker stacked in
    # front (instead of needing an extreme priority that would consume events
    # from everything else) and doesn't swallow the key from other views.
    if draw_state.on_action("non_blocking_ctrl_shift_f_down", priority_delta=512):
        gs = Core.melty.open_window("GlobalSearch")
        if gs is not None:
            # Summon the box to just above the cursor so it pops up where you're
            # looking and is ready to type into.
            mx, my = imgui.get_mouse_pos()
            Core.melty.summon_window(gs, mx, my - 65)
        GlobalSearch._focus_requested = True
        request_render()

    # Ctrl+F root fallback: the per-view Ctrl+F (core_render's searchable
    # block) only registers while the view actually RENDERS - a fully
    # cache-blitted window (an idle code/index pane) never registers, so the
    # key would go nowhere. The root always renders: resolve the hovered
    # searchable view via the BVH (parent-most, matching the per-view inverted
    # logic) and activate its search exactly as the per-view path would.
    # non_blocking, so a front view that DID register still gets the event -
    # both then act on the same parent-most view, which is idempotent.
    if draw_state.on_action("non_blocking_ctrl_f_down", priority_delta=512):
        mx, my = imgui.get_mouse_pos()
        target = None
        owns_ctrl_f = False
        _hits = Core.melty.bvh_query(mx, my)
        # Only the front window's blocker competes - hits from windows layered
        # behind the cursor must neither own the key nor become the target.
        _front_win = _hits[0].root_window if _hits else None
        for ds in _hits:
            if _front_win is not None and ds.root_window is not _front_win:
                continue
            # A view whose render func declares the inverted_ctrl_f_down event
            # param (e.g. the context menu's Input tab, which routes Ctrl+F to
            # its own filter box) OWNS the key for its duration - activating a
            # searchable descendant's find bar here would fight that filter.
            _fn = getattr(ds, '_view_func', None)
            if _fn is not None:
                _code = getattr(inspect.unwrap(_fn), '__code__', None)
                if _code is not None and 'inverted_ctrl_f_down' in _code.co_varnames[:_code.co_argcount]:
                    owns_ctrl_f = True
                    break
            kw = getattr(ds, '_kwargs', None) or {}
            if not (kw.get('searchable')
                    or getattr(kw.get('render_func'), '_searchable', False)):
                continue
            if target is None or getattr(ds, 'z_pos', 0) < getattr(target, 'z_pos', 0):
                target = ds
        if target is not None and not owns_ctrl_f:
            if Core.melty.focused_ds is not None and Core.melty.focused_ds is not target:
                Core.melty.focused_ds.search_active = False
                Core.melty.cache.invalidate(Core.melty.focused_ds._tile_id, force=True)
            target.search_active = True
            target._search_was_active = False     # find box re-claims focus
            Core.melty.clear_focus(not_this=target)
            Core.melty.focused_ds = target
            Core.melty.cache.invalidate_up(target._tile_id, force=True, max_depth=12)
            request_render()

    if draw_state.on_action("non_blocking_ctrl_z_down"):
        UndoManager.undo()

    # Redo: Ctrl+Shift+Z (mac/linux convention) or Ctrl+Y (Windows convention).
    if (draw_state.on_action("non_blocking_ctrl_shift_z_down")
            or draw_state.on_action("non_blocking_ctrl_y_down")):
        UndoManager.redo()

    # Esc dismisses the GlobalSearch window while it's open. Handled here on the
    # root (always hover-eligible) rather than on the window itself, so it works
    # no matter where the cursor is. non_blocking so the front window's blocker
    # doesn't eat it; only subscribes while open, so it doesn't swallow Esc from
    # a per-view search otherwise.
    _gs = Core.melty.find_window("GlobalSearch")
    if _gs is not None and not _gs.closed and draw_state.on_action("non_blocking_escape_key_down_inverted", priority_delta=512):
        _gs.closed = True
        Core.melty.text_focused_ds = None
        Core.melty.focused_ds = None

        request_render()

    draw_any(Core.melty.registered_windows, name="Dock", with_header=draw_header,
             mode=(Mode.WINDOW_MANAGER_SORTED, Mode.WINDOW))

    for window_cls, stored_kwargs in Core.melty.annotated_window_classes.values():

        # Copy: the stored dict is the @window decorator kwargs and persists
        # across frames. Popping view_func out of it would consume the override
        # after the first frame, so later frames fall back to draw_with_modes.
        kwargs = dict(stored_kwargs)
        kwargs.setdefault('show_bg', True)
        kwargs.setdefault('name', f"{window_cls.__name__}##@window")

        is_render_func = hasattr(window_cls, "__render_func__")
        if is_render_func:
            kwargs.setdefault('mode', Mode.MODE_WINDOW)
            kwargs.setdefault("disable_scroll", True)
            window_cls(**kwargs)
        else:
            kwargs['disable_scroll'] = True
            kwargs.setdefault('mode', (Mode.NEW_CODE, Mode.MODE_WINDOW))
            window_func = kwargs.pop("view_func", code_file_io)
            window_func(window_cls, **kwargs)

    # Self-registering RenderHost objects (view/core_conversion/render_host.py): each
    # drives a stateful wrapper and draws into its own window. Snapshot the values — a
    # host may register/remove during render (re-entrant mutation).
    for h_idx, host in enumerate(list(Core.melty.render_hosts.values())):
        host.draw()

    from src.lsd.gl_gui.model.app_model import TensorView
    draw_any(TensorView, name="Tensorview", mode=(Mode.WINDOW))
    #
    # draw_any(filesystem_proxy, name="Filesystem", disable_scroll=False, mode=Mode.WINDOW)
    # draw_any([screenshots], name="Screenshots", mode=Mode.WINDOW,
    #             child_kwargs={"child_kwargs":{"auto_resize": True}, "shadow":False,
    #                           "show_name":False, "show_header":False, "show_bg":False, "horizontal":True})
    #
    # global drop_down_selection
    # changed, selection = draw_dropdown(drop_down_selection, collection=dropdown_demo_data,
    #                                    name="Dropdown Demo", mode=Mode.WINDOW, tint=(0.180984, 0.2, 0.2))
    #
    # if changed:
    #     drop_down_selection = selection
    #     print("Drop down change", repr(selection))
    #
    # draw_collection(vis.root.lora_collection, name="Loras", mode=Mode.WINDOW)
    # draw_any(vis.root.lora_collection, name="Loras Alt View", mode=Mode.WINDOW)
    # # draw_any(vis.root.lora_collection.loras, name="Loras View Three", child_kwargs={
    # #     'is_tree': True, 'expanded': False, 'show_add_delete': True}, mode=Mode.WINDOW)

    # normalized_sub_mask, _, _ = Melty.filter.normalize(Melty.cache._mask_tex)
    # draw_texture(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
    #             max_brightness=30, name="mask_tex", live=True, mode=Mode.WINDOW)
    draw_any(Core.melty.cache.snapshot_tex, show_bg=True, name="Viewport", live=True, mode=Mode.WINDOW)

    normalized_sub_mask, _, _ = Core.melty.filter.normalize(Core.melty.cache._full_mask_tex)
    draw_any(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
             max_brightness=30, name="full_mask_tex", live=True, mode=Mode.WINDOW)

    mouse_pos = imgui.get_mouse_pos()
    ds_under_mouse = Core.melty.bvh_query(mouse_pos[0], mouse_pos[1])
    ds_names = [ds.name for ds in ds_under_mouse]
    draw_any(ds_names, name="Draw State under mouse", show_bg=True, wrap=True, use_cache=True, mode=Mode.WINDOW,
             live=True)


@render_func
def test_widget(input_value, name, unique, **kwargs):
    imgui.text("Test Widget")
    draw_text("Editable Text", name="editable_text", show_bg=True)


source = "x = foo(val=1)\nprint(x)\nsome_list=[0, 1, 2, 3]\n"
module = cst.parse_module(source)
proxy = cst_wrap(module)
name_edits = {}
code_export_str = "Test"

filesystem_proxy = FolderProxy("/home/lukas/test_folder", text_mode=True)

screenshots = FolderProxy(Toggles.screenshots, text_mode=True)


# Main draw function, called by the GUI framework

@live
class TestObj:
    def __init__(self):
        self.test_val = 0.0
        self.test_list = [1, 2, 3, 4, 5]


test_obj = TestObj()


def draw_melty_windows(vis):
    flags = (imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE |
             imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS |
             imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS | imgui.WINDOW_NO_NAV_INPUTS | imgui.WINDOW_NO_NAV |
             imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS)

    # style.frame_padding = (4, 2)


    imgui.set_next_window_position(0, 0)
    # Fill the entire screen
    fb_w, fb_h = map(int, imgui.get_io().display_size)

    imgui.set_next_window_size(fb_w, fb_h)
    title = "main##window_melty"
    opened, _ = begin(title, closable=False, flags=flags)

    Core.melty.imgui_main_window_hovered = imgui.is_window_hovered()

    Core.melty.begin_frame()

    # imgui.invisible_button("window_blocker", width=fb_w, height=fb_h)
    imgui.set_cursor_screen_pos((0, 0))
    imgui.set_item_allow_overlap()

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Core.melty.max_depth)
    Core.melty.channels_split = True
    Core.melty.window_stack.append((title, True))

    draw_main(name="Main Window", vis=vis, width=fb_w, height=fb_h)

    from src.lsd.gl_gui.applet.test_applet import render_app
    render_app()

    Core.melty.end_frame()

    # End frame ###############
    Core.melty.window_stack.pop()
    draw_list.channels_merge()
    Core.melty.channels_split = False

    end()


@render_func(is_default_for=PendingTexture, use_cache=True, wrap=True, z_offset=1, selectable=False,
             show_bg=True, auto_resize=True, with_header=draw_header)
def draw_pending_texture(input_value: PendingTexture, draw_state):
    if input_value.texture_id is None:
        imgui.text(f"Uploading... {id(input_value)}")
        return False, input_value

    max_size = 300
    if input_value.tex_width > input_value.tex_height:
        width = max_size
        height = int(max_size * input_value.tex_height / input_value.tex_width)
    else:
        height = max_size
        width = int(max_size * input_value.tex_width / input_value.tex_height)

    return_val = draw_texture(input_value.texture_id, initial={"width":width, "height":height},
                              name=f"{draw_state.id}_inner", auto_resize=False,
                              show_header=False, use_cache=True, wrap=False, tint=(0.11, 0.29, 0.52))

    return return_val


@render_func(is_default_for=numpy.uint32, show_bg=True, use_cache=False, show_add_delete=False, z_offset=2,
             fill_height=True, selectable=True,
             indent_size=0, min_width=35, min_height=35, wrap=False, disable_scroll=True,
             zoom_speed=0.3, with_header=draw_header, manual_content_height=True)
def draw_texture(input_value: numpy.uint32, hovered, scroll_y_changed, middle_mouse_drag, right_mouse_drag,
                 zoom_state: ZoomState, zoom_speed, header_height=0, min_zoom=0.1,
                 max_zoom=50.0, style_manager=None, max_brightness=5.0, max_contrast=5.0,
                 draw_state=None, jet=False, **kwargs):
    original_id = input_value
    texture_id = input_value
    imgui.dummy(draw_state.width, draw_state.height - 20)

    # Ensure we have valid state if this is the first run
    if not hasattr(zoom_state, 'zoom'):
        zoom_state.zoom = 1.0
        zoom_state.center_u = 0.5
        zoom_state.center_v = 0.5

    # Check if opengl texture ID is valid
    if not gl.glIsTexture(texture_id):
        imgui.text(f"Error: {texture_id} is not a valid texture")
        return False, input_value

    # 1. Query Texture Properties
    original_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)

    width = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH)
    height = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    if width > 16384 or height > 16384:
        imgui.text(f"Error: Texture size {width}x{height} exceeds maximum supported size.")
        return False, input_value

    if width == 0 or height == 0:
        return False, input_value

    # 2. Canvas Setup (Fill available space)
    view_width = max(1, draw_state.width)
    view_height = max(1, draw_state.height)

    # 3. Calculate Aspect Ratio Corrections
    tex_aspect = width / height
    view_aspect = view_width / view_height

    # Calculate the visible UV width/height based on zoom and aspect ratio.
    if view_aspect > tex_aspect:
        # View is wider: Fit to Height
        uv_height_size = 1.0 / zoom_state.zoom
        uv_width_size = uv_height_size * (view_aspect / tex_aspect)
    else:
        # View is taller: Fit to Width
        uv_width_size = 1.0 / zoom_state.zoom
        uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    mixed_color = (1, 1, 1, 1)
    highlight_color = (1, 1, 1, 1)

    if style_manager is not None:
        mixed_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                   value=0.3,
                                                   factor=0.9,
                                                   saturation_scale=1.0,
                                                   alpha=1.0)
        highlight_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                       value=1.0,
                                                       factor=0.9,
                                                       saturation_scale=1.0,
                                                       alpha=1.0)
    io = imgui.get_io()
    overlay: _DrawList = imgui.get_overlay_draw_list()

    if right_mouse_drag:
        b_str = f"{zoom_state.brightness:.3f}"
        overlay.add_text(right_mouse_drag.x, right_mouse_drag.y - 30,
                         col=imgui.get_color_u32_rgba(*highlight_color[:3], 1),
                         text=f"brightness:{zoom_state.brightness:.3}\ncontrast:{zoom_state.contrast:.3}")

        if io.key_shift:
            if io.key_ctrl:
                zoom_state.hue += right_mouse_drag.dx * 0.001
                zoom_state.saturation -= right_mouse_drag.dy * 0.001
            else:
                zoom_state.brightness += right_mouse_drag.dx * 0.001
                zoom_state.contrast -= right_mouse_drag.dy * 0.001
        else:
            if io.key_ctrl:
                zoom_state.hue += right_mouse_drag.dx * 0.005
                zoom_state.saturation -= right_mouse_drag.dy * 0.005
            else:
                zoom_state.brightness += right_mouse_drag.dx * 0.005
                zoom_state.contrast -= right_mouse_drag.dy * 0.005

        # zoom_state.brightness = max(0.0, min(max_brightness, zoom_state.brightness))
        # zoom_state.contrast = max(0.0, min(max_contrast, zoom_state.contrast))

    if jet:
        texture_id = Core.melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Core.melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Core.melty.filter.jet(texture_id, offset=zoom_state.hue)
    else:
        texture_id = Core.melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Core.melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Core.melty.filter.hue_saturation(
            texture_id,
            saturation=(zoom_state.saturation),
            hue_shift=(zoom_state.hue),
        )

    # texture_id = Core.melty.filter.swirl(
    #     input_value,
    #     radius=1.0,
    #     angle=(zoom_state.brightness * 5),
    # )
    # texture_id = Core.melty.filter.swirl(
    #     texture_id,
    #     angle=zoom_state.brightness,
    #     radius=zoom_state.contrast
    # )

    p_min = (draw_state.abs_left + 2, draw_state.abs_top + 2)
    p_max = (draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.height - 2)
    p_min_x, p_min_y = p_min[0], p_min[1]

    scroll_delta = 0
    if scroll_y_changed is not None:
        scroll_delta = scroll_y_changed.value

    # --- Logic: Zoom and Pan ---

    zoom_delta = 0.0

    # 4a. Handle Zoom Triggers (Scroll & Keyboard)

    # Keyboard Shortcuts (1, 2, 3, 4)
    forced_zoom = -1.0
    key_1 = 49
    numpad_key_1 = 321
    if hovered:
        if imgui.is_key_pressed(key_1) or imgui.is_key_pressed(numpad_key_1):  # Key '1'
            forced_zoom = 1.0
            # Reset Pan to Center
            zoom_state.center_u = 0.5
            zoom_state.center_v = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(50):  # Key '2'
            forced_zoom = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(51):  # Key '3'
            forced_zoom = 0.25
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(52):  # Key '4'
            forced_zoom = 0.125
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0

    if forced_zoom > 0:
        zoom_state.zoom = forced_zoom
        # Recalculate uv sizes immediately for consistent bounding this frame
        if view_aspect > tex_aspect:
            uv_height_size = 1.0 / zoom_state.zoom
            uv_width_size = uv_height_size * (view_aspect / tex_aspect)
        else:
            uv_width_size = 1.0 / zoom_state.zoom
            uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    # Scroll Logic
    if scroll_delta != 0:
        if io.key_shift:
            zoom_delta = scroll_delta * zoom_speed * 0.3
        else:
            zoom_delta = scroll_delta * zoom_speed
    elif middle_mouse_drag and middle_mouse_drag.modifiers == glfw.MOD_CONTROL:
        zoom_delta = io.mouse_delta.y * -0.008

    # 4b. Handle Pan (Middle Click Drag)
    if middle_mouse_drag and middle_mouse_drag.modifiers != glfw.MOD_CONTROL:
        u_scale = uv_width_size / view_width
        v_scale = uv_height_size / view_height
        if middle_mouse_drag.modifiers == glfw.MOD_SHIFT:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale * 0.5
            zoom_state.center_v += middle_mouse_drag.dy * v_scale * 0.5
        else:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale
            zoom_state.center_v += middle_mouse_drag.dy * v_scale

    # 4c. Apply Zoom Logic (Zoom to Cursor)
    if zoom_delta != 0.0:
        zoom_factor = 1.0 + zoom_delta
        new_zoom = max(min_zoom, min(zoom_state.zoom * zoom_factor, max_zoom))

        if new_zoom != zoom_state.zoom:
            mouse_pos = imgui.get_mouse_pos()

            if io.key_ctrl:
                mouse_u_ratio, mouse_v_ratio = (0.5, 0.5)
            else:
                mouse_u_ratio = (mouse_pos[0] - p_min_x) / view_width
                mouse_v_ratio = (mouse_pos[1] - p_min_y) / view_height

            curr_uv_w = uv_width_size
            curr_uv_h = uv_height_size

            # Recalculate NEW UV dimensions
            if view_aspect > tex_aspect:
                new_uv_h = 1.0 / new_zoom
                new_uv_w = new_uv_h * (view_aspect / tex_aspect)
            else:
                new_uv_w = 1.0 / new_zoom
                new_uv_h = new_uv_w * (tex_aspect / view_aspect)

            diff_w = curr_uv_w - new_uv_w
            diff_h = curr_uv_h - new_uv_h

            zoom_state.center_u += diff_w * (mouse_u_ratio - 0.5)
            zoom_state.center_v += diff_h * (0.5 - mouse_v_ratio)

            zoom_state.zoom = new_zoom

            # Update these for Step 5
            uv_width_size = new_uv_w
            uv_height_size = new_uv_h

    # 5. Calculate Final UVs and Clamp to Bounds
    half_uv_w = uv_width_size * 0.5
    half_uv_h = uv_height_size * 0.5

    # --- Bounding Logic Start ---
    margin_px = 20.0

    pixel_u = uv_width_size / view_width
    pixel_v = uv_height_size / view_height
    margin_u = margin_px * pixel_u
    margin_v = margin_px * pixel_v

    min_u = -half_uv_w + margin_u
    max_u = 1.0 + half_uv_w - margin_u

    if min_u > max_u:
        zoom_state.center_u = 0.5
    else:
        zoom_state.center_u = max(min_u, min(zoom_state.center_u, max_u))

    min_v = -half_uv_h + margin_v
    max_v = 1.0 + half_uv_h - margin_v

    if min_v > max_v:
        zoom_state.center_v = 0.5
    else:
        zoom_state.center_v = max(min_v, min(zoom_state.center_v, max_v))
    # --- Bounding Logic End ---

    uv_x_min = zoom_state.center_u - half_uv_w
    uv_x_max = zoom_state.center_u + half_uv_w
    uv_y_min = zoom_state.center_v - half_uv_h
    uv_y_max = zoom_state.center_v + half_uv_h

    uv_a = (uv_x_min, uv_y_max)
    uv_b = (uv_x_max, uv_y_min)

    # 6. Clip and Draw
    scale_u_px = view_width / uv_width_size
    scale_v_px = view_height / uv_height_size

    # Project Texture Edges
    raw_img_left = p_min_x + (0.0 - uv_x_min) * scale_u_px
    raw_img_right = p_min_x + (1.0 - uv_x_min) * scale_u_px
    raw_img_top = p_min_y + (uv_y_max - 1.0) * scale_v_px
    raw_img_bottom = p_min_y + (uv_y_max - 0.0) * scale_v_px

    # Intersect with Viewport
    clip_left = max(p_min_x, raw_img_left)
    clip_right = min(p_max[0], raw_img_right)
    clip_top = max(p_min_y, raw_img_top)
    clip_bottom = min(p_max[1], raw_img_bottom)

    draw_list: _DrawList = imgui.get_window_draw_list()

    if imgui.is_mouse_hovering_rect(clip_left, clip_top, clip_right, clip_bottom):
        draw_state.hover_reported = True
    else:
        draw_state.hover_reported = False

    Core.melty.push_clip((clip_left, clip_top, clip_right - 3, clip_bottom))
    draw_list.add_image_rounded(texture_id,
                                a=p_min,
                                b=p_max,
                                uv_a=uv_a,
                                uv_b=uv_b,
                                rounding=5.0)
    draw_list.add_rect(raw_img_left, raw_img_top, raw_img_right + 1, raw_img_bottom + 1,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       0.0, 0, 1.0)
    Core.melty.pop_clip()

    line_height = imgui.get_text_line_height()
    draw_list.add_text(max(p_min_x + 5, raw_img_left), clip_top - line_height - 5,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       text=f"{original_id} - {texture_id} - {width}x{height} - Zoom: {zoom_state.zoom:.2f}x")

    gl.glBindTexture(gl.GL_TEXTURE_2D, original_texture)

    return False, draw_state


@render_func(is_default_for=ManagedWindow, is_tree=False, show_name=False, use_cache=True,
             shadow=False, show_bg=False, selectable=False, show_add_delete=False,
             show_tint=False, wrap=False, with_header=draw_header, temp=True)
def draw_managed_window(input_value, name, draw_state, mouse_down=False, selectable=False, **kwargs):
    try:
        window_draw_state = input_value.draw_state
    except Exception as e:
        imgui.text(f"Error accessing draw_state: {e}")
        return False, input_value

    window_input_value = input_value.input_value
    name = window_draw_state.name

    start_cursor = imgui.get_cursor_screen_pos()
    imgui.dummy(4, 20)
    imgui.same_line()

    if not window_draw_state.persistent and not window_draw_state.seen and window_draw_state.closed:
        Core.melty.delete_window(window_draw_state)

    window_tint = None

    if hasattr(window_input_value, 'tint') and window_input_value.tint is not None:
        changed, new_tint = draw_tuple(window_input_value.tint, name="")
        if changed:
            window_input_value.tint = new_tint
            window_draw_state.tint = window_input_value.tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_input_value.tint

    elif window_draw_state.tint is not None:
        changed, new_tint = draw_tuple(window_draw_state.tint, name="")
        if changed:
            window_draw_state.tint = new_tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_draw_state.tint

    imgui.same_line()

    if mouse_down:
        window_draw_state.closed = not window_draw_state.closed

    button_height = 31
    target_spacing = 81
    target_tint_value = 0.103

    if name == "Window Manager":
        button(f"{name}", color=(0, 0, 0, 0),
               saturation=1.3, width=130, height=button_height)[0]
        return

    # Pass the search-match flags (set by draw_collection for this key) through
    # to the name button so it can draw the find highlight — the visible row is
    # this button, not a header.
    _search_match = kwargs.get("search_match", False)
    _search_current = kwargs.get("search_current", False)
    if window_draw_state.closed:
        if button(f"{name}", color=window_tint, z_offset=-4, tint_value=0.035, factor=0.92, text_value=0.305,
                  saturation=0.872, width=draw_state.content_width - target_spacing, height=button_height,
                  search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = False
            this_window_right = draw_state.abs_left + draw_state.width
            from_zero_x = window_draw_state.abs_left - window_draw_state.window_pos[0]
            from_zero_y = window_draw_state.abs_top - window_draw_state.window_pos[1]
            window_draw_state.window_pos = (this_window_right + 10 - from_zero_x, draw_state.abs_top - from_zero_y)
            Core.melty.move_window_to_front(window_draw_state)
            Core.melty.cache.invalidate_up_by_obj(input_value)
    else:
        if button(f"{name}", saturation=1.315, z_offset=4, color=window_tint, factor=0.659, value=-0.205, text_value=1.357,
                  width=draw_state.content_width - target_spacing, height=button_height,
                  search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = True

    imgui.same_line()

    if window_tint is None or not isinstance(window_tint, tuple) or len(window_tint) < 3:
        window_tint = (2.558, 0.5, 0.5)

    imgui.set_cursor_screen_pos((draw_state.abs_left + draw_state.content_width-20, draw_state.abs_top))
    target_icon = ""  # Target icon (FontAwesome Unicode)
    if  button(f"{target_icon}##{name}", height=button_height, color=window_tint, z_offset=2, tint_value=target_tint_value,
           factor=0.799,
           saturation=0.764, shadow=False)[0]:
        this_window_right = draw_state.abs_left + draw_state.width
        from_zero_x = window_draw_state.abs_left - window_draw_state.window_pos[0]
        from_zero_y = window_draw_state.abs_top - window_draw_state.window_pos[1]
        window_draw_state.window_pos = (this_window_right + 10 - from_zero_x, draw_state.abs_top - from_zero_y)
        Core.melty.move_window_to_front(window_draw_state)
        Core.melty.cache.invalidate_up_by_obj(input_value)

    imgui.set_cursor_screen_pos(start_cursor)

    live_tint = (0.409, 0.1, 0.1)

    if window_draw_state.live:
        fa_live_icon = ""
        imgui.text_colored(fa_live_icon, *(live_tint))
        imgui.same_line()


def draw(vis):
    draw_melty_windows(vis)


def export_code(test_param_2: int = 5):
    # print(f"hello {test_param_2}")
    global code_export_str
    code_export_str = proxy.node.code


@render_func(use_cache=False)
def draw_drag_drop_target(input_value, draw_state, on_drag, do_flow, depth,
                          collection, key, melty, y_offset, enable_flow, min_width,
                          unique, tag, style_manager, offset=0, indent_size=10):
    if Core.melty.active_layer == Core.melty.drag_layer:
        return False, 0.0

    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Core.melty.is_window_enabled():
        return False, 0.0

    if melty.initial_drag_offset is None:
        return False, 0.0

    if key is None:
        pass
    # ----------------- top spacing -----------
    falloff = 25.0  # Higher is gentler
    if enable_flow:
        drop_gap = 6.0
    else:
        drop_gap = 0.0

    drag_delta_curve = 1.0 - max(0.0, min(1.0, 1.0 - abs(melty.drag_delta[1] / 15.0)))

    mouse_pos = imgui.get_mouse_pos()
    cursor_top = imgui.get_cursor_screen_pos()[1]
    cursor_left = imgui.get_cursor_screen_pos()[0]
    static_offset = drop_gap
    distance_to_mouse = abs(mouse_pos[1] - cursor_y_screen -
                            melty.initial_drag_offset[1] - drop_gap + static_offset)
    bell_curve = max(0.0, min(1.0, 1.0 - (distance_to_mouse / falloff)))

    window_size = imgui.get_window_size()
    window_pos = imgui.get_window_position()
    window_rect = (window_pos[0], window_pos[1],
                   window_pos[0] + window_size[0],
                   window_pos[1] + window_size[1])
    mouse_over_window = imgui.is_mouse_hovering_rect(*window_rect)

    if melty.drag_in_progress:
        if melty.dragged_item is None:
            melty.drag_in_progress = False

        elif id(melty.dragged_item._input_value) == id(collection):
            return False, 0.0

    if melty.drag_in_progress and do_flow and not on_drag and mouse_over_window:
        flow_spacing = drop_gap * bell_curve * drag_delta_curve
    else:
        flow_spacing = 0.0
        drag_delta_curve = 1.0

    if tag == "top":
        Core.melty.flow_spacing += (flow_spacing)
        # imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + (flow_spacing))

    draw_list = imgui.get_window_draw_list()
    # if Core.melty.channels_split:
    #     draw_list.channels_set_current(min(Core.melty.max_depth - 1, depth + 2))

    # line_width = imgui.get_style().frame_padding.y * 2.0
    # color = style_manager.make_color_rgb(*(1.0, 1.0, 1.0), factor=1.0,
    #                                      value=1.0, alpha=1.0, saturation_scale=0.3)

    # cursor_bottom = imgui.get_cursor_screen_pos()[1]
    # ------------------ end spacing -----------
    cursor_bottom = cursor_top + max(2.0, flow_spacing)

    if tag == "bottom":
        # span = cursor_bottom - cursor_top
        cursor_bottom += 0
        cursor_top += 0

    if melty.drag_in_progress and not on_drag and do_flow and mouse_over_window:
        if draw_state.height is not None:
            active_drop = (melty.drag_drop_target == draw_state.unique
                           and tag == melty.drag_drop_target_tag)

            if Core.melty.channels_split:
                draw_list.channels_set_current(min(Core.melty.get_channel() + 1, Core.melty.max_depth - 1))

                if active_drop:
                    draw_list.channels_set_current(min(Core.melty.get_channel() + 2, Core.melty.max_depth - 1))
                    cursor_bottom += ((1.0 - drag_delta_curve) * drop_gap)

            if distance_to_mouse < melty.nearest_drop_distance:
                melty.nearest_drop_distance = distance_to_mouse
                melty.nearest_drop_target = draw_state.unique
                melty.nearest_drop_target_tag = tag

                melty.drag_drop_action.target_unique = draw_state.unique
                melty.drag_drop_action.target_tag = tag
                melty.drag_drop_action.target_key = key
                melty.drag_drop_action.target_collection = collection
                melty.drag_drop_action.target_draw_state = draw_state

                if melty.drag_drop_action.target_key is None:
                    pass

            height_as_factor = 800.0
            drag_distance = sqrt(melty.drag_delta[0] ** 2 + melty.drag_delta[1] ** 2)
            initial_fade_offset = max(min(1.0, melty.total_drag_distance / 10.0), 0.0)
            if melty.total_drag_frames < 1:
                initial_fade_offset = 0.0
            opacity = max(0.0, min(1.0, 1.0 - (distance_to_mouse / (height_as_factor * 0.3))))
            opacity *= initial_fade_offset
            # opacity = 1.0 if active_drop else opacity

            bg_tint = Core.melty.get_bg_color(-1)
            bg_style = GlobalStyle.get_global_constant("bg_style", folder="bg_styles")

            color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                     value=1.3,
                                                     alpha=opacity, saturation=0.8)

            # color = style_manager.make_color_rgb(*bg_tint, factor=0.0,
            #                                      value=1.0, alpha=opacity, saturation_scale=1.0)
            inactive_color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                              value=0.7,
                                                              alpha=opacity, saturation=0.8)
            # if draw_state.width == None:
            #     draw_state.width = min_width
            # if draw_state.left == None:
            #     draw_state.left = 1

            padding = imgui.get_style().frame_padding.x

            color = color if active_drop else inactive_color

            top = cursor_top - 1
            bottom = max(draw_state.abs_top, cursor_bottom - 1)
            left = draw_state.abs_left + offset
            right = draw_state.abs_left + draw_state.width - indent_size
            width = draw_state.width
            height = draw_state.height

            draw_list.add_rect_filled(left, top, right, bottom,
                                      col=imgui.get_color_u32_rgba(*color), rounding=4.0)

            if opacity > 0:
                Core.melty.cache.mask_mark_rect(draw_state, Core.melty.max_depth - 1, draw_state.shadow_depth, left, top, width,
                                           height,
                                           key=f"{left}x{top}_flow")
            #
            # draw_list.add_line(draw_state.left, draw_state.abs_top - 2 - offset,
            #                    draw_state.left + draw_state.width,
            #                    draw_state.abs_top - 2 - offset,
            #                    col=imgui.get_color_u32_rgba(*color), thickness=3)

    return False, flow_spacing


@hotkey(glfw.KEY_O)
def toggle_offscreen():
    if Core.melty.cache.enabled:
        Core.melty.cache.set_enabled(False)
    else:
        Core.melty.cache.set_enabled(True)


import imgui


def draw_vertical_scrollbar(content_height: float,
                            view_height: float,
                            view_width: float,
                            scroll_offset: float,
                            scrollbar_width: float,
                            left: float = 0.0,
                            top: float = 0.0,
                            *,
                            pad: float = 0.0,
                            rounding: float = 3.0,
                            min_grab_size: float | None = None):
    # Style & colors
    style = imgui.get_style()
    if min_grab_size is None:
        min_grab_size = float(style.grab_min_size)

    col_track = imgui.get_color_u32_rgba(0, 0, 0, 0.1)
    col_grab = imgui.get_color_u32_rgba(1, 1, 1, 0.3)
    col_border = imgui.get_color_u32(imgui.COLOR_BORDER)

    # Early clamps & deriveds
    view_height = max(0.0, float(view_height))
    view_width = max(0.0, float(view_width))
    content_height = max(0.0, float(content_height))
    scrollbar_width = max(0.0, float(scrollbar_width))

    max_scroll = max(0.0, content_height - view_height)
    scroll_offset = float(max(0.0, min(scroll_offset, max_scroll)))

    # Anchor the container at the current cursor position in screen space
    origin_x, origin_y = (left, top)

    bar_margin = 4.0
    bar_margin_x = 1.0

    # Track geometry (stick it to the right edge of the container)
    track_w = min(scrollbar_width, view_width)
    track_h = view_height
    track_x1 = origin_x + (view_width - track_w) - bar_margin_x
    track_y1 = origin_y + bar_margin
    track_x2 = track_x1 + track_w - bar_margin_x
    track_y2 = track_y1 + track_h - bar_margin * 2

    # Compute grab size & position
    if content_height <= 0.0 or track_h <= 0.0:
        grab_h = 0.0
        t = 0.0
    else:
        # Proportional size with a minimum; cap to track height.
        ratio = view_height / content_height if content_height > 0.0 else 1.0
        grab_h = max(min_grab_size, ratio * track_h)
        grab_h = min(grab_h, track_h)

        # Normalized scroll position -> grab top
        travel = max(0.0, track_h - grab_h)
        t = 0.0 if max_scroll == 0.0 else (scroll_offset / max_scroll)
        t = max(0.0, min(1.0, t))  # clamp just in case

    grab_y1 = track_y1 + (max(0.0, track_h - grab_h) * t)
    grab_y2 = grab_y1 + grab_h

    # Inner padding for nicer visuals
    inner_x1 = track_x1 + pad
    inner_x2 = track_x2 - pad
    inner_y1 = track_y1 + pad
    inner_y2 = track_y2 - pad
    grab_x1 = inner_x1
    grab_x2 = inner_x2
    grab_y1 = max(inner_y1, min(grab_y1, inner_y2 - (grab_y2 - grab_y1)))
    grab_y2 = grab_y1 + max(0.0, min(grab_h, inner_y2 - inner_y1))

    # Draw
    dl = imgui.get_window_draw_list()
    # Track
    track_w = track_x2 - track_x1
    track_h = track_y2 - track_y1
    dl.add_rect_filled(track_x1, track_y1, track_x2, track_y2, col_track, rounding)
    # Core.melty.cache.mask_mark_rect(Core.melty.depth, track_x1, track_y1, track_w, track_h,
    #                            key=str(Core.melty.unique_stack[-1]) + "scrollbar")

    dl.add_rect(track_x1, track_y1, track_x2, track_y2, col_border, rounding)
    # Grab
    if grab_y2 > grab_y1 and grab_x2 > grab_x1:
        dl.add_rect_filled(grab_x1, grab_y1, grab_x2, grab_y2, col_grab, rounding)
        dl.add_rect(grab_x1, grab_y1, grab_x2, grab_y2, col_border, rounding)

    return {
        "offset": scroll_offset,
        "track_min": (track_x1, track_y1),
        "track_max": (track_x2, track_y2),
        "grab_min": (grab_x1, grab_y1),
        "grab_max": (grab_x2, grab_y2),
        "visible": content_height > view_height
    }


bg_style_default = {
    "value": 0.01,
    "saturation": 1.2,
    "alpha": 1.0,
    'max_value': 1.0
}


def get_bg_color(depth, rounding, style_manager, auto_resize):
    depth_factor = GlobalStyle.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.95
    depth_offset = GlobalStyle.get_global_constant("depth_offset", default=0.0, folder="bg_styles") - 1.3
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))

    hovered_offset = 0.0

    def mix_colors(c1, c2, fac):
        return (c1[0] * (1 - fac) + c2[0] * fac,
                c1[1] * (1 - fac) + c2[1] * fac,
                c1[2] * (1 - fac) + c2[2] * fac)

    global bg_style_default
    bg_style = GlobalStyle.get_global_constant("bg_style", default=bg_style_default, folder="bg_styles")
    outline_factor = GlobalStyle.get_global_constant("outline_factor", default=1.0, folder="bg_styles") * 1.4

    if not auto_resize:
        outline_factor *= 1.3

    if auto_resize:
        bleed_factor = 0.2
    else:
        bleed_factor = 0.0
    bg_bleed = Core.melty.get_bg_color(-1)
    bg_bleed = style_manager.make_custom_styled(*bg_bleed, input=bg_style,
                                                value=0.6,
                                                alpha=1.0, saturation=1.8)
    bg_color = (style_manager.
                make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))
    bg_color = mix_colors(bg_color, bg_bleed, bleed_factor)
    return bg_color


def seperator(height):
    imgui.dummy(0, snap_int(height / 2))
    imgui.separator()
    imgui.dummy(0, snap_int(height / 2))


def test_func():
    # Some comment
    # Comment here
    some_val = 1.706
    some_dict = {"some_key": -0.02,
                 "key": False,
                 "key_2": 2.421
                 }

def compute_bg_color(bg_offset=0, tint=None, nested_bg=False):
    depth_wrap = 34
    depth_scale = 1.629
    intensity_factor = 0.021
    intensity_offset = -0.336
    outline_depth_mul = 0.786
    # More text
    bleed_style = {'value': -0.111, 'alpha': 1.12, 'saturation': 7.045}

    bg_style = {
        'value': -0.004, 'saturation': 1.101,
        'alpha': 0.504, 'max_value': 1.8,
    }

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )

    # -- Depth calculation -------------------
    max_depth = 15
    bg_depth = Core.melty.bg_depth if Core.melty.bg_depth is not None else 0
    bg_offset = bg_offset if bg_offset is not None else 0
    wrapped_depth = min(max_depth, (bg_depth % depth_wrap) + bg_offset)
    scaled_depth = wrapped_depth * depth_scale
    depth_intensity = (scaled_depth + intensity_offset) * intensity_factor
    max_depth_intensity = 0.652
    depth_intensity = min(depth_intensity, max_depth_intensity)


    # ── Outline color ──────────────────────────────────────────
    depth_mul = outline_depth_mul
    if not nested_bg:
        depth_mul *= 1.00

    # ── Background bleed color ─────────────────────────────────
    bleed_factor = 0.501 if nested_bg else 0.446

    bleed_base = Core.melty.get_bg_color(-1)
    bleed_color = Melty.style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )

    # ── Fill rendering ─────────────────────────────────────────
    bg_color = Melty.style_manager.make_color_style_value(input=bg_style, value=max(0.0, depth_intensity))
    bg_color = mix_colors(bg_color, bleed_color, bleed_factor)

    return bg_color

@window
def draw_bg(left=25, top=0, width=0, height=57, depth=0, rounding=6.0, bg_offset=0,
            outline=True, bg_color=None, opacity=0.0,
            style_manager=None, tint=None, outline_tint=None, selected=False,
            hovered=False, pressed=False, nested_bg=False, **kwargs):
    # -- Constants ---------------------------------
    min_value = -0.272
    depth_wrap = 300
    depth_scale = 2.713
    # [tint=(1,1,1)]
    corner_radius = rounding
    border_inset = 2.802
    border_inset_half = 1.5
    stroke_width = 4.0
    # How depth maps to color intensity
    intensity_factor = 0.021
    intensity_offset = 3.137

    some_var = [32, 18, 19]
    # Outline color tuning
    outline_base = 1.765
    outline_depth_mul = 0.786
    outline_sat = {'default': 1.1, 'nested': 1.473}

    # More text
    bleed_mix = {'nested': 0.472, 'default': 0.526}
    bleed_style = {'value': -0.035, 'alpha': 1.112, 'saturation': 6.592}
    outline_bleed_mix = 0.272
    # Hover offsets per interaction state
    hover_offset_by_state = {
        'default': -1.807,
        'selected': -1.401,
        'pressed_hi': -1.813,  # pressed + opacity > 0.5
        'pressed_lo': -0.441,
    }
    bg_style = {
        'value': -0.004, 'saturation': 1.101,
        'alpha': 0.504, 'max_value': 1.8,
    }

    # ── Helpers ────────────────────────────────────────────────
    def current_indent_px():
        return Core.melty.current_indent

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )

    # -- Depth calculation -------------------
    max_depth = 30
    if Core.melty.bg_depth + bg_offset < 2:
        wrapped_depth = min(max_depth, (Core.melty.bg_depth) + bg_offset)
    else:
        wrapped_depth = min(max_depth, (Core.melty.bg_depth % depth_wrap) + bg_offset)

    scaled_depth = wrapped_depth * depth_scale
    depth_intensity = (scaled_depth + intensity_offset) * intensity_factor
    max_depth_intensity = 0.652
    depth_intensity = min(depth_intensity, max_depth_intensity)

    # ── Geometry ───────────────────────────────────────────────
    right = left + width
    bottom = top + height

    fill_rect = (
        snap_int(left) + border_inset, snap_int(top) + border_inset,
        snap_int(right) - border_inset, snap_int(bottom) - border_inset,
    )
    outline_rect = (
        snap_int(left) + border_inset_half, snap_int(top) + border_inset_half,
        snap_int(right) - border_inset_half, snap_int(bottom) - border_inset_half,
    )

    # ── Interaction hover offset ───────────────────────────────
    hover_offset = hover_offset_by_state['default']
    if selected:
        hover_offset = hover_offset_by_state['selected']
    elif pressed:
        if opacity > 0.5:
            hover_offset = hover_offset_by_state['pressed_hi']
        else:
            hover_offset = hover_offset_by_state['pressed_lo']

    # ── Outline style ──────────────────────────────────────────
    sat = outline_sat['default']
    depth_mul = outline_depth_mul
    if not nested_bg:
        depth_mul *= 1.00
        sat = outline_sat['nested']

    # ── Background bleed color ─────────────────────────────────
    bleed_factor = bleed_mix['nested'] if nested_bg else bleed_mix['default']

    bleed_base = Core.melty.get_bg_color(-2)
    bleed_color = style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )
    bleed_base = mix(*Core.melty.get_bg_color(-1)[:3], *bleed_color[:3], 0.32)
    bleed_color = style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )

    # ── Outline rendering ──────────────────────────────────────
    outline_value = max(min_value, depth_intensity * depth_mul + outline_base + hover_offset)
    outline_color = style_manager.make_color_style_value(
        input=bg_style, saturation=sat, value=outline_value,
    )
    outline_color = mix_colors(outline_color, bleed_color, outline_bleed_mix)

    if outline:
        packed_outline = imgui.get_color_u32_rgba(*outline_color[:3], 1.0)
        if outline_tint is not None:
            packed_outline = imgui.get_color_u32_rgba(*outline_tint[:3], 1.0)
        imgui.get_window_draw_list().add_rect(
            *outline_rect, col=packed_outline, rounding=corner_radius, thickness=stroke_width,
        )

    # ── Fill rendering ─────────────────────────────────────────
    if bg_color is None:
        bg_color = style_manager.make_color_style_value(input=bg_style, value=max(min_value, depth_intensity))
        bg_color = mix_colors(bg_color, bleed_color, bleed_factor)

    packed_fill = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)
    if tint is not None:
        packed_fill = imgui.get_color_u32_rgba(*tint[:3], opacity)

    if opacity > 0.0:
        imgui.get_window_draw_list().add_rect_filled(*fill_rect, col=packed_fill, rounding=corner_radius)

    return False, bg_color



@render_func(use_cache=True, selectable=False, disable_scroll=True, indent_size=0, show_bg=False, min_width=5,
             min_height=10, wrap=True, show_add_delete=False)
def button(input_value="", width=5, height=14, draw_state=None, alpha=1.0, left_mouse_held=False, shadow=True, left_mouse_down=False,
           color=(0.533, 0.068, 0.5), highlight_hovered=True, hovered=False, style_manager=None, show_button_bg=True,
           factor=1.0, tint_value=0.32, text_value=1.023, saturation=0.8, text_saturation=0.4, text_align="center",
           search_match=False, search_current=False, tint=None, rounding=None, corner_radius=6.0, text_pad=15):

    if color is not None:
        if not isinstance(color, tuple) or len(color) < 3:
            color = (2.558, 0.5, 0.5)
        if shadow:
            if left_mouse_held:
                draw_state.z_offset = 0
            else:
                draw_state.z_offset = 3.0
        else:
            draw_state.z_offset = 0.0

        if hovered and highlight_hovered:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value + 0.05,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        else:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        text_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=text_value + (1.5 if hovered else 0.0),
                                                  factor=factor, saturation_scale=text_saturation, alpha=1.0)
    else:
        text_color = (1.0, 1.0, 1.0)
        mixed_color = (0, 0, 0)

    button_txt = str(input_value).split("##")[0]
    min_size = imgui.calc_text_size(button_txt)
    width = max(width, min_size[0] + text_pad)
    height = max(height, min_size[1])
    draw_list: _DrawList = imgui.get_window_draw_list()
    bx0, by0 = imgui.get_cursor_screen_pos()

    imgui.dummy(width, height)
    draw_state.width = width
    draw_state.height = height

    # draw_state.width = btn_size[0]
    # draw_state.height = btn_size[1]


    bx1, by1 = bx0 + width, by0 + height
    # `corner_radius` is an auto-state param: Mode / class defaults, internal
    # draw_state.corner_radius writes all flow into it, and the mirror keeps
    # framework painters (selection highlight, blit mask) in sync. `rounding`
    # is an individual per-call override on top (e.g. the search results,
    # which read as a flat list of items, pass rounding to square the corners).
    rnd = corner_radius if rounding is None else rounding

    if alpha > 0.0 and show_button_bg:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  imgui.get_color_u32_rgba(*mixed_color[:3], alpha), rounding=rnd)

    # Tint fill: button mutes `color` into a dark bg, so to show a window's tint
    # we paint the raw colour over it — at low alpha so it stays a subtle wash.
    elif tint is not None and show_button_bg:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], 0.33),
                                  rounding=rnd)

    # Selection / match highlight: translucent WHITE, so the tint still reads
    # through it instead of being covered. The active row is brighter + outlined.
    if search_match:
        _a = 64 if search_current else 26
        draw_list.add_rect_filled(bx0, by0, bx1, by1, (_a << 24) | (255 << 16) | (255 << 8) | 255, rounding=rnd)

    if text_align == "left":
        draw_list.add_text(draw_state.abs_left + 5,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           imgui.get_color_u32_rgba(*text_color[:3], 1.0), button_txt)
    elif text_align == "right":
        draw_list.add_text(draw_state.abs_left + width - min_size[0] - 5,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           imgui.get_color_u32_rgba(*text_color[:3], 1.0), button_txt)
    else:
        draw_list.add_text(draw_state.abs_left + (width - min_size[0]) / 2.0 + 2,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           imgui.get_color_u32_rgba(*text_color[:3], 1.0), button_txt)

    if search_match and search_current:
        draw_list.add_rect(bx0, by0, bx1, by1, (200 << 24) | (255 << 16) | (255 << 8) | 255,
                           rounding=rnd, thickness=1.5)


    if left_mouse_down:
        request_render()
        return True, input_value

    return False, None


def render_profiler_time(input_value=None, brief=False, style_manager=None):
    """
    Renders the time taken for a specific operation in the profiler.
    """
    in_ms = input_value * 1000.0
    if brief:
        if in_ms >= 0.99:
            formatted_value = f"{(in_ms):.1f}ms"
        else:
            formatted_value = f"{(in_ms):.2f}ms"
        if formatted_value.startswith("0."):
            formatted_value = formatted_value[1:]
    else:
        formatted_value = f"{in_ms:.2f} ms"
    golden_yellow = (2.0, 0.5, 0)
    dynamic_saturation_factor = GlobalStyle.profiler["object_attr"][
        "dynamic_saturation_factor"]
    dynamic_saturation_offset = GlobalStyle.profiler["object_attr"][
        "dynamic_saturation_offset"]
    saturation = GlobalStyle.profiler["object_attr"]["saturation"]
    value = GlobalStyle.profiler["object_attr"]["value"]
    dynamic_sat = (float(in_ms + dynamic_saturation_offset) * dynamic_saturation_factor)
    text_tint = style_manager.make_color_rgb(*golden_yellow, factor=1.0 - dynamic_sat,
                                             value=min(1.0, max(0, value + dynamic_sat * 0.5)),
                                             alpha=1.0,
                                             saturation_scale=max(0, saturation - dynamic_sat))[:3]
    imgui.text_colored(f"{formatted_value}", *text_tint)
    return False, input_value


@render_func(header_same_line=True, use_cache=True, is_default_for=(types.NoneType),
             shadow=False, is_tree=False, with_header=draw_header, temp=True)
def draw_none(input_value: NoneType):
    imgui.align_text_to_frame_padding()
    imgui.text_colored("None", *(1, 1, 1), 0.2)
    return False, input_value


@render_func(is_default_for=(bool), use_cache=True, is_tree=False, wrap=True,
             header_same_line=True, min_width=20, align_header=True, shadow=False, with_header=draw_header, temp=True)
def draw_bool(input_value: bool):
    changed, is_checked = imgui.checkbox("##bool", input_value)
    if changed:
        return True, is_checked

    return False, input_value


@render_func(is_default_for=(str), shadow=False, wrap_text=False, show_bg=False, is_tree=False, wrap=False,
             show_header=False,
             show_add_delete=False, show_name=False, use_cache=True,
             disable_scroll=True, min_width=30, with_header=draw_header, temp=True)
def text(input_value: str, wrap, wrap_text=False, text_color=(1, 1, 1), draw_state=None, font=None):
    _font_pushed = False
    if font is not None and Core.melty.font_mgr is not None:
        _font_handle = Core.melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True

    text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
    if wrap_text and text_size[1] > imgui.get_text_line_height() * 4 and not wrap:
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.push_style_color(imgui.COLOR_TEXT, text_color[0], text_color[1], text_color[2], 1.0)
        imgui.text_wrapped(str(input_value))
        imgui.pop_style_color()
        imgui.pop_text_wrap_pos()

    else:
        if text_color is not None:
            imgui.text_colored(str(input_value), text_color[0], text_color[1], text_color[2], 1.0)
        else:
            imgui.text(str(input_value))

    imgui.same_line(0)
    imgui.dummy(2, 0)

    if font is not None:
        imgui.pop_font()

    return False, input_value


@render_func(is_default_for=(str), shadow=False, show_bg=False, wrap=False, selectable=False,
             is_tree=False, show_add_delete=False, use_cache=True, min_height=20,
             disable_scroll=True, with_header=draw_header, temp=True)
def draw_str(input_value: str, draw_state, editable=True, wrap=False, min_width=110, immediate_return=False, alpha=1.0):
    if not editable:
        imgui.push_style_var(imgui.STYLE_ALPHA, alpha)

        text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.text_wrapped(str(input_value))
        imgui.pop_text_wrap_pos()

        imgui.pop_style_var(1)
        return False, input_value

    some_int = 29
    line_count = input_value.count('\n') + 1
    line_height = imgui.get_text_line_height()
    text_height = imgui.calc_text_size(str(input_value))[1] + line_height * 2
    if line_count == 1:
        padding = imgui.get_style().frame_padding.y
        height = imgui.get_text_line_height() + padding

    else:
        text_bottom = draw_state.abs_top + text_height
        clamped_bottom = text_bottom
        height = clamped_bottom - draw_state.abs_top

    show_controls = True

    if not show_controls:
        imgui.push_style_var(imgui.STYLE_ALPHA, 0)

    if line_count == 1:
        if not wrap:
            item_width = draw_state.content_width - 1
        else:
            item_width = min_width

        if immediate_return:
            imgui.set_next_item_width(item_width)
            changed, value = imgui.input_text("##str", str(input_value))
        else:
            imgui.set_next_item_width(item_width)
            changed, value = imgui.input_text("##str", str(input_value),
                                              flags=imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
    else:
        imgui.set_cursor_screen_pos((snap_int(draw_state.abs_left), snap_int(draw_state.abs_top)))
        # disable scrolling
        changed, value = draw_text(str(input_value), name=draw_state.name +"##innder", 
                                    editable=True, with_header=draw_header,
                                   show_name=False, is_tree=False, temp=True)
        imgui.dummy(draw_state.content_width, text_height - height + 10)

    if not show_controls:
        imgui.pop_style_var(1)

    if changed:
        return True, value
    return changed, value


def sort_dict_alphabetically(input_value, **kwargs):
    changed = False
    attr_name = "name"
    first_item = next(iter(input_value.items()), None)[1]
    if hasattr(first_item, attr_name):
        sorted_dict = dict(sorted(input_value.items(), key=lambda item: str(getattr(item[1], attr_name)).lower()))
        return changed, sorted_dict
    else:
        imgui.text("Cannot sort: items do not have 'name' attribute")
        return False, input_value


@render_func()
def unsort_dict_alphabetically(input_value, ref=None, changed=False):
    if ref is None:
        imgui.text("Original order not available")
        return False, input_value
    else:
        # Ref is the original dict
        ref.update(input_value)
        return changed, ref


def param_source_matrix(input_value, keys=None, func=None, include_unmatched=False, **kwargs):
    """Pivot a render function's possible INPUTS into a parameter × source
    table — the inputs-tab aggregator. `input_value` is the collected sources,
    a {source_name: {param: value}} mapping (caller kwargs, @defaults on the
    model class, mode kwargs, @render_func decorator kwargs, signature
    defaults, …); `keys` is the function's parameter-name list (rows) — pass it
    directly, or pass `func` and they're derived via inspect (unwrapped, minus
    the catch-all params). Returns {param: {source_name: value}}:

        rows     one per parameter, in parameter order — an EMPTY row means no
                 source sets it (still shown: the point is mapping the full
                 input surface in one spot)
        columns  one per source that sets the param, in source order

    Cells alias the source values (no copies). With include_unmatched, keys a
    source sets that are NOT parameters append as extra rows at the end —
    typos and **kwargs ride-throughs stay visible instead of vanishing.
    Shaped like sort_dict_alphabetically: a plain (changed, value) chain node;
    apply_param_source_matrix below is the unsort-style reverse.
    Source COLOR-CODING is not this function's job: codecs carry a tint
    (new_codecs.render_kwargs) that core_render merges in as the lowest
    kwargs layer, so codec-backed values color themselves wherever drawn."""
    changed = False
    if isinstance(input_value, dict):
        items = list(input_value.items())
    else:
        items = [(f"source_{i}", s) for i, s in enumerate(input_value or ())]
    items = [(str(n), s) for n, s in items if isinstance(s, dict)]

    if keys is None and func is not None:
        try:
            keys = [p for p in inspect.signature(inspect.unwrap(func)).parameters
                    if p not in ("args", "kwargs", "o_kwargs", "next_kwargs")]
        except (TypeError, ValueError):
            keys = []
        # A render func's input surface is its signature PLUS the kwargs the
        # @render_func machinery itself consumes (width/height/tint/shadow and
        # the flag zoo) - shared by every render func, loaded once per process
        # by re-scanning the decorator source (render_func_kwarg_names).
        if getattr(func, "__render_func__", False):
            _seen = set(keys)
            keys += [k for k in render_func_kwarg_names() if k not in _seen]
    keys = list(keys or [])

    matrix = {}
    for k in keys:
        row = {}
        for sname, sdict in items:
            if k in sdict:
                row[sname] = sdict[k]
        matrix[k] = row
    if include_unmatched:
        for sname, sdict in items:
            for k in sdict:
                if k not in matrix or (k not in keys and sname not in matrix[k]):
                    matrix.setdefault(k, {})[sname] = sdict[k]
    return changed, matrix


# Attributes pinned into the signature section of the inputs matrix even
# though they come from the @render_func machinery, not the view function's
# own signature.
MATRIX_DEFAULT_PRIORITY = ("tint",)

# Framework-injected parameters: present in most view-function signatures but
# never user-tunable, so they don't belong in the signature section.
_MATRIX_FRAMEWORK_PARAMS = {"input_value", "draw_state", "args", "kwargs",
                            "o_kwargs", "next_kwargs", "meta", "viewstate",
                            "self", "unique", "changed"}


def signature_param_names(func):
    """The view function's OWN tunable parameters (unwrapped signature minus
    the framework-injected names) plus MATRIX_DEFAULT_PRIORITY — the rows
    pinned to the top section of the inputs matrix. For draw_float that's
    min_value/max_value/speed/…; tint rides along from the default list."""
    try:
        params = inspect.signature(inspect.unwrap(func)).parameters
    except (TypeError, ValueError):
        return []
    names = [p for p in params if p not in _MATRIX_FRAMEWORK_PARAMS]
    names += [k for k in MATRIX_DEFAULT_PRIORITY if k not in names]
    return names


@render_func()
def apply_param_source_matrix(input_value, ref=None, changed=False):
    """Reverse of param_source_matrix — the unsort_dict_alphabetically analog.
    `ref` is the ORIGINAL {source_name: dict} sources mapping; every edited
    cell writes back into the source dict it came from (a tint edited under
    the 'caller' column lands in the caller-kwargs dict), so each source's own
    save path can persist it. Sources absent from ref are left untouched."""
    if ref is None:
        imgui.text("Original sources not available")
        return False, input_value
    for param, row in input_value.items():
        if not isinstance(row, dict):
            continue
        for sname, val in row.items():
            src = ref.get(sname) if isinstance(ref, dict) else None
            # Skip already-equal cells: bubbling-wrapped source dicts mark
            # their host dirty on each write, so only real edits write back.
            if isinstance(src, dict) and (param not in src or src[param] is not val):
                src[param] = val
    return changed, ref


@render_func(use_cache=True, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, is_tree=True, temp=True, searchable=False)
def draw_param_matrix(input_value, wrap=True, search_text="", draw_state=None, source_tints=None, unique=None,
                      source_locations=None, priority_params=(), view_draw_state=None, **kwargs):
    """The inputs-tab matrix view: rows = parameters, cells = the sources that
    set them. Cells are parse FRAGMENTS (leaves pulled out of their codec's
    parse), so they can't naturally adopt the codec tint the way a whole
    codec-typed value does — this view is special: it looks the tint up per
    source (the tab maps each column to its codec) and applies it MANUALLY to
    every cell, alpha-boosted, so the data source is highly visible at a
    glance. Empty rows are skipped here — this is the provenance view; the
    full parameter surface is the matrix dict itself. Cell edits mutate the
    row in place and report changed, for apply_param_source_matrix write-back.

    `priority_params` (see signature_param_names) pins those rows into a top
    section — the view function's own signature params plus the default list —
    divided from the remaining machinery/unmatched rows by a separator.

    `search_text` filters the rows by parameter name (the input tab's
    always-on filter box feeds it): exact substring for short terms, the
    shared typo-tolerant matcher (_fuzzy_key_match) for longer ones."""
    changed = False
    tints = source_tints or {}
    locations = source_locations or {}
    folder_icon = "\uf07b"  # FA folder -- explicit escape, see jump_to.py
    # Uniform label/button width across every cell so the values align into a
    # column no matter how long each source's name is.
    _snames = {sn for r in input_value.values() if isinstance(r, dict) for sn in r}
    btn_w = max((imgui.calc_text_size(f"{folder_icon} {sn}")[0] for sn in _snames),
                default=0.0) + 15
    font = Font.JETBRAINS_MONO_22
    _hdr_font = Core.melty.font_mgr.get(font) if Core.melty.font_mgr else None

    search_q = str(search_text or "").strip().lower()

    def _shown(param, row):
        return isinstance(row, dict) and row and not param.startswith('_')

    prio_set = set(priority_params or ())
    prio_items = [(p, input_value[p]) for p in (priority_params or ())
                  if p in input_value and _shown(p, input_value[p])]
    rest_items = [(p, r) for p, r in input_value.items()
                  if p not in prio_set and _shown(p, r)]

    if search_q:
        prio_items = [(p, r) for p, r in prio_items
                      if _fuzzy_key_match(search_q, p.lower())]
        rest_items = [(p, r) for p, r in rest_items
                      if _fuzzy_key_match(search_q, p.lower())]

    def _draw_rows(items, name_alpha=1.0):
        nonlocal changed
        for param, row in items:
            # One row per attribute name: the name once, in a slightly larger
            # font, with every source's value grouped & indented beneath it.
            imgui.dummy(0, 2)
            if _hdr_font is not None:
                imgui.push_font(_hdr_font)

            param = param[:min(len(param), 17)]

            imgui.text_colored(param, 1,1,1, name_alpha)
            if _hdr_font is not None:
                imgui.pop_font()

            imgui.same_line()
            indent_size = 174
            imgui.indent(indent_size)
            for sname, val in row.items():
                tint = tints.get(sname)

                # The source label IS the jump button: a single-width button naming
                # the source (draw_text / @render_func(...) / @defaults(...))
                # that opens its file in the IDE; the value sits on the same
                # line with show_name=False, so one element does both the
                # labeling and the navigation.
                tint_kwargs = {"alpha": 0.0, "tint": tint} if tint else {}
                clicked = button(f"{folder_icon} {sname}", width=btn_w, height=22, shadow=True,
                                    text_saturation=0.9, use_cache=True, text_align="left", text_value=0.389,
                                 name=f"jump_{sname}##{param}_{unique}", show_button_bg=True,
                                 **tint_kwargs)[0]
                loc = locations.get(sname)
                if clicked and loc:
                    from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
                    threading.Thread(target=open_in_intellij, args=(str(loc[0]),),
                                     kwargs={"line_number": loc[1]},
                                     daemon=True).start()
                imgui.same_line()

                # key routes the cell by ATTRIBUTE name (a tint cell gets the
                # swatch/picker, not draw_collection); the SOURCE stays in place
                # identity via name while the button above displays it.
                ch, nv = draw_any(val, name=f"{sname}##{param}_{unique}",
                                  key=param,
                                  tint=tint,
                                  show_name=False, wrap=True,
                                  bg_offset=2, z_offset=0, disable_scroll=True)
                if ch:
                    row[sname] = nv
                    changed = True

                imgui.dummy(0, 1)
            imgui.unindent(indent_size)

    # While filtered, drop a section header whose rows all filtered out.
    if prio_items or not search_q:
        draw_text(f"def {view_draw_state._view_func.__name__}",
                    is_tree=False, editable=False, width=draw_state.content_width - 14,
                    font=Font.JETBRAINS_MONO_30)
    _draw_rows(prio_items, name_alpha=1.0)
    if prio_items and rest_items:
        imgui.dummy(0, 16)
        imgui.dummy(0, 16)

    if rest_items or not search_q:
        draw_text(f"core_render.py",
                is_tree=False, editable=False, width=draw_state.content_width - 15,
                                font=Font.JETBRAINS_MONO_30)
    _draw_rows(rest_items, name_alpha=0.2)
    if search_q and not prio_items and not rest_items:
        imgui.text_colored(f"no parameters match '{search_q}'", 1, 1, 1, 0.3)
    return changed, input_value


@render_func(is_default_for=UsageRef, use_cache=True, shadow=True, z_offset=2, show_bg=True, with_header=draw_header,
             is_tree=True, tint=(0.11, 0.1, 0.16))
def draw_usage(input_value: UsageRef):
    imgui.text(
        f"{input_value.path} {input_value.line}:{input_value.column} {input_value.scope} {input_value.module_name}")

    return False, input_value


@render_func(is_default_for=(Comment), shadow=False, indent_size=4, selectable=False, use_cache=True,
             show_bg=False, with_header=None, is_tree=True, temp=True)
def draw_comment(input_value: Comment, draw_state, style_manager, cursor_hover=False):
    changed, value = False, input_value

    imgui.dummy(0, 4)
    depth = max(0.0, Core.melty.bg_depth)
    depth_scale = 0.067
    name_style = {
        'value': 0.143, 'saturation': 0.92,
        'alpha': 0.014, 'max_value': 0.787,
        'depth_factor': 0.741
    }
    depth_intensity = float(depth) * depth_scale
    name_style['value'] = depth_intensity * name_style['depth_factor'] + name_style['value']
    alpha = 0.4
    sat_depth_factor = 0.0
    sat_depth_offset = 0.188
    sat_shift = float(depth + sat_depth_offset) * sat_depth_factor
    name_style['saturation'] = name_style['saturation'] + sat_shift

    name_color = style_manager.make_color_style_value(input=name_style)

    imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
    imgui.push_style_color(imgui.COLOR_TEXT, *name_color[:3], alpha)
    imgui.text_wrapped(str(input_value[2:]))
    imgui.pop_style_color()
    imgui.pop_text_wrap_pos()

    if changed:
        return True, value


@render_func(use_cache=False, show_bg=True, shadow=False, selectable=False, with_header=None)
def draw_color_picker(input_value, wrap=True, draw_state=None, **kwargs):
    """Large immediate-mode HSV colour picker: a saturation/value square plus a
    hue bar. `input_value` is a 3- or 4-float RGB(A) tuple in 0..1; returns
    (changed, new_tuple). Entirely stateless — HSV is derived from the value each
    frame and the edit written straight back (imgui's own is_item_active tracks
    the drag), so it can be dropped anywhere; draw_tuple wraps it in Mode.POPOVER."""
    imgui.dummy(0,3)
    vals = list(input_value)
    has_alpha = len(vals) >= 4
    r, g, b = float(vals[0]), float(vals[1]), float(vals[2])
    a = float(vals[3]) if has_alpha else 1.0
    h, s, v = imgui.color_convert_rgb_to_hsv(r, g, b)

    SQ, BAR_W, GAP = 180, 18, 8
    dl = imgui.get_window_draw_list()
    white = imgui.get_color_u32_rgba(1, 1, 1, 1)
    black = imgui.get_color_u32_rgba(0, 0, 0, 1)
    trans = imgui.get_color_u32_rgba(0, 0, 0, 0)
    changed = False
    hsv_changed = False  # only convert HSV->RGB when the square/hue is actually changed

    # Everything renders in NATURAL imgui flow (invisible_button advances the
    # cursor, same_line for the hue bar, plain widgets below). That keeps imgui's
    # content-size tracking honest so the auto-resized popover grows to match the
    # square + channel rows + hex.

    # --- SV square: white->hue across, transparent->black down ---
    sx0, sy0 = imgui.get_cursor_screen_pos()
    hr, hg, hb = imgui.color_convert_hsv_to_rgb(h, 1.0, 1.0)
    hue = imgui.get_color_u32_rgba(hr, hg, hb, 1)
    dl.add_rect_filled_multicolor(sx0, sy0, sx0 + SQ, sy0 + SQ, white, hue, hue, white)
    dl.add_rect_filled_multicolor(sx0, sy0, sx0 + SQ, sy0 + SQ, trans, trans, black, black)
    imgui.invisible_button("##sv", SQ, SQ)
    if imgui.is_item_active():
        mx, my = imgui.get_mouse_pos()
        s = min(max((mx - sx0) / SQ, 0.0), 1.0)
        v = 1.0 - min(max((my - sy0) / SQ, 0.0), 1.0)
        hsv_changed = True

    # --- Hue bar to its right, 6 gradient segments ---
    imgui.same_line(spacing=GAP)
    hx0, hy0 = imgui.get_cursor_screen_pos()
    for i in range(6):
        t0, t1 = i / 6.0, (i + 1) / 6.0
        r0, g0, b0 = imgui.color_convert_hsv_to_rgb(t0, 1, 1)
        r1, g1, b1 = imgui.color_convert_hsv_to_rgb(t1, 1, 1)
        c0 = imgui.get_color_u32_rgba(r0, g0, b0, 1)
        c1 = imgui.get_color_u32_rgba(r1, g1, b1, 1)
        dl.add_rect_filled_multicolor(hx0, hy0 + SQ * t0, hx0 + BAR_W, hy0 + SQ * t1, c0, c0, c1, c1)
    imgui.invisible_button("##hue", BAR_W, SQ)
    if imgui.is_item_active():
        h = min(max((imgui.get_mouse_pos()[1] - hy0) / SQ, 0.0), 1.0)
        hsv_changed = True

    # --- Markers (after input, at the current position) ---
    cx, cy = sx0 + s * SQ, sy0 + (1.0 - v) * SQ
    dl.add_circle(cx, cy, 6, black, thickness=1.0)
    dl.add_circle(cx, cy, 5, white, thickness=1.5)
    hmy = hy0 + h * SQ
    dl.add_rect(hx0 - 1, hmy - 2, hx0 + BAR_W + 1, hmy + 2, white, thickness=1.5)

    # Fold the SV/hue edit back to RGB only when the square or hue bar was just
    # dragged. Otherwise keep the original input RGB untouched - round-tripping
    # RGB->HSV->RGB every frame accumulates conversion error and feeds it back as
    # next frame's input, which is what made the RGB sliders jitter.
    if hsv_changed:
        r, g, b = imgui.color_convert_hsv_to_rgb(h, s, v)
        changed = True

    # --- RGBA drag floats (natural flow, below the square row). Dragging a
    # channel edits RGB directly, overriding the HSV-derived value this frame. ---
    imgui.dummy(0, 4)
    imgui.push_item_width(SQ + GAP + BAR_W)
    out = []
    for lbl, cur in ([("R", r), ("G", g), ("B", b)] + ([("A", a)] if has_alpha else [])):
        imgui.set_next_item_width(draw_state.content_width - 30)
        ch, nv = imgui.drag_float(f"{lbl}##cp_{lbl}", cur, 0.004, 0.0, 1.0, "%.3f")
        if ch:
            changed = True
        out.append(min(max(nv, 0.0), 1.0))
        imgui.dummy(0,1)
    imgui.pop_item_width()
    r, g, b = out[0], out[1], out[2]
    if has_alpha:
        a = out[3]

    # --- Hex code label (#rrggbb, +a with alpha) ---
    ri, gi, bi = (int(round(c * 255)) for c in (r, g, b))
    hex_str = (f"#{ri:02x}{gi:02x}{bi:02x}{int(round(a * 255)):02x}"
               if has_alpha else f"#{ri:02x}{gi:02x}{bi:02x}")
    imgui.text(hex_str)

    if changed:
        request_render()
        return True, ((r, g, b, a) if has_alpha else (r, g, b))
    return False, input_value


@render_func(is_default_for=('tint', 'help_yellow_tint', 'context_select_tint', "text_color"), has_popup=True,
             indent_size=2, is_tree=False, align_header=False, header_same_line=True, wrap=True,
             show_name=True, selectable=False, max_width=100, min_width=33, use_cache=False, with_header=draw_header)
def draw_tuple(input_value: tuple, name, unique, draw_state):
    changed = False
    is_color = (len(input_value) in (3, 4)
                and all(isinstance(c, (float, int)) for c in input_value))
    if is_color:
        # A swatch trigger that opens our own colour-picker popover (replacing
        # imgui's built-in popup). Same popover pattern as the dropdown: identity
        # in Melty.popover_focused_ds is the open state; click toggles it; the
        # picker window is anchored under the swatch and dismissed on outside
        # click / Esc. The picker itself is stateless and returns the new colour.
        from src.lsd.gl_gui.view.mode import Mode
        is_open = Melty.popover_focused_ds is draw_state
        col = list(input_value)
        alpha = col[3] if len(col) == 4 else 1.0
        imgui.same_line(spacing=4)
        flags = imgui.COLOR_EDIT_NO_TOOLTIP
        if imgui.color_button(f"##swatch{unique}{name}", col[0], col[1], col[2], alpha,
                              flags=flags, width=0, height=18):
            Melty.popover_focused_ds = None if is_open else draw_state
            if not is_open:
                Melty._popover_open_frame = Melty.frame_count  # grace the opening click
            request_render()
        is_open = Melty.popover_focused_ds is draw_state  # reflect the update this frame

        # The picker popover is closable and fixed size (auto-resize is off for
        # closable windows), and the content is raw imgui (not child render_funcs)
        # so the framework can't measure it. Size the window to fit the SV square
        # (180) + the N RGBA drag-float rows + the hex label, so nothing clips.
        picker_h = 180 + 14 + len(input_value) * 26 + 26
        # parent_window=draw_state anchors the popover under the swatch AND makes
        # the tuple the picker's ancestor, so clear_focus (which searches the
        # clicked view's ancestor closure) keeps the popover open when you click
        # inside it, and dismisses it when you click anywhere else.
        color_changed, new_color = draw_color_picker(input_value, name=f"color_picker{unique}",
                                                closed=not is_open, window_pos=(0, 10),
                                                parent_window=draw_state, width=216, height=picker_h, mode=Mode.POPOVER)
        if is_open:
            if color_changed:
                input_value = tuple(new_color)
                changed = True
            # Dismiss on a click outside the swatch/popover, or on Esc.
            # if imgui.is_mouse_clicked(0):
            #     mx, my = imgui.get_mouse_pos()
            #     if not any(_ds_in_subtree(d, draw_state) for d in Core.melty.bvh_query(mx, my)):
            #         Melty.popover_focused_ds = None
            #         request_render()
            if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
                Melty.popover_focused_ds = None
                request_render()
            # Keep re-rendering while a bar/square is being dragged so the live
            # imgui interaction (is_item_active) updates each frame.
            if Melty.imgui_any_item_active or imgui.is_mouse_down(0):
                Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
                request_render()
                
    elif len(input_value) > 0 and isinstance(input_value[0], (float, int)):
        str_value = ", ".join([str(v) for v in input_value])
        ch, input_str = imgui.input_text("##tuple", str_value)
        if ch:
            try:
                new_tuple = eval(f"({input_str},)")
                if isinstance(new_tuple, tuple):
                    input_value = new_tuple
                    changed = True
            except Exception:
                pass
    else:
        changed, input_value = draw_collection(input_value=input_value)

    return changed, input_value


class TestClass(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = 2
        self.str_val = "Test"


@render_func
def draw_float_ctx(input_value):
    imgui.text('Float content menu')
    imgui.dummy(30, 30)
    draw_float(0.0, name="test")
    imgui.text(f"WxH {input_value.width} {input_value.height}")
    imgui.text(f"Content width {input_value.content_width} {input_value.height}")

    imgui.text(f"Abs Left/Top {input_value.abs_left} {input_value.abs_top}")
    imgui.text(f"Header WxH {input_value.header_width} {input_value.header_height}")

    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_rect(upper_left_x=input_value.abs_left, upper_left_y=input_value.abs_top,
                       lower_right_x=input_value.abs_left + input_value.width,
                       lower_right_y=input_value.abs_top + input_value.height,
                       col=imgui.get_color_u32_rgba(1, 0, 0, 0.5), thickness=1.0)


@render_func(is_default_for=float, use_cache=True, shadow=False,
             is_tree=False, show_bg=False,
             with_header=draw_header, temp=True)
def draw_float(input_value: float,
               draw_state,
               wrap=False,
               min_width=80,
               min_value=-98.703,
               max_value=99.264,
               speed=0.0042):
    
    if not wrap:
        imgui.set_next_item_width(draw_state.content_width)
    else:
        imgui.set_next_item_width(min_width)
    changed, value = imgui.drag_float("", input_value,
                                      format='%.3f',
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)

    if changed:
        return True, value

    return False, input_value


@render_func(is_default_for=(Parameter), wraps=render_func, with_header=draw_header)
def draw_parameter(input_value):
    parameter_default = input_value.default
    if parameter_default is inspect.Parameter.empty:
        imgui.same_line()
        imgui.text("<No Default>")
    else:
        return draw_any(parameter_default, show_name=False, show_add_delete=False)


@render_func(is_default_for=(types.MappingProxyType), shadow=False, show_bg=False, show_add_delete=False,
             with_header=draw_header)
def draw_mapping_proxy(input_value):
    # To list first, then back to mapping proxy
    try:
        dict_values = dict(input_value)
        changed, new_dict = draw_collection(dict_values, show_bg=False, indent_size=0, show_header=False,
                                            show_add_delete=False)
        if changed:
            return True, types.MappingProxyType(new_dict)

    except Exception as e:
        imgui.text(f"Error converting MappingProxyType to dict: {e}")
        return False, input_value

    return changed, input_value


@render_func(wraps=render_func, show_add_delete=False, with_header=draw_header)
def eval_function(input_value, draw_state):
    signature = inspect.signature(input_value)
    params = signature.parameters
    changed, new_val = draw_any(params, name="Parameters", show_add_delete=False)
    if changed:
        set_fn_defaults(input_value, new_val)

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))
    push_style_var(imgui.STYLE_FRAME_PADDING, (8, 6))
    push_style_var(imgui.STYLE_FRAME_ROUNDING, 6)

    function_args = inspect.signature(input_value).parameters
    kwargs = {}
    for name, param in function_args.items():
        if param.default is not inspect.Parameter.empty:
            kwargs[name] = param.default
        else:
            kwargs[name] = None
    try:
        result = input_value(**kwargs)
        draw_any(result, name="Result", show_header=True, show_add_delete=False)
        if draw_state._result != result:
            Core.melty.cache.invalidate_all()
        draw_state._result = result

    except Exception as e:
        print(f"Error calling function '{input_value.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())

    pop_style_var(3)

    return changed, input_value


@render_func(is_default_for="LSDStudio", show_bg=True, tint=(0.6, 0.2, 0.8), with_header=draw_header)
def draw_lsd_studio(input_val):
    imgui.text("An LSD Studio Instance")


@render_func(is_default_for="ImGuiStyleManager", tint=(0.8, 0.7, 0), use_cache=True, with_header=None)
def draw_style_manager(input_val):
    text("Style Manager", wrap=True, height=21)
    return False, input_val


@render_func(is_default_for="Melty", use_cache=True, with_header=None)
def draw_vis(input_val):
    imgui.text("Melty")
    return False, input_val


@render_func(is_default_for="AppModel", show_bg=True, tint=(0.6, 0.2, 0.8), with_header=draw_header)
def draw_app_model(input_val):
    imgui.text("An App Model Instance")


def _format_run_error(exc):
    """One compact, UI-ready error: `Type: message`, then the deepest
    traceback frame in PROJECT code (site-packages/stdlib frames are where
    the error SURFACED, not where it's fixable) with its source line."""
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    target = None
    for fr in reversed(frames):
        if "site-packages" not in fr.filename and "/lib/python" not in fr.filename:
            target = fr
            break
    if target is None and frames:
        target = frames[-1]
    text = f"{type(exc).__name__}: {exc}"
    if target is not None:
        text += f"\n{Path(target.filename).name}:{target.lineno} in {target.name}"
        if target.line:
            text += f"\n    {target.line}"
    return text


@render_func(is_default_for=(types.FunctionType, types.MethodType), shadow=True, use_cache=True, show_add_delete=False, selectable=False, show_bg=True,
             parent_show_add_delete=False, is_tree=False, show_name=False, with_header=draw_header)
def draw_function(input_value, name, draw_state, unique, auto_run=None, wrap=False,
                  show_run_button=True, **kwargs):
    """`auto_run`: opt-in compile-and-run — pass any comparable version token
    (e.g. id(fn.__code__)); the function runs whenever the token CHANGES or a
    parameter is edited, no button click. The token is stored before running
    so a throwing function doesn't retry every frame. `show_run_button=False`
    drops the named run button (the streamlined live-lab look)."""
    if not callable(input_value):
        imgui.text("Not a callable function")
        return False, input_value
    params_edited = False
    try:
        signature = inspect.signature(input_value)
        params = signature.parameters
        if len(draw_state.params) != len(params):
            param_dict = {}

            for name, param in params.items():
                if name == 'kwargs':
                    continue
                if param.default is not inspect.Parameter.empty:
                    param_dict[name] = param.default
                else:
                    param_type = param.annotation
                    default_value = param.default
                    if default_value is not inspect.Parameter.empty:
                        param_dict[name] = default_value
                    else:
                        if name in Core.melty.global_attrs:
                            param_dict[name] = Core.melty.global_attrs[name]

            draw_state.params = param_dict
        if len(draw_state.params) > 0:
            changed, new_val = draw_collection(draw_state.params, name="Parameters", initial={"expanded":False},
                                               show_add_delete=False, shadow=False, z_offset=0, parent_show_add_delete=False, 
                                               horizontal=True, wrap=wrap,
                                               child_kwargs={"max_width": 200, "shadow":False,
                                                             "show_bg": False, "use_cache": True, "z_offset": 0.0
                                                             })
            if changed:
                draw_state.params = new_val
                params_edited = True
    except Exception as e:
        imgui.text(f"Error inspecting function parameters: {e}")
        draw_state.params = {}

        sees_this = 0

    def _run():
        try:
            draw_state.result = input_value(**draw_state.params)
            draw_state.misc.pop("_run_error", None)
            Core.melty.cache.invalidate_up_current(force=True)
        except Exception as e:
            # Surfaced in the UI (red text where the result goes), anchored at
            # the deepest frame in PROJECT code - the line the user can fix.
            draw_state.misc["_run_error"] = _format_run_error(e)
            Core.melty.cache.invalidate_up_current(force=True)
            print(f"Error calling function '{input_value.__name__}': {e}")
            print_colored_traceback(*sys.exc_info())

    if auto_run is not None and (params_edited
                                 or draw_state.misc.get("_auto_run_ver") != auto_run):
        draw_state.misc["_auto_run_ver"] = auto_run
        _run()

    if show_run_button and button(f"{input_value.__name__}##{unique}", height=29,
                                  bg_offset=0, tint=(0.021, 0.104, 0.167, 0.0))[0]:
        _run()

    run_error = draw_state.misc.get("_run_error")
    if run_error:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_colored(run_error, 1.0, 0.45, 0.40, 1.0)
        imgui.pop_text_wrap_pos()

    if draw_state.result is not None:
        draw_any(draw_state.result, name="Result", header_same_line=True, show_header=False, show_add_delete=False)

    # pop_style_var(3)

    return False, input_value


@render_func(is_default_for=(int), shadow=False, use_cache=False, wrap=False, header_same_line=True,
             is_tree=False, with_header=draw_header, align_header=True, temp=True)
def draw_int(input_value: int, draw_state=None, min_width=80, wrap=False, min_value=-1000.0, max_value=1000.0, speed=0.1, unique=0):
    if not wrap:
        imgui.set_next_item_width(draw_state.content_width)
    else:
        imgui.set_next_item_width(min_width)
        
    max_int = 2147483647
    if input_value < max_int:
        changed, value = imgui.drag_int("##int", input_value,
                                        change_speed=speed,
                                        min_value=min_value,
                                        max_value=max_value)
        if changed:
            return True, value

        return changed, value
    return False, input_value


@render_func(show_header=False, show_name=False, show_bg=True, with_header=draw_header)
def draw_debug_label(input_value: str):
    imgui.text(input_value)


@render_func(is_default_for=Enum, is_tree=False, shadow=False, align_header=False,
             selectable=False, header_same_line=True, show_add_delete=False,
             parent_show_add_delete=False, with_header=draw_header, temp=True)
def draw_enum(input_value: Enum, draw_state=None, unique=0, style_manager=None, enum_tint=(0.3, 0.3, 0.3)):
    # Delegate to draw_tab_bar so enums get its wrapping + styling for free.
    # Enums are single-select: pass the current value as the lone selection and
    # render every member as a tab; names are the prettified member names.
    options = list(input_value.__class__)

    if len(options) > 4:
        changed, selection = draw_dropdown(input_value, collection=options, show_header=False,
                                           name=f"{input_value.__class__.__name__}##{unique}enum")

        if changed:
            return True, selection
    else:

        names = [opt.name.replace("_", " ").capitalize() for opt in options]
        changed, selected = draw_tab_bar([input_value], collection=options, names=names, name=f"{unique}_enum", wrap=True,
                                          z_offset=-1, rounding=5, as_toggles=False, bg_offset=-3)
        if changed and selected:
            return True, selected[0]
    return False, input_value


@render_func(is_tree=False, show_bg=True, shadow=False, use_cache=True, z_offset=0, header_same_line=True,
             disable_scroll=True,
             indent_size=0, show_add_delete=False, show_name=False, selectable=False, parent_show_add_delete=False,
             with_header=draw_header)
def draw_tab_bar(input_value: list, tab_height=30, names=None, tint_value=0.235, tint_saturation=0.372, unique=None,
                 collection=None, as_toggles=False, tints=None, draw_state=None):
    """Tab bar with multi-select via shift-click. input_value is the list of selected items, collection is all available tabs.
    Tabs wrap onto a new row when the cumulative width would exceed draw_state.content_width.

    tints: optional list of (r, g, b) tint colors, one per tab in `collection`. Entries that are
    None (or beyond the list) fall back to the neutral grey. (Defaults to None rather than [] to
    avoid the mutable-default-arg pitfall; behaves identically to an empty list.)"""
    if collection is None:
        return False, input_value
        
    imgui.dummy(0,0)
    imgui.same_line()

    io = imgui.get_io()
    changed = False
    selected = list(input_value)
    if selected is None:
        selected = []
    if names is None and hasattr(input_value, 'keys') and hasattr(input_value, 'values'):
        input_value = list(input_value.values())
        names = list(input_value.keys())

    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 10, imgui.get_cursor_screen_pos()[1]))

    # push_style_var(imgui.STYLE_ITEM_SPACING, (2, 0))

    # Mirror button()'s sizing: width = calc_text_size(label_text).x + 15.
    # spacing matches STYLE_ITEM_SPACING.x set above.
    spacing = 2
    button_padding = 15
    content_width = draw_state.content_width if draw_state is not None else 0
    row_width = 0.0

    def _tab_text(t):
        return t.name if hasattr(t, 'name') else str(t)

    for i, tab in enumerate(collection):
        raw = _tab_text(tab)
        # label_text = raw.replace("_", " ")
        label = f"{raw}"
        if names is not None and i < len(names):
            label = f"{names[i]}"
        active = tab in selected

        tab_color = (0.5, 0.5, 0.5)
        tinted = tints is not None and i < len(tints) and tints[i] is not None
        if tinted:
            tab_color = tints[i]

        new_value = 0.15 if not tinted else 0.1

        # make_color_rgb mixes `color` toward the theme color by `factor`; factor=1.0 (button's
        # default) discards `color` entirely. Drop factor for tinted tabs so the tint shows, and
        # give inactive tinted tabs a faint fill (the default alpha=0.0 draws no rect at all).
        tab_factor = 0.30 if tinted else 1.2

        tab_width = imgui.calc_text_size(label.split("##")[0]).x + button_padding

        if active:
            selected_value = 0.23
            clicked = button(label, z_offset=2, name=f"tab_{i}_{unique}",
                             height=tab_height - 3, tint_value=new_value + selected_value - 0.03,
                             color=tab_color, factor=tab_factor, draw=True)[0]
        else:
            saturation = 1.0 if tinted else 0.3
            clicked = button(label, indent_size=0, height=tab_height, draw=True, z_offset=0.0,
                             alpha=0.0 if tinted else 0.0, tint_value=new_value if not tinted else 0.1, saturation=saturation,
                             name=f"tab_{i}_{unique}_deactivated", color=tab_color, factor=tab_factor,
                             text_value=1.0 if not tinted else 0.9,
                             shadow=False)[0]

        if clicked:
            changed = True
            if io.key_shift or as_toggles:
                if active:
                    selected.remove(tab)
                else:
                    selected.append(tab)
            else:
                selected = [tab]

        row_width = tab_width if row_width == 0 else row_width + spacing + tab_width

        # Look ahead: if the next tab won't fit on this row, skip same_line()
        # so imgui's cursor flows to the next line, and reset row_width.
        if i < len(collection) - 1:
            label = names[i + 1] if names is not None and i + 1 < len(names) else _tab_text(collection[i + 1])
            next_w = imgui.calc_text_size(label).x + button_padding
            if content_width > 0 and row_width + spacing + next_w + 20 > content_width:
                row_width = 0
                continue

        same_line()

    imgui.dummy(0, 0)

    # pop_style_var(1)

    if changed:
        Core.melty.refresh_nested_windows(draw_state)

    return changed, selected


@render_func(with_header=draw_header, is_tree=False, shadow=False)
def draw_enum_tabs(input_value: type, tab_state: TabState):
    enum_states = list(input_value)
    selected = tab_state.selected_tabs

    changed, new_selected = draw_tab_bar(selected, collection=enum_states)
    if changed:
        tab_state.selected_tabs = new_selected

    return False, input_value


def draw_debug(x, y, label, color=(1, 0, 0), size=16):
    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_circle_filled(x, y, size, imgui.get_color_u32_rgba(*color, 1.0))
    draw_list.add_text(x + size + 2, y - size / 2, imgui.get_color_u32_rgba(*color, 1.0), label)


def draw_lens(lens, draw_state):
    """Render a single Lens against draw_state: resolve its root, then either
    focus the live leaf in place (in-place kinds) or run its generated
    parse→focus→save chain (code kinds). Returns (changed, _)."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import focus
    root = lens.root(draw_state)
    if root is None:
        imgui.text_colored(f"{lens.kind or lens.label}: n/a here", 0.5, 0.5, 0.5)
        return False, root
    if lens.chain is None:
        return focus(root, path=lens.path, default=lens.default, kind=lens.kind, name=lens.label + lens.name)
    return draw_any(root, chain=lens.chain(root), name=lens.label)


@render_func(use_cache=True, show_bg=True, selectable=False)
def draw_tint_context(input_value: DrawState, tab_state: TabState = None, **kwargs):
    """Render every tint source as its own picker. Each lens in
    LENSES_BY_ATTR["tint"] is shown via the same focus/draw_tuple machinery;
    present sources get a color picker, absent ones get a "+ Add". No precedence
    or selection — just one row per source of tint."""
    from src.lsd.gl_gui.view.mode import LENSES_BY_ATTR
    ds = input_value
    changed = False
    for lens in LENSES_BY_ATTR.get("tint", []):
        c, _ = draw_lens(lens, ds)
        changed = changed or c
        imgui.separator()
    return changed, None


@window
@render_func()
def context_menu_settings(input_value, draw_state):
    code_file_io(draw_context_menu, mode=Modes.NEW_CODE)


def run_scoped_eval(code, view_func, draw_state, local_vars):
    """Run `code` with the view function's ACTUAL call-time locals in scope.

    Called from the render wrapper (core_render) right before it invokes the
    view function, so `local_vars` is the exact set of arguments the function is
    about to receive -- its initial locals (`input_value`, `draw_state`, and
    every kwarg by name). On top of those we layer the function's module globals
    (so the snippet resolves the same free names the body would) plus the `value`
    and `ds` aliases. Locals win over globals, mirroring normal scoping.

    Delegates to the same eval/exec + stdout-capture machinery the MCP
    `eval_python` tool uses, so a trailing expression's repr comes back alongside
    any printed output. The namespace is a fresh dict layered over a *copy* of
    the module globals, so assignments in the snippet don't leak back into the
    module.
    """
    from src.lsd.gl_gui.mcp_eval import _run_code
    ns = {}
    if view_func is not None:
        # Same free names the function body resolves.
        ns.update(getattr(view_func, "__globals__", {}))
    if local_vars:
        ns.update(local_vars)  # the view function's call-time locals
    ns.setdefault("draw_state", draw_state)
    ns.setdefault("ds", ns.get("draw_state"))
    ns.setdefault("value", ns.get("input_value"))
    out, result, error = _run_code(code, ns)
    parts = []
    if out.strip():
        parts.append(out.rstrip())
    if result is not None:
        parts.append("=> " + result)
    if error:
        parts.append(error.rstrip())
    return "\n".join(parts) if parts else "(no output)"


# ── Context menu tabs ────────────────────────────────────────────────────────
# Each tab body is its own render_func. draw_context_menu places one per selected
# column by calling it with column=t_idx; the tab then owns a single-column
# region, so the inner views inside it no longer pass column themselves.

@render_func(use_cache=True, show_bg=False, show_header=False, disable_scroll=False, searchable=True, show_name=False, selectable=False)
def draw_info_tab(input_value, search_text='', unique=None, **kwargs):
    """Read-only dump of the inspected view's draw_state fields. `search_text`
    (the menu's resolved search term) probes an arbitrary kwarg/attr by name."""
    info_items = ["name", "searchable", "scroll_disabled", "_default_view_func", "column", "closable", "current_mode",
                  "mode",
                  "show_add_delete", "_source", "window_pos", "left", "top", "width", "height", "content_height",
                  "scroll_offset",
                  "final_max_column", "_column_cursor", "_content_rect", "_max_column_index", "_outside_column_height",
                  "disable_scroll"]

    if search_text is None or search_text == "":
        item_value = ""
    elif search_text in input_value._kwargs:
        item_value = input_value._kwargs.get(search_text, 'Not found')
    elif search_text in input_value.__dict__:
        item_value = getattr(input_value, search_text, 'Not found')
    else:
        item_value = 'Not found'

    imgui.spacing()
    text(str(item_value), show_bg=False, tint=(0.5, 0.5, 0.0), show_header=True, show_name=True,
         wrap=False, name=f"{search_text}##it", editable=False)

    draw_str(str(len(input_value._view_children)),
             show_bg=True, tint=(0.1, 0.01, 0.4), show_header=True, wrap=False, show_name=True,
             name=f"._view_children##{unique}", editable=False)
    draw_str(str(input_value.scroll_visible),
             show_bg=True, tint=(0.1, 0.01, 0.4), show_header=True, wrap=False, show_name=True,
             name=f"scroll_enabled##{unique}", editable=False)

    draw_str(str(input_value.abs_clipped_height),
             show_bg=True, tint=(0.1, 0.01, 0.4), show_header=True, wrap=False, show_name=True,
             name=f"abs_clip_height##{unique}", editable=False)
    draw_str(str(input_value._observed_content_height),
             show_bg=True, tint=(0.1, 0.01, 0.4), show_header=True, wrap=False, show_name=True,
             name=f"_observed_content_height##{unique}", editable=False)

    draw_str(str(input_value.abs_content_height),
             show_bg=True, tint=(0.1, 0.01, 0.4), show_header=True, wrap=False, show_name=True,
             name=f"abs_content_height##{unique}", editable=False)
    text(f"{input_value._view_func.__name__}", show_bg=True, show_name=True, show_header=True, wrap=True,
         name="Rendered by", editable=False, tint=(0.84, 0.68, 0.639))
    text(f"{type(input_value._raw_input_value).__name__}", show_name=True,
         show_header=True, name="input_value type", editable=False)

    text(f"{input_value.window_index}", show_name=True, show_header=True, name="window_index",
         editable=False, tint=(0.8, 0.8, 0.2))

    text(f"{input_value._default_view_func}", show_name=True, name="default_view_func", editable=False)

    text(f"{input_value._kwargs.get('real_type', None)}", show_name=True, name="kwargs type", editable=False)

    text(f"{input_value._kwargs.get('type_collection', None)}", show_name=True,
         name="kwargs collection type", editable=False)

    for info_item in info_items:
        if info_item in input_value._kwargs:
            item_value = input_value._kwargs.get(info_item, 'Not found')
        elif info_item in input_value.__dict__:
            item_value = getattr(input_value, info_item, 'Not found')
        else:
            item_value = 'Not found'

        if isinstance(item_value, (int, float, str, bool, Enum)):
            text(f"{item_value}", name=info_item, show_name=True, show_header=True, editable=False)
        else:
            draw_any(item_value, name=info_item, show_name=True,
                     show_header=True, show_add_delete=False, draw=True)

    if button("print_stack_trace")[0]:
        print_stack_trace()
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_config_tab(input_value, **kwargs):
    """List the inspected view function's configurable parameters and their
    current values (kwarg override, else signature default)."""
    view_func = input_value._view_func
    if view_func is None:
        text("No view function")
        return False, input_value

    # Unwrap the @render_func wrapper to read the original signature.
    raw_func = getattr(view_func, '__wrapped__', view_func)
    sig = inspect.signature(raw_func)
    ds_kwargs = input_value._kwargs or {}
    # Framework-injected params the user doesn't configure.
    skip_params = {"input_value", "draw_state", "args", "o_kwargs",
                   "kwargs", "meta", "viewstate", "self"}
    for param_name, param in sig.parameters.items():
        if param_name in skip_params:
            if param_name in ds_kwargs:
                param_value = ds_kwargs[param_name]
                text(f"{param.__class__.__name__}", name=param_name,
                     editable=False, tint=(0.8, 0.8, 0.2))
            continue

        if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD):
            continue

        # Current value: kwarg override, else the signature default.
        if param_name in ds_kwargs:
            param_value = ds_kwargs[param_name]
        elif param.default is not inspect.Parameter.empty:
            param_value = object()
        else:
            param_value = None

        if isinstance(param_value, (int, float, str, bool, Enum)):
            text(f"{param_value}", name=param_name, editable=False)
        else:
            draw_any(param_value, name=param_name,
                     show_name=True, show_header=True,
                     show_add_delete=False, draw=True)
    return False, input_value


@render_func(use_cache=True, show_bg=False, live=False, mode=Modes.WINDOW, show_header=False, show_name=False, selectable=False)
def draw_live_tab(input_value, **kwargs):
    """List the inspected view function's configurable parameters and their
    current values (kwarg override, else signature default)."""
    imgui.text("Re-renders view frequently, bad for performance but good for debugging")

    params_to_view = ["unique", ("abs_left"), ("abs_top", "top_offset"), "scroll_offset", ("abs_top_true", "top_offset_true"), ("width", "height"), ("content_width", "content_height"), "layer", "z_offset"]
    for to_view in params_to_view:
        if isinstance(to_view, str):
            value = getattr(input_value, to_view, 'N/A')
            text(f"{to_view}: {value}", name=to_view, editable=False)
        else:
            values = [getattr(input_value, attr, 'N/A') for attr in to_view]
            text(f"{', '.join(to_view)}: {', '.join(str(v) for v in values)}", name=", ".join(to_view), editable=False)

    if isinstance(input_value._raw_input_value, (dict, list, tuple)):
        text(f"Length: {len(input_value._raw_input_value)}", name="raw_input_length", editable=False)



    # imgui.text_colored(f"Unique {input_value.unique}", *(0.5, 0.01, 0.6))
    # imgui.dummy(0,2)
    #
    # imgui.text_colored(f"Top, Left {input_value.abs_top}, {input_value.abs_left}", *(0.5, 0.5, 0.0))
    # imgui.dummy(0, 2)
    #
    # #Width, height
    # imgui.text_colored(f"Width, Height {input_value.width}, {input_value.height}", *(0.5, 0.01, 0.6))
    # imgui.dummy(0, 2)
    #
    # imgui.text_colored(f"Unique {input_value.unique}", *(0.5, 0.01, 0.6))
    # imgui.dummy(0, 2)

    return False, input_value


@render_func(use_cache=False, show_bg=False, is_tree=False, show_header=False, show_name=False, selectable=False)
def draw_func_tab(input_value, **kwargs):
    """Editable source of the inspected view function; hotswaps on save.
    Routes through Mode.FILE_TREE — the same cache-backed code_file_io path a
    folder-files leaf uses — so all editors share one code path."""
    from src.lsd.gl_gui.view.mode import Mode
    view_func = input_value._view_func
    if view_func is not None:
        view_func_name = view_func.__name__ if hasattr(view_func, '__name__') else str(view_func)
        change, new_view_func = draw_any(view_func, mode=Mode.FILE_TREE, name=view_func_name)
    else:
        draw_str("No view function specified", name="View Function", editable=False)
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_eval_tab(input_value, draw_state, unique=None, enter_key_down=None,
                  menu_draw_state=None, **kwargs):
    """Arbitrary-code REPL scoped to the inspected view function. This tab is just
    an editor + trigger: it stashes the snippet and a pending flag on the TARGET
    widget's draw_state. The eval itself runs back in that widget's render wrapper
    (core_render), right before it calls the view func -- so the snippet sees the
    view function's real call-time locals. We read the result back off the same
    draw_state."""
    target = input_value  # (possibly walked-up) target's draw_state
    eval_view_func = target._view_func

    code = getattr(target, '_eval_code', None)
    if code is None:
        code = "input_value"
    # Single-line editor: Enter never reaches the editor as a newline (its newline
    # handler is gated on `not single_line`); instead the menu claims the
    # enter-down event via its on_enter_key_down param and uses it to fire the
    # eval below.
    # return_extras gives the code box's draw_state so we can tell when it holds
    # text focus (and thus when Enter should fire the eval -- see below).
    box = draw_text(
        code, name=f"eval_code##{unique}", padding_right=100,
        single_line=True, show_bg=True, show_header=False,
        return_extras=True, tint=(0.05, 0.15, 0.08))
    code_changed, new_code = box[0], box[1]
    code_ds = box[2] if len(box) > 2 else None
    if code_changed:
        target._eval_code = new_code
        code = new_code

    def _fire_eval():
        # Stash the snippet + arm the trigger, then force the target to actually
        # re-render (bypassing its cache) so its wrapper runs func() -- and our
        # eval hook -- this/next frame.
        target._eval_code = code
        target._eval_pending = True
        target._eval_request_gen = getattr(target, '_eval_generation', 0) + 1
        target.invalidate()
        target._parent.invalidate_up(max_depth=5)
        request_render()

    run_clicked = button("Run", height=30, name=f"eval_run##{unique}",
                         color=(0.2, 0.7, 0.3), factor=0.8)[0]
    # Enter fires the eval. Listen for it directly (the way draw_text reads keys
    # off the frame queue) instead of relying on the menu to forward it: while the
    # single-line code box holds text focus, Enter never reaches it as a newline,
    # so we claim it here when that box is the focused editor.
    enter_pressed = (code_ds is not None and Core.melty.text_focused_ds is code_ds
                     and any(k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
                             for k, _ in Core.melty.frame_key_events))
    if run_clicked or enter_pressed:
        _fire_eval()

    # The eval lands in the target's wrapper, a separate render pass. Until the
    # requested generation is served, keep THIS tab (and the owning menu) live so
    # we re-run and re-read the fresh result rather than serving a stale cache.
    if getattr(target, '_eval_generation', 0) < getattr(target, '_eval_request_gen', 0):
        draw_state.invalidate()  # this tab's own draw_state
        if menu_draw_state is not None:
            menu_draw_state.invalidate()  # the menu so it re-calls this tab
        request_render()

    eval_result = getattr(target, '_eval_result', None)
    if eval_result:
        draw_text(eval_result, name=f"eval_result##{unique}",
                  show_bg=True, show_header=True,
                  show_name=True, editable=False,
                  wrap=False, bg_offset=-100, width=draw_state.content_width,
              
                  tint=(0.0, 0.0, 0.0))
    return False, input_value


from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost

class ContextMenuState:
    def __init__(self):
        self.render_func_str = None
        self.render_func_dict = None
        self.class_str = None
        self.class_dict = None
        self.call_site = None
        self.call_site_dict = None
        self.mode_str = None
        self.mode_dict = None
        # What the cached hosts above were built FOR. This menu's up/down nav
        # retargets the same tab draw_state (and thus this same cm_state) at an
        # ancestor view, so the hosts must rebuild when the target changes.
        self.host_key = None
        self.call_site_key = None
        self.mode_key = None


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False, disable_scroll=False,
             temp=True, searchable=True)
def draw_input_tab(input_value, cm_state:ContextMenuState, draw_state, wrap=True, unique=None, class_to_show=None,
                   enter_key_pressed=None, **kwargs):
    """The three editable sources behind this view, in dispatch order:

      1. RENDER FUNCTION — the render_func whose body produced the view, edited
         whole via FunctionCodec. It isn't on the captured stack (the capture runs
         in the wrapper BEFORE the body executes, so the innermost frame is the
         filtered wrapper), so it's read from `_view_func`.
      2. CALLER — the direct `draw_x(...)` call site that invoked this view, edited
         via CallerCodec (spans just the call expression). `_call_site` is the
         nearest real caller (filename, lineno), captured once on menu-open with the
         render-dispatch machinery already filtered out (caller_site, core_render).
      3. DECORATIONS — the `@...` block on the value's class, edited via
         DecorationsCodec. `class_to_show` is resolved by draw_context_menu (the
         value's own class, or the nearest parent with source for a primitive
         field); only classes carry decorations, so it's skipped otherwise.
      4. MODE — the ACTIVE mode's entry kwargs in its enum class source
         (mode.py), edited via ModeCodec. Which member to show comes from the
         target's _kwargs ('current_mode', stamped by the wrapper when a mode
         config matched); skipped when no mode drove this view."""

    # Source/cst hosts come from the process-wide code-host cache, keyed by the
    # live reference - every menu opened on the same render_func/class/call
    # site shares ONE host pair, so the code isn't re-loaded and re-parsed per
    # open (code_hosts_for in new_converters; earlier like this tab used to
    # build inline).
    host_key = (input_value._view_func, class_to_show)
    if cm_state.host_key != host_key:
        cm_state.host_key = host_key
        cm_state.render_func_str, cm_state.render_func_dict = code_hosts_for(input_value._view_func)
        cm_state.class_str, cm_state.class_dict = code_hosts_for(class_to_show)
        # The nav retargeted us at a new view; its call site differs (and
        # may not be captured yet - see the lazy capture in draw_context_menu).
        cm_state.call_site = None
        cm_state.call_site_dict = None
        cm_state.call_site_key = None

    # _call_site can remain a frame or two after retargeting (lazy one-shot
    # capture on the ancestor's next render), so check it every pass.
    call_site = getattr(input_value, "_call_site", None)
    if call_site is not None and call_site != cm_state.call_site_key:
        cm_state.call_site_key = call_site
        filename, lineno = call_site
        cm_state.call_site, cm_state.call_site_dict = code_hosts_for(CallSite(filename, lineno))

    # The ACTIVE mode driving this view - the wrapper stamps it into the
    # view's kwargs when a mode config matches (kwargs['current_mode'],
    # core_render; 'mode' for the recursive variant), so it rides on the
    # target's _kwargs. The host is for the mode's ENUM CLASS (whose source
    # holds every member), keyed per class so retargeting at a view under a
    # different mode enum rebuilds; which member to show is re-read each pass.
    current_mode = input_value._kwargs.get('current_mode')
    mode_cls = type(current_mode) if current_mode is not None else None
    if cm_state.mode_key != mode_cls:
        cm_state.mode_key = mode_cls
        cm_state.mode_str, cm_state.mode_dict = (
            code_hosts_for(mode_cls) if mode_cls is not None else (None, None))

    # ── Recompile (hotswap) - the same Run path code_file_io draws on a file
    # leaf (menu_files / FILE_TREE). A matrix edit saves SOURCE to disk via
    # the hosts' chain_out, but the live render function keeps its old defaults
    # until a hotswap. The button/runner work against the str_host's own
    # CodeState, so Run compiles the host's live buffer (the edit already
    # merged in), not a possibly-stale disk read. None until the lazy host is
    # drawn/loaded so the button appears a beat after the menu opens.
    code_state = host_code_state(cm_state.render_func_str)
    if code_state is not None and code_state.address is not None:
        clicked = recompile_button(code_state, unique=unique)
        recompile_status(code_state, draw_state)
        # Alt+Enter (or the editor's usual Ctrl+Enter) while hovering the tab -
        # enter_key_pressed is the auto-subscribed Enter-down InputEvent, same
        # mechanism as code_file_io's hotkey; modifiers ride on the event.
        hotkey = bool(enter_key_pressed and (enter_key_pressed.alt or enter_key_pressed.ctrl))
        run_recompile(input_value._view_func, code_state, draw_state,
                      start=clicked or hotkey, name=f"recompile{unique}")

    # ── Every input in one table: parameter × source matrix ───────────────
    # Collect each parsed source dict (columns), pivot against the render
    # function's parameter list (rows) via param_source_matrix, and draw the
    # whole input surface as one grid. Rebuilt every frame from the live
    # parses (cheap dict scans), so a background parse landing or an edit to
    # any source shows up immediately. Edits to a cell write back into the
    # SOURCE dict they came from (apply_param_source_matrix) - those dicts are
    # the hosts' bubbling-wrapped parse nodes, so the edit makes the owning
    # host dirty and rides its normal chain_out update path.
    # Each source column maps to the CODEC that owns the data - the codec's
    # render_kwargs tint IS the source color. The matrix cells are parse
    # fragments that can't adopt it naturally (type-based codec fragments), so
    # the tint map rides into draw_param_matrix, which applies it manually
    # and individually per cell.
    from src.lsd.gl_gui.view.core_conversion.new_codecs import (
        FunctionCodec, CallerCodec, DecorationsCodec, TypeCodec, ModeCodec)

    def _codec_tint(codec):
        return (getattr(codec, "render_kwargs", None) or {}).get("tint")

    sources = {}
    source_tints = {}
    source_locations = {}

    def _add_source(sname, sdict, codec, location=None):
        if isinstance(sdict, dict) and sdict:
            sources[sname] = sdict
            source_tints[sname] = _codec_tint(codec)
            if location is not None and location[0] is not None:
                source_locations[sname] = location

    # Source names carry the origin: the bare function name labels the
    # signature column, decorator-call spellings (@render_func(name), ...)
    # label the decorator-backed columns. Locations feed the jump-to buttons.
    view_fn = inspect.unwrap(input_value._view_func)
    fn_name = getattr(view_fn, "__name__", "?")
    try:
        fn_file = inspect.getsourcefile(view_fn)
    except TypeError:
        fn_file = None
    fn_loc = (fn_file, getattr(getattr(view_fn, "__code__", None),
                               "co_firstlineno", None))

    cls_name, cls_loc = "defaults", None
    if isinstance(class_to_show, type):
        cls_name = class_to_show.__name__
        try:
            cls_loc = (inspect.getsourcefile(class_to_show),
                       inspect.getsourcelines(class_to_show)[1])
        except (TypeError, OSError):
            cls_loc = None

    _add_source(fn_name, cm_state.render_func_dict.deep.parameters(), FunctionCodec,
                location=fn_loc)
    call_site_dict = cm_state.call_site_dict.deep.unwrap() if cm_state.call_site_dict else None
    if call_site_dict:
        from src.lsd.gl_gui.view.core_conversion.chain_converters import caller_func_name
        _add_source(caller_func_name(input_value._call_stack) or "caller",
                    call_site_dict, CallerCodec, location=call_site)
    _add_source(f"@render_func({fn_name})",
                cm_state.render_func_dict.deep.decorators.render_func(),
                DecorationsCodec, location=fn_loc)
    _add_source(f"@window({fn_name})",
                cm_state.render_func_dict.deep.decorators.window(),
                DecorationsCodec, location=fn_loc)
    _add_source(f"@defaults({cls_name})", cm_state.class_dict.deep.decorators.defaults(),
                TypeCodec, location=cls_loc)

    # MODE - the active mode is entry in its enum class source (e.g.
    # `NEW_CODE = {types...: ModeOverrides(kwargs={...})}` in mode.py), not as
    # the kwargs dict that entry stamps into this view. A mode can hold
    # SEVERAL type-keyed entries (TEXT_ONLY); the parse can't be type-matched
    # (its keys are unevaluated source), so pick the candidate whose keys best
    # overlap the LIVE matched config (get_config_for) - the entry that
    # actually drove THIS view. Edits merge into the mode_dict host's parse
    # and ride its normal chain_out/save back into the enum's source file.
    imgui.text(f"{str(input_value._kwargs.get('mode'))} {input_value._kwargs.get('current_mode')}")
    if cm_state.mode_dict is None:
        imgui.text(f"No mode source for {mode_cls}")
    else:
        imgui.text(f"mode source for {mode_cls}")

    if current_mode is not None and cm_state.mode_dict is not None:
        candidates = [c for c in
                      cm_state.mode_dict.deep[current_mode.name].kwargs.all()
                      if isinstance(c, dict)]
        live_keys = set()
        if isinstance(getattr(current_mode, "value", None), dict):
            live_cfg = current_mode.get_config_for(input_value._raw_input_value)
            if live_cfg is not None and live_cfg.kwargs:
                live_keys = set(live_cfg.kwargs)
        mode_kwargs = max(candidates,
                          key=lambda c: len(live_keys & set(c)), default=None)
        mode_loc = None
        try:
            cls_lines, cls_start = inspect.getsourcelines(mode_cls)
            member_off = next(
                (i for i, l in enumerate(cls_lines)
                 if l.lstrip().startswith((f"{current_mode.name} =",
                                           f"{current_mode.name}="))), 0)
            mode_loc = (inspect.getsourcefile(mode_cls), cls_start + member_off)
        except (TypeError, OSError):
            pass

        imgui.text(f"mode source for {mode_kwargs}"[:50])

        _add_source(str(current_mode), mode_kwargs, ModeCodec, location=mode_loc)

    # ── Search - the STANDARD searchable path; no special find box. The tab
    # is searchable=True, so Ctrl+F over it opens the framework's floating
    # find bar (view_render, searchable=True) which maintains the term on
    # this draw_state.search_text and invalidates this tab per keystroke.
    # That same term feeds draw_param_matrix's fuzzy row filter below, and the
    # search session on pty.search_stack reaches the matrix cells' editors
    # for in-place highlighting like any other searchable view.

    if sources:
        _, matrix = param_source_matrix(sources, func=input_value._view_func,
                                        include_unmatched=True)
        changed, value = draw_param_matrix(matrix, source_tints=source_tints,
                                           source_locations=source_locations,
                                           view_draw_state=input_value, wrap=True,
                                           width=draw_state.content_width-17,
                                           priority_params=tuple(signature_param_names(input_value._view_func)),
                                           search_text=str(draw_state.search_text or ""),
                                           name=f"{input_value._view_func.__name__} inputs##matrix{unique}",
                                           disable_scroll=True)
        if changed and isinstance(value, dict):
            # Pure write-back (no UI) - call the bare function, not this wrapper.
            apply_param_source_matrix.__wrapped__(value, ref=sources, changed=True)

    # Each *_dict RenderHost parses its source on a background worker in its OWN draw
    # loop; this tab merely READS the materialized value (h.deep....) and draws it. When
    # a parse lands, the host's after_render() runs the loop but can't reach this
    # cached subtree - so register this tab's draw_state as a listener and the host
    # invalidates us when its value changes. Replaces the old "invalidate for the first
    # 10ms" guess, which expired before the ~400ms chain_in debounce, leaving the
    # dict blank until a manual mouse-over.
    for _h in (cm_state.render_func_dict, cm_state.class_dict,
               cm_state.call_site_dict, cm_state.mode_dict):
        if _h is not None:
            _h.notify_on_change(draw_state)

    # common = dict(mode=Modes.NEW_CODE, min_width=100, max_height=300, fill_height=False)
    #
    # view_func = getattr(input_value, "_view_func", None)
    # if inspect.isfunction(view_func):
    #     draw_any(view_func, name=f"{view_func.__name__} (self)##self_{unique}", **common)

    # call_site = getattr(input_value, "_call_site", None)
    # if call_site is not None:
    #     filename, lineno = call_site
    #     draw_any(CallSite(filename, lineno), name=f"caller:{lineno}##caller_{unique}", **common)
    #
    # if isinstance(class_to_show, type):
    #     draw_any(Decorations(class_to_show),
    #              name=f"{class_to_show.__name__} decorations##deco_{unique}", **common)

    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, disable_scroll=False, show_name=False, selectable=False)
def draw_class_tab(input_value, class_to_show=None, class_is_parent=False, class_name='',  **kwargs):
    """Editable class source. For a primitive field this is the parent object's
    class (e.g. Lora for a Lora.rank float) -- labelled so the source is clear.
    Routes through Mode.FILE_TREE — the same cache-backed code_file_io path a
    folder-files leaf uses — so all editors share one code path."""
    from src.lsd.gl_gui.view.mode import Mode
    if class_is_parent:
        text(f"Parent type of {class_name}", name="Source",
             editable=False, tint=(1.0, 0.64, 0.113))
    cls_change, new_cls = draw_any(class_to_show, mode=Mode.FILE_TREE,
                                   name=class_to_show.__name__)
    return False, input_value


@render_func(use_cache=True, show_bg=False, show_header=False, show_name=False, selectable=False)
def draw_mode_tab(input_value, draw_state, current_mode=None, **kwargs):
    """Show the current Mode's value string."""
    if current_mode is not None:
        mode_change, new_mode = text(str(current_mode.value), width=draw_state.content_width,
                                     name=str(current_mode))
    return False, input_value


@render_func(use_cache=True, disable_scroll=True, show_header=False,
             header_same_line=False, show_tint=False, show_name=False, is_tree=False)
def draw_context_menu(input_value, draw_state, cursor_hover_inverted, func, unique=None, search_text='',
                      search_active=False, up_key_pressed=None,
                      down_key_pressed=None, enter_key_down=None, tab_state: TabState = None, **kwargs):
    context_menu_offset = input_value.context_menu_offset

    # imgui.text(type(input_value._input_value).__name__)
    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 1, imgui.get_cursor_screen_pos()[1] - 18))
    if up_key_pressed:
        print("Up key pressed")

    fa_up_arrow = ""
    fa_down_arrow = ""
    if input_value._parent.id is not None:
        if button(fa_up_arrow, height=50)[0] or up_key_pressed:
            input_value.context_menu_offset += 1
            Core.melty.cache.invalidate_up(draw_state._tile_id, max_depth=5)
            Core.melty.cache.invalidate_up(input_value._tile_id, max_depth=5)

        imgui.same_line()
    if input_value.context_menu_offset > 0:
        if button(fa_down_arrow, height=50)[0] or down_key_pressed:
            input_value.context_menu_offset = max(0, input_value.context_menu_offset - 1)
            Core.melty.cache.invalidate_up(draw_state._tile_id, max_depth=5)
            Core.melty.cache.invalidate_up(input_value._tile_id, max_depth=5)
    else:
        imgui.dummy(30, 30)



    imgui.same_line()
    imgui.text_colored(f"{context_menu_offset}", 1, 1, 1, 0.3)
    imgui.same_line()

    # Screenshot this menu's parent view, top of the menu below the nav arrows.
    # Deferred so the menu isn't in the shot: front the owning window (so the
    # view is visible), queue the view capture, hide this menu (asking to reopen
    # it afterward), then let screenshot.process_take_screenshot_flags grab the
    # view's rect a few frames later and reopen the menu.
    if button(f" ", height=30, tint=(0,0,0,1.0), name=f"screenshot_window##{unique}")[0]:
        from src.lsd.gl_gui.screenshot import request_view_capture
        view_ds = input_value  # the view this menu is for (offset-walked)
        Core.melty.move_window_to_front(view_ds.root_window)
        draw_state._reopen = True
        request_view_capture(view_ds, Core.melty.frame_count, reopen_menu_ds=draw_state)
        draw_state.closed = True
        request_render()

    imgui.same_line()

    # Same deferred screenshot, then hand it to Claude: once the shot lands,
    # boot a fresh claude-d session pre-typed (NOT sent) with the shot path +
    # the view's render function (the same function the Input tab edits), and
    # open the Claude Terminals window so the new session's terminal comes up.
    if button(f" claude", height=30, tint=(0,0,0,1.0), name=f"claude_session##{unique}")[0]:
        from src.lsd.gl_gui.screenshot import request_view_capture
        view_ds = input_value
        # Resolve the menu's offset-walked target so the shot + function match
        # what the tabs will show (the walk proper happens above the buttons).
        for _ in range(context_menu_offset):
            if view_ds._parent is None or view_ds._parent is view_ds:
                break
            view_ds = view_ds._parent
        fn = inspect.unwrap(view_ds._view_func)
        fn_name = getattr(fn, "__name__", "?")
        try:
            fn_file = inspect.getsourcefile(fn)
        except Exception:
            fn_file = None
        fn_line = getattr(getattr(fn, "__code__", None), "co_firstlineno", None)
        loc = f"{fn_file}:{fn_line}" if fn_file else "unknown location"

        def _start_claude(shot_path, _name=fn_name, _loc=loc):
            from src.lsd.gl_gui.view.playground.claude_terminals import (
                launch_claude_session, open_claude_terminals_window)
            launch_claude_session(
                f"Take a look at this screenshot of a view in the studio: {shot_path} "
                f"It is rendered by the function `{_name}` in {_loc}. ")
            open_claude_terminals_window()

        Core.melty.move_window_to_front(view_ds.root_window)
        draw_state._reopen = True
        request_view_capture(view_ds, Core.melty.frame_count, reopen_menu_ds=draw_state,
                             on_captured=_start_claude)
        draw_state.closed = True
        request_render()

    imgui.same_line()


    offset_ds = input_value
    for i in range(context_menu_offset):
        if offset_ds._parent is None:
            break
        offset_ds = offset_ds._parent

    # The menu walked up to an ancestor (offset > 0). That ancestor never had its
    # OWN context menu open, so the context_menu_open capture gate never ran for
    # it and its _call_site is None — caller lenses come up empty. Ask its next
    # inline render to capture the site (lazy, one-shot), and invalidate it so it
    # re-renders fresh rather than from cache (where the capture line is skipped).
    if (offset_ds is not input_value and not offset_ds._call_site_captured
            and not offset_ds._call_site_requested):
        offset_ds._call_site_requested = True
        if Core.melty.cache is not None:
            Core.melty.cache.invalidate_up(offset_ds._tile_id, max_depth=5)
        request_render()

    input_value._offset_ds = offset_ds
    input_value = offset_ds

    # Font awesome info icon unicode: \uf05a
    gear_icon = f"\uf013"
    config_icon_fa = f"{gear_icon} Config"
    info_icon_fa = " Info"
    view_func_name = offset_ds._view_func.__name__
    class_name = type(input_value._raw_input_value).__name__
    paint_brush_icon = f"\uf1fc"
    tint_tab_name = f"{paint_brush_icon} Tint"
    terminal_icon = f"\uf120"  # fa-terminal
    eval_tab_name = f"{terminal_icon} Eval"
    keyboard_icon = f"\uf11c"  # fa-keyboard
    input_tab_name = f"{keyboard_icon} Input"
    # Lightning bolt
    live_icon = f"\uf0e7"
    live_tab = f"{live_icon} Live"

    # Resolve which class's source to show in the class tab.
    # For a non-primitive value that's just the value's own class. For a
    # primitive field (e.g. a float `rank`) the value itself has no source,
    # so walk up the parent chain to the nearest object that does have source
    # code -- so e.g. Lora.rank still shows Lora's class, labelled as parent.
    raw_value = input_value._raw_input_value
    class_to_show = None
    class_is_parent = False
    # A bubbling tree node's type is a runtime-generated `Bubbling_<Base>` with no source
    # — resolve its real base (e.g. GeneralParse) so the Class/Decorations tabs resolve
    # rather than erroring.
    from src.lsd.gl_gui.view.core_conversion.bubbling import base_of_bubbling
    # Use exact-type matching, not isinstance: a subclass of a primitive
    # (e.g. CodeLine(str)) DOES have its own source, so it should show its
    # own class tab rather than being treated as a bare primitive.
    if type(raw_value) not in (int, float, str, bool):
        class_to_show = base_of_bubbling(type(raw_value))
    else:
        max_walk = 4
        ancestor = input_value._parent
        while ancestor is not None and max_walk > 0:
            a_raw = getattr(ancestor, '_raw_input_value', UNSET_VALUE)
            a_type = base_of_bubbling(type(a_raw)) if a_raw is not UNSET_VALUE else None
            if a_type is not None and getattr(a_type, '__module__', None) \
                    not in (None, 'builtins', '_collections_abc'):
                class_to_show = a_type
                class_is_parent = True
                break
            ancestor = ancestor._parent
            max_walk -= 1

    # Font awesome: fa-code () for the view function, fa-cube () for the class.
    func_tab = f" {view_func_name}"
    if class_to_show is not None:
        class_tab = f" {class_to_show.__name__}" + (" (parent)" if class_is_parent else "")
    else:
        class_tab = f" {class_name}"

    # Static tint colors for the fixed Config / Info tabs; remaining tabs use the neutral grey.
    config_tint = (0.12, 0.38, 0.772) 
    info_tint = (0.545, 0.469, 0.012) 

    tab_names = []
    tab_tints = []
    tab_names.append(info_icon_fa)
    tab_tints.append(info_tint)
    tab_names.append(config_icon_fa)
    tab_tints.append(config_tint)
    tab_names.append(func_tab)
    tab_tints.append(None)
    tab_names.append(eval_tab_name)
    tab_tints.append((0.2, 0.7, 0.3))  # green for the eval/REPL tab
    tab_names.append(input_tab_name)
    tab_tints.append((0.4, 0.2, 0.7))  # purple for the input tab
    tab_names.append(tint_tab_name)
    tab_tints.append(Core.melty._saturated_rgb(draw_state.tint))  # orange tint for the tint tab
    if class_to_show is not None:
        tab_names.append(class_tab)
        tab_tints.append(None)

    tab_names.append(live_tab)
    tab_tints.append((0.7, 0.0, 0.0))

    indices = list(range(len(tab_names)))

    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [indices[4]]

    current_mode = input_value._kwargs.get('mode', None)
    mode_tab = str(current_mode)
    if current_mode is not None:
        tab_names.append(mode_tab)

    # Indices list

    imgui.dummy(0,1)
    imgui.same_line()
    tab_changed, new_tabs = draw_tab_bar(tab_state.selected_tabs, names=tab_names, wrap=True, tab_height=40,
                                         tint_value=0.7,
                                         width=max(50, draw_state.content_width - 282),
                                         show_bg=True, name=f"tab_bar#{view_func_name}{unique}",
                                         z_offset=-0.5, bg_offset=-7, draw=True,
                                         collection=indices, tints=tab_tints, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    imgui.dummy(0,2)

    for t_idx, static_tab in enumerate(tab_state.selected_tabs):
        if static_tab >= len(tab_names):
            draw_func_tab(input_value, name=f"func_tab_{t_idx}##{unique}", disable_scroll=True, column=t_idx)
        this_tab = tab_names[static_tab]
        # Each tab is its own render_func placed at column=t_idx; the tab owns a
        # single-column region so its inner views don't pass column themselves.
        if this_tab == tint_tab_name:
            draw_tint_context(input_value, name=f"Context Tint##{unique}", column=t_idx)

        elif this_tab == info_icon_fa:
            # Resolve the effective search term (menu kwarg, else its search box).
            info_search = search_text if search_text != "" else draw_state.search_text
            draw_info_tab(input_value, search_text=info_search, unique=unique,
                          name=f"info_tab_{t_idx}##{unique}", column=t_idx)

        elif this_tab == config_icon_fa:
            draw_config_tab(input_value, name=f"config_tab_{t_idx}##{unique}", column=t_idx)

        elif this_tab == func_tab:
            draw_func_tab(input_value, name=f"func_tab_{t_idx}##{unique}", disable_scroll=False, column=t_idx)

        elif this_tab == eval_tab_name:
            draw_eval_tab(input_value, unique=unique, enter_key_down=enter_key_down,
                          menu_draw_state=draw_state,
                          name=f"eval_tab_{t_idx}##{unique}", column=t_idx)

        elif this_tab == input_tab_name:
            draw_input_tab(input_value, class_to_show=class_to_show,
                     name=f"input_tab_{t_idx}##{unique}", wrap=False,
                      column=t_idx, disable_scroll=False)


        elif this_tab == class_tab:
            draw_class_tab(input_value, class_to_show=class_to_show, class_is_parent=class_is_parent,
                           class_name=class_name, name=f"class_tab_{t_idx}##{unique}", column=t_idx)

        elif this_tab == mode_tab:
            draw_mode_tab(input_value, current_mode=current_mode,
                          name=f"mode_tab_{t_idx}##{unique}", column=t_idx)

        elif this_tab == live_tab:
            draw_live_tab(input_value, name=f"live_tab_{t_idx}##{unique}", column=t_idx)


    imgui.dummy(0, 30)

    return False, input_value


from src.lsd.gl_gui.view.core_views.core_render import render_func


@render_func(show_bg=True, use_cache=True, shadow=False, with_header=draw_header)
def draw_undo_manager(input_value, **kwargs):
    """Render the undo history grouped by undo step (one user action), newest
    first. Changes from the same action share a group_id and undo together, so we
    draw a separator between groups and indent the changes within each. input_value
    is the UndoManager class (handed in by @window), read its `history` deque."""
    history = list(getattr(input_value, "history", ()))
    # Count distinct groups for the summary line.
    group_count = len({c.group_id for c in history})
    imgui.text(f"{group_count} undo step(s), {len(history)} change(s)")

    prev_gid = None
    shown = 0
    for change in reversed(history):
        if change.group_id != prev_gid:
            imgui.separator()
            prev_gid = change.group_id
        name = getattr(change.draw_state, "name", None) or "?"
        from_val = str(change.old)[:20]  # truncate long values for readability
        to_val = str(change.new)[:20]
        imgui.text(f"  {name}: {from_val} -> {to_val}")
        shown += 1
        if shown >= 12:
            imgui.text(f"... and {len(history) - shown} more")
            break
    return False, input_value


@render_func(use_cache=True, temp=True)
def draw_drop_down_item(input_value, name="", unique=0, shadow=False, draw_state=None, **kwargs):
    hovered = draw_state._bounding_hovered
    clicked, _ = button(name, name=f"{unique}{name}_dd_item", show_bg=True, height=25,
                        shadow=False, hovered=hovered)

    # Hover highlight: paint a translucent wash over this item's own box (its
    # draw_state is the row, so abs_left/abs_top + width/height frame it exactly)
    # straight onto the window draw list so it sits over the button fill.
    if hovered:
        dl = imgui.get_window_draw_list()
        dl.add_rect_filled(draw_state.abs_left, draw_state.abs_top,
                           draw_state.abs_left + draw_state.width,
                           draw_state.abs_top + draw_state.height,
                           imgui.get_color_u32_rgba(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    if clicked:
        return True, name

    return False, input_value


@render_func(use_cache=True, show_bg=False, shadow=True, selectable=False,
             is_tree=False, show_name=True, with_header=draw_header)
def draw_dropdown(input_value, collection, name, draw_state, unique, drop_down_state: DropDownState, text_align="left", **kwargs):
    """Root of a recursive dropdown. Renders a trigger button showing the current
    selection; clicking it opens the (click-to-open) root popover. Nested dict
    rows inside the popover open their own sub-menus on hover. Returns
    (changed, selected_leaf) when the user picks a value.

    Open/closed is a single global slot -- ``Melty.popover_focused_ds`` holds the
    draw_state of whichever dropdown's popover is currently shown. Each dropdown
    decides it's open by identity (``popover_focused_ds is draw_state``), so
    opening one popover implicitly closes every other (they all fail the test).
    ``drop_down_state`` is the per-view scratch object the framework re-injects
    every frame; we stash the last picked leaf on it for the trigger label."""
    from src.lsd.gl_gui.view.mode import Mode

    # Is THIS dropdown the one whose popover is showing?
    is_open = Melty.popover_focused_ds is draw_state

    # Label shows the last pick (sticky across frames via drop_down_state),
    # falling back to the raw input value.
    # Title shows the LABEL/key of the current selection (e.g. "red"), not the raw
    # value (which may be a tuple/number); selected_label is stamped at pick-time.
    _sel_label = getattr(drop_down_state, "selected_label", "") or ""
    current = _sel_label if _sel_label else (str(input_value) if input_value is not None else "")
    caret = "" if is_open else ""  # fa-chevron-down / fa-chevron-right

    # Compact mode: in a very narrow slot (e.g. an inline table cell) there's no room
    # for the caret + button chrome, so show NOTHING but the selected value. Still a
    # real (bg-less) button, so it stays clickable to open the popover. Threshold is
    # tunable via compact_below (px).
    #
    # The trigger's slot width: an explicit caller `width` wins, only without one
    # do we fall back to the measured content_width. Measurement must never feed
    # back into the trigger size - an open popover inflates content_width, which
    # would flip compact mode off and balloon the trigger (~240px), wrapping the
    # header row it sits in: the disagreement between the drawn size and the
    # measured item rect is exactly what reads as animation jitter.
    _slot_w = kwargs.get("width") or draw_state.content_width
    compact = _slot_w < kwargs.get("compact_below", 50)
    if compact:
        # Show the VALUE itself (not the label/key). For a name->glyph dropdown
        # the trigger must stay the glyph, not become the picked key's name.
        drop_down_display_str = str(input_value if input_value is not None else current)[:30]
    else:
        drop_down_display_str = f"{caret} {str(current)[:30]}"
    bg_offset = 4 if is_open else 7

    # A compact trigger hugs its glyph: minimal text pad and a centered label,
    # so a small chevron/icon cell doesn't balloon to full text label width.
    trigger_pad = kwargs.get("text_pad", 6 if compact else 15)
    trigger_align = "center" if compact else text_align
    trigger_w = max(15 if compact else 18, _slot_w)

    trigger_h = (getattr(draw_state, "content_height", 0) or 25) if compact else 25
    # Colour the trigger by the selected item's embedded tint (input_value is the
    # current selection passed by the caller), falling back to the view's tint.
    trigger_tint = _dd_obj_tint(input_value, draw_state.tint)
    clicked, _ = button(drop_down_display_str, name=f"{name}_dd_trigger", show_bg=False, width=trigger_w,
                         show_button_bg=True, shadow=True, tint=trigger_tint, height=19, disable_scroll=True,
                        z_offset=3, text_align=trigger_align, bg_offset=bg_offset, text_pad=trigger_pad)

    if clicked:

        was_open = is_open
        Melty.popover_focused_ds = None if is_open else draw_state
        is_open = Melty.popover_focused_ds is draw_state
        if is_open and not was_open:
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
            # Fresh open: start with an empty query and give the search box a few
            # frames to grab text focus so the user can type to filter immediately.
            drop_down_state.search_query = ""
            drop_down_state.search = ""
            drop_down_state._focus_search = 8
            # Start the highlight on the last-selected item (expanded to it) rather
            # than the top, so re-opening starts where you left off.
            _sp = _dd_as_tuple(getattr(drop_down_state, "selected_path", ()))
            drop_down_state.cursor_path = _sp
            drop_down_state.open_path = _sp[:-1] if _sp else ()
            drop_down_state._kbd_mode = True
            drop_down_state._last_mouse = None
            drop_down_state._had_focus = False
        if not is_open:
            _dd_close(drop_down_state)
            draw_state.invalidate()

        request_render()


    # The popover is a latching window -- the first call registers it and it
    # stays alive, so we always call it and toggle visibility with `closed`
    # rather than skipping the call (skipping would leave the last-open frame
    # painted). closed=True hides the whole subtree. window_pos pins it just
    # under the trigger (relative to this dropdown window) so it doesn't drift
    # off as a free-floating draggable; temp keeps it ephemeral. root_state
    # carries the single open-path the recursion expands; path_prefix starts
    # empty at the root. A pick bubbles back as (changed, value).
    if is_open:
        # A mouse move switches back to hover mode so the highlight follows the
        # pointer again (until the next arrow key locks keyboard mode).
        _mp = imgui.get_mouse_pos()
        _lm = getattr(drop_down_state, "_last_mouse", None)
        if _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5):
            drop_down_state._kbd_mode = False
        drop_down_state._last_mouse = (_mp[0], _mp[1])

    changed, new_item = draw_dd_menu(collection, tint=draw_state.tint,
                                     name=f"{unique}_menu",
                                     closed=not is_open, temp=True, shadow=False,
                                     window_pos=(0, trigger_h -_DD_ROW_H), max_height=500,
                                     parent_window=draw_state, swoosh=False, disable_scroll=False,
                                     root_state=drop_down_state, path_prefix=())
    if is_open:
        if changed:
            _p = _dd_as_tuple(getattr(drop_down_state, "_picked_path", ()))
            drop_down_state.selected_path = _p
            drop_down_state.selected_label = _dd_label_for_path(collection, _p)
            Melty.popover_focused_ds = None  # picking dismisses the popover
            _dd_close(drop_down_state)
            draw_state.invalidate()
            request_render()
            return True, new_item

        # The dropdown owns the keyboard while open: its search box holds text
        # focus (taken on open, released on close), so OTHER text editors gate off
        # (they all check Melty.text_focused_ds) and never act on the same keys. We
        # only handle nav keys while we actually hold that focus - that's what
        # keeps the arrow/Enter/Esc collisions with whatever editor was active.
        box_tile = getattr(drop_down_state, "_search_box_tile", None)
        text_focused = (Melty.text_focused_ds is not None and box_tile is not None
                        and getattr(Melty.text_focused_ds, "_tile_id", None) == box_tile)

        # Esc dismisses the open dropdown (and releases its text focus via
        # _dd_close). Ungated: the global text box Esc handler may have already
        # cleared the box's focus this same frame, so we don't require it here.
        if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
            Melty.popover_focused_ds = None
            _dd_close(drop_down_state)
            draw_state.invalidate()
            request_render()
            return False, input_value

        # Arrows / Enter only while we own the keyboard, so they don't also drive
        # whatever editor was active when the dropdown opened.
        if text_focused:
            search = getattr(drop_down_state, "search", "") or ""
            picked = _dd_handle_keys(collection, drop_down_state, search=search,
                                     text_focused=text_focused)
            if picked is not UNSET_VALUE:
                _p = _dd_as_tuple(getattr(drop_down_state, "_picked_path", ()))
                drop_down_state.selected_path = _p
                drop_down_state.selected_label = _dd_label_for_path(collection, _p)
                Melty.popover_focused_ds = None
                _dd_close(drop_down_state)
                draw_state.invalidate()
                request_render()
                return True, picked

        # Click-outside dismissal: a fresh left click landing on neither the
        # trigger nor anywhere inside the popover subtree closes it (matches
        # native popover behaviour). Selection clicks already returned above, and
        # sub-menus open on hover, so this only fires for genuine outside clicks.
        if imgui.is_mouse_clicked(0) and not clicked:
            mx, my = imgui.get_mouse_pos()
            under = Core.melty.bvh_query(mx, my)
            if not any(_ds_in_subtree(ds, draw_state) for ds in under):
                Melty.popover_focused_ds = None
                _dd_close(drop_down_state)
                draw_state.invalidate()
                request_render()

        # Focus settle (bounded, NOT a permanent repaint loop): right after open
        # the search box asks for text focus, but the opening click's
        # request_focus can race it the same frame. While the box hasn't
        # confirmed focus and we're still within the small retry budget, re-run
        # so it asks again; it gets within a frame or two and this stops.
        # Steady-state open repaints nothing - mouse changes invalidate via
        # _dd_set_cursor, keys via begin_frame. The retry must invalidate the
        # BOX's own chain (invalidate_up from its tile): the menu is a separate
        # cached window subtree, so invalidating just this trigger view never
        # re-rendered the box on a reopen and request_focus never re-fired.
        if getattr(drop_down_state, "_focus_search", 0) > 0:
            box_tile = getattr(drop_down_state, "_search_box_tile", None)
            if box_tile is not None:
                Melty.cache.invalidate_up(box_tile, force=True)
            Melty.cache.invalidate(draw_state._tile_id, force=True)
            request_render()

    return False, input_value


def _ds_in_subtree(node, ancestor, max_depth=64):
    """True if ``node`` is ``ancestor`` or a descendant of it. Walks the
    ``_parent`` chain, stopping on the root's self-loop (root._parent is root)."""
    seen = 0
    while node is not None and seen < max_depth:
        if node is ancestor:
            return True
        parent = getattr(node, "_parent", None)
        if parent is None or parent is node:
            break
        node = parent
        seen += 1
    return False


def _dd_entries(container):
    """Normalized (key, value, label, is_branch) rows for one level. Dict rows
    read by their key (small/medium); list/tuple rows by their value
    (left/center/right) since the index isn't meaningful to the user."""
    if isinstance(container, dict):
        items = list(container.items())
        labelled = [(k, v, str(k)) for k, v in items]
    else:
        labelled = [(i, v, str(v)) for i, v in enumerate(container)]
    return [(k, v, lbl, isinstance(v, (dict, list))) for k, v, lbl in labelled]


def _dd_subtree_matches(value, search):
    """True if `search` (already lowercased) appears anywhere in this value's
    subtree, so a branch stays visible while searching when a descendant matches."""
    if not search:
        return True
    if isinstance(value, dict):
        return any(search in str(k).lower() or _dd_subtree_matches(v, search)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(_dd_subtree_matches(v, search) for v in value)
    return search in str(value).lower()


def _dd_visible_entries(container, search=""):
    """Rows shown for a level under `search`: a leaf whose label matches, or a
    branch matching by label OR holding a matching descendant. Empty `search`
    keeps everything."""
    if not isinstance(container, (dict, list)):
        return []
    rows = _dd_entries(container)
    if not search:
        return rows
    return [(k, v, lbl, br) for (k, v, lbl, br) in rows
            if search in lbl.lower() or (br and _dd_subtree_matches(v, search))]


def _dd_walk(collection, path):
    """Descend `collection` along a key/index `path`, returning the node there or
    None if the path no longer resolves (e.g. after a search prunes it)."""
    node = collection
    for k in path:
        try:
            node = node[k]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def _dd_rows_at(collection, path, search):
    """Visible rows at `path`, applying the once-a-branch-matches-by-label rule:
    if any ancestor key on `path` matched the search by its own label, that whole
    subtree counts as a match, so deeper levels are shown unfiltered."""
    container = _dd_walk(collection, path)
    ancestor_matched = bool(search) and any(search in str(k).lower() for k in path)
    return _dd_visible_entries(container, "" if ancestor_matched else search)


def _dd_first_match_leaf(container, search, prefix=()):
    """DFS for the path to the first selectable leaf the search reveals, so the
    cursor can jump straight to it (auto-expanding the branches above). A branch
    that matches by its own label contributes its first leaf unfiltered."""
    for key, value, label, is_branch in _dd_visible_entries(container, search):
        path = tuple(prefix) + (key,)
        if not is_branch:
            return path
        sub_search = "" if (search and search in label.lower()) else search
        sub = _dd_first_match_leaf(value, sub_search, path)
        if sub is not None:
            return sub
    return None


def _dd_as_tuple(x):
    """Coerce a stored path-state to a tuple. The states are meant to be key
    tuples, but DropDownState is a DictConversion and its machinery can alias a
    complex stored value (e.g. a Lora) across fields; this keeps the dropdown
    robust to any input type by never iterating a non-sequence."""
    if isinstance(x, tuple):
        return x
    if isinstance(x, list):
        return tuple(x)
    return ()


def _dd_obj_tint(obj, fallback=None):
    """An object's embedded tint (a 3+-tuple `.tint`, e.g. on a Lora), else
    `fallback`. Used to colour each row by its value and the trigger by the
    selected value."""
    t = getattr(obj, "tint", None)
    if isinstance(t, (tuple, list)) and len(t) >= 3:
        return tuple(t)
    return fallback


def _dd_label_for_path(collection, path):
    """Display label for a selected leaf path: the KEY for a dict entry (e.g.
    "red"), the VALUE for a list entry (e.g. "left"). Used for the trigger title
    so it reads as a name, not a raw value (which may be a tuple/number)."""
    if not path:
        return ""
    parent = _dd_walk(collection, tuple(path[:-1]))
    if isinstance(parent, dict):
        return str(path[-1])
    return str(_dd_walk(collection, tuple(path)))


def _dd_set_cursor(root_state, cursor_path, is_branch):
    """Point the highlight at `cursor_path` and derive the open path from it: a
    branch expands its own sub-menu, a leaf collapses back to its parent level.
    This is the single writer for both hover and keyboard, so they stay in sync."""
    if root_state is None:
        return
    new_cursor = tuple(cursor_path)
    new_open = tuple(cursor_path) if is_branch else tuple(cursor_path[:-1])
    if (new_cursor == _dd_as_tuple(root_state.cursor_path)
            and new_open == _dd_as_tuple(root_state.open_path)):
        return
    root_state.cursor_path = new_cursor
    root_state.open_path = new_open



def _dd_close(root_state):
    """Reset popover state on close: collapse the open/cursor paths, clear the
    search query, and release the search box's text focus if it held it."""
    if root_state is None:
        return
    root_state.open_path = ()
    root_state.cursor_path = ()
    root_state.search_query = ""
    root_state.search = ""
    root_state._focus_search = 0
    root_state._had_focus = False
    box_tile = getattr(root_state, "_search_box_tile", None)
    tf = Melty.text_focused_ds
    if tf is not None and box_tile is not None and getattr(tf, "_tile_id", None) == box_tile:
        Melty.text_focused_ds = None


def _dd_handle_keys(collection, root_state, search="", text_focused=False):
    """Arrow-key navigation while the popover is open. Up/Down move within the
    current level, Right (or Enter on a branch) descends, Left collapses to the
    parent, Enter on a leaf picks it. Returns the picked leaf value, or
    UNSET_VALUE when nothing was chosen this frame. Reads the GLFW-callback key
    queue so it works without the menu being hovered."""
    if root_state is None:
        return UNSET_VALUE
    keys = list(Core.melty.frame_key_events)

    def pressed(*codes):
        return any(k in codes for k, _ in keys)

    down = pressed(glfw.KEY_DOWN)
    up = pressed(glfw.KEY_UP)
    right = pressed(glfw.KEY_RIGHT)
    left = pressed(glfw.KEY_LEFT)
    enter = pressed(glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
    # While typing a query, Left/Right are the search box's text cursor, not tree
    # navigation, so the dropdown doesn't double-act on them.
    if text_focused and search:
        left = right = False
    if not (down or up or right or left or enter):
        return UNSET_VALUE

    # A nav key fired: switch to keyboard-select mode so hover stops moving the
    # cursor until the mouse actually moves again (cleared in draw_dropdown).
    root_state._kbd_mode = True

    # Navigation level = the cursor's parent; rows = its (search-filtered)
    # siblings. Fall back to the root level if the cursor path went stale.
    cursor = _dd_as_tuple(getattr(root_state, "cursor_path", ()))
    level = cursor[:-1]
    rows = _dd_rows_at(collection, level, search)
    if not rows:
        level = ()
        rows = _dd_rows_at(collection, level, search)
        cursor = ()
    if not rows:
        return UNSET_VALUE

    level_keys = [r[0] for r in rows]
    had_cursor = bool(cursor) and cursor[-1] in level_keys
    idx = level_keys.index(cursor[-1]) if had_cursor else 0

    # Left collapses the current sub-menu and highlights its parent row.
    if left and level:
        root_state.cursor_path = tuple(level)
        root_state.open_path = tuple(level[:-1])
        request_render()
        return UNSET_VALUE

    # From no selection, the first Up/Down just focuses on the first row; otherwise
    # it steps (wrapping). Right/Enter act on whatever row is current.
    if had_cursor:
        if down:
            idx = (idx + 1) % len(rows)
        elif up:
            idx = (idx - 1) % len(rows)
    key, value, label, is_branch = rows[idx]
    new_cursor = tuple(level) + (key,)

    # Right / Enter on a branch descends into its first visible child.
    if (right or enter) and is_branch:
        kids = _dd_rows_at(collection, new_cursor, search)
        if kids:
            ck, cv, _cl, cbr = kids[0]
            _dd_set_cursor(root_state, new_cursor + (ck,), cbr)
            request_render()
            return UNSET_VALUE

    if enter and not is_branch:
        _dd_set_cursor(root_state, new_cursor, False)
        root_state._picked_path = tuple(new_cursor)
        return value

    _dd_set_cursor(root_state, new_cursor, is_branch)
    request_render()
    return UNSET_VALUE


_DD_MENU_W = 170
_DD_ROW_H = 24


def _dd_noop_set(*_a, **_k):
    """No-op set_attr for the draw_collection rendering a dropdown level: the rows
    list is rebuilt each frame, so draw_collection must NOT write a picked value
    back into it — the pick is surfaced via return_item instead."""
    return None


@render_func(use_cache=True, show_bg=True, shadow=True, selectable=False, temp=True,
             closable=True, melty_window=False, auto_resize=True, with_header=None,
             max_height=420, min_width=300, swoosh=False)
def draw_dd_menu(input_value, draw_state, root_state=None, unique=0, path_prefix=(), tint=None,
                 show_search=True, text_align="right", row_tags=None, **kwargs):
    """One level of the dropdown, drawn as its own temp popover window. Iterates
    the level's entries and renders each as a row (`_dd_menu_row`); a leaf click
    or a pick inside a nested sub-menu bubbles back up as (changed, value).

    Expansion is driven by `root_state.open_path` (a single chain of keys, see
    DropDownState) — NOT by each row testing its own hover. A row renders its
    sub-menu only when `open_path` runs through it, so at most one sub-menu is
    open per level and hidden siblings can never resurface. `path_prefix` is this
    level's key chain from the root; each row's full path is prefix + its key.

    `show_search` draws the root-level filter box. Autocomplete (the code editor's
    suggestion popup) passes False: the editor itself owns text focus and the
    half-typed identifier IS the filter, so a second focus-stealing search box
    would fight it. The caller pre-filters the rows in that case.

    The popover and its rows are CACHED tiles; cursor/open paths live in
    root_state (mutated in place), which cache keys can't see. Repaints are
    driven by explicit invalidation: hover via _dd_set_cursor, keys via the
    begin_frame popover hook (or the code editor's per-event invalidate_up) —
    invalidate_up specifically, since it cascades to the row tiles; a plain
    invalidate leaves the inner dd_rows collection clean and it blit-skips."""
    if show_search and not path_prefix and root_state is not None:
        # Root owns the search box. Single-line so Up/Down/Enter pass through to
        # menu nav; it auto-focuses once when the menu opens (_focus_search).
        q = getattr(root_state, "search_query", "") or ""
        box = draw_text(q, name=f"dd_search{unique}", show_name=False, searchable=False,
                        single_line=True, is_search_box=True, is_tree=False,
                        with_header=None, with_footer=None, show_bg=True, shadow=False,
                        request_focus=getattr(root_state, "_focus_search", 0) > 0,
                        font=Font.JETBRAINS_MONO_19,
                        tint=tint, return_extras=True)
        q_changed, new_q = box[0], box[1]
        box_ds = box[2] if len(box) > 2 else None
        if box_ds is not None:
            root_state._search_box_tile = box_ds._tile_id
            # Focus retry is a bounded countdown: the first popover's
            # move-to-front clears text focus the frame the box first grabs it
            # (apply_move_to_front(): parent != front window), so a one-shot
            # request is lost. Re-request for a few frames until it lands, then
            # clear (0). Draw_dropdown drives the re-runs while this is > 0.
            if Melty.text_focused_ds is box_ds:
                root_state._focus_search = 0
            elif getattr(root_state, "_focus_search", 0) > 0:
                root_state._focus_search -= 1
        new_search = str(new_q or "").strip().lower()
        if q_changed:
            root_state.search_query = new_q
            # Jump the cursor onto the first matching leaf, auto-expanding all
            # branches above it, so the leaf is visible and one Enter selects it
            # (instead of Enter-to-open-then-Enter-to-pick).
            if new_search:
                leaf = _dd_first_match_leaf(input_value, new_search)
                if leaf is not None:
                    _dd_set_cursor(root_state, leaf, False)
                else:
                    root_state.cursor_path = ()
                    root_state.open_path = ()
            else:
                root_state.cursor_path = ()
                root_state.open_path = ()
        root_state.search = new_search

    search = str(getattr(root_state, "search", "") or "")
    # Cursor/open paths are passed down as ROW INPUTS (not read by the row's
    # root_state): a row is a use_cache=True tile, so its highlight only repaints
    # when an input changes. Throwing the paths through makes keyboard nav and the
    # default-selection-on-open repaint (hover repaints via a separate hook).
    open_path = _dd_as_tuple(getattr(root_state, "open_path", ()))
    cursor_path = _dd_as_tuple(getattr(root_state, "cursor_path", ()))

    ancestor_matched = bool(search) and any(search in str(k).lower() for k in path_prefix)
    rows = _dd_visible_entries(input_value, "" if ancestor_matched else search)
    # if not rows and search:
    #     imgui.dummy(180, 6)
    #     text("  No matches", width=180, height=_DD_ROW_H, name="dd_nomatch",
    #          text_colour=(1, 1, 1))
    #     return False, None

    # Render the level's rows through draw_collection so it scrolls + virtualizes
    # (off-screen culling) for free - the old manual loop rendered EVERY row each
    # frame, which crawled for the 967-icon list. Each row is a
    # (key, value, label, is_branch) tuple handed to _dd_menu_row, which derives
    # its own path/cursor/open-state from path_prefix + root_state. A leaf click /
    # sub-menu pick bubbles back as a picked value via return_item; the no-op
    # set_attr keeps draw_collection from writing that value back into `rows`.
    changed, picked = draw_collection(
        rows, name=f"dd_rows_{unique}", show_search=False, show_bg=False,
        with_header=None, mode=None, return_item=True, set_attr=_dd_noop_set,
        item_spacing_y=0, use_cache=True,
        child_kwargs={"view_func": dd_menu_row, 'show_bg':False, 'shadow':False, "path_prefix": tuple(path_prefix),
                      "root_state": root_state, "tint": tint, "text_align": text_align, 'z_offset':0,
                      "row_tags": row_tags, "cursor_path": cursor_path, "open_path": open_path})
    return (True, picked) if changed else (False, input_value)


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False, temp=True, show_add_delete=False,
             with_header=None, disable_scroll=True, min_width=300, swoosh=False, z_offset=0)
def dd_menu_row(input_value, draw_state, text_align="right", path_prefix=(),
                root_state=None, tint=None, row_tags=None, cursor_path=(), open_path=(), **kwargs):
    """A single menu row. `input_value` is the row TUPLE (key, value, label,
    is_branch) — draw_collection hands each level's rows in one at a time (so it
    can scroll / virtualize the level for free). `cursor_path` / `open_path` are
    passed IN (not read from root_state) so they're cache-key inputs: a row is a
    use_cache=True tile, so its keyboard highlight / open chevron only repaint when
    an input changes. Leaves are a button returning the VALUE on click; branch rows
    show a chevron and own a nested `draw_dd_menu` to their right, shown only while
    on the open path. Hovering points the shared cursor here; the highlight is
    painted over the row box when hovered / keyboard-current.

    `row_tags` (optional) maps a value -> short dim string drawn right-aligned —
    the code editor's completion popup uses it for the kind label (func/class/…)."""
    key, value, label, is_branch = input_value
    row_path = tuple(path_prefix) + (key,)
    open_path = _dd_as_tuple(open_path)
    cursor_path = _dd_as_tuple(cursor_path)
    sub_open = open_path[:len(row_path)] == row_path
    is_cursor = cursor_path == row_path
    tag = row_tags.get(value) if row_tags else None

    hovered = draw_state._bounding_hovered
    # Colour the row by its value's embedded tint (e.g. a Lora's .tint), falling
    # back to the menu tint for plain values.
    tint = _dd_obj_tint(value, tint)
    fa_chrevron_right = f"\uf054"

    kbd_mode = getattr(root_state, "_kbd_mode", True)
    if hovered and not kbd_mode:
        _dd_set_cursor(root_state, row_path, is_branch)

    active = is_cursor if kbd_mode else hovered
    if active:
        dl = imgui.get_window_draw_list()
        dl.add_rect_filled(draw_state.abs_left, draw_state.abs_top,
                           draw_state.abs_left + draw_state.width,
                           draw_state.abs_top + draw_state.height,
                           imgui.get_color_u32_rgba(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    chevron = f"  {fa_chrevron_right}" if is_branch else "    "  # fa-chevron-right
    if is_branch:
        clicked, _ = button(f"{label}{chevron}", name=f"{label}_ddrow", width=draw_state.content_width - 10,
                            height=_DD_ROW_H, hovered=hovered, text_value=0.36, text_saturation=0.799, shadow=False,
                             rounding=0, show_button_bg=False, show_bg=False, use_cache=True,
                            text_align=text_align, tint=tint)
    else:
        clicked, _ = button(f"{label}{chevron}", name=f"{label}_ddrow", show_button_bg=False, 
                            width=draw_state.content_width - 10, height=_DD_ROW_H, hovered=hovered,
                            text_saturation=0.716, z_offset=0, shadow=False, show_bg=False, use_cache=True,
                            text_align=text_align, tint=tint)

    # In keyboard-select mode the arrow keys paint the highlight; hover neither
    # moves the cursor nor paints, until the mouse moves (draw_dropdown clears it).


    # ONE highlight, framed to this row's box. Mouse mode keys off the live hover;
    # keyboard mode keys off the cursor. Using a separate source per mode (rather
    # than hover OR cursor) avoids briefly painting both the stale-cursor row and
    # the freshly-hovered row, which doubled the wash and looked inconsistent.


    # Dim kind tag, right-aligned over the row (drawn last so it sits above the
    # highlight). The name is left-aligned by the caller's text_align. A long
    # label could run under a long tag, so the tag gets an opaque backing rect
    # first: the parent window's actual bg fill (bg_color_stack top = nearest
    # show_bg ancestor) with the active-row wash (white @ 0.16, see above)
    # re-composed in, so the mask is invisible on both active and highlighted
    # rows while still cutting off the label.
    if tag:
        dl = imgui.get_window_draw_list()
        tw = imgui.calc_text_size(tag).x
        tx = draw_state.abs_left + draw_state.width - tw - 10
        ty = draw_state.abs_top + (draw_state.height - imgui.get_text_line_height()) * 0.5
        if Melty.bg_color_stack:
            r, g, b = Melty.bg_color_stack[-1][:3]
            if active:
                r, g, b = r * 0.84 + 0.16, g * 0.84 + 0.16, b * 0.84 + 0.16
            mask = imgui.get_color_u32_rgba(min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0),
                                            min(max(b, 0.0), 1.0), 1.0)
            dl.add_rect_filled(tx - 6, draw_state.abs_top + 1,
                               tx + tw + 6, draw_state.abs_top + draw_state.height - 1, mask)
        dl.add_text(tx, ty, imgui.get_color_u32_rgba(0.55, 0.6, 0.72, 0.85), tag)

    if is_branch:
        # Always call the sub-menu (so off-path ones stay registered but hidden
        # via closed=True, never leaving a stale painted frame); only the on-path
        # branch actually draws. Pinned to the right of this row with window_pos.
        changed, picked = draw_dd_menu(value, name=f"{label}_submenu", tint=tint,
                                       closed=not sub_open, temp=True, use_cache=True,
                                       window_pos=(draw_state.width, -_DD_ROW_H), show_add_delete=False,
                                       parent_window=draw_state, disable_scroll=False,
                                       root_state=root_state, path_prefix=row_path)
        if changed:
            return True, picked
    elif clicked:
        root_state._picked_path = tuple(row_path)
        return True, value

    return False, value


@render_func(is_default_for=(DrawState), tint=(0.2, 0.6, 0.8), show_bg=True, shadow=False, with_header=None)
def draw_draw_state_info(input_value: DrawState):
    imgui.text(f"DrawState")
    imgui.text(f"Tile ID: {input_value._tile_id}")
    imgui.text(f"Content WxH: {input_value.content_width} x {input_value.content_height}")


@render_func(use_cache=True, with_header=draw_header, show_bg=True, is_default_for=Pending)
def draw_pending(input_value, draw_state=None):
    imgui.text(input_value.originated.__name__)
    imgui.push_text_wrap_pos(draw_state.left + draw_state.content_width)
    imgui.text_wrapped(str(input_value.status))
    imgui.pop_text_wrap_pos()

    return False, input_value


from src.lsd.gl_gui.model.core_model.core_enums import PendingAction


@render_func(use_cache=True, show_header=False, shadow=True)
def pending_window(input_value, button_name, pending=None, draw_state=None,
                   show_revert=False, show_load=False):
    draw_text(str(pending.status), width=draw_state.width, name="Status", show_bg=True, shadow=False, with_footer=None)
    imgui.dummy(0, 5)

    if button(str(button_name), width=100, height=25)[0]:
        return True, PendingAction.APPLY
    if show_revert:
        same_line()
        if button("Revert", width=100, height=25, color=(0.8, 0.3, 0.3),
                  factor=0.3, value=0.0, text_value=2.0, saturation=0.4)[0]:
            return True, PendingAction.REVERT
    if show_load:
        same_line()
        if button("Load", width=100, height=25, color=(0.3, 0.5, 0.8), factor=0.8)[0]:
            return True, PendingAction.LOAD

    return False, input_value


@render_func(use_cache=True, max_height=500, searchable=False)
def draw_search(input_value=None, draw_state=None):
    """Floating find bar for searchable views that have no header. Rendered as
    a Mode.WINDOW from core_render when search is active; draws the shared
    render_search UI against the owning view's draw_state (search_owner)."""
    owner = input_value
    render_search(owner, unique=owner._tile_id, draw_state=draw_state)

    if not input_value.search_active:
        draw_state.closed = True

    return False, input_value


@render_func(use_cache=True, show_header=True, selectable=False, with_header=draw_header)
def draw_single(input_value: any, view_func=None, mode: any = None, **kwargs):
    changed, return_val = view_func(input_value, mode=mode)
    return changed, return_val


@render_func(use_cache=False, show_header=True, max_height=30, selectable=False, with_header=draw_header)
def draw_blank(input_value: any, **kwargs):
    return False, None


def draw_any(input_value: any = None, view_func=None, mode: any = None, chain=None, **kwargs):
    # ── New chain system (opt-in) ─────────────────────────────
    # if chain is not None:
    #     from src.lsd.gl_gui.view.core_conversion.chain import run_chain
    #     return run_chain(chain, input_value, **kwargs)

    # print(f"{kwargs.get('key', None)}: draw_any called with type {type(input_value).__name__} and view_func {view_func.__name__ if view_func else None}")

    # # meta selection
    kwargs_view_func = view_func
    key = kwargs.get("key", None)
    real_type = kwargs.get("real_type", type(input_value))
    collection_type = kwargs.get("type_collection", type(kwargs.get("collection", None)))

    # if view_func is None:
    #     view_func = Core.melty.get_default_view_function(real_type=real_type, collection_type=collection_type, attrib_key=key)
    #
    # if view_func is None:
    #     view_func = draw_collection

    if view_func is None:
        new_default = Core.melty.get_default_view_function(real_type=real_type, collection_type=collection_type,
                                                      attrib_key=key)
        if new_default is None:
            new_default = draw_collection
        if view_func is None:
            view_func = new_default

    # Explicit chain= (e.g. a lens) runs the render_func chain executor directly,
    # bypassing type/mode routing. Mode-derived chains are still handled below.
    if chain is not None:
        return run_chain(input_value, chain=chain, **kwargs)

    if mode is None:
        mode = Core.melty.mode_stack[-1] if len(Core.melty.mode_stack) > 0 else None

    if isinstance(mode, tuple) and len(mode) > 0:
        main_mode = mode[0]
    else:
        main_mode = mode

    # --- Search: forward the active search term to searchable child views ---
    # The term rides Core.melty.search_stack so it reaches the whole subtree. We
    # simply hand it to each searchable view via its `search_text` param and
    # let the view decide what to do with it (the text editor highlights
    # matches in place). No value conversion or filtering happens here.
    # if (len(Core.melty.search_stack) > 0
    #         and (getattr(kwargs_view_func, '_searchable', False) or kwargs.get("searchable", False))
    #         and "search_text" not in kwargs):
    #     kwargs["search_text"] = Core.melty.search_stack[-1]

    if main_mode is not None:
        # Loop over super types
        mode_config = main_mode.get_config_for(input_value)
        if mode_config is not None and mode_config.func is None and (
                mode_config.kwargs.get("convert", None) is not None
                or mode_config.kwargs.get("convert_in", None) is not None):
            convert_in = mode_config.kwargs.get("convert_in", None)
            if convert_in is not None:
                # Infer target type from the return annotation of the last converter
                import inspect
                last_fn = convert_in[-1]
                ret = inspect.signature(last_fn).return_annotation
                convert_to_type = ret if ret is not inspect.Parameter.empty else None
            else:
                convert_to_type = mode_config.kwargs["convert"][-1]
            mode_config = main_mode.get_config_for(the_type=convert_to_type) if convert_to_type is not None else None
            if mode_config is not None and mode_config.func is not None:
                view_func = draw_single
                kwargs_view_func = mode_config.func

        elif mode_config is not None and mode_config.func is not None:
            if isinstance(mode_config.func, tuple):
                kwargs['chain'] = mode_config.func
                kwargs['route'] = mode_config.route
                view_func = run_chain
            else:
                view_func = mode_config.func
                kwargs_view_func = view_func

    # kwargs['use_cache'] = True
    kwargs['mode'] = mode
    kwargs['view_func'] = kwargs_view_func

    return_val = view_func(input_value, **kwargs)

    return return_val

