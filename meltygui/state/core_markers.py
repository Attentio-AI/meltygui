from enum import Enum
from typing import Any

from meltygui.core.rendering.core_decoration import Core


def _is_field_candidate(v) -> bool:
    # Treat non-callables that aren't descriptors as "fields"
    return not callable(v) and not isinstance(v, (staticmethod, classmethod, property))


def _extract_override(annotation, value):
    """Turn a field annotation into ``(view_function, override_kwargs)`` so it
    can be registered in Melty's default maps, or return ``None`` if it carries
    no override.

    Supported annotation shapes (all resolved at class-definition time):
      * ``field: draw_float(min_value=15.0)`` — already an ``AnnotationOverride``.
      * ``field: draw_tuple``               — a bare ``@render_func`` view fn.
      * ``field: no_render``                — a ``@meta_preset`` (returns kwargs).
      * ``field: render_with(fn)``          — a meta_preset already called (dict).
    """
    if annotation is None:
        return None

    # Carrier produced by a @render_func used in annotation position.
    if getattr(annotation, 'is_annotation_override', False):
        return annotation.view_function, dict(annotation.kwargs)

    # Bare @render_func view function: route this attribute to it with no kwargs.
    if getattr(annotation, '__render_func__', False):
        return annotation, {}

    # Bare @meta_preset (e.g. no_render): calling it yields a kwargs dict.
    if getattr(annotation, '__meta_preset__', False):
        try:
            annotation = annotation()
        except Exception:
            return None

    # A meta_preset result: a plain kwargs dict, possibly carrying view_function.
    if isinstance(annotation, dict):
        ov_kwargs = dict(annotation)
        # meta_preset fills *args/**kwargs params with junk for varargs presets.
        ov_kwargs.pop('args', None)
        ov_kwargs.pop('kwargs', None)
        view_function = ov_kwargs.pop('view_function', None)
        return view_function, ov_kwargs

    return None


class FieldMeta(type):

    @classmethod
    def __prepare__(mcls, name, bases):
        # any mapping works; order is guaranteed in modern Python
        return {}

    def __setattr__(cls, name, value):
        # print(f"Class attribute {name} set to {value!r}")
        super().__setattr__(name, value)

    def __new__(mcls, name, bases, namespace):
        new_namespace = {}
        field_defaults = {}
        # key -> (view_function|None, override_kwargs); registered into Melty's
        # default maps below, once the class object exists to key them by.
        pending_overrides = {}
        annotations = namespace.get("__annotations__", {})



        for key, value in namespace.items():
            if key.startswith("__") and key.endswith("__"):
                new_namespace[key] = value
                continue

            if not _is_field_candidate(value):
                new_namespace[key] = value
                continue

            field_defaults[key] = value
            new_namespace[key] = value

            override = _extract_override(annotations.get(key, None), value)
            if override is not None:
                pending_overrides[key] = override

        new_namespace["__field_defaults__"] = field_defaults

        cls = super().__new__(mcls, name, bases, new_namespace)

        # Store per-attribute annotation overrides in the same Melty maps that
        # @defaults and type defaults use, keyed by (class, attribute).
        for key, (view_function, ov_kwargs) in pending_overrides.items():
            if view_function is not None:
                Core.melty.default_funcs_by_name_type[cls][key] = view_function
            for ov_key, ov_val in ov_kwargs.items():
                Core.melty.default_kwargs_by_attrib_type[cls][key][ov_key] = ov_val

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
