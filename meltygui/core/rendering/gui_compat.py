"""Path-finding bridge: legacy @render_func apps on the Rust @gui retained host.

Not an interface. It answers one question: what does the retained runtime need
before an unmodified legacy app (the code editor) runs on it?

    MELTY_RUST=1            every cached legacy view (use_cache=True) in an OS
                            window is served by a retained Rust node
    MELTY_RUST_VIEWS=a,b    only these view functions become nodes (default all)
    MELTY_RUST_SKIP=a,b     these never do
    MELTY_RUST_WINDOWS=1    closable Melty windows (popovers, menus) become
                            nodes too. Known broken: dropdown rows lose their
                            width and labels. Default: they stay legacy.
    MELTY_RUST_TRACE=1      print why each node executed
    MELTY_RUST_STATS=path   append one JSON line of counters per second
    MELTY_FRAME_LOG=path    per-second frame CPU times, with or without MELTY_RUST
    MELTY_RUST_TREE=path    the view catalog: once a second, every view function
                            seen so far with its callers, how it was served
                            (legacy, fast host, node run, node hit, island run,
                            island hit, native) and its time

How it works, and where each piece lives:

- `route` is called at the top of every legacy wrapper call (core_render). A
  cached view becomes `Bridge.call`: a retained node whose body is that same
  legacy wrapper (`Bridge.execute`), so the wrapper stays as the view's chrome.
- Rust owns identity, dirty state, the draw packet and its composition. The
  legacy blit never serves a routed view, but the legacy cache still keeps its
  bookkeeping for it (context, live overlays, input owners, depth mask) and
  its invalidation calls are forwarded here (`tile_cache`).
- A node executes when a rule of the legacy prologue says so (`Bridge.call`),
  and only at its call site: a legacy body needs its caller's stacks.
- A clean node gets what a legacy blit hit gets (`Bridge.replay`).
- Legacy code draws in window coordinates, so a node's rectangle is the whole
  surface, positioned by how far its caller moved since the capture.

Native `@gui` views are hosted in the same graph: the bridge is the `_window`
host `gui_prototype.gui` dispatches to (`Bridge.call`, `Bridge.native`). A
native view may be declared from any legacy body, retained or immediate
(`use_cache=False`), and may nest other native views. Its identity is scoped
by the legacy view that calls it. Rust schedules native nodes on its own from
pointer and keyboard input (`BridgeInput`); legacy-hosting nodes are
caller-bound (`RetainedGraph.bind`) and wake through `Bridge.wake`.

The other direction, a legacy view called from a native body, is an island
(`Bridge.island`): a legacy-hosting node under the native node, captured in
window coordinates, whose body runs in the legacy context the native view was
declared in (`Ambient`) instead of a live legacy caller's. So it can run alone:
`Bridge.service` applies the legacy rules to it once a frame while its native
parent is clean, and its edits reach the native caller on that caller's next
run. Windows called from a native body (popovers) stay legacy, as everywhere.
"""
from collections import Counter
from contextlib import contextmanager
from functools import wraps
import inspect
import json
import os
import time

from meltygui.core.rendering.gui_imgui_input import ImGuiInput

PASS = object()     # route(): the legacy wrapper handles this call itself
# Lever: frames an undeclared node (and its packet) outlives its last call.
KEEP_FRAMES = 1200
# Levers: a top-level legacy node holds a texture the size of the surface, so
# only this many stay once nobody calls them, inside this texture budget.
IDLE_ROOTS = 6
TEXTURE_BYTES = 1 << 30
_PRIMITIVES = (type(None), int, float, str, bool, tuple, set)

_mode = os.environ.get('MELTY_RUST', '')
_only = frozenset(filter(None, os.environ.get('MELTY_RUST_VIEWS', '').split(',')))
_skip = frozenset(filter(None, os.environ.get('MELTY_RUST_SKIP', '').split(',')))
_trace = bool(os.environ.get('MELTY_RUST_TRACE'))
_windows = bool(os.environ.get('MELTY_RUST_WINDOWS'))
_stats_path = os.environ.get('MELTY_RUST_STATS')
_tree_path = os.environ.get('MELTY_RUST_TREE')


def enabled():
    return _mode not in ('', '0')


class Entry:
    """One routed legacy view: its node, and what the legacy prologue would
    have compared or recomputed for it on a cache hit."""
    __slots__ = ('unique', 'node', 'draw_state', 'parent', 'pending', 'reason', 'call', 'size',
                 'extent', 'advance', 'hovered', 'frame', 'runs', 'name', 'input', 'result',
                 'tile', 'origin', 'ambient', 'executed', 'shift', 'run_frame', 'anchor',
                 'natives', 'site', 'host', 'local', 'returned', 'settled', 'epoch', 'live')

    def __init__(self, unique, name):
        self.unique, self.name = unique, name
        self.node = self.draw_state = self.parent = self.call = self.size = None
        self.pending, self.reason = True, 'first'
        self.extent = (0., 0.)    # item size of the last run
        self.advance = 0.         # and how far it moved the cursor down
        self.hovered = False
        self.input = self.result = self.tile = None
        self.origin = (0., 0.)    # the caller's cursor when the packet was captured
        self.ambient = None       # layout inputs the legacy prologue reads from its surroundings
        self.executed = False     # whether the current declaration ran the body
        self.shift = (0., 0.)     # how far the caller moved since the capture
        self.anchor = None        # cursor minus the legacy draw state's position, at the capture
        self.frame = -1           # last declaration
        self.run_frame = -1       # last execution
        self.runs = 0
        self.natives = None       # Native records this view's body declared
        # An island: this legacy view is called from a native body.
        self.site = None          # Native record of the legacy call site above it
        self.host = None          # the native node whose body calls it
        self.local = (0., 0.)     # the cursor in that node's coordinates
        self.returned = None      # an edit it made on its own, for the native caller
        self.settled = -1         # frame whose run or hit epilogue is done
        self.epoch = -1           # the native body run that last called it
        self.live = False


class Ambient:
    """The legacy context at one call site: Melty's stacks and the scalars
    the legacy wrapper reads from its surroundings, plus the style tint.
    `enter` puts copies in place, `leave` puts back what was there."""
    __slots__ = ('values', 'tint')
    # Lever: per-call legacy state outside Melty's *_stack lists.
    scalars = ('depth', 'z_pos', 'shadow_depth', 'bg_depth', 'active_layer', 'paint_rank',
               'indent_count', 'unindent_count', 'flow_spacing')
    names = None

    @classmethod
    def capture(cls):
        from meltygui.core.melty import Melty
        if cls.names is None:
            cls.names = tuple(name for name, value in vars(Melty).items()
                              if name.endswith('_stack') and isinstance(value, list)) + cls.scalars
        ambient = cls()
        ambient.values = [list(value) if type(value) is list else value
                          for value in (getattr(Melty, name) for name in cls.names)]
        ambient.tint = Melty.style_manager.get_tint()
        return ambient

    def enter(self):
        from meltygui.core.melty import Melty
        manager = Melty.style_manager
        saved = [getattr(Melty, name) for name in self.names], manager.get_tint()
        for name, value in zip(self.names, self.values):
            setattr(Melty, name, list(value) if type(value) is list else value)
        if saved[1] != self.tint:
            manager.set_imgui_tint(*self.tint[:4])
        return saved

    def leave(self, saved):
        from meltygui.core.melty import Melty
        values, tint = saved
        for name, value in zip(self.names, values):
            setattr(Melty, name, value)
        if Melty.style_manager.get_tint() != tint:
            Melty.style_manager.set_imgui_tint(*tint[:4])


class Native:
    """One native @gui view declared from legacy code: what the legacy side
    knew at its call site, for input routing and independent executions."""
    __slots__ = ('node', 'owner', 'host', 'clip', 'tint', 'frame', 'ambient', 'islands')

    def __init__(self):
        self.node = None      # retained node, known after the first declaration
        self.owner = None     # Entry whose packet contains this view, or None at the root
        self.host = None      # enclosing legacy draw state
        self.clip = None      # legacy clip rectangle at the call site
        self.tint = None      # legacy style tint at the call site
        self.frame = -1
        self.ambient = None   # the legacy context at the call site, for legacy views inside
        self.islands = {}     # legacy unique -> Entry called from this view or its native children


class BridgeInput(ImGuiInput):
    """Pointer and keyboard routing for native nodes only. Legacy views keep
    the legacy event system; their nodes never take retained input."""
    bridge = None

    def hit(self, position):
        bridge, cache = self.bridge, self.cache
        x, y = position[0] - cache.origin[0], position[1] - cache.origin[1]
        _, hits = cache.graph.hits(x, y)
        for node, _, _ in hits:
            entry = bridge.nodes.get(node)
            if entry is not None:
                if entry.site is not None and bridge.covers(entry, x, y):
                    return None     # a legacy view in a native body: the legacy event system owns it
                continue
            record = bridge.record(node)
            if record is not None and bridge.reachable(record, x, y):
                return node
        return None

    def finish(self, node, capture):
        if node not in self.bridge.nodes:
            super().finish(node, capture)


class Bridge:
    """Per OS surface. `current` is the one whose frame is being drawn."""
    current = None

    def __init__(self, surface):
        from meltygui.core.rendering.retained_gui_prototype import RetainedGui
        self.surface = surface
        self.cache = RetainedGui(calibration=False, input=self.input, texture_bytes=TEXTURE_BYTES)
        self.cache.auto_layout = True
        self.cache.inline_roots = True
        self.cache.on_declare = self.declared
        self.cache.on_failure = self.failed
        self.cache.request_frame = surface.request_frame
        self.cache.melty_colors()
        self.views = {}       # legacy wrapper -> retained view
        self.entries = {}     # legacy unique -> Entry
        self.nodes = {}       # retained node -> Entry (the legacy-hosting nodes)
        self.owners = {}      # legacy tile id -> Entry whose packet shows that view
        self.stack = []       # entries executing, outermost first
        self.calling = None   # entry whose node body runs next
        self.declaring = []   # entries being declared, outermost first
        # Native @gui views: the _window host protocol and their legacy call sites.
        self.bindings = {}    # @gui view -> declare(input_value, **kwargs)
        self.natives = {}     # retained node -> Native
        self.native_keys = {} # (view, key) -> Native
        self.occurrences = {} # (legacy unique, view) -> unnamed calls this frame
        self.declaring_native = None
        self.epoch = 0        # native body runs so far: one legacy identity per run
        self.popups = set()   # native nodes whose last run called a legacy window
        self.hosts = set()    # native nodes whose last run called a legacy view
        self.deferred = set() # island nodes held back until the next frame (`defer`)
        self.filled = set()   # native nodes whose current run gave its height to a legacy view
        self.late = False     # after Melty.end_frame: no legacy body may run any more
        self.reported = set() # failures already printed
        self.runtime = None   # immediate (use_cache=False) dispatch, created on first use
        self.token = None
        self.keyboard = surface.impl
        self.keyboard.gui_character_callback = self.cache.imgui_input.character
        self.size = (1., 1.)
        self.frame = None     # the retained host frame, open from begin() to finish()
        self.stats = Counter()
        self._stats_at = time.monotonic()

    def input(self, cache):
        routing = BridgeInput(cache)
        routing.bridge = self
        return routing

    # ---- frame ----------------------------------------------------------
    def begin(self):
        """Open this surface's frame. It stays open through Melty.end_frame:
        legacy windows defer their bodies to the layer pass that runs there."""
        import meltygui_imgui as imgui
        if self.frame is not None:
            self.finish()
        size = tuple(imgui.get_io().display_size)
        if size != self.size:
            self.size = size
            self.invalidate_all('surface resized')
        from meltygui.core.rendering.gui_prototype import _window
        cache = self.cache
        cache.origin = (0, 0)
        self.occurrences.clear()
        self.late = False
        if self.deferred:
            cache.geometry.frozen.difference_update(self.deferred)
            self.deferred.clear()
        if _tree_path:
            Catalog.frame()
        from meltygui.core.melty import Melty
        if self.popups and Melty.returned_values:
            # A legacy window answered last frame: its native callers read the answer.
            for node in list(self.popups):
                if node in cache.records:
                    cache.graph.invalidate_node(node)
                else:
                    self.popups.discard(node)
        # Native nodes under the pointer or holding focus become dirty; Rust
        # runs them on its own. Retained hit regions (cache.region) follow.
        routing = cache.imgui_input
        if cache.listeners or cache.focus.stack:
            # The frame's ordered key presses and repeats, for listening views and open popups (Escape).
            routing.keys.extend(Melty.frame_key_events)
        routing.process(imgui.get_io())
        if routing.hover is not None or cache.hover is not None or cache.drag:
            cache.pointer(imgui.get_mouse_pos(), pressed=imgui.is_mouse_clicked(0),
                          down=imgui.is_mouse_down(0), released=imgui.is_mouse_released(0))
        self.wake()
        if self.runtime is not None:
            self.runtime.begin_frame(scope=self.scope(), width=size[0], cache=cache)
        self.frame = cache.frame()
        self.frame.__enter__()
        self.token = _window.set(self)
        Bridge.current = self

    def finish(self):
        """After the layer pass: retire undeclared roots, compose, report."""
        frame, self.frame = self.frame, None
        if frame is None:
            if Bridge.current is self:
                Bridge.current = None
            return
        # A root node goes undeclared whenever a legacy blit tile served its
        # caller. Keep it and its packet until it has been idle for a while.
        from meltygui.core.melty import Melty
        import meltygui_imgui as imgui
        from meltygui.core.rendering.gui_prototype import _window
        cache = self.cache
        horizon = Melty.frame_count - KEEP_FRAMES
        roots = cache.frame_roots
        idle = sorted((entry for entry in self.entries.values()
                       if entry.parent is None and entry.site is None and entry.frame < Melty.frame_count),
                      key=lambda entry: entry.frame, reverse=True)
        dropped = set(idle[IDLE_ROOTS:])

        def expired(record):
            """Too old, or drawn by a root that was just dropped."""
            if record.frame < horizon:
                return True
            while record is not None:
                if record in dropped:
                    return True
                record = record.owner if isinstance(record, Native) else record.parent
            return False
        for unique, entry in list(self.entries.items()):
            if entry.site is not None:
                # An island lives and dies with its node under the native parent.
                if expired(entry) or (entry.node is not None and entry.node not in cache.records):
                    self.forget(entry)
            elif expired(entry):
                self.forget(entry)
            elif entry.parent is None and entry.node is not None and roots is not None:
                roots.add(entry.node)
        for key, record in list(self.native_keys.items()):
            if expired(record):
                del self.native_keys[key]
                self.natives.pop(record.node, None)
            elif record.owner is None and record.node is not None and roots is not None:
                roots.add(record.node)
        frame.__exit__(None, None, None)
        # Melty.end_frame is behind us and no legacy body may run any more:
        # a native body that calls legacy views waits for the next frame's
        # flush, inside the legacy frame. The others still declare native
        # views, so the host stays set until the flush is done.
        frozen = cache.geometry.frozen
        waiting = {node for node in self.hosts if node not in frozen}
        frozen |= waiting
        self.late = True
        try:
            cache.flush()
        finally:
            self.late = False
            frozen -= waiting
            if self.token is not None:
                _window.reset(self.token)
                self.token = None
            if self.runtime is not None:
                self.runtime.end_frame()
        if any(node in cache.records and cache.graph.dirty(node) for node in waiting):
            self.surface.request_frame()
        # A native edit returned to a legacy caller: that caller runs next frame.
        if self.wake():
            self.surface.request_frame()
        # A widget in a native node keeps its drag from the OS-window gestures.
        cache.register_host_pointer(Melty.event_handler, imgui.get_mouse_pos())
        cache.present((0, 0))
        # Only now: the flush above still routes legacy calls made by native bodies.
        if Bridge.current is self:
            Bridge.current = None
        self.report()

    def scope(self):
        from meltygui.core.runtime.app import _state
        return (_state['app_id'], 'legacy-bridge')

    def wake(self):
        """Legacy-hosting nodes the graph dirtied (a native child returned an
        edit or changed size): they run when their callers do."""
        woke = False
        for node in self.cache.graph.take_woken():
            entry = self.nodes.get(node)
            while entry is not None:
                if not entry.pending:
                    entry.pending, entry.reason, woke = True, 'native child', True
                entry = entry.parent
        return woke

    def report(self):
        if _stats_path and time.monotonic() - self._stats_at >= 1.0:
            self._stats_at = time.monotonic()
            with open(_stats_path, 'a') as stream:
                stream.write(json.dumps(dict(self.stats, nodes=len(self.entries))) + '\n')
            self.stats.clear()

    # ---- routing --------------------------------------------------------
    def legacy_view(self, wrapper, func, o_kwargs, input_value, kwargs, site=None):
        """Declare the node for one cached legacy view, executing its legacy
        wrapper only when a rule the legacy prologue applied says it is stale.
        `site`: (Native record, native node, its window origin) for a call made
        from a native body (`island`)."""
        import meltygui_imgui as imgui
        from meltygui.core.melty import Melty
        from meltygui.core.rendering.view_identity import view_unique, view_tile_id, get_draw_state
        name = legacy_name(func, input_value, o_kwargs, kwargs)
        explicit = kwargs.get('draw_state')
        if 'layer_unique' in kwargs:
            unique = kwargs['layer_unique']     # a window's layer-pass re-entry
        elif explicit is not None:
            # The caller owns the state (tile manager instances): that is the identity.
            unique = explicit.unique
        else:
            unique, _ = view_unique(name, func.__name__, key=kwargs.get('key', ''),
                                    unique_name=kwargs.get('unique_name', name), old_suffix=kwargs.get('suffix'))
        entry = self.entries.get(unique)
        if entry is None:
            entry = self.entries[unique] = Entry(unique, func.__name__)
        graph = self.cache.graph
        cursor = tuple(imgui.get_cursor_screen_pos())
        if site is None:
            if entry.frame == Melty.frame_count:
                # The same identity twice in one frame: the legacy duplicate-unique path.
                self.stats['duplicate'] += 1
                return PASS
            entry.parent = self.stack[-1] if self.stack else None
        else:
            record, native, origin = site
            if entry.epoch == self.epoch and entry.frame == Melty.frame_count:
                raise RuntimeError(f'{func.__name__}: one legacy view identity twice in a @gui body; '
                                   f'give the calls a name or a key')
            # The native body draws in its own coordinates, a legacy view in the window's.
            entry.local, cursor = cursor, (origin[0] + cursor[0], origin[1] + cursor[1])
            entry.site, entry.host, entry.epoch, entry.parent = record, native, self.epoch, None
            record.islands[unique] = entry
            if ((o_kwargs.get('determines_height', True) is False or o_kwargs.get('fill_height') is not None)
                    and kwargs.get('parent_window') is None):
                # A view that cannot size itself (draw_text, layouts) fills
                # what is left of its host: the wrapper's Melty.root_fill
                # rule, with the native view as the host. All get the width,
                # the first one in a fixed-height view the remaining height.
                _, _, native_width, native_height = graph.rect(native)
                if kwargs.get('width') is None:
                    kwargs['width'] = max(1., native_width - entry.local[0])
                if (kwargs.get('height') is None and native not in self.filled
                        and graph.constraints(native)[1] >= 0):
                    kwargs['height'] = max(40., native_height - entry.local[1])
                    self.filled.add(native)
                kwargs.setdefault('auto_resize', False)
                kwargs.setdefault('show_header', False)
        entry.frame = Melty.frame_count
        size = (kwargs.get('width'), kwargs.get('height'))
        # The legacy prologue re-derives a cached view's size from its
        # surroundings on every call, hit or not. A node only sees what is
        # compared here, so the available width stands in for that.
        ambient = round(imgui.get_content_region_available()[0])
        if entry.tile is None:
            entry.tile = view_tile_id(name, unique, explicit if explicit is not None else get_draw_state(unique))
            self.owners[entry.tile] = entry
        live = bool(kwargs.get('live', o_kwargs.get('live')) or kwargs.get('draw'))
        if site is not None and entry.returned is not None:
            # This caller has not read the view's own edit yet: its argument is the older value.
            input_value = entry.input
        if not entry.pending and entry.node is not None and graph.dirty(entry.node):
            # The graph dirtied it since the last wake(): a native child's edit.
            entry.pending, entry.reason = True, 'native child'
        if not entry.pending:
            reason = self.stale(entry, cursor, size, ambient, input_value, live)
            if reason is None and site is not None and not kwargs.get('use_cache', o_kwargs.get('use_cache', False)):
                reason = 'uncached'     # drawn with its caller, every time the caller runs
            if reason:
                entry.pending, entry.reason = True, reason
        entry.size = size
        entry.ambient = ambient
        entry.input = input_value
        entry.live = live
        entry.call = (wrapper, kwargs, cursor)
        if entry.pending and entry.node is not None:
            if self.late or (site is not None and entry.run_frame == Melty.frame_count):
                self.defer(entry)
            else:
                # Dirtied here, at the call site, so the body runs inside its
                # caller's legacy context (unique, window, clip and style stacks).
                graph.invalidate_bound(entry.node)
        if site is not None:
            # The node starts at the window's corner: its capture is in window coordinates.
            entry.shift = (-origin[0], -origin[1])
        else:
            # A capture is in window coordinates. Until the next one, the node
            # sits where its caller moved to, and native nodes inside follow it.
            entry.shift = ((0., 0.) if entry.pending or entry.node is None
                           else (cursor[0] - entry.origin[0], cursor[1] - entry.origin[1]))
        self.calling = entry
        self.declaring.append(entry)
        view = self.views.get(wrapper)
        if view is None:
            view = self.views[wrapper] = self.view(func)
        width, height = self.size
        entry.executed = False
        entry.result = None
        try:
            # The input stays out of the node: Rust would associate it, compare
            # it and repaint its other users on its own schedule, outside the
            # legacy call context those bodies need.
            view(None, key=unique, left=entry.shift[0], top=entry.shift[1], width=width, height=height)
        finally:
            self.calling = None
            self.declaring.pop()
        result = entry.result
        if site is not None:
            if entry.executed:
                entry.settled = Melty.frame_count
            if result and result[0]:
                self.edited(entry, result[1])
            elif entry.returned is not None:
                result = entry.returned     # the edit it made while this caller was clean
            entry.returned = None
            if result and result[0]:
                # One more run of the caller shows its state after the edit.
                graph.invalidate_node(native)
        changed, value = result or (False, input_value)
        Melty.seen_unique.add(unique)
        if kwargs.get('return_extras'):
            return changed, value, entry.draw_state
        return changed, value

    def edited(self, entry, value):
        """An island returned an edit. Its next run starts from that value,
        whether or not its native caller has read it by then (a second key
        must not be applied to the text before the first), and shows it."""
        entry.input = value
        entry.pending, entry.reason = True, 'edited'

    def defer(self, entry):
        """This view's body waits for the next frame. A legacy body reads the
        frame's events, so it runs once a frame however often its native
        caller does, and never after Melty.end_frame. Its packet stands."""
        self.deferred.add(entry.node)
        self.cache.geometry.frozen.add(entry.node)
        self.surface.request_frame()

    def stale(self, entry, cursor, size, ambient, input_value, live):
        """Why this view's body has to run now, by the rules the legacy
        prologue applies to a cached view on every call. None: its packet stands."""
        state = entry.draw_state
        if size != entry.size:
            return 'size'
        if ambient != entry.ambient:
            return 'available width'
        if entry.runs < 2 or (state is not None and state.size_change):
            # The legacy cache serves a tile from its third frame, at a settled size.
            return 'settling'
        if (isinstance(input_value, _PRIMITIVES) or isinstance(entry.input, _PRIMITIVES)) \
                and (type(input_value) is not type(entry.input) or input_value != entry.input):
            # The legacy "untracked value changed" rule; objects are invalidated explicitly.
            return 'input'
        if live:
            return 'live'
        if state is None or state.width is None or state.height is None or state.frame_count < 2:
            return 'unmeasured'
        if state._external_change:
            return 'external change'
        if cursor != entry.origin and entry.anchor != anchor(cursor, state):
            # The caller moved and the legacy geometry did not follow (it
            # only follows an ancestor's scroll): hit tests, event
            # rectangles and overlays inside would be left behind.
            return 'moved'
        hovered = state.is_bounding_hovered()
        changed = hovered or hovered != entry.hovered
        entry.hovered = hovered
        return 'hover' if changed else None

    def view(self, func):
        bridge = self

        def body(cache=None):
            bridge.execute(cache)
        body.__name__ = func.__name__
        body.__qualname__ = 'legacy.' + func.__qualname__
        body.__module__ = func.__module__
        return self.cache.gui(body, place=self.place)

    def execute(self, cache):
        """The node body: the legacy wrapper, live, at the caller's cursor."""
        import meltygui_imgui as imgui
        from meltygui.core.melty import Melty
        entry, self.calling = self.calling, None
        if entry is None:
            # Only reachable before the node is bound (its first declaration
            # always runs here, with an entry).
            raise RuntimeError('legacy node executed outside its caller')
        wrapper, kwargs, cursor = entry.call
        input_value = entry.input
        if entry.node != cache.current:
            self.nodes.pop(entry.node, None)
            entry.node = cache.current
            self.nodes[entry.node] = entry
            # Its body needs this call site's legacy stacks: Rust never runs it alone.
            cache.graph.bind(entry.node)
        if entry.site is not None:
            if self.late:
                # First declared after Melty.end_frame: an empty packet now, its body next frame.
                entry.pending, entry.reason = True, 'late'
                self.surface.request_frame()
                return
            # A second run in one frame (alone, then with its native caller) is no duplicate.
            Melty.seen_unique.discard(entry.unique)
        entry.natives = None
        entry.origin = cursor
        entry.executed = True
        entry.pending = False
        entry.run_frame = Melty.frame_count
        entry.runs += 1
        self.stats['executed'] += 1
        self.stats['run: ' + entry.reason] += 1
        if _trace:
            print(f'[rust] run {entry.name} ({entry.reason}) depth={len(self.stack)}', flush=True)
        imgui.set_cursor_screen_pos(cursor)
        # The capture has its own draw list: the legacy z-layer channels start over.
        split, Melty.channels_split = Melty.channels_split, False
        self.stack.append(entry)
        legacy = Melty.cache
        imgui.begin_group()
        try:
            # draw=True is the legacy "render live through the tile path": the
            # legacy cache opens its own context for this view (parent keys,
            # live overlays, input owners, the depth-mask tile) and never
            # serves its blit. Rust serves the pixels.
            result = wrapper(input_value, **{**kwargs, '_rust_inner': True, 'use_cache': True,
                                             'draw': True, 'return_extras': True})
        finally:
            imgui.end_group()
            entry.extent = tuple(imgui.get_item_rect_size())
            entry.advance = imgui.get_cursor_screen_pos()[1] - cursor[1]
            self.stack.pop()
            if Melty.channels_split:
                imgui.get_window_draw_list().channels_merge()
            Melty.channels_split = split
        changed, value, state = result if len(result) == 3 else (*result, entry.draw_state)
        entry.draw_state = state
        entry.result = (changed, value)
        if state is not None:
            entry.hovered = state._bounding_hovered
            entry.anchor = anchor(cursor, state)
            if state._tile_id != entry.tile:
                # The wrapper resolved another identity than legacy_name() predicted.
                self.stats['identity mismatch'] += 1
                if _trace:
                    print(f'[rust] identity mismatch {entry.name}: {entry.tile!r} != {state._tile_id!r}', flush=True)
                legacy.key_to_draw_state.setdefault(entry.tile, state)
                entry.tile = state._tile_id
                self.owners[entry.tile] = entry

    def replay(self, entry, state):
        """What a legacy blit hit does besides drawing pixels: the skipped
        body's event subscriptions and child-window requests are issued again,
        live overlays of the views inside are painted, and the view's depth
        mask is marked (shadows, overlay masking)."""
        import meltygui_imgui as imgui
        from meltygui.core.melty import Melty
        from meltygui.core.cache.tile_cache import _Ctx
        legacy = Melty.cache
        # The wrapper's own share of a hit (core_render, after mark_start_offscreen).
        state.replay_body_actions()
        state.replay_surface_requests()
        if not legacy.enabled or state.width is None or state.height is None:
            return
        # The cache's share (mark_end_offscreen with drew_cached): descendants'
        # live overlays and input subscriptions, and this view's depth-mask mark.
        key = legacy._resolve_key(state._tile_id)
        layer = max(legacy._LAYER_MIN, min(legacy._LAYER_MAX, state.z_pos))
        Melty.tile_id_stack.append(state._tile_id)
        imgui.push_id(f'{key}{layer}_offscreen')
        imgui.begin_group()
        legacy._stack.append(_Ctx(draw_state=state, key=key, pos=tuple(imgui.get_cursor_screen_pos()),
                                  size=(state.width, state.height), layer=layer,
                                  depth_and_layer=state.shadow_depth, drew_cached=True,
                                  auto_resize=state.auto_resize))
        legacy.mark_end_offscreen()

    def attend(self, entry):
        """A legacy view whose body did not run: the native views it declared
        last time were not declared either, so the legacy views inside them
        get their frame here."""
        if not entry.natives:
            return
        for record in list(entry.natives):
            if record.owner is not entry or record.node is None or record.frame < entry.run_frame:
                entry.natives.discard(record)
            elif record.islands:
                self.service(record)

    def place(self, node):
        """After a declaration: the node's image at the origin, in the caller's
        channel and clip, then the layout advance the legacy wrapper made."""
        import meltygui_imgui as imgui
        entry = self.declaring[-1]
        width, height = self.size
        island = entry.site is not None
        # An island's caller draws in its own coordinates: the hit epilogue
        # paints in the window's, so `service` does it outside the capture.
        cursor = entry.local if island else entry.call[2]
        hit = not entry.executed and entry.draw_state is not None
        if hit and not island:
            # Before the layout advance: the legacy epilogue submits its own (empty) group.
            self.stats['hit'] += 1
            self.replay(entry, entry.draw_state)
        self.cache._draw_image(imgui.get_window_draw_list(), node, entry.shift[0], entry.shift[1], width, height)
        # The capture discarded the layout its body advanced: repeat it here.
        # The item gives the caller's own line start; only the vertical step
        # is taken from the capture, whose lines start at its left edge.
        imgui.set_cursor_screen_pos(cursor)
        imgui.dummy(*entry.extent)
        imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0], cursor[1] + entry.advance))
        if hit and not island:
            self.attend(entry)      # above this view's image

    # ---- legacy views in native bodies --------------------------------------
    def island(self, native, wrapper, func, o_kwargs, input_value, kwargs):
        """One legacy view called from a native body (`route`)."""
        import meltygui_imgui as imgui
        record = self.record(native) or self.declaring_native
        if record is None:
            raise RuntimeError(f'{func.__name__}: called from a @gui view the bridge did not declare')
        self.hosts.add(native)
        closable = bool(kwargs.get('closable', o_kwargs.get('closable')) or kwargs.get('as_window'))
        window = (closable or kwargs.get('melty_window', o_kwargs.get('melty_window'))
                  or kwargs.get('glfw_window') or kwargs.get('just_shadow'))
        with self.site(record, native) as origin:
            if not window:
                self.stats['island'] += 1
                if not _tree_path:
                    return self.legacy_view(wrapper, func, o_kwargs, input_value, kwargs, (record, native, origin))
                runs = self.stats['executed']
                try:
                    return self.legacy_view(wrapper, func, o_kwargs, input_value, kwargs, (record, native, origin))
                finally:
                    Catalog.seen(func, 'island run' if self.stats['executed'] != runs else 'island hit')
            # A window draws itself in the layer pass (or on its own surface):
            # only its request is made here, at the window position of the call.
            self.popups.add(native)
            if _tree_path:
                Catalog.seen(func, 'legacy window')
            local = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((origin[0] + local[0], origin[1] + local[1]))
            try:
                return wrapper(input_value, **{**kwargs, '_rust_inner': True})
            finally:
                imgui.set_cursor_screen_pos(local)

    @contextmanager
    def site(self, record, native):
        """The legacy context of a call made in a native body: the one its
        nearest legacy call site was declared in, one level down, with the
        native view as identity scope and clip. Legacy views there are
        top-level for the legacy cache, so `service` is who replays them.
        Yields the native view's window origin."""
        from meltygui.core.melty import Melty
        cache = self.cache
        if record.ambient is None:
            record.ambient = Ambient.capture()
        saved = record.ambient.enter()
        legacy = Melty.cache
        contexts, legacy._stack = legacy._stack, []
        # A legacy view with no legacy view around it takes its position from
        # the cursor only under a root fill (view_identity.place_in_parent_window).
        # The fill itself is spent: `legacy_view` sizes such views to the native view.
        fill = Melty.root_fill, Melty.root_fill_used
        Melty.root_fill, Melty.root_fill_used = (*cache.graph.rect(native)[2:], 0.), True
        Melty.unique_stack.append(cache.uniques[native])
        Melty.depth += 1
        clip, outer = cache.graph.clip_rect(native), Melty.get_clip_rect()
        if outer is not None:
            clip = (max(clip[0], outer[0]), max(clip[1], outer[1]), min(clip[2], outer[2]), min(clip[3], outer[3]))
        Melty.clip_stack.append(clip)
        try:
            yield cache._position(native)
        finally:
            Melty.root_fill, Melty.root_fill_used = fill
            legacy._stack = contexts
            record.ambient.leave(saved)

    def service(self, record):
        """Once a frame, where a native view's image was placed: the legacy
        views inside it (`record.islands`) that no body ran this frame get
        what a legacy call gives a cached view, a run when a rule says so,
        else the hit epilogue. Their native callers stay clean."""
        import meltygui_imgui as imgui
        from meltygui.core.melty import Melty
        cache, frame = self.cache, Melty.frame_count
        graph = cache.graph
        hosts = {}
        for entry in list(record.islands.values()):
            node = entry.node
            if entry.settled == frame or entry.site is not record:
                continue
            if node is None or node not in cache.records or self.nodes.get(node) is not entry:
                if entry.frame < frame:
                    self.forget(entry)      # its native caller no longer calls it
                continue
            hosts.setdefault(entry.host, []).append(entry)
        for native, entries in hosts.items():
            with self.site(record, native) as origin:
                for entry in entries:
                    node, state = entry.node, entry.draw_state
                    called, entry.frame, entry.settled = entry.frame == frame, frame, frame
                    cursor = (origin[0] + entry.local[0], origin[1] + entry.local[1])
                    if entry.pending:
                        reason = entry.reason
                    elif called:
                        reason = None       # its caller applied the rules a moment ago
                    elif graph.dirty(node):
                        reason = 'native child'
                    else:
                        reason = self.stale(entry, cursor, entry.size, entry.ambient, entry.input, entry.live)
                    if reason and entry.run_frame == frame:
                        # It ran with a native caller this frame already (`defer`).
                        entry.pending, entry.reason, reason = True, reason, None
                        self.surface.request_frame()
                    if reason:
                        self.run(entry, reason, cursor)
                    elif state is not None:
                        self.stats['hit'] += 1
                        held = imgui.get_cursor_screen_pos()
                        imgui.set_cursor_screen_pos(entry.origin)
                        try:
                            self.replay(entry, state)
                            self.attend(entry)
                        finally:
                            imgui.set_cursor_screen_pos(held)
                        Melty.seen_unique.add(entry.unique)
                    if _tree_path:
                        Catalog.seen(entry.call[0], 'island run' if reason else 'island hit')

    def run(self, entry, reason, cursor):
        """An island's body on its own, where its native caller left it."""
        cache, node = self.cache, entry.node
        graph = cache.graph
        wrapper, kwargs, _ = entry.call
        entry.call = (wrapper, kwargs, cursor)
        entry.pending, entry.reason = True, reason
        # Its capture is in window coordinates: the node starts at the window's corner.
        x, y = cache._position(node)
        if x or y:
            state = cache.states[node]
            state.left, state.top = state.left - x, state.top - y
        layout = entry.extent, entry.advance
        entry.executed, entry.result = False, None
        self.calling = entry
        graph.invalidate_bound(node)
        try:
            cache._execute(node, independent=True)
        finally:
            self.calling = None
        changed = bool(entry.result and entry.result[0])
        if changed:
            entry.returned = entry.result
            self.edited(entry, entry.result[1])
        if changed or (entry.extent, entry.advance) != layout:
            # The native caller reads the edit, or lays out around the new size.
            graph.invalidate_node(entry.host)
            self.surface.request_frame()

    def covers(self, entry, x, y):
        """Whether a window point is on the legacy view an island draws."""
        state = entry.draw_state
        if state is None or not state.width or not state.height:
            return False
        left, top = state.abs_left, state.abs_top
        return left <= x < left + state.width and top <= y < top + state.height

    def forget(self, entry):
        self.entries.pop(entry.unique, None)
        self.owners.pop(entry.tile, None)
        if self.nodes.get(entry.node) is entry:
            del self.nodes[entry.node]
        if entry.site is not None:
            entry.site.islands.pop(entry.unique, None)

    # ---- native @gui views ------------------------------------------------
    def call(self, view, input_value, kwargs):
        """The _window host protocol: bind a @gui view on first use."""
        declare = self.bindings[view] = self.bind(view)
        return declare(input_value, **kwargs)

    def bind(self, view):
        from meltygui.core.melty import Melty
        from meltygui.core.rendering.gui_prototype import _bind_native, _new_native
        func, options = view.__gui_definition__
        options = dict(options)
        retained = options.pop('use_cache', True)
        options.pop('live', None)
        bridge, cache, nodes, natives = self, self.cache, self.nodes, self.natives
        if retained:
            @wraps(func)
            def hosted(**arguments):
                # Rust runs this node whenever its input says so, not only
                # under its legacy caller: give it that caller's style tint.
                node = cache.current
                record = natives.get(node)
                # A new body run: legacy identities, window calls and the
                # fill height start over for this view (`island`).
                bridge.epoch += 1
                bridge.popups.discard(node)
                bridge.filled.discard(node)
                if node in bridge.hosts:
                    # Its last run called legacy views: whatever legacy code
                    # this body reaches (draw_any's dispatch) reads the legacy
                    # context of its call site, however Rust came to run it.
                    bridge.hosts.discard(node)
                    site = record or bridge.record(node)
                    if site is not None and site.ambient is not None:
                        saved = site.ambient.enter()
                        try:
                            return func(**arguments)
                        finally:
                            site.ambient.leave(saved)
                manager = Melty.style_manager
                if record is None or record.tint is None or manager.get_tint() == record.tint:
                    return func(**arguments)
                previous = manager.get_tint()
                manager.set_imgui_tint(*record.tint[:4])
                try:
                    return func(**arguments)
                finally:
                    manager.set_imgui_tint(*previous[:4])
            inner = cache.gui(hosted, **options)
        else:
            if self.runtime is None:
                self.runtime = _new_native(graphics=True, identities=cache.identities)
                self.runtime.begin_frame(scope=self.scope(), width=self.size[0], identity_frame=False, cache=cache)
            inner = _bind_native(self.runtime, func, **options)
        automatic = retained and options.get('width', 300) is None
        name = options.get('name')

        def declare(input_value=None, **kwargs):
            stack = cache.stack
            if stack and stack[-1] not in nodes:
                return inner(input_value, **kwargs)     # a native caller: plain @gui
            return bridge.native(view, inner, retained, automatic, name, input_value, kwargs)
        return declare

    def native(self, view, inner, retained, automatic, name, input_value, kwargs):
        """One @gui view declared from a legacy body."""
        import meltygui_imgui as imgui
        from meltygui.core.melty import Melty
        # Identity: the calling legacy view's, then key, name or occurrence
        # exactly as @gui orders them. Legacy bodies run conditionally, so a
        # plain occurrence among all root calls would shift.
        scope = Melty.unique_stack[-1] if Melty.unique_stack else 0
        if 'key' in kwargs:
            given = kwargs['key']
            key = ('legacy', scope, 'key', *given) if isinstance(given, tuple) else ('legacy', scope, 'key', given)
        elif kwargs.get('name', name):
            key = ('legacy', scope, 'name', kwargs.get('name', name))
        else:
            counter = (scope, view)
            index = self.occurrences[counter] = self.occurrences.get(counter, -1) + 1
            key = ('legacy', scope, 'index', index)
        kwargs['key'] = key
        if not retained:
            self.stats['native immediate'] += 1
            if _tree_path:
                Catalog.seen(view.__gui_definition__[0], 'native @gui (immediate)')
            return inner(input_value, **kwargs)
        host = Melty.draw_state_stack[-1] if Melty.draw_state_stack else None
        if automatic and kwargs.get('width') is None:
            # width=None: what is left of the legacy caller's content width.
            if host is not None and host.width:
                left = imgui.get_cursor_screen_pos()[0]
                kwargs['width'] = max(1., min(host.content_width or host.width,
                                              host.abs_left + host.width - left))
            else:
                kwargs['width'] = max(1., imgui.get_content_region_available()[0])
        record = self.native_keys.get((view, key))
        if record is None:
            record = self.native_keys[view, key] = Native()
        record.owner = self.stack[-1] if self.stack else None
        record.host, record.clip = host, Melty.get_clip_rect()
        record.tint = Melty.style_manager.get_tint()
        record.frame = Melty.frame_count
        if record.owner is not None:
            if record.owner.natives is None:
                record.owner.natives = set()
            record.owner.natives.add(record)
        if record.ambient is None or record.islands:
            # What a legacy view called inside this view runs in (`site`).
            record.ambient = Ambient.capture()
        self.stats['native declared'] += 1
        if _tree_path:
            Catalog.seen(view.__gui_definition__[0], 'native @gui')
        previous, self.declaring_native = self.declaring_native, record
        try:
            result = inner(input_value, **kwargs)
        finally:
            self.declaring_native = previous
        if record.islands:
            self.service(record)
        return result

    def failed(self, node, error):
        """RetainedGui.on_failure: a native node Rust ran on its own raised.
        It becomes caller-bound, so the error surfaces where a legacy caller
        declares it next (the legacy error boundary), not from a flush."""
        import traceback
        self.stats['native failed'] += 1
        self.cache.graph.bind(node)
        message = f'{type(error).__name__}: {error}'
        if message not in self.reported:
            self.reported.add(message)
            traceback.print_exception(error)

    def declared(self, node, parent):
        """RetainedGui.on_declare: learn the node of the native view being declared."""
        record = self.declaring_native
        if record is None or node in self.nodes:
            return
        if parent == (record.owner.node if record.owner is not None else 0):
            if record.node != node:
                self.natives.pop(record.node, None)
                record.node = node
                self.natives[node] = record
            self.declaring_native = None

    def record(self, node):
        """The legacy call site a native node descends from."""
        graph = self.cache.graph
        while node and node not in self.nodes:
            record = self.natives.get(node)
            if record is not None:
                return record
            node = graph.parent(node)
        return None

    def alive(self, record):
        """Whether a Native or an Entry is part of what is on screen."""
        from meltygui.core.melty import Melty
        owner = record.owner if isinstance(record, Native) else record.parent
        if owner is None:
            return record.frame >= Melty.frame_count - 1
        return record.frame >= owner.run_frame and self.alive(owner)

    def reachable(self, record, x, y):
        """The legacy hover rule for a native view: on screen, inside the
        caller's clip, and under no Melty window that floats above the caller."""
        from meltygui.core.melty import Melty
        if not self.alive(record) or not (Melty.imgui_main_window_hovered or Melty.imgui_popup_open):
            return False
        clip = record.clip
        if clip is not None and not (clip[0] <= x < clip[2] and clip[1] <= y < clip[3]):
            return False
        host = record.host
        window = host.parent_window if host is not None else None
        depth = host.shadow_depth if host is not None else 0
        for state in Melty.bvh_query(x, y):
            if state.closable and state is not host and state is not window and state.shadow_depth > depth:
                return False
        return True

    # ---- legacy invalidation stream -------------------------------------
    def owns(self, tile, draw_state):
        """A legacy view rendered inside the executing node."""
        if self.stack:
            self.owners[tile] = self.stack[-1]

    def invalidated(self, tile, reason='invalidate'):
        entry = self.owners.get(tile)
        woke = False
        while entry is not None:
            if not entry.pending:
                entry.pending, entry.reason, woke = True, reason, True
            entry = entry.parent
        if woke:
            # As in the legacy cache, an invalidation asks for no frame of its
            # own: whoever changed something calls request_render.
            self.stats['invalidated'] += 1

    def invalidate_all(self, reason='invalidate_all'):
        for entry in self.entries.values():
            entry.pending, entry.reason = True, reason

    def close(self):
        if self.frame is not None:
            self.finish()
        self.keyboard.gui_character_callback = None
        self.cache.on_declare = None
        self.cache.close()
        if self.runtime is not None:
            self.runtime.clear()
            self.runtime = None
        for mapping in (self.views, self.entries, self.nodes, self.owners, self.bindings, self.natives,
                        self.native_keys):
            mapping.clear()


def anchor(cursor, state):
    """Where the caller's cursor sits relative to the legacy draw state, in
    whole pixels. Constant while the legacy geometry tracks the caller."""
    return round(cursor[0] - state.abs_left), round(cursor[1] - state.abs_top)


def legacy_name(func, input_value, o_kwargs, kwargs):
    """The name the legacy wrapper would hash for this call (core_render)."""
    from meltygui.core.melty import Melty
    name = kwargs.get('name', o_kwargs.get('name', ''))
    collection = kwargs.get('collection')
    if name == '' and Melty.depth == 0:
        return 'Unnamed' + func.__name__ + collection.__class__.__name__
    name_func = kwargs.get('name_func', o_kwargs.get('name_func'))
    if name_func is not None:
        try:
            name = str(name_func(input_value))
        except Exception as error:
            name = str(error)
    if name == '':
        if isinstance(input_value, (int, float, str, bool)):
            text = str(input_value)
            short = text[:10] + '...' if len(text) > 10 else text
            return (short.replace(' ', '_').replace('\n', '_').replace('%', '_').replace('/', '_')
                    + collection.__class__.__name__)
        return str(kwargs.get('key', '')) + collection.__class__.__name__
    return name


class Catalog:
    """MELTY_RUST_TREE: which view functions the running app draws, who calls
    them and how each call was served. Times come from the legacy wrapper's
    own exclusive-time rows (core_render, Melty._frame_view_ms)."""
    views = {}      # view name -> row
    hosts = []      # open fast hosts: (draw_state, body, start)
    frames = 0
    written = 0.

    @classmethod
    def row(cls, func):
        row = cls.views.get(func.__name__)
        if row is None:
            code = getattr(inspect.unwrap(func), '__code__', None)
            row = cls.views[func.__name__] = dict(
                module=func.__module__, qualname=func.__qualname__,
                file=code.co_filename if code else None, line=code.co_firstlineno if code else None,
                calls=0, served=Counter(), parents=Counter(), depth=99)
        return row

    @classmethod
    def seen(cls, func, served):
        from meltygui.core.melty import Melty
        row = cls.row(func)
        row['calls'] += 1
        row['served'][served] += 1
        row['depth'] = min(row['depth'], Melty.depth)
        stack = Melty.draw_state_stack
        caller = getattr(stack[-1], '_view_func', None) if stack else None
        row['parents'][caller.__name__ if caller is not None else '(window root)'] += 1

    @classmethod
    def install(cls):
        """Fast hosts (core/rendering/fast_view.py) never reach `route`. Count
        each at its bind and time it up to its finish on the wrapper's
        exclusive stack, so its time leaves the enclosing view's row. The
        three functions are looked up per call, so replacing them is enough."""
        from meltygui.core.melty import Melty
        from meltygui.core.rendering import fast_view
        bind, skip, finish = fast_view.bind_fast_draw_state, fast_view.skip_offscreen, fast_view.finish_fast_view

        def close(draw_state, kind):
            if not cls.hosts or cls.hosts[-1][0] is not draw_state:
                return      # unwound by an exception: the next frame rebalances
            _, body, start = cls.hosts.pop()
            stack, total = Melty._view_excl_stack, time.perf_counter() - start
            child = stack.pop() if stack else 0.
            if stack:
                stack[-1] += total
            seconds, count = Melty._frame_view_ms.get(body.__name__ + kind, (0., 0))
            Melty._frame_view_ms[body.__name__ + kind] = (seconds + total - child, count + 1)

        @wraps(bind)
        def bound(body, host, input_value, kwargs):
            start = time.perf_counter()
            cls.seen(body, 'fast host')
            draw_state = bind(body, host, input_value, kwargs)
            cls.hosts.append((draw_state, body, start))
            Melty._view_excl_stack.append(0.)
            return draw_state

        @wraps(skip)
        def skipped(draw_state, kwargs):
            off = skip(draw_state, kwargs)
            if off:
                close(draw_state, '~s')
            return off

        @wraps(finish)
        def finished(draw_state, *args, **kwargs):
            try:
                return finish(draw_state, *args, **kwargs)
            finally:
                close(draw_state, '~f')
        fast_view.bind_fast_draw_state, fast_view.skip_offscreen, fast_view.finish_fast_view = bound, skipped, finished

    @classmethod
    def frame(cls):
        from meltygui.core.melty import Melty
        if getattr(Melty, '_frame_view_ms', None) is None:
            Melty._frame_view_ms, Melty._frame_view_body_ms = {}, {}
            cls.install()
        Melty._view_excl_stack = []     # rebalanced each frame, as the studio does
        del cls.hosts[:]
        cls.frames += 1
        now = time.monotonic()
        if now - cls.written >= 1.0:
            cls.written = now
            times, bodies = Melty._frame_view_ms, Melty._frame_view_body_ms
            rows = {}
            for name, row in cls.views.items():
                rows[name] = dict(row, served=dict(row['served']), parents=dict(row['parents']),
                                  full=times.get(name, (0., 0)), replay=times.get(name + '~r', (0., 0)),
                                  early=times.get(name + '~w', (0., 0)), body=bodies.get(name, 0.),
                                  fast=times.get(name + '~f', (0., 0)), offscreen=times.get(name + '~s', (0., 0)))
            with open(_tree_path, 'a') as stream:
                stream.write(json.dumps(dict(t=round(time.time(), 2), frames=cls.frames, views=rows)) + '\n')


def route(wrapper, func, o_kwargs, input_value, kwargs):
    """Called at the top of every legacy wrapper call while a bridge draws."""
    bridge = Bridge.current
    if bridge is None:
        if _tree_path:
            Catalog.seen(func, 'outside the bridge')
        return PASS
    stack = bridge.cache.stack
    if stack and stack[-1] not in bridge.nodes:
        # A native node draws in its own coordinates and runs on Rust's
        # schedule; a legacy body needs window coordinates and legacy stacks.
        return bridge.island(stack[-1], wrapper, func, o_kwargs, input_value, kwargs)
    surface = kwargs.get('glfw_window')     # a child OS window: its own surface draws it
    closable = bool(kwargs.get('closable', o_kwargs.get('closable')) or kwargs.get('as_window'))
    # The legacy wrapper caches a closable window unless it is told not to.
    cached = kwargs.get('use_cache', o_kwargs.get('use_cache', closable))
    window = not _windows and (closable or kwargs.get('melty_window', o_kwargs.get('melty_window')))
    name = func.__name__
    if surface:
        if _tree_path:
            Catalog.seen(func, 'child OS window request')
        return PASS
    if (not cached or window or kwargs.get('just_shadow')
            # A window queues itself inline and draws on its layer-pass re-entry.
            or ('layer_unique' in kwargs) != (_windows and closable)
            or (_only and name not in _only) or name in _skip):
        if cached and bridge.stack:
            # A legacy tile's texture can be freed under the packet that drew it.
            kwargs['use_cache'] = False
            bridge.stats['uncached in node'] += 1
        elif cached:
            bridge.stats['legacy cached'] += 1
        if _tree_path:
            Catalog.seen(func, 'legacy window' if closable else
                         'legacy cached' if kwargs.get('use_cache', cached) and cached else 'legacy uncached')
        return PASS
    bridge.stats['routed'] += 1
    if _tree_path:
        runs = bridge.stats['executed']
        try:
            return bridge.legacy_view(wrapper, func, o_kwargs, input_value, kwargs)
        finally:
            Catalog.seen(func, 'node run' if bridge.stats['executed'] != runs else 'node hit')
    return bridge.legacy_view(wrapper, func, o_kwargs, input_value, kwargs)


def tile_cache():
    """The surface's legacy cache, forwarding invalidations to its bridge."""
    from meltygui.core.cache.tile_cache import TileCacheMasked

    class RustTileCache(TileCacheMasked):
        bridge = None

        def mark_uncached(self, name, input_value, collection, key, draw_state):
            super().mark_uncached(name, input_value, collection, key, draw_state)
            if self.bridge is not None:
                self.bridge.owns(key, draw_state)

        def invalidate(self, k, *args, **kwargs):
            super().invalidate(k, *args, **kwargs)
            if self.bridge is not None:
                self.bridge.invalidated(k)

        def invalidate_up(self, k, *args, **kwargs):
            # Before the legacy lookup swaps an untiled key for its parent's.
            if self.bridge is not None:
                self.bridge.invalidated(k, 'invalidate_up')
            super().invalidate_up(k, *args, **kwargs)

        def invalidate_parent(self, k, *args, **kwargs):
            if self.bridge is not None:
                self.bridge.invalidated(k, 'invalidate_parent')
            super().invalidate_parent(k, *args, **kwargs)

        def invalidate_all(self, other_windows=True):
            super().invalidate_all(other_windows=other_windows)
            if self.bridge is not None:
                self.bridge.invalidate_all()

    return RustTileCache()


def draw_root(surface, draw):
    """Draw one OS window's legacy root through its bridge."""
    bridge = getattr(surface, '_rust_compat', None)
    if bridge is None:
        bridge = surface._rust_compat = Bridge(surface)
        from meltygui.core.melty import Melty
        if hasattr(Melty.cache, 'bridge'):
            Melty.cache.bridge = bridge
    bridge.begin()
    return draw()


def draw_native_root(surface, view, view_kwargs):
    """Draw one OS window whose root is a @gui view through its bridge, so the
    root and the native views below it can call legacy views. The root fills
    the window below the title bar, as it does on the native host."""
    import meltygui_imgui as imgui
    from meltygui.core.melty import Melty

    def draw():
        width, height = Melty.root_fill[:2] if Melty.root_fill is not None else imgui.get_content_region_available()
        arguments = dict(view_kwargs)
        value = arguments.pop('value', None)
        arguments.setdefault('width', width)
        arguments.setdefault('height', height)
        return view(value, **arguments)
    return draw_root(surface, draw)


def in_native_body():
    """fast_view.needs_wrapper: a wrapperless host paints into its caller's
    draw list, and a native body's is in that view's own coordinates."""
    bridge = Bridge.current
    if bridge is None:
        return False
    stack = bridge.cache.stack
    return bool(stack) and stack[-1] not in bridge.nodes


# Lever: MELTY_FRAME_LOG=path appends, once a second, the CPU time of the
# surface frames drawn (UI submission only: no swap, no GPU). Works with and
# without MELTY_RUST, so the two hosts can be compared on the same input.
_frame_log = os.environ.get('MELTY_FRAME_LOG')
_frames = []
_frames_at = [0.]


def frame_clock():
    return time.perf_counter() if _frame_log else None


def frame_done(started):
    now = time.perf_counter()
    _frames.append(now - started)
    if now - _frames_at[0] >= 1.0:
        ordered = sorted(_frames)
        with open(_frame_log, 'a') as stream:
            stream.write(json.dumps(dict(t=round(time.time(), 2), frames=len(ordered),
                                         mean_ms=1000 * sum(ordered) / len(ordered),
                                         median_ms=1000 * ordered[len(ordered) // 2],
                                         p95_ms=1000 * ordered[int(len(ordered) * .95)],
                                         max_ms=1000 * ordered[-1])) + '\n')
        _frames.clear()
        _frames_at[0] = now
