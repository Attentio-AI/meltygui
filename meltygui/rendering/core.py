import inspect
import sys
import zlib
from copy import copy
from functools import wraps
from typing import Any

import imgui

from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, LSDView, request_render
from src.lsd.gl_gui.melty import Melty


class DrawState:
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self, unique: int):
        self.unique = unique  # stable UI ID
        self.expanded = True
        self.value_cache = None
        self.name = ""
        self.height = None
        self.expanded_height = None
        # add more per-widget stuff as needed

_draw_state_registry = {}

def get_draw_state(unique: int) -> DrawState:
    """Get or create a ViewState object for a widget ID."""
    if unique not in _draw_state_registry:
        _draw_state_registry[unique] = DrawState(unique)
    return _draw_state_registry[unique]

def strhash(s: str) -> int:
    """Stable 32-bit hash of a string."""
    return zlib.crc32(s.encode("utf-8")) & 0xffffffff

def combine(h: int, s: str) -> int:
    """Order-sensitive, stable combine (FNV-style)."""
    return ((h * 16777619) ^ strhash(s)) & 0xffffffff

def ui_id(meta=None, this_name=None, max_depth=20, root_function="render", suffix=None) -> int:
    """
    Generate a stable UI ID from the call stack + optional metadata.

    - meta: optional Meta object to fold in attribute name/type
    - max_depth: limit to avoid walking the whole interpreter stack
    """
    h = 0
    frame = sys._getframe(1)  # skip ui_id itself
    depth = 0
    func_name = ""
    annotation_mode = True
    while frame and depth < max_depth and func_name != root_function:
        code = frame.f_code
        func_name = code.co_name
        cls_name = ""
        if "self" in frame.f_locals:
            cls_name = frame.f_locals["self"].__class__.__name__
        scope = f"{cls_name}.{func_name}" if cls_name else func_name
        h = combine(h, scope)
        frame = frame.f_back
        depth += 1

        if func_name == root_function:
            annotation_mode = False

    if meta is not None:
        if hasattr(meta, "name"):
            h = combine(h, f"{meta.name}:{meta.datatype}")
        else:
            h = combine(h, str(meta.datatype))

    unique = h if suffix is None else ((h * 16777619) ^ strhash(str(suffix))) & 0xffffffff

    return unique, depth, annotation_mode

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

def render_func(*args, **kwargs):
    """
    Decorator for render functions.
    - Computes stable UI ID (unique) from callstack+meta.
    - Provides a per-widget viewstate object (with .unique).
    - Injects meta/viewstate only if the function signature wants them.
    - Pushes/pops ImGui ID scope automatically.
    """

    # Handle default type arguments to @render_func
    first_arg = args[0] if args else None
    if 'is_default_for' in kwargs:
        if not callable(first_arg):
            def class_wrapper(the_func):
                return render_func(the_func, *args, **kwargs)
            return class_wrapper
    # ----- end default type argument handling -----
    func = first_arg if callable(first_arg) else None
    max_depth = 20
    sig = inspect.signature(func)
    params = sig.parameters
    param_types = [params[p].annotation for p in params]
    name_to_param_type = {}
    for idx, param_name in enumerate(params):
        name_to_param_type[param_name] = param_types[idx]

    param_defaults = {p: params[p].default for p in params if params[p].default is not inspect.Parameter.empty}

    wanted_params = list(params.keys())

    # ----- Handle default type argument to @render_func -----
    is_default_for = kwargs.get('is_default_for', None)
    if isinstance(is_default_for, (tuple, list)):
        for a_type in is_default_for:
            if isinstance(a_type, type):
                kwargs.pop('is_default_for', None)
                from src.lsd.gl_gui.view.core_views.core_presets import Meta
                new_meta = Meta(param_defaults)
                new_meta.view_function = render_func(*args, **kwargs, annotation_mode=True)
                for k, v in param_defaults.items():
                    setattr(new_meta, k, v)
                for k, v in kwargs.items():
                    setattr(new_meta, k, v)
                Melty.type_defaults[a_type] = new_meta
    elif isinstance(is_default_for, type):
        kwargs.pop('is_default_for', None)
        from src.lsd.gl_gui.view.core_views.core_presets import Meta
        new_meta = Meta(param_defaults)
        new_meta.view_function = render_func(*args, **kwargs, annotation_mode=True)
        for k, v in param_defaults.items():
            setattr(new_meta, k, v)
        for k, v in kwargs.items():
            setattr(new_meta, k, v)
        Melty.type_defaults[is_default_for] = new_meta

    # ----- end default type argument handling -----

    @wraps(func)
    def wrapper(*args, **kwargs):
        needed_actions = {}
        is_root = len(Melty.unique_stack) == 0
        if is_root:
            Melty.action_stack = {}
            Melty.unique_stack = []


        first_arg = args[0] if args else None
        input_value = kwargs.get("input_value", first_arg)
        second_arg = args[1] if len(args) > 1 else None
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
        unique, depth, annotation_mode = ui_id(meta, max_depth=max_depth, suffix=suffix) if meta else (0, 0)
        from src.lsd.gl_gui.view.core_views.core_presets import Meta
        if 'annotation_mode' in kwargs or annotation_mode:
            # Class decoration mode, no args
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

            # View function was used as annotation, ie. some_param: render_func = 0.0
            new_meta = Meta(param_defaults)
            for k, v in param_defaults.items():
                setattr(new_meta, k, v)

            for k, v in kwargs.items():
                setattr(new_meta, k, v)
            new_meta.view_function = wrapper
            return new_meta

        draw_state = kwargs.get("draw_state", get_draw_state(unique))
        kwargs["draw_state"] = draw_state
        meta.draw_state = draw_state
        meta.input_value = input_value
        kwargs["unique"] = unique
        kwargs["depth"] = depth
        kwargs["max_depth"] = max_depth
        for kwarg in kwargs:
            setattr(meta, kwarg, kwargs[kwarg])

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
            if wanted_param in Melty.actions:
                needed_actions[wanted_param] = Melty.actions[wanted_param]

            if wanted_param in Melty.triggered_actions:
                triggered = Melty.triggered_actions.get(wanted_param, 0)
                if unique == triggered:
                    found_param = True
                else:
                    found_param = False

            if wanted_param not in kwargs:
                if wanted_param == "meta":
                    found_param = meta
                elif wanted_param == "draw_state":
                    found_param = draw_state
                elif wanted_param == "name":
                    found_param = attr_name
                elif wanted_param == "unique":
                    found_param = unique

            if expected_type is not None and expected_type is not Any and not annotation_empty:
                if found_param is not None and not isinstance(found_param, expected_type):
                    found_param = param_defaults.get(wanted_param, None)
            elif wanted_param in vars(meta):
                found_param = getattr(meta, wanted_param)

            # Try global constants
            if found_param is None and wanted_param in vars(Melty):
                found_param = getattr(Melty, wanted_param)

            if found_param is not None:
                kwargs[wanted_param] = found_param

        if not meta.visible_in_ui:
            return False, None

        return_value = None
        # from src.lsd.gl_gui.view.core_views.new_core_view import draw_header
        # is_header = func.__name__ == draw_header.__name__
        # if not is_header and kwargs.get("show_header", True):
        #     draw_header(**kwargs)
        imgui.begin_group()
        push_id(unique)

        Melty.unique_stack.append(unique)
        try:
            clean_args = copy(kwargs)
            to_delete = []
            for to_provide in clean_args.keys():
                if to_provide not in wanted_params:
                    to_delete.append(to_provide)
            for an_arg in to_delete:
                clean_args.pop(an_arg)

            from src.lsd.gl_gui.view.core_views.new_core_view import draw_with_func

            for km, vm in vars(Melty).items():
                if not km.startswith("_"):
                    if km not in kwargs:
                        kwargs[km] = vm
            kwargs['depth'] = depth
            return_value = draw_with_func(func=func, clean_args=clean_args, **kwargs)

        except Exception as e:
            print_colored_traceback()
        finally:


            # Handle actions
            for name, action in needed_actions.items():
                if action.re_arm_condition(unique):
                    Melty.cleared_actions.remove(name) if name in Melty.cleared_actions else None
                    Melty.triggered_actions.pop(name, None)

                if unique not in Melty.cleared_actions:
                    if action.trigger_condition(unique):
                        action_stack = Melty.action_stack.get(name, [])
                        action_stack.append(unique)
                        Melty.action_stack[name] = action_stack

            if return_value is None:
                changed, new_value = False, None
            elif isinstance(return_value, tuple) and len(return_value) == 2:
                changed, new_value = return_value
            else:
                imgui.text("Unsupported return from render_func")
                changed, new_value = False, None
            Melty.unique_stack.pop()

            if len(Melty.unique_stack) == 0:
                for action in Melty.actions.keys():
                    action_stack = Melty.action_stack.get(action, [])
                    if len(action_stack) > 0:
                        last = action_stack[-1]
                        trigger_action_for = None
                        if Melty.actions[action].clear_condition(last):
                            Melty.cleared_actions.add(action)
                            Melty.triggered_actions.pop(action, None)
                        if action not in Melty.cleared_actions:
                            trigger_action_for = last
                        if trigger_action_for is not None:
                            Melty.triggered_actions[action] = last
                            request_render()

                Melty.action_stack = {}

                Melty.last_triggered_actions = copy(Melty.triggered_actions)

            pop_id()
            imgui.end_group()

        return changed, new_value

    return wrapper
