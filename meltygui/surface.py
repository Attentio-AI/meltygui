"""OS-window surfaces: one melty frame per GLFW window (app.py's @glfw_window).

A Surface owns everything that belongs to ONE OS window: the GLFW window and
its GL context (all surfaces share one GL share group, rooted at app.py's
hidden owner window), an imgui context sharing the owner's font atlas, the
SplitOverlayRenderer, the GLFW-callback input backend and its InputHandler,
the tile cache (FBO-bound), the fp16 scene target, and the frameless-window
chrome state (titlebar.py, os_frame.py).

melty was written for one window: that per-window state lives in `Melty`
class attributes and in module globals of titlebar / os_frame / scene_target
/ wayland_move / wayland_color / mouse_cursor. Surfaces render strictly one
after another, so instead of threading a window through thousands of call
sites, `activate()` swaps that state: the previously active surface stashes
the live values, this one restores its own. Everything rebuilt inside
Melty.begin_frame / end_frame (layers, stacks, events) needs no swapping —
see the catalog in app.py's history. The studio, which never creates a
Surface, is untouched: with no surface active nothing is ever swapped.

Frame (``frame()``): the studio's root-window ritual (new_core_view.
draw_melty_windows) with the melty title bar, window controls, corner cut
and drop shadow when the window is frameless (Toggles.Melty.
wayland_show_frame off), and the body drawn inline into the root. Views the
body draws at root level fill the window (Melty.root_fill, consumed by the
render wrapper) so a one-view body needs no width/height.
"""
from __future__ import annotations

import copy
import time
from collections import defaultdict
from types import SimpleNamespace

import glfw
import imgui
import OpenGL.GL as gl

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui import (mouse_cursor, os_frame, scene_target, titlebar, titlebar_buttons,
                            wayland_color, wayland_move)
from src.lsd.gl_gui.events import input_handler
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils import custom_views as views
from src.lsd.gl_gui.utils.glfw_utils import request_render

# --- Per-window state -----------------------------------------------------------
# Melty class attributes that persist across frames and belong to one window.
MELTY_ATTRS = ('glfw_window', 'vis', 'framebuffer_size', 'frame_inset', 'frame_origin',
               'root_draw_states', 'root_draw_states_by_layer', 'cache', 'backend',
               'event_handler', 'frame_key_events', 'hovered_ds', 'imgui_main_window_hovered',
               'glfw_close_requested', 'any_window_hovered', 'any_window_hovered_pending',
               'filter')      # its executor's VAO is per GL context
# Module globals that assume one window (titlebar/os_frame resolve "the window"
# through Melty.glfw_window, so with the swap they see this window's).
MODULE_GLOBALS = {
    titlebar: ('_pressed_button', '_wm_move_started', '_rdrag', '_corner_gl',
               '_input_rect_applied', '_geometry_applied', '_self_resize',
               '_pending_surface_size', '_pending_surface_offset', '_frame_surface_offset',
               '_pending_surface_fit', '_pending_surface_wait'),
    os_frame: ('_STATE',),
    scene_target: ('_STATE',),
    wayland_move: ('_STATE',),
    wayland_color: ('_STATE',),
    mouse_cursor: ('_applied', '_applied_immediate', '_requested', '_external'),
    input_handler: ('_BUTTON_PROBE',),
}
_DEFAULTS = None      # Default module globals, captured before the first surface
_DEBUG = bool(__import__('os').environ.get('MELTY_DEBUG'))


def _capture_defaults():
    global _DEFAULTS
    if _DEFAULTS is not None:
        return
    _DEFAULTS = {}
    for mod, names in MODULE_GLOBALS.items():
        for name in names:
            value = mod.__dict__[name]
            try:
                _DEFAULTS[(mod, name)] = copy.deepcopy(value)
            except Exception:
                _DEFAULTS[(mod, name)] = value


ROOT_FLAGS = (imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE
              | imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS
              | imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS | imgui.WINDOW_NO_NAV_INPUTS
              | imgui.WINDOW_NO_NAV | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS
              | imgui.WINDOW_NO_SCROLL_WITH_MOUSE)
ROOT_MAX_BG_VALUE = 0.130     # draw_main's max_bg_value: the ground's brightness cap


class Surface:
    all: list = []          # open surfaces in creation order
    active = None
    owner_window = None     # app.py's hidden share-group root
    owner_context = None    # the imgui context that owns the font atlas
    app_id = 'melty'

    def __init__(self, name, body, *, title=None, size=(1280, 800), parent=None,
                 draw_state=None, tint=None):
        """``body(surface)`` draws the window's content inline into the root.
        ``parent``: the Surface this one is a child of (glfw_window=True calls);
        ``draw_state``: the child's root draw_state (its window_pos/size are the
        parent-relative geometry, exactly as for a closable melty window);
        ``tint``: the root ground's tint (None: Toggles.Melty.app_root_tint)."""
        _capture_defaults()
        self.name, self.body, self.parent, self.draw_state = name, body, parent, draw_state
        self.tint = tint
        self.title = _unique_title(title or name)
        self.request = None         # the melty.surface_children entry of a child
        self.toplevel = None        # xdg_toplevel proxy (wayland_move), for set_parent
        self.await_ack = False      # a rect sent, the parent yet to show it
        self.sent_at = 0.0
        self.seen_rect = None       # the feed's rect last tick (present_children)
        self.stale = False          # closing because its call stopped, not an OS close
        self.children: list = []
        self.frames = 0
        self.closed = False
        self.last_sent_rect = None
        self.chrome = not titlebar.wants_os_decoration()
        transparent = titlebar.wants_transparent_framebuffer()
        self.transparent = transparent

        glfw.default_window_hints()
        glfw.window_hint(glfw.VISIBLE, True)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 4)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, transparent)
        glfw.window_hint(glfw.ALPHA_BITS, 8 if transparent else 0)
        glfw.window_hint(glfw.DECORATED, glfw.FALSE if self.chrome else glfw.TRUE)
        glfw.window_hint(glfw.STENCIL_BITS, 8)
        glfw.window_hint(glfw.RESIZABLE, True)
        try:
            glfw.window_hint_string(glfw.WAYLAND_APP_ID, Surface.app_id)
            glfw.window_hint_string(glfw.X11_CLASS_NAME, Surface.app_id)
        except Exception:
            pass
        share = Surface.owner_window
        self.window = glfw.create_window(int(size[0]), int(size[1]), self.title, None, share)
        if not self.window:
            raise SystemExit('glfw.create_window failed')

        # Fresh per-window state: module defaults + a clean Melty set.
        self._mods = {key: _fresh(value) for key, value in _DEFAULTS.items()}
        registry = Melty.draw_state_registry
        if registry is None:
            registry = Melty.draw_state_registry = {}
        self._melty = dict(
            glfw_window=self.window,
            vis=SimpleNamespace(root=SimpleNamespace(draw_state_registry=registry),
                                window=self.window, tracked_keys=[], first_frame_keys=set(),
                                fa_font=None),
            framebuffer_size=None, frame_inset=0, frame_origin=(0, 0),
            root_draw_states=defaultdict(list), root_draw_states_by_layer=defaultdict(list),
            cache=None, backend=None, event_handler=input_handler.InputHandler(),
            frame_key_events=[], hovered_ds=None, imgui_main_window_hovered=False,
            glfw_close_requested=False, any_window_hovered=False, any_window_hovered_pending=False,
            filter=type(Melty.filter)())

        # imgui context sharing the owner's atlas (the owner context OWNS it,
        # so this surface can die without taking the atlas with it).
        owner_io = _owner_io()
        self.ctx = imgui.create_context(shared_font_atlas=owner_io.fonts)
        # create_context only makes the new context current when NONE is
        # (the owner's is), so select ours before touching its io.
        imgui.set_current_context(self.ctx)
        imgui.get_io().ini_file_name = None
        # The renderer uploads the atlas in its constructor, BEFORE it stamps
        # display_size: the FreeType hinting pass (fonts.hint_atlas) frames on
        # the current context and dies on the (-1, -1) default.
        imgui.get_io().display_size = (float(size[0]), float(size[1]))
        self.activate()
        glfw.swap_interval(1)
        from src.lsd.gl_gui.view.core_views.split_overlay_renderer import SplitOverlayRenderer
        from src.lsd.gl_gui.view.core_views.blit_offscreen import TileCacheMasked
        self.impl = SplitOverlayRenderer(self.window)
        Melty.init_input_backend(self.window)
        Melty.cache = TileCacheMasked()
        # The blit cache stays OFF for an app window: switched on (at the
        # studio's frame-2 schedule or later) it serves nested popovers'
        # row tiles blank / shadow-less and paints a black block under a
        # context menu (09-12, melty_code_editor) - a Melty gap still to
        # find. Consequences of running cache-off are handled elsewhere:
        # marks resolve their owning window from the window stack
        # (TileCacheMasked._mark_position - ownerless marks read as topmost
        # and cast over nested windows), and retained tiles of an undrawn
        # branch die without a capture ( (branch_dropped).
        Melty.cache.enabled = False
        if glfw.get_platform() == glfw.PLATFORM_WAYLAND:
            wayland_move.attach(self.window)
            self.toplevel = wayland_move.toplevel_proxy()
            self.hdr_tagged = wayland_color.attach_window(self.window)
        else:
            self.hdr_tagged = False
        if parent is not None:
            parent.children.append(self)
            if self.toplevel and parent.toplevel:
                wayland_move.set_parent(self.toplevel, parent.toplevel)
        self._hook_callbacks()
        Surface.all.append(self)
        # The chrome chrome window's shadow margin: the content the caller
        # sized is laid out inside a surface grown by the margin on every
        # side. Requested like a studio's launch restore - applied at the
        # first frame (apply_pending_surface_size, self-flagged so the
        # resize hook leaves it alone) and, on Hyprland, fitted into the
        # work area: the compositor centres a window at its initial size
        # and the box grows anchored top-left, so an unfitted window runs
        # off the screen and its clamps fight the regrow (09-11).
        inset = int(titlebar.window_inset()) if self.chrome else 0
        if inset > 0:
            titlebar.request_surface_size(self.window, int(size[0]) + 2 * inset,
                                          int(size[1]) + 2 * inset, fit=True)

    # --- activation ----------------------------------------------------------------
    def _stash(self):
        for name in MELTY_ATTRS:
            self._melty[name] = getattr(Melty, name)
        for (mod, name) in self._mods:
            self._mods[(mod, name)] = mod.__dict__[name]

    def _restore(self):
        for name, value in self._melty.items():
            setattr(Melty, name, value)
        for (mod, name), value in self._mods.items():
            mod.__dict__[name] = value

    def activate(self):
        """Make this surface's window, GL context, imgui context and per-window
        state current. Cheap when already active."""
        if Surface.active is not self:
            if _DEBUG:
                print(f'[surface] activate {getattr(Surface.active, "title", None)!r} -> {self.title!r}', flush=True)
            if Surface.active is not None:
                Surface.active._stash()
            Surface.active = self
            self._restore()
        glfw.make_context_current(self.window)
        imgui.set_current_context(self.ctx)

    def _hook_callbacks(self):
        """GLFW callbacks fire inside poll_events — and inside any GLFW call
        that dispatches Wayland events (set_window_size, a roundtrip), i.e.
        possibly while ANOTHER surface is mid-frame. The input backend
        appends to Melty.frame_key_events and titlebar's resize hook reads
        its globals, so chain a wrapper in front of every callback that
        loads OUR per-window state for the duration of the callback and
        puts the interrupted surface's back afterwards. Never the GL or
        imgui context: the interrupted frame keeps rendering into its own."""
        def hook(setter, extra=None):
            prev = None

            def wrapper(window, *args):
                interrupted = Surface.active
                if interrupted is not self:
                    if interrupted is not None:
                        interrupted._stash()
                    Surface.active = self
                    self._restore()
                try:
                    if extra is not None:
                        extra(*args)
                    if prev is not None:
                        prev(window, *args)
                finally:
                    if interrupted is not self:
                        self._stash()
                        Surface.active = interrupted
                        if interrupted is not None:
                            interrupted._restore()
            prev = setter(self.window, wrapper)

        hook(glfw.set_key_callback)
        hook(glfw.set_char_callback)
        hook(glfw.set_mouse_button_callback)
        hook(glfw.set_scroll_callback)
        hook(glfw.set_cursor_pos_callback)
        hook(glfw.set_cursor_enter_callback)
        hook(glfw.set_window_size_callback)
        hook(glfw.set_window_focus_callback, self._on_focus)
        hook(glfw.set_framebuffer_size_callback, self._on_framebuffer_size)
        hook(glfw.set_window_close_callback, lambda *_: request_render())

    def _on_focus(self, focused):
        request_render()
        if focused:
            # The user may have changed the desktop's title-bar buttons in
            # the meantime (titlebar_buttons: rate-limited re-read).
            titlebar_buttons.refresh()

    def _on_framebuffer_size(self, width, height):
        request_render()
        applied = titlebar.on_surface_resized(self.window, width, height)
        if _DEBUG:
            print(f'[surface {self.title}] framebuffer {width}x{height} '
                  f'self_resize={titlebar._self_resize} regrow={applied}', flush=True)

    # --- render ---------------------------------------------------------------------
    def _draw_render_hosts(self):
        """The studio's draw_main draws every registered RenderHost once a
        frame (RenderHost.draw_all); that is what loads a code_file_io host's
        file, reparses it after an edit and auto-saves it. A melty app's body
        is a plain draw function, so the surface runs the pump for it, after
        the body and inside the imgui frame. Hosts are process-global: once
        per app tick, however many surfaces draw in it."""
        if not Melty.render_hosts or Melty.render_hosts_tick == Melty.app_tick:
            return
        Melty.render_hosts_tick = Melty.app_tick
        from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
        RenderHost.draw_all()

    def frame(self):
        self.activate()
        if glfw.window_should_close(self.window):
            self.closed = True
            return
        titlebar.apply_pending_surface_size(self.window)
        self.impl.process_inputs()
        fb_w, fb_h = glfw.get_framebuffer_size(self.window)
        if fb_w == 0 or fb_h == 0:
            return
        scene_target.begin(fb_w, fb_h)
        transparent = titlebar._frame_transparent(self.window)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0 if transparent else 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        Melty.apply_ui_scale(self.impl)
        views.new_frame()
        io = imgui.get_io()
        disp_w, disp_h = io.display_size
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(disp_w, disp_h)
        imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (0.0, 0.0))
        imgui.push_style_var(imgui.STYLE_WINDOW_BORDERSIZE, 0.0)
        views.begin('main##window_melty', closable=False, flags=ROOT_FLAGS)
        imgui.pop_style_var(2)
        # melty only routes mouse events to views while the root imgui
        # window is hovered (draw_view.hover_eligible).
        Melty.imgui_main_window_hovered = imgui.is_window_hovered()
        Melty.begin_frame()
        if self.chrome:
            os_frame.begin_frame()
            titlebar.poll_os_window_drag()
            os_frame.solve()
        imgui.set_cursor_screen_pos((0, 0))
        imgui.set_item_allow_overlap()
        draw_list = imgui.get_window_draw_list()
        draw_list.channels_split(Melty.max_depth)
        Melty.channels_split = True
        Melty.window_stack.append((self.name, True))

        radius = titlebar.frame_corner_radius() if transparent else 0.0
        previous_tint, bg_color = self._root_background(disp_w, disp_h, radius)
        if transparent and Toggles.Melty.window_shadow_lift > 0:
            from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
            add_shadow((0, 0, disp_w, disp_h), offset=0.5, corner_radius=radius, clip=False)

        top = titlebar.top_inset() if self.chrome else 0.0
        imgui.set_cursor_screen_pos((0, top))
        Melty.root_fill = (float(disp_w), float(disp_h) - top, float(top))   # (w, h below the chrome, top inset)
        Melty.root_fill_used = False
        # The body runs INSIDE the ground, as the studio's windows run inside
        # draw_main's show_bg: one bg depth down, the ground's tint and colour
        # on the stacks draw_bg reads for the bleed (the wrapper's own fill).
        Melty.bg_depth += 1
        Melty.bg_stack.append(Melty.style_manager.get_tint())
        Melty.bg_color_stack.append(bg_color)
        try:
            self.body(self)
            self._draw_render_hosts()
        finally:
            Melty.root_fill = None
            Melty.bg_depth -= 1
            Melty.bg_stack.pop()
            Melty.bg_color_stack.pop()
            Melty.style_manager.set_imgui_tint(*previous_tint)
        if self.chrome:
            titlebar.paint_window_controls(draw_list)
        Melty.end_frame()
        if self.chrome:
            os_frame.flush()
        Melty.window_stack.pop()
        draw_list.channels_merge()
        Melty.channels_split = False
        views.end()
        if self.chrome:
            titlebar.draw_titlebar(self.window)
        views.end_frame()
        try:
            Melty.post_frame(self.impl, self.window)   # imgui render, shadow, corner cut, PQ, swap
        except Exception:
            if _DEBUG:
                import ctypes
                cur = glfw.get_current_context()
                cg = titlebar._corner_gl
                print(f'[surface {self.title}] post_frame FAILED ctx={ctypes.cast(cur, ctypes.c_void_p).value:#x} '
                      f'mine={ctypes.cast(self.window, ctypes.c_void_p).value:#x} corner_gl={id(cg):#x} '
                      f'corner_cache={getattr(cg, "_cache", None) and list(getattr(cg, "_cache").keys())} '
                      f'mods_corner={id(self._mods[(titlebar, "_corner_gl")]):#x} active={Surface.active.title!r}',
                      flush=True)
            raise
        self.frames += 1

    def _root_background(self, w, h, radius):
        """The window's ground: what the studio's Main Window paints under
        its windows (draw_main's show_bg — draw_bg at depth 0 under the root
        tint, capped at the same max_bg_value), edge to edge with the alpha
        cut's corner radius and no outline stroke. Sets the style tint for
        the body; returns (the tint to restore, the ground's colour)."""
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_bg
        style_manager = Melty.style_manager
        previous_tint = style_manager.get_tint()
        tint = self.tint if self.tint is not None else Toggles.Melty.app_root_tint
        style_manager.set_imgui_tint(*tint[:4])
        _, bg_color = draw_bg(bypass=True, left=0, top=0, width=w, height=h, rounding=radius,
                              outline=False, opacity=1.0, max_bg_value=ROOT_MAX_BG_VALUE,
                              depth=Melty.shadow_depth, style_manager=style_manager)
        return previous_tint, bg_color

    # --- geometry (children) ---------------------------------------------------------
    def content_size(self):
        return glfw.get_framebuffer_size(self.window)

    # --- teardown --------------------------------------------------------------------
    def destroy(self):
        if _DEBUG:
            print(f'[surface] destroy {self.title!r} (active={getattr(Surface.active, "title", None)!r})', flush=True)
        for child in list(self.children):
            child.destroy()
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)
        self.activate()
        from src.lsd.gl_gui.gl_state import GLState, current_context
        context = current_context()
        try:
            if len(Surface.all) > 1:
                # Other surfaces live on: the renderer's shutdown would
                # delete the shader program and the font texture, SHARED
                # across the GL share group. Drop only this context's own
                # vertex objects.
                impl = self.impl
                if getattr(impl, '_vao_handle', -1) > 0:
                    gl.glDeleteVertexArrays(1, [impl._vao_handle])
                for name in ('_vbo_handle', '_elements_handle'):
                    handle = getattr(impl, name, -1)
                    if handle > 0:
                        gl.glDeleteBuffers(1, [handle])
                impl._vao_handle = impl._vbo_handle = impl._elements_handle = 0
            else:
                self.impl.shutdown()
        except Exception:
            pass
        # This surface's GL objects, deleted while it's still current: the
        # scene target, the chrome's GLState and the shader filter. Whatever
        # else is queued for it later (the GLState updates after the window
        # is gone) is discarded below - its names die with the context.
        scene_target.shutdown()
        for state in (titlebar._corner_gl, scene_target._STATE.get('gl')):
            if state is not None:
                try:
                    state.release()
                except Exception:
                    pass
        try:
            Melty.filter.cleanup()
        except Exception:
            pass
        try:
            GLState.flush_deletes()
        except Exception:
            pass
        if self in Surface.all:
            Surface.all.remove(self)
        Surface.active = None
        # The imgui context must not be current when destroyed.
        imgui.set_current_context(Surface.owner_context)
        imgui.destroy_context(self.ctx)
        glfw.destroy_window(self.window)
        self.window = None
        GLState.discard_context(context)
        if Surface.all:
            Surface.all[0].activate()
        elif Surface.owner_window is not None:
            glfw.make_context_current(Surface.owner_window)


def root_view_kwargs(name, /, **kwargs):
    """The kwargs that draw a render func as the active surface's ROOT melty
    window: a closable window pinned to the OS window (layouts — draw_rows /
    draw_columns — register their edges on the enclosing WINDOW, so the
    root must be one), sized to it, never dragged, its own close and shadow
    off (the OS window has the chrome). Shared by app._draw_root (a
    `@glfw_window` over a render func) and Melty.draw_surface_root (a
    `glfw_window=True` child).

    ``with_header=`` (the decorator's, the call's) puts the MELTY HEADER in
    the chrome row — the same header a studio window wears, beside the OS
    window's controls: `show_name` names it after the OS window's title
    (`display_name`, live through a retitle), the tint chip edits the
    view's tint, and the geometry keeps clear of the controls on both sides
    — `header_indent` past a left-side group, and a `with_header_end` that
    only claims the right group's width (titlebar.draw_header_controls), so
    the wrapper right-aligns and clips the header exactly as it does
    around a header's close button. No collapse arrow (`is_tree=False`): an
    OS window does not fold to its header. Without a header the body
    starts under the control row (titlebar.top_inset).

    Caller kwargs win over every pinned value (``show_header=False`` hides
    a passed header, ``disable_scroll=False`` scrolls the root, a ``name=``
    the fallback ``name`` — positional-only, so a child's own kwargs pass)."""
    width, height, top = Melty.root_fill
    header = kwargs.get('with_header') is not None
    if header:
        surface = Surface.active
        chrome = surface is not None and surface.chrome
        left_inset, right_inset = titlebar.chrome_insets() if chrome else (0.0, 0.0)
        # The header row IS the chrome row: the view starts at the very
        # top and the body is immediately under the header.
        height, top = height + top, 0.0
        kwargs.setdefault('is_tree', False)
        kwargs.setdefault('show_tint', True)
        kwargs.setdefault('display_name', surface.title if surface is not None else name)
        kwargs.setdefault('header_indent', left_inset)
        kwargs.setdefault('with_header_end', titlebar.draw_header_controls if right_inset > 0 else None)
    else:
        kwargs.setdefault('with_header_end', None)
    pinned = dict(name=name, closable=True, draggable=False, window_pos=(0, top), width=width, height=height,
                  auto_resize=False, show_header=header, with_footer=None, shadow=False, show_bg=True,
                  selectable=False, use_cache=True, disable_scroll=True, indent_size=5,
                  initial={'width': width, 'height': height, 'window_pos': (0, top)})
    return pinned | kwargs


def _fresh(value):
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _owner_io():
    if Surface.owner_context is None:
        raise RuntimeError('Surface.owner_context is not set (app.py creates it)')
    prev = imgui.get_current_context()
    imgui.set_current_context(Surface.owner_context)
    try:
        return imgui.get_io()
    finally:
        if prev is not None:
            imgui.set_current_context(prev)


def _unique_title(title):
    """Window titles double as the geometry feed's per-window key (several
    windows share our pid and class), so no two surfaces share one."""
    taken = {s.title for s in Surface.all}
    if title not in taken:
        return title
    n = 2
    while f'{title} ({n})' in taken:
        n += 1
    return f'{title} ({n})'
