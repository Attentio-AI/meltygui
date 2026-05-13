import imgui

from src.lsd.gl_gui.view.core_views.core_render import render_func


class DebugBlitOffscreen:

    some_val = True
    display_mask_rects = True
    display_roots = True

class OtherClass:
    some_val = False


@render_func(use_cache=True)
def draw_blit_debug():

    from src.lsd.gl_gui.view.core_views.new_core_view import draw_with_modes
    from src.lsd.gl_gui.view.mode import Mode
    from src.lsd.gl_gui.view.mode import ModeGroup

    imgui.text("Blit Debug Renderer")


    draw_with_modes(input_value=DebugBlitOffscreen, name="slkdjf", modes=ModeGroup.CODE)

    imgui.text("Other Class")