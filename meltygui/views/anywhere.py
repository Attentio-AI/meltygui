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


def _driving_source(srcs, attr_name):
    """The source name actually driving `attr_name`: the highest-priority
    (SourcePriority order) WRITABLE source that currently sets it, else the
    signature source as the stamp-fallback, else None. Shared by
    get_source_for / from_anywhere / set_anywhere so they can never disagree."""
    sources, kinds = srcs["sources"], srcs["kinds"]
    writable = set(srcs["writable"])
    # `is not None`: a parse'd `param=None` (signature defaults, cleared
    # kwargs) is DECLARED-UNSET - it must not claim driving over a source
    # holding a real value (the signature's child_kwargs=None poisoned
    # from_anywhere for every real setter below it).
    candidates = [sname for sname in sources
                  if sname in writable
                  and not _unset_value(sources[sname].get(attr_name))]
    if candidates:
        return min(candidates, key=lambda s: _source_priority(kinds.get(s)))
    return next((s for s in sources
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
# are verified end-to-end.
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
        return live
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
        return live
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


def set_anywhere(attr_name, value, draw_state, class_to_show=None):
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
    throw, and write anyway (the user asked for the set)."""
    from src.lsd.gl_gui.notifications import notify
    if attr_name not in SET_ANYWHERE_PARAMS:
        notify(f"set_anywhere: '{attr_name}' not in SET_ANYWHERE_PARAMS",
               tag="set_anywhere")
        return None
    srcs = _sources_for(draw_state, class_to_show)
    sources = srcs["sources"]
    target = _driving_source(srcs, attr_name)
    if target is None:
        notify(f"set_anywhere: no writable source for '{attr_name}'",
               tag="set_anywhere")
        return None

    live = (draw_state._kwargs or {}).get(attr_name, _UNSET)
    # (No write-time value cross-check here: mid-trip the live value
    # LEGITIMATELY differs from the source, so comparing now cries wolf on
    # every invalid set. Verification is deferred - see anywhere_value: 2
    # frames after the round trip lands, live matches what we set.)

    sources[target][attr_name] = value
    # Last-written source, by attr - lazily maintained (stamped here on every
    # set, and by the popover's lazy resolve on first open): cheap provenance
    # for display without a per-frame collection.
    _last = getattr(draw_state, "_sa_last_source", None)
    if _last is None:
        _last = {}
        draw_state._sa_last_source = _last
    _last[attr_name] = target
    # In-flight display cache (see anywhere_value): stamp what we set and
    # what the live value was when we set it. Re-sets during a drag refresh
    # the UI value but KEEP the original live_at_set - live hasn't moved yet,
    # and that's the baseline whose change means "the trip landed".
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
    # Deferred writer-side hotswap: code-backed sources only become LIVE via
    # recompile, and the source text only exists after the host's debounced
    # chain_out. Snapshot the current buffer identity; the per-frame tick
    # (anywhere_value → _anywhere_recompile_tick) starts the recompile when
    # the buffer moves and polls the runner until the hotswap lands.
    cm_state = getattr(draw_state, "_sa_cm_state", None)
    _t_kind = srcs["kinds"].get(target)
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


# ── draw_state.locate_<param> ────────────────────────────────────────────────
# The ds_header tint set's two-line pattern (read anywhere_value, write
# set_anywhere) as a plain attribute access on draw_state:
#
#     tint = draw_state.locate_tint          # framework-resolved value
#     draw_state.locate_tint = (1, 0, 1, 1)  # writes to the DRIVING source
#
# ASYMMETRIC on purpose: the GETTER is just the resolved value (_kwargs, with
# the in-flight set cache and the ds defaults anywhere_value already applies)
# - no source collection, no parse, cheap enough to read per frame. Only the
# SETTER runs the anywhere machinery: pick the driving source, write into its
# parse dict, and drive the deferred save/hotswap.
#
# Installed as real properties (one per SET_ANYWHERE_PARAMS entry) rather than
# a DrawState __getattr__/__setattr__ hook: __setattr__ would sit on EVERY
# draw_state attribute write, per view per frame. Properties cost nothing for
# the names that aren't ours.
LOCATE_PREFIX = "locate_"


def _locate_property(attr_name):
    def _get(self):
        return anywhere_value(attr_name, self)

    def _set(self, value):
        set_anywhere(attr_name, value, self)

    return property(_get, _set,
                    doc=f"'{attr_name}' as melty resolved it; assigning writes "
                        f"it back to whichever input source drives it.")


def install_locate_properties(cls=None):
    """Stamp `locate_<param>` onto DrawState for every SET_ANYWHERE_PARAMS
    entry. Runs at import (and again on every hotswap of this module, which
    re-installs against the live class), so growing SET_ANYWHERE_PARAMS is the
    only step needed to expose a new param."""
    if cls is None:
        from src.lsd.gl_gui.model.core_model.draw_state import DrawState
        cls = DrawState
    for attr_name in SET_ANYWHERE_PARAMS:
        setattr(cls, LOCATE_PREFIX + attr_name, _locate_property(attr_name))
    return cls


install_locate_properties()
