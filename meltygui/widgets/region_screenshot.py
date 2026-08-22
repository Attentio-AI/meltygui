"""Region screenshot tool — Ctrl+Shift+3 (draw_main's root hotkey) or
Actions.screenshot arms it; a crosshair follows the cursor on the overlay
draw list; click-drag a box; on release the pixels inside the box are read
from the main framebuffer (GL_BACK, after the frame is fully composited — the
deferred capture queue in screenshot.py), saved as a PNG under the configured
shot dir, and opened in the code editor exactly like a global-search file
hit (open_in_editor → the file's ImageCodec tab).

State is module-level (one tool, never more than one capture in flight);
`draw(draw_state)` runs from draw_main every frame and is a no-op unless
armed. While armed the tool owns the mouse: full-screen BLOCKING left-button
subscriptions at a priority above every window and blocker, so the press
that starts the box can't land on whatever sits under the cursor. Esc (or
the hotkey again) cancels.
"""

import glfw
import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core

# Above every window / blocker (the live lab's blocking handlers use 1024).
_PRIORITY_DELTA = 4096
_MIN_BOX_PX = 3          # smaller than this on release = a click, not a box
_LINE_COLOR = (1.0, 1.0, 1.0, 0.75)
_BOX_COLOR = (0.35, 0.75, 1.0, 1.0)
_FILL_COLOR = (0.35, 0.75, 1.0, 0.12)


class RegionScreenshot:
    armed = False
    start = None        # (x, y) in points where the drag began; None = no box yet


def arm():
    """Enter capture mode (idempotent)."""
    RegionScreenshot.armed = True
    RegionScreenshot.start = None
    request_render()


def cancel():
    RegionScreenshot.armed = False
    RegionScreenshot.start = None
    request_render()


def toggle():
    cancel() if RegionScreenshot.armed else arm()


def _open_captured(path):
    """screenshot.py's on_captured: runs on the render thread inside
    post_frame — defer the editor open to between frames, like any other
    external window mutation."""
    from src.lsd.gl_gui.view.playground.open_files import open_in_editor
    Melty.post_to_render(lambda: open_in_editor(path))


def _finish(x0, y0, x1, y1):
    """Release: queue the framebuffer read of the box (settled a couple of
    frames so this frame's overlay — crosshair, box — has cleared)."""
    from src.lsd.gl_gui.screenshot import request_region_capture
    RegionScreenshot.armed = False
    RegionScreenshot.start = None
    left, top = min(x0, x1), min(y0, y1)
    w, h = abs(x1 - x0), abs(y1 - y0)
    if w < _MIN_BOX_PX or h < _MIN_BOX_PX:
        # A click, not a box: stay armed so the user can try again.
        RegionScreenshot.armed = True
        request_render()
        return
    request_region_capture(left, top, w, h, Melty.frame_count,
                           name="screenshot", on_captured=_open_captured)


def draw(draw_state):
    """Per-frame body (from draw_main): claim the mouse, track the box, paint
    the crosshair/box on the overlay, and fire the capture on release."""
    if not RegionScreenshot.armed:
        return
    if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
        cancel()
        return

    io = imgui.get_io()
    full = (0.0, 0.0, float(io.display_size.x), float(io.display_size.y))
    sub = dict(view_id="region_shot", rect=full, priority_delta=_PRIORITY_DELTA)
    down = draw_state.on_action("left_mouse_down", **sub)
    drag = draw_state.on_action("left_mouse_drag", **sub)
    release = draw_state.on_action("left_mouse_drag_release", **sub)
    draw_state.on_action("left_mouse_up", **sub)     # claimed so nothing under us sees the click
    draw_state.on_action("left_mouse_click", **sub)

    mx, my = imgui.get_mouse_pos()
    if down is not None:
        RegionScreenshot.start = (float(down.x), float(down.y))
    if release is not None and RegionScreenshot.start is not None:
        sx, sy = RegionScreenshot.start
        _finish(sx, sy, float(release.x), float(release.y))
        return          # nothing painted this frame - the capture reads a clean frame
    if drag is None and RegionScreenshot.start is not None and not imgui.is_mouse_down(0):
        RegionScreenshot.start = None   # button went up without the drag activating

    overlay = imgui.get_overlay_draw_list()
    overlay.channels_set_current(Core.melty.max_layer - 1)
    line = imgui.get_color_u32_rgba(*_LINE_COLOR)
    overlay.add_line(full[0], my, full[2], my, line, 1.0)
    overlay.add_line(mx, full[1], mx, full[3], line, 1.0)

    if RegionScreenshot.start is not None:
        sx, sy = RegionScreenshot.start
        x0, y0, x1, y1 = min(sx, mx), min(sy, my), max(sx, mx), max(sy, my)
        overlay.add_rect_filled(x0, y0, x1, y1, imgui.get_color_u32_rgba(*_FILL_COLOR))
        overlay.add_rect(x0, y0, x1, y1, imgui.get_color_u32_rgba(*_BOX_COLOR), 0.0, 0, 1.0)
        label = f"{int(x1 - x0)} × {int(y1 - y0)}"
    else:
        label = f"{int(mx)}, {int(my)}  —  drag to capture, Esc to cancel"
    ts = imgui.calc_text_size(label)
    lx = mx + 12 if mx + 12 + ts.x < full[2] else mx - 12 - ts.x
    ly = my + 12 if my + 12 + ts.y < full[3] else my - 12 - ts.y
    overlay.add_rect_filled(lx - 3, ly - 1, lx + ts.x + 3, ly + ts.y + 1,
                            imgui.get_color_u32_rgba(0.0, 0.0, 0.0, 0.6), 3.0)
    overlay.add_text(lx, ly, imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.95), label)
    # The crosshair rides the cursor: keep frames coming while armed.
    request_render()
