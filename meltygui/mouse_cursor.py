"""Native mouse-cursor shapes for Melty views (I-beam over text, resize
arrows over column dividers / window edges / the corner handle, ...).

Two ways a view says which shape the pointer should show:

* **Subscription-level (preferred).** Pass ``cursor=`` to
  ``draw_state.on_action(...)`` / ``InputHandler.register_hovered``. The
  shape rides on the hover subscription, so it obeys exactly the z-order and
  blocker rules the events do — a closable window in front drops it, and a
  child's subscription beats its parent's. ``InputHandler.process_frame``
  resolves it topmost-first and it STICKS for the whole of a captured drag:
  the shape shown at the press stays until release however far the pointer
  strays from the grab rect, and hover shapes are suppressed meanwhile (no
  I-beam while a window is dragged across an editor). A cursor-only
  subscription is ``on_action([], view_id=..., rect=..., cursor=...)`` — no
  events, just the shape over that rect. Views the wrapper registers for
  events can declare a whole-content shape with ``@render_func(mouse_cursor=…)``.
  ``cursor_gate="left_mouse_dragged"`` ties the shape to a DRAG HANDLE: it
  shows only where this registration is the subscriber that would capture
  that event (the topmost ``left_mouse_dragged`` subscriber under the
  pointer), so a window's move handle shows ``MOVE`` on its bare areas and
  nothing over a child that takes the drag itself (a slider, a text
  selection) — the shape tracks where the drag would actually land.
* **Immediate.** ``imgui.set_mouse_cursor(shape)`` inside code that runs
  THIS frame (imgui resets it every ``new_frame``) — for gestures whose owner
  is not a hover subscription: the right-drag corner resize block in
  core_render, the OS-window edges in titlebar.py, imgui's own widgets. A
  non-arrow imgui shape wins over the subscription shape.

``apply(window)`` runs once per frame at the render tail (``Melty.render``,
GLFW's event thread) and pushes the resolved shape to GLFW, on change only.
It steps aside while another owner (the region-screenshot tool) has set its
own cursor image — that owner reports through ``note_external_cursor``.
"""
import os
import struct

from src.lsd.gl_gui import window_api as glfw
import imgui

ARROW = imgui.MOUSE_CURSOR_ARROW
TEXT = imgui.MOUSE_CURSOR_TEXT_INPUT
RESIZE_ALL = imgui.MOUSE_CURSOR_RESIZE_ALL
RESIZE_NS = imgui.MOUSE_CURSOR_RESIZE_NS
RESIZE_EW = imgui.MOUSE_CURSOR_RESIZE_EW
RESIZE_NESW = imgui.MOUSE_CURSOR_RESIZE_NESW
RESIZE_NWSE = imgui.MOUSE_CURSOR_RESIZE_NWSE
HAND = imgui.MOUSE_CURSOR_HAND
NOT_ALLOWED = imgui.MOUSE_CURSOR_NOT_ALLOWED

# Directional shapes - one arrow, pointing the way the edge/corner moves:
# right_side = |>, left_side = <|, bottom_right_corner = ↘ in the L of the
# corner, ... They sit past imgui's enum (NOT_ALLOWED = 8 is its last), so
# imgui.set_mouse_cursor won't carry them: use `request(shape)` for an
# immediate shape, `cursor=` on a subscription. GLFW has no standard cursor
# for them either - on Wayland they come from the theme image
# (load_theme_cursor), elsewhere _GLFW_SHAPE maps them to the double arrows.
RESIZE_E = 101      # |>  right_side
RESIZE_W = 102      # <|  left edge
RESIZE_N = 103      # top edge
RESIZE_S = 104      # bottom edge
RESIZE_NW = 105     # top-left corner
RESIZE_SE = 106     # bottom-right corner
RESIZE_NE = 107     # top-right corner
RESIZE_SW = 108     # bottom-left corner
# The window-move shape: shown where a left drag would move a window - a
# melty window's move handle (core_render's `window_move` on_action) and
# the OS window's drag strip / drag-anywhere background (titlebar.py) -
# gated by `cursor_gate="left_mouse_dragged"` so it only appears where that
# handle would actually CAPTURE the drag. It is on screen most of the time,
# so it is NOT the four-way fleur: it is the theme's own arrow (the default
# pointer, same hotspot) recoloured DARK GREY (`_RECOLORED`,
# load_theme_cursor) - a quiet "this drags the window" hint.
MOVE = 109

_GLFW_SHAPE = {
    TEXT: glfw.IBEAM_CURSOR,
    RESIZE_ALL: glfw.RESIZE_ALL_CURSOR,
    RESIZE_NS: glfw.RESIZE_NS_CURSOR,
    RESIZE_EW: glfw.RESIZE_EW_CURSOR,
    RESIZE_NESW: glfw.RESIZE_NESW_CURSOR,
    RESIZE_NWSE: glfw.RESIZE_NWSE_CURSOR,
    HAND: glfw.POINTING_HAND_CURSOR,
    NOT_ALLOWED: glfw.NOT_ALLOWED_CURSOR,
    # Directional shapes: the nearest double arrow when no theme image loads.
    RESIZE_E: glfw.RESIZE_EW_CURSOR, RESIZE_W: glfw.RESIZE_EW_CURSOR,
    RESIZE_N: glfw.RESIZE_NS_CURSOR, RESIZE_S: glfw.RESIZE_NS_CURSOR,
    RESIZE_NW: glfw.RESIZE_NWSE_CURSOR, RESIZE_SE: glfw.RESIZE_NWSE_CURSOR,
    RESIZE_NE: glfw.RESIZE_NESW_CURSOR, RESIZE_SW: glfw.RESIZE_NESW_CURSOR,
    MOVE: glfw.ARROW_CURSOR,     # no badge without a theme image: the plain arrow
}

# Theme search NAMES per shape - the XDG name first, then the legacy X name,
# the same pairs GLFW's own standard-cursor lookup tries.
_SHAPE_NAMES = {
    TEXT: ("text", "xterm"),
    RESIZE_ALL: ("all-scroll", "fleur"),
    RESIZE_NS: ("ns-resize", "sb_v_double_arrow"),
    RESIZE_EW: ("ew-resize", "sb_h_double_arrow"),
    RESIZE_NESW: ("nesw-resize", "fd_double_arrow"),
    RESIZE_NWSE: ("nwse-resize", "bd_double_arrow"),
    HAND: ("pointer", "hand2"),
    NOT_ALLOWED: ("not-allowed", "crossed_circle"),
    RESIZE_E: ("e-resize", "right_side"),
    RESIZE_W: ("w-resize", "left_side"),
    RESIZE_N: ("n-resize", "top_side"),
    RESIZE_S: ("s-resize", "bottom_side"),
    RESIZE_NW: ("nw-resize", "top_left_corner"),
    RESIZE_SE: ("se-resize", "bottom_right_corner"),
    RESIZE_NE: ("ne-resize", "top_right_corner"),
    RESIZE_SW: ("sw-resize", "bottom_left_corner"),
    MOVE: ("default", "left_ptr"),      # the regular arrow, recoloured below
}
# Recoloured shapes: `shape -> (body grey, outline grey)`. The theme image's
# dark pixels (the body) go to the body grey, its bright pixels (the outline)
# to the outline grey, by luminance - so a black-on-white arrow keeps its
# white outline but reads on light AND dark. How to tune: raise the body
# grey for a lighter pointer, lower the outline grey to darken the rim.
_RECOLORED = {
    MOVE: (70, 255),
}
# libXcursor's default search path (XCURSOR_PATH overrides it).
_XCURSOR_DEFAULT_PATH = "~/.local/share/icons:~/.icons:/usr/share/icons:/usr/share/pixmaps"
_XCURSOR_IMAGE_TYPE = 0xFFFD0002
_GLFW_DEFAULT_CURSOR_SIZE = 16   # wl_init.c loadCursorTheme, XCURSOR_SIZE unset

_cursors = {}        # (shape, theme names, greys) -> GLFWcursor | (None = the platform default arrow)
_applied = ARROW     # shape last pushed to GLFW; None = unknown, re-push
_applied_immediate = False   # the last push came from an immediate shape
_requested = None    # request(): immediate shape for THIS frame, consumed at the tail
_external = False    # another owner has set its own cursor image


def _theme_dirs():
    path = os.environ.get("XCURSOR_PATH") or _XCURSOR_DEFAULT_PATH
    return [os.path.expanduser(d) for d in path.split(":") if d]


def _find_cursor_file(theme, names, _seen=None):
    """`<dir>/<theme>/cursors/<name>` across the search path, trying every
    name in `names` per theme before following the theme's index.theme
    `Inherits=` chain — libXcursor's resolution order."""
    _seen = _seen if _seen is not None else set()
    if theme in _seen:
        return None
    _seen.add(theme)
    inherits = []
    for directory in _theme_dirs():
        base = os.path.join(directory, theme)
        for name in names:
            candidate = os.path.join(base, "cursors", name)
            if os.path.isfile(candidate):
                return candidate
        index = os.path.join(base, "index.theme")
        if os.path.isfile(index):
            try:
                with open(index, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        key, sep, value = line.partition("=")
                        if sep and key.strip().lower() == "inherits":
                            inherits += [t.strip() for t in value.replace(";", ",").split(",") if t.strip()]
            except OSError:
                pass
    for parent in inherits:
        found = _find_cursor_file(parent, names, _seen)
        if found:
            return found
    return None


def load_theme_cursor(shape, size, theme=None):
    """The theme's image for `shape` at the nominal `size` nearest to what
    the theme ships (Xcursor's rule, ties to the larger), as
    (PIL RGBA image, xhot, yhot) ready for glfw.create_cursor. Frame 0 only.
    A `_RECOLORED` shape comes back recoloured. None when the theme has no
    such shape."""
    theme = theme or os.environ.get("XCURSOR_THEME") or "default"
    loaded = _load_theme_image(theme, _SHAPE_NAMES.get(shape), size)
    greys = _RECOLORED.get(shape)
    if loaded is None or greys is None:
        return loaded
    image, xhot, yhot = loaded
    return _recolor(image, *greys), xhot, yhot


def _recolor(image, body_grey, outline_grey):
    """`image` with every pixel's RGB replaced by a grey between `body_grey`
    (where the source is black) and `outline_grey` (where it is white),
    picked by the source pixel's luminance; alpha untouched."""
    import numpy as np
    from PIL import Image
    pixels = np.asarray(image).astype(np.float32)
    luminance = pixels[..., :3].mean(axis=-1, keepdims=True) / 255.0
    pixels[..., :3] = body_grey + (outline_grey - body_grey) * luminance
    return Image.fromarray(pixels.round().astype(np.uint8), "RGBA")


def _load_theme_image(theme, names, size):
    """One theme cursor file (`names` tried in order, then the `default`
    theme) decoded to (PIL RGBA image, xhot, yhot). Xcursor pixels are
    premultiplied ARGB; GLFW wants straight RGBA, so the alpha is divided
    back out. None when the theme has no such shape."""
    if not names:
        return None
    path = _find_cursor_file(theme, names)
    if path is None and theme != "default":
        path = _find_cursor_file("default", names)
    if path is None:
        return None
    with open(path, "rb") as f:
        data = f.read()
    magic, header_size, version, toc_count = struct.unpack_from("<4sIII", data, 0)
    if magic != b"Xcur":
        return None
    images = []      # (nominal-size, position) in file order = frame order
    for i in range(toc_count):
        entry_type, subtype, position = struct.unpack_from("<III", data, header_size + 12 * i)
        if entry_type == _XCURSOR_IMAGE_TYPE:
            images.append((subtype, position))
    if not images:
        return None
    best = min(images, key=lambda e: (abs(e[0] - size), -e[0]))[0]
    position = next(pos for nominal, pos in images if nominal == best)
    (_chunk_size, _type, _subtype, _version,
     width, height, xhot, yhot, _delay) = struct.unpack_from("<9I", data, position)
    import numpy as np
    from PIL import Image
    argb = np.frombuffer(data, dtype="<u4", count=width * height, offset=position + 36).reshape(height, width)
    alpha = (argb >> 24).astype(np.uint32)
    channels = [(argb >> 16) & 255, (argb >> 8) & 255, argb & 255]
    safe_alpha = np.where(alpha == 0, 1, alpha)
    straight = [np.minimum(255, (c * 255 + safe_alpha // 2) // safe_alpha) for c in channels]
    rgba = np.stack(straight + [alpha], axis=-1).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA"), int(xhot), int(yhot)


def _wayland():
    try:
        return glfw.get_platform() == glfw.PLATFORM_WAYLAND
    except Exception:
        return bool(os.environ.get("WAYLAND_DISPLAY"))


def _cursor_size():
    """The nominal size GLFW itself loaded the theme at (its default arrow
    stays GLFW's), so our images match it: XCURSOR_SIZE — exported from
    GNOME at boot by glfw_utils.export_desktop_cursor_env — else 16."""
    try:
        return int(os.environ.get("XCURSOR_SIZE", "")) or _GLFW_DEFAULT_CURSOR_SIZE
    except ValueError:
        return _GLFW_DEFAULT_CURSOR_SIZE


def _glfw_cursor(shape):
    """The GLFW cursor object for `shape`, created once.

    On Wayland the theme's image is loaded HERE and handed to GLFW as an
    image cursor (glfw.create_cursor), not requested as a standard cursor:
    a standard cursor is a wl_cursor, and GLFW arms its animation timer
    from the frame's `delay` without checking the frame count — Bibata
    (and other clickgen themes) stamp delay=13 on every STATIC shape — so
    the same buffer was re-attached and committed every 13 ms, a light
    irregular flicker of exactly the I-beam / resize shapes (the default
    arrow is a stack-local cursor GLFW never re-sets). An image cursor has
    no wl_cursor, so no timer. Image cursors are buffer scale 1: right-sized
    on scale-1 monitors, blurry (not wrong-sized) on scale-2 ones. Any
    failure falls back to the standard cursor; a shape the theme can't
    provide at all falls back to the arrow and is not retried."""
    # Keyed by the theme names and recolour greys too: a hotswap that edits
    # them (this module's registries to re-exec) gets the new image at once.
    key = (shape, _SHAPE_NAMES.get(shape), _RECOLORED.get(shape))
    if key in _cursors:
        return _cursors[key]
    std = _GLFW_SHAPE.get(shape)
    cur = None
    if std is not None and _wayland():
        try:
            loaded = load_theme_cursor(shape, _cursor_size())
            if loaded is not None:
                image, xhot, yhot = loaded
                cur = glfw.create_cursor(image, xhot, yhot)
        except Exception as e:
            print(f"mouse_cursor: theme image for shape {shape} failed, using GLFW's: {e}")
            cur = None
    if std is not None and cur is None:
        try:
            cur = glfw.create_standard_cursor(std)
        except Exception as e:
            print(f"mouse_cursor: no native cursor for shape {shape}: {e}")
            cur = None
    _cursors[key] = cur
    return cur


def request(shape):
    """Immediate shape for this frame — the module-side twin of
    imgui.set_mouse_cursor for shapes imgui's enum can't carry (the
    directional ones). Same contract: call it from code that runs THIS
    frame; the render-tail apply consumes it, so it must be re-asserted
    every frame it applies. Wins over subscription shapes like imgui's."""
    global _requested
    _requested = shape


def note_external_cursor(on):
    """Another owner (region_screenshot's crosshair) has set (`on`) or
    released the window cursor. While on, `apply` leaves the cursor alone;
    on release the next frame re-pushes ours (GLFW is at the default then)."""
    global _external, _applied, _applied_immediate
    _external = bool(on)
    if not on:
        _applied = None
        _applied_immediate = False


def apply(window, early=False):
    """Push the resolved cursor shape to GLFW. Render thread only.

    Called TWICE per frame. `early=True` from Melty.begin_frame right after
    the input handler resolved `cursor_shape` against the freshest pointer
    position — BEFORE the draw pass, so a slow frame (draw_text re-rendering)
    can't hold an I-beam the pointer has already left; the shape leads the
    frame instead of trailing it. `early=False` from the render tail, where
    imgui's immediate shapes (titlebar edges, corner resize) are known. An
    immediate shape is re-asserted every frame it applies, so while the tail
    is showing one the early push leaves it alone — otherwise the two would
    alternate. (What this can't fix: the render thread is also the GLFW
    event thread, so nothing moves the shape while a frame is mid-draw.)"""
    global _applied, _applied_immediate, _requested
    if _external:
        return
    if imgui.get_io().config_flags & imgui.CONFIG_NO_MOUSE_CURSOR_CHANGE:
        return
    shape = imgui.get_mouse_cursor()
    immediate = shape != ARROW and shape != imgui.MOUSE_CURSOR_NONE
    if not immediate and _requested is not None:
        shape, immediate = _requested, True
    if not early:
        _requested = None        # consumed; re-asserted per frame by its owner
    if not immediate:
        from src.lsd.gl_gui.melty import Melty
        shape = Melty.event_handler.cursor_shape
        if shape is None:
            shape = ARROW
    if early and _applied_immediate:
        return
    if shape == _applied:
        _applied_immediate = immediate
        return
    try:
        glfw.set_cursor(window, _glfw_cursor(shape))
    except Exception as e:
        print(f"mouse_cursor: set_cursor failed for shape {shape}: {e}")
    _applied = shape
    _applied_immediate = immediate
