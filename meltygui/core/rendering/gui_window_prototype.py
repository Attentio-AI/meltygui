"""Private @glfw_window adapter for the opt-in native @gui experiment."""
from meltygui.core.rendering.gui_prototype import _bind_native, _new_native, _window
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


class _GuiWindow:
    def __init__(self, *, graphics=True):
        self.cache = RetainedGui(graphics=graphics)
        self.cache.auto_layout = True
        self.native = _new_native(graphics=graphics)
        self.bindings = {}
        self.closed = False
        self.input_backend = None

    def solve_native(self, adapter):
        from meltygui.core.rendering.gui_native_collision import NativeCollision
        geometry=self.cache.geometry
        if geometry.native is None:geometry.native=NativeCollision(geometry)
        geometry.native.begin(adapter)

    def call(self, view, input_value, kwargs):
        if self.closed:
            raise RuntimeError('GUI window is closed')
        if view not in self.bindings:
            func, options = view.__gui_definition__
            options = dict(options)
            cached = options.pop('use_cache', True)
            options.pop('live', None)
            self.bindings[view] = (self.cache.gui(func, **options) if cached
                                   else _bind_native(self.native, func, **options))
        return self.bindings[view](input_value, **kwargs)

    def draw(self, surface, view, view_kwargs):
        import meltygui_imgui as imgui
        from meltygui.core.melty import Melty
        if self.closed:
            raise RuntimeError('GUI window is closed')
        width, height = (Melty.root_fill[:2] if Melty.root_fill is not None
                         else imgui.get_content_region_available())
        arguments = dict(view_kwargs)
        value = arguments.pop('value', None)
        arguments.setdefault('width', width)
        arguments.setdefault('height', height)
        self.cache.request_frame = surface.request_frame
        self.cache.origin = (0, 0)
        if self.cache.graphics and self.input_backend is None:
            self.input_backend = surface.impl
            self.input_backend.gui_character_callback = self.cache.imgui_input.character
            self.cache.imgui_input.clipboard = (surface.impl._get_clipboard_text,
                                                surface.impl._set_clipboard_text)
        token = _window.set(self)
        self.native.begin_frame(scope=id(surface), width=width, cache=self.cache)
        try:
            self.cache.process_host_input(
                drag_position=(self.input_backend.drag_mouse_pos
                               if self.input_backend is not None else None))
            self.cache.flush()
            if self.cache.geometry.native is not None:
                pairs=self.cache.geometry.native.pairs
                axes=self.cache.geometry.axes
                width=axes[0].position(pairs[0][1])-axes[0].position(pairs[0][0])
                height=axes[1].position(pairs[1][1])-axes[1].position(pairs[1][0])
                top=Melty.root_fill[2] if Melty.root_fill is not None else 0.
                arguments['width'],arguments['height']=width,height-top
            with self.cache.frame():
                result = view(value, **arguments)
            self.cache.flush()
            if self.cache.graphics:
                # Resolve ownership from this frame's actual ImGui items,
                # after all dirty contexts ran, rather than from cache bounds.
                self.cache.register_host_pointer(Melty.event_handler, imgui.get_mouse_pos())
            self.cache.present((0, 0))
            if view.__gui_definition__[1].get('live', False):
                surface.request_frame()
            return result
        finally:
            self.native.end_frame()
            _window.reset(token)

    def close(self):
        if not self.closed:
            if self.input_backend is not None:
                self.input_backend.gui_character_callback = None
                self.input_backend = None
                self.cache.imgui_input.clipboard = None
            self.cache.close()
            self.cache.request_frame = None
            self.native.clear()
            self.bindings.clear()
            self.closed = True


def draw_gui_surface(surface, view, view_kwargs):
    if getattr(surface, '_gui_prototype', None) is None:
        surface._gui_prototype = _GuiWindow()
    return surface._gui_prototype.draw(surface, view, view_kwargs)
