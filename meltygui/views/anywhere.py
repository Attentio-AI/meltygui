"""The set/get-anywhere API — read and write a view parameter at whichever
input source is actually DRIVING it (signature default, caller kwarg, mode
entry, @defaults decoration, class var, override comment, instance attr...).

`SourcePriority` is the hardcoded ranking of those sources; `_driving_source`
is the single pick every entry point shares, so `get_source_for`,
`from_anywhere`, `anywhere_value` and `set_anywhere` can never disagree.
The source registry itself is collected by `collect_input_sources`
(new_core_view) — imported lazily inside `_sources_for` to keep this module
free of an import cycle with the view code that calls it.
"""

from enum import Enum

from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core

# We want to use the order of the tab elements as a way of determining the
# source of the input. From the function's perspective it just has parameters
# injected. Melty is routing data from many different sources and does its
# best to pick sane defaults when there are multiple sources. Which source
# takes priority depends on how the code is written, and the code may change.
# Rather than trying to infer the priority, we're just hard coding it here -
# adjust the order of the elements to reflect the runtime behavior of melty.
# (Below is a rough memory of which sources take priority; reorder freely.)
class SourcePriority(Enum):
    LIVE_COMMENT = 0             # the `# [tint=...]` override comment - the
                                 # wrapper SPLATS it over kwargs after every
                                 # other kwargs merges, so at runtime it
                                 # beats @defaults, callers, and codecs alike
    MODE = 1
    RENDER_FUNC = 2              # signature defaults (def draw_x(speed=3))
    WINDOW_DECORATION = 3        # @window(...) on a class or class - outranks
                                 # @defaults (the window kwargs drive the
                                 # window that renders the value)
    CALLER = 4                   # call-site kwargs are EXPLICITLY passed, so in
                                 # the wrapper's gauntlet they beat EVERY
                                 # injected default layer, @defaults included
                                 # (verified live: draw_code_tabs_and_cache's
                                 # child_kwargs= wins over the GeneralParse
                                 # class @defaults). DEPTH is the natural
                                 # tiebreaker (see _source_priority) - no
                                 # CALLER_0/CALLER_1 members needed
    MODE_CHILD_KWARGS = 5        # child_kwargs={...} inside the PARENT's mode
                                 # entry (Modes.NEW_CODE). Same merge into the
                                 # child call as CHILD_KWARGS below. but on the
                                 # parent it's the MODE setting child_kwargs -
                                 # and mode outranks the parent's caller and
                                 # @defaults - so it wins the dict whenever the
                                 # mode entry carries one
    CHILD_KWARGS = 6             # the PARENT view's child_kwargs={...} - an
                                 # explicit call kwarg on the child, so
                                 # caller-strength: above @defaults for the
                                 # same reason as CALLER; the dict itself
                                 # lives at any of the parent's OWN sources,
                                 # resolved via from_anywhere("child_kwargs",
                                 # parent)
    AT_DEFAULT_CODE_TYPE = 7     # @defaults on a PARSED class in the value tree
    AT_DEFAULT_OBJ_TYPE = 8      # @defaults on the value's runtime class
    DECORATION = 9               # @render_func(...) kwargs on the view func
    INSTANCE_ATTR = 10           # whitelisted live attr on the value object
                                 # (core_render.OBJ_ATTR_PARAMS, e.g.
                                 # Lora.tint) - injected via setdefault, so
                                 # every kwargs-borne source above wins
    CLASS_VAR = 11               # class-level assignment on the value's class -
                                 # drives the view through the SAME getattr
                                 # injection as INSTANCE_ATTR, where the
                                 # instance var shadows it (Python lookup
                                 # order), so it ranks below the instance
    CODEC = 12                   # the active codec's render_kwargs - the
                                 # wrapper's lowest kwargs MERGE layer
                                 # (core_render `render_kwargs | ...`);
                                 # loses to every getattr-injected source
                                 # above, beats only the ds fallback
    DRAW_STATE = 13              # the draw_state's own attrs (ds.tint - the
                                 # style cascade's lowest fallback, persisted
                                 # with window state) - the default: drives
                                 # it when nothing else sets the param


# The tab's kind captions → priority. Kinds are the single naming source
# (collect_input_sources stamps them); this is just the lookup.
_KIND_TO_PRIORITY = {
    "mode": SourcePriority.MODE,
    "signature": SourcePriority.RENDER_FUNC,
    "code class default": SourcePriority.AT_DEFAULT_CODE_TYPE,
    "class default": SourcePriority.AT_DEFAULT_OBJ_TYPE,
    "code comment": SourcePriority.LIVE_COMMENT,
    "class var": SourcePriority.CLASS_VAR,
    "decoration": SourcePriority.DECORATION,
    "window decoration": SourcePriority.WINDOW_DECORATION,   # @window on the func
    "class decoration": SourcePriority.WINDOW_DECORATION,    # @window on the class
    "instance attr": SourcePriority.INSTANCE_ATTR,
    "attr default": SourcePriority.AT_DEFAULT_OBJ_TYPE,  # @defaults(attr="x", ...)
    "child kwargs": SourcePriority.CHILD_KWARGS,
    "mode child kwargs": SourcePriority.MODE_CHILD_KWARGS,
    "codec": SourcePriority.CODEC,
    "draw state": SourcePriority.DRAW_STATE,
}


def _source_priority(kind):
    """Sort key for a source's kind caption: (SourcePriority value, depth).
    Caller rows ("caller", "caller +1", ...) share one enum member with their
    walk depth as the tiebreaker, so priority never hardcodes caller depth."""
    if isinstance(kind, str) and kind.startswith("caller"):
        depth = int(kind.split("+", 1)[1]) if "+" in kind else 0
        return (SourcePriority.CALLER.value, depth)
    p = _KIND_TO_PRIORITY.get(kind)
    return (p.value, 0) if p is not None else (len(SourcePriority) + 1, 0)


def _sources_for(draw_state, class_to_show=None):
    """collect_input_sources for a TARGET draw_state outside the context menu:
    the host-caching ContextMenuState rides on the draw_state so repeat calls
    (a drag writing per-release, say) reuse the parsed hosts. class_to_show
    defaults to the value's runtime class, so the @defaults/class-var rows
    resolve the same way here as under the context menu."""
    from src.lsd.gl_gui.view.core_views.new_core_view import (
        ContextMenuState, collect_input_sources)
    if class_to_show is None:
        raw = draw_state._raw_input_value
        # A view can SHOW a class itself (a @window class like Toggles):
        # its class rows (@window(cls) / class vars / @defaults) anchor on
        # THAT class - `raw.__class__` would hand them `type`.
        class_to_show = raw if isinstance(raw, type) else getattr(raw, "__class__", None)
    cm_state = getattr(draw_state, "_sa_cm_state", None)
    if cm_state is None:
        cm_state = ContextMenuState()
        draw_state._sa_cm_state = cm_state
    srcs = collect_input_sources(draw_state, cm_state, class_to_show)
    # Keep-alive + revive: code hosts are EVICTABLE - the idle sweep pops them
    # off the draw_state once their consumers close (RenderHost.sweep). A
    # swept host still passes writes: the tree stays dirty but nothing runs
    # its chain_out, so the write silently never fires (set_anywhere writing
    # into a dirty orphan was the first field failure). notify_on_change is
    # the sanctioned per-frame pulse: stamps liveness AND re-registers a
    # swept host that the upstream straining. Same thing the input
    # tab does at its end with its own draw_state.
    for _h in (cm_state.render_func_dict, cm_state.class_dict,
               cm_state.mode_dict,
               *[dh for (_sh, dh) in (cm_state.call_site_hosts or [])]):
        if _h is not None:
            _h.notify_on_change(draw_state)
    return srcs


_UNSET = object()


# Source layers that OUTRANK the draw_state/auto-state layer in the wrapper's
# kwargs gauntlet: their-set values (mode overrides, caller kwargs,
# child_kwargs, the comment splat, @window decoration kwargs). A write to a
# param driven by one of these must edit the source or an edit would be
# shadowed at runtime. Everything below (signature defaults, @render_func
# kwargs, @defaults, class vars, instance attrs, codec render_kwargs) merges
# BENEATH auto-state - a plain draw_state write both takes effect immediately
# and persists (auto_params), so it is the default write target.
_ABOVE_DRAW_STATE = {
    SourcePriority.LIVE_COMMENT.value, SourcePriority.MODE.value,
    SourcePriority.WINDOW_DECORATION.value, SourcePriority.CALLER.value,
    SourcePriority.MODE_CHILD_KWARGS.value, SourcePriority.CHILD_KWARGS.value,
}


# ── source speed: fast vs slow writers ──────────────────────────────────────
# A source's SPEED is how a write becomes LIVE. Fast sources apply in place -
# a draw_state field, a live instance attr, the codec's in-memory
# render_kwargs - one setattr/item-set and the next frame reads it. Slow
# sources are code-backed: the write lands in a parse dict and only becomes
# live through the debounced chain_out → save + recompile/hotswap trip. That
# trip is fine per click, but a DRAG writes every frame, and one trip per
# frame craters the frame rate (120 to ~30fps measured on the voxel camera
# driving a `# [cam_brightness=...]` comment). So while a left/right/middle
# mouse button is held - or a scroll is in flight (no release event, so
# "recent tick within the same interval") - slow writes DEFER: the value
# lands in the in-flight display cache (_sa_pending - anywhere_value/
# locate_* reads already prefer it) plus a per-ds deferred dict, and the
# real set_anywhere runs ONCE, when input goes quiet, via
# flush_deferred_writes (called per frame by draw_main).
_FAST_PRIORITIES = {
    SourcePriority.DRAW_STATE.value, SourcePriority.INSTANCE_ATTR.value,
    SourcePriority.CODEC.value,
}


def source_is_slow(kind):
    """True when a write to a source of this kind round-trips through
    save/recompile rather than applying in place."""
    return _source_priority(kind)[0] not in _FAST_PRIORITIES


# ── source precision: low-precision writers ─────────────────────────────────
# Some sources store floats as literal TEXT in code, where full float64
# precision is noise (`# [cam_x=0.30000000000000004]`). Kinds marked here get
# their float writes rounded to LOW_PRECISION_DECIMALS before landing in the
# source; the FULL-precision value parks on the draw_state's _sa_precise
# overlay, which anywhere_value serves for as long as the live value is still
# a rounding of it - the view sees the precise float (a voxel camera is
# accumulating sub-4dp drag deltas) while the code keeps a readable one. A
# write that ONLY moves digits beyond the cap skips the save/recompile trip
# entirely. The overlay is in-memory: an app reload sees the rounded source
# value (accepted trade-off). Mark a new source by adding its kind here.
LOW_PRECISION_DECIMALS = 4
_LOW_PRECISION_KINDS = {"code comment", "class var"}


def source_is_low_precision(kind):
    """True when float writes to a source of this kind are rounded to
    LOW_PRECISION_DECIMALS decimal places (see the block comment above)."""
    return kind in _LOW_PRECISION_KINDS


def _round_low_precision(value):
    """`value` rounded to LOW_PRECISION_DECIMALS — elementwise for float
    sequences; anything without a float passes through untouched."""
    if isinstance(value, float):
        return round(value, LOW_PRECISION_DECIMALS)
    if (isinstance(value, (tuple, list))
            and any(isinstance(v, float) for v in value)):
        seq = tuple if isinstance(value, tuple) else list
        return seq(_round_low_precision(v) for v in value)
    return value


def _is_rounding_of(live, full):
    """True when `live` is `full` rounded at LOW_PRECISION_DECIMALS or
    coarser — i.e. the source still holds OUR write (possibly re-capped by
    the source's float formatter), not an external edit."""
    try:
        if live is full or bool(live == full):
            return True
    except Exception:
        return False
    if (isinstance(full, float) and isinstance(live, (int, float))
            and not isinstance(live, bool)):
        return any(live == round(full, dp)
                   for dp in range(LOW_PRECISION_DECIMALS + 1))
    if (isinstance(full, (tuple, list)) and isinstance(live, (tuple, list))
            and len(full) == len(live)):
        return all(_is_rounding_of(lv, fv) for lv, fv in zip(live, full))
    return False


def _stamp_precise(attr_name, value, draw_state):
    """Park the full-precision value a low-precision source write rounded
    away; anywhere_value serves it over the rounded live value."""
    precise = getattr(draw_state, "_sa_precise", None)
    if precise is None:
        precise = {}
        draw_state._sa_precise = precise
    precise[attr_name] = value


def _precise_or_live(attr_name, draw_state, live):
    """The full-precision overlay for `attr_name` while the live value is
    still a rounding of it; once the source moves elsewhere (an external
    edit, another driver) the overlay drops and live reads resume."""
    precise = getattr(draw_state, "_sa_precise", None)
    if precise is None or attr_name not in precise:
        return live
    full = precise[attr_name]
    if _is_rounding_of(live, full):
        return full
    del precise[attr_name]
    return live


_DRAG_BUTTONS = ("left_mouse", "right_mouse", "middle_mouse")


def _drag_active():
    """A mouse button is currently held — the window during which slow-source
    writes defer. Level state from the input handler (not per-frame events),
    so it can't miss between drag events."""
    h = getattr(Core.melty, "event_handler", None)
    return h is not None and any(h.is_down(b) for b in _DRAG_BUTTONS)


# Scroll has no release: a gesture is "over" once no tick has arrived for the
# quiet window. Wheel notches land ~100ms+ apart, so the window must span the
# inter-tick gap or every notch would flush its own save/recompile trip.
_SCROLL_QUIET_FRAMES = 24
# Frame of the last seen scroll event - module registry, survives hotswap.
_last_scroll_frame = globals().get("_last_scroll_frame", [-1_000_000])


def _note_scroll():
    """Stamp the frame when a scroll event is in flight. events_by_type is
    drained at frame start (begin_frame), so the flush call at the top of
    draw_main sees this frame's ticks before any view's handler writes."""
    if "scroll_y_changed" in (Core.melty.events_by_type or {}):
        _last_scroll_frame[0] = Core.melty.frame_count


def _scroll_recent():
    return Core.melty.frame_count - _last_scroll_frame[0] < _SCROLL_QUIET_FRAMES


def _input_busy():
    """True while a gesture that should hold off slow writes is in flight —
    a held drag button, or a scroll within its quiet window."""
    return _drag_active() or _scroll_recent()


# Draw_states holding deferred writes, flushed on release. Module registry -
# survives hotswap (re-exec reuses the existing global).
_DEFERRED_DS = globals().get("_DEFERRED_DS", set())


def _stamp_pending(attr_name, value, draw_state):
    """In-flight display cache (see anywhere_value): remember what was set and
    what the live value was when the set was issued. Re-sets during a drag
    refresh the UI value but KEEP the original live_at_set — live hasn't moved
    yet, and that's the baseline whose change means "the trip landed"."""
    live = (draw_state._kwargs or {}).get(attr_name, _UNSET)
    pending = getattr(draw_state, "_sa_pending", None)
    if pending is None:
        pending = {}
        draw_state._sa_pending = pending
    prior = pending.get(attr_name)
    # _UNSET normalizes to None: anywhere_value reads live with a None
    # default, and the baselines must compare equal until the trip lands.
    live_at_set = prior[1] if prior is not None else (
        None if live is _UNSET else live)
    pending[attr_name] = (value, live_at_set)
    getattr(draw_state, "_sa_verify", {}).pop(attr_name, None)


def _defer_write(attr_name, value, draw_state, class_to_show):
    """Park a slow-source write for the duration of the drag: the display
    cache serves reads immediately; flush_deferred_writes runs the real
    set_anywhere on release."""
    deferred = getattr(draw_state, "_sa_deferred", None)
    if deferred is None:
        deferred = {}
        draw_state._sa_deferred = deferred
    deferred[attr_name] = (value, class_to_show)
    _DEFERRED_DS.add(draw_state)
    _stamp_pending(attr_name, value, draw_state)


def flush_deferred_writes():
    """Per-frame (draw_main): once no drag button is held, run each parked
    write through the normal set_anywhere — one slow trip per gesture, not
    one per event. A no-op set-check when nothing is parked."""
    _note_scroll()
    if not _DEFERRED_DS:
        return
    if _input_busy():
        # Rendering is event-driven: after the last scroll tick no further
        # input arrives, so keep frames flowing until the quiet window expires
        # and the deferred writes actually flush.
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
        return
    for ds in list(_DEFERRED_DS):
        _DEFERRED_DS.discard(ds)
        deferred = getattr(ds, "_sa_deferred", None) or {}
        items = list(deferred.items())
        deferred.clear()
        for attr_name, (value, class_to_show) in items:
            set_anywhere(attr_name, value, ds, class_to_show=class_to_show,
                         allow_any=True, ds_fallback=True)


def _pref_matches(pref, kind):
    """True when a source row's kind caption satisfies a preferred_source
    flag — given either as a kind caption string ("code comment") or a
    SourcePriority member (matched through the same priority table)."""
    if isinstance(pref, SourcePriority):
        return _source_priority(kind)[0] == pref.value
    return kind == pref


def _preferred_source_for(draw_state):
    """The `preferred_source` flag riding this view's resolved kwargs, or an
    enclosing window's — a spawning view (live view) stamps it on the value
    windows it opens, and edits from anywhere in that subtree (params panel
    rows, context menus) should land at the nominated source. Walks _parent
    (self-loop root) then hops parent_window, same as other ancestor walks."""
    node, hops = draw_state, 0
    while node is not None and hops < 32:
        pref = (getattr(node, "_kwargs", None) or {}).get("preferred_source")
        if pref is not None:
            return pref
        parent = getattr(node, "_parent", None)
        nxt = parent if parent is not None and parent is not node else None
        if nxt is None:
            pw = getattr(node, "parent_window", None)
            nxt = pw if pw is not None and pw is not node else None
        node = nxt
        hops += 1
    return None


def get_value_for_source(attr_name, input_source, draw_state, class_to_show=None):
    """(input_source, value) for `attr_name` as set by a specific
    SourcePriority source on this view, or (input_source, None) when that
    source doesn't set it. The same registry the input tab shows."""
    srcs = _sources_for(draw_state, class_to_show)
    for sname, sdict in srcs["sources"].items():
        kind = srcs["kinds"].get(sname)
        if _source_priority(kind)[0] == input_source.value and attr_name in sdict:
            return input_source, sdict[attr_name]
    return input_source, None


def _unset_value(v):
    """True when a stored value can't DRIVE a param: None (declared-unset) or
    a fully transparent color (an alpha-0 4-tuple — the codec opt-out
    convention; it renders nothing, so it must not claim the pick)."""
    if v is None:
        return True
    return (isinstance(v, (tuple, list)) and len(v) >= 4
            and isinstance(v[3], (int, float)) and not v[3])


def _setting_source(srcs, attr_name):
    """The highest-priority (SourcePriority order) WRITABLE source that
    actually SETS `attr_name`, or None when no source does. Split out from
    _driving_source so a caller can tell "some source holds this value" from
    "nothing does, the pick is only the stamp-fallback"."""
    sources, kinds = srcs["sources"], srcs["kinds"]
    writable = set(srcs["writable"])
    # `is not None`: a parse'd `param=None` (signature defaults, cleared
    # kwargs) is DECLARED-UNSET - it must not claim driving over a source
    # holding a real value (the signature's child_kwargs=None poisoned
    # from_anywhere for every real setter below it).
    candidates = [sname for sname in sources
                  if sname in writable
                  and not _unset_value(sources[sname].get(attr_name))]
    if not candidates:
        return None
    return min(candidates, key=lambda s: _source_priority(kinds.get(s)))


def _driving_source(srcs, attr_name):
    """The source name actually driving `attr_name`: the highest-priority
    WRITABLE source that currently sets it, else the signature source as the
    stamp-fallback, else None. Shared by get_source_for / from_anywhere /
    set_anywhere so they can never disagree."""
    target = _setting_source(srcs, attr_name)
    if target is not None:
        return target
    kinds, writable = srcs["kinds"], set(srcs["writable"])
    return next((s for s in srcs["sources"]
                 if kinds.get(s) == "signature" and s in writable), None)


def get_source_for(attr_name, draw_state, class_to_show=None):
    """Name of the source driving `attr_name` on this view (display label)."""
    return _driving_source(_sources_for(draw_state, class_to_show), attr_name)


def from_anywhere(attr_name, draw_state, class_to_show=None, default=None):
    """Read `attr_name` from whichever source is driving it — the read mirror
    of set_anywhere, resolved through the same registry and priority pick.
    `default` when no source sets the attr (the driving pick may be the
    signature FALLBACK, which doesn't set it yet — a + affordance case)."""
    srcs = _sources_for(draw_state, class_to_show)
    target = _driving_source(srcs, attr_name)
    if target is None:
        return default
    return srcs["sources"][target].get(attr_name, default)


# Parameters the set-anywhere round trip supports. The trip is multi-frame
# (write → debounced chain_out → save → hotswap → new o_kwargs), so supported
# params also get the in-flight display cache below; grow this list as params
# are verified end-to-end. This gates DIRECT set_anywhere usage: both generic
# accessors - `draw_state.locate_<param>` and the ParamProxy - pass
# allow_any=True, but the point of an arbitrary-name accessor is that any
# param on the view is settable.
SET_ANYWHERE_PARAMS = ("tint",)


def anywhere_value(attr_name, draw_state, default=None):
    """The value a set-anywhere editor should DISPLAY for `attr_name`:
    draw_state._kwargs — the framework-resolved truth — except while a
    set_anywhere round trip is in flight, when it's the pending UI value
    (cached on the draw_state by set_anywhere), so the widget doesn't snap
    back to the stale value for the frames the write→save→hotswap takes.

    Completion is "the live value MOVED off what it was when the set was
    issued" — never equality with the set value, which round-trips through
    source formatting (float reformat) and might never compare equal. Moving
    to anything (our set landing, or someone else's edit) clears the entry
    and live reads resume."""
    _anywhere_recompile_tick(draw_state)
    live = (draw_state._kwargs or {}).get(attr_name)
    if _unset_value(live):
        live = None      # alpha-0 = the codec opt-out; fall through
    if live is None:
        # The style cascade's own last fallback: the draw_state field
        # (ds.tint) - the DRAW_STATE source. Without this, ds-tinted windows
        # read as "no value" here while visibly wearing one.
        live = getattr(draw_state, attr_name, None)
    if live is None:
        live = default
    _anywhere_verify_tick(attr_name, draw_state, live)
    pending = getattr(draw_state, "_sa_pending", None)
    entry = pending.get(attr_name) if pending else None
    if entry is None:
        return _precise_or_live(attr_name, draw_state, live)
    ui_value, live_at_set = entry
    try:
        moved = not (live is live_at_set or bool(live == live_at_set))
    except Exception:
        moved = True
    if moved:
        del pending[attr_name]
        # The trip LANDED (live moved off the at-set baseline). Queue the
        # value cross-check for 2 frames out - comparing any sooner fires
        # on every valid set (the old write = check's failure mode).
        verify = getattr(draw_state, "_sa_verify", None)
        if verify is None:
            verify = {}
            draw_state._sa_verify = verify
        verify[attr_name] = (ui_value, Core.melty.frame_count)
        return _precise_or_live(attr_name, draw_state, live)
    return ui_value


def _anywhere_agrees(a, b):
    """Tolerant post-trip comparison: a set value round-trips through source
    formatting (floats re-rendered at 2 decimals, a 3-tuple may come back
    4-long), so exact equality would flag working trips. Numeric sequences
    compare elementwise over the common prefix at 0.01; incomparables pass
    (no basis to warn)."""
    if a is b:
        return True
    try:
        if bool(a == b):
            return True
    except Exception:
        return True
    if (isinstance(a, (tuple, list)) and isinstance(b, (tuple, list))
            and a and b):
        try:
            return all(abs(float(x) - float(y)) <= 0.01
                       for x, y in zip(a, b))
        except (TypeError, ValueError):
            return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 0.01
    return True


def _anywhere_verify_tick(attr_name, draw_state, live):
    """Deferred set_anywhere cross-check: 2 frames after a trip lands,
    compare the live value against what was set. A real divergence here
    means the SourcePriority pick wrote to a source that ISN'T driving this
    view — the one thing the alert exists to catch."""
    verify = getattr(draw_state, "_sa_verify", None)
    entry = verify.get(attr_name) if verify else None
    if entry is None:
        return
    set_value, landed_frame = entry
    if Core.melty.frame_count - landed_frame < 2:
        return
    del verify[attr_name]
    if not _anywhere_agrees(live, set_value):
        from src.lsd.gl_gui.notifications import notify
        notify(f"set_anywhere: '{attr_name}' settled at {live!r}, not the "
               f"{set_value!r} that was set — SourcePriority may not match "
               f"melty's routing for this view", tag="set_anywhere")


def _owning_code_host(cm_state, kind):
    """(str_host, source_obj) whose buffer a write of this kind lands in — the
    pair the writer-side hotswap drives. None for kinds that apply LIVE with
    no compile (comment splat, instance attr) or aren't wired yet (callers)."""
    if kind in ("signature", "decoration", "window decoration"):
        return cm_state.render_func_str, (cm_state.host_key or (None, None))[0]
    if kind in ("class var", "class default", "class decoration"):
        return cm_state.class_str, (cm_state.host_key or (None, None))[1]
    if kind == "mode":
        return cm_state.mode_str, cm_state.mode_key
    return None, None


def _anywhere_recompile_tick(draw_state):
    """Writer-side hotswap driver, run every frame the anywhere UI reads a
    value (anywhere_value). set_anywhere stamps what it's waiting for; the
    tick starts the recompile the moment the owning host's buffer moves off
    the pre-write snapshot (the debounced chain_out landing), then keeps the
    runner polled until the hotswap lands. This lives ENTIRELY on the writer
    side — code_file_io has no edit-driven recompile (the cache hosts also
    back visible editor panes, where any `edited` trigger fires per
    keystroke)."""
    pend = getattr(draw_state, "_sa_recompile", None)
    if not pend:
        return
    from src.lsd.gl_gui.view.core_conversion.new_converters import (
        host_code_state, run_recompile)
    # Keep the owning host alive through the wait - it may be an idle-swept
    # cache host that still needs to load and run its chain.
    pend["host"].notify_on_change(draw_state)
    cs = host_code_state(pend["host"])
    if cs is None or cs.address is None:
        return
    if pend["buf"] is None:
        # Stamped before the host had a code_state: adopt the first buffer we
        # see as the pre-write baseline and wait for it to move.
        pend["buf"] = cs.text_cache
        return
    start = False
    if not pend["started"]:
        if cs.text_cache is pend["buf"]:
            return                      # chain_out hasn't landed yet
        pend["started"] = True
        pend["start_frame"] = Core.melty.frame_count
        start = True
    run_recompile(pend["source"], cs, draw_state, start=start,
                  name=f"sa_recompile{draw_state.unique}")
    landed = cs._recompiled_on_frame is not None and         cs._recompiled_on_frame >= pend["start_frame"]
    # Timeout only counts AFTER the compile started - comparing an unstarted
    # pend's start_frame=0 against the session frame count cleared every
    # stamp on its first tick (the runner silently never ran).
    if landed or (pend["started"]
                  and Core.melty.frame_count - pend["start_frame"] > 600):
        draw_state._sa_recompile = None


def set_anywhere(attr_name, value, draw_state, class_to_show=None, allow_any=False,
                 ds_fallback=False):
    """Set `attr_name` at whichever input source is actually driving it —
    code, comment, decoration, mode entry — using the same registry the input
    tab edits. The write is a plain item-set on the source's bubbling parse
    dict, so the owning host goes dirty and saves through its normal chain.

    The DRIVING source is the highest-priority (SourcePriority order) writable
    source that currently sets the attr; when none sets it, the value stamps
    into the render function's signature defaults (the + button's fallback).
    Returns the source name written to, or None when nothing writable exists.

    Sanity cross-check, not bulletproof: if the driving source's pre-write
    value disagrees with the live draw_state._kwargs value, our hardcoded
    priority table probably mis-ranked this view's sources — notify, don't
    throw, and write anyway (the user asked for the set).

    `allow_any` skips the SET_ANYWHERE_PARAMS gate — the generic accessors
    pass it (the whole point of `locate_<param>` is that any signature param
    is writable), while direct calls keep the whitelist.

    `ds_fallback` changes what happens when NO source sets the param: instead
    of stamping the signature default (the + affordance's behavior), the value
    is kept on the draw_state. Also generic-accessor behavior — see the branch
    below."""
    from src.lsd.gl_gui.notifications import notify
    if not allow_any and attr_name not in SET_ANYWHERE_PARAMS:
        notify(f"set_anywhere: '{attr_name}' not in SET_ANYWHERE_PARAMS",
               tag="set_anywhere")
        return None
    # Mid-drag repeat write to an already-deferred attr: skip the registry
    # walk entirely - the drag's FIRST write resolved the target (slow) and
    # parked it; later frames just stamp the parked value. This is what
    # makes drag frames ~ instantaneous.
    _deferred = getattr(draw_state, "_sa_deferred", None)
    if _deferred and attr_name in _deferred and _input_busy():
        _deferred[attr_name] = (value, class_to_show)
        _stamp_pending(attr_name, value, draw_state)
        _last = getattr(draw_state, "_sa_last_source", None)
        return _last.get(attr_name) if _last else None
    srcs = _sources_for(draw_state, class_to_show)
    sources = srcs["sources"]
    # A spawning view can NOMINATE where edits land: live view stamps
    # preferred_source="code comment" on the value windows it spawns, so a
    # panel/menu edit targets the site's `# [<key>=...]` comment even when the
    # comment doesn't set the param yet - the write CREATES the entry there
    # (the registered row is a _LazyOverrideEntry when no comment exists,
    # and its first write materializes one). Falls through to the normal
    # pick when no writable source of that kind is registered (tree unparsed).
    target = None
    pref = _preferred_source_for(draw_state)
    if pref is not None:
        _writable = set(srcs["writable"])
        target = next((s for s in sources
                       if s in _writable
                       and _pref_matches(pref, srcs["kinds"].get(s))), None)
    if target is None and ds_fallback:
        # The DRAW_STATE is the DEFAULT write target: a plain ds.<param>
        # write (the attribute is CREATED if it doesn't exist yet, same as
        # the auto-state mirror and hand-rolled panels), which the wrapper
        # feeds back into kwargs and persists as a diverged auto_param.
        # Only a source that genuinely outranks the auto-state layer at
        # runtime (_ABOVE_DRAW_STATE: comment, mode, caller, child_kwargs,
        # @window) claims the edit into code state. A lower-layer source
        # (signature default, @defaults, class var, codec) must NOT stamp it:
        # the ds write beats those at runtime anyway, and e.g. rewriting
        # `def draw_x(param=...)` would recompile the module per slider drag.
        #
        # "Beats those at runtime" is true for AUTO-STATE-MIRRORED params,
        # whose ds write rides kwargs as a diverged auto_param. A RESERVED
        # DrawState field (tint - auto-state param names DrawState already
        # owns) stays at the DRAW_STATE source instead: the cascade's LAST
        # fallback, shadowed by ANY setting source (the Lora's injected
        # instance .tint kept winning while the header wrote ds.tint - the
        # swatch snapped back every frame). For reserved params the fallback
        # only applies when NO source sets the param; otherwise fall through
        # and write the driving source itself.
        _setting = _setting_source(srcs, attr_name)
        _kind = srcs["kinds"].get(_setting) if _setting is not None else None
        from src.lsd.gl_gui.view.core_views.core_render import (
            _draw_state_reserved_names)
        # None (DrawState not constructible yet) makes reserved set unknown;
        # keep the legacy pick for the call rather than throwing.
        _mirrored = attr_name not in (_draw_state_reserved_names() or ())
        if (_setting is None
                or (_mirrored
                    and _source_priority(_kind)[0] not in _ABOVE_DRAW_STATE)):
            setattr(draw_state, attr_name, value)
            return "draw state"
    if target is None:
        target = _driving_source(srcs, attr_name)
    if target is None:
        notify(f"set_anywhere: no writable source for '{attr_name}'",
               tag="set_anywhere")
        return None

    # Last-written source, by attr - lazily maintained (stamped here on every
    # set, and by the popover's lazy resolve on first open): cheap provenance
    # for display without a per-frame collection.
    _last = getattr(draw_state, "_sa_last_source", None)
    if _last is None:
        _last = {}
        draw_state._sa_last_source = _last
    _last[attr_name] = target

    # SLOW target + gesture in flight (drag or scroll): park the write
    # instead of running the save/recompile cycle per event (see the
    # full-speed block above).
    if _input_busy() and source_is_slow(srcs["kinds"].get(target)):
        _defer_write(attr_name, value, draw_state, class_to_show)
        return target

    # (No write-time sanity cross-check here: mid-trip the live value
    # LEGITIMATELY disagrees with the source, so comparing now cries wolf on
    # every working set. Verification is deferred - see anywhere_value: 2
    # frames after the round trip lands, live vs what we set.)
    _t_kind = srcs["kinds"].get(target)
    write_value = value
    if source_is_low_precision(_t_kind):
        write_value = _round_low_precision(value)
        try:
            _rounded_away = not bool(write_value == value)
        except Exception:
            _rounded_away = True
        if _rounded_away:
            _stamp_precise(attr_name, value, draw_state)
            # A write that only moves digits BELOW the limit is a no-op at the
            # source: the overlay already serves the precise value, so skip
            # the save/recompile trip (and clear any parked pending entry -
            # no trip means the live baseline will never move to clear it).
            try:
                if bool(sources[target].get(attr_name) == write_value):
                    _pending = getattr(draw_state, "_sa_pending", None)
                    if _pending:
                        _pending.pop(attr_name, None)
                    return target
            except Exception:
                pass
    sources[target][attr_name] = write_value
    # In-flight display cache - see _stamp_pending. The FULL-precision value:
    # the UI keeps serving it through the trip, then the overlay takes over.
    _stamp_pending(attr_name, value, draw_state)
    # Deferred writer-side hotswap: code-backed sources only become LIVE via
    # recompile, and the source text only exists after the host's debounced
    # chain_out. Snapshot the current buffer identity; the per-frame tick
    # (anywhere_value → _anywhere_recompile_tick) starts the recompile when
    # the buffer moves and polls the runner until the hotswap lands.
    cm_state = getattr(draw_state, "_sa_cm_state", None)
    if _t_kind in ("child kwargs", "attr default"):
        # These rows are parse rows of an ANCESTOR's code (stacked
        # @defaults) - which ancestor is not guessable from the ds graph (a
        # Lora item's parent is a plain dict view), but the written row KNOWS
        # its owner: its bubbling root IS the owning render host, whose
        # upstream render host's input is the real source object. Stamp the
        # deferred hotswap against exactly that; without it the trip never
        # lands and the pending cache shows the un-landed value forever.
        from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
        _row = sources.get(target)
        _broot = (getattr(_row, "_bubble_root", None)
                  or getattr(getattr(_row, "_dp", None), "_bubble_root", None))
        if isinstance(_broot, RenderHost):
            _sh = _broot.input_value if isinstance(_broot.input_value, RenderHost) else None
            _src_obj = getattr(_sh, "input_value", None) if _sh is not None else None
            if _sh is not None and _src_obj is not None:
                from src.lsd.gl_gui.view.core_conversion.new_converters import host_code_state
                _cs = host_code_state(_sh)
                draw_state._sa_recompile = {
                    "host": _sh, "source": _src_obj, "started": False,
                    "start_frame": 0,
                    "buf": _cs.text_cache if _cs is not None else None}
        return target
    if cm_state is not None:
        _rc_host, _rc_source = _owning_code_host(cm_state, _t_kind)
        if _rc_host is not None and _rc_source is not None:
            from src.lsd.gl_gui.view.core_conversion.new_converters import host_code_state
            _cs = host_code_state(_rc_host)
            draw_state._sa_recompile = {
                "host": _rc_host, "source": _rc_source, "started": False,
                "start_frame": 0,
                "buf": _cs.text_cache if _cs is not None else None}
    return target


def view_param_names(draw_state):
    """The render view's own input parameters, in signature order — what
    `locate_params` iterates.

    Signature params minus the ones that aren't inputs at all: the wrapper's
    injected/plumbing names and event params (core_render's own
    _AUTO_PARAM_EXCLUDE / _is_event_param_name — the same predicates
    auto-state uses, so the two lists can't drift), plus injected-state params
    (`gl_state: GLState = None` — a class annotation with a None default, owned
    by set_default's misc path). DrawState-reserved names (width, tint, ...)
    are deliberately KEPT: auto-state skips them because they have legacy
    manual handling, but they're still real inputs of the view.

    Also deliberately NOT extended with render_func_kwarg_names(): those
    framework kwargs are shared by every view and would bury its actual
    params."""
    import inspect
    func = getattr(draw_state, "_view_func", None)
    if func is None:
        return []
    try:
        params = inspect.signature(inspect.unwrap(func)).parameters
    except (TypeError, ValueError):
        return []
    from src.lsd.gl_gui.view.core_views.core_render import (
        _AUTO_PARAM_EXCLUDE, _is_event_param_name)
    out = []
    for name, p in params.items():
        if name in _AUTO_PARAM_EXCLUDE or _is_event_param_name(name):
            continue
        if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                      inspect.Parameter.VAR_KEYWORD):
            continue
        ann = p.annotation
        if (ann is not inspect.Parameter.empty and inspect.isclass(ann)
                and p.default is None):
            continue        # injected state (GLState / CodeState / ...)
        out.append(name)
    return out


# Builtin bases a signature default may SPECIALIZE (TensorDim(int)). The
# stored/parsed value round-trips as the plain base (comments literal_eval to
# int, auto_params to the base), so the read edge re-wraps it in the
# default's type; that type is what routes the value to its custom renderer.
_SPECIALIZE_BASES = (int, float, str, tuple)
_default_types_cache = {}


def _signature_default_types(draw_state):
    """{param: type} for signature defaults whose type is a strict SUBCLASS
    of a builtin base — the params whose values should be re-specialized on
    read. Cached per (wrapper, unwrapped) function identity pair so a hotswap
    that changes the signature refreshes it."""
    import inspect
    func = getattr(draw_state, "_view_func", None)
    if func is None:
        return {}
    try:
        inner = inspect.unwrap(func)
    except Exception:
        return {}
    key = (id(func), id(inner))
    cached = _default_types_cache.get(key)
    if cached is not None:
        return cached
    out = {}
    try:
        for n, p in inspect.signature(inner).parameters.items():
            d = p.default
            if d is inspect.Parameter.empty or d is None or isinstance(d, bool):
                continue
            t = type(d)
            for b in _SPECIALIZE_BASES:
                if isinstance(d, b) and t is not b:
                    out[n] = t
                    break
    except (TypeError, ValueError):
        pass
    _default_types_cache[key] = out
    return out


class ParamProxy(dict):
    """Live dict view over one render view's input parameters:

        ds.locate_params["x_dim"]           # resolved value (from _kwargs)
        ds.locate_params["x_dim"] = 0       # set_anywhere on the driving source
        for param, value in ds.locate_params: ...
        draw_collection(ds.locate_params)   # renders like any dict

    A REAL dict subclass, so every isinstance(x, dict) path in the framework
    (draw_collection's key routing, converters, serialization probes, `{**p}`,
    C-level fast paths that bypass overridden methods entirely) treats it as
    the dict it looks like. The inherited storage holds a SNAPSHOT of the
    resolved values, refreshed by `refresh()` whenever the draw_state hands
    the proxy out; the overridden accessors read live on top of it.

    Reads and writes keep the `locate_<param>` asymmetry: reading is the
    framework-resolved value, only writing walks the source registry. Writes
    skip the SET_ANYWHERE_PARAMS whitelist and fall back to the draw_state
    when no source in code sets the param (allow_any / ds_fallback).

    Iterating yields (param, value) PAIRS — the loop this exists for. That's
    the one deviation from dict, and it's confined to Python-level `for x in
    proxy`: `keys()`, `dict(proxy)`, `{**proxy}` and every C-level consumer go
    through the storage and see plain keys."""

    # No instance __dict__: draw_collection prefers getattr(input_value, key)
    # over collection[key] for anything that has one, which would hand a param
    # named like a dict method (`items`, `values`, ...) a bound method instead
    # of its value. With __slots__ the proxy is storage-only and every read
    # resolves through __getitem__.
    __slots__ = ("_ds",)

    def __init__(self, draw_state):
        super().__init__()
        self._ds = draw_state
        self.refresh()

    def _specialize(self, name, value):
        """Re-wrap a plain parsed value in the signature default's subtype
        (int 1 → TensorDim(1)) so type routing picks the custom renderer.
        Values the subtype can't take (a dim given by NAME) pass through."""
        dt = _signature_default_types(self._ds).get(name)
        if dt is None or value is None or type(value) is dt:
            return value
        try:
            return dt(value)
        except (TypeError, ValueError):
            return value

    def refresh(self):
        """Re-snapshot the inherited storage from the live values. Cheap (a
        _kwargs read per param) and the reason a handed-out proxy is never
        stale, including for consumers that read the storage directly."""
        ds = self._ds
        live = {k: self._specialize(k, anywhere_value(k, ds))
                for k in view_param_names(ds)}
        dict.clear(self)
        dict.update(self, live)     # bypasses __setitem__; a snapshot, not a set
        return self

    def __getitem__(self, name):
        if not dict.__contains__(self, name):
            raise KeyError(name)
        return self._specialize(name, anywhere_value(name, self._ds))

    def __setitem__(self, name, value):
        set_anywhere(name, value, self._ds, allow_any=True, ds_fallback=True)
        # Take the snapshot in step so in-frame reads (a collection row
        # re-reading what it just wrote) don't show the pre-write value.
        dict.__setitem__(self, name, value)

    def get(self, name, default=None):
        if not dict.__contains__(self, name):
            return default
        return self._specialize(name, anywhere_value(name, self._ds,
                                                     default=default))

    def items(self):
        return [(k, self._specialize(k, anywhere_value(k, self._ds)))
                for k in dict.keys(self)]

    def values(self):
        return [v for _k, v in self.items()]

    def __iter__(self):
        return iter(self.items())

    def __repr__(self):
        return f"ParamProxy({dict(self.items())!r})"


# ── draw_state.locate_* ─────────────────────────────────────────────────────
# The accessors themselves live on the DrawState CLASS, in
# model/core_model/draw_state.py, so they work on every draw_state whether or
# not this module has been imported yet; they call back into anywhere_value /
# set_anywhere / ParamProxy here via a lazy descriptor. `locate_<param>` is
# generic over the param: reads route through DrawState.__getattr__ (miss-only,
# so ordinary reads pay nothing) and writes through DrawState._locate_set,
# which @live's existing __setattr__ dispatches on the LOCATE_PARAMS. See the
# comment block there for why the two paths are wired differently.
