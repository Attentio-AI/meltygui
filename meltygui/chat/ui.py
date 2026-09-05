"""Chat studio: draw-list navigation/transcript over provider-owned dictionaries."""
import colorsys
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path
import uuid

import imgui
import glfw

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.chat.messages import (Message, AssistantMessage, UserMessage, ToolCall,
    ReasoningMessage, PythonString, CodeString, Reference, user_message, BashString, ToolOutput, FileTags, CommandExecution)
from src.lsd.gl_gui.toggles import Tint, Toggles
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.columns import ColumnLayout
from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw_dropdown, draw_tuple_fast, draw_bg
from src.lsd.gl_gui.view.core_views.headers import flat_button, draw_header_arrow
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.text_editor import draw_text
from src.lsd.gl_gui.view.playground import internet_accounts


@no_save("revision", "viewports", "text_layouts", "rename")
class ChatInterfaceState(DictConversion):
    def __init__(self):
        super().__init__()
        self.provider = ""
        self.account = ""
        self.selected = {}
        self.drafts = {}
        self.projects = {}
        self.follow = True
        self.answers = {}
        self.revision = 0
        self.viewports = {}
        self.text_layouts = {}
        self.rename = None
        self.message_expanded = {}
        self.output_expanded = {}


def _color(tint, alpha=1):
    return imgui.get_color_u32_rgba(*tint[:3], alpha)


def _button(draw_state, key, label, x, y, width, tint, enabled=True, height=None,
            selected=False, background=True):
    cursor = imgui.get_cursor_screen_pos()
    try:
        imgui.set_cursor_screen_pos((x, y))
        return flat_button(label, draw_state if enabled else None, key,
            pos=(x, y), width=width, height=height or Melty.px(25),
            color=tint, alpha=(1 if enabled else 0.4) if background else 0,
            tint_value=0.23 if selected else 0.16,
            shadow_offset=11 if selected else 6, hovered=None if enabled else False)
    finally:
        imgui.set_cursor_screen_pos(cursor)


def _tint_chip(meta, draw_state, key, x, y):
    changed, tint = draw_tuple_fast(tuple(meta["tint"]), draw_state, key,
        x=x, y=y, size=Melty.px(17), outline=True, priority_delta=6,
        setter=lambda value: meta.__setitem__("tint", value))
    if changed:
        meta["tint"] = tint
    return changed


def _reorder(chats, key, direction):
    keys = list(chats)
    index = keys.index(key)
    # Move within this project's group; keep other projects untouched.
    project = dict.__getitem__(chats, key)["project"]
    candidates = [other for other in keys if dict.__getitem__(chats, other)["project"] == project]
    position = candidates.index(key) + direction
    if not 0 <= position < len(candidates):
        return
    other_index = keys.index(candidates[position])
    keys[index], keys[other_index] = keys[other_index], keys[index]
    for item in keys:
        chats[item] = chats.pop(item)


def _soft_wrap(text, columns):
    """Word-wrap each line to `columns` monospace cells; indentation and blank lines survive."""
    if columns <= 0:
        return text
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip(" \t")
        lead = line[:len(line) - len(stripped)]
        room = max(1, columns - len(lead.expandtabs(4)))
        while len(stripped) > room:
            cut = stripped.rfind(" ", 0, room + 1)
            if cut <= 0:
                cut = room  # one word wider than the row: break it
            out.append(lead + stripped[:cut].rstrip())
            stripped = stripped[cut:].lstrip(" ")
        out.append(lead + stripped)
    return "\n".join(out)


def _text_layout(state, key, text, prefix="", font=Font.FONTAWESOME_MONO_19, wrap_width=None, keep=False):
    """Stable display buffer and height; no per-frame string/kwargs churn.

    Measure in the same font as draw_text. Explicit line breaks and a fixed
    height keep offscreen layout identical to onscreen layout, like the
    measured-pane skips in draw_stack_trace. `wrap_width` (px) soft-wraps the
    DISPLAY buffer here — draw_text itself never reflows — so the height is
    the wrapped height and the memo re-runs when the width changes.
    """
    font = Melty.font_mgr.get(font) if Melty.font_mgr else None
    signature = (text, type(text), getattr(text, "language", None), Melty.ui_scale, font, prefix, "trimmed", wrap_width)
    memo = state.text_layouts.get(key)
    if memo is not None and memo[0][0] is text and (memo[0][1:] == signature[1:]
                                                     or keep and memo[0][1:-1] == signature[1:-1]):
        # `keep`: an off-screen leaf mid-resize keeps its last wrap, whatever width it was in.
        return memo[1], memo[2]
    if font is not None:
        imgui.push_font(font)
    try:
        line_px = imgui.get_text_line_height() * 1.2
        display = prefix + text if prefix else text
        if wrap_width is not None:
            # The editor's text is a little in on its left edge; leave that slack.
            columns = int((wrap_width - Melty.px(16)) / max(1.0, imgui.calc_text_size("M").x))
            display = _soft_wrap(str(display), columns)
        if display.endswith(("\n", "\r")) or display is not text:
            trimmed = display.rstrip("\r\n")
            display = type(text)(trimmed, text.language) if isinstance(text, CodeString) else type(text)(trimmed)
        text_width = max((imgui.calc_text_size(line).x for line in display.split("\n")), default=0)
        memo = (signature, display, (display.count("\n") + 1) * line_px, text_width)
        state.text_layouts[key] = memo
        return memo[1], memo[2]
    finally:
        if font is not None:
            imgui.pop_font()


def _scroll_position(view, content_height, height, wheel=0, drag_fraction=None):
    maximum = max(0.0, content_height - height)
    offset = view.get("offset", 0.0)
    if wheel:
        speed = min(Toggles.ScrollSettings.scroll_speed,
                    Toggles.ScrollSettings.max_increment_fraction * height)
        offset -= wheel * speed
        view["follow"] = False
    if drag_fraction is not None:
        offset = maximum * drag_fraction
        view["follow"] = False
    offset = max(0.0, min(offset, maximum))
    if view.get("follow"):
        offset = maximum
    elif (wheel < 0 or drag_fraction is not None) and maximum > 0 and offset >= maximum - 1:
        view["follow"] = True
    view["offset"] = offset
    return offset


@contextmanager
def _viewport(draw_state, state, key, width, height, content_height, follow=False):
    """One clipped, independently scrolling region, owned by the window.

    Only its visible footprint advances layout; content offsets never move
    the composer or change the window's own scroll geometry.
    """
    view = state.viewports.setdefault(key, {"offset": 0.0, "follow": follow})
    x, y = imgui.get_cursor_screen_pos()
    rect = (x, y, x + width, y + height)
    wheel = draw_state.on_action("scroll_y_changed", view_id=key + ":wheel",
                                 rect=rect, priority_delta=40)
    offset = _scroll_position(view, content_height, height, wheel.value if wheel is not None else 0)
    maximum = max(0, content_height - height)
    bar_width = Melty.px(7)
    thumb_height = min(height, max(Melty.px(24), height * height / max(height, content_height)))
    travel = height - thumb_height
    if maximum > 0 and travel > 0:
        thumb_y = y + travel * offset / maximum
        grab = (x + width - bar_width, thumb_y, x + width, thumb_y + thumb_height)
        for event in ("left_mouse_down", "left_mouse_held", "left_mouse_clicked"):
            draw_state.on_action(event, view_id=key + ":grab", rect=grab, priority_delta=50)
        drag = draw_state.on_action("left_mouse_drag", view_id=key + ":grab", rect=grab, priority_delta=50)
        if drag is not None:
            offset = _scroll_position(view, content_height, height,
                                      drag_fraction=offset / maximum + drag.dy / travel)
    Melty.push_clip(rect)
    try:
        yield x, y - offset, Melty.get_clip_rect()
        if maximum > 0 and travel > 0:
            thumb_y = y + travel * offset / maximum
            add_shadow((x + width - bar_width, thumb_y, bar_width, thumb_height),
                       offset=0, corner_radius=3)
            imgui.get_window_draw_list().add_rect_filled(
                x + width - bar_width, thumb_y, x + width, thumb_y + thumb_height,
                _color((0.55, 0.65, 0.65)), rounding=3)
    finally:
        Melty.pop_clip()
        imgui.set_cursor_screen_pos((x, y))
        imgui.dummy(width, height)


def _visible(y, height, clip):
    return clip is None or y + height > clip[1] and y < clip[3]


@lru_cache(maxsize=128)
def _tint_style(tint):
    from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager
    style = ImGuiStyleManager()
    style.current_rgb = tint[:3]
    style.hsv = colorsys.rgb_to_hsv(*tint[:3])
    return style


@lru_cache(maxsize=128)
def _text_tint(tint):
    # The editor's neutral foreground, nudged toward the item's hue.
    neutral = (169 / 255, 183 / 255, 198 / 255)
    hue = Tint.dd_text(tint[:3])
    return tuple(a * 0.8 + b * 0.2 for a, b in zip(neutral, hue))


def _card(x, y, width, height, tint, selected=False):
    draw_list = imgui.get_window_draw_list()
    body_channel = Melty.get_channel()
    if Melty.channels_split:
        draw_list.channels_set_current(body_channel - 1)
    try:
        add_shadow((x, y, width, height), offset=2 if selected else 1,
                   corner_radius=Melty.px(6))
        _, color = draw_bg(left=x, top=y, width=width, height=height,
                style_manager=_tint_style(tuple(tint)), opacity=1, outline=False,
                rounding=Melty.px(6), max_bg_depth=1 if selected else 0,
                max_bg_value=0.23 if selected else 0.18, selected=selected)
        return color
    finally:
        if Melty.channels_split:
            draw_list.channels_set_current(body_channel)


def _label_width(text):
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        return imgui.calc_text_size(text).x
    finally:
        if font is not None:
            imgui.pop_font()


def _draw_prose(text, x, y, width, height, tint, clip=None):
    """User / assistant prose straight to the draw list: pre-wrapped lines, editor
    font, no render wrapper, no child tile. Rows outside `clip` are skipped."""
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    Melty.push_clip((x, y, x + width, y + height))
    try:
        line_px = imgui.get_text_line_height() * 1.2
        color = _color(_text_tint(tuple(tint)))
        for index, line in enumerate(str(text).split("\n")):
            top = y + index * line_px
            if _visible(top, line_px, clip) and line:
                draw_list.add_text(x + Melty.px(4), top, color, line)
    finally:
        Melty.pop_clip()
        if font is not None:
            imgui.pop_font()


def _icon_chip(icon, x, y, height, color):
    """A small flat rounded background hugging one header glyph; returns its width."""
    width = _label_width(icon) + Melty.px(8)
    draw_list = imgui.get_window_draw_list()
    channel = Melty.get_channel()
    if Melty.channels_split:
        draw_list.channels_set_current(channel - 1)
    try:
        draw_list.add_rect_filled(x - Melty.px(4), y + Melty.px(2), x - Melty.px(4) + width, y + height - Melty.px(2),
                                  _color(color), rounding=Melty.px(4))
    finally:
        if Melty.channels_split:
            draw_list.channels_set_current(channel)
    return width


def _title(text, x, y, width, height, tint):
    """Navigation labels have no render wrapper or child tile."""
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    Melty.push_clip((x, y, x + width, y + height))
    try:
        draw_list.add_text(x, y + max(0, (height - imgui.get_text_line_height()) / 2),
                           _color(_text_tint(tuple(tint))), text)
    finally:
        Melty.pop_clip()
        if font is not None:
            imgui.pop_font()


def _apply_chat_drop(chats, keys, drop):
    """Dock drop indices refer to registered rows, including offscreen rows."""
    if drop is None or drop.kind != "reorder" or drop.key not in keys:
        return False
    project = chats.get(drop.key)["project"]
    group = [i for i, key in enumerate(keys) if chats.get(key)["project"] == project]
    if not min(group) <= drop.insert_index <= max(group) + 1:
        return False
    rows = {key: chats.get(key) for key in keys}
    if not drop.apply(rows):
        return False
    # Collapsed projects retain their order. Mutations are dict-only;
    # the proxy records order without archiving deleted-and-reinserted rows.
    reordered = iter(rows)
    order = [next(reordered) if key in rows else key for key in chats]
    for key in order:
        chats[key] = chats.pop(key)
    return True


def draw_chat_sidebar(chats, draw_state, state, width, height):
    """Immediate layout; visible text leaves own their cached render tiles."""
    changed = False
    groups = {}
    cards = []
    archive_key = None
    trash_icon = f""
    trash_width = Melty.px(25)
    row_width = max(Melty.px(80), width - Melty.px(7))
    gap, pad = Melty.px(2), Melty.px(3)
    for key, chat in chats.items():
        groups.setdefault(chat["project"], []).append((key, chat))
    for project in sorted(groups, key=lambda name: chats.projects[name]["order"]):
        meta = chats.projects[project]
        label, heading_height = _text_layout(state, "project:" + project, Path(project).name or "No project")
        heading_height += Melty.px(2)
        children = []
        if meta["expanded"]:
            for key, chat in groups[project]:
                text, row_height = _text_layout(state, "chat:" + key, chat["title"])
                children.append((key, chat.metadata, text, row_height + Melty.px(2)))
        card_height = pad * 2 + heading_height + sum(row[3] + gap for row in children)
        cards.append((project, meta, label, heading_height, children, card_height))
    registered = []
    total = sum(card[5] + gap for card in cards)
    with _viewport(draw_state, state, "sidebar:" + state.account, width, height, total) as (x, y, clip):
        for project, meta, label, heading_height, children, card_height in cards:
            if _visible(y, card_height, clip):
                _card(x, y, row_width, card_height, meta["tint"])
            heading_y = y + pad
            if _visible(heading_y, heading_height, clip):
                imgui.set_cursor_screen_pos((x + pad, heading_y))
                imgui.push_id("project-arrow:" + state.account + ":" + project)
                try:
                    if draw_header_arrow(meta["expanded"]):
                        meta["expanded"] = not meta["expanded"]
                        changed = True
                finally:
                    imgui.pop_id()
                changed |= _tint_chip(meta, draw_state, "project:" + project + ":tint",
                                      x + Melty.px(23), heading_y + max(0, (heading_height - Melty.px(17)) / 2))
                _title(label, x + Melty.px(46), heading_y,
                       row_width - Melty.px(46), heading_height, meta["tint"])
            child_y = heading_y + heading_height + gap
            for key, child_meta, text, row_height in children:
                registered.append(key)
                rect = (x + Melty.px(46), child_y, x + row_width - trash_width, child_y + row_height)
                renaming = getattr(state, "rename", None)
                editing = renaming is not None and renaming["account"] == state.account and renaming["key"] == key
                # Register every laid-out row so drop indices do not depend on scroll.
                drag = DragDrop.on_drag(rect, key=key, value=chats.get(key), draw_state=draw_state)
                if drag:
                    try:
                        flat_button(text, None, "chat-ghost:" + key,
                            width=row_width - Melty.px(46), height=row_height,
                            color=child_meta["tint"], text_color=_text_tint(tuple(child_meta["tint"])),
                            text_offset_x=0, layout=False, draw_list=drag.draw_list)
                    finally:
                        DragDrop.end_drag()
                    child_y += row_height + gap
                    continue
                if _visible(child_y, row_height, clip):
                    tint = child_meta["tint"]
                    selected = key == state.selected.get(state.account)
                    if selected:
                        _card(x + pad, child_y, row_width - pad * 2, row_height, tint, selected=True)
                    if _button(draw_state, "chat:" + key, "", x + pad, child_y,
                               row_width - pad * 2 - trash_width, tint, height=row_height, background=False):
                        state.selected[state.account] = key
                        changed = True
                    row_rect = (x + pad, child_y, x + row_width - pad, child_y + row_height)
                    hovered = draw_state.on_action("cursor_hover", view_id="chat-hover:" + key,
                                                   rect=row_rect) is not None
                    if hovered and _button(draw_state, "archive:" + key, trash_icon,
                                           x + row_width - pad - trash_width, child_y,
                                           trash_width, tint, not chats.get(key)["running"],
                                           height=row_height, background=False):
                        archive_key = key
                    if draw_state.on_action("left_mouse_double_clicked", view_id="rename:" + key,
                                            rect=rect, priority_delta=5) is not None:
                        state.rename = {"account": state.account, "key": key, "draft": text, "focus": True}
                        renaming = state.rename
                        editing = True
                        state.selected[state.account] = key
                        changed = True
                    changed |= _tint_chip(child_meta, draw_state, "chat:" + key + ":tint",
                                          x + Melty.px(23), child_y + max(0, (row_height - Melty.px(17)) / 2))
                    if not editing:
                        _title(text, x + Melty.px(46), child_y,
                               row_width - Melty.px(46) - trash_width, row_height, tint)
                    else:
                        imgui.set_cursor_screen_pos((x + Melty.px(46), child_y))
                        request_focus = editing and renaming["focus"]
                        edited, value, text_ds = draw_text(renaming["draft"] if editing else text,
                            wrap=False, name="chat-row:" + state.account + ":" + key,
                            request_focus=request_focus, select_all_on_focus=request_focus, single_line=True,
                            return_extras=True,
                            width=row_width - Melty.px(46) - trash_width, height=row_height, is_tree=False,
                            editable=editing, focusable=editing, syntax_highlight=False, autocomplete=False,
                            show_header=False, show_bg=False, shadow=False, show_widgets=False,
                            show_file_header=False, show_jump_bar=False, scope_collapse=False,
                            disable_scroll=True, freeze_resize=True, use_cache=True, imgui_padding=False,
                            tint=tint, text_tint=_text_tint(tuple(tint)), fim="")
                    if editing:
                        renaming["focus"] = False
                        if edited:
                            renaming["draft"] = value
                            changed = True
                        keys_pressed = {event[0] for event in Melty.frame_key_events}
                        if glfw.KEY_ESCAPE in keys_pressed:
                            state.rename = None
                            changed = True
                        elif (glfw.KEY_ENTER in keys_pressed or glfw.KEY_KP_ENTER in keys_pressed
                              or not request_focus and Melty.text_focused_ds is not text_ds):
                            if renaming["draft"].strip():
                                chats.get(key)["title"] = renaming["draft"].strip()
                            state.rename = None
                            changed = True
                child_y += row_height + gap
            y += card_height + gap
        changed |= _apply_chat_drop(chats, registered, DragDrop.on_drop(draw_state=draw_state))
    if archive_key is not None:
        del chats[archive_key]
        if state.selected.get(state.account) == archive_key:
            state.selected[state.account] = next(iter(chats), None)
        if getattr(state, "rename", None) is not None and state.rename["key"] == archive_key:
            state.rename = None
        changed = True
    return changed


def _message_leaves(value, path=(), depth=0):
    """Keep nested arguments/results inspectable without a renderer per dict."""
    if isinstance(value, Reference):
        yield path, value, depth
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _message_leaves(child, path + (str(key),), depth + 1)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _message_leaves(child, path + (str(index),), depth + 1)
    elif value is not None:
        yield path, value, depth


def _code_background(x, y, width, height, color, shadow=1):
    draw_list = imgui.get_window_draw_list()
    channel = Melty.get_channel()
    if Melty.channels_split:
        draw_list.channels_set_current(channel - 1)
    try:
        if shadow:
            add_shadow((x, y, width, height), offset=shadow, corner_radius=Melty.px(6))
        draw_list.add_rect_filled(x, y, x + width, y + height, _color(color), rounding=Melty.px(6))
    finally:
        if Melty.channels_split:
            draw_list.channels_set_current(channel)


def _terminal_layout(state, key, message, width):
    """Cache a passive VT screen by input identity; never start a shell."""
    import pyte
    import re
    command = message["content"]["command"]
    output = message["content"].get("output", "")
    cwd = message["details"].get("cwd", "")
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        char_width = imgui.calc_text_size("M").x
        line_height = imgui.get_text_line_height() * 1.2
    finally:
        if font is not None:
            imgui.pop_font()
    show_all = state.output_expanded.get(key, False)
    signature = (command, output, cwd, line_height, font, Melty.ui_scale, "preview", show_all)
    memo = state.text_layouts.get((key, "terminal"))
    if (memo is not None and all(a is b for a, b in zip(memo[0][:3], signature[:3]))
            and memo[0][3:] == signature[3:]):
        return memo[1], memo[2]
    parts = output.split("\n", 5)
    has_more = len(parts) > 5 and bool(parts[5].rstrip("\r\n"))
    shown_output = output if show_all or not has_more else "\n".join(parts[:5])
    prompt = ("\x1b[1;32m" + str(cwd) + "\x1b[0m" if cwd else "") + "\x1b[1;32m$\x1b[0m "
    text = prompt + str(command).rstrip("\r\n") + ("\n" + str(shown_output) if shown_output else "")
    # The captured terminal has a maximum content width, independent of the view.
    # Allow two cells per code point for wide glyphs; the sparse grid trims the rest.
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    columns = max(1, max(len(line.expandtabs(8)) for line in plain.split("\n")) * 2)
    screen = pyte.Screen(columns, text.count("\n") + 2)
    screen.set_mode(20)  # LF also returns to column zero, as captured shell output expects.
    pyte.Stream(screen).feed(text)
    last = max((index for index, row in screen.buffer.items() if row), default=0)
    grid = tuple(tuple(screen.buffer[row][col] for col in range(max(screen.buffer[row], default=-1) + 1))
                 for row in range(last + 1))
    value = (grid, char_width, line_height)
    height = len(grid) * line_height + Melty.px(8)
    state.text_layouts[(key, "terminal")] = (signature, value, height, has_more,
        max((len(row) for row in grid), default=0) * char_width + Melty.px(8))
    return value, height


@render_func(tint=(0.2, 0.8, 0.4), use_cache=True, show_bg=False, with_header=None,
             with_footer=None, shadow=False, imgui_padding=False, disable_scroll=True)
def draw_chat_terminal(input_value, draw_state=None):
    from src.lsd.gl_gui.view.playground.terminal_playground import _resolve
    grid, char_width, line_height = input_value
    x, y = draw_state.abs_left + Melty.px(4), draw_state.abs_top + Melty.px(4)
    draw_list = imgui.get_window_draw_list()
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        clip = Melty.get_clip_rect()
        first = max(0, int((clip[1] - y) // line_height)) if clip else 0
        last = min(len(grid), int((clip[3] - y) // line_height) + 1) if clip else len(grid)
        for index in range(first, last):
            row = grid[index]
            top = y + index * line_height
            column = 0
            visible_columns = min(len(row), max(0, int((draw_state.width - Melty.px(8)) / char_width) + 1))
            while column < visible_columns:
                cell = row[column]
                end = column + 1
                while end < visible_columns and row[end][1:] == cell[1:]:
                    end += 1
                foreground = _resolve(cell.fg, (0.85, 0.85, 0.85), cell.bold)
                background = _resolve(cell.bg, (0.018, 0.022, 0.025))
                if cell.reverse:
                    foreground, background = background, foreground
                left = x + column * char_width
                if cell.bg != "default" or cell.reverse:
                    if Melty.channels_split:
                        draw_list.channels_set_current(Melty.get_channel() - 1)
                    try:
                        draw_list.add_rect_filled(left, top, x + end * char_width, top + line_height, _color(background))
                    finally:
                        if Melty.channels_split:
                            draw_list.channels_set_current(Melty.get_channel())
                draw_list.add_text(left, top, _color(foreground), "".join(char.data for char in row[column:end]))
                column = end
        imgui.dummy(draw_state.width, len(grid) * line_height + Melty.px(8))
    finally:
        if font is not None:
            imgui.pop_font()
    return False, input_value


def _message_preview(message):
    """The first line a collapsed row would otherwise hide, markdown emphasis stripped."""
    if isinstance(message, CommandExecution):
        source = str(message["content"].get("command", ""))
    elif isinstance(message, ReasoningMessage):
        source = "\n".join(str(value) for _, value, _ in _message_leaves(message["content"])
                          if isinstance(value, str))
    else:
        return ""
    for line in source.splitlines():
        line = line.strip().strip("*_#`").strip()
        if line:
            return line
    return ""


def _message_label(message, expanded):
    bash_icon = f""
    write_icon = f""
    label = message.label
    if isinstance(message, ReasoningMessage):
        return "" if expanded else _message_preview(message)
    if isinstance(message, ToolCall):
        files = message.get("summary", {})
        if any(entry.get("access", "write") == "write" for entry in files.values()):
            label = write_icon
        elif isinstance(message["content"].get("command"), BashString):
            label = bash_icon
            preview = "" if expanded else _message_preview(message)
            if preview:
                label += "  " + preview
        else:
            name = message["details"].get("tool") or message["details"].get("name")
            if name:
                label += " · " + str(name)
    return label


def _message_failed(message):
    details = message["details"]
    return (message.get("status") in ("failed", "declined", "error")
            or details.get("exitCode") not in (None, 0)
            or bool(details.get("error")))


def _failure_badge(x, y, width, height):
    icon = f""
    cursor = imgui.get_cursor_screen_pos()
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel() - 1)
    try:
        draw_list.add_rect_filled(x, y, x + width, y + height, _color((0.30, 0.055, 0.075)), rounding=Melty.px(4))
    finally:
        if Melty.channels_split:
            draw_list.channels_set_current(Melty.get_channel())
    try:
        imgui.set_cursor_screen_pos((x, y))
        flat_button(icon + " Failed", None, "chat-failed", width=width, height=height,
                    color=(0.9, 0.16, 0.22), text_color=(1.0, 0.76, 0.77),
                    alpha=0, hovered=False, layout=False)
    finally:
        imgui.set_cursor_screen_pos(cursor)


def _hold_scroll_anchor(view, rows, height, restore):
    """Pin the top visible row to its screen position across relayouts.

    Each frame records (row key, pixels of that row above the viewport top);
    a frame whose heights moved (`restore`) re-derives the offset from it, so
    the reader's row stays put while rows above it re-wrap. Follow mode wins.
    """
    tops, y = {}, 0.0
    for row in rows:
        tops[row[0]] = y
        y += row[3]
    anchor = view.get("anchor")
    if restore and anchor is not None and not view.get("follow") and anchor[0] in tops:
        view["offset"] = max(0.0, min(tops[anchor[0]] + anchor[1], max(0.0, y - height)))
    offset = view.get("offset", 0.0)
    for row in rows:
        if tops[row[0]] + row[3] > offset:
            view["anchor"] = (row[0], offset - tops[row[0]])
            return
    view["anchor"] = None


def draw_messages(messages, draw_state, state, key, width, height,
                  conversation_tint=(0.4, 0.8, 0.7)):
    """Immediate-mode message layout; only visible text leaves use cached views.

    Message subclasses describe semantics, leaf types describe rendering.
    Resources stay inert references: no image loading, opening paths or I/O.
    """
    if not hasattr(state, "message_expanded"):
        state.message_expanded = {}
    if not hasattr(state, "output_expanded"):
        state.output_expanded = {}
    # User and assistant prose sit inset in their span.
    # [tint=(0.95, 0.6, 0.25)]
    user_inset = Melty.px(12)
    # [tint=(0.95, 0.6, 0.25)]
    user_pad = Melty.px(6)
    rows = []
    row_width = max(Melty.px(60), width - Melty.px(7))
    # Resize: wrapping every leaf per frame is O(transcript), and re-wrapped
    # heights above the viewport shove the scroll around. While the width is
    # moving only VISIBLE prose re-wraps (off-screen leaves keep their last
    # wrap), and the top visible row is pinned to its screen position through
    # the gesture; the frame the width settles re-wraps everything and pins again.
    view = state.viewports.setdefault(key, {"offset": 0.0, "follow": True})
    width_moving = view.get("width") not in (None, row_width)
    settling = view.get("unsettled", False) and not width_moving
    view["width"], view["unsettled"] = row_width, width_moving
    viewport_top = view.get("offset", 0.0)
    layout_y = 0
    # Chat layout: user cards sit off the left edge, assistant prose off the right,
    # by the same margin: 50 px in a narrow transcript, 100 px once it is wide.
    # [tint=(0.95, 0.6, 0.25)]
    chat_indent = min(Melty.px(100), max(Melty.px(50), row_width - Melty.px(600)))
    # Code blocks darken the window's PAINTED fill — `Melty.bg_color_stack`
    # top, what the wrapper's draw_bg actually returned (`bg_stack` holds the
    # tint, `draw_state.bg_color` the nested recipe; both are the wrong shade).
    # Bash a small step under the window, flat; python darker, shadowed.
    # [tint=(0.95, 0.6, 0.25)]
    bash_darken = 0.74
    # [tint=(0.95, 0.6, 0.25)]
    python_darken = 0.65
    bash_shadow = 0
    # A terminal block never grows wider than this; long lines clip at its edge.
    # [tint=(0.95, 0.6, 0.25)]
    terminal_max_width = Melty.px(500)
    window_color = tuple(Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
    header_height = max(Melty.px(23), imgui.get_text_line_height() + Melty.px(4))
    for message_id, message in messages.items():
        if not isinstance(message, Message):
            continue  # older cached sessions are replaced by the provider factory
        if isinstance(message, ReasoningMessage) and not any(
                isinstance(value, str) and value.strip() for _, value, _ in _message_leaves(message["content"])):
            continue
        row_key = key + ":" + message_id
        summary = message.get("summary")
        files = {filename: counts for filename, counts in summary.items()
                 if counts.get("access", "write") == "write"} if isinstance(summary, FileTags) else {}
        terminal = isinstance(message, CommandExecution)
        has_header = not isinstance(message, (UserMessage, AssistantMessage))
        collapsible = has_header and isinstance(message, (ToolCall, ReasoningMessage))
        # Every collapsible message starts collapsed; the arrow / file tags open it.
        expanded = not collapsible or state.message_expanded.get(row_key, False)
        leaves = []
        show_more = False
        content_width = 0
        if expanded and terminal:
            display, leaf_height = _terminal_layout(state, row_key, message, row_width)
            leaves.append((("terminal",), display, leaf_height, 0, ""))
            memo = state.text_layouts[(row_key, "terminal")]
            show_more, content_width = memo[3], memo[4]
        elif expanded:
            content = message["content"] or message["details"]
            for path, value, depth in _message_leaves(content):
                if isinstance(message, ReasoningMessage) and isinstance(value, str) and not value.strip():
                    continue
                indent = min(depth - 1, 4) * Melty.px(10) if isinstance(message, ToolCall) else 0
                indent = Melty.px(20) if isinstance(message, ReasoningMessage) else max(0, indent)
                prose = isinstance(message, (UserMessage, AssistantMessage))
                # Assistant prose starts at the row edge so it lines up with the tool rows'
                # arrow; user prose keeps its inset in the card.
                indent = user_inset if isinstance(message, UserMessage) else 0 if prose else indent
                if isinstance(value, str):
                    # Only plain prose wraps; code, tool output and everything else keeps its width.
                    wraps = prose and not isinstance(value, CodeString)
                    off_screen = layout_y > viewport_top + height or layout_y + Melty.px(2000) < viewport_top
                    display, leaf_height = _text_layout(state, (row_key, path), value,
                        wrap_width=row_width - chat_indent - (2 * user_inset if isinstance(message, UserMessage) else 0) if wraps else None,
                        keep=width_moving and off_screen)
                else:
                    display, leaf_height = value, header_height
                # Tool leaf labels and language captions are drawn directly.
                caption = (" / ".join(p for p in path if not p.isdecimal())
                           if isinstance(message, ToolCall) else
                           getattr(value, "language", "") if isinstance(value, CodeString) else "")
                if caption:
                    leaf_height += header_height
                leaves.append((path, display, leaf_height, indent, caption))
                measured = state.text_layouts[(row_key, path)][3] if isinstance(value, str) else imgui.calc_text_size(str(value)).x
                content_width = max(content_width, indent + measured + Melty.px(8),
                                    indent + _label_width(caption) + Melty.px(8))
        tags = []
        tag_x, tag_y = Melty.px(20), 0
        available_width = (min(row_width, terminal_max_width) if terminal else row_width) - (Melty.px(100) if _message_failed(message) else 0)
        if files:
            section_label = f""
            label_width = _label_width(section_label) + Melty.px(12)
            if tag_x > Melty.px(20) and tag_x + label_width + Melty.px(70) > available_width:
                tag_x = Melty.px(20)
                tag_y += header_height + Melty.px(2)
            tags.append((None, section_label, tag_x, tag_y, label_width))
            tag_x += label_width
            for filename, counts in files.items():
                count_label = (f"  +{counts['added']} -{counts['removed']}"
                               if counts.get("added") is not None else "")
                tag_width = min(max(Melty.px(30), available_width - Melty.px(20)),
                                imgui.calc_text_size(Path(filename).name + count_label).x + Melty.px(16))
                if tag_x + tag_width > available_width:
                    tag_x = Melty.px(20)
                    tag_y += header_height + Melty.px(2)
                tags.append((filename, counts, tag_x, tag_y, tag_width))
                tag_x += tag_width + Melty.px(3)
        summary_height = (tag_y + header_height if tags else
                          0 if isinstance(message, ReasoningMessage) and expanded else
                          header_height if has_header else
                          user_pad if isinstance(message, UserMessage) else 0)
        gap = 0 if isinstance(message, UserMessage) else Melty.px(4)
        full_height = (summary_height + sum(leaf[2] for leaf in leaves) + gap
                       + (user_pad if isinstance(message, UserMessage) else 0)
                       + (header_height if show_more else 0))
        fitted_width = row_width
        if isinstance(message, ToolCall):
            header_width = (max(tag[2] + tag[4] for tag in tags) if tags else
                            Melty.px(20) + _label_width(_message_label(message, expanded)))
            header_width += Melty.px(104) if _message_failed(message) else Melty.px(8)
            fitted_width = min(row_width, max(content_width, header_width, Melty.px(110) if show_more else 0))
            if terminal:
                fitted_width = min(fitted_width, terminal_max_width)  # collapsed labels clip at this cap too
        rows.append((row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header))
        layout_y += full_height
    total = sum(row[3] for row in rows)
    _hold_scroll_anchor(view, rows, height, width_moving or settling)
    changed = False
    with _viewport(draw_state, state, key, width, height, total, follow=True) as (x, y, clip):
        for row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header in rows:
            tint = conversation_tint
            # User messages carry the chat tint; assistant prose stays unboxed.
            underlying = window_color
            prose = isinstance(message, (UserMessage, AssistantMessage))
            side = chat_indent if isinstance(message, UserMessage) else 0
            row_span = row_width - chat_indent if prose else row_width
            if isinstance(message, UserMessage) and _visible(y, full_height, clip):
                underlying = _card(x + side, y, row_span, full_height, tint) or underlying
            if isinstance(message, ToolCall):
                underlying = tuple(c * (bash_darken if isinstance(message, CommandExecution) else python_darken)
                                   for c in underlying[:3])
                # The header row carries only its icon chip; the block background
                # starts under it, covering the expanded content.
                if expanded and full_height > summary_height + Melty.px(4) and _visible(y, full_height, clip):
                    _code_background(x, y + summary_height, fitted_width, full_height - summary_height - Melty.px(4),
                                     underlying, shadow=bash_shadow if isinstance(message, CommandExecution) else 1)
            if tags:
                from src.lsd.gl_gui.model.app_model import FileMeta
                from src.lsd.gl_gui.view.playground.open_files import draw_changed_file_header
                root = getattr(getattr(Melty, "vis", None), "root", None)
                metadata = getattr(getattr(root, "file_meta_collection", None), "file_meta", {})
                for filename, counts, tag_x, tag_y, tag_width in tags:
                    file_y = y + tag_y
                    if filename is None:
                        if _visible(file_y, header_height, clip):
                            _icon_chip(counts, x + tag_x, file_y, header_height, underlying)
                            _title(counts, x + tag_x, file_y, tag_width, header_height, underlying)
                        continue
                    if _visible(file_y, header_height, clip):
                        file_path = str(Path(message["details"].get("cwd") or "") / filename)
                        file_tint = FileMeta.painted_tint(metadata.get(file_path)) or underlying
                        imgui.set_cursor_screen_pos((x + tag_x, file_y))
                        if draw_changed_file_header(filename, file_tint, draw_state,
                                view_id=row_key + ":file:" + filename, width=tag_width, height=header_height,
                                added=counts["added"], removed=counts["removed"], active=True,
                                prefix=""):
                            state.message_expanded[row_key] = not expanded
                            changed = True
            if (summary_height and has_header or collapsible) and _visible(y, header_height, clip):
                header_x = x
                if collapsible:
                    imgui.set_cursor_screen_pos((x, y))
                    imgui.push_id(row_key + ":expand")
                    try:
                        if draw_header_arrow(expanded):
                            state.message_expanded[row_key] = not expanded
                            changed = True
                    finally:
                        imgui.pop_id()
                    header_x += Melty.px(20)
                failed = _message_failed(message)
                if not tags and not (isinstance(message, ReasoningMessage) and expanded):
                    label = _message_label(message, expanded)
                    if isinstance(message, ToolCall) and label and 0xF000 <= ord(label[0]) <= 0xF8FF:
                        _icon_chip(label[0], header_x, y, header_height, underlying)
                    _title(label, header_x, y, fitted_width - (header_x - x) - (Melty.px(100) if failed else 0),
                           header_height, underlying if isinstance(message, ToolCall) else tint)
                if failed:
                    _failure_badge(x + fitted_width - Melty.px(96), y, Melty.px(92), header_height)
            leaf_y = y + summary_height
            for path, value, leaf_height, indent, caption in leaves:
                if _visible(leaf_y, leaf_height, clip):
                    text_y = leaf_y
                    left = x + side + indent
                    leaf_width = row_span - indent - (user_inset if isinstance(message, UserMessage) else 0)
                    if not isinstance(message, ToolCall) and isinstance(value, (PythonString, BashString, ToolOutput)):
                        is_bash = isinstance(value, (BashString, ToolOutput))
                        _code_background(left, leaf_y, leaf_width, leaf_height,
                                         tuple(c * (bash_darken if is_bash else python_darken) for c in underlying[:3]),
                                         shadow=bash_shadow if is_bash else 6)
                    if isinstance(value, BashString):
                        caption = "$ bash"
                    if caption:
                        _title(caption, left, text_y, leaf_width, header_height, tint)
                        text_y += header_height
                    if path == ("terminal",):
                        imgui.set_cursor_screen_pos((x, text_y))
                        draw_chat_terminal(value, name=row_key + ":terminal", width=fitted_width, height=leaf_height)
                    elif isinstance(value, Reference):
                        # Never paint data URLs / encoded image data as text.
                        name = value.get("name") or value.get("path") or ""
                        _title(value.label + (" · " + str(name) if name else "") + " (preview pending)",
                               left, text_y, leaf_width, header_height, tint)
                    elif isinstance(value, str) and prose and not isinstance(value, CodeString):
                        _draw_prose(value, left, text_y, leaf_width, leaf_height, tint, clip)
                    elif isinstance(value, str):
                        imgui.set_cursor_screen_pos((left, text_y))
                        draw_text(value, wrap=False, name=row_key + ":" + repr(path),
                            width=leaf_width,
                            height=leaf_height - (header_height if caption else 0),
                            editable=False, syntax_highlight=isinstance(value, (PythonString, BashString)),
                            syntax_language="bash" if isinstance(value, BashString) else "python",
                            autocomplete=False, show_widgets=False, show_header=False, with_footer=None,
                            show_file_header=False, show_jump_bar=False, scope_collapse=False,
                            disable_scroll=True, freeze_resize=True, imgui_padding=False,
                            use_cache=True, tint=tint, text_tint=_text_tint(tuple(tint)),
                            is_tree=False, show_bg=False, shadow=False, bg_offset=-1, fim="")
                    else:
                        _title(str(value), left, text_y, leaf_width, header_height, tint)
                leaf_y += leaf_height
            if show_more and _visible(leaf_y, header_height, clip):
                show_all = state.output_expanded.get(row_key, False)
                if _button(draw_state, row_key + ":output-more", "Show less" if show_all else "Show more",
                           x + Melty.px(4), leaf_y, Melty.px(102), underlying,
                           height=header_height, background=False):
                    state.output_expanded[row_key] = not show_all
                    changed = True
            y += full_height
    return changed


def _cleanup_chat(draw_state):
    for account in internet_accounts.accounts.values():
        proxy = account.pop("_chat_proxy", None)
        if proxy is not None:
            proxy.close()


@window(tint=(0.07, 0.09, 0.10), display_name="Chat", icon=f"", initial={"width": 1100, "height": 760})
@render_func(auto_resize=False, min_width=700, min_height=500,
             on_cleanup=_cleanup_chat, use_cache=True, disable_scroll=True, imgui_padding=False, indent_size=0)
def draw_chat_interface(input_value=None, draw_state=None, state: ChatInterfaceState = None,
                        column_edges=None, **kwargs):
    # Ephemeral layout state also adopts already-open windows on hotswap.
    if not hasattr(state, "viewports"):
        state.viewports = {}
        state.text_layouts = {}
    accounts = internet_accounts.accounts
    if not accounts.loaded:
        accounts.load()
    accounts.ensure_kinds()
    providers = {kind.chat_label: key for key, kind in internet_accounts.KINDS.items() if kind.chat_label}
    if not providers:
        return False, input_value
    if state.provider not in providers.values():
        state.provider = next((key for key in providers.values()
                               if internet_accounts.KINDS[key].chat_available),
                              next(iter(providers.values())))
    changed, provider = draw_dropdown(internet_accounts.KINDS[state.provider].chat_label,
        collection=providers, name="Chat provider", width=210, show_header=False, is_tree=False)
    if changed:
        state.provider = provider
        state.account = ""
    entries = accounts.of_kind(state.provider)
    if state.account not in [entry["id"] for entry in entries]:
        state.account = entries[0]["id"]
    imgui.same_line()
    edited, account_id = draw_dropdown(accounts[state.account]["label"],
        collection={entry["label"] + " · " + entry["id"]: entry["id"] for entry in entries},
        name="Chat account", width=260, show_header=False, is_tree=False)
    if edited:
        state.account = account_id
    changed |= edited
    account = accounts[state.account]
    kind = internet_accounts.KINDS[state.provider]

    def wake():
        # Like Fast Dock's external-change edge: the worker has queued new data.
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        if Melty.cache is not None:
            Melty.cache.invalidate_up_by_obj(state, force=True)
            if getattr(draw_state, "_tile_id", None) is not None:
                Melty.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=8)
        request_render()

    proxy = kind.chats(account, wake=wake)
    # Drain existing sessions even while a placeholder provider is selected.
    for other in accounts.values():
        other_proxy = other.get("_chat_proxy")
        if other_proxy is not None and other_proxy is not proxy and not other_proxy.closed:
            changed |= other_proxy.drain()
            other_proxy.reconcile()
    if proxy is None:
        return changed, input_value  # registered placeholders deliberately stay blank
    proxy.wake = wake
    changed |= proxy.drain()
    proxy.reconcile()  # sync edits made through this view before resetting metadata
    x, y = imgui.get_cursor_screen_pos()
    tint = kind.tint
    notice = proxy.error or ("Loading conversations…" if proxy.loading else "")
    if notice:
        for line in notice.split("\n"):
            imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.65, 0.45)), line)
            imgui.dummy(1, imgui.get_text_line_height())
        if proxy.error:
            x, y = imgui.get_cursor_screen_pos()
            if _button(draw_state, "reconnect-chat", "Reconnect", x, y, Melty.px(110), tint):
                kind.close_chat(account)
                return True, input_value
            imgui.dummy(1, Melty.px(30))
    selected = state.selected.get(state.account)
    if selected not in proxy:
        selected = next(iter(proxy), None)
        state.selected[state.account] = selected
    body_top = imgui.get_cursor_screen_pos()[1]
    body_bottom = draw_state.abs_top + (draw_state.height or Melty.px(760)) - Melty.px(16)
    body_height = max(Melty.px(180), body_bottom - body_top)
    columns = ColumnLayout(draw_state, 2, column_edges=column_edges,
                           column_widths=[260, None], column_mins=[180, 400], padding=Melty.px(5), padding_y=0, border_color=None)
    with columns.cell(0, height=body_height) as width:
        x, y = imgui.get_cursor_screen_pos()
        footer_height = Melty.px(30)
        changed |= draw_chat_sidebar(proxy, draw_state, state, width, body_height - footer_height)
        y += body_height - Melty.px(25)
        imgui.set_cursor_screen_pos((x, y))
        if _button(draw_state, "new-chat", "+ New conversation", x, y, min(width, Melty.px(190)), tint, not proxy.loading and not proxy.error):
            key = str(uuid.uuid4())
            current = proxy.get(state.selected.get(state.account))
            project = current["project"] if current else state.projects.get(state.account) or str(Path(__file__).resolve().parents[5])
            proxy[key] = {"title": "New conversation", "project": project}
            state.selected[state.account] = key
            changed = True
        imgui.dummy(width, Melty.px(25))
    # Sidebar selection takes effect in the same frame.
    selected = state.selected.get(state.account)
    with columns.cell(1, height=body_height) as width:
        if selected in proxy:
            chat = proxy[selected]
            meta = chat.metadata
            x, y = imgui.get_cursor_screen_pos()
            changed |= _tint_chip(meta, draw_state, "title-tint:" + selected, x, y + Melty.px(5))
            _title(chat["title"], x + Melty.px(26), y,
                   width - Melty.px(26), Melty.px(28), meta["tint"])
            imgui.set_cursor_screen_pos((x, y))
            imgui.dummy(width, Melty.px(28))
            x, y = imgui.get_cursor_screen_pos()
            transcript_key = "history:" + state.account + ":" + selected
            for request_id, request in list(chat["requests"].items()):
                if request["kind"] == "approval":
                    for line in request["text"].split("\n"):
                        imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.75, 0.4)), line)
                        imgui.dummy(1, imgui.get_text_line_height())
                    x, y = imgui.get_cursor_screen_pos()
                    for index, decision in enumerate(("accept", "decline")):
                        if _button(draw_state, f"answer:{request_id}:{decision}", decision.title(),
                                   x + index * Melty.px(95), y, Melty.px(90), tint):
                            request["answer"] = {"decision": decision}
                            changed = True
                    imgui.dummy(1, Melty.px(30))
                else:
                    answers = state.answers.setdefault(str(request_id), {})
                    for question in request["data"].get("questions", []):
                        edited, answer = draw_text(answers.get(question["id"], ""), name=question["question"],
                            height=50, wrap=False, syntax_highlight=False, line_numbers=None, fim="", is_tree=False)
                        if edited:
                            answers[question["id"]] = answer
                            changed = True
                    x, y = imgui.get_cursor_screen_pos()
                    if _button(draw_state, f"reply:{request_id}", "Answer", x, y, Melty.px(85), tint):
                        request["answer"] = {"answers": {key: {"answers": [value]} for key, value in answers.items()}}
                        changed = True
                    imgui.dummy(1, Melty.px(30))
            # Reserve the composer before laying out the history. Scrolling
            # changes only message coordinates inside this history region.
            history_height = max(Melty.px(40), body_bottom - imgui.get_cursor_screen_pos()[1]
                                 - Melty.px(145 if chat.error or chat.loading else 120))
            changed |= draw_messages(chat["messages"], draw_state, state, transcript_key,
                                 width, history_height, conversation_tint=meta["tint"])
            if chat.error or chat.loading:
                message = chat.error or "Loading conversation…"
                imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.65, 0.4)), message)
                imgui.dummy(1, Melty.px(25))
            draft_key = state.account + ":" + selected
            edited, draft = draw_text(state.drafts.get(draft_key, ""), name="Message:" + selected,
                height=85, width=width, wrap=False, syntax_highlight=False, line_numbers=None, fim="", show_header=False,
                use_cache=True, is_tree=False, shadow=False, bg_offset=-1)
            if edited:
                state.drafts[draft_key] = draft
                changed = True
            x, y = imgui.get_cursor_screen_pos()
            if _button(draw_state, "send-stop", "Stop" if chat["running"] else "Send", x, y, Melty.px(85),
                       meta["tint"], chat["running"] or (bool(draft.strip()) and chat.loaded and not chat.error)):
                if chat["running"]:
                    chat["running"] = False
                else:
                    chat["messages"][str(uuid.uuid4())] = user_message(draft)
                    state.drafts[draft_key] = ""
                changed = True
            imgui.dummy(1, Melty.px(30))
    columns.finish()
    proxy.reconcile()
    if changed:
        state.revision += 1
    return changed, input_value
    if changed:
        state.revision += 1
    return changed, input_value