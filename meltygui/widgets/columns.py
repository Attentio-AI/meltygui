import imgui

from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window



@window(tint=(0.9, 0.9, 0.0))
@render_func
def draw_test_columns(draw_state):
    column_count = 5

    content_width = draw_state.content_width
    column_width = content_width / column_count

    for i in range(column_count):
        imgui.text(f"Column {i+1}")
        imgui.next_column()

    imgui.text("Columns playground")