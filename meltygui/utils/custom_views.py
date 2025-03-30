import threading
import traceback

import glfw


def print_stack_trace():
    # Get the current stack frame info
    stack = traceback.extract_stack()

    # Format and print the stack trace (excluding this function call)
    formatted_stack = traceback.format_list(stack[:-1])

    for frame in formatted_stack:
        print(frame, end='')  # end='' to avoid extra newlines


_needs_render = threading.Event()

def request_render():
    _needs_render.set()
    glfw.post_empty_event()

def does_need_render():
    return _needs_render.is_set()