"""Collection view functions and supporting definitions."""
from collections import defaultdict
from collections import deque
from collections.abc import MutableMapping
from enum import Enum
from meltygui.core.conversion.bubbling import _BubblingDict
from meltygui.core.conversion.bubbling import _DeepPath
from meltygui.hdr_color import pack_color
from meltygui.core.melty import CollectionAction
from meltygui.core.melty import Melty
from meltygui.core.melty import SearchTerm
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.shaped import Shaped
from meltygui.state.new_core_model import ColorPickerState
from meltygui.state.new_core_model import TabState
from meltygui.core.runtime.toggles import Toggles
from meltygui.view.header_view import draw_header
from types import NoneType
import meltygui_imgui as imgui
import types


@render_func(use_cache=False, show_bg=False, indent_size=2, disable_scroll=True,
             shadow=False, selectable=False, bg_offset=1)
def draw_collection_as_tabs(input_value, tab_state: TabState = None, draw_state=None, unique=0,
                            excluded=None, included=None, show_excluded=False, show_system=False,
                            folder_type=None):
    """Draws a dict as a tab bar: each inner collection gets its own tab (key = tab
    name, contents via draw_any); all non-collection items are grouped into one
    final "General" tab.

    folder_type: a type or tuple of types that get their own tab, overriding
    the default "any collection" rule — e.g. folder_type=(dict, GeneralParse)
    puts dicts and GeneralParses in tabs while tuples/lists land in General.

    Item filtering matches draw_collection: `excluded` names are hidden,
    `included` names always show (overriding every hide rule), the type's
    __excluded_attrs__ hide unless Toggles.show_excluded, and _underscored_
    keys hide unless show_system. show_excluded=True disables all hiding."""
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.decoration_view import draw_bg
    from meltygui.view.tab_view import draw_tab_bar
    from meltygui.core.rendering.render_dispatch import draw_any
    import meltygui.core.input.drag_drop_core as _drag_drop

    if excluded is None:
        excluded = set()
    if included is None:
        included = set()
    excl_attrs = getattr(type(input_value), "__excluded_attrs__", None)

    def _key_visible(key):
        key_str = str(key).split("##")[0]
        if key_str in included:
            return True
        if show_excluded:
            return True
        if (excl_attrs is not None and not Toggles.show_excluded
                and key_str in excl_attrs):
            return False
        if key_str in excluded:
            return False
        if not show_system and (key_str.startswith("_") or key_str.endswith("_")):
            return False
        return True

    if folder_type is not None:
        tab_types = folder_type
    else:
        tab_types = (dict, defaultdict, MutableMapping, types.MappingProxyType, list, tuple, set, deque)
    visible = [(k, v) for k, v in input_value.items() if _key_visible(k)]
    tab_keys = [k for k, v in visible if isinstance(v, tab_types)]
    general = {k: v for k, v in visible if not isinstance(v, tab_types)}

    tabs = [str(k) for k in tab_keys]
    key_by_name = {str(k): k for k in tab_keys}
    if general:
        tabs.append("General")
    if not tabs:
        return False, input_value

    tab_state.selected_tabs = [t for t in tab_state.selected_tabs if t in tabs]
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [tabs[0]]

    # Tab tints come from each child's render kwargs (via return_extras below).
    # The bar draws before the children, so tints lag by one frame; they're held
    # on tab_state (serialized) so tabs retain their color across sessions.
    if getattr(tab_state, "tab_tints", None) is None:
        tab_state.tab_tints = {}
    tints = [tab_state.tab_tints.get(t) for t in tabs]
    # View icons ride the same one-frame-lag path as tints: each child's
    # resolved `icon` kwarg (the rendered icon, e.g. from # [icon=...] comments)
    # is held on tab_state and prefixed onto its tab label.
    if getattr(tab_state, "tab_icons", None) is None:
        tab_state.tab_icons = {}
    icons = [tab_state.tab_icons.get(t) for t in tabs]

    indent_size = 6
    imgui.dummy(0, 5)
    spacing = 8

    # Framework DragDrop: this draw_state IS the drop collection (its
    # input_value is the dict a Reorder/Insert applies to via
    # Melty.dnd_requests + the undo tail - undo included). The tab buttons
    # register as its whole-rect drag items; _dnd_horizontal makes the slot
    # lines vertical gaps between tabs. dnd_keys maps each tab to its
    # (dict key, dict index) - insert indices in the slots are then already
    # dict-space, with non-collection keys being skipped. The synthetic
    # General tab gets None: not draggable and contributes no slot.
    draw_state._dnd_drop_target = True
    draw_state._dnd_horizontal = True
    dict_keys = list(input_value.keys())
    dnd_keys = []
    for t in tabs:
        k = key_by_name.get(t) if not (t == "General" and t not in key_by_name) else None
        dnd_keys.append((k, dict_keys.index(k)) if k is not None else None)

    tab_changed, new_tabs, bar_ds = draw_tab_bar(input_value=tab_state.selected_tabs,
                                                 tab_height=30, show_bg=False, bg_offset=1,
                                                 name=f"tab_bar{unique}", wrap=True,
                                                 collection=tabs, tints=tints, icons=icons, as_toggles=False,
                                                 dnd_collection_ds=draw_state, dnd_keys=dnd_keys,
                                                 return_extras=True)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    # The bar's view is cached, but the dragged button must be baked through
    # the bar's body at the gesture edges (pickup: pick up the floating window
    # kwargs + bake the placeholder; drop: restore the inline button). Between
    # the edges DragDrop._keep_alive re-registers the floating window on its
    # layer, so no per-frame invalidation is needed.
    dnd_active_here = _drag_drop.DragDrop.active and _drag_drop.DragDrop.source_ds is draw_state
    if dnd_active_here != getattr(draw_state, "_tab_dnd_was_active", False):
        draw_state._tab_dnd_was_active = dnd_active_here
        if bar_ds is not None:
            request_render()

    imgui.dummy(0, 2)
    changed = False
    # Rendered content views in visual order, as (dict_index, draw_state) -
    # for the between-content drop slots published below.
    content_stack = []
    # Selected tabs stack vertically in tab-bar order (no columns).
    for tab in tabs:
        if tab not in tab_state.selected_tabs:
            continue
        content_dragged = False
        if tab == "General" and tab not in key_by_name:
            general_changed, new_general, child_ds = draw_any(general, name=f"Tab: General {unique}",
                                                              disable_scroll=False, indent_size=indent_size,
                                                              use_cache=True, return_extras=True)
            if general_changed:
                for k, v in new_general.items():
                    input_value[k] = v
            changed |= general_changed
        else:
            key = key_by_name[tab]
            # With several tabs open, each content view is ALSO a drag item of
            # this dict (key= and _collection_draw_state to make its header a
            # pickup handle from DragDrop.register_item) - dragging a stacked
            # collection reorders the tabs just like dragging a tab button.
            # The item_ds identity check disambiguates the two views sharing
            # (collection, key): only the one actually picked up floats.
            multi = len(tab_state.selected_tabs) > 1
            content_extra = {}
            content_dragged = False
            if multi:
                content_extra["key"] = key
                prev_ds = (getattr(draw_state, "_tab_child_ds", None) or {}).get(tab)
                if (prev_ds is not None and _drag_drop.DragDrop.item_ds is prev_ds
                        and _drag_drop.DragDrop.is_dragged_child(draw_state, key)):
                    content_dragged = True
                    content_extra.update(_drag_drop.DragDrop.dragged_item_kwargs())

            content_extra["use_cache"] = True
            tab_content_changed, value, child_ds = draw_any(input_value[key], name=f"{tab}",
                                                            disable_scroll=False, indent_size=indent_size,
                                                            return_extras=True,
                                                            **content_extra)
            if child_ds is not None:
                # Membership follows multi-select: cleared when only one tab
                # is open so the lone content view stops being a pickup handle.
                child_ds._collection_draw_state = draw_state if multi else None
                if not content_dragged:
                    content_stack.append((dict_keys.index(key), child_ds))
            if content_dragged:
                # The dragged content deferred to a floating window - hold its
                # vertical slot open at its pickup rect (placeholder draws its
                # own item spacing).
                _drag_drop.DragDrop.draw_placeholder(False, spacing,
                                                     style_manager=Core.melty.style_manager,
                                                     draw_bg=draw_bg)
            if tab_content_changed:
                input_value[key] = value
            changed |= tab_content_changed

        if not content_dragged:
            # (the placeholder path already added its own item spacing)
            imgui.dummy(0, spacing)

        if child_ds is not None:
            # Track each tab's content draw_state so deselected tabs can be
            # marked hidden below (they stop rendering but keep stale
            # geometry, which leaked DragDrop drop lines).
            if getattr(draw_state, "_tab_child_ds", None) is None:
                draw_state._tab_child_ds = {}
            draw_state._tab_child_ds[tab] = child_ds
            child_ds._hidden_offscreen = False

        child_tint = child_ds._kwargs.get("tint", None) if child_ds is not None else None
        if child_tint is not None and tab_state.tab_tints.get(tab) != child_tint:
            tab_state.tab_tints[tab] = child_tint
            draw_state.invalidate()

        child_icon = child_ds._kwargs.get("icon", None) if child_ds is not None else None
        if child_icon is not None and tab_state.tab_icons.get(tab) != child_icon:
            tab_state.tab_icons[tab] = child_icon
            draw_state.invalidate()

    # Deselected tabs' content: not collapsed, not closed, but no longer
    # rendered - so nothing downstream knows it's invisible. Stamp
    # _hidden_offscreen (the same flag end_frame uses for spawner-scrolled
    # nested tiles) so DragDrop's slot sweep skips their whole subtrees.
    hidden_reg = getattr(draw_state, "_tab_child_ds", None)
    if hidden_reg:
        for t, t_ds in hidden_reg.items():
            if t not in tab_state.selected_tabs or t not in tabs:
                t_ds._hidden_offscreen = True

    # Publish drop slots BETWEEN the stacked content views (the framework only
    # derives slots from _children, which here are the tab buttons): one
    # horizontal line above each content (insert before its dict key) and one
    # below the last (append after it). Rebuilt from live geometry every
    # render, so they track scroll/reflow; a dragged content is excluded (its
    # home slot is the cancel target). Insert indices are dict-space,
    # same as the bar slots.
    extra_slots = []
    for idx, c_ds in content_stack:
        top, left = c_ds.abs_top, c_ds.abs_left
        if top is None or left is None or not c_ds.width:
            continue
        extra_slots.append((idx, left, left + c_ds.width, top - 2, False))
    if extra_slots:
        last_idx, last_ds = content_stack[-1]
        if last_ds.abs_top is not None and last_ds.abs_left is not None and last_ds.width:
            extra_slots.append((last_idx + 1, last_ds.abs_left,
                                last_ds.abs_left + last_ds.width,
                                last_ds.abs_top + (last_ds.height or 0) + 3, False))
    draw_state._dnd_extra_slots = extra_slots

    return changed, input_value


# The parse dicts (code/libcst_conversion.py) are registered by name: the code
# stack loads with the first code edit, not with the collection view.
_PARSE_DICT_TYPES = ('GeneralParse', 'CallParse', 'ClassParse', 'EnumParse', 'FunctionParse')


@render_func(is_default_for=(dict, MutableMapping, defaultdict, tuple, list, *_PARSE_DICT_TYPES,
                             _BubblingDict, _DeepPath),
             use_cache=True, header_same_line=False, show_bg=True, show_instance_vars=False, align_header=False,
             manual_content_height=True, shadow=True, selectable=False, bg_offset=-0.8,
             wrap=False, with_header=draw_header, indent_size=3, searchable=True, child_kwargs=None)
def draw_collection(input_value, draw_state, depth, style_manager, meta, icon=None,
                    mode=None, keys=None, get_attr=None, set_attr=None, show_excluded=False,
                    child_kwargs=None, show_bg=False, show_search=False, align_header=False, wrap=False,
                    on_collapse=False, search_text="", return_item=False, close_triggers_delete=False,
                    on_expand=False, show_add_delete=False, show_add_types=None, item_spacing_y=3, show_system=False,
                    included=None, horizontal=False, show_indices=False, excluded=None, annotation=None,
                    drop_tail_height=None, immediate_dnd=False, **kwargs):
    """
    Universal collection renderer
    immediate_dnd=True is how fast_draw_collection hosts this body: rows are
    drag handles through DragDrop.on_drag / on_drop (the code editor tab bar's
    model) instead of the wrapper's register_item + dnd_requests path, which
    a row without a @render_func wrapper never reaches.
    show_add_types={"Display Name": TypeA, ...} draws a second + button in the
    header that instantiates the chosen type (rendered by draw_header; the
    value just rides the kwargs through). Several entries get a chevron
    dropdown to pick from; a single entry binds the + directly with no
    chevron. A bare list of types is accepted and keyed by __name__.
    """
    from meltygui.core.conversion.render_host import RenderHost
    from meltygui.editor.text_editor import _scroll_into_view
    from meltygui.core.windowing.glfw_utils import print_stack_trace
    from meltygui.view.decoration_view import draw_bg
    from meltygui.view.header_view import draw_header_end
    from meltygui.core.cache.tile_marks import snap_int
    from meltygui.model.collection_model import annotation_item_type
    from meltygui.model.collection_model import _collection_match_keys
    from meltygui.model.search_model import _fuzzy_key_match
    from meltygui.core.rendering.render_dispatch import draw_any
    from meltygui.core.rendering.render_dispatch import seperator
    import meltygui.core.input.drag_drop_core as _drag_drop

    
    

    if excluded is None:
        excluded = set()

    if included is None:
        included = set()

    if child_kwargs is None:
        child_kwargs = {}

    # A RenderHost rendered DIRECTLY (draw_collection(host) - the settings
    # column of draw_space_mouse, the modifies playground) is this view's
    # delegate, so pulse it: notify_on_change is the per-frame liveness
    # stamp that idle sweep (RenderHost.sweep) reads, so it re-registers a
    # swept host. Without the pulse an evictable code host was deregistered
    # after idle_frames, the wrapper never drew again, and an edit eve
    # dirtied the dict but the outbound chain (dict -> cst -> source -> save)
    # never ran - the code never updated (Lukas 09-04). Idle frames skip
    # the body, so a swept host revives on the next frame an edit re-runs it.
    if isinstance(input_value, RenderHost):
        input_value.notify_on_change(draw_state)

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
    # meltygui.search_walk) can count this collection's key matches without
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

    # Immediate-mode drag and drop (fast_draw_collection): same gate as
    # drag_drop_core's render-func path - only dict / list values reorder.
    dnd_rows = (immediate_dnd and get_attr is None
                and isinstance(draw_state._raw_input_value, (dict, list)))

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
                'wrap': wrap,
                'search_match': key_is_match,
                'search_current': key_is_current,
            }

            if "content_width" in draw_state._kwargs:
                item_kwargs['content_width'] = draw_state._kwargs['content_width'] - Core.melty.spacing[0] * 2

            # Folders: a dict child of a typed-add collection (show_add_types)
            # inherits the same type choices, so nested folders keep the
            # [+ <type> v] affordance all the way down.
            if show_add_types and isinstance(item, dict):
                item_kwargs['show_add_types'] = show_add_types

            # Per-field `# [tint=...]` comment overrides
            # (__overrides__['__<field>__'] from the parent) are fed by the
            # render_func wrapper from the collection= + key= passed above -
            # see the __overrides__ block in core_render.

            item_kwargs = item_kwargs | child_kwargs
            if dnd_rows and child_draw_state is not None and child_draw_state.header_height:
                # The row's header band is its drag handle (last render's
                # geometry; abs_* follows scroll live). idx keeps slot and
                # drop indices collection-space past hidden / skipped rows.
                row_left, row_top = child_draw_state.abs_left, child_draw_state.abs_top
                drag = _drag_drop.DragDrop.on_drag(
                    child_draw_state.get_header_rect(), key=key, value=item, draw_state=draw_state, index=idx,
                    box=(row_left, row_top, row_left + (child_draw_state.width or 0),
                         row_top + (child_draw_state.height or 0)))
                if drag:
                    # This row IS the drag: paint its ghost at the cursor and
                    # hold the inline slot open (DragDrop frames it as the
                    # home / cancel target). Its subtree does not render, so
                    # hide its stale drop slots from the sweep.
                    draw_drag_ghost(drag, key_str, style_manager)
                    _drag_drop.DragDrop.end_drag()
                    child_draw_state._hidden_offscreen = True
                    imgui.dummy(1, drag.h)
                    imgui.dummy(0, item_spacing_y)
                    continue
                child_draw_state._hidden_offscreen = False
            if is_dragged and not immediate_dnd:
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
                # Membership is what DragDrop.register_item keys the wrapper
                # path's header handle on; immediate rows registered above.
                returned_ds._collection_draw_state = None if immediate_dnd else draw_state
                returned_ds.relative_pos = relative_pos
                # Child window closed via its X (closable + closed) -> queue its key
                # for removal from the collection (applied after the loop).
                if returned_ds.closable and returned_ds.closed:
                    if close_triggers_delete:
                        to_delete.add(key)
                if key_is_current:
                    search_current_h = returned_ds.header_height
                if is_dragged and not immediate_dnd:
                    # The child deferred to a floating window and drew nothing
                    # inline — hold its slot open with a placeholder so the
                    # collection layout doesn't shift. Neither the horizontal
                    # branch nor any other math off returned_ds may run here:
                    # returned_ds.abs_* is the floating window glued to the
                    # mouse, so a mid-drag re-render would measure the flow
                    # back of the cursor. The placeholder anchors itself to the
                    # pickup slot (DragDrop.home_rect).
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

    # Static drop tail for drag-and-drop collections: a fixed strip of empty
    # space below the last row. Its height counts towards the collection's
    # measured height, which pushes the PARENT's "append to folder" slot down
    # - without it a folder ends right at its last child, so that slot and the
    # nested collection's own "append at end" slot (last child bottom + 3) land
    # nearly on top of each other. Also gives an empty collection a droppable
    # body. Always present (NOT drag-conditional, per Lukas) so layout never
    # shifts when a drag begins. Gated to dict/list here - exactly what
    # the DnD system treats as a drop target (see drag_drop._is_target_collection).
    # Height comes live from Toggles.Collection.drop_tail_height (so the Toggles
    # UI edits it globally); a per-call drop_tail_height= kwarg overrides it.
    _drop_tail = (drop_tail_height if drop_tail_height is not None
                  else Toggles.Collection.drop_tail_height)
    if (_drop_tail and not horizontal
            and isinstance(draw_state._raw_input_value, (dict, list))):
        tail_h = int(_drop_tail)
        # Single-line collections (the wrapper same-line's the body BESIDE the
        # header when not multi_line - e.g. a small dict) need the tail to
        # also span the header height, or a bare vertical dummy sits to the
        # right of the header and never grows the box past header height. Add
        # the header height so the tail reaches BELOW the header, giving an
        # empty collection a real droppable body. Multi-line collections
        # already stack the body under the header, so a bare tail is fine.
        if not draw_state.multi_line:
            tail_h += int(draw_state.header_height or 0)
        imgui.dummy(1, tail_h)

    if dnd_rows:
        # Close the immediate-mode body: publish the between-row drop slots
        # and apply a drop that landed here, as one undo step (the inverse
        # mutation rides the undo stack, as the wrapper tail records it).
        drop = _drag_drop.DragDrop.on_drop(horizontal=horizontal, draw_state=draw_state)
        if not draw_state._dnd_extra_slots:
            # No row registered (empty collection): one append slot in the tail.
            draw_state._dnd_extra_slots = [collection_append_slot(draw_state, len(input_value))]
        if drop is not None:
            position = drop.index if isinstance(input_value, list) else drop.key
            mutation = {"reorder": lambda: _drag_drop.Reorder(position, drop.insert_index),
                        "insert": lambda: _drag_drop.Insert(drop.key, drop.value, drop.insert_index),
                        "remove": lambda: _drag_drop.Remove(position)}[drop.kind]()
            dropped, new_collection, inverse = mutation.apply(input_value)
            if dropped:
                from meltygui.state.core_undo import UndoManager
                input_value = new_collection
                changed = True
                # Rows moved but no SIZE changed: every cached relative_pos
                # describes the pre-drop layout - force a full measure.
                draw_state.invalid_content_height = True
                if inverse is not None:
                    UndoManager.record(draw_state, inverse, mutation)
                draw_state.invalidate()
                invalidate_collection_rows(draw_state)

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
                          and not imgui.is_mouse_down(2) and not Melty.space_mouse_drag
                          and not premature_break):
        draw_state.content_height = snap_int(content_height)
        draw_state.invalid_content_height = False

    draw_state.premature_break = premature_break
    if return_item:
        if changed:
            return changed, item_to_return
        else:
            return False, input_value

    return changed, input_value


# ── fast_draw_collection: draw_collection without the @render_func wrapper ──
# (its defaults are draw_collection's own decoration kwargs: one definition)
# Wrapper-only features: a call carrying any of these (truthy) is handed to
# the draw_collection wrapper, which owns windows, converters, columns,
# fixed sizes, footers and the right-click menu. use_cache=True is a caller
# asking for a tile boundary at that level (draw_collection_as_tabs' tab
# contents); the levels below it are fast again.
FAST_COLLECTION_WRAPPER_KWARGS = (
    "closable", "as_window", "glfw_window", "melty_window", "parent_window", "convert_in", "convert_out",
    "convert", "with_wrapper", "with_footer", "column", "drives", "pending", "auto_apply", "background",
    "width", "height", "fill_height", "max_height", "selectable", "context_menu", "draw_state", "use_cache",
    "layer_unique", "_converter_mode", "bypass", "expanded_mode", "freeze_resize", "just_shadow")
# Depth the collection's box is lifted above its surroundings per shadow=True
# (the wrapper's internal_z_offset), as an add_shadow offset.
# [tint=(0.55, 0.85, 0.95)]
fast_collection_shadow_lift = 1.0
# Ghost of a dragged row: tint mixed toward black, and its label.
# [tint=(0.95, 0.75, 0.25)]
drag_ghost_fill = (0.32, 0.92)      # (tint brightness, alpha)
# [tint=(0.95, 0.75, 0.25)]
drag_ghost_text = (0.92, 0.92, 0.92, 1.0)


def draw_drag_ghost(drag, label, style_manager):
    """The dragged row of an immediate-mode collection, painted where
    DragDrop.on_drag parked it (overlay list, raw draw calls only - no layout)."""
    tint = style_manager.get_tint()
    brightness, alpha = drag_ghost_fill
    drag.draw_list.add_rect_filled(drag.x, drag.y, drag.x + drag.w, drag.y + drag.h,
                                   pack_color(tint[0] * brightness, tint[1] * brightness,
                                              tint[2] * brightness, alpha), rounding=5.0)
    drag.draw_list.add_text(drag.x + 22, drag.y + 3, pack_color(*drag_ghost_text), str(label).split("##")[0])


def invalidate_collection_rows(draw_state, max_depth=32):
    """Force every tile under a collection to repaint after its rows moved
    (drop, mutation undo). Rows are identified by key / list index, so each
    row's views now hold another row's value; their tiles are not children
    of a tile of ours (a fast collection owns none), so walk the rows."""
    if Melty.cache is None or max_depth <= 0:
        return
    for child_draw_state in list(draw_state._children.values()):
        if child_draw_state is None or child_draw_state is draw_state:
            continue
        if child_draw_state._tile_id is not None:
            Melty.cache.invalidate(child_draw_state._tile_id, force=True)
        for view_child in list(child_draw_state._view_children.values()):
            if view_child._tile_id is not None:
                Melty.cache.invalidate(view_child._tile_id, force=True)
        invalidate_collection_rows(child_draw_state, max_depth - 1)


def collection_append_slot(draw_state, insert_index):
    """One horizontal "append here" drop slot at the imgui cursor, spanning
    the collection's box - an empty or collapsed collection's only slot."""
    left = draw_state.abs_left
    return (insert_index, left, left + (draw_state.width or 0), imgui.get_cursor_screen_pos()[1], False)


# DrawState.replay_body_actions replays the records of children whose wrapper carries this
# (a tileless host under a cached tile), so the fast host is marked once its body is defined.
FAST_HOST_MARK = "fast_host"


def collection_view_for_draw_any():
    """The renderer draw_any hands a collection to: the fast host under a
    collection body when Toggles.Collection.fast_draw_collection is on, else
    the draw_collection wrapper. The OUTERMOST collection keeps the wrapper
    and its tile: a cache hit then skips the whole tree (a fast collection
    owns no tile, so without this boundary every level re-runs whenever the
    enclosing view's body does). Every collection below it is fast."""
    if not Toggles.Collection.fast_draw_collection or Melty.in_annotation_mode():
        return draw_collection
    enclosing = Melty.draw_state_stack[-1] if Melty.draw_state_stack else None
    if enclosing is None or enclosing._view_func is not draw_collection.__wrapped__:
        return draw_collection
    return fast_draw_collection


def fast_draw_collection(input_value=None, **kwargs):
    """draw_collection without the @render_func wrapper: what draw_any
    forwards every collection type to. Nested data pays the wrapper once per
    LEAF plus once for the outermost collection (the tree's tile boundary)
    instead of once per level.

    It hosts draw_collection's own body (one body, two hosts) and does by hand
    the part of the wrapper a collection uses: identity + DrawState
    (view_identity), the kwargs layers, width, tint, background, add_shadow,
    draw_header, expand / collapse, indent, the render stacks, measurement,
    BVH sync and undo. It owns no tile: like a use_cache=False view it paints
    into the enclosing tile and registers with mark_uncached, so
    draw_state.invalidate() reaches the tile that holds its paint.

    Drag and drop is immediate-mode (the body's immediate_dnd rows). Returns
    (changed, value), plus the draw_state with return_extras=True.
    Not here: the right-click inspector, Ctrl+F ownership (a forwarded search
    term still highlights), and everything in FAST_COLLECTION_WRAPPER_KWARGS."""
    from meltygui.core.cache.tile_marks import add_shadow
    from meltygui.core.cache.tile_marks import snap_int
    from meltygui.core.core_render import pop_id
    from meltygui.core.core_render import push_id
    from meltygui.core.rendering.fast_view import bind_fast_draw_state
    from meltygui.core.rendering.fast_view import close_fast_box
    from meltygui.core.rendering.fast_view import draw_fast_header
    from meltygui.core.rendering.fast_view import finish_fast_view
    from meltygui.core.rendering.fast_view import measure_fast_box
    from meltygui.core.rendering.fast_view import push_view_tint
    from meltygui.core.rendering.fast_view import resolve_fast_kwargs
    from meltygui.core.rendering.fast_view import skip_offscreen
    from meltygui.core.rendering.render_dispatch import compute_bg_color
    from meltygui.view.decoration_view import draw_bg

    # A direct call is a caller asking for the fast host: only a wrapper-only
    # feature sends it to the wrapper. Toggles.Collection.fast_draw_collection
    # and the outermost-tile rule apply to draw_any's routing (collection_view_for).
    if any(kwargs.get(wrapper_kwarg) for wrapper_kwarg in FAST_COLLECTION_WRAPPER_KWARGS):
        return draw_collection(input_value, **kwargs)

    body = draw_collection.__wrapped__
    return_extras = kwargs.pop("return_extras", False)
    input_value = kwargs.pop("input_value", input_value)
    if Melty.depth > Melty.max_depth:
        return (False, None, None) if return_extras else (False, None)

    collection = kwargs.get("collection", None)
    kwargs, mode_stacked = resolve_fast_kwargs(body, draw_collection.__header_defaults__, input_value, kwargs,
                                               skip_defaults=("immediate_dnd", "child_kwargs"))
    if kwargs.get("view_func") is not None and kwargs["view_func"] is not fast_draw_collection:
        # A value / comment that picks its own renderer is the wrapper's to route.
        if mode_stacked:
            Melty.mode_stack.pop()
        return draw_collection(input_value, return_extras=return_extras, **kwargs)
    kwargs.pop("view_func", None)

    # The host's own draw_state bookkeeping is not a user edit: silenced, a
    # DrawState write skips change tracking (the wrapper does the same, see
    # invalidation_decoration.new_setattr). Tracking is back on for the body.
    caller_silence = Melty.silence_invalidate
    Melty.silence_invalidate = True
    draw_state = bind_fast_draw_state(body, fast_draw_collection, input_value, kwargs)
    if skip_offscreen(draw_state, kwargs):
        Melty.silence_invalidate = caller_silence
        if mode_stacked:
            Melty.mode_stack.pop()
        return (False, input_value, draw_state) if return_extras else (False, input_value)
    unique, name, tile_id = draw_state.unique, draw_state.name, draw_state._tile_id
    style_manager = Melty.style_manager
    available_width, live_clip = measure_fast_box(draw_state, kwargs)

    # ── depth, z order and the shadow the box casts. add_shadow takes the
    # lift relative to the surface we sit on, so it runs BEFORE shadow_depth
    # is raised for our own content ──
    show_bg = kwargs.get("show_bg", False) or not draw_state.expanded
    total_z_offset = ((draw_state.z_offset or 0) + (kwargs.get("z_offset", 0) or 0)
                      + (fast_collection_shadow_lift if kwargs.get("shadow", False) else 0))
    has_box = draw_state.width > 5 and (draw_state.height or 0) > 5
    start_shadow_depth = Melty.shadow_depth
    if kwargs.get("shadow", False) and show_bg and has_box and Melty.inside_clip(draw_state=draw_state):
        add_shadow((draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height),
                   offset=1 + total_z_offset, corner_radius=draw_state.corner_radius)
    Melty.depth += 1
    Melty.unique_stack.append(unique)
    Melty.draw_state_stack.append(draw_state)
    Melty.collection_stack.append(collection)
    draw_state.depth = Melty.depth
    draw_state.layer = Melty.active_layer
    start_z_pos = Melty.z_pos
    Melty.z_pos = (Melty.paint_rank * Melty.max_depth) + Melty.depth
    draw_state.z_pos = Melty.z_pos
    Melty.shadow_depth = Melty.shadow_depth + total_z_offset + (1 if kwargs.get("shadow", False) else 0)
    draw_state.depth_and_layer = (Melty.shadow_depth, Melty.paint_rank)
    kwargs["depth"] = Melty.depth
    push_id(unique)

    changed, value = False, input_value
    previous_tint = None
    bg_pushed = False
    try:
        dynamic_style = kwargs.get("style", kwargs.get("tint"))
        previous_tint = push_view_tint(draw_state, input_value, kwargs)
        draw_state.bg_color = compute_bg_color(bg_offset=kwargs.get("bg_offset", None), nested_bg=True,
                                               max_bg_depth=kwargs.get("max_bg_depth", None),
                                               max_bg_value=kwargs.get("max_bg_value", None))

        # ── background ──
        draw_list = imgui.get_window_draw_list()
        if not Melty.channels_split:
            # First view into this draw list this frame (the wrapper's prologue).
            draw_list.channels_split(Melty.max_depth)
            Melty.channels_split = True
        passed_z_offset = kwargs.get("z_offset", 0) or 0
        if show_bg:
            bg_color = (0, 0, 0, 0)
            if Melty.channels_split:
                draw_list.channels_set_current(max(0, min(Melty.get_channel() + passed_z_offset - 2,
                                                          Melty.max_depth - 1)))
            if has_box:
                if Toggles.dynamic_styles:
                    Melty.add_background(dynamic_style)
                else:
                    bg_return = draw_bg(bypass=True, left=draw_state.abs_left, top=draw_state.abs_top,
                                        width=draw_state.width, height=draw_state.height,
                                        rounding=draw_state.corner_radius, bg_offset=kwargs.get("bg_offset", 0),
                                        outline=kwargs.get("bg_outline", True),
                                        max_bg_depth=kwargs.get("max_bg_depth", None),
                                        max_bg_value=kwargs.get("max_bg_value", None),
                                        depth=Melty.shadow_depth, selected=False, opacity=1.0,
                                        saturation=kwargs.get("saturation", 1.0), pressed=False,
                                        style_manager=style_manager,
                                        nested_bg=kwargs.get("bg_offset", 0) >= 0)
                    if bg_return is not None:
                        bg_color = bg_return[1]
            Melty.bg_color_stack.append(bg_color)
        if Melty.channels_split:
            draw_list.channels_set_current(max(0, min(Melty.get_channel() + passed_z_offset
                                                      + (draw_state.z_offset or 0), Melty.max_depth - 1)))

        # ── header ──
        outline_margin = 3 if kwargs.get("show_bg", False) else 0
        imgui.begin_group()
        header_changed, header_value = draw_fast_header(draw_state, kwargs, outline_margin)
        if header_changed:
            changed, value = True, header_value

        # ── body ──
        if kwargs.get("indent_size", 0) > 0:
            cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((cursor[0] + kwargs["indent_size"], cursor[1]))
        imgui.begin_group()
        if draw_state.expanded:
            left, top = snap_int(draw_state.left), snap_int(draw_state.top)
            Melty.push_clip((left, top, left + snap_int(draw_state.width),
                             top + snap_int(draw_state.height or 0)))
            if show_bg:
                Melty.bg_depth += 1
                Melty.bg_stack.append(style_manager.get_tint())
                imgui.dummy(outline_margin / 2, outline_margin / 2)
            Melty.bg_depth += kwargs.get("bg_offset", 0)
            bg_pushed = True
            start_cursor = imgui.get_cursor_screen_pos()
            body_kwargs = {k: v for k, v in kwargs.items() if k not in ("input_value", "immediate_dnd")}
            body_kwargs.setdefault("meta", None)
            # This host owns no tile: when the enclosing tile is a blit-cache
            # hit its body (the rows' drag handles, DragDrop.on_drag) does not
            # run, so the on_action calls are recorded here and the tile's
            # replay_body_actions re-issues them through fast_host below.
            draw_state._body_actions = (Melty.frame_count, [])
            Melty.silence_invalidate = False
            body_return = body(input_value, immediate_dnd=True, **body_kwargs)
            Melty.silence_invalidate = True
            draw_state.observed_content_height = int(imgui.get_cursor_screen_pos()[1] - start_cursor[1])
            if isinstance(body_return, tuple) and len(body_return) >= 2:
                if body_return[0]:
                    changed, value = True, body_return[1]
                elif not changed:
                    value = body_return[1]
        elif isinstance(input_value, (dict, list)):
            # Collapsed: the body does not run, so its row slots are stale -
            # keep one "append into this folder" slot under the header.
            draw_state._dnd_immediate = True
            draw_state._dnd_extra_slots = [collection_append_slot(draw_state, len(input_value))]
        if bg_pushed:
            Melty.bg_depth -= kwargs.get("bg_offset", 0)
            if show_bg:
                Melty.bg_depth -= 1
                Melty.bg_stack.pop()
            Melty.pop_clip()
            bg_pushed = False
        imgui.end_group()
        draw_state._content_rect = imgui.get_item_rect_size()
        if draw_state.expanded:
            draw_state.content_height = draw_state._content_rect[1]
        if close_fast_box(draw_state, kwargs):
            # The bg / shadow above were sized from the previous measure.
            draw_state.invalidate()
            from meltygui.core.windowing.glfw_utils import request_render
            request_render()
        if draw_state._parent is not None and draw_state._parent is not draw_state:
            draw_state._parent._melty_content_height += draw_state.height
        draw_state.pos_changed()
        draw_state.last_seen = Melty.frame_count
        draw_state.frame_count += 1
    finally:
        if bg_pushed:
            Melty.bg_depth -= kwargs.get("bg_offset", 0)
            if show_bg:
                Melty.bg_depth -= 1
                Melty.bg_stack.pop()
            Melty.pop_clip()
        if show_bg and len(Melty.bg_color_stack) > 0:
            Melty.bg_color_stack.pop()
        if previous_tint is not None:
            style_manager.set_imgui_tint(*previous_tint)
        pop_id()
        Melty.collection_stack.pop()
        Melty.draw_state_stack.pop()
        Melty.unique_stack.pop()
        Melty.depth -= 1
        Melty.z_pos = start_z_pos
        Melty.shadow_depth = start_shadow_depth
        if mode_stacked:
            Melty.mode_stack.pop()
        if Melty.depth == 0 and Melty.channels_split:
            # A root-level call owns the draw list's channels (the wrapper's
            # root epilogue): merge what the first view split.
            Melty.channels_split = False
            imgui.get_window_draw_list().channels_merge()

    changed, value = finish_fast_view(draw_state, input_value, changed, value,
                                      on_rows_moved=invalidate_collection_rows)
    Melty.silence_invalidate = caller_silence
    if return_extras:
        return changed, value, draw_state
    return changed, value


# draw_any forwards every collection type here. Filed by hand (no @render_func
# to do it through is_default_for); runs after draw_collection's decoration
# above, so these entries replace the wrapper's.
Melty.register_default_view(fast_draw_collection, (
    dict, MutableMapping, defaultdict, tuple, list, *_PARSE_DICT_TYPES, _BubblingDict, _DeepPath))


@render_func(is_default_for=(
'tint', 'help_yellow_tint', 'color', 'context_select_tint', "text_color", "gradient_color", "outline_color",
    # Any 3- or 4-tuple of numbers with a float in it is a colour (promoted
    # dtype: `(0, 0, 0, 0.1)` works, an all-int `(1, 2, 3)` doesn't). The
    # name entries above also catch plain tints like `tint=(0, 0, 0)`.
    Shaped(tuple, (3,), float), Shaped(tuple, (4,), float)),
             has_popup=True,
             indent_size=2, is_tree=False, align_header=True, header_same_line=True, wrap=True,
             show_name=True, selectable=False, max_width=100, min_width=33, use_cache=False, with_header=draw_header)
def draw_tuple(input_value: tuple | types.NoneType, name, unique, draw_state, outline=False,
               info=None):
    """The default colour-tuple value: ``draw_tuple_fast``'s chip and picker
    popover (one implementation for value rows, headers, tab bars and file
    rows), given a layout slot in the value row. ``None`` draws the hollow
    chip; a click stamps in an opaque black."""
    # Change the chip's size here.
    chip_size = 17

    is_color = (isinstance(input_value, tuple) and len(input_value) in (3, 4)
                and all(isinstance(channel, (float, int)) for channel in input_value))
    if input_value is not None and not is_color:
        # A tuple matched by NAME ('tint', 'color') that holds no colour: the
        # chip's click would overwrite it, so it gets no chip.
        return False, input_value
    if input_value is None and not draw_state.multi_line:
        # Single-line rows only: line placement belongs to the wrapper, and
        # forcing same_line on a multi_line frame drags the chip back onto
        # the header's line.
        imgui.same_line(spacing=4)
    left, top = imgui.get_cursor_screen_pos()
    changed, value = draw_tuple_fast(input_value, draw_state, view_id=f"swatch{unique}{name}",
                                     x=left, y=top, size=chip_size, outline=outline, info=info)
    # The fast chip claims no layout (and parks the cursor under itself while
    # its picker is open): the value row's slot is claimed here.
    imgui.set_cursor_screen_pos((left, top))
    imgui.dummy(chip_size, chip_size)
    return changed, value


def draw_tuple_fast(input_value, draw_state, view_id, x=None, y=None, size=17,
                    outline=False, info=None, priority_delta=4, setter=None,
                    swatch=None, view_owner=None):
    """draw_tuple's colour chip for immediate-mode bodies (the code editor's
    tab bar) — the fast_dock idea: no render_func / imgui widget per chip,
    the swatch goes straight to the draw list and the click is a plain
    `draw_state.on_action` sub on the chip's rect, so a bar of N tabs pays
    N rects instead of N wrapper calls. The picker is the same
    `draw_color_picker` POPOVER draw_tuple opens; several chips share one
    draw_state, so the open one is `draw_state._tint_edit_key == view_id`
    (beside Melty.popover_focused_ds, which names the draw_state). `x`/`y`
    default to the current cursor screen position; the chip claims no
    layout. Returns (changed, value) like draw_tuple. `setter(value)` is the
    write the caller makes with a changed value — given, every change is
    recorded on the undo stack (a SetterChange on the host's draw_state,
    keyed by view_id) and undo/redo re-apply it through the setter, since
    a chip has no wrapper of its own for Melty.undo_requests to land in.
    `swatch` (an rgb(a) tuple) is what the chip PAINTS instead of the value
    itself — a muted preview, e.g. the tint mixed toward its background —
    while the picker still opens on, and edits, the real value.
    `view_owner` is the VIEW draw_state whose paint the chip edits (a
    window header's tint chip passes its host — not the `owner` flag below,
    which is this chip owning the popover): given, the picker also carries
    the view's `bg_offset` / `z_offset` rows (`draw_view_offsets_fast`,
    written back through `view_owner.locate_<param>`) and, with
    `Toggles.dynamic_styles`, the Residuals tab."""
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.color_view import draw_color_picker
    from meltygui.view.color_view import _popover_anchor
    from meltygui.view.color_view import color_picker_height
    from meltygui.view.color_view import color_picker_top_offset
    from meltygui.view.color_view import color_picker_width
    import meltygui.core.windowing.window_api as glfw

    # [tint=(0.85, 0.75, 0.05)]
    corner_radius = 4.0
    # [tint=(0.85, 0.75, 0.05)]
    outline_color = (1.0, 1.0, 1.0, 0.55)
    # The checker under a translucent chip (draw_tuple's COLOR_PREVIEW_HALF).
    checker_dark, checker_light = (0.25, 0.25, 0.25), (0.6, 0.6, 0.6)

    changed = False
    if x is None or y is None:
        cx, cy = imgui.get_cursor_screen_pos()
        x = cx if x is None else x
        y = cy if y is None else y
    rect = (x, y, x + size, y + size)
    draw_list = imgui.get_window_draw_list()

    is_color = (isinstance(input_value, tuple) and len(input_value) in (3, 4)
                and all(isinstance(c, (float, int)) for c in input_value))
    if not is_color:
        # Missing tint: a hollow chip; a click stamps in an opaque black.
        draw_list.add_rect(x, y, x + size, y + size,
                           pack_color(1.0, 1.0, 1.0, 0.35),
                           rounding=corner_radius)
        if draw_state.on_action("left_mouse_down", view_id=view_id, rect=rect,
                                priority_delta=priority_delta) is not None:
            request_render()
            return True, (0.0, 0.0, 0.0, 1.0)
        return False, input_value

    shown = swatch if swatch is not None else input_value
    r, g, b = float(shown[0]), float(shown[1]), float(shown[2])
    alpha = float(shown[3]) if len(shown) == 4 else 1.0
    if alpha < 1.0:
        # Left half: colour over a checkerboard at its real alpha; right
        # half: the colour opaque - so transparency shows in the chip.
        half = x + size * 0.5
        draw_list.add_rect_filled(x, y, half, y + size,
                                  pack_color(*checker_dark, 1.0),
                                  rounding=corner_radius,
                                  flags=imgui.DRAW_ROUND_CORNERS_LEFT)
        cell = size * 0.5
        draw_list.add_rect_filled(x + cell * 0.5, y, half, y + cell * 0.5,
                                  pack_color(*checker_light, 1.0))
        draw_list.add_rect_filled(x, y + cell * 0.5, x + cell * 0.5, y + size,
                                  pack_color(*checker_light, 1.0))
        draw_list.add_rect_filled(x, y, half, y + size,
                                  pack_color(r, g, b, alpha),
                                  rounding=corner_radius,
                                  flags=imgui.DRAW_ROUND_CORNERS_LEFT)
        draw_list.add_rect_filled(half, y, x + size, y + size,
                                  pack_color(r, g, b, 1.0),
                                  rounding=corner_radius,
                                  flags=imgui.DRAW_ROUND_CORNERS_RIGHT)
    else:
        draw_list.add_rect_filled(x, y, x + size, y + size,
                                  pack_color(r, g, b, 1.0),
                                  rounding=corner_radius)
    if outline:
        draw_list.add_rect(x - 1.5, y - 1.5, x + size + 1.5, y + size + 1.5,
                           pack_color(*outline_color),
                           rounding=corner_radius)

    # `owner`: this chip last opened the popover. It stays the owner past an
    # outside click / escape / re-run (which clear the slot) until its next
    # run, where it draws the picker once more with closed=True and lets go.
    # The window itself does not wait for that run: a Mode.POPOVER window is
    # discarded by end_frame as soon as the slot no longer names it or an
    # ancestor (Melty.popover_orphaned) - so a chip whose host stopped
    # running (tab switched away, tile served from the blit cache) leaves its
    # picker hanging on screen.
    # draw_tuple's lifecycle: the popover slot names a draw_state whose
    # ancestor closure is protected from the click-away clear_focus. In
    # draw_tuple that is the chip's OWN draw_state; here every chip shares
    # the host's (the whole editor is its closure — nothing outside the
    # picker could ever close it), so the slot holds the PICKER WINDOW's
    # draw_state (its own closure = the picker) once it exists, and the
    # host only for the opening frame (covered by the popover grace).
    owner = getattr(draw_state, "_tint_edit_key", None) == view_id
    picker_name = f"color_picker{view_id}"
    # The picker hangs off the HEADER row, so its parent_window is the
    # enclosing WINDOW (what draw_tuple's shared ds has), never the host
    # itself: a collapsed host reads abs_closed, and end_frame discards
    # any nested window under an abs_closed parent - the picker opened on
    # a collapsed item's header closes on its first open.
    anchor = _popover_anchor(draw_state)
    picker_ds = None
    if owner:
        for nested in Melty.root_draw_states.get(anchor.id, ()):
            if getattr(nested, "name", None) == picker_name:
                picker_ds = nested
                break
    is_open = owner and Melty.popover_focused_ds is not None and (
        Melty.popover_focused_ds is draw_state
        or Melty.popover_focused_ds is picker_ds)
    if draw_state.on_action("left_mouse_down", view_id=view_id, rect=rect,
                            priority_delta=priority_delta) is not None:
        if is_open:
            Melty.popover_focused_ds = None
        else:
            Melty.popover_focused_ds = draw_state
            draw_state._tint_edit_key = view_id
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
            owner = True
        is_open = not is_open
        draw_state.invalidate()
        request_render()
    if not owner:
        return False, input_value

    # ---- the picker popover: drawn while owned, closed once not open ----
    from meltygui.core.cache.invalidation_tracker import Note
    _info = (info() if callable(info) else info) if is_open else None
    picker_h = color_picker_height(4, bool(_info), has_owner=view_owner is not None)
    # Anchor under the chip; flip up past the display bottom (draw_tuple).
    _disp_w, _disp_h = imgui.get_io().display_size

    def popover_top_offset(height):
        """Under the chip; flipped above it past the display bottom; pinned
        to the display top when neither side has the room (a short window),
        so the picker's header and first rows are never cut off."""
        below = color_picker_top_offset()
        if y + size + below + height <= _disp_h - 10:
            return below
        above = -(height + 38)
        display_margin = 4
        return max(above, display_margin - (y + size))

    _pop_y = popover_top_offset(picker_h)
    # Flip LEFT the same way: a chip at a row's right edge (the file
    # listing's tint picker) cannot open off the display's right side, so
    # anchor the picker off the chip's right edge instead of its left.
    _pop_x = 0
    picker_w = color_picker_width()
    residual_tab = (view_owner is not None and Toggles.dynamic_styles and picker_ds is not None
                    and any(isinstance(state, ColorPickerState) and state.tab == "residuals"
                            for state in picker_ds.misc.values()))
    if residual_tab:
        picker_w = max(picker_w, 520)
        picker_h = max(picker_h, 560)
        _pop_y = popover_top_offset(picker_h)
    if x + picker_w > _disp_w - 10:
        _pop_x = -(picker_w - size)
    imgui.set_cursor_screen_pos((x, y + size))
    color_changed, new_color, picker_ds = draw_color_picker(
        input_value, name=picker_name, closed=not is_open,
        window_pos=(_pop_x, _pop_y), info=_info, parent_window=anchor,
        owner=view_owner,
        width=picker_w, height=picker_h, mode=Modes.POPOVER,
        return_extras=True)

    imgui.same_line(spacing=0)
    if is_open and Melty.popover_focused_ds is draw_state:
        # Hand the slot from the host to the picker window now that it is
        # drawn (same frame, after the opening grace) - the draw_state the
        # wrapper hands back, never a lookup that can miss: while the slot
        # held the HOST, every click inside the host's window (the whole
        # editor for a side-bar chip) protected it, and the picker could not
        # be dismissed. From here the window lives on the slot alone -
        # Melty.popover_orphaned discards it at end_frame once the slot
        # moves away, whether or not this chip ever runs again.
        Melty.popover_focused_ds = picker_ds
    if not is_open:
        draw_state._tint_edit_key = None
        if picker_ds is not None:
            # The icon row's menu (draw_view_icon_fast) goes with the picker.
            picker_ds._popover_keeps_escape = False
        # The picker closed on the final frame: one DEEP cascade so every
        # nested tile under the host (depth-shifted bgs, headers, rows)
        # settles on it - the live edits below only reached the host's
        # direct children.
        Melty.cache.invalidate_up(draw_state._tile_id, force=True,
                                  max_depth=Toggles.Style.tint_edit_close_depth,
                                  note=Note(name="tint chip closed", reason=str(view_id),
                                            tint=(0.85, 0.75, 0.05)))
        request_render()
        return False, input_value
    if color_changed:
        previous = input_value
        from meltygui.core.styling.style import Style
        input_value = (Style(new_color, **previous.__getnewargs_ex__()[1])
                       if isinstance(previous, Style) and new_color is not None
                       else tuple(new_color) if new_color is not None else None)
        changed = True
        if setter is not None:
            from meltygui.state.core_undo import UndoManager
            UndoManager.record(draw_state, previous, input_value, setter=setter,
                               key=view_id, label=str(view_id))
        # A live edit: the host (the bg its tint paints) and its direct
        # children re-render - a shallow cascade, because the picker fires
        # this every frame of a drag. The host's OWN tile is what
        # `draw_state.invalidate()` covered; its children are what it
        # missed (the tint shows through them). Deeper tiles wait for the
        # closing cascade above.
        Melty.cache.invalidate_up(draw_state._tile_id, force=True,
                                  max_depth=Toggles.Style.tint_edit_live_depth,
                                  note=Note(name="tint chip edit", reason=str(view_id),
                                            tint=(0.85, 0.75, 0.05)))
        request_render()
    if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
        # A nested menu inside the picker (draw_view_icon_fast's icon row)
        # takes the Escape while open, whether the picker body ran before or
        # after this check: the flag covers the after, the frame stamp the
        # before. begin_frame's Escape clear honours the same flag.
        icon_menu_open = picker_ds is not None and (
            getattr(picker_ds, "_popover_keeps_escape", False)
            or getattr(picker_ds, "_popover_escape_frame", -1) == Melty.frame_count)
        if not icon_menu_open:
            Melty.popover_focused_ds = None
        request_render()
    # Keep the frames coming while a slider/square drag is live - the
    # picker is use_cache=False, so a frame is all it needs; the host's
    # tiles are triggered by the change branch above, never a frame.
    if Melty.imgui_any_item_active or imgui.is_mouse_down(0):
        request_render()
    return changed, input_value


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


fast_draw_collection.fast_host = True
