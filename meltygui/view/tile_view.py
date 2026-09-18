"""A tile's editor picker and the selected render function's content."""
from meltygui import imgui
from meltygui.model.tile_model import Tile
from meltygui.view.dropdown_view import draw_dropdown


def renderer_decoration(renderer):
    """The kwargs a ``@render_func(...)`` decoration stamped on ``renderer``
    (its ``__header_defaults__``), or an empty dict for a plain callable."""
    decoration = getattr(renderer, "__header_defaults__", None)
    return decoration if isinstance(decoration, dict) else {}


def renderer_label(renderer):
    """The picker label for a renderer: its decoration's icon and
    display_name when available, else its function name."""
    decoration = renderer_decoration(renderer)
    name = decoration.get("display_name") or renderer.__name__
    icon = decoration.get("icon")
    return f"{icon} {name}" if icon else name


def renderer_tint(renderer):
    """The renderer's own ``@render_func(tint=...)`` (never the tint a caller
    passes when invoking it), or None."""
    tint = renderer_decoration(renderer).get("tint")
    if isinstance(tint, (tuple, list)) and len(tint) >= 3:
        return tuple(tint)
    return None


def draw_tile_content(input_value: Tile, width, height, multi_instance_renderers=(),
                      layout_frame=None, use_cache=False):
    """The tile's editor picker and its selected renderer. ``use_cache`` puts
    the renderer on the blit cache (the wrapper's mark_start_offscreen /
    mark_end_offscreen around its body): a tile whose view was not invalidated
    draws its captured texture instead of running the renderer."""
    # Change the picker size here; it sits in the tile's bottom-left corner and
    # the editor receives the area above it.
    picker_height = 28.0
    picker_width = 180.0
    tile = input_value
    choices = {"Empty": None}
    row_tints = {}
    for renderer in multi_instance_renderers:
        label = renderer_label(renderer)
        if label in choices:
            label = f"{label} ({renderer.__name__})"
        choices[label] = renderer
        tint = renderer_tint(renderer)
        if tint is not None:
            row_tints[renderer] = tint
    left, top = imgui.get_cursor_screen_pos()
    content_height = max(0.0, height - picker_height)
    imgui.set_cursor_screen_pos((left, top + content_height))
    selected, renderer = draw_dropdown(
        tile.render_func, collection=choices, name="Editor type", key=tile.id,
        show_name=False, width=min(picker_width, width), height=picker_height,
        show_header=False, row_tints=row_tints,
        display_label="Empty" if tile.render_func is None else renderer_label(tile.render_func),
    )
    if selected:
        tile.render_func = renderer

    changed = selected
    if tile.render_func is not None:
        imgui.set_cursor_screen_pos((left, top))
        content_changed, value = tile.render_func(
            tile.input_value,
            unique_name=f"{tile.render_func.__module__}.{tile.render_func.__qualname__}",
            width=width,
            height=content_height,
            auto_resize=False, show_header=False, use_cache=use_cache,
            key=tile.id, instance=tile.id, layout_frame=layout_frame,
        )
        if content_changed:
            tile.input_value = value
        changed |= content_changed
    imgui.set_cursor_screen_pos((left, top + height))
    return changed, tile
