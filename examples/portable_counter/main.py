"""The same app entry point runs on desktop and in the UIKit host."""
import meltygui
from meltygui import draw_any, glfw_window, persisted, render_func
from meltygui.view.header_view import flat_button, draw_header

meltygui.boot(app_id='melty-portable-counter')
counter = persisted('counter', lambda: {'count': 0})
settings = {'step': 1}


@glfw_window(name='Portable counter', settings=settings, value=counter,
             preferences=settings, with_header=draw_header)
@render_func(tint=(0.3, 0.65, 0.9))
def main(input_value: dict, draw_state, preferences: dict):
    step = preferences['step']
    changed = flat_button(f'Add {step}', draw_state, 'increment')
    if changed:
        input_value['count'] += step
    edited, _ = draw_any(input_value, name='Counter value', width=draw_state.content_width)
    return changed or edited, input_value


meltygui.run()
