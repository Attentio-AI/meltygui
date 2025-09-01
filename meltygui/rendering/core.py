import inspect
import sys
import zlib
from functools import wraps

import imgui

from src.lsd.gl_gui.utils.custom_views import print_colored_traceback

class DrawState:
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self, unique: int):
        self.unique = unique  # stable UI ID
        self.expanded = True
        self.value_cache = None
        self.name = ""
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

def ui_id(meta=None, this_name=None, max_depth=10, root_function="render") -> int:
    """
    Generate a stable UI ID from the call stack + optional metadata.

    - meta: optional Meta object to fold in attribute name/type
    - max_depth: limit to avoid walking the whole interpreter stack
    """
    h = 0
    frame = sys._getframe(1)  # skip ui_id itself
    depth = 0
    func_name = ""
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

    if meta is not None:
        if hasattr(meta, "name"):
            h = combine(h, f"{meta.name}:{meta.type}")
        else:
            h = combine(h, str(meta))

    return h, depth

def render_func(func):
    """
    Decorator for render functions.
    - Computes stable UI ID (unique) from callstack+meta.
    - Provides a per-widget viewstate object (with .unique).
    - Injects meta/viewstate only if the function signature wants them.
    - Pushes/pops ImGui ID scope automatically.
    """
    sig = inspect.signature(func)
    params = sig.parameters
    wanted_params = list(params.keys())
    max_depth = 10

    @wraps(func)
    def wrapper(*args, **kwargs):
        first_arg = args[0] if args else None
        second_arg = args[1] if len(args) > 1 else None
        attr_name = kwargs.get("name", "")
        from src.lsd.gl_gui.view.core_views.core_presets import Meta
        meta = kwargs.get("meta", Meta.get_default())
        suffix = kwargs.get("suffix", None)
        unique, depth = ui_id(meta) if meta else (0, 0)
        unique = unique if suffix is None else ((unique * 16777619) ^ strhash(str(suffix))) & 0xffffffff
        input_value = kwargs.get("input_value", first_arg)
        draw_state = kwargs.get("draw_state", second_arg)
        if draw_state is None:
            draw_state = get_draw_state(unique)

        if attr_name == "alpha":
            pass
        # Clean up kwargs to only what the function wants
        for wanted_param in wanted_params:
            if wanted_param not in kwargs:
                if wanted_param == "meta":
                    kwargs[wanted_param] = meta
                elif wanted_param == "draw_state":
                    kwargs[wanted_param] = draw_state
                elif wanted_param == "input_value":
                    kwargs[wanted_param] = input_value
                elif wanted_param == "suffix":
                    kwargs[wanted_param] = suffix
                else:
                    kwargs[wanted_param] = None

        to_delete = []
        for to_provide in kwargs.keys():
            if to_provide not in wanted_params:
                to_delete.append(to_provide)
        for an_arg in to_delete:
            kwargs.pop(an_arg)

        if not meta.visible_in_ui:
            return False, None

        class_meta = type(input_value).meta if hasattr(type(input_value), "meta") else None
        if class_meta is not None:
            is_window = class_meta.is_window
        else:
            is_window = False

        if is_window:
            title = attr_name or input_value.__class__.__name__
            opened, _ = imgui.begin(f"{title}##window_{str(unique)}", True)

        imgui.text_colored(f"{attr_name}", *(0.8, 0.3, 0.5, 1.0))
        imgui.same_line()
        imgui.text_colored(f"({type(input_value).__name__})", *(0.8, 0.0, 0.5, 1.0))
        imgui.same_line()
        imgui.text_colored(f"({str(unique)})", *(0.8, 0.0, 0.5, 1.0))
        indent_size = 10
        changed, new_value = False, None
        is_collection = isinstance(input_value, (dict, list, tuple, set)) or (
                hasattr(input_value, "__dict__") and depth < max_depth)
        if is_collection:
            imgui.indent(indent_size)
            # Handle collections
            if isinstance(input_value, dict):
                changed = False
                for k, v in input_value.items():
                    # Derive Meta for each entry
                    item_changed, new_value = meta.view_function(input_value=v, meta=meta, suffix=k, name=k)
                    changed |= item_changed
            elif isinstance(input_value, (list, tuple, set)):
                changed = False
                for i, v in enumerate(input_value):
                    item_changed, new_value = meta.view_function(input_value=v, meta=meta, suffix=i, name=str(i))
                    changed |= item_changed
            elif hasattr(input_value, "__dict__") and depth < max_depth:  # class or module instance
                changed = False
                for k, v in vars(input_value).items():
                    if k == "alpha":
                        pass
                    # skip private attrs, methods, etc.
                    if (k.startswith("__") and k.endswith("__")) or k.startswith("_"):
                        continue
                    try:
                        child_meta = type(input_value).get_field_meta(field_name=k) if (
                            hasattr(type(input_value), "get_field_meta")) else Meta.get_default()
                        if child_meta is not None:
                            kwargs['meta'] = child_meta

                        item_changed, new_value = child_meta.view_function(input_value=v, meta=child_meta, suffix=k,
                                                                           name=k)
                        changed |= item_changed

                    except Exception as e:
                        print_colored_traceback()
                        pass
            imgui.unindent(indent_size)
        else:
            imgui.push_id(str(unique))
            try:
                # Signature not known, must be safe
                return_value = func(**kwargs)
                if return_value is None:
                    return False, None
                elif isinstance(return_value, tuple) and len(return_value) == 2:
                    return return_value
                else:
                    imgui.text("Unsupported return from render_func")
                    return False, None
            except Exception as e:
                print_colored_traceback()
            finally:
                imgui.pop_id()

        if is_window:
            imgui.end()

        return changed, new_value

    return wrapper
