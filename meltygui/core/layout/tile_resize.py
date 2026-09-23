"""Layout-owned replay of existing child caches during tile resize.

No textures live here: records borrow DrawStates whose resident images and
release-time invalidation belong to TileCacheMasked.
"""
from dataclasses import dataclass, field
import inspect


def renderer_version(renderer):
    return (renderer, getattr(renderer, '__code__', None),
            getattr(inspect.unwrap(renderer), '__code__', None))


@dataclass
class TileResizeRecord:
    version: tuple
    input_value: object
    rect: tuple
    body: object = None
    controls: list = field(default_factory=list)
    toolbar: bool = False
    link_parameter_count: int = 0

    def replay(self, tile, rect, cache):
        from meltygui.core.melty import Melty
        from meltygui import imgui
        from meltygui.view.tile_view import tile_control_layout

        if (cache is None or renderer_version(tile.render_func) != self.version
                or tile.input_value is not self.input_value or not self.controls
                or (tile.render_func is not None and self.body is None)):
            return False
        x, y, width, height = rect
        content_height, picker_width, slots = tile_control_layout(width, height, self.link_parameter_count)
        body_height = height if self.toolbar else content_height
        if width <= 0 or body_height <= 0:
            return False
        items = []
        if self.body is not None:
            items.append((self.body, (x, y, width, body_height),
                          height - content_height if self.toolbar else 0))
        # Body first, then its controls, retaining the original paint order.
        items.append((self.controls[0], (x, y + content_height, picker_width, height - content_height), 0))
        items.extend((ds, (x + left, y + content_height + top, slot_width,
                           height - content_height), 0)
                     for ds, (left, top, slot_width) in zip(self.controls[1:], slots))
        Melty.push_clip((x, y, x + width, y + height))
        try:
            return cache.replay_resize_batch(items)
        finally:
            Melty.pop_clip()
            if Melty.channels_split:
                imgui.get_window_draw_list().channels_set_current(Melty.get_channel())
