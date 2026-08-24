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
import glfw
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

_GLFW_SHAPE = {
    TEXT: glfw.IBEAM_CURSOR,
    RESIZE_ALL: glfw.RESIZE_ALL_CURSOR,
    RESIZE_NS: glfw.RESIZE_NS_CURSOR,
    RESIZE_EW: glfw.RESIZE_EW_CURSOR,
    RESIZE_NESW: glfw.RESIZE_NESW_CURSOR,
    RESIZE_NWSE: glfw.RESIZE_NWSE_CURSOR,
    HAND: glfw.POINTING_HAND_CURSOR,
    NOT_ALLOWED: glfw.NOT_ALLOWED_CURSOR,
}

_cursors = {}        # shape -> GLFWcursor* (None = the GLFW default arrow)
_applied = ARROW     # shape last pushed to GLFW; None = unknown, re-push
_applied_immediate = False   # the last push was from imgui.set_mouse_cursor
_external = False    # another owner has set its own cursor image


def _glfw_cursor(shape):
    """The GLFW cursor object for `shape`, created once. A shape the
    platform's cursor theme can't provide (GLFW returns NULL / raises) falls
    back to the default arrow and is not retried."""
    if shape in _cursors:
        return _cursors[shape]
    std = _GLFW_SHAPE.get(shape)
    cur = None
    if std is not None:
        try:
            cur = glfw.create_standard_cursor(std)
        except Exception as e:
            print(f"mouse_cursor: no native cursor for shape {shape}: {e}")
            cur = None
    _cursors[shape] = cur
    return cur


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
    global _applied, _applied_immediate
    if _external:
        return
    if imgui.get_io().config_flags & imgui.CONFIG_NO_MOUSE_CURSOR_CHANGE:
        return
    shape = imgui.get_mouse_cursor()
    immediate = shape != ARROW and shape != imgui.MOUSE_CURSOR_NONE
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
