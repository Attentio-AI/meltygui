"""Shared presentation dependencies reach views without becoming saved controls."""
from types import SimpleNamespace

from meltygui.core.core_render import render_func
from meltygui.core.core_render import render_func_kwarg_names
from meltygui.core.parameter_core import view_param_names
from meltygui.view.tensor_view import draw_tensor_dim, draw_voxels


def test_render_context_is_not_an_editable_view_parameter():
    assert 'ui_scale' not in view_param_names(SimpleNamespace(_view_func=draw_tensor_dim))
    assert 'font_manager' not in view_param_names(SimpleNamespace(_view_func=draw_voxels))
    assert {'ui_scale', 'font_manager'}.isdisjoint(render_func_kwarg_names())


def test_render_context_follows_runtime_and_allows_explicit_overrides(gl_context, monkeypatch):
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame

    runtime = _init_melty()
    seen = []

    @render_func(tint=(0.3, 0.5, 0.7), show_bg=False, use_cache=False)
    def context_probe(input_value: int, draw_state=None, ui_scale=1.0, font_manager=None):
        seen.append((ui_scale, font_manager, draw_state))
        return False, input_value

    first_fonts = SimpleNamespace(get=lambda font: None)
    second_fonts = SimpleNamespace(get=lambda font: None)
    override_fonts = object()

    def frame(**kwargs):
        _tick_frame(runtime)
        begin_frame()
        try:
            context_probe(1, name='context injection probe', **kwargs)
        finally:
            end_frame()

    monkeypatch.setattr(runtime, 'ui_scale', 1.25)
    monkeypatch.setattr(runtime, 'font_mgr', first_fonts)
    frame()
    monkeypatch.setattr(runtime, 'ui_scale', 1.75)
    monkeypatch.setattr(runtime, 'font_mgr', second_fonts)
    frame()
    frame(ui_scale=0.75, font_manager=override_fonts)
    frame()
    assert [(scale, fonts) for scale, fonts, _ in seen] == [
        (1.25, first_fonts), (1.75, second_fonts), (0.75, override_fonts), (1.75, second_fonts)]
    for _, _, draw_state in seen:
        assert {'ui_scale', 'font_manager'}.isdisjoint(draw_state.auto_params)
