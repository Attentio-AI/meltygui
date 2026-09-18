"""The colour picker's icon row: `locate_icon` round-trips through the
draw_state tier like the offset rows, and the picker reserves its row."""
import pytest
from meltygui.state.new_core_model import DrawState
from meltygui.core.rendering import parameter_core as anywhere
from meltygui.core.runtime.toggles import Toggles


@pytest.fixture
def bare_window(monkeypatch):
    """A draw_state no code source sets params for: every locate write
    falls through to the DRAW_STATE tier."""
    ds = DrawState()
    ds._kwargs = {}
    srcs = dict(sources={}, kinds={}, writable=[], locations={})
    monkeypatch.setattr(anywhere, '_sources_for', lambda *args: srcs)
    monkeypatch.setattr(anywhere, '_input_busy', lambda: False)
    monkeypatch.setattr(anywhere, '_note_scroll', lambda: None)
    monkeypatch.setattr(anywhere, '_anywhere_recompile_tick', lambda *args: None)
    monkeypatch.setattr(anywhere, '_anywhere_verify_tick', lambda *args: None)
    yield ds
    anywhere._DEFERRED_DS.discard(ds)


def test_locate_icon_lands_on_the_draw_state_and_reads_back(bare_window):
    ds = bare_window
    assert ds.locate_icon is None
    ds.locate_icon = ""
    assert ds.__dict__.get("icon") == ""
    assert ds.locate_icon == ""
    ds.locate_icon = None
    assert ds.__dict__.get("icon") is None
    assert ds.locate_icon is None


def test_wrapper_kwargs_adopt_the_draw_state_icon(bare_window):
    """The wrapper's DRAW_STATE fallback: a mode default's icon=None yields
    to the stored glyph, a passed glyph keeps priority."""
    ds = bare_window
    ds.locate_icon = ""

    def adopt(kwargs):   # the core_render wrapper's icon block
        if kwargs.get("icon") is None:
            stored = ds.__dict__.get("icon")
            if stored is not None:
                kwargs["icon"] = stored
        return kwargs

    assert adopt({})["icon"] == ""
    assert adopt({"icon": None})["icon"] == ""
    assert adopt({"icon": ""})["icon"] == ""


def test_picker_reserves_the_icon_row():
    from meltygui.view.color_view import color_picker_height
    without = color_picker_height(4, has_owner=False)
    with_owner = color_picker_height(4, has_owner=True)
    assert with_owner - without == (Toggles.ColorPicker.offsets_height
                                    + Toggles.ColorPicker.icon_row_height)


def test_current_glyph_resolves_to_its_menu_row():
    from meltygui.model.dropdown_model import _dd_path_for_value
    from meltygui.model.dropdown_model import _dd_walk
    from meltygui.model.icon_model import FA_ICONS
    path = _dd_path_for_value(FA_ICONS, "")   # code
    assert path is not None and len(path) == 2
    assert _dd_walk(FA_ICONS, tuple(path)) == ""
