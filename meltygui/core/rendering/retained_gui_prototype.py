"""Throwaway retained @gui graph. Explicit invalidation; no model hashing.

Rust owns graph reconciliation, dirty scheduling, result mailboxes, command
storage, texture allocation and GL replay. This bridge owns Python callables,
isolated ImGui layout contexts and translation of host input into view events.
Cache boundaries accept a constrained width and fixed or measured height.
"""
from contextlib import contextmanager
from collections import deque
from functools import wraps
from pathlib import Path
from types import FunctionType
import ctypes
import math

from meltygui.core.rendering.gui_prototype import _bind_native, _new_native
from meltygui.core.rendering.gui_imgui_input import ImGuiInput


def _same(a, b):
    """Cheap argument semantics: exact primitives by value, objects by identity."""
    return a is b or (type(a) is type(b) and type(a) in (str, int, float, bool, type(None)) and a == b)


def _alias(value):
    if isinstance(value, (str, Path)):
        return 'path:' + str(value)
    return f'object:{id(value)}'


class RetainedGui:
    def __init__(self, *, graphics=True):
        from meltygui.core.rendering._gui_native import RetainedGraph
        self.graph = RetainedGraph()
        self.graphics = graphics
        self.gpu = None
        self.stack = []
        self.records = {}
        self.invocations = {}
        self.regions = {}
        self.staged_regions = {}
        self.events = {}
        self.renderers = []
        self.contexts = {}
        self.states = {}
        self.backgrounds = {}
        self.texture_refs = {}
        self.auto_layout = False
        self.request_frame = None
        self.root_ids = set()
        self.origin = (0, 0)
        self.hover = None
        self.drag = None
        self.closed = False
        self.frame_roots = None
        self._last_pointer = None
        self._positions = {}
        self.window_ids = {}
        self.layout_log = deque(maxlen=64)
        self.imgui_input = ImGuiInput(self)
        from meltygui.core.rendering.gui_layout_prototype import GuiGeometry
        self.geometry = GuiGeometry(self)

    @property
    def current(self):
        return self.stack[-1]

    def associate(self, value, node=None):
        self.graph.associate(node or self.current, _alias(value), value)

    def invalidate(self, value):
        """Objects, paths, functions and returned native drawstates are aliases."""
        self.geometry.frozen.clear()
        definition = value.__dict__.get('__gui_definition__') if isinstance(value, FunctionType) else None
        if definition is not None:
            value = definition[0]
        count = self.graph.invalidate(_alias(value))
        if count and self.request_frame:
            self.request_frame()
        return count

    def invalidate_id(self, unique):
        self.geometry.frozen.discard(unique)
        self.graph.invalidate_node(unique)
        if self.request_frame:
            self.request_frame()

    @contextmanager
    def frame(self):
        """Reconcile declarations made by the external, uncached host."""
        if self.frame_roots is not None:
            raise RuntimeError('nested retained host frame')
        previous = self.root_ids.copy()
        self.frame_roots = set()
        try:
            yield self
        except BaseException:
            for node in self.frame_roots - previous:
                self.graph.retire(node)
            self.root_ids = previous
            raise
        else:
            for node in previous - self.frame_roots:
                self.graph.retire(node)
            self.root_ids = self.frame_roots
        finally:
            self.frame_roots = None
            self._retire()

    def gui(self, func=None, *, width=300, height=100, min_height=0, max_height=8192,
            inject=None, events=(), **decoration):
        if func is None:
            return lambda fn: self.gui(fn, width=width, height=height, min_height=min_height,
                                       max_height=max_height, inject=inject, events=events, **decoration)
        renderer_id = len(self.renderers) + 1
        definition = [func, func.__code__]
        self.renderers.append(definition)

        @wraps(func)
        def wrapper(input_value=None, **kwargs):
            if self.closed:
                raise RuntimeError('retained runtime is closed')
            key = kwargs.pop('key', func.__name__)
            portal = kwargs.pop('melty_window', False)
            if kwargs.pop('glfw_window', False):
                raise NotImplementedError('This experiment implements internal windows; native child surfaces are not connected yet')
            initial = kwargs.pop('initial', {})
            extras = kwargs.pop('return_extras', False)
            width_given, height_given = 'width' in kwargs, 'height' in kwargs
            w, h = kwargs.pop('width', width), kwargs.pop('height', height)
            minimum = kwargs.pop('min_height', min_height)
            maximum = kwargs.pop('max_height', max_height)
            if (not math.isfinite(minimum) or not math.isfinite(maximum)
                    or not 0 <= minimum <= maximum <= 8192):
                raise ValueError('height bounds must satisfy 0 <= min_height <= max_height <= 8192')
            minimum, maximum = math.ceil(minimum), math.ceil(maximum)
            parent = self.stack[-1] if self.stack else 0
            window_identity = (parent,renderer_id,key)
            saved_window = self.window_ids.get(window_identity) if portal else None
            if portal:
                old_rect = self.graph.info(saved_window)['rect'] if saved_window in self.records else None
                if not width_given: w = old_rect[2] if old_rect else initial.get('width',w)
                if not height_given: h = old_rect[3] if old_rect else initial.get('height',h)
            allocation = self.geometry.allocation(parent) if not portal else None
            if self.graphics and (parent or self.auto_layout) and not portal:
                import meltygui_imgui as imgui
                x, y = imgui.get_cursor_screen_pos()
            else:
                x, y = initial.get('window_pos', (0, 0))
            if allocation is not None:
                ax, ay, w, h = allocation
                px, py = self._position(parent)
                x, y = ax-px, ay-py
            if w is None:
                if parent:
                    w = self.graph.info(parent)['constraints'][0] - x
                elif self.graphics:
                    w = imgui.get_content_region_available()[0]
                else:
                    raise ValueError('width=None requires a parent or graphics host')
            if not math.isfinite(w) or not 0 < w <= 8192 or (h is not None and (not math.isfinite(h) or h < 0)):
                raise ValueError('cached boundaries require positive width and nonnegative height')
            # Keep logical constraint geometry fractional. Only GPU storage
            # needs whole pixels; rounding a cell here fights its layout solver.
            w, h = float(w), None if h is None else float(h)
            if h is not None:
                h = min(maximum, max(minimum, h))
            arguments = {**decoration, **kwargs, 'width': w, 'height': h,
                         'min_height': minimum, 'max_height': maximum}
            node = self.graph.declare(parent, renderer_id, key, input_value, arguments,
                                      (float(x), float(y), float(w), float(-1 if h is None else h)), portal)
            self._positions.clear()
            if portal:
                self.window_ids[window_identity] = node
            previous = self.invocations.get(node)
            pending = self.graph.pending(node)
            if previous is not None:
                old_input, old_args = previous
                acknowledgement = _same(self.graph.result(node, False)[1], input_value)
                if ((not pending and not _same(old_input, input_value) and not acknowledgement) or old_args.keys() != arguments.keys()
                        or any(not _same(old_args[k], v) for k, v in arguments.items())):
                    self.graph.invalidate_node(node)
                if not pending and old_input is not input_value:
                    self.graph.dissociate(node, _alias(old_input))
            self.invocations[node] = input_value, arguments.copy()
            if node not in self.records:
                native = _new_native(graphics=self.graphics)
                self.records[node] = (native, _bind_native(native, func, inject=inject, events=events), func)
                self.associate(func, node)
                self.associate(wrapper, node)
                self.graph.associate(node, f'renderer:{renderer_id}', func)
            self.associate(input_value, node)
            self.geometry.bind(node, parent, portal=portal)
            if parent == 0:
                self.root_ids.add(node)
                if self.frame_roots is not None:
                    self.frame_roots.add(node)
            # An independently produced return is consumed before reexecuting.
            # In particular, an old scalar caller argument cannot overwrite it.
            if self.graph.dirty(node) and not pending and node not in self.geometry.frozen:
                self._execute(node, independent=False)
            # Root layout settles before the immediate caller places later items.
            if not parent:
                self.flush()
            _, _, w, h = self.graph.info(node)['rect']
            if self.graphics:
                if parent and not portal:
                    import meltygui_imgui as imgui
                    self._draw_image(imgui.get_window_draw_list(),node,x,y,w,h)
                    imgui.dummy(w, h)
                elif self.auto_layout and not parent and not portal:
                    import meltygui_imgui as imgui
                    imgui.dummy(w, h)
            result = self.graph.result(node, consume=True)
            if result[0] and parent:
                # The caller may have drawn a label before assigning this return.
                # One signalled refresh displays its post-edit state consistently.
                self.graph.invalidate_node(parent)
            if extras:
                return *result, self.states[node]
            return result

        wrapper.__gui__ = True
        return wrapper

    def _context(self, node, w, h):
        import meltygui_imgui as imgui
        if node not in self.contexts:
            previous = imgui.get_current_context()
            atlas = imgui.get_io().fonts
            context = imgui.create_context(shared_font_atlas=atlas)
            self.contexts[node] = context
            imgui.set_current_context(context)
            io = imgui.get_io()
            io.ini_file_name = None
            io.display_size = (w, h)
            io.delta_time = 1 / 60
            # Settle ImGui's new-window bookkeeping without executing user code.
            imgui.new_frame()
            self._begin_window(w, h)
            imgui.get_window_draw_list().pop_clip_rect()
            imgui.end()
            imgui.render()
            imgui.set_current_context(previous)
        return self.contexts[node]

    @staticmethod
    def _begin_window(w, h):
        import meltygui_imgui as imgui
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(w, h)
        imgui.begin('retained-body', flags=(imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE
                    | imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_SAVED_SETTINGS
                    | imgui.WINDOW_NO_SCROLL_WITH_MOUSE | imgui.WINDOW_NO_BACKGROUND))
        imgui.set_cursor_screen_pos((0, 0))
        imgui.get_window_draw_list().push_clip_rect(0, 0, w, h, False)

    def _execute(self, node, *, independent):
        self._positions.clear()
        native, renderer, func = self.records[node]
        input_value, arguments = self.graph.invocation(node)
        w, requested_h = self.graph.info(node)['constraints']
        # Measure without the old texture's clip truncating newly grown content.
        # This is a logical capture viewport, not an 8192px texture allocation.
        h = 8192 if requested_h < 0 else max(1, requested_h)
        events = self.events.pop(node, {})
        self.graph.begin(node)
        self.stack.append(node)
        self.staged_regions[node] = []
        previous = None
        parent_texture_refs = None
        try:
            self.geometry.begin(node)
            if self.graphics:
                import meltygui_imgui as imgui
                from meltygui.core.rendering._gui_native import TextureCache
                if self.gpu is None:
                    self.gpu = TextureCache()
                previous = imgui.get_current_context()
                # This binding's keepalive list is shared by ImGui contexts.
                # A child's new_frame clears it while the parent's draw list is
                # unfinished. Own those Python texture-ID objects through the
                # nested capture, including user-submitted images and failures.
                parent_texture_refs = tuple(previous._keepalive_cache)
                context = self._context(node, w, h)
                imgui.set_current_context(context)
                imgui.get_io().display_size = (w, h)
                self.imgui_input.apply(node, imgui.get_io())
                imgui.new_frame()
                self._begin_window(w, h)
            try:
                native.begin_frame(scope=node, width=w, cache=self, view_events=events)
                try:
                    result = renderer(input_value, **arguments, return_extras=True)
                finally:
                    native.end_frame()
                self.states[node] = result[2]
                self.associate(result[2], node)
            finally:
                if self.graphics:
                    self.imgui_input.finish(node)
                    imgui.get_window_draw_list().pop_clip_rect()
                    imgui.end()
                    imgui.render()
            measured_h = result[2].height if requested_h < 0 else requested_h
            if not math.isfinite(measured_h) or measured_h < 0 or measured_h > 8192:
                raise ValueError('measured cache height must be 0..8192 pixels')
            measured_h = min(arguments['max_height'], max(arguments['min_height'], measured_h))
            result[2].height = measured_h
            tint = result[2].get('tint', (0., 0., 0., 0.))
            background = tuple(tint) + ((1.,) if len(tint) == 3 else ())
            if len(background) != 4 or any(not math.isfinite(c) or not 0 <= c <= 1 for c in background):
                raise ValueError('prototype tint must be RGB or RGBA components in 0..1')
            if self.graphics:
                self.gpu.target(node, math.ceil(w), max(1, math.ceil(measured_h)))
                self.gpu.begin_packet(node)
                texture_refs = []
                for draw_list in imgui.get_draw_data().commands_lists:
                    commands, offset = [], 0
                    for command in draw_list.commands:
                        texture = command.texture_id
                        texture_refs.append(texture)
                        commands.append((int(texture), command.elem_count,
                                         tuple(command.clip_rect), offset, 0))
                        offset += command.elem_count
                    self.gpu.add_packet(node,
                        ctypes.string_at(draw_list.vtx_buffer_data, draw_list.vtx_buffer_size * 20),
                        ctypes.string_at(draw_list.idx_buffer_data, draw_list.idx_buffer_size * 4), commands)
                # Retained commands outlive this ImGui frame. A texture-ID object
                # may itself own its resource, so retain it until packet replacement.
                self.texture_refs[node] = tuple(texture_refs)
            self.graph.commit(node, result[:2], independent)
            self._retire()
            self.backgrounds[node] = background
            old_rect = self.graph.info(node)['rect']
            resized = self.graph.measured(node, w, measured_h)
            self.geometry.captured(node)
            if resized:
                self.layout_log.append((func.__name__, node, old_rect[2:], (w, measured_h)))
            if resized and self.request_frame:
                self.request_frame()
            self.regions[node] = self.staged_regions.pop(node)
            self.geometry.commit(node)
            self._last_pointer = None  # the retained hit geometry may have changed
            if result[0]:
                self.geometry.frozen.clear()
                self.graph.invalidate(_alias(input_value), exclude=node)
        except BaseException:
            self.geometry.abort(node)
            self.graph.abort(node)
            self.staged_regions.pop(node, None)
            self.events.setdefault(node, {}).update(events)
            raise
        finally:
            self.stack.pop()
            if previous is not None:
                imgui.set_current_context(previous)
                previous._keepalive_cache[:] = parent_texture_refs

    def _compose(self):
        if self.graphics and self.gpu:
            # Keep the complete bottom-up replay order, but save/restore host GL
            # state once for the batch rather than once for every cached view.
            nodes = [node for node in self.graph.composition_order() if node not in self.stack]
            if nodes:
                self.gpu.render_many([(node, self.backgrounds[node]) for node in nodes])
                for node in nodes:
                    self.graph.composed(node)

    def flush(self):
        """Run only dirty functions, then replay retained packets bottom-up."""
        for definition in self.renderers:
            func, old_code = definition
            if func.__code__ is not old_code:
                self.invalidate(func)
                definition[1] = func.__code__
        # A legitimate deep chain may need one upward layout wave per level.
        limit = max(64, len(self.records) * 2 + 1)
        for _ in range(limit):
            dirty = [node for node in self.graph.dirty_nodes() if node not in self.geometry.frozen]
            if not dirty:
                break
            for node in dirty:
                if self.graph.dirty(node):
                    self._execute(node, independent=True)
            self._retire()
        else:
            pending = [(self.records[node][2].__name__, node, self.graph.info(node)['rect'])
                       for node in self.graph.dirty_nodes()]
            raise RuntimeError(f'retained invalidation did not converge in {limit} passes: {pending}; '
                               f'recent layout changes: {list(self.layout_log)[-8:]}')
        self._compose()
        self._retire()

    def _retire(self):
        for node in self.graph.take_retired():
            self.geometry.retire(node)
            self.imgui_input.retire(node)
            self._last_pointer = None
            record = self.records.pop(node, None)
            if record:
                record[0].clear()
            if self.gpu:
                self.gpu.remove(node)
            if node in self.contexts:
                import meltygui_imgui as imgui
                previous = imgui.get_current_context()
                imgui.set_current_context(self.contexts[node])
                self.imgui_input.release_context_callbacks()
                imgui.destroy_context(self.contexts.pop(node))
                imgui.set_current_context(previous)
            for mapping in (self.invocations, self.events, self.regions, self.states,
                            self.backgrounds, self.texture_refs):
                mapping.pop(node, None)
            self.root_ids.discard(node)
            for key,value in tuple(self.window_ids.items()):
                if value==node:self.window_ids.pop(key)

    def region(self, key, rect):
        """Retain a local hit region; input is injected as view_events on wake."""
        self.staged_regions[self.current].append((key, tuple(rect)))

    def send(self, node, key, value=True):
        self.events.setdefault(node, {})[key] = value
        self.graph.invalidate_node(node)

    def _position(self, node):
        try:
            return self._positions[node]
        except KeyError:
            pass
        info = self.graph.info(node)
        x, y, _, _ = info['rect']
        if info['parent']:
            px, py = self._position(info['parent'])
            x, y = x + px, y + py
        if info['portal']:
            y += 28
        self._positions[node] = (x,y)
        return x, y

    def window_rect(self, node):
        x,y=self._position(node)
        _,_,w,h=self.graph.info(node)['rect']
        return x,y-28,w,h

    def _inside_clip(self, node, x, y):
        """Inline descendants respect cached ancestor boundaries; portals escape."""
        while node:
            info = self.graph.info(node)
            px, py = self._position(node)
            _, _, w, h = info['rect']
            if not (px <= x < px + w and py <= y < py + h):
                return False
            if info['portal']:
                break
            node = info['parent']
        return True

    def pointer(self, position, *, pressed=False, down=False, released=False):
        """Host calls this outside cached bodies. Movement only reexecutes a
        view when its retained hover state changes; window motion just places pixels.
        """
        sample = (tuple(position), self.origin, pressed, down, released)
        if sample == self._last_pointer:
            return
        self._last_pointer = sample
        x, y = position[0] - self.origin[0], position[1] - self.origin[1]
        if self.drag:
            node, dx, dy = self.drag
            if down and node in self.records:
                self.graph.move_window(node, x - dx, y - dy)
                self._positions.clear()
            else:
                self.drag = None
            return
        target = None
        # Windows occlude underlying content even where no control is present.
        portal_hit, hits = self.graph.hits(x, y)
        if pressed and portal_hit is not None:
            self.graph.raise_window(portal_hit)
        for node, px, py in hits:
            for key, (rx, ry, rw, rh) in self.regions.get(node, []):
                if px + rx <= x < px + rx + rw and py + ry <= y < py + ry + rh:
                    target = node, key
                    break
            if target:
                break
        if target != self.hover:
            if self.hover and self.hover[0] in self.records:
                self.send(self.hover[0], 'hover', None)
            if target:
                self.send(target[0], 'hover', target[1])
            self.hover = target
        if pressed and target:
            self.send(*target)

    def process_host_input(self, *, drag_position=None):
        """Uncached adapter at the host boundary, never inside a cached body.

        Route raw ImGui IO and retained hit regions. Claim gestures through the host
        router so a retained control cannot also drag the outer OS window.
        """
        import meltygui_imgui as imgui
        self.imgui_input.process(imgui.get_io())
        # Resolve actual widget ownership before allowing background gestures.
        self.flush()
        if self.geometry.gesture is None:
            self.pointer(imgui.get_mouse_pos())
        claims=self.imgui_input.pointer_claims.get(self.imgui_input.mouse_owner,(False,False))
        claims=(claims[0] or self.hover is not None or self.drag is not None,claims[1])
        self.geometry.pointer(imgui.get_mouse_pos(),pressed=imgui.is_mouse_clicked(0),
                              down=imgui.is_mouse_down(0),released=imgui.is_mouse_released(0),
                              right_pressed=imgui.is_mouse_clicked(1),right_down=imgui.is_mouse_down(1),
                              right_released=imgui.is_mouse_released(1),reverse=imgui.is_mouse_double_clicked(1),
                              widget_claim=claims,drag_position=(imgui.get_mouse_pos()
                                  if drag_position is None else drag_position))
        if self.geometry.gesture is None:
            self.pointer(imgui.get_mouse_pos(), pressed=imgui.is_mouse_clicked(0),
                         down=imgui.is_mouse_down(0), released=imgui.is_mouse_released(0))
        if self.geometry.native and not imgui.is_mouse_down(1) and self.geometry.gesture is None:
            self.geometry.native.release()

    def register_host_pointer(self, handler, position):
        x, y = position[0] - self.origin[0], position[1] - self.origin[1]
        occupied = self.hover is not None or self.drag is not None
        if not occupied:
            occupied = any(wx <= x < wx + w and wy <= y < wy + h + 28
                           for wx, wy, w, h in
                           (self.window_rect(node) for node in self.graph.windows()))
        left, right = self.imgui_input.pointer_claims.get(self.imgui_input.mouse_owner, (False, False))
        subscriptions = []
        if occupied or left or self.geometry.claim[0]:
            subscriptions.extend(['left_mouse_dragged', 'left_mouse_double_dragged',
                                  'left_mouse_double_clicked'])
        if occupied or right or self.geometry.claim[1]:
            subscriptions.extend(['right_mouse_dragged', 'right_mouse_double_dragged'])
        if subscriptions:
            handler.register_hovered(
                ('gui-pointer', id(self)), subscriptions)

    def present(self, origin):
        import meltygui_imgui as imgui
        self.origin = origin
        dl = imgui.get_window_draw_list()
        for node in sorted(self.root_ids):
            self._image(dl, node, origin)
        self.geometry.paint(dl,origin)
        for node in self.graph.windows():
            x, y, w, h = self.window_rect(node)
            x += origin[0]
            y += origin[1]
            left,top,right,bottom=(math.floor(v+.5) for v in (x,y,x+w,y+28+h))
            dl.add_rect_filled(left+5,top+5,right+5,bottom+5,0x66000000,5)
            dl.add_rect_filled(left,top,right,top+28,0xFF65513B,5)
            dl.add_text(left+9,top+5,0xFFFFFFFF,'Owned @gui window (drag title)')
            self._draw_image(dl,node,x,y+28,w,h)
            self.geometry.paint(dl,origin,portal=node)

    def _draw_image(self,dl,node,x,y,w,h):
        texture,tw,th=self.gpu.image(node)
        left,top,right,bottom=(math.floor(v+.5) for v in (x,y,x+w,y+h))
        dl.push_clip_rect(left,top,right,bottom,True)
        try:
            dl.add_image(texture,(left,top),(left+tw,top+th),(0,1),(1,0))
        finally:
            dl.pop_clip_rect()

    def _image(self, dl, node, origin):
        x, y, w, h = self.graph.info(node)['rect']
        x += origin[0]
        y += origin[1]
        self._draw_image(dl,node,x,y,w,h)

    def close(self):
        if not self.closed:
            self.graph.clear()
            self._retire()
            if self.gpu:
                self.gpu.close()
                self.gpu = None
            self.imgui_input.host_io = None
            self.imgui_input.clipboard = None
            self.imgui_input.characters.clear()
            self.imgui_input.text = ()
            self.imgui_input.cache = None
            self.geometry.axes = ()
            self.geometry.transactions.clear()
            self.geometry.native = None
            self.geometry.cache = None
            self.renderers.clear()
            self.graph = None
            self.closed = True
