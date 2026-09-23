"""Run with .venv/bin/python examples/tile_sibling_links.py.

Choose a source for either parameter in the observer's Links picker, then
increment a counter. Unlink to restore the observer's retained local state.
"""
import meltygui
from meltygui import DrawState, render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.layout.tile_manager_core import TileManagerState, draw_tiles
from meltygui.core.rendering.window_decoration import window
from meltygui.model.tile_model import Tile, Split
from meltygui.examples.tile_manager_demo import CounterState, draw_tile_counter
from meltygui.view.text_view import draw_text


@render_func(multi_instance=True, tint=(0.34, 0.24, 0.42), display_name="Observer")
def draw_observer(input_value: object,
                  counter_view: DrawState[draw_tile_counter] = None,
                  counter: CounterState = None):
    value = ('Unlinked' if counter_view is None
             else str(counter_view.misc['counter_state'].count))
    draw_text(f"DrawState source: {value}", editable=False, key='view')
    draw_text(f"State object: {counter.count}", editable=False, key='state')
    return False, input_value


class LinkDemo(DictConversion):
    def __init__(self):
        super().__init__()
        self.tree = Split('x', [
            Tile('Observer', render_func=draw_observer),
            Split('y', [Tile('Counter A', render_func=draw_tile_counter),
                        Tile('Counter B', render_func=draw_tile_counter)]),
        ])


@window
@render_func(tint=(0.24, 0.30, 0.20), auto_resize=False, show_bg=True)
def draw_link_demo(input_value: object, draw_state,
                   model: LinkDemo = None, tile_state: TileManagerState = None):
    changed = draw_tiles(model.tree, draw_state, tile_state=tile_state,
                         multi_instance_renderers=(draw_observer, draw_tile_counter))
    return changed, input_value


@meltygui.glfw_window(name='Sibling links', app_id='meltygui-sibling-links-demo',
                     width=960, height=640)
@render_func(tint=(0.24, 0.30, 0.20), determines_height=False)
def sibling_links(input_value: object = None, draw_state=None):
    return draw_link_demo(input_value, width=draw_state.width, height=draw_state.height)
