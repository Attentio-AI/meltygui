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

from meltygui.core.rendering.core_decoration import Core

# We want to use the order of the tab elements as a way of determining the
# source of the input. From the function's perspective it just has parameters
# injected. Melty is routing data from many different sources and does its
# best to pick sane defaults when there are multiple sources. Which source
# takes priority depends on how the code is written, and the code may change.
# Rather than trying to infer the priority, we're just hard coding it here -
# edit the order of the elements to change the runtime behavior of meltygui.
# (Below is a rough memory of which sources take priority; reorder freely.)
class SourcePriority(Enum):
    LIVE_COMMENT = 0             # the `# [tint=...]` override comment - the
                                 # wrapper SPLATS it over kwargs after every
                                 # other kwargs merges, so at runtime it
                                 # beats @defaults, callers, and codecs alike
    MODE = 1
    WINDOW_DECORATION = 2        # @window(...) on the func or class, and
                                 # @glfw_window(...) on the func - outranks
                                 # @defaults (the window kwargs drive the
                                 # window that renders the value)
    CALLER = 3                   # call-site kwargs - EXPLICITLY passed, so in
                                 # the wrapper's gauntlet they beat every
                                 # injected default layer, @defaults included
                                 # (verified live: draw_code_tabs_and_cache's
                                 # child_kwargs= wins over the GeneralParse
                                 # class @defaults). DEPTH is the natural
                                 # tiebreaker (see _source_priority) - no
                                 # CALLER_0/CALLER_1 members needed
    MODE_CHILD_KWARGS = 4        # child_kwargs={...} inside the PARENT's mode
                                 # entry (Modes.NEW_CODE). Same merge into the
                                 # child call as CHILD_KWARGS below. but on the
                                 # parent it's the MODE setting child_kwargs -
                                 # and mode outranks the parent's caller and
                                 # @defaults - so it wins the dict whenever the
                                 # mode entry carries one
    CHILD_KWARGS = 5             # the PARENT view's child_kwargs={...} - an
                                 # explicit call kwarg on the child, so
                                 # caller-strength: above @defaults for the
                                 # same reason as CALLER; the dict itself
                                 # lives at any of the parent's OWN sources,
                                 # resolved via from_anywhere("child_kwargs",
                                 # parent)
    AT_DEFAULT_CODE_TYPE = 6     # @defaults on a PARSED class in the value tree
    AT_DEFAULT_OBJ_TYPE = 7     # @defaults on the value's runtime class
    DECORATION = 8               # @render_func(...) kwargs on the view func
    INSTANCE_ATTR = 9            # whitelisted instance attr on the value object
                                 # (core_render.OBJ_ATTR_PARAMS, e.g.
                                 # Lora.tint) - injected via setdefault, so
                                 # every kwargs-borne source above wins
    CLASS_VAR = 10               # class-body assignment on the value's class -
                                 # reaches the view through the SAME getattr
                                 # injection as INSTANCE_ATTR, where the
                                 # instance var shadows it (Python lookup
                                 # order), so it ranks below the instance
    CODEC = 11                   # the active codec's render_kwargs - the
                                 # wrapper's lowest kwargs MERGE layer
                                 # (core_render `render_kwargs | ...`);
                                 # loses to every getattr-injected source
                                 # above, drives only the layers below
    RENDER_FUNC = 12             # signature defaults (def draw_x(speed=3)) —
                                 # the WEAKEST code source: Python only applies
                                 # a default when the name is absent from
                                 # kwargs entirely, so every kwargs-borne
                                 # source above wins. Ranked at 2 it used to
                                 # mask caller/child_kwargs as _setting_source
                                 # for any signature-defaulted param
                                 # (syntax_highlight on draw_text), sending
                                 # set_anywhere's ds_fallback when a draw_state
                                 # write the explicit kwarg then shadowed
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
    "glfw window decoration": SourcePriority.WINDOW_DECORATION,  # @glfw_window on the func
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
    from meltygui.state.inspection_state import ContextMenuState
    from meltygui.core.rendering.render_dispatch import collect_input_sources
    if not draw_state._call_site_captured and not draw_state._call_site_requested:
        draw_state._call_site_requested = True
        draw_state.invalidate_up(max_depth=6)
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
    for _h in (cm_state.render_func_dict, getattr(cm_state, "decoration_dict", None), cm_state.class_dict,
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
    if attr_name == "view_func":
        live = getattr(draw_state, "_wrapper", None) or draw_state._view_func
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


# ── Live write: the in-memory twin of the recompile ─────────────────────────
# A write to a DECORATOR source (`@window(tint=...)`, `@glfw_window(tint=...)`,
# `@render_func(tint=...)`) only became visible once the mouse-up recompile
# re-ran the decorator and the reconcile installed its fresh kwargs. Every
# render reads those kwargs from their place - the same place the recompile
# replaces - so a drag can write the value straight there and the window
# follows the picker live; the recompile on release lands the same value
# and nothing moves. Each case: kind caption → apply(attr, value,
# draw_state, class_to_show) → True when a registration was hit.
def _apply_window_decoration(attr_name, value, draw_state, class_to_show):
    """`Melty.annotated_window_classes[name] = (obj, kwargs)`: draw_main
    copies `kwargs` into the window's call every frame. Matched on the
    registered OBJECT (the view's wrapper or raw for a @window func, the
    shown class for @window(cls)), never on the key — a `name=` registration
    keys by that name. The dict is looked up per call: the recompile's
    reconcile swaps in a fresh one."""
    registry = getattr(Core.melty, "annotated_window_classes", None)
    if not isinstance(registry, dict):
        return False
    view_func = (draw_state._kwargs or {}).get("_view_func_origin", getattr(draw_state, "_view_func", None))
    owners = {id(o) for o in (view_func, _raw_of(view_func), class_to_show) if o is not None}
    hit = False
    for entry in registry.values():
        if isinstance(entry, tuple) and len(entry) == 2 and id(entry[0]) in owners \
                and isinstance(entry[1], dict):
            entry[1][attr_name] = value
            hit = True
    return hit


def _apply_glfw_window_decoration(attr_name, value, draw_state, class_to_show):
    """`app._ROOTS[i] = (fn, config)`: the root body reads
    `config['view_kwargs']` every frame (app._root_body), and a re-run
    decorator updates that same dict in place."""
    import meltygui.core.runtime.app as app
    view_func = (draw_state._kwargs or {}).get("_view_func_origin", getattr(draw_state, "_view_func", None))
    owners = {id(o) for o in (view_func, _raw_of(view_func)) if o is not None}
    hit = False
    for fn, config in app._ROOTS:
        if id(fn) in owners or id(_raw_of(fn)) in owners:
            kwargs = config.get('view_kwargs')
            if not isinstance(kwargs, dict):
                kwargs = config['view_kwargs'] = {}
            kwargs[attr_name] = value
            hit = True
    return hit


def _apply_render_func_decoration(attr_name, value, draw_state, class_to_show):
    """`@render_func(tint=…)` lives in the wrapper's closure: the `o_kwargs`
    dict (merged under every call's kwargs) and, for the tint, the
    `_decoration_tint` cell the wrapper compares identities against. The
    hotswap's _transfer_wrapper_state later overwrites both cells with the
    freshly-decorated values — the same ones."""
    wrapper = getattr(draw_state, "_view_func", None)
    code = getattr(wrapper, "__code__", None)
    cells = getattr(wrapper, "__closure__", None)
    if code is None or cells is None:
        return False
    names = code.co_freevars
    if "o_kwargs" not in names:
        return False
    o_kwargs = cells[names.index("o_kwargs")].cell_contents
    if not isinstance(o_kwargs, dict):
        return False
    o_kwargs[attr_name] = value
    if attr_name == "tint":
        # The identity the wrapper's `_tint_as_decoration` compares against
        # sits in that nested helper's own closure (a cell of the wrapper).
        for cell in _decoration_tint_cells(wrapper):
            cell.cell_contents = value
    return True


def _decoration_tint_cells(wrapper):
    """The `_decoration_tint` cells reachable from a render_func wrapper:
    on the wrapper itself or on a nested function it closes over."""
    found = []
    seen = set()
    stack = [wrapper]
    while stack:
        fn = stack.pop()
        if id(fn) in seen:
            continue
        seen.add(id(fn))
        code = getattr(fn, "__code__", None)
        cells = getattr(fn, "__closure__", None)
        if code is None or not cells:
            continue
        for name, cell in zip(code.co_freevars, cells):
            try:
                content = cell.cell_contents
            except ValueError:
                continue
            if name == "_decoration_tint":
                found.append(cell)
            elif callable(content) and getattr(content, "__closure__", None):
                stack.append(content)
    return found


def _raw_of(func):
    try:
        import inspect
        return inspect.unwrap(func) if func is not None else None
    except Exception:
        return None


_LIVE_APPLY = {
    "window decoration": _apply_window_decoration,
    "class decoration": _apply_window_decoration,
    "glfw window decoration": _apply_glfw_window_decoration,
    "decoration": _apply_render_func_decoration,
}


def live_apply(attr_name, value, draw_state, kind, class_to_show=None):
    """Write `value` into the in-memory registration a source of `kind`
    feeds the render loop from (_LIVE_APPLY), so the view shows it on the
    next frame without waiting for the save + recompile. False for kinds
    that only become live through a recompile (signature, @defaults,
    callers, mode) — those keep the display cache alone."""
    apply = _LIVE_APPLY.get(kind)
    if apply is None:
        return False
    try:
        hit = apply(attr_name, value, draw_state, class_to_show)
    except Exception as e:
        print(f"[live_apply] {kind} {attr_name}: {e}")
        return False
    if hit:
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
    return hit


def _defer_write(attr_name, value, draw_state, class_to_show, source=None, kind=None):
    """Park a slow-source write for the duration of the drag: the display
    cache serves reads immediately; flush_deferred_writes runs the real
    set_anywhere on release."""
    deferred = getattr(draw_state, "_sa_deferred", None)
    if deferred is None:
        deferred = {}
        draw_state._sa_deferred = deferred
    deferred[attr_name] = (value, class_to_show, source, kind)
    _DEFERRED_DS.add(draw_state)
    _stamp_pending(attr_name, value, draw_state)
    live_apply(attr_name, value, draw_state, kind, class_to_show)


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
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
        return
    for ds in list(_DEFERRED_DS):
        _DEFERRED_DS.discard(ds)
        deferred = getattr(ds, "_sa_deferred", None) or {}
        items = list(deferred.items())
        deferred.clear()
        for attr_name, (value, class_to_show, source, _kind) in items:
            set_anywhere(attr_name, value, ds, class_to_show=class_to_show,
                         allow_any=True, ds_fallback=True, source=source)


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
    if attr_name == "view_func" and srcs.get("view_func") is not None:
        import inspect
        from meltygui.core.input.view_selection import resolve_view_func
        live = inspect.unwrap(resolve_view_func(srcs["view_func"]))
        candidates = [s for s in candidates if not (getattr(sources[s], "direct", False)
                                                   or getattr(sources[s], "implicit_view_func", False))
                      or inspect.unwrap(resolve_view_func(sources[s][attr_name])) is live]
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
    if attr_name == "view_func":
        return next((s for s in srcs["sources"]
                     if kinds.get(s) == "draw state" and s in writable), None)
    return next((s for s in srcs["sources"]
                 if kinds.get(s) == "signature" and s in writable), None)


def get_source_for(attr_name, draw_state, class_to_show=None):
    """Name of the source driving `attr_name` on this view (display label)."""
    return _driving_source(_sources_for(draw_state, class_to_show), attr_name)


# Call sites in these framework locations are BAD default write targets - a
# kwarg stamped into view_collection's dispatch call, the render loop
# (end_frame / update_melty_windows / the studio's draw), or any core_view
# plumbing would restyle every view app-wide. Caller rows located here are
# skipped by default_write_source's pick - they stay visible and manually
# pickable in the info tab's dropdown.
_FRAMEWORK_CALLER_DIRS = ("/meltygui/rendering/", "/meltygui/views/",
                          "/meltygui/code/", "/meltygui/state/", "/meltygui/utils/",
                          "/meltygui/editor/", "/meltygui_pro/editor/")
_FRAMEWORK_CALLER_FILES = ("melty.py", "app.py", "surface.py", "background.py")


def _is_framework_caller(location):
    """True when a caller row's (file, line) sits inside the meltygui framework
    — or is unknown, which must never be defaulted into either."""
    if not location or not location[0]:
        return True
    p = str(location[0]).replace("\\", "/")
    if "/meltygui/" in p or "/meltygui_pro/" in p:
        return True
    if any(d in p for d in _FRAMEWORK_CALLER_DIRS):
        return True
    return ("/meltygui/" in p
            and p.rsplit("/", 1)[-1] in _FRAMEWORK_CALLER_FILES)


def window_source_writable(srcs, target):
    """Automatic window gestures may always edit comments/local state.

    Source-code changes require the GUI-editor toggle; framework dispatch
    call sites are never an automatic target, even with that toggle enabled.
    """
    from meltygui.core.runtime.toggles import Toggles
    kind = srcs['kinds'].get(target)
    if kind in ('code comment', 'draw state'):
        return True
    if not Toggles.dangerous_edit_mode:
        return False
    if isinstance(kind, str) and kind.startswith('caller'):
        return not _is_framework_caller(srcs['locations'].get(target))
    return target is not None


def window_position_movable(draw_state, kwargs):
    if kwargs.get('window_pos') is None:
        return True
    # Live windows nominate their comment even before it has an entry.
    if kwargs.get('preferred_source') == 'code comment':
        return True
    srcs = _sources_for(draw_state)
    return window_source_writable(srcs, _setting_source(srcs, 'window_pos'))


def default_write_source(attr_name, draw_state, class_to_show=None, srcs=None):
    """The source name a NEW write of `attr_name` should default to — the
    info tab's picker preselection.

    1. A source already setting the attr wins (the normal driving pick).
    2. Otherwise, use the source setting view_func when available.
    3. With no view_func source, use the view's OTHER params as a cue: the writable source
       already defining the most of them is where this view is being
       configured, so a new param belongs there too (SourcePriority as the
       tie-breaker). The signature is excluded — it defines EVERY param by
       construction and would always win — and so is the draw_state (it's
       the fallback, not a configuration site).
    4. No cue at all: the highest-priority writable code source, else the
       draw_state."""
    if srcs is None:
        srcs = _sources_for(draw_state, class_to_show)
    writable = set(srcs["writable"])

    def _eligible(sname):
        kind = srcs["kinds"].get(sname)
        if sname not in writable or kind in ("signature", "draw state"):
            return False
        if isinstance(kind, str) and kind.startswith("caller"):
            # Caller row names are the frame's function name (with an
            # optional " ^N" dedup suffix). A dunder name (__call__ - a
            # wrapper/dispatch protocol) is machinery regardless of where
            # it lives, never a place to stamp a view kwarg.
            fn = sname.split(" ^", 1)[0]
            if fn.startswith("__") and fn.endswith("__"):
                return False
            return not _is_framework_caller(srcs["locations"].get(sname))
        return True

    target = _setting_source(srcs, attr_name)
    if target is not None and _eligible(target):
        return target
    target = _setting_source(srcs, "view_func")
    if target is not None:
        return target
    others = set(view_param_names(draw_state))
    others.discard(attr_name)

    candidates = [s for s in srcs["sources"] if _eligible(s)]
    best, best_key = None, None
    for sname in candidates:
        sdict = srcs["sources"][sname]
        try:
            count = sum(1 for k in sdict if k in others)
        except Exception:
            count = 0
        if not count:
            continue
        key = (-count, _source_priority(srcs["kinds"].get(sname)))
        if best_key is None or key < best_key:
            best, best_key = sname, key
    if best is not None:
        return best
    if candidates:
        return min(candidates,
                   key=lambda s: _source_priority(srcs["kinds"].get(s)))
    return "draw_state"


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
SET_ANYWHERE_PARAMS = ("tint", "view_func")


def anywhere_value(attr_name, draw_state, default=None, live_kwargs=None):
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
    live = (draw_state._kwargs if live_kwargs is None else live_kwargs) or {}
    live = live.get(attr_name)
    if attr_name == "view_func":
        live = getattr(draw_state, "_wrapper", None) or draw_state._view_func
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
        from meltygui.core.diagnostics.notifications import notify
        notify(f"set_anywhere: '{attr_name}' settled at {live!r}, not the "
               f"{set_value!r} that was set — SourcePriority may not match "
               f"meltygui's routing for this view", tag="set_anywhere")


def _owning_code_host(cm_state, kind):
    """(str_host, source_obj) whose buffer a write of this kind lands in — the
    pair the writer-side hotswap drives. None for kinds that apply LIVE with
    no compile (comment splat, instance attr) or aren't wired yet (callers)."""
    if kind in ("decoration", "window decoration", "glfw window decoration") and getattr(cm_state, "decoration_str", None) is not None:
        return cm_state.decoration_str, cm_state.decoration_key
    if kind in ("signature", "decoration", "window decoration", "glfw window decoration"):
        return cm_state.render_func_str, (cm_state.host_key or (None, None))[0]
    if kind in ("class var", "class default", "class decoration"):
        return cm_state.class_str, (cm_state.host_key or (None, None))[1]
    if isinstance(kind, str) and kind.startswith("caller"):
        # One host pair per walked frame (_collect_input_sources), indexed by
        # the kind's depth suffix. The str host's input is the CallSite, which
        # recompile_source hotswaps via _recompile_caller (the ENCLOSING
        # function, with the edited statement spliced-in).
        depth = int(kind.split("+", 1)[1]) if "+" in kind else 0
        hosts = cm_state.call_site_hosts or []
        if depth < len(hosts) and hosts[depth][0] is not None:
            _sh = hosts[depth][0]
            return _sh, getattr(_sh, "input_value", None)
        return None, None
    if kind in ("mode", "mode child kwargs"):
        # "mode child kwargs" rows are registered off the PARENT's mode host,
        # but code_hosts_for caches per-mode reference, so the target's own
        # mode host (same enum class) is the same host pair. A leaf whose
        # current_mode enum differs from the parent's would stamp the wrong
        # host and the recompile wait times out harmlessly.
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
    # Once per frame: anywhere_value calls this per PARAM (the params panel /
    # info tab read every param each refresh), and a second run_recompile
    # of the same name in one frame triggers run_in_background's duplicate-
    # unique detection (sa_recompile<u> : Forcing re-render" spam).
    if pend.get("tick_frame") == Core.melty.frame_count:
        return
    pend["tick_frame"] = Core.melty.frame_count
    from meltygui.code.new_converters import host_code_state
    from meltygui.code.new_converters import run_recompile
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
    landed = (cs._recompiled_on_frame is not None
              and cs._recompiled_on_frame >= pend["start_frame"])
    # Timeout only counts AFTER the compile started - comparing an unstarted
    # pend's start_frame=0 against the session frame count cleared every
    # stamp on its first tick (the runner silently never ran).
    if landed or (pend["started"]
                  and Core.melty.frame_count - pend["start_frame"] > 600):
        draw_state._sa_recompile = None
        # Switching draw_any's renderer can retire this DrawState. If it is
        # reused later, its old baseline may still equal the live renderer;
        # don't resurrect a selection whose recompile already completed.
        pending = getattr(draw_state, "_sa_pending", None)
        if pending:
            pending.pop("view_func", None)


def set_anywhere(attr_name, value, draw_state, class_to_show=None, allow_any=False,
                 ds_fallback=False, source=None):
    """Set `attr_name` at whichever input source is actually driving it —
    code, comment, decoration, mode entry — using the same registry the input
    tab edits. The write is a plain item-set on the source's bubbling parse
    dict, so the owning host goes dirty and saves through its normal chain.

    The DRIVING source is the highest-priority (SourcePriority order) writable
    source that currently sets the attr; when none sets it, the value stamps
    alongside view_func, or into the signature defaults when no source sets
    view_func (the + button's fallback).
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
    follows the source setting view_func, falling back to draw_state when
    there is none. This also applies to auto-state params driven only by
    default layers; higher-priority sources keep their existing writes.

    `source` names an EXPLICIT target (a registered source name, or the
    literal "draw_state") — the info tab's per-param picker passes it. It
    overrides preferred_source and the driving pick entirely; the write can
    also CREATE the entry at that source (a new caller kwarg, a new class
    var), which is how the picker's + affordance stamps a param nothing
    sets yet."""
    from meltygui.core.diagnostics.notifications import notify
    if not allow_any and attr_name not in SET_ANYWHERE_PARAMS:
        notify(f"set_anywhere: '{attr_name}' not in SET_ANYWHERE_PARAMS",
               tag="set_anywhere")
        return None
    if attr_name == "view_func":
        from meltygui.core.input.view_selection import resolve_view_func
        try:
            value = resolve_view_func(value)
        except ValueError as error:
            notify(str(error), tag="set_anywhere")
            return None
    # Mid-drag repeat write to an already-deferred attr: skip the registry
    # walk entirely - the drag's FIRST write resolved the target (slow) and
    # parked it; later frames just stamp the parked value. This is what
    # makes drag frames ~ instantaneous.
    _deferred = getattr(draw_state, "_sa_deferred", None)
    if _deferred and attr_name in _deferred and _input_busy():
        _prev = _deferred[attr_name]
        deferred_source = source if source is not None else _prev[2]
        _kind = _prev[3]
        _deferred[attr_name] = (value, class_to_show, deferred_source, _kind)
        _stamp_pending(attr_name, value, draw_state)
        live_apply(attr_name, value, draw_state, _kind, class_to_show)
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
    if source is not None:
        # Explicit target from the picker: honor it or fail loudly - silently
        # falling back to the automatic pick would write somewhere the user
        # didn't choose.
        _writable = set(srcs["writable"])
        if source in sources and source in _writable:
            target = source
        elif source == "draw_state":
            # The ds row only registers once a whitelisted attr diverged, so
            # the picker offers the literal name even when unregistered.
            if attr_name == "view_func":
                draw_state.auto_params[attr_name] = value
                draw_state.invalidate_up(max_depth=6)
            else:
                setattr(draw_state, attr_name, value)
            _last_ds = getattr(draw_state, "_sa_last_source", None)
            if _last_ds is None:
                _last_ds = {}
                draw_state._sa_last_source = _last_ds
            _last_ds[attr_name] = "draw state"
            return "draw state"
        else:
            notify(f"set_anywhere: picked source {source!r} isn't writable "
                   f"for '{attr_name}'", tag="set_anywhere")
            return None
    pref = _preferred_source_for(draw_state) if target is None else None
    if pref is not None:
        _writable = set(srcs["writable"])
        target = next((s for s in sources
                       if s in _writable
                       and _pref_matches(pref, srcs["kinds"].get(s))), None)
    if target is None and ds_fallback:
        # Preserve sources above auto-state and existing reserved param
        # sources (such as tint). Otherwise save alongside view_func, with
        # the old draw_state fallback for calls without a renderer source.
        _setting = _setting_source(srcs, attr_name)
        _kind = srcs["kinds"].get(_setting) if _setting is not None else None
        from meltygui.core.core_render import _draw_state_reserved_names
        # None (DrawState not constructible yet) makes reserved set unknown;
        # keep the legacy pick for the call rather than throwing.
        _mirrored = attr_name not in (_draw_state_reserved_names() or ())
        if (_setting is None
                or (_mirrored
                    and _source_priority(_kind)[0] not in _ABOVE_DRAW_STATE)):
            target = _setting_source(srcs, "view_func")
            if target is None:
                if attr_name == "view_func":
                    draw_state.auto_params[attr_name] = value
                    draw_state.invalidate_up(max_depth=6)
                else:
                    setattr(draw_state, attr_name, value)
                # Stamp provenance like every other target - without it a ds
                # write is invisible ("where did my line_height=2 go?"): the
                # value lives only in auto_params.
                _last_ds = getattr(draw_state, "_sa_last_source", None)
                if _last_ds is None:
                    _last_ds = {}
                    draw_state._sa_last_source = _last_ds
                _last_ds[attr_name] = "draw state"
                return "draw state"
    if target is None and _setting_source(srcs, attr_name) is None:
        target = _setting_source(srcs, "view_func")
    if target is None:
        target = _driving_source(srcs, attr_name)
    if target is None:
        notify(f"set_anywhere: no writable source for '{attr_name}'",
               tag="set_anywhere")
        return None
    if attr_name == "view_func" and srcs["kinds"].get(target) == "signature" and attr_name not in sources[target]:
        notify("Choose a caller, defaults, comment, or draw-state source for the renderer", tag="set_anywhere")
        return None

    if source is None and ds_fallback and attr_name in ('closed', 'window_pos'):
        # Parameter panels inherit their inspected window's comment for
        # parameter edits, but their own geometry/visibility belongs to the
        # panel. Never close or move the ancestor through that inherited row.
        owner = srcs.get('comment_owners', {}).get(target, draw_state)
        if owner is not draw_state:
            setattr(draw_state, attr_name, value)
            last = getattr(draw_state, '_sa_last_source', None)
            if last is None:
                last = {}
                draw_state._sa_last_source = last
            last[attr_name] = 'draw state'
            return 'draw state'
        if not window_source_writable(srcs, target):
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
        _defer_write(attr_name, value, draw_state, class_to_show, source=source,
                     kind=srcs["kinds"].get(target))
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
    if attr_name == "view_func":
        from meltygui.core.input.view_selection import resolve_view_func
        from meltygui.core.input.view_selection import view_reference_code
        value = resolve_view_func(value)
        if _t_kind == "code comment":
            write_value = f"RenderFuncs.{value.__name__}" if value is not None else None
        elif not getattr(sources[target], "direct", False) and source_is_slow(_t_kind):
            location = srcs["locations"].get(target)
            write_value = view_reference_code(value, location[0] if location else None)
        else:
            write_value = value
    sources[target][attr_name] = write_value
    # The in-memory twin of the recompile the write arms below: the window
    # shows the value now, the hotswap lands the same live on the source's
    # save (see _LIVE_APPLY).
    live_apply(attr_name, value, draw_state, _t_kind, class_to_show)
    # A write to a BELOW-draw_state layer (signature, @defaults, class var,
    # codec, var) would be otherwise shadowed by a diverged auto_param
    # riding kwargs. The ds layer is framework session state, not user code -
    # so the code write CLAIMS the param: drop the stale auto_param and let
    # the new source value drive (the field case: a stale
    # auto_params['hide_internal'] kept overriding a fresh signature edit).
    if _t_kind != "draw state" and _source_priority(_t_kind)[0] not in _ABOVE_DRAW_STATE:
        _ap = getattr(draw_state, "auto_params", None)
        if isinstance(_ap, dict):
            _ap.pop(attr_name, None)
    # In-flight display cache - see _stamp_pending. The FULL-precision value:
    # the UI keeps serving it through the trip, then the overlay takes over.
    _stamp_pending(attr_name, value, draw_state)
    # Deferred writer-side hotswap: code-backed sources only become LIVE via
    # recompile, and the source text only exists after the host's debounced
    # chain_out. Snapshot the current buffer identity; the per-frame tick
    # (anywhere_value → _anywhere_recompile_tick) starts the recompile when
    # the buffer moves and polls the runner until the hotswap lands.
    _arm_recompile(draw_state, sources, target, _t_kind)
    return target


def _arm_recompile(draw_state, sources, target, kind):
    """Stamp the deferred writer-side hotswap for a code-backed edit at
    `target` (kind caption `kind`) — shared by set_anywhere and
    clear_anywhere. The per-frame tick (_anywhere_recompile_tick) starts the
    recompile when the owning host's buffer moves off the snapshot."""
    if kind in ("child kwargs", "mode child kwargs", "attr default"):
        # These rows are parse rows of an ANCESTOR's code (stacked
        # @defaults) - which ancestor is not guessable from the ds graph (a
        # Lora item's parent is a plain dict view), but the written row KNOWS
        # its owner: its bubbling root IS the owning render host, whose
        # upstream render host's input is the real source object. Stamp the
        # deferred hotswap against exactly that; without it the trip never
        # lands and the pending cache shows the un-landed value forever.
        from meltygui.core.conversion.render_host import RenderHost
        _row = sources.get(target)
        _broot = (getattr(_row, "_bubble_root", None)
                  or getattr(getattr(_row, "_dp", None), "_bubble_root", None))
        if isinstance(_broot, RenderHost):
            _sh = _broot.input_value if isinstance(_broot.input_value, RenderHost) else None
            _src_obj = getattr(_sh, "input_value", None) if _sh is not None else None
            if _sh is not None and _src_obj is not None:
                from meltygui.code.new_converters import host_code_state
                _cs = host_code_state(_sh)
                draw_state._sa_recompile = {
                    "host": _sh, "source": _src_obj, "started": False,
                    "start_frame": 0,
                    "buf": _cs.text_cache if _cs is not None else None}
        return
    cm_state = getattr(draw_state, "_sa_cm_state", None)
    if cm_state is not None:
        _rc_host, _rc_source = _owning_code_host(cm_state, kind)
        if _rc_host is not None and _rc_source is not None:
            from meltygui.code.new_converters import host_code_state
            _cs = host_code_state(_rc_host)
            draw_state._sa_recompile = {
                "host": _rc_host, "source": _rc_source, "started": False,
                "start_frame": 0,
                "buf": _cs.text_cache if _cs is not None else None}


def clear_anywhere(attr_name, draw_state, source, class_to_show=None):
    """Delete `attr_name`'s entry AT `source` — the info tab's trash button.
    A code source loses its parse entry (bubbling __delitem__ dirties the
    host → normal chain_out/save) and the module hotswaps through the same
    deferred trip as a set_anywhere write. "draw_state" drops the diverged
    auto_param (and nulls a whitelisted ds attr), so lower-priority layers
    resume driving. Returns the source cleared, or None when it held
    nothing."""
    from meltygui.core.diagnostics.notifications import notify
    srcs = _sources_for(draw_state, class_to_show)
    # In-flight caches for this attr are stale either way a clear goes.
    for _slot in ("_sa_pending", "_sa_precise", "_sa_deferred"):
        _d = getattr(draw_state, _slot, None)
        if isinstance(_d, dict):
            _d.pop(attr_name, None)
    kind = srcs["kinds"].get(source)
    if source == "draw_state" or kind == "draw state":
        cleared = False
        _ap = getattr(draw_state, "auto_params", None)
        if isinstance(_ap, dict) and attr_name in _ap:
            del _ap[attr_name]
            cleared = True
        from meltygui.core.core_render import OBJ_ATTR_PARAMS
        if (attr_name in OBJ_ATTR_PARAMS
                and getattr(draw_state, attr_name, None) is not None):
            setattr(draw_state, attr_name, None)
            cleared = True
        return "draw_state" if cleared else None
    sdict = srcs["sources"].get(source)
    if not isinstance(sdict, dict) or attr_name not in sdict:
        notify(f"clear_anywhere: {source!r} doesn't set '{attr_name}'",
               tag="set_anywhere")
        return None
    if kind == "instance attr" and hasattr(sdict, "_obj"):
        # Snapshot adapter over the live object - clear the OBJECT, not just
        # the snapshot (a snapshot del would resurrect next collect).
        try:
            delattr(sdict._obj, attr_name)
        except AttributeError:
            setattr(sdict._obj, attr_name, None)
        dict.__delitem__(sdict, attr_name)
        draw_state.invalidate_up(max_depth=6)
        return source
    if kind == "codec":
        # _CodecSource writes fan out to per-file meta / class render_kwargs;
        # a snapshot del wouldn't reach them. Not wired yet.
        notify(f"clear_anywhere: clearing at the codec isn't supported yet",
               tag="set_anywhere")
        return None
    if attr_name == "view_func" and getattr(sdict, "direct", False):
        notify("A direct call needs a render function; choose another renderer", tag="set_anywhere")
        return None
    del sdict[attr_name]
    _arm_recompile(draw_state, srcs["sources"], source, kind)
    return source


def _permute_slots(sdict, order):
    """Refill the slots of `sdict`'s keys that appear in `order` with those
    same keys sorted by `order`; every other key (dunder bookkeeping, params
    the panel doesn't show, comment keys) keeps its slot. Slot permutation,
    the same rule every CST writer applies on save (_reorder_params /
    _reorder_call_kwargs / _reorder_class_body / _reformat_override_comment),
    so the dict's new order is exactly what the source will read. In place
    via clear/update, which a bubbling parse dict bubbles to its host as a
    change. False when fewer than two keys are involved or nothing moves."""
    present = [k for k in dict.keys(sdict) if k in order]
    if len(present) < 2:
        return False
    wanted = sorted(present, key=order.__getitem__)
    if wanted == present:
        return False
    refill = iter(wanted)
    items = []
    for k, v in dict.items(sdict):
        if k in order:
            nk = next(refill)
            items.append((nk, dict.__getitem__(sdict, nk)))
        else:
            items.append((k, v))
    sdict.clear()
    sdict.update(items)
    return True


def reorder_anywhere(keys, draw_state, class_to_show=None):
    """Write a new ORDER of the view's params — `keys`, the complete key order
    the params panel was dragged into — to every code-backed source that
    stores two or more of them: the signature's parameter list, a caller's
    or decorator's kwargs, a class body, a mode entry's kwargs, an override
    comment. Each is an ordered store in its own right, so each one follows
    the panel's relative order (slot permutation: a source holding a SUBSET
    of the params neither gains nor loses keys) and they all agree after the
    drag — the signature among them, which is where the panel's own order
    derives from. Only parse-node dicts qualify: the adapter rows (instance
    attr, draw_state, codec) are snapshots over live objects with no
    persisted order. Returns the source names written, in priority order."""
    from meltygui.core.conversion.bubbling import _BubblingDictMixin
    srcs = _sources_for(draw_state, class_to_show)
    order = {k: i for i, k in enumerate(keys)}
    writable = set(srcs["writable"])
    written = []
    for sname, sdict in srcs["sources"].items():
        if sname not in writable or not isinstance(sdict, _BubblingDictMixin):
            continue
        if _permute_slots(sdict, order):
            written.append(sname)
    # Deferred writer-side hotswap, like set_anywhere. One stamp per
    # draw_state, so arm lowest-priority first and let the signature (the
    # first registered, the one the panel's order reads back from) win.
    for sname in reversed(written):
        _arm_recompile(draw_state, srcs["sources"], sname, srcs["kinds"].get(sname))
    return written


def _pending_param_order(draw_state):
    """The view function's parameter order as the PENDING source has it: the
    signature parse held by this view's code host (`_sa_cm_state`, there once
    anything has collected the view's sources). A reorder rewrites that parse
    at once while the live function only follows after the save + hotswap
    trip, so ordering the proxy by it shows the drag's result immediately
    instead of snapping back for the trip's duration. None when no host is
    in reach — the live signature order stands."""
    cm_state = getattr(draw_state, "_sa_cm_state", None)
    host = getattr(cm_state, "render_func_dict", None)
    if host is None:
        return None
    try:
        params = host.deep.parameters()
    except Exception:
        return None
    if not isinstance(params, dict):
        return None
    return [k for k in dict.keys(params)
            if isinstance(k, str) and not k.startswith("__")]


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
    return _func_param_names(getattr(draw_state, "_view_func", None))


def _func_param_names(func):
    """Input param names of a render func (unwrapped), with the wrapper's
    injected/plumbing/event/state params filtered — the engine behind
    view_param_names and header_param_names."""
    import inspect
    if func is None:
        return []
    try:
        params = inspect.signature(inspect.unwrap(func)).parameters
    except (TypeError, ValueError):
        return []
    from meltygui.core.core_render import _AUTO_PARAM_EXCLUDE
    from meltygui.core.core_render import _is_event_param_name
    out = []
    for name, p in params.items():
        if name in _AUTO_PARAM_EXCLUDE or _is_event_param_name(name):
            continue
        if name.startswith("_"):
            continue        # placeholder/private (`_`), not a real input
        if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                      inspect.Parameter.VAR_KEYWORD):
            continue
        ann = p.annotation
        if (ann is not inspect.Parameter.empty and inspect.isclass(ann)
                and p.default is None):
            continue        # injected state (GLState / CodeState / ...)
        out.append(name)
    return out


def signature_default_for(attr_name, draw_state):
    """The param's DECLARED default: the view function's signature, else the
    header function's (locate_all_params spans both). None when neither
    declares one — the + affordance falls back to this when the resolved
    value is None (a header param nothing sets resolves to None; stamping
    that None would create an entry that still reads as unset)."""
    import inspect
    for fn in (getattr(draw_state, "_view_func", None),
               (getattr(draw_state, "_kwargs", None) or {}).get("with_header")):
        if not callable(fn):
            continue
        try:
            p = inspect.signature(inspect.unwrap(fn)).parameters.get(attr_name)
        except (TypeError, ValueError):
            continue
        if p is not None and p.default is not inspect.Parameter.empty:
            return p.default
    return None


# Header plumbing _AUTO_PARAM_EXCLUDE didn't cover - the header receives
# these from the wrapper/parent per call, they're never user inputs.
_HEADER_PARAM_EXCLUDE = {"meltygui", "parent_show_add_delete", "name_func"}


def header_param_names(draw_state):
    """Params of the view's HEADER function — the resolved `with_header`
    kwarg (a render_func or plain callable). Empty when the view has no
    header. Same filtering as the view's own params."""
    hf = (getattr(draw_state, "_kwargs", None) or {}).get("with_header")
    if not callable(hf):
        return []
    return [n for n in _func_param_names(hf) if n not in _HEADER_PARAM_EXCLUDE]


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
    __slots__ = ("_ds", "_names_fn")

    def __init__(self, draw_state, names_fn=None):
        super().__init__()
        self._ds = draw_state
        # names_fn: which params this proxy spans - view_param_names by
        # default; the grouped locate_all_params supplies the header-only
        # supplier for its 'header' sub-dict. Same read/write semantics
        # either way (the header draws against the same draw_state/kwargs).
        self._names_fn = names_fn
        # Warm the view's code hosts now (one registry walk; the hosts parse
        # in the background): a reorder only reaches sources that are parsed
        # when the drop lands, and without this the first drag on a fresh
        # panel would be the call that first creates the signature host and
        # the comment would move while the header (the panel's own order)
        # stayed put. Guarded: a proxy can be minted for a view whose sources
        # haven't been collected (no view func yet), it's a plain snapshot.
        try:
            _sources_for(draw_state)
        except Exception:
            pass
        self.refresh()

    def _names(self):
        names = (self._names_fn or view_param_names)(self._ds)
        # Live-signature names go in the PENDING signature's order when a code
        # host holds one (see _pending_param_order); ones the host doesn't
        # know (header params) keep their relative order at the end.
        pending = _pending_param_order(self._ds)
        if pending:
            pos = {k: i for i, k in enumerate(pending)}
            names.sort(key=lambda n: pos.get(n, len(pos)))
        return names

    def reorder_keys(self, keys):
        """drag_drop.Reorder's collection hook: `keys` is this proxy's
        complete new key order. The order lives in the view's code sources,
        so it is written there (reorder_anywhere) and the snapshot follows
        on refresh — the pending signature parse already reads back in the
        new order. True when any source moved."""
        written = reorder_anywhere(list(keys), self._ds)
        if written:
            self.refresh()
        return bool(written)

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
                for k in self._names()}
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


def _header_only_param_names(draw_state):
    """Header params MINUS ones the view itself declares — those belong to
    the 'params' group (view-first dedup, same rule the flat proxy had)."""
    own = set(view_param_names(draw_state))
    return [n for n in header_param_names(draw_state) if n not in own]


class GroupedParamProxy(dict):
    """locate_all_params' shape: two nested live dicts —

        {'params': <ParamProxy over the view's own params>,
         'header': <ParamProxy over the header-only params>}

    Each leaf keeps ParamProxy semantics (reads resolve via anywhere_value,
    item-writes route through set_anywhere); the grouping just separates the
    two origins so they render/route as distinct nested dicts. refresh()
    re-snapshots both — the DrawState property calls it per access, same
    stable-identity rules as locate_params."""
    __slots__ = ()

    def __init__(self, draw_state):
        super().__init__()
        dict.__setitem__(self, "params", ParamProxy(draw_state))
        dict.__setitem__(self, "header",
                         ParamProxy(draw_state,
                                    names_fn=_header_only_param_names))

    def refresh(self):
        for v in dict.values(self):
            v.refresh()
        return self

    def __setitem__(self, key, value):
        # draw_collection writes the changed CHILD dict into its parent
        # (`grouped['params'] = edited`) - accepting that would swap the live
        # sub-proxy for a plain dict snapshot and disconnect routing. The
        # sub-proxies are canonical and already received the leaf writes in
        # place, so a group-slot write is a no-op; unknown keys are refused.
        if key not in self:
            raise KeyError(f"GroupedParamProxy has fixed groups, not {key!r}")


# ── draw_state.locate_* ─────────────────────────────────────────────────────
# The accessors themselves live on the DrawState CLASS, in
# model/core_model/draw_state.py, so they work on every draw_state whether or
# not this module has been imported yet; they call back into anywhere_value /
# set_anywhere / ParamProxy here via a lazy descriptor. `locate_<param>` is
# generic over the param: reads route through DrawState.__getattr__ (miss-only,
# so ordinary reads pay nothing) and writes through DrawState._locate_set,
# which @live's existing __setattr__ dispatches on the LOCATE_PARAMS. See the
# comment block there for why the two paths are wired differently.
