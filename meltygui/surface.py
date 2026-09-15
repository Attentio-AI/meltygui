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

from src.lsd.gl_gui import window_api as glfw
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
               'dynamic_style_gl',
               'root_draw_states', 'root_draw_states_by_layer', 'cache', 'backend',
               'event_handler', 'frame_key_events', 'hovered_ds', 'imgui_main_window_hovered',
               'glfw_close_requested', 'any_window_hovered', 'any_window_hovered_pending',
               'filter',      # its executor's VAO is per GL context
               # The hit-test tree and the focus slots are per OS window: boxes
               # are window-local coordinates, so one shared tree had window B's
               # views answering window A's hit test at the same local point (and
               # a closed window's boxes out of sync - the 09-12 two-roots repro).
               '_bvh', '_bvh_id_to_ds', '_bvh_gen', '_bvh_query_cache', '_bvh_query_cache_gen',
               'bvh_hover_ids', 'focused_ds', 'text_focused_ds', 'popover_focused_ds')
# Module globals that assume one window (titlebar/os_frame resolve "the window"
# through Melty.glfw_window, so with the swap they see this window's).
MODULE_GLOBALS = {
    titlebar: ('_pressed_button', '_wm_move_started', '_rdrag', '_corner_gl',
               '_input_rect_applied', '_geometry_applied', '_self_resize',
               '_pending_surface_size', '_pending_surface_offset', '_frame_surface_offset',
               '_pending_surface_fit', '_pending_surface_wait', '_last_surface_size'),
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
    session = None            # the AppSession app.py loaded (app_session.py), if any

    def __init__(self, name, body, *, width=1280, height=800, parent=None,
                 draw_state=None, tint=None, on_close=None):
        """``name`` is the window's name AND its OS title (a child's `##suffix`
        is stripped from the title); ``width`` / ``height`` the content size —
        the universal melty names, as on `@window` and every view.
        ``body(surface)`` draws the window's content inline into the root.
        ``parent``: the Surface this one is a child of (glfw_window=True calls);
        ``draw_state``: the child's root draw_state (its window_pos/size are the
        parent-relative geometry, exactly as for a closable melty window);
        ``tint``: the root ground's tint (None: Toggles.Melty.app_root_tint)."""
        _capture_defaults()
        self.name, self.body, self.parent, self.draw_state = name, body, parent, draw_state
        self.tint = tint
        self.on_close = on_close      # asked when an OS close lands; False keeps the window
        width, height = int(width), int(height)
        self.title = _unique_title(name.split('##')[0])
        self.request = None         # the melty.surface_children entry of a child
        self.toplevel = None        # xdg_toplevel proxy (wayland_move), for set_parent
        self.parent_linked = False
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
        self.window = glfw.create_window(width, height, self.title, None, share)
        if not self.window:
            raise SystemExit('glfw.create_window failed')

        # Fresh per-window state: module defaults + a clean Melty set.
        self._mods = {key: _fresh(value) for key, value in _DEFAULTS.items()}
        self.root_windows = {}  # live collision roots belonging to this surface window
        # vis.root is what Melty reads and stores off (draw_state_registry,
        # surface_windows): the user's persisted session (app_session.py)
        # when app.py loaded one, else a stand-in around the shared registry.
        root = Surface.session
        if root is None:
            registry = Melty.draw_state_registry
            if registry is None:
                registry = Melty.draw_state_registry = {}
            root = SimpleNamespace(draw_state_registry=registry)
        self._melty = dict(
            glfw_window=self.window,
            vis=SimpleNamespace(root=root,
                                window=self.window, tracked_keys=[], first_frame_keys=set(),
                                fa_font=None),
            framebuffer_size=None, frame_inset=0, frame_origin=(0, 0),
            root_draw_states=defaultdict(list), root_draw_states_by_layer=defaultdict(list),
            cache=None, backend=None, dynamic_style_gl=None, event_handler=input_handler.InputHandler(),
            frame_key_events=[], hovered_ds=None, imgui_main_window_hovered=False,
            glfw_close_requested=False, any_window_hovered=False, any_window_hovered_pending=False,
            filter=type(Melty.filter)(),
            _bvh=type(Melty._bvh)(), _bvh_id_to_ds={}, _bvh_gen=0, _bvh_query_cache={},
            _bvh_query_cache_gen=-1, bvh_hover_ids=set(),
            focused_ds=None, text_focused_ds=None, popover_focused_ds=None)

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
        imgui.get_io().display_size = (float(width), float(height))
        self.activate()
        titlebar.note_surface_size(self.window)
        glfw.swap_interval(1)
        from src.lsd.gl_gui.view.core_views.split_overlay_renderer import SplitOverlayRenderer
        from src.lsd.gl_gui.view.core_views.blit_offscreen import TileCacheMasked
        self.impl = SplitOverlayRenderer(self.window)
        Melty.init_input_backend(self.window)
        Melty.cache = TileCacheMasked()
        # The blit cache starts OFF and resize() switches it on after the
        # second frame, the studio's schedule (lsd_studio: frame_count 2).
        # Off, nothing is ever tile-cached: every view, every RenderHost
        # body and draw_text reverts on every frame and freeze_resize
        # never engages (09-13, melty_code_editor: 18 ms hover frames).
        # The popover artifacts that kept it off (row tiles blank, a black
        # block under a context menu, 09-12) were captures of a menu
        # opened off the display's right edge served after it was pinned
        # back in - TileCacheMasked._fill_unfilled now re-renders those.
        Melty.cache.enabled = False
        if glfw.get_platform() == glfw.PLATFORM_WAYLAND:
            # The tag is the window's stable identity for the compositor
            # (remembered floating state key on app id + tag), the name
            # before its uniqueness suffix.
            wayland_move.attach(self.window, tag=name.split('##')[0])
            self.toplevel = wayland_move.toplevel_proxy()
            self.hdr_tagged = wayland_color.attach_window(self.window)
        else:
            self.hdr_tagged = False
        if parent is not None:
            parent.children.append(self)
            if self.toplevel and parent.toplevel:
                self.parent_linked = wayland_move.set_parent(self.toplevel, parent.toplevel)
        self._hook_callbacks()
        Surface.all.append(self)
        # Install the shadow margin BEFORE the first buffer is committed. A
        # deferred IPC resize overwrites Hyprland's restored content size and
        # first maps a content box smaller by twice the inset. Grow only the
        # native surface here; subsequent compositor configures become the
        # authority for the content size (on_surface_resized adds the margin).
        inset = int(titlebar.window_inset()) if self.chrome else 0
        if inset > 0:
            content_width, content_height = glfw.get_window_size(self.window)
            titlebar.set_surface_size(self.window, content_width + 2 * inset,
                                      content_height + 2 * inset, box=False)
        titlebar.sync_window_geometry(self.window)

    # --- activation ----------------------------------------------------------------
    def _stash(self):
        for name in MELTY_ATTRS:
            self._melty[name] = getattr(Melty, name)
        for (mod, name) in self._mods:
            self._mods[(mod, name)] = mod.__dict__[name]

    def _restore(self):
        # A surface created before the dynamic-style code was hotswapped in
        # must not inherit another surface's GL resources.
        self._melty.setdefault('dynamic_style_gl', None)
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
            # ... or flipped left-drag move in the desktop's Settings.
            from src.lsd.gl_gui import hypr_left_drag
            hypr_left_drag.refresh()

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
            # The window's ×, the compositor's close and set_window_should_close
            # all land here; a window's on_close may decline (and hide instead).
            if self.on_close is not None and self.on_close(self) is False:
                glfw.set_window_should_close(self.window, False)
            else:
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
        # Standalone apps do not run the studio's draw_main. Commit source
        # edits deferred while a picker/slider held the pointer here too.
        from src.lsd.gl_gui.view.core_views.anywhere import flush_deferred_writes
        flush_deferred_writes()
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
        # The root fills the OS MODEL's size (os_frame.content_size): equal
        # to the display except while our own resize is still landing.
        fill_w, fill_h = os_frame.content_size((disp_w, disp_h)) if self.chrome else (disp_w, disp_h)
        Melty.root_fill = (float(fill_w), float(fill_h) - top, float(top))   # (w, h below the chrome, top y)
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
        if self.request is not None:
            Melty.finish_surface_root(self.request, self)
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
        if self.frames == 2 and not Melty.cache.enabled:
            Melty.cache.set_enabled(True)

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
        for state in (titlebar._corner_gl, scene_target._STATE.get('gl'), Melty.dynamic_style_gl):
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
        # This window's Wayland objects, released while its proxies are alive
        # (glfw.destroy_window takes the wl_surface): the HDR buffer / feedback
        # / description, and wayland_move's per-window references. The
        # connection-wide seat, pointer and managers stay for the others.
        if self.hdr_tagged or glfw.get_platform() == glfw.PLATFORM_WAYLAND:
            try:
                wayland_color.detach()
            except Exception:
                pass
            try:
                wayland_move.detach(self.window)
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
    # OS bodies orchestrate render calls and shortcuts every requested frame.
    # Descendant bodies have their own caches; the app still sleeps when idle.
    pinned = dict(name=name, closable=True, draggable=False, frame_pinned=True,
                  window_pos=(0, top), width=width, height=height,
                  auto_resize=False, show_header=header, with_footer=None, shadow=False, show_bg=True,
                  selectable=False, use_cache=False, disable_scroll=True, indent_size=5,
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
