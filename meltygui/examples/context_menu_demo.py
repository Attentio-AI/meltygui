#!/usr/bin/env python3
"""The studio's context-menu playground as an app: fields drawn with
draw_any, a right-click menu on each (`@window` -> `@glfw_window`)."""
import meltygui_imgui as imgui
from meltygui import glfw_window
from meltygui.rendering.core import render_func
from meltygui.views.values import draw_any

demo = {"greeting": "right-click me", "count": 3}


@glfw_window(name='Context menu demo', width=520, height=260)
@render_func(tint=(0.78, 0.16, 0.75), auto_resize=False, min_width=420, min_height=200)
def draw_context_menu_demo(_, draw_state):
    imgui.text("context_menu={label: callable} — right-click a value below")
    changed, value = draw_any(demo["greeting"], name="greeting",
                              context_menu={"Uppercase": lambda: demo.update(greeting=demo["greeting"].upper()),
                                            "Reset": lambda: demo.update(greeting="right-click me")})
    if changed:
        demo["greeting"] = value
    changed, value = draw_any(demo["count"], name="count",
                              context_menu={"Add one": lambda: demo.update(count=demo["count"] + 1)})
    if changed:
        demo["count"] = value
