"""The codec registries: which codec class loads, edits and saves a value of
a type (``type_to_codec``) or a file with an extension (``extension_to_codec``).

The codecs themselves live in new_codecs.py, which registers each class here
with ``@register_codec`` when it is imported — with the rest of the code stack
(libcst). This module is light, so the render path (core_render's
_codec_for_type) can consult the registries on the first frame without
loading that stack: until it loads, no codec is registered.
"""
extension_to_codec = {}
type_to_codec = {}


def file_icon_for_path(path):
    """Extension-only codec glyph for file lists and tabs; never read file data."""
    from pathlib import Path
    # File browsers also work before any editor has initialized the codecs.
    from meltygui.code import new_codecs

    codec = extension_to_codec.get(Path(path).suffix.lower())
    return codec.icon_for_path(path) if codec is not None else None


def codec_for_type(value_type):
    """The codec class registered for `value_type` or a base of it (MRO
    walk), or None."""
    for base in value_type.__mro__:
        codec = type_to_codec.get(base)
        if codec is not None:
            return codec
    return None


def file_badge_for_path(path):
    """Codec-owned (extension label, accent RGB, mark) without file I/O."""
    from pathlib import Path
    from meltygui.code import new_codecs

    codec = extension_to_codec.get(Path(path).suffix.lower())
    return codec.badge_for_path(path) if codec is not None else None
