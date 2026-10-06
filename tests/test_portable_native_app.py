"""A second ordinary app exercises the real shared frame with recorded GPU calls."""
from pathlib import Path
import subprocess
import sys
import textwrap


def test_portable_app_boots_draws_and_checkpoints_without_pro_or_desktop_graphics(tmp_path):
    application = Path(__file__).resolve().parents[1] / 'examples/portable_counter'
    program = textwrap.dedent('''
        import importlib.abc
        import json
        import os
        from pathlib import Path
        import sys
        from types import SimpleNamespace
        from meltygui.platforms.ios.compile_shaders import generate
        import meltygui

        sandbox, application = map(Path, sys.argv[1:])
        generate(Path(meltygui.__file__).parent, sandbox / 'shaders')
        manifest = (sandbox / 'shaders/metal-programs.json').read_text()
        class NoDesktopOrPro(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in ('OpenGL', 'glfw', 'meltygui_pro'):
                    raise AssertionError('Unexpected platform/app dependency: ' + fullname)
        sys.meta_path.insert(0, NoDesktopOrPro())
        Path.home = classmethod(lambda cls: sandbox)
        os.chdir(application)
        sys.platform = 'ios'
        from meltygui.core.runtime.native_app import start_native_application
        from meltygui.core.graphics.metal_renderer import MetalRenderer
        from meltygui.core.windowing.surface_frame import root_view_kwargs
        from meltygui.core.windowing import titlebar
        from meltygui.core.melty import Melty

        class GPU:
            next_texture = 0
            meshes = 0
            def program_manifest(self): return manifest
            def create_texture(self, *args):
                self.next_texture += 1
                return self.next_texture
            def delete_texture(self, *args): pass
            def clear_texture(self, *args): pass
            def upload_texture(self, *args): pass
            def quad(self, *args): pass
            def shape(self, *args): pass
            def set_scene(self, *args): pass
            def mesh(self, *args): self.meshes += 1

        gpu = GPU()
        host = SimpleNamespace(request_frame=lambda: None, set_safe_zone=lambda top, bottom: None,
                               set_keyboard_visible=lambda value: None)
        native = start_native_application({'entry_module': 'main'}, host,
                                          lambda: MetalRenderer(gpu))
        assert native.config['app_id'] == 'melty-portable-counter'
        for frame in range(5):
            native.frame(dict(width=640, height=480, scale=1, now=10+frame/60,
                              presentation_time=10+(frame+1)/60), [])
            native.presented()
        assert gpu.meshes > 0
        assert native.cache._tiles
        import main
        before = main.counter['count']
        for index, kind in enumerate(('touch_begin', 'touch_end', None, None)):
            events = [dict(kind=kind, touch_id=1, x=25, y=44)] if kind else []
            native.frame(dict(width=640, height=480, scale=1, now=10.2+index/60,
                              presentation_time=10.21+index/60), events)
        assert main.counter['count'] == before + main.settings['step']
        Melty.root_fill = (640, 480, 0)
        assert root_view_kwargs('Counter', with_header=object())['with_header_end'] is titlebar.draw_header_controls
        Melty.root_fill = None
        # The ordinary app's settings window uses the shared managed lifecycle.
        native.settings.request_open()
        native.frame(dict(width=640, height=480, scale=1, now=11, presentation_time=11.01), [])
        assert not native.settings.open_requested
        settings_window = next(ds for ds in Melty.draw_state_registry.values()
                               if ds.name == 'Settings')
        assert not settings_window.closed
        assert (settings_window.width, settings_window.height) == (520, 640)
        assert settings_window.frame_count > 0

        # Use the same native request inside another managed window, including
        # a grandchild. Exercise actual rendering, not just request routing.
        from meltygui import render_func, draw_file_selector
        from meltygui.core.windowing.window_visibility import WindowCallState
        dialogs = [WindowCallState(), WindowCallState(), WindowCallState()]
        opened = [True]
        finish = [False]
        received = []
        @render_func(use_cache=False)
        def leaf(input_value, draw_state):
            if finish[0]:
                draw_state.closed = True
                return True, 'selected'
            return False, input_value
        @render_func(use_cache=False)
        def child(input_value, draw_state):
            if dialogs[1].needs_call(opened[0]):
                changed, value = dialogs[1].draw(
                    leaf, None, name='Grandchild', glfw_window=True,
                    open_requested=opened[0], window_size=(180, 120))
                if changed:
                    received.append(value)
            return False, input_value
        @render_func(use_cache=False)
        def parent(input_value, draw_state):
            dialogs[0].draw(child, None, name='Child', glfw_window=True,
                            open_requested=opened[0], window_size=(400, 300))
            dialogs[2].draw(draw_file_selector, str(sandbox), name='Open file',
                            glfw_window=True, open_requested=opened[0],
                            window_size=(720, 640))
            return False, input_value
        native.body = lambda surface: parent(None, **root_view_kwargs('Parent'))
        def frames(start, count=3):
            for index in range(start, start + count):
                native.frame(dict(width=1000, height=900, scale=1,
                                  now=index/60, presentation_time=(index+1)/60), [])
                opened[0] = False
        frames(720)
        states = {ds.name: ds for ds in Melty.draw_state_registry.values()
                  if ds.name in ('Child', 'Grandchild', 'Open file')}
        for name, size in [('Child', (400, 300)), ('Grandchild', (180, 120)),
                           ('Open file', (720, 640))]:
            ds = states[name]
            assert (ds.width, ds.height) == size, (name, ds.width, ds.height)
            assert ds.frame_count > 0 and not ds.closed
            assert ds.parent_window is not None
            assert ds._kwargs['glfw_window'] is False
        assert states['Grandchild'].parent_window is states['Child']
        # Initial geometry must not pin a resized window on the next call.
        states['Child'].width = 430
        states['Child'].height = 320
        frames(723)
        assert (states['Child'].width, states['Child'].height) == (430, 320)
        finish[0] = True
        frames(726)
        assert states['Grandchild'].closed
        assert received == ['selected']
        finish[0] = False
        opened[0] = True
        frames(729)
        assert not states['Grandchild'].closed
        assert not Melty.surface_requests and not Melty.surface_windows
        native.suspend()
        assert (sandbox / 'Library/Application Support/melty-portable-counter/session.pkl').exists()
        native.close()
        assert not any(name.split('.')[0] in ('OpenGL', 'glfw', 'meltygui_pro') for name in sys.modules)
        print('portable app: frames, settings, cache, checkpoint and cleanup passed')
    ''')
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path), str(application)],
                            close_fds=False, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'portable app: frames' in result.stdout
    assert 'Exception in' not in result.stdout and 'Traceback' not in result.stderr, result.stdout + result.stderr
