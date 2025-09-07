import inspect
import math
import sys
import time
import zlib
from copy import copy
from functools import wraps
from types import NoneType
from typing import Any

import imgui

from src.lsd.gl_gui.model.core_model.new_core_model import DrawState
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, request_render
from src.lsd.gl_gui.melty import Melty, ActionType, apply_collection_action, MeltyState

melty_state_registry = {}
def get_melty_state(unique: int):
    """Get or create a Melty state object for a widget ID."""
    if unique not in melty_state_registry or melty_state_registry[unique] is None:
        melty_state_registry[unique] = MeltyState()

    return melty_state_registry[unique]


def get_draw_state(unique: int) -> DrawState:
    """Get or create a ViewState object for a widget ID."""
    if unique not in Melty.vis.root.draw_state_registry or Melty.vis.root.draw_state_registry[unique] is None:
        Melty.vis.root.draw_state_registry[unique] = DrawState()
        Melty.vis.root.draw_state_registry[unique].unique = unique

    Melty.vis.root.draw_state_registry[unique].delete_countdown = Melty.save_draw_state_for
    return Melty.vis.root.draw_state_registry[unique]

def strhash(s: str) -> int:
    """Stable 32-bit hash of a string."""
    return zlib.crc32(s.encode("utf-8")) & 0xffffffff

def combine(h: int, s: str) -> int:
    """Order-sensitive, stable combine (FNV-style)."""
    return ((h * 16777619) ^ strhash(s)) & 0xffffffff

def ui_id(meta=None, this_name=None, root_function="render", suffix=None) -> int:
    """
    Generate a stable UI ID from the call stack + optional metadata.

    - meta: optional Meta object to fold in attribute name/type
    - max_depth: limit to avoid walking the whole interpreter stack
    """
    h = 0
    frame = sys._getframe(2)  # skip ui_id itself
    code = frame.f_code
    func_name = code.co_name
    cls_name = ""
    if "self" in frame.f_locals:
        cls_name = frame.f_locals["self"].__class__.__name__
    scope = f"{cls_name}.{func_name}" if cls_name else func_name
    h = combine(h, scope)

    if meta is not None:
        if hasattr(meta, "name"):
            h = combine(h, f"{meta.name}:{meta.datatype}")
        else:
            h = combine(h, str(meta.datatype))
    unique = h if suffix is None else ((h * 16777619) ^ strhash(str(suffix))) & 0xffffffff

    return unique, Melty.depth

id_stack = []
stack_holder = {}
def push_id(unique_id):
    global id_stack
    imgui.push_id(str(unique_id))
    id_stack.append(unique_id)

def pop_id():
    global id_stack
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
            inner_func = kwargs.get('inner_func', None)
        else:
            inner_func = r_func


        func = first_arg if callable(first_arg) else None
        # use the specified wrapper if r_func
        wrap_sig = inspect.signature(inner_func)
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
        wrap_defaults = {p: wrap_params[p].default for p in wrap_params if wrap_params[p].default is not inspect.Parameter.empty}

        params = params | wrap_params
        param_types = param_types + wrap_param_types
        name_to_param_type = name_to_param_type | wrap_name_to_param_type
        param_defaults = param_defaults | wrap_defaults

        wanted_params = list(params.keys())

        is_default_for = kwargs.get('is_default_for', None)
        if isinstance(is_default_for, (tuple, list)):
            for a_type in is_default_for:
                if isinstance(a_type, type):
                    kwargs.pop('is_default_for', None)
                    from src.lsd.gl_gui.view.core_views.core_presets import Meta
                    new_meta = Meta(param_defaults)
                    new_meta.view_function = wrapper(*args, **kwargs)
                    for k, v in param_defaults.items():
                        setattr(new_meta, k, v)
                    for k, v in kwargs.items():
                        setattr(new_meta, k, v)
                    Melty.type_defaults[a_type] = new_meta
        elif isinstance(is_default_for, type):
            kwargs.pop('is_default_for', None)
            from src.lsd.gl_gui.view.core_views.core_presets import Meta
            new_meta = Meta(param_defaults)
            new_meta.view_function = wrapper(*args, **kwargs)
            for k, v in param_defaults.items():
                setattr(new_meta, k, v)
            for k, v in kwargs.items():
                setattr(new_meta, k, v)
            Melty.type_defaults[is_default_for] = new_meta
        try:
            wrap_func = None
            if 'wraps' in o_kwargs:
                wrap_func = o_kwargs.get('wraps', None)
                func = wrap_func(func, param_defaults=param_defaults, **kwargs)

            out_func = r_func(func, param_types=param_types, wanted_params=wanted_params,
                              wanted_params_inner=wrap_defaults,
                          param_defaults=param_defaults, name_to_param_type=name_to_param_type,
                          **o_kwargs)

            if wrap_func is not None:
                out_func = wrap_func(out_func, inner_func=r_func, **o_kwargs)

            return out_func
        except Exception as e:
            print_colored_traceback()
            return False, None
    #
    # do_wrap = o_kwargs.pop('wraps', None)
    # if callable(do_wrap):
    # #     wrapper = do_wrap(wrapper, **o_kwargs)
    # wrap_func = None
    # if 'wraps' in o_kwargs:
    #     wrap_func = o_kwargs.pop('wraps', None)
    #     wrapper = wrap_func(inner_func, **o_kwargs)

    return wrapper

def annotation_track(*args, wrapper, **kwargs):
    first_arg = args[0] if args else None
    param_defaults = kwargs.get('param_defaults', None)
    from src.lsd.gl_gui.view.core_views.core_presets import Meta
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

            new_meta = Meta(param_defaults)
            for k, v in param_defaults.items():
                setattr(new_meta, k, v)

            for k, v in kwargs.items():
                setattr(new_meta, k, v)
            new_meta.view_function = wrapper
            if for_type is not None:
                first_arg.default_meta_for = getattr(first_arg, 'default_meta_for', {})
                first_arg.default_meta_for[for_type] = new_meta
            else:
                first_arg.meta = new_meta

            return first_arg

        # View function was used as annotation, ie. some_param: render_float = 0.0
        new_meta = Meta(param_defaults)
        for k, v in param_defaults.items():
            setattr(new_meta, k, v)

        for k, v in kwargs.items():
            setattr(new_meta, k, v)
        new_meta.view_function = wrapper
        return new_meta
    return None

@render_wrapper
def render_func(*args, **o_kwargs):
    func = args[0] if args else None
    param_types = o_kwargs.get("param_types", None)
    wanted_params = o_kwargs.get("wanted_params", None)
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
    def wrapper(*args, **kwargs):
        start_time = time.time()
        if kwargs.get("bypass", False):
            kwargs.pop("bypass", None)
            return func(*args, **kwargs)

        o_kwargs.update(kwargs)
        annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
        if annotation is not None:
            return annotation

        first_arg = args[0] if args else None
        input_value = kwargs.get("input_value", first_arg)
        attr_name = kwargs.get("name", "")
        from src.lsd.gl_gui.view.core_views.core_presets import Meta
        meta = kwargs.get("meta", None)
        if meta is None:
            # Use class meta as default if available
            if hasattr(type(input_value), "meta"):
                meta = getattr(type(input_value), "meta")
            else:
                meta = Meta.get_new_defaults(default_value=input_value)
                for wanted_param in wanted_params:
                    if wanted_param in param_defaults:
                        setattr(meta, wanted_param, param_defaults[wanted_param])

        kwargs["meta"] = meta
        suffix = kwargs.get("suffix", attr_name)
        kwargs["suffix"] = suffix
        unique, depth = ui_id(meta, suffix=suffix) if meta else (0, 0)

        draw_state = get_draw_state(unique)
        meta.draw_state = draw_state
        meta.input_value = input_value
        kwargs["unique"] = unique
        kwargs["depth"] = depth


        is_root = len(Melty.unique_stack) == 0
        if is_root:
            Melty.unique_stack = []
            melty = get_melty_state(unique)
            melty.nearest_drop_distance = melty.max_distance
            melty.nearest_drop_target = None
            melty.nearest_drop_target_tag = None
            request_render()
        else:
            melty = get_melty_state(Melty.unique_stack[0])

        # for kwarg in kwargs:
        #     setattr(meta, kwarg, kwargs[kwarg])

        expected_type = param_types[wanted_params.index("input_value")] if "input_value" in wanted_params else None
        annotation_empty = expected_type == inspect.Parameter.empty

        if not annotation_empty:
            if expected_type is not Any and isinstance(expected_type, type):
                if not isinstance(input_value, expected_type):
                    yellow = (1.0, 1.0, 0.0, 1.0)
                    if imgui.button(f"Fix Type##{unique}"):
                        return True, expected_type()
                    imgui.same_line()
                    imgui.text_colored(f"Type mismatch in {func.__name__}\n"
                                       f"Expected {expected_type.__name__}, "
                                       f"got {type(input_value).__name__}", *yellow)
                    return False, None

        # Clean up kwargs to only what the function wants
        for wanted_param in wanted_params:
            expected_type = name_to_param_type.get(wanted_param, None)
            annotation_empty = expected_type is inspect.Parameter.empty
            found_param = None


            if wanted_param not in kwargs:
                if wanted_param == "meta":
                    found_param = meta
                elif wanted_param == "draw_state":
                    found_param = draw_state
                elif wanted_param == "name":
                    found_param = attr_name
                elif wanted_param == "unique":
                    found_param = unique
                elif wanted_param == "unique":
                    found_param = unique
                elif wanted_param == "on_action":
                    if unique in melty.triggered_actions:
                        found_param = melty.triggered_actions.get(unique)

            if (expected_type is not None and expected_type is not Any and expected_type is not NoneType
                    and not annotation_empty):
                if found_param is not None and not isinstance(found_param, expected_type):
                    found_param = param_defaults.get(wanted_param, None)
            elif wanted_param in vars(meta):
                found_param = getattr(meta, wanted_param)

            if wanted_param == "on_click":
                found_param = melty.check_event(unique, 0, ActionType.CLICK)
            elif wanted_param == "on_drag":
                found_param = melty.check_event(unique, 0, ActionType.DRAG)
            elif wanted_param == "on_drag_up":
                found_param = melty.check_event(unique, 0, ActionType.DRAG_UP)
            elif wanted_param == "on_hover":
                found_param = melty.check_event(unique, 0, ActionType.HOVERED)

            # Try global constants
            if found_param is None and wanted_param in vars(Melty):
                found_param = getattr(Melty, wanted_param)

            if wanted_param == "melty":
                found_param = melty

            if found_param is not None and wanted_param not in kwargs:
                kwargs[wanted_param] = found_param

            if (wanted_param not in kwargs and wanted_param and
                    wanted_param != 'o_args' and wanted_param != 'o_kwargs' and
                    wanted_param != 'args' and wanted_param != 'kwargs'):
                kwargs[wanted_param] = param_defaults.get(wanted_param, None)

        if not meta.visible_in_ui:
            return False, None

        return_value = None
        # from src.lsd.gl_gui.view.core_views.new_core_view import draw_header
        # is_header = func.__name__ == draw_header.__name__
        # if not is_header and kwargs.get("show_header", True):
        #     draw_header(**kwargs)
        start_y_pos = imgui.get_cursor_screen_pos()[1]

        imgui.begin_group()
        push_id(unique)
        Melty.depth = Melty.depth + 1

        if 'next_kwargs' in wanted_params:
            kwargs['next_kwargs'] = kwargs

        Melty.unique_stack.append(unique)
        try:
            clean_args = copy(kwargs)
            to_delete = []
            for to_provide in clean_args.keys():
                if to_provide not in wanted_params:
                    to_delete.append(to_provide)
            for an_arg in to_delete:
                clean_args.pop(an_arg)

            for km, vm in vars(Melty).items():
                if not km.startswith("_"):
                    if km not in kwargs:
                        kwargs[km] = vm
            kwargs['depth'] = depth

            spacing = kwargs.get('spacing', Melty.spacing)
            padding = kwargs.get('padding', Melty.padding)

            imgui.push_style_var(imgui.STYLE_ITEM_SPACING, spacing)
            imgui.push_style_var(imgui.STYLE_FRAME_PADDING, padding)
            return_value = func(**clean_args)
            imgui.pop_style_var(2)
        except Exception as e:
            print_colored_traceback()
        finally:
            # Needs to go after mouse down check
            imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
            imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
            pop_id()
            imgui.end_group()
            imgui.pop_style_var(2)

            Melty.depth = Melty.depth - 1
            # Leave view
            Melty.unique_stack.pop()

            melty.triggered_actions.pop(unique, None)

            for m_btn in [0,1,2]:
                btn_state = draw_state.mouse_btn_state[m_btn]

                if btn_state.drag_released:
                    btn_state.drag_released = False
                    melty.dragged_item = None

                was_mouse_down = btn_state.mouse_down
                btn_state.clicked = False

                if btn_state.drag_released:
                    btn_state.drag_released = False
                if draw_state.hovered and imgui.is_window_hovered():
                    if imgui.is_mouse_down(m_btn) and btn_state.mouse_up:
                        if not btn_state.mouse_down:
                            current_mouse_pos = imgui.get_mouse_pos()
                            btn_state.mouse_down_pos = imgui.get_mouse_pos()
                            btn_state.initial_screen_pos = (draw_state.left, draw_state.top)
                            melty.initial_drag_offset = (current_mouse_pos[0] - draw_state.left,
                                                         current_mouse_pos[1] - draw_state.top)

                        btn_state.mouse_down = True
                        melty.mark_event(unique, m_btn, ActionType.DOWN)
                    if not imgui.is_mouse_down(m_btn):
                        btn_state.mouse_up = True
                else:
                    btn_state.mouse_up = False
                if was_mouse_down and not imgui.is_mouse_down(m_btn):
                    btn_state.clicked = True
                    melty.mark_event(unique, m_btn, ActionType.CLICK)

                if not imgui.is_mouse_down(m_btn):
                    btn_state.mouse_down = False
                    if btn_state.dragged:
                        btn_state.drag_released = True
                        melty.mark_event(unique, m_btn, ActionType.DRAG_UP)

                    btn_state.dragged = False

                if btn_state.mouse_down:
                    current_mouse_pos = imgui.get_mouse_pos()
                    distance = math.sqrt((current_mouse_pos[0] - btn_state.mouse_down_pos[0]) ** 2 +
                                            (current_mouse_pos[1] - btn_state.mouse_down_pos[1]) ** 2)
                    btn_state.drag_delta = (current_mouse_pos[0] - btn_state.mouse_down_pos[0],
                                             current_mouse_pos[1] - btn_state.mouse_down_pos[1])

                    if abs(distance) >= 2 or btn_state.dragged:
                        btn_state.dragged = True
                        melty.drag_in_progress = True
                        melty.dragged_item = draw_state
                        melty.mark_event(unique, m_btn, ActionType.DRAG)

            if draw_state.hovered and imgui.is_window_hovered():
                if unique not in melty.triggered_actions:
                    melty.mark_event(unique, 0, ActionType.HOVERED)

            is_hovered = draw_state.is_hovered()
            draw_state.hovered = False
            if is_hovered:
                melty.hover_stack.append(unique)

            if not imgui.is_mouse_down(0):
                melty.drag_in_progress = False

            hovered_draw_state = None
            # Root view
            if len(Melty.unique_stack) == 0:
                # Did mouse move

                if len(melty.hover_stack) > 0:
                    last = melty.hover_stack[0]
                    hovered_draw_state = Melty.vis.root.draw_state_registry.get(last, None)
                    if hovered_draw_state is not None:
                        hovered_draw_state.hovered = True
                melty.hover_stack = []

                if not melty.nearest_drop_target is None:
                    melty.drag_drop_target = melty.nearest_drop_target
                    melty.drag_drop_target_tag = melty.nearest_drop_target_tag

                ######### apply drag & drop -----------
                while len(melty.actions_to_apply) > 0:
                    action = melty.actions_to_apply.pop(0)
                    action.print()
                    result = apply_collection_action(action)
                    print(result)
                    request_render()

                melty.actions_to_apply = []

            if return_value is None:
                changed, new_value = False, None
            elif isinstance(return_value, tuple) and len(return_value) == 2:
                changed, new_value = return_value
            else:
                imgui.text("Unsupported return from render_func")
                changed, new_value = False, None

            end_time = time.time()
            draw_state.render_time = end_time - start_time

        return changed, new_value

    return wrapper
