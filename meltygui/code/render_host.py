"""
RenderHost — a dict that drives a stateful wrapper and holds its editable value.

A `RenderHost` is just a `dict`. It is NOT a view function. Instead it WRAPS a
stateful func (`code_file_io`, `convert_in_and_out_value`) that follows the shape

    stateful_work()  →  view_func(value)  →  stateful_work()

(load → edit → save for code_file_io; chain_in → edit → chain_out for convert). The
host hands that wrapper its OWN private view_func; whatever the wrapper passes in —
a source STRING from code_file_io, the parsed TREE from convert_in_and_out_value —
is materialized into the host's dict (held under `value_key`, type intact), rendered,
and any edit flows back out the same channel. So the host "looks like a dict with a
single value inside," and you use it like that value.

Chaining is plain data-flow through `input_value`: a proxy whose `input_value` is
another `RenderHost` reads that upstream proxy's held value ("used like a string").
Edits flow back upstream — `draw()` writes the wrapper's output onto the upstream
proxy, which goes dirty and saves one frame later. That one-frame lag is the only
cost of decoupling the proxies into independent `draw()` calls.

    string_proxy = RenderHost(wrapper=code_file_io,             input_value=InCode)
    dict_proxy   = RenderHost(wrapper=convert_in_and_out_value, input_value=string_proxy,
                              child_kwargs={chain_in, chain_out, route})

`draw_main` calls `draw()` on each registered host every frame (upstream first), so
each proxy renders into its own little Melty window; and because it's a plain dict,
`draw_collection(proxy)` renders it anywhere too. Nested edits bubble (see
bubbling.py) so a deep change to the held tree marks the host without a manual touch.
"""

import sys

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.bubbling import install_bubbling, _reinstall_children, _DeepAttrMixin
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, Core
from src.lsd.gl_gui.view.invalidation_tracker import Note

_UNSET = object()


@defaults()
class RenderHost(_DeepAttrMixin, dict):
    """A dict that drives a stateful `wrapper` and holds the value it edits.

    Args:
      io_function:      the stateful func to drive (code_file_io / convert_in_and_out_value).
                    `None` → a plain data-bag: `draw()` just renders the dict. This can contain UI but does not need to
                    Any UI drawn as part of this method will end up in a settings panel, not the main UI
      input_value:  what the wrapper operates on — a raw value (a class, a path), or
                    an upstream `RenderHost` (resolved to its held value each frame).
      child_kwargs: extra kwargs for the wrapper (e.g. convert's chain_in/out + route).
      settings_renderer: This is draw inside the settings panel, and entirely optional

                    (default: `draw_any`, which routes str → editor, dict → collection).
      value_key:    the dict key the held value lives under (type intact).
      standalone:   register with Melty so draw_main calls `draw()` (its own window)."""

    # The host currently rendering, so render_host_view can find it without threading
    # it through cache-hashed kwargs.
    _active = []

    # Log every change the proxy triggers (dict-dirty + outbound edits) with a caller
    # trail - for diagnosing spurious saves. `RenderHost.debug_changes = False` silences.
    debug_changes = False

    def __init__(self, io_function=None, *args, input_value=None, child_kwargs=None,
                 settings_renderer=None, name=None, hidden=False, window=True, standalone=True,
                 value_key="value", **extra):
        super().__init__(*args)
        self.io_function = io_function
        # `is None` (not falsy): an empty host-dict is a valid input_value.
        self.input_value = self if input_value is None else input_value
        # Wrapper options (chain_in/out, route, ...). `extra` kwargs fold in too, so you
        # can pass them directly instead of nesting a child_kwargs dict.
        self.child_kwargs = {**(child_kwargs or {}), **extra}
        self.settings_renderer = settings_renderer
        self.name = name or getattr(io_function, "__name__", None) or "RenderHost"
        self.hidden = hidden
        self.window = window
        self.standalone = standalone
        # The held value lives under this key, type intact - so a GeneralParse stays a
        # GeneralParse for chain_out, and the proxy "looks like a dict with one value".
        self.value_key = value_key

        self._external_change = False   # dict (or a nested value) mutated since last render
        self._draw_state = None         # render_host_view's draw_state (the window envelope)
        self._wrapper_draw_state = None  # the WRAPPER's draw_state (code_file_io / convert) via
                                         # return_extras - so a value edit can invalidate IT and
                                         # the wrapper actually re-runs to process the change.
        self._last_resolved = _UNSET    # last resolved input - drives the wrapper's external_change
        self._pending_external = False  # input changed (detected in draw()) → external_change for the wrapper
        self._awaiting_inbound = False  # an upstream edit happened; a remote derived result (e.g.
                                        # convert's BACKGROUND chain_in) is in flight - materialize it
                                        # when it lands, not just on the pulse frame.
        # Frame stamps for event ordering. The held value (a LOCAL edit) is set
        # immediately; the wrapper's derived result (a re-parse) is asynchronous and may be
        # based on a source that lags the last local edit. We only accept an inbound
        # result when the source it's based on is at least as NEW as the last local edit
        # - otherwise the newest event (the local edit) wins. See _internal_view_func.
        self._input_change_frame = 0    # frame the resolved INPUT (source) last changed
        self._local_edit_frame = 0      # frame the held value was last LOCALLY edited
        self._last_return = None
        self._registered = False

        # Bubble nested changes: upgrade existing contents + a non-self input tree so
        # a deep edit (held['cfg']['rank'] = 16, or a grabbed GeneralParse) marks the
        # host without a manual touch(). See bubbling.py.
        if len(self):
            _reinstall_children(self, self)
        if self.input_value is not self and not isinstance(self.input_value, RenderHost):
            self.input_value = install_bubbling(self.input_value, self)

        if standalone:
            self.register()

    # ── Registration ──────────────────────────────────────────────────────────
    def register(self):
        """Add to Melty.render_hosts so draw_main calls draw() each frame. Idempotent."""
        if not self._registered:
            Melty.render_hosts[id(self)] = self
            self._registered = True

        return self

    def remove(self):
        Melty.render_hosts.pop(id(self), None)
        self._registered = False
        return self

    @classmethod
    def all(cls):
        return list(Melty.render_hosts.values())

    @classmethod
    def current(cls):
        return cls._active[-1] if cls._active else None

    # ── Visibility ────────────────────────────────────────────────────────────
    def show(self):
        self.hidden = False
        request_render()
        return self

    def hide(self):
        self.hidden = True
        request_render()
        return self

    def toggle(self):
        self.hidden = not self.hidden
        request_render()
        return self

    # ── Change logging (diagnose spurious saves) ──────────────────────────────
    def _caller_trail(self, frames=4, skip=2):
        out = []
        try:
            f = sys._getframe(skip)
            for _ in range(frames):
                if f is None:
                    break
                base = f.f_code.co_filename.rsplit("/", 1)[-1]
                out.append(f"{base}:{f.f_lineno} {f.f_code.co_name}")
                f = f.f_back
        except Exception:
            pass
        return " <- ".join(out)

    def _log_change(self, event, **info):
        if not RenderHost.debug_changes:
            return
        extra = "  ".join(f"{k}={v}" for k, v in info.items())
        print(f"[RenderHost:{self.name}] {event}  {extra}  ║ {self._caller_trail()}")

    # ── Dict mutation → external_change (bubble root) ──────────────────────────
    def _mark_changed(self):
        self._external_change = True
        self._local_edit_frame = Melty.frame_count   # stamp: a LOCAL edit happened NOW
        # Invalidate BOTH the window envelope AND the wrapper's own draw_state: the
        # envelope so render_host_view re-runs and calls the wrapper, and the wrapper
        # so its blit-cached body actually re-executes to process the edit (load/save
        # or chain_out). Invalidating only the envelope leaves the wrapper replayed.
        for ds in (self._draw_state, self._wrapper_draw_state):
            if ds is not None:
                try:
                    # ds.invalidate(frame_delta=0)
                    # ds.invalidate(frame_delta=1)
                    note = Note(name="RenderHost _mark_changed", tint=(1, 0.5, 0), draw_state=ds)
                    ds.invalidate(note=note)

                except Exception:
                    pass
        self._log_change("DIRTY (dict/nested mutated)")
        request_render()

    def touch(self):
        """Manually flag a change. Rarely needed — nested dict/list/GeneralParse edits
        bubble automatically (bubbling.py). Use for an ATTRIBUTE write on a nested
        non-container, or an in-place edit of an opaque leaf. Returns self."""
        self._mark_changed()
        return self

    # NOTE: deep path traversal is the EXPLICIT `.deep` property (from _DeepAttrMixin),
    # never a blanket __getattr__. A blanket __getattr__ would answer the framework's
    # bare attribute probes on the host with a _DeepPath stand-in instead of raising
    # AttributeError, and rendering (which expects a _BubblingDict) chokes on it. Bare
    # `host.foo` must behave like a normal dict/object; use `host.deep.foo...()` to
    # traverse.

    def __setitem__(self, key, value):
        # Re-assigning an equal value isn't an edit (draw_collection writes its
        # rendered value back each frame) - store it but don't mark dirty / redraw.
        unchanged = key in self and dict.__getitem__(self, key) is value

        value = install_bubbling(value, self)
        super().__setitem__(key, value)
        if not unchanged:
            self._mark_changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._mark_changed()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        _reinstall_children(self, self)
        self._mark_changed()

    def setdefault(self, key, default=None):
        existed = key in self
        if not existed:
            default = install_bubbling(default, self)
        result = super().setdefault(key, default)
        if not existed:
            self._mark_changed()
        return result

    def pop(self, *args):
        had = bool(self) and (not args or args[0] in self)
        result = super().pop(*args)
        if had:
            self._mark_changed()
        return result

    def popitem(self):
        result = super().popitem()
        self._mark_changed()
        return result

    def clear(self):
        if self:
            super().clear()
            self._mark_changed()
        else:
            super().clear()

    # ── The host's private view_func (handed to its wrapper) ───────────────────
    def _internal_view_func(self, input_value=None, external_change=False, **kwargs):
        """The view_func the host hands its wrapper. The wrapper passes the value to
        edit — a source STRING (code_file_io) or the parsed TREE (convert_in_and_out_
        value) — and we:
          INBOUND : materialize it into our dict (held under value_key), unless we're
                    mid-edit (dirty) — never clobber a change flowing the other way.
          RENDER  : draw the held value (str → editor, dict → collection). A dict/tree
                    edit mutates in place and bubbles (→ dirty); a string edit returns.
          OUTBOUND: if anything changed, return (True, held) so the wrapper saves /
                    chain_outs it. Returns the held value in the wrapper's native shape."""
        pre_dirty = self._external_change
        self._external_change = False

        # INBOUND. The HELD value is AUTHORITATIVE: it was set via __setitem__ (the
        # user's draw_text string) and flows OUT to the wrapper. We do NOT materialize
        # the wrapper's value (code_file_io's text_cache, which LAGS the latest
        # keystrokes and is a stale-to-save snapshot) back over it - that's the "save
        # replaces the text with the saved version, losing chars typed during edit" bug.
        #
        # Two exceptions:
        #   - a genuine UPSTREAM change - draw() saw a NEW resolved input object and set
        #     `_pending_external` (NOT the wrapper's `external_change` param, which for
        #     code_file_io is just its per-keystroke reconvert pulse) - wins even over a
        #     local dirty; drop the stale dirty so we don't surface a spurious outbound.
        #   - an INITIAL fill, when nothing is held yet.
        # An upstream change (draw() set `_pending_external`) means the wrapper will
        # produce a FRESH derived result - but convert's chain_in re-parses on a
        # BACKGROUND thread, so it fires a frame+ after the pulse. Latch "awaiting" so we
        # still materialize it when it arrives (a new object), not just on the pulse
        # frame - otherwise the gate blocks the catch-up and the dict holds the PREVIOUS
        # parse forever ("dict always holds the old snapshot").
        if self._pending_external:
            self._awaiting_inbound = True

        # FILE-WATCHER / external reload. The codec registers a file watcher on the
        # host's draw_state at resolve_address; when the file changes, FileWatch sets
        # that draw_state dirty and code_file_io re-reads the file into text_cache. draw()
        # CAN'T see this - code_file_io's input object (a class / path) is immutable, so
        # _pending_external never fires for it. Detect it here instead: the wrapper passed
        # us a value we did NOT produce (an external_change pulse whose input_value is a
        # DIFFERENT object than our held one) while we're not mid-edit. A keystroke does
        # NOT match - after we surface, text_cache IS our held object, so `is not held` is
        # false. Treat it like an upstream change (stamp input_change so frame precedence
        # treats the source as fresh, arm awaiting); the pull below then materializes it.
        if (external_change and input_value is not None and input_value is not self
                and input_value is not self._held() and not pre_dirty):
            self._input_change_frame = Melty.frame_count
            self._awaiting_inbound = True
            # The file changed on disk - the wrapper/blit cache won't know to re-render
            # the reloaded value, so invalidate this proxy's view explicitly (same path
            # as the initial fill below).
            if self._draw_state is not None and self._draw_state._parent is not None:
                self._draw_state._parent.invalidate_by_obj(
                    obj=self, note=Note(name="file_reload", tint=(1, 0.6, 0.1)))
            request_render()

        # FRAME PRECEDENCE (all O(1) - no content comparison). Is there a GENUINE pending
        # local edit, newer than the source it round-trips to? Each cross-thread round-trip
        # adds a frame of lag, and rendering the held value writes reconstructed children
        # back (draw_collection) a frame later - a local 1-frame "edit" echo. So a diff
        # of ≤1 is NOT genuine; only a local edit MORE than 1 frame ahead of the source
        # counts (a live drag, whose source lags by the whole debounced save round-trip,
        # many frames). This single predicate drives BOTH directions and replaces the
        # `not pre_dirty` gate, which the echo tripped every cycle (the catch-up ed
        # re-surfacing → re-saving forever).
        local_ahead = self._local_edit_frame > self._input_change_frame + 1

        if input_value is not None and input_value is not self:
            if self.value_key not in self:
                self._materialize(input_value)
                note = Note(name="_materialize", tint=(1, 1.0, 1.0))
                # Needed for initial load
                self._draw_state._parent.invalidate_by_obj(obj=self, note=note)
                request_render()

                # initial fill
            elif (self._awaiting_inbound and external_change
                  and input_value is not self._held()
                  and not local_ahead):

                # Pull only the fresh parse. `external_change` is the wrapper signalling a
                # FRESH derived result THIS frame (convert sets it only on the frame its
                # background chain_in FINISHES, with the current source's parse - and
                # run_in_background coalesces to latest-only, so a finished result is never
                # superseded). Without this gate we'd pull whatever `primary` happens to be
                # when `local_ahead` flips false - which, mid round-trip, is the STALE
                # last-good parse of the PREVIOUS source → the 428↔445 snap-back/oscillation.
                # `not local_ahead` still lets a genuine live edit win.
                # Needed
                note = Note(name="Render host, self", tint=(1, 1, 1))
                self._draw_state._parent.invalidate_by_obj(obj=self, note=note)
                self._materialize(input_value)
                self._awaiting_inbound = False

        renderer = self.settings_renderer or RenderFuncs.draw_blank
        render_kwargs = {k: v for k, v in kwargs.items() if k != "routed"}
        render_kwargs.setdefault("name", f"{self.name}##held")
        result = renderer(self._held(), **render_kwargs)
        r_changed, r_new = (result[0], result[1]) if isinstance(result, tuple) and len(result) >= 2 else (False, result)
        # A string edit (immutable) comes back via the return; store it. A dict/tree
        # edit mutated in place and already set _external_change via bubbling.
        if r_changed and self.get(self.value_key) is not r_new:
            # Needed
            self[self.value_key] = r_new
            self._draw_state._parent.invalidate_by_obj(obj=input_value)
            self._draw_state._parent.invalidate_by_obj(obj=self)

            request_render()

        # OUTBOUND only a GENUINE local edit - one MORE than 1 frame ahead of the source
        # (a real drag), NOT the 1-frame draw_collection frame echo. Without this, the
        # echo re-surfaces the just-materialized value every cycle → chain_out → save in
        # a self-sustaining loop. r_changed (the renderer's own edit) always surfaces.
        if self._external_change:
            self._wrapper_draw_state._parent.invalidate(obj=input_value)

            self._draw_state._parent.invalidate_by_obj(obj=input_value)
            self._draw_state._parent.invalidate_by_obj(obj=self)

            request_render()

        edited = bool((pre_dirty and local_ahead) or self._external_change or r_changed)
        self._external_change = False
        if edited:
            # Needed
            note = Note(name="Render host, nested view edited", tint=(1, 1, 1))
            self._draw_state._parent.invalidate_by_obj(obj=input_value, note=note)
            request_render()
            note = Note(name="Render host, self", tint=(1, 1, 1))
            self._draw_state._parent.invalidate_by_obj(obj=self, note=note)

            self._log_change("OUTBOUND → return (True, value)",
                             pre_dirty=pre_dirty, r_changed=r_changed, local_ahead=local_ahead)
            return True, self._outbound_value()
        return False, self._outbound_value()

    # ── Materialize / surface ──────────────────────────────────────────────────
    def _materialize(self, value):
        """Hold an INBOUND value under value_key (type intact, bubbling installed).

        IDEMPOTENT by identity: already holding this exact object → no-op (the wrapper
        hands back the SAME parsed object every frame until a re-parse; without this
        the render loop spins). RAW dict ops, so it doesn't re-fire _mark_changed."""
        if value is self:
            return
        if self.value_key is not None:
            if self.get(self.value_key) is value:
                return
            new = {self.value_key: value}
        elif isinstance(value, dict):
            if len(self) == len(value) and all(self.get(k) is v for k, v in value.items()):
                return
            new = value
        else:
            new = {"value": value}
        dict.clear(self)
        dict.update(self, new)
        _reinstall_children(self, self)
        request_render()

    def _held(self):
        """The single held value (string / tree) the wrapper edits — None until first
        materialized (so a not-yet-ready upstream proxy resolves to None). The whole
        dict in mirror mode (value_key=None)."""
        if self.value_key is not None:
            return self.get(self.value_key)
        return self

    _outbound_value = _held

    def _set_held(self, value):
        """Cross-proxy write: a downstream proxy pushes the wrapper's output (a new
        source string) onto this upstream proxy → it surfaces to ITS wrapper and saves.

        The LOCAL __setitem__ value is AUTHORITATIVE: a pending local edit (dirty — the
        user just typed) is NOT overwritten, so a late/stale background result (a
        debounced chain_out snapshotted frames ago) can't clobber what's being typed.
        All O(1) — no content comparison."""
        if self.value_key is None:
            return
        if self._external_change:
            return                              # pending local edit wins over a write-back
        if self.get(self.value_key) is value:
            return                              # same object - no change
        self[self.value_key] = value

    # ── Lifecycle: draw the wrapper in this host's window (draw_main calls this) ─
    def draw(self, **extra):
        """Drive the wrapper for one frame. Resolve input_value (an upstream proxy →
        its held value), run the wrapper with our private view_func via the window
        envelope, and write the wrapper's output back upstream on edit."""
        if self.hidden:
            return None
        from src.lsd.gl_gui.view.mode import Mode

        iv = self.input_value
        if isinstance(iv, RenderHost):
            iv = iv._held()

        # Detect the input change HERE, not in render_host_view. draw() runs EVERY frame
        # (draw_main is uncached), whereas render_host_view's window body is blit-cached
        # and won't run on the frame an upstream proxy's value changed - so detecting it
        # there drops the external_change event intermittently. On a change: flag it for
        # the wrapper (so convert re-parses) AND invalidate the wrapper so its body
        # actually re-runs to consume the flag. Identity compare - a changed source is a
        # new string object; no content comparison needed.


        if iv is not self._last_resolved:
            self._last_resolved = iv
            self._pending_external = True
            self._input_change_frame = Melty.frame_count   # stamp: the input (source) changed here
            # Invalidate BOTH the window envelope AND the wrapper's draw_state. The
            # envelope so render_host_view re-runs and CALLS the wrapper; the wrapper
            # so its blit-cached body actually re-executes (re-parses) - invalidating
            # only the envelope re-calls a wrapper that just replays its cache, so the
            # re-parse never happens and the change is lost (an intermittent bug).
            for ds in (None, self._wrapper_draw_state):
                if ds is not None:
                    note = Note(name="Renderhost _last_resolved", tint=(1, 0.0, 0.0), draw_state=ds)
                    ds.invalidate(note=note)
                    ds.invalidate_by_obj(obj=self, note=note)

            request_render()

        win_kwargs = {"name": self.name}
        if self.window:
            win_kwargs.setdefault("mode", Mode.HOST_WINDOW)
            win_kwargs["active_layer"] = 1
            win_kwargs["unmanaged"] = True

        win_kwargs.update(extra)

        RenderHost._active.append(self)
        try:
            win_kwargs['tint'] = (0.3, 0, 0.7, 0.1)
            win_kwargs['height'] = 20

            result = render_host_view(iv, **win_kwargs)
        finally:
            RenderHost._active.pop()
        self._last_return = result if isinstance(result, tuple) else (False, result)

        return self._last_return[1]

    def __repr__(self):
        w = getattr(self.io_function, "__name__", self.io_function)
        flags = [f for f in ("hidden" if self.hidden else "", "dirty" if self._external_change else "",
                             "" if self.standalone else "embedded") if f]
        tail = f" [{', '.join(flags)}]" if flags else ""
        return f"RenderHost({self.name!r} -> {w}{tail}, {dict.__repr__(self)})"


@render_func(use_cache=True, selectable=False, temp=True)
def render_host_view(input_value, external_change=False, draw_state=None, name=None, **kwargs):
    """Window envelope + wrapper driver for a RenderHost (draw_main → host.draw()).

    A render_func so the host gets a Melty window (mode=Mode.WINDOW chrome here) and a
    persistent draw_state. It runs the host's wrapper with the host's private
    view_func, computes the wrapper's `external_change` (the resolved input changed
    since last frame — so convert's chain_in re-parses on new source; code_file_io
    self-manages and ignores it), and writes the wrapper's output back to the upstream
    proxy on edit.

    Resolves its host by NAME, not the ambient `_active` stack: a Mode.WINDOW body
    renders DEFERRED (at frame end), after `draw()` has popped `_active`, so
    `RenderHost.current()` is None by then — the name (a passed kwarg) survives."""
    host = RenderHost.current()
    if host is None and name is not None:
        host = next((h for h in Melty.render_hosts.values() if h.name == name), None)
    if host is None:
        return False, input_value
    host._draw_state = draw_state

    # data-bag (no wrapper): just render the dict itself.
    if host.io_function is None:
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_collection
        return draw_collection(host, name=host.name)

    # external_change for the wrapper: the framework's, OR the input-changed flag that
    # draw() set this frame (computed there because draw() runs every frame; consuming
    # it here, in the deferred/cached body, is reliable since draw() also runs us).
    ext = bool(external_change or host._pending_external)

    # return_extras=True → the wrapper hands back its OWN draw_state as a 3rd value;
    # store it so a later value edit can invalidate it (see _mark_changed) and the
    # wrapper actually re-runs instead of replaying its blit cache. NOTE: reset
    # _pending_external AFTER the wrapper - _internal_view_func reads it (during this
    # call) to tell a genuine upstream change from the wrapper's own per-keystroke pulse.
    result = host.io_function(input_value=input_value, view_func=host._internal_view_func,
                          external_change=ext, return_extras=True, **host.child_kwargs)
    host._pending_external = False
    if isinstance(result, tuple) and len(result) == 3:
        edited, out, host._wrapper_draw_state = result
    else:
        edited, out = result

    # Cross-proxy write-back: the wrapper's output (e.g. convert's new source string)
    # lands on the upstream proxy, which goes dirty and saves one frame later.
    if edited and isinstance(host.input_value, RenderHost):
        host.input_value._set_held(out)
    return edited, out
