"""Demo for `context_menu=` — a right-click menu on any render call.

Pass `context_menu={label: callable}` to a view and core_render draws the
labels as a dropdown menu at the pointer (draw_context_menu_items); the
Inspect row at the bottom of that menu opens the inspector (draw_context_menu).
Nothing else is wired: the wrapper owns the right-click, the popover and the
swap, exactly like it owns the inspector today.
"""
import imgui

from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any

# Module-level so hotswap re-exec reuses it and the edits made through the
# menu keep their state while iterating.
demo = {"greeting": "right-click me", "count": 3}


@window
@render_func(tint=(0.55, 0.25, 0.5), auto_resize=False, min_width=420, min_height=200)
def draw_context_menu_demo(_, draw_state):
    imgui.text("context_menu={label: callable} — right-click a value below")

    def _upper():
        demo["greeting"] = demo["greeting"].upper()

    def _lower():
        demo["greeting"] = demo["greeting"].lower()

    def _reset():
        demo["greeting"] = "right-click me"

    changed, value = draw_any(demo["greeting"], name="greeting",
                              context_menu={"Uppercase": _upper, "Lowercase": _lower,
                                            "Reset": _reset})
    if changed:
        demo["greeting"] = value

    def _bump():
        demo["count"] += 1

    def _zero():
        demo["count"] = 0

    changed, value = draw_any(demo["count"], name="count",
                              context_menu={"Add one": _bump, "Zero": _zero})
    if changed:
        demo["count"] = value
