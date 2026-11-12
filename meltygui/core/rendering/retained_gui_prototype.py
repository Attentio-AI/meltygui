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


# Lever: the longest string still treated as a path. A longer one is a value
# (a whole file's text): aliasing it by content would copy and hash it on
# every declaration, so it aliases by identity like any other object.
PATH_ALIAS = 4096


def _alias(value):
    if isinstance(value, Path) or (isinstance(value, str) and len(value) <= PATH_ALIAS):
        return 'path:' + str(value)
    return f'object:{id(value)}'


# Absent tint, distinct from every caller value including None.
_MISSING = object()
# Background of a view without a tint: transparent.
_CLEAR = (0., 0., 0., 0.)


class _Binding:
    """One view identity: the definition's shared native engine, this
    identity's handle and state. The native declaration table remembers the
    identity's last resolved call under the same handle."""
    __slots__ = ('native', 'renderer', 'call', 'func', 'state', 'unique', 'handle')

    def __init__(self, native, renderer, func, state, unique, handle):
        self.native, self.renderer, self.func = native, renderer, func
        # The native renderer the executor calls directly.
        self.call = renderer.__gui_native__
        self.state, self.unique, self.handle = state, unique, handle


class RetainedGui:
    def __init__(self, *, graphics=True, state_registry=None, calibration=True, input=ImGuiInput,
                 texture_bytes=None):
        # Lever: bytes of node textures the GPU cache may hold (None: the native default).
        self.texture_bytes = texture_bytes
        self.imgui = None
        # The binding's texture keepalive list, resolved on first capture.
        self._keepalive = None
        if graphics:
            import meltygui_imgui as imgui
            self.imgui = imgui
            if not hasattr(getattr(imgui, 'DrawCapture', None), 'begin_view'):
                raise RuntimeError(
                    'The retained GUI prototype requires meltygui-imgui 2.0.5 with DrawCapture.begin_view. '
                    'Install the matching binding wheel into the Python environment running '
                    'this app; rebuilding the Rust extension does not update the binding. '
                    'See docs/GUI_PROTOTYPE.md for setup instructions.')
        from meltygui.core.rendering._gui_native import RetainedGraph, Identities, Declarations
        self.identities = Identities(state_registry)
        self.uniques = {}
        self.caller_keys = {}
        self.graph = RetainedGraph()
        # Remembered resolutions of repeated declarations, declared natively.
        self.declared = Declarations(self.graph, self.identities, self.imgui)
        self.graphics = graphics
        self.gpu = None
        from meltygui.core.rendering.gui_calibration import Calibration
        self.calibration = Calibration(enabled=graphics and calibration)
        from meltygui.core.runtime.toggles import Toggles
        self._toggles = Toggles
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
        # Node to its identity binding, so retirement can drop the memoized call.
        self.declarations = {}
        # Per-definition [runtime, renderer] pairs, emptied by close().
        self.shared_natives = []
        self.regions = {}
        self.staged_regions = {}
        self.events = {}
        # Views declared with `listen=`: node to the event names it takes
        # (gui_imgui_input.py). Empty until an app declares one.
        self.listeners = {}
        # Views declared with `cursor=`: node to the pointer shape over it.
        self.cursors = {}
        # Views a worker thread asked to run again (`wake`), drained by flush().
        self.woken = deque()
        # For threads without the interpreter (`waker`): what hands out their handles, and what
        # turns the tokens they queued into `wake` calls. None until a view asks for a waker.
        self.wakers = self.drain = None
        self.renderers = []
        self.captures = {}
        self.states = {}
        self.backgrounds = {}
        self.texture_refs = {}
        # Set by melty_colors(): the raster follows the host's glyph settings each frame.
        self.melty = False
        self.auto_layout = False
        # Hosts that declare retained views from inside an immediate tree (the
        # legacy bridge, gui_compat.py): a root's image is drawn where it is
        # declared, in the caller's draw list, instead of by present().
        self.inline_roots = False
        # Host hook, called with (node, parent) by every declaration that
        # reaches the bridge (the first one of an identity always does).
        self.on_declare = None
        # Host hook, called with (node, error) when a view Rust ran on its own
        # raises. Without one the error leaves flush(), as before.
        self.on_failure = None
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
        # `input` decides which views execute for pointer and keyboard input.
        self.imgui_input = input(self)
        from meltygui.core.rendering.gui_layout_prototype import GuiGeometry
        self.geometry = GuiGeometry(self)
        # Runs one view execution natively over these same dictionaries and
        # calls the hooks below (_layout_begin, _background, ...) for the rest.
        from meltygui.core.rendering._gui_native import Executor
        from meltygui.core.rendering.gui_imgui_input import _NO_CLAIM
        self.executor = Executor(self, self.graph, graphics, self.imgui_input, self.stack, self.events,
                                 self.backgrounds, self.texture_refs, self.staged_regions, self.regions,
                                 self.capture_info, self.geometry.changes, self.imgui_input.right_active,
                                 self.imgui_input.pointer_claims, self.imgui_input.active, _NO_CLAIM, _CLEAR)
        self.executor.set_origin(self._origin)
        self.executor.set_auto_cache(self._auto_cache)
        # A dirty child declared from its remembered call runs there directly.
        self.declared.set_executor(self.executor)

    @property
    def origin(self):
        """Where presentation places this cache's content; captures follow it."""
        return self._origin

    @origin.setter
    def origin(self, value):
        self._origin = value
        executor = getattr(self, 'executor', None)
        if executor is not None:
            executor.set_origin(value)

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

    def wake(self, node):
        """Thread safe: run `node` again on the next frame, for a view whose
        background result arrived. The only cache method a worker may call."""
        self.woken.append(node)
        if self.request_frame:
            self.request_frame()

    def waker(self, node):
        """`wake(node)` for a thread that never takes the interpreter lock:
        a (handle, token) pair it calls by C ABI (`glfw_utils.waker`). What
        it queues joins `woken` at the next flush, of any cache."""
        if self.wakers is None:
            from meltygui.core.windowing import glfw_utils
            self.wakers, self.drain = glfw_utils.waker, glfw_utils.drain_wakes
        return self.wakers(self, node)

    @contextmanager
    def frame(self):
        """Reconcile declarations made by the external, uncached host."""
        if self.frame_roots is not None:
            raise RuntimeError('nested retained host frame')
        if self._frame_started is None:
            self._frame_started = time.monotonic()
        self.identities.begin_frame()
        # A view follows its caller within a few frames of an edit the caller did not take (retained.rs, `SETTLE`).
        if self.graph.settle() and self.request_frame:
            self.request_frame()
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
            inject=None, events=(), state_id=None, no_save=(), scroll=False, place=None, listen=None,
            cursor=None, **decoration):
        _check_position_arguments(decoration, no_save)
        if func is None:
            return lambda fn: self.gui(fn, left=left, top=top, width=width, height=height, min_height=min_height,
                                       max_height=max_height, inject=inject, events=events,
                                       state_id=state_id, no_save=no_save, scroll=scroll, place=place,
                                       listen=listen, cursor=cursor, **decoration)
        decorated_left, decorated_top = left, top
        parameters = inspect.signature(func).parameters
        def position_default(name, decorated):
            parameter = parameters.get(name)
            if decorated is None and parameter is not None and parameter.default is not inspect.Parameter.empty:
                return parameter.default
            return decorated
        left, top = position_default('left', left), position_default('top', top)
        # Bumped when a source edit changes the position defaults, so memoized
        # declarations resolved from the old defaults are not reused.
        generation = [0]
        def refresh_positions():
            nonlocal parameters, left, top
            previous = (left, top)
            parameters = inspect.signature(func).parameters
            left = position_default('left', decorated_left)
            top = position_default('top', decorated_top)
            if previous == (left, top):
                return
            generation[0] += 1
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
        graphics, imgui, bindings = self.graphics, self.imgui, self.bindings
        # One native runtime and compiled parameter plan for every identity of
        # this definition; each identity only adds its own state. close()
        # empties it so the native objects are released on the owning thread.
        shared = []
        self.shared_natives.append(shared)
        decoration_keyed, decoration_key = 'key' in decoration, decoration.get('key')
        decoration_name = decoration.get('name')
        layout_keys = ('left', 'top', 'width', 'height', 'scroll_offset', 'content_offset')

        @wraps(func)
        def wrapper(input_value=None, **kwargs):
            stack = self.stack
            if graphics and not stack:
                if not imgui.is_frame_active():
                    with self.capture_frame():
                        return wrapper(input_value, **kwargs)
            if self.closed:
                raise RuntimeError('retained runtime is closed')
            identities = self.identities
            # An executing owner already has an active identity scope.
            if not stack and self.frame_roots is None and not identities.active:
                identities.begin_frame()
            if 'key' in kwargs:
                keyed, key = True, kwargs['key']
            else:
                keyed, key = decoration_keyed, decoration_key
            parent = stack[-1] if stack else 0
            geometry, graph = self.geometry, self.graph
            # One native crossing resolves the identity and, for a call whose
            # keyword arguments, flow position and state geometry match the
            # remembered resolution, declares the view again. A child under an
            # executing parent is finished there, executed first when dirty
            # (a tuple).
            declared = self.declared
            outcome = declared.declare(parent, renderer_id, keyed, key, kwargs.get('name', decoration_name),
                                       generation[0], geometry.cell_scope is None,
                                       not geometry.bindings and place is None,
                                       not geometry.frozen, self.auto_layout, input_value, kwargs)
            kind = type(outcome)
            if kind is tuple:
                return outcome
            portal = False
            if kind is list:
                # Declared from the remembered resolution; continue below with
                # change detection, execution and placement.
                identity_handle, node, flags, resolved = outcome
                binding = bindings[identity_handle]
                state = binding.state
                arguments, x, y, w, h, auto_height, scrollable, extras, caller_keys = resolved
            else:
                identity_handle = outcome
                binding = bindings.get(identity_handle)
                if binding is None:
                    if not shared:
                        native = _new_native(graphics=graphics, identities=identities)
                        shared.extend((native, _bind_native(native, func, inject=inject, events=events,
                                                            state_id=state_id, no_save=no_save, **decoration)))
                    native, renderer = shared
                    unique = identities.unique(identity_handle)
                    binding = bindings[identity_handle] = _Binding(native, renderer, func, native.state(unique),
                                                                   unique, identity_handle)
                state = binding.state
                if 'window_pos' in kwargs or ('initial' in kwargs and 'window_pos' in (kwargs['initial'] or {})):
                    raise TypeError('@gui uses relative left and top, not window_pos')
                raw = kwargs.copy()
                kwargs.pop('key', None)
                portal = kwargs.pop('melty_window', False)
                scrollable = kwargs.pop('scroll', scroll)
                caller = dict(kwargs)
                if kwargs.pop('glfw_window', False):
                    raise NotImplementedError('This experiment implements internal windows; native child surfaces are not connected yet')
                initial = kwargs.pop('initial', decoration.get('initial', {}))
                extras = kwargs.pop('return_extras', False)
                w, h = kwargs.pop('width', width), kwargs.pop('height', height)
                minimum = kwargs.pop('min_height', min_height)
                maximum = kwargs.pop('max_height', max_height)
                if (not math.isfinite(minimum) or not math.isfinite(maximum)
                        or not 0 <= minimum <= maximum <= 8192):
                    raise ValueError('height bounds must satisfy 0 <= min_height <= max_height <= 8192')
                minimum, maximum = math.ceil(minimum), math.ceil(maximum)
                allocation = geometry.allocation(parent) if not portal else None
                reads_cursor = bool(graphics and (parent or self.auto_layout) and not portal)
                if reads_cursor:
                    x, y = imgui.get_cursor_screen_pos()
                else:
                    x, y = (0, 0)
                flow_x, flow_y = x, y
                if allocation is not None:
                    ax, ay, w, h = allocation
                    px, py = self._position(parent)
                    x, y = ax-px, ay-py
                automatic_width = None
                if w is None:
                    if parent:
                        w = graph.constraints(parent)[0] - x
                    elif graphics:
                        w = imgui.get_content_region_available()[0]
                    else:
                        raise ValueError('width=None requires a parent or graphics host')
                    automatic_width = w
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
                x, y = float(x), float(y)
                w, h = float(w), min(maximum, max(minimum, float(h)))
                arguments = {**decoration, **kwargs, 'width': w, 'height': None if auto_height else h,
                             'min_height': minimum, 'max_height': maximum}
                caller_keys = tuple(caller)
                # The cursor only matters to an axis that neither the caller nor
                # a position default fixes, and to an automatic width.
                if not reads_cursor:
                    flow_x = flow_y = None
                else:
                    if (left is not None or 'left' in caller) and automatic_width is None: flow_x = None
                    if top is not None or 'top' in caller: flow_y = None
                # Layout cells and portals re-resolve every declaration: their
                # rectangle comes from the constraint solver, not these inputs.
                if portal or allocation is not None:
                    declared.forget(identity_handle)
                else:
                    # Also records the state geometry this call resolved to.
                    declared.remember(identity_handle, raw, generation[0], flow_x, flow_y, automatic_width,
                                      (arguments, x, y, w, h, auto_height, scrollable, extras, caller_keys), state)
                node, flags = graph.declare_view(parent, 0, identity_handle, input_value, arguments,
                                                 (x, y, w, -1. if auto_height else h), portal, state, scrollable)
            pending, dirty = flags & 1, flags & 2
            invocations = self.invocations
            previous = invocations.get(node)
            if previous is None:
                invocations[node] = input_value, arguments
                self.caller_keys[node] = caller_keys
            else:
                old_input, old_args = previous
                new_input = old_input is not input_value
                # Identity/scalar checks only; an unchanged memoized declaration
                # passes the very same arguments dictionary.
                if ((new_input and not pending and not _same(old_input, input_value)
                     and not _same(graph.result(node, False)[1], input_value))
                        or (old_args is not arguments and (old_args.keys() != arguments.keys()
                            or any(not _same(old_args[k], v) for k, v in arguments.items()
                                   if k not in layout_keys)))):
                    graph.invalidate_node(node)
                    dirty = True
                if new_input and not pending:
                    graph.dissociate(node, _alias(old_input))
                if new_input or old_args is not arguments:
                    invocations[node] = input_value, arguments
                    self.caller_keys[node] = caller_keys
                    self.executor.set_caller_keys(node, caller_keys)
            if node not in self.records:
                self.uniques[node] = binding.unique
                self.declarations[node] = binding
                self.records[node] = binding.native, binding.renderer, func
                self.states[node] = state
                self.executor.attach(node, binding.call, func, binding.unique, state, caller_keys)
                if listen is not None:
                    self.listeners[node] = frozenset(listen)
                if cursor is not None:
                    self.cursors[node] = cursor
                self.associate(func, node)
                self.associate(wrapper, node)
                graph.associate(node, f'renderer:{renderer_id}', func)
            # Identity aliases remain registered until replacement or retirement.
            # Path-like values can supply changing text, so resolve those again.
            if previous is None or previous[0] is not input_value or isinstance(input_value, (str, Path)):
                self.associate(input_value, node)
            visible = flags & 8
            if portal or geometry.cell_scope is not None or geometry.bindings:
                geometry.bind(node, parent, portal=portal)
                # Rebinding a layout cell can invalidate this view.
                dirty = graph.dirty(node)
                if flags & 4:
                    visible = graph.visible(node)
            if parent == 0:
                self.root_ids.add(node)
                if self.frame_roots is not None:
                    self.frame_roots.add(node)
            # An independently produced return is consumed before reexecuting.
            # In particular, an old scalar caller argument cannot overwrite it.
            # Cull whole scroll tiles (flag 4: the parent scrolls). A non-scrolling
            # parent's packet needs its children's textures even outside the
            # ancestor viewport. Unmeasured auto-height tiles need one capture
            # before culling.
            if (dirty and not pending and node not in geometry.frozen
                    and (not flags & 4 or (auto_height and node not in self.backgrounds) or visible)):
                self._execute(node, independent=False)
            # Root layout settles before the immediate caller places later items.
            if not parent:
                self.flush()
            changed, value, w, h = graph.conclude(node)
            if self.on_declare is not None:
                self.on_declare(node, parent)
            if place is not None:
                # `place`: this definition's host draws the image and owns the
                # layout advance (a legacy view in window coordinates).
                place(node)
                if extras:
                    return changed, value, self.states[node]
                return changed, value
            if graphics:
                if (parent or self.inline_roots) and not portal:
                    imgui.set_cursor_screen_pos((x, y))
                    if not flags & 4:
                        self._draw_image(imgui.get_window_draw_list(),node,x,y,w,h)
                    imgui.dummy(w, h)
                elif self.auto_layout and not parent and not portal:
                    imgui.set_cursor_screen_pos((x, y))
                    imgui.dummy(w, h)
            if changed and parent:
                # The caller may have drawn a label before assigning this return.
                # One signalled refresh displays its post-edit state consistently.
                graph.invalidate_node(parent)
            if extras:
                return changed, value, self.states[node]
            return changed, value

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
        if node not in self.captures:
            self.captures[node] = self.imgui.DrawCapture(f'gui-capture-{id(self)}-{node}')
        return self.captures[node]

    def _execute(self, node, *, independent):
        """Execute one dirty view. The native executor runs the capture scope,
        body, measurement, packet copy and commit; the hooks below cover what
        that straight path does not."""
        self.executor.run(node, independent)

    def _refresh(self, node):
        # A source edit: recompile the parameter plan before the body runs.
        self.declarations[node].renderer.__gui_refresh__()

    def _layout_begin(self, node):
        return self.geometry.begin(node)

    def _ensure_gpu(self):
        if self.gpu is None:
            from meltygui.core.rendering._gui_native import TextureCache
            self.gpu = TextureCache() if self.texture_bytes is None else TextureCache(self.texture_bytes)
            self.declared.set_gpu(self.gpu)
        return self.gpu

    def melty_colors(self):
        """Give the Rust raster the host's colour pipeline (hdr_color.py):
        packed vertex colours, linear half-float targets and, per frame, the
        host's glyph treatment (_glyphs). Call before the first capture."""
        import meltygui_imgui as imgui
        from meltygui import hdr_color
        hdr_range, octaves = hdr_color._curve_params()
        matrix = hdr_color.P3_TO_SRGB
        self._ensure_gpu().melty_colors(
            float(hdr_range), float(octaves), [float(matrix[row][column]) for column in range(3) for row in range(3)],
            1., int(imgui.get_io().fonts.texture_id))
        self.melty = True

    def _glyphs(self):
        # The atlas is shared and replaced by any surface's font load; the
        # toggles and the window's output are live. A change repaints every raster.
        import meltygui_imgui as imgui
        import meltygui.core.windowing.window_api as window_api
        fonts = imgui.get_io().fonts
        gamma = float(self._toggles.Fonts.text_gamma)
        tap = window_api.text_subpixel() / max(1, fonts.texture_width)
        if self.gpu.glyphs(int(fonts.texture_id), gamma if gamma > 0 else 1., tap):
            for node in self.backgrounds:
                self.graph.request_composition(node)

    def _keepalive_list(self):
        # One list for the binding's lifetime; new frames clear it in place.
        return self.imgui.get_current_context()._keepalive_cache

    def _state_replaced(self, node, state):
        binding = self.declarations[node]
        binding.state = self.states[node] = state
        # The remembered call was resolved against the previous state object.
        self.declared.forget(binding.handle)
        self.associate(state, node)

    def _first_capture(self, node, state):
        # A declaration has state before its first execution, including culled views.
        self.associate(state, node)

    def _background(self, state, arguments):
        tint = state.get('tint', arguments.get('tint', _CLEAR))
        background = tuple(tint) + ((1.,) if len(tint) == 3 else ())
        if len(background) != 4 or any(not math.isfinite(c) or not 0 <= c <= 1 for c in background):
            raise ValueError('prototype tint must be RGB or RGBA components in 0..1')
        return background

    def _commit_regions(self, node):
        # Hit regions staged by the body replace the previous execution's.
        staged = self.staged_regions.pop(node, None)
        if staged is not None:
            self.regions[node] = staged
        else:
            self.regions.pop(node, None)

    def _layout_end(self, node, resized, old_width, old_height, width, height):
        geometry = self.geometry
        if geometry.frames:
            geometry.captured(node)
        if resized:
            self.layout_log.append((self.declarations[node].func.__name__, node,
                                    (old_width, old_height), (width, height)))
            if self.request_frame:
                self.request_frame()
        if node in geometry.transactions:
            geometry.commit(node)

    def _propagate(self, node, input_value):
        # A returned edit: other views of the same input repaint.
        self.geometry.frozen.clear()
        self.graph.invalidate(_alias(input_value), exclude=node)

    def _abort_execution(self, node, events):
        self.geometry.abort(node)
        self.graph.abort(node)
        self.staged_regions.pop(node, None)
        self.events.setdefault(node, {}).update(events)

    def _release_references(self, capture, start):
        from meltygui.core.rendering.gui_draw_capture import release_captured_references
        release_captured_references(capture, start)

    def _track_capture(self, node, parent=None, owner=None, size=None):
        """Account one capture's residency. Execution supplies what it already
        knows; a representation change looks the capture up again."""
        if size is None:
            parent, owner = self.graph.capture_owner(node)
            owner = owner or None
            size = self.gpu.capture_bytes(node)
        # Empty command captures still own draw buffers and count toward residency.
        size = max(1, size)
        old_capture = self.capture_info.get(node)
        self.capture_children[parent].add(node)
        self._scroll_tile_revision += 1
        difference = size - (old_capture[1] if old_capture else 0)
        self.capture_info[node] = parent, size, owner
        if owner is not None:
            self.scroll_tile_bytes += difference
            self.scroll_tiles[owner] = self.scroll_tiles.get(owner, 0) + difference

    def _select_rasters(self):
        enabled = self._toggles.Rust.auto_cache
        if enabled != self._auto_cache:
            self._auto_cache = enabled
            self.executor.set_auto_cache(enabled)
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
        graph, gpu, backgrounds = self.graph, self.gpu, self.backgrounds
        choose, costs, observed = self.raster_policy.choose, self.costs, self.calibration.views
        for observation in gpu.take_workloads(64):
            node = observation[0]
            if node not in backgrounds:
                continue
            # Roots and portals stay rasters. One crossing answers whether a
            # rasterized ancestor covers this view, its pixels and current form.
            context = graph.raster_context(gpu, node)
            if context is None:
                continue
            covered, pixels, raster = context
            selected = choose(observation, costs, observed.get(node), pixels=pixels,
                              raster=raster, covered=covered)
            if selected != raster:
                try:
                    changed = gpu.raster(node, selected)
                except RuntimeError as error:
                    if selected and str(error) == 'prototype texture budget exceeded':
                        continue
                    raise
                if changed:
                    self.raster_policy.applied(node, selected)
                    graph.request_composition(node)
                    self._track_capture(node)
                    if selected:
                        # Only a representation transition walks this subtree.
                        # A cached ancestor makes descendant raster storage
                        # redundant until that ancestor is demoted again.
                        pending = list(self.capture_children.get(node, ()))
                        while pending:
                            child = pending.pop()
                            if graph.is_portal(child):
                                continue
                            pending.extend(self.capture_children.get(child, ()))
                            if gpu.raster(child, False):
                                self.raster_policy.applied(child, False)
                                graph.request_composition(child)
                                self._track_capture(child)

    def _compose(self):
        if self.graphics and self.gpu:
            if self.melty:
                self._glyphs()
            self._select_rasters()
            # The complete bottom-up replay, including one save/restore of host
            # GL state for the batch, runs natively. Views still executing are
            # skipped: their packets are incomplete.
            touched = self.graph.compose(self.gpu, self.backgrounds, self.stack)
            tiles = self.scroll_tiles
            if tiles:
                for child in touched:
                    if child in tiles:
                        tiles.move_to_end(child)

    def _drop_capture(self, node):
        self.executor.forget_capture(node)
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
        if self.drain is not None:
            self.drain()
        while self.woken:
            node = self.woken.popleft()
            if node in self.records:
                self.geometry.frozen.discard(node)
                self.graph.invalidate_node(node)
        for definition in self.renderers:
            func, old_code, refresh_positions = definition
            if func.__code__ is not old_code:
                refresh_positions()
                self.invalidate(func)
                definition[1] = func.__code__
        if self.graphics:
            # Representation-only replay does not submit widgets and must not
            # invent an extra host frame that would expire the active widget ID.
            if not self.imgui.is_frame_active() and self.graph.dirty_nodes():
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
                    try:
                        self._execute(node, independent=True)
                    except Exception as error:
                        if self.on_failure is None:
                            raise
                        self.on_failure(node, error)
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
            binding = self.declarations.pop(node, None)
            if binding is not None:
                # A retired view no longer pins its last caller arguments.
                self.declared.forget(binding.handle)
            self._drop_capture(node)
            self.executor.forget(node)
            for mapping in (self.invocations, self.events, self.regions, self.states, self.uniques, self.caller_keys,
                            self.backgrounds, self.texture_refs, self.listeners, self.cursors):
                mapping.pop(node, None)
            self.root_ids.discard(node)

    def region(self, key, rect):
        """Retain a local hit region; input is injected as view_events on wake."""
        self.staged_regions.setdefault(self.current, []).append((key, tuple(rect)))

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

    def title_height(self, node):
        """A floating window's title strip: its content offset. A popover
        declared with `content_offset=(0, 0)` has no title, shadow or drag."""
        return self.graph.state(node).content_offset[1]

    def window_rect(self, node):
        x,y=self._position(node)
        _,_,w,h=self.graph.rect(node)
        return x,y-self.title_height(node),w,h

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
        # An execution since the last sample may have changed the hit geometry.
        sample = (tuple(position), self.origin, pressed, down, released, self.executor.runs)
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
        claims=(claims[0] or self.imgui_input.left or self.hover is not None or self.drag is not None,claims[1])
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
        """Keep from the host's OS window gestures what is not theirs here. Over a Melty window
        both buttons are the layout's (it moves and resizes that window). Elsewhere a left drag
        is kept when a view takes it (a hit region, a view listening for the pointer, an ImGui
        item) and a right drag only when an ImGui item was activated by it: over everything
        else the right drag resizes the OS window."""
        x, y = position[0] - self.origin[0], position[1] - self.origin[1]
        window = self.drag is not None or any(wx <= x < wx + w and wy <= y < wy + h + self.title_height(node)
                                              for node in self.graph.windows()
                                              for wx, wy, w, h in (self.window_rect(node),))
        left, right = self.imgui_input.pointer_claims.get(self.imgui_input.mouse_owner, (False, False))
        subscriptions = []
        if window or left or self.imgui_input.left or self.hover is not None or self.geometry.claim[0]:
            subscriptions.extend(['left_mouse_dragged', 'left_mouse_double_dragged',
                                  'left_mouse_double_clicked'])
        if window or right or self.geometry.claim[1]:
            subscriptions.extend(['right_mouse_dragged', 'right_mouse_double_dragged'])
        if subscriptions:
            handler.register_hovered(
                ('gui-pointer', id(self)), subscriptions)

    def present(self, origin):
        import meltygui_imgui as imgui
        self.origin = origin
        dl = imgui.get_window_draw_list()
        for node in sorted(self.root_ids):
            if not self.graph.is_portal(node) and not self.inline_roots:
                self._image(dl, node, origin)
        self.geometry.paint(dl,origin)
        for node in self.graph.windows():
            x, y, w, h = self.window_rect(node)
            x += origin[0]
            y += origin[1]
            title=self.title_height(node)
            if title:
                left,top,right,bottom=(math.floor(v+.5) for v in (x,y,x+w,y+title+h))
                dl.add_rect_filled(left+5,top+5,right+5,bottom+5,0x66000000,5)
                dl.add_rect_filled(left,top,right,top+title,0xFF65513B,5)
                dl.add_text(left+9,top+5,0xFFFFFFFF,'Owned @gui window (drag title)')
            self._draw_image(dl,node,x,y+title,w,h)
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
            # Released here, on the owning thread, rather than by a later collection.
            self.declared.clear()
            self.declared = None
            self.executor.clear()
            self.executor = None
            if self.gpu:
                self.gpu.close()
                self.gpu = None
            self.imgui_input.characters.clear()
            self.imgui_input.text = ()
            self.imgui_input.cache = None
            self.geometry.axes = ()
            self.geometry.transactions.clear()
            # The shared snapshot owns solver forks; release them on this thread.
            self.geometry._snapshot_value = self.geometry._snapshot_stamp = None
            self.geometry.native = None
            self.geometry.cache = None
            self.renderers.clear()
            for binding in self.bindings.values(): binding.native.clear()
            self.bindings.clear()
            self.declarations.clear()
            for shared in self.shared_natives: shared.clear()
            self.shared_natives.clear()
            self.identities.clear()
            self.graph = None
            self.closed = True
