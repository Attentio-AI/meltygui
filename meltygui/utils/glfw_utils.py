import threading

import glfw


def request_render():
    _needs_render.set()
    glfw.post_empty_event()


_needs_render = threading.Event()
