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
        states = [*self.controls, *([self.body] if self.body is not None else [])]
        if not all(cache.can_replay_resize(ds) for ds in states):
            return False
        x, y, width, height = rect
        content_height, picker_width, slots = tile_control_layout(width, height, self.link_parameter_count)
        body_height = height if self.toolbar else content_height
        if width <= 0 or body_height <= 0:
            return False
        Melty.push_clip((x, y, x + width, y + height))
        try:
            if self.body is not None:
                cache.replay_resize(self.body, (x, y, width, body_height),
                                    footer_height=height - content_height if self.toolbar else 0)
            # Repaint controls after a toolbar-bearing body; its capture may
            # include their old pixels. Both use the same live footer slots.
            cache.replay_resize(self.controls[0], (x, y + content_height, picker_width, height - content_height))
            for ds, (left, top, slot_width) in zip(self.controls[1:], slots):
                cache.replay_resize(ds, (x + left, y + content_height + top,
                                         slot_width, height - content_height))
        finally:
            Melty.pop_clip()
            if Melty.channels_split:
                imgui.get_window_draw_list().channels_set_current(Melty.get_channel())
        return True
