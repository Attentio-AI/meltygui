"""Demo for the draw_columns / draw_rows layouts (view/core_views/columns.py).

Every edge line — the window's four frame edges, each column edge, each
row edge — is a draggable object shared by reference between the views
that meet on it. Lines move independently and only carry a neighbour on
physical contact (MIN_COLUMN_WIDTH along x, MIN_ROW_HEIGHT along y);
pushed through the pile, an interior line moves the window frame itself.
A right-drag anywhere latches the column edge to the cursor's right AND
the row edge below it (left+right-drag: the edges to its left / above)
and drives both through the same solve.
"""
import meltygui_imgui as imgui

from meltygui.view.layout_view import draw_rows
from meltygui.model.layout_model import Columns
from meltygui.model.layout_model import Rows
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window

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

# Rows stack top to bottom on the same machinery. The middle row is the
# Columns demo above, so its column edges live inside a row band; the last
# row is a Columns whose inner cell is itself a Rows - the outer row's
# edges are passed through the Columns cell to it, so its interior band
# collides with the rows above and the window's bottom frame edge.
rows_demo = Rows({
    "toolbar": {"query": "rows", "limit": 12, "live": True},
    "panes": columns_demo,
    "stack": Columns({
        "log": ["row edges collide like column edges",
                "drag a band between rows",
                "push through: the window frame follows"],
        "inner_rows": Rows({
            "a": {"x": 1},
            "b": [2, 7, 1],
        }),
    }),
})


# auto_resize=False: a fixed-size window so the edges have a stable space to
# resize in; edges move independently of the window either way.
@window
@render_func(tint=(0.19, 0.23, 0.29), auto_resize=False, min_width=720, min_height=420)
def draw_columns_demo(_, draw_state):
    imgui.text("draw_rows / draw_columns: shared edges — rows, columns, nested both ways, "
               "and the window frame all collide")
    draw_rows(rows_demo, name="rows_demo_body")
