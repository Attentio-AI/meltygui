"""Throwaway retained @gui graph. Explicit invalidation; no model hashing.

Rust owns graph reconciliation, dirty scheduling, result mailboxes, command
storage, texture allocation and GL replay. This bridge owns Python callables,
draw capture scopes in the host ImGui context and retained input invalidation.
Cache boundaries accept a constrained width and fixed or measured height.
"""
from contextlib import contextmanager
from collections import deque, OrderedDict, defaultdict
from functools import wraps
from pathlib import Path
from types import FunctionType
import ctypes
import math
import inspect
import time

from meltygui.core.rendering.gui_prototype import _bind_native, _new_native, _renderer_name, _check_position_arguments
from meltygui.core.rendering.gui_imgui_input import ImGuiInput


def _same(a, b):
    """Cheap argument semantics: exact primitives by value, objects by identity."""
    return a is b or (type(a) is type(b) and type(a) in (str, int, float, bool, type(None)) and a == b)


def _alias(value):
    if isinstance(value, (str, Path)):
        return 'path:' + str(value)
    return f'object:{id(value)}'


class RetainedGui:
    def __init__(self, *, graphics=True, state_registry=None, calibration=True):
        if graphics:
            import meltygui_imgui as imgui
            if not hasattr(imgui, 'DrawCapture'):
                raise RuntimeError(
                    'The retained GUI prototype requires meltygui-imgui 2.0.4 with DrawCapture. '
                    'Install the matching binding wheel into the Python environment running '
                    'this app; rebuilding the Rust extension does not update the binding. '
                    'See docs/GUI_PROTOTYPE.md for setup instructions.')
        from meltygui.core.rendering._gui_native import RetainedGraph, Identities
        self.identities = Identities(state_registry)
        self.uniques = {}
        self.caller_keys = {}
        self.graph = RetainedGraph()
        self.graphics = graphics
        self.gpu = None
        from meltygui.core.rendering.gui_calibration import Calibration
        self.calibration = Calibration(enabled=graphics and calibration)
        from meltygui.core.runtime.toggles import Toggles
        self._auto_cache = Toggles.Rust.auto_cache
        from meltygui.core.rendering.gui_cache_policy import RasterPolicy
        self.raster_policy = RasterPolicy()
        self.stack = []
        self.records = {}
        self.bindings = {}
        self.scroll_tiles = OrderedDict()
        self.capture_children = defaultdict(set)
        self.capture_info = {}
        self.scroll_tile_bytes = 0
        self.max_scroll_tiles = 256
        self.scroll_tile_budget = 32 * 1024 * 1024
        self._scroll_tile_revision = 0
        self._eviction_inputs = None
        self.invocations = {}
        self.regions = {}
        self.staged_regions = {}
        self.events = {}
        self.renderers = []
        self.captures = {}
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
        self._frame_started = None
        self.layout_log = deque(maxlen=64)
        self.imgui_input = ImGuiInput(self)
        from meltygui.core.rendering.gui_layout_prototype import GuiGeometry
        self.geometry = GuiGeometry(self)

    @property
    def costs(self):
        """Immutable local cost snapshot; CPU and GPU estimates stay separate."""
        return self.calibration.snapshot

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
        if self._frame_started is None:
            self._frame_started = time.monotonic()
        self.identities.begin_frame()
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

    def gui(self, func=None, *, left=None, top=None, width=300, height=100, min_height=0, max_height=8192,
            inject=None, events=(), state_id=None, no_save=(), scroll=False, **decoration):
        _check_position_arguments(decoration, no_save)
        if func is None:
            return lambda fn: self.gui(fn, left=left, top=top, width=width, height=height, min_height=min_height,
                                       max_height=max_height, inject=inject, events=events,
                                       state_id=state_id, no_save=no_save, scroll=scroll, **decoration)
        decorated_left, decorated_top = left, top
        parameters = inspect.signature(func).parameters
        def position_default(name, decorated):
            parameter = parameters.get(name)
            if decorated is None and parameter is not None and parameter.default is not inspect.Parameter.empty:
                return parameter.default
            return decorated
        left, top = position_default('left', left), position_default('top', top)
        def refresh_positions():
            nonlocal parameters, left, top
            previous = (left, top)
            parameters = inspect.signature(func).parameters
            left = position_default('left', decorated_left)
            top = position_default('top', decorated_top)
            if previous == (left, top):
                return
            defaults = {name: value for name, value in (('left', left), ('top', top)) if value is not None}
            for node, (_, _, renderer_func) in self.records.items():
                if renderer_func is not func:
                    continue
                arguments = self.invocations[node][1]
                caller = {key: arguments[key] for key in self.caller_keys[node] if key in arguments}
                self.states[node].configure(defaults, caller, {})
                if left is None or top is None:
                    parent = self.graph.parent(node)
                    if parent: self.graph.invalidate_node(parent)
        renderer_id = self.identities.renderer(_renderer_name(func, state_id))
        definition = [func, func.__code__, refresh_positions]
        self.renderers.append(definition)

        @wraps(func)
        def wrapper(input_value=None, **kwargs):
            if self.graphics and not self.stack:
                import meltygui_imgui as imgui
                if not imgui.is_frame_active():
                    with self.capture_frame():
                        return wrapper(input_value, **kwargs)
            _check_position_arguments(kwargs)
            if self.closed:
                raise RuntimeError('retained runtime is closed')
            if self.frame_roots is None and not self.identities.active:
                self.identities.begin_frame()
            keyed = 'key' in kwargs or 'key' in decoration
            key = kwargs.pop('key', decoration.get('key'))
            name = kwargs.get('name', decoration.get('name'))
            identity_handle = self.identities.handle(renderer_id, key, name, keyed)
            portal = kwargs.pop('melty_window', False)
            scrollable = kwargs.pop('scroll', scroll)
            if identity_handle not in self.bindings:
                native = _new_native(graphics=self.graphics, identities=self.identities)
                renderer = _bind_native(native, func, inject=inject, events=events,
                                        state_id=state_id, no_save=no_save, **decoration)
                unique = self.identities.unique(identity_handle)
                self.bindings[identity_handle] = native, renderer, func, native.state(unique), unique
            native, renderer, _, state, unique = self.bindings[identity_handle]
            caller = dict(kwargs)
            if kwargs.pop('glfw_window', False):
                raise NotImplementedError('This experiment implements internal windows; native child surfaces are not connected yet')
            initial = kwargs.pop('initial', decoration.get('initial', {}))
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
            allocation = self.geometry.allocation(parent) if not portal else None
            if self.graphics and (parent or self.auto_layout) and not portal:
                import meltygui_imgui as imgui
                x, y = imgui.get_cursor_screen_pos()
            else:
                x, y = (0, 0)
            if allocation is not None:
                ax, ay, w, h = allocation
                px, py = self._position(parent)
                x, y = ax-px, ay-py
            if w is None:
                if parent:
                    w = self.graph.constraints(parent)[0] - x
                elif self.graphics:
                    w = imgui.get_content_region_available()[0]
                else:
                    raise ValueError('width=None requires a parent or graphics host')
            auto_height = h is None
            if auto_height: h = state.get('height', 0.) or 0.
            if (not math.isfinite(w) or not 0 < w <= 8192
                    or not math.isfinite(h) or h < 0):
                raise ValueError('cached boundaries require positive width and nonnegative height')
            caller = {k: v for k, v in caller.items() if not (k in ('left', 'top', 'width', 'height') and v is None)}
            if allocation is not None:
                caller.update(width=w, height=h, left=x, top=y)
            state.configure({'width': w, 'height': h,
                             'left': float(x) if left is None else left,
                             'top': float(y) if top is None else top,
                             'scroll_offset': (0., 0.), 'content_offset': (0., 28. if portal else 0.)},
                            caller, initial)
            x, y = state.left, state.top
            w, h = state.width, state.height
            if not math.isfinite(w) or not 0 < w <= 8192 or not math.isfinite(h) or h < 0:
                raise ValueError('cached boundaries require positive width and nonnegative height')
            w, h = float(w), min(maximum, max(minimum, float(h)))
            arguments = {**decoration, **kwargs, 'width': w, 'height': None if auto_height else h,
                         'min_height': minimum, 'max_height': maximum}
            node = self.graph.declare(parent, 0, identity_handle, input_value, arguments,
                                      (float(x), float(y), w, -1. if auto_height else h), portal, state, scrollable)
            previous = self.invocations.get(node)
            pending = self.graph.pending(node)
            if previous is not None:
                old_input, old_args = previous
                changed_input = not pending and not _same(old_input, input_value)
                if ((changed_input and not _same(self.graph.result(node, False)[1], input_value)) or old_args.keys() != arguments.keys()
                        or any(not _same(old_args[k], v) for k, v in arguments.items()
                               if k not in ('left','top','width','height','scroll_offset','content_offset'))):
                    self.graph.invalidate_node(node)
                if not pending and old_input is not input_value:
                    self.graph.dissociate(node, _alias(old_input))
            self.invocations[node] = input_value, arguments.copy()
            self.caller_keys[node] = tuple(caller)
            if node not in self.records:
                self.uniques[node] = unique
                self.records[node] = native, renderer, func
                self.states[node] = state
                self.associate(func, node)
                self.associate(wrapper, node)
                self.graph.associate(node, f'renderer:{renderer_id}', func)
            # Identity aliases remain registered until replacement or retirement.
            # Path-like values can supply changing text, so resolve those again.
            if previous is None or previous[0] is not input_value or isinstance(input_value, (str, Path)):
                self.associate(input_value, node)
            self.geometry.bind(node, parent, portal=portal)
            if parent == 0:
                self.root_ids.add(node)
                if self.frame_roots is not None:
                    self.frame_roots.add(node)
            # An independently produced return is consumed before reexecuting.
            # In particular, an old scalar caller argument cannot overwrite it.
            # Cull whole scroll tiles. A non-scrolling parent's packet needs
            # its children's textures even outside the ancestor viewport.
            # Unmeasured auto-height tiles need one capture before culling.
            if (self.graph.dirty(node) and not pending and node not in self.geometry.frozen
                    and (not parent or not self.graph.is_scrollable(parent)
                         or (auto_height and node not in self.backgrounds)
                         or self.graph.visible(node))):
                self._execute(node, independent=False)
            # Root layout settles before the immediate caller places later items.
            if not parent:
                self.flush()
            _, _, w, h = self.graph.rect(node)
            if self.graphics:
                if parent and not portal:
                    import meltygui_imgui as imgui
                    imgui.set_cursor_screen_pos((x, y))
                    if not self.graph.is_scrollable(parent):
                        self._draw_image(imgui.get_window_draw_list(),node,x,y,w,h)
                    imgui.dummy(w, h)
                elif self.auto_layout and not parent and not portal:
                    import meltygui_imgui as imgui
                    imgui.set_cursor_screen_pos((x, y))
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

    @contextmanager
    def capture_frame(self):
        """Direct graph users get one host frame; hosted apps already own theirs."""
        import meltygui_imgui as imgui
        if not self.graphics or imgui.is_frame_active():
            yield
            return
        imgui.new_frame()
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(*imgui.get_io().display_size)
        imgui.begin(f'##retained-host-{id(self)}', flags=(imgui.WINDOW_NO_TITLE_BAR |
                    imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR |
                    imgui.WINDOW_NO_SAVED_SETTINGS | imgui.WINDOW_NO_BACKGROUND))
        try:
            yield
        finally:
            imgui.end()
            imgui.render()

    def _capture(self, node):
        import meltygui_imgui as imgui
        if node not in self.captures:
            self.captures[node] = imgui.DrawCapture(f'gui-capture-{id(self)}-{node}')
        return self.captures[node]

    def _execute(self, node, *, independent):
        native, renderer, func = self.records[node]
        input_value, arguments = self.graph.invocation(node)
        # Invocation supplies canonical height/auto-height. Keep its read before
        # the geometry sync, without materializing unrelated info dictionary fields.
        old_rect = self.graph.rect(node)
        w = old_rect[2]
        requested_h = -1. if arguments['height'] is None else arguments['height']
        # Measure without the old texture's clip truncating newly grown content.
        # This is a logical capture viewport, not an 8192px texture allocation.
        h = 8192 if requested_h < 0 else max(1, requested_h)
        events = self.events.pop(node, {})
        self.graph.begin(node)
        self.stack.append(node)
        self.staged_regions[node] = []
        capture = None
        reference_start = 0
        try:
            self.geometry.begin(node)
            if self.graphics:
                import meltygui_imgui as imgui
                from meltygui.core.rendering._gui_native import TextureCache
                if self.gpu is None:
                    self.gpu = TextureCache()
                scope = self._capture(node)
                x, y = self._position(node)
                reference_start = len(imgui.get_current_context()._keepalive_cache)
                scope.begin(w, h, self.origin[0] + x, self.origin[1] + y)
                capture = scope
            try:
                native.begin_frame(width=w, cache=self, view_events=events, identity_frame=False)
                try:
                    result = renderer(input_value, **arguments, return_extras=True,
                                      _gui_unique=self.uniques[node], _gui_explicit=self.caller_keys[node])
                finally:
                    native.end_frame()
                state_changed = self.states[node] is not result[2]
                self.states[node] = result[2]
                # A declaration has state before its first execution, including
                # culled views. Register that first alias before skipping repeats.
                if state_changed or node not in self.backgrounds:
                    self.associate(result[2], node)
            finally:
                if self.graphics:
                    try:
                        self.imgui_input.finish(node, capture)
                    finally:
                        capture.end()
            measured_h = (result[2].content_height if self.graphics else result[2].height) if requested_h < 0 else requested_h
            if not math.isfinite(measured_h) or measured_h < 0 or measured_h > 8192:
                raise ValueError('measured cache height must be 0..8192 pixels')
            measured_h = min(arguments['max_height'], max(arguments['min_height'], measured_h))
            # Graph measurement writes resolved size without authoring an override.
            tint = result[2].get('tint', arguments.get('tint', (0., 0., 0., 0.)))
            background = tuple(tint) + ((1.,) if len(tint) == 3 else ())
            if len(background) != 4 or any(not math.isfinite(c) or not 0 <= c <= 1 for c in background):
                raise ValueError('prototype tint must be RGB or RGBA components in 0..1')
            if self.graphics:
                if self._auto_cache:
                    self.gpu.reference(node, math.ceil(w), max(1, math.ceil(measured_h)))
                    if not self.graph.parent(node) or self.graph.is_portal(node):
                        self.gpu.raster(node, True)
                else:
                    self.gpu.target(node, math.ceil(w), max(1, math.ceil(measured_h)))
                self.gpu.begin_packet(node)
                texture_refs = []
                for draw_list in capture.draw_lists:
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
            resized = self.graph.measured(node, w, measured_h)
            self.geometry.captured(node)
            if resized:
                self.layout_log.append((func.__name__, node, old_rect[2:], (w, measured_h)))
            if resized and self.request_frame:
                self.request_frame()
            self.regions[node] = self.staged_regions.pop(node)
            self.geometry.commit(node)
            if self.graphics:
                self._track_capture(node)
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
            if capture is not None:
                from meltygui.core.rendering.gui_draw_capture import release_captured_references
                release_captured_references(capture, reference_start)

    def _track_capture(self, node):
        parent = self.graph.parent(node)
        owner, ancestor = node, parent
        while ancestor and not self.graph.is_scrollable(ancestor):
            owner, ancestor = ancestor, self.graph.parent(ancestor)
        owner = owner if ancestor else None
        # Empty command captures still own draw buffers and count toward residency.
        size = max(1, self.gpu.capture_bytes(node))
        old_capture = self.capture_info.get(node)
        if old_capture != (parent, size, owner):
            self._scroll_tile_revision += 1
        difference = size - (old_capture[1] if old_capture else 0)
        self.capture_info[node] = parent, size, owner
        self.capture_children[parent].add(node)
        if owner is not None:
            self.scroll_tile_bytes += difference
            self.scroll_tiles[owner] = self.scroll_tiles.get(owner, 0) + difference

    def _select_rasters(self):
        from meltygui.core.runtime.toggles import Toggles
        enabled = Toggles.Rust.auto_cache
        if enabled != self._auto_cache:
            self._auto_cache = enabled
            # A global switch is a rare full transition. No bodies or capture buffers
            # change; ordinary policy ticks consume a bounded affected queue.
            for node in self.backgrounds:
                mandatory = not self.graph.parent(node) or self.graph.is_portal(node)
                if self.gpu.raster(node, mandatory or not enabled):
                    self.graph.request_composition(node)
                    self._track_capture(node)
            self.gpu.shared_pass(enabled)
        if not enabled:
            return
        for observation in self.gpu.take_workloads(64):
            node = observation[0]
            if node not in self.backgrounds:
                continue
            parent = self.graph.parent(node)
            if not parent or self.graph.is_portal(node):
                continue
            ancestor, covered = parent, False
            while ancestor:
                if self.graph.is_portal(ancestor):
                    break
                if self.graph.parent(ancestor) and self.gpu.is_raster(ancestor):
                    covered = True
                    break
                ancestor = self.graph.parent(ancestor)
            _, _, w, h = self.graph.rect(node)
            raster = self.gpu.is_raster(node)
            selected = self.raster_policy.choose(observation, self.costs,
                self.calibration.views.get(node), pixels=math.ceil(w)*math.ceil(h),
                raster=raster, covered=covered)
            if selected != raster:
                try:
                    changed = self.gpu.raster(node, selected)
                except RuntimeError as error:
                    if selected and str(error) == 'prototype texture budget exceeded':
                        continue
                    raise
                if changed:
                    self.raster_policy.applied(node, selected)
                    self.graph.request_composition(node)
                    self._track_capture(node)
                    if selected:
                        # Only a representation transition walks this subtree.
                        # A cached ancestor makes descendant raster storage
                        # redundant until that ancestor is demoted again.
                        pending = list(self.capture_children.get(node, ()))
                        while pending:
                            child = pending.pop()
                            if self.graph.is_portal(child):
                                continue
                            pending.extend(self.capture_children.get(child, ()))
                            if self.gpu.raster(child, False):
                                self.raster_policy.applied(child, False)
                                self.graph.request_composition(child)
                                self._track_capture(child)

    def _compose(self):
        if self.graphics and self.gpu:
            self._select_rasters()
            # Keep the complete bottom-up replay order, but save/restore host GL
            # state once for the batch rather than once for every cached view.
            nodes = [node for node in self.graph.composition_order() if node not in self.stack]
            if nodes:
                for node in nodes:
                    placements = self.graph.take_placements(node)
                    if self.graph.is_scrollable(node):
                        children = self.graph.visible_children(node)
                        self.gpu.children(node, children)
                        for child, *_ in children:
                            if child in self.scroll_tiles:
                                self.scroll_tiles.move_to_end(child)
                    else:
                        for child, x, y, w, h in placements:
                            if child in self.backgrounds:
                                self.gpu.place_child(node, child, (x, y, w, h), (x, y, x+w, y+h))
                self.gpu.render_many([(node, self.backgrounds[node]) for node in nodes])
                for node in nodes:
                    self.graph.composed(node)

    def _drop_capture(self, node):
        self.raster_policy.retire(node)
        info = self.capture_info.pop(node, None)
        if info is not None:
            self._scroll_tile_revision += 1
            parent, size, owner = info
            self.capture_children[parent].discard(node)
            if not self.capture_children[parent]: self.capture_children.pop(parent, None)
            if owner is not None:
                self.scroll_tile_bytes -= size
                remaining = self.scroll_tiles.get(owner, 0) - size
                if remaining > 0: self.scroll_tiles[owner] = remaining
                else: self.scroll_tiles.pop(owner, None)
        self.capture_children.pop(node, None)
        if self.gpu: self.gpu.remove(node)
        self.imgui_input.retire(node)
        if node in self.captures:
            self.captures.pop(node).close()
        self.texture_refs.pop(node, None)
        self.backgrounds.pop(node, None)

    def _evict_scroll_tiles(self):
        """Evict offscreen captures, including nested widgets, but keep their state."""
        if not self.graphics or (len(self.scroll_tiles) <= self.max_scroll_tiles
                                and self.scroll_tile_bytes <= self.scroll_tile_budget):
            return
        inputs = (self.graph.visibility_revision(), self._scroll_tile_revision,
                  self.max_scroll_tiles, self.scroll_tile_budget,
                  frozenset(self.imgui_input.active), self.imgui_input.capture, self.imgui_input.focus)
        if inputs == self._eviction_inputs:
            return
        protected = set(self.imgui_input.active) | set(self.graph.windows())
        protected.update(n for n in (self.imgui_input.capture, self.imgui_input.focus) if n is not None)
        for node in tuple(self.scroll_tiles):
            if (len(self.scroll_tiles) <= self.max_scroll_tiles
                    and self.scroll_tile_bytes <= self.scroll_tile_budget):
                break
            if node not in self.scroll_tiles or self.graph.visible(node): continue
            pending, subtree = [node], []
            while pending:
                current = pending.pop()
                subtree.append(current)
                pending.extend(self.capture_children.get(current, ()))
            if any(n in protected for n in subtree): continue
            for current in reversed(subtree):
                self._drop_capture(current)
                self.graph.invalidate_node(current)
                self.graph.composed(current)
        # Record the remaining residency after successful eviction, including
        # over-budget sets whose only remaining captures are protected.
        self._eviction_inputs = (inputs[0], self._scroll_tile_revision, *inputs[2:])

    def flush(self):
        """Run only dirty functions, then replay retained packets bottom-up."""
        for definition in self.renderers:
            func, old_code, refresh_positions = definition
            if func.__code__ is not old_code:
                refresh_positions()
                self.invalidate(func)
                definition[1] = func.__code__
        if self.graphics:
            import meltygui_imgui as imgui
            # Representation-only replay does not submit widgets and must not
            # invent an extra host frame that would expire the active widget ID.
            if not imgui.is_frame_active() and self.graph.dirty_nodes():
                with self.capture_frame():
                    return self.flush()
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
            pending = [(self.records[node][2].__name__, node, self.graph.rect(node))
                       for node in self.graph.dirty_nodes()]
            raise RuntimeError(f'retained invalidation did not converge in {limit} passes: {pending}; '
                               f'recent layout changes: {list(self.layout_log)[-8:]}')
        self._compose()
        self._evict_scroll_tiles()
        self._retire()

    def _retire(self):
        for node in self.graph.take_retired():
            self.calibration.retire(node)
            self.geometry.retire(node)
            self._last_pointer = None
            self.records.pop(node, None)
            self._drop_capture(node)
            for mapping in (self.invocations, self.events, self.regions, self.states, self.uniques, self.caller_keys,
                            self.backgrounds, self.texture_refs):
                mapping.pop(node, None)
            self.root_ids.discard(node)

    def region(self, key, rect):
        """Retain a local hit region; input is injected as view_events on wake."""
        self.staged_regions[self.current].append((key, tuple(rect)))

    def send(self, node, key, value=True):
        self.events.setdefault(node, {})[key] = value
        self.graph.invalidate_node(node)

    def _position(self, node):
        return self.graph.origin(node) if node else (0., 0.)

    def position(self, node, *, relative_to=0, content=False):
        """Convert viewport origins without executing any view bodies."""
        return self.graph.origin(node, relative_to, content)

    def scroll_at(self, position, delta):
        """Scroll the innermost viewport able to consume this content-space delta."""
        self.calibration.input()
        changed = self.graph.scroll_at(position[0]-self.origin[0], position[1]-self.origin[1], *delta)
        if changed:
            self._last_pointer = None
            if self.request_frame: self.request_frame()
        return changed

    def window_rect(self, node):
        x,y=self._position(node)
        _,_,w,h=self.graph.rect(node)
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
        self.calibration.input()
        x, y = position[0] - self.origin[0], position[1] - self.origin[1]
        if self.drag:
            node, dx, dy = self.drag
            if down and node in self.records:
                parent = self.graph.parent(node)
                px, py = self._position(parent)
                self.states[node].left = x - dx - px
                self.states[node].top = y - dy - py
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
        self._frame_started = time.monotonic()
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
            if not self.graph.is_portal(node):
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

        if self.gpu and self._frame_started is not None:
            self.calibration.maintenance(self.gpu, frame_seconds=time.monotonic()-self._frame_started,
                pressure=bool(self.drag or self.imgui_input.active or self.imgui_input.capture is not None))
        self._frame_started = None

    def _draw_image(self,dl,node,x,y,w,h):
        texture,tw,th=self.gpu.image(node)
        left,top,right,bottom=(math.floor(v+.5) for v in (x,y,x+w,y+h))
        dl.push_clip_rect(left,top,right,bottom,True)
        try:
            dl.add_image(texture,(left,top),(left+tw,top+th),(0,1),(1,0))
        finally:
            dl.pop_clip_rect()

    def _image(self, dl, node, origin):
        x, y, w, h = self.graph.rect(node)
        x += origin[0]
        y += origin[1]
        self._draw_image(dl,node,x,y,w,h)

    def prepare_save(self):
        self.identities.prepare_save()

    def close(self):
        if not self.closed:
            self.prepare_save()
            self.graph.clear()
            self._retire()
            self.calibration.close()
            if self.gpu:
                self.gpu.close()
                self.gpu = None
            self.imgui_input.characters.clear()
            self.imgui_input.text = ()
            self.imgui_input.cache = None
            self.geometry.axes = ()
            self.geometry.transactions.clear()
            self.geometry.native = None
            self.geometry.cache = None
            self.renderers.clear()
            for native, _, _, _, _ in self.bindings.values(): native.clear()
            self.bindings.clear()
            self.identities.clear()
            self.graph = None
            self.closed = True
