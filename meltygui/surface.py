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
from src.lsd.gl_gui import (mouse_cursor, os_frame, scene_target, titlebar,
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
               'glfw_close_requested', 'any_window_hovered', 'any_window_hovered_pending')
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
ROOT_BG = (0.10, 0.10, 0.10, 1.0)


class Surface:
    all: list = []          # open surfaces in creation order
    active = None
    owner_window = None     # app.py's hidden share-group root
    owner_context = None    # the imgui context that owns the font atlas
    app_id = 'melty'

    def __init__(self, name, body, *, title=None, size=(1280, 800), parent=None,
                 draw_state=None):
        """``body(surface)`` draws the window's content inline into the root.
        ``parent``: the Surface this one is a child of (glfw_window=True calls);
        ``draw_state``: the child's root draw_state (its window_pos/size are the
        parent-relative geometry, exactly as for a closable melty window)."""
        _capture_defaults()
        self.name, self.body, self.parent, self.draw_state = name, body, parent, draw_state
        self.title = title or name
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
            root_draw_states={}, root_draw_states_by_layer=defaultdict(list),
            cache=None, backend=None, event_handler=input_handler.InputHandler(),
            frame_key_events=[], hovered_ds=None, imgui_main_window_hovered=False,
            glfw_close_requested=False, any_window_hovered=False, any_window_hovered_pending=False)

        # imgui context sharing the owner's atlas (the owner context OWNS it,
        # so this surface can die without taking the atlas with it).
        owner_io = _owner_io()
        self.ctx = imgui.create_context(shared_font_atlas=owner_io.fonts)
        imgui.get_io().ini_file_name = None
        self.activate()
        glfw.swap_interval(1)
        from src.lsd.gl_gui.view.core_views.split_overlay_renderer import SplitOverlayRenderer
        from src.lsd.gl_gui.view.core_views.blit_offscreen import TileCacheMasked
        self.impl = SplitOverlayRenderer(self.window)
        Melty.init_input_backend(self.window)
        Melty.cache = TileCacheMasked()
        Melty.cache.enabled = False
        if glfw.get_platform() == glfw.PLATFORM_WAYLAND:
            wayland_move.attach(self.window)
            self.hdr_tagged = wayland_color.attach_window(self.window)
        else:
            self.hdr_tagged = False
        if parent is not None:
            parent.children.append(self)
            _set_xdg_parent(self.window, parent.window)
        self._hook_callbacks()
        Surface.all.append(self)

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
            if Surface.active is not None:
                Surface.active._stash()
            Surface.active = self
            self._restore()
        glfw.make_context_current(self.window)
        imgui.set_current_context(self.ctx)

    def _hook_callbacks(self):
        """GLFW callbacks fire inside poll_events, possibly while ANOTHER
        surface's state is loaded (the input backend appends to
        Melty.frame_key_events; titlebar's resize hook uses its globals).
        Chain a wrapper in front of every callback that activates us first."""
        def hook(setter, extra=None):
            prev = None

            def wrapper(window, *args):
                self.activate()
                if extra is not None:
                    extra(*args)
                if prev is not None:
                    prev(window, *args)
            prev = setter(self.window, wrapper)

        hook(glfw.set_key_callback)
        hook(glfw.set_char_callback)
        hook(glfw.set_mouse_button_callback)
        hook(glfw.set_scroll_callback)
        hook(glfw.set_cursor_pos_callback)
        hook(glfw.set_cursor_enter_callback)
        hook(glfw.set_window_size_callback)
        hook(glfw.set_window_focus_callback, lambda *_: request_render())
        hook(glfw.set_framebuffer_size_callback, self._on_framebuffer_size)
        hook(glfw.set_window_close_callback, lambda *_: request_render())

    def _on_framebuffer_size(self, width, height):
        request_render()
        titlebar.on_surface_resized(self.window, width, height)

    # --- render ---------------------------------------------------------------------
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
        self._root_background(draw_list, disp_w, disp_h, radius)
        if transparent and Toggles.Melty.window_shadow_lift > 0:
            from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
            add_shadow((0, 0, disp_w, disp_h), offset=0.5, corner_radius=radius, clip=False)

        top = titlebar.top_inset() if self.chrome else 0.0
        imgui.set_cursor_screen_pos((0, top))
        Melty.root_fill = (float(disp_w), float(disp_h) - top)
        Melty.root_fill_used = False
        try:
            self.body(self)
        finally:
            Melty.root_fill = None
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
        Melty.post_frame(self.impl, self.window)   # imgui render, shadow, corner cut, blur, swap
        self.frames += 1

    def _root_background(self, draw_list, w, h, radius):
        """The window's visible body (the studio's draw_main draws its own):
        edge to edge, the same corner radius as the alpha cut."""
        from src.lsd.gl_gui.hdr_color import pack_color
        colour = ROOT_BG
        sm = Melty.style_manager
        try:
            colour = sm.get_color('window_bg') if sm is not None and hasattr(sm, 'get_color') else colour
        except Exception:
            colour = ROOT_BG
        draw_list.add_rect_filled(0, 0, w, h, pack_color(*colour), rounding=radius)

    # --- geometry (children) ---------------------------------------------------------
    def content_size(self):
        return glfw.get_framebuffer_size(self.window)

    # --- teardown --------------------------------------------------------------------
    def destroy(self):
        for child in list(self.children):
            child.destroy()
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)
        self.activate()
        try:
            self.impl.shutdown()
        except Exception:
            pass
        scene_target.shutdown()
        if self in Surface.all:
            Surface.all.remove(self)
        Surface.active = None
        # The imgui context must not be current when destroyed.
        imgui.set_current_context(Surface.owner_context)
        imgui.destroy_context(self.ctx)
        glfw.destroy_window(self.window)
        self.window = None
        if Surface.all:
            # The renderer's shutdown deleted the SHARED font texture it had
            # uploaded; a survivor re-uploads it.
            survivor = Surface.all[0]
            survivor.activate()
            survivor.impl.refresh_font_texture()
        elif Surface.owner_window is not None:
            glfw.make_context_current(Surface.owner_window)


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


def _set_xdg_parent(child, parent):
    """xdg_toplevel.set_parent: the compositor keeps the child above its
    parent and minimises them together. Best effort through wayland_move's
    proxy discovery; unavailable is fine."""
    try:
        wayland_move.set_parent(child, parent)
    except Exception:
        pass
