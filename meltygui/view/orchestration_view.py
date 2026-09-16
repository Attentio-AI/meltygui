"""Orchestration view functions and supporting definitions."""
from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.state.orchestration_state import OrchestratorPanelState
import meltygui_imgui as imgui
import time


@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True, tint=(0.719, 0.478, 0.208))
def draw_orchestrator(input_value=None, draw_state=None, style_manager=None,
                      panel_state: OrchestratorPanelState = None,
                      renaming_key="", rename_text="",
                      left_mouse_down=False, left_mouse_double_clicked=False, **kwargs):
    # NOTE the click param names: "double_left_mouse_down" canonicalises
    # to the SAME (input_id, DOWN) key as left_mouse_down (DOWN has no double
    # promotion) but the per-frame name cache keeps one name per key - it
    # silently drops every plain press. DOUBLE_CLICKED is its own key.
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.cache.tile_cache import add_shadow
    from meltygui.core.automation.orchestration_core import Orchestrator
    from meltygui.core.automation.orchestration_core import _format_value
    from meltygui.core.automation.orchestration_core import _wrap_text
    from meltygui.core.automation.orchestration_core import cue_gesture
    from meltygui.core.automation.orchestration_core import cue_get
    from meltygui.core.automation.orchestration_core import cue_has_target
    from meltygui.core.automation.orchestration_core import cue_press_frac
    from meltygui.core.automation.orchestration_core import failure_report
    from meltygui.core.automation.orchestration_core import generalized_commands
    from meltygui.core.automation.orchestration_core import group_events
    from meltygui.core.automation.orchestration_core import parse_argument
    import meltygui.core.automation.orchestration_core

    meltygui.core.automation.orchestration_core._window_draw_state = draw_state
    Orchestrator._precondition_watch = set()      # re-declared by the command rows below
    root = getattr(Melty.vis, "root", None)
    store = getattr(root, "orchestrations", None)
    if store is None:
        imgui.text("model not loaded")
        return False, input_value
    orchestrations = store.orchestrations

    # ---- icons (glyph literals - the editor renders them as a font) ----
    record_icon = f""
    stop_icon = f""
    play_icon = f""
    plus_icon = f""
    delete_icon = f""
    restore_icon = f""
    chevron_right_icon = f""
    chevron_down_icon = f""
    rename_icon = f""
    check_icon = f""                          # precondition rows: "target hittable"

    # ---- styling (fast_dock recipe) ----
    row_bg_value, row_text_value = 0.06, 0.95
    factor, saturation = 0.85, 1.0
    button_bg_value, button_text_value = 0.13, 1.25
    hover_bg_boost, hover_text_boost = 0.05, 0.5
    text_saturation = 0.8
    dim_text_value = 0.45
    record_tint = (0.85, 0.25, 0.25)              # armed recording button / pulse
    restore_on_tint = (0.4, 0.85, 0.5)            # restore chip when checked

    # ---- geometry, authored at ui_scale 1.0 and scaled once per frame ----
    px = Melty.px
    # [tint=(0.939, 0.453, 0.245)]
    row_height = px(28.0)
    row_gap = px(3.0)
    toolbar_height = px(26.0)
    toolbar_gap = px(8.0)
    button_pad_x = px(10.0)
    button_gap = px(6.0)
    chip_width = px(24.0)                          # per-row icon buttons (play/restore/delete)
    corner = px(6.0)
    pad_x = px(8.0)
    text_nudge_y = px(-1.0)
    status_height = px(20.0)

    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    origin_x, origin_y = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width or (draw_state.width or 300)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    press = left_mouse_down
    # [tint=(0.62, 0.47, 0.95)]
    click = (press.x, press.y) if (press and hasattr(press, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)
    changed = False

    # The engine trims the stop click off a take's tail with this rect.
    Orchestrator.window_rect = (draw_state.abs_left, draw_state.abs_top,
                                draw_state.abs_left + (draw_state.width or content_width),
                                draw_state.abs_top + (draw_state.height or 200))

    def _mix(tint, value, mix_factor, mix_saturation):
        color = tint if (isinstance(tint, tuple) and len(tint) >= 3) else (0.5, 0.5, 0.5)
        return style_manager.make_color_rgb(color[0], color[1], color[2], value=value,
                                            factor=mix_factor, saturation_scale=mix_saturation)

    def _u32(color, alpha=1.0):
        return pack_color(color[0], color[1], color[2], alpha)

    def _in(rect, x, y):
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def _button(x, y, width, height, label, tint, active=False, icon_only=False):
        """Draw a toolbar/chip button, return (clicked, right_edge)."""
        rect = (x, y, x + width, y + height)
        hovered = hover_ok and _in(rect, mouse_x, mouse_y)
        bg_value = button_bg_value + (0.08 if active else 0.0) \
            + (hover_bg_boost if hovered else 0.0)
        background = _mix(tint, bg_value, factor, saturation)
        text_color = _mix(tint, button_text_value + (hover_text_boost if hovered else 0.0),
                          factor, text_saturation)
        if active or hovered or not icon_only:
            add_shadow((x, y, width, height), corner_radius=corner, clip=clip)
            draw_list.add_rect_filled(rect[0], rect[1], rect[2], rect[3],
                                      _u32(background), rounding=corner)
        label_size = imgui.calc_text_size(label)
        draw_list.add_text(x + (width - label_size[0]) / 2.0,
                           y + (height - label_size[1]) / 2.0 + text_nudge_y,
                           _u32(text_color), label)
        clicked = click is not None and _in(rect, click[0], click[1])
        return clicked, rect[2]

    # ---- toolbar: Record/Stop - Play/Abort - + New ----
    toolbar_x = origin_x + pad_x
    toolbar_y = origin_y
    engine_busy = (Orchestrator.replaying is not None or bool(Orchestrator._restore_steps)
                   or Orchestrator._task is not None)
    is_recording = Orchestrator.recording is not None

    # Record New ALWAYS creates a fresh orchestration and records into it;
    # re-recording an existing row use that row's own record chip.
    record_label = f"{stop_icon}  Stop" if is_recording \
        else f"{record_icon}  Record New"
    record_width = imgui.calc_text_size(record_label)[0] + 2 * button_pad_x
    record_clicked, edge = _button(toolbar_x, toolbar_y, record_width, toolbar_height,
                                   record_label, record_tint if is_recording else draw_state.tint,
                                   active=is_recording)
    if record_clicked and not engine_busy:
        if is_recording:
            Orchestrator.stop_recording()
        else:
            from meltygui.models.orchestration import Orchestration
            target = Orchestration()
            target.name = f"Orchestration {len(orchestrations) + 1}"
            orchestrations[target.id] = target
            Orchestrator.start_recording(target)
        changed = True

    if engine_busy:
        abort_label = f"{stop_icon}  Abort"
        abort_width = imgui.calc_text_size(abort_label)[0] + 2 * button_pad_x
        abort_clicked, edge = _button(edge + button_gap, toolbar_y, abort_width,
                                      toolbar_height, abort_label, record_tint, active=True)
        if abort_clicked:
            Orchestrator.abort("stop button")

    # ---- rows (one per orchestration, each expands to its detail tabs) ----
    rows_top = toolbar_y + toolbar_height + toolbar_gap
    row_left = origin_x + pad_x
    row_right = origin_x + content_width - pad_x
    row_stride = row_height + row_gap
    row_y = rows_top
    detail_row_height = px(18.0)
    detail_indent = px(34.0)
    tab_height = px(20.0)
    tab_pad_x = px(8.0)
    precondition_indent = detail_indent + px(26.0)   # precondition rows sit under their command
    cue_tint = (0.85, 0.65, 0.2)                     # cue / effect rows (undo anchors)
    fail_tint = (0.92, 0.32, 0.3)                    # failed row / error bar
    success_tint = (0.32, 0.78, 0.42)                # success row wash / ok tag
    dim_color = _mix(draw_state.tint, dim_text_value, factor, text_saturation)
    fail_color = _mix(fail_tint, row_text_value + 0.2, 0.35, 1.2)
    success_color = _mix(success_tint, row_text_value + 0.1, 0.35, 1.2)
    expanded_map = panel_state.expanded if panel_state is not None else {}
    tab_map = panel_state.tab if panel_state is not None else {}
    dbl = (left_mouse_double_clicked.x, left_mouse_double_clicked.y) \
        if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None

    def _start_rename(key, orchestration):
        draw_state.renaming_key = key
        draw_state.rename_text = orchestration.name
        if panel_state is not None:
            panel_state._rename_focus = True
            panel_state._rename_was_active = False
        request_render()

    def _commit_rename(orchestration):
        text = (draw_state.rename_text or "").strip()
        if text and text != orchestration.name:
            orchestration.name = text
            orchestration.custom_name = True      # auto-name never overwrites this
            request_render()
            committed = True
        else:
            committed = False
        draw_state.renaming_key = ""
        return committed

    def _detail_rows(rows_list, playing, fail_at=None):
        """Both tabs render through here — same replay-progress highlight:
        the row being replayed brightens, finished rows dim, effect rows
        (cues / commands) wear the amber, the FAILED row (the cue that
        never verified / the event the replay stopped at) reads red."""
        nonlocal row_y
        for row in rows_list:
            start_index, end_index, row_kind, label, tag = row[:5]
            if clip is None or not (row_y + detail_row_height < clip[1] or row_y > clip[3]):
                running = playing and start_index <= Orchestrator._replay_index \
                    < max(end_index, start_index + 1)
                if row_kind == "cue":               # cue rows sit AT their index
                    failed_here = (fail_at is not None and not playing
                                   and start_index == fail_at)
                else:
                    failed_here = (fail_at is not None and not playing
                                   and start_index <= fail_at
                                   < max(end_index, start_index + 1))
                if failed_here:
                    label_color = fail_color
                elif running:
                    label_color = _mix(draw_state.tint, row_text_value + 0.4,
                                       factor, text_saturation)   # being replayed
                elif playing and max(end_index, start_index + 1) <= Orchestrator._replay_index:
                    label_color = dim_color                        # already replayed
                elif row_kind in ("cue", "expand", "window", "move", "goto", "set"):
                    label_color = _mix(cue_tint, row_text_value, 0.4, 1.2)
                else:
                    label_color = _mix(draw_state.tint, row_text_value * 0.75,
                                       factor, text_saturation)
                draw_list.add_text(row_left + detail_indent, row_y + text_nudge_y,
                                   _u32(label_color), label)
                if tag:
                    tag_width = imgui.calc_text_size(tag)[0]
                    draw_list.add_text(row_right - px(4) - tag_width,
                                       row_y + text_nudge_y, _u32(dim_color), tag)
            row_y += detail_row_height

    def _command_rows(key, orchestration, tint, rows_list, playing_raw, cmd_cursor,
                      fail_cue_index=None):
        """Commands tab: each leaf-edit command's ARGUMENT is an inline edit
        box — the recorded value pre-filled (defaults reproduce the take
        exactly), an edit stores an override (amber text, × resets) and
        routes play through change_value with the edited value. Highlight:
        raw replay by event span, command playback by cue ordinal."""
        nonlocal row_y, changed
        overrides = getattr(orchestration, "overrides", None)
        if overrides is None:
            overrides = orchestration.overrides = {}
        for ordinal, row in enumerate(rows_list):
            start_index, end_index, row_kind, label, tag = row[:5]
            arg = row[5] if len(row) > 5 else None
            row_visible = clip is None or not (row_y + detail_row_height < clip[1]
                                               or row_y > clip[3])
            if row_visible:
                if cmd_cursor is not None:
                    running = ordinal == cmd_cursor
                    finished = ordinal < cmd_cursor
                else:
                    running = playing_raw and start_index <= Orchestrator._replay_index \
                        < max(end_index, start_index + 1)
                    finished = playing_raw \
                        and max(end_index, start_index + 1) <= Orchestrator._replay_index
                if fail_cue_index is not None and ordinal == fail_cue_index \
                        and not running:
                    label_color = fail_color
                elif running:
                    label_color = _mix(draw_state.tint, row_text_value + 0.4,
                                       factor, text_saturation)
                elif finished:
                    label_color = dim_color
                elif arg is None:
                    label_color = _mix(cue_tint, row_text_value, 0.4, 1.2)
                else:
                    label_color = _mix(draw_state.tint, row_text_value * 0.75,
                                       factor, text_saturation)
                # per-action run chip (the indent gutter): replays just THIS
                # command's event span - overrides remap its gesture as in
                # a full play - its cue verifying at the main
                run_clicked, _ = _button(row_left + px(10), row_y, px(18.0),
                                         px(16.0), play_icon, tint, icon_only=True)
                if run_clicked and not is_recording and not engine_busy:
                    Orchestrator.play(orchestration, start=start_index,
                                      end=max(end_index, start_index), generalize=True)
                    changed = True
                draw_list.add_text(row_left + detail_indent, row_y + text_nudge_y,
                                   _u32(label_color), label)
                if arg is not None:
                    override_key = str(ordinal)
                    modified = override_key in overrides
                    value = overrides.get(override_key, arg)
                    value_text = value if isinstance(value, str) else _format_value(value)
                    input_width = px(150.0) if isinstance(arg, str) else px(72.0)
                    input_x = (row_left + detail_indent
                               + imgui.calc_text_size(label)[0] + px(6))
                    imgui.set_cursor_screen_pos((input_x, row_y - px(1)))
                    imgui.push_item_width(input_width)
                    if modified:
                        imgui.push_style_color(imgui.COLOR_TEXT, 1.0, 0.75, 0.3, 1.0)
                    entered, text = imgui.input_text(
                        f"##orch-arg-{key}-{ordinal}", value_text, 128)
                    imgui.set_item_allow_overlap()
                    if modified:
                        imgui.pop_style_color()
                    imgui.pop_item_width()
                    if entered:
                        ok, parsed = parse_argument(text, arg)
                        if ok:
                            if parsed == arg:
                                overrides.pop(override_key, None)   # back to default
                            else:
                                overrides[override_key] = parsed
                            changed = True
                            request_render()
                    close_x = input_x + input_width + px(4)
                    draw_list.add_text(close_x, row_y + text_nudge_y,
                                       _u32(label_color), ")")
                    if modified:
                        reset_clicked, _ = _button(close_x + px(14), row_y, px(18.0),
                                                   px(16.0), delete_icon, tint,
                                                   icon_only=True)
                        if reset_clicked:
                            overrides.pop(override_key, None)
                            changed = True
                            request_render()
                if tag and arg is None:
                    tag_width = imgui.calc_text_size(tag)[0]
                    draw_list.add_text(row_right - px(4) - tag_width,
                                       row_y + text_nudge_y, _u32(dim_color), tag)
            row_y += detail_row_height
            if not cue_has_target(orchestration.cues[ordinal]):
                continue
            # ---- preconditions for this command (any cue with a target): one indented row per
            # unmet precondition (outermost first - the order the solver
            # takes them), the cheapest fix as its tag, a run chip that
            # satisfies preconditions up to and including THIS one ----
            Orchestrator._precondition_watch.add((key, ordinal))
            preconditions = Orchestrator._preconditions.get((key, ordinal))
            if preconditions is None:
                continue                                  # not listed yet (next sync)
            if not preconditions:
                if row_visible:
                    draw_list.add_text(row_left + precondition_indent, row_y + text_nudge_y,
                                       _u32(dim_color), f"{check_icon} target hittable")
                row_y += detail_row_height
                continue
            for precondition in preconditions:
                if clip is None or not (row_y + detail_row_height < clip[1] or row_y > clip[3]):
                    runnable = precondition["kind"] not in ("unresolved", "error", "hittable")
                    run_pre = False
                    if runnable:
                        run_pre, _ = _button(row_left + precondition_indent - px(22), row_y,
                                             px(18.0), px(16.0), play_icon, tint, icon_only=True)
                    if run_pre and not is_recording and not engine_busy:
                        from meltygui.core.automation.value_core import precondition_task
                        cue = orchestration.cues[ordinal]
                        path = tuple(cue_get(cue, "chain") or [cue_get(cue, "name") or "?"])
                        Orchestrator.submit(precondition_task(
                            path, precondition["key"], orchestration=orchestration,
                            gesture=cue_gesture(cue), press_frac=cue_press_frac(cue)))
                        changed = True
                    pre_color = (fail_color if precondition["kind"] in ("unresolved", "error")
                                 else dim_color if precondition["kind"] == "hittable"
                                 else _mix(cue_tint, row_text_value, 0.4, 1.2))
                    label_text = precondition["label"]
                    if precondition["kind"] == "hittable":
                        label_text = f"{check_icon} {label_text}"
                    draw_list.add_text(row_left + precondition_indent, row_y + text_nudge_y,
                                       _u32(pre_color), label_text)
                    fixes = precondition.get("fixes") or []
                    if fixes:
                        fix_tag = f"fix: {fixes[0][1]}" + (f" (+{len(fixes) - 1})" if len(fixes) > 1 else "")
                        fix_width = imgui.calc_text_size(fix_tag)[0]
                        draw_list.add_text(row_right - px(4) - fix_width,
                                           row_y + text_nudge_y, _u32(dim_color), fix_tag)
                row_y += detail_row_height

    for key, orchestration in list(orchestrations.items()):
        row_rect = (row_left, row_y, row_right, row_y + row_height)
        header_visible = clip is None or not (row_rect[3] < clip[1]
                                              or row_rect[1] > clip[3])
        tint = orchestration.tint if isinstance(orchestration.tint, tuple) else draw_state.tint
        is_playing = Orchestrator.replaying is orchestration
        take_failure = (Orchestrator.last_failure
                        if (Orchestrator.last_failure is not None
                            and Orchestrator.last_failure.orchestration is orchestration)
                        else None)
        take_success = (Orchestrator.last_success
                        if (Orchestrator.last_success is not None
                            and Orchestrator.last_success.orchestration is orchestration
                            and take_failure is None)
                        else None)
        is_take = Orchestrator.recording is orchestration
        is_expanded = bool(expanded_map.get(key))
        renaming = renaming_key == key
        row_hovered = hover_ok and _in(row_rect, mouse_x, mouse_y)
        chip_y = row_y + (row_height - px(22)) / 2.0
        deleted = False

        if header_visible:
            bg_value = row_bg_value + (hover_bg_boost if row_hovered else 0.0)
            # every row floats on a soft drop shadow; a succeeded take's
            # row washes green (failure red wins - checked above)
            add_shadow((row_rect[0], row_rect[1], row_right - row_left, row_height),
                       corner_radius=corner, clip=clip)
            bg_tint = success_tint if take_success is not None else tint
            draw_list.add_rect_filled(row_rect[0], row_rect[1], row_rect[2], row_rect[3],
                                      _u32(_mix(bg_tint, bg_value + (0.03 if take_success else 0.0),
                                                factor, saturation)),
                                      rounding=corner)

            # chevron (expand/collapse) + play chips
            chevron_clicked, chip_edge = _button(
                row_left + px(4), chip_y, chip_width, px(22),
                chevron_down_icon if is_expanded else chevron_right_icon,
                tint, icon_only=True)
            if chevron_clicked:
                if is_expanded:
                    expanded_map.pop(key, None)
                else:
                    expanded_map[key] = True
                is_expanded = not is_expanded
                request_render()
            # play = every command through change_value to its override or
            # its original value (a verbatim replay had no button worth
            # keeping — Lukas 09-01; `play(generalize=False)` still exists
            # for the tests and tooling)
            play_clicked, chip_edge = _button(chip_edge + px(2), chip_y, chip_width, px(22),
                                              play_icon, tint, icon_only=True)
            if play_clicked and not is_recording and not engine_busy:
                Orchestrator.play(orchestration, generalize=True)
                changed = True
            # per-row record chip: re-record INTO this orchestration (events
            # and cues replaced; the auto-name refreshes too, unless the
            # name was set by hand). Shows stop while THIS take records.
            record_chip_clicked, chip_edge = _button(
                chip_edge + px(2), chip_y, chip_width, px(22),
                stop_icon if is_take else record_icon,
                record_tint if is_take else tint,
                active=is_take, icon_only=True)
            if record_chip_clicked:
                if is_take:
                    Orchestrator.stop_recording()
                    changed = True
                elif not is_recording and not engine_busy:
                    Orchestrator.start_recording(orchestration)
                    changed = True

            # right side, outermost in: delete · restore · rename · tag
            delete_left = row_right - px(4) - chip_width
            if row_hovered and not is_take and not is_playing and not renaming:
                delete_clicked, _ = _button(delete_left, chip_y, chip_width, px(22),
                                            delete_icon, tint, icon_only=True)
                if delete_clicked:
                    orchestrations.pop(key, None)
                    expanded_map.pop(key, None)
                    tab_map.pop(key, None)
                    changed = True
                    deleted = True
            restore_left = delete_left - button_gap - chip_width
            restore_tint = restore_on_tint if orchestration.restore_on_finish else tint
            restore_clicked, _ = _button(restore_left, chip_y, chip_width, px(22),
                                         restore_icon, restore_tint,
                                         active=orchestration.restore_on_finish,
                                         icon_only=True)
            if restore_clicked:
                orchestration.restore_on_finish = not orchestration.restore_on_finish
                changed = True
            rename_left = restore_left - button_gap - chip_width
            if row_hovered and not renaming:
                rename_clicked, _ = _button(rename_left, chip_y, chip_width, px(22),
                                            rename_icon, tint, icon_only=True)
                if rename_clicked:
                    _start_rename(key, orchestration)
                    renaming = True

            if is_playing:
                tag = f"{Orchestrator._replay_index}/{len(orchestration.events)}"
            else:
                override_count = len(getattr(orchestration, "overrides", None) or {})
                tag = f"{orchestration.duration:g}s · {len(orchestration.events)} ev" \
                    + (f" · {override_count} edited" if override_count else "")
                if take_failure is not None:
                    tag += " · failed"
                elif take_success is not None:
                    tag += " · ok"
            tag_width = imgui.calc_text_size(tag)[0]
            tag_left = rename_left - button_gap - tag_width
            if is_playing:
                tag_color = _mix(tint, dim_text_value, factor, text_saturation)
            elif take_failure is not None:
                tag_color = fail_color
            elif take_success is not None:
                tag_color = success_color
            else:
                tag_color = _mix(tint, dim_text_value, factor, text_saturation)
            draw_list.add_text(tag_left,
                               row_y + (row_height - imgui.get_text_line_height()) / 2.0
                               + text_nudge_y, _u32(tag_color), tag)

            # name: inline rename input, or the label (double-click to rename)
            name_left = chip_edge + px(8)
            if deleted:
                pass
            elif renaming:
                imgui.set_cursor_screen_pos((name_left, row_y + px(3)))
                imgui.push_item_width(max(px(90), min(px(240),
                                                      tag_left - name_left - px(10))))
                if panel_state is not None and getattr(panel_state, "_rename_focus", False):
                    imgui.set_keyboard_focus_here()
                    panel_state._rename_focus = False
                entered, text = imgui.input_text(f"##orch-rename-{key}",
                                                 draw_state.rename_text or "", 128,
                                                 imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
                imgui.pop_item_width()
                draw_state.rename_text = text
                item_active = imgui.is_item_active()
                was_active = (getattr(panel_state, "_rename_was_active", False)
                              if panel_state is not None else True)
                if entered or (was_active and not item_active):
                    changed = _commit_rename(orchestration) or changed
                elif panel_state is not None:
                    panel_state._rename_was_active = item_active
            else:
                text_value = row_text_value + (hover_text_boost if row_hovered else 0.0)
                name_color = _mix((record_tint if is_take else tint), text_value,
                                  factor, text_saturation)
                label = orchestration.name + ("  (recording)" if is_take else "")
                draw_list.add_text(name_left,
                                   row_y + (row_height - imgui.get_text_line_height()) / 2.0
                                   + text_nudge_y, _u32(name_color), label)
                if dbl is not None and _in((name_left, row_y, tag_left, row_y + row_height),
                                           dbl[0], dbl[1]):
                    _start_rename(key, orchestration)

        row_y += row_stride
        if deleted:
            continue

        # ---- expanded detail: Commands (generalized) | Events (raw) ----
        if is_expanded:
            current_tab = tab_map.get(key, "commands")
            tab_x = row_left + detail_indent
            for tab_key, tab_label in (("commands", "Commands"), ("events", "Events")):
                width = imgui.calc_text_size(tab_label)[0] + 2 * tab_pad_x
                tab_clicked, tab_edge = _button(tab_x, row_y, width, tab_height,
                                                tab_label, tint,
                                                active=current_tab == tab_key)
                if tab_clicked and current_tab != tab_key:
                    tab_map[key] = tab_key
                    current_tab = tab_key
                    request_render()
                tab_x = tab_edge + button_gap
            row_y += tab_height + px(4)
            fail_at = None
            if take_failure is not None:
                if (take_failure.cue_index is not None
                        and take_failure.cue_index < len(orchestration.cues)):
                    fail_at = cue_get(orchestration.cues[take_failure.cue_index],
                                      "at", 0)
                else:
                    fail_at = take_failure.event_index
            if current_tab == "events":
                detail = group_events(orchestration.events, orchestration.cues)
                if not detail:
                    detail = [(0, 0, "", "no events yet — press Record", "")]
                _detail_rows(detail, is_playing, fail_at=fail_at)
            else:
                detail = generalized_commands(orchestration)
                if detail:
                    _command_rows(key, orchestration, tint, detail, is_playing, None,
                                  fail_cue_index=(take_failure.cue_index
                                                  if take_failure is not None else None))
                else:
                    _detail_rows([(0, 0, "", "no commands yet — effects appear "
                                             "as recording touches the undo stacks", "")],
                                 False)
            row_y += px(4)

    if not orchestrations:
        empty_color = _mix(draw_state.tint, dim_text_value, factor, text_saturation)
        draw_list.add_text(row_left + px(4), rows_top + px(4), _u32(empty_color),
                           "No orchestrations — press Record to capture one.")
        row_y += row_stride

    # ---- status footer ----
    if Orchestrator.status:
        status_color = _mix(record_tint if is_recording else draw_state.tint,
                            row_text_value, factor, text_saturation)
        draw_list.add_text(row_left + px(4), row_y + px(2), _u32(status_color),
                           Orchestrator.status)
        row_y += status_height

    # ---- error bar: the full reason for the most recent failure ----
    failure = Orchestrator.last_failure
    if failure is not None:
        bar_top = row_y + px(4)
        bar_line_height = imgui.get_text_line_height()
        bar_pad_y = px(5.0)
        dismiss_reserve = px(4) + px(18.0) + button_gap
        message_wrap_width = max(px(60), (row_right - row_left) - px(16) - dismiss_reserve)
        failed_name = str(getattr(failure.orchestration, "name", None) or "task")
        where = ""
        if failure.cue_index is not None:
            where = f" (command {failure.cue_index + 1})"
        elif failure.event_index is not None:
            where = f" (event {failure.event_index})"
        message = f"{failed_name}{where}: {failure.reason}"
        message_lines = _wrap_text(message, message_wrap_width)
        bar_height = max(px(24.0), bar_pad_y * 2 + bar_line_height * len(message_lines))
        add_shadow((row_left, bar_top, row_right - row_left, bar_height),
                   corner_radius=corner, clip=clip)
        draw_list.add_rect_filled(row_left, bar_top, row_right, bar_top + bar_height,
                                  _u32(_mix(fail_tint, 0.045, 0.55, 1.1)),
                                  rounding=corner)
        message_rect = (row_left, bar_top, row_right - dismiss_reserve, bar_top + bar_height)
        message_hovered = hover_ok and _in(message_rect, mouse_x, mouse_y)
        # click anywhere on the message copies it (whole line, not just the
        # visible part); "copied" shows in place for a beat
        just_copied = (getattr(failure, "copied_at", None) is not None
                       and time.time() - failure.copied_at < 1.2)
        if click is not None and _in(message_rect, click[0], click[1]):
            try:
                imgui.set_clipboard_text(failure_report(failure))
            except Exception:
                pass
            failure.copied_at = time.time()
            just_copied = True
            request_render()
        if message_hovered:
            draw_list.add_rect_filled(message_rect[0], message_rect[1], message_rect[2],
                                      message_rect[3], _u32(_mix(fail_tint, 0.09, 0.55, 1.1)),
                                      rounding=corner)
        # the whole reason, WRAPPED to the bar (a solver failure lists every
        # fix it tried - one line clipped the part that mattered)
        shown_lines = (["debug report copied to clipboard"] if just_copied else message_lines)
        for line_index, line in enumerate(shown_lines):
            draw_list.add_text(row_left + px(8),
                               bar_top + bar_pad_y + line_index * bar_line_height + text_nudge_y,
                               _u32(success_color if just_copied else fail_color), line)
        if just_copied:
            request_render()                          # let the confirmation fade
        dismiss_clicked, _ = _button(row_right - px(4) - px(18.0),
                                     bar_top + (bar_height - px(16.0)) / 2.0,
                                     px(18.0), px(16.0), delete_icon, fail_tint,
                                     icon_only=True)
        if dismiss_clicked:
            Orchestrator.last_failure = None
            request_render()
        row_y = bar_top + bar_height + px(2)

    imgui.dummy(content_width, max(1.0, (row_y - origin_y) + row_gap))
    return changed, input_value
