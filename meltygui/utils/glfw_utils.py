import sys
import threading
import traceback

import glfw


def print_stack_trace(size=None, skip=-1, stack=None):
    # Get the current stack frame information
    if stack is None:
        stack = traceback.extract_stack()

    # Format and print the stack trace (excluding this function call)
    if size is None:
        formatted_stack = traceback.format_list(stack[:skip])
    else:
        formatted_stack = traceback.format_list(stack[-size:skip])

    for frame in formatted_stack:
        print(frame, end='')  # end='' to avoid double newlines


def request_render():
    # stack = traceback.extract_stack()
    # from src.lsd.gl_gui.melty import Melty
    # Melty.last_request_render = stack[-2].name
    # print_stack_trace(size=5)
    _needs_render.set()
    glfw.post_empty_event()


_needs_render = threading.Event()
