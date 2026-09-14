"""File selection using the same explorer as the standalone file browser."""
from pathlib import Path

import imgui

from src.lsd.gl_gui.app import pressed

from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.new_core_view import draw_button
from src.lsd.gl_gui.view.playground.fast_file_explorer import draw_fast_file_explorer


class FileSelectorState(DictConversion):
    def __init__(self):
        super().__init__()
        self.directory = None


@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=True,
             show_bg=False, shadow=False, determines_height=False)
def draw_file_selector(input_value: str | None = None, draw_state=None,
                       selector_state: FileSelectorState = None,
                       escape_key_pressed=False, choose_folder=False,
                       context_menu=None, browse=None):
    """Return (True, absolute_path) once when a file is activated.

    Navigation stays here. input_value seeds the initial directory (home
    by default); later opens remember the last directory. Double-click or
    Enter selects a file. Cancel / the window close button returns no change.
    In an OS child, selection closes the window. Pass open_requested=True
    for one frame to open/reopen it; False leaves its current state alone.
    Omit open_requested to open immediately on the first call.

    ``choose_folder=True`` makes it a FOLDER picker: files are inert, a
    "Choose Folder" button beside Cancel returns the directory being
    shown (double-click still descends). ``context_menu`` = {label:
    callable(path)} is the listing's right-click menu (the explorer's).
    ``browse`` = a directory to navigate to — a path or ``(path, token)``
    — applied once per distinct value; keep passing it.
    """
    if selector_state.directory is None:
        directory = Path(input_value or Path.home()).expanduser().resolve()
        selector_state.directory = str(directory if directory.is_dir() else directory.parent)
    # ``browse``: a directory to show - a path, or ``(path, token)`` where
    # a new token re-applies the same path - applied once per distinct
    # value (the settings editor's shortcuts column pick). An OS-child body
    # runs on its own thread, so the host keeps passing the token rather
    # than passing it for one frame.
    if browse != getattr(selector_state, "_browse_applied", None):
        selector_state._browse_applied = browse
        target = browse[0] if isinstance(browse, tuple) else browse
        if target:
            target = Path(target).expanduser()
            if target.is_dir():
                selector_state.directory = str(target)
    left, top, right, bottom = draw_state.get_content_rect()
    changed, picked = draw_fast_file_explorer(
        selector_state.directory, name='files', width=right - left,
        height=max(120, bottom - top - 35),
        folder_bg_boost=-0.23, folder_bg_rounding=10.0, context_menu=context_menu)
    if choose_folder:
        chosen, _ = draw_button(label='Choose Folder', name='choose', show_header=False,
                                width=130, height=25)
        imgui.same_line()
        if chosen:
            draw_state.closed = True
            return True, selector_state.directory
    cancelled, _ = draw_button(label='Cancel', name='cancel', show_header=False,
                               width=90, height=25)
    if cancelled or escape_key_pressed or pressed('escape'):
        draw_state.closed = True
        return False, None
    if changed:
        path = Path(picked).expanduser().resolve()
        if path.is_dir():
            selector_state.directory = str(path)
        elif path.is_file() and not choose_folder:
            draw_state.closed = True
            return True, str(path)
    return False, None
