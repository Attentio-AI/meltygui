"""Settings paint and click through real, hidden OS-decorated surfaces."""
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
    from meltygui.core.graphics.overlay_renderer import SplitOverlayRenderer
    from meltygui.core.input.input_handler import set_button_probe
    from meltygui.core.input.pynput_backend import GlfwQueueBackend
    from meltygui.core.melty import Melty
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.windowing import titlebar
    from meltygui.core.windowing.surface import Surface
    from meltygui.view.header_view import draw_header

    header, result_path = sys.argv[1:]
    result_path = Path(result_path)
    Toggles.Melty.enhanced_titlebar = False
    if sys.platform != 'darwin':
        titlebar.backend_supported = lambda: False
    Toggles.show_fps = True
    pointer = [-100., -100.]
    root_state = None

    # Keep native GL contexts and the real render loop, without showing or
    # focusing a window on the user's desktop. All persistence is temporary.
    window_hint = glfw.window_hint
    glfw.window_hint = lambda hint, value: window_hint(
        hint, False if hint == glfw.VISIBLE else value)
    glfw.show_window = lambda window: None
    glfw.focus_window = lambda window: None

    process_inputs = SplitOverlayRenderer.process_inputs
    def inject(self):
        process_inputs(self)
        surface = Surface.active
        io = imgui.get_io()
        io.mouse_pos = pointer if surface.parent is None else (-100., -100.)
        down = surface.parent is None and surface.frames == 4
        io.mouse_down[0] = down
        set_button_probe(lambda name: down if name == 'left_mouse' else None)
        if surface.parent is None and surface.frames in (4, 5):
            feed = Melty.event_handler.feed_down if down else Melty.event_handler.feed_up
            feed('left_mouse', *pointer)
    SplitOverlayRenderer.process_inputs = inject
    GlfwQueueBackend.button_really_down = lambda self, name: imgui.get_io().mouse_down[0] if name == 'left_mouse' else None

    # Capture the rendered framebuffer before its swap, for visual review.
    swap_buffers = glfw.swap_buffers
    def capture(window):
        from OpenGL import GL as gl
        from PIL import Image
        surface = Surface.active
        if surface.frames == 3 or (surface.parent is not None and surface.frames == 1):
            width, height = glfw.get_framebuffer_size(window)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, 0)
            gl.glReadBuffer(gl.GL_BACK)
            pixels = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            image = Image.frombytes('RGBA', (width, height), pixels).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            image.save(result_path.with_name('settings.png' if surface.parent else 'root.png'))
        swap_buffers(window)
    glfw.swap_buffers = capture

    frame = Surface.frame
    def advance(self):
        frame(self)
        if self.parent is not None or self.closed:
            return
        if self.frames == 3:
            assert not self.chrome
            assert glfw.get_window_attrib(self.window, glfw.DECORATED)
            assert titlebar.control_kinds() == ((), ('settings',))
            button = titlebar._button_layout(imgui.get_io().display_size.x, False)[0]
            x0, y0, x1, y1 = button[2]
            pointer[:] = [(x0 + x1) / 2, (y0 + y1) / 2]
            assert root_state.window_pos[1] == (0 if header == 'header' else titlebar.top_inset())
        if self.frames >= 12:
            children = [child for child in self.children if not child.closed and child.frames > 0]
            assert len(children) == 1, (self.frames, self.children)
            assert children[0].title == 'Settings'
            assert children[0].settings is None
            result_path.write_text(json.dumps({'opened': True, 'header': header,
                'frames': self.frames, 'child_frames': children[0].frames,
                'body_top': root_state.window_pos[1]}))
            glfw.set_window_should_close(self.window, True)
        self.request_frame()
    Surface.frame = advance

    @meltygui.glfw_window(name='Native settings check', app_id='native-settings-test',
                         width=600, height=400, settings={'size': 14, 'wrap': False},
                         with_header=draw_header if header == 'header' else None)
    @render_func(determines_height=False)
    def check(input_value: object = None, draw_state=None):
        global root_state
        root_state = draw_state
        imgui.text('Click the cog to open Settings.')
        return False, input_value

    meltygui.run()
''')


@pytest.mark.skipif(sys.platform != 'darwin' and not (
    os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')),
    reason='needs a display for hidden native GL contexts')
@pytest.mark.parametrize('header', ['header', 'no_header'])
def test_settings_cog_opens_a_native_child_with_os_decorations(tmp_path, header):
    script = tmp_path / 'settings_app.py'
    script.write_text(APP)
    result_path = tmp_path / 'result.json'
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / 'state'),
               XDG_CACHE_HOME=str(tmp_path / 'cache'), XDG_CONFIG_HOME=str(tmp_path / 'config'),
               XDG_DATA_HOME=str(tmp_path / 'data'), MELTY_FILE_META=str(tmp_path / 'file_meta.pkl'))
    env.pop('MELTY_BENCH', None)
    process = subprocess.run([sys.executable, str(script), header, str(result_path)], env=env,
                             close_fds=False, capture_output=True, text=True, timeout=45)
    assert process.returncode == 0, process.stdout + process.stderr
    assert result_path.exists(), process.stdout + process.stderr
    assert json.loads(result_path.read_text())['opened']
    assert result_path.with_name('root.png').exists()
    assert result_path.with_name('settings.png').exists()
