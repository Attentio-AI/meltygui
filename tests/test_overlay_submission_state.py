"""Draw commands must see correct GL state despite redundant-call suppression."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core.graphics import overlay_renderer as module


def command(texture, clip=(1, 2, 31, 42)):
    return SimpleNamespace(texture_id=texture, clip_rect=clip, elem_count=3)


def draw_list(*commands):
    return SimpleNamespace(commands=commands, vtx_buffer_size=12,
                           idx_buffer_size=3 * len(commands),
                           vtx_buffer_data=0, idx_buffer_data=0)


@pytest.fixture
def submission(monkeypatch):
    gl = Mock()
    for name in dir(module.gl):
        if name.startswith('GL_'):
            setattr(gl, name, getattr(module.gl, name))
    gl.glGetIntegerv.return_value = 99
    gl.glIsEnabled.return_value = False
    monkeypatch.setattr(module, 'gl', gl)
    monkeypatch.setattr(module, 'get_common_gl_state', lambda: ())
    restore = Mock()
    monkeypatch.setattr(module, 'restore_common_gl_state', restore)
    renderer = SimpleNamespace(
        io=SimpleNamespace(display_size=(200, 100), display_fb_scale=(1, 1)),
        _refresh_style_shader=lambda: None, _frame_origin=lambda: (5, 7),
        _lcd_ok=False, _bind_style_context=lambda _: 0,
        _unbind_style_context=lambda _: None, _bind_text_mode=lambda: 10,
        _shader_handle=1, _attrib_location_tex=2, _attrib_proj_mtx=3,
        _loc_atlas=4, _vao_handle=5, _vbo_handle=6, _elements_handle=7,
        debug_static_mask=False, debug_overlay_mask=False)
    state = {}
    gl.glBindTexture.side_effect = lambda target, texture: state.update(texture=texture)
    gl.glUniform1i.side_effect = lambda location, value: state.update({location: value})
    gl.glScissor.side_effect = lambda *box: state.update(scissor=box)
    draws = []
    renderer._draw_style_elements = lambda commands, texture, start, end, kind: draws.append(
        (state['texture'], state.get(4), state['scissor'], start, end))
    return renderer, gl, state, draws, restore


def test_draw_lists_preserve_state_and_order_across_lists_and_invocations(submission):
    renderer, gl, state, draws, restore = submission
    lists = [draw_list(command(10), command(10), command(20)),
             draw_list(command(21), command(21, (2, 3, 32, 43)), command(10))]
    render = module.SplitOverlayRenderer._render_command_lists
    render(renderer, None, lists)
    assert draws == [
        (10, 1, (6, 65, 30, 40), 0, 3),
        (10, 1, (6, 65, 30, 40), 3, 6),
        (20, 0, (6, 65, 30, 40), 6, 9),
        (21, 0, (6, 65, 30, 40), 0, 3),
        (21, 0, (7, 64, 30, 40), 3, 6),
        (10, 1, (6, 65, 30, 40), 6, 9)]
    assert gl.glBindTexture.call_count == 4
    assert [c.args for c in gl.glUniform1i.call_args_list if c.args[0] == 4] == [(4, 1), (4, 0), (4, 1)]
    assert gl.glScissor.call_count == 3
    restore.assert_called_once_with(())
    assert gl.glUseProgram.call_args.args == (99,)
    assert gl.glBindVertexArray.call_args.args == (99,)
    # Another renderer can leave arbitrary state between invocations.
    state.update(texture=999, scissor=(0, 0, 0, 0))
    state[4] = 0
    render(renderer, None, [draw_list(command(10))])
    assert draws[-1] == (10, 1, (6, 65, 30, 40), 0, 3)


def test_channel_stencil_invalidates_scissor_without_losing_command_slices(submission):
    renderer, gl, state, draws, restore = submission
    renderer._setup_channel_stencil = lambda *args: gl.glScissor(0, 0, 1, 1)
    melty = SimpleNamespace(max_layer=100, registered_windows={}, root_draw_states={})
    # One merged command straddles two channels. Both need the command's clip,
    # even though it matches the previous channel's final clip.
    commands = draw_list(command(10), command(10))
    module.SplitOverlayRenderer._render_overlay_channels(
        renderer, None, commands, [(1, 0, 2), (2, 2, 6)], melty)
    assert draws == [(10, 1, (6, 65, 30, 40), 0, 2),
                     (10, 1, (6, 65, 30, 40), 2, 3),
                     (10, 1, (6, 65, 30, 40), 3, 6)]
    assert gl.glBindTexture.call_count == 1
    assert gl.glScissor.call_count == 4
    restore.assert_called_once_with(())


def test_shader_without_atlas_uniform_does_not_write_it(submission):
    renderer, gl, state, draws, restore = submission
    renderer._bind_text_mode = lambda: -1
    module.SplitOverlayRenderer._render_command_lists(
        renderer, None, [draw_list(command(0), command(0))])
    assert [d[:2] for d in draws] == [(0, None), (0, None)]
    assert gl.glBindTexture.call_count == 1
    assert all(c.args[0] != 4 for c in gl.glUniform1i.call_args_list)
