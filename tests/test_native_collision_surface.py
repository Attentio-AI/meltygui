"""Exercise fixed collision bounds through a real, OS-decorated Surface.

The model alone did not catch Surface skipping its collision lifecycle when
custom chrome was disabled (including every macOS window).
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest


APP = textwrap.dedent('''\
    import json
    import sys
    from pathlib import Path

    import meltygui
    from meltygui import imgui, window_api as glfw
    from meltygui.core.core_render import render_func
    from meltygui.core.layout.column_core import ColumnLayout, RowLayout
    from meltygui.core.melty import Melty
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.windowing import os_frame, titlebar
    from meltygui.core.windowing.surface import Surface

    axis, result_path = sys.argv[1:]
    result_path = Path(result_path)
    Toggles.Melty.wayland_show_frame = True
    Toggles.Melty.push_os_window_edges = True
    # On Linux this also exercises an unavailable native adjustment backend.
    # macOS uses the actual capability check against an absent endpoint,
    # independent of whether the user's Melty Windows utility is running.
    if sys.platform == 'darwin':
        from meltygui.core.windowing import melty_windows
        melty_windows.PATH = str(result_path.with_suffix('.absent-socket'))
    else:
        titlebar.can_adjust_window_edges = lambda window: False
    os_frame._any_button_down = lambda: True
    travels = [0., -500., -500., -100., 0., 900., 900., 0.]
    observed = []
    state = {'index': 0, 'travel': 0.}

    def advance(window, draw_state, edges):
        try:
            box = (*glfw.get_window_pos(window), *glfw.get_window_size(window))
            if not observed:
                state.update(box=box, initial=edges[1][axis])
            observed.append({'box': box, 'divider': edges[1][axis],
                             'body': [draw_state.width, draw_state.height],
                             'travel': state['travel']})
            expected = max(120., min(500., state['initial'] + state['travel']))
            assert abs(edges[1][axis] - expected) < 1., (edges, expected)
            assert box == state['box'], (box, state['box'])
            assert (draw_state.width, draw_state.height) == (800., 800.)
            assert draw_state.window_pos == (0., 0.)
            assert not Surface.active.chrome
            assert os_frame.mode() == 'walls'
            assert titlebar._pending_surface_size is None
            index = state['index']
            if index == len(travels):
                result_path.write_text(json.dumps({'passed': True, 'frames': observed}))
                glfw.set_window_should_close(window, True)
                return
            travel = travels[index]
            pending = draw_state._pending_drags if axis == 'x' else draw_state._pending_row_drags
            pending.append((edges[1], edges[1][axis] + travel - state['travel'], True))
            for native_axis in ('x', 'y'):
                os_frame.queue_drag(native_axis, 0, -100.)
                os_frame.queue_drag(native_axis, 1, 100.)
            state.update(index=index + 1, travel=travel)
            Surface.active.request_frame()
        except Exception as error:
            result_path.write_text(json.dumps({'passed': False, 'error': repr(error),
                                               'frames': observed}))
            glfw.set_window_should_close(window, True)

    @meltygui.glfw_window(name='Collision fallback check', app_id='collision-fallback-test',
                         width=800, height=800)
    @render_func(use_cache=False, show_header=False, determines_height=False)
    def check(input_value: object = None, draw_state=None, column_edges=None, row_edges=None):
        if axis == 'x':
            layout = ColumnLayout(draw_state, 2, column_edges=column_edges,
                                  column_widths=[300., None], column_mins=[120., 200.],
                                  column_maxes=[500., None])
        else:
            layout = RowLayout(draw_state, 2, row_edges=row_edges,
                               row_heights=[300., None], row_mins=[120., 200.],
                               row_maxes=[500., None])
        for index in range(2):
            with layout.cell(index):
                imgui.text('Local edges resize inside a fixed native frame.')
        layout.finish()
        window = Surface.active.window
        Melty.post_to_render(lambda: advance(window, draw_state, layout.edges))
        return False, input_value

    meltygui.run()
''')


@pytest.mark.skipif(sys.platform != 'darwin' and not (
    os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')),
    reason='needs a display to exercise a real Surface')
@pytest.mark.parametrize('axis', ['x', 'y'])
def test_os_decorated_surface_keeps_contacts_fixed_during_drag_and_reversal(tmp_path, axis):
    script = tmp_path / 'collision_app.py'
    script.write_text(APP)
    result_path = tmp_path / 'result.json'
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / 'state'),
               XDG_CACHE_HOME=str(tmp_path / 'cache'), XDG_CONFIG_HOME=str(tmp_path / 'config'),
               MELTY_FILE_META=str(tmp_path / 'file_meta.pkl'))
    env.pop('MELTY_BENCH', None)
    process = subprocess.run([sys.executable, str(script), axis, str(result_path)], env=env,
                             close_fds=False, capture_output=True, text=True, timeout=45)
    assert process.returncode == 0, process.stdout + process.stderr
    assert result_path.exists(), process.stdout + process.stderr
    result = json.loads(result_path.read_text())
    assert result['passed'], result
    assert len(result['frames']) == 9
