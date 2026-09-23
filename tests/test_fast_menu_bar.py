"""The menu bar paints in its parent while preserving popup state."""
from test_fast_draw_dropdown import melty, _frames


def test_menu_bar_has_no_tile_and_keeps_state(melty):
    from meltygui.view.menu_view import draw_menu_bar
    from meltygui.state.new_core_model import MenuBarState
    menus = {"File": {"New": lambda: None}, "Run": {}}
    result = _frames(melty, lambda: draw_menu_bar(menus, name="menu", return_extras=True))
    changed, value, state = result
    assert not changed and value is menus
    assert state._wrapper is draw_menu_bar
    assert not state.use_cache
    assert isinstance(state.misc["menu_bar_state"], MenuBarState)
    saved = state.misc["menu_bar_state"]
    again = _frames(melty, lambda: draw_menu_bar(menus, name="menu", return_extras=True))
    assert again[2] is state and state.misc["menu_bar_state"] is saved
    assert melty.depth == 0 and not melty.draw_state_stack and not melty.unique_stack


def test_menu_title_toggles_focus_without_wrapper(melty, monkeypatch):
    from meltygui.view.menu_view import draw_menu_bar
    from meltygui.view import header_view, dropdown_view
    menus = {"File": {"New": lambda: None}}
    monkeypatch.setattr(header_view, "flat_button", lambda *args, **kwargs: True)
    monkeypatch.setattr(dropdown_view, "draw_dd_menu", lambda *args, **kwargs: (False, None, None))
    _, _, ds = _frames(melty, lambda: draw_menu_bar(menus, name="menu", return_extras=True), count=1)
    assert ds.misc["menu_bar_state"].open_title == "File"
    assert melty.popover_focused_ds is ds and melty.text_focused_ds is ds
    _frames(melty, lambda: draw_menu_bar(menus, name="menu"), count=1)
    assert ds.misc["menu_bar_state"].open_title is None
    assert melty.popover_focused_ds is None
