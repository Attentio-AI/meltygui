"""A tile's editor picker and the selected render function's content."""
from meltygui import imgui
from meltygui.model.tile_model import Tile
from meltygui.view.dropdown_view import draw_dropdown


def draw_tile_content(input_value: Tile, width, height, multi_instance_renderers=(),
                      layout_frame=None):
    # Change the picker height here; the editor receives the remaining tile area.
    picker_height = 28.0
    tile = input_value
    choices = {"Empty": None}
    choices.update({renderer.__name__: renderer for renderer in multi_instance_renderers})
    left, top = imgui.get_cursor_screen_pos()
    selected, renderer = draw_dropdown(
        tile.render_func, collection=choices, name="Editor type", key=tile.id,
        show_name=False, width=width, height=picker_height,
        show_header=False,
        display_label="Empty" if tile.render_func is None else tile.render_func.__name__,
    )
    if selected:
        tile.render_func = renderer

    changed = selected
    if tile.render_func is not None:
        imgui.set_cursor_screen_pos((left, top + picker_height))
        content_changed, value = tile.render_func(
            tile.input_value,
            unique_name=f"{tile.render_func.__module__}.{tile.render_func.__qualname__}",
            width=width,
            height=max(0.0, height - picker_height),
            auto_resize=False, show_header=False,
            key=tile.id, instance=tile.id, layout_frame=layout_frame,
        )
        if content_changed:
            tile.input_value = value
        changed |= content_changed
    return changed, tile
