"""A plain ImGui app: its own GLFW window, frame loop, thread and cleanup.

    .venv/bin/python examples/standalone_counter.py

Nothing here knows about meltygui. examples/gui_module_demo.py hosts this same
file as a view, twice, through gui_module.
"""
import signal
import threading

import glfw
import imgui
import OpenGL.GL as gl
from imgui.integrations.glfw import GlfwRenderer

input_value = 'hello'
title = 'Counter'
clicks = 0
running = True


def draw():
    global input_value, clicks
    imgui.text(f'{title}: {input_value}')
    if imgui.button('Click me'):
        clicks += 1
    imgui.same_line()
    imgui.text(f'clicks {clicks}')
    changed, text = imgui.input_text('text', input_value, 256)
    if changed:
        input_value = text


def render_loop():
    glfw.init()
    # An app id is what lets the compositor treat this as a normal (floating, movable) window.
    glfw.window_hint_string(glfw.WAYLAND_APP_ID, 'standalone-counter')
    glfw.window_hint_string(glfw.X11_CLASS_NAME, 'standalone-counter')
    window = glfw.create_window(480, 200, title, None, None)
    glfw.make_context_current(window)
    imgui.create_context()
    backend = GlfwRenderer(window)
    frames = 0
    try:
        while running and not glfw.window_should_close(window):
            glfw.poll_events()
            backend.process_inputs()
            imgui.new_frame()
            imgui.begin(title)
            draw()
            frames += 1
            imgui.text(f'frames {frames}')
            imgui.end()
            gl.glClearColor(0.1, 0.1, 0.12, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            imgui.render()
            backend.render(imgui.get_draw_data())
            glfw.swap_buffers(window)
    finally:
        print(f'{title}: closed after {frames} frames with {clicks} clicks')
        backend.shutdown()
        glfw.terminate()


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGINT, stop)
thread = threading.Thread(target=render_loop)
thread.start()
thread.join()
