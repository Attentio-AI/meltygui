"""Color core functions and supporting definitions."""



def _style_policy_source(owner, field):
    """Match style inheritance, including policy-only (nonpainting) parents."""
    from meltygui.style import default_tint_accumulation
    from meltygui.style import default_scalar_accumulation
    seen = set()
    current = owner
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        style = current.locate_style
        if style is None:
            style = current.locate_tint
        fn = getattr(style, field, None)
        if fn is not None:
            return fn, current
        current = current._parent
    return (default_tint_accumulation if field == "tint_fn" else default_scalar_accumulation), None


def _add_style_policy(owner, field):
    """Stamp the effective policy through locate, like Inputs' + value seed."""
    from meltygui.utils.glfw_utils import request_render

    from meltygui.style import Style
    from meltygui.state.core_undo import UndoManager
    original = owner.locate_style
    attr = "locate_style" if original is not None else "locate_tint"
    if original is None:
        original = owner.locate_tint
    if getattr(original, field, None) is not None:
        return
    fn, _ = _style_policy_source(owner, field)
    style = original if isinstance(original, Style) else Style(
        original or (0, 0, 0), absolute=original is not None)
    metadata = style.__getnewargs_ex__()[1]
    metadata[field] = fn
    updated = Style(tuple(style), **metadata)
    setter = lambda value: setattr(owner, attr, value)
    UndoManager.record(owner, original, updated, setter=setter,
                       key="add_style_policy_" + field, label="Add style policy")
    setter(updated)
    owner.invalidate()
    request_render()
