#!/usr/bin/env python3
"""The studio's columns playground (view/playground/columns_playground.py)
as an app: `@window` swapped for `@glfw_window`, nothing else changed."""
import meltygui_imgui as imgui
from meltygui import glfw_window
from meltygui.view.layout_view import draw_rows
from meltygui.model.layout_model import Columns
from meltygui.model.layout_model import Rows
from meltygui.core.core_render import render_func

columns_demo = Columns({
    "settings": {"alpha": 0.5, "steps": 12, "label": "left pane", "enabled": True},
    "notes": "Drag a divider: the column resizes, its neighbours slide, and "
             "when they run out of slack the window edge gets pushed.",
    "integer": [1, 1, 1, 1],
    "nested": Columns({"inner_a": {"x": 1, "y": 2}, "inner_b": [3, 1, 4, 1, 5]}),
})
rows_demo = Rows({
    "toolbar": {"query": "rows", "limit": 12, "live": True},
    "panes": columns_demo,
    "stack": Columns({
        "log": ["row edges collide like column edges", "drag a band between rows",
                "push through: the window frame follows"],
        "inner_rows": Rows({"a": {"x": 1}, "b": [2, 7, 1]}),
    }),
})


@glfw_window(name='Columns demo', width=900, height=560)
@render_func(tint=(0.19, 0.23, 0.29), auto_resize=False, min_width=720, min_height=420)
def draw_columns_demo(_, draw_state):
    imgui.text("draw_rows / draw_columns: shared edges — rows, columns, nested both ways")
    draw_rows(rows_demo, name="rows_demo_body")
