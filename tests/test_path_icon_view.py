"""Inline path artwork keeps row geometry and occurrence identities intact."""
from pathlib import Path
from types import SimpleNamespace

from meltygui.view import path_icon_view as view
from meltygui.model.folder_icon_model import PathIcon


class DrawList:
    def __init__(self):
        self.images, self.lines, self.text = [], [], []

    def add_image(self, *args, **kwargs):
        self.images.append((args, kwargs))

    def add_line(self, *args):
        self.lines.append(args)

    def add_text(self, *args):
        self.text.append(args)


def test_artwork_fits_slot_and_retains_tint_mark():
    class Texture:
        pixels = SimpleNamespace(width=64, height=32)

        def __int__(self):
            return 42

    dl = DrawList()
    view.paint_path_icon(dl, Path('/app'), Texture(), 10, 20, 18, 123, True, None)
    assert dl.images[0][0] == (42, (10, 24.5), (28, 33.5))
    assert dl.lines == [(11, 38, 27, 38, 123, 1.5)]


def test_unknown_file_falls_back_to_file_glyph(monkeypatch):
    monkeypatch.setattr(view.imgui, 'get_font_size', lambda: 16)
    dl = DrawList()
    view.paint_path_icon(dl, Path('unknown.unsupported'), None, 0, 0, 18, 123, False, None)
    assert dl.text[-1][-1] == '\uf15b'


def test_repeated_path_occurrences_keep_separate_identity_and_cursor(monkeypatch):
    calls, cursors = [], []
    monkeypatch.setattr(view.imgui, 'get_cursor_screen_pos', lambda: (5, 6))
    monkeypatch.setattr(view.imgui, 'set_cursor_screen_pos', cursors.append)
    monkeypatch.setattr(view, 'draw_path_icon', lambda *a, **kw: calls.append((a, kw)))
    icon = PathIcon('/app', custom_icon='custom')
    view.path_icon(icon, 10, 20, 18, 123, key='auto')
    view.path_icon(icon, 10, 44, 18, 123, key='explicit')
    assert calls[0][1]['name'] != calls[1][1]['name']
    assert all(call[0] == ('/app',) and call[1]['custom_icon'] == 'custom' for call in calls)
    assert cursors == [(10, 20), (5, 6), (10, 44), (5, 6)]


def test_clipped_toolbar_ignores_icon_slots_past_ellipsis(monkeypatch):
    calls = []
    monkeypatch.setattr(view, 'path_icon', lambda *a, **kw: calls.append(a))
    dl = DrawList()
    view.path_label(dl, '…', 0, 0, 123, {2: '/app'})
    assert not calls and dl.text[-1][-1] == '…'


def test_batched_rows_bypass_per_icon_views_and_share_repeated_paths(monkeypatch):
    from meltygui.state.path_icon_state import PathIconState
    state = PathIconState()
    dl = DrawList()
    monkeypatch.setattr(view.imgui, 'get_window_draw_list', lambda: dl)
    monkeypatch.setattr(view.imgui, 'get_font_size', lambda: 16)
    monkeypatch.setattr(view, 'draw_path_icon', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('a batched icon must not create a UI view')))
    for i in range(40):
        view.path_icon('/app', 0, i * 20, 18, 123, icons=state)
    assert state.requested == {Path('/app')}
    assert len(dl.text) == 40
