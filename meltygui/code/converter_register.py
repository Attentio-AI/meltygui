# file_codecs.py
from __future__ import annotations

import inspect
from typing import Any, Tuple


def converter(converter_fn=None, registry: Any = None):
    print_reset = "\033[0m"
    print_green = "\033[92m"
    print_red = "\033[91m"
    print_bold = "\033[1m"
    print(f"{print_green}Registering converter with registry: {registry}{print_reset}")
    """Class decorator: instantiate + register a Codec. Also mirrors to Melty.ext_to_type if you want."""
    if converter_fn is None:
        return lambda fn: converter(fn, registry)

    # Inspect the converter function's type hints to determine from_type and to_type
    # Use inspect to check input and return types
    func_signature = inspect.signature(converter_fn)
    fn_params = func_signature.parameters
    if len(fn_params) < 1:
        print(f"{print_red}{print_bold}Error: Converter function {converter_fn.__name__} must have at least one param.{print_reset}")
        return converter_fn

    to_type = func_signature.return_annotation
    if to_type is inspect.Signature.empty:
        print(f"{print_red}{print_bold}Warning: Converter function {converter_fn.__name__} has no return type annotation.{print_reset}")
        return converter_fn

    from_param = fn_params.get("value", None)
    from_type = from_param.annotation if from_param else None
    # Get first return if multiple ie. dict | Path
    if func_signature.return_annotation is not None and hasattr(func_signature.return_annotation, "__args__"):
        to_type = func_signature.return_annotation.__args__[0]
        print(f"{print_green}Multiple return types detected, using first: {to_type}{print_reset}")

    if not hasattr(registry, '_converters') or not isinstance(registry._converters, dict):
        setattr(registry, '_converters', {})

    if hasattr(registry, "_converters") and isinstance(registry._converters, dict):
        registry._converters[(from_type, to_type)] = converter_fn
        print(f"Registered converter: {from_type} -> {to_type}")

    return converter_fn