"""meltygui apps: ``@glfw_window`` turns a draw function into an OS window.

    from meltygui import glfw_window, draw_text, pressed

    @glfw_window
    def editor():
        changed, new = draw_text(text)
        ...

The first decoration boots meltygui: the start-up shortcuts (warm_start.py),
glfw.init and a hidden owner window (the GL share group's root and the imgui
context that owns the font atlas) on the calling thread, and meltygui's heavy
imports on a background thread — the two overlap, as hdr-viewer measured.
Every decoration registers its function; the loop starts when the main
module's top level finishes (a trace hook on that frame's return — atexit
is too late: threading is already shut down), or explicitly with ``run()``.

Each window is a Surface (surface.py): frameless with meltygui's own title bar,
window controls, corner cut and shadow unless Toggles.Melty.wayland_show_frame
asks for the compositor's frame. Windows are peers: closing one closes it
alone, the loop ends when the last is gone. Child windows come from
``draw_something(glfw_window=True)`` inside a body (the render wrapper) and
follow their parent like nested meltygui windows.

Every launch appends a phase timing table to ~/.cache/<app_id>/startup.log;
MELTY_BENCH=1 also prints it and exits after the first frame.
"""
from __future__ import annotations

import inspect
import os
import pathlib
import sys
import threading
import traceback
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
    return pathlib.Path(path).stem.replace('_', '-') if path else 'meltygui-app'


def boot(app_id=None):
    """Start meltygui: shortcuts, glfw, the owner window, the import thread.
    Idempotent; the first @glfw_window calls it."""
    if _state['booted']:
        if app_id and app_id != _state['app_id']:
            print(f"meltygui: app_id {app_id!r} ignored — already booted as {_state['app_id']!r} "
                  f"(the first boot names the session and cache directories)", file=sys.stderr)
        return
    _state['booted'] = True
    _state['app_id'] = app_id or _default_app_id()
    cache = pathlib.Path(os.environ.get('XDG_CACHE_HOME') or pathlib.Path.home() / '.cache') / _state['app_id']
    _state['cache'] = cache
    _register_editable(getattr(sys.modules.get('__main__'), '__file__', None))
    os.environ.setdefault('GDK_BACKEND', 'wayland')
    import meltygui.warm_start as warm_start
    warm_start.prepare(cache)
    import meltygui.window_api as glfw
    from meltygui.toggles import Toggles
    backend = glfw.select_backend(Toggles.windows.native_os_windows)
    if backend == 'wayland':
        sys._lsd_wayland_libdecor_disabled = True
    # While the import thread holds the GIL, each of pyGLFW's Python-side
    # steps waits up to a switch interval for it (5 ms default: window
    # creation went 80 -> 150 ms). Shorten it until the imports are done.
    _state['switch_interval'] = sys.getswitchinterval()
    sys.setswitchinterval(0.0002)
    thread = threading.Thread(target=_run_imports, name='meltygui-imports', daemon=True)
    thread.start()
    _state['imports'] = thread
    mark(f'{backend} window API ready')
    # libdecor loads its GTK plugin at init (~60 ms) and the frameless window
    # never shows it: meltygui's own hint (Toggles.Melty.wayland_native_frame)
    # disables it and records that for titlebar.py, whose chrome only runs
    # on the native frame. MELTY_LIBDECOR=1 keeps libdecor (compositors
    # without xdg-decoration), which also means the compositor's frame.
    if backend == 'glfw' and os.environ.get('MELTY_LIBDECOR'):
        sys._lsd_wayland_libdecor_disabled = False
    from meltygui.utils.glfw_utils import apply_wayland_frame_hint
    apply_wayland_frame_hint()
    if not glfw.init():
        raise SystemExit('glfw.init failed')
    mark(f'{backend}.init')
    if backend == 'glfw':
        warm_start.remember_glfw_library(cache)
    glfw.window_hint(glfw.VISIBLE, False)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 4)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    owner = glfw.create_window(1, 1, 'meltygui owner', None, None)
    if not owner:
        raise SystemExit('glfw.create_window (owner) failed')
    glfw.default_window_hints()
    _state['owner'] = owner
    mark('owner window created')
    # The first eglMakeCurrent loads driver state (~30 ms on NVIDIA). Do it
    # while the import worker is still running, before joining the worker.
    glfw.make_context_current(owner)
    mark('owner context current')


def _run_imports():
    try:
        # PyOpenGL only imports numpy during the first renderer call,
        # after the driver work could have overlapped it. No GL calls here.
        from OpenGL.arrays import numpymodule  # noqa: F401
        import meltygui_imgui as imgui  # noqa: F401
        import OpenGL.GL  # noqa: F401
        mark('imgui/numpy/GL imported (bg)')
        import meltygui.runtime as runtime  # noqa: F401
        import meltygui.surface as surface  # noqa: F401
        import meltygui.editor.text as text_editor
        import meltygui.views.texture_view as texture_view  # noqa: F401
        mark('meltygui imported (bg)')
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
    the global style manager, meltygui's flags."""
    import meltygui.window_api as glfw
    import meltygui_imgui as imgui
    from meltygui.runtime import Melty
    from meltygui.fonts import FontManager
    from meltygui.surface import Surface
    from meltygui.toggles import Toggles
    from meltygui.views.utils.imgui_style_manager_class import ImGuiStyleManager
    import meltygui.warm_start as warm_start
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
    # The app's persisted draw states (app_session.py): loaded on the
    # first Surface so every surface's `Melty.vis.root` is the session and
    # get_draw_state / note_window_seen / the window z-order read and write
    # the stores that run() saves on exit.
    session = _load_session()
    _register_projects()
    Melty.draw_state_registry = session.draw_state_registry
    Melty.adopt_registered_windows(session)
    Surface.session = session
    mark('session loaded')
    # meltygui boots in "annotation mode" (view calls return carriers, nothing
    # renders) until the studio's Melty.init() clears it. That init also
    # starts file watchers and a jedi worker we do not need.
    Melty.annotation_mode = False
    Toggles.show_fps = False
    if os.environ.get('MELTY_NO_OS_FRAME'):
        Toggles.Melty.push_os_window_edges = False
    Surface.app_id = _state['app_id']
    import meltygui.geometry_feed as geometry_feed
    geometry_feed.start()          # for rects: the size fit, child placement, os_frame
    mark('meltygui configured')


def _load_session():
    """The app's AppSession (app_session.load), read once: by the first
    `persisted` call or by _init_melty, whichever comes first. Needs the
    meltygui imports (the pickled classes), so it waits for them."""
    session = _state.get('session')
    if session is None:
        _wait_imports()
        import meltygui.app_session as app_session
        session = app_session.load(_state['app_id'])
        _state['session'] = session
        mark('session loaded')
    return session


def persisted(name, factory, *, app_id=None):
    """An object the app keeps between runs: last run's saved `name`, or a
    fresh `factory()` when there is none (or the saved one is not an
    instance of `factory`, a class). It rides the app's session
    (app_session.AppSession.app_state) and is saved with the draw states
    when the loop exits, so it must be a DictConversion — its public,
    non-@no_save fields persist, exactly as a studio model field does.

        open_files = meltygui.persisted('open_files', OpenFiles, app_id='meltygui-code-editor')

    `app_id` names the session file when this runs before the first
    `@glfw_window` (the usual place — the object feeds the window's body);
    the decorator's later `app_id` must match it."""
    boot(app_id)
    session = _load_session()
    saved = session.app_state.get(name)
    if saved is None or (isinstance(factory, type) and not isinstance(saved, factory)):
        saved = factory()
        session.app_state[name] = saved
    return saved


# --- the decorator ------------------------------------------------------------------
def _register_editable(file):
    """Make the project holding `file` editable source (address.add_editable_root):
    the app's own code loads into the studio's code hosts, so its decorators
    are input sources (the inputs tab, the header's tint chip, `locate_<param>`)
    and its files hotswap, exactly like the checkout's."""
    if not file:
        return
    from meltygui.code.address import add_editable_root
    add_editable_root(file)


def _register_projects():
    """Every folder marked as a project in the shared file-meta store
    (file_meta.mark_project — machine-wide, so a project marked in the
    studio or another app counts here) is editable source too, like the
    app's own tree. The store is a small pickle; read once at init."""
    try:
        from meltygui.extensions import source_folders as project_roots
        from meltygui.code.address import add_editable_root
        for root in project_roots():
            add_editable_root(root)
    except Exception:
        traceback.print_exc()


def glfw_window(fn=None, *, name=None, width=1280, height=800, app_id=None, on_close=None, **view_kwargs):
    """Register ``fn`` as an OS window. ``fn()`` draws the window's content
    each frame; views it draws at root level fill the window.

    ``name`` (default: the function's name) is the window's name AND its
    OS title — one per window; ``width`` / ``height`` its content size.
    ``on_close(surface)`` is asked when the window is told to close (the
    title bar's ×, the compositor, `glfw.set_window_should_close`): return
    False to keep it (hide it, say — a chat app with a turn streaming).
    These are the universal meltygui names (`@window`, every view's kwargs),
    never `title=` or `size=` (Lukas 09-12).

    Every other keyword argument is the root VIEW's, exactly as `@window`'s
    are the studio window's (`tint=`, `disable_scroll=`, `value=`,
    `with_header=`, `show_name=`, ...): a render-func body is drawn with
    them as the window's root view; a plain-function body runs under the
    `tint` (the style tint its filling view colours from). Like `@window`
    the decorator is an INPUT SOURCE of the view (`@glfw_window(<fn>)` in
    the inputs tab, read and written by `locate_<param>`): its file's
    project becomes editable source, and a hotswap that re-runs the
    decorator (an edited kwarg lands as a recompile of the def) updates
    the registered window's config IN PLACE — same name, same window,
    the new kwargs on the next frame — instead of registering a second
    root."""
    def wrap(fn):
        boot(app_id)
        try:
            source = inspect.getsourcefile(inspect.unwrap(fn))
        except TypeError:
            source = None
        _register_editable(source)
        config = dict(name=name or fn.__name__, width=int(width), height=int(height), on_close=on_close,
                      view_kwargs=view_kwargs)
        for index, (registered, existing) in enumerate(_ROOTS):
            if existing['name'] == config['name']:
                existing.update(config)        # the live config object: the body reads it
                if not _state['ran']:
                    _ROOTS[index] = (fn, existing)
                break
        else:
            _ROOTS.append((fn, config))
        if not _state.get('hooked'):
            _state['hooked'] = True
            _hook_main_return()
        return fn
    return wrap(fn) if fn is not None else wrap


def _root_body(fn, name, view_kwargs=None, config=None):
    """The window's body. A plain function draws inline into the surface
    root (its filling view sizes itself to the window). A RENDER FUNC (the
    @window playgrounds: `@glfw_window` over `@render_func`, the direct
    swap) is drawn as the window's root view the way the studio draws a
    @window: a meltygui view filling the window, with the background and
    layout context its children (draw_rows, draw_any, fields) expect —
    minus the closable chrome, which the OS window provides.

    ``config`` is the root's LIVE registration (the dict in _ROOTS): a
    render-func body reads its `view_kwargs` on every frame, so a
    re-decoration (a hotswapped `@glfw_window(tint=...)` edit) reaches the
    open window; without it `view_kwargs` is fixed."""
    def current_kwargs():
        source = config.get('view_kwargs') if config is not None else view_kwargs
        return dict(source or {})
    if hasattr(fn, '__render_func__'):
        return lambda surface: _draw_root(fn, name, **current_kwargs())
    if current_kwargs().get('tint') is None and config is None:
        return lambda surface: fn()

    def tinted(surface):
        # The body runs under the decorator's tint (what draw_bg and the
        # style colours read), and after - the same push/restore the
        # wrapper does around a tinted view. Read per frame: a live tint
        # edit (anywhere.live_apply) or a re-decoration changes the config.
        tint = current_kwargs().get('tint')
        if tint is None:
            fn()
            return
        from meltygui.runtime import Melty
        previous = Melty.style_manager.get_tint()
        Melty.style_manager.set_imgui_tint(*tint[:4])
        try:
            fn()
        finally:
            Melty.style_manager.set_imgui_tint(*previous)
    return tinted


def _searchable_body(body):
    """The root body plus the app's global search (app_search.draw: a no-op
    unless meltygui.global_search enabled it and this is its window), drawn
    AFTER the body so the search window floats above the root."""
    def searchable(surface):
        body(surface)
        from meltygui.extensions import call
        from meltygui.editor.source_preview import draw_pending_preview
        draw_pending_preview()
        call('root_draw', surface)
    return searchable


def _draw_root(fn, name, value=None, **kwargs):
    """Draw the render func ``fn`` as the window's root view, filling it:
    what `@glfw_window` over `@render_func` does each frame. ``value`` is
    the view's input value (None: the view owns its state); ``kwargs`` are
    the decorator's view kwargs. ``with_header=draw_header`` puts the meltygui
    header in the chrome row beside the window controls
    (surface.root_view_kwargs)."""
    from meltygui.surface import root_view_kwargs
    # A closable meltygui window (the studio's Mode.MODE_WINDOW), pinned to
    # the surface: layouts (draw_rows / draw_columns) register their
    # children on the enclosing view, so the root must be one.
    return fn(value, **root_view_kwargs(name or fn.__name__, **kwargs))


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
            _state['hooked'] = False
            if not state['failed']:
                run()
        return local

    _state['tracer'] = lambda fr, event, arg: None
    sys.settrace(_state['tracer'])
    frame.f_trace_lines = False
    frame.f_trace = local


# --- the loop -----------------------------------------------------------------------
def _unhook_main_return():
    """Drop the implicit-start trace hook. It needs GLOBAL tracing on to
    fire its local 'return' hook, and global tracing costs a callback per
    Python call — fine for the moment between the decorators and the
    module's return, ruinous for a whole session: a script that calls
    run() itself never returns from its module while the loop runs, and
    every frame ran under the tracer (draw_text 26 ms for an empty line,
    the chat window at 7 fps, 09-12)."""
    if not _state.get('hooked'):
        return
    if sys.gettrace() is not _state.get('tracer'):
        return                      # a debugger's tracer, not ours: leave it
    sys.settrace(None)
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_globals.get('__name__') == '__main__' and frame.f_code.co_name == '<module>':
            frame.f_trace = None
        frame = frame.f_back
    _state['hooked'] = False


def run():
    """Open every registered window and run until the last one closes."""
    if _state['ran']:
        return
    _unhook_main_return()
    _state['ran'] = True
    if not _state['booted']:
        boot()
    import meltygui.window_api as glfw
    _wait_imports()
    _init_melty()
    from meltygui.runtime import Melty
    from meltygui.surface import Surface
    from meltygui.extensions import call
    if _ROOTS:
        call('root_ready', _ROOTS[0][1]['name'])
    for fn, kw in _ROOTS:
        view_kwargs = kw.get('view_kwargs') or {}
        # A plain-function body draws straight onto the surface, so its
        # decorator tint is the surface's; a render-func body gets its
        # kwargs from _draw_root and sits on the default ground.
        ground_tint = None if hasattr(fn, '__render_func__') else view_kwargs.get('tint')
        Surface(kw['name'], _searchable_body(_root_body(fn, kw['name'], view_kwargs, config=kw)),
                width=kw['width'], height=kw['height'], tint=ground_tint, on_close=kw.get('on_close'))
    mark(f'{len(Surface.all)} window(s) created')
    import meltygui.utils.glfw_utils as glfw_utils
    bench = os.environ.get('MELTY_BENCH')
    first = True
    frames = 0
    frametime = [] if os.environ.get('MELTY_FRAMETIME') else None
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
                started = time.perf_counter() if frametime is not None else 0.0
                for surface in list(Surface.all):
                    try:
                        surface.frame()
                    except Exception:
                        _state['failed'] = True
                        raise
                if frametime is not None:
                    # MELTY_FRAMETIME=1 prints a line per frame with the render
                    # thread's time for it (the budget for 120 fps is 8.3 ms).
                    spent = (time.perf_counter() - started) * 1000
                    print(f'meltygui: frame {frames} {spent:.1f} ms', flush=True)
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
        if not _state['failed']:
            _flush_pending_saves()
            _save_session()
        for surface in list(Surface.all):
            surface.destroy()
        glfw.terminate()


def _flush_pending_saves():
    """Write the edits the file hosts hold. A code_file_io host's save is
    the studio's deferred model: each edit queues into PendingSave (in
    memory) and the disk write happens at apply_all_saves, which the studio
    runs from Melty.shutdown. An app that draws such hosts (the code editor)
    exits through here, so flush here too — before the surfaces go, while
    the imgui context the codecs' notifications expect is still alive. A
    failed frame skips it: nothing written from a broken state."""
    from meltygui.editor.pending_save import PendingSave
    if not PendingSave.pending_saves:
        return
    _debug(f'flushing {len(PendingSave.pending_saves)} pending save(s)')
    PendingSave.apply_all_saves()


def _save_session():
    """Persist the draw states (app_session.save) — the studio's exit save,
    for an app. Skipped after a failed frame like the pending saves: a
    broken state must not replace the last good session."""
    session = _state.get('session')
    if session is None:
        return
    import meltygui.app_session as app_session
    path = app_session.save(session, _state['app_id'])
    _debug(f'session saved to {path}' if path else 'session save failed')


def _open_requested_children():
    """Child surfaces the render wrapper asked for (glfw_window=True) since
    the last tick: Melty.surface_requests, filled by surface_window_request."""
    from meltygui.runtime import Melty
    from meltygui.surface import Surface
    requests = Melty.surface_requests
    while requests:
        req = requests.pop(0)
        parent = req.parent_surface
        if parent is None or parent not in Surface.all or req.surface is not None or req.closed:
            continue
        ds = req.draw_state
        size = req.window_size
        child = Surface(req.name, _child_body(req), width=size[0], height=size[1],
                        parent=parent, draw_state=ds)
        child.request = req
        req.surface = child
        _debug(f'child surface {child.title!r} of {parent.title!r} size={size}')


def _child_body(req):
    def body(surface):
        from meltygui.runtime import Melty
        Melty.draw_surface_root(req, surface)
    return body


def _close_stale_children():
    """Immediate mode: a child whose glfw_window=True call was not made
    this tick closes (its parent stopped drawing it); the next call
    reopens it."""
    from meltygui.runtime import Melty
    for req in list(Melty.surface_windows.values()):
        child = req.surface
        if child is not None and req.tick != Melty.app_tick:
            child.closed = True
            child.stale = True


def _note_closed(surface):
    """A surface on its way out. An OS close of a child (the title bar's
    X, the compositor) leaves its request CLOSED: the parent's calls
    return (False, None) from then on, as a closed meltygui window's do. A
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
    """Children follow their parent exactly as nested meltygui windows do:
    the request's window_pos is the parent-relative offset. Each tick the
    child's target rect = the parent's screen rect (geometry feed) + the
    offset, sent to the compositor when it differs from what was last
    sent (Hyprland). A child the USER dragged — its feed rect moved while
    the parent's did not, once our own last placement was acknowledged —
    adopts the new offset, unless window_pos= pinned it. GNOME: rects are
    read but never sent."""
    if not parent.children:
        return
    import meltygui.window_api as glfw
    import meltygui.geometry_feed as geometry_feed
    import meltygui.titlebar as titlebar
    import meltygui.wayland_move as wayland_move
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
        # Retry a missing relationship, including surfaces created before
        # the set_parent fix was hotswapped into the running app.
        if (getattr(child, 'toplevel', None) and parent.toplevel
                and not getattr(child, 'parent_linked', False)):
            child.parent_linked = wayland_move.set_parent(child.toplevel, parent.toplevel)
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
        # Following a parent's POSITION only. Reissuing GLFW's size
        # through the compositor races pending edge-solver commits and
        # turns an ordinary resize into a second, foreign resize.
        if child.last_sent_rect is None or target[:2] != child.last_sent_rect[:2]:
            if crect is not None and tuple(crect[:2]) == target[:2]:
                child.last_sent_rect = target       # already there
            elif geometry_feed.place_window(child.title, target, resize=False):
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
    import meltygui.window_api as glfw
    from meltygui.runtime import Melty
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
    from meltygui.runtime import Melty
    return Melty.root_fill
