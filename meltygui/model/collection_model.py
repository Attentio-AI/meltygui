"""Collection model functions and supporting definitions."""
from enum import Enum
from meltygui.toggles import Toggles
from types import NoneType


def annotation_item_type(annotation):
    """Item type a collection annotation implies for new entries:
    Dict[str, Lora] -> Lora, List[X] -> X, Optional[T] -> T. None when the
    annotation carries no usable element type."""
    args = [a for a in getattr(annotation, "__args__", ()) if a is not NoneType]
    if not args:
        return None
    return args[-1]


def _collection_match_keys(input_value, keys, excluded, show_excluded):
    """The (index, lowercased key string) pairs draw_collection renders and
    searches, in key order — the basis for both counting key matches and
    resolving which key holds the current match, without rendering. `index` is
    the position in `keys`, so it lines up with the render loop. Mirrors the
    loop's key-string derivation and skip filters."""
    out = []
    parent_cls_name = input_value.__class__.__name__
    excl_attrs = getattr(type(input_value), "__excluded_attrs__", None)
    for idx, key in enumerate(keys):
        if isinstance(key, (float, Enum, NoneType)):
            key_str = parent_cls_name
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)
        if str(key).split("##")[0] in excluded:
            continue
        if (not show_excluded and excl_attrs is not None
                and not Toggles.show_excluded and str(key) in excl_attrs):
            continue
        if not show_excluded and (key_str.startswith("_") or key_str.endswith("_")):
            continue
        out.append((idx, key_str.lower()))
    return out
