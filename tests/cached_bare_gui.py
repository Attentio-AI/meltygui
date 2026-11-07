"""Minimal ImGui capture bridge for the isolated Rust cached forwarding wrapper.

Explicit caller keys and geometry are the contract. Call invalidate after ANY
input, geometry, texture, source or tree change. No automatic dependency tracking,
layout, input routing, persistence, eviction or independent leaf updates.
"""
import ctypes
import math


class CaptureBridge:
    def __init__(self):
        from meltygui import imgui
        from meltygui.core.rendering._gui_native import TextureCache
        self.imgui = imgui
        self.gpu = TextureCache()
        self.captures = {}
        self.texture_refs = {}
        self.stack = []
        self.origins = []
        self.recompose = False
        self.closed = False

    def capture(self, node, root, function, input_value, arguments):
        if self.closed:
            raise RuntimeError('Closed capture bridge')
        imgui = self.imgui
        w, h = arguments['width'], arguments['height']
        if not all(math.isfinite(v) and 0 < v <= 8192 for v in (w, h)):
            raise ValueError('Explicit cache dimensions must be in (0, 8192]')
        children = []
        parent_x, parent_y = self.origins[-1] if self.origins else (0., 0.)
        origin = (parent_x + arguments['left'], parent_y + arguments['top'])
        self.stack.append(children)
        self.origins.append(origin)
        capture = None
        reference_start = len(imgui.get_current_context()._keepalive_cache)
        try:
            self.gpu.reference(node, math.ceil(w), math.ceil(h))
            if root:
                self.gpu.raster(node, True)
            if node not in self.captures:
                self.captures[node] = imgui.DrawCapture(f'bare-capture-{id(self)}-{node}')
            capture = self.captures[node]
            capture.begin(w, h, *origin)
            try:
                result = function(input_value, **arguments)
            finally:
                capture.end()
            self.gpu.begin_packet(node)
            texture_refs = []
            for draw_list in capture.draw_lists:
                commands, offset = [], 0
                for command in draw_list.commands:
                    texture = command.texture_id
                    texture_refs.append(texture)
                    commands.append((int(texture), command.elem_count, tuple(command.clip_rect), offset, 0))
                    offset += command.elem_count
                self.gpu.add_packet(node,
                    ctypes.string_at(draw_list.vtx_buffer_data, draw_list.vtx_buffer_size * 20),
                    ctypes.string_at(draw_list.idx_buffer_data, draw_list.idx_buffer_size * 4), commands)
            self.texture_refs[node] = tuple(texture_refs)
            self.gpu.children(node, children)
            return result
        finally:
            self.stack.pop()
            self.origins.pop()
            if capture is not None:
                from meltygui.core.rendering.gui_draw_capture import release_captured_references
                release_captured_references(capture, reference_start)

    def place(self, node, arguments, root, dirty):
        if self.closed:
            raise RuntimeError('Closed capture bridge')
        x, y, w, h = (arguments[k] for k in ('left', 'top', 'width', 'height'))
        if not root:
            self.stack[-1].append((node, x, y, w, h))
            return
        if dirty or self.recompose:
            self.gpu.render_many([(node, (0., 0., 0., 0.))])
        texture, tw, th = self.gpu.image(node)
        left, top, right, bottom = (math.floor(v + .5) for v in (x, y, x+w, y+h))
        draw_list = self.imgui.get_window_draw_list()
        draw_list.push_clip_rect(left, top, right, bottom, True)
        try:
            draw_list.add_image(texture, (left, top), (left+tw, top+th), (0, 1), (1, 0))
        finally:
            draw_list.pop_clip_rect()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.gpu.close()
        for capture in self.captures.values():
            capture.close()
        self.captures.clear()
        self.texture_refs.clear()
