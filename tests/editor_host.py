"""A hidden-window host for `@gui` views in tests and benchmarks: the frame
loop of gui_window_prototype.py without a surface."""
import meltygui_imgui as imgui

from meltygui.core.rendering.gui_prototype import _window
from meltygui.core.rendering.gui_window_prototype import _GuiWindow


class Host:
    def __init__(self, view, **arguments):
        from conftest import _ensure_gl_context
        _ensure_gl_context()
        self.io = io = imgui.get_io()
        io.mouse_pos = (-100, -100)
        for i in range(len(io.mouse_down)):
            io.mouse_down[i] = False
        for i in range(len(io.keys_down)):
            io.keys_down[i] = False
        io.key_ctrl = io.key_alt = io.key_shift = io.key_super = False
        io.mouse_wheel = io.mouse_wheel_horizontal = 0
        self.window = _GuiWindow()
        self.cache = self.window.cache
        self.view, self.arguments = view, arguments
        self.result = None
        self.frame()

    def frame(self, position=None, down=None, keys=(), text='', wheel=0., **arguments):
        """One frame: optional pointer state, key presses [(key, mods)], typed text."""
        io, cache = self.io, self.cache
        self.arguments.update(arguments)
        io.delta_time = 1 / 60
        if position is not None:
            io.mouse_pos = position
        if down is not None:
            io.mouse_down[0] = down
        io.mouse_wheel = wheel
        for item in keys:
            cache.imgui_input.key(*item)
        for char in text:
            cache.imgui_input.character(ord(char))
        token = _window.set(self.window)
        self.window.native.begin_frame(scope=('test', 'editor'), width=io.display_size[0], cache=cache)
        try:
            with cache.capture_frame():
                cache.process_host_input()
                cache.flush()
                with cache.frame():
                    self.result = self.view(**self.arguments)
                cache.flush()
        finally:
            self.window.native.end_frame()
            _window.reset(token)
        return self.result

    def click(self, x, y, count=1):
        for _ in range(count):
            self.frame((x, y), True)
            self.frame((x, y), False)

    def close(self):
        self.window.close()
