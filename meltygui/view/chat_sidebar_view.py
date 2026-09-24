"""The chat sidebar: a folder tree of conversations by project directory (the
"All chats" pane, with the folders added by hand) and a flat recent feed,
sharing one set of row controls (select, rename, archive, tint, new-in-folder)."""
import time
from pathlib import Path

import meltygui_imgui as imgui
import meltygui.core.windowing.window_api as window_api
from meltygui.chat.chat_interface import TRASH_ICON, _label_width, _text_layout, _tint_slot, _title, _viewport
from meltygui.chat.chat_interface import conversation_source_tag
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.styling.fonts import Font
from meltygui.model.chat_folder_model import (
    empty_recent_chat, fold_column, folder_default, folder_layout, folder_runs,
    folder_settings, recent_runs, save_folder_settings)
from meltygui.model.chat_model import (
    conversation_folder_tint, is_active, project_tint, recent_chat_time, sidebar_visible, turn_status)
from meltygui.model.file_metadata_model import set_row_tint
from meltygui.view.chat_decoration_view import (
    _button, _card, _caret, _color, _hovering, _running_dot, _text_tint, _tint_style)
from meltygui.view.chat_source_icon_view import draw_source_icon
from meltygui.view.chat_view import _visible
from meltygui.view.text_view import draw_text


def drawing_channel():
    """The draw-list channel decorations paint on while Melty splits channels."""
    return Melty.get_channel() if Melty.channels_split else None


def folder_label_color(tint):
    return _tint_style(tuple(tint)).make_color_style_value(input={
        'value': 7.788, 'saturation': 1.559, 'max_value': 1.601})[:3]


def show_all_control(pane, draw_state, state, x, y, width, height, overlay_state=None):
    if pane != 'all':
        return False, 0
    label = 'Show all'
    control_width = _label_width(label) + Melty.px(28)
    tint = Toggles.Chat.navigation_tint
    left = x + width - control_width
    if overlay_state is None:
        changed = _button(draw_state, 'show-all-folders', '', left, y, control_width,
                          tint, height=height, background=False, shadow=False, ui_scale=Melty.ui_scale)
    else:
        from meltygui.view.header_view import flat_button
        changed = flat_button('', draw_state, 'show-all-folders', pos=(left, y),
                              width=control_width, height=height, color=tint,
                              alpha=0, shadow=False, layout=False, paint=False)
    settings = folder_settings()
    if changed:
        settings['show_all_folders'] = not settings['show_all_folders']
        state.revision += 1
        save_folder_settings()
    if overlay_state is not None:
        overlay_state.show_all = (y - draw_state.abs_top,
                                  draw_state.abs_left + draw_state.width - x - width,
                                  control_width, height, settings['show_all_folders'])
        return changed, control_width
    draw_list = imgui.get_window_draw_list()
    box_left, box_top = left + Melty.px(4), y + (height - Melty.px(10)) / 2
    color = _color(tuple(c * 0.55 for c in _text_tint(tuple(tint))))
    draw_list.add_rect(box_left, box_top, box_left + Melty.px(10), box_top + Melty.px(10),
                       color, rounding=Melty.px(2))
    if settings['show_all_folders']:
        draw_list.add_rect_filled(box_left + Melty.px(2), box_top + Melty.px(2),
                                 box_left + Melty.px(8), box_top + Melty.px(8), color)
    _title(label, left + Melty.px(20), y, control_width - Melty.px(20), height,
           tint, brightness=0.55)
    return changed, control_width


def draw_size_label(size, right, y, height, tint):
    label = f'{size / 1_000_000:.1f} MB' if size is not None else '— MB'
    font = Melty.font_mgr.get(Font.JETBRAINS_MONO_15) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        width = imgui.calc_text_size(label).x + Melty.px(8)
        left = right - width
        color = tuple(c * 0.65 * float(Toggles.Melty.arrow_brightness)
                      for c in folder_label_color(tint))
        draw_list = imgui.get_window_draw_list()
        if Melty.channels_split:
            draw_list.channels_set_current(Melty.get_channel())
        draw_list.add_text(left, y + max(0, (height - imgui.get_text_line_height()) / 2),
                           _color(color), label)
        return left
    finally:
        if font is not None:
            imgui.pop_font()


def draw_chat_sidebar(sources, draw_state, state, width, height, cutoff=None, new_conversation=None, pane="all",
                        file_metadata=None, scrollbar_overlays=None, project_filter=None):
    """Render the shared recursive chat tree with the shared chat row interactions."""
    from meltygui.chat.chat_proxy import ChatProxy
    if isinstance(sources, ChatProxy):
        sources = [(state.account, sources, None, "")]
    changed = False
    action = state.hovered_folder_action
    column_x, column_y = imgui.get_cursor_screen_pos()
    if pane != 'recent' and action is not None and _hovering(column_x, column_y, width, height, pointer=imgui.get_mouse_pos()):
        fold_column(state, pane, action)
        state.hovered_folder_action = None
        changed = True
    archive = None
    trash_icon = TRASH_ICON
    trash_width = Melty.px(25)
    row_width = max(Melty.px(80), width - Melty.px(7))
    gap, pad = Melty.px(2), Melty.px(3)
    memos = state.viewports.setdefault("sidebar-cards", {})
    # The cards lay out the same until a source's conversations, the window's
    # state (a pick, a fold, a rename) or the filter minute changes. Titles
    # stay on one line; width only changes clipping in the paint pass below.
    # Keep the tree and card heights during divider drags as well as scrolling.
    signature = (project_filter, tuple((account_id, getattr(chats, "revision", None), len(chats) if chats is not None else 0)
                       for account_id, chats, _, _ in sources),
                 folder_default(state, pane), tuple(state.folder_expanded.items()), state.revision, int(cutoff // 60) if cutoff else None, Melty.ui_scale,
                 tuple(state.sidebar_chat_limits.items()), new_conversation is not None, tuple(sorted(state.selected.items())), state.account, folder_settings()['show_all_folders'], tuple(folder_settings()['added_folders']), int(time.time() // 5))
    memo = memos.get(pane)
    if memo is not None and len(memo) == 6 and memo[0] == signature:
        cards = memo[1]
    else:
        entries = []
        for account_id, chats, kind, _label in sources:
            if chats is None:
                continue
            selected_key = state.selected.get(account_id)
            source_tint = tuple(kind.tint) if kind is not None else (0.6, 0.6, 0.6)
            for key, chat in chats.items():
                if project_filter and (not chat['project'] or not Path(chat['project']).is_relative_to(project_filter)):
                    continue
                # Terminal-title helper sessions stay in the backend, but do not
                # take up space in either conversation list.
                if ' '.join(str(chat['title']).casefold().split()).startswith('name a terminal window'):
                    continue
                if pane == 'recent' and empty_recent_chat(chat):
                    continue
                if (not any(part.startswith(".") and part not in (".", "..") for part in Path(chat["project"]).parts)
                        and sidebar_visible(key, chat, cutoff, selected_key)):
                    entries.append((-recent_chat_time(chat), account_id, chats, kind, key, chat, source_tint))
        entries.sort(key=lambda entry: entry[0])
        remaining = 0
        smart_collapse = state.pending_smart_collapse == pane
        if pane == 'all':
            runs = folder_runs(entries, state, pane=pane, collapse_small=smart_collapse, project_filter=project_filter)
        else:
            # Recent chats are flat and globally ordered by activity.
            limit = state.sidebar_chat_limits.get(pane, 8)
            remaining = max(0, len(entries) - limit)
            runs = recent_runs(entries[:limit])
        cards = []
        for index, (project, rows, depth) in enumerate(runs):
            fallback = next((s for s in sources if s[0] == state.account), sources[0] if sources else (None, None, None, ""))
            first_account, first_chats = (rows[0][1], rows[0][2]) if rows else fallback[:2]
            folder_key = ((pane, project) if pane == 'all' else
                          (pane, project, rows[-1][1], rows[-1][4]))
            if smart_collapse and pane != 'all':
                state.folder_expanded[folder_key] = len(rows) <= 3
            expanded = pane == 'recent' or state.folder_expanded.get(folder_key, folder_default(state, pane))
            # The folder's tint is its directory's in the file-meta store; an
            # unpainted folder (and its unpainted rows) wears the source's.
            painted = project_tint(project, file_metadata)
            heading_tint = painted or (rows[0][6] if rows else (0.6, 0.6, 0.6))
            if pane == 'recent':
                heading_tint = conversation_folder_tint(project, file_metadata)
            folder_label = (Path(project).name or project) if project else "No project"
            label, heading_height = _text_layout(state, "project:" + project, folder_label)
            heading_height += Melty.px(2)
            children = []
            if expanded:
                limit = state.sidebar_chat_limits.get(folder_key, 8) if pane == 'all' else len(rows)
                for _, account_id, chats, kind, key, chat, source_tint in rows[:limit]:
                    text, row_height = _text_layout(state, "chat:" + account_id + ":" + key, " ".join(str(chat["title"]).split()))
                    children.append((account_id, chats, kind, key, chat.metadata, text, row_height + Melty.px(2),
                                     tuple(heading_tint)))
                if len(rows) > limit:
                    more_label, more_height = _text_layout(
                        state, 'folder-more:' + project, f'Show more ({len(rows) - limit} remaining)')
                    children.append((None, None, None, None, None, more_label,
                                     more_height + Melty.px(2), tuple(heading_tint)))
            card_height = pad * 2 + heading_height + sum(row[6] + gap for row in children)
            cards.append((index, project, folder_key, label, heading_height, rows, children, card_height,
                          painted, heading_tint, first_account, first_chats, depth))
        if smart_collapse:
            state.pending_smart_collapse = None
        if pane == 'recent':
            layout, total = [], 0
            for card in cards:
                row_height = card[6][0][6]
                layout.append((total, row_height, total))
                total += row_height + gap
            total = max(0, total - gap)
        else:
            layout, total = folder_layout(cards, pad, gap)
        more_text, more_height = ('', 0)
        if remaining:
            more_text, more_height = _text_layout(state, 'more:' + pane,
                                                  f'Show more ({remaining} remaining)')
            more_height += pad * 2
        memos[pane] = (signature, cards, layout, total, more_text, more_height)
    layout, content_height, more_text, more_height = memos[pane][2:]
    total = content_height + more_height
    with _viewport(draw_state, state, "sidebar:" + pane + ":" + ":".join(source[0] for source in sources),
                   width, height, total, scrollbar_overlays=scrollbar_overlays,
                   stretch_scrollbar=pane == 'recent') as (base_x, base_y, clip):
        for (index, project, folder_key, label, heading_height, rows, children, card_height,
             painted, heading_tint, first_account, first_chats, depth) in cards:
            top, card_height, chat_top = layout[index]
            y = base_y + top
            indent = min(Melty.px(12) * depth, max(0, width - Melty.px(160)))
            x = base_x + indent
            row_width = max(Melty.px(80), width - Melty.px(7) - indent)
            run_id = f"{pane}:{index}:{project}"
            if pane != 'recent' and (painted or depth > 0) and _visible(y, card_height, clip):
                plate_tint = heading_tint if painted else tuple(
                    Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
                _card(x, y, row_width, card_height, plate_tint, shadow=False, ui_scale=Melty.ui_scale, channel=drawing_channel())
            heading_y = y + pad
            if pane != 'recent' and _visible(heading_y, heading_height, clip):
                expanded = pane == 'recent' or state.folder_expanded.get(folder_key, folder_default(state, pane))
                arrow_top = max(heading_y, clip[1]) if clip else heading_y
                arrow_bottom = min(heading_y + heading_height, clip[3]) if clip else heading_y + heading_height
                arrow_tint = heading_tint if painted else tuple(Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
                plus_x = x + row_width - pad - trash_width
                picker_x = plus_x - Melty.px(20)
                caret_x = picker_x - Melty.px(20)
                label_left = x + pad
                label_right = picker_x if pane == 'recent' else caret_x
                if pane != 'recent' and _caret(draw_state, "project-arrow:" + run_id, caret_x, arrow_top,
                          Melty.px(17), max(0, arrow_bottom - arrow_top), expanded,
                          arrow_tint, brightness=0.55, ui_scale=Melty.ui_scale):
                    state.folder_expanded[folder_key] = not expanded
                    changed = True
                heading_hovered = _hovering(x, heading_y, row_width, heading_height, pointer=imgui.get_mouse_pos())
                picker_open = getattr(draw_state, '_tint_edit_key', None) == 'tint_project:' + run_id
                if file_metadata is not None and (heading_hovered or picker_open):
                    changed |= _tint_slot(draw_state, "project:" + run_id, painted,
                                          picker_x, heading_y + max(0, (heading_height - Melty.px(13)) / 2),
                                          heading_hovered, heading_tint,
                                          lambda value, _p=project: set_row_tint(file_metadata, _p, value),
                                          show_brush=heading_hovered, brush_tint=arrow_tint)
                # The heading's +: a new conversation in this folder, in the
                # source of the run's newest conversation.
                if _button(draw_state, "project-heading:" + run_id, "", label_left, arrow_top,
                           max(0, label_right - label_left), heading_tint,
                           height=max(0, arrow_bottom - arrow_top), background=False,
                           event="left_mouse_down", ui_scale=Melty.ui_scale):
                    state.folder_expanded[folder_key] = True
                    changed |= not expanded
                header_color = folder_label_color(arrow_tint)
                # _button places Melty's flat_button and restores the layout cursor.
                if new_conversation is not None and _button(
                        draw_state, "new-in:" + run_id, "+", plus_x, heading_y, trash_width, arrow_tint,
                        first_chats is not None and not first_chats.loading and not first_chats.error,
                        height=heading_height, background=False,
                        text_color=tuple(c * 0.65 * float(Toggles.Melty.arrow_brightness)
                                         for c in header_color), ui_scale=Melty.ui_scale):
                    new_conversation(first_account, first_chats, project)
                    changed = True
                _title(label, label_left, heading_y,
                       max(0, label_right - label_left), heading_height, arrow_tint,
                       brightness=0.65 * float(Toggles.Melty.arrow_brightness), ellipsis=True,
                       text_color=header_color)
                if not expanded and any(is_active(entry[5]) for entry in rows):
                    _running_dot(label_right - Melty.px(7), heading_y + heading_height / 2, heading_tint, ui_scale=Melty.ui_scale, channel=drawing_channel())
            child_y = base_y + chat_top
            # Conversation tags share the left edge of sibling folder cards.
            child_indent = min(Melty.px(12) * (depth + 1), max(0, width - Melty.px(160)))
            for account_id, chats, kind, key, child_meta, text, row_height, row_tint in children:
                if account_id is None:
                    if _visible(child_y, row_height, clip):
                        top = max(child_y, clip[1]) if clip else child_y
                        bottom = min(child_y + row_height, clip[3]) if clip else child_y + row_height
                        left = base_x + child_indent
                        more_width = max(0, x + row_width - pad - left)
                        if _button(draw_state, 'folder-more:' + run_id, '', left, top,
                                   more_width, row_tint, height=max(0, bottom - top),
                                   background=False, event='left_mouse_down', ui_scale=Melty.ui_scale):
                            state.sidebar_chat_limits[folder_key] = state.sidebar_chat_limits.get(folder_key, 8) + 8
                            changed = True
                        label_tint = heading_tint if painted else tuple(
                            Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
                        _title(text, left, child_y, more_width, row_height, label_tint, ellipsis=True,
                               brightness=0.65 * float(Toggles.Melty.arrow_brightness),
                               text_color=folder_label_color(label_tint))
                    child_y += row_height + gap
                    continue
                row_id = pane + ":" + account_id + ":" + key
                rect = (x + pad, child_y, x + row_width - trash_width, child_y + row_height)
                renaming = getattr(state, "rename", None)
                editing = (renaming is not None and renaming["account"] == account_id
                           and renaming["key"] == key and renaming.get("pane", pane) == pane)
                # Rows order by time: no drag reordering, and nothing registered
                # for a row scrolled out of the list.
                if not _visible(child_y, row_height, clip):
                    child_y += row_height + gap
                    continue
                if draw_state.on_action("right_mouse_down", view_id="chat-menu:" + row_id, priority_delta=3,
                                        rect=(x + pad, child_y, x + row_width - pad, child_y + row_height)):
                    state.chat_menu = {"account": account_id, "key": key, "pane": pane}
                    changed = True
                tint = row_tint
                selected = account_id == state.account and key == state.selected.get(account_id)
                active = is_active(chats.get(key))
                if pane == 'recent' or selected or active:
                    _card(x + pad, child_y, row_width - pad * 2, row_height, tint,
                          max_bg_value=0.36 if selected else 0.18 if active else 0.14, selected=selected,
                          shadow=selected or active,
                          shadow_offset=float(Toggles.Chat.selected_chat_shadow_offset), ui_scale=Melty.ui_scale, channel=drawing_channel())
                # A row cut by the list's edge takes the pointer only where it
                # shows: hit rects ignore the clip, and a full-height one would
                # reach over the filter chips above or the button below.
                hit_top = max(child_y, clip[1]) if clip else child_y
                hit_bottom = min(child_y + row_height, clip[3]) if clip else child_y + row_height
                # Select on press: CLICKED waits 250 ms for the row's rename
                # double-click. DOWN remains immediate and preserves renaming.
                if hit_bottom - hit_top >= 1 and _button(draw_state, "chat:" + row_id, "", x + pad, hit_top,
                           row_width - pad * 2 - trash_width, tint, height=hit_bottom - hit_top,
                           background=False, event="left_mouse_down", ui_scale=Melty.ui_scale):
                    state.selected[account_id] = key
                    state.account = account_id
                    if kind is not None:
                        state.provider = kind.name
                    changed = True
                row_rect = (x + pad, child_y, x + row_width - pad, child_y + row_height)
                hovered = draw_state.on_action("cursor_hover", view_id="chat-hover:" + row_id,
                                               rect=row_rect) is not None
                if not is_active(chats.get(key)) and hovered and hit_bottom - hit_top >= 1 and _button(
                        draw_state, "archive:" + row_id, trash_icon, x + row_width - pad - trash_width, hit_top,
                        trash_width, tint, height=hit_bottom - hit_top, background=False, ui_scale=Melty.ui_scale):
                    archive = (chats, account_id, key)
                if draw_state.on_action("left_mouse_double_clicked", view_id="rename:" + row_id,
                                        rect=rect, priority_delta=5) is not None:
                    state.rename = {"account": account_id, "key": key, "pane": pane, "draft": text, "focus": True}
                    renaming = state.rename
                    editing = True
                    state.selected[account_id] = key
                    state.account = account_id
                    changed = True
                title_left = x + pad + Melty.px(3)
                right = x + row_width - pad
                if hovered and not is_active(chats.get(key)):
                    right -= trash_width
                if kind is not None:
                    tag = conversation_source_tag(kind, chats.get(key))
                    locked = tag.endswith(' \uf023')
                    tag = tag.removesuffix(' \uf023')
                    tag_tint = (Toggles.Chat.claude_tag_tint if kind.name == "anthropic"
                                else Toggles.Chat.codex_tag_tint if kind.name == "codex" else tuple(kind.tint))
                    tag_text_width = Melty.px(16)
                    status_width = Melty.px(12) if locked or is_active(chats.get(key)) else 0
                    tag_width = tag_text_width + Melty.px(6)
                    tag_left = title_left
                    imgui.get_window_draw_list().add_rect_filled(tag_left, child_y, tag_left + tag_width,
                        child_y + row_height, _color(tag_tint, 0.12), rounding=Melty.px(4))
                    if not draw_source_icon(kind.name, tag_left + Melty.px(3),
                                            child_y + (row_height - tag_text_width) / 2,
                                            tag_text_width, tag_tint):
                        _title(tag, tag_left + Melty.px(3), child_y, tag_text_width,
                               row_height, tag_tint, text_color=tag_tint)
                    status_left = tag_left + tag_width + Melty.px(5)
                    if locked:
                        _title('\uf023', status_left, child_y, status_width, row_height,
                               tag_tint, text_color=tag_tint)
                    elif is_active(chats.get(key)):
                        _running_dot(status_left + status_width / 2, child_y + row_height / 2, tint, ui_scale=Melty.ui_scale, channel=drawing_channel())
                    title_left = status_left + status_width + (Melty.px(5) if status_width else 0)
                size = chats.get(key).get("size_bytes")
                label_tint = heading_tint if painted else tuple(
                    Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
                right = draw_size_label(size, right, child_y, row_height, label_tint)
                badge, _, status_tint = turn_status(chats.get(key))
                if badge and not editing:
                    badge_width = min(_label_width(badge) + Melty.px(12),
                                      max(0, (right - title_left) * 0.48))
                    _title(badge, right - badge_width, child_y, badge_width, row_height,
                           status_tint, text_color=status_tint, ellipsis=True)
                    right -= badge_width + Melty.px(5)
                title_width = max(0, right - title_left - Melty.px(5))
                if not editing:
                    _title(text, title_left, child_y, title_width, row_height, tint, ellipsis=True)
                else:
                    imgui.set_cursor_screen_pos((title_left, child_y))
                    request_focus = editing and renaming["focus"]
                    edited, value, text_ds = draw_text(renaming["draft"] if editing else text,
                        wrap=False, name="chat-row:" + row_id,
                        request_focus=request_focus, select_all_on_focus=request_focus, single_line=True,
                        return_extras=True,
                        width=title_width, height=row_height, is_tree=False,
                        editable=editing, focusable=editing, syntax_highlight=False, autocomplete=False,
                        show_header=False, show_bg=False, shadow=False, show_widgets=False,
                        show_file_header=False, show_jump_bar=False, scope_collapse=False,
                        disable_scroll=True, freeze_resize=True, use_cache=True, imgui_padding=False,
                        tint=tint, text_tint=_text_tint(tuple(tint)), fim="")
                if editing:
                    if Melty.text_focused_ds is text_ds:
                        renaming["focused"] = True
                        renaming["focus"] = False
                    if edited:
                        renaming["draft"] = value
                        changed = True
                    keys_pressed = {event[0] for event in Melty.frame_key_events}
                    if window_api.KEY_ESCAPE in keys_pressed:
                        state.rename = None
                        changed = True
                    elif (window_api.KEY_ENTER in keys_pressed or window_api.KEY_KP_ENTER in keys_pressed
                          or renaming.get("focused", False) and Melty.text_focused_ds is not text_ds):
                        if renaming["draft"].strip():
                            chats.get(key)["title"] = renaming["draft"].strip()
                        state.rename = None
                        changed = True
                child_y += row_height + gap
        more_y = base_y + content_height
        if more_height and _visible(more_y, more_height, clip):
            top = max(more_y, clip[1]) if clip else more_y
            bottom = min(more_y + more_height, clip[3]) if clip else more_y + more_height
            tint = (0.6, 0.6, 0.6)
            if _button(draw_state, 'show-more:' + pane, '', base_x + pad, top,
                       width - pad * 2, tint, height=max(0, bottom - top),
                       background=False, event='left_mouse_down', ui_scale=Melty.ui_scale):
                state.sidebar_chat_limits[pane] = state.sidebar_chat_limits.get(pane, 8) + 8
                changed = True
            label_tint = tuple(Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
            _title(more_text, base_x + pad, more_y + pad, width - pad * 2,
                   more_height - pad * 2, label_tint, ellipsis=True,
                   brightness=0.65 * float(Toggles.Melty.arrow_brightness),
                   text_color=folder_label_color(label_tint))
    if archive is not None:
        chats, account_id, key = archive
        del chats[key]
        if state.selected.get(account_id) == key:
            state.selected[account_id] = next(iter(chats), None)
        if getattr(state, "rename", None) is not None and state.rename["key"] == key:
            state.rename = None
        changed = True
    return changed, min(total, height)  # the height the list actually uses
