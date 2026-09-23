"""Hosting a view body WITHOUT the @render_func wrapper.

A "fast" view is a plain function draw_any dispatches to that runs an
existing view body (`draw_collection.__wrapped__`) and does by hand the small
part of the wrapper that body relies on: the kwargs layers, identity +
DrawState (view_identity), the box, the tint, draw_header, measurement, BVH
sync and undo. One body, two hosts: the @render_func decoration stays the
host for everything a fast host hands back (windows, converters, columns,
fixed sizes, the right-click inspector).

A fast view owns no tile. Like a use_cache=False view it paints into the
enclosing tile and registers with TileCache.mark_uncached, so
draw_state.invalidate() reaches the tile that holds its paint.

fast_draw_collection (view/collection_view.py) is the one host: it composes
these steps around its background, shadow, stacks and drag and drop. Value
rows (bool, str, int, float) render through their @render_func wrappers.
"""
import inspect

import meltygui_imgui as imgui

from meltygui.core.cache.tile_marks import snap_int
from meltygui.core.layout.cursor_core import same_line
from meltygui.core.melty import Melty
from meltygui.core.rendering.view_identity import get_draw_state
from meltygui.core.rendering.view_identity import link_parent
from meltygui.core.rendering.view_identity import place_in_parent_window
from meltygui.core.rendering.view_identity import view_tile_id
from meltygui.core.rendering.view_identity import view_unique
from meltygui.core.runtime.toggles import Toggles

# Wrapper-only features: a call carrying any of these (truthy) is handed to
# the body's @render_func wrapper. use_cache=True is a caller asking for a
# tile boundary at that level.
FAST_VIEW_WRAPPER_KWARGS = (
    "closable", "as_window", "glfw_window", "melty_window", "parent_window", "convert_in", "convert_out",
    "convert", "with_wrapper", "with_footer", "with_header_end", "column", "drives", "pending", "auto_apply",
    "background", "width", "height", "fill_height", "selectable", "context_menu", "draw_state", "use_cache",
    "layer_unique", "_converter_mode", "bypass", "expanded_mode", "freeze_resize", "just_shadow", "changed",
    "show_bg", "shadow", "horizontal", "initial", "window_pos", "draw_overlay")
# Names a body's signature may ask for that the host supplies itself.
_HOST_SUPPLIED = ("input_value", "draw_state", "style_manager", "unique", "name", "suffix", "depth")

# A body's signature facts keyed by its code object, so a hotswapped
# signature is re-read (the function object itself survives hotswap).
_signature_cache = {}


def stamp(draw_state, name, value):
    """draw_state.<name> = value, skipped when it already holds that value.
    DrawState writes go through the instrumented setter
    (invalidation_decoration.new_setattr) and a host re-stamps ~60 fields a
    view per frame that almost never change."""
    state = draw_state.__dict__
    if name in state:
        current = state[name]
        if current is value or (type(current) is type(value) and current == value):
            return
    setattr(draw_state, name, value)


def view_signature(body):
    """(keyword defaults, accepted names or None when the body takes **kwargs)."""
    cached = _signature_cache.get(body.__code__)
    if cached is None:
        parameters = inspect.signature(body).parameters
        defaults = {param_name: param.default for param_name, param in parameters.items()
                    if param.default is not inspect.Parameter.empty}
        takes_any = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())
        cached = (defaults, None if takes_any else frozenset(parameters))
        if len(_signature_cache) > 256:
            _signature_cache.clear()
        _signature_cache[body.__code__] = cached
    return cached


def wants_wrapper(kwargs, wrapper_kwargs=FAST_VIEW_WRAPPER_KWARGS):
    """True when this call needs the @render_func host."""
    if not Toggles.Collection.fast_draw_collection or Melty.in_annotation_mode():
        return True
    for wrapper_kwarg in wrapper_kwargs:
        if kwargs.get(wrapper_kwarg):
            return True
    return False


def resolve_fast_kwargs(body, decoration, input_value, kwargs, skip_defaults=()):
    """The wrapper's kwargs layers, lowest first: the body's signature
    defaults (the wrapper feeds those to the header too - show_add_delete=
    False keeps a header's add button off), the decoration, type defaults,
    attribute defaults, the caller, the mode, the value's own attrs and the
    `# [..]` comment overrides. Returns (kwargs, mode_stacked); the caller
    pops Melty.mode_stack when mode_stacked."""
    from meltygui.core.core_render import OBJ_ATTR_PARAMS
    real_type = kwargs.get("real_type", type(input_value))
    collection = kwargs.get("collection", None)
    key = kwargs.get("key", "")
    modes = kwargs.pop("mode", None)
    if modes is not None and not isinstance(modes, tuple):
        modes = (modes,)
    mode_stacked = False
    mode_kwargs = {}
    for mode in modes or ():
        if mode is None:
            continue
        mode_config = mode.get_config_for(input_value)
        if mode_config is not None and mode_config.kwargs is not None:
            mode_kwargs |= mode_config.kwargs
            mode_kwargs["current_mode"] = mode
            if mode_config.recursive:
                mode_kwargs["mode"] = mode
                if mode is modes[0]:
                    Melty.mode_stack.append(mode)
                    mode_stacked = True
    signature_defaults = {param_name: default for param_name, default in view_signature(body)[0].items()
                          if param_name not in skip_defaults and param_name not in _HOST_SUPPLIED}
    kwargs = (signature_defaults | decoration | Melty.default_kwargs_by_type[real_type]
              | Melty.default_kwargs_by_attrib_type[kwargs.get("type_collection", type(collection))][key]
              | kwargs | mode_kwargs)
    if input_value is not None and not isinstance(input_value, dict):
        for attr_param in OBJ_ATTR_PARAMS:
            if attr_param not in kwargs and getattr(input_value, attr_param, None) is not None:
                kwargs[attr_param] = getattr(input_value, attr_param)
    # The parent's override entry for this key, then the dict's own (the
    # wrapper's two __overrides__ sources).
    override_sources = []
    if isinstance(collection, dict) and isinstance(collection.get("__overrides__"), dict):
        override_sources.append(collection["__overrides__"].get(f"__{key}__"))
    if isinstance(input_value, dict):
        override_sources.append(input_value.get("__overrides__"))
    for overrides in override_sources:
        if isinstance(overrides, dict):
            for override_key, override_value in overrides.items():
                if not (isinstance(override_key, str) and override_key.startswith("__")):
                    kwargs[override_key] = override_value
    if len(Melty.search_stack) > 0:
        kwargs["search_text"] = Melty.search_stack[-1]
    return kwargs, mode_stacked


def bind_fast_draw_state(body, host, input_value, kwargs):
    """Identity + DrawState for one fast view call, and the stamps the rest
    of the framework reads off a draw_state. Hashed by the BODY's name, so a
    view keeps its saved draw_state whichever host draws it. Fills kwargs
    with what a header / body is injected with."""
    from meltygui.core.core_render import _restamp_kwargs
    collection = kwargs.get("collection", None)
    key = kwargs.get("key", "")
    name = kwargs.get("name", "")
    if name == "":
        if isinstance(input_value, (int, float, str, bool)):
            shown = str(input_value)
            shown = shown[:10] + "..." if len(shown) > 10 else shown
            for unsafe in (" ", "\n", "%", "/"):
                shown = shown.replace(unsafe, "_")
            name = shown + collection.__class__.__name__
        else:
            name = str(key) + collection.__class__.__name__
    unique, suffix = view_unique(name, body.__name__, key=key, unique_name=kwargs.get("unique_name", name),
                                 old_suffix=kwargs.get("suffix", None))
    draw_state = get_draw_state(unique)
    if draw_state._view_func is not None and draw_state._view_func is not body:
        draw_state.invalidate_up(max_depth=6)
    stamp(draw_state, "_view_func", body)
    stamp(draw_state, "_wrapper", host)
    stamp(draw_state, "_tile_id", view_tile_id(name, unique, draw_state))
    place_in_parent_window(draw_state, left=kwargs.get("left"), view_offset=kwargs.get("view_offset", True))
    link_parent(draw_state)
    if "bg_offset" not in kwargs and draw_state.__dict__.get("bg_offset") is not None:
        kwargs["bg_offset"] = draw_state.__dict__["bg_offset"]
    if kwargs.get("icon") is None and draw_state.__dict__.get("icon") is not None:
        kwargs["icon"] = draw_state.__dict__["icon"]
    kwargs.update(input_value=input_value, draw_state=draw_state, name=name, unique=unique, suffix=suffix,
                  style_manager=Melty.style_manager, func=body, render_func=host)
    _restamp_kwargs(draw_state, kwargs)
    stamp(draw_state, "unique", unique)
    stamp(draw_state, "name", name)
    stamp(draw_state, "closable", False)
    stamp(draw_state, "use_cache", False)
    stamp(draw_state, "persistent", kwargs.get("persistent", True))
    if kwargs.get("temp", False):
        stamp(draw_state, "dlt_count", 0)
    stamp(draw_state, "_collection", Melty.collection_stack[-1] if len(Melty.collection_stack) > 0 else None)
    stamp(draw_state, "_raw_input_value", input_value)
    stamp(draw_state, "_bg_stack", list(Melty.bg_stack))
    stamp(draw_state, "_bg_depth", Melty.bg_depth)
    if kwargs.get("expanded") is not None:
        stamp(draw_state, "expanded", kwargs["expanded"])
        kwargs["is_tree"] = False
    if draw_state.expanded != draw_state._last_expanded:
        draw_state.invalid_content_height = True
        if draw_state._parent is not None:
            draw_state._parent.invalid_content_height = True
        stamp(draw_state, "_last_expanded", draw_state.expanded)
    return draw_state


# A fast view this far outside the live clip reserves its box instead of
# drawing (px of slack so a row is live before it scrolls into view).
offscreen_margin = 60


def skip_offscreen(draw_state, kwargs):
    """True when this view was drawn as an empty box because it sits entirely
    outside the live clip. A @render_func row that the collection body could
    not row-skip (invalid_content_height frames: expand / collapse, a height
    change, search) is still a cheap tile blit; a fast row owns no tile, so
    without this every invisible row pays its full header and body on exactly
    those frames. Uses the last measure, so it needs one: never on a view's
    first frames, while it waits for a remeasure, or on a search frame that
    must reach every row."""
    height = draw_state.height
    if (not height or draw_state.frame_count <= 2 or Melty.frame_count <= 2
            or draw_state.invalid_content_height):
        return False
    search_term = kwargs.get("search_text")
    if getattr(search_term, "scroll_to", False):
        return False
    clip = Melty.get_clip_rect()
    if clip is None:
        return False
    top = draw_state.abs_top
    if top is None or not (top + height < clip[1] - offscreen_margin or top > clip[3] + offscreen_margin):
        return False
    imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
    imgui.dummy(1, height)
    imgui.pop_style_var(1)
    draw_state.last_seen = Melty.frame_count
    return True


def measure_fast_box(draw_state, kwargs):
    """left / top from the cursor, width from the enclosing wrap frame (the
    height stays the last measure), then the single-line / multi-line split
    and content_width. Returns (available_width, live_clip)."""
    content_margin = len(Melty.bg_stack) * 2.0
    stamp(draw_state, "left", draw_state.abs_left)
    stamp(draw_state, "top", draw_state.abs_top)
    live_clip = Melty.get_clip_rect()
    if len(Melty.fixed_size_stack) > 0:
        wrap_frame = Melty.fixed_size_stack[-1]
        wrap_left, wrap_width = wrap_frame.abs_left, wrap_frame.width
        if live_clip is not None and wrap_width is not None and live_clip[2] < wrap_left + wrap_width:
            wrap_width = max(0, live_clip[2] - wrap_left)
    elif live_clip is not None:
        wrap_left, wrap_width = live_clip[0], live_clip[2] - live_clip[0]
    else:
        wrap_left, wrap_width = 0, imgui.get_io().display_size[0]
    available_width = wrap_width - (draw_state.abs_left - wrap_left) - content_margin
    stamp(draw_state, "width", snap_int(available_width))
    stamp(draw_state, "auto_resize", True)
    stamp(draw_state, "corner_radius", kwargs.get("corner_radius", 5.0))
    single_line_width = available_width - (draw_state.header_width + draw_state.header_end_width) - 10
    if "content_width" in kwargs:
        stamp(draw_state, "multi_line", True)
    elif ((single_line_width < (kwargs.get("min_width", draw_state.min_width) or 30)
           or (draw_state.height or 0) - draw_state.footer_height > 50)
          and not kwargs.get("header_same_line", False)):
        stamp(draw_state, "multi_line", True)
        stamp(draw_state, "content_width", available_width)
    else:
        stamp(draw_state, "multi_line", False)
        stamp(draw_state, "content_width", single_line_width)
    if draw_state.expanded:
        stamp(draw_state, "min_width", kwargs.get("min_width", draw_state.min_width))
        stamp(draw_state, "min_height", kwargs.get("min_height", draw_state.min_height))
        if draw_state.min_width is not None:
            stamp(draw_state, "width", max(draw_state.width, draw_state.min_width))
            stamp(draw_state, "content_width", max(draw_state.content_width, draw_state.min_width))
        if draw_state.height is not None and draw_state.min_height is not None:
            stamp(draw_state, "height", max(draw_state.height, draw_state.min_height))
    stamp(draw_state, "footer_height", 0)
    stamp(draw_state, "footer_width", 0)
    stamp(draw_state, "header_end_width", 0)
    if live_clip is not None:
        stamp(draw_state, "clip_rect", live_clip)
        window = draw_state.parent_window
        draw_state._clip_win_anchor = ((window._abs_left(), window._abs_top())
                                       if window is not None and window is not draw_state else None)
    has_box = draw_state.width > 5 and (draw_state.height or 0) > 5
    stamp(draw_state, "_bounding_hovered", draw_state.is_bounding_hovered())
    if has_box:
        # What hover_eligible gates every on_action of this view on.
        draw_state.fully_clipped = Melty.fully_inside_clip(
            rect=(draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height))
        draw_state.inside_clip = Melty.inside_clip(
            rect=(draw_state.abs_left, draw_state.abs_top + draw_state.header_height,
                  draw_state.width, draw_state.height))
    return available_width, live_clip


def push_view_tint(draw_state, input_value, kwargs):
    """Apply the view's tint to the style manager: a parsed class's @defaults
    tint, else the resolved tint kwarg, the collection's __tint__ entry or
    the draw_state's. Returns the tint to restore afterwards (None = nothing
    pushed)."""
    if Toggles.dynamic_styles:
        return None
    style_manager = Melty.style_manager
    collection = kwargs.get("collection", None)
    new_tint = kwargs.get("tint")
    if not (isinstance(new_tint, (tuple, list)) and len(new_tint) >= 3):
        new_tint = None
        if isinstance(input_value, dict) and isinstance(input_value.get("decorators"), dict):
            for decorator_value in input_value["decorators"].values():
                if (isinstance(decorator_value, dict)
                        and not (decorator_value.get("attr") or decorator_value.get("attrib"))
                        and isinstance(decorator_value.get("tint"), (tuple, list))
                        and len(decorator_value["tint"]) >= 3):
                    new_tint = decorator_value["tint"]
    if new_tint is None and getattr(collection, "__tint__", None) and draw_state.name in collection.__tint__:
        new_tint = collection.__tint__[draw_state.name]
    if (new_tint is None and draw_state.tint is not None and kwargs.get("show_bg", False)
            and kwargs.get("show_tint", False)):
        new_tint = draw_state.tint
    if new_tint is None:
        return None
    previous_tint = style_manager.get_tint()
    style_manager.set_imgui_tint(*new_tint[:4])
    kwargs["tint"] = style_manager.get_tint()
    return previous_tint


def draw_fast_header(draw_state, kwargs, outline_margin=0):
    """Run with_header(**kwargs) in its own group and record the header's
    box on the draw_state. Returns (changed, value) from the header (an
    in-header add / tint edit), or (False, None)."""
    header = kwargs.get("with_header", None)
    if header is None or not kwargs.get("show_header", True):
        stamp(draw_state, "header_left", draw_state.left)
        stamp(draw_state, "header_top", draw_state.top)
        stamp(draw_state, "header_width", 0)
        stamp(draw_state, "header_height", 0)
        imgui.begin_group()
        imgui.end_group()
        return False, None
    changed, value = False, None
    imgui.begin_group()
    header_start = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((header_start[0] + outline_margin + float(kwargs.get("header_indent", 0.0) or 0.0),
                                 header_start[1] + outline_margin))
    header_return = header(**kwargs)
    if isinstance(header_return, tuple) and len(header_return) >= 2 and header_return[0]:
        changed, value = True, header_return[1]
    imgui.set_cursor_screen_pos(header_start)
    imgui.end_group()
    if imgui.is_item_active() or imgui.is_item_activated():
        Melty.report_imgui_active()
    header_size = imgui.get_item_rect_size()
    stamp(draw_state, "header_left", header_start[0])
    stamp(draw_state, "header_top", header_start[1])
    stamp(draw_state, "header_width", header_size[0])
    stamp(draw_state, "header_height", header_size[1])
    if draw_state.parent_window is not None and not draw_state.multi_line:
        draw_state.parent_window.max_header_width = min(
            Toggles.Collection.max_preferred_header_width,
            max(draw_state.parent_window.max_header_width, draw_state.header_natural_width))
    if not draw_state.multi_line:
        same_line(spacing=0.0)
    return changed, value


def close_fast_box(draw_state, kwargs):
    """End the view's outer group with no trailing item spacing (the
    wrapper's final end_group) and commit the measured height."""
    imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
    imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
    imgui.end_group()
    imgui.pop_style_var(2)
    box = imgui.get_item_rect_size()
    if imgui.is_item_active() or imgui.is_item_activated():
        Melty.report_imgui_active()
    previous_height = draw_state.height
    height = min(snap_int(box[1]), kwargs.get("max_height", None) or 70000)
    if kwargs.get("min_height", None) is not None:
        height = max(height, kwargs["min_height"])
    stamp(draw_state, "height", height)
    if height != previous_height:
        draw_state.invalid_content_height = True
        if draw_state._parent is not None:
            draw_state._parent.invalid_content_height = True
        return True
    return False


def finish_fast_view(draw_state, input_value, changed, value, on_rows_moved=None):
    """The wrapper tail: cache registration and undo / redo. A request
    registered for this draw_state replaces its output (a drop's inverse
    mutation applies to the live collection)."""
    from meltygui.state.core_undo import handle_undo
    kwargs = draw_state._kwargs
    if Melty.cache is not None:
        Melty.cache.mark_uncached(draw_state.name, input_value, kwargs.get("collection", None),
                                  draw_state._tile_id, draw_state)
    if draw_state in Melty.undo_requests:
        requested, target_ui = Melty.undo_requests.pop(draw_state)
        if getattr(requested, "__collection_mutation__", False):
            try:
                _, value, _ = requested.apply(value)
                draw_state.invalid_content_height = True
                if on_rows_moved is not None:
                    on_rows_moved(draw_state)
            except Exception as mutation_error:
                print(f"undo mutation apply failed: {mutation_error}")
        else:
            value = requested
        changed = True
        draw_state.apply_undo_state(target_ui)
        draw_state.invalidate()
    else:
        handle_undo(changed, input_value, value, draw_state)
    return changed, value
