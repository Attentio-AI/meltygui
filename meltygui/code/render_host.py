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
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.bubbling import install_bubbling, _reinstall_children, is_unchanged
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


_UNSET = object()


@defaults()
class RenderHost(dict):
    """A dict that drives a stateful `wrapper` and holds the value it edits.

    Args:
      wrapper:      the stateful func to drive (code_file_io / convert_in_and_out_value).
                    `None` → a plain data-bag: `draw()` just renders the dict.
      input_value:  what the wrapper operates on — a raw value (a class, a path), or
                    an upstream `RenderHost` (resolved to its held value each frame).
      child_kwargs: extra kwargs for the wrapper (e.g. convert's chain_in/out + route).
      renderer:     how to draw the held value in the wrapper's view slot
                    (default: `draw_any`, which routes str → editor, dict → collection).
      value_key:    the dict key the held value lives under (type intact).
      standalone:   register with Melty so draw_main calls `draw()` (its own window)."""

    # The host currently rendering, so render_host_view can find it without threading
    # it through cache-hashed kwargs.
    _active = []

    # Log every change the proxy triggers (dict-dirty + outbound edits) with a caller
    # trail - for diagnosing spurious saves. `RenderHost.debug_changes = False` silences.
    debug_changes = True

    def __init__(self, wrapper=None, *args, input_value=None, child_kwargs=None,
                 renderer=None, name=None, hidden=False, window=True, standalone=True,
                 value_key="value", **extra):
        super().__init__(*args)
        self.wrapper = wrapper
        # `is None` (not falsy): an empty host-dict is a valid input_value.
        self.input_value = self if input_value is None else input_value
        # Wrapper options (chain_in/out, route, ...). `extra` kwargs fold in too, so you
        # can pass them directly instead of nesting a child_kwargs dict.
        self.child_kwargs = {**(child_kwargs or {}), **extra}
        self.renderer = renderer
        self.name = name or getattr(wrapper, "__name__", None) or "RenderHost"
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
            request_render()
        return self

    def remove(self):
        Melty.render_hosts.pop(id(self), None)
        self._registered = False
        request_render()
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
        # Invalidate BOTH the window envelope AND the wrapper's own draw_state: the
        # envelope so render_host_view re-runs and calls the wrapper, and the wrapper
        # so its blit-cached body actually re-executes to process the edit (load/save
        # or chain_out). Invalidating only the envelope leaves the wrapper replayed.
        for ds in (self._draw_state, self._wrapper_draw_state):
            if ds is not None:
                try:
                    ds.invalidate()
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

    # Every value the host stores is bubbling-upgraded so nested mutations bubble back.
    def __setitem__(self, key, value):
        # Re-assigning an equal value isn't an edit (draw_collection writes its
        # rendered value back each frame) - store it but don't mark dirty / redraw.
        unchanged = key in self and is_unchanged(dict.__getitem__(self, key), value)
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

        if not pre_dirty and input_value is not None and input_value is not self:
            self._materialize(input_value)

        from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
        renderer = self.renderer or draw_any
        render_kwargs = {k: v for k, v in kwargs.items() if k != "routed"}
        render_kwargs.setdefault("name", f"{self.name}##held")
        result = renderer(self._held(), **render_kwargs)
        r_changed, r_new = (result[0], result[1]) if isinstance(result, tuple) and len(result) >= 2 else (False, result)
        # A string edit (immutable) comes back via the return; store it. A dict/tree
        # edit mutated in place and already set _external_change via bubbling.
        if r_changed and self.get(self.value_key) is not r_new:
            self[self.value_key] = r_new

        edited = bool(pre_dirty or self._external_change or r_changed)
        self._external_change = False
        if edited:
            self._log_change("OUTBOUND → return (True, value)",
                             pre_dirty=pre_dirty, r_changed=r_changed, external_change=external_change)
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
        source string) onto this upstream proxy. Goes through the public __setitem__
        so it bubbles + marks dirty → this proxy surfaces it to ITS wrapper next frame
        (string_proxy → code_file_io saves). Identity-guarded so an unchanged push
        doesn't spuriously dirty."""
        if self.value_key is not None and self.get(self.value_key) is not value:
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

        win_kwargs = {"name": self.name}
        if self.window:
            win_kwargs.setdefault("mode", Mode.WINDOW)
        win_kwargs.update(extra)

        RenderHost._active.append(self)
        try:
            result = render_host_view(iv, **win_kwargs)
        finally:
            RenderHost._active.pop()
        self._last_return = result if isinstance(result, tuple) else (False, result)
        return self._last_return[1]

    def __repr__(self):
        w = getattr(self.wrapper, "__name__", self.wrapper)
        flags = [f for f in ("hidden" if self.hidden else "", "dirty" if self._external_change else "",
                             "" if self.standalone else "embedded") if f]
        tail = f" [{', '.join(flags)}]" if flags else ""
        return f"RenderHost({self.name!r} -> {w}{tail}, {dict.__repr__(self)})"


@render_func(use_cache=True, selectable=False)
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
    if host.wrapper is None:
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_collection
        return draw_collection(host, name=host.name)

    ext = bool(external_change or (input_value is not host._last_resolved))
    host._last_resolved = input_value

    # return_extras=True → the wrapper hands back its OWN draw_state as a 3rd value;
    # store it so a later value edit can invalidate it (see _mark_dirty) and the
    # wrapper fully re-runs instead of replaying its blit cache.
    result = host.wrapper(input_value=input_value, view_func=host._internal_view_func,
                          external_change=ext, return_extras=True, **host.child_kwargs)
    if isinstance(result, tuple) and len(result) == 3:
        edited, out, host._wrapper_draw_state = result
    else:
        edited, out = result

    # Cross-proxy write-back: the wrapper's output (e.g. convert's new source string)
    # lands on the upstream proxy, which goes dirty and saves one frame later.
    if edited and isinstance(host.input_value, RenderHost):
        host.input_value._set_held(out)
    return edited, out
