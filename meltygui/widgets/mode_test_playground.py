"""Usage example for the inputs tab's mode-attached child_kwargs source.

    @window draw_mode_test ──draw_any(mode=Modes.TEXT)──► draw_collection
                                                            │ child_kwargs
                                                            ▼
                                                         draw_text  (one per key)

Mode.TEXT (view/mode.py) carries `child_kwargs={line_height, wrap,
syntax_highlight}` in its dict entry. Nothing here passes those — they reach
each draw_text leaf purely through the mode.

To see the new source: right-click a text leaf → inputs tab. Two child_kwargs
rows appear, and they are independently editable:

  child_kwargs (mode)   ← Mode.TEXT's dict, jumps to / saves into mode.py
  child_kwargs          ← whichever NON-mode parent source sets one (the
                          caller below, once you uncomment it)

Editing `line_height` on the mode row rewrites mode.py and every leaf
reflows; the plain row edits this file instead.
"""
import meltygui_imgui as imgui

from meltygui.modes import Modes
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.window_decoration import window
from meltygui.views.new_core_view import draw_any

# Module level so hotswap-re-exec reuses it - edits made in the window survive
# while iterating on the mode.
mode_test_text = {
    "intro": "Mode.TEXT sends every string in this dict to draw_text.",
    "styling": "line_height, wrap and syntax_highlight are NOT passed here — "
               "they arrive from Mode.TEXT's child_kwargs and are editable "
               "from the inputs tab's 'child_kwargs (mode)' row.",
    "code_sample": "def unhighlighted(x):\n    return x * 2  # syntax_highlight=False",
}


@window
@render_func(tint=(0.22, 0.38, 0.31), auto_resize=False, min_width=560, min_height=320)
def draw_mode_test(input_value, draw_state):
    imgui.text("Mode.TEXT — leaf style comes from the mode's child_kwargs")
    draw_any(mode_test_text, name="mode_test_body", mode=Modes.TEXT)
    # Uncomment to give the leaves a SECOND, non-mode child_kwargs setter (this
    # call site) - the inputs tab then shows both rows side by side, the mode
    # one still winning at runtime:
    # draw_any(mode_test_text, name="mode_test_body_caller", mode=Modes.TEXT,
    #          child_kwargs={"line_height": 2.0})
