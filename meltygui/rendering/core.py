import inspect
import math
import sys
import time
import types
import zlib
from collections import defaultdict
from copy import copy
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any

import glfw
import imgui
from imgui.core import _DrawList

from imgui.core import _IO

from src.lsd.gl_gui.background import Background, Pending
from src.lsd.gl_gui.collision import Collisions
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import Comment
from src.lsd.gl_gui.view.core_conversion.path_finder import convert, explain_chain, NO_VALUE, PendingState, invert_path
from src.lsd.gl_gui.view.core_views.core_render_helpers import draw_vertical_scrollbar, floating_text
from src.lsd.gl_gui.model.core_model.draw_state import DrawState, Hotkey, DragMode, Anchor, TileMode, AttrDict, \
    UNSET_VALUE, ApplyMode
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, \
    push_style_var, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace, trace_group, get_live_frames
from src.lsd.gl_gui.melty import Melty, apply_collection_action, MeltyState, DepthState, \
    delete_from_collection, ManagedWindow
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.core_meta import Meta

melty_state_registry = {}
static_melty = MeltyState()

id_stack = []
stack_holder = {}

channels_split_stack = False
child_stack_holder = {}


def path_to_string(path_list):
    str_list = []
    for item in path_list:
        str_list.append(str(item.__name__ if hasattr(item, "__name__") else str(item)))
    return " -> ".join(str_list)


def render_wrapper(*o_args, **o_kwargs):
    first_arg = o_args[0] if o_args else None
    if not callable(first_arg):
        def class_wrapper(the_func):
            return render_wrapper(the_func, *o_args, **o_kwargs)

        return class_wrapper

    r_func = o_args[0] if o_args else None

    @wraps(r_func)
    def wrapper(*args, **kwargs):

        first_arg = args[0] if args else None
        if not callable(first_arg):
            def class_wrapper(the_func):
                return wrapper(the_func, *args, **kwargs)

            return class_wrapper

        if 'inner_func' in kwargs:
            render_func = kwargs.get('inner_func', None)
        else:
            render_func = r_func

        func = first_arg if callable(first_arg) else None
        # Use the specified wrapper if r_func
        wrap_sig = inspect.signature(render_func)
        sig = inspect.signature(func)
        params = sig.parameters
        wrap_params = wrap_sig.parameters

        param_types = [params[p].annotation for p in params]
        wrap_param_types = [wrap_params[p].annotation for p in wrap_params]
        name_to_param_type = {}
        for idx, param_name in enumerate(params):
            name_to_param_type[param_name] = param_types[idx]

        wrap_name_to_param_type = {}
        for idx, param_name in enumerate(wrap_params):
            wrap_name_to_param_type[param_name] = wrap_param_types[idx]

        param_defaults = {p: params[p].default for p in params if params[p].default is not inspect.Parameter.empty}
        wrap_defaults = {p: wrap_params[p].default for p in wrap_params if
                         wrap_params[p].default is not inspect.Parameter.empty}

        params = params | wrap_params
        param_types = param_types + wrap_param_types
        name_to_param_type = name_to_param_type | wrap_name_to_param_type
        param_defaults = param_defaults | wrap_defaults

        wanted_params = list(params.keys())
        wanted_params.remove("args") if "args" in wanted_params else None
        wanted_params.remove("o_kwargs") if "o_kwargs" in wanted_params else None

        def add_default(register_type):
            kwargs.pop('is_default_for', None)
            new_meta = Meta()
            new_meta.view_function = wrapper(*args, **kwargs)
            Melty.type_defaults[register_type] = new_meta

            if not isinstance((register_type), str):
                Melty.type_to_default_view_func[register_type].add(func)

        is_default_for = kwargs.get('is_default_for', None)
        if isinstance(is_default_for, (tuple, list)):
            for a_type in is_default_for:
                add_default(a_type)
        elif isinstance(is_default_for, type):
            add_default(is_default_for)
        elif isinstance(is_default_for, str):
            add_default(is_default_for)
        try:
            wrap_func = None
            if 'wraps' in o_kwargs:
                wrap_func = o_kwargs.get('wraps', None)
                func = wrap_func(func, header_defaults=kwargs, param_defaults=param_defaults, **kwargs)

            out_func = r_func(func, am_a_header=kwargs.get('am_a_header', False),
                              param_types=param_types, wanted_params=wanted_params,
                              wanted_params_inner=wrap_defaults, header_defaults=kwargs,
                              param_defaults=param_defaults, name_to_param_type=name_to_param_type,
                              **o_kwargs)

            if wrap_func is not None:
                o_kwargs.update(kwargs)
                out_func = wrap_func(out_func, am_a_header=True, inner_func=r_func, **kwargs)
            return out_func
        except Exception as e:
            print_colored_traceback(*sys.exc_info())
            return False, None

    return wrapper



@render_wrapper
def render_func(*args, **o_kwargs):
    func = args[0] if args else None
    param_types = o_kwargs.get("param_types", None)
    wanted_params = o_kwargs.get("wanted_params", None)
    header_defaults = o_kwargs.get("header_defaults", None)
    param_defaults = o_kwargs.get("param_defaults", None)
    name_to_param_type = o_kwargs.get("name_to_param_type", None)

    """
    Decorator for render functions.
    - Computes stable UI ID (unique) from callstack+meta.
    - Provides a per-widget viewstate object (with .unique).
    - Injects meta/viewstate only if the function signature wants them.
    - Pushes/pops ImGui ID scope automatically.
    """

    @wraps(func)
    def wrapper(input_value=None, **kwargs):
        if Melty.annotation_mode:
            args = [list(kwargs)[0]]
            annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
            if annotation is not None:
                return annotation

        modes = kwargs.get("mode", None)
        if not isinstance(modes, tuple):
            modes = (modes,) if modes is not None else None

        mode_stacked = False
        if modes is not None:
            mode_config = modes[0].value.get(type(input_value), None)
            if mode_config is not None and mode_config.recursive:
                Melty.mode_stack.append(modes)
                mode_stacked = True

            for mode in modes:
                if mode is not None:
                    mode_config = mode.get_config_for(input_value)
                    if mode_config is not None:
                        override_kwargs = mode_config.kwargs
                        kwargs = kwargs | override_kwargs
                        if not mode_config.recursive:
                            kwargs.pop('mode', None)
                        else:
                            kwargs['mode'] = mode

        as_window = kwargs.get("as_window", False)
        if as_window:
            kwargs['show_bg'] = True
            kwargs['z_offset'] = 0
            kwargs['selectable'] = False
            kwargs['use_cache'] = True
            kwargs['melty_window'] = True
            kwargs['closable'] = True
            kwargs['auto_resize'] = False
            kwargs['draggable'] = True
            kwargs['show_tint'] = True
            # kwargs['return_extras'] = True
            kwargs['show_header'] = True
            kwargs['z_offset'] = -3
            kwargs['disable_scroll'] = False

            from src.lsd.gl_gui.view.core_views.new_core_view import draw_header_end, draw_header
            kwargs['with_header_end'] = draw_header_end
            kwargs['with_header'] = draw_header

        passed_width = kwargs.get('width', None)
        passed_height = kwargs.get('height', None)

        Melty.silence_invalidate = True
        start_time = time.time()
        if kwargs.get("bypass", False):
            kwargs.pop("bypass", None)
            return func(**kwargs)
        return_extras = kwargs.get('return_extras', False)
        name = kwargs.get("name", "")
        active_layer = kwargs.get("active_layer", None)

        return_value = None
        is_root = Melty.depth == 0
        # first_arg = args[0] if args else None
        input_value = kwargs.get("input_value", input_value)

        content_margin = ((len(Melty.bg_stack)) * 2.0)

        kwargs = o_kwargs | kwargs

        if header_defaults is not None:
            kwargs = header_defaults | kwargs

        if not Melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_split(Melty.max_depth)
            Melty.channels_split = True

        if name == "" and is_root:
            kwargs["name"] = str(len(melty_state_registry)) + "root"
            name = kwargs["name"]

        name_func = kwargs.get("name_func", None)
        if name_func is not None:
            try:
                name = name_func(input_value)
                if not isinstance(name, str):
                    name = str(name)
            except Exception as e:
                name = str(f"{e}")

        if Melty.depth <= 3:
            style = imgui.get_style()
            style.frame_rounding = 5.0
            style.item_spacing = (4, 0)
            style.window_padding = (3, 0)
            style.frame_padding = (4, 1)

        key = kwargs.get("key", None)
        key = key if key is not None else ""
        if name == "" and not is_root:
            if isinstance(input_value, (int, float, str, bool)):
                name = str(input_value) + input_value.__class__.__name__
            else:
                name = str(key) + input_value.__class__.__name__

        if Melty.depth > Melty.max_depth:
            if return_extras:
                return False, None, None
            return False, None

        # ----- Unique computation BEFORE pushing ID scope (avoid divergence) -----
        old_suffix = kwargs.get("suffix", None)
        unique_name = kwargs.get("unique_name", name)

        index = key if isinstance(key, int) else 0
        suffix = Melty.unique_stack[-1] if len(Melty.unique_stack) > 0 else (name or "")

        # Keep original behavior of always appending name (even if empty)
        if hasattr(input_value, 'id'):
            suffix = f"{suffix}_{str(getattr(input_value, 'id'))}"
        else:
            suffix = f"{old_suffix}_{suffix}_{unique_name}_{key}"

        if "layer_unique" in kwargs:
            unique = kwargs.pop("layer_unique")
        else:
            root_window_name = Melty.melty_window_stack[-1].name if len(Melty.melty_window_stack) > 0 else "Root"
            if is_root:
                unique = ui_id(datatype=type(input_value), suffix=name + unique_name + str(key) + func.__name__)
                suffix = f"{unique_name}_{func.__name__}_{unique}_{key}"
            else:
                unique = ui_id(datatype=type(input_value), suffix=suffix + unique_name +
                                                                  name + root_window_name +
                                                                  str(key) + func.__name__, idx=index)

        draw_state: DrawState = kwargs.get("draw_state", get_draw_state(unique))
        closable = kwargs.get("closable", False)
        # apply_mode = kwargs.get("apply_mode", ApplyMode.INSTANT)
        auto_apply = kwargs.get("auto_apply", ())

        tile_id = strhash(str(unique) + str(draw_state.id))
        draw_state._tile_id = tile_id
        if len(Melty.melty_window_stack) > 0:
            draw_state.parent_window = Melty.melty_window_stack[-1]
            draw_state.left_offset, draw_state.top_offset = (
                imgui.get_cursor_screen_pos()[0] - draw_state.parent_window.left,
                imgui.get_cursor_screen_pos()[1] - draw_state.parent_window.top)

        draw_state.closed = kwargs.get("closed", draw_state.closed)

        if closable:
            # Only perform this check on floating windows
            if draw_state._parent is not None and not draw_state._parent.clipped:
                if return_extras:
                    return False, None, draw_state
                return False, None

            if draw_state.parent_window is None:
                Melty.registered_windows[tile_id].input_value = input_value
                Melty.registered_windows[tile_id].draw_state = draw_state
                Melty.registered_windows[tile_id].window_args = kwargs
                Melty.registered_windows[tile_id].name = name

                if tile_id not in Melty.registered_windows:
                    Melty.cache.invalidate_by_obj(Melty.registered_windows)

            if draw_state.closed and not input_value == Melty.registered_windows:
                Melty.root_draw_states[draw_state.id] = []
                if return_extras:
                    return False, None, draw_state
                return False, None
            elif draw_state.closed and input_value == Melty.registered_windows:
                draw_state.closed = False



        draw_state._kwargs = kwargs
        ds_kwargs = copy(kwargs)
        exclude_ds_kwargs = ["input_value", "wanted_params", "depth", "shadow_depth",
                             "name", "z_offset", "use_cache", "active_layer", "auto_resize",
                             "unique", "suffix", "collection", "expanded_rect", "z_pos", "tint", "bg_offset",
                             "meta", "depth", "next_kwargs", "param_types"]
        for exclude_key in exclude_ds_kwargs:
            ds_kwargs.pop(exclude_key, None)
        draw_state.__dict__.update(ds_kwargs)

        draw_state.unique = unique
        draw_state._collection = Melty.collection_stack[-1] if len(Melty.collection_stack) > 0 else None
        draw_state.name = name
        original_type = type(input_value)
        converted_input = False

        draw_state.closable = closable
        draw_state.behind = kwargs.get("behind", draw_state.behind)
        draw_state.tile_mode = kwargs.get("tile_mode", draw_state.tile_mode)

        window_key = tile_id
        draw_state.persistent = kwargs.get("persistent", True)
        if draw_state.kwargs.temp:
            draw_state.dlt_count = 0

        computed_unique = unique
        # -------------------------------------------------------------------------
        if is_root:
            # NOTE: We already computed 'unique' correctly for root scope; do not recompute.
            Melty.unique_stack = []
            Melty.draw_state_stack = []
            Melty.flow_spacing = 0.0
            melty = get_melty_state()
            melty.nearest_drop_distance = melty.max_distance
            melty.nearest_drop_target = None
            melty.nearest_drop_target_tag = None
            Melty.indent_count = 0
            Melty.unindent_count = 0
        else:
            melty = get_melty_state()

        # After you compute `new_unique` for `obj` in the render loop:
        root = Melty.vis.root
        registry = root.draw_state_registry
        pending = Melty.move_draw_state_pending

        if pending:  # any remaps waiting?
            ds = pending.pop(id(input_value), None)  # is this object moved?
            if ds is not None:
                # If the draw state tracks its own unique, retire the old one
                old_u = getattr(ds, "unique", None)
                if old_u is not None:
                    registry.pop(old_u, None)
                    ds.unique = unique  # keep the DS in sync

                # Install under the new unique (overwrite if needed)
                registry[unique] = ds.deepcopy()

                # Optional: clean up empty dict to avoid pointless checks later
                if not pending:
                    # FIX: ensure to reset the same dict we read from
                    Melty.move_draw_state_pending = {}

        if len(Melty.draw_state_stack) > 0:
            draw_state._parent = Melty.draw_state_stack[-1]

        original_width_b = draw_state.width
        original_height_b = draw_state.height
        style_manager = Melty.global_attrs.get("style_manager", None)
        collection = kwargs.get("collection", None)

        # Handle untracked object invalidation
        if not hasattr(input_value, "__melty__"):
            if Melty.frame_count > 2 and draw_state.frame_count > 2:
                if isinstance(input_value, (type(None), int, float, str, bool, tuple, set)):
                    if draw_state._raw_input_value != input_value:
                        Melty.cache.invalidate_up(draw_state._tile_id, max_depth=7)
                        if kwargs.get("collection", None) is not None:
                            Melty.cache.invalidate_up_by_obj(collection, name=name, max_depth=5)
                            Melty.last_attr = draw_state.name
                            request_render()

        # if isinstance(input_value, Pending):
        #     raise Exception("Pending needs to be handled before saving to cache")
        draw_state._raw_input_value = input_value
        if draw_state._input_value_cache["external_state"][0] == UNSET_VALUE:
            if not isinstance(input_value, Pending):
                input_hash = Background.simple_hash(input_value)
                # if isinstance(input_value, Pending):
                #     raise Exception("Pending needs to be handled before saving to cache")
                draw_state._input_value_cache["external_state"] = (input_value, Melty.frame_count, input_hash)


        draw_state._wrapper = wrapper
        draw_state._bg_stack = copy(Melty.bg_stack)
        draw_state._bg_depth = Melty.bg_depth
        draw_state.current_tint = style_manager.get_tint()

        #############################################
        ###### Layer rendering delay
        original_active_layer = Melty.active_layer
        draw_state._start_z_pos = min(Melty.z_pos, 3)
        draw_state._parent_ctx = Melty.cache.get_current_parent()

        if active_layer is None:
            if (melty.dragged_item is not None and melty.drag_in_progress and
                    draw_state is not None and melty.dragged_item.id == draw_state.id):
                kwargs['layer'] = Melty.drag_layer
                kwargs['start_pos'] = imgui.get_cursor_screen_pos()
            else:

                if closable:
                    if draw_state is not None and draw_state.parent_window is not None:
                        # Nested window inherits layer from parent
                        window_z_pos = draw_state.parent_window.layer + 2
                    else:
                        # Managed windows are not nested, so we check the registry directly to find their layer
                        # Indicates this is not a nested window
                        window_z_pos = list(Melty.registered_windows.keys()).index(window_key) \
                            if window_key in Melty.registered_windows else None
                        if window_z_pos is not None:
                            window_z_pos = max(window_z_pos, Melty.active_layer)

                    kwargs['layer'] = window_z_pos

            if kwargs.get("layer", None) is not None and len(Melty.layers) > 0:
                layer = kwargs.pop("layer", None)

                if layer == len(Melty.registered_windows) - 1 and not "z_absolute" in kwargs:
                    layer = len(Melty.registered_windows) + Melty.top_layer_boost

                if closable and len(Melty.melty_window_stack) > 0:
                    draw_state._is_nested = True

                    parent_ds = draw_state._parent
                    Melty.root_draw_states[parent_ds.id].append(draw_state)

                    layer = layer + Melty.nested_layer_boost + len(Melty.root_draw_states[parent_ds.id])

                kwargs["active_layer"] = layer

                if layer >= len(Melty.layers) - 1:
                    layer = len(Melty.layers) - 1

                # draw_state.layer = layer
                Melty.layers[layer].append(draw_state)
                return_value = (False, None)
                if draw_state._tile_id in Melty.returned_values:
                    return_value = Melty.returned_values.pop(draw_state._tile_id)

                Melty.cache.mark_uncached(name, input_value, collection, tile_id, draw_state)

                if return_extras:
                    if len(return_value) == 3:
                        return return_value
                    else:
                        return *return_value, draw_state
                return return_value

        else:
            Melty.active_layer = active_layer

        kwargs['return_extras'] = False
        start_shadow_depth = Melty.shadow_depth
        if unique in Melty.seen_unique:
            if 'draw_state' in wanted_params:
                overlay_list: _DrawList = imgui.get_overlay_draw_list()
                overlay_list.add_text(*imgui.get_cursor_screen_pos(),
                                      imgui.get_color_u32_rgba(1.0, 0.0, 0.0, 1.0),
                                      f"ID")
                kwargs['use_cache'] = False
                # return False, None

        Melty.seen_unique.add(unique)

        no_cursor_reset = imgui.get_cursor_screen_pos()

        if not draw_state.expanded:
            kwargs.pop("width", None)
            kwargs.pop("height", None)

        has_collection = collection is not None and not isinstance(collection, tuple)
        if has_collection:
            Melty.collection_stack.append(collection)


        if not draw_state.expanded:
            passed_width = None
            passed_height = None

        fixed_size = not draw_state.auto_resize or kwargs.get("height", None) or draw_state.height == kwargs.get(
            "max_height", 1e8) or closable
        auto_resize = kwargs.get("auto_resize", True) or not draw_state.expanded
        draw_state.auto_resize = auto_resize and not fixed_size

        # Restore expanded =================
        if draw_state._last_expanded is not None and draw_state._last_expanded != draw_state.expanded:
            if draw_state._last_expanded:
                draw_state.expanded_rect = (draw_state.left, draw_state.top, draw_state.width, draw_state.height)
            else:
                draw_state._collapsed_rect = (draw_state.left, draw_state.top, draw_state.width, draw_state.height)

            if draw_state.expanded:
                # Restore rect
                draw_state.left, draw_state.right, draw_state.width, draw_state.height = draw_state.expanded_rect
                draw_state.expanded_rect = (0, 0, 0, 0)
            else:
                # Save rect
                draw_state.left, draw_state.right, draw_state.width, draw_state.height = draw_state._collapsed_rect


        if draw_state.expanded:
            draw_state.expanded_rect = (0, 0, 0, 0)
        #     draw_state.expanded_rect = (draw_state.left, draw_state.top, draw_state.width, draw_state.height)
        # else:
        #     draw_state._collapsed_rect = (draw_state.left, draw_state.top, draw_state.width, draw_state.height)

        draw_state._last_expanded = draw_state.expanded
        # End restore expanded ================

            # draw_state.expanded_rect = (0, 0, 0, 0)
        Melty.draw_state_stack.append(draw_state)

        draw_state._has_popup = kwargs.get("has_popup", False)
        if passed_width is not None:
            draw_state.width = snap_int(passed_width)
        if passed_height is not None:
            draw_state.height = snap_int(passed_height)

        Melty.input_value_stack.append(input_value)
        inc_depth = False

        # if hasattr(input_value, "tint"):
        #     draw_state.tint = getattr(input_value, "tint")
        draw_state.tint = kwargs.get("tint", draw_state.tint)

        melty_window = kwargs.get("melty_window", False)
        melty_window_header = kwargs.get("melty_window", False)
        draw_state.melty_window = melty_window_header
        previous_tint = None
        convert_path = None

        if closable and draw_state._is_nested and draw_state.current_tint is not None:
            style_manager.set_imgui_tint(*draw_state.current_tint)

        try:

            meta = kwargs.get("meta", None)
            if meta is None:
                # Use class meta as default if available
                if hasattr(type(input_value), "meta"):
                    meta = getattr(type(input_value), "meta")
                else:
                    if hasattr(Meta, 'get_child_meta'):
                        meta = Meta.get_child_meta(None, field_name=kwargs.get("name", ''), value=input_value)
                    else:
                        meta = Meta.get_new_defaults(default_value=input_value)

            if name is not None and name != "":
                meta.name = name

            meta.unique = unique

            def set_default(key, default_value, type=None):
                if key in vars(meta) and vars(meta)[key] is not None:
                    default_value = vars(meta)[key]

                # Custom draw state object to be dynamically created for unmatched params
                if key in draw_state.misc and type is not None:
                    default_value = draw_state.misc[key]
                    draw_state.misc_used.add(key)

                    if not default_value.__class__.__name__ == type.__name__:
                        default_value = None
                        draw_state.misc.pop(key, None)

                if default_value is None:
                    default_value = (param_defaults or {}).get(key, default_value)

                    # Create a new instance for the custom draw state object
                    if type is not None and default_value is None:
                        draw_state.misc[key] = type()
                        draw_state.misc_used.add(key)
                        default_value = draw_state.misc[key]

                kwargs.setdefault(key, default_value)

            kwargs = Melty.global_attrs | kwargs

            set_default("input_value", input_value)
            set_default("draw_state", draw_state)
            set_default("name", name)
            set_default("melty", melty)
            set_default("unique", unique)
            set_default("suffix", suffix)
            set_default("window_stack", Melty.window_stack)
            set_default("func", func)
            set_default("render_func", wrapper)
            kwargs.setdefault('meta', meta)

            ########## New event handler system ##########
            unique_events = Melty.events.get(tile_id, {})
            kwargs = unique_events | kwargs
            ##############################################

            kwargs = meta.__dict__ | kwargs
            for param in wanted_params:
                if param not in kwargs and param != "kwargs" and param != 'args' and param != 'o_kwargs' and param != 'next_kwargs':
                    wanted_type = name_to_param_type.get(param, None)
                    if wanted_type is inspect.Parameter.empty:
                        wanted_type = None
                    set_default(param, None, wanted_type)

            draw_state._kwargs = kwargs
            if draw_state.kwargs is None:
                draw_state.kwargs = AttrDict(kwargs)
            else:
                draw_state.kwargs.rebind(kwargs)

            draw_state.just_shadow = kwargs.get("just_shadow", False)

            inc_depth = True
            Melty.unique_stack.append(computed_unique)

            last_draw_state = Melty.last_draw_state[Melty.depth][0]
            if last_draw_state is not None:
                new_index_in_parent = Melty.collection_index_stack[-1] if len(Melty.collection_index_stack) > 0 else 0
                if draw_state.index_in_parent != new_index_in_parent:
                    draw_state.previous = last_draw_state
                    last_draw_state.next = draw_state
                    draw_state.index_in_parent = new_index_in_parent
            Melty.last_draw_state[Melty.depth] = (draw_state, kwargs.get("collection", None))

            passed_z_offset = kwargs.get("z_offset", 0)
            ds_z_offset = draw_state.z_offset
            internal_z_offset = 0
            if draw_state.pressed:
                if kwargs.get("shadow", False):
                    if draw_state.selected:
                        internal_z_offset = -3.0
                    else:
                        internal_z_offset = 1
                else:
                    if draw_state.selected:
                        internal_z_offset = -1
                    else:
                        internal_z_offset = 0

            elif draw_state.selected:
                if kwargs.get("shadow", False):
                    internal_z_offset = -2.0
                else:
                    internal_z_offset = -0.5

            else:
                if kwargs.get("shadow", False):
                    internal_z_offset = 1
                else:
                    internal_z_offset = 0

            total_z_offset = ds_z_offset + passed_z_offset + internal_z_offset

            draw_state.total_z_offset
            Melty.depth = Melty.depth + 1

            draw_state.depth = Melty.depth
            draw_state.layer = Melty.active_layer

            Melty.z_pos = (Melty.active_layer * Melty.max_depth) + Melty.depth
            draw_state.z_pos = Melty.z_pos

            kwargs['depth'] = Melty.depth
            draw_state._draggable = kwargs.get("draggable", False)

            # Wrapping
            parent_wrap = Melty.wrap_stack[-1] if len(Melty.wrap_stack) > 0 else False
            this_wrap = kwargs.get("wrap", False)
            if not auto_resize:
                parent_wrap = False
                this_wrap = False

            Melty.wrap_stack.append(this_wrap or parent_wrap)

            if draw_state.get_drag_mode() == DragMode.RESIZE_BR:
                melty.hover_stack.append(unique)

            ############################# WINDOW SETUP #####################################################
            ############ HANDLE WINDOW DRAGGING ########################
            imgui_active = Melty.imgui_active or Melty.imgui_popup_open

            if closable:
                # melty_hovered = draw_state.on_action("on_hover", view_id="window_hover", priority_delta=1)
                Melty.melty_window_stack.append(draw_state)

                if draw_state.window_pos is None and draw_state.width is not None:
                    draw_state.window_pos = (0, 0)

                if 'window_pos' in kwargs:
                    draw_state.window_pos = kwargs.get('window_pos', draw_state.window_pos)
            else:
                draw_state.window_pos = (0,0)

            if not auto_resize and (passed_width is None or passed_height is None):
                corner_rect = get_resize_handle(draw_state)
                handle_drag = draw_state.on_action("left_mouse_drag", view_id="window_resize",
                                                   rect=corner_rect, priority_delta=1)

                corner_drag = draw_state.on_action("right_mouse_drag", view_id="corner_drag", priority_delta=-1)
                if handle_drag is None:
                    handle_drag = corner_drag

                if handle_drag and not auto_resize:
                    if draw_state._initial_window_size is None:
                        draw_state._initial_window_size = (draw_state.width, draw_state.height)

                    draw_state.expanded = True
                    size_w = draw_state._initial_window_size[0] + handle_drag.total_dx
                    size_h = draw_state._initial_window_size[1] + handle_drag.total_dy
                    if passed_height is None:
                        draw_state.height = snap_int(max(size_h, 25))

                    if passed_width is None:
                        draw_state.width = snap_int(max(size_w, 25))
                else:
                    draw_state._initial_window_size = None

            if draw_state.expanded:
                draw_state.min_width = kwargs.get("min_width", draw_state.min_width)
                draw_state.min_height = kwargs.get("min_height", draw_state.min_height)

                if draw_state.width is not None and draw_state.min_width is not None:
                    draw_state.width = max(draw_state.width, draw_state.min_width)

                if draw_state.height is not None and draw_state.min_height is not None:
                    draw_state.height = max(draw_state.height, draw_state.min_height)

            if draw_state.window_pos is not None and closable:
                on_held = draw_state.on_action("left_mouse_held", "window_move", priority_delta=-2)
                on_drag = draw_state.on_action("left_mouse_drag", "window_move")
                left_mouse_down = draw_state.on_action("left_mouse_down", "window_move", priority_delta=-1)

                if left_mouse_down:
                    if len(Melty.melty_window_stack) > 0 and Melty.melty_window_stack[-1].parent_window is None:
                        Melty.move_window_to_front(Melty.melty_window_stack[-1])
                if on_drag and not imgui_active and not "window_pos" in kwargs:
                    if draw_state._initial_window_pos is None:
                        draw_state._initial_window_pos = (draw_state.window_pos[0],
                                                          draw_state.window_pos[1])

                    pos_x = draw_state._initial_window_pos[0] + on_drag.total_dx
                    pos_y = draw_state._initial_window_pos[1] + on_drag.total_dy
                    draw_state.window_pos = (pos_x, pos_y)
                else:
                    draw_state._initial_window_pos = None

                anchor_pos = kwargs.get("anchor", Anchor.TOP_LEFT)
                draw_state.anchor_pos = anchor_pos
                imgui.set_cursor_screen_pos((snap_int(draw_state.abs_left), snap_int(draw_state.abs_top)))

            kwargs['melty_window'] = False
            Melty.size_stack.append((draw_state.width, draw_state.height))

            if len(Melty.melty_window_stack) > 0:
                Melty.is_melty_window = True
            else:
                Melty.is_melty_window = False

            ######################## ERROR HANDLING FOR TYPES ########################
            cursor_pos = imgui.get_cursor_pos()
            imgui.set_cursor_pos((snap_int(cursor_pos[0]), snap_int(cursor_pos[1])))


            use_cache = kwargs.get("use_cache", False) and Melty.cache.enabled
            draw_state.use_cache = use_cache
            # if (draw_state.parent_window is not None and draw_state.window_pos is not None and
            #         draw_state.parent_window.window_pos is not None and closable):
            draw_state.left = draw_state.abs_left
            draw_state.top = draw_state.abs_top

            if fixed_size:
                Melty.fixed_size_stack.append(draw_state)


            if fixed_size and auto_resize and closable and draw_state.multi_line:
                draw_state.width = 400

            header_width = draw_state.header_width + draw_state.header_end_width
            if len(Melty.fixed_size_stack) > 0:
                fixed_size_draw_state = Melty.fixed_size_stack[-1]
                parent_wrap_width = fixed_size_draw_state.width
                parent_wrap_height = fixed_size_draw_state.height
                parent_wrap_left = fixed_size_draw_state.abs_left
                parent_wrap_top = fixed_size_draw_state.abs_top
            else:
                clip_size = Melty.get_clip_size()
                clip_rect = Melty.get_clip_rect()
                if clip_size is not None:
                    parent_wrap_width = clip_size[0]
                    parent_wrap_left = clip_rect[0]
                    parent_wrap_top = clip_rect[1]
                    parent_wrap_height = clip_size[1]
                else:
                    parent_wrap_width = imgui.get_io().display_size[0]
                    parent_wrap_left = 0
                    parent_wrap_top = 0
                    parent_wrap_height = imgui.get_io().display_size[1]

            column_parent = draw_state._parent
            column = kwargs.get("column", None)
            draw_state.final_max_column = draw_state._current_max_column
            # draw_state._current_max_column = 0
            draw_state._column_cursor = defaultdict(lambda: [0, 0])  # column -> (x, y)
            x_offset = draw_state.abs_left - parent_wrap_left
            if kwargs.get("fill_height", False) and passed_height is None and auto_resize:
                draw_state.height = snap_int(parent_wrap_height - (imgui.get_cursor_screen_pos()[1] - parent_wrap_top))
            if column is not None and column_parent is not None:
                column_parent._current_max_column = max(column_parent._current_max_column, column)
                parent_wrap_width = column_parent.content_width / (column_parent.final_max_column + 1)
                parent_wrap_left = draw_state.abs_left + snap_int(parent_wrap_width * column)
                available_width = parent_wrap_width - 2
            else:
                available_width = (parent_wrap_width - x_offset - content_margin)

            if len(Melty.fixed_size_stack) > 0:
                if (draw_state.auto_resize and not closable and not
                    Melty.is_wrapped() and passed_width is None):
                    draw_state.width = available_width

            single_line_avail = available_width - header_width - 5
            header_same_line = kwargs.get("header_same_line", False)
            if ((single_line_avail < 100 or draw_state.height - draw_state.footer_height > 50)
                    and not header_same_line and not Melty.is_wrapped()):
                draw_state.multi_line = True
                draw_state.content_width = available_width
            else:
                draw_state.multi_line = False
                draw_state.content_width = single_line_avail


            ################# Columns

            if column is not None and column_parent is not None:
                column_cursor_y = column_parent._column_cursor[column][1]
                current_cursor = imgui.get_cursor_screen_pos()
                imgui.set_cursor_screen_pos((parent_wrap_left, column_parent.abs_top + column_cursor_y + column_parent.header_height))
                draw_state.left_offset = parent_wrap_left - column_parent.abs_left
                draw_state.top_offset = column_parent.abs_top + column_cursor_y + draw_state.header_height - column_parent.abs_top
                draw_state.left = parent_wrap_left
                draw_state.top = parent_wrap_top + column_cursor_y + draw_state.header_height

                Collisions.register(column_parent)

            if draw_state.final_max_column > 1:
                column_width = draw_state.content_width / (draw_state.final_max_column + 1)
                draw_list: _DrawList = imgui.get_window_draw_list()
                for c in range(1, draw_state.final_max_column + 1):
                    # Draw divider lines, we are the parent now
                    draw_list.add_line(draw_state.left + snap_int(column_width * c), draw_state.top,
                                       draw_state.left + snap_int(column_width * c), draw_state.top + snap_int(draw_state.height),
                                       imgui.get_color_u32_rgba(0.0, 0.0, 0.0, 0.3), 1)


            ##########################



            if kwargs.get("live", False):
                draw_state.live = True
                fa_live_icon = "\uf0e7  Live"
                draw_list: _DrawList = imgui.get_window_draw_list()
                draw_list.add_text(draw_state.left + 5, draw_state.top - 20,
                                   imgui.get_color_u32_rgba(1.0, 0.0,
                                                            0.0, 1.0), fa_live_icon)
                Melty.cache.invalidate(tile_id)
            kwargs.pop("live", None)
            if not meta.visible_in_ui:
                if return_extras:
                    return False, None, draw_state
                return False, None

            # inside_clip = Melty.inside_clip(rect=(draw_state.abs_left, draw_state.abs_top,
            #                                             draw_state.width, draw_state.height))
            # if not inside_clip and draw_state.call_count > 2 and len(Melty.melty_window_stack) > 0:
            #     imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top + draw_state.height))
            #     return False, None


            last_bounding_hovered = draw_state._bounding_hovered
            new_bounding_hovered = draw_state.is_bounding_hovered()
            hover_changed = last_bounding_hovered != new_bounding_hovered
            draw_state._bounding_hovered = new_bounding_hovered
            if (draw_state.width is None or draw_state.height is None or hover_changed or
                    draw_state._bounding_hovered or draw_state._imgui_popover_open):
                if (not Melty.on_drag or draw_state.nested_window
                        and not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1)):
                    if not draw_state.just_shadow:
                        Melty.cache.invalidate(tile_id, force=True)

            if draw_state.width > 0 and draw_state.height > 0:
                inside_clip = Melty.fully_inside_clip(rect=(draw_state.left, draw_state.top,
                                                            draw_state.width, draw_state.height))
                needs_invalidate = False
                if inside_clip != draw_state.fully_clipped and inside_clip:
                    needs_invalidate = True
                draw_state.fully_clipped = inside_clip

                inside_clip = Melty.inside_clip(rect=(draw_state.left, draw_state.top,
                                                      draw_state.width, draw_state.height))
                if inside_clip != draw_state.clipped and inside_clip:
                    needs_invalidate = True
                draw_state.clipped = inside_clip

                if needs_invalidate and not Melty.window_drag and not imgui.is_mouse_down(2):
                    Melty.cache.invalidate(tile_id, force=True)

            if kwargs.get("shadow", False):
                Melty.shadow_depth = Melty.shadow_depth + 1 + total_z_offset
            else:
                Melty.shadow_depth = Melty.shadow_depth + total_z_offset

            draw_state.depth_and_layer = (Melty.shadow_depth, Melty.active_layer)
            if draw_state.tile_mode == TileMode.MIN:
                draw_state.shadow_margin = 0
            else:
                draw_state.shadow_margin = 0

            push_id(unique)

            if melty_window:
                Melty.push_clip((draw_state.left, draw_state.top,
                                 draw_state.left + draw_state.width,
                                 draw_state.top + draw_state.height))
            clip_rect = Melty.get_clip_rect()
            if clip_rect is not None:
                draw_state.clip_rect = clip_rect


            # Filter out originated functions that have an error pending state
            # no point auto-applying a converter that will just error out.
            _error_originated = set()
            for _p in draw_state._all_pending.values():
                if isinstance(_p, Pending) and _p.state == PendingState.ERROR:
                    _error_originated.add(_p.originated)

            _active_auto_apply = tuple(f for f in auto_apply if f not in _error_originated)

            if draw_state._save_pending_obj is not None and draw_state._save_pending_obj.originated in _active_auto_apply:
                if draw_state._save_pending_obj.originated not in _error_originated:
                    draw_state._apply_save = draw_state._save_pending_obj.originated
                    Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                    request_render()

            if draw_state._internal_pending is not None and draw_state._internal_pending.originated in _active_auto_apply:
                draw_state._apply_load = draw_state._internal_pending.originated
                Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                request_render()

            # if draw_state._apply_save is not None:
            #     Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
            #     Melty.cache.invalidate(draw_state._tile_id, force=True)
            #     request_render()
            #
            # if draw_state._apply_load is not None:
            #     Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
            #     Melty.cache.invalidate(draw_state._tile_id, force=True)
            #     request_render()

            content_rect = (0,0)
            unset_input = draw_state._input_value == UNSET_VALUE
            if Melty.cache.mark_start_offscreen(draw_state=draw_state):
                Melty.root_draw_states[draw_state.id] = []
                from src.lsd.gl_gui.view.core_views.new_core_view import draw_window
                from src.lsd.gl_gui.view.core_views.new_core_view import pending_window

                # draw_state.left = snap_int(draw_state.abs_left)
                # draw_state.top = snap_int(draw_state.abs_top)
                # if len(draw_state._all_pending) > 0:
                #     draw_window(
                #             input_value=draw_state._all_pending,
                #             closed=False, tint=draw_state.tint,
                #             pending_name="All Pending", name=f"{unique} Pending")
                #
                # for key, pending in draw_state._all_pending.items():
                #     if pending is not None:
                #         from src.lsd.gl_gui.view.core_views.new_core_view import draw_pending
                #         if pending.state == PendingState.ERROR:
                #             draw_window(pending, name=f"{key}", tint=(1,0,0))


                if draw_state._show_save and not draw_state._save_pending_obj.originated in auto_apply:
                    from src.lsd.gl_gui.view.core_views.new_core_view import Mode
                    if pending_window(
                            input_value=f"save", return_extras=True,
                            closed=False, tint=draw_state.tint, window_pos=(0,0), auto_resize=True, wrap=True,
                            button_name="Save", name=f"Save", anchor=Anchor.BOTTOM_LEFT, pending=draw_state._save_pending_obj,
                            mode=Mode.WINDOW_CLEAN)[0]:
                        print(f"Applying save for {draw_state._save_pending_obj.originated}")
                        draw_state._apply_save = draw_state._save_pending_obj.originated
                        draw_state._pending_convert = True
                        Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                        Melty.cache.invalidate_up(draw_state._tile_id, force=True)
                        request_render()

                if draw_state._show_load and not draw_state._internal_pending.originated in auto_apply:
                    from src.lsd.gl_gui.view.core_views.new_core_view import Mode

                    if pending_window(input_value=f"load",
                                   closed=False, window_pos=(0,0), auto_resize=True,
                                   pending=draw_state._internal_pending, wrap=True,
                                   button_name="Load", name=f"Load", anchor=Anchor.BOTTOM_LEFT, tint=draw_state.tint,
                                   mode=Mode.WINDOW_CLEAN)[0]:
                        draw_state._apply_load = draw_state._internal_pending.originated
                        Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True)
                        Melty.cache.invalidate_up(draw_state._tile_id, max_depth=5, force=True)

                        draw_state._pending_convert = True
                        request_render()

                if (draw_state.left is not None and draw_state.top is not None and
                    draw_state.width is not None and draw_state.height is not None) and closable:
                    if (draw_state.width > 0 and draw_state.height > 0):
                        reset_to = imgui.get_cursor_screen_pos()

                        imgui.invisible_button(str(unique) + "window_blocker", width=draw_state.width,
                                               height=draw_state.height)
                        imgui.set_cursor_screen_pos(reset_to)
                        imgui.set_item_allow_overlap()

                begin_group(unique)

                draw_state.left = snap_int(draw_state.left)
                draw_state.top = snap_int(draw_state.top)
                # expected_type = param_types[0] if len(param_types) > 0 else None
                convert_path = kwargs.get('convert', [])
                if isinstance(convert_path, list) and len(convert_path) > 0:
                    if not isinstance(convert_path[-1], type):
                        to_type = Melty._converter_to_type.get(convert_path[-1], None)[1]
                    else:
                        to_type = convert_path[-1] if len(convert_path) > 0 else None
                    if to_type is None or (isinstance(to_type, type) and isinstance(input_value, to_type)):
                        needs_convert = False
                    else:
                        needs_convert = True
                else:
                    needs_convert = False
                    to_type = None

                thead_launch_frame = 0
                if needs_convert:
                    input_hash = Background.simple_hash(draw_state._raw_input_value)
                    cached_hash = Background.simple_hash(draw_state._input_value_cache["external_state"][0])
                    if input_hash == cached_hash:
                        input_changed = False
                    else:
                        input_changed = True
                        Melty.cache.invalidate_up(draw_state._tile_id)
                        Melty.cache.invalidate_up(draw_state._parent._tile_id)
                        print(f"Input changed for {draw_state.name}, invalidating cache. New hash: {input_hash}, Old hash: {cached_hash}")
                        request_render()

                    if draw_state._apply_load is not None or draw_state._pending_convert:
                        input_changed = True

                    # if input_changed:
                        # if isinstance(draw_state._raw_input_value, Pending):
                        #     raise Exception("Pending needs to be handled before passed to cache")

                        # Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
                        # Melty.cache.invalidate(draw_state._tile_id, force=True)
                        # request_render()
                    if (draw_state._raw_input_value == UNSET_VALUE or
                            (draw_state._raw_input_value is None and convert_path[0] != types.NoneType)):
                        internal_value, thead_launch_frame = draw_state._input_value_cache["internal_state"]

                    else:
                        if input_hash != cached_hash:
                            start_frame = Melty.frame_count
                        else:
                            start_frame = Melty.frame_count


                        if isinstance(convert_path[0], type):
                            if len(convert_path) > 1:
                                converter_kwargs = Melty.converter_flags_by_type.get(
                                    (convert_path[0], convert_path[1]), {})
                            else:
                                converter_kwargs = Melty.converter_flags_by_type.get((convert_path[0], None), {})
                        else:
                            converter_kwargs = Melty.converter_flags.get(convert_path[0], {})

                        if (not Melty.on_drag and not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1)) or draw_state._input_value_cache["internal_state"][0] == UNSET_VALUE:

                            no_cache = converter_kwargs.get("stateful", False)
                            if input_changed:
                                no_cache = True

                            internal_value = Background.run(convert, value=draw_state._raw_input_value,
                                                            user_id=str(draw_state.unique) + f" | input convert {str(convert_path[-1].__name__)}",
                                                            invalidate_id=draw_state._parent._tile_id, draw_state=draw_state,
                                                            on_frame=start_frame, no_cache=no_cache,
                                                            apply=draw_state._apply_load,
                                                            path=convert_path, cache_id=str(draw_state.unique), registry=Melty, )

                            if isinstance(internal_value, tuple):
                                internal_value, thead_launch_frame = internal_value
                            if not isinstance(internal_value, Pending):
                                if draw_state._apply_load is not None:
                                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True)
                                    request_render()
                                draw_state._apply_load = None
                                draw_state._pending_convert = False
                                draw_state._internal_pending = None
                                draw_state._show_load = False
                                draw_state._all_pending['load_pending'] = None
                            else:
                                if internal_value.state != PendingState.BACKGROUND:
                                    draw_state._all_pending['load_pending'] = internal_value

                        else:
                            internal_value = draw_state._input_value_cache["internal_state"][0]

                        # request_render()
                        if not isinstance(internal_value, Pending):
                            if draw_state._apply_load is not None:
                                thead_launch_frame = Melty.frame_count
                                # Melty.cache.invalidate_up_by_obj(draw_state._raw_input_value)
                                Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                                # Melty.cache.invalidate(draw_state._tile_id, force=True)
                                request_render()
                    if isinstance(internal_value, tuple):
                        internal_value, thead_launch_frame = internal_value
                    internal_changed = False
                    # if isinstance(internal_value, Pending):
                    #     input_changed = True
                    # internal_hash = Background.simple_hash(internal_value)
                    # cached_internal_hash = Background.simple_hash(draw_state._input_value_cache["internal_state"][0])
                    if internal_value != draw_state._input_value_cache["internal_state"][0]:
                        if isinstance(internal_value, Pending) and internal_value.state != PendingState.BACKGROUND:
                            internal_changed = True
                            # Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
                            request_render()

                            # draw_state._input_value_cache["internal_state"] = internal_value, thead_launch_frame
                        # Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
                        # Melty.cache.invalidate(draw_state._tile_id, force=True)


                    if input_changed or internal_changed or draw_state._input_value_cache["internal_state"][0] == UNSET_VALUE:
                        if isinstance(internal_value, Pending):
                            if internal_value.state == PendingState.CONFIRM:
                                draw_state._internal_pending = internal_value
                                if not draw_state._show_load:
                                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True)
                                    request_render()
                                draw_state._show_load = True
                                if type(internal_value.wrapped) != type(draw_state._input_value_cache["internal_state"][0]):
                                    internal_value = draw_state._input_value_cache["internal_state"][0]
                                else:
                                    internal_value = internal_value.wrapped

                                # if isinstance(internal_value, Pending):
                                #     raise Exception("Pending needs to be handled before saving to cache")

                        if isinstance(internal_value, Pending):
                            draw_state._load_pending = True
                            draw_state._load_pending_for += 1
                            # Debounce loading spinner icon
                            if draw_state._load_pending_for > 2:
                                draw_pending_status(draw_state, internal_value)
                            # print(f"Internal value pending for {draw_state.name}, invalidating cache. Status: {internal_value.status}")
                            # Melty.cache.invalidate_up(draw_state._tile_id, force=True)
                            # request_render()
                        else:
                            draw_state._load_pending = False
                            draw_state._load_pending_for = 0
                            prev_internal_hash = Background.simple_hash(
                                draw_state._input_value_cache["internal_state"][0])

                            if thead_launch_frame >= draw_state._input_value_cache["internal_state"][1] + 1:
                                # if isinstance(internal_value, Pending):
                                #     raise Exception("Pending needs to be handled before saving to cache")
                                draw_state._input_value_cache["internal_state"] = internal_value, thead_launch_frame
                                draw_state._input_value = internal_value
                                new_internal_hash = Background.simple_hash(
                                    draw_state._input_value_cache["internal_state"][0])

                                if prev_internal_hash != new_internal_hash:
                                    Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
                                    Melty.cache.invalidate(draw_state._tile_id, force=True)
                                    request_render()

                    converted_input = True
                    draw_state.explain_convert = path_to_string(convert_path)
                    draw_state._input_value = draw_state._input_value_cache["internal_state"][0]
                    kwargs["input_value"] = draw_state._input_value_cache["internal_state"][0]

                    draw_state._input_value = draw_state._input_value_cache["internal_state"][0]
                    # if isinstance(draw_state._input_value_cache["external_state"][0], Pending):
                    #     raise Exception("Pending needs to be handled before saving to cache")
                    draw_state._raw_input_value = draw_state._input_value_cache["external_state"][0]
                else:
                    draw_state._input_value = input_value
                    kwargs["input_value"] = draw_state._input_value

                highlight = draw_state.selected

                show_bg = kwargs.get("show_bg", False) or (
                        highlight and draw_state.height < 60) or not draw_state.expanded
                expected_type = param_types[0] if len(param_types) > 0 else None

                # Input value is indexable
                if isinstance(input_value, dict) and "decorators" in input_value:
                    for decorator_name, decorator_value in input_value["decorators"].items():
                        if "tint" in decorator_value and decorator_value["tint"] is not None:
                            previous_tint = style_manager.get_tint()
                            style_manager.set_imgui_tint(*decorator_value["tint"])
                elif "tint" in kwargs and kwargs.get("tint", None) is not None:
                    previous_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*kwargs.get("tint"))

                elif hasattr(input_value, "tint") and input_value.tint is not None and isinstance(input_value.tint, (tuple, list)):
                    previous_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*input_value.tint)
                elif hasattr(collection, "__tint__") and getattr(collection, "__tint__"):
                    if name in collection.__tint__:
                        previous_tint = style_manager.get_tint()
                        style_manager.set_imgui_tint(*collection.__tint__[name])

                elif draw_state.tint is not None and kwargs.get("show_bg", False) and kwargs.get("show_tint",
                                                                                                 False):
                    previous_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*draw_state.tint)

                if converted_input:
                    # reformat icon wrench
                    converted_icon_text = f"\uf0ad"
                    overlay_list: _DrawList = imgui.get_window_draw_list()
                    overlay_list.add_text(*(draw_state.left + draw_state.header_width + 5, draw_state.top + 5),
                                          imgui.get_color_u32_rgba(0.5, 0.0, 0.0, 1.0),
                                          f"{converted_icon_text}")

                if show_bg:
                    if Melty.channels_split:
                        offscreen_depth = Melty.get_channel()
                        draw_list = imgui.get_window_draw_list()
                        draw_list.channels_set_current(
                            max(0, min(offscreen_depth + passed_z_offset - 2,
                                       Melty.max_depth - 1)))

                    draw_state.corner_radius = 5.0
                    from src.lsd.gl_gui.view.core_views.new_core_view import draw_bg
                    style_manager = Melty.global_attrs['style_manager']
                    global_style = Melty.global_attrs['global_style']

                    bg_color = (0, 0, 0, 0)
                    if draw_state.width > 5 and draw_state.height > 5:
                        nested_bg = not closable and kwargs.get("bg_offset", 0) >= 0
                        bg_return = draw_bg(bypass=True, left=draw_state.left, top=draw_state.top,
                                              width=draw_state.width, height=draw_state.height,
                                              rounding=draw_state.corner_radius,
                                              depth=Melty.shadow_depth, selected=draw_state.selected,
                                              global_style=global_style, opacity=1.0 if show_bg else 0.0,
                                              pressed=draw_state.pressed,
                                              style_manager=style_manager, nested_bg=nested_bg)
                        if bg_return is not None:
                            bg_color = bg_return[1]

                    Melty.bg_color_stack.append(bg_color)

                if Melty.channels_split:
                    offscreen_depth = Melty.get_channel()
                    draw_list = imgui.get_window_draw_list()
                    draw_list.channels_set_current(
                        max(0, min(offscreen_depth + passed_z_offset + ds_z_offset, Melty.max_depth - 1)))

                if kwargs.get("selectable", True):
                    left_mouse_down_press = draw_state.on_action("left_mouse_held", "press", priority_delta=-1)
                    draw_state.pressed = True if left_mouse_down_press else False
                    click = draw_state.on_action("left_mouse_click", priority_delta=0)
                    middle_down = draw_state.on_action("middle_mouse_down", priority_delta=0)

                    if middle_down:
                        Melty.selected = set()
                        Melty.selected.add(draw_state)
                        Melty.last_selected = draw_state

                    elif click and not Melty.imgui_active:
                        Melty.previous_select = copy(Melty.selected)
                        if not click.modifiers:
                            if len(Melty.selected) == 1 and draw_state in Melty.selected:
                                Melty.selected.remove(draw_state)
                            else:
                                Melty.selected = set()
                                Melty.selected.add(draw_state)
                                Melty.last_selected = draw_state
                        elif click.modifiers == glfw.MOD_CONTROL:
                            if draw_state in Melty.selected:
                                Melty.selected.remove(draw_state)
                            else:
                                Melty.selected.add(draw_state)

                            Melty.cache.invalidate(tile_id)

                        elif click.modifiers == glfw.MOD_SHIFT:
                            if draw_state in Melty.selected:
                                Melty.selected.remove(draw_state)
                                adding = False
                            else:
                                Melty.selected.add(draw_state)
                                adding = True

                            if Melty.last_selected is not None:
                                # Check if both have the same parent
                                it_count = 0
                                max_iter = 1000
                                seen = set()
                                items_to_select = []

                                ds_index = draw_state.index_in_parent
                                last_index = Melty.last_selected.index_in_parent
                                go_back = ds_index > last_index
                                if go_back:
                                    next_ds = draw_state.previous
                                else:
                                    next_ds = draw_state.next

                                while (id(next_ds) not in seen and
                                       id(next_ds) != id(Melty.last_selected) and
                                       next_ds is not None
                                       and it_count < max_iter):
                                    seen.add(id(next_ds))
                                    items_to_select.append(next_ds)

                                    if go_back:
                                        next_ds = next_ds.previous
                                    else:
                                        next_ds = next_ds.next
                                    it_count += 1

                                if id(next_ds) == id(Melty.last_selected):
                                    for item in items_to_select:
                                        if adding:
                                            if item not in Melty.selected:
                                                Melty.selected.add(item)
                                        else:
                                            if item in Melty.selected:
                                                Melty.selected.remove(item)

                                        Melty.cache.invalidate(item._tile_id)

                                Melty.cache.invalidate(draw_state._parent._tile_id)
                                Melty.cache.invalidate(Melty.last_selected._parent._tile_id)

                                request_render()

                            Melty.last_selected = draw_state
                            request_render()

                    draw_state.selected = draw_state in Melty.selected



                ########### CONTEXT MENU HANDLING ############



                from src.lsd.gl_gui.view.core_views.new_core_view import default_context_menu
                draw_context_menu = kwargs.get("context_menu", default_context_menu)
                if draw_context_menu is not None:
                    right_click = draw_state.on_action("right_mouse_clicked")
                    if right_click:
                        draw_state.context_menu_open = not draw_state.context_menu_open
                        if draw_state.context_menu_ds is not None:
                            draw_state.context_menu_ds.closed = not draw_state.context_menu_open
                    if draw_state.context_menu_open:
                        bg_offset = 0
                        if draw_state._is_nested:
                            bg_offset = 0
                        # Melty.bg_depth += bg_offset
                        tint = style_manager.get_tint()

                        mixed_color = style_manager.make_color_rgb(tint[0], tint[1], tint[2],
                                                                   value=0.03, factor=0.2,
                                                                   saturation_scale=0.5,
                                                                   alpha=1.0)
                        from src.lsd.gl_gui.view.core_views.new_core_view import draw_window

                        returned_val = draw_window(input_value=draw_state, view_func=draw_context_menu, func=func,
                                                   tint=mixed_color, show_tint=False, show_add_delete=False,
                                                   width=400, max_height=800, bg_offset=bg_offset,
                                                   persistent=False, anchor=Anchor.BOTTOM_LEFT,
                                                   with_footer=None, use_cache=True,
                                                   name=f"{name}##context_menu_{unique}", auto_resize=True,
                                                   return_extras=True)

                        ctx_ds = returned_val[2]
                        ctx_ds.tint = tint
                        # Melty.bg_depth -= bg_offset
                        draw_state.context_menu_ds = ctx_ds
                        # ctx_ds.parent_window = Melty.melty_window_stack[-1] if len(Melty.melty_window_stack) > 0 else None
                        if ctx_ds.last_seen is None:
                            ctx_ds.closed = False
                            ctx_ds.window_pos = (0, 0)

                        if ctx_ds.closed:
                            draw_state.context_menu_open = False

                if kwargs.get("show_bg", False):
                    outline_margin = 3
                else:
                    outline_margin = 0

                header_start = imgui.get_cursor_screen_pos()

                #
                # if "with_footer" in kwargs and kwargs.get("with_footer", None) is not None:
                #     imgui.set_cursor_screen_pos(
                #         (draw_state.left, draw_state.top + draw_state.height - draw_state.footer_height))
                #     from src.lsd.gl_gui.view.core_views.new_core_view import empty

                if "with_footer" in kwargs and kwargs.get("with_footer", None) is not None:
                    if closable and draw_state.expanded:
                        imgui.set_cursor_screen_pos(
                            (draw_state.left, draw_state.top + draw_state.height - draw_state.footer_height))
                        from src.lsd.gl_gui.view.core_views.new_core_view import empty
                        empty(name="footer_shadow", z_offset=0,
                              tile_mode=TileMode.MAX, width=draw_state.width - 2,
                              height=draw_state.footer_height)
                        imgui.set_cursor_screen_pos(header_start)

                # if draw_state.closable:
                #
                #     imgui.set_cursor_screen_pos(header_start)
                #     from src.lsd.gl_gui.view.core_views.new_core_view import empty
                #     empty(input_value=input_value, name=f"shadow{unique}", shadow=True, width=draw_state.width - 2,
                #           show_bg=False, height=draw_state.header_height + 2, z_offset=-2)
                if "with_header" in kwargs and kwargs.get("with_header", None) is not None and kwargs.get("show_header",
                                                                                                          True):

                    next_kwargs = kwargs.get('next_kwargs', {})
                    next_kwargs['func'] = func
                    next_kwargs['outer_func'] = wrapper
                    next_kwargs['show_bg'] = kwargs.get("show_bg", True)
                    draw_header = kwargs.get("with_header", None)
                    imgui.begin_group()
                    header_start_cursor = imgui.get_cursor_screen_pos()

                    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] + outline_margin,
                                                 imgui.get_cursor_screen_pos()[1] + outline_margin))
                    draw_header(**kwargs)


                    imgui.set_cursor_screen_pos(header_start_cursor)
                    end_group()
                    if imgui.is_item_active() or imgui.is_item_activated():
                        Melty.report_imgui_active()
                    draw_state.header_left = header_start_cursor[0]
                    draw_state.header_top = header_start_cursor[1]
                    header_rect = imgui.get_item_rect_size()
                    draw_state.header_width = header_rect[0]
                    draw_state.header_height = header_rect[1]

                    if not draw_state.multi_line:
                        same_line()

                else:
                    draw_state.header_left = draw_state.left
                    draw_state.header_top = draw_state.top
                    draw_state.header_width = 0
                    draw_state.header_height = 0
                    imgui.begin_group()
                    end_group()

                if "with_header_end" in kwargs and kwargs.get("with_header_end", None) is not None and kwargs.get(
                        "show_header",
                        True):
                    next_kwargs = kwargs.get('next_kwargs', {})
                    next_kwargs['func'] = func
                    next_kwargs['outer_func'] = wrapper
                    next_kwargs['show_bg'] = kwargs.get("show_bg", True)
                    draw_header_end = kwargs.get("with_header_end", None)

                    clip_size = Melty.get_clip_size()
                    imgui.same_line()

                    if clip_size is None and (not auto_resize or closable):
                        clip_size = (draw_state.width, draw_state.height)
                    current_cursor = imgui.get_cursor_screen_pos()
                    if closable:
                        margin = 0
                    else:
                        margin = 10

                    if clip_size is not None:
                        end_x = max(draw_state.left,
                                    draw_state.left + clip_size[0] - draw_state.header_end_width - margin)
                        end_x = max(end_x, draw_state.left + draw_state.header_width + 10)
                        if not draw_state.expanded:
                            end_x = draw_state.left + draw_state.header_width + 10
                        imgui.set_cursor_screen_pos((end_x,
                                                     imgui.get_cursor_screen_pos()[1] + outline_margin))

                    # if Melty.channels_split:
                    #     draw_list = imgui.get_window_draw_list()
                    #     draw_list.channels_set_current(Melty.get_channel() + kwargs.get("channel_offset", 0))

                    imgui.begin_group()
                    start_c = imgui.get_cursor_screen_pos()
                    draw_header_end(**kwargs)
                    imgui.set_cursor_screen_pos(start_c)
                    imgui.same_line(spacing=0)
                    imgui.dummy(outline_margin, 0)
                    end_group()
                    if imgui.is_item_active() or imgui.is_item_activated():
                        Melty.report_imgui_active()

                    end_header_rect = imgui.get_item_rect_size()
                    draw_state.header_end_width = end_header_rect[0]

                    if closable:
                        current_cursor = imgui.get_cursor_screen_pos()
                        imgui.set_cursor_screen_pos((draw_state.left, draw_state.top))
                        from src.lsd.gl_gui.view.core_views.new_core_view import empty
                        empty(name="header_shadow", z_offset=-1,
                              tile_mode=TileMode.MAX, width=draw_state.width - 2,
                              height=draw_state.header_height)
                        imgui.set_cursor_screen_pos(current_cursor)

                    if not draw_state.multi_line:
                        imgui.same_line()
                        imgui.set_cursor_screen_pos(current_cursor)

                else:
                    draw_state.header_end_width = 0

                # header_end_cursor = imgui.get_cursor_screen_pos()
                # if draw_state.closable:
                #     imgui.set_cursor_screen_pos(header_start)
                #     from src.lsd.gl_gui.view.core_views.new_core_view import empty
                #     empty(input_value=input_value, name=f"empty_{unique}", width=draw_state.width,
                #           show_bg=False, height=draw_state.header_height + 2, z_offset=-1)
                #     imgui.set_cursor_screen_pos(header_end_cursor)
                ####################################################################################
                #### with callback header

                if draw_state.header_left is not None and draw_state.left is not None:
                    draw_state.header_left_delta = draw_state.left - draw_state.header_left
                    draw_state.header_top_delta = draw_state.top - draw_state.header_top

                indent_x = kwargs.get("indent_size", 0)
                if indent_x > 0:
                    sc = imgui.get_cursor_screen_pos()
                    imgui.set_cursor_screen_pos((sc[0] + indent_x,
                                                 sc[1]))

                imgui.begin_group()

                is_hovered = draw_state.on_action("cursor_hover", view_id="test", priority_delta=-1) is not None

                if Melty.inside_clip(draw_state=draw_state):
                    hover_eligible = draw_state.hover_eligible() and draw_state.hover_reported
                else:
                    hover_eligible = False
                if hover_eligible:
                    if closable:
                        Melty.any_window_hovered_pending = True
                    max_layer_depth = Melty.max_depth * Melty.max_layer + Melty.max_depth
                    priority = max_layer_depth - draw_state.z_pos
                    event_names = copy(wanted_params)
                    Melty.event_handler.register_hovered(tile_id, event_names, priority, tile_id,
                                                         selected=draw_state.selected)

                ###########################################################
                kwargs['next_kwargs'] = kwargs
                if 'kwargs' in wanted_params:
                    clean_args = kwargs
                else:
                    clean_args = {k: kwargs[k] for k in wanted_params if k in kwargs}
                ###########################################################
                draw_state.channel = Melty.get_channel()

                if not auto_resize:
                    draw_list = imgui.get_window_draw_list()
                    if Melty.channels_split:
                        draw_list.channels_set_current((Melty.max_depth - 1))

                    if passed_height is None or passed_width is None:
                        draw_resize_handle(draw_state)
                    if Melty.channels_split:
                        draw_list.channels_set_current(Melty.get_channel() + kwargs.get("channel_offset", 0))

                ##### Register With event handler #########################

                ##########################################################

                if hasattr(input_value, 'pending_upload') and callable(getattr(input_value, 'pending_upload')):
                    try:
                        pending = input_value.pending_upload()
                        # request_render()
                    except Exception as e:
                        print(f"Error checking pending upload: {e}")

                ############# HANDLE SELECTION
                top = draw_state.top
                left = draw_state.left
                width = draw_state.width
                height = draw_state.height

                top = snap_int(top)
                left = snap_int(left)
                width = snap_int(width)
                height = snap_int(height)

                # left, top, width, height = Melty.apply_clip_ds(draw_state)

                #### MAIN CALL #######################
                clip_rect = Melty.get_clip_rect()
                Melty.push_clip((left, top,
                                 left + width,
                                 top + height - draw_state.footer_height))

                if show_bg:
                    Melty.bg_depth += 1 + kwargs.get("bg_offset", 0)
                    Melty.bg_stack.append(style_manager.get_tint())

                return_value = draw_inner_main(clean_args, draw_state,
                                               input_value, unique, kwargs)


                if show_bg:
                    Melty.bg_depth -= 1 + kwargs.get("bg_offset", 0)
                    Melty.bg_stack.pop()
                    Melty.bg_color_stack.pop()

                draw_state._imgui_is_hovered = draw_state._imgui_is_item_hovered and is_hovered

                if imgui.is_item_active() or imgui.is_item_activated():
                    Melty.report_imgui_active()
                #######################
                content_rect = imgui.get_item_rect_size()
                draw_state._content_rect = content_rect
                end_group()
                Melty.pop_clip()


                if draw_state.scroll_visible:
                    imgui.set_cursor_screen_pos((draw_state.left + 2, draw_state.top + draw_state.header_height + 2))
                    inset_start = imgui.get_cursor_screen_pos()
                    from src.lsd.gl_gui.view.core_views.new_core_view import empty
                    empty(name="inset", z_offset=-2,
                          tile_mode=TileMode.MAX, width=draw_state.width - 4,
                          height=draw_state.height - draw_state.header_height - draw_state.footer_height - 4)
                    imgui.set_cursor_screen_pos(inset_start)

                if ("with_footer" in kwargs and kwargs.get("with_footer", None) is not None and
                        draw_state.expanded):

                    next_kwargs = kwargs.get('next_kwargs', {})
                    next_kwargs['func'] = func
                    next_kwargs['outer_func'] = wrapper
                    next_kwargs['show_bg'] = kwargs.get("show_bg", True)
                    draw_footer = kwargs.get("with_footer", None)

                    current_cursor = imgui.get_cursor_screen_pos()
                    if not auto_resize:
                        imgui.set_cursor_screen_pos((current_cursor[0] + outline_margin,
                                                     draw_state.top + draw_state.height - draw_state.footer_height - outline_margin))

                    push_id(str(unique) + "footer")
                    imgui.begin_group()

                    foot_start = imgui.get_cursor_screen_pos()

                    draw_footer(**kwargs)
                    imgui.set_cursor_screen_pos(foot_start)

                    if imgui.is_item_active() or imgui.is_item_activated():
                        Melty.report_imgui_active()
                    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))


                    end_group()
                    pop_style_var(2)
                    footer_rect = imgui.get_item_rect_size()
                    draw_state.footer_height = footer_rect[1]
                    draw_state.footer_width = footer_rect[0]
                    pop_id()

                else:
                    draw_state.footer_height = 0
                    draw_state.footer_width = 0


                if not kwargs.get("imgui_padding", True):
                    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                    end_group()
                    pop_style_var(2)
                else:
                    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                    end_group()
                    pop_style_var(2)

                if previous_tint is not None:
                    style_manager.set_imgui_tint(*previous_tint)



            draw_state._imgui_is_edited = imgui.is_item_edited()
            draw_state._imgui_is_activated = imgui.is_item_activated()
            draw_state._imgui_is_active = imgui.is_item_active()
            draw_state._imgui_is_focused = imgui.is_item_focused()
            draw_state._imgui_is_item_hovered = imgui.is_item_hovered()
            item_rect = imgui.get_item_rect_size()
            if passed_height is not None:
                item_rect = (item_rect[0], passed_height)
            if passed_width is not None:
                item_rect = (passed_width, item_rect[1])
            if return_value is not None and len(return_value) >= 2:
                child_changed = return_value[0]
                new_value_child = return_value[1]
                report_value = new_value_child
                report_changed = False

                if draw_state._apply_save:
                    converted_input = True
                thead_launch_frame = 0
                if converted_input or draw_state._apply_save:
                    # Clear ERROR pending if the user edits the code -
                    # the new code might be valid, so allow auto_apply to retry.
                    if child_changed:
                        _cur_sp = draw_state._all_pending.get("save_pending")
                        if isinstance(_cur_sp, Pending) and _cur_sp.state == PendingState.ERROR:
                            # print(f"Clearing ERROR pending for {draw_state.name} since user edited the value. Error pending status: {_cur_sp.status}")
                            draw_state._all_pending["save_pending"] = None
                    if child_changed:
                        # # draw_state._input_value_cache["external_state"] = ...
                        # if isinstance(new_value_child, Pending):
                        #     raise Exception("Pending needs to be handled before saving to cache")
                        draw_state._input_value_cache["internal_state"] = new_value_child, Melty.frame_count + 1
                        # Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
                        # Melty.cache.invalidate(draw_state._tile_id, force=True)
                        # request_render()
                    else:
                        new_value_child = draw_state._input_value_cache["internal_state"][0]

                    if isinstance(convert_path, list):
                        ####################################### SAVE HANDLER
                        # Reverse the path to convert back up to the original type
                        convert_path = invert_path(convert_path, registry=Melty)
                        # no_bg_needed = (not child_changed and not input_input
                        #                and draw_state._save_pending_for == 0
                        #                and not draw_state._apply_save
                        #                and not draw_state._show_load
                        #                and not draw_state._save_pending
                        #                and not draw_state._show_save
                        #                and not draw_state._apply_load
                        #                and not input_changed)


                        if new_value_child == UNSET_VALUE or (
                                draw_state._raw_input_value is None and convert_path[0] != types.NoneType):
                            external_value = draw_state._input_value_cache["external_state"] = (
                            input_value, draw_state._input_value_cache["external_state"][1])
                        else:
                            if not Melty.on_drag and not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1):
                                if isinstance(convert_path[0], type):
                                    if len(convert_path) > 1:
                                        converter_kwargs = Melty.converter_flags_by_type.get(
                                            (convert_path[0], convert_path[1]), {})
                                    else:
                                        converter_kwargs = Melty.converter_flags_by_type.get((convert_path[0], None), {})
                                else:
                                    converter_kwargs = Melty.converter_flags.get(convert_path[0], {})


                                # do_apply = draw_state._apply_save
                                # if apply_mode == ApplyMode.CONFIRM:
                                #     do_apply = draw_state._apply_save
                                # elif apply_mode == ApplyMode.INSTANT:
                                #     do_apply = True



                                no_cache = converter_kwargs.get("stateful", False)
                                if child_changed:
                                    no_cache = True


                                if draw_state._apply_save is not None:
                                    no_cache=True
                                external_value = Background.run(convert, user_id=str(unique) + f" | output convert {str(convert_path[-1].__name__)}",
                                                                invalidate_id=draw_state._parent._tile_id,
                                                                cache_id=str(draw_state.unique),
                                                                on_frame=Melty.frame_count, no_cache=no_cache,
                                                                value=new_value_child, apply=draw_state._apply_save,
                                                                path=convert_path, registry=Melty, )
                                if isinstance(external_value, tuple):
                                    external_value, thead_launch_frame = external_value

                                if not isinstance(external_value, Pending):
                                    if draw_state._show_save or draw_state._apply_save is not None:
                                        draw_state._apply_save = None
                                        Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4, force=True)

                                    _cur_sp = draw_state._all_pending.get("save_pending")
                                    if _cur_sp is not None and not (isinstance(_cur_sp, Pending) and _cur_sp.state == PendingState.ERROR):
                                        # print(f"Clearing pending for {draw_state.name} since conversion returned non-pending value {external_value}")
                                        draw_state._all_pending["save_pending"] = None

                                else:
                                    if external_value.state == PendingState.ERROR:
                                        draw_state._apply_save = None
                                        draw_state._save_pending_obj = None
                                        draw_state._show_save = False
                                    if external_value.state != PendingState.BACKGROUND:
                                        # Don't let a successful error overwrite an ERROR in _all_pending
                                        _cur_sp = draw_state._all_pending.get("save_pending")
                                        if not (isinstance(_cur_sp, Pending) and _cur_sp.state == PendingState.ERROR and external_value.state != PendingState.ERROR):
                                            draw_state._all_pending["save_pending"] = external_value


                            else:
                                external_value = draw_state._input_value_cache["external_state"][0]
                        if isinstance(external_value, tuple):
                            external_value, thead_launch_frame = external_value

                        if not isinstance(external_value, Pending):
                            if draw_state._show_save or draw_state._apply_save is not None:
                                Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4, force=True)
                                request_render()

                            draw_state._save_pending_obj = None
                            draw_state._show_save = False

                        if isinstance(external_value, Pending):
                            if external_value.state == PendingState.ERROR:
                                # Clear save dialog, the error is tracked via _all_pending only.
                                draw_state._save_pending_obj = None
                                draw_state._show_save = False
                            elif external_value.state == PendingState.CONFIRM:
                                draw_state._save_pending_obj = external_value
                                if not draw_state._show_save:
                                    Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                                    request_render()
                                external_value = external_value.wrapped if isinstance(external_value.wrapped, original_type) else draw_state._raw_input_value
                                draw_state._show_save = True

                        if isinstance(external_value, Pending):
                            draw_state._save_pending_for += 1
                            report_changed = False
                            if draw_state._save_pending_for > 2:
                                draw_pending_status(draw_state, external_value)
                            # Melty.cache.invalidate(draw_state._tile_id, force=True)
                        else:
                            draw_state._save_pending_for = 0
                            prev_hash = Background.simple_hash(draw_state._input_value_cache["external_state"][0])
                            if thead_launch_frame >= draw_state._input_value_cache["external_state"][1]:
                                hash_val = Background.simple_hash(external_value)
                                draw_state._input_value_cache[
                                    "external_state"] = external_value, thead_launch_frame, hash_val
                                if prev_hash != hash_val or draw_state._apply_save is not None:
                                    report_changed = True
                                    report_value = external_value
                                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4, force=True)
                                    # Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
                                    request_render()
                        # if input_conversion_background:
                        #     draw_state._pending_convert = True
                        #     Melty.cache.invalidate_up_by_obj(draw_state._collection)
                        #     Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                        #     Melty.cache.invalidate_up(draw_state._tile_id, force=True)
                        #     request_render()

                else:
                    # Synchronous

                    report_changed = child_changed
                    report_value = new_value_child

                # if isinstance(report_value, Pending):
                #     raise Exception("Pending needs to be handled before saving to cache")
                return_value = (report_changed, report_value, *return_value[2:])

            if use_cache:
                Melty.cache.mark_end_offscreen()

            # if draw_state._save_pending_obj is not None and draw_state._save_pending_obj.originated in auto_apply:
            #     # draw_state._apply_save = draw_state._save_pending_obj.originated
            #     # Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
            #     # Melty.cache.invalidate_up_by_obj(draw_state._raw_input_value, force=True)
            #
            #     request_render()
            #
            # if draw_state._internal_pending is not None and draw_state._internal_pending.originated in auto_apply:
            #     # draw_state._apply_load = draw_state._internal_pending.originated
            #     # Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
            #     # Melty.cache.invalidate_up_by_obj(draw_state._raw_input_value, force=True)
            #
            #     request_render()

            #
            # if draw_state._apply_save is not None:
            #     Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
            #     Melty.cache.invalidate(draw_state._tile_id, force=True)
            #     request_render()
            #
            # if draw_state._apply_load is not None:
            #     Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
            #     Melty.cache.invalidate(draw_state._tile_id, force=True)
            #     request_render()


            if melty_window:
                Melty.pop_clip()
            if fixed_size:
                Melty.fixed_size_stack.pop()
            if draw_state._has_popup:
                is_popup_open = Melty.imgui_popup_open

                if is_popup_open != draw_state._imgui_popover_open and not is_popup_open:
                    Melty.cache.invalidate_up_by_obj(input_value, max_depth=4)
                if is_popup_open:
                    Melty.report_imgui_active()
                draw_state._imgui_popover_open = Melty.imgui_popup_open

            pop_id()
            if closable:
                Melty.melty_window_stack.pop()
            if has_collection:
                Melty.collection_stack.pop()
            Melty.input_value_stack.pop()
            Melty.size_stack.pop()
            Melty.wrap_stack.pop()

            if melty_window and draw_state.width < 30:
                draw_state.width = 30
            if melty_window and draw_state.height < 30:
                draw_state.height = 30

            if not draw_state.kwargs.manual_content_height:
                draw_state.content_height = draw_state._content_rect[1]

            if auto_resize:
                if kwargs.get("wrap", False):
                    if passed_width is None:
                        max_width = kwargs.get("max_width", 1e9)
                        draw_state.width = snap_int(min(item_rect[0], max_width))


                if passed_height is None:
                    max_height = kwargs.get("max_height", 1e9)

                    if not closable:
                        draw_state.height = snap_int(min(item_rect[1], max_height))
                    else:
                        display_height = imgui.get_io().display_size[1]
                        if draw_state.expanded:
                            min_height = content_rect[1] + draw_state.footer_height + draw_state.header_height + 10
                        else:
                            min_height = 0
                        draw_state.height = snap_int(max(min_height, min(item_rect[1], min(display_height, max_height))))

            if (draw_state.width != original_width_b or
                    draw_state.height != original_height_b):
                if (draw_state._collection_draw_state is not None and not imgui.is_mouse_down(0) and not
                imgui.is_mouse_down(1) and not imgui.is_mouse_down(2)):
                    draw_state._collection_draw_state.invalid_content_height = True

                if draw_state._parent is not None:
                    draw_state._parent.invalid_content_height = True

                draw_state.size_change = True
            else:
                if not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2):
                    draw_state.size_change = False

            if draw_state.width > 10000:
                draw_state.width = 10000

            if draw_state.height > 30000:
                draw_state.height = 30000

            column = kwargs.get("column", None)
            if column is not None and column_parent is not None:
                column_parent._column_cursor[column][1] += draw_state.height # This will lag behind a frame, but keeping code tidy instead

            draw_state._hovered = False
            draw_state.hotkey_receiver = False
            draw_state.last_seen = Melty.frame_count

            if kwargs.get("no_cursor", False):
                imgui.set_cursor_screen_pos(no_cursor_reset)
                imgui.dummy(0, 0)

            if is_root:
                style = imgui.get_style()
                style.item_spacing = Melty.original_spacing
                style.window_padding = Melty.original_window_padding
                style.frame_padding = Melty.original_frame_padding

                # if Melty.previous_select is not None:
                #     for prev_select in Melty.previous_select:
                #         Melty.cache.invalidate(prev_select._tile_id)
                #     Melty.previous_select = None

                if Melty.channels_split:
                    draw_list = imgui.get_window_draw_list()
                    Melty.channels_split = False
                    draw_list.channels_merge()

        except Exception as e:
            with trace_group(f"Drawing {func.__name__} {draw_state.name}", hash=draw_state.unique) as g:
                watch = ["draw_state.name", "input_value", "convert_path", "clean_args.input_value", "func.__name__", "mode"]
                print_stack_trace(frames=get_live_frames(), section="UI Thread",
                                  group=g, watch=watch)
                print_stack_trace(exception=e, section="Exception",
                                  group=g, watch=watch)

        finally:

            Melty.active_layer = original_active_layer
            Melty.shadow_depth = start_shadow_depth

            if mode_stacked:
                Melty.mode_stack.pop()

            draw_state.frame_count += 1
            if Melty.imgui_crashed:
                if return_extras:
                    return False, None, draw_state
                return False, None
            if inc_depth:
                Melty.draw_state_stack.pop()

                Melty.depth = Melty.depth - 1
                Melty.unique_stack.pop()

            use_cache = kwargs.get("use_cache", False) and Melty.cache.enabled
            if not use_cache:
                Melty.cache.mark_uncached(draw_state.name, input_value, collection, tile_id, draw_state)

            return_draw_state = draw_state
            if return_value is None:
                child_changed, new_value = False, None
            elif isinstance(return_value, tuple) and len(return_value) == 3:
                child_changed, new_value, return_draw_state = return_value
            elif isinstance(return_value, tuple) and len(return_value) == 2:
                child_changed, new_value = return_value
            elif isinstance(return_value, bool):
                child_changed, new_value = return_value, input_value
            else:
                imgui.text("Unsupported return from render_func")
                child_changed, new_value = False, None

            end_time = time.time()
            draw_state.render_time = end_time - start_time
            style = imgui.get_style()

            if is_root:
                style.item_spacing = Melty.original_spacing
                style.window_padding = Melty.original_window_padding
                style.frame_padding = Melty.original_frame_padding

                if Melty.previous_select is not None:
                    for prev_select in Melty.previous_select:
                        Melty.cache.invalidate(prev_select._tile_id)
                    Melty.previous_select = None

                if Melty.channels_split:
                    draw_list = imgui.get_window_draw_list()
                    Melty.channels_split = False
                    draw_list.channels_merge()

            if return_extras:
                return child_changed, new_value, return_draw_state
            return child_changed, new_value

    def draw_pending_status(draw_state, pending_obj):
        load_icon = f"\uf110"
        draw_list: _DrawList = imgui.get_window_draw_list()
        if pending_obj.state == PendingState.ERROR:
            draw_list.add_text(
                *(draw_state.left + 2,
                  draw_state.top + draw_state.height - draw_state.footer_height - 20),
                imgui.get_color_u32_rgba(1, 0, 0, 1.0),
                f"{load_icon} {pending_obj.status}")
        else:
            draw_list.add_text(
                *(draw_state.left + 2, draw_state.top + draw_state.height - draw_state.footer_height - 20),
                imgui.get_color_u32_rgba(1, 1, 1, 0.5),
                f"{load_icon} {pending_obj.status}")

    def draw_inner_main(clean_args, draw_state, input_value, unique, kwargs):
        return_value = None
        expected_type = param_types[0] if len(param_types) > 0 else None
        annotation_empty = expected_type == inspect.Parameter.empty
        input_value = clean_args.get('input_value', input_value)


        if not annotation_empty:
            if expected_type is not Any and isinstance(expected_type, type):
                if not isinstance(input_value, expected_type):
                    yellow = (1.0, 1.0, 0.0, 1.0)
                    if imgui.button(f"Fix Type##{unique}"):
                        if expected_type == tuple:
                            return True, (0,0,0)
                        return True, expected_type()
                    same_line()
                    type_class_path = f"{expected_type.__module__}.{expected_type.__name__}"
                    actual_type_class_path = f"{type(input_value).__module__}.{type(input_value).__name__}"
                    imgui.text_colored(f"Type mismatch in {func.__name__}\n"
                                       f"Expected {type_class_path}, "
                                       f"got {actual_type_class_path}", *yellow)
                    return False, None

        use_cache = kwargs.get("use_cache", False) and Melty.cache.enabled
        draw_state.use_cache = use_cache
        kwargs.pop("use_cache", None)
        global_toggles = kwargs.get("global_toggles", {})
        if global_toggles.offscreen_debug:
            depth_tint = (Melty.depth * 0.05)
            jet = jet_color(depth_tint)
            floating_text(f"{func.__name__} w:{Melty.depth}", tint=jet)


        clip_height = draw_state.height - draw_state.footer_height - draw_state.header_height
        needs_scroll = draw_state.content_height > clip_height and draw_state.multi_line

        if draw_state.just_shadow or kwargs.get("disable_scroll", False):
            needs_scroll = False

        draw_state.scroll_visible = needs_scroll

        if needs_scroll:

            scroll_y_changed = draw_state.on_action("scroll_y_changed", view_id="view_scroll", priority_delta=10)
            scroll_delta = 0
            if scroll_y_changed is not None:
                scroll_delta = scroll_y_changed.value
                Melty.selected = set()
                Melty.selected.add(draw_state)

            scroll_offset = draw_state.scroll_offset
            current_x = scroll_offset[0]
            current_y = scroll_offset[1]
            direction = -1
            scroll_speed = 250.0
            new_offset_y = current_y + scroll_delta * direction * scroll_speed

            min_scroll_y = 0
            max_scroll_y = max(0, draw_state.content_height - clip_height + 5)

            # Give views time to settle
            if Melty.frame_count > 2:
                if not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2):
                    draw_state.scroll_offset = (current_x,
                                                max(min_scroll_y, min(new_offset_y, max_scroll_y)))

            current_tint = Melty.global_attrs['style_manager'].get_tint()
            if hasattr(input_value, 'tint'):
                current_tint = input_value.tint

            # if not draw_state.closed:
            #     draw_vertical_scrollbar(draw_state.content_height, view_height=draw_state._parent.height,
            #                             view_width=draw_state._parent.width,
            #                             scroll_offset=draw_state.scroll_offset[1], scrollbar_width=5,
            #                             left=draw_state._parent.left,
            #                             top=draw_state._parent.top,
            #                             tint=current_tint),

        do_scroll = needs_scroll

        scroll_offset = draw_state.scroll_offset if do_scroll else (0, 0)

        if do_scroll:
            Melty.push_clip((draw_state.left, draw_state.top + draw_state.header_height + 2,
                             draw_state.left + draw_state.width,
                             draw_state.top + draw_state.header_height + draw_state.height + 2))
            start_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((start_cursor[0],
                                         start_cursor[1] - scroll_offset[1]))

        # If we are using the new callback header, gate rendering behind expanded
        Melty.silence_invalidate = False
        if draw_state.expanded:
            is_primitive = input_value is None or isinstance(input_value,
                                                             (int, float, str, bool, tuple)) and not hasattr(
                input_value, '__dict__')

            if not is_primitive and (id(input_value) in Melty.seen_values and
                                     Melty.seen_values.count(input_value) > 1 or Melty.depth > 15):
                imgui.text("Recursive reference detected: " + str(input_value))
            else:

                if not is_primitive:
                    Melty.seen_values.append(id(input_value))
                #################################################################################################
                return_value = func(**clean_args)
                ################################################################################################
                imgui.set_item_allow_overlap()
                if not is_primitive:
                    Melty.seen_values.pop()

        if do_scroll:
            Melty.pop_clip()
            start_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((start_cursor[0],
                                         start_cursor[1] + scroll_offset[1]))

        return return_value

    return wrapper


def begin_window(unique_id, *args, **kwargs):
    return_val = imgui.begin(unique_id, *args, **kwargs)
    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)
    Melty.channels_split = True
    return return_val


def begin_group(unique_id=0, *args, **kwargs):
    return imgui.begin_group()


def end_group():
    imgui.end_group()


def end_window(unique_id):
    imgui.get_window_draw_list().channels_merge()
    Melty.channels_split = False
    imgui.end()


def push_id(unique_id):
    global id_stack
    if Melty.imgui_crashed:
        return
    imgui.push_id(str(unique_id))
    id_stack.append(unique_id)


def pop_id():
    global id_stack
    if Melty.imgui_crashed:
        return

    imgui.pop_id()
    id_stack.pop()


def tmp_undo_stack(undo_point_id):
    """Context manager to temporarily clear the ID stack."""
    global id_stack
    global stack_holder
    stack_holder[undo_point_id] = id_stack[:]
    for _ in stack_holder[undo_point_id]:
        imgui.pop_id()
    id_stack = []


def redo_stack(undo_point_id):
    """Restore the ID stack to a previously saved state."""
    global id_stack
    global stack_holder
    if undo_point_id in stack_holder:
        saved_stack = stack_holder.pop(undo_point_id)
        for uid in saved_stack:
            imgui.push_id(str(uid))
        id_stack = saved_stack


def get_melty_state():
    global melty_state_registry, static_melty
    return static_melty


def get_draw_state(unique: int) -> DrawState:
    """Get or create a ViewState object for a widget ID."""
    if unique not in Melty.vis.root.draw_state_registry or Melty.vis.root.draw_state_registry[unique] is None:
        Melty.vis.root.draw_state_registry[unique] = DrawState()
        Melty.vis.root.draw_state_registry[unique].unique = unique

    Melty.vis.root.draw_state_registry[unique].dlt_count = Melty.save_draw_state_for
    return Melty.vis.root.draw_state_registry[unique]


def strhash(s: str) -> int:
    """Stable 32-bit hash of a string."""
    return zlib.crc32(s.encode("utf-8")) & 0xffffffff


def combine(h: int, s: str) -> int:
    """Order-sensitive, stable combine (FNV-style)."""
    return ((h * 16777619) ^ strhash(s)) & 0xffffffff


def ui_id(datatype=None, suffix=None, idx=0) -> int:
    """
    Generate a stable UI ID from the call stack + optional metadata.

    - meta: optional Meta object to fold in attribute name/type
    - max_depth: limit to avoid walking the whole interpreter stack
    """
    h = 0
    # frame = sys._getframe(2)  # skip ui_id itself
    code = None
    func_name = ""
    cls_name = ""

    scope = f"{cls_name}.{func_name}" if cls_name else func_name
    h = combine(h, scope)
    datatype = datatype if datatype is not None else Any
    h = combine(h, str(datatype))
    suffix_int = strhash(str(suffix))

    unique = h if suffix is None else (((h * 16777619) ^ suffix_int) + (idx + 1))
    unique = strhash(str(unique))

    return unique


class WrapType(Enum):
    CHILD = 1
    ID = 2
    GROUP = 3
    WINDOW = 4


# Hotkey decorator
def listens_for(hotkey):
    def decorator(func):
        Melty.hotkey_registry[func] = Melty.hotkey_registry.get(func, {})
        if isinstance(hotkey, (list, tuple)):
            for hk in hotkey:
                Melty.hotkey_registry[func][hk.name] = hk
        elif isinstance(hotkey, Hotkey):
            Melty.hotkey_registry[func][hotkey.name] = hotkey
        return func

    return decorator


def annotation_track(*args, wrapper, **kwargs):
    first_arg = args[0] if args else None
    if Melty.annotation_mode:
        # Class decoration mode, with args
        if 'for_type' in kwargs and not isinstance(first_arg, type):
            def class_wrapper(cls):
                inner_args = args[1:]
                return wrapper(cls, *inner_args, **kwargs)

            return class_wrapper

        # Class decoration mode, ie. @render_as_float
        if isinstance(first_arg, type):
            for_type = kwargs.get('for_type', None)
            kwargs.pop('for_type', None)
            kwargs.pop('default_value', None)
            args = args[1:] if len(args) > 1 else ()

            new_meta = Meta()
            for k, v in kwargs.items():
                setattr(new_meta, k, v)
            new_meta.view_function = wrapper
            if for_type is not None:
                first_arg.default_meta_for = getattr(first_arg, 'default_meta_for', {})
                first_arg.default_meta_for[for_type] = new_meta
            else:
                first_arg.meta = new_meta

            return first_arg

        # # View function was used as annotation, ie. some_param: as_float = 0.0
        new_meta = Meta()

        for k, v in kwargs.items():
            setattr(new_meta, k, v)
        new_meta.view_function = wrapper
        return new_meta

    return None


def apply_drag_and_drop():
    ######## -------- apply drag & drop -----------
    did_apply = False
    while len(Melty.actions_to_apply) > 0:
        action = Melty.actions_to_apply.pop(0)
        result = apply_collection_action(action)
        if action.source_collection == action.target_collection:
            Melty.cache.invalidate_up_by_obj(action.source_collection, max_depth=8)
        else:
            Melty.cache.invalidate_up_by_obj(action.source_collection, max_depth=8)
            Melty.cache.invalidate_up_by_obj(action.target_collection, max_depth=8)
        did_apply = True

    Melty.actions_to_apply = []
    if did_apply:
        request_render()


def get_resize_handle(a_ds):
    if a_ds.left is None or a_ds.height is None or a_ds.width is None or a_ds.top is None:
        return (0, 0, 0, 0)
    left = a_ds.left
    top = a_ds.top
    right = left + a_ds.width
    bottom = top + a_ds.height

    right, bottom = Melty.apply_clip((right, bottom))

    margin = 34
    left, top = (right - margin, bottom - margin)
    return left, top, right, bottom


def draw_resize_handle(a_ds):
    if a_ds.width is None:
        return
    if a_ds.height is None:
        return
    if a_ds.left is None:
        return

    rect_br = get_resize_handle(a_ds)
    draw_list = imgui.get_window_draw_list()
    width = rect_br[2] - rect_br[0]
    height = rect_br[3] - rect_br[1]
    right, bottom = (rect_br[2], rect_br[3])
    left, top = (right - width, bottom - height)

    if width <= 0 or height <= 0:
        return

    current_cursor = imgui.get_cursor_screen_pos()
    if a_ds.expanded:
        imgui.set_cursor_screen_pos((left, top))
        imgui.invisible_button(str(a_ds.unique) + "resize_btn", width, height)

    alpha = 0.0
    if imgui.is_mouse_hovering_rect(left, top, right, bottom):
        Melty.blocker_hovered = True
        alpha = 0.5

    # Resizeable corner drag
    arrow_size = 13
    margin = 1
    draw_list.add_triangle_filled(
        right - margin - 1, bottom - arrow_size - margin,
        right - margin - 1, bottom - margin,
        right - margin - 1 - arrow_size, bottom - margin,
        imgui.get_color_u32_rgba(1, 1, 1, alpha)
    )
    # Bottom corner
    if alpha > 0.0:
        Melty.cache.mask_mark_rect(a_ds, Melty.max_depth - 1, a_ds.shadow_depth,
                                   right - arrow_size - margin - 1,
                                   bottom - margin - arrow_size, arrow_size, arrow_size,
                                   key=str(a_ds.unique) + "resize")
    if a_ds.expanded:
        imgui.set_cursor_screen_pos(current_cursor)

    return (left, top, right, bottom)


def jet_color(val: float):
    # jet color function
    four_value = 4.0 * val
    r = min(four_value - 1.5, -four_value + 4.5)
    g = min(four_value - 0.5, -four_value + 3.5)
    b = min(four_value + 0.5, -four_value + 2.5)
    return max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b)), 1.0



