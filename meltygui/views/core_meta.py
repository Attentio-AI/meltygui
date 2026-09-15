from enum import Enum
from typing import Any

from meltygui.rendering.decorators.core_decoration import Core


class AnnotationOverride:
    """Carrier produced when a ``@render_func`` view function is used as a field
    annotation, e.g. ``alpha: draw_float(min_value=15.0)`` or ``tint: draw_tuple``.

    It just holds the view function and any per-field kwarg overrides captured at
    class-definition time. ``FieldMeta`` extracts these and registers them into
    Melty's ``default_funcs_by_name_type`` / ``default_kwargs_by_attrib_type``
    maps, keyed by (owning class, attribute) — the same place ``@defaults``
    writes. Nothing reads the carrier after that."""

    is_annotation_override = True

    def __init__(self, view_function=None, kwargs=None):
        self.view_function = view_function
        self.kwargs = dict(kwargs or {})

