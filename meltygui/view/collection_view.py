"""Collection view functions and supporting definitions."""
from collections import defaultdict
from collections import deque
from collections.abc import MutableMapping
from enum import Enum
from meltygui.core.conversion.bubbling import _BubblingDict
from meltygui.core.conversion.bubbling import _DeepPath
from meltygui.code.libcst_conversion import CallParse
from meltygui.code.libcst_conversion import ClassParse
from meltygui.code.libcst_conversion import EnumParse
from meltygui.code.libcst_conversion import FunctionParse
from meltygui.code.libcst_conversion import GeneralParse
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


@render_func(is_default_for=(dict, MutableMapping, defaultdict, tuple, list, GeneralParse,
                             CallParse, ClassParse, EnumParse, FunctionParse, _BubblingDict, _DeepPath),
             use_cache=True, header_same_line=False, show_bg=True, show_instance_vars=False, align_header=False,
             manual_content_height=True, shadow=True, selectable=False, bg_offset=-0.8,
             wrap=False, with_header=draw_header, indent_size=3, searchable=True, child_kwargs=None)
def draw_collection(input_value, draw_state, depth, style_manager, meta, icon=None,
                    mode=None, keys=None, get_attr=None, set_attr=None, show_excluded=False,
                    child_kwargs=None, show_bg=False, show_search=False, align_header=False, wrap=False,
                    on_collapse=False, search_text="", return_item=False, close_triggers_delete=False,
                    on_expand=False, show_add_delete=False, show_add_types=None, item_spacing_y=3, show_system=False,
                    included=None, horizontal=False, show_indices=False, excluded=None, annotation=None,
                    drop_tail_height=None, **kwargs):
    """
    Universal collection renderer
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
    from meltygui.core.cache.tile_cache import snap_int
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
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.color_view import draw_color_picker
    from meltygui.view.control_view import button
    from meltygui.view.color_view import color_picker_height
    from meltygui.view.color_view import color_picker_top_offset
    from meltygui.view.color_view import color_picker_width
    import meltygui.core.windowing.window_api as glfw

    is_open = False
    changed = False

    if input_value is None:
        # Position BEFORE drawing, exactly like the color branch below - a
        # first frame that skips the same_line breaks the header row (every
        # remaining item wraps to a new line) and the early return under the
        # button never restores it. Single-line rows only: line placement
        # belongs in the wrapper (it same_lines after the header unless it
        # chose multi_line), and forcing same_line on a multi_line frame drags
        # the widget back onto the header's line.
        if not draw_state.multi_line:
            imgui.same_line(spacing=4)
        if button("", height=21, shadow=False, z_offset=0, corner_radius=4, tint=(0, 0, 0, 0.1
                                                                                   ), tint_value=0.14, use_cache=True,
                  show_bg=True, text_pad=7, name=f"add_tuple##{unique}",
                  show_button_bg=True)[0]:
            input_value = (0.0, 0.0, 0.0, 1.0)
            request_render()
            return True, input_value

    else:
        # if not draw_state.multi_line:
        #     imgui.same_line(spacing=4)

        is_color = input_value is not None and isinstance(input_value, tuple) and len(input_value) in (3, 4) and all(
            isinstance(c, (float, int)) for c in input_value)
        if is_color:
            # A swatch trigger that opens our own colour-picker popover (replacing
            # imgui's built-in popup). Same popover pattern as the dropdown: identity
            # in Melty.popover_focused_ds is the open state; click toggles it; the
            # picker window is anchored under the swatch and dismissed on outside
            # click / Esc. The picker itself is stateless and returns the new colour.
            from meltygui.core.rendering.mode import Mode
            is_open = Melty.popover_focused_ds is draw_state
            col = list(input_value)
            alpha = col[3] if len(col) == 4 else 1.0
            # ALPHA_PREVIEW_HALF makes the swatch split: one half shows the colour
            # composited over a checkerboard at its real alpha, the other fully
            # opaque - so a length-4 tuple's transparency is visible in the chip itself
            # (plain color_button forces opaque regardless of the alpha we pass).
            flags = imgui.COLOR_EDIT_NO_TOOLTIP | imgui.COLOR_EDIT_ALPHA_PREVIEW_HALF
            if imgui.color_button(f"##swatch{unique}{name}", col[0], col[1], col[2], alpha,
                                  flags=flags, width=17, height=17):
                Melty.popover_focused_ds = None if is_open else draw_state
                if not is_open:
                    Melty._popover_open_frame = Melty.frame_count  # grace the opening click
                request_render()
            if outline:
                # Tight ring around the CHIP (the item just drawn) - the
                # widget's draw_state box is the measured min/max_width
                # envelope, far wider than the swatch.
                _omn, _omx = imgui.get_item_rect_min(), imgui.get_item_rect_max()
                imgui.get_window_draw_list().add_rect(
                    _omn.x - 1.5, _omn.y - 1.5, _omx.x + 1.5, _omx.y + 1.5,
                    pack_color(1.0, 1.0, 1.0, 0.55), rounding=4.0)
            is_open = Melty.popover_focused_ds is draw_state  # reflect the toggle this frame

            # The picker window is closable -> fixed size (auto-resize is off for
            # closable windows), and its content is raw imgui (not child render_funcs)
            # so the framework can't measure it. Size the window to fit the SV square
            # (180) + the N channel drag-floats and the hex line, so nothing clips.
    # info: a general-purpose caption for the popover (e.g. the anywhere
    # swatch's "last known source"). A CALLABLE resolves only while the
    # popover is open for a lazy path, so closed swatches never pay for it.
    _info = None
    if is_open and info is not None:
        _info = info() if callable(info) else info
    picker_h = color_picker_height(4, bool(_info))
    # parent_window=draw_state anchors the popover under the swatch and makes
    # the tuple the picker's ancestor, so clear_focus (which protects the
    # clicked swatch window closure) leaves the popover open when you click
    # the tuple, and dismisses it when you click anywhere else.
    # Flip up when opening down would run past the display bottom: the
    # popover renders at the invoking cursor + window_pos (core_render's
    # nested-window anchor), and the cursor here sits just under the swatch -
    # so the up offset is the picker's own height plus the swatch row.
    _pop_y = color_picker_top_offset()
    _anchor_y = imgui.get_cursor_screen_pos()[1]
    _disp_h = imgui.get_io().display_size[1]
    if _anchor_y + _pop_y + picker_h > _disp_h - 10:
        _pop_y = -(picker_h + 38)
    color_changed, new_color = draw_color_picker(input_value, name=f"color_picker{unique}",
                                                 closed=not is_open, window_pos=(0, _pop_y), info=_info,
                                                 parent_window=draw_state, width=color_picker_width(), height=picker_h,
                                                 mode=Modes.POPOVER)
    if is_open:
        if color_changed:
            if new_color is not None:
                input_value = tuple(new_color)
            else:
                input_value = None
                request_render()
            changed = True
        # Dismiss on a click outside the swatch/popover, or on Esc.
        # if imgui.is_mouse_clicked(0):
        #     mx, my = imgui.get_mouse_pos()
        #     if not any(_is_in_subtree(d, draw_state) for d in Core.melty.bvh_query(mx, my)):
        #         Melty.popover_focused_ds = None
        #         request_render()
        if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
            Melty.popover_focused_ds = None
            request_render()
        # Keep re-rendering while a slider/square is being dragged so the live
        # imgui interaction (is_item_active) updates the frame.
        if Melty.imgui_any_item_active or imgui.is_mouse_down(0):
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
            request_render()

    # elif input_value is not None and len(input_value) > 0 and isinstance(input_value[0], (float, int)):
    #     str_value = ", ".join([str(v) for v in input_value])
    #     ch, input_str = imgui.input_text("##tuple", str_value)
    #     if ch:
    #         try:
    #             new_tuple = eval(f"({input_str},)")
    #             if isinstance(new_tuple, tuple):
    #                 input_value = new_tuple
    #                 changed = True
    #         except Exception:
    #             pass
    # else:
    #     changed, input_value = draw_collection(input_value=input_value)

    return changed, input_value


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
    _pop_y = color_picker_top_offset()
    _disp_w, _disp_h = imgui.get_io().display_size
    if y + size + _pop_y + picker_h > _disp_h - 10:
        _pop_y = -(picker_h + 38)
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
        _pop_y = color_picker_top_offset()
        if y + size + _pop_y + picker_h > _disp_h - 10:
            _pop_y = -(picker_h + 38)
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
