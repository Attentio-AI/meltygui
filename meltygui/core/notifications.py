import functools as _functools
import gc as _gc
import sys as _sys
import locale
import threading as _threading
import time
from collections import deque, defaultdict
from contextlib import contextmanager as _contextmanager

import meltygui.core.window_api as glfw
import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.core.fonts import Font
from meltygui.core.toggles import Toggles

# Use the system's locale time format (e.g. 12-hour AM/PM if configured)
# for %X instead of the default "C" locale 24-hour clock.
try:
    locale.setlocale(locale.LC_TIME, "")
except locale.Error:
    pass


def _timestamp():
    """Locale-aware time string (AM/PM on systems configured for 12-hour)."""
    return time.strftime("%X", time.localtime())


def _fade_opacity(created_at):
    """Opacity that decays with age: 100% fresh, 50% at ~10s, floored at 30%."""
    elapsed = time.time() - created_at
    return max(0.3, 1.0 - 0.05 * elapsed)

class NotificationCenter:
    max_notifications = 20
    notifications = deque(maxlen=max_notifications)
    tagged_notifications = defaultdict(lambda: deque(maxlen=NotificationCenter.max_notifications))
    ignore_tags = ["host"]
    # Single live value per tag (overwritten on each display() call), shown in
    # the dedicated "Live" column. Insertion order = row order.
    live_values = {}


class Notification:
    """One toast. `stack` is an optional list of (filename, lineno, funcname)
    tuples, OUTERMOST FIRST (the capture_stack() shape); `jump` is the
    (path, line) the entry opens in the code editor when clicked — by default
    the innermost stack frame. `count` is how many times the same (tag, text,
    stack) was notified while this entry was in the column."""
    __slots__ = ("text", "tint", "time_label", "created_at", "stack", "jump", "count")

    def __init__(self, text, tint, time_label, created_at, stack=None, jump=None):
        self.text = text
        self.tint = tint
        self.time_label = time_label
        self.created_at = created_at
        self.stack = stack
        self.jump = jump
        self.count = 1

    # Legacy 4-tuple unpacking (text, tint, time_label, created_at).
    def __iter__(self):
        return iter((self.text, self.tint, self.time_label, self.created_at))

    @property
    def key(self):
        return (self.text, self.jump, tuple(self.stack) if self.stack else None)

    @property
    def label(self):
        """Text as drawn: the message, a repeat count, and the jump site."""
        out = self.text if self.count == 1 else f"{self.text} ({self.count})"
        if self.jump is not None:
            path, line = self.jump
            out += f"\n{_short_path(path)}:{line}"
        return out

    @property
    def copy_text(self):
        """Clipboard form: the label plus the whole captured stack."""
        out = self.label
        if self.stack:
            out += "\n" + "\n".join(f'  File "{f}", line {ln}, in {fn}' for f, ln, fn in self.stack)
        return out


def _short_path(path):
    path = str(path)
    marker = "/src/"
    i = path.rfind(marker)
    return path[i + 1:] if i >= 0 else path.rsplit("/", 1)[-1]


def capture_stack(skip_files=(), skip_funcs=(), limit=12):
    """Cheap stack capture for notify(stack=...): walks live frames with
    sys._getframe (no source-line lookup, unlike traceback.extract_stack) and
    returns [(filename, lineno, funcname), ...] OUTERMOST FIRST, at most
    `limit` frames. Frames whose file ends with an entry of `skip_files` or
    whose function name is in `skip_funcs` are dropped from the INNER end
    until the first frame that is neither — so the innermost kept frame is
    the "caller" of the skipped plumbing. Returns [] off the interpreter."""
    f = _sys._getframe(1)
    out = []
    skipping = True
    while f is not None and len(out) < limit:
        code = f.f_code
        fn = code.co_filename
        if skipping and (fn.endswith(tuple(skip_files)) or code.co_name in skip_funcs
                         or fn.endswith("notifications.py")):
            f = f.f_back
            continue
        skipping = False
        out.append((fn, f.f_lineno, code.co_name))
        f = f.f_back
    out.reverse()
    return out


def notify(text, tint=(1,1,1,1), tag=None, urgent=False, stack=None, jump=None):
    """Push a toast into the `tag` column. `stack` (see capture_stack) makes
    the entry clickable: clicking opens `jump` — default the innermost stack
    frame — in the code editor. Repeats of the same (tag, text, stack) while
    the previous entry is still in the column bump its count (drawn as
    "(3)") and refresh its time instead of adding a row."""
    formatted_time = _timestamp()
    created_at = time.time()
    tint = tint if len(tint) == 4 else (*tint, 1)  # Ensure color has alpha
    if stack:
        stack = [tuple(fr[:3]) for fr in stack]
        if jump is None:
            jump = (stack[-1][0], stack[-1][1])
    if jump is not None:
        jump = (str(jump[0]), int(jump[1]))

    column = NotificationCenter.tagged_notifications["General" if tag is None else tag]
    entry = Notification(text, tint, formatted_time, created_at, stack=stack, jump=jump)
    for existing in column:
        if existing.key == entry.key:
            existing.count += 1
            existing.tint = tint
            existing.time_label = formatted_time
            existing.created_at = created_at
            column.remove(existing)
            column.appendleft(existing)
            break
    else:
        column.appendleft(entry)

    if urgent:
        from meltygui.core.glfw_utils import request_render
        request_render()


@_contextmanager
def lag_span(label, min_ms=50.0):
    """Notify (tag "lag") when the wrapped block ran slower than `min_ms` —
    near-zero cost when fast, so it can sit on hot paths. Includes the thread
    name: a slow span on a worker still stalls the render thread for its
    GIL-held portion, so every entry here is a frame-drop suspect."""
    # Stack of the SPAN SITE, captured on entry (a few µs - no source
    # lookup); clicking the toast opens the `with lag_span(...)` line. Skips
    # contextlib's generator plumbing and lag_traced's wrapper.
    stack = capture_stack(skip_files=("contextlib.py",), skip_funcs=("wrapper", "lag_span"))
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        if ms >= min_ms:
            tint = (1.0, 0.25, 0.2) if ms >= 300 else (1.0, 0.65, 0.2)
            notify(f"{label}  {ms:.0f}ms  [{_threading.current_thread().name}]",
                   tint=tint, tag="lag", stack=stack)


def lag_traced(label, min_ms=50.0):
    """Decorator form of lag_span — stamp on any function suspected of
    GIL-held stalls; it reports only when a call actually ran slow."""
    def deco(fn):
        @_functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with lag_span(label, min_ms=min_ms):
                return fn(*args, **kwargs)
        return wrapper
    return deco


# GC watch: a generation-2 collection walks every live object with the GIL
# held - with several large cst dicts resident that's an intermittent multi-
# hundred-ms stall attributed to whichever thread happened to allocate. The
# lazy install keeps a hotswap re-exec (which reuses module globals) from
# stacking callbacks.
_gc_start = {}


def _gc_watch(phase, info):
    gen = info.get("generation")
    if phase == "start":
        _gc_start[gen] = time.perf_counter()
        return
    t0 = _gc_start.pop(gen, None)
    if t0 is not None:
        ms = (time.perf_counter() - t0) * 1000.0
        if ms >= 20.0:
            # urgent=False: a gc callback can fire at ANY allocation point,
            # including mid-operation at boot - never pull request_render (and
            # its lazy glfw import) from here; the next UI frame shows it.
            try:
                # The stack is whatever allocation the collector interrupted -
                # still the best "who triggered it" there is.
                notify(f"gc gen{gen}  {ms:.0f}ms  collected={info.get('collected')}"
                       f"  [{_threading.current_thread().name}]",
                       tint=(1.0, 0.25, 0.2) if ms >= 300 else (1.0, 0.65, 0.2),
                       tag="lag", urgent=False, stack=capture_stack(skip_funcs=("_gc_watch",)))
            except Exception:
                pass


# One installed callback per PROCESS, tracked on sys: a module-global guard
# resets on the in-place restart's re-import, and each stacked callback pinned
# its session's module dict (and fired on every GC). Replace, don't append.
# (Sweeping by name also sheds the stack left by sessions predating this.)
_gc.callbacks[:] = [cb for cb in _gc.callbacks
                    if getattr(cb, "__name__", None) != "_gc_watch"]
_gc.callbacks.append(_gc_watch)
_sys._lsd_gc_watch = _gc_watch


def _format_value(value):
    """Render a variety of value types into a compact display string."""
    # scalar tensors / numpy scalars into a plain python number
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = value.item()
        except Exception:
            return str(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def display(value, tint=(1, 1, 1, 1), tag=None, urgent=True):
    """Show a single live value in the "Live" column, keyed by `tag`.

    Unlike notify(), which keeps a running list per tag, display() overwrites the
    value for a tag each call so it updates in place over time."""
    formatted_time = _timestamp()
    created_at = time.time()
    tint = tint if len(tint) == 4 else (*tint, 1)  # Ensure color has alpha
    key = tag if tag is not None else "value"
    NotificationCenter.live_values[key] = (_format_value(value), tint, formatted_time, created_at)

    # if urgent:
    #     from meltygui.core.glfw_utils import request_render
    #     request_render()


def _wrap_text(text, max_width):
    """Greedy word-wrap `text` into lines no wider than `max_width` px.
    Preserves explicit newlines. A single word wider than the limit is left on
    its own line (it overflows rather than being split mid-word)."""
    if max_width <= 0:
        return [text]
    # Memoized per (text, width): every visible toast re-wraps its text
    # twice a frame (height measure + draw), each a calc_text_size per word.
    memo_key = (text, max_width)
    hit = _WRAP_MEMO.get(memo_key)
    if hit is not None:
        return hit
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = word if not current else current + " " + word
            if not current or imgui.calc_text_size(candidate).x <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    if len(_WRAP_MEMO) > 1024:
        _WRAP_MEMO.clear()
    _WRAP_MEMO[memo_key] = lines
    return lines


_WRAP_MEMO = globals().get("_WRAP_MEMO", {})     # (text, max_width) → lines


def _entry_height(label, content, content_width, line_height, padding):
    """Vertical space one stacked entry consumes: wrapped block + bg padding +
    the inter-entry gap (matches _draw_column_entry's bg_top - padding step)."""
    label_size = imgui.calc_text_size(label)
    lines = _wrap_text(content, content_width - (label_size.x + padding))
    return max(1, len(lines)) * line_height + padding * 3


def _draw_column_entry(draw_list, column_left, content_width, line_height, padding,
                       bg_bottom, label, label_color, content, content_color, opacity=1.0,
                       hit_rects=None, copy_text=None, jump=None, clip=None):
    """Draw one stacked entry: a left-aligned `label` (time / tag name) followed
    by the wrapped, tinted `content`. `label_color`/`content_color` are RGBA
    tuples; `opacity` scales every alpha so the whole toast fades with age.
    When `hit_rects` is given, appends (rect, copy_text, jump) so the caller
    can make the entry click-to-copy (or click-to-jump when `jump` is a
    (path, line)); `clip` (a scroll viewport rect) clamps the hit rect so the
    scrolled-out part of an entry can't swallow clicks. Returns the next
    bg_bottom (above this one)."""
    label_size = imgui.calc_text_size(label)
    content_x = column_left + label_size.x + padding

    lines = _wrap_text(content, content_width - (label_size.x + padding))
    block_height = max(1, len(lines)) * line_height
    bg_top = bg_bottom - (block_height + padding * 2)
    content_top = bg_top + padding

    # The entry under the mouse is drawn at full opacity regardless of age.
    mouse = imgui.get_io().mouse_pos
    if (column_left - padding <= mouse.x <= column_left + content_width + padding
            and bg_top <= mouse.y <= bg_bottom):
        opacity = 1.0

    label_u32 = pack_color(label_color[0], label_color[1],
                                         label_color[2], label_color[3] * opacity)
    content_u32 = pack_color(content_color[0], content_color[1],
                                           content_color[2], content_color[3] * opacity)

    rect = (column_left - padding, bg_top, column_left + content_width + padding, bg_bottom)

    # background rectangle with some transparency
    draw_list.add_rect_filled(*rect, pack_color(0, 0, 0, opacity), rounding=2)

    if hit_rects is not None:
        hit_rect = rect if clip is None else (max(rect[0], clip[0]), max(rect[1], clip[1]),
                                              min(rect[2], clip[2]), min(rect[3], clip[3]))
        if hit_rect[0] < hit_rect[2] and hit_rect[1] < hit_rect[3]:
            hit_rects.append((hit_rect, content if copy_text is None else copy_text, jump))

    # label on the first line, then the wrapped, left-aligned content
    draw_list.add_text(column_left, content_top, label_u32, label)
    for li, line in enumerate(lines):
        draw_list.add_text(content_x, content_top + li * line_height, content_u32, line)

    return bg_top - padding


# Tags whose band is collapsed to its title strip (toggled by the tree arrow
# beside the title, see _draw_column_title / _handle_arrow_click). Hotswap-safe.
_collapsed_tags = globals().get("_collapsed_tags") or set()


def _draw_column_title(draw_list, x, y, color, title, hit_rects=None, copy_text=None,
                       arrow_rects=None, collapsed=False):
    """Draw a column's tree arrow + title. The arrow (▾ open / ▸ collapsed)
    is a click target that toggles the band (registered on `arrow_rects` as
    (rect, tag)); the title text is a click target that copies `copy_text`
    (the whole column). Returns the title's right edge."""
    # [tint=(0.95, 0.61, 0.07)]
    arrow_size = 8
    # [tint=(0.36, 0.68, 0.89)]
    arrow_gap = 6

    line_height = imgui.get_text_line_height()
    cx = x + arrow_size / 2
    cy = y + line_height / 2
    half = arrow_size / 2
    if collapsed:   # ▸
        draw_list.add_triangle_filled(cx - half / 2, cy - half, cx - half / 2, cy + half,
                                      cx + half, cy, color)
    else:           # ▾
        draw_list.add_triangle_filled(cx - half, cy - half / 2, cx + half, cy - half / 2,
                                      cx, cy + half, color)
    if arrow_rects is not None:
        arrow_rects.append(((x - 2, y, x + arrow_size + arrow_gap, y + line_height), title))

    text_x = x + arrow_size + arrow_gap
    draw_list.add_text(text_x, y, color, title)
    size = imgui.calc_text_size(title)
    if hit_rects is not None and copy_text:
        hit_rects.append(((text_x, y, text_x + size.x, y + size.y), copy_text, None))
    return text_x + size.x


def _handle_arrow_click(arrow_rects):
    """A click on a band's tree arrow toggles that band between its full
    height and just its title strip. Returns True when it consumed the click."""
    if not arrow_rects or not imgui.is_mouse_clicked(0):
        return False
    mouse = imgui.get_io().mouse_pos
    for (x0, y0, x1, y1), tag in arrow_rects:
        if x0 <= mouse.x <= x1 and y0 <= mouse.y <= y1:
            _collapsed_tags.symmetric_difference_update({tag})
            return True
    return False


def _draw_scrolled_column(draw_list, io, tag, rows, column_left, content_width,
                          line_height, padding, viewport_top, viewport_bottom,
                          hit_rects, badge_rects, show_badge=True):
    """Draw one category's rows inside a fixed-height scrolling viewport.

    `rows` is newest-first: (label, label_color, content, content_color,
    created_at, copy_text, jump). Newest sits at the viewport bottom. Scroll
    is per-`tag` and STICKY at the newest end: offset 0 follows new rows as
    they arrive; a scrolled-back column instead holds its place (the offset
    grows with the content) and shows a clickable "N new" badge (see
    _handle_badge_click) counting rows newer than the last pinned view."""
    # [tint=(0.95, 0.61, 0.07)]
    wheel_lines_per_tick = 3
    # [tint=(0.36, 0.68, 0.89)]
    thumb_width = 3
    # [tint=(0.72, 0.53, 0.94)]
    thumb_min_height = 24.0

    viewport = (column_left - padding, viewport_top,
                column_left + content_width + padding, viewport_bottom)
    viewport_height = viewport_bottom - viewport_top
    heights = [_entry_height(label, content, content_width, line_height, padding)
               for label, _lc, content, _cc, _t, _cp, _j in rows]
    content_height = sum(heights)
    max_offset = max(0.0, content_height - viewport_height)

    state = _scroll_state.get(tag)
    if state is None:
        state = _scroll_state[tag] = {"offset": 0.0, "height": 0.0, "seen_time": 0.0}

    # A scrolled-back view holds still while new rows grow the column's content.
    growth = content_height - state["height"]
    if state["offset"] > 0 and growth > 0:
        state["offset"] += growth
    state["height"] = content_height

    hovered = (viewport[0] <= io.mouse_pos.x <= viewport[2]
               and viewport[1] <= io.mouse_pos.y <= viewport[3])
    if hovered and io.mouse_wheel:
        # wheel-up = back toward older rows
        state["offset"] += io.mouse_wheel * line_height * wheel_lines_per_tick
    state["offset"] = min(max(state["offset"], 0.0), max_offset)
    offset = state["offset"]

    newest_time = max((created_at for _l, _lc, _c, _cc, created_at, _cp, _j in rows),
                      default=0.0)
    if offset <= 0:
        state["seen_time"] = newest_time
    unseen = (sum(1 for _l, _lc, _c, _cc, created_at, _cp, _j in rows
                  if created_at > state["seen_time"]) if offset > 0 else 0)

    # Stack rows upward from the bottom, shifted DOWN by the scroll offset so
    # older rows come into view; the clip rect swallows any overhang.
    draw_list.push_clip_rect(viewport[0], viewport[1], viewport[2], viewport[3], True)
    bg_bottom = viewport_bottom + offset
    for (label, label_color, content, content_color, created_at,
         copy_text, jump), height in zip(rows, heights):
        entry_top = bg_bottom - (height - padding)
        if entry_top > viewport_bottom:      # newest rows scrolled out below
            bg_bottom -= height
            continue
        if bg_bottom < viewport_top:         # everything further up is out too
            break
        opacity = _fade_opacity(created_at)
        bg_bottom = _draw_column_entry(draw_list, column_left, content_width,
                                       line_height, padding, bg_bottom,
                                       label, label_color, content, content_color, opacity,
                                       hit_rects=hit_rects, copy_text=copy_text,
                                       jump=jump, clip=viewport)
    draw_list.pop_clip_rect()

    # Slim scroll thumb on the column's right side while it overflows.
    if max_offset > 0 and (hovered or offset > 0):
        thumb_height = max(thumb_min_height,
                           viewport_height * viewport_height / content_height)
        travel = viewport_height - thumb_height
        thumb_bottom = viewport_bottom - (offset / max_offset) * travel
        draw_list.add_rect_filled(viewport[2] - thumb_width, thumb_bottom - thumb_height,
                                  viewport[2], thumb_bottom,
                                  pack_color(1, 1, 1, 0.25), rounding=1)

    if show_badge and unseen:
        _draw_new_badge(draw_list, tag, unseen, column_left, content_width,
                        line_height, padding, viewport_bottom, badge_rects)


def _draw_new_badge(draw_list, tag, unseen, column_left, content_width,
                    line_height, padding, viewport_bottom, badge_rects):
    """Small "N new" pill at the bottom of a scrolled-back column; clicking it
    (see _handle_badge_click) jumps the column back to the sticky end."""
    # [tint=(0.95, 0.61, 0.07)]
    arrow_width = 6
    # [tint=(0.36, 0.68, 0.89)]
    pill_extra_width = 6

    text = f"{unseen} new"
    text_size = imgui.calc_text_size(text)
    pill_width = text_size.x + arrow_width + padding * 4 + pill_extra_width
    pill_height = line_height + padding * 2
    x0 = column_left + (content_width - pill_width) / 2
    y1 = viewport_bottom - padding * 2
    y0 = y1 - pill_height
    yellow = pack_color(1, 1, 0, 1)
    draw_list.add_rect_filled(x0, y0, x0 + pill_width, y1,
                              pack_color(0, 0, 0, 0.9),
                              rounding=pill_height / 2)
    draw_list.add_rect(x0, y0, x0 + pill_width, y1, yellow,
                       rounding=pill_height / 2, thickness=1.0)
    text_x = x0 + padding * 2 + 2
    draw_list.add_text(text_x, y0 + padding, yellow, text)
    # down-pointing triangle after the text ("new toasts are below")
    arrow_x = text_x + text_size.x + 4
    arrow_y = y0 + pill_height / 2
    draw_list.add_triangle_filled(arrow_x, arrow_y - 3, arrow_x + arrow_width, arrow_y - 3,
                                  arrow_x + arrow_width / 2, arrow_y + 3, yellow)
    badge_rects.append(((x0, y0, x0 + pill_width, y1), tag))


# Per-tag scroll state, keyed by column title. "offset" is pixels scrolled
# back from the newest end - 0 means pinned to the end, so new toasts
# auto-scroll into view (sticky). "height" is last frame's content height,
# used to hold a scrolled-back view still while new toasts grow the column.
# "seen_time" is the newest created_at that was on screen while pinned;
# anything newer feeds the "N new" badge. Hotswap-safe.
_scroll_state = globals().get("_scroll_state") or {}


def _file_tint(path):
    """The tint the user painted on `path` in the code editor (FileMeta), as
    RGBA, or None. Same store the editor tabs / folder tree read."""
    try:
        from meltygui.editor.source_ui import _file_meta_tint
        rgb = _file_meta_tint(path)
    except Exception:
        return None
    return (*rgb, 1) if rgb else None


# Rect of the most recently copied entry and when it was copied, so the click gets
# a brief visual confirmation. Guarded so a hotswap re-exec keeps the value.
_copy_flash = globals().get("_copy_flash")
_COPY_FLASH_SECONDS = 0.6


def _handle_badge_click(badge_rects):
    """A click on a column's "N new" badge jumps that column back to the
    sticky end (offset 0), where the seen-marker resets on the next frame.
    Returns True when a badge consumed the click."""
    if not badge_rects or not imgui.is_mouse_clicked(0):
        return False
    mouse = imgui.get_io().mouse_pos
    for (x0, y0, x1, y1), tag in badge_rects:
        if x0 <= mouse.x <= x1 and y0 <= mouse.y <= y1:
            state = _scroll_state.get(tag)
            if state is not None:
                state["offset"] = 0.0
            return True
    return False


def _handle_entry_click(hit_rects):
    """Click handling: a click inside an entry with a `jump` opens that
    (path, line) in the code editor (global search's jump path); otherwise —
    and always with ctrl held — the entry's text goes on the clipboard. Either
    way the entry flashes."""
    global _copy_flash
    if not hit_rects or not imgui.is_mouse_clicked(0):
        return
    io = imgui.get_io()
    mx, my = io.mouse_pos.x, io.mouse_pos.y
    # Later entries are drawn above earlier ones and never overlap, so the first
    # containing rect is the hit.
    for (x0, y0, x1, y1), text, jump in hit_rects:
        if x0 <= mx <= x1 and y0 <= my <= y1:
            if jump is not None and not io.key_ctrl:
                from meltygui.core.extensions import open_source as open_in_editor
                open_in_editor(jump[0], jump[1])
            else:
                imgui.set_clipboard_text(text)
            _copy_flash = ((x0, y0, x1, y1), time.time())
            return


def _draw_copy_flash(draw_list):
    """Outline the entry that was just copied, fading out over ~0.6s."""
    if _copy_flash is None:
        return
    (x0, y0, x1, y1), copied_at = _copy_flash
    remaining = _COPY_FLASH_SECONDS - (time.time() - copied_at)
    if remaining <= 0:
        return
    alpha = remaining / _COPY_FLASH_SECONDS
    draw_list.add_rect(x0, y0, x1, y1,
                       pack_color(1, 1, 0, alpha), rounding=2, thickness=1.5)
    from meltygui.core.glfw_utils import request_render
    request_render()


def draw_notifications():
    io = imgui.get_io()
    display_size = io.display_size
    draw_list = imgui.get_overlay_draw_list()

    from meltygui.core.melty import Melty
    font_handle = Melty.font_mgr.get(Font.JETBRAINS_MONO_14) if Melty.font_mgr else None
    if font_handle is not None:
        imgui.push_font(font_handle)

    try:
        # [tint=(0.36, 0.68, 0.89)]
        padding = 2
        # [tint=(0.95, 0.61, 0.07)]
        column_width = 300                          # also the toast max width
        # Bottom strip of each band that its title text sits in.
        # [tint=(0.72, 0.53, 0.94)]
        title_strip = 30
        # Gap between the bands and the window's right edge.
        # [tint=(0.42, 0.79, 0.42)]
        edge_margin = 10
        # Per-category band height - a Toggles knob, applied live.
        category_height = Toggles.Notifications.category_height

        content_width = column_width - padding * 2  # left-aligned content box
        line_height = imgui.get_text_line_height()
        title_color = pack_color(1, 1, 0, 1)

        tagged_columns = [(tag, notifications) for tag, notifications
                          in NotificationCenter.tagged_notifications.items()
                          if tag not in NotificationCenter.ignore_tags]
        live_entries = [(tag + " ", value_str)
                        for tag, (value_str, _c, _t, _a) in NotificationCenter.live_values.items()]

        n_columns = len(tagged_columns) + (1 if live_entries else 0)
        if n_columns == 0:
            return

        # Categories stack VERTICALLY along the right edge: the first tag's
        # history in the bottom-right corner, each further category in its own
        # fixed-height band above it, "Live" topmost. Within each band the title
        # is at the bottom with the scrolling viewport above it.
        column_left = display_size.x - column_width - edge_margin
        hit_rects = []
        badge_rects = []
        arrow_rects = []
        # Bottom of the NEXT band to lay out; bands stack upward, a collapsed
        # band taking only its title strip so the ones above it pack down.
        next_band_bottom = [display_size.y]

        def band_rect(tag):
            """(title_top, viewport_top, viewport_bottom, collapsed) of `tag`'s
            band, stacked above the previously laid-out one."""
            collapsed = tag in _collapsed_tags
            band_bottom = next_band_bottom[0]
            title_top = band_bottom - title_strip
            height = title_strip if collapsed else category_height
            next_band_bottom[0] = band_bottom - height
            return title_top, band_bottom - height, title_top - padding, collapsed

        for tag, notifications in tagged_columns:
            title_top, viewport_top, viewport_bottom, collapsed = band_rect(tag)

            # tag title pinned at the bottom of the band; clicking it copies
            # the band's whole history, not just the entries on screen.
            _draw_column_title(draw_list, column_left, title_top, title_color, tag,
                               hit_rects, "\n".join(n.copy_text for n in notifications),
                               arrow_rects=arrow_rects, collapsed=collapsed)
            if collapsed:
                continue

            rows = []
            for n in notifications:
                # Entries that jump to a file take that file's editor tint.
                tint = (_file_tint(n.jump[0]) if n.jump is not None else None) or n.tint
                rows.append((n.time_label, tint, n.label, tint, n.created_at,
                             n.copy_text, n.jump))
            _draw_scrolled_column(draw_list, io, tag, rows, column_left, content_width,
                                  line_height, padding, viewport_top, viewport_bottom,
                                  hit_rects, badge_rects)

        # dedicated "Live" band above the tagged notification bands - each row
        # is one tag's current value, tinted, updated in place over time - so
        # it scrolls like the others but skips the "new" badge (in-place
        # updates would keep it lit permanently).
        if live_entries:
            title_top, viewport_top, viewport_bottom, collapsed = band_rect("Live")
            label_color = (0.6, 0.6, 0.6, 1)

            _draw_column_title(draw_list, column_left, title_top, title_color, "Live",
                               hit_rects, "\n".join(f"{label}{value_str}"
                                                    for label, value_str in live_entries),
                               arrow_rects=arrow_rects, collapsed=collapsed)

        if live_entries and not collapsed:
            rows = [(live_tag + " ", label_color, value_str, color, created_at, None, None)
                    for live_tag, (value_str, color, _time_label, created_at)
                    in NotificationCenter.live_values.items()]
            _draw_scrolled_column(draw_list, io, "Live", rows, column_left, content_width,
                                  line_height, padding, viewport_top, viewport_bottom,
                                  hit_rects, badge_rects, show_badge=False)

        if not _handle_arrow_click(arrow_rects) and not _handle_badge_click(badge_rects):
            _handle_entry_click(hit_rects)
        _draw_copy_flash(draw_list)
    finally:
        if font_handle is not None:
            imgui.pop_font()

