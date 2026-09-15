"""Region screenshot tool — Ctrl+Shift+3 (draw_main's root hotkey) or
Actions.screenshot arms it; a crosshair follows the cursor on the overlay
draw list; click-drag a box; on release the pixels inside the box are read
from the main framebuffer (GL_BACK, after the frame is fully composited — the
deferred capture queue in screenshot.py), saved as a PNG under the configured
shot dir, added to the code editor as a tab (its ImageCodec view) WITHOUT
switching to it, and its path is put on the clipboard.

State is module-level (one tool, never more than one capture in flight);
`draw(draw_state)` runs from draw_main every frame and is a no-op unless
armed. While armed the tool owns the mouse: full-screen BLOCKING left-button
subscriptions at a priority above every window and blocker, so the press
that starts the box can't land on whatever sits under the cursor. Esc (or
the hotkey again) cancels.

The crosshair + camera are the CURSOR IMAGE while armed (glfw.create_cursor
from a PIL render, hotspot at the crosshair centre), not overlay drawing:
anything drawn in a frame uses the frame-start mouse sample and shows a
frame later, while the compositor moves the cursor plane with zero latency
— overlay crosshairs visibly trailed the pointer. The cursor plane is the
one thing that can sit exactly where the pointer is. Only the drag box
(anchored at the press) is drawn on the overlay, with a readout pill beside
it — top-left `x, y` and `w × h` in window points — so the tool doubles as
a measure tool (drag over a thing, read its rect off the label, Esc).
"""

from src.lsd.gl_gui import window_api as glfw
import imgui
from src.lsd.gl_gui.hdr_color import pack_color

from src.lsd.gl_gui import mouse_cursor
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core

# Above every window / blocker (the live lab's blocking handlers use 1024).
_PRIORITY_DELTA = 4096
_MIN_BOX_PX = 3          # smaller than this on release = a click, not a box
_ICON = "\uf030"        # FA camera (baked into the cursor image)
_BOX_COLOR = (0.35, 0.75, 1.0, 1.0)
_FILL_COLOR = (0.35, 0.75, 1.0, 0.12)
_CURSOR_SIZE, _CURSOR_HOT, _CURSOR_ARM, _CURSOR_GAP = 64, 20, 16, 3


class RegionScreenshot:
    armed = False
    start = None        # (x, y) in points where the drag began; None = no box yet
    cursor = None       # *cursor* (built once, render thread)
    cursor_set = False  # our cursor is the window's current cursor


def _build_cursor_image():
    """Crosshair (gap around the hotspot, dark halo under a white hairline)
    with the FA camera glyph below-right — the same glyph Actions.screenshot
    wears. Returns (PIL image, hotspot)."""
    from PIL import Image, ImageDraw, ImageFont
    from src.lsd.gl_gui.fonts import _RESOURCES
    size, hot, arm, gap = _CURSOR_SIZE, _CURSOR_HOT, _CURSOR_ARM, _CURSOR_GAP
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for col, w in (((0, 0, 0, 140), 3), ((255, 255, 255, 230), 1)):
        for seg in ((hot - arm, hot, hot - gap, hot), (hot + gap, hot, hot + arm, hot),
                    (hot, hot - arm, hot, hot - gap), (hot, hot + gap, hot, hot + arm)):
            d.line(seg, fill=col, width=w)
    font = ImageFont.truetype(str(_RESOURCES / "fontawesome-webfont.ttf"), 22)
    gx, gy = hot + 9, hot + 7
    d.text((gx + 1, gy + 1), _ICON, font=font, fill=(0, 0, 0, 150))
    d.text((gx, gy), _ICON, font=font, fill=(255, 255, 255, 235))
    return img, hot


def _set_tool_cursor(on):
    """Swap the window cursor to the crosshair/camera image (on) or back to
    the default (off). GLFW calls belong on the event-pumping thread — the
    render thread — which is where draw() runs."""
    if RegionScreenshot.cursor_set == on:
        return
    window = glfw.get_current_context()
    if window is None:
        return
    try:
        if on and RegionScreenshot.cursor is None:
            img, hot = _build_cursor_image()
            RegionScreenshot.cursor = glfw.create_cursor(img, hot, hot)
        glfw.set_cursor(window, RegionScreenshot.cursor if on else None)
        RegionScreenshot.cursor_set = on
        # Keep the per-frame shape push (mouse_cursor.apply) from our image.
        mouse_cursor.note_external_cursor(on)
    except Exception as e:
        print(f"region_screenshot: cursor swap failed: {e}")


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
    post_frame — defer the editor work to between frames, like any other
    external window mutation. The shot becomes an editor TAB without
    stealing the selection (OpenFiles.open_file only — not open_in_editor,
    whose jump_to_path the editor adopts as its active tab): the user is
    mid-work in whatever they were screenshotting. The tab bars repaint so
    the new tab shows. The saved file's PATH also goes on the clipboard
    (text, via GLFW's clipboard — the studio is the focused Wayland client,
    so this is the one clipboard write that always lands)."""
    from src.lsd.gl_gui.view.playground.open_files import editor_window_draw_state

    def _land():
        open_files = getattr(getattr(Melty.vis, "root", None), "open_files", None)
        if open_files is not None:
            open_files.open_file(path)
            # The tab list changed under the editors' bodies: force both
            # instances through their blit cache so the new tab appears.
            for inst in (0, 1):
                win = editor_window_draw_state(inst)
                if win is not None and Melty.cache is not None and win._tile_id is not None:
                    Melty.cache.invalidate_up(win._tile_id, force=True, max_depth=4)
        imgui.set_clipboard_text(str(path))
        request_render()
    Melty.post_to_render(_land)


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


def box_label_lines(x0, y0, x1, y1):
    """The readout for a normalized box (x0 <= x1, y0 <= y1), in WINDOW
    POINTS — the coordinate system every draw_state rect and the capture
    itself use, so a measurement read off the label maps straight onto
    `draw_state.abs_left` / `width` values. Line 1 = top-left corner,
    line 2 = size."""
    x, y = int(round(x0)), int(round(y0))
    w, h = int(round(x1 - x0)), int(round(y1 - y0))
    return (f"{x}, {y}", f"{w} × {h}")


def label_rect(x0, y0, x1, y1, label_w, label_h, display_w, display_h, gap):
    """Where the readout pill goes: below the box, right-aligned to its
    right edge (outside, so the measured content stays visible). Off the
    bottom of the display → inside the box's bottom-right corner; past the
    right edge → slid left to fit; a box that fills the screen → inside,
    clamped. Returns (left, top)."""
    left = x1 - label_w
    top = y1 + gap
    if top + label_h > display_h:
        top = y1 - gap - label_h
        left = x1 - gap - label_w
    left = max(0.0, min(left, display_w - label_w))
    top = max(0.0, top)
    return left, top


def _draw_box_label(overlay, x0, y0, x1, y1, display_w, display_h):
    """Paint the coordinate/size readout next to the drag box (the "measure
    tool" half of the feature): a dark pill, box-coloured text."""
    # [tint=(0.35, 0.75, 1.0)]
    padding = 5
    # [tint=(0.95, 0.61, 0.07)]
    gap = 6
    # [tint=(0.36, 0.68, 0.89)]
    label_background = (0.05, 0.05, 0.08, 0.85)
    lines = box_label_lines(x0, y0, x1, y1)
    sizes = [imgui.calc_text_size(line) for line in lines]
    text_w = max(size.x for size in sizes)
    line_h = sizes[0].y
    label_w = text_w + padding * 2
    label_h = line_h * len(lines) + padding * 2
    left, top = label_rect(x0, y0, x1, y1, label_w, label_h, display_w, display_h, gap)
    overlay.add_rect_filled(left, top, left + label_w, top + label_h,
                            pack_color(*label_background), rounding=4.0)
    overlay.add_rect(left, top, left + label_w, top + label_h,
                     pack_color(*_BOX_COLOR), rounding=4.0, thickness=1.0)
    text_color = pack_color(*_BOX_COLOR)
    for index, line in enumerate(lines):
        overlay.add_text(left + padding, top + padding + line_h * index, text_color, line)


def draw(draw_state):
    """Per-frame body (from draw_main): claim the mouse, track the box, paint
    the crosshair/box + its coordinate/size readout on the overlay, and fire
    the capture on release."""
    if not RegionScreenshot.armed:
        _set_tool_cursor(False)     # back to the default after cancel / esc
        return
    if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
        cancel()
        _set_tool_cursor(False)
        return
    _set_tool_cursor(True)

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

    if RegionScreenshot.start is not None:
        overlay = imgui.get_overlay_draw_list()
        overlay.channels_set_current(Core.melty.max_layer - 1)
        sx, sy = RegionScreenshot.start
        x0, y0, x1, y1 = min(sx, mx), min(sy, my), max(sx, mx), max(sy, my)
        overlay.add_rect_filled(x0, y0, x1, y1, pack_color(*_FILL_COLOR))
        overlay.add_rect(x0, y0, x1, y1, pack_color(*_BOX_COLOR), 0.0, 0, 1.0)
        _draw_box_label(overlay, x0, y0, x1, y1, full[2], full[3])
        # The box's moving edge tracks the cursor: keep frames coming.
        request_render()
