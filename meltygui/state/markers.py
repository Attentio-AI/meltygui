import inspect
import sys
import zlib
from copy import copy, deepcopy
from functools import wraps
from typing import Any

import imgui

from src.lsd.gl_gui.utils.custom_views import print_colored_traceback

class Val:
    def __init__(self, option:Any):
        self.option = option


class Meta:
    """Metadata container for fields."""
    default = None

    def __init__(self, *args, **kwargs):
        self.name = "default_name"
        self.float_min = -1000.0
        self.float_max = 1000.0
        self.float_speed = 0.001
        self.visible_in_ui = True
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_object
        self.view_function = draw_object
        self.value = None
        self.type = Any
        self.__dict__.update(kwargs)

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.get(self.name, self.default)

    def __set__(self, instance, value):
        instance.__dict__[self.name] = value

    def __repr__(self):
        return f"<Meta name={self.name} type={self.type} {self.__dict__}>"

    @classmethod
    def get_new_defaults(cls, value=None, *args, **kwargs):
        """Return the global default Meta object."""
        default = Meta(*args, **kwargs)
        default.value = value
        return default

    @classmethod
    def get_default(cls):
        """Return the global default Meta object."""
        if cls.default is None:
            cls.default = Meta.get_new_defaults()
        return cls.default


def _is_field_candidate(v) -> bool:
    # Treat non-callables that aren't descriptors as "fields"
    return not callable(v) and not isinstance(v, (staticmethod, classmethod, property))


class FieldMeta(type):
    @classmethod
    def __prepare__(mcls, name, bases):
        # any mapping works; order is guaranteed in modern Python
        return {}

    def __setattr__(cls, name, value):
        print(f"Class attribute {name} set to {value!r}")
        super().__setattr__(name, value)

    def __new__(mcls, name, bases, namespace):
        new_namespace = {}
        field_defaults = {}
        field_meta = {}

        if name == "Lora":
            pass

        for key, value in namespace.items():
            if key.startswith("__") and key.endswith("__"):
                new_namespace[key] = value
                continue

            if not _is_field_candidate(value):
                new_namespace[key] = value
                continue

            # marker line
            if isinstance(value, Meta):
                field_defaults[key] = value.value
                field_meta[key] = value
                new_namespace[key] = value.value
                new_namespace[f"{key}_meta"] = value
            else:
                # Plain value still becomes a field
                field_defaults[key] = value
                new_namespace[key] = value

        new_namespace["__field_defaults__"] = field_defaults
        new_namespace["__field_meta__"] = field_meta
        new_namespace["meta"] = field_meta

        cls = super().__new__(mcls, name, bases, new_namespace)
        return cls

    def __call__(cls, *args, **kwargs):
        """
        Make a fresh instance, then backfill any missing attributes
        from __field_defaults__ (deepcopying mutables).
        """
        obj = super().__call__(*args, **kwargs)

        # Only fill attributes not explicitly set by __init__
        for k, v in getattr(cls, "__field_defaults__").items():
            if k not in obj.__dict__:
                setattr(obj, k, v)
        return obj


    def get_field_meta(cls, field_name):
        """Get Meta object for a given field, or default."""
        return getattr(cls, f"{field_name}_meta", Meta.get_default())


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
        meta = kwargs.get("meta", Meta.get_default())
        suffix = kwargs.get("suffix", None)
        unique, depth = ui_id(meta) if meta else (0,0)
        unique = unique if suffix is None else ((unique * 16777619) ^ strhash(str(suffix))) & 0xffffffff
        input_value = kwargs.get("input_value", first_arg)
        draw_state = kwargs.get("draw_state", second_arg)
        if draw_state is None:
            draw_state = get_draw_state(unique)

        if attr_name == "alpha":
            pass
        # Clean up kwargs to only what the method wants
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

        is_window = hasattr(input_value, "__class__") and getattr(input_value.__class__, "__is_window__",
                                                                       False)
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
                    # Derive meta from dict entry
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
                    if isinstance(v, FieldMeta):
                        continue
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

                        item_changed, new_value = child_meta.view_function(input_value=v, meta=child_meta, suffix=k, name=k)
                        changed |= item_changed

                    except Exception as e:
                        print_colored_traceback()
                        pass
            imgui.unindent(indent_size)
        else:
            imgui.push_id(str(unique))
            try:
                # Signature not known, so be safe
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

# disabled = Marker(disabled=True)
# colored_text = Marker(colored_text=True)
# class Car(metaclass=FieldMeta):
#     # class attribute
#     visibleinui
#     wheels: int = 4
#
#     # instance attribute
#     colored_text((1, 0, 0))
#     year: int = 2025
#
#     disabled
#     mileage: int = 0
#
# # c = Car()
# print(c.year)  # 2025
# print(c.mileage)  # 0
# print(Car.wheels)  # 4
#
# # Meta
# print(Car.year.meta)  # {'colored_text': True, 'args': (1, 0, 0)}
# print(Car.mileage.meta)  # {'disabled': True}
# print(Car.wheels.meta)  # {'visibleinui': True}
