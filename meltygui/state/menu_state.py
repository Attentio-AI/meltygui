"""Menu state functions and supporting definitions."""
from meltygui.state.new_core_model import DropDownState


def _menu_state(state, title):
    """The DropDownState of `title`'s menu, created on first sight."""
    menu_state = state.menus.get(title)
    if not isinstance(menu_state, DropDownState):
        menu_state = state.menus[title] = DropDownState()
    return menu_state
