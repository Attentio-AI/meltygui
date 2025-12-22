import inspect
import math
import sys
import time
import zlib
from copy import copy
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any

import glfw
import imgui

from src.lsd.gl_gui.view.core_views.core_render_helpers import draw_vertical_scrollbar, floating_text
from src.lsd.gl_gui.model.core_model.draw_state import DrawState, Hotkey, DragMode
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, print_stack_trace, \
    push_style_var, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render
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

        def add_default(value):
            kwargs.pop('is_default_for', None)
            new_meta = Meta()
            new_meta.view_function = wrapper(*args, **kwargs)
            Melty.type_defaults[value] = new_meta

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
    am_a_header = o_kwargs.get("am_a_header", False)

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
            args=[list(kwargs)[0]]
            annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
            if annotation is not None:
                return annotation

        passed_width = kwargs.get('width', None)
        passed_height = kwargs.get('height', None)

        if am_a_header:
            kwargs['am_a_header'] = True

        if kwargs.get('am_a_header', False):
            pass

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
        window_key = f"{name}_window"
        kwargs['return_extras'] = False

        kwargs = o_kwargs | kwargs

        if header_defaults is not None:
            kwargs = header_defaults | kwargs

        if not Melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_split(Melty.max_depth)
            Melty.channels_split = True

        if name == "" and is_root:
            Melty.wrapped_depth = 0
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


        if Melty.depth == 0:
            style = imgui.get_style()
            style.item_spacing = (4, 0)
            style.window_padding = (3, 0)
            style.frame_padding = (4, 1)

        key = kwargs.get("key", None)
        key = key if key is not None else ""
        if name == "" and not is_root:
            name = str(key) + input_value.__class__.__name__

        if Melty.depth > Melty.max_depth:
            if return_extras:
                return False, None, kwargs
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

        root_window_name = Melty.melty_window_stack[-1][4].name if len(Melty.melty_window_stack) > 0 else "Root"
        if is_root:
            unique = ui_id(datatype=type(input_value), suffix=name + unique_name + str(key) + func.__name__)
            suffix = f"{unique_name}_{func.__name__}_{unique}_{key}"
        else:
            unique = ui_id(datatype=type(input_value), suffix=suffix + unique_name + root_window_name + str(key) + func.__name__,
                           idx=index)

        # if active_layer is not None:
        #     unique = kwargs.get("unique", unique)

        computed_unique = unique
        # -------------------------------------------------------------------------

        start_cursor = imgui.get_cursor_screen_pos()
        end_cursor = imgui.get_cursor_screen_pos()

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

        draw_state: DrawState = get_draw_state(unique)
        tile_id = strhash(str(computed_unique) + str(draw_state.id))
        draw_state._tile_id = tile_id

        original_width_b = draw_state.width
        original_height_b = draw_state.height

        if active_layer is None:

            if (melty.dragged_item is not None and melty.drag_in_progress and
                    draw_state is not None and melty.dragged_item.id == draw_state.id):
                kwargs['layer'] = Melty.drag_layer
                kwargs['start_pos'] = imgui.get_cursor_screen_pos()
            else:
                window_z_pos = list(Melty.registered_windows.keys()).index(window_key) \
                    if window_key in Melty.registered_windows else None
                kwargs['layer'] = window_z_pos

            if kwargs.get("layer", None) is not None and len(Melty.layers) > 0:
                layer = kwargs.pop("layer", None)
                if layer >= len(Melty.layers):
                    layer = len(Melty.layers) - 1
                kwargs["active_layer"] = layer
                # kwargs['unique'] = unique
                Melty.layers[layer].append((wrapper, input_value, kwargs, draw_state))
                return_value = (False, None)
                if draw_state.id in Melty.returned_values:
                    return_value = Melty.returned_values.pop(draw_state.id)

                if draw_state.left is not None and draw_state.top is not None:
                    if draw_state.width is not None and draw_state.height is not None:
                        imgui.set_cursor_screen_pos((start_cursor[0],
                                                     start_cursor[1] + draw_state.height))

                collection = kwargs.get("collection", None)
                Melty.cache.mark_uncached(name, input_value, collection, tile_id, draw_state)

                if return_extras:
                    return *return_value, kwargs
                return return_value

        if not draw_state.expanded:
            kwargs.pop("width", None)
            kwargs.pop("height", None)

        # Check untracked object invalidation
        if not hasattr(input_value, "__melty__"):
            if Melty.frame_count > 2 and draw_state.frame_count > 2:
                if isinstance(input_value, (type(None), int, float, str, bool, tuple, set)):
                    if draw_state._input_value != input_value:
                        if kwargs.get("collection", None) is not None:
                            Melty.cache.invalidate(tile_id)
                            request_render()

        collection = kwargs.get("collection", None)
        has_collection = collection is not None and not isinstance(collection, tuple)
        if has_collection:
            Melty.collection_stack.append(collection)

        draw_state._input_value = input_value
        draw_state._collection = Melty.collection_stack[-1] if len(Melty.collection_stack) > 0 else None
        draw_state.name = name
        if not draw_state.expanded:
            kwargs["auto_resize"] = True

        draw_state._has_popup = kwargs.get("has_popup", False)
        draw_state.auto_resize = kwargs.get("auto_resize", True)
        auto_resize = kwargs.get("auto_resize", True)
        kwargs.pop("auto_resize", None)
        if auto_resize:
            if passed_width is not None:
                draw_state.width = passed_width
            if passed_height is not None:
                draw_state.height = snap_int(passed_height)

        Melty.input_value_stack.append(input_value)
        inc_depth = False
        Melty.wrapped_depth = Melty.wrapped_depth + 1
        melty_window = False
        draw_state.tint = kwargs.get("tint", draw_state.tint)

        is_header = "with_header" in func.__name__
        melty_window = kwargs.get("melty_window", False) and not is_header

        previous_tint = None
        style_manager = None
        if melty_window:
            style_manager = Melty.global_attrs.get("style_manager", None)
            previous_tint = style_manager.get_tint()
            if hasattr(input_value, 'tint') and getattr(input_value, "tint") is not None:
                style_manager.set_imgui_tint(*getattr(input_value, "tint"))
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

            is_initial_draw_state = draw_state in Melty.draw_state_stack
            if is_initial_draw_state:
                draw_state._did_use_cache = False

            inc_depth = "draw_state" in wanted_params or is_root
            inc_depth = True

            Melty.unique_stack.append(computed_unique)

            if len(Melty.draw_state_stack) <= Melty.depth:
                Melty.draw_state_stack.append(draw_state)
            else:
                Melty.draw_state_stack[Melty.depth] = draw_state

            Melty.depth = Melty.depth + 1

            kwargs['depth'] = Melty.depth
            draw_state._draggable = kwargs.get("draggable", False)

            if draw_state.get_drag_mode() == DragMode.RESIZE_BR:
                melty.hover_stack.append(unique)

            ############################# WINDOW SETUP #####################################################
            ############ HANDLE WINDOW DRAGGING ########################
            imgui_active = Melty.imgui_active or Melty.imgui_popup_open

            # if draw_state.window_size is not None and not auto_resize and draw_state.expanded:
            #     draw_state.width = draw_state.window_size[0]
            #     draw_state.height = snap_int(draw_state.window_size[1])
            #     draw_state.bounding_width = draw_state.window_size[0]
            #     draw_state.bounding_height = snap_int(draw_state.window_size[1])

            if melty_window:
                # melty_hovered = draw_state.on_action("on_hover", view_id="window_hover", priority_delta=1)
                Melty.melty_window_stack.append((draw_state.window_pos, (draw_state.width, draw_state.height), unique, False, draw_state))
                if draw_state.window_pos is None and draw_state.width is not None:
                    draw_state.window_pos = Melty.init_window_cursor

            if draw_state.width is None and kwargs.get("min_width", None) is not None:
                draw_state.width = kwargs.get("min_width", None)

            if draw_state.width is not None and kwargs.get("min_width", None) is not None:
                draw_state.width = max(draw_state.width, kwargs.get("min_width", None))
                # if draw_state.window_size is not None:
                #     draw_state.window_size = (max(draw_state.window_size[0], kwargs.get("min_width", None)),
                #                                 draw_state.window_size[1])

            if draw_state.height is None and kwargs.get("min_height", None) is not None:
                draw_state.height = kwargs.get("min_height", None)

            if draw_state.height is not None and kwargs.get("min_height", None) is not None:
                draw_state.height = max(draw_state.height, kwargs.get("min_height", None))
                # if draw_state.window_size is not None:
                #     draw_state.window_size = (draw_state.window_size[0], max(draw_state.window_size[1],
                #                                                              kwargs.get("min_height", None)))

            if not auto_resize:
                corner_rect = get_resize_handle(draw_state)

                handle_drag = draw_state.on_action("left_mouse_drag", view_id="window_resize",
                                                   rect=corner_rect, priority_delta=2)

                if melty_window:
                    corner_drag = draw_state.on_action("right_mouse_drag", priority_delta=-2)
                    if handle_drag is None:
                        handle_drag = corner_drag

                # mouse_down = draw_state.on_action("left_mouse_down", view_id="window_resize",
                #                                   rect=corner_rect, priority_delta=2)
                # if mouse_down:
                #     print("Released resize")
                #     draw_state._initial_window_size = None

                if handle_drag and not auto_resize:
                    if draw_state._initial_window_size is None:
                        # if draw_state.window_size is None:
                        #     draw_state.window_size = (draw_state.width, draw_state.height)

                        draw_state._initial_window_size = (draw_state.width, draw_state.height)


                    size_w = draw_state._initial_window_size[0] + handle_drag.total_dx
                    size_h = draw_state._initial_window_size[1] + handle_drag.total_dy
                    draw_state.width, draw_state.height = (max(size_w, 25), snap_int(max(size_h, 24)))
                    min_width = kwargs.get('min_width', 25)
                    min_height = kwargs.get('min_height', 24)
                    draw_state.width = max(draw_state.width, min_width)
                    draw_state.height = max(draw_state.height, min_height)
                    draw_state.expanded = True
                else:
                    draw_state._initial_window_size = None

            if draw_state.window_pos is not None and melty_window:
                on_drag = draw_state.on_action("left_mouse_drag", "window_move")

                if on_drag and not imgui_active:
                    if draw_state._initial_window_pos is None:
                        draw_state._initial_window_pos = (draw_state.window_pos[0],
                                                          draw_state.window_pos[1])

                    pos_x = draw_state._initial_window_pos[0] + on_drag.total_dx
                    pos_y = draw_state._initial_window_pos[1] + on_drag.total_dy
                    draw_state.window_pos = (pos_x, pos_y)
                else:
                    draw_state._initial_window_pos = None

                imgui.set_cursor_screen_pos((snap_int(draw_state.window_pos[0]),
                                             snap_int(draw_state.window_pos[1])))

            kwargs['melty_window'] = False
            Melty.size_stack.append((draw_state.width, draw_state.height))

            if len(Melty.melty_window_stack) > 0:
                Melty.is_melty_window = True
            else:
                Melty.is_melty_window = False

            draw_state.min_width = kwargs.get("min_width", draw_state.min_width)
            draw_state.min_height = kwargs.get("min_height", draw_state.min_height)

            ######################## ERROR HANDLING FOR TYPES ########################
            cursor_pos = imgui.get_cursor_pos()
            imgui.set_cursor_pos((snap_int(cursor_pos[0]), snap_int(cursor_pos[1])))
            # spacing = kwargs.get('spacing', Melty.spacing)
            # padding = kwargs.get('padding', Melty.padding)
            # push_style_var(imgui.STYLE_ITEM_SPACING, spacing)
            # push_style_var(imgui.STYLE_FRAME_PADDING, padding)

            draw_state.left, draw_state.top = imgui.get_cursor_screen_pos()

            draw_state.left = snap_int(draw_state.left)
            draw_state.top = snap_int(draw_state.top)
            draw_state.clip_rect = Melty.get_clip_rect()

            if not is_header and (draw_state.left is not None and draw_state.top is not None and
                    draw_state.width is not None and draw_state.height is not None) and melty_window:
                if (draw_state.width > 0 and draw_state.height > 0):
                    reset_to = imgui.get_cursor_screen_pos()

                    imgui.invisible_button(str(unique) + "window_blocker", width=draw_state.width, height=draw_state.height)
                    imgui.set_cursor_screen_pos(reset_to)
                    imgui.set_item_allow_overlap()


            begin_group(unique)
            push_id(unique)

            start_cursor = imgui.get_cursor_screen_pos()
            start_cursor = (snap_int(start_cursor[0]), snap_int(start_cursor[1]))

            expected_type = param_types[wanted_params.index("input_value")] if "input_value" in wanted_params else None
            annotation_empty = expected_type == inspect.Parameter.empty
            if not annotation_empty:
                if expected_type is not Any and isinstance(expected_type, type):
                    if not isinstance(input_value, expected_type):
                        yellow = (1.0, 1.0, 0.0, 1.0)
                        if imgui.button(f"Fix Type##{unique}"):
                            return True, expected_type()
                        same_line()
                        type_class_path = f"{expected_type.__module__}.{expected_type.__name__}"
                        actual_type_class_path = f"{type(input_value).__module__}.{type(input_value).__name__}"
                        imgui.text_colored(f"Type mismatch in {func.__name__}\n"
                                           f"Expected {type_class_path}, "
                                           f"got {actual_type_class_path}", *yellow)
                        return False, None

            if melty_window and kwargs.get("closable", True):
                if window_key not in Melty.registered_windows:
                    Melty.registered_windows[window_key] = ManagedWindow(input_value=input_value,
                                                                         draw_state=kwargs.get('draw_state', None),
                                                                         window_args=kwargs,
                                                                         name=kwargs.get('name', 'Managed Window'))
                else:
                    Melty.registered_windows[window_key].input_value = input_value
                    Melty.registered_windows[window_key].draw_state = kwargs.get('draw_state', None)
                    Melty.registered_windows[window_key].window_args = kwargs
                    Melty.registered_windows[window_key].name = kwargs.get('name', 'Managed Window')

            if kwargs.get("closable", False):
                if draw_state.closed and not input_value == Melty.registered_windows:
                    if return_extras:
                        return False, None, kwargs
                    return False, None

            if not meta.visible_in_ui:
                if return_extras:
                    return False, None, kwargs
                return False, None
            ###########################################################
            kwargs['next_kwargs'] = kwargs
            if 'kwargs' in wanted_params:
                clean_args = kwargs
            else:
                clean_args = {k: kwargs[k] for k in wanted_params if k in kwargs}
            ###########################################################

            if not auto_resize:
                draw_list = imgui.get_window_draw_list()
                if Melty.channels_split:
                    draw_list.channels_set_current((Melty.max_depth - 1))
                draw_resize_handle(draw_state)
                if Melty.channels_split:
                    draw_list.channels_set_current(Melty.get_channel())
            kwargs['on_drag'] = False
            clip = True
            if (draw_state.left is None or draw_state.top is None or
                    draw_state.width is None or draw_state.height is None):
                clip = False

            ##### Register With event handler #########################
            hover_eligible = draw_state.hover_eligible()
            if hover_eligible:
                max_layer_depth = Melty.max_depth * Melty.max_layer + Melty.max_depth
                layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
                priority = max_layer_depth - layer_and_depth
                event_names = copy(wanted_params)
                Melty.event_handler.register_hovered(tile_id, event_names, priority)

            ######################################################

            if hasattr(input_value, 'pending_upload') and callable(getattr(input_value, 'pending_upload')):
                if input_value.pending_upload():
                    request_render()

            is_hovered = draw_state.on_action("cursor_hover", view_id="test") is not None

            ############# HANDLE SELECTION
            top = kwargs.get("header_top", draw_state.top)
            left = kwargs.get("header_left", draw_state.left)
            width = kwargs.get("header_width", draw_state.width)
            height = kwargs.get("header_height", draw_state.height)

            header_height_diff = draw_state.top - top
            header_width_diff = draw_state.left - left
            c_width = draw_state.width + header_width_diff
            c_height = draw_state.height + header_height_diff

            # width = max(c_width, width)
            height = max(c_height, height)

            draw_state.bg_rect = (left, top, width, height)

            if not is_header:
                click = draw_state.on_action("left_mouse_down")
                if click:
                    Melty.move_window_to_front()
                    previous_select = copy(Melty.selected)

                    if not click.modifiers:
                        Melty.selected = set()
                        Melty.selected.add(draw_state)
                        Melty.last_selected = draw_state
                        Melty.cache.invalidate(tile_id)

                        for prev_select in previous_select:
                            Melty.cache.invalidate(prev_select._tile_id)
                            # Melty.cache.invalidate_up_by_obj(obj=prev_select._collection, recursive=True, max_depth=2)

                    elif click.modifiers == glfw.MOD_CONTROL and click.action == "down":
                        if draw_state in Melty.selected:
                            Melty.selected.remove(draw_state)
                        else:
                            Melty.selected.add(draw_state)

                        Melty.cache.invalidate(tile_id)
                        request_render()
                    elif click.modifiers == glfw.MOD_SHIFT and click.action == "down":
                        new_select = draw_state
                        last_select = Melty.last_selected

                        if draw_state in Melty.selected:
                            Melty.selected.remove(draw_state)
                        else:
                            Melty.selected.add(draw_state)
                        request_render()

            selected = False
            if draw_state in Melty.selected:
                selected = True
                # draw_state.draw_rect(rounding=5.0)

            show_bg = kwargs.get("show_bg", False)
            if show_bg or selected:
                from src.lsd.gl_gui.view.core_views.new_core_view import draw_bg
                style_manager = Melty.global_attrs['style_manager']
                global_style = Melty.global_attrs['global_style']

                Melty.undo_clip(unique, 1)
                _, bg_color = draw_bg(bypass=True, left=left, top=top,
                                      width=width - 1, height=height,
                                      depth=Melty.depth, selected=draw_state in Melty.selected,
                                      global_style=global_style, opacity=1.0 if show_bg else 0.0,
                                      style_manager=style_manager, auto_resize=auto_resize)

                draw_state.bg_color = bg_color
                Melty.redo_clip(unique)
            #### MAIN CALL #######################################################
            if clip:
                cursor_pos = imgui.get_cursor_screen_pos()
                Melty.push_clip((left, top,
                                 left + width,
                                 top + height))

            return_value = draw_inner_main(clean_args, draw_state, input_value, kwargs, melty, tile_id, unique, melty_window)

            if clip:
                Melty.pop_clip()

            #######################
            if imgui.is_item_active() or imgui.is_item_activated():
                Melty.report_imgui_active()
            draw_state._imgui_is_edited = imgui.is_item_edited()
            draw_state._imgui_is_activated = imgui.is_item_activated()
            draw_state._imgui_is_active = imgui.is_item_active()
            draw_state._imgui_is_focused = imgui.is_item_focused()
            draw_state._imgui_is_item_hovered = imgui.is_item_hovered()
            draw_state._imgui_is_hovered = draw_state._imgui_is_item_hovered and is_hovered

            if draw_state._has_popup:
                is_popup_open = Melty.imgui_popup_open
                if is_popup_open != draw_state._imgui_popover_open and not is_popup_open:
                    Melty.cache.invalidate_up_by_obj(input_value, max_depth=6)

                draw_state._imgui_popover_open = Melty.imgui_popup_open
        except Exception as e:
            print_colored_traceback(*sys.exc_info())
        finally:
            draw_state.frame_count += 1
            # pop_style_var(2)
            if Melty.imgui_crashed:
                if return_extras:
                    return False, None, kwargs
                return False, None

            if has_collection:
                Melty.collection_stack.pop()

            is_header = "with_header" in func.__name__

            # Has to go after mouse down check
            # push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
            # push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))

            pop_id()
            if not kwargs.get("imgui_padding", True):
                push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                end_group()
                pop_style_var(2)
            else:
                end_group()

            item_rect = imgui.get_item_rect_size()

            if melty_window and draw_state.width < 30:
                draw_state.width = 30

            if melty_window and draw_state.height < 30:
                draw_state.height = 30

            if auto_resize:
                if passed_width is None:
                    draw_state.width = snap_int(item_rect[0])
                else:
                    draw_state.width = passed_width

                if passed_height is None:
                    draw_state.height = snap_int(item_rect[1])
                else:
                    draw_state.height = passed_height

            if (draw_state.width != original_width_b or
                    draw_state.height != original_height_b):
                if draw_state._parent is not None:
                    draw_state._parent.invalid_content_height = True
                request_render()

            if draw_state.width > 10000:
                draw_state.width = 10000
            if draw_state.height > 10000:
                draw_state.height = 10000

            ################################# SCROLLING
            d_left = draw_state.left
            d_top = draw_state.top
            d_width = draw_state.width
            d_height = draw_state.height
            scrollbar_width = 5.0

            needs_scroll = (draw_state.content_height > draw_state.height or draw_state.scroll_offset[1] > 0) if (
                    draw_state.height is not None) else False
            draw_state.scroll_visible = needs_scroll
            scroll_delta = 0.0

            if needs_scroll:
                scroll_y_changed = draw_state.on_action("scroll_y_changed", view_id="view_scroll", priority_delta=2)

                if scroll_y_changed is not None:
                    scroll_delta = scroll_y_changed.value

                scroll_offset = draw_state.scroll_offset
                current_x = scroll_offset[0]
                current_y = scroll_offset[1]
                direction = -1
                scroll_speed = 150.0
                new_offset_y = current_y + scroll_delta * direction * scroll_speed

                min_scroll_y = 0
                max_scroll_y = max(0, draw_state.content_height - d_height)
                if not imgui.is_mouse_down(0):
                    draw_state.scroll_offset = (current_x,
                                                max(min_scroll_y, min(new_offset_y, max_scroll_y)))

                current_tint = Melty.global_attrs['style_manager'].get_tint()
                if hasattr(input_value, 'tint'):
                    current_tint = input_value.tint

                if not draw_state.closed:

                    draw_vertical_scrollbar(draw_state.content_height, view_height=d_height,
                                            view_width=d_width,
                                            scroll_offset=draw_state.scroll_offset[1], scrollbar_width=scrollbar_width,
                                            left=d_left,
                                            top=d_top,
                                            tint=current_tint),
            # elif not imgui.is_mouse_down(0):
            #     draw_state.scroll_offset = (0, 0)



            ########################################### ACTIONS #######################

            draw_state._hovered = False
            draw_state.hotkey_receiver = False

            if inc_depth:
                Melty.depth = Melty.depth - 1
                Melty.unique_stack.pop()

            Melty.input_value_stack.pop()
            Melty.size_stack.pop()

            if melty_window:
                Melty.melty_window_stack.pop()

            if previous_tint is not None:
                style_manager.set_imgui_tint(*previous_tint)

            if return_value is None:
                changed, new_value = False, None
            elif isinstance(return_value, tuple) and len(return_value) == 2:
                changed, new_value = return_value
            elif isinstance(return_value, bool):
                changed, new_value = return_value, input_value
            else:
                imgui.text("Unsupported return from render_func")
                changed, new_value = False, None

            Melty.wrapped_depth = Melty.wrapped_depth - 1

            end_time = time.time()
            # if not "with_header" in func.__name__:
            draw_state.render_time = end_time - start_time

            if is_root:
                style = imgui.get_style()
                style.item_spacing = Melty.original_spacing
                style.window_padding = Melty.original_window_padding
                style.frame_padding = Melty.original_frame_padding

                if Melty.channels_split:
                    draw_list = imgui.get_window_draw_list()
                    Melty.channels_split = False
                    draw_list.channels_merge()
            # over_header_end_time = time.time()
            #
            # overhead_time = (over_header_end_time - over_header_start_time)
            #

            # imgui.same_line()
            # imgui.begin_group()
            # global_toggles = kwargs.get("global_toggles", {})
            # do_profile = global_toggles.profiler == ProfileMode.ON
            # style_manager = kwargs.get("style_manager", None)
            # global_style = kwargs.get("global_style", None)
            # profile_overhead = global_toggles.profiler == ProfileMode.OVERHEAD
            # if do_profile:
            #     imgui.text(func.__name__)
            #     imgui.same_line()
            #     profile_time = end_time - start_time
            #     from src.lsd.gl_gui.view.core_views.new_core_view import render_profiler_time
            #     render_profiler_time(input_value=profile_time, brief=True,
            #                          style_manager=style_manager, global_style=global_style)
            # elif profile_overhead:
            #     imgui.text(func.__name__)
            #     imgui.same_line()
            #     profile_time = overhead_time
            #     from src.lsd.gl_gui.view.core_views.new_core_view import render_profiler_time
            #     render_profiler_time(input_value=profile_time, brief=True,
            #                          style_manager=style_manager, global_style=global_style)
            # imgui.end_group()
            if return_extras:
                return changed, new_value, kwargs
            return changed, new_value

    def draw_inner_main(clean_args, draw_state, input_value, kwargs, melty, tile_id, unique, melty_window):
        return_value = None
        start_cursor = imgui.get_cursor_screen_pos()
        is_header = "with_header" in func.__name__
        not_header = "with_header" not in func.__name__
        use_cache = kwargs.get("use_cache", False) and Melty.cache.enabled
        kwargs.pop("use_cache", None)
        collection = kwargs.get("collection", None)
        global_toggles = kwargs.get("global_toggles", {})
        if global_toggles.offscreen_debug:
            depth_tint = (Melty.wrapped_depth * 0.05)
            jet = jet_color(depth_tint)
            floating_text(f"{func.__name__} w:{Melty.wrapped_depth}", tint=jet)
        if draw_state._has_popup:
            draw_state._imgui_popover_open = Melty.imgui_popup_open

        if draw_state.width is not None and draw_state.height is not None:
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

                if needs_invalidate:
                    Melty.cache.invalidate(tile_id, force=True)

        last_bounding_hovered = draw_state._bounding_hovered
        new_bounding_hovered = draw_state.is_bounding_hovered()
        hover_changed = last_bounding_hovered != new_bounding_hovered
        draw_state._bounding_hovered = new_bounding_hovered
        if (draw_state.width is None or draw_state.height is None or hover_changed or
                draw_state._bounding_hovered or draw_state._imgui_popover_open):
            if not Melty.on_drag and not Melty.on_scroll:
                Melty.cache.invalidate(tile_id, force=True)

        if use_cache:
            offscreen_depth = Melty.get_channel()
            if Melty.channels_split:
                draw_list = imgui.get_window_draw_list()
                draw_list.channels_set_current(min(offscreen_depth, Melty.max_depth - 1))

        layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
        clip_height = Melty.get_clip_size()[1]

        enable_scroll = kwargs.get("enable_scroll", False)
        draw_state = kwargs.get("draw_state", draw_state)

        do_scroll = enable_scroll and (clip_height < draw_state.content_height or draw_state.scroll_offset[1] > 0) and not_header
        indent_x = kwargs.get("indent_size", 0)
        indent_x = 0

        if is_header:
            draw_state.wrapped_left = start_cursor[0]
            draw_state.wrapped_top = start_cursor[1]
        else:
            spacing = imgui.get_style().item_spacing
            draw_state.header_height = start_cursor[1] - draw_state.wrapped_top - spacing[1]

        scroll_offset = draw_state.scroll_offset if do_scroll else (0, 0)
        if do_scroll or indent_x > 0:
            start_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((start_cursor[0] + indent_x,
                                         start_cursor[1] - scroll_offset[1]))

        if not use_cache or Melty.cache.mark_start_offscreen(input_value=input_value, collection=collection,
                                                             draw_state=draw_state, key=tile_id, name=draw_state.name,
                                                             layer=layer_and_depth, caller=func):

            return_value = func(**clean_args)
            imgui.set_item_allow_overlap()

            draw_state._imgui_scroll_y = imgui.get_scroll_y()

        if not use_cache:
            Melty.cache.mark_uncached(draw_state.name, input_value, collection, tile_id, draw_state)

        if use_cache:
            Melty.cache.mark_end_offscreen()

        if do_scroll or indent_x > 0:
            start_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((start_cursor[0] - indent_x,
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


# def handle_actions(melty, unique, draw_state, func_name):
#
#     if not imgui.is_mouse_down():
#         melty.initial_scroll_offset = (0, 0)
#         melty.initial_drag_offset = None
#         melty.mouse_down_pos = None
#     else:
#         pass
#
#     if (Melty.is_window_enabled() and (imgui.is_window_hovered() or melty.drag_in_progress)) or Melty.blocker_hovered:
#         for m_btn in [0, 1, 2]:
#             btn_state = draw_state.mouse_btn_state[m_btn]
#
#             if btn_state.drag_released:
#                 btn_state.drag_released = False
#                 melty.dragged_item = None
#                 melty.dragged_tile = None
#
#             was_mouse_down = btn_state.mouse_down
#             btn_state._clicked = False
#
#             # Mouse down from glfw
#             global_mouse_down = imgui.is_mouse_down(m_btn)
#             mouse_pos = imgui.get_mouse_pos()
#
#             if btn_state.drag_released:
#                 btn_state.drag_released = False
#
#             if draw_state.id in Melty.hovered_drawstate:
#                 if global_mouse_down and btn_state.mouse_up:
#                     if not btn_state.mouse_down or melty.mouse_down_pos is None:
#                         melty.total_drag_distance = 0.0
#                         melty.total_drag_frames = 0
#                         current_mouse_pos = mouse_pos
#                         btn_state.mouse_down_pos = mouse_pos
#                         btn_state.initial_scroll_offset = Melty.scroll_stack[-1] if len(Melty.scroll_stack) > 0 else (
#                         0, 0)
#                         melty.initial_scroll_offset = Melty.scroll_stack[-1] if len(Melty.scroll_stack) > 0 else (0, 0)
#
#                         melty.mouse_down_pos = mouse_pos
#                         btn_state.initial_screen_pos = (draw_state.left, draw_state.top)
#                         btn_state.initial_window_pos = draw_state.window_pos
#                         btn_state.initial_window_size = (draw_state.width, draw_state.height)
#                         mel_state.drag_mode = draw_state.get_drag_mode()
#
#                         if draw_state.left is not None and draw_state.top is not None:
#                             melty.initial_drag_offset = (current_mouse_pos[0] - draw_state.left,
#                                                           current_mouse_pos[1] - draw_state.top)
#                             melty.mark_event(unique, m_btn, ActionType.DOWN)
#
#                     btn_state.mouse_down = True
#
#                 if not global_mouse_down:
#                     btn_state.mouse_up = True
#             else:
#                 btn_state.mouse_up = True
#             if was_mouse_down and not global_mouse_down:
#                 btn_state._clicked = True
#                 melty.mark_event(unique, m_btn, ActionType.CLICK)
#
#             if not global_mouse_down:
#                 btn_state.mouse_down = False
#                 if btn_state.dragged:
#                     melty.total_drag_distance = 0.0
#                     melty.total_drag_frames = 0
#                     btn_state.drag_released = True
#                     melty.initial_scroll_offset = (0, 0)
#                     btn_state.initial_scroll_offset = None
#                     melty.initial_drag_offset = None
#                     btn_state.initial_screen_pos = None
#                     melty.mouse_down_pos = None
#                     melty.mark_event(unique, m_btn, ActionType.DRAG_UP)
#
#                 btn_state.dragged = False
#
#             if btn_state.mouse_down:
#                 current_mouse_pos = mouse_pos
#                 distance = math.sqrt((current_mouse_pos[0] - btn_state.mouse_down_pos[0]) ** 2 +
#                                      (current_mouse_pos[1] - btn_state.mouse_down_pos[1]) ** 2)
#                 btn_state.drag_delta = (current_mouse_pos[0] - btn_state.mouse_down_pos[0],
#                                         current_mouse_pos[1] - btn_state.mouse_down_pos[1])
#
#                 if melty.last_mouse_pos is not None:
#                     this_m = mouse_pos
#                     last_m = melty.last_mouse_pos
#                     frame_drag_distance = math.sqrt(
#                         (this_m[0] - last_m[0]) ** 2 + (this_m[1] - last_m[1]) ** 2)
#                     melty.total_drag_distance += frame_drag_distance
#                     melty.total_drag_frames += 1
#                 if melty.total_drag_frames >= 2 or btn_state.dragged:
#                     btn_state.dragged = True
#                     melty.drag_in_progress = True
#
#                     if draw_state.window_id is None:
#                         melty.dragged_item = draw_state
#                         melty.dragged_tile = Melty.get_current_tile()
#                     melty.mark_event(unique, m_btn, ActionType.DRAG)
#                     melty.drag_delta = btn_state.drag_delta
#
#         # if draw_state.id in Melty.hovered_drawstate:
#         #     if unique not in melty.triggered_actions:
#         #         melty.mark_event(unique, 0, ActionType.HOVERED)
#
#         # Handle scroll
#         was_scrolled = False
#         if draw_state.height is not None and draw_state.is_hovered():
#             if draw_state.max_height > draw_state.height:
#                 scroll_y = imgui.get_io().mouse_wheel
#                 if scroll_y != 0.0:
#                     melty.mark_event(unique, scroll_y,
#                                      ActionType.SCROLL, value=scroll_y)
#
#         global_mouse_down = glfw.get_mouse_button(Melty.glfw_window, 0) == glfw.PRESS
#         if not global_mouse_down:
#             melty.drag_in_progress = False


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
    if a_ds.left is None:
        return (0, 0, 0, 0)
    left = a_ds.left
    top = a_ds.top
    right = left + a_ds.width
    bottom = top + a_ds.height - 1

    margin = 34
    return (right - margin, bottom - margin, right, bottom)


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

    if width <= 0 or height <= 0:
        return

    current_cursor = imgui.get_cursor_screen_pos()
    if a_ds.expanded:
        imgui.set_cursor_screen_pos((rect_br[0], rect_br[1]))
        imgui.invisible_button(str(a_ds.unique) + "resize_btn", width, height)

    alpha = 0.0
    if imgui.is_mouse_hovering_rect(rect_br[0], rect_br[1], rect_br[2], rect_br[3]):
        Melty.blocker_hovered = True
        alpha = 0.5
    #
    # draw_list.add_rect_filled(rect_br[0], rect_br[1], rect_br[2], rect_br[3],
    #                            imgui.get_color_u32_rgba(0.8, 0.8, 0.2, 0.3))

    # Resizeable corner drag
    arrow_size = 13
    margin = 1
    draw_list.add_triangle_filled(
        rect_br[2] - margin - 1, rect_br[3] - arrow_size - margin,
        rect_br[2] - margin - 1, rect_br[3] - margin,
        rect_br[2] - margin - 1 - arrow_size, rect_br[3] - margin,
        imgui.get_color_u32_rgba(1, 1, 1, alpha)
    )
    # Bottom corner
    if alpha > 0.0:
        Melty.cache.mask_mark_rect(Melty.max_depth - 1,
                                   rect_br[2] - arrow_size - margin - 1,
                                   rect_br[3] - margin - arrow_size, arrow_size, arrow_size,
                                   key=str(a_ds.unique) + "resize")
    if a_ds.expanded:
        imgui.set_cursor_screen_pos(current_cursor)

def jet_color(val: float):
    # jet color function
    four_value = 4.0 * val
    r = min(four_value - 1.5, -four_value + 4.5)
    g = min(four_value - 0.5, -four_value + 3.5)
    b = min(four_value + 0.5, -four_value + 2.5)
    return max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b)), 1.0



