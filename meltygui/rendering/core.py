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

from lsd.gl_gui.view.core_views.core_render_helpers import draw_vertical_scrollbar, floating_text
from src.lsd.gl_gui.model.core_model.draw_state import DrawState, Hotkey, DragMode
from src.lsd.gl_gui.model.core_model.stable_hash import stable_hash
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, print_stack_trace, \
    push_style_var, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.melty import Melty, ActionType, apply_collection_action, MeltyState, DepthState, \
    delete_from_collection, ManagedWindow
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.core_meta import Meta
from src.lsd.gl_gui.view.core_views.decoration.profile_decoration import profile

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
        try:
            wrap_func = None
            if 'wraps' in o_kwargs:
                wrap_func = o_kwargs.get('wraps', None)
                func = wrap_func(func, header_defaults=kwargs, param_defaults=param_defaults, **kwargs)


            out_func = r_func(func, am_a_header=kwargs.get('am_a_header', False), param_types=param_types, wanted_params=wanted_params,
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
    def wrapper(*args, **kwargs):
        if Melty.annotation_mode:
            annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
            if annotation is not None:
                return annotation

        if am_a_header:
            kwargs['am_a_header'] = True

        if kwargs.get('am_a_header', False):
            pass

        start_time = time.time()
        if kwargs.get("bypass", False):
            kwargs.pop("bypass", None)
            return func(*args, **kwargs)
        return_extras = kwargs.get('return_extras', False)
        name = kwargs.get("name", "")
        active_layer = kwargs.get("active_layer", None)

        return_value = None
        is_root = Melty.depth == 0
        first_arg = args[0] if args else None
        input_value = kwargs.get("input_value", first_arg)
        window_key = f"{name}_window"


        kwargs['return_extras'] = False

        kwargs = o_kwargs | kwargs

        if header_defaults is not None:
            kwargs.update(header_defaults)

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

        if is_root:
            unique = ui_id(datatype=type(input_value), suffix=name + unique_name + str(key) + func.__name__)
            suffix = f"{unique_name}_{func.__name__}_{unique}_{key}"
        else:
            unique = ui_id(datatype=type(input_value), suffix=suffix + unique_name + str(key) + func.__name__,
                           idx=index)

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

        def move_window_to_front(possible_window_name):
            window_key = f"{possible_window_name}_window"
            if window_key in Melty.registered_windows:
                # Remove and re-add to move to end (top)
                window = Melty.registered_windows.pop(window_key)
                Melty.registered_windows[window_key] = window

            Melty.cache.invalidate_by_obj(input_value)
            Melty.cache.invalidate_by_obj(Melty.registered_windows)

        if active_layer is None:
            if (melty.dragged_item is not None and melty.drag_in_progress and
                    draw_state is not None and melty.dragged_item.id == draw_state.id):
                kwargs['layer'] = Melty.drag_layer
                kwargs['start_pos'] = imgui.get_cursor_screen_pos()
            else:
                window_z_pos = list(Melty.registered_windows.keys()).index(window_key) \
                    if window_key in Melty.registered_windows else None
                kwargs['layer'] = window_z_pos

        if kwargs.get("layer", None) is not None:
            layer = kwargs.pop("layer", None)
            kwargs["active_layer"] = layer
            Melty.layers[layer].append((wrapper, args, kwargs, draw_state))
            return_value = (False, None)
            if draw_state.id in Melty.returned_values:
                return_value = Melty.returned_values.pop(draw_state.id)

            if draw_state.left is not None and draw_state.top is not None:
                if draw_state.width is not None and draw_state.height is not None:
                    imgui.set_cursor_screen_pos((start_cursor[0],
                                                 start_cursor[1] + draw_state.height))

            if return_extras:
                return *return_value, kwargs
            return return_value

        if not draw_state.expanded:
            kwargs.pop("width", None)
            kwargs.pop("height", None)

        passed_width = kwargs.get('width', None)
        passed_height = kwargs.get('height', None)

        # Check untracked object invalidation
        if not hasattr(input_value, "__melty__"):
            if Melty.frame_count > 2 and draw_state.frame_count > 2:
                if isinstance(input_value, (type(None), int, float, str, bool, tuple, set)):
                    if draw_state._input_value != input_value:
                        if kwargs.get("collection", None) is not None:
                            # print(name, "old value:", draw_state._input_value, "new value:", input_value)
                            # print(name)
                            Melty.cache.invalidate_up_by_obj(kwargs.get("collection", None), name)
                            request_render()

        collection = kwargs.get("collection", None)
        has_collection = collection is not None and not isinstance(collection, tuple)
        if has_collection:
            Melty.collection_stack.append(collection)

        draw_state._input_value = input_value
        draw_state._collection = Melty.collection_stack[-1] if len(Melty.collection_stack) > 0 else None
        draw_state.name = name

        draw_state._has_popup = kwargs.get("has_popup", False)
        draw_state.auto_resize = kwargs.get("auto_resize", False)

        if draw_state.auto_resize:
            if passed_width is not None:
                draw_state.width = passed_width
            if passed_height is not None:
                draw_state.height = passed_height

        # Melty.all_uniques.add(unique)

        is_initial_draw_state = True
        # nested_call = input_value == Melty.input_value_stack[-1] if len(Melty.input_value_stack) > 0 else False
        Melty.input_value_stack.append(input_value)
        inc_depth = False
        Melty.wrapped_depth = Melty.wrapped_depth + 1
        depth_to_restore = None
        wrapped_depth_to_restore = None
        melty_window = False
        scroll_cursor_start = imgui.get_cursor_pos()

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

            ############ LEGACY EVENT HANDLERS ############
            set_default("on_click", melty.check_event(unique, 0, ActionType.CLICK))
            set_default("on_drag", melty.check_event(unique, 0, ActionType.DRAG))
            set_default("on_drag_up", melty.check_event(unique, 0, ActionType.DRAG_UP))
            set_default("on_mouse_down", melty.check_event(unique, 0, ActionType.DOWN))
            set_default("on_hover", melty.check_event(unique, 0, ActionType.HOVERED))

            # if melty.top_event.get(ActionType.SCROLL, None) == unique:
            #     on_scroll = melty.check_event_value(unique, -1, ActionType.SCROLL)
            #     print("on_scroll:", on_scroll, "name:", name)
            #     set_default("on_scroll", on_scroll)
            #     melty.top_event.pop(ActionType.SCROLL, None)
            #     melty.top_event_depth.pop(ActionType.SCROLL, None)

                # needs_scroll = draw_state.content_height > draw_state.height if draw_state.height is not None else False
                # if needs_scroll:
                #     scroll_offset = draw_state.scroll_offset
                #     current_x = scroll_offset[0]
                #     current_y = scroll_offset[1]
                #     direction = -1
                #     scroll_speed = 100.0
                #     new_offset_y = current_y + on_scroll * direction * scroll_speed
                #
                #     min_scroll_y = 0
                #     max_scroll_y = max(0, draw_state.content_height - draw_state.height)
                #     draw_state.scroll_offset = (current_x,
                #                                 max(min_scroll_y, min(new_offset_y, max_scroll_y)))

                    # Melty.cache.invalidate_up_current(max_depth=1)

            # set_default("on_action", melty.triggered_actions.get(unique, None))
            set_default("func", func)
            set_default("render_func", wrapper)

            # if func in Melty.hotkey_registry:
            #     hotkey_actions = Melty.hotkey_registry.get(func, {})
            #     for hk_name, hk in hotkey_actions.items():
            #         if draw_state.hotkey_receiver or (not hk.scoped and Melty.window_hovered):
            #             if hk.is_active() and Melty.is_key_pressed(hk.key):
            #                 kwargs.setdefault(hk_name, True)
            #             else:
            #                 kwargs.setdefault(hk_name, False)
            kwargs.setdefault('meta', meta)

            ###################### END LEGACY EVENT HANDLERS ############

            ########## New event handler system ##########
            unique_events = Melty.events.get(str(tile_id), {})
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
                # Melty.unique_stack[Melty.depth] = computed_unique
                Melty.draw_state_stack[Melty.depth] = draw_state

            Melty.depth = Melty.depth + 1

            requested_z = kwargs.get("z_pos", None)
            if requested_z is not None:
                depth_to_restore = Melty.depth
                wrapped_depth_to_restore = Melty.wrapped_depth
                Melty.depth = requested_z
                relative_change = abs(requested_z - Melty.wrapped_depth)
                Melty.wrapped_depth = Melty.wrapped_depth + relative_change

            kwargs['depth'] = Melty.depth
            draw_state._draggable = kwargs.get("draggable", False)

            if draw_state.get_drag_mode() == DragMode.RESIZE_BR:
                melty.hover_stack.append(unique)

            ############################# WINDOW SETUP #####################################################

            max_layer_depth = Melty.max_depth * Melty.max_layer + Melty.max_depth
            layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
            priority = max_layer_depth - layer_and_depth

            window_drag = False

            window_drag = active_layer == Melty.drag_layer and melty.drag_in_progress

            # if window_drag:
            #     if Melty.depth == 1:
            #         mouse_pos = imgui.get_mouse_pos()
            #         if melty.mouse_down_pos is None:
            #             melty.mouse_down_pos = mouse_pos
            #         mouse_down_x = melty.mouse_down_pos[0]
            #         mouse_down_y = melty.mouse_down_pos[1]
            #         drag_delta = (mouse_pos[0] - mouse_down_x, mouse_pos[1] - mouse_down_y)
            #         current_sx, current_sy = Melty.scroll_stack[-1] if len(Melty.scroll_stack) > 0 else (0, 0)
            #         current_pos = imgui.get_cursor_screen_pos()
            #
            #         pos_x = current_pos[0] + drag_delta[0]
            #         pos_y = current_pos[1] + drag_delta[1]
            #         imgui.set_cursor_screen_pos((pos_x, pos_y))

            # if draw_state.hover_eligible():
            #     max_layer_depth = Melty.max_depth * Melty.max_layer + Melty.max_depth
            #     layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
            #     priority = max_layer_depth - layer_and_depth
            #     Melty.event_handler.on_hovered(f"{tile_id}_window", subscribed=["left_mouse_drag"], priority=priority)


            ############ HANDLE WINDOW DRAGGING ########################
            is_header = "with_header" in func.__name__
            auto_resize = kwargs.get("auto_resize", True)
            melty_window = kwargs.get("melty_window", False) and not is_header

            if melty_window:
                Melty.melty_window_stack.append((draw_state.window_pos, draw_state.window_size, unique))
                if draw_state.window_pos is None and draw_state.width is not None:
                    draw_state.window_pos = Melty.init_window_cursor
            if not auto_resize:


                # corner_drag = draw_state.on_action("right_mouse_drag", priority_delta=-2)
                corner_rect = get_resize_handle(draw_state)
                handle_drag = draw_state.on_action("left_mouse_drag", view_id="window_resize",
                                                   rect=corner_rect, priority_delta=1)
                # if handle_drag is None:
                #     handle_drag = corner_drag

                if handle_drag and not auto_resize:
                    if draw_state.window_size is None:
                        draw_state.window_size = (draw_state.width, draw_state.height)
                    size_w = draw_state.window_size[0] + handle_drag.dx
                    size_h = draw_state.window_size[1] + handle_drag.dy
                    draw_state.width, draw_state.height = (max(size_w, 25), max(size_h, 24))
                    min_width = kwargs.get('min_width', 25)
                    min_height = kwargs.get('min_height', 24)
                    # draw_state.width = max(draw_state.width, min_width)
                    # draw_state.height = max(draw_state.height, min_height)
                    draw_state.window_size = (draw_state.width, draw_state.height)
                    draw_state.expanded = True

            if draw_state.window_pos is not None:
                on_drag = draw_state.on_action("left_mouse_drag")
                if on_drag:
                    print("Dragging window:", name, "to pos:", draw_state.window_pos)
                    pos_x = draw_state.window_pos[0] + on_drag.dx
                    pos_y = draw_state.window_pos[1] + on_drag.dy
                    draw_state.window_pos = (pos_x, pos_y)

                imgui.set_cursor_screen_pos((snap_int(draw_state.window_pos[0]),
                                             snap_int(draw_state.window_pos[1])))






            # # Is melty window or resizable
            # if ((kwargs.get("melty_window", False) or (not kwargs.get("auto_resize", True)) and
            #      draw_state.drag_mode == DragMode.RESIZE_BR)
            #         and kwargs.get("on_drag", False)):
            #     window_drag = True
            #     mouse_pos = imgui.get_mouse_pos()
            #     # kwargs['z_index'] = Melty.depth + 2
            #
            #     mouse_down_x = draw_state.mouse_btn_state[0].mouse_down_pos[0]
            #     mouse_down_y = draw_state.mouse_btn_state[0].mouse_down_pos[1]
            #     drag_delta = (mouse_pos[0] - mouse_down_x, mouse_pos[1] - mouse_down_y)
            #
            #     if draw_state.drag_mode == DragMode.WINDOW and kwargs.get("melty_window", False):
            #         start_pos_x = draw_state.mouse_btn_state[0].initial_window_pos[0]
            #         start_pos_y = draw_state.mouse_btn_state[0].initial_window_pos[1]
            #         pos_x = start_pos_x + drag_delta[0]
            #         pos_y = start_pos_y + drag_delta[1]
            #         draw_state.window_pos = (pos_x, pos_y)
            #         stop_render()
            #     elif draw_state.drag_mode == DragMode.RESIZE_BR:
            #         start_pos_x = draw_state.mouse_btn_state[0].initial_window_size[0]
            #         start_pos_y = draw_state.mouse_btn_state[0].initial_window_size[1]
            #         size_w = start_pos_x + drag_delta[0]
            #         size_h = start_pos_y + drag_delta[1]
            #         draw_state.width, draw_state.height = (max(size_w, 25), max(size_h, 24))
            #         min_width = kwargs.get('min_width', 25)
            #         min_height = kwargs.get('min_height', 24)
            #         draw_state.width = max(draw_state.width, min_width)
            #         draw_state.height = max(draw_state.height, min_height)
            #
            #         draw_state.window_size = (draw_state.width, draw_state.height)
            #         draw_state.expanded = True

            # if kwargs.get("window_pos", None) is not None:
            #     draw_state.window_pos = kwargs.get("window_pos", None)
            #
            # if kwargs.get("melty_window", False):
            #     melty_window = True
            #     Melty.melty_window_stack.append((draw_state.window_pos, draw_state.window_size, unique))
            #     if draw_state.window_pos is None and draw_state.width is not None:
            #         draw_state.window_pos = Melty.init_window_cursor
            #         cursor_spacing = 5
            #         Melty.init_window_cursor = (Melty.init_window_cursor[0] +
            #                                     300 + cursor_spacing,
            #                                     Melty.init_window_cursor[1])
            #
            #     if draw_state.window_pos is not None:
            #         imgui.set_cursor_screen_pos((snap_int(draw_state.window_pos[0]),
            #                                      snap_int(draw_state.window_pos[1])))
            # else:
            #     draw_state.window_pos = None

            kwargs['melty_window'] = False

            Melty.size_stack.append((draw_state.width, draw_state.height))

            if len(Melty.melty_window_stack) > 0:
                Melty.is_melty_window = True
            else:
                Melty.is_melty_window = False

            draw_state.min_width = kwargs.get("min_width", draw_state.min_width)
            draw_state.min_height = kwargs.get("min_height", draw_state.min_height)

            if draw_state.width is None and kwargs.get("min_width", None) is not None:
                draw_state.width = kwargs.get("min_width", None)

            if draw_state.width is not None and kwargs.get("min_width", None) is not None:
                draw_state.width = max(draw_state.width, kwargs.get("min_width", None))
                if draw_state.window_size is not None:
                    draw_state.window_size = (max(draw_state.window_size[0], kwargs.get("min_width", None)),
                                                draw_state.window_size[1])

            if draw_state.height is None and kwargs.get("min_height", None) is not None:
                draw_state.height = kwargs.get("min_height", None)

            if draw_state.height is not None and kwargs.get("min_height", None) is not None:
                draw_state.height = max(draw_state.height, kwargs.get("min_height", None))
                if draw_state.window_size is not None:
                    draw_state.window_size = (draw_state.window_size[0], max(draw_state.window_size[1],
                                                                             kwargs.get("min_height", None)))

            ######################## ERROR HANDLING FOR TYPES ########################

            cursor_pos = imgui.get_cursor_pos()
            imgui.set_cursor_pos((snap_int(cursor_pos[0]), snap_int(cursor_pos[1])))
            spacing = kwargs.get('spacing', Melty.spacing)
            padding = kwargs.get('padding', Melty.padding)
            push_style_var(imgui.STYLE_ITEM_SPACING, spacing)
            push_style_var(imgui.STYLE_FRAME_PADDING, padding)

            if not draw_state.expanded:
                kwargs["auto_resize"] = True


            begin_group(unique)
            push_id(unique)

            start_cursor = imgui.get_cursor_screen_pos()

            if not draw_state.expanded:
                kwargs["auto_resize"] = True

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

            if melty_window:
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

            if not kwargs.get("auto_resize", True):
                draw_list = imgui.get_window_draw_list()
                if Melty.channels_split:
                    draw_list.channels_set_current((Melty.max_depth - 2))
                draw_resize_handle(draw_state)
                if Melty.channels_split:
                    draw_list.channels_set_current(Melty.depth)
            kwargs['on_drag'] = False
            clip = not kwargs.get("auto_resize", True)
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
                # event_names.extend(['hover_event', "scroll_y_changed"])
                not_header = "with_header" not in func.__name__
                clip_height = Melty.get_clip_size()[1]
                enable_scroll = kwargs.get("enable_scroll", False)
                do_scroll = enable_scroll and clip_height < draw_state.content_height and not_header

                if do_scroll:
                    event_names.extend(["scroll_y_changed"])
                # event_names.extend(["left_click_down"])
                Melty.event_handler.register_hovered(str(tile_id), event_names, priority)

            ######################################################

            if hasattr(input_value, 'pending_upload') and callable(getattr(input_value, 'pending_upload')):
                if input_value.pending_upload():
                    request_render()



            #### MAIN CALL #######################################################
            if clip:
                cursor_pos = imgui.get_cursor_screen_pos()
                Melty.push_clip((cursor_pos[0], cursor_pos[1],
                                 cursor_pos[0] + draw_state.width + 1,
                                 cursor_pos[1] + draw_state.height))

            return_value = draw_inner_main(clean_args, draw_state, input_value, kwargs, melty, tile_id, unique)
            if clip:
                Melty.pop_clip()
            #######################


            draw_state._imgui_is_edited = imgui.is_item_edited()
            draw_state._imgui_is_active = imgui.is_item_active()
            draw_state._imgui_is_focused = imgui.is_item_focused()
            draw_state._imgui_is_hovered = imgui.is_item_hovered()

            if draw_state._has_popup:
                is_popup_open = Melty.imgui_popup_open
                if is_popup_open != draw_state._imgui_popover_open and not is_popup_open:
                    Melty.cache.invalidate_up_by_obj(input_value)

                draw_state._imgui_popover_open = Melty.imgui_popup_open
        except Exception as e:
            print_colored_traceback(*sys.exc_info())
        finally:
            draw_state.frame_count += 1
            pop_style_var(2)
            if Melty.imgui_crashed:
                if return_extras:
                    return False, None, kwargs
                return False, None

            if has_collection:
                Melty.collection_stack.pop()

            # Has to go after mouse down check
            push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
            push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
            scroll_cursor_end = imgui.get_cursor_pos()

            pop_id()
            end_group()
            pop_style_var(2)

            # height = scroll_cursor_end[1] - scroll_cursor_start[1]

            # if draw_state.did_render:
            #     content_height = scroll_cursor_end[1] - scroll_cursor_start[1]
            #     draw_state.content_height = (content_height)

            if draw_state.left is not None and draw_state.top is not None:
                if draw_state.width is not None and draw_state.height is not None:
                    imgui.set_cursor_screen_pos((start_cursor[0],
                                                 start_cursor[1] + draw_state.height))
            item_rect = imgui.get_item_rect_size()

            # # draw_state.content_height = item_rect[1]
            #
            #
            # if len(Melty.clip_stack) > 0:
            #     clip_width = Melty.clip_stack[-1][2] - Melty.clip_stack[-1][0]
            #     clip_height = Melty.clip_stack[-1][3] - Melty.clip_stack[-1][1]
            #     item_rect = (min(item_rect[0], clip_width), item_rect[1])

            original_width_b = draw_state.bounding_width
            original_height_b = draw_state.bounding_height
            # if kwargs.get("auto_resize", True):
            #     draw_state.window_size = None

            draw_state.bounds_left = snap_int(start_cursor[0])
            draw_state.bounds_top = snap_int(start_cursor[1])

                # if kwargs.get("auto_resize", True) or draw_state.window_size is not None:
                #     draw_state.width = snap_int(item_rect[0])
                #     draw_state.height = snap_int(item_rect[1])
            if not kwargs.get("auto_resize", True) and draw_state.window_size is not None:
                margin = 250

                display_size = imgui.get_io().display_size
                clamped_size = (
                    min(draw_state.window_size[0], display_size[0]),
                    min(draw_state.window_size[1], display_size[1] - margin)
                )
                draw_state.window_size = clamped_size
                draw_state.width = draw_state.window_size[0]
                draw_state.height = draw_state.window_size[1]
                draw_state.bounding_width = draw_state.window_size[0]
                draw_state.bounding_height = draw_state.window_size[1]

                # if not melty_window and draw_state.content_height > draw_state.height:
                #     if len(Melty.clip_stack) > 0:
                #         clip_width = Melty.clip_stack[-1][2] - Melty.clip_stack[-1][0]
                #         clip_height = Melty.clip_stack[-1][3] - Melty.clip_stack[-1][1]
                #         draw_state.width, draw_state.height = (min(draw_state.window_size[0], clip_width),
                #                                                  min(clip_height, draw_state.window_size[1]))
            else:
                if draw_state.did_render:
                    draw_state.bounding_width = snap_int(item_rect[0])
                draw_state.bounding_height = snap_int(item_rect[1])

                if (draw_state.bounding_width != original_width_b or
                        draw_state.bounding_height != original_height_b):
                    request_render()

                if draw_state.window_size is None and melty_window:
                    window_margin = 8
                    draw_state.window_size = snap_int(item_rect[0]) + window_margin, snap_int(item_rect[1])

                if passed_width is None:
                    draw_state.width = snap_int(item_rect[0])
                else:
                    draw_state.width = kwargs.get("width", draw_state.width)

                if passed_height is None:
                    draw_state.height = snap_int(item_rect[1])
                else:
                    draw_state.height = kwargs.get("height", draw_state.height)

            if draw_state.width > 10000:
                draw_state.width = 10000
            if draw_state.height > 10000:
                draw_state.height = 10000

            ################################# SCROLLING
            # if kwargs.get("enable_scroll", False):
            #     draw_state.content_height = item_rect[1]

            d_left = draw_state.bounds_left
            d_top = draw_state.bounds_top
            d_width = draw_state.width
            d_height = draw_state.height
            scroll_bar_offset = 30
            scrollbar_width = 4.0

            # if kwargs.get("enable_scroll", False):
            clip_height = Melty.get_clip_size()[1]
            needs_scroll = draw_state.content_height > draw_state.height if draw_state.height is not None else False
            draw_state.scroll_visible = needs_scroll

            if needs_scroll:
                draw_vertical_scrollbar(draw_state.content_height, view_height=d_height,
                                        view_width=d_width,
                                        scroll_offset=draw_state.scroll_offset[1], scrollbar_width=scrollbar_width,
                                        left=d_left,
                                        top=d_top)

            ############# HANDLE SELECTION
            max_layer_depth = Melty.max_depth * Melty.max_layer + Melty.max_depth
            layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
            priority = max_layer_depth - layer_and_depth
            if draw_state.hover_eligible():
                Melty.event_handler.register_hovered(str(tile_id), ["left_mouse_down"], priority)
            click = Melty.on("left_mouse_down", tile_id)

            if click:
                previous_select = copy(Melty.selected)
                if not click.modifiers:
                    Melty.selected = set()
                    Melty.selected.add(draw_state)
                    Melty.last_selected = draw_state

                    move_window_to_front(draw_state.name)
                    for prev_select in previous_select:
                        Melty.cache.invalidate(prev_select._tile_id)
                        Melty.cache.invalidate_up_by_obj(obj=prev_select._collection, force=True)
                        Melty.cache.invalidate_up_by_obj(obj=prev_select._input_value, force=True)
                        request_render()

                elif click.modifiers == glfw.MOD_CONTROL and click.action == "down":
                    if draw_state in Melty.selected:
                        Melty.selected.remove(draw_state)
                    else:
                        Melty.selected.add(draw_state)

                    Melty.cache.invalidate(draw_state._tile_id)
                    Melty.cache.invalidate_up_by_obj(obj=draw_state._collection, force=True)
                    request_render()
                elif click.modifiers == glfw.MOD_SHIFT and click.action == "down":
                    new_select = draw_state
                    last_select = Melty.last_selected

                    if draw_state in Melty.selected:
                        Melty.selected.remove(draw_state)
                    else:
                        Melty.selected.add(draw_state)

                    Melty.cache.invalidate(draw_state._tile_id)
                    Melty.cache.invalidate_up_by_obj(obj=draw_state._collection, force=True)
                    request_render()

            if draw_state in Melty.selected:
                # Draw rounded rectangle overlay to show selection
                draw_state.draw_rect(rounding=5.0)

            ########################################### ACTIONS #######################
            is_hovered = draw_state.is_hovered()
            # last_bounding_hovered = draw_state.is_bounding_hovered()
            # hover_changed = last_bounding_hovered != draw_state._bounding_hovered
            # draw_state._bounding_hovered = draw_state.is_bounding_hovered()

            # Melty.triggered_actions.pop(unique, None)
            # handle_actions(melty, unique, draw_state, func)

            draw_state._hovered = False
            draw_state.hotkey_receiver = False
            if is_hovered:
                melty.hover_stack.append(unique)

            if is_hovered and func in Melty.hotkey_registry:
                melty.hotkey_stack.append(unique)

            ###############################################################################
            if depth_to_restore is not None:
                Melty.depth = depth_to_restore
                Melty.wrapped_depth = wrapped_depth_to_restore

            if inc_depth:
                Melty.depth = Melty.depth - 1
                Melty.unique_stack.pop()

            Melty.input_value_stack.pop()
            Melty.size_stack.pop()

            hovered_draw_state = None
            # Root view

            if melty_window:
                Melty.melty_window_stack.pop()

            # if melty_window:
            #     imgui.set_cursor_screen_pos((0, 0))

            # if kwargs.get("on_drag", False):
            #     imgui.set_cursor_screen_pos((draw_state.cursor_left, draw_state.cursor_top))

            # Melty.cache._tile_id += 1

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
            draw_state.render_time = end_time - start_time

            if return_extras:
                return changed, new_value, kwargs
            return changed, new_value

    def draw_inner_main(clean_args, draw_state, input_value, kwargs, melty, tile_id, unique):
        return_value = None
        start_cursor = imgui.get_cursor_screen_pos()
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
                inside_clip = Melty.inside_clip(rect=(start_cursor[0], start_cursor[1],
                                                      draw_state.width, draw_state.height))
                if inside_clip != draw_state.clipped and inside_clip:
                    Melty.cache.invalidate(tile_id, force=True)

                draw_state.clipped = inside_clip
        not_header = "with_header" not in func.__name__
        last_bounding_hovered = draw_state._bounding_hovered
        new_bounding_hovered = draw_state.is_bounding_hovered()
        hover_changed = last_bounding_hovered != new_bounding_hovered
        draw_state._bounding_hovered = new_bounding_hovered
        # last_bounding_hovered = draw_state.is_bounding_hovered()

        if use_cache:
            if (draw_state.width is None or draw_state.height is None or
                               draw_state._hovered or hover_changed or draw_state._bounding_hovered or draw_state._imgui_popover_open):
                Melty.cache.invalidate(tile_id, force=True)

            offscreen_depth = Melty.depth
            if Melty.channels_split:
                draw_list = imgui.get_window_draw_list()
                draw_list.channels_set_current(min(offscreen_depth, Melty.max_depth - 1))

        layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
        clip_height = Melty.get_clip_size()[1]

        enable_scroll = kwargs.get("enable_scroll", False)
        draw_state = kwargs.get("draw_state", draw_state)

        do_scroll = enable_scroll and clip_height < draw_state.content_height and not_header
        indent_x = kwargs.get("indent_size", 0)
        indent_x = 0
        scroll_offset = draw_state.scroll_offset if do_scroll else (0, 0)
        if do_scroll or indent_x > 0:
            start_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((start_cursor[0] + indent_x,
                                         start_cursor[1] - scroll_offset[1]))


        if not use_cache or Melty.cache.mark_start_offscreen(input_value=input_value, collection=collection,
                                                             draw_state=draw_state, key=tile_id, name=draw_state.name,
                                                             layer=layer_and_depth, caller=func):

            cursor_start = imgui.get_cursor_screen_pos()
            return_value = func(**clean_args)
            cursor_end = imgui.get_cursor_screen_pos()
            height = cursor_end[1] - cursor_start[1]
            # draw_state.content_height = height

            if "with_header" not in func.__name__:
                if enable_scroll:
                    if clip_height < height:
                        clean_args.pop("enable_scroll", None)

                        # if Melty.channels_split:
                        #     draw_list = imgui.get_window_draw_list()
                        #     draw_list.channels_set_current(min(Melty.depth + 3, Melty.max_depth - 2))
                        #
                        # draw_vertical_scrollbar(draw_state.content_height, view_height=height,
                        #                         view_width=draw_state.width,
                        #                         scroll_offset=draw_state.scroll_offset[1],
                        #                         border_size=4,
                        #                         left=cursor_start[0],
                        #                         top=cursor_start[1])

                        # if Melty.channels_split:
                        #     draw_list = imgui.get_window_draw_list()
                        #     draw_list.channels_set_current(min(Melty.depth, Melty.max_depth - 2))
                        if str(tile_id) in Melty.events:
                            scroll_y_changed = Melty.events[str(tile_id)].get("scroll_y_changed", None)
                            scroll_delta = 0
                            if scroll_y_changed is not None:
                                scroll_delta = scroll_y_changed.value
                            scroll_offset = draw_state.scroll_offset
                            current_x = scroll_offset[0]
                            current_y = scroll_offset[1]
                            direction = -1
                            scroll_speed = 100.0
                            new_offset_y = current_y + scroll_delta * direction * scroll_speed

                            min_scroll_y = 0
                            max_scroll_y = max(0, draw_state.content_height - clip_height)
                            draw_state.scroll_offset = (current_x,
                                                        max(min_scroll_y, min(new_offset_y, max_scroll_y)))
                    else:
                        draw_state.scroll_offset = (0, 0)

            draw_state._imgui_scroll_y = imgui.get_scroll_y()
            draw_state.did_render = True

        else:
            draw_state.did_render = False
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
            Melty.cache.invalidate_up_by_obj(action.source_collection, max_depth=2)
        else:
            Melty.cache.invalidate_up_by_obj(action.source_collection, max_depth=2)
            Melty.cache.invalidate_up_by_obj(action.target_collection, max_depth=2)
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
    if a_ds.bounds_left is None:
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

    # draw_list.add_rect_filled(rect_br[0], rect_br[1], rect_br[2], rect_br[3],
    #                           imgui.get_color_u32_rgba(0.8, 0.8, 0.2, 0.3))

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



