"""Header locate edits the native app's real decorator, through save and hotswap."""
import plistlib
import subprocess
import sys
import uuid


APP_SOURCE = '''from meltygui import glfw_window, render_func
from meltygui.view.header_view import draw_header

SEEN = {}

@glfw_window(name='Tint check', app_id='tint-check', tint=(0.32, 0.42, 0.54), with_header=draw_header)
@render_func()
def main(input_value: object, draw_state):
    SEEN['ds'] = draw_state
    return False, input_value
'''


PROGRAM = r'''
import ast
import inspect
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import meltygui
from meltygui.platforms.ios.compile_shaders import generate
from meltygui.platforms.ios.Python import melty_ios_bootstrap as bootstrap

sandbox = Path(sys.argv[1])
phase = sys.argv[2]
bundle = sandbox / 'Melty.app'
generate(Path(meltygui.__file__).parent, sandbox / 'shaders')
manifest = (sandbox / 'shaders/metal-programs.json').read_text()

class GPU:
    next_texture = 0
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
    def mesh(self, *args): pass

sys.modules['_melty_ios'] = SimpleNamespace(
    request_frame=lambda: None, set_safe_zone=lambda top, bottom: None,
    set_keyboard_visible=lambda value: None, get_clipboard_text=lambda: '',
    set_clipboard_text=lambda value: None, write_log=sys.__stdout__.write)
sys.modules['_melty_metal'] = GPU()
Path.home = classmethod(lambda cls: sandbox / 'data')
sys.platform = 'ios'
# Mirror the native host's explicit PyConfig app root; the installed toolkit
# and dependencies still import normally, outside this simulated app bundle.
sys.path.append(str(bundle / 'app'))
bootstrap.__file__ = str(bundle / 'host/melty_ios_bootstrap.py')
bootstrap.initialize(dict(
    documents=str(Path.home() / 'Documents'), workspace=str(Path.home() / 'Documents/Projects'),
    application_support=str(Path.home() / 'Library/Application Support'),
    cache=str(Path.home() / 'Library/Caches'), entry_module='main', renderer_available=True))

import main
from meltygui.code.fileref import is_editable_source, is_writable_file
from meltygui.core.runtime import app
from meltygui.core.rendering.parameter_core import get_source_for, _sources_for
from meltygui.editor.pending_save import PendingSave

source = Path(inspect.getsourcefile(inspect.unwrap(main.main)))
assert source == Path(main.__file__)
assert not source.is_relative_to(bundle)
assert is_editable_source(source) and is_writable_file(source)
assert not is_editable_source(bundle / 'app/main.py')
assert not is_writable_file(bundle / 'app/main.py')
original_fn, config = app._ROOTS[0]
expected = (0.1, 0.2, 0.3) if phase == 'relaunch' else (0.32, 0.42, 0.54)
assert config['view_kwargs']['tint'] == expected, (phase, config['view_kwargs']['tint'], expected)

def frame(events=()):
    now = time.monotonic()
    bootstrap.frame(dict(width=640, height=480, scale=1, now=now,
                         presentation_time=now+1/60), list(events))
    bootstrap.presented()

frame()
ds = main.SEEN['ds']
deadline = time.monotonic() + 15
while get_source_for('tint', ds) != '@glfw_window(main)':
    assert time.monotonic() < deadline, _sources_for(ds)['kinds']
    frame()
    time.sleep(.02)
sources = _sources_for(ds)
assert sources['locations']['@glfw_window(main)'][0] == str(source)
assert tuple(sources['sources']['@glfw_window(main)']['tint']) == expected

def source_tint(text):
    function = next(node for node in ast.parse(text).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'main')
    decorator = next(node for node in function.decorator_list
                     if isinstance(node, ast.Call) and node.func.id == 'glfw_window')
    return ast.literal_eval(next(kw.value for kw in decorator.keywords if kw.arg == 'tint'))

if phase == 'edit':
    # A native picker drag previews immediately and commits its source edit
    # on touch release, through the shared deferred-write lifecycle.
    frame([dict(kind='touch_begin', touch_id=1, x=320, y=240)])
    ds.locate_tint = (0.1, 0.2, 0.3)
    assert config['view_kwargs']['tint'] == (0.1, 0.2, 0.3)
    frame([dict(kind='touch_end', touch_id=1, x=320, y=240)])
    deadline = time.monotonic() + 15
    while (source_tint(PendingSave.current_file_text(source)) != (0.1, 0.2, 0.3)
           or ds._sa_recompile is not None):
        assert time.monotonic() < deadline, PendingSave.current_file_text(source)
        get_source_for('tint', ds)
        ds.locate_tint  # The picker reads this each frame, driving its hotswap.
        frame()
        time.sleep(.02)
    # Hotswap keeps the live root callback and its config, then draws the tint.
    for _ in range(5):
        frame()
        time.sleep(.02)
    assert len(app._ROOTS) == 1
    assert app._ROOTS[0][0] is original_fn
    assert app._ROOTS[0][1] is config
    assert main.SEEN['ds'] is ds
    assert tuple(ds._kwargs['tint']) == (0.1, 0.2, 0.3)
# Source edits join the ordinary pending-save buffer and persist at checkpoint.
bootstrap.suspend()
assert source_tint(source.read_text(encoding='utf-8')) == ((0.1, 0.2, 0.3) if phase == 'edit' else expected)
bootstrap.close()
print('header tint: decorator, source save, identity and ' + phase + ' passed')
'''


def test_ios_header_tint_saves_to_decorator_and_resets_only_on_rebuild(tmp_path):
    bundle = tmp_path / 'Melty.app'
    (bundle / 'app').mkdir(parents=True)
    source = bundle / 'app/main.py'
    source.write_text(APP_SOURCE, encoding='utf-8')
    settings = bundle / 'HostSettings.plist'
    for phase in ('edit', 'relaunch', 'rebuild'):
        if phase != 'relaunch':
            settings.write_bytes(plistlib.dumps({
                'entry_module': 'main', 'source_generation': uuid.uuid4().hex}))
        # The iOS PyConfig disables bytecode caches; edits can keep the same
        # file size and occur within one timestamp-cache second.
        result = subprocess.run([sys.executable, '-B', '-u', '-c', PROGRAM, str(tmp_path), phase],
                                close_fds=False, text=True, capture_output=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'header tint:' in result.stdout
        assert 'Traceback' not in result.stderr and 'Exception in' not in result.stdout, result.stdout + result.stderr
        assert source.read_text(encoding='utf-8') == APP_SOURCE
