"""Chat view functions and supporting definitions."""
from meltygui.view.chat_decoration_view import _text_tint
from meltygui.view.chat_decoration_view import _tint_style
from bisect import bisect_right
from functools import lru_cache
from meltygui.chat.messages import BashString, CommandExecution, ReasoningMessage, Reference, ToolCall
from meltygui.core.services.chat_core import _cleanup_chat
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.state.chat_state import ChatInterfaceState
from meltygui.core.runtime.toggles import Tint, Toggles
from pathlib import Path
import colorsys
import meltygui_imgui as imgui
import time
import uuid


@render_func(tint=(0.2, 0.8, 0.4), use_cache=True, show_bg=False, with_header=None,
             with_footer=None, shadow=False, imgui_padding=False, disable_scroll=True)
def draw_chat_terminal(input_value, draw_state=None):
    from meltygui.view.chat_decoration_view import _color
    from meltygui.core.styling.fonts import Font

    from meltygui.view.terminal_view import _resolve
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
                    draw_list.add_rect_filled(left, top, x + end * char_width, top + line_height, _color(background))
                draw_list.add_text(left, top, _color(foreground), "".join(char.data for char in row[column:end]))
                column = end
        imgui.dummy(draw_state.width, len(grid) * line_height + Melty.px(8))
    finally:
        if font is not None:
            imgui.pop_font()
    return False, input_value


def draw_chat_queue(proxy, selected, chat, draw_state, state, key, width, tint):
    from meltygui.view.chat_decoration_view import _button
    from meltygui.chat.chat_interface import _title
    from meltygui.chat.chat_interface import _viewport
    from meltygui.chat.messages import input_text

    queued = chat.get("queued_messages", {})
    if not queued:
        return False
    changed = False
    x, y = imgui.get_cursor_screen_pos()
    paused = chat.get("queue_paused", False)
    label = f"{len(queued)} queued" + (" · paused" if paused else " · sends after this turn")
    _title(label, x, y, max(1, width - Melty.px(90)), Melty.px(28), tint, brightness=0.7, ellipsis=True)
    if paused and _button(draw_state, key + ":resume", "Resume", x + width - Melty.px(85), y,
                          Melty.px(85), tint, height=Melty.px(28), enabled=not chat.get("locked", False), ui_scale=Melty.ui_scale):
        proxy.resume_queue(selected)
        changed = True
    imgui.set_cursor_screen_pos((x, y))
    imgui.dummy(width, Melty.px(28))
    with _viewport(draw_state, state, key + ":queue", width, Melty.px(28) * min(3, len(queued)),
                   Melty.px(28) * len(queued)) as (x, y, clip):
        for index, (identifier, message) in enumerate(list(queued.items())):
            top = y + index * Melty.px(28)
            if not _visible(top, Melty.px(28), clip):
                continue
            _title(input_text(message).replace("\n", " "), x, top, max(1, width - Melty.px(175)),
                   Melty.px(28), tint, brightness=0.7, ellipsis=True)
            if _button(draw_state, key + ":send-now:" + identifier, "Send now", x + width - Melty.px(170), top,
                       Melty.px(85), tint, not chat.get("locked", False), height=Melty.px(28), background=False, ui_scale=Melty.ui_scale):
                changed |= proxy.send_queued(selected, identifier)
            if _button(draw_state, key + ":remove:" + identifier, "Remove", x + width - Melty.px(80), top,
                       Melty.px(72), tint, height=Melty.px(28), background=False, ui_scale=Melty.ui_scale):
                proxy.cancel_queued(selected, identifier)
                changed = True
    return changed


def draw_messages(messages, draw_state, state, key, width, height,
                  conversation_tint=(0.4, 0.8, 0.7), revision=None, file_metadata=None):
    """Immediate-mode message layout; only visible text leaves use cached views.

    Message subclasses describe semantics, leaf types describe rendering.
    Resources stay inert references: no image loading, opening paths or I/O.
    """
    from meltygui.view.chat_decoration_view import _button
    from meltygui.view.chat_decoration_view import _card
    from meltygui.view.chat_decoration_view import _caret
    from meltygui.chat.chat_interface import _code_background
    from meltygui.chat.chat_interface import _draw_image
    from meltygui.chat.chat_interface import _draw_prose
    from meltygui.chat.chat_interface import _failure_badge
    from meltygui.chat.chat_interface import _icon_chip
    from meltygui.chat.chat_interface import _label_width
    from meltygui.chat.chat_interface import _prose_metrics
    from meltygui.chat.chat_interface import _terminal_layout
    from meltygui.chat.chat_interface import _text_layout
    from meltygui.chat.chat_interface import _title
    from meltygui.chat.chat_interface import _viewport
    from meltygui.chat.chat_interface import image_box
    from meltygui.chat.chat_interface import image_cache
    from meltygui.view.chat_decoration_view import prose_offset
    from meltygui.chat.chat_interface import transcript_entries
    from meltygui.chat.messages import AssistantMessage
    from meltygui.chat.messages import BashString
    from meltygui.chat.messages import CodeString
    from meltygui.chat.messages import CommandExecution
    from meltygui.chat.messages import FileTags
    from meltygui.chat.messages import ImageReference
    from meltygui.chat.messages import Message
    from meltygui.chat.messages import PythonString
    from meltygui.chat.messages import ReasoningMessage
    from meltygui.chat.messages import Reference
    from meltygui.chat.messages import ToolCall
    from meltygui.chat.messages import ToolOutput
    from meltygui.chat.messages import UserMessage
    from meltygui.view.text_view import draw_text
    import meltygui.core.windowing.window_api as glfw

    if not hasattr(state, "message_expanded"):
        state.message_expanded = {}
    if not hasattr(state, "output_expanded"):
        state.output_expanded = {}
    if not hasattr(state, "image_sizes"):
        state.image_sizes = {}
    # User and assistant prose sit inset in their span.
    # [tint=(0.95, 0.6, 0.25)]
    user_inset = Melty.px(12)
    # [tint=(0.95, 0.6, 0.25)]
    user_pad = Melty.px(6)
    rows = []
    viewport_width = max(Melty.px(60), width - Melty.px(7))
    row_width = min(viewport_width, Melty.px(Toggles.Chat.transcript_max_width))
    # Resize: wrapping every leaf per frame is O(transcript), and re-wrapped
    # heights above the viewport shove the scroll around. While the width is
    # moving only VISIBLE prose re-wraps (off-screen leaves keep their last
    # wrap), and the top visible row is pinned to its screen position through
    # the gesture; the frame the width settles re-wraps everything and pins again.
    view = state.viewports.setdefault(key, {"offset": 0.0, "follow": True})
    prose_index, prose_texts = {}, []      # every prose leaf in order: the selection's coordinates
    width_moving = view.get("width") not in (None, row_width)
    settling = view.get("unsettled", False) and not width_moving
    view["width"], view["unsettled"] = row_width, width_moving
    row_memos = view.setdefault("rows", {})   # per-row layouts (text_layouts holds visible leaf wraps)
    viewport_top = view.get("offset", 0.0)
    layout_y = 0
    # User bubbles align to the right of the centered transcript, with a
    # narrower cap and a small left inset even when the window is narrow.
    # [tint=(0.95, 0.6, 0.25)]
    chat_indent = max(Melty.px(50), row_width - Melty.px(Toggles.Chat.user_message_max_width))
    # Code blocks darken the window's PAINTED fill: `Melty.bg_color_stack`
    # top, what the wrapper's draw_bg actually returned (`bg_stack` holds the
    # recipe, `draw_state.bg_color` the nested recipe; both hold the wrong shade).
    # The factors and shadow offsets live in Toggles.Chat.
    bash_darken = Toggles.Chat.bash_darken
    python_darken = Toggles.Chat.python_darken
    bash_shadow = Toggles.Chat.bash_shadow_offset
    # Rows: [icon chip][content]. Every non-user row's content — command
    # previews, file tags and their wrapped rows, terminals, thinking text,
    # assistant prose — sits in ONE column at icon_column; the chips hang in
    # the gutter to its left. Hovering an icon row swaps its glyph for the
    # expand caret (no caret column of its own); an expanded row's icon and
    # Failed badge share its first content line. Previews are dimmed to recede.
    # [tint=(0.95, 0.6, 0.25)]
    icon_column = Melty.px(26)
    icon_brightness = Toggles.Chat.icon_brightness
    command_text_brightness = Toggles.Chat.command_text_brightness
    # A terminal block never grows wider than this; long lines clip at its edge.
    # [tint=(0.95, 0.6, 0.25)]
    terminal_max_width = Melty.px(Toggles.Chat.terminal_max_width)
    window_color = tuple(Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
    header_height = max(Melty.px(23), imgui.get_text_line_height() + Melty.px(4))
    # The layout pass - every message, visible or not - is the transcript's
    # cost; it only changes with the content (the proxy's revision, the
    # message count), the width, or an expand / collapse. Otherwise the
    # previous frame's rows serve (the draw pass below is per visible row).
    pictures = image_cache()
    pictures.watch(draw_state)
    signature = (row_width, chat_indent, terminal_max_width, revision, len(messages), id(messages), pictures.generation,
                 getattr(state, "concise", False), frozenset(getattr(state, "action_groups", {}).items()),
                 hash(frozenset(k for k, v in state.message_expanded.items() if v)),
                 hash(frozenset(k for k, v in state.output_expanded.items() if v)))
    cached = view.get("layout")
    if cached is not None and cached[0] == signature and not width_moving and not settling and not view.get("relayout"):
        rows, prose_index, prose_texts = cached[1], cached[2], cached[3]
    else:
        previous_user = False
        for message_id, message in transcript_entries(messages, state, key):
            if not isinstance(message, Message):
                continue  # older cached sessions are replaced by the provider factory
            if isinstance(message, ReasoningMessage) and not any(
                    isinstance(value, str) and value.strip() for _, value, _ in _message_leaves(message["content"])):
                continue
            row_key = key + ":" + message_id
            user = isinstance(message, UserMessage)
            leading_space = Melty.px(20) if rows and (user or previous_user) else 0
            previous_user = user
            if message.get("kind") == "actionGroup":
                full_height = header_height + Melty.px(4) + leading_space
                rows.append((row_key, message, [], full_height, False, False, [], header_height,
                             row_width, False, True, "", leading_space))
                layout_y += full_height
                continue
            terminal = isinstance(message, CommandExecution)
            has_header = not isinstance(message, (UserMessage, AssistantMessage))
            collapsible = has_header and isinstance(message, (ToolCall, ReasoningMessage))
            # Every collapsible row starts collapsed; the caret / file tags open it.
            expanded = not collapsible or state.message_expanded.get(row_key, False)
            # A collapsed row of a finished tool call lays out the same every
            # frame: keep its row between frames (most of an old transcript).
            row_signature = None
            if collapsible and not expanded and message.get("status") in ("completed", "failed"):
                row_signature = (id(message), leading_space, message.get("status"), row_width, chat_indent, header_height,
                                 terminal_max_width, len(message["content"]), id(message.get("summary")))
            elif (not has_header and message.get("status") != "running" and not width_moving
                  and not any(isinstance(value, ImageReference) for value in message["content"].values())):
                # Finished assistant: its text is the same object until set_text replaces it.
                row_signature = (id(message), leading_space, id(getattr(message, "source_text", None)), message.get("status"),
                                 row_width, chat_indent, header_height, terminal_max_width, len(message["content"]))
            if row_signature is not None:
                memo = row_memos.get(row_key)
                if memo is not None and memo[0] == row_signature:
                    row = memo[1]
                    if not has_header:
                        for path, display, *_ in row[2]:      # all prose leaves keep their selection slots
                            if isinstance(display, str) and not isinstance(display, CodeString):
                                prose_index[(row_key, path)] = len(prose_texts)
                                prose_texts.append(str(display))
                    rows.append(row)
                    layout_y += row[3]
                    continue
            summary = message.get("summary")
            files = {filename: counts for filename, counts in summary.items()
                     if counts.get("access", "write") == "write"} if isinstance(summary, FileTags) else {}
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
                    indent = 0 if isinstance(message, ReasoningMessage) else max(0, indent)  # its icon column indents it
                    prose = isinstance(message, (UserMessage, AssistantMessage))
                    # Assistant prose starts at the row edge so it lines up with the tool rows'
                    # arrow; user prose keeps its inset inside the card.
                    indent = user_inset if isinstance(message, UserMessage) else 0 if prose else indent
                    image_entry = image_cache().entry(value) if isinstance(value, ImageReference) else None
                    if isinstance(value, ImageReference):
                        # A picture: its bounding box plus the caption line under it
                        # (a payload is not measured or treated as text).
                        # The same span the draw pass hands _draw_image (or leaf_width).
                        span = ((row_width - chat_indent if isinstance(message, UserMessage) else row_width) - indent
                                - (0 if isinstance(message, UserMessage) else icon_column)
                                - (user_inset if isinstance(message, UserMessage) else 0))
                        if isinstance(message, AssistantMessage):
                            span = min(span, terminal_max_width)
                        image_width, image_height = image_box(
                            image_entry, max(Melty.px(40), span), state.image_sizes.get(row_key + ":image:" + repr(path)))
                        display, leaf_height = value, image_height + header_height
                    elif isinstance(value, str):
                        # Only plain prose wraps; code, tool output and everything else keeps its lines.
                        wraps = prose and not isinstance(value, CodeString)
                        off_screen = layout_y > viewport_top + height or layout_y + Melty.px(2000) < viewport_top
                        display, leaf_height = _text_layout(state, (row_key, path), value,
                            wrap_width=(row_width - chat_indent - 2 * user_inset if isinstance(message, UserMessage)
                                        else min(row_width - icon_column, terminal_max_width)) if wraps else None,
                            keep=width_moving and off_screen)
                        if wraps:
                            prose_index[(row_key, path)] = len(prose_texts)
                            prose_texts.append(str(display))
                    else:
                        display, leaf_height = value, header_height
                    # Tool field labels and language captions are drawn directly.
                    caption = (" / ".join(p for p in path if not p.isdecimal())
                               if isinstance(message, ToolCall) else
                               getattr(value, "language", "") if isinstance(value, CodeString) else "")
                    if caption:
                        leaf_height += header_height
                    leaves.append((path, display, leaf_height, indent, caption))
                    measured = (state.text_layouts[(row_key, path)][3] if isinstance(value, str) else
                                # A picture sits past the icon column: the card must grow around it
                                image_width + (0 if isinstance(message, UserMessage) else icon_column)
                                if isinstance(value, ImageReference) else imgui.calc_text_size(str(value)).x)
                    content_width = max(content_width, indent + measured + Melty.px(8),
                                        indent + _label_width(caption) + Melty.px(8))
            tags = []
            icon = _message_icon(message)
            content_x = 0 if isinstance(message, UserMessage) else icon_column
            tags_x = content_x
            tag_x, tag_y = tags_x, 0
            available_width = (min(row_width, terminal_max_width) if terminal else row_width) - (Melty.px(100) if _message_failed(message) else 0)
            if files:
                for filename, counts in files.items():
                    count_label = (f"  +{counts['added']} -{counts['removed']}"
                                   if counts.get("added") is not None else "")
                    tag_width = min(max(Melty.px(30), available_width - Melty.px(20)),
                                    imgui.calc_text_size(Path(filename).name + count_label).x + Melty.px(16))
                    if tag_x > tags_x and tag_x + tag_width > available_width:
                        tag_x = tags_x  # wrapped rows line up after the icon and caret
                        tag_y += header_height + Melty.px(2)
                    tags.append((filename, counts, tag_x, tag_y, tag_width))
                    tag_x += tag_width + Melty.px(3)
            summary_height = (tag_y + header_height if tags else
                              0 if isinstance(message, ReasoningMessage) and expanded else
                              0 if icon and expanded and leaves else
                              header_height if has_header else
                              user_pad if isinstance(message, UserMessage) else 0)
            gap = 0 if isinstance(message, UserMessage) else Melty.px(4)
            full_height = (summary_height + sum(leaf[2] for leaf in leaves) + gap
                           + (user_pad if isinstance(message, UserMessage) else 0)
                           + (header_height if show_more else 0))
            fitted_width = row_width
            if isinstance(message, UserMessage):
                # Keep the prose renderer's 4px inset plus trailing glyph margin
                # inside the leaf clip, as well as the bubble's outer padding.
                fitted_width = min(row_width - chat_indent,
                                   max(Melty.px(40), content_width + user_inset))
            if isinstance(message, ToolCall):
                header_width = (max(tag[2] + tag[4] for tag in tags) if tags else
                                tags_x + _label_width(_message_label(message, expanded)))
                header_width += Melty.px(104) if _message_failed(message) else Melty.px(8)
                fitted_width = min(row_width, max(content_width, header_width, Melty.px(110) if show_more else 0))
                if terminal:
                    fitted_width = min(fitted_width, terminal_max_width)  # collapsed labels clip at the cap too
            full_height += leading_space
            row = (row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header, icon, leading_space)
            if row_signature is not None and not (width_moving or settling):
                # (a row wrapped mid-resize keeps stale off-screen wraps: never memoised)
                row_memos[row_key] = (row_signature, row)
            rows.append(row)
            layout_y += full_height
        view["layout"] = (signature, rows, prose_index, prose_texts)
        view["row_geometry"] = _row_geometry(rows)
    geometry = view["row_geometry"]
    ends = geometry[1]
    total = ends[-1] if ends else 0.0
    # A row the reader just expanded / collapsed / showed more of re-laid out
    # this frame: hold their row, and drop follow so a tall block opening near
    # the bottom doesn't fling the viewport off its end.
    relayout = view.pop("relayout", False)
    if relayout:
        view["follow"] = False
    _hold_scroll_anchor(view, rows, height, width_moving or settling or relayout, geometry)
    changed = False
    selecting = False        # a prose leaf took this frame's selection
    with _viewport(draw_state, state, key, width, height, total, follow=True) as (x, y, clip):
        x += max(0, (viewport_width - row_width) / 2)
        # The viewport returns the scrolled origin. Skip entire off-screen rows
        # before touching their leaves, tints, file metadata or event handlers.
        first = bisect_right(ends, clip[1] - y) if clip is not None else 0
        y += ends[first - 1] if first else 0.0
        for row_index in range(first, len(rows)):
            if clip is not None and y >= clip[3]:
                break
            row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header, icon, leading_space = rows[row_index]
            y += leading_space
            full_height -= leading_space
            if message.get("kind") == "actionGroup":
                if not hasattr(state, "action_groups"):
                    state.action_groups = {}
                expanded = state.action_groups.get(row_key, False)
                label = ("▾ " if expanded else "▸ ") + message["details"]["label"]
                if _button(draw_state, row_key, "", x, y, row_width, conversation_tint,
                           height=header_height, background=False, ui_scale=Melty.ui_scale):
                    state.action_groups[row_key] = not expanded
                    view["relayout"] = True
                    changed = True
                _title(label, x, y, row_width, header_height, conversation_tint, brightness=0.65, ellipsis=True)
                y += full_height
                continue
            content_x = 0 if isinstance(message, UserMessage) else icon_column
            tint = conversation_tint
            # User messages carry the chat tint; assistant prose stays unboxed.
            underlying = window_color
            prose = isinstance(message, (UserMessage, AssistantMessage))
            side = row_width - fitted_width if isinstance(message, UserMessage) else 0
            row_span = fitted_width if isinstance(message, UserMessage) else row_width
            if isinstance(message, UserMessage) and _visible(y, full_height, clip):
                underlying = _card(x + side, y, row_span, full_height, tint,
                                   max_bg_value=Toggles.Chat.user_message_bg_value, ui_scale=Melty.ui_scale, channel=Melty.get_channel() if Melty.channels_split else None) or underlying
            if isinstance(message, ToolCall):
                underlying = tuple(c * (bash_darken if isinstance(message, CommandExecution) else python_darken)
                                   for c in underlying[:3])
                # The header row carries only its icon chip; the block background
                # starts under it, covering the expanded content.
                if expanded and full_height > summary_height + Melty.px(4) and _visible(y, full_height, clip):
                    _code_background(x + content_x, y + summary_height, fitted_width - content_x,
                                     full_height - summary_height - Melty.px(4),
                                     underlying, shadow=bash_shadow if isinstance(message, CommandExecution) else None)
            if tags:
                from meltygui.view.file_view import draw_changed_file_header
                from meltygui.models.file_meta import FileMeta
                metadata = file_metadata
                painted = FileMeta.painted_tint if metadata else None
                for filename, counts, tag_x, tag_y, tag_width in tags:
                    file_y = y + tag_y
                    if _visible(file_y, header_height, clip):
                        file_path = str(Path(message["details"].get("cwd") or "") / filename)
                        file_tint = (painted(metadata.get(file_path)) if painted else None) or underlying
                        imgui.set_cursor_screen_pos((x + tag_x, file_y))
                        if draw_changed_file_header(filename, file_tint, draw_state,
                                view_id=row_key + ":file:" + filename, width=tag_width, height=header_height,
                                added=counts["added"], removed=counts["removed"], active=True,
                                prefix="", shadow_offset=Toggles.Chat.file_tag_shadow_offset):
                            state.message_expanded[row_key] = not expanded
                            view["relayout"] = True
                            changed = True
            if (summary_height and has_header or collapsible) and _visible(y, header_height, clip):
                header_x = x + content_x
                # An icon row shows its caret IN the icon while the pointer is over
                # the row. priority_delta 3 = the wrapper level: a lower delta is
                # pruned by the enclosing wrapper's own blocker and never delivers.
                row_hovered = not icon or draw_state.on_action(
                    "cursor_hover", view_id=row_key + ":hover", priority_delta=3,
                    rect=(x, y, x + fitted_width, y + full_height)) is not None
                if icon:
                    chip_color = (underlying if isinstance(message, ToolCall)
                                  else tuple(c * bash_darken for c in window_color))
                    glyph_x = _icon_chip(icon, x, y, icon_column - Melty.px(2), header_height, chip_color)
                    if not (collapsible and row_hovered):
                        _title(icon, glyph_x, y, icon_column, header_height, chip_color, icon_brightness)
                if collapsible and row_hovered:
                    if _caret(draw_state, row_key + ":expand", x, y, icon_column - Melty.px(2), header_height,
                              expanded, chip_color if icon else tint, icon_brightness if icon else 1.0, ui_scale=Melty.ui_scale):
                        state.message_expanded[row_key] = not expanded
                        view["relayout"] = True
                        changed = True
                failed = _message_failed(message)
                if not tags and not (isinstance(message, ReasoningMessage) and expanded):
                    label = _message_label(message, expanded)
                    if icon and label.startswith(icon):
                        label = label[len(icon):].lstrip()  # the icon is already on the row
                    if label:
                        # +4 for the same inner inset prose, tag text and terminal output sit at.
                        _title(label, header_x + Melty.px(4), y, fitted_width - (header_x - x) - Melty.px(4) - (Melty.px(100) if failed else 0),
                               header_height, underlying if isinstance(message, ToolCall) else tint,
                               command_text_brightness if icon else 1.0)
                if failed:
                    # Collapsed: the header line reserved room at its end. Expanded:
                    # the block's bottom-right corner, on the Show more / less line.
                    badge_y = y if summary_height else y + full_height - Melty.px(4) - header_height
                    _failure_badge(x + fitted_width - Melty.px(96), badge_y, Melty.px(92), header_height)
            leaf_y = y + summary_height
            for path, value, leaf_height, indent, caption in leaves:
                if _visible(leaf_y, leaf_height, clip):
                    text_y = leaf_y
                    left = x + side + indent + content_x
                    leaf_width = row_span - indent - content_x - (user_inset if isinstance(message, UserMessage) else 0)
                    if isinstance(message, AssistantMessage):
                        leaf_width = min(leaf_width, terminal_max_width)  # assistant prose caps like a terminal
                    if not isinstance(message, ToolCall) and isinstance(value, (PythonString, BashString, ToolOutput)):
                        is_bash = isinstance(value, (BashString, ToolOutput))
                        _code_background(left, leaf_y, leaf_width, leaf_height,
                                         tuple(c * (bash_darken if is_bash else python_darken) for c in underlying[:3]),
                                         shadow=bash_shadow if is_bash else None)
                    if isinstance(value, BashString):
                        caption = "$ bash"
                    if caption:
                        _title(caption, left, text_y, leaf_width, header_height, tint)
                        text_y += header_height
                    if path == ("terminal",):
                        imgui.set_cursor_screen_pos((x + content_x, text_y))
                        draw_chat_terminal(value, name=row_key + ":terminal", width=fitted_width - content_x, height=leaf_height)
                    elif isinstance(value, ImageReference):
                        image_name = row_key + ":image:" + repr(path)
                        # A user bubble contains its image; resizing can still grow
                        # into the conversation's full user-message span.
                        if isinstance(message, UserMessage):
                            leaf_width = row_width - chat_indent - indent - user_inset
                        previous_size = image_box(image_cache().entry(value), leaf_width,
                                                  state.image_sizes.get(image_name))
                        size = _draw_image(value, left, text_y, leaf_width,
                                           leaf_height - header_height * (2 if caption else 1), header_height, tint,
                                           name=image_name, size=state.image_sizes.get(image_name))
                        if size is not None and size != state.image_sizes.get(image_name):
                            state.image_sizes[image_name] = size
                            if size != previous_size:
                                view["relayout"] = True
                                changed = True
                    elif isinstance(value, Reference):
                        # Never paint data URLs / encoded image data as text.
                        name = value.get("name") or value.get("path") or ""
                        _title(value.label + (" · " + str(name) if name else "") + " (preview pending)",
                               left, text_y, leaf_width, header_height, tint)
                    elif isinstance(value, str) and prose and not isinstance(value, CodeString):
                        # Selection: a drag on a leaf starts it, the drag's
                        # head follows the pointer across leaves, Ctrl+C copies
                        # (below). All draw-list: no editor per leaf.
                        index = prose_index.get((row_key, path))
                        selection = view.get("selection")
                        span = None
                        if index is not None:
                            line_px, char_w = _prose_metrics()
                            leaf_rect = (left, text_y, left + leaf_width, text_y + leaf_height)
                            if draw_state.on_action("left_mouse_down", view_id=row_key + ":select:" + repr(path),
                                                    rect=leaf_rect, priority_delta=3) is not None:
                                offset = prose_offset(value, left, text_y, imgui.get_mouse_pos(), line_px, char_w, ui_scale=Melty.ui_scale)
                                selection = view["selection"] = {"anchor": (index, offset), "head": (index, offset),
                                                                 "dragging": True}
                                selecting = True
                                changed = True
                            elif selection and selection.get("dragging") and imgui.is_mouse_down(0):
                                mouse = imgui.get_mouse_pos()
                                if text_y <= mouse[1] < text_y + leaf_height:
                                    head = (index, prose_offset(value, left, text_y, mouse, line_px, char_w, ui_scale=Melty.ui_scale))
                                    if head != tuple(selection["head"]):
                                        selection["head"] = head
                                        changed = True
                            if selection:
                                span = selection_slice(selection, index, len(str(value)))
                        _draw_prose(value, left, text_y, leaf_width, leaf_height, tint, clip, selected=span)
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
                           height=header_height, background=False, ui_scale=Melty.ui_scale):
                    state.output_expanded[row_key] = not show_all
                    changed = True
            y += full_height
    selection = view.get("selection")
    if selection is not None:
        if selection.get("dragging"):
            if imgui.is_mouse_down(0):
                changed = True           # keep frames coming while the drag runs
            else:
                selection["dragging"] = False
                if tuple(selection["anchor"]) == tuple(selection["head"]):
                    view["selection"] = None      # a press without a drag selects nothing
                changed = True
        elif imgui.is_mouse_clicked(0) and not selecting:
            view["selection"] = None              # a click elsewhere clears it
            changed = True
        elif imgui.get_io().key_ctrl and any(k == glfw.KEY_C for k, _ in Melty.frame_key_events):
            text = selection_text(selection, prose_texts)
            if text:
                imgui.set_clipboard_text(text)
    return changed


def draw_chat_requests(requests, draw_state, state, key, width, height, tint):
    """Pending approvals stay in a bounded scroll area above the composer."""
    from meltygui.view.chat_decoration_view import _button
    from meltygui.chat.chat_interface import _draw_prose
    from meltygui.chat.chat_interface import _text_layout
    from meltygui.chat.chat_interface import _title
    from meltygui.chat.chat_interface import _viewport
    from meltygui.view.text_view import draw_text

    line_height = imgui.get_text_line_height() * 1.2
    layouts = []
    total = 0
    for request_id, request in requests.items():
        if "answer" in request:
            continue
        text, text_height = _text_layout(state, (key, request_id), request.get("text", "Tool permission"), wrap_width=width)
        questions = request.get("data", {}).get("questions", []) if request["kind"] != "approval" else []
        layouts.append((request_id, request, text, text_height, questions))
        total += text_height + len(questions) * Melty.px(76) + Melty.px(36)
    changed = False
    with _viewport(draw_state, state, key, width, height, total) as (x, y, clip):
        for request_id, request, text, text_height, questions in layouts:
            _draw_prose(text, x, y, width - Melty.px(7), text_height, tint, clip)
            y += text_height
            answers = state.answers.setdefault(str(request_id), {})
            for question in questions:
                _title(question["question"], x, y, width, Melty.px(24), tint, ellipsis=True)
                y += Melty.px(26)
                imgui.set_cursor_screen_pos((x, y))
                edited, value = draw_text(answers.get(question["id"], ""), name=key + ":" + question["id"],
                    width=width - Melty.px(7), height=50, wrap=False, syntax_highlight=False,
                    show_header=False, fim="", is_tree=False)
                if edited:
                    answers[question["id"]] = value
                    changed = True
                y += Melty.px(50)
            decisions = ("accept", "decline") if request["kind"] == "approval" else ("answer",)
            for index, decision in enumerate(decisions):
                top, bottom = max(y, clip[1]), min(y + Melty.px(30), clip[3])
                if bottom > top and _button(draw_state, f"answer:{request_id}:{decision}", decision.title(),
                    x + index * Melty.px(100), top, Melty.px(95), tint, height=bottom - top, ui_scale=Melty.ui_scale):
                    request["answer"] = ({"decision": decision} if decision != "answer" else
                        {"answers": {identifier: {"answers": [value]} for identifier, value in answers.items()}})
                    changed = True
            y += Melty.px(36)
    return changed


def draw_token_badge(chat, x, y, width, height):
    """The conversation's context usage at the right end of its header
    (tooltip: the count against the window); returns the width it took."""
    from meltygui.chat.chat_interface import _label_width
    from meltygui.chat.chat_interface import _title
    from meltygui.model.chat_model import token_badge
    from meltygui.view.chat_decoration_view import _color

    label, color, detail = token_badge(chat)
    padding = Melty.px(10)
    badge_width = min(width, _label_width(label) + 2 * padding)
    left = x + width - badge_width
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    draw_list.add_rect_filled(left, y, left + badge_width, y + height, _color(color, 0.18), rounding=Melty.px(5))
    _title(label, left + padding, y, max(1, badge_width - 2 * padding), height, color, text_color=color, ellipsis=True)
    if imgui.is_mouse_hovering_rect(left, y, left + badge_width, y + height):
        imgui.set_tooltip(detail)
    return badge_width


def draw_turn_banner(chat, width, height):
    """Whose turn it is, under the transcript (`turn_status`'s banner text)."""
    from meltygui.chat.chat_interface import _title
    from meltygui.model.chat_model import turn_status
    from meltygui.view.chat_decoration_view import _color

    _, label, tint = turn_status(chat)
    x, y = imgui.get_cursor_screen_pos()
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    draw_list.add_rect_filled(x, y, x + width, y + height - Melty.px(4), _color(tint, 0.13), rounding=Melty.px(5))
    _title(label, x + Melty.px(10), y, max(1, width - Melty.px(20)), height - Melty.px(4), tint,
           text_color=tint, ellipsis=True)
    imgui.set_cursor_screen_pos((x, y))
    imgui.dummy(width, height)


def draw_conversation_title(chat, draw_state, state, selected, x, y, width, height, tint):
    """Double-click the header title to rename the same chat shown in the lists."""
    from meltygui.chat.chat_interface import _title
    from meltygui.view.text_view import draw_text
    import meltygui.core.windowing.window_api as glfw

    changed = False
    renaming = state.rename
    editing = (renaming is not None and renaming.get("pane") == "header"
               and renaming["account"] == state.account and renaming["key"] == selected)
    if not editing and draw_state.on_action(
            "left_mouse_double_clicked", view_id="rename:header:" + state.account + ":" + selected,
            rect=(x, y, x + width, y + height), priority_delta=5) is not None:
        state.rename = renaming = {"account": state.account, "key": selected, "pane": "header",
                                   "draft": chat["title"], "focus": True}
        editing = changed = True
    if not editing:
        _title(chat["title"], x, y, width, height, tint, ellipsis=True)
        return changed
    imgui.set_cursor_screen_pos((x, y))
    request_focus = renaming["focus"]
    edited, value, text_ds = draw_text(renaming["draft"],
        name="chat-title:" + state.account + ":" + selected, wrap=False,
        request_focus=request_focus, select_all_on_focus=request_focus, single_line=True,
        return_extras=True, width=width, height=height, is_tree=False,
        editable=True, focusable=True, syntax_highlight=False, autocomplete=False,
        show_header=False, show_bg=False, shadow=False, show_widgets=False,
        show_file_header=False, show_jump_bar=False, scope_collapse=False,
        disable_scroll=True, freeze_resize=True, use_cache=True, imgui_padding=False,
        tint=tint, text_tint=_text_tint(tuple(tint)), fim="")
    focused = Melty.text_focused_ds is text_ds
    if focused:
        renaming["focused"] = True
        renaming["focus"] = False
    if edited:
        renaming["draft"] = value
        changed = True
    keys_pressed = {event[0] for event in Melty.frame_key_events} if focused else set()
    if glfw.KEY_ESCAPE in keys_pressed:
        state.rename = None
        changed = True
    elif (glfw.KEY_ENTER in keys_pressed or glfw.KEY_KP_ENTER in keys_pressed
          or renaming.get("focused", False) and not focused):
        if renaming["draft"].strip():
            chat["title"] = renaming["draft"].strip()
        state.rename = None
        changed = True
    return changed


@render_func(tint=(0.35, 0.55, 0.75), auto_resize=False, use_cache=True,
             disable_scroll=True, imgui_padding=False, show_header=False, show_bg=False)
def draw_chat_navigation(input_value, draw_state=None, state: ChatInterfaceState = None,
                         row_edges=None, new_conversation=None, file_metadata=None, **kwargs):
    """Conversation lists and Internet Accounts share a resizable left column."""
    from meltygui.chat.chat_interface import AGE_FILTERS
    from meltygui.view.chat_decoration_view import _button
    from meltygui.view.chat_decoration_view import _caret
    from meltygui.chat.chat_interface import _label_width
    from meltygui.chat.chat_interface import _title
    from meltygui.model.chat_model import age_cutoff
    from meltygui.view.chat_sidebar_view import draw_chat_sidebar
    from meltygui.view.chat_sidebar_view import show_all_control
    from meltygui.chat.chat_interface import navigation_row_sizes
    from meltygui.core.layout.column_core import RowLayout
    import meltygui.accounts.internet_accounts as internet_accounts

    changed = False
    if not hasattr(state, "sections_expanded"):
        state.sections_expanded = {"recent": True, "all": True, "accounts": True}
    panes = ("accounts", "all", "recent")
    opened = [state.sections_expanded.get(pane, True) for pane in panes]
    heading_height = Melty.px(26)
    tint = Toggles.Chat.navigation_tint
    def section_heading(pane, x, y, width):
        control_changed, control_width = show_all_control(
            pane, draw_state, state, x, y, width, heading_height)
        width = max(0, width - control_width)
        expanded = state.sections_expanded.get(pane, True)
        toggled = _caret(draw_state, "section:" + pane, x, y, Melty.px(18), heading_height, expanded, tint, ui_scale=Melty.ui_scale)
        label = {"all": "All chats", "recent": "Recent chats", "accounts": "Internet Accounts"}[pane]
        _title(label, x + Melty.px(20), y, max(0, width - Melty.px(20)), heading_height, tint, brightness=0.7)
        header_clicked = _button(draw_state, "section-label:" + pane, "", x + Melty.px(20), y,
                           max(0, width - Melty.px(20)), tint, height=heading_height,
                           background=False, shadow=False, event="left_mouse_down", ui_scale=Melty.ui_scale)
        if toggled:
            state.sections_expanded[pane] = not expanded
        elif header_clicked:
            state.sections_expanded[pane] = True
        return control_changed or toggled or (header_clicked and not expanded)
    window = draw_state.parent_window or draw_state
    top = imgui.get_cursor_screen_pos()[1] - window.abs_top
    minimum = heading_height + 8  # RowLayout's top and bottom padding.
    row_heights = navigation_row_sizes(state, row_edges, opened, top, draw_state.height, minimum)
    # Leave unused space below the headings when every section is closed.
    # Otherwise the last capped row would have to fill the whole column.
    extent = draw_state.height if any(opened) else minimum * len(panes)
    rows = RowLayout(draw_state, 3, row_edges=row_edges,
                     top_edge={"y": top}, bottom_edge={"y": top + extent},
                     row_heights=row_heights,
                     row_mins=[minimum] * 3,
                     row_maxes=[None if expanded else minimum for expanded in opened],
                     persist=True, resizable=True,
                     padding=4, padding_x=0, border_color=None)
    for index, pane in enumerate(panes):
        with rows.cell(index) as height:
            width = rows.inner_width()
            x, top = imgui.get_cursor_screen_pos()
            changed |= section_heading(pane, x, top, width)
            if not opened[index]:
                continue
            y = top + heading_height
            if pane == "accounts":
                imgui.set_cursor_screen_pos((x, y))
                from meltygui.view.account_view import draw_internet_accounts
                edited, _ = draw_internet_accounts(
                    internet_accounts.accounts, name="chat-internet-accounts",
                    width=width, height=max(0, height - heading_height),
                    show_header=False, show_name=False, show_bg=False, shadow=False,
                    # This embedded view borrows the chat view's accounts;
                    # collapsing it should not close their running backends.
                    on_cleanup=None)
                changed |= edited
                continue
            if pane == "recent":
                chip_x, chip_height = x, Melty.px(22)
                hours_selected = getattr(state, "age_hours", 0) or 24
                for label, hours in AGE_FILTERS:
                    if not hours:
                        continue
                    chip_width = _label_width(label) + Melty.px(16)
                    if chip_x > x and chip_x + chip_width > x + width:
                        chip_x, y = x, y + chip_height + Melty.px(3)
                    if _button(draw_state, "age:" + label, label, chip_x, y, chip_width, tint,
                               height=chip_height, selected=hours == hours_selected, shadow=False,
                               background=hours == hours_selected, ui_scale=Melty.ui_scale):
                        state.age_hours = hours
                        changed = True
                    chip_x += chip_width + Melty.px(4)
                y += chip_height + Melty.px(5)
            imgui.set_cursor_screen_pos((x, y))
            edited, _ = draw_chat_sidebar(input_value, draw_state, state, width,
                max(0, height - (y - top)),
                cutoff=age_cutoff(getattr(state, "age_hours", 0) or 24) if pane == "recent" else None,
                new_conversation=new_conversation, pane=pane, file_metadata=file_metadata)
            changed |= edited
    rows.finish()
    return changed, input_value


def draw_effort_slider(draw_state, key, value, levels, x, y, width, height, tint):
    """A discrete slider using the owning view's replayable input actions."""
    from meltygui.view.chat_decoration_view import _color
    from meltygui.chat.chat_interface import _title

    if not levels or value not in levels:
        _title("Effort: " + ("Unavailable" if not levels else "Loading…"), x, y, width, height,
               tint, brightness=0.6, ellipsis=True)
        return False, value
    selected = levels.index(value)
    left, right = x + Melty.px(8), x + width - Melty.px(8)
    rect = (x, y, x + width, y + height)
    pressed = draw_state.on_action("left_mouse_down", view_id=key, rect=rect, priority_delta=4)
    dragged = draw_state.on_action("left_mouse_drag", view_id=key + ":drag", rect=rect, priority_delta=4)
    changed = False
    if len(levels) > 1 and (pressed is not None or dragged is not None):
        fraction = max(0, min(1, (imgui.get_mouse_pos()[0] - left) / max(1, right - left)))
        index = round(fraction * (len(levels) - 1))
        changed = index != selected
        selected = index
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    track_y = y + height - Melty.px(5)
    color = _text_tint(tuple(tint))
    draw_list.add_line(left, track_y, right, track_y, _color(color, 0.3), Melty.px(2))
    for index in range(len(levels)):
        tick_x = left + (right - left) * index / max(1, len(levels) - 1)
        draw_list.add_circle_filled(tick_x, track_y, Melty.px(2), _color(color, 0.4), 12)
    knob_x = left + (right - left) * selected / max(1, len(levels) - 1)
    draw_list.add_circle_filled(knob_x, track_y, Melty.px(4), _color(color), 16)
    _title("Effort: " + levels[selected].title(), x + Melty.px(4), y,
           width - Melty.px(8), height - Melty.px(9), tint, brightness=0.8, ellipsis=True)
    return changed, levels[selected]


@render_func(auto_resize=False, min_width=700, min_height=500, show_bg=False, shadow=False, selectable=False,
             on_cleanup=_cleanup_chat, use_cache=True, disable_scroll=True, imgui_padding=False, indent_size=0)
def draw_chat_interface(input_value=None, draw_state=None, bg_offset=-2, state: ChatInterfaceState = None,
                        column_edges=None, new_project=None, default_project=None, file_metadata=None,
                        ctrl_shift_equal_down=False, ctrl_shift_minus_down=False, layout_frame=None,
                        **kwargs):
    """The chat window. A new conversation runs in `new_project` when given
    (an app started for one project), else in the selected conversation's
    project, else `default_project` (an app's cwd; the studio: its checkout).
    ``layout_frame`` (a tile's ``(left, right, top, bottom)`` edge dicts, as
    draw_tile_content passes) pins the sidebar / transcript columns inside
    that tile instead of the window frame."""
    from meltygui.chat.chat_interface import REFRESH_S
    from meltygui.view.chat_decoration_view import _button
    from meltygui.view.chat_decoration_view import _color
    from meltygui.chat.chat_interface import _draw_prose
    from meltygui.chat.chat_interface import _label_width
    from meltygui.view.chat_decoration_view import _running_dot
    from meltygui.chat.chat_interface import _text_layout
    from meltygui.chat.chat_interface import _title
    from meltygui.chat.chat_interface import apply_folder_shortcuts
    from meltygui.chat.chat_interface import chat_activity
    from meltygui.chat.chat_interface import chat_context_menu_items
    from meltygui.chat.chat_interface import chat_effort_levels
    from meltygui.chat.chat_interface import chat_models
    from meltygui.chat.chat_interface import chat_project_choices
    from meltygui.chat.chat_interface import chat_sources
    from meltygui.model.chat_model import conversation_folder_tint
    from meltygui.model.chat_model import is_active
    from meltygui.chat.chat_interface import is_new_chat
    from meltygui.chat.chat_interface import pick_source
    from meltygui.model.chat_model import project_tint
    from meltygui.chat.chat_interface import source_label
    from meltygui.chat.chat_interface import switch_new_chat_project
    from meltygui.chat.chat_interface import switch_new_chat_source
    from meltygui.chat.messages import user_message
    from meltygui.view.text_view import draw_text
    from meltygui.core.layout.column_core import ColumnLayout
    import meltygui.accounts.internet_accounts as internet_accounts
    import meltygui.core.windowing.window_api as glfw

    # Ephemeral layout state also adopts already-open windows on hotswap.
    if not hasattr(state, "viewports"):
        state.viewports = {}
        state.text_layouts = {}
    if not hasattr(state, "sidebar_chat_limits"):
        state.folder_column_defaults = {}
        state.sidebar_chat_limits = {}
        state.hovered_folder_action = None
        state.pending_smart_collapse = None
    folder_events = []
    if ctrl_shift_equal_down:
        folder_events.append((glfw.KEY_EQUAL, glfw.MOD_CONTROL | glfw.MOD_SHIFT))
    if ctrl_shift_minus_down:
        folder_events.append((glfw.KEY_MINUS, glfw.MOD_CONTROL | glfw.MOD_SHIFT))
    folders_changed = apply_folder_shortcuts(state, folder_events)
    accounts = internet_accounts.accounts
    if not accounts.loaded:
        accounts.load()
    accounts.ensure_kinds()
    sources = chat_sources(accounts)
    if not sources:
        return False, input_value
    kinds = {account_id: kind for account_id, kind in sources}
    if state.account not in kinds:
        state.account = next((account_id for account_id, kind in sources if kind.chat_available), sources[0][0])
    shown = [account_id for account_id in (getattr(state, "sources", None) or [state.account]) if account_id in kinds]
    if not shown:
        shown = [state.account]
    if state.account not in shown:
        state.account = shown[0]
    state.sources = shown
    kind = kinds[state.account]
    state.provider = kind.name
    account = accounts[state.account]
    changed = folders_changed

    def wake():
        # Like Fast Dock's external-change edge: the worker has queued new data.
        from meltygui.core.windowing.glfw_utils import request_render
        if Melty.cache is not None:
            Melty.cache.invalidate_up_by_obj(state, force=True)
            if getattr(draw_state, "_tile_id", None) is not None:
                Melty.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=8)
        request_render()

    # A session per shown source (None: a registered placeholder, kept blank).
    proxies = {account_id: kinds[account_id].chats(accounts[account_id], wake=wake) for account_id in shown}
    proxy = proxies[state.account]
    for shown_proxy in proxies.values():
        if shown_proxy is not None:
            shown_proxy.wake = wake
    # Drain every open session, shown or not: a hidden session still streams.
    for other in accounts.values():
        other_proxy = other.get("_chat_proxy")
        if other_proxy is not None and not other_proxy.closed:
            # Only the displayed transcript needs eager history refreshes.
            # Source tabs can leave several proxies alive in this window.
            other_proxy.active_key = (state.selected.get(state.account)
                                      if other_proxy is proxy else None)
            changed |= other_proxy.drain()
            other_proxy.reconcile()  # collect edits made in another view before applying metadata
    x, y = imgui.get_cursor_screen_pos()
    tint = kind.tint
    notice = "" if proxy is None else (proxy.error or ("Loading conversations…" if proxy.loading else ""))
    model_error = getattr(proxy, "models_error", None)
    if model_error:
        notice = (notice + "\n" if notice else "") + "Could not load models: " + model_error
    if notice:
        for line in notice.split("\n"):
            imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.65, 0.45)), line)
            imgui.dummy(1, imgui.get_text_line_height())
        if proxy.error or model_error:
            x, y = imgui.get_cursor_screen_pos()
            if _button(draw_state, "reconnect-chat", "Reconnect", x, y, Melty.px(110), tint, ui_scale=Melty.ui_scale):
                kind.close_chat(account)
                return True, input_value
            imgui.dummy(1, Melty.px(30))
    selected = state.selected.get(state.account)
    if proxy is not None and selected not in proxy:
        selected = next(iter(proxy), None)
        state.selected[state.account] = selected
    body_top = imgui.get_cursor_screen_pos()[1]
    body_bottom = draw_state.abs_top + (draw_state.height or Melty.px(760)) - Melty.px(16)
    body_height = max(Melty.px(180), body_bottom - body_top)
    frame_kwargs = ({"left_edge": layout_frame[0], "right_edge": layout_frame[1],
                     "band": layout_frame[2:]} if layout_frame is not None else {})
    # The minimums fit the frame they lay out in: a tile narrower than both
    # gives the transcript the room first, so the columns never pass its edge.
    available = (layout_frame[1]["x"] - layout_frame[0]["x"] if layout_frame is not None
                 else draw_state.width) or 0
    sidebar_min, transcript_min = 180, 400
    if 0 < available < sidebar_min + transcript_min:
        sidebar_min = min(sidebar_min, max(60, available * 0.25))
        transcript_min = max(1, min(transcript_min, available - sidebar_min))
    columns = ColumnLayout(draw_state, 2, column_edges=column_edges,
                           column_widths=[260, None], column_mins=[sidebar_min, transcript_min],
                           padding=Melty.px(Toggles.Chat.column_gap), padding_y=0, border_color=None,
                           **frame_kwargs)
    with columns.cell(0, height=body_height) as width:
        x, y = imgui.get_cursor_screen_pos()
        top = y
        # The source bar: a tab per chat account, in its provider's tint, the
        # shown ones lifted. Click: that source; Shift+click: toggle it into
        # / out of the shown set. Tabs wrap in a narrow column.
        tab_height, tab_x = Melty.px(24), x
        for account_id, source_kind in sources:
            label = source_label(account_id, source_kind, accounts)
            tab_width = _label_width(label) + Melty.px(18)
            if tab_x > x and tab_x + tab_width > x + width:
                tab_x, y = x, y + tab_height + Melty.px(3)
            if _button(draw_state, "source:" + account_id, label, tab_x, y, tab_width, source_kind.tint,
                       enabled=source_kind.chat_available, height=tab_height, selected=account_id in shown,
                       shadow=False, ui_scale=Melty.ui_scale):
                pick_source(state, account_id, {a: k.name for a, k in sources}, additive=imgui.get_io().key_shift)
                changed = True
                # The sessions for this frame were opened above with the old
                # set: the next frame (asked for now) draws the new one.
                from meltygui.core.windowing.glfw_utils import request_render
                request_render()
            tab_x += tab_width + Melty.px(4)
        y += tab_height + Melty.px(Toggles.Chat.header_margin)
        shown_sources = [(account_id, proxies[account_id], kinds[account_id],
                          source_label(account_id, kinds[account_id], accounts)) for account_id in shown]

        def start_conversation(account_id, chats, project=None):
            """A fresh conversation in that source: in `project` (a heading's +),
            else `new_project` when the app was started for one, else its
            selected conversation's project, else `default_project`."""
            from meltygui.core.runtime.paths import application_root
            key = str(uuid.uuid4())
            current = chats.get(state.selected.get(account_id))
            project = (project or new_project or (current["project"] if current else None)
                       or state.projects.get(account_id) or default_project or str(application_root()))
            created = time.time()
            chats[key] = {"title": "New conversation", "project": project,
                          "created_at": created, "updated": created}
            state.selected[account_id] = key
            state.account = account_id
            state.provider = kinds[account_id].name

        # Every few seconds each shown backend looks for sessions written
        # elsewhere (a terminal's Claude Code): their rows light up too.
        now = time.time()
        for shown_proxy in proxies.values():
            if shown_proxy is not None and now - getattr(shown_proxy, "refreshed", 0.0) > REFRESH_S:
                shown_proxy.refreshed = now
                shown_proxy.refresh()
        imgui.set_cursor_screen_pos((x, y))
        edited, _ = draw_chat_navigation(shown_sources, name="chat-navigation",
            state=state, new_conversation=start_conversation,
            context_menu=chat_context_menu_items(state, shown_sources),
            width=width, height=max(0, body_height - (y - top) - Melty.px(35)),
            tint=Toggles.Chat.navigation_tint, file_metadata=file_metadata)
        changed |= edited
        y = top + body_height - Melty.px(30)
        # The New conversation button follows the list directly (a list taller
        # than the column scrolls and the button sits at the column's foot);
        # it starts one in the primary folder (a heading's + picks its folder).
        imgui.set_cursor_screen_pos((x, y))
        if _button(draw_state, "new-chat", "+ New conversation", x, y, min(width, Melty.px(190)), tint,
                   proxy is not None and not proxy.loading and not proxy.error, ui_scale=Melty.ui_scale):
            start_conversation(state.account, proxy)
            changed = True
        imgui.dummy(width, Melty.px(25))
    # Sidebar selection takes effect in the same frame.
    selected = state.selected.get(state.account)
    proxy = proxies.get(state.account)
    kind = kinds[state.account]
    account = accounts[state.account]
    tint = kind.tint
    with columns.cell(1, height=body_height) as width:
        if proxy is not None and selected in proxy:
            chat = proxy[selected]
            meta = chat.metadata
            chat_tint = tuple(project_tint(chat["project"], file_metadata=file_metadata) or kind.tint)
            x, y = imgui.get_cursor_screen_pos()
            header_width = max(1, width - Melty.px(100))
            badge_width = draw_token_badge(chat, x, y, header_width, Melty.px(28))
            changed |= draw_conversation_title(chat, draw_state, state, selected, x, y,
                max(1, header_width - badge_width - Melty.px(10)), Melty.px(28), chat_tint)
            concise = getattr(state, "concise", False)
            if _button(draw_state, "concise-view", "✓ Concise" if concise else "Concise",
                       x + width - Melty.px(95), y, Melty.px(95), chat_tint,
                       height=Melty.px(28), background=False, ui_scale=Melty.ui_scale):
                state.concise = not concise
                changed = True
            imgui.set_cursor_screen_pos((x, y))
            imgui.dummy(width, Melty.px(28))
            x, y = imgui.get_cursor_screen_pos()
            project_label, project_height = _text_layout(state, "directory:" + selected,
                "\uf07b  " + (chat["project"] or "No working directory"), wrap_width=max(1, width))
            folder_tint = tuple(conversation_folder_tint(chat["project"], file_metadata=file_metadata) or chat_tint)
            _title(project_label, x, y, width, project_height, folder_tint,
                   text_color=folder_tint)
            imgui.set_cursor_screen_pos((x, y))
            imgui.dummy(width, project_height + Melty.px(6))
            x, y = imgui.get_cursor_screen_pos()
            transcript_key = "history:" + state.account + ":" + selected
            # Reserve the composer before laying out the history. Scrolling
            # changes only message coordinates inside this history region.
            active = is_active(chat)
            project_editable = is_new_chat(chat) and not chat.get("external_busy") and not chat.get("locked")
            working = chat["running"] or bool(chat.turn_id) or "send" in chat.inflight
            action_extra_height = Melty.px(36) if working else 0
            effort_extra_height = Melty.px(36) if project_editable and kind.name in ("codex", "anthropic") else 0
            controls_extra_height = action_extra_height + effort_extra_height
            queue_count = len(chat.get("queued_messages", {}))
            queue_height = Melty.px(28) * (1 + min(3, queue_count)) if queue_count else 0
            requests_height = min(Melty.px(240), body_height * 0.35) if chat["requests"] else 0
            status_message = chat.error or ("Loading conversation…" if chat.loading else "")
            if chat.get("locked") and not status_message:
                from meltygui.chat.writer_locks import lock_message
                status_message = lock_message(chat.get("lock_owner"))
            locked = chat.get("locked", False)
            status_pad = Melty.px(12) if locked and status_message else 0
            status_icon = Melty.px(24) if status_pad else 0
            status_text, status_height = _text_layout(state, transcript_key + ":status", status_message,
                wrap_width=max(1, width - 2 * status_pad - status_icon)) if status_message else ("", 0)
            status_height += 2 * status_pad
            history_height = max(Melty.px(40), body_bottom - imgui.get_cursor_screen_pos()[1]
                                 - requests_height - status_height - queue_height - controls_extra_height - Melty.px(120 + (26 if active else 0)))
            banner_height = min(Melty.px(34), history_height)
            changed |= draw_messages(chat["messages"], draw_state, state, transcript_key,
                                 width, max(1, history_height - banner_height), conversation_tint=chat_tint, revision=proxy.revision, file_metadata=file_metadata)
            draw_turn_banner(chat, width, banner_height)
            if active:
                x, y = imgui.get_cursor_screen_pos()
                _running_dot(x + Melty.px(10), y + Melty.px(12), chat_tint, ui_scale=Melty.ui_scale, channel=Melty.get_channel() if Melty.channels_split else None)
                _title(chat_activity(chat, kind.chat_label), x + Melty.px(22), y, width - Melty.px(22), Melty.px(24),
                       chat_tint, brightness=0.8, ellipsis=True)
                imgui.set_cursor_screen_pos((x, y))
                imgui.dummy(1, Melty.px(26))
            if status_message:
                x, y = imgui.get_cursor_screen_pos()
                if locked:
                    imgui.get_window_draw_list().add_rect_filled(x, y, x + width, y + status_height,
                        _color((0.5, 0.12, 0.12), 0.5), rounding=Melty.px(7))
                    _title("\uf023", x + status_pad, y + status_pad, status_icon,
                           Melty.px(23), (0.95, 0.55, 0.5))
                _draw_prose(status_text, x + status_pad + status_icon, y + status_pad,
                    width - 2 * status_pad - status_icon, status_height - 2 * status_pad,
                    (0.95, 0.7, 0.65) if locked else (0.95, 0.65, 0.4))
                imgui.dummy(1, status_height)
            if requests_height:
                changed |= draw_chat_requests(chat["requests"], draw_state, state,
                    transcript_key + ":requests", width, requests_height, chat_tint)
            changed |= draw_chat_queue(proxy, selected, chat, draw_state, state, transcript_key, width, chat_tint)
            draft_key = state.account + ":" + selected
            recovered = chat.pop("recovered_draft", None)
            if recovered:
                current = state.drafts.get(draft_key, "")
                state.drafts[draft_key] = recovered + ("\n\n" + current if current else "")
                changed = True
            locked = chat.get("locked", False)
            draft = state.drafts.get(draft_key, "")
            draft_generation = getattr(state, "draft_generations", {}).get(draft_key, 0)
            edited, draft = draw_text(draft, name="Message:" + selected + ":" + str(draft_generation),
                height=85, width=width, wrap=False, syntax_highlight=False, line_numbers=None, fim="", show_header=False,
                use_cache=True, is_tree=False, shadow=False, bg_offset=-1)
            if edited:
                state.drafts[draft_key] = draft
                changed = True
            x, y = imgui.get_cursor_screen_pos()
            can_send = bool(draft.strip()) and chat.loaded and not locked and not chat.get("external_busy") and (not chat.error or chat.get("retryable_error"))
            send = _button(draw_state, "send", "Send", x, y, Melty.px(85), chat_tint,
                           can_send, height=Melty.px(30), shadow=False, ui_scale=Melty.ui_scale)
            queue = working and _button(draw_state, "queue", "Queue", x + Melty.px(95), y,
                                        Melty.px(85), chat_tint, can_send, height=Melty.px(30), shadow=False, ui_scale=Melty.ui_scale)
            if send or queue:
                if proxy.queue_message(selected, user_message(draft), interrupt=bool(send)):
                    state.drafts[draft_key] = ""
                    if not hasattr(state, "draft_generations"):
                        state.draft_generations = {}
                    state.draft_generations[draft_key] = draft_generation + 1
                changed = True
            if working and _button(draw_state, "stop", "Stop", x + Melty.px(190), y,
                                   Melty.px(85), chat_tint, not locked,
                                   height=Melty.px(30), shadow=False, ui_scale=Melty.ui_scale):
                proxy.stop_chat(selected)
                changed = True
            buttons_y = y
            y += action_extra_height
            from meltygui.view.dropdown_view import draw_dropdown
            permissions = {"Ask permission": "ask", "Full access": "full"}
            if not getattr(proxy, "inherits_defaults", False) and is_new_chat(chat) and meta.get("model") in (None, "", "default"):
                default_model = getattr(proxy, "default_model", None)
                if default_model:
                    meta["model"] = default_model
                    changed = True
            inherits_defaults = getattr(proxy, "inherits_defaults", False)
            defaults = proxy.defaults_for(chat["project"]) if inherits_defaults else {}
            models = chat_models(kind, proxy, meta.get("model", ""))
            if inherits_defaults:
                default_model = defaults.get("model") or getattr(proxy, "default_model", None)
                models = {"Default · " + (default_model or "Codex model"): "default", **models}
                access = ("Full access" if defaults.get("sandbox_mode") == "danger-full-access"
                          and defaults.get("approval_policy") == "never" else "Configured access")
                permissions = {"Default · " + access: "default", **permissions}
            new_chat = is_new_chat(chat)
            if new_chat:
                # New drafts may choose any available source, including ones
                # whose conversations are currently hidden in the sidebar.
                grouped_models = {}
                for source_id, source_kind in sources:
                    if not source_kind.chat_available:
                        continue
                    if source_id not in proxies:
                        proxies[source_id] = source_kind.chats(accounts[source_id], wake=wake)
                    source_proxy = proxies[source_id]
                    if source_proxy is None:
                        continue
                    options = chat_models(source_kind, source_proxy,
                        meta.get("model", "") if source_id == state.account else "")
                    if getattr(source_proxy, "inherits_defaults", False):
                        source_defaults = source_proxy.defaults_for(chat["project"])
                        default_model = source_defaults.get("model") or getattr(source_proxy, "default_model", None)
                        options = {"Default · " + (default_model or "Codex model"): "default", **options}
                    grouped_models[source_label(source_id, source_kind, accounts)] = {
                        label: (source_id, model) for label, model in options.items()}
                model_choices = grouped_models
            else:
                model_choices = models
            from meltygui.chat.codex_settings import effective_settings
            from meltygui.chat.codex_settings import fast_service_tier
            effective = effective_settings(chat, defaults, getattr(proxy, "default_model", None)) if kind.name == "codex" else {}
            fast_tier = fast_service_tier(proxy, effective.get("model")) if kind.name == "codex" else None
            fast_width = Melty.px(80) if fast_tier else 0
            has_effort = kind.name in ("codex", "anthropic")
            effort_width = Melty.px(150) + fast_width if has_effort and not project_editable else 0
            controls_start = 0 if working else Melty.px(95)
            control_x = x + controls_start
            control_count = 3 if project_editable else 2
            control_slot = max(Melty.px(12), (width - controls_start - effort_width) / control_count - Melty.px(6))
            for index, (field, choices, default) in enumerate((("permissions", permissions, "ask"), ("model", model_choices, ""))):
                current = meta.get(field, "default" if inherits_defaults else default)
                if inherits_defaults and (not current or field == "model" and not meta.get("model_explicit")):
                    current = "default"
                if field == "model" and kind.name == "codex" and chat.get("codex_settings"):
                    current = effective.get("model") or current
                labels = models if field == "model" else permissions
                label = next((label for label, value in labels.items() if value == current),
                             current if current and current != "default" else "Loading models…")
                slot = min(_label_width(label) + Melty.px(46), Melty.px(240),
                           control_slot)
                imgui.set_cursor_screen_pos((control_x, y))
                edited, value = draw_dropdown(label, collection=choices, display_label=label,
                    name=field + ":" + draft_key, width=slot - Melty.px(4), height=Melty.px(30), trigger_height=Melty.px(30),
                    show_header=False, shadow=False, tint=chat_tint,
                    text_pad=8)
                control_x += slot + Melty.px(6)
                if edited:
                    if field == "model" and new_chat:
                        source_id, model = value
                        changed |= switch_new_chat_source(state, proxies, kinds, selected, source_id, model)
                    else:
                        meta[field] = value
                        meta[field + "_selected_at"] = time.time()
                        if field == "model":
                            meta["model_explicit"] = True
                        changed = True
            if project_editable:
                current_chat = proxies[state.account][state.selected[state.account]]
                project = current_chat["project"]
                label = "\uf07b  " + (Path(project).name or project or "Working directory")
                imgui.set_cursor_screen_pos((control_x, y))
                project_choices = chat_project_choices(state, proxies, project, new_project, default_project)
                edited, value = draw_dropdown(project,
                    collection={"\uf07b  " + label: value for label, value in project_choices.items()},
                    row_tints={path: project_tint(path, file_metadata=file_metadata) for path in project_choices},
                    display_label=label, name="project:" + draft_key,
                    width=max(1, x + width - control_x), height=Melty.px(30), trigger_height=Melty.px(30),
                    show_header=False, shadow=False, tint=tuple(project_tint(project, file_metadata=file_metadata) or kind.tint), text_pad=8)
                if edited:
                    changed |= switch_new_chat_project(state, proxies, value)
            if has_effort:
                if project_editable:
                    control_x = x + Melty.px(95)
                model = meta.get("model")
                if model in (None, "", "default") or inherits_defaults and not meta.get("model_explicit"):
                    model = defaults.get("model") or getattr(proxy, "default_model", None)
                if kind.name == "codex":
                    model = effective.get("model") or model
                if fast_tier:
                    tier = effective.get("service_tier", getattr(proxy, "model_default_service_tiers", {}).get(model))
                    fast = tier == fast_tier or tier in ("fast", "priority") and fast_tier in ("fast", "priority")
                    if _button(draw_state, "fast:" + draft_key, "✓ Fast" if fast else "Fast",
                               control_x, y + effort_extra_height, fast_width - Melty.px(6), chat_tint,
                               selected=fast, height=Melty.px(30), shadow=False, ui_scale=Melty.ui_scale):
                        meta["service_tier"] = None if fast else fast_tier
                        meta["service_tier_selected_at"] = time.time()
                        changed = True
                    control_x += fast_width
                levels = chat_effort_levels(kind, proxy, model)
                effort = effective.get("effort") if kind.name == "codex" else meta.get("effort")
                if effort in (None, "", "default") or effort not in levels:
                    if kind.name == "codex":
                        effort = defaults.get("model_reasoning_effort") or getattr(proxy, "model_default_efforts", {}).get(model)
                    else:
                        effort = proxy.default_effort_for(chat["project"], model)
                    if effort and effort not in levels and levels:
                        order = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
                        eligible = [level for level in levels if level in order and effort in order
                                    and order.index(level) <= order.index(effort)]
                        effort = eligible[-1] if eligible else levels[0]
                edited, effort = draw_effort_slider(draw_state, "effort:" + draft_key,
                    effort, levels, control_x, y + effort_extra_height,
                    max(Melty.px(80), x + width - control_x), Melty.px(30), chat_tint)
                if edited:
                    meta["effort"] = effort
                    meta["effort_selected_at"] = time.time()
                    changed = True
            imgui.set_cursor_screen_pos((x, buttons_y))
            imgui.dummy(1, Melty.px(30) + controls_extra_height)
    columns.finish()
    if proxy is not None:
        proxy.reconcile()
    if changed:
        state.revision += 1
        # A click's effect (a row expanded, a filter picked) lays out on the
        # NEXT frame; ask for it now instead of waiting for the next input.
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
    return changed, input_value
    if changed:
        state.revision += 1
    return changed, input_value


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


def _visible(y, height, clip):
    return clip is None or y + height > clip[1] and y < clip[3]


def selection_slice(selection, index, length):
    """The (lo, hi) character span of prose leaf `index` inside a transcript
    selection ({anchor: (leaf, offset), head: (leaf, offset)}), None when the
    leaf is outside it or the selection is empty."""
    (first, first_offset), (last, last_offset) = sorted([tuple(selection["anchor"]), tuple(selection["head"])])
    if (first, first_offset) == (last, last_offset) or not first <= index <= last:
        return None
    lo = first_offset if index == first else 0
    hi = last_offset if index == last else length
    return (lo, hi) if hi > lo else None


def selection_text(selection, texts):
    """The selected prose, leaves joined by a blank line."""
    parts = []
    for index, text in enumerate(texts):
        span = selection_slice(selection, index, len(text))
        if span is not None:
            parts.append(str(text)[span[0]:span[1]])
    return "\n\n".join(parts)


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


def _message_icon(message):
    """The glyph that leads a row: pencil for writes, terminal for bash, brain for thinking."""
    bash_icon = f""
    write_icon = f""  # pencil-alt: the shipped face is FontAwesome 5, no f040 pencil
    thinking_icon = f""
    if isinstance(message, ReasoningMessage):
        return thinking_icon
    if not isinstance(message, ToolCall):
        return ""
    files = message.get("summary", {})
    if any(entry.get("access", "write") == "write" for entry in files.values()):
        return write_icon
    if isinstance(message["content"].get("command"), BashString):
        return bash_icon
    return ""


def _message_label(message, expanded):
    label = message.label
    if isinstance(message, ReasoningMessage):
        return "" if expanded else _message_preview(message)
    if isinstance(message, ToolCall):
        icon = _message_icon(message)
        if icon:
            label = icon
            # Read-only file references still leave a summary dict behind; only a
            # WRITE row (tags shown) drops the first-line preview.
            writes = any(entry.get("access", "write") == "write" for entry in message.get("summary", {}).values())
            preview = "" if expanded or writes else _message_preview(message)
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


def _hold_scroll_anchor(view, rows, height, restore, geometry=None):
    """Pin the top visible row to its screen position across relayouts.

    Each frame records (row key, pixels of that row above the viewport top);
    a frame whose heights moved (`restore`) re-derives the offset from it, so
    the reader's row stays put while rows above it re-wrap. Follow mode wins.
    """
    if geometry is None:
        geometry = _row_geometry(rows)
    tops, ends = geometry
    total = ends[-1] if ends else 0.0
    anchor = view.get("anchor")
    if restore and anchor is not None and not view.get("follow") and anchor[0] in tops:
        view["offset"] = max(0.0, min(tops[anchor[0]] + anchor[1], max(0.0, total - height)))
    offset = view.get("offset", 0.0)
    index = bisect_right(ends, offset)
    view["anchor"] = ((rows[index][0], offset - tops[rows[index][0]])
                      if index < len(rows) else None)


def _row_geometry(rows):
    """Index the transcript once per layout, for scrolling and visible drawing."""
    tops, ends, y = {}, [], 0.0
    for row in rows:
        tops[row[0]] = y
        y += row[3]
        ends.append(y)
    return tops, ends


# Claude Code's tint: the tile picker's row and the melty-claude window.
CLAUDE_CHAT_TINT = (0.35, 0.38, 0.44)
# Sidebar tints and ordering, kept between runs and shared by every app that
# draws this chat (melty-claude's original location).
CLAUDE_CHAT_METADATA = Path.home() / ".cache" / "melty-claude" / "chat_metadata.json"


def draw_claude_chat(input_value=None, new_project=None, default_project=None, **kwargs):
    """The Claude Code chat, ready for a window body or a tile: the chat
    interface over the detached chat service (conversations continue in every
    app that draws it), with folder tints from the shared file-meta store and
    the sidebar metadata kept in CLAUDE_CHAT_METADATA.

    A plain function, not a render boundary: `draw_chat_interface` is the one
    @render_func, and the caller's kwargs (a tile's ``width`` / ``height`` /
    ``key`` / ``layout_frame``...) go straight to it. An app offers it in its
    tile picker through its host's ``multi_instance_renderers``.

    A new conversation runs in ``new_project`` when given (an app started for
    one project), else in the selected conversation's project, else
    ``default_project``, else the input's project (`chat_project`: a path, or
    whatever an application registered for its model - a tile split from a code
    editor inherits its open files), else the cwd."""
    from meltygui.chat.metadata import persistent_metadata
    from meltygui.model.chat_model import chat_project
    from meltygui.models.file_meta import file_meta_store

    persistent_metadata(CLAUDE_CHAT_METADATA)   # before the first proxy is created
    project = default_project or chat_project(input_value) or str(Path.cwd())
    draw_chat_interface(name="claude-code", new_project=new_project, default_project=project,
                        show_bg=False, file_metadata=file_meta_store(), **kwargs)
    return False, input_value


# The tile picker's row (renderer_decoration reads a plain callable's too).
draw_claude_chat.__header_defaults__ = {"tint": CLAUDE_CHAT_TINT, "icon": f"\uf075", "display_name": "Claude Code"}
