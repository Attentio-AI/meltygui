"""Directory subscriptions and browser runtime diagnostics."""
import os
import sys
from meltygui.core.melty import Melty, FileWatch

# ── the directory watch ─────────────────────────────────────────────────────
# directory (str) -> the listing draw_states showing it. One FileWatch emitter per
# dir in this map; a listing moves its emitter on navigation (watch_directory)
# and the observer-thread listener posts an invalidate to the render thread.
def _existing_directory_watchers():
    # One-time resource transfer when this module is first loaded by hotswap.
    # Existing FileWatch callbacks and per-view state retain this same mapping.
    previous_module = sys.modules.get("meltygui.files.fast_file_explorer")
    if previous_module is not None:
        return vars(previous_module).get("_WATCHERS", {})
    return {}


_WATCHERS = globals().get("_WATCHERS")
if _WATCHERS is None:
    _WATCHERS = _existing_directory_watchers()


def _on_file_event(src_path):
    """FileWatch global listener (observer thread): an entry of a watched
    directory changed — created, modified, moved, deleted — repaint the
    listings showing that directory. Bumps nothing else; the listing's
    mtime memo notices what changed."""
    directory = os.path.dirname(src_path)
    watchers = _WATCHERS.get(directory) or _WATCHERS.get(src_path)
    if not watchers:
        return

    def repaint(draw_states=tuple(watchers)):
        for draw_state in draw_states:
            draw_state.invalidate()
    Melty.post_to_render(repaint)


def watch_directory(draw_state, state, dir_key):
    """Point this listing's emitter at `dir_key`: the previous directory's
    emitter is retired when no other listing shows it (the inotify instance
    cap is per user), the new one scheduled through FileWatch.watch_dir."""
    if state._watched == dir_key:
        return
    if _on_file_event not in FileWatch.global_listeners:
        # Hotswap-safe: an older copy of this function is replaced by itself.
        FileWatch.global_listeners[:] = [f for f in FileWatch.global_listeners
                                         if getattr(f, "__name__", "") != "_on_file_event"]
        FileWatch.global_listeners.append(_on_file_event)
    FileWatch.start()
    previous = state._watched
    if previous is not None:
        holders = _WATCHERS.get(previous)
        if holders is not None:
            holders.discard(draw_state)
            if not holders:
                del _WATCHERS[previous]
                FileWatch.unwatch_dir(previous)
    _WATCHERS.setdefault(dir_key, set()).add(draw_state)
    FileWatch.watch_dir(dir_key)
    state._watched = dir_key



from weakref import WeakKeyDictionary
_browser_size_traces = globals().get("_browser_size_traces", WeakKeyDictionary())

def _trace_browser_size(stage, draw_state, **details):
    """File browser size diagnostics (the nested-OS-window resize glitch):
    one line per CHANGE of the view's box / content rect / the surface's
    GLFW window and framebuffer size, to the bounded resize trace
    (.melty cache root, resize-<pid>.log) and stderr. Never raises."""
    try:
        import sys
        import meltygui.core.diagnostics.resize_trace as resize_trace
        from meltygui import window_api as glfw
        window = getattr(Melty, "glfw_window", None)
        window_size = fb_size = None
        if window is not None:
            window_size = tuple(glfw.get_window_size(window))
            fb_size = tuple(glfw.get_framebuffer_size(window))
        try:
            import meltygui.core.windowing.geometry_feed as geometry_feed
            frame = geometry_feed._current_frame()
            details["feed"] = (geometry_feed.backend(), geometry_feed._hypr_selector(),
                               geometry_feed.hypr_honors_geometry(),
                               None if frame is None else (frame.get("at"), frame.get("size")))
        except Exception as error:
            details["feed"] = f"error {error!r}"
        stamp = (draw_state.width, draw_state.height, draw_state.abs_left, draw_state.abs_top,
                 window_size, fb_size, tuple(sorted(details.items())))
        if _browser_size_traces.get(draw_state) == stamp:
            return
        _browser_size_traces[draw_state] = stamp
        resize_trace.record(stage, draw_state, window_size=window_size, fb_size=fb_size,
                            gesture=bool(Melty.resize_gesture_live()), **details)
        print(f"[{stage}] f{Melty.frame_count} view {draw_state.width}x{draw_state.height} "
              f"at ({draw_state.abs_left}, {draw_state.abs_top}) window {window_size} "
              f"fb {fb_size} {details}", file=sys.stderr, flush=True)
    except Exception:
        pass


