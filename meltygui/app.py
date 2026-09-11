"""melty apps: ``@glfw_window`` turns a draw function into an OS window.

    from melty import glfw_window, draw_text, pressed

    @glfw_window
    def editor():
        changed, new = draw_text(text)
        ...

The first decoration boots melty: the start-up shortcuts (warm_start.py),
glfw.init and a hidden owner window (the GL share group's root and the imgui
context that owns the font atlas) on the calling thread, and melty's heavy
imports on a background thread — the two overlap, as hdr-viewer measured.
Every decoration registers its function; the loop starts when the main
module's top level finishes (a trace hook on that frame's return — atexit
is too late: threading is already shut down), or explicitly with ``run()``.

Each window is a Surface (surface.py): frameless with melty's own title bar,
window controls, corner cut and shadow unless Toggles.Melty.wayland_show_frame
asks for the compositor's frame. Windows are peers: closing one closes it
alone, the loop ends when the last is gone. Child windows come from
``draw_something(glfw_window=True)`` inside a body (the render wrapper) and
follow their parent like nested melty windows.

Every launch appends a phase timing table to ~/.cache/<app_id>/startup.log;
MELTY_BENCH=1 also prints it and exits after the first frame.
"""
from __future__ import annotations

import os
import pathlib
import sys
import threading
import time

_T0 = float(os.environ.get('MELTY_T0') or time.time())
_MARKS = [('launcher exec', _T0), ('interpreter + stdlib', time.time())]
_ROOTS: list = []            # (fn, kwargs) in decoration order
_state = dict(booted=False, ran=False, app_id=None, cache=None, imports=None,
              import_error=None, switch_interval=None, failed=False)


def mark(label):
    _MARKS.append((label, time.time()))


def _write_startup_log(app_id, subject):
    lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_T0))}  {subject}  (pid {os.getpid()})",
             '     +ms    dms  phase']
    prev = _T0
    for label, t in sorted(_MARKS, key=lambda m: m[1]):
        lines.append(f'  {(t - _T0) * 1000:6.0f} {(t - prev) * 1000:6.0f}  {label}')
        prev = t
    lines.append(f'  total {(_MARKS[-1][1] - _T0) * 1000:.0f} ms')
    text = '\n'.join(lines) + '\n\n'
    path = _state['cache'] / 'startup.log'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as f:
            f.write(text)
    except OSError as e:
        print(f'{app_id}: cannot write {path}: {e}', file=sys.stderr)
    if os.environ.get('MELTY_BENCH'):
        print(text, end='', file=sys.stderr, flush=True)


# --- boot -------------------------------------------------------------------------
def _default_app_id():
    main = sys.modules.get('__main__')
    path = getattr(main, '__file__', None)
    return pathlib.Path(path).stem.replace('_', '-') if path else 'melty-app'


def _repo_root():
    return pathlib.Path(__file__).resolve().parents[3]


def boot(app_id=None):
    """Start melty: shortcuts, glfw, the owner window, the import thread.
    Idempotent; the first @glfw_window calls it."""
    if _state['booted']:
        return
    _state['booted'] = True
    _state['app_id'] = app_id or _default_app_id()
    cache = pathlib.Path(os.environ.get('XDG_CACHE_HOME') or pathlib.Path.home() / '.cache') / _state['app_id']
    _state['cache'] = cache
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault('GDK_BACKEND', 'wayland')
    from src.lsd.gl_gui import warm_start
    warm_start.prepare(cache)
    # While the import thread holds the GIL, each of pyGLFW's Python-side
    # steps waits up to a switch interval for it (5 ms default: window
    # creation went 80 -> 150 ms). Shorten it until the imports are done.
    _state['switch_interval'] = sys.getswitchinterval()
    sys.setswitchinterval(0.0002)
    thread = threading.Thread(target=_run_imports, name='melty-imports', daemon=True)
    thread.start()
    _state['imports'] = thread
    import glfw
    mark('glfw imported')
    # libdecor loads its C plugin at init (~60 ms) and the frameless window
    # then shows it: melty's frame hint (Toggles.Melty.wayland_native_frame)
    # disables it and records that for titlebar.py, whose chrome only runs
    # on the native frame. MELTY_LIBDECOR=1 keeps libdecor (compositors
    # without xdg-decoration), which also means the compositor's frame.
    if os.environ.get('MELTY_LIBDECOR'):
        sys._lsd_wayland_libdecor_disabled = False
    from src.lsd.gl_gui.utils.glfw_utils import apply_wayland_frame_hint
    apply_wayland_frame_hint()
    if not glfw.init():
        raise SystemExit('glfw.init failed')
    mark('glfw.init')
    warm_start.remember_glfw_library(cache)
    glfw.window_hint(glfw.VISIBLE, False)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 4)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    owner = glfw.create_window(1, 1, 'melty owner', None, None)
    if not owner:
        raise SystemExit('glfw.create_window (owner) failed')
    glfw.default_window_hints()
    _state['owner'] = owner
    mark('owner window created')


def _run_imports():
    try:
        import numpy  # noqa: F401
        import imgui  # noqa: F401
        import OpenGL.GL  # noqa: F401
        mark('imgui/numpy/GL imported (bg)')
        from src.lsd.gl_gui import melty  # noqa: F401
        from src.lsd.gl_gui import surface  # noqa: F401
        from src.lsd.gl_gui.view.core_views import text_editor, texture_view  # noqa: F401
        mark('melty imported (bg)')
    except BaseException as e:  # re-raised on the main thread
        _state['import_error'] = e


def _wait_imports():
    thread = _state['imports']
    if thread is not None:
        thread.join()
        _state['imports'] = None
        if _state['switch_interval'] is not None:
            sys.setswitchinterval(_state['switch_interval'])
    if _state['import_error'] is not None:
        raise _state['import_error']


def _init_melty():
    """Once, after the imports: the owner imgui context with the font atlas,
    the global style manager, melty's flags."""
    import glfw
    import imgui
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.fonts import FontManager
    from src.lsd.gl_gui.surface import Surface
    from src.lsd.gl_gui.toggles import Toggles
    from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager
    from src.lsd.gl_gui import warm_start
    owner = _state['owner']
    glfw.make_context_current(owner)
    Surface.owner_window = owner
    Surface.owner_context = imgui.create_context()
    imgui.get_io().ini_file_name = None
    imgui.get_io().display_size = (1.0, 1.0)     # the atlas hinting pass frames on it
    Melty.glfw_window = owner
    Melty.font_mgr = FontManager(imgui.get_io(), Melty.resolve_ui_scale())
    Melty.font_mgr.prewarm()
    warm_start.cache_hinted_atlas(Melty.font_mgr, _state['cache'])
    mark('fonts loaded')
    Melty.style_manager = ImGuiStyleManager()
    Melty.global_attrs['style_manager'] = Melty.style_manager
    if Melty.draw_state_registry is None:
        Melty.draw_state_registry = {}
    # melty boots in "annotation mode" (view calls return carriers, nothing
    # renders) until the studio's Melty.init() clears it. That init also
    # starts file watchers and a jedi worker we do not need.
    Melty.annotation_mode = False
    Toggles.show_fps = False
    if os.environ.get('MELTY_NO_OS_FRAME'):
        Toggles.Melty.push_os_window_edges = False
    Surface.app_id = _state['app_id']
    from src.lsd.gl_gui import geometry_feed
    geometry_feed.start()          # for rects: the size fit, child placement, os_frame
    mark('melty configured')


# --- the decorator ------------------------------------------------------------------
def glfw_window(fn=None, *, name=None, title=None, size=(1280, 800), app_id=None):
    """Register ``fn`` as an OS window. ``fn()`` draws the window's content
    each frame; views it draws at root level fill the window."""
    def wrap(fn):
        boot(app_id)
        _ROOTS.append((fn, dict(name=name or fn.__name__, title=title, size=size)))
        if not _state.get('hooked'):
            _state['hooked'] = True
            _hook_main_return()
        return fn
    return wrap(fn) if fn is not None else wrap


def _root_body(fn, name):
    """The window's body. A plain function draws inline into the surface
    root (its filling view sizes itself to the window). A RENDER FUNC (the
    @window playgrounds: `@glfw_window` over `@render_func`, the direct
    swap) is drawn as the window's root view the way the studio draws a
    @window: a melty view filling the window, with the background and
    layout context its children (draw_rows, draw_any, fields) expect —
    minus the closable chrome, which the OS window provides."""
    if not hasattr(fn, '__render_func__'):
        return lambda surface: fn()

    def body(surface):
        from src.lsd.gl_gui.melty import Melty
        width, height, top = Melty.root_fill
        # A closable melty window (the studio's Mode.MODE_WINDOW), sized to
        # the surface: layouts (draw_rows / draw_columns) register their
        # edges on the enclosing WINDOW, so the root must be one. Its own
        # chrome is off - the OS window has its title bar.
        fn(None, name=name, closable=True, draggable=False, window_pos=(0, top),
           width=width, height=height, auto_resize=False, show_header=False,
           with_footer=None, with_header_end=None, shadow=False, show_bg=True,
           selectable=False, use_cache=True, disable_scroll=True, indent_size=5,
           initial={'width': width, 'height': height, 'window_pos': (0, top)})
    return body


def _hook_main_return():
    """Start the loop when the main module's top level returns, so a script
    is just decorated functions. A local trace function on that frame sees
    its 'return' event (tracing has to be enabled globally for local trace
    functions to fire; the global one declines every other frame, and line
    events are off, so the cost is one call per function call until the
    module ends). Declines when a debugger already traces; ``run()`` then
    has to be called explicitly."""
    if sys.gettrace() is not None:
        return
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get('__name__') != '__main__':
        frame = frame.f_back
    if frame is None or frame.f_code.co_name != '<module>':
        return
    state = {'failed': False}

    def local(fr, event, arg):
        if event == 'exception':
            state['failed'] = True
        elif event == 'return':
            sys.settrace(None)
            fr.f_trace = None
            if not state['failed']:
                run()
        return local

    sys.settrace(lambda fr, event, arg: None)
    frame.f_trace_lines = False
    frame.f_trace = local


# --- the loop -----------------------------------------------------------------------
def run():
    """Open every registered window and run until the last one closes."""
    if _state['ran']:
        return
    _state['ran'] = True
    if not _state['booted']:
        boot()
    import glfw
    _wait_imports()
    _init_melty()
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.surface import Surface
    for fn, kw in _ROOTS:
        Surface(kw['name'], _root_body(fn, kw['name']), title=kw['title'] or kw['name'], size=kw['size'])
    mark(f'{len(Surface.all)} window(s) created')
    from src.lsd.gl_gui.utils import glfw_utils
    bench = os.environ.get('MELTY_BENCH')
    first = True
    frames = 0
    glfw_utils.request_render()
    try:
        while Surface.all:
            glfw.poll_events()
            _open_requested_children()
            # A frame only on a request (request_render: the input layer's
            # callbacks, Surface's focus/resize/close hooks, animations via
            # frames_left, post-render rendering, a window / white flag), as in
            # the studio's loop; an idle app blocks in wait_events. The flag
            # is cleared BEFORE the frame so a request made mid-frame
            # remains for the next iteration. app_tick advances only with a
            # frame: a child not drawn in a TICK closes (_close_stale_children).
            if glfw_utils._needs_render.is_set():
                glfw_utils._needs_render.clear()
                Melty.app_tick += 1
                frames += 1
                for surface in list(Surface.all):
                    try:
                        surface.frame()
                    except Exception:
                        _state['failed'] = True
                        raise
                _close_stale_children()
            for surface in list(Surface.all):
                _present_children(surface)
            for surface in list(Surface.all):
                if surface.closed:
                    _note_closed(surface)
                    surface.destroy()
            if first and Surface.all:
                first = False
                mark('first frame presented')
                _write_startup_log(_state['app_id'], ' '.join(s.name for s in Surface.all))
                if bench:
                    break
            if glfw_utils._needs_render.is_set():
                continue
            # glfw.wait_events also returns for events not asked for (on
            # Hyprland the NVIDIA EGL driver's per-swap wl_buffer teardown is
            # posted on the event queue), hence the gate above. Children
            # follow their parent through the geometry feed, a child that
            # requests nothing: poll while any exist. The idle wait is
            # bounded so a signal (Ctrl+C) gets a turn: Python runs its
            # handler between bytecodes, never inside a blocked OS call.
            glfw.wait_events_timeout(1 / 60 if any(s.children for s in Surface.all) else 1.0)
    finally:
        _debug(f'{frames} frames rendered')
        for surface in list(Surface.all):
            surface.destroy()
        glfw.terminate()


def _open_requested_children():
    """Child surfaces the render wrapper asked for (glfw_window=True) since
    the last tick: Melty.surface_requests, filled by surface_window_request."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.surface import Surface
    requests = Melty.surface_requests
    while requests:
        req = requests.pop(0)
        parent = req.parent_surface
        if parent is None or parent not in Surface.all or req.surface is not None:
            continue
        ds = req.draw_state
        size = req.window_size
        child = Surface(req.name, _child_body(req), title=req.name.split('##')[0], size=size,
                        parent=parent, draw_state=ds)
        child.request = req
        req.surface = child
        _debug(f'child surface {child.title!r} of {parent.title!r} size={size}')


def _child_body(req):
    def body(surface):
        from src.lsd.gl_gui.melty import Melty
        Melty.draw_surface_root(req, surface)
    return body


def _close_stale_children():
    """Immediate mode: a child whose glfw_window=True call was not made
    this tick closes (its parent stopped drawing it); the next call
    reopens it."""
    from src.lsd.gl_gui.melty import Melty
    for req in list(Melty.surface_windows.values()):
        child = req.surface
        if child is not None and req.tick != Melty.app_tick:
            child.closed = True
            child.stale = True


def _note_closed(surface):
    """A surface on its way out. An OS close of a child (the title bar's
    X, the compositor) leaves its request CLOSED: the parent's calls
    return (False, None) from then on, as a closed melty window's do. A
    stale child (not drawn this tick) just drops its surface."""
    req = surface.request
    if req is not None:
        req.surface = None
        if not getattr(surface, 'stale', False):
            req.closed = True
            if req.draw_state is not None:
                req.draw_state.closed = True
    for child in list(surface.children):
        _note_closed(child)


ACK_TIMEOUT_S = 0.5


def _debug(msg):
    if os.environ.get('MELTY_DEBUG'):
        print(f'[app] {msg}', flush=True)


def _present_children(parent):
    """Children follow their parent exactly as nested melty windows do:
    the request's window_pos is the parent-relative offset. Each tick the
    child's target rect = the parent's screen rect (geometry feed) + the
    offset, sent to the compositor when it differs from what was last
    sent (Hyprland). A child the USER dragged — its feed rect moved while
    the parent's did not, once our own last placement was acknowledged —
    adopts the new offset, unless window_pos= pinned it. GNOME: rects are
    read but never sent."""
    if not parent.children:
        return
    import glfw
    from src.lsd.gl_gui import geometry_feed, titlebar
    prect = geometry_feed.surface_rect(parent.title)
    if prect is None:
        return
    now = time.monotonic()
    parent_moved = parent.seen_rect is not None and tuple(prect[:2]) != tuple(parent.seen_rect[:2])
    parent.seen_rect = prect
    for child in list(parent.children):
        req = child.request
        if req is None or child.window is None:
            continue
        crect = geometry_feed.surface_rect(child.title)
        child_moved = (crect is not None and child.seen_rect is not None
                       and tuple(crect[:2]) != tuple(child.seen_rect[:2]))
        child.seen_rect = crect
        if child.await_ack and child.last_sent_rect is not None:
            if (crect is not None and tuple(crect[:2]) == tuple(child.last_sent_rect[:2])) \
                    or now - child.sent_at > ACK_TIMEOUT_S:
                child.await_ack = False
                child_moved = False          # that was our placement landing
        if (child_moved and not parent_moved and not req.pinned and not child.await_ack
                and child.last_sent_rect is not None):
            req.window_pos = (crect[0] - prect[0], crect[1] - prect[1])
            _debug(f'child {child.title!r} dragged: offset now {req.window_pos}')
        pos = req.window_pos
        width, height = glfw.get_window_size(child.window)
        if geometry_feed.hypr_honors_geometry():
            # The box is the CONTENT: the surface less the shadow inset.
            child.activate()
            inset = int(titlebar.window_inset())
            width, height = max(1, width - 2 * inset), max(1, height - 2 * inset)
        target = (int(prect[0] + pos[0]), int(prect[1] + pos[1]), int(width), int(height))
        if target != child.last_sent_rect:
            if crect is not None and tuple(crect) == target:
                child.last_sent_rect = target       # already there
            elif geometry_feed.place_window(child.title, target):
                _debug(f'place {child.title!r} at {target} (parent {prect[:2]} + {pos})')
                child.last_sent_rect = target
                child.await_ack = True
                child.sent_at = now


# Window input -----------------------------------------------------------------------------
_MODS = {'ctrl': 2, 'control': 2, 'shift': 1, 'alt': 4, 'super': 8, 'meta': 8}


def pressed(combo):
    """Edge-triggered ``'ctrl+s'``-style check against this frame's key
    events of the active window (GLFW press + repeat, so a held chord
    repeats). Modifiers must match exactly."""
    import glfw
    from src.lsd.gl_gui.melty import Melty
    parts = [p.strip().lower() for p in combo.split('+') if p.strip()]
    mods = 0
    key = None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        else:
            key = getattr(glfw, 'KEY_' + p.upper(), None)
            if key is None:
                raise ValueError(f'unknown key in {combo!r}: {p}')
    mask = 0xF
    return any(k == key and (m & mask) == mods for k, m in Melty.frame_key_events)


def content_size():
    """The (width, height) a root-level view fills in the active window."""
    from src.lsd.gl_gui.melty import Melty
    return Melty.root_fill
