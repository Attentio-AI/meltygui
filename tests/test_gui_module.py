"""gui_module: a standalone ImGui app's source split into load, draw and terminate."""
import threading

import pytest

from meltygui.core.rendering.gui_module import App, SplitError, gui_module, split
from test_gui_window_prototype import host

# The loop in a thread's target, cleanup in a finally, the main thread joining.
THREADED = '''
import atexit, signal, threading
import glfw

input_value = "hello"
log = []

def draw():
    global input_value
    log.append(input_value)
    if input_value == "edit":
        input_value = "edited"

def render_loop():
    window = glfw.create_window(1, 1, "x", None, None)
    frames = 0
    try:
        while not glfw.window_should_close(window):
            glfw.poll_events()
            frames += 1
            draw()
            glfw.swap_buffers(window)
    finally:
        log.append(("cleanup", frames))

atexit.register(lambda: log.append("atexit"))
thread = threading.Thread(target=render_loop)
thread.start()
signal.signal(signal.SIGINT, lambda *_: None)
thread.join()
log.append("joined")
'''

TOP_LEVEL = '''
import glfw
count = 0
window = glfw.create_window(1, 1, "x", None, None)
while not glfw.window_should_close(window):
    glfw.poll_events()
    count += 1
    if count == 3:
        break
done = count
'''

METHOD = '''
import glfw

class Game:
    ticks = 0
    def run(self):
        while True:
            glfw.poll_events()
            self.ticks += 1

def main():
    global game
    game = Game()
    game.run()

if __name__ == "__main__":
    main()
'''


def started(source, **args):
    app = App(split(source))
    app.start(args)
    assert app.error is None and app.loaded
    return app


def test_threaded_app_splits_into_load_draw_and_terminate():
    program = split(THREADED)
    assert program.parameters == {'input_value': 'hello', 'log': []}
    app = started(THREADED, input_value='passed')
    log = app.namespace['log']
    assert log == []                        # load ran the module, not the loop
    app.step()
    app.namespace['input_value'] = 'again'
    app.step()
    assert log == ['passed', 'again']
    app.close()
    assert log[2:] == [('cleanup', 2), 'joined', 'atexit']
    assert app.namespace is None and not app.frames and not app.loaded


def test_top_level_loop_keeps_module_globals_and_can_end_itself():
    app = started(TOP_LEVEL)
    for _ in range(5):
        app.step()
    assert not app.frames and app.namespace['done'] == 3


def test_loop_in_a_method_behind_the_main_guard():
    app = started(METHOD)
    app.step()
    app.step()
    assert app.namespace['game'].ticks == 2


def test_values_route_to_module_globals_by_name_and_position(monkeypatch, tmp_path):
    from meltygui.core.runtime.app import _root_body
    path = tmp_path / 'named.py'
    path.write_text('''
import glfw
label = "a"
size = 1
seen = []
while True:
    glfw.poll_events()
    seen.append((label, size))
    if size == 5:
        label = "five"
''')
    window, surface = host(monkeypatch)
    view = gui_module(path, background=False)

    def call(**kwargs):
        return _root_body(view, 'named root', config={'view_kwargs': kwargs})(surface)

    call(size=3)
    call(value='b', size=4)             # the positional value is the first global, label
    assert call(size=5) == (True, 'five')
    app = next(iter(window.cache.bindings.values())).state.app
    assert app.namespace['seen'] == [('a', 3), ('b', 4), ('b', 5)]
    window.close()


def test_instances_do_not_share_globals():
    first, second = started(TOP_LEVEL), started(TOP_LEVEL)
    first.step()
    assert (first.namespace['count'], second.namespace['count']) == (1, 0)


def test_other_threads_are_real_threads():
    app = started('''
import threading, glfw
seen = []
worker = threading.Thread(target=lambda: seen.append(threading.current_thread().name), name="worker")
worker.start()
worker.join()
while True:
    glfw.poll_events()
''')
    assert app.namespace['seen'] == ['worker']


def test_errors_are_kept_for_the_view():
    app = App(split('import glfw\nwhile True:\n    glfw.poll_events()\n    1 / 0\n'))
    app.start({})
    app.step()
    assert 'ZeroDivisionError' in app.error and not app.frames


@pytest.mark.parametrize('source', ['x = 1', '''
import glfw
while True:
    glfw.poll_events()
while True:
    glfw.swap_buffers(None)
'''])
def test_no_single_frame_loop_is_an_error(source):
    with pytest.raises(SplitError, match='expected one frame loop'):
        split(source)


def test_view_overrides_returns_and_terminates(monkeypatch, tmp_path):
    from meltygui.core.runtime.app import _root_body
    path = tmp_path / 'threaded.py'
    path.write_text(THREADED)
    window, surface = host(monkeypatch)
    view = gui_module(path, background=False)
    logs = []

    def call(value=None):
        config = {'view_kwargs': {'value': value}}
        result = _root_body(view, 'module root', config=config)(surface)
        app = next(iter(window.cache.bindings.values())).state.app
        logs.append(app.namespace['log'])
        return result

    assert call('passed') in (None, (False, 'passed'))
    assert call('edit') == (True, 'edited')
    assert logs[-1] == ['passed', 'edit']
    window.close()
    assert logs[-1][2:] == [('cleanup', 2), 'joined', 'atexit']
