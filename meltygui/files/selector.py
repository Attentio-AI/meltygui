"""File selection using the same explorer as the standalone file browser."""
from pathlib import Path

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
def draw_file_selector(input_value: str = None, draw_state=None,
                       selector_state: FileSelectorState = None,
                       escape_key_pressed=False):
    """Return (True, absolute_path) once when a file is activated.

    Navigation stays here. input_value seeds the initial directory (home
    by default); later opens remember the last directory. Double-click or
    Enter selects a file. Cancel / the window close button returns no change.
    In an OS child, selection closes the window. Pass open_requested=True
    for one frame to open/reopen it; False leaves its current state alone.
    Omit open_requested to open immediately on the first call.
    """
    if selector_state.directory is None:
        directory = Path(input_value or Path.home()).expanduser().resolve()
        selector_state.directory = str(directory if directory.is_dir() else directory.parent)
    left, top, right, bottom = draw_state.get_content_rect()
    changed, picked = draw_fast_file_explorer(
        selector_state.directory, name='files', width=right - left,
        height=max(120, bottom - top - 35),
        folder_bg_boost=-0.23, folder_bg_rounding=10.0)
    cancelled, _ = draw_button(label='Cancel', name='cancel', show_header=False,
                               width=90, height=25)
    if cancelled or escape_key_pressed or pressed('escape'):
        draw_state.closed = True
        return False, None
    if changed:
        path = Path(picked).expanduser().resolve()
        if path.is_dir():
            selector_state.directory = str(path)
        elif path.is_file():
            draw_state.closed = True
            return True, str(path)
    return False, None
