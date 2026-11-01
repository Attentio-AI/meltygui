"""Embedded hosts keep their ImGui frame usable through lifecycle failures."""
import pytest
import meltygui_imgui as imgui

from meltygui.core.melty import Melty, EmbeddedFrameState
from meltygui.core.rendering.overlay import overlay_draw_target, overlay_draw_list


@pytest.mark.parametrize('phase', ['begin_frame', 'end_frame'])
def test_native_failure_restores_host_and_next_frame(monkeypatch, phase):
    previous = imgui.get_current_context()
    context = imgui.create_context()
    io = imgui.get_io()
    io.ini_file_name = None
    io.display_size = (640, 480)
    io.fonts.get_tex_data_as_rgba32()
    state = EmbeddedFrameState()
    original = getattr(Melty, phase)

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('native lifecycle failure')

    try:
        for failing in (True, False):
            imgui.new_frame()
            imgui.begin('embedded host')
            style = imgui.get_style()
            style.alpha = 0.7
            colors = tuple(tuple(style.colors[i]) for i in range(imgui.COLOR_COUNT))
            clip = tuple(imgui.get_window_draw_list().get_clip_rect_min())
            try:
                with monkeypatch.context() as patch:
                    if failing:
                        patch.setattr(Melty, phase, fail)
                        with pytest.raises(RuntimeError, match='native lifecycle failure'):
                            with Melty.embedded_frame(state):
                                imgui.text('preview')
                    else:
                        with Melty.embedded_frame(state):
                            imgui.text('recovered preview')
                assert style.alpha == pytest.approx(0.7)
                assert tuple(tuple(style.colors[i]) for i in range(imgui.COLOR_COUNT)) == colors
                assert tuple(imgui.get_window_draw_list().get_clip_rect_min()) == clip
                assert not Melty.channels_split
                assert not Melty._overlay_channels_active
                imgui.text('host remains drawable')
            finally:
                imgui.end()
                imgui.render()
    finally:
        state.close()
        imgui.destroy_context(context)
        imgui.set_current_context(previous)


def test_embedded_overlay_restores_channel_routing_after_error(monkeypatch):
    monkeypatch.setattr(Melty, '_overlay_channels_active', True)
    outer, inner = object(), object()
    with overlay_draw_target(outer):
        assert not Melty._overlay_channels_active
        with pytest.raises(RuntimeError):
            with overlay_draw_target(inner):
                assert overlay_draw_list() is inner
                raise RuntimeError('overlay failure')
        assert overlay_draw_list() is outer
        assert not Melty._overlay_channels_active
    assert Melty._overlay_channels_active
