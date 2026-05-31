import inspect
import sys
import threading
import types
from collections import deque, defaultdict
from collections.abc import MutableMapping
from enum import Enum
from inspect import Parameter
from math import sqrt
from pathlib import Path
from types import NoneType

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
from src.lsd.gl_gui.toggles import Toggles, Tint
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, push_style_var, \
    pop_style_var, end, begin
from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace, request_render
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import Comment, GeneralParse, UsageRef
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.codec_register import registry as FILE_CODECS
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.cst_proxy import *
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import hotkey, tint
from src.lsd.gl_gui.view.core_views.decoration.invalidation_decoration import live
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.folders_proxy import FolderProxy
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_footer, render_search
from src.lsd.gl_gui.view.core_views.inspect_utils import set_fn_defaults
from src.lsd.gl_gui.view.core_views.tensor_views import draw_tensor
from src.lsd.gl_gui.view.core_views.text_editor import draw_text, _scroll_into_view
from src.shader_library.shader_manager.texture_manager import PendingTexture
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


@render_func(use_cache=True, show_bg=True, width=20, height=22, tile_mode=TileMode.MAX,
             auto_resize=False, just_shadow=True, selectable=False, no_cursor=True, temp=True)
def empty(input_val):
    pass


@render_func(is_default_for=types.ModuleType, use_cache=True,
             show_bg=True, with_header=draw_header, with_footer=draw_footer)
def draw_module(input_value: types.ModuleType, draw_state, **kwargs):
    imgui.text(f"Module: {input_value.__name__}")


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
    target = Melty.find_window(name) if name else None
    if target is not None and target is not win:
        target.closed = False
        # Summon it (move + raise) to where the search is, so it comes to you
        # instead of staying at its old, maybe off-screen, spot.
        gs = Melty.find_window("GlobalSearch")
        if gs is not None and gs.abs_left is not None:
            Melty.summon_window(target, gs.abs_left, gs.abs_top)
        else:
            Melty.move_window_to_front(target)
        Melty.focused_ds = target
        # Sole-select it so it shows Melty's selection outline.
        Melty.selected = {target}
        Melty.last_selected = target
        request_render()
        return

    if win is not None:
        win.closed = False
        Melty.move_window_to_front(win)
    Melty.focused_ds = ds
    Melty.selected = {ds}
    Melty.last_selected = ds
    if ds.abs_top is not None and ds.height is not None:
        _scroll_into_view(ds, ds.abs_top, ds.abs_top + ds.height)
    request_render()


def _dismiss_global_search():
    """Close the GlobalSearch window and release the box's text focus — called
    after a result is activated (clicked or Enter), so picking a result also
    dismisses the search. Also clears the query, so the next open starts fresh
    (Esc, which doesn't call this, leaves the query for resuming)."""
    win = Melty.find_window("GlobalSearch")
    if win is not None:
        win.closed = True
    GlobalSearch.query = ""
    GlobalSearch._last_query = None
    GlobalSearch.selected = 0
    Melty.text_focused_ds = None
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


@render_func(is_default_for=(dict, MutableMapping, defaultdict, tuple, list, GeneralParse), use_cache=True,
            header_same_line=False, show_bg=True, show_instance_vars=False, align_header=False,
            manual_content_height=True, disable_scroll=True, shadow=True, selectable=False,
            wrap=False, with_header=draw_header, indent_size=5, searchable=True)
def draw_collection(input_value, draw_state, depth, style_manager, meta,
                    mode=None, keys=None, get_attr=None, set_attr=None, show_excluded=False,
                    child_kwargs=None, show_bg=True, show_search=True, align_header=False,
                    on_collapse=False, search_text="", return_item=False,
                    on_expand=False, show_add_delete=True, item_spacing_y=1, show_system=False,
                    horizontal=False, show_indices=False, excluded=None, **kwargs):
    """
    Universal collection renderer
    """
    if excluded is None:
        excluded = set()

    if child_kwargs is None:
        child_kwargs = {}

    changed = False
    if hasattr(input_value, 'children') and isinstance(input_value.children, (list, dict, defaultdict,
                                                                              types.MappingProxyType, deque)):
        input_value = input_value.children

    if show_bg and draw_state.total_z_offset < 0:
        imgui.dummy(1,3)
    else:
        imgui.dummy(1,1)


    # --- Setup per collection type ---
    collection = input_value
    # When the value *is* a class (e.g. an @window-registered class drawn
    # directly), its meta-attribute `{field}_meta` overrides live on the class
    # itself, not on its metaclass. Use the class as parent_type so get_child_meta
    # can find them; otherwise fall back to the instance when type.
    parent_type = input_value if isinstance(input_value, type) else input_value.__class__
    if isinstance(input_value, (str, int, float, bool, Enum, NoneType)):
        imgui.text("No view for type: " + str(type(input_value)))
        return False, input_value
    if keys is None:
        if isinstance(input_value, (dict, list, tuple, set, defaultdict, MutableMapping, types.MappingProxyType, deque)):
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

        elif hasattr(input_value, "__dict__") and depth < Melty.max_depth:
            if hasattr(type(input_value), "__field_defaults__") and hasattr(input_value, 'to_dict'):
                type(input_value).__field_defaults__.update(input_value.__dict__)
                keys = type(input_value).__field_defaults__.keys()
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

    # --- search (key matching) ---
    # The dual resolution like the text editor: a forwarded SearchTerm carries
    # the shared cross-view session, or the owner's own session when this
    # collection hosts the find UI. We claim one highlight per matching key, in
    # visual order interleaved with the children (claimed below) so the
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
    # the off-screen detection would skip - until the row holding the current
    # match is reached and can scroll into view.
    _search_full_render = search_session is not None and search_session.scroll_to

    # Stash a matcher so the search owner's tree walk (DrawState.descend /
    # melty.search_walk) can count this collection's key matches without
    # rendering. It counts only this collection's key keys (the whole key list,
    # not just the rows the loop below draws); child collections / text editors
    # are separate tree nodes with their own matchers, so the walk sums them
    # without double-counting. Keys are recomputed lazily on call (only on
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
    # local index on us (_search_active_local) when one of OUR keys holds it -
    # the same walk also computed the count, so index and count agree.
    # Translate that index (over our full matches, in visual order) to the key
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
    rect = Melty.get_clip_rect()

    premature_break = False

    Melty.collection_index_stack.append(0)
    this_collection = len(Melty.collection_index_stack) - 1

    max_items = 5000
    start_index = 0
    end_index = min(len(keys) - 1, max_items)

    scroll_offset = draw_state.scroll_offset
    true_left = draw_state.left - scroll_offset[0]
    true_top = draw_state.top - scroll_offset[1]

    # remove excluded from keys
    item_to_return = None

    for idx in range(start_index, end_index + 1):
        key = keys[idx]
        relative_pos = imgui.get_cursor_screen_pos()
        relative_pos = (relative_pos[0] - true_left,
                        relative_pos[1] - true_top + item_spacing_y)

        child_draw_state = draw_state._children.get(idx, None)

        # ----- off-screen detection -----
        # Is this row scrolled outside the viewport? When not searching, skip it
        # entirely up front (the perf early-out). On a search-counting frame keep
        # `clipped` to decide below: reuse a cached match count (no render) or
        # render to (re)count.
        clipped = False
        if (not horizontal and child_draw_state is not None
                and child_draw_state.relative_pos is not None
                and not Melty.frame_count <= 2
                and (not draw_state.invalid_content_height or imgui.is_mouse_down(0)
                     or imgui.is_mouse_down(1) or imgui.is_mouse_down(2))):
            _spy = true_top + child_draw_state.relative_pos[1] - child_draw_state.header_height
            _bottom = _spy + child_draw_state.height + child_draw_state.header_height
            clipped = (_bottom + child_draw_state.height < rect[1] or _spy > rect[3])

        if clipped and not _search_full_render:
            imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0],
                                         (true_top + child_draw_state.relative_pos[1] +
                                          child_draw_state.height)))
            continue

        Melty.collection_index_stack[this_collection] = idx
        item = None
        if get_attr is None:
            if isinstance(collection, dict) and key not in collection:
                imgui.text("Key not found: " + str(key))
                continue

            if hasattr(input_value, "__dict__") and hasattr(input_value, key):
                item = getattr(input_value, key, None)
            else:
                item = collection[key]
        else:
            try:
                item = get_attr(input_value, key)
            except Exception as e:
                imgui.text(f"Error getting key {key}")

        # visual separator (object extras)
        if key is None and item is None:
            seperator(Melty.spacing[1])
            continue

        if not show_excluded and hasattr(type(input_value), "__excluded_attrs__"):
            if not Toggles.show_excluded:
                if str(key) in type(input_value).__excluded_attrs__:
                    continue
        display_name = None

        if str(key).split("##")[0] in excluded:
            continue

        # apply global skip to all types
        if isinstance(key, (float, Enum, NoneType)):
            key_str = f"{input_value.__class__.__name__}"
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)

        if not show_excluded and (not show_system and (key_str.startswith("_") or key_str.endswith("_"))):
            continue

        # ----- SEARCH (key match) -----
        # Whether this key is the search-current match was decided by the
        # owner's pre-loop walk (resolved to _current_key_idx above); we just
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

            y_offset = Melty.collection_spacing
            if show_indices:
                display_name = f"{str(idx)}"

           

            item_kwargs = {
                'type_collection' : kwargs.get("real_type", type(input_value)),
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
                'y_offset': y_offset,
                'mode': mode,
                'search_match': key_is_match,
                'search_current': key_is_current,
            }
            

            # Per-field overrides: a `# [tint=...]` comment above a primitive
            # field is stored on the parent under __overrides__['__<field>__'].
            # Feed those into the child's kwargs (the child has no dict of its
            # own to store them). Skip dunder keys defensively.
            if isinstance(input_value, dict):
                _parent_ov = input_value.get("__overrides__")
                if isinstance(_parent_ov, dict):
                    _field_ov = _parent_ov.get(f"__{key}__")
                    if isinstance(_field_ov, dict):
                        for _ok, _ov in _field_ov.items():
                            if not (isinstance(_ok, str) and _ok.startswith("__")):
                                item_kwargs[_ok] = _ov

            item_kwargs = item_kwargs | child_kwargs
            if isinstance(input_value, (list, tuple)) or horizontal:
                item_kwargs['align_header'] = False

            if horizontal and child_draw_state is not None:
                rect = Melty.get_clip_rect()
                right_edge = rect[2]
                space_left = right_edge - (imgui.get_cursor_screen_pos()[0] + child_draw_state.width)

                if space_left < 0:
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
                if key_is_current:
                    search_current_h = returned_ds.header_height
                if horizontal:
                    imgui.same_line(spacing=0)
                    imgui.set_cursor_screen_pos((returned_ds.abs_left + returned_ds.width, returned_ds.abs_top))
                else:
                    imgui.dummy(0, item_spacing_y)

            if isinstance(out_val, CollectionAction):
                # perform the move; this should mutate the plain dicts you supply
                result = Melty.to_apply(out_val)
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

    Melty.collection_index_stack.pop()

    # When navigation just happened, scroll the current key into view.
    # draw_collection disables its own scroll, so _scroll_into_view walks up fo
    # the real scroll container. (A current match inside a child is scrolled by
    # the child itself.) _current_key_idx came from the owner's index.
    if search_session is not None and search_session.scroll_to:
        draw_state._search_current_key = _current_key_idx
        if search_current_y is not None:
            h = search_current_h or imgui.get_text_line_height()
            _scroll_into_view(draw_state, search_current_y, search_current_y + h)

    end_pos = imgui.get_cursor_pos()[1]
    content_height = (end_pos - start_cursor)
    imgui.dummy(1, 1)

    if not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2) and not premature_break:
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
def draw_property(input_value:property, draw_state, **kwargs):
    imgui.text_colored(f"Property: {input_value.fget.__name__}", 1.0, 0.5, 0.0, 1.0)
    # value = input_value.fget(input_value)
    # draw_any(value, name="value", show_bg=True, draw_state=draw_state)

@render_func(is_default_for=(type), show_bg=True, align_header=False, use_cache=True, shadow=False,
             with_header=draw_header)
def draw_type(input_value:type, **kwargs):
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


@render_func(show_bg=True, use_cache=True, selectable=False, with_header=draw_header, bg_offset=3, auto_resize=True)
def draw_global_search(input_value, draw_state=None, **kwargs):
    """Renders the GlobalSearch window: the search box plus the matching nodes
    from the draw_state tree draw_main registered on us. Results are recomputed
    only when the query changes (the walk is the expensive part)."""
    # Expose our own window draw_state + honour a focus request from draw_main's
    # Ctrl+Shift+F shortcut (one-shot: give the window's text focus this frame).
    input_value.window_ds = draw_state
    _focus = input_value._focus_requested
    input_value._focus_requested = False

    # return_extras gives the box's draw_state so we can tell when it holds text
    # focus (that is when our arrow/enter result-navigation should be live).
    _box = draw_text(input_value.query, name="Search", searchable=False, single_line=True,
                     font=Font.JETBRAINS_MONO_40, request_focus=_focus, return_extras=True)
    changed, new_query = _box[0], _box[1]
    box_ds = _box[2] if len(_box) > 2 else None
    if changed:
        input_value.query = new_query

    q = (input_value.query or "").strip().lower()
    if q != input_value._last_query:
        input_value._last_query = q
        input_value.results = (global_search_results(input_value.root, q, exclude=draw_state)
                               if input_value.root is not None and len(q) >= 2 else [])
        input_value.selected = 0  # reset highlight to the top result on a new query

    # Flatten the grouped results to their on-screen order, what the highlight
    # moves through and what each index value refers to.
    groups = group_results_by_window(input_value.results, input_value.root)
    flat = [(label, ds, win) for win, items in groups.items() for (label, ds) in items]
    n = len(flat)
    input_value.selected = (input_value.selected % n) if n else 0

    # While the box holds text focus (single-line, so Up/Down/Enter don't touch
    # it): Up/Down move the highlight by result, Ctrl+Up/Down jump between window
    # sections (group starts), and Enter launches the highlighted result.
    if n and box_ds is not None and Melty.text_focused_ds is box_ds:
        # Flat indices where each window group begins (for Ctrl jumps).
        starts = [i for i in range(n) if i == 0 or flat[i][2] is not flat[i - 1][2]]

        def _group_idx():  # index into `starts` of the group holding `selected`
            gi = 0
            for j, s in enumerate(starts):
                if s <= input_value.selected:
                    gi = j
            return gi

        downs = [m for k, m in Melty.frame_key_events if k == glfw.KEY_DOWN]
        ups = [m for k, m in Melty.frame_key_events if k == glfw.KEY_UP]
        enters = [k for k, _ in Melty.frame_key_events if k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)]
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
    # tint (from the ManagedWindow) and the highlighted row stands out. A row
    # that names a window (a Dock entry) is tinted by that window, not its
    # container, so it matches the window it represents.
    idx = 0
    for win, items in groups.items():
        win_label = str(getattr(win, 'name', '') or '').split("##")[0] or "?"
        imgui.dummy(2,10)
        text(win_label, height=26, indent_size=10, text_color=Melty.window_tint(getattr(win, 'name', None)),
             wrap=True, name=f"gsg_{idx}", width=w, font=Font.DEJAVU_SANS_22)
        for label, ds in items:
            sel = (idx == input_value.selected)
            entry = Melty.find_window(getattr(ds, 'name', None))
            tint = Melty.window_tint(getattr(ds, 'name', None) if entry is not None
                                     else getattr(win, 'name', None))
            if tint is None:
                tint = ds.tint

            if button(label, text_align="left", name=f"gsr_{idx}", width=w, height=24, show_bg=False, use_cache=False,
                      color=tint, tint_value=-0.6, factor=0.8, shadow=False, z_offset=0, saturation=1.0,
                      search_match=sel, search_current=sel, rounding=0)[0]:
                go_to_search_result(ds, win)
                _dismiss_global_search()
            idx += 1
    return False, input_value

@window(view_func=draw_global_search, mode=Modes.WINDOW_AUTO_FIT)
@defaults(tint=(0.15076258778572083, 0.2957677, 0.4697674512863159))
class GlobalSearch:
    query = ""
    root = None          # draw_main's draw_state, registered each frame
    window_ds = None     # this window's own draw_state (for the show shortcut)
    _focus_requested = False
    _last_query = None
    results = []         # cached [(label, draw_state)] for the current query
    selected = 0         # index (in on-screen order) of the arrow-key highlight


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
    tint_value=0.0
    tint_saturation=0.688
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
        mode_changed, value = draw_any(input_value, name=f"Mode: {mode} {unique}", mode=mode, selectable=False, show_name=False,
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
            func, func_kwargs = func
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
            changed=False
            value=None

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


@render_func(use_cache=False, show_bg=True, searchable=True, selectable=False,
             show_tint=True, bg_offset=-1, with_header=draw_header)
def draw_main(input_value, vis, search_text="", draw_state=None, **kwargs):
    global test_obj
    global cst_dict
    global test_code
    from src.lsd.gl_gui.view.mode import Mode

    # Register the root draw_state so GlobalSearch can walk the whole UI tree.
    GlobalSearch.root = draw_state

    # Ctrl+Shift+F: reveal the GlobalSearch window and focus its box. A
    # non_blocking action handler - so it survives a window blocker stacked in
    # front (instead of needing an extreme priority that would consume events
    # from everything else) and doesn't eat the key from other views.
    if draw_state.on_action("non_blocking_ctrl_shift_f_down"):
        gs = Melty.open_window("GlobalSearch")
        if gs is not None:
            # Summon the box to just above the cursor so it pops up where you're
            # looking and is ready to type into.
            mx, my = imgui.get_mouse_pos()
            Melty.summon_window(gs, mx, my - 65)
        GlobalSearch._focus_requested = True
        request_render()

    # Esc dismisses the GlobalSearch window while it's open. Handled here on the
    # root (always hover-eligible) rather than on the window itself, so it works
    # no matter where the cursor is. non_blocking so the front window's blocker
    # doesn't eat Esc; only subscribes while open, so it doesn't swallow Esc from
    # a per-view search otherwise.
    _gs = Melty.find_window("GlobalSearch")
    if _gs is not None and not _gs.closed and draw_state.on_action("non_blocking_escape_key_down_inverted"):
        _gs.closed = True
        Melty.text_focused_ds = None
        Melty.focused_ds = None
        request_render()

    draw_any(Melty.registered_windows, name="Dock", with_header=draw_header,
             mode=(Mode.WINDOW_MANAGER_SORTED, Mode.WINDOW))

    for window_cls, stored_kwargs in Melty.annotated_window_classes.values():

        # Copy: the stored dict is the @window decorator kwargs and persists
        # across frames. Popping view_func out of it would consume the override
        # after the first frame, so later frames fall back to draw_with_modes.
        kwargs = dict(stored_kwargs)
        kwargs.setdefault('show_bg', True)
        kwargs.setdefault('name', f"{window_cls.__name__}##@window")

        is_render_func = hasattr(window_cls, "__render_func__")
        if is_render_func:
            kwargs.setdefault('mode', Mode.MODE_WINDOW)
            kwargs['disable_scroll'] = True
            window_cls(None, **kwargs)
        else:
            kwargs['disable_scroll'] = True
            kwargs.setdefault('mode', Mode.MODE_WINDOW)
            kwargs.setdefault('modes', (Mode.CODE_UI, Mode.CODE_PLAIN_TEXT, Mode.RUNNING))
            window_func = kwargs.pop("view_func", draw_with_modes)
            window_func(window_cls, **kwargs)

    changed, value = draw_with_modes(draw_header, name="draw_header", show_bg=True, mode=(Mode.WINDOW), modes=(Mode.CODE_UI,
                                                                                                               Mode.CODE_PLAIN_TEXT))
    if changed:
        test_code = value

    changed, value = draw_any(input_value=draw_bg, name="draw_bg_new_mode",
                                     show_bg=True, mode=(Mode.CODE, Mode.WINDOW))
    if changed:
        test_code = value

    changed, value = draw_with_modes(input_value=draw_bg, name="draw_bg",
                              show_bg=True, modes=(Mode.CODE_UI,
                                                   Mode.CODE_PLAIN_TEXT,
                                                   Mode.RUNNING), mode=(Mode.WINDOW))
    if changed:
        test_code = value


    draw_with_modes(input_value=test_func, name="demo test",
                                     show_bg=True, mode=(Mode.WINDOW), modes=[Mode.CODE_PLAIN_TEXT, Mode.CODE_UI])

    draw_tensor(some_test_tensor, name="Tensor", mode=Mode.WINDOW)

    # some_path = Path("/home/lukas/test_folder/test_list.txt")
    # changed, value = draw_any(some_path, name="test_path_render", mode=(Mode.FILE_META, Mode.WINDOW))
    # if changed:
    #     path = value

    from src.lsd.gl_gui.model.app_model import TensorView
    draw_any(TensorView, name="Tensorview", mode=(Mode.WINDOW))

    # draw_any(global_toggles, name="Toggles", auto_resize=True, wrap=True, use_cache=True, show_bg=True, mode=Mode.WINDOW)
    # changed, new_val = draw_any(Melty.cache.enabled, name="Offscreen Rendering", wrap=True, show_bg=True, use_cache=True)
    # if changed:
    #     if new_val:
    #         Melty.cache.set_enabled(True)
    #     else:
    #         Melty.cache.set_enabled(False)
    #     Melty.cache.invalidate_all()
    #     request_render()

    # changed, new_val = draw_any(Melty.cache.copy_debug_mode, name="Offscreen Debug", wrap=True, use_cache=True)
    # if changed:
    #     # Melty.cache.offscreen_debug_mode = new_val
    #     Melty.cache.copy_debug_mode = new_val
    #     Melty.cache.invalidate_all()
    #     request_render()

    # changed, new_val = draw_any(Melty.cache.offscreen_scale, name="Debug Scale",
    #                             min_value=0.0, max_value=255.0, wrap=True, use_cache=True)
    # if changed:
    #     Melty.cache.offscreen_scale = new_val
    #     Melty.cache.invalidate_all()
    #     request_render()
    #
    # global some_float
    # changed, new_float = draw_window(some_float[0], name="Conversion Test", view_func=draw_collection, convert=dict)
    # if changed:
    #     print("New float value:", new_float)
    #     some_float[0] = new_float

    # global selected_tabs
    # changed, new_tabs = draw_tab_bar(selected_tabs, collection=["Alpha", "Beta", "Gamma", "Delta"],
    #                                  name="Tab Bar Demo", mode=Mode.WINDOW)
    # if changed:
    #     selected_tabs = new_tabs


    # draw_enum_tabs(ProfileType, name="Enum Tab Bar Demo", mode=Mode.WINDOW)

    # draw_window(monitor, name="Monitor")

    # draw_window(threading.enumerate(), name="Threads")

    # draw_window(core_model, name="Module test")

    # test_columns(input_value="Nksjlkjne", mode=Mode.WINDOW, name="Test Columns")

    # draw_window(draw_main, name="Draw Main Function")

    # draw_window(test_obj, name="Layer 1")

    draw_any(filesystem_proxy, name="Filesystem", mode=Mode.WINDOW)

    draw_any(input_value=proxy, name="CST Proxy", mode=Mode.WINDOW)

    global drop_down_selection
    changed, selection = draw_dropdown(drop_down_selection, collection=dropdown_demo_data,
                                       name="Dropdown Demo", mode=Mode.WINDOW, tint=(0.180984, 0.2, 0.2))
    if changed:
        drop_down_selection = selection
        print("Drop down change", str(selection))

    draw_collection(vis.root.lora_collection, name="Test Window 1", mode=Mode.WINDOW)
    draw_any(vis.root.lora_collection, name="Test Window 2", mode=Mode.WINDOW)
    draw_any(vis.root.lora_collection.loras, name="Test Window 3", child_kwargs={
       'is_tree':True, 'expanded':False, 'show_add_delete': False}, mode=Mode.WINDOW)


    # draw_window("test", name="Test Widget Window")

    # changed, new_val = draw_window(0.0, layer=31, name="Test return")
    # if changed:
    #     print("Value changed:", new_val)

    # changed, new_val = draw_window([1, 2, 3, 4, 5], name="Test List", horizontal=True, tint=(1, 0, 0))

    normalized_sub_mask, _, _ = Melty.filter.normalize(Melty.cache._mask_tex)
    draw_texture(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
                max_brightness=30, name="mask_tex", live=True, mode=Mode.WINDOW)
    draw_any(Melty.cache.snapshot_tex, show_bg=True, name="Viewport", live=True, mode=Mode.WINDOW)

    normalized_sub_mask, _, _ = Melty.filter.normalize(Melty.cache._full_mask_tex)
    draw_any(normalized_sub_mask, show_bg=True, max_contrast=30, jet=True,
                max_brightness=30, name="full_mask_tex", live=True, mode=Mode.WINDOW)
    #
    # draw_window("input_val", name="Outer live", view_func=test_widget, live=True)
    # draw_window("input_val", name="Outer no live", view_func=test_widget, live=False)

    # draw_window(Melty.last_request_render, show_bg=True, name="Last Invalid")

    mouse_pos = imgui.get_mouse_pos()
    ds_under_mouse = Melty.bvh_query(mouse_pos[0], mouse_pos[1])
    ds_names = [ds.name for ds in ds_under_mouse]
    draw_any(ds_names, name="Draw State under mouse", show_bg=True, wrap=True, use_cache=True, mode=Mode.WINDOW, live=True)
    #
    last_ds_under_mouse = list(Melty.selected)[-1] if len(Melty.selected) > 0 else None
    # if last_ds_under_mouse is not None:
    #     Melty.active_layer = last_ds_under_mouse.layer
    #     last_ds_under_mouse.layer = Melty.active_layer
    #     last_ds_under_mouse.z_pos = Melty.z_pos
    #     last_ds_under_mouse.depth_and_layer = (Melty.shadow_depth, Melty.active_layer)
    #     last_ds_under_mouse._kwargs['active_layer'] = Melty.active_layer
    #
    #     if Melty.channels_split:
    #         imgui.get_window_draw_list().channels_set_current(min(Melty.active_layer, Melty.max_depth - 1))
    #     Melty.draw(last_ds_under_mouse, detached=True)


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

    Melty.imgui_main_window_hovered = imgui.is_window_hovered()

    Melty.begin_frame()

    # imgui.invisible_button("window_blocker", width=fb_w, height=fb_h)
    imgui.set_cursor_screen_pos((0, 0))
    imgui.set_item_allow_overlap()

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)
    Melty.channels_split = True
    Melty.window_stack.append((title, True))

    draw_main(name="Main Window", vis=vis, width=fb_w, height=fb_h)

    from src.lsd.gl_gui.applet.test_applet import render_app
    render_app()

    Melty.end_frame()

    # End frame ###############
    Melty.window_stack.pop()
    draw_list.channels_merge()
    Melty.channels_split = False

    end()


@render_func(is_default_for=PendingTexture, use_cache=True, z_offset=0, selectable=False,
             show_bg=False, auto_resize=True, with_header=draw_header)
def draw_pending_texture(input_value: PendingTexture, draw_state):
    if input_value.texture_id is None:
        imgui.text(f"Uploading... {id(input_value)}")
        return False, input_value

    return_val = draw_texture(input_value.texture_id, name=f"{draw_state.id}_inner", auto_resize=False,
                              show_header=False, use_cache=True, wrap=False, tint=(0.2, 0.2, 0.3))

    return return_val


@render_func(is_default_for=numpy.uint32, show_bg=True, use_cache=False, show_add_delete=False, z_offset=2, fill_height=True,
             indent_size=0, min_width=100, min_height=100, wrap=False, disable_scroll=True,
             enable_scroll=True, zoom_speed=0.3, with_header=draw_header, manual_content_height=True)
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
        texture_id = Melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Melty.filter.jet(texture_id, offset=zoom_state.hue)
    else:
        texture_id = Melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Melty.filter.hue_saturation(
            texture_id,
            saturation=(zoom_state.saturation),
            hue_shift=(zoom_state.hue),
        )

    # texture_id = Melty.filter.swirl(
    #     input_value,
    #     radius=1.0,
    #     angle=(zoom_state.brightness * 5),
    # )
    # texture_id = Melty.filter.swirl(
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
        # Recalculate uv_size immediately for consistent bounding this frame
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

            # Recalculate new UV dimensions
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

            # Use these for Step 5
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

    # 6. Project and Draw
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

    Melty.push_clip((clip_left, clip_top, clip_right - 3, clip_bottom))
    draw_list.add_image_rounded(texture_id,
                                a=p_min,
                                b=p_max,
                                uv_a=uv_a,
                                uv_b=uv_b,
                                rounding=5.0)
    draw_list.add_rect(raw_img_left, raw_img_top, raw_img_right + 1, raw_img_bottom + 1,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       0.0, 0, 1.0)
    Melty.pop_clip()

    line_height = imgui.get_text_line_height()
    draw_list.add_text(max(p_min_x + 5, raw_img_left), clip_top - line_height - 5,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       text=f"{original_id} - {texture_id} - {width}x{height} - Zoom: {zoom_state.zoom:.2f}x")

    gl.glBindTexture(gl.GL_TEXTURE_2D, original_texture)

    return False, draw_state


@render_func(is_default_for=ManagedWindow, is_tree=False, show_name=False, use_cache=True, 
             shadow=False, show_bg=False, selectable=False, show_add_delete=False, 
             show_tint=False, wrap=False, with_header=draw_header)
def draw_managed_window(input_value, name, draw_state, mouse_down=False, selectable=False, **kwargs):
    try:
        window_draw_state = input_value.draw_state
    except Exception as e:
        imgui.text(f"Error accessing draw_state: {e}")
        return False, input_value

    window_input_value = input_value.input_value
    name = window_draw_state.name

    start_cursor = imgui.get_cursor_screen_pos()
    imgui.dummy(5, 20)
    imgui.same_line()

    if not window_draw_state.persistent and not window_draw_state.seen and window_draw_state.closed:
        Melty.delete_window(window_draw_state)

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

    # Pass the search-match flags (set by draw_header for this key) through
    # to the name button so it can draw the find highlight - the visible row is
    # this button, not a header.
    _search_match = kwargs.get("search_match", False)
    _search_current = kwargs.get("search_current", False)
    if window_draw_state.closed:
        if button(f"{name}", color=window_tint, z_offset=-2, tint_value=0.1, factor=0.95, text_value=0.3,
                  saturation=1.2, width=draw_state.content_width - target_spacing, height=button_height,
                  search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = False
    else:
        if button(f"{name}", saturation=1.5, z_offset=0, color=window_tint, factor=0.6, value=0.2, text_value=1.0,
                  width=draw_state.content_width - target_spacing, height=button_height,
                  search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = True

    imgui.same_line()

    target_icon = ""  # Target icon (FontAwesome Unicode)
    if button(f"{target_icon}##{name}", height=button_height, color=window_tint, z_offset=-2, tint_value=target_tint_value, factor=0.9,
              saturation=0.2, shadow=False)[0]:
        this_window_right = draw_state.abs_left + draw_state.width
        from_zero_x = window_draw_state.abs_left - window_draw_state.window_pos[0]
        from_zero_y = window_draw_state.abs_top - window_draw_state.window_pos[1]
        window_draw_state.window_pos = (this_window_right + 10 - from_zero_x, draw_state.abs_top - from_zero_y)
        Melty.move_window_to_front(window_draw_state)
        Melty.cache.invalidate_up_by_obj(input_value)

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
    if Melty.active_layer == Melty.drag_layer:
        return False, 0.0

    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Melty.is_window_enabled():
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
        Melty.flow_spacing += (flow_spacing)
        # imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + (flow_spacing))

    draw_list = imgui.get_window_draw_list()
    # if Melty.channels_split:
    #     draw_list.channels_set_current(min(Melty.max_depth - 1, depth + 2))

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

            if Melty.channels_split:
                draw_list.channels_set_current(min(Melty.get_channel() + 1, Melty.max_depth - 1))

                if active_drop:
                    draw_list.channels_set_current(min(Melty.get_channel() + 2, Melty.max_depth - 1))
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

            bg_tint = Melty.get_bg_color(-1)
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
                Melty.cache.mask_mark_rect(draw_state, Melty.max_depth - 1, draw_state.shadow_depth, left, top, width,
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
    if Melty.cache.enabled:
        Melty.cache.set_enabled(False)
    else:
        Melty.cache.set_enabled(True)


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
    # Melty.cache.mask_mark_rect(Melty.depth, track_x1, track_y1, track_w, track_h,
    #                            key=str(Melty.unique_stack[-1]) + "scrollbar")

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
    bg_bleed = Melty.get_bg_color(-1)
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
    some_dict = {"some_key" : -0.02, 
    "key":False,
    "key_2":2.421
    }


def draw_bg(left=25, top=0, width=0, height=57, depth=0, rounding=6.0, bg_offset=0,
        outline=True, bg_color=None, opacity=0.0,
        style_manager=None, tint=None, outline_tint=None, selected=False,
        hovered=False, pressed=False, nested_bg=False, **kwargs):
    # -- Constants ---------------------------------
    depth_wrap        = 34
    depth_scale       = 1.629
    # [tint=(1,1,1)]
    corner_radius     = rounding
    border_inset      = 2.802
    border_inset_half = 1.5
    stroke_width      = 4.0
    # How depth maps to color intensity
    intensity_factor  = 0.021
    intensity_offset  = -0.336

    some_var = [32,18,19]
    # Outline color tuning
    outline_base      = 1.765
    outline_depth_mul = 0.786
    outline_sat       = {'default': 1.1, 'nested': 1.473}
    

    # More constants 
    bleed_mix         = {'nested': 0.501, 'default': 0.446}
    bleed_style       = {'value': -0.111, 'alpha': 1.12, 'saturation': 7.045}
    outline_bleed_mix = 0.272
    # Hover offset per interaction state
    hover_offset_by_state = {
        'default':    -1.807,
        'selected':    -1.401,
        'pressed_hi': -1.813,   # pressed + opacity > 0.5
        'pressed_lo':  -0.441,
    }
    bg_style = {
        'value': -0.004, 'saturation': 1.101,
        'alpha': 0.504, 'max_value': 1.8,
    }
    
    # ── Helpers ────────────────────────────────────────────────
    def current_indent_px():
        return Melty.current_indent

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )
    # -- Depth calculation -------------------
    max_depth = 15
    wrapped_depth = min(max_depth, (Melty.bg_depth % depth_wrap) + bg_offset)
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

        snap_int(left) + border_inset_half,  snap_int(top) + border_inset_half,
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

    bleed_base = Melty.get_bg_color(-1)
    bleed_color = style_manager.make_custom_styled(
        *bleed_base, input=bg_style, **bleed_style,
    )

    # ── Outline rendering ────────────────────────────────────
    outline_value = max(0, depth_intensity * depth_mul + outline_base + hover_offset)
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
        bg_color = style_manager.make_color_style_value(input=bg_style, value=max(0, depth_intensity))
        bg_color = mix_colors(bg_color, bleed_color, bleed_factor)

    packed_fill = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)
    if tint is not None:
        packed_fill = imgui.get_color_u32_rgba(*tint[:3], opacity)

    if opacity > 0.0:
        imgui.get_window_draw_list().add_rect_filled(*fill_rect, col=packed_fill, rounding=corner_radius)

    return False, bg_color

@render_func(use_cache=True, selectable=False, disable_scroll=True,show_bg=False, min_width=10, min_height=10, wrap=True)
def button(input_value="", draw_state=None, alpha=1.0, left_mouse_held=False, shadow=True, left_mouse_down=False,
           color=(0.5, 0.5, 0.5), hovered=False, width=None, height=None, style_manager=None,
           factor=1.0, tint_value=0.32, text_value=1.023, saturation=0.8, unique=0, text_align="center",
           search_match=False, search_current=False, tint=None, rounding=None):
    if color is not None:
        if shadow:
            if left_mouse_held:
                draw_state.z_offset = 0
            else:
                draw_state.z_offset = 3.0
        else:
            draw_state.z_offset = 0.0

        if hovered:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value + 0.05,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        else:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        text_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=text_value,
                                                  factor=factor, saturation_scale=0.4, alpha=1.0)
    else:
        text_color = (1.0, 1.0, 1.0)
        mixed_color = (0, 0, 0)

    button_txt = str(input_value).split("##")[0]
    min_size = imgui.calc_text_size(button_txt)

    width = max(min_size[0] + 15, width or 10)
    height = max(min_size[1], height or 10)
    imgui.dummy(width, height)
    draw_list: _DrawList = imgui.get_window_draw_list()

    bx0, by0 = draw_state.abs_left, draw_state.abs_top
    bx1, by1 = bx0 + width, by0 + height
    # `rounding` lets callers round the corners (e.g. the search results, which
    # read as a flat wall of buttons) - else use the view's default radius.
    if rounding is not None:
        draw_state.corner_radius = rounding
    rnd = (draw_state.corner_radius) if rounding is None else rounding

    if alpha > 0.0:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  imgui.get_color_u32_rgba(*mixed_color[:3], alpha), rounding=rnd)

    # Tint fill: button dilutes `color` into a dark bg, so to show a window's tint
    # we paint the raw color over it - at low alpha so it stays a subtle wash.
    if tint is not None:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], 0.33),
                                  rounding=rnd)

    # Selection/search match highlight: translucent WHITE fill so the tint still reads
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


    return False, input_value


def render_profiler_time(input_value=None, brief=False, style_manager=None ):
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
    imgui.text("None")
    return False, input_value


@render_func(is_default_for=(bool), use_cache=True, is_tree=False, wrap=True,
header_same_line=True, min_width=20, align_header=True, shadow=False, with_header=draw_header, temp=True)
def draw_bool(input_value: bool):
    changed, is_checked = imgui.checkbox("##bool", input_value)
    if changed:
        return True, is_checked
        
    return False, input_value

@render_func(is_default_for=(str), shadow=False, wrap_text=False, show_bg=False, is_tree=False, wrap=False, show_header=False,
             show_add_delete=False, show_name=False, use_cache=True,
             disable_scroll=True, min_width=30, with_header=draw_header, temp=True)
def text(input_value: str, wrap, wrap_text=False, text_color=(1,1,1), draw_state=None, font=None):
    _font_pushed = False
    if font is not None and Melty.font_mgr is not None:
        _font_handle = Melty.font_mgr.get(font)
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
    imgui.dummy(2,0)

    if font is not None:
        imgui.pop_font()

    return False, input_value

@render_func(is_default_for=(str), shadow=False, show_bg=False, wrap=False,
             is_tree=False, show_add_delete=False, use_cache=True, min_width=60, min_height=20,
             disable_scroll=True, with_header=draw_header, temp=True)
def draw_str(input_value: str, draw_state, editable=True, immediate_return=False, alpha=1.0):
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
        if immediate_return:
            imgui.set_next_item_width(draw_state.content_width - 1)
            changed, value = imgui.input_text("##str", str(input_value))
        else:
            imgui.set_next_item_width(draw_state.content_width)
            changed, value = imgui.input_text("##str", str(input_value),
                                              flags=imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
    else:
        imgui.set_cursor_screen_pos((snap_int(draw_state.abs_left), snap_int(draw_state.abs_top)))
        # disable scrolling
        changed, value = draw_text(str(input_value), editable=True, with_header=draw_header,
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
        # ref is the original dict
        ref.update(input_value)
        return changed, ref

@render_func(is_default_for=UsageRef, use_cache=True, shadow=True, z_offset=2, show_bg=True, with_header=draw_header,
             is_tree=True, tint=(0.11, 0.1, 0.16))
def draw_usage(input_value: UsageRef):
    imgui.text(f"{input_value.path} {input_value.line}:{input_value.column} {input_value.scope} {input_value.module_name}")

    return False, input_value

@render_func(is_default_for=(Comment), shadow=True, selectable=False, use_cache=True,
             show_bg=False, with_header=None, is_tree=True, temp=True)
def draw_comment(input_value: Comment, draw_state, cursor_hover=False):
    margin = 10
    imgui.dummy(0, margin)
    line_height = imgui.get_text_line_height()
    changed, value = False, input_value
    help_yellow_tint = (9.9999e-07, 9.999989e-07, 1e-06)
    help_icon = ""
    draw_list: _DrawList = imgui.get_window_draw_list()
    character_width = imgui.calc_text_size(help_icon)[0]
    # Draw circle background for comment
    radius = 6
    center_x = draw_state.abs_left + radius
    center_y = draw_state.abs_top + radius + margin + 3
    color = imgui.get_color_u32_rgba(*help_yellow_tint, 0.3)
    # imgui.dummy(radius * 2 + 2, radius * 2)
    cursor_hover = imgui.is_item_hovered()
    # draw_list.add_circle_filled(center_x, center_y, radius, imgui.get_color_u32_rgba(0.8, 0.8, 0.7, 1.0))
    draw_list.add_text(center_x - character_width / 2, center_y - line_height / 2,
                       imgui.get_color_u32_rgba(0.8, 0.8, 0.7, 1.0), help_icon)

    # if cursor_hover:
    #     popup_max_width = 300
    #     text_size = imgui.calc_text_size(str(input_value), wrap_width=popup_max_width)
    #     popup_width = popup_max_width
    #
    #     imgui.set_cursor_screen_pos((draw_state.abs_left + radius * 2 + 5, draw_state.abs_top))
    #     draw_window(str(input_value), editable=False, window_pos=(0,0), width=popup_width, height=text_size[1] + 5,
    #                 with_header_end=None, with_header=None, with_footer=None)
    imgui.same_line(spacing=0)
    alpha = 0.572
    text(str(input_value[1:]), alpha=0, bg_offset=-2, height=20, indent_size=5, selectable=False, editable=False,
             is_tree=False, wrap=True, use_cache=True, show_bg=True, z_offset=-2, shadow=True, with_header=None, show_name=False)


    if changed:
        return True, value

@render_func(is_default_for=('tint', 'help_yellow_tint', 'context_select_tint'), has_popup=True, indent_size=0, is_tree=False,
             show_name=False, selectable=False, wrap=True, min_width=40, use_cache=False, with_header=None)
def draw_tuple(input_value: tuple, name, unique):
    if len(input_value) > 0 and isinstance(input_value[0], (float, int)):
        if len(input_value) == 4:
            # imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (4, 0))
            # imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (4, 0))

            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit4(
                f"##picker_edit{unique}{name}",
                color_list[0], color_list[1], color_list[2], color_list[3],
                flags=color_flags)

            # imgui.pop_style_var(2)
            if changed:
                input_value = (color[0], color[1], color[2], color[3])
        elif len(input_value) == 3:
            # imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (4, 0))
            # imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (4, 0))

            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL |
                           imgui.COLOR_EDIT_NO_ALPHA | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit3(
                f"##picker_edit{unique}{name}",
                color_list[0], color_list[1], color_list[2],
                flags=color_flags)

            # imgui.pop_style_var(2)
            if changed:
                input_value = (color[0], color[1], color[2])
        else:
            str_value = ", ".join([str(v) for v in input_value])
            changed, input_str = imgui.input_text("##tuple", str_value)
            if changed:
                try:
                    new_tuple = eval(f"({input_str},)")
                    if isinstance(new_tuple, tuple):
                        input_value = new_tuple
                except Exception as e:
                    print(f"Error parsing tuple: {e}")
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
             is_tree=False, show_bg=False, min_width=60,
             with_header=draw_header, temp=True)
def draw_float(input_value: float, 
               draw_state,
               min_value=-100.0, 
               max_value=99.264, 
               speed=0.0042):
    imgui.set_next_item_width(min(600, max(30, draw_state.content_width)))
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


@render_func(is_default_for=(types.MappingProxyType), shadow=False, show_bg=False,  show_add_delete=False, with_header=draw_header)
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
            Melty.cache.invalidate_all()
        draw_state._result = result

    except Exception as e:
        print(f"Error calling function '{input_value.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())

    pop_style_var(3)

    return changed, input_value


@render_func(is_default_for="LSDStudio", show_bg=True, tint=(0.6, 0.2, 0.8), with_header=draw_header)
def draw_lsd_studio(input_val):
    imgui.text("An LSD Studio Instance")


@render_func(is_default_for="ImGuiStyleManager", tint=(0.8,0.7,0), use_cache=True, with_header=None)
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


@render_func(is_default_for=(types.FunctionType, types.MethodType), show_add_delete=False, show_bg=False, 
        parent_show_add_delete=False, is_tree=False, show_name=False, with_header=draw_header)
def draw_function(input_value, name, draw_state, unique):
    if not callable(input_value):
        imgui.text("Not a callable function")
        return False, input_value

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
                        if name in Melty.global_attrs:
                            param_dict[name] = Melty.global_attrs[name]

            draw_state.params = param_dict
        if len(draw_state.params) > 0:
            changed, new_val = draw_collection(draw_state.params, name="Parameters", indent_size=0,
            show_add_delete=False,parent_show_add_delete=False, horizontal=True, child_kwargs={"wrap":True, "widtgh":200, "show_bg":True, "use_cache":True, "z_offset":2.0})
            if changed:
                draw_state.params = new_val
    except Exception as e:
        imgui.text(f"Error inspecting function parameters: {e}")
        draw_state.params = {}


    if imgui.button(f"{input_value.__name__}##{unique}"):
        try:
            draw_state.result = input_value(**draw_state.params)
            Melty.cache.invalidate_up_current(force=True)
        except Exception as e:
            print(f"Error calling function '{input_value.__name__}': {e}")
            print_colored_traceback(*sys.exc_info())

    if draw_state.result is not None:
        draw_any(draw_state.result, name="Result", header_same_line=True, show_header=False, show_add_delete=False)

    # pop_style_var(3)

    return False, input_value


@render_func(is_default_for=(int), shadow=False, use_cache=True, min_width=60, wrap=False,
             is_tree=False, with_header=draw_header, align_header=True, temp=True)
def draw_int(input_value: int, draw_state=None, min_value=-1000.0, max_value=1000.0, speed=0.05, unique=0):
    imgui.set_next_item_width(draw_state.content_width)

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


@render_func(is_default_for=Enum, is_tree=False, shadow=False, selectable=False, header_same_line=True,
             parent_show_add_delete=False, with_header=draw_header, temp=True)
def draw_enum(input_value: Enum, style_manager=None, enum_tint=(0.3, 0.3, 0.3)):
    # Delegate to draw_tab_bar so enums get its wrapping and styling for free.
    # Enums are single-select: pass the current value as the lone selection and
    # render every member as a tab; names are the prettified member names.
    options = list(input_value.__class__)
    names = [opt.name.replace("_", " ").capitalize() for opt in options]
    changed, selected = draw_tab_bar([input_value], collection=options, names=names,
                                     unique="enum", as_toggles=False)
    if changed and selected:
        return True, selected[0]
    return False, input_value



@render_func(is_tree=False, show_bg=True, shadow=False, use_cache=True, z_offset=0, header_same_line=True,
             indent_size=0, show_add_delete=False, show_name=False, selectable=False, parent_show_add_delete=False, with_header=draw_header)
def draw_tab_bar(input_value: list, tab_height=20, names=None, tint_value=0.202, tint_saturation=0.372, unique=None,
                 collection=None, as_toggles=False, tints=None, draw_state=None):
    """Tab bar with multi-select via shift-click. input_value is the list of selected items, collection is all available tabs.
    Tabs wrap onto a new row when the cumulative width would exceed draw_state.content_width.

    tints: optional list of (r, g, b) tint colors, one per tab in `collection`. Entries that are
    None (or beyond the list) fall back to the neutral grey. (Defaults to None rather than [] to
    avoid the mutable-default-arg pitfall; behaves identically to an empty list.)"""
    if collection is None:
        return False, input_value

    io = imgui.get_io()
    changed = False
    selected = list(input_value)
    if selected is None:
        selected = []
    if names is None and hasattr(input_value, 'keys') and hasattr(input_value, 'values'):
        input_value = list(input_value.values())
        names = list(input_value.keys())

    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 6, imgui.get_cursor_screen_pos()[1]))

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 0))

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

        value = 0.3 if not tinted else 0.1

        # make_color_rgb mixes `color` toward the theme color by `factor`; factor=1.0 (button's
        # default) discards `color` entirely. Drop factor for tinted tabs so the tint shows, and
        # give inactive tinted tabs a faint fill (the default alpha=0.0 draws no rect at all).
        tab_factor = 0.50 if tinted else 1.0

        tab_width = imgui.calc_text_size(label.split("##")[0]).x + button_padding

        if active:
            selected_value = 0.204
            clicked = button(label, indent_size=0, z_offset=3, name=f"tab_{i}_{unique}",
                             height=tab_height, value=value + selected_value,
                             color=tab_color, factor=tab_factor, draw=True)[0]
        else:
            saturation = 1.0 if tinted else 0.3
            clicked = button(label, indent_size=0, height=tab_height, draw=True, z_offset=0.0,
                             alpha=0.0 if tinted else 0.0, value=value if not tinted else 0.1, saturation=saturation,
                             name=f"tab_{i}_{unique}_deactivated", color=tab_color, factor=tab_factor, text_value=1.0 if not tinted else 0.9,
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

    imgui.dummy(0,0)

    pop_style_var(1)

    if changed:
        Melty.refresh_nested_windows(draw_state)

    return changed, selected



@render_func(with_header=draw_header, is_tree=False, shadow=False)
def draw_enum_tabs(input_value: type, tab_state: TabState):
    enum_states = list(input_value)
    selected = tab_state.selected_tabs

    changed, new_selected = draw_tab_bar(selected, collection=enum_states)
    if changed:
        tab_state.selected_tabs = new_selected

    return False, input_value



def draw_debug(x,y, label, color=(1, 0, 0), size=16):
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

@render_func(use_cache=True, disable_scroll=True, show_header=False,
             header_same_line=False, show_tint=False, show_name=False, is_tree=False)
def draw_context_menu(input_value, draw_state, cursor_hover_inverted, func, unique=None, up_key_pressed=None,
                      down_key_pressed=None, tab_state: TabState = None, **kwargs):

    context_menu_offset = input_value.context_menu_offset
    info_items = ["name", "scroll_disabled", "_default_view_func", "column", "closable", "current_mode", "mode",
                  "show_add_delete", "_source", "window_pos", "left", "top", "width", "height", "content_height", "scroll_offset",
                  "final_max_column", "_column_cursor", "_content_rect", "_max_column_index", "_outside_column_height", "disable_scroll" ]

    # imgui.text(type(input_value._input_value).__name__)
    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 3, imgui.get_cursor_screen_pos()[1] - 20))
    if up_key_pressed:
        print("Up key pressed")

    fa_up_arrow = ""
    fa_down_arrow = ""
    if input_value._parent.id is not None:
        if button(fa_up_arrow, height=30)[0] or up_key_pressed:
            input_value.context_menu_offset += 1
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=5)
            Melty.cache.invalidate_up(input_value._tile_id, max_depth=5)

        imgui.same_line()
    if input_value.context_menu_offset > 0:
        if button(fa_down_arrow, height=30)[0] or down_key_pressed:
            input_value.context_menu_offset = max(0, input_value.context_menu_offset - 1)
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=5)
            Melty.cache.invalidate_up(input_value._tile_id, max_depth=5)

    else:
        imgui.dummy(30, 30)

    imgui.same_line()
    imgui.text_colored(f"{context_menu_offset}", 1, 1, 1, 0.3)
    imgui.same_line()

    offset_ds = input_value
    for i in range(context_menu_offset):
        if offset_ds._parent is None:
            break
        offset_ds = offset_ds._parent

    # The user walked up to an ancestor (offset > 0). That ancestor never had its
    # OWN context menu open, so the context_menu_open capture gate never ran for
    # it and its _call_site is None - caller lenses come up empty. Ask its next
    # inline render to capture the site (lazy, one-shot), and invalidate it so it
    # re-renders fresh rather than from cache (where the capture line is skipped).
    if (offset_ds is not input_value and not offset_ds._call_site_captured
            and not offset_ds._call_site_requested):
        offset_ds._call_site_requested = True
        if Melty.cache is not None:
            Melty.cache.invalidate_up(offset_ds._tile_id, max_depth=5)
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


    # Find which class's source to show in the class tab.
    # For a non-primitive value that's just the value's own class. For a
    # primitive field (e.g. a float `rank`) the field itself has no source,
    # so walk up the parent chain to the first object that does have source
    # code -- so e.g. Lora.rank still shows Lora's class, labelled as parent.
    raw_value = input_value._raw_input_value
    class_to_show = None
    class_is_parent = False
    if not isinstance(raw_value, (int, float, str, bool)):
        class_to_show = type(raw_value)
    else:
        max_walk = 4
        ancestor = input_value._parent
        while ancestor is not None and max_walk > 0:
            a_raw = getattr(ancestor, '_raw_input_value', UNSET_VALUE)
            a_type = type(a_raw) if a_raw is not UNSET_VALUE else None
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


    # Static tint colors for the fixed Config / Info tabs; other tabs use the neutral grey.
    config_tint = (0., 0.2, 0.5)  # steel blue
    info_tint = (1.0, 0.64, 0.113)    # teal

    tab_names = []
    tab_tints = []
    tab_names.append(info_icon_fa)
    tab_tints.append(info_tint)
    tab_names.append(config_icon_fa)
    tab_tints.append(config_tint)
    tab_names.append(func_tab)
    tab_tints.append(None)
    tab_names.append(tint_tab_name)
    tab_tints.append(Melty._saturated_rgb(draw_state.tint))  # Custom tint for the tint tab
    if class_to_show is not None:
        tab_names.append(class_tab)
        tab_tints.append(None)

    indices = list(range(len(tab_names)))

    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [indices[0]]

    current_mode = input_value._kwargs.get('mode', None)
    mode_tab = str(current_mode)
    if current_mode is not None:
        tab_names.append(mode_tab)

    # Tab list

    tab_changed, new_tabs = draw_tab_bar(tab_state.selected_tabs, names=tab_names, wrap=True, tab_height=30, tint_value=0.7,
                                         width=max(50, draw_state.content_width - 100),
                                         show_bg=True, name=f"tab_bar#{view_func_name}{unique}",
                                         z_offset=-2, bg_offset=-1, draw=True, 
                                         collection=indices, tints=tab_tints, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    for t_idx, static_tab in enumerate(tab_state.selected_tabs):
        from src.lsd.gl_gui.view.mode import Mode
        if static_tab < len(tab_names):
            if tab_names[static_tab] == tint_tab_name:
                changed, new_tint = draw_tint_context(input_value, name=f"Context Tint##{unique}", column=t_idx)
                if changed:
                    pass

            if tab_names[static_tab] == info_icon_fa:


                changed, watch = draw_text(draw_state.watch, width=draw_state.content_width,
                                          name="Watch##{unique}", column=t_idx, immediate_return=True, editable=True, show_bg=True)
                if changed:
                    draw_state.watch = watch
                if draw_state.watch in input_value._kwargs:
                    item_value = input_value._kwargs.get(draw_state.watch, 'Not found')
                elif draw_state.watch in input_value.__dict__:
                    item_value = getattr(input_value, draw_state.watch, 'Not found')
                else:
                    item_value = 'Not found'

                text(str(item_value), show_bg=False, tint=(0.1, 0.01, 0.4), wrap=False, name=f"{draw_state.watch}##it",
                     column=t_idx, editable=False)

                imgui.new_line()

                draw_str(str(len(input_value._view_children)),
                     show_bg=True, tint=(0.1, 0.01, 0.4), show_header=True, wrap=False, show_name=True, name=f"._view_children##{unique}",
                     column=t_idx, editable=False)
                draw_str(str(input_value.scroll_visible),
                     show_bg=True, tint=(0.1, 0.01, 0.4), show_header=True, wrap=False, show_name=True,
                     name=f"scroll_enabled##{unique}",
                     column=t_idx, editable=False)

                

                text(f"{input_value._view_func.__name__}", show_bg=True, show_name=True, show_header=True, wrap=True, name="Rendered by", column=t_idx,
                     editable=False, tint=(0.84, 0.68, 0.639))
                text(f"{type(input_value._raw_input_value).__name__}", name="input_value type", column=t_idx, editable=False)

                text(f"{input_value.window_index}", name="window_index", column=t_idx,
                     editable=False, tint=(0.8, 0.8, 0.2))

                text(f"{input_value._default_view_func}", name="default_view_func", column=t_idx,
                     editable=False)

                text(f"{input_value._kwargs.get('real_type', None)}", name="kwargs type", column=t_idx,
                     editable=False)

                text(f"{input_value._kwargs.get('type_collection', None)}", name="kwargs collection type", column=t_idx,
                     editable=False)

                for info_item in info_items:
                    if info_item in input_value._kwargs:
                        item_value = input_value._kwargs.get(info_item, 'Not found')
                    elif info_item in input_value.__dict__:
                        item_value = getattr(input_value, info_item, 'Not found')
                    else:
                        item_value = 'Not found'

                    if isinstance(item_value, (int, float, str, bool, Enum)):
                        text(f"{item_value}", name=info_item, show_name=True, show_header=True, column=t_idx, editable=False)
                    else:
                        draw_any(item_value, name=info_item, column=t_idx, show_name=True,
                                show_header=True, show_add_delete=False, draw=True)

                if button("print_stack_trace", column=t_idx)[0]:
                    print_stack_trace()

            if tab_names[static_tab] == config_icon_fa:
                view_func = input_value._view_func
                if view_func is None:
                    text("No view function", column=t_idx)
                else:
                    # Unwrap the @render_func wrapper to read the original signature.
                    raw_func = getattr(view_func, '__wrapped__', view_func)
                    sig = inspect.signature(raw_func)
                    ds_kwargs = input_value._kwargs or {}
                    # Auto-injected params the user doesn't configure.
                    skip_params = {"input_value", "draw_state", "args", "o_kwargs",
                                   "kwargs", "meta", "viewstate", "self"}
                    for param_name, param in sig.parameters.items():
                        if param_name in skip_params:
                            if param_name in ds_kwargs:
                                param_value = ds_kwargs[param_name]
                                text(f"{param.__class__.__name__}", name=param_name, column=t_idx,
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
                            text(f"{param_value}", name=param_name, column=t_idx,
                                 editable=False)
                        else:
                            draw_any(param_value, name=param_name, column=t_idx,
                                     show_name=True, show_header=True,
                                     show_add_delete=False, draw=True)
            if tab_names[static_tab] == func_tab:
                # Jump-to-caller: open the call site where this widget's render
                # func was invoked. Reads the (filename, lineno) cached at
                # menu-open (_call_site) - only available at offset 0.
                _site = getattr(input_value, '_call_site', None)
                if _site is not None:
                    _caller_file, _caller_line = _site
                    # Resolve the enclosing caller function name from the line --
                    # same approach the Convert tab uses (chain_converters).
                    from src.lsd.gl_gui.view.core_conversion.chain_converters import _enclosing_function
                    _caller_fn = _enclosing_function(_caller_file, _caller_line)
                    _caller_label = f"{Path(_caller_file).name}:{_caller_line}"
                    if _caller_fn is not None:
                        _caller_label = f"{_caller_fn.__name__}  ({_caller_label})"
                    imgui.text("Called from")
                    imgui.same_line()
                    if button(_caller_label,
                              height=30, value=0.4, saturation=1.5,
                              column=t_idx, name="jump_to_caller")[0]:
                        from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
                        threading.Thread(
                            target=open_in_intellij,
                            args=(str(_caller_file),),
                            kwargs={"line_number": _caller_line},
                            daemon=True).start()

                view_func = input_value._view_func
                # Draw view function
                if view_func is not None:
                    view_func_name = view_func.__name__ if hasattr(view_func, '__name__') else str(view_func)
                    change, new_view_func = draw_with_modes(view_func, column=t_idx, modes=(Mode.CODE_PLAIN_TEXT, Mode.CODE_UI), name=view_func_name)
                    if change:
                        print(f"Changing view function from {view_func.__name__} to {new_view_func.__name__}")
                        input_value._view_func = new_view_func
                        # input_value._kwargs['view_function'] = new_view_func
                else:
                    draw_str("No view function specified", name="View Function", column=t_idx, editable=False)

            if tab_names[static_tab] == class_tab:
                # Draw class source. For a primitive field this is the parent
                # object's class (e.g. Lora for a LoraID), so so label it
                # clearly so it's obvious the source is the owning type.
                if class_is_parent:
                    text(f"Parent type of {class_name}", name="Source", column=t_idx,
                         editable=False, tint=info_tint)
                cls_change, new_cls = draw_with_modes(class_to_show, column=t_idx,
                                                      modes=(Mode.CODE_PLAIN_TEXT, Mode.CODE_UI),
                                               name=class_to_show.__name__)

            if tab_names[static_tab] == mode_tab:
                if current_mode is not None:
                    #prettiafy the string using json indent

                    mode_change, new_mode = text(str(current_mode.value), column=t_idx, width=draw_state.content_width,
                                                            name=str(current_mode))
    imgui.dummy(0,30)

    return False, input_value


@render_func(use_cache=True)
def draw_drop_down_item(input_value, name="", unique=0, shadow=False, **kwargs):
    if button(name, name=f"{unique}{name}_dd_item", show_bg=True, height=25, shadow=False)[0]:
        return True, name

    return False, input_value

@render_func(use_cache=True, show_bg=True, shadow=False, selectable=False,
             is_tree=False, show_name=True, with_header=draw_header)
def draw_dropdown(input_value, collection, name, draw_state, drop_down_state: DropDownState, **kwargs):
    """Root of a recursive dropdown. Renders a trigger button showing the current
    selection; clicking it opens the (click-to-open) root popover. Nested dict
    rows inside the popover open their own sub-menus on hover. Returns
    (changed, selected_leaf) when the user picks a value."""
    arrow_icon = ""
    drop_down_display_str = f"{arrow_icon} {name}: {str(input_value)[:30]}"
    arrow_icon = ""
    text(arrow_icon)
    clicked, selected = button(drop_down_display_str, name=f"{name}_dd_trigger",
                               show_bg=True, width=draw_state.content_width - 10, height=25)
    if clicked:
        print(f"Dropdown trigger clicked for {name}, opening popover")


    # clicked, selected = draw_drop_down_item(input_value, name=f"{str(input_value)[:20]}")
    # if clicked:
    #     return True, selected

    from src.lsd.gl_gui.view.mode import Mode
    changed, new_item = draw_any(collection, tint=draw_state.tint,
                                 name=f"{draw_state.name}_nested",
                                 mode=Mode.DROPDOWN_WINDOW)
    if changed:
        # print(f"Dropdown changed: returning {new_item}")
        return True, new_item


    return False, input_value


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
def draw_single(input_value:any, view_func=None, mode:any=None, **kwargs):
    changed, return_val = view_func(input_value, mode=mode)
    return changed, return_val


@render_func(use_cache=False, show_header=True, selectable=False, with_header=draw_header)
def draw_blank(input_value: any, **kwargs):

    return False, None


def draw_any(input_value:any=None, view_func=None, mode:any=None, chain=None, **kwargs):
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
    #     view_func = Melty.get_default_view_function(real_type=real_type, collection_type=collection_type, attrib_key=key)
    #
    # if view_func is None:
    #     view_func = draw_collection

    if view_func is None:
        new_default = Melty.get_default_view_function(real_type=real_type, collection_type=collection_type,
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
        mode = Melty.mode_stack[-1] if len(Melty.mode_stack) > 0 else None

    if isinstance(mode, tuple) and len(mode) > 0:
        main_mode = mode[0]
    else:
        main_mode = mode

    # --- Search: forward the active search term to searchable child views ---
    # The term rides Melty.search_stack so it reaches the whole subtree. We
    # simply hand it to each searchable view via its "search_text` param and
    # let the view decide what to do with it (the text editor highlights
    # matches in place). No value conversion or filtering happens here.
    # if (len(Melty.search_stack) > 0
    #         and (getattr(kwargs_view_func, '_searchable', False) or kwargs.get("searchable", False))
    #         and "search_text" not in kwargs):
    #     kwargs["search_text"] = Melty.search_stack[-1]

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

