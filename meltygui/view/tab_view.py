"""Tab view functions and supporting definitions."""
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.core.core_decoration import Core
from meltygui.state.new_core_model import TabState
from meltygui.view.header_view import draw_header
import meltygui_imgui as imgui


@render_func(is_tree=False, show_bg=True, shadow=False, use_cache=False, header_same_line=True,
             disable_scroll=True,
             indent_size=0, show_add_delete=False,
             show_name=False, selectable=False, parent_show_add_delete=False,
             with_header=draw_header)
def draw_tab_bar(input_value: list, tab_height=30, names=None, tint_value=0.235, tint_saturation=0.372, unique=None,
                 collection=None, as_toggles=False, tints=None, icons=None, excluded=None, width=None,
                 dnd_collection_ds=None, dnd_keys=None,
                 draw_state=None):
    """Tab bar with multi-select via shift-click. input_value is the list of selected items, collection is all available tabs.
    Tabs wrap onto a new row when the cumulative width would exceed draw_state.content_width.

    tints: optional list of (r, g, b) tint colors, one per tab in `collection`. Entries that are
    None (or beyond the list) fall back to the neutral grey. (Defaults to None rather than [] to
    avoid the mutable-default-arg pitfall; behaves identically to an empty list.)

    dnd_collection_ds + dnd_keys: opt tabs into the framework DragDrop. Each
    tab button registers as a whole-rect drag item of `dnd_collection_ds` (the
    draw_state whose input_value is the collection being reordered — the
    Reorder/Insert lands on ITS return via Melty.dnd_requests). dnd_keys is a
    list parallel to `collection`: (key, collection_index) per tab, or None
    for tabs that aren't draggable (e.g. a synthetic General tab). The owner
    must stamp _dnd_drop_target/_dnd_horizontal on dnd_collection_ds so slot
    lines render vertically between the tabs."""
    from meltygui.view.control_view import button
    from meltygui.view.header_view import flat_button
    from meltygui.core.cursor_core import same_line
    import meltygui.core.drag_drop_core as _drag_drop

    if collection is None:
        return False, input_value

    if excluded is None:
        excluded = set()
    # Content-left edge, captured before the dummy/same_line/-10 shift below.
    # The wrap limit is measured from here so it lines up with content_width.
    origin_x = imgui.get_cursor_screen_pos()[0]

    imgui.dummy(0, 0)
    imgui.same_line()

    # [tint=(0.894, 0.568, 0.204, 1.0), show_tint=True]
    io = imgui.get_io()
    changed = False
    selected = list(input_value)
    if selected is None:
        selected = []
    if names is None and hasattr(input_value, 'keys') and hasattr(input_value, 'values'):
        input_value = list(input_value.values())
        names = list(input_value.keys())

    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] - 10, imgui.get_cursor_screen_pos()[1]))
    # First-row start x after the -10 shift; wrapped rows realign to this
    # (new_line() alone resets to the window content x, which is ~10px right).
    row_start_x = imgui.get_cursor_screen_pos()[0]

    # Mirror button()'s sizing: width = calc_text_size(label_text).x + text_pad (15).
    # Scaled like button() scales its text_pad, so the wrap measurement below
    # matches the width the buttons actually take.
    # [tint=(0.124, 0.65, 0.087, 1.0), show_tint=True]
    button_padding = Melty.px(15)
    # tab_height arrives as an authored-at-1.0 constant (callers pass 30/40),
    # so it scales here - once, up front, so the button, the click test and
    # the drag placeholder below all agree on one height. The drag path
    # deliberately overrides this with the MEASURED pickup height, which is
    # already in real pixels.
    tab_height = Melty.px(tab_height)
    content_width = draw_state.content_width if draw_state is not None else 0
    x_limit = origin_x + content_width if content_width > 0 else None

    # An explicitly passed width is a real constraint content_width knows
    # nothing about (the context menu passes width=content_width-282) — clamp
    # to it. draw_state.width and abs_clip_rect are NOT constraints here: with
    # wrap=True the wrapper writes the measured content extent back into
    # draw_state.width (and the clip rect derives from it), so clamping to
    # them ratchets the bar narrower on every reflow until each tab fits on
    # its own row.
    if width and draw_state is not None:
        view_right = draw_state._abs_left() + width - 3
        x_limit = view_right if x_limit is None else min(x_limit, view_right)

    def _tab_text(t):
        return t.name if hasattr(t, 'name') else str(t)

    for i, tab in enumerate(collection):
        raw = _tab_text(tab)
        # label_text = raw.replace("_", " ")

        if i in excluded:
            continue
        label = f"{raw}"
        if names is not None and i < len(names):
            label = f"{names[i]}"
        # Icons parallels `collection` like tints; prefix before the## so the
        # imgui id (and click identity) stays keyed on the bare tab name.
        if icons is not None and i < len(icons) and icons[i]:
            label = f"{icons[i]} {label}"
        active = tab in selected

        tab_color = (0.5, 0.5, 0.5)
        tinted = tints is not None and i < len(tints) and tints[i] is not None
        if tinted:
            tab_color = tints[i]

        new_value = 0.15 if not tinted else 0.1

        # make_color_rgb mixes `color` toward the theme color by `factor`; factor=1.0 (button's
        # default) discards `color` entirely. Drop factor for tinted tabs so the tint shows, and
        # give inactive tinted tabs a faint fill (the default alpha=0.0 draws no rect at all).
        tab_factor = 0.30 if tinted else 1.2

        tab_width = imgui.calc_text_size(label.split("##")[0]).x + button_padding

        # Wrap before drawing: the previous iteration's same_line() left the
        # cursor at this tab's real start (with item spacing included), so
        # comparing live cursor + width against the content right edge needs
        # no estimated spacing or fudge factor.
        if i > 0 and x_limit is not None and imgui.get_cursor_screen_pos()[0] + tab_width > x_limit:
            imgui.new_line()
            imgui.set_cursor_screen_pos((row_start_x, imgui.get_cursor_screen_pos()[1]))

        # Framework drag-and-drop: this button acts as a whole-rect drag
        # item of dnd_collection_ds (key= is what DragDrop._begin picks up).
        # While IT is the dragged child it renders with the floating-window
        # kwargs (detached, fixed to pickup size, glued to the cursor) and a
        # plain dummy holds its slot open in the flow.
        dnd = None
        dragged = False
        dnd_extra = {}
        if dnd_collection_ds is not None and dnd_keys is not None and i < len(dnd_keys):
            dnd = dnd_keys[i]
        _tx, _ty = imgui.get_cursor_screen_pos()
        btn_height = tab_height
        if dnd is not None:
            dnd_extra = {"key": dnd[0], "dnd_handle": True, "return_extras": True}
            # Same (collection, key) can be carried by two views - this button
            # and the tab's stacked content view (draw_collection_as_tabs
            # multi-select). Only the view actually picked up detaches, so
            # require the dragged item to BE this button's draw state.
            dragged = (_drag_drop.DragDrop.is_dragged_child(dnd_collection_ds, dnd[0])
                       and _drag_drop.DragDrop.item_ds is dnd_collection_ds._children.get(dnd[1]))
            if dragged:
                dnd_extra.update(_drag_drop.DragDrop.dragged_item_kwargs())
                # dragged_item_kwargs pins width/height to the pickup size -
                # height would collide with the explicit height= below, so
                # route it through btn_height (the pinned button size).
                btn_height = dnd_extra.pop("height", btn_height)

        if dnd is not None:
            # dnd: tabs - legacy @render_func buttons: DragDrop needs a
            # per-tab draw_state to register as the drag child (pickup,
            # floating window, slot placeholder). Only the context menu's
            # reorderable tab bar takes this branch.
            if active:
                selected_value = 0.23
                btn_res = button(label, z_offset=2, name=f"tab_{i}_{unique}",
                                 height=btn_height, tint_value=new_value + selected_value - 0.03,
                                 color=tab_color, factor=tab_factor, draw=True, **dnd_extra)
            else:
                saturation = 1.0 if tinted else 0.3
                btn_res = button(label, indent_size=0, height=btn_height, draw=True, z_offset=0.0,
                                 alpha=0.0 if tinted else 0.0, tint_value=new_value if not tinted else 0.1,
                                 saturation=saturation,
                                 name=f"tab_{i}_{unique}_deactivated", color=tab_color, factor=tab_factor,
                                 text_value=1.0 if not tinted else 0.9,
                                 shadow=False, **dnd_extra)
            # A draggable tab must not change the selection on mouse-DOWN (a
            # drag pickup would eat a multi-select). Override the button's
            # down-click and select on CLICKED instead: the input handler only
            # passes it for a release within MOVE_MAX_DISTANCE of the press,
            # so a press that becomes a drag never selects.
            clicked = False
            if not dragged and draw_state is not None:
                clicked = draw_state.on_action(
                    "left_mouse_clicked", view_id=f"tab_click_{i}",
                    rect=(_tx, _ty, _tx + tab_width, _ty + tab_height),
                    priority_delta=2) is not None
            btn_ds = btn_res[2] if len(btn_res) == 3 else None
            if btn_ds is not None:
                # Fulfill the drop-collection contract on the OWNER's ds (see
                # DragDrop._is_drop_collection): children keyed by collection
                # index, backlinked for register_item/_begin.
                btn_ds._collection_draw_state = dnd_collection_ds
                if dnd_collection_ds._children is None:
                    dnd_collection_ds._children = {}
                dnd_collection_ds._children[dnd[1]] = btn_ds
            if dragged:
                # The dragged button deferred to a floating window and drew
                # nothing inline - hold its slot open at the current flow
                # position so the bar doesn't reflow mid-drag.
                imgui.dummy(tab_width, tab_height)
                clicked = False
        else:
            # Plain tabs: draw-list rendering (flat_button - the fast-dock
            # model). The per-tab @render_func buttons cost ~0.7ms each in
            # wrapper machinery alone; on the editor's 8-tab strip that was
            # the largest single slice of the hovered frame. Visual parity
            # with the old button params: active tabs get the filled rect,
            # inactive draw label-only (alpha=0), hover brightens the text
            # like the button's hovered text_value boost. Framework
            # shadows/z-offset on the active tab are gone (no draw_state) -
            # the fast-dock tradeoff.
            if active:
                # flat_button applies its own shadow when it draws a bg (the
                # inactive alpha=0 tabs stay flat).
                clicked = flat_button(
                    label, draw_state, view_id=f"tab_{i}",
                    width=tab_width, height=btn_height,
                    color=tab_color, factor=tab_factor,
                    tint_value=new_value + 0.23 - 0.03)
            else:
                clicked = flat_button(
                    label, draw_state, view_id=f"tab_{i}",
                    width=tab_width, height=btn_height,
                    color=tab_color, factor=tab_factor, alpha=0.0,
                    tint_value=new_value if not tinted else 0.1,
                    saturation=1.0 if tinted else 0.3,
                    text_value=1.0 if not tinted else 0.9)

        if clicked:
            changed = True
            if io.key_shift or as_toggles:
                if active:
                    selected.remove(tab)
                else:
                    selected.append(tab)
            else:
                selected = [tab]

        same_line()

    imgui.dummy(0, 0)

    if changed:
        Core.melty.refresh_nested_windows(draw_state)

    return changed, selected


@render_func(with_header=draw_header, is_tree=False, shadow=False)
def draw_enum_tabs(input_value: type, tab_state: TabState):
    enum_states = list(input_value)
    selected = tab_state.selected_tabs

    changed, new_selected = draw_tab_bar(selected, collection=enum_states)
    if changed:
        tab_state.selected_tabs = new_selected

    return False, input_value
