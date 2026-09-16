"""Account view functions and supporting definitions."""
from meltygui.core.services.account_core import _cleanup_accounts
from meltygui.core.melty import Melty
from meltygui.model.account_model import AccountStore
from meltygui.core.core_render import render_func
from meltygui.state.account_state import AccountsPanelState
import meltygui_imgui as imgui


@render_func(use_cache=True, selectable=False, show_add_delete=False,
             on_cleanup=_cleanup_accounts,
             is_tree=False, show_name=True, shadow=True,
             is_default_for="AccountStore", tint=(0.888, 0.669, 0.443))
def draw_internet_accounts(
        # [tint=(0.85, 0.75, 0.05)]
        input_value: AccountStore,
        draw_state, panel_state: AccountsPanelState = None, style_manager=None,
        non_blocking_left_mouse_down=False, **kwargs):
    from meltygui.accounts.internet_accounts import ACCOUNTS_PATH
    from meltygui.accounts.internet_accounts import AnthropicKind
    from meltygui.accounts.internet_accounts import Button
    from meltygui.accounts.internet_accounts import CodexKind
    from meltygui.accounts.internet_accounts import KINDS
    from meltygui.accounts.internet_accounts import _color_u32
    from meltygui.accounts.internet_accounts import _ellipsize
    from meltygui.accounts.internet_accounts import _format_gb
    from meltygui.accounts.internet_accounts import _mix
    from meltygui.accounts.internet_accounts import _refresh_stale
    from meltygui.accounts.internet_accounts import _wrap_usage_label
    from meltygui.accounts.internet_accounts import is_default
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.cache.tile_cache import add_shadow
    import meltygui.accounts.internet_accounts

    meltygui.accounts.internet_accounts._window_draw_state = draw_state
    store = input_value
    if not store.loaded:
        store.load()
    store.ensure_kinds()  # adopt newly registered kinds after a hotswap
    if panel_state is not None:
        if "usage" not in panel_state.__dict__:
            panel_state.usage = {}      # a legacy instance from before the field existed (hotswap)
        # A fresh account dict (boot / restart / reload) has no runtime
        # panel states yet - take the persisted ones, and last session's
        # usage numbers for the rows that can show them.
        for account_entry in store.values():
            for key in AccountsPanelState.PANEL_KEYS:
                if key not in account_entry:
                    account_entry[key] = bool(panel_state.open.get(f"{account_entry['id']}{key}"))
            kind = KINDS.get(account_entry.get("kind"))
            if isinstance(kind, (AnthropicKind, CodexKind)):
                kind.restore_usage(account_entry, panel_state.usage.get(account_entry["id"]))

    # ---- styling (fast_dock recipe) ----
    row_bg_value, row_text_value = 0.06, 0.95
    factor, saturation = 0.90, 1.0
    button_bg_value, button_text_value = 0.13, 1.25
    primary_bg_value = 0.22
    hover_bg_boost, hover_text_boost = 0.05, 0.5
    text_saturation = 0.8

    # Status lamp colours (and the status text that inherits them), by
    # probe state — add a state here when a kind's probe grows one.
    # [tint=(0.35, 0.9, 0.45)]
    state_tints = {
        "ready": (0.35, 0.85, 0.45),
        "busy": (0.85, 0.75, 0.35),
        "needs_login": (0.95, 0.65, 0.25),
        "warning": (0.95, 0.7, 0.3),
        "error": (0.95, 0.35, 0.35),
        "unknown": (0.55, 0.58, 0.65),
    }
    # Usage-bar fill by the endpoint's severity (Claude subscription rows)
    # — the lamp palette, so "warning" reads the same everywhere.
    # [tint=(0.95, 0.7, 0.3)]
    severity_tints = {
        "normal": state_tints["ready"],
        "warning": state_tints["warning"],
        "critical": state_tints["error"],
        "exceeded": state_tints["error"],
    }

    # ---- row geometry, authored at ui_scale 1.0 and scaled once per frame ----
    px = Melty.px
    # [tint=(0.939, 0.453, 0.245)]
    row_height = px(34.0)
    row_gap = px(6.0)
    pad_x = px(10.0)
    corner = px(6.0)
    button_height = px(24.0)
    button_pad_x = px(10.0)
    button_gap = px(6.0)
    sub_row_height = px(30.0)         # secondary rows (field editors, device code card, model rows)
    strip_line_height = button_height + px(6)   # one wrapped line of buttons (row and sub-row height)
    stamp_row_height = px(18.0)       # the "as of 21:42:10" line under the usage bars
    kind_header_height = px(26.0)
    # Below this much text room the buttons wrap onto their own line inside
    # the row (the row grows) — raise it and narrow windows wrap sooner.
    # [tint=(0.994, 0.872, 0.0)]
    min_text_width = px(150.0)
    # [tint=(0.35, 0.85, 0.94)]
    text_inset = px(30.0)             # label x inset (after the status lamp)
    text_nudge_y = px(-1.0)

    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    origin_x, origin_y = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width or (draw_state.width or 300)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    press = non_blocking_left_mouse_down
    # [tint=(0.62, 0.47, 0.95)]
    click = (press.x, press.y) if (press and hasattr(press, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)
    line_height = imgui.get_text_line_height()
    row_left, row_right = origin_x + pad_x, origin_x + content_width - pad_x
    # True whenever a button handler or a field edit mutated the store this
    # frame - it is the view's `changed` return (style guide rule 10).
    pressed = [False]

    def visible(top, bottom):
        return clip is None or not (bottom < clip[1] or top > clip[3])

    def button_width(button):
        if button.icon is not None and button.label is None:
            return button_height
        return imgui.calc_text_size(button.label)[0] + 2 * button_pad_x

    def buttons_width(buttons):
        return (sum(button_width(button) for button in buttons)
                + button_gap * max(0, len(buttons) - 1))

    def pack_buttons(buttons, max_width):
        """The strip as right-aligned LINES, each no wider than max_width —
        a narrow window gets two or three lines of buttons instead of a
        strip running off the row's left edge. (A single button wider than
        the row still takes a line of its own.)"""
        lines, line, width = [], [], 0.0
        for button in buttons:
            extra = button_width(button) + (button_gap if line else 0.0)
            if line and width + extra > max_width:
                lines.append(line)
                line, width = [button], button_width(button)
            else:
                line.append(button)
                width += extra
        if line:
            lines.append(line)
        return lines

    def strip_layout(buttons, strip_max, text_avail, min_text):
        """(lines, wrap) for a row whose buttons share the line with text:
        wrap when the strip needs more than one line or would leave the
        text less than `min_text`."""
        lines = pack_buttons(buttons, strip_max)
        wrap = len(lines) > 1 or text_avail < min_text
        return lines, wrap

    def draw_buttons(buttons, right, top, tint, account, hint_slot):
        """Right-aligned button strip ending at `right`. Runs click handlers."""
        strip_right = right
        for button in reversed(buttons):
            width = button_width(button)
            left = strip_right - width
            bottom = top + button_height
            enabled = button.enabled and not (account or {}).get("_busy")
            hovered = (enabled and hover_ok
                       and left <= mouse_x <= strip_right and top <= mouse_y <= bottom)
            bg_value = ((primary_bg_value if button.primary else button_bg_value)
                        + (hover_bg_boost if hovered else 0.0))
            button_tint = (0.85, 0.35, 0.35) if button.danger else tint
            bg_color = _mix(style_manager, button_tint,
                            bg_value if enabled else button_bg_value * 0.5, factor, saturation)
            text_color = _mix(style_manager, button_tint,
                              (button_text_value + (hover_text_boost if hovered else 0.0))
                              if enabled else 0.45,
                              factor, text_saturation)
            if enabled and visible(top, bottom):
                add_shadow((left, top, width, button_height), offset=8 if button.primary else 4,
                           corner_radius=corner, clip=clip)
            draw_list.add_rect_filled(left, top, strip_right, bottom,
                                      _color_u32(bg_color), rounding=corner)
            if button.icon is not None and button.label is None:
                icon_size = imgui.calc_text_size(button.icon)
                draw_list.add_text(left + (width - icon_size[0]) / 2.0,
                                   top + (button_height - icon_size[1]) / 2.0 + text_nudge_y,
                                   _color_u32(text_color), button.icon)
            else:
                label_size = imgui.calc_text_size(button.label)
                draw_list.add_text(left + button_pad_x,
                                   top + (button_height - label_size[1]) / 2.0 + text_nudge_y,
                                   _color_u32(text_color), button.label)
            if hovered and button.tip:
                hint_slot[0] = button.tip
            if (enabled and click is not None
                    and left <= click[0] <= strip_right and top <= click[1] <= bottom):
                try:
                    button.on_click(account)
                except Exception as error:
                    if account is not None:
                        account["_status"] = ("error", str(error)[:90])
                pressed[0] = True
                request_render()
            strip_right = left - button_gap
        return strip_right + button_gap          # left edge of the strip

    # ---- layout pass ----
    # (kind, account, y, row height, buttons, wrap, subs) - heights are final
    # here so the scroll dummy and the hit-tests agree.
    y = origin_y
    # [tint=(0.989, 0.17, 0.497)]
    layout = []
    for kind in KINDS.values():
        layout.append(("head", kind, None, y, kind_header_height, None, False, []))
        y += kind_header_height + px(2)
        for account_entry in store.of_kind(kind.name):
            buttons = list(kind.actions(account_entry))
            if store.removable(account_entry):
                buttons.append(Button(None, lambda account: store.remove(account["id"]),
                                      icon=f"", danger=True,
                                      tip=("Remove account — the next row becomes the default"
                                           if is_default(account_entry) else "Remove account")))
            text_avail = ((row_right - px(6)) - (row_left + text_inset)
                          - buttons_width(buttons) - button_gap)
            lines, wrap = strip_layout(buttons, (row_right - px(6)) - (row_left + px(6)),
                                       text_avail, min_text_width)
            height = row_height + (len(lines) * strip_line_height if wrap else 0)
            subs = []
            if account_entry.get("_edit"):
                subs.extend(("field", field) for field in kind.fields if not field.hidden)
            subs.extend(kind.sub_rows(account_entry))
            # Sub-rows with a button strip (card, model) grow the same way:
            # (sub, height, button lines, wrap) - the painter reads them.
            sub_strip_max = (row_right - px(12)) - (row_left + text_inset + px(6))
            sub_items = []
            # Align bars to the longest full name in this account's usage rows.
            usage_label_width = max([px(150)] + [imgui.calc_text_size(sub[1]["label"])[0]
                                                 for sub in subs if sub[0] == "usage"])
            usage_available = row_right - row_left - text_inset - px(20)
            for sub in subs:
                sub_height, sub_lines, sub_wrap = sub_row_height, None, False
                if sub[0] == "stamp":
                    sub_height = stamp_row_height
                elif sub[0] == "usage":
                    percent_width = imgui.calc_text_size(f"{sub[1]['percent']:.0f}%")[0]
                    sub_wrap = usage_label_width + percent_width + px(70) > usage_available
                    sub_lines = (_wrap_usage_label(sub[1]["label"], usage_available)
                                 if sub_wrap else usage_label_width)
                    if sub_wrap:
                        sub_height += len(sub_lines) * line_height + px(4)
                elif sub[0] in ("card", "model"):
                    sub_buttons = (sub[1][1] if sub[0] == "card"
                                   else kind.model_actions(account_entry, sub[1]))
                    sub_lines, sub_wrap = strip_layout(
                        sub_buttons, sub_strip_max,
                        sub_strip_max - buttons_width(sub_buttons) - button_gap, min_text_width)
                    if sub_wrap:
                        sub_height += len(sub_lines) * strip_line_height
                sub_items.append((sub, sub_height, sub_lines, sub_wrap))
            layout.append(("account", kind, account_entry, y, height, lines, wrap, sub_items))
            # The account's card: its header line AND its sub-rows
            # (px(4) gap above them, px(4) pad below) - one block per account.
            y += height + sum(item[1] for item in sub_items) + (px(8) if sub_items else 0) + row_gap
        y += px(4)
    footer_y = y
    total_height = (footer_y - origin_y) + row_height
    top_inset = (origin_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(content_width, max(1.0, total_height + max(0.0, top_inset)))

    # [tint=(0.939, 0.836, 0.595)]
    hint = [None]

    for item in layout:
        what, kind, account_entry, row_top, height, lines, wrap, sub_items = item
        tint = kind.tint
        if what == "head":
            if visible(row_top, row_top + height):
                text_color = _mix(style_manager, tint, 1.0, factor, text_saturation)
                draw_list.add_text(row_left + px(2), row_top + (height - line_height) / 2.0,
                                   _color_u32(text_color, 0.85), f"{kind.icon}  {kind.label}")
                draw_buttons([Button(f" account",
                                     lambda _account, kind=kind: store.add(kind.name))],
                             row_right, row_top + (height - button_height) / 2.0, tint, None, hint)
            continue

        _refresh_stale(account_entry)
        row_bottom = row_top + height
        block_bottom = row_bottom + (sum(item[1] for item in sub_items) + px(8) if sub_items else 0)
        state, status_text = kind.status(account_entry)
        if account_entry.get("_busy") or account_entry.get("_probing"):
            state, status_text = "busy", (status_text if state != "unknown" else "…")
        # Hoisted above the visibility gate: the model sub-rows below read
        # these even when their parent row is scrolled offscreen.
        lamp_color = state_tints.get(state, state_tints["unknown"])
        text_color = _mix(style_manager, tint, row_text_value, factor, text_saturation)
        if visible(row_top, block_bottom):
            # the account's card: header line + its sub-rows, hover over all of it
            row_hovered = (hover_ok and row_left <= mouse_x <= row_right
                           and row_top <= mouse_y <= block_bottom)
            bg_color = _mix(style_manager, tint, row_bg_value + (0.02 if row_hovered else 0.0),
                            factor, saturation)
            draw_list.add_rect_filled(row_left, row_top, row_right, block_bottom,
                                      _color_u32(bg_color), rounding=corner)
        if visible(row_top, row_bottom):
            # status lamp, recessed
            lamp_radius = px(4.5)
            lamp_x, lamp_y = row_left + px(14), row_top + row_height / 2.0
            add_shadow((lamp_x - lamp_radius, lamp_y - lamp_radius, 2 * lamp_radius, 2 * lamp_radius),
                       offset=-1, corner_radius=lamp_radius, clip=clip)
            draw_list.add_circle_filled(lamp_x, lamp_y, lamp_radius, _color_u32(lamp_color), 16)
            # buttons: on the row line, or wrapped onto their own line(s)
            if wrap:
                strip_left = row_right - px(6)
                for index, line in enumerate(lines):
                    draw_buttons(line, row_right - px(6),
                                 row_top + row_height + px(2) + index * strip_line_height,
                                 tint, account_entry, hint)
            else:
                strip_left = draw_buttons(lines[0], row_right - px(6),
                                          row_top + (row_height - button_height) / 2.0,
                                          tint, account_entry, hint)
            # status text (the account's email / host / user), fitted to the
            # space left of the strip - the kind header above names the service.
            text_x = row_left + text_inset
            text_right = strip_left - button_gap - px(4)
            text_y = row_top + (row_height - line_height) / 2.0 + text_nudge_y
            status_fit = _ellipsize(status_text, text_right - text_x)
            if status_fit:
                draw_list.add_text(text_x, text_y, _color_u32(lamp_color, 0.9), status_fit)

        # ---- sub rows ----
        sub_top = row_bottom + px(4)
        for sub, sub_height, sub_lines, sub_wrap in sub_items:
            sub_bottom = sub_top + sub_height
            sub_left, sub_right = row_left + text_inset, row_right - px(6)

            def draw_sub_strip(sub_top=sub_top, sub_lines=sub_lines, sub_wrap=sub_wrap):
                """The sub-row's buttons: beside its text, or on wrapped
                lines under it. Returns the text's right limit."""
                if sub_wrap:
                    for index, line in enumerate(sub_lines):
                        draw_buttons(line, sub_right - px(6),
                                     sub_top + sub_row_height - px(2) + index * strip_line_height,
                                     tint, account_entry, hint)
                    return sub_right - px(10)
                return draw_buttons(sub_lines[0], sub_right - px(6),
                                    sub_top + (sub_row_height - button_height) / 2.0,
                                    tint, account_entry, hint) - button_gap

            if sub[0] == "field":
                field = sub[1]
                if visible(sub_top, sub_bottom):
                    label_color = _mix(style_manager, tint, 0.7, factor, text_saturation)
                    # The label column shrinks with the row so the editor keeps a usable width.
                    label_width = min(px(110), max(px(50), (sub_right - sub_left) * 0.35))
                    draw_list.add_text(sub_left,
                                       sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y,
                                       _color_u32(label_color, 0.85),
                                       _ellipsize(field.label, label_width - px(6)))
                    field_left = sub_left + label_width
                    field_width = max(px(60), sub_right - field_left)
                    if _draw_field(account_entry, field, field_left,
                                   sub_top + (sub_row_height - px(26)) / 2.0, field_width, px(26), store=store):
                        pressed[0] = True
            elif sub[0] == "card":
                # A highlighted strip with its own buttons: the kind supplies
                # (message, [buttons...]) - Copilot's device code, the Anthropic
                # sign-in waiting for the browser.
                message, card_buttons = sub[1]
                if visible(sub_top, sub_bottom):
                    add_shadow((sub_left, sub_top, sub_right - sub_left, sub_height),
                               offset=11, corner_radius=corner, clip=clip)
                    card_bg_color = _mix(style_manager, tint, 0.16, factor, saturation)
                    draw_list.add_rect_filled(sub_left, sub_top, sub_right, sub_bottom,
                                              _color_u32(card_bg_color), rounding=corner)
                    text_right = draw_sub_strip()
                    message_fit = _ellipsize(message, text_right - (sub_left + px(10)))
                    draw_list.add_text(sub_left + px(10),
                                       sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y,
                                       _color_u32((1.0, 0.95, 0.85)), message_fit)
            elif sub[0] == "usage":
                # One rate-limit window: label · bar (fill = severity tint,
                # width = percent) · percent · reset countdown / spend detail.
                # Label may sit beside the bar, or wrap above it in a narrow
                # row. Layout uses the same label height used by this painter.
                row = sub[1]
                if visible(sub_top, sub_bottom):
                    from meltygui.completion.providers.claude_usage import reset_text
                    text_y = sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y
                    fill_color = severity_tints.get(row["severity"], state_tints["unknown"])
                    label_color = (text_color if row["active"]
                                   else _mix(style_manager, tint, 0.75, factor, text_saturation))
                    label_x = sub_left + px(6)
                    available = (sub_right - px(8)) - label_x
                    bar_height = px(10)
                    percent_label = f"{row['percent']:.0f}%"
                    percent_width = imgui.calc_text_size(percent_label)[0]
                    right_text = row["detail"] or reset_text(row["resets_at"])
                    right_width = imgui.calc_text_size(right_text)[0] if right_text else 0.0
                    # [tint=(0.75, 0.55, 0.9)]
                    label_width = 0 if sub_wrap else sub_lines
                    label_gap = 0 if sub_wrap else px(10)
                    label_height = len(sub_lines) * line_height + px(4) if sub_wrap else 0
                    if sub_wrap:
                        for index, label_line in enumerate(sub_lines):
                            draw_list.add_text(label_x, sub_top + index * line_height + px(2),
                                               _color_u32(label_color), label_line)
                        text_y += label_height
                    else:
                        draw_list.add_text(label_x, text_y, _color_u32(label_color), row["label"])
                    rest = available - label_width - label_gap - percent_width - px(8)
                    if right_text and rest < right_width + px(12):
                        right_text, right_width = "", 0.0
                    bar_left = label_x + label_width + label_gap
                    bar_right = (sub_right - px(8) - right_width - (px(12) if right_text else 0.0)
                                 - percent_width - px(8))
                    bar_top = sub_top + label_height + (sub_row_height - bar_height) / 2.0
                    if bar_right - bar_left >= px(40):
                        track_color = _mix(style_manager, tint, 0.02, factor, saturation)
                        add_shadow((bar_left, bar_top, bar_right - bar_left, bar_height),
                                   offset=-1, corner_radius=bar_height / 2.0, clip=clip)
                        draw_list.add_rect_filled(bar_left, bar_top, bar_right, bar_top + bar_height,
                                                  _color_u32(track_color), rounding=bar_height / 2.0)
                        fill_right = bar_left + (bar_right - bar_left) * min(100.0, row["percent"]) / 100.0
                        if fill_right > bar_left + px(2):
                            draw_list.add_rect_filled(bar_left, bar_top, fill_right, bar_top + bar_height,
                                                      _color_u32(fill_color, 0.95), rounding=bar_height / 2.0)
                        draw_list.add_text(bar_right + px(8), text_y, _color_u32(fill_color), percent_label)
                    else:
                        draw_list.add_text(bar_left, text_y, _color_u32(fill_color), percent_label)
                    if right_text:
                        draw_list.add_text(sub_right - px(8) - right_width, text_y,
                                           _color_u32((0.72, 0.75, 0.82), 0.85), right_text)
            elif sub[0] == "stamp":
                if visible(sub_top, sub_bottom):
                    stamp_fit = _ellipsize(sub[1], (sub_right - px(8)) - (sub_left + px(6)))
                    stamp_width = imgui.calc_text_size(stamp_fit)[0]
                    draw_list.add_text(sub_right - px(8) - stamp_width,
                                       sub_top + (stamp_row_height - line_height) / 2.0 + text_nudge_y,
                                       _color_u32((0.6, 0.62, 0.7), 0.7), stamp_fit)
            elif sub[0] == "note":
                if visible(sub_top, sub_bottom):
                    draw_list.add_text(sub_left + px(6),
                                       sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y,
                                       _color_u32((0.7, 0.72, 0.8), 0.8),
                                       _ellipsize(sub[1], (sub_right - px(6)) - (sub_left + px(6))))
            elif sub[0] == "model":
                model = sub[1]
                if visible(sub_top, sub_bottom):
                    model_bg_color = _mix(style_manager, tint, 0.09 if model["loaded"] else 0.04,
                                          factor, saturation)
                    add_shadow((sub_left, sub_top, sub_right - sub_left, sub_height),
                               offset=2 if model["loaded"] else 1, corner_radius=corner, clip=clip)
                    draw_list.add_rect_filled(sub_left, sub_top, sub_right, sub_bottom,
                                              _color_u32(model_bg_color), rounding=corner)
                    strip_left = draw_sub_strip() + button_gap
                    model_lamp = state_tints["ready"] if model["loaded"] else state_tints["unknown"]
                    draw_list.add_circle_filled(sub_left + px(12), sub_top + sub_row_height / 2.0,
                                                px(3.5), _color_u32(model_lamp), 12)
                    name_x = sub_left + px(24)
                    text_y = sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y
                    name_fit = _ellipsize(model["name"],
                                          min(px(260), strip_left - button_gap - name_x))
                    draw_list.add_text(name_x, text_y, _color_u32(text_color), name_fit)
                    info_x = name_x + imgui.calc_text_size(name_fit)[0] + px(10)
                    info = _format_gb(model["size"])
                    if model["loaded"]:
                        info += f" · loaded on {model['where'] or '?'}"
                    info_fit = _ellipsize(info, strip_left - button_gap - px(4) - info_x)
                    if info_fit:
                        draw_list.add_text(info_x, text_y,
                                           _color_u32((0.72, 0.75, 0.82), 0.85), info_fit)
            sub_top = sub_bottom

    # ---- footer: file hints / notes / errors ----
    if visible(footer_y, footer_y + row_height):
        note = hint[0] or f"{ACCOUNTS_PATH}"
        if store.error:
            note += f"   ·   {store.error}"
        draw_list.add_text(row_left, footer_y + (row_height - line_height) / 2.0,
                           _color_u32((0.6, 0.62, 0.7), 0.6),
                           _ellipsize(note, row_right - row_left))

    # ---- persist the panel toggles ----
    # The account dicts' runtime flags are this frame's truth (the kinds'
    # buttons toggle them); mirror them into panel_state, which persists.
    if panel_state is not None:
        open_panels = {f"{account_entry['id']}{key}": True
                       for account_entry in store.values()
                       for key in AccountsPanelState.PANEL_KEYS if account_entry.get(key)}
        if open_panels != panel_state.open:
            panel_state.open = open_panels
        # ... and the usage numbers, so next session starts from them.
        usage_cache = {}
        for account_entry in store.values():
            kind = KINDS.get(account_entry.get("kind"))
            if isinstance(kind, (AnthropicKind, CodexKind)):
                entry = kind.usage_cache_entry(account_entry)
                if entry is not None:
                    usage_cache[account_entry["id"]] = entry
        if usage_cache != panel_state.usage:
            panel_state.usage = usage_cache

    return pressed[0], input_value


def _draw_field(input_value: dict, field, left, top, width, height, *, store: AccountStore):
    """An editable field: a single-line draw_text row (the editor, so focus,
    selection and paste all work). Returns True when the edit changed the
    stored value."""
    from meltygui.view.text_view import draw_text
    key = f"acct_{input_value['id']}_{field.name}"
    value = input_value.get(field.name) or ""
    imgui.set_cursor_screen_pos((left, top))
    changed, new_value = draw_text(value, name=key, single_line=True, width=width, height=height,
                                   show_widgets=False, show_root_backgrounds=False,
                                   show_header=False, show_file_header=False, show_jump_bar=False,
                                   shadow=False, use_cache=True, temp=True, autocomplete=False,
                                   syntax_highlight=False, line_numbers=False, fim="")
    if changed and isinstance(new_value, str) and new_value != value:
        store.set_field(input_value["id"], field.name, new_value.strip())
        return True
    return False
