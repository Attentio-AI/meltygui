"""Typed MCP queries over live Melty state (the render thread's view of it).

The tools in mcp_server.py (`find_views`, `describe_view`, `hit_test`,
`param_sources`, `tile_cache`) each call one `collect_*` function here through
`mcp_eval.request_call`, so the walk runs ON the render thread between frames
(draw_states, the BVH, the input handler's per-frame tables and the tile cache
are all render-thread state) and comes back as a JSON-serializable dict.
Every result carries `frame` — `Melty.frame_count` when it was read.

Views are addressed by `draw_state._tile_id` (the cache key AND the event
view_id, `"<name>##<hash>"`), matched exact → unique substring → `ds.id`
prefix. Plain functions, no imgui: a headless test renders a view through the
wrapper and calls the collectors directly (tests/test_mcp_query.py).
"""
import inspect
import json


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _melty():
    from src.lsd.gl_gui.melty import Melty
    return Melty


def _short(value, limit=120):
    """One-line repr for kwargs / values, bounded (a tensor repr can be MBs)."""
    try:
        import torch
        if isinstance(value, torch.Tensor):
            return f"Tensor{tuple(value.shape)} {value.dtype} {value.device}"
    except Exception:
        pass
    try:
        text = repr(value)
    except Exception as exc:
        text = f"<repr failed: {type(exc).__name__}>"
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _func_info(func):
    """(qualname, "file:line") of a render func, unwrapped past @render_func."""
    if func is None:
        return None, None
    try:
        raw = inspect.unwrap(func)
    except Exception:
        raw = func
    name = getattr(raw, "__qualname__", None) or getattr(raw, "__name__", None) or repr(raw)
    where = None
    code = getattr(raw, "__code__", None)
    if code is not None:
        where = f"{code.co_filename}:{code.co_firstlineno}"
    return name, where


def _rect(ds):
    """Live [left, top, width, height]; the cached abs_* when the live
    accessors aren't available (test stubs)."""
    try:
        left, top = ds._abs_left(), ds._abs_top()
    except Exception:
        left, top = getattr(ds, "abs_left", 0.0), getattr(ds, "abs_top", 0.0)
    return [round(float(left), 1), round(float(top), 1),
            round(float(getattr(ds, "width", 0.0) or 0.0), 1),
            round(float(getattr(ds, "height", 0.0) or 0.0), 1)]


def _rect4(rect):
    if rect is None:
        return None
    try:
        return [round(float(v), 1) for v in rect]
    except Exception:
        return None


def _window_name(ds):
    """The title list_windows shows for a root window (ManagedWindow.name),
    else the draw_state's own name / tile_id prefix (nested windows)."""
    if ds is None:
        return None
    Melty = _melty()
    tile_id = getattr(ds, "_tile_id", None)
    managed = Melty.registered_windows.get(tile_id) if tile_id is not None else None
    if managed is not None and getattr(managed, "name", None):
        return managed.name
    name = getattr(ds, "name", None)
    if name:
        return name
    return str(tile_id).split("##")[0] if tile_id else None


def _all_views():
    """Every draw_state Melty knows about right now: BVH-indexed views, the
    root windows and the paint-ordered window list, deduped by identity."""
    Melty = _melty()
    seen = {}
    for ds in list(Melty._bvh_id_to_ds.values()):
        seen.setdefault(id(ds), ds)
    for managed in list(Melty.registered_windows.values()):
        ds = getattr(managed, "draw_state", None)
        if ds is not None:
            seen.setdefault(id(ds), ds)
    for ds in list(Melty.paint_ordered_ds):
        seen.setdefault(id(ds), ds)
    return list(seen.values())


def resolve_view(ref):
    """The draw_state a tool argument names: exact _tile_id, else the unique
    tile_id substring, else a ds.id prefix. Returns (ds, error_text)."""
    ref = (ref or "").strip()
    if not ref:
        return None, "no view given — pass a _tile_id from find_views / hit_test"
    views = _all_views()
    for ds in views:
        if getattr(ds, "_tile_id", None) == ref:
            return ds, None
    lowered = ref.lower()
    partial = [ds for ds in views
               if lowered in str(getattr(ds, "_tile_id", "")).lower()]
    if len(partial) == 1:
        return partial[0], None
    by_id = [ds for ds in views if str(getattr(ds, "id", "")).startswith(ref)]
    if len(by_id) == 1:
        return by_id[0], None
    candidates = partial or by_id
    if candidates:
        names = sorted(str(getattr(ds, "_tile_id", ds.id)) for ds in candidates)[:12]
        return None, f"'{ref}' matches {len(candidates)} views: " + ", ".join(names)
    return None, f"no view matches '{ref}'"


def _tile_state(ds):
    """The tile-cache facts of one view, or None when it has no tile."""
    Melty = _melty()
    cache = Melty.cache
    key = getattr(ds, "_tile_id", None)
    if cache is None or key is None:
        return None
    tile = cache._tiles.get(key)
    if tile is None:
        return None
    tracker_note = None
    try:
        from src.lsd.gl_gui.view.invalidation_tracker import InvalidateTracker
        note = InvalidateTracker.invalidations.get(key)
        if note is not None:
            tracker_note = {"name": note.name, "reason": note.reason, "frame": note.frame}
    except Exception:
        pass
    return {
        "dirty": bool(tile.dirty),
        "force_invalidate": bool(tile.force_invalidate),
        "last_clean_cache_frame": tile.last_clean_frame,
        "last_invalidated_cache_frame": tile.last_invalidated_frame,
        "cache_frame": cache._frame_id,
        "size": list(tile.size) if tile.size else None,
        "alloc_size": list(tile.alloc_size) if tile.alloc_size else None,
        "last_bump": getattr(tile, "_last_bump", None),
        "blit_served_frame": getattr(ds, "_blit_served_frame", None),
        "did_use_cache": getattr(ds, "_did_use_cache", None),
        "tracker_note": tracker_note,
        "parent_key": cache.key_to_parent_key.get(key),
        "child_count": len(cache.parent_key_to_child_keys.get(key, ())),
    }


def view_summary(ds):
    """The one-row description of a view (find_views rows, hit_test stack
    entries, parent / child chains)."""
    Melty = _melty()
    func_name, func_where = _func_info(getattr(ds, "_view_func", None))
    parent_window = getattr(ds, "parent_window", None)
    try:
        root_window = ds.root_window
    except Exception:
        root_window = None
    try:
        clip = _rect4(ds.abs_clip_rect)
    except Exception:
        clip = None
    try:
        abs_closed = bool(ds.abs_closed)
    except Exception:
        abs_closed = None
    try:
        abs_layer = ds.abs_layer
    except Exception:
        abs_layer = None
    raw_input = getattr(ds, "_raw_input_value", None)
    return {
        "tile_id": getattr(ds, "_tile_id", None),
        "id": getattr(ds, "id", None),
        "name": getattr(ds, "name", None),
        "func": func_name,
        "func_where": func_where,
        "rect": _rect(ds),
        "clip_rect": clip,
        "closed": bool(getattr(ds, "closed", False)),
        "abs_closed": abs_closed,
        "hidden_offscreen": bool(getattr(ds, "_hidden_offscreen", False)),
        "closable": bool(getattr(ds, "closable", False)),
        "nested_window": bool(getattr(ds, "nested_window", False)),
        "layer": getattr(ds, "layer", None),
        "abs_layer": abs_layer,
        "z_pos": getattr(ds, "z_pos", None),
        "depth": getattr(ds, "depth", None),
        "hovered": id(ds) in Melty.bvh_hover_ids,
        "bounding_hovered": bool(getattr(ds, "_bounding_hovered", False)),
        "selected": bool(getattr(ds, "selected", False)),
        "parent_window": getattr(parent_window, "_tile_id", None),
        "window": _window_name(root_window),
        "input_type": type(raw_input).__name__ if raw_input is not None else None,
    }


# ----------------------------------------------------------------------------
# 1. find_views / describe_view
# ----------------------------------------------------------------------------

def collect_find_views(func="", name="", window="", include_closed=False, limit=50):
    """Views matching the case-insensitive substring filters, front-most
    first (z_pos descending). `window` filters on the ROOT window's title."""
    Melty = _melty()
    func = (func or "").lower()
    name = (name or "").lower()
    window = (window or "").lower()
    rows = []
    for ds in _all_views():
        if not include_closed:
            try:
                if ds.closed or ds.abs_closed:
                    continue
            except Exception:
                pass
        row = view_summary(ds)
        if func and func not in str(row["func"] or "").lower():
            continue
        if name and name not in f"{row['name'] or ''} {row['tile_id'] or ''}".lower():
            continue
        if window and window not in str(row["window"] or "").lower():
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["z_pos"] or 0), reverse=True)
    total = len(rows)
    rows = rows[:max(1, int(limit))]
    return {"frame": Melty.frame_count, "total": total, "returned": len(rows), "views": rows}


def _kwargs_summary(ds):
    kwargs = getattr(ds, "_kwargs", None) or {}
    skip = {"next_kwargs", "draw_state", "o_kwargs", "input_value", "style_manager"}
    return {str(k): _short(v) for k, v in kwargs.items()
            if k not in skip and not str(k).startswith("_")}


def _parent_chain(ds, max_hops=32):
    """Render-tree ancestors, nearest first (the root's _parent is itself)."""
    chain = []
    seen = {id(ds)}
    node = getattr(ds, "_parent", None)
    while node is not None and id(node) not in seen and len(chain) < max_hops:
        seen.add(id(node))
        chain.append(view_summary(node))
        node = getattr(node, "_parent", None)
    return chain


def _window_chain(ds, max_hops=32):
    chain = []
    seen = {id(ds)}
    node = getattr(ds, "parent_window", None)
    while node is not None and id(node) not in seen and len(chain) < max_hops:
        seen.add(id(node))
        chain.append(view_summary(node))
        node = getattr(node, "parent_window", None)
    return chain


def _children(ds, depth, budget):
    """Render-tree children (`_view_children`, this frame's, else `_children`)
    as summaries, recursing `depth` levels within a total `budget` of rows."""
    if depth <= 0 or budget[0] <= 0:
        return []
    kids = list((getattr(ds, "_view_children", None) or {}).values())
    if not kids:
        kids = list((getattr(ds, "_children", None) or {}).values())
    out = []
    for kid in kids:
        if budget[0] <= 0:
            break
        if not hasattr(kid, "_tile_id"):
            continue
        budget[0] -= 1
        row = view_summary(kid)
        row["children"] = _children(kid, depth - 1, budget)
        out.append(row)
    return out


def _subscriptions_for(tile_id):
    """This frame's event registrations (view_id, priority, [event names],
    cursor) whose view_id belongs to `tile_id` — from the input handler's
    per-frame hovered table, so only views under the real pointer have any."""
    Melty = _melty()
    handler = getattr(Melty, "event_handler", None)
    if handler is None:
        return []
    from src.lsd.gl_gui.events import input_handler as IH
    subs = []
    for view_id, priority, keys in list(getattr(handler, "_hovered", [])):
        owner = IH._view_id_to_tile_id.get(view_id, view_id)
        if owner != tile_id and view_id != tile_id:
            continue
        names = IH._view_id_names_cache.get(view_id, {})
        flags = IH._view_id_flags_cache.get(view_id, {})
        events = []
        for key in sorted(keys):
            entry = {"event": names.get(key, f"{key[0]}:{key[1]}")}
            inverted, non_blocking = flags.get(key, (False, False))
            if inverted:
                entry["inverted"] = True
            if non_blocking:
                entry["non_blocking"] = True
            events.append(entry)
        cursor = getattr(handler, "_view_cursor", {}).get(view_id)
        subs.append({
            "view_id": view_id,
            "priority": priority,
            "blocker": view_id in getattr(handler, "_blocker_views", ()),
            "events": events,
            "cursor": (str(cursor[0]) if cursor else None),
            "cursor_rect": (_rect4(cursor[1]) if cursor and cursor[1] else None),
        })
    subs.sort(key=lambda s: s["priority"])
    return subs


def collect_describe_view(view, children_depth=1, max_children=60):
    """Everything about one view: summary, kwargs, auto_params, event rects,
    this frame's subscriptions, the tile-cache entry, the render-tree and
    window ancestor chains and the children subtree."""
    Melty = _melty()
    ds, error = resolve_view(view)
    if ds is None:
        return {"frame": Melty.frame_count, "error": error}
    out = {"frame": Melty.frame_count, "view": view_summary(ds)}
    out["kwargs"] = _kwargs_summary(ds)
    auto_params = getattr(ds, "auto_params", None) or {}
    out["auto_params"] = {str(k): _short(v) for k, v in auto_params.items()}
    event_rects = getattr(ds, "_event_rects", None) or {}
    out["event_rects"] = {str(k): [_rect4(r) for r in rects] for k, rects in event_rects.items()}
    out["subscriptions"] = _subscriptions_for(ds._tile_id)
    out["tile"] = _tile_state(ds)
    out["window_pos"] = _rect4(getattr(ds, "window_pos", None))
    out["content_size"] = [getattr(ds, "content_width", None), getattr(ds, "content_height", None)]
    out["header_height"] = getattr(ds, "header_height", None)
    out["scroll"] = [getattr(ds, "scroll_offset", None), getattr(ds, "scroll_offset_x", None)]
    out["parents"] = _parent_chain(ds)
    out["windows"] = _window_chain(ds)
    out["children"] = _children(ds, int(children_depth), [int(max_children)])
    return out


# ----------------------------------------------------------------------------
# 2. hit_test
# ----------------------------------------------------------------------------

def collect_hit_test(x, y):
    """The BVH stack at (x, y), front to back (bvh_query's order: z_pos),
    each with its priority and — for the frame's real pointer position — the
    event subscriptions and cursor registrations the input handler holds.
    Subscriptions are only registered for views under the ACTUAL pointer,
    so `pointer` says where they apply; at any other (x, y) the stack is
    still exact but `subscriptions` are empty."""
    Melty = _melty()
    x, y = float(x), float(y)
    try:
        stack = list(Melty.bvh_query(x, y))
    except Exception as exc:
        return {"frame": Melty.frame_count, "error": f"bvh_query failed: {exc!r}"}
    handler = getattr(Melty, "event_handler", None)
    pointer = None
    if handler is not None:
        pointer = [getattr(handler, "_cursor_x", None), getattr(handler, "_cursor_y", None)]
    rows = []
    for ds in stack:
        row = view_summary(ds)
        # The stored z_pos → the priority an on_action from this frame
        # started with (the `priority` property reads the frame's LIVE
        # paint state, meaningless between frames).
        try:
            row["priority"] = ds._action_base_priority(getattr(ds, "z_pos", None) or 0)
        except Exception:
            row["priority"] = None
        row["subscriptions"] = _subscriptions_for(ds._tile_id)
        rows.append(row)
    hovered = getattr(Melty, "hovered_ds", None)
    out = {
        "frame": Melty.frame_count,
        "point": [x, y],
        "pointer": pointer,
        "pointer_matches_point": (pointer is not None and pointer[0] is not None
                                  and abs(pointer[0] - x) < 0.5 and abs(pointer[1] - y) < 0.5),
        "hovered_ds": getattr(hovered, "_tile_id", None),
        "cursor_shape": str(getattr(handler, "cursor_shape", None)) if handler else None,
        "drag_capture": ({str(k): [str(v[0]), str(v[1])] for k, v in
                          getattr(handler, "_drag_capture", {}).items()} if handler else {}),
        "blockers": sorted(str(v) for v in getattr(handler, "_blocker_views", ())) if handler else [],
        "stack": rows,
    }
    return out


# ----------------------------------------------------------------------------
# 3. param_sources
# ----------------------------------------------------------------------------

def collect_param_sources(view, param=""):
    """The inputs tab as data: per parameter the value the view reads, the
    driving source (the SourcePriority pick), and every source that sets it
    in priority order."""
    Melty = _melty()
    ds, error = resolve_view(view)
    if ds is None:
        return {"frame": Melty.frame_count, "error": error}
    from src.lsd.gl_gui.view.core_views import anywhere as A
    from src.lsd.gl_gui.view.core_views.new_core_view import param_source_matrix
    try:
        srcs = A._sources_for(ds)
    except Exception as exc:
        return {"frame": Melty.frame_count, "view": view_summary(ds),
                "error": f"collect_input_sources failed: {exc!r}"}
    kinds = srcs.get("kinds", {})
    writable = set(srcs.get("writable", ()))
    locations = srcs.get("locations", {})
    matrix = param_source_matrix(srcs["sources"], func=getattr(ds, "_view_func", None),
                                 include_unmatched=True)
    if isinstance(matrix, tuple):
        matrix = matrix[1]
    wanted = (param or "").strip()
    params = {}
    for name, per_source in matrix.items():
        if wanted and name != wanted:
            continue
        setters = []
        for sname, value in per_source.items():
            setters.append({
                "source": sname,
                "kind": kinds.get(sname),
                "priority": A._source_priority(kinds.get(sname))[0],
                "writable": sname in writable,
                "unset": A._unset_value(value),
                "value": _short(value),
            })
        setters.sort(key=lambda s: (s["priority"], s["source"]))
        try:
            driving = A.get_source_for(name, ds)
        except Exception:
            driving = None
        try:
            value = _short(A.anywhere_value(name, ds))
        except Exception:
            value = None
        params[name] = {"value": value, "driving": driving, "setters": setters}
    # A source's dict is a placeholder `{}` until its code host has parsed
    # (background work over the next frames): report that instead of an
    # empty setter dict that reads as "nothing sets it".
    sources = [{"source": s, "kind": kinds.get(s), "writable": s in writable,
                "parsed": bool(srcs["sources"].get(s)),
                "location": (list(locations[s]) if locations.get(s) else None)}
               for s in sorted(srcs["sources"], key=lambda s: A._source_priority(kinds.get(s)))]
    pending = [s["source"] for s in sources if not s["parsed"] and s["kind"] != "draw state"]
    out = {"frame": Melty.frame_count, "view": view_summary(ds), "sources": sources,
           "params": params}
    if pending:
        out["pending_sources"] = pending
        out["note"] = "unparsed sources are still loading in the background — query again in a few frames"
    if wanted and wanted not in params:
        out["error"] = f"no param '{wanted}' on this view; params: {sorted(matrix)}"
    return out


# ----------------------------------------------------------------------------
# 4. tile_cache
# ----------------------------------------------------------------------------

def collect_tile_cache(view="", history_frames=0, limit=100):
    """Tile-cache state. With `view`: that tile's entry plus its invalidation
    history over the last `history_frames` frames. Without: totals, the
    per-frame body-run / cache-hit / capture counts over `history_frames`
    (newest last) and the most recent invalidations (`limit`)."""
    Melty = _melty()
    cache = Melty.cache
    if cache is None:
        return {"frame": Melty.frame_count, "error": "no tile cache"}
    now = Melty.frame_count
    since = now - int(history_frames)
    history = list(getattr(cache, "_invalidation_history", None) or ())
    out = {"frame": now, "cache_frame": cache._frame_id, "enabled": bool(getattr(cache, "enabled", True))}
    if view:
        ds, error = resolve_view(view)
        if ds is None:
            return {"frame": now, "error": error}
        out["view"] = view_summary(ds)
        out["tile"] = _tile_state(ds)
        key = ds._tile_id
        rows = [h for h in history if h[1] == key and (history_frames <= 0 or h[0] >= since)]
        out["invalidations"] = [
            {"frame": f, "name": n, "reason": r, "force": force} for (f, _k, n, r, force) in rows[-int(limit):]]
        return out
    tiles = cache._tiles
    dirty = sum(1 for t in tiles.values() if t.dirty)
    out["tiles"] = len(tiles)
    out["dirty_tiles"] = dirty
    out["this_frame"] = {"body_runs": cache._frame_body_runs, "cache_hits": cache._frame_cache_hits}
    stats = list(getattr(cache, "_frame_stats", None) or ())
    if history_frames > 0:
        stats = [s for s in stats if s[0] >= since]
    else:
        stats = stats[-10:]
    out["frames"] = [{"frame": f, "body_runs": runs, "cache_hits": hits, "captures": caps}
                     for (f, runs, hits, caps) in stats]
    rows = [h for h in history if history_frames <= 0 or h[0] >= since]
    out["invalidations"] = [
        {"frame": f, "key": k, "name": n, "reason": r, "force": force}
        for (f, k, n, r, force) in rows[-int(limit):]]
    # Who invalidates most over the window of the "per-frame invalidation" tell.
    counts = {}
    for (_f, k, n, _r, _force) in rows:
        counts[(k, n)] = counts.get((k, n), 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
    out["top_invalidators"] = [{"key": k, "name": n, "count": c} for ((k, n), c) in top]
    return out


# ----------------------------------------------------------------------------
# Tool plumbing
# ----------------------------------------------------------------------------

def run_query(collect, model_server, timeout=10.0):
    """Run a no-arg collector on the render thread and return JSON text."""
    from src.lsd.gl_gui.mcp_eval import request_call
    result, error = request_call(collect, model_server, timeout=timeout)
    if error:
        return json.dumps({"error": error}, indent=1)
    return json.dumps(result, indent=1, default=str)
