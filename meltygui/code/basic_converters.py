"""
Usage examples for the Melty converter system + auto-chaining.

This file includes a minimal registry and @converter decorator so
everything runs standalone.  Replace with your real implementation.
"""

from types import SimpleNamespace, ModuleType
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from enum import Enum
from typing import Any

from meltygui.runtime import Melty
from meltygui.code.libcst_conversion import register
from meltygui.code.path_finder import convert
from meltygui.code.path_finder import explain_chain
from meltygui.code.path_finder import all_paths
from meltygui.code.path_finder import all_reachable_from
from meltygui.code.path_finder import T


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Define a subset of converters (enough to show interesting chains)        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@register
def int_to_str(value: int) -> str:
    return str(value)


@register
def str_to_int(value: str) -> int:
    return int(value.strip())


@register
def float_to_str(value: float) -> str:
    return str(value)


@register
def str_to_float(value: str) -> float:
    return float(value.strip().replace(",", ""))


@register
def int_to_float(value: int) -> float:
    return float(value)


@register
def float_to_int(value: float) -> int:
    return int(value)


@register
def float_to_decimal(value: float) -> Decimal:
    return Decimal(str(value))


@register
def decimal_to_float(value: Decimal) -> float:
    return float(value)


@register
def str_to_decimal(value: str) -> Decimal:
    return Decimal(value.strip())


@register
def decimal_to_str(value: Decimal) -> str:
    return str(value)


@register
def float_to_fraction(value: float) -> Fraction:
    return Fraction(value).limit_denominator()


@register
def fraction_to_float(value: Fraction) -> float:
    return float(value)


@register
def str_to_bytes(value: str) -> bytes:
    return value.encode("utf-8")


@register
def bytes_to_str(value: bytes) -> str:
    return value.decode("utf-8")


@register
def int_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value)


@register
def datetime_to_int(value: datetime) -> int:
    return int(value.timestamp())


@register
def datetime_to_str(value: datetime) -> str:
    return value.isoformat()


@register
def str_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


@register
def int_to_timedelta(value: int) -> timedelta:
    return timedelta(seconds=value)


@register
def timedelta_to_int(value: timedelta) -> int:
    return int(value.total_seconds())


@register
def dict_to_str(value: dict) -> str:
    import json
    return json.dumps(value, indent=2, default=str)


@register
def str_to_dict(value: str) -> dict:
    import json
    return json.loads(value)


@register
def list_to_dict(value: list) -> dict:
    return dict(value)


@register
def dict_to_list(value: dict) -> list:
    return [list(pair) for pair in value.items()]


@register
def dict_to_namespace(value: dict) -> SimpleNamespace:
    return SimpleNamespace(**value)


@register
def namespace_to_dict(value: SimpleNamespace) -> dict:
    return vars(value).copy()


@register
def list_to_tuple(value: list) -> tuple:
    return tuple(value)


@register
def tuple_to_list(value: tuple) -> list:
    return list(value)


@register
def list_to_set(value: list) -> set:
    return set(value)


@register
def set_to_list(value: set) -> list:
    return sorted(value, key=repr)


@register
def exception_to_dict(value: Exception) -> dict:
    return {
        "__type__": type(value).__name__,
        "__str__": str(value),
        "args": [repr(a) for a in value.args],
    }


"""
Reversible object ↔ dict converters.

object_to_dict puts actual attributes as top-level dict keys so they
display naturally in a UI.  Reconstruction metadata is tucked under
a single "__meta__" key that the UI filters out.

dict_to_object tries to instantiate the real class, falling back to
SimpleNamespace if the class can't be found or constructed.
"""

import sys
from types import SimpleNamespace


def _serialize_value(v):
    """Store the actual value when possible, fall back to a tagged repr."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        converted = [_serialize_value(x) for x in v]
        if isinstance(v, tuple):
            return {"__type__": "tuple", "__items__": converted}
        if isinstance(v, set):
            return {"__type__": "set", "__items__": converted}
        if isinstance(v, frozenset):
            return {"__type__": "frozenset", "__items__": converted}
        return converted
    if isinstance(v, dict):
        return {"__type__": "dict", "__items__": {str(k): _serialize_value(val) for k, val in v.items()}}
    return {"__type__": "repr", "__repr__": repr(v), "__class__": type(v).__qualname__}


def _deserialize_value(v):
    """Reverse of _serialize_value."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, list):
        return [_deserialize_value(x) for x in v]
    if isinstance(v, dict):
        tag = v.get("__type__")
        if tag == "tuple":
            return tuple(_deserialize_value(x) for x in v["__items__"])
        if tag == "set":
            return set(_deserialize_value(x) for x in v["__items__"])
        if tag == "frozenset":
            return frozenset(_deserialize_value(x) for x in v["__items__"])
        if tag == "dict":
            return {k: _deserialize_value(val) for k, val in v["__items__"].items()}
        if tag == "repr":
            try:
                return eval(v["__repr__"])  # noqa: S307
            except Exception:
                return v["__repr__"]
        return {k: _deserialize_value(val) for k, val in v.items()}
    return v


@register
def object_to_dict(value: object) -> dict:
    """Generic object → dict with attributes as top-level keys.

    The dict looks like:
        {
            "color": "red",
            "size": 42,
            "items": [1, 2, 3],
            "__meta__": {"__type__": "Widget", "__module__": "my_app.models"}
        }

    The UI sees color, size, items.  __meta__ is filtered out by
    the dunder convention but carries everything needed for dict_to_object.
    """
    cls = type(value)
    result = {}

    # Value types (int, float, str, bool, bytearray, bytes, etc.) don't
    # store their payload in __dict__ or __slots__ - it lives in the
    # C struct.  Capture it explicitly so the round-trip preserves it.
    _VALUE_TYPES = (int, float, str, bool, bytes, bytearray, complex)
    if isinstance(value, _VALUE_TYPES):
        result["value"] = value

    # Hoist __dict__ attributes to top level
    if hasattr(value, "__dict__"):
        for k, v in value.__dict__.items():
            if k.startswith("__") and k.endswith("__"):
                continue
            result[k] = _serialize_value(v)

    # Hoist __slots__ attributes to top level
    slots = set()
    for klass in cls.__mro__:
        slots.update(getattr(klass, "__slots__", ()))
    for s in sorted(slots):
        if s.startswith("__") and s.endswith("__"):
            continue
        try:
            result[s] = _serialize_value(getattr(value, s))
        except AttributeError:
            pass

    # Tuck reconstruction metadata under a single dunder key
    result["__meta__"] = {
        "__type__": cls.__qualname__,
        "__module__": cls.__module__,
    }

    return result


@register
def dict_to_object(value: dict) -> object:
    """Reconstruct an object from an object_to_dict snapshot.

    Resolution strategy:
      1. Look up the real class from __meta__.__module__ + __meta__.__type__
      2. Instantiate with __new__ (bypassing __init__) and set attributes
      3. Fall back to SimpleNamespace if the class can't be found
    """
    meta = value.get("__meta__", {})
    type_name = meta.get("__type__", "Unknown")
    module_name = meta.get("__module__", "")

    # Everything except dunders is an attribute to restore
    attrs = {k: _deserialize_value(v) for k, v in value.items()
             if not (k.startswith("__") and k.endswith("__"))}

    # Try to find and instantiate the real class
    cls = _resolve_class(module_name, type_name)
    if cls is not None:
        try:
            # Value types (int, float, str, etc.) need the value passed
            # to the constructor - you can't instantiate them via __new__
            if "value" in attrs and issubclass(cls, (int, float, str, bool, bytes, bytearray, complex)):
                obj = cls(attrs.pop("value"))
                # Subclasses of builtins might still have other attrs
                for k, v in attrs.items():
                    setattr(obj, k, v)
                return obj

            obj = cls.__new__(cls)
            for k, v in attrs.items():
                setattr(obj, k, v)
            return obj
        except Exception:
            pass

    # Fallback
    ns = SimpleNamespace(**attrs)
    ns.__original_type__ = type_name
    ns.__original_module__ = module_name
    return ns


def _resolve_class(module_name: str, qualname: str):
    """Try to find a class by module + qualname, handling nested classes."""
    mod = sys.modules.get(module_name)
    if mod is None:
        return None
    obj = mod
    for part in qualname.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj if isinstance(obj, type) else None

#
# @register
# def float_to_dict(value: float) -> dict:
#     return {
#         "value": value,
#     }
#
# @register
# def dict_to_float(value: dict) -> float:
#     return float(value["value"])


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Import the chaining system                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# Assuming chaining.py is in the same directory or on the path.
# Adjust the import to match your setup.


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Examples                                                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
# if __name__ == "__main__":
#
#     print(f"Registry: {Melty}")
#
#     # ── 1. Direct conversions (single hop) ───────────────────────────
#
#     section("Direct conversions")
#
#     print(f"  int → str:       {convert(42, str, registry=Melty)!r}")
#     print(f"  str → float:     {convert('3.14', float, registry=Melty)!r}")
#     print(f"  float → Decimal: {convert(0.1, Decimal, registry=Melty)!r}")
#     print(f"  int → datetime:  {convert(0, datetime, registry=Melty)!r}")
#
#     # ── 2. Auto-chained conversions (multi-hop) ─────────────────────
#
#     section("Auto-chained conversions")
#
#     # int → float → Decimal   (no direct int→Decimal registered)
#     result = convert(42, Decimal, registry=Melty)
#     print(f"  int → Decimal:   {result!r}  (type: {type(result).__name__})")
#     print(f"    chain: {explain_chain(int, Decimal, registry=Melty)}")
#
#     # int → float → Fraction
#     result = convert(3, Fraction, registry=Melty)
#     print(f"  int → Fraction:  {result!r}")
#     print(f"    chain: {explain_chain(int, Fraction, registry=Melty)}")
#
#     # int → str → bytes
#     result = convert(12345, bytes, registry=Melty)
#     print(f"  int → bytes:     {result!r}")
#     print(f"    chain: {explain_chain(int, bytes, registry=Melty)}")
#
#     # int → str → dict (via JSON - "42" isn't valid JSON, but timestamps are fun)
#     # Let's do datetime → str → dict instead
#     # Actually: int → datetime → str
#     result = convert(1700000000, str, registry=Melty)
#     print(f"  int → str:       {result!r}  (direct)")
#
#     # ── 3. Longer chains ─────────────────────────────────────────────
#
#     section("Longer chains")
#
#     # dict → str → bytes
#     data = {"name": "Alice", "balance": 100}
#     result = convert(data, bytes, registry=Melty)
#     print(f"  dict → bytes:    {result[:50]!r}...")
#     print(f"    chain: {explain_chain(dict, bytes, registry=Melty)}")
#
#     # dict → SimpleNamespace (direct)
#     ns = convert({"x": 1, "y": 2}, SimpleNamespace, registry=Melty)
#     print(f"  dict → NS:       {ns!r}")
#
#     # dict → list → tuple
#     result = convert({"a": 1, "b": 2}, tuple, registry=Melty)
#     print(f"  dict → tuple:    {result!r}")
#     print(f"    chain: {explain_chain(dict, tuple, registry=Melty)}")
#
#     # tuple → list → set
#     result = convert((3, 1, 4, 1, 5, 9), set, registry=Melty)
#     print(f"  tuple → set:     {result!r}")
#     print(f"    chain: {explain_chain(tuple, set, registry=Melty)}")
#
#     # ── 4. Subclass resolution ───────────────────────────────────────
#
#     section("Subclass resolution")
#
#     # ValueError is a subclass of Exception - should hit Exception→dict
#     err = ValueError("something broke")
#     result = convert(err, dict, registry=Melty)
#     print(f"  ValueError → dict: {result}")
#     print(f"    chain: {explain_chain(ValueError, dict, registry=Melty)}")
#
#     # And longer chain down: ValueError → dict → str
#     result = convert(err, str, registry=Melty)
#     print(f"  ValueError → str:  {result[:60]!r}...")
#     print(f"    chain: {explain_chain(ValueError, str, registry=Melty)}")
#
#
#     # A custom type hits object→dict via MRO
#     class Widget:
#         def __init__(self, color, size):
#             self.color = color
#             self.size = size
#
#
#     w = Widget("red", 42)
#     result = convert(w, dict, registry=Melty)
#     print(f"  Widget → dict:     {result}")
#     print(f"    chain: {explain_chain(Widget, dict, registry=Melty)}")
#
#     # Widget → dict → str
#     result = convert(w, str, registry=Melty)
#     print(f"  Widget → str:      {result[:60]!r}...")
#
#     # Widget → dict → SimpleNamespace
#     result = convert(w, SimpleNamespace, registry=Melty)
#     print(f"  Widget → NS:       {result!r}")
#     print(f"    chain: {explain_chain(Widget, SimpleNamespace, registry=Melty)}")
#
#     # ── 5. Round-trip demonstration ──────────────────────────────────
#
#     section("Round-trips (showing lossy-ness)")
#
#     original = 3.14159
#     as_fraction = convert(original, Fraction, registry=Melty)
#     back = convert(as_fraction, float, registry=Melty)
#     print(f"  float → Fraction → float:  {original} → {as_fraction} → {back}")
#     print(f"    lost precision: {original != back}")
#
#     original_dt = datetime(2024, 6, 15, 12, 30, 45)
#     as_int = convert(original_dt, int, registry=Melty)
#     back_dt = convert(as_int, datetime, registry=Melty)
#     print(f"  datetime → int → datetime: {original_dt} → {as_int} → {back_dt}")
#     print(f"    lost subsecond: {original_dt != back_dt}")
#
#     # ── 6. Introspection tools ───────────────────────────────────────
#
#     section("Introspection: all_reachable_from(int)")
#
#     reachable = all_reachable_from(int, registry=Melty)
#     print(f"  int can reach {len(reachable)} types:")
#     for t in sorted(reachable, key=lambda t: t.__name__):
#         print(f"    → {t.__name__}")
#
#     section("Introspection: all_paths(int, bytes)")
#
#     paths = all_paths(int, bytes, registry=Melty)
#     for i, p in enumerate(paths):
#         names = " → ".join(t.__name__ for t in p)
#         print(f"  path {i + 1} ({len(p) - 1} hops): {names}")
#
#     section("Introspection: all_paths(tuple, set)")
#
#     paths = all_paths(tuple, set, registry=Melty)
#     for i, p in enumerate(paths):
#         names = " → ".join(t.__name__ for t in p)
#         print(f"  path {i + 1} ({len(p) - 1} hops): {names}")
#
#     # ── 7. Error case ────────────────────────────────────────────────
#
#     section("Error: no path exists")
#
#     try:
#         convert(42, ModuleType, registry=Melty)
#     except TypeError as e:
#         print(f"  {e}")
#
#     print()