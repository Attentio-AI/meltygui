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
import threading
import time

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.notifications import notify
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.perf_trace import trace as _ptrace, trace_rl as _ptrace_rl
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

    # Class-level fallbacks for the consumer-notify debounce fields, so host
    # instances born before a hotswap of this class still resolve them.
    _local_edit_time = 0.0
    _pending_notify_name = None
    _notify_timer = None

    def __init__(self, io_function=None, *args, input_value=None, child_kwargs=None,
                 settings_renderer=None, name=None, hidden=False, window=True, standalone=True,
                 value_key="value", evictable=False, **extra):
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
        # evictable → the idle sweep (RenderHost.sweep) may DEREGISTER this host
        # from Melty.render_hosts when ALL its consumer windows close - it stays
        # in the code-host cache and re-registers on reopen (notify_on_change →
        # register). Module-level singleton proxies (files_proxy, claude_proxy, the
        # playground demos) default this False so they're never swept; only
        # code_hosts_for opts its short-lived cache pairs in.
        self.evictable = evictable
        self._birth_frame = Melty.frame_count   # idle-sweep birth grace
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
        self._local_edit_time = 0.0     # wall clock of the last local edit - drives the
                                        # consumer-notify debounce (typing_hold)
        self._pending_notify_name = None  # deferred _notify_consumers note name
        self._notify_timer = None         # armed flush timer for the deferred notify
        self._last_return = None
        self._registered = False
        # Draw_states that READ this host's value from OUTSIDE its own draw loop (e.g.
        # draw_input_tab renders host.deep.parameters() inside the context menu). The
        # host parses on a background worker in its own main window; when the value
        # finally materializes, request_render() wakes the loop but doesn't reach the
        # external cached subtrees - so they'd show nothing until an unrelated manual
        # invalidation. They register here and _materialize invalidates them on change.
        # Maps each consumer draw_state → the frame it last re-registered
        # (notify_on_change); the idle sweep reads that last-seen stamp (plus
        # abs_closed) to decide whether this host still has an alive user.
        self._consumers = {}

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
        """Add to Melty.render_hosts so draw_main calls draw() each frame. Idempotent.

        Also re-registers the upstream proxy in the chain (a dict_host's str_host):
        str_host has no consumers of its own, so reviving the dict_host after an idle
        sweep must bring its source proxy back too, or the value goes stale. Guarded on
        the upstream's own _registered flag, so a chain can't recurse forever."""
        if not self._registered:
            Melty.render_hosts[id(self)] = self
            self._registered = True

        iv = self.input_value
        if isinstance(iv, RenderHost) and iv is not self and not iv._registered:
            iv.register()

        return self

    def notify_on_change(self, draw_state):
        """Register an EXTERNAL consumer's draw_state to be invalidated when this host's
        held value next materializes/changes. Idempotent. For code that reads the host's
        value (e.g. host.deep.parameters()) and draws it OUTSIDE the host's own draw loop
        — without this its cached subtree never re-runs when a background parse lands.

        Doubles as the per-frame "I am a live user" pulse the idle sweep reads: call it
        every frame the consumer draws (not just on edit), so its last-seen stamp stays
        current. Reviving a host the sweep deregistered happens here too — reopening a
        closed view re-registers it (and its upstream proxy chain) in Melty.render_hosts."""
        if draw_state is None:
            return
        self._consumers[draw_state] = Melty.frame_count
        # Reopened view → the idle sweep had popped us from render_hosts; bring the
        # host (and its str_host) back into the draw loop. Idempotent.
        if self.standalone and not self._registered:
            self.register()
        # Toggle-off fallback: with no sweep pruning each frame, cap growth by
        # dropping closed consumers when the map gets large.
        if len(self._consumers) > 64:
            self._consumers = {ds: f for ds, f in self._consumers.items()
                               if not getattr(ds, 'closed', False)}

    def remove(self):
        Melty.render_hosts.pop(id(self), None)
        self._registered = False
        return self

    # One outstanding catch-up timer for the typing hold (class-wide - the hold
    # gates ALL hosts at once in draw_main's loop).
    _typing_wake_timer = None

    @classmethod
    def typing_hold(cls):
        """True while host draws should be deferred because the user is typing:
        a draw_text editor holds focus AND a key was pressed within the debounce
        window. The draw_main host loop checks this next to its click/drag/
        scroll skip — host draws (reconverts, saves) are deferrable work that
        would otherwise eat into typing frames. Arms a one-shot wake for the
        window's expiry so the skipped draws catch up even when no further
        input produces a frame; a newer keypress simply re-enters the hold on
        that wake's frame and chains the next one."""
        from src.lsd.gl_gui.toggles import Toggles
        window = Toggles.HostLifecycle.host_typing_debounce_ms / 1000.0
        if window <= 0 or Melty.text_focused_ds is None:
            return False
        # A physically HELD key counts as pressed regardless of the stamp -
        # repeats don't reliably re-stamp it (Wayland repeat gaps exceed the OS
        # repeat delay), so the hold would otherwise expire mid-press. Reconcile
        # against glfw.get_key first: a RELEASE that went elsewhere (focus
        # stolen mid-hold) would strand the key in the set and starve host
        # draws forever.
        held = bool(Melty._keys_down)
        if held and Melty.glfw_window is not None:
            import glfw
            for k in list(Melty._keys_down):
                try:
                    if glfw.get_key(Melty.glfw_window, k) != glfw.PRESS:
                        Melty._keys_down.discard(k)
                except Exception:
                    Melty._keys_down.discard(k)
            held = bool(Melty._keys_down)
        remaining = Melty._last_key_time + window - time.monotonic()
        if not held and remaining <= 0:
            return False
        # Catch-up wake: for a debounce tail, at its expiry; while a key is
        # held past the stamp window, a full window out - RELEASE re-stamps
        # and forces a frame anyway, the hold is just the safety net.
        timer = cls._typing_wake_timer
        if timer is None or not timer.is_alive():
            timer = threading.Timer(remaining if remaining > 0 else window,
                                    cls._typing_wake)
            timer.daemon = True
            cls._typing_wake_timer = timer
            timer.start()
        return True

    @classmethod
    def _typing_wake(cls):
        cls._typing_wake_timer = None
        request_render()

    @classmethod
    def all(cls):
        return list(Melty.render_hosts.values())

    @classmethod
    def current(cls):
        return cls._active[-1] if cls._active else None

    # ── Idle sweep: deregister hosts whose consumer windows have all closed ─────
    @staticmethod
    def _consumer_closed(ds):
        """A consumer draw_state counts as gone once its window is abs_closed —
        the primary, immediate trigger. A vanished/broken draw_state (raises) is
        treated as gone too."""
        try:
            return bool(ds.abs_closed)
        except Exception:
            return True

    def _prune_consumers(self, now, k):
        """Drop consumers that are abs_closed OR whose k-frame liveness net
        tripped. The net keys on the LATER of two stamps: registration
        (notify_on_change, body actually ran) and ds.last_seen (the blit
        compositor stamps it every frame the tile is drawn, even when the body
        cache-skips). A blit-cached view in an OPEN window stops registering
        for minutes but keeps compositing, so it survives; a closed dropdown /
        context-menu popover stops BOTH (and is never abs_closed — popovers
        don't set closed), so it ages out instead of pinning the host in
        Melty.render_hosts forever. Registration-only aging (the pre-2026-07
        net) pruned the blit-cached case too — host swept, external file
        change had no pump and no consumer, view froze on stale values.

        The net's window is deliberately wider than k: last_seen lags while
        an ANCESTOR's tile covers the consumer (only the topmost blitted tile
        gets stamped — measured lags of a few hundred frames on live views),
        so the raw k=120 would prune covered-but-live consumers and re-open
        the frozen-view hole. 10·k keeps those safe with ~5x margin while a
        popover orphan (measured 1000+ frames stale on BOTH stamps) still
        ages out and lets its host sweep."""
        k = 10 * k
        cons = self._consumers
        if not isinstance(cons, dict):          # pre-hotswap list shape - reset
            self._consumers = {}
            return
        self._consumers = {ds: seen for ds, seen in cons.items()
                           if not self._consumer_closed(ds)
                           and (now - max(seen, getattr(ds, 'last_seen', None) or 0)) <= k}

    @classmethod
    def sweep(cls):
        """Deregister evictable hosts with no active consumer from Melty.render_hosts
        so draw_main stops drawing them each frame. The host and its parse stay in the
        code-host cache (NOT evicted) — reopening the view re-registers it via
        notify_on_change → register. Toggle-gated; runs once per frame from end_frame."""
        from src.lsd.gl_gui.toggles import Toggles
        if not Toggles.HostLifecycle.deregister_idle:
            return
        k = int(Toggles.HostLifecycle.idle_frames)
        now = Melty.frame_count
        hosts = list(Melty.render_hosts.values())

        # First pass: direct liveness. Non-evictable singletons are always alive;
        # a freshly-born host is held through a birth grace (its consumer may not
        # have drawn yet - Mode.WINDOW bodies render deferred).
        alive = {}
        for h in hosts:
            if not getattr(h, "evictable", False):
                alive[id(h)] = True
                continue
            if now - getattr(h, "_birth_frame", 0) <= k:
                alive[id(h)] = True
                continue
            h._prune_consumers(now, k)
            alive[id(h)] = bool(h._consumers)

        # Second pass: a host referenced as a live host's input_value stays alive.
        # The str_host behind a dict_host has no consumers of its own, so it would
        # otherwise be swept out from under the dict_host that still needs it.
        changed = True
        while changed:
            changed = False
            for h in hosts:
                if not alive.get(id(h)):
                    continue
                iv = getattr(h, "input_value", None)
                if isinstance(iv, RenderHost) and iv is not h and not alive.get(id(iv)):
                    alive[id(iv)] = True
                    changed = True

        for h in hosts:
            if getattr(h, "evictable", False) and not alive.get(id(h)):
                h.remove()      # pop from render_hosts but KEEP the cache entry

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
        self._local_edit_time = time.monotonic()     # re-arms the consumer-notify debounce
        # Debug timeline: who dirtied this host (a symbol-attach bubbling into the
        # held gp would show up here as _distribute_by_name / _post_symbol_attach).
        _ptrace_rl(("host-dirty", self.name), f"host DIRTY {self.name} <- {self._caller_trail(frames=5)}",
                   min_interval=0.05)
        # Invalidate BOTH the window envelope AND the wrapper's own draw_state: the
        # envelope so render_host_view re-runs and calls the wrapper, and the wrapper
        # so its blit-cached body actually re-executes to process the edit (load/save
        # or chain_out). Invalidating only the envelope leaves the wrapper replayed.
        for ds in (None, self._wrapper_draw_state):
            if ds is not None:
                try:
                    pass
                    # ds.invalidate(frame_delta=0)
                    # ds.invalidate(frame_delta=1)
                    note = Note(name="RenderHost _mark_changed", tint=(1, 0.5, 0), draw_state=ds)
                    ds.invalidate(note=note)
                    notify("invalidate #1", tag="host", tint=(1,0,1))

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

        # convert_in_and_out_value tags each finished chain_in with the GENERATION (the
        # origin local-edit frame) of the source it parsed - the echo of our own edit
        # carries that edit's frame, a genuine external change carries "now". Use it as
        # the frame-precedence baseline below (not the frame the parse happens to LAND),
        # so a re-parse of an OLDER edit lands strictly before a newer local edit and
        # can't clobber it - that race is the value-flicker. None on non-finish frames and
        # for the code_file_io path (which doesn't pass it). Pop so it never reaches the
        # renderer's kwargs.
        inbound_gen = kwargs.pop("inbound_gen", None)
        if inbound_gen is not None:
            self._input_change_frame = inbound_gen

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

        # A fresh DERIVED-result pulse must reach external consumers even when
        # the VALUE didn't change: a FAILED parse keeps last_good held (so
        # _materialize never runs) and parks the error in the wrapper's
        # ModesState - consumers that read that error (the folder-tree editor's
        # red-line highlight, via draw_text_from_code_cache) would otherwise
        # never re-run their cached subtrees to show or clear it.
        if external_change and self._consumers:
            self._notify_consumers(name="RenderHost result pulse")

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
            # Keep the inbound generation when convert provided one (above); only the
            # code_file_io path (inbound_gen=None) falls back to "now".
            if inbound_gen is None:
                self._input_change_frame = Melty.frame_count
            self._awaiting_inbound = True


            # The file changed on disk - the wrapper/blit cache won't know to re-render
            # the reloaded value, so invalidate this proxy's view explicitly (same path
            # as the initial fill below).
            if self._draw_state is not None and self._draw_state._parent is not None:
                self._draw_state._parent.invalidate_up_by_obj(
                    obj=self, note=Note(name="file_reload", tint=(1, 0.6, 0.1)), max_depth=3)
            request_render()

        # FRAME PRECEDENCE (all O(1) — no content comparison). Is there a GENUINE pending
        # local edit, newer than the source this inbound represents? `_input_change_frame`
        # is the source's ORIGIN generation: for convert it's the edit-frame the parse
        # round-trips from (tagged by convert_in_and_out_value, NOT the frame the parse
        # landed - that distinction is what stops the flicker), and for code_file_io it's
        # the frame its external source changed. Rendering the held value writes
        # reconstructed children back (draw_collection) a frame later - a benign 1-frame
        # "edit" echo - so a diff of ≤1 is NOT genuine; only a local edit MORE than 1
        # frame ahead of what the inbound represents counts, and it WINS (inbound rejected).
        local_ahead = self._local_edit_frame > self._input_change_frame + 1

        if input_value is not None and input_value is not self:
            if self.value_key not in self:
                self._materialize(input_value)
                note = Note(name="_materialize", tint=(1, 1.0, 1.0))
                # Needed for initial load
                self._draw_state._parent.invalidate_up_by_obj(obj=self, note=note, max_depth=3)
                self._draw_state._parent.invalidate_up(note=note, max_depth=7)

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

        if self.value_key not in self:
            # Nothing held yet (pre-materialize): the renderer draws a blank
            # where the content will be - stamp the placeholder frame so the
            # wrapper commit doesn't restore persisted content heights over it
            # (see Melty.pending_placeholder_frame).
            Melty.pending_placeholder_frame = Melty.frame_count
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
            self._wrapper_draw_state._parent.invalidate()
            self._draw_state._parent.invalidate_by_obj(obj=input_value)
            self._draw_state._parent.invalidate_by_obj(obj=self)
            notify("invalidate #5", tag="host", tint=(1, 0, 1))

            request_render()

        _ext = self._external_change
        edited = bool((pre_dirty and local_ahead) or _ext or r_changed)
        self._external_change = False
        if edited:
            # Change timeline: WHY this host reports an edit to its wrapper - for the
            # convert (dict) host this is exactly what starts a chain_out.
            _ptrace(f"host OUTBOUND edited {self.name}",
                    pre_dirty=pre_dirty, local_ahead=local_ahead,
                    ext=_ext, r_changed=r_changed)
            # Needed
            note = Note(name="Render host, nested view edited", tint=(1, 1, 1))
            # self._draw_state._parent.invalidate_by_obj(obj=input_value, note=note)
            # notify("invalidate #6", tag="host", tint=(1, 0, 1))

            request_render()
            note = Note(name="Render host, self", tint=(1, 1, 1))
            # self._draw_state._parent.invalidate_by_obj(obj=self, note=note)
            # notify("invalidate #7", tag="host", tint=(1, 0, 1))
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
        # The held value just CHANGED - invalidate any external consumer subtrees so they
        # re-run and pick it up (the background-parse-lands-into-a-cached-context-menu
        # case).
        self._notify_consumers(name="RenderHost value materialized")
        request_render()

    def _notify_consumers(self, name="RenderHost value changed"):
        """Invalidate external consumer subtrees (notify_on_change) — DEBOUNCED
        against active typing. While the held value is being locally edited
        (a keystroke stream — each edit stamps _local_edit_time), every finished
        background reconvert would pulse here and invalidate EVERY consumer,
        including a second editor window over the same file, per keystroke —
        large-file re-renders that stall typing. Instead the notify is deferred
        (trailing, re-armed per edit) and fires ONCE, a quiet interval after the
        last local edit. Notifies with no recent local edit (external reload,
        initial load) pass through immediately. The typing editor itself doesn't
        need the pulse — its keystrokes render through the focused editor
        directly."""
        from src.lsd.gl_gui.toggles import Toggles
        window = Toggles.HostLifecycle.consumer_notify_debounce_ms / 1000.0
        remaining = self._local_edit_time + window - time.monotonic()
        if remaining > 0:
            self._pending_notify_name = name
            self._arm_notify_timer(remaining)
            return
        self._notify_consumers_now(name)

    def _arm_notify_timer(self, delay):
        prev = self._notify_timer
        if prev is not None:
            prev.cancel()
        t = threading.Timer(delay, self._flush_pending_notify)
        t.daemon = True
        self._notify_timer = t
        t.start()

    def _flush_pending_notify(self):
        """Timer body: fire the deferred consumer notify — unless another local
        edit landed while waiting, in which case re-arm for the remainder (the
        trailing-debounce re-arm for edits that produced no new pulse)."""
        self._notify_timer = None
        name = self._pending_notify_name
        if name is None:
            return
        from src.lsd.gl_gui.toggles import Toggles
        window = Toggles.HostLifecycle.consumer_notify_debounce_ms / 1000.0
        remaining = self._local_edit_time + window - time.monotonic()
        if remaining > 0:
            self._arm_notify_timer(remaining)
            return
        self._pending_notify_name = None
        self._notify_consumers_now(f"{name} (debounced)")

    def _notify_consumers_now(self, name):
        """The actual invalidation fan-out: they read this host's value/error from
        OUTSIDE its own draw loop, so nothing else re-runs them. Climb their
        ancestors (invalidate_up) since the consumer is usually a nested cached
        view that won't re-run unless its parents do."""
        # Snapshot: this can run on a background worker (via _materialize) while
        # the render thread rebuilds _consumers in notify_on_change / the sweep -
        # a live dict would raise "changed size during iteration".
        # max_depth: the consumer renders THIS host's data, so a new value must
        # invalidate its whole cached subtree - the blit cache has no data
        # key, children replay purely on tile dirtiness, and a depth-2 cascade
        # left every deeper view compositing its old capture (the window
        # re-drew, flickered, and still showed stale data until a hover
        # invalidated the subtree for real). Depth 8 covers the deepest
        # consumer trees; the cascade's clip gate already skips scrolled-out
        # children (invalidate_scrolled_in catches those later), and this
        # runs once per materialized value, not per frame.
        for cds in list(self._consumers):
            tid = getattr(cds, "_tile_id", None)
            if tid is not None:
                Melty.cache.invalidate_up(tid, force=True, max_depth=8,
                                          note=Note(name=name,
                                                    tint=(0.4, 1.0, 0.6), draw_state=cds))
            #     notify("invalidate #8", tag="host", tint=(1, 0, 1))

        if self._consumers:
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
    def draw_needed(self):
        """Event-driven pump gate for draw_main's host loop: False when this
        is a hidden cache host with nothing to do this frame, so keeping
        dozens of code-cache pairs REGISTERED costs ~nothing (each polled
        draw is ~0.1ms of pure wrapper/envelope overhead). All real work is
        already invalidation-driven — the io bodies are use_cache=True, so on
        a clean frame they blit-replay and do nothing — which makes "has
        work" exactly:
          - a flagged edit (_external_change / _pending_external),
          - the upstream value changed identity (what draw() polls for),
          - never drawn yet (initial load pending),
          - the envelope/wrapper tile is invalidated (any ds.invalidate —
            _mark_changed, run_in_background completions, consumer notifies),
          - the slow staggered heartbeat (1-in-30): the safety net for
            signals that don't touch tiles (debounce timers, missed edges) —
            a missed wake costs ≤~250ms on pipelines already debounced by
            hundreds of ms, and the save channel can never wedge.
        Visible hosts (real windows / non-## names / non-evictable) always
        draw — they are on screen every frame by definition."""
        # A file-watch dispatch flagged the wrapper ds (external file change).
        # The flag alone just bypasses the wrapper's OWN cache gate - but if
        # the envelope above it is blit-cached, the replay never descends to
        # the wrapper, the gate is never consulted, and the flag sits
        # unconsumed forever (the io body never reloads). Invalidate this
        # host's parent tile chain so the walk actually reaches the wrapper.
        # Cheap and self-limiting: one host's tiles, and only until the body
        # runs and clears the flag.
        wds0 = self._wrapper_draw_state
        if wds0 is not None and getattr(wds0, "_external_change", False):
            if Melty.cache is not None and wds0._tile_id is not None:
                Melty.cache.invalidate_up(wds0._tile_id, force=True, max_depth=4)
            return True
        if self.window or not getattr(self, "evictable", False) \
                or not self.name.startswith("##"):
            return True
        if self.hidden:
            return False
        if self._external_change or self._pending_external:
            return True
        iv = self.input_value
        if isinstance(iv, RenderHost):
            iv = iv._held()
        if iv is not self._last_resolved:
            return True
        env_ds = getattr(self, "_draw_state", None)
        wds = self._wrapper_draw_state
        if env_ds is None or wds is None:
            return True
        cache = Melty.cache
        if cache is not None:
            for ds in (env_ds, wds):
                if cache._is_dirty(cache._tiles.get(getattr(ds, "_tile_id", None))):
                    return True
        return (Melty.frame_count + (id(self) >> 4)) % 30 == 0

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

        draw=False
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
                    # ds.invalidate(note=note)
                    # ds.invalidate_by_obj(obj=self, note=note)
                    notify("invalidate #9", tag="host", tint=(1, 0, 1))
                    draw=True

            request_render()

        win_kwargs = {"name": self.name}
        if self.window:
            win_kwargs.setdefault("mode", Mode.HOST_WINDOW)
            win_kwargs["active_layer"] = 1
            win_kwargs["unmanaged"] = True
            win_kwargs["closed"] = False
            win_kwargs['height'] = 40
            win_kwargs['width'] = 40
            win_kwargs["window_pos"] = (-38, 100)

        win_kwargs.update(extra)

        RenderHost._active.append(self)
        try:
            win_kwargs['tint'] = (0.3, 0, 0.7, 0.1)
            # win_kwargs['height'] = 40

            result = render_host_view(iv, draw=draw, **win_kwargs)
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
def render_host_view(input_value, external_change=False, draw=False, draw_state=None, name=None, **kwargs):
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
    host.child_kwargs['temp'] = True
    notify(f"{host.io_function.__name__}", tag="host", tint=(1, 0.5, 0.5))
    host.child_kwargs["input_value"] = input_value
    result = host.io_function(draw=draw, view_func=host._internal_view_func,
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
        # A PROGRAMMATIC edit just serialized (chain_out from a param panel /
        # lens / live-view window - typing never takes this branch: keystrokes
        # enter the str host directly and don't chain_out). The code editor
        # over this file is also consumer of this dict host (notify_on_change,
        # draw_text_from_code_cache), but the regular consumer notify is
        # trailing-debounced ~2s against typing and re-armed by the
        # _mark_changed - under quick successive edits it starves and the
        # editor keeps showing the pre-edit cache. Notify NOW: one fan-out per
        # completed edit round, the editors re-run and read the fresh held
        # string.
        host._notify_consumers_now("programmatic edit write-back")

    return edited, out
