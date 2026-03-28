"""
Converter decorator.  Threads apply and cache_id through all converters.

    @converter(registry=Melty)
    def str_to_module(value: str) -> cst.Module:
        return cst.parse_module(value)

All registered converters accept (value, apply=False, cache_id=None)
regardless of original signature.  The decorator absorbs extra kwargs
for simple converters and passes them through for load/save wrappers.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any


def _fn_accepts(fn, param_name: str) -> bool:
    """Check if fn accepts a given parameter."""
    if getattr(fn, f"_accepts_{param_name}", False):
        return True
    try:
        return param_name in inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return False


def converter(converter_fn=None, registry: Any = None, *,
              from_type: type | None = None,
              to_type: type | None = None,
              load_data=None,
              save_data=None,
              **kwargs):
    if converter_fn is None:
        return lambda fn: converter(
            fn, registry,
            from_type=from_type, to_type=to_type,
            load_data=load_data, save_data=save_data,
            **kwargs
        )

    # ── Infer types from signature ──────────────────────────────────

    func_signature = inspect.signature(converter_fn)
    fn_params = func_signature.parameters

    inferred_to = func_signature.return_annotation
    if inferred_to is inspect.Signature.empty:
        inferred_to = None
    if inferred_to is not None and hasattr(inferred_to, "__args__"):
        inferred_to = inferred_to.__args__[0]

    inferred_from = None
    for param_name in ("value", "data"):
        param = fn_params.get(param_name)
        if param is not None and param.annotation is not inspect.Parameter.empty:
            inferred_from = param.annotation
            break

    actual_from = from_type or inferred_from
    actual_to = to_type or inferred_to

    # ── Wrap with load/save if needed ───────────────────────────────

    wrapped_fn = converter_fn

    if load_data is not None:
        if actual_from is None:
            raise TypeError(
                f"Converter {converter_fn.__name__} with load_data requires from_type=")
        from src.lsd.gl_gui.view.core_conversion.address import make_load_wrapper
        wrapped_fn = make_load_wrapper(converter_fn, load_data, actual_from)

    if save_data is not None:
        if actual_to is None:
            raise TypeError(
                f"Converter {converter_fn.__name__} with save_data requires to_type=")
        from src.lsd.gl_gui.view.core_conversion.address import make_save_wrapper
        load_cache = None
        inverse_fn = kwargs.get("inverse_of")
        if inverse_fn is not None and hasattr(inverse_fn, "_local_cache"):
            load_cache = inverse_fn._local_cache
        wrapped_fn = make_save_wrapper(converter_fn, save_data, actual_to,
                                       load_cache=load_cache)

    # ── Normalize args: all converters accept (value, apply, cache_id) ──

    has_apply = _fn_accepts(wrapped_fn, "apply")
    has_cache_id = _fn_accepts(wrapped_fn, "cache_id")

    if not has_apply or not has_cache_id:
        inner = wrapped_fn

        @functools.wraps(inner)
        def _normalized(value, apply=False, cache_id=None):
            kw = {}
            if has_apply:
                kw["apply"] = apply
            if has_cache_id:
                kw["cache_id"] = cache_id
            return inner(value, **kw)

        _normalized._accepts_apply = True
        _normalized._accepts_cache_id = True
        # Preserve wrapper attributes
        for attr in ("_watched_from_type", "_watched_to_type", "_local_cache"):
            if hasattr(inner, attr):
                setattr(_normalized, attr, getattr(inner, attr))
        wrapped_fn = _normalized

    # ── Register ────────────────────────────────────────────────────

    if not hasattr(registry, '_converters') or not isinstance(registry._converters, dict):
        setattr(registry, '_converters', {})

    if actual_from is not None and actual_to is not None:
        registry._converters[(actual_from, actual_to)] = wrapped_fn

    if hasattr(registry, "_converter_to_type") and isinstance(registry._converter_to_type, dict):
        registry._converter_to_type[wrapped_fn] = (actual_from, actual_to)

    # ── Store flags ─────────────────────────────────────────────────

    if hasattr(registry, 'converter_flags_by_type') and isinstance(registry.converter_flags_by_type, dict):
        if kwargs:
            registry.converter_flags_by_type[(actual_from, actual_to)] = kwargs

    if hasattr(registry, 'converter_flags') and isinstance(registry.converter_flags, dict):
        if kwargs:
            registry.converter_flags[wrapped_fn] = kwargs
            if "inverse_of" in kwargs:
                inverse_fn = kwargs["inverse_of"]
                if inverse_fn not in registry.converter_flags:
                    registry.converter_flags[inverse_fn] = {}
                registry.converter_flags[inverse_fn]["inverse_of"] = wrapped_fn

    if load_data is not None:
        if hasattr(registry, 'converter_flags') and isinstance(registry.converter_flags, dict):
            if wrapped_fn not in registry.converter_flags:
                registry.converter_flags[wrapped_fn] = {}
            registry.converter_flags[wrapped_fn]["stateful"] = True

    return wrapped_fn