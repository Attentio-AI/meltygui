"""Demo for the new draw_columns layout (view/core_views/columns.py).

Lines-only stage: for n columns, n+1 draggable edge lines (window left
edge, the column edges, window right edge) are drawn over the content and
stored on the draw_state. Lines move independently and only carry a
neighbour on physical contact (MIN_COLUMN_WIDTH). Views don't follow the
lines yet — children render in normal flow underneath.
"""
import imgui

from src.lsd.gl_gui.view.core_views.columns import draw_columns, Columns
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

# Module-level so hotswap re-exec reuses it and edits keep their state while
# iterating on draw_columns.
columns_demo = Columns({
    "settings": {"alpha": 0.5, "steps": 12, "label": "left pane", "enabled": True},
    "notes": "Drag a divider: the column resizes, its neighbours slide, and "
             "when they run out of slack the window edge gets pushed.",
    "integer": [1,1,1,1],
    "nested": Columns({
        "inner_a": {"x": 1, "y": 2},
        "inner_b": [3, 1, 4, 1, 5],
    }),
})


# auto_resize=False: a fixed-size window so the row has a stable frame to
# resize in; edges move independently of the window either way.
@window
@render_func(tint=(0.16, 0.35, 0.49), auto_resize=False, min_width=720, min_height=420)
def draw_columns_demo(_, draw_state):
    imgui.text("draw_columns: shared edges — cells, nested rows, and the window frame all collide")
    draw_columns(columns_demo, name="columns_demo_body")
