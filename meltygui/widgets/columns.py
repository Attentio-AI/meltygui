"""Demo for the new draw_columns layout (view/core_views/columns.py).

No column_widths is passed, so the layout is stateful: dividers start at an
equal split and drags persist on the draw_state. The "nested" cell is a
Columns dict, so it renders as nested columns — dragging its inner dividers
pushes the enclosing cell wider and, past the slack, the window edge.
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
    "nested": Columns({
        "inner_a": {"x": 1, "y": 2},
        "inner_b": [3, 1, 4, 1, 5],
    }),
})


# auto_resize=False: a fixed-size window, so draw_columns registers drag
# handles on its left/right edges (an auto window's width is content-driven
# and would snap back).
@window
@render_func(tint=(0.16, 0.35, 0.49), auto_resize=False, min_width=720, min_height=420)
def draw_columns_demo(_, draw_state):
    imgui.text("draw_columns: rigid columns; drag edges, contacts push, window edges drag too")
    draw_columns(columns_demo, name="columns_demo_body")
