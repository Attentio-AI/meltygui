"""gui_module: a standalone ImGui app's source split into load, draw and terminate."""
import threading

import pytest

from meltygui.core.rendering.gui_module import App, SplitError, gui_module, split
from test_gui_window_prototype import host

# The loop in a thread's target, cleanup in a finally, the main thread joining.
THREADED = '''
import atexit, signal, threading
import glfw, imgui

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
            imgui.new_frame()
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
    """Loaded and run to its first event wait: the next step is its first frame."""
    app = App(split(source))
    app.start(args)
    assert app.error is None and app.loaded
    app.step()
    assert app.error is None
    return app


def test_threaded_app_splits_into_load_draw_and_terminate():
    program = split(THREADED)
    assert program.parameters == ('input_value', 'log', 'thread') and program.first == 'input_value'
    app = started(THREADED, input_value='passed')
    log = app.namespace['log']
    assert log == []                        # load and setup ran, not a frame
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
    assert not app.frames and app.namespace['done'] == 3      # the code after the loop ran


def test_loop_in_a_method_behind_the_main_guard():
    app = started(METHOD)
    app.step()
    app.step()
    assert app.namespace['game'].ticks == 2


def test_values_route_to_module_globals_by_name_and_position(monkeypatch, tmp_path):
    from meltygui.core.runtime.app import _root_body
    path = tmp_path / 'named.py'
    path.write_text('''
import glfw, imgui
label = "a"
size = 1
seen = []
while True:
    glfw.poll_events()
    imgui.new_frame()
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


def test_meltygui_app_window_function_is_the_frame(monkeypatch, tmp_path):
    from meltygui.core.runtime.app import _root_body
    path = tmp_path / 'melty_app.py'
    path.write_text('''
import meltygui
from meltygui import os_window
text = "own"
seen = []
closed = []

@os_window(name="Demo", app_id="demo", width=900, height=700, on_close=lambda: closed.append(True))
def app(width=None, height=None):
    global text
    seen.append((text, width, height))
    if text == "edit":
        text = "edited"

meltygui.run()
''')
    window, surface = host(monkeypatch)
    view = gui_module(path, background=False)

    def call(value=None):
        result = _root_body(view, 'melty root', config={'view_kwargs': {'value': value}})(surface)
        return result, next(iter(window.cache.bindings.values())).state.app

    _, app = call()
    seen, closed = app.namespace['seen'], app.namespace['closed']
    assert seen == [('own', 400, 250)]              # the window function fills the view
    result, _ = call('edit')
    assert result == (True, 'edited') and seen[-1][0] == 'edit'
    window.close()
    assert closed == [True]


def test_style_colors_set_during_setup_are_not_popped_before_they_were_pushed():
    app = App(split('''
import glfw, imgui
def main():
    imgui.get_style().colors[imgui.COLOR_TEXT] = (1, 1, 1, 1)
    while True:
        glfw.poll_events()
        imgui.new_frame()
main()
'''))
    app.start({})
    app.draw(None, None)        # setup sets a colour, then the first frame: nothing was pushed yet
    assert app.error is None and len(app.colors) == 1


def test_windowing_calls_that_do_nothing_are_compiled_away():
    import dis
    program = split('''
import glfw, imgui
import OpenGL.GL as gl
frames = 0
def main():
    global frames
    window = glfw.create_window(1, 1, "x", None, None)
    glfw.make_context_current(window)
    while not glfw.window_should_close(window):
        glfw.poll_events()
        imgui.new_frame()
        frames += 1
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        imgui.render()
        glfw.swap_buffers(window)
    glfw.terminate()
main()
''')
    main = next(const for const in program.code.co_consts if getattr(const, 'co_name', '') == 'main')
    called = {instruction.argval for instruction in dis.get_instructions(main) if instruction.opname == 'LOAD_ATTR'}
    assert called == {'create_window', 'closing'}       # the one call whose answer the app keeps
    app = App(program)
    app.start({})
    assert app.draw(None, None) == 0 and app.draw(None, None) == 0
    assert app.namespace['frames'] == 2 and app.error is None
    namespace = app.namespace
    app.close()
    assert app.namespace is None


def test_a_wait_reached_through_another_name_goes_through_its_shim():
    app = App(split('''
import glfw, imgui
backend = glfw
count = 0
while not backend.window_should_close(None):
    backend.wait_events_timeout(0.25)
    imgui.new_frame()
    count += 1
'''))
    app.start({})
    assert app.draw(None, None) == 0.25 and app.draw(None, None) == 0.25
    assert app.namespace['count'] == 2 and app.wait is None


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
    app.step()
    assert 'ZeroDivisionError' in app.error and not app.frames


def test_no_event_wait_is_an_error():
    app = App(split('x = 1'))
    app.start({})
    assert 'nothing to draw' in app.error
    with pytest.raises(SplitError, match='plain function or at module level'):
        split('import glfw\npump = lambda: glfw.poll_events()\n')


def test_event_driven_loop_draws_on_refresh_and_asks_for_its_timed_wait():
    app = App(split('''
import glfw, imgui
wake = 1
drawn = []
def woke(*_):
    global wake
    wake = 2
glfw.set_window_refresh_callback(None, woke)
while not glfw.window_should_close(None):
    if wake > 0:
        glfw.poll_events()
    else:
        glfw.wait_events_timeout(0.5)
    if wake <= 0:
        continue
    wake -= 1
    imgui.new_frame()
    drawn.append(wake)
closed = True
'''))
    assert app.program.blocks
    app.start({})
    drawn = app.namespace['drawn']
    assert app.draw(None, None) == 0    # setup drew nothing: refreshed, then a frame; it polls next
    assert drawn == [1]
    assert app.draw(None, None) == 0.5  # its own next frame; then it waits
    assert drawn == [1, 0]
    assert app.draw(None, None) == 0    # idle: nothing to draw, so it is refreshed and draws
    assert drawn == [1, 0, 1]
    namespace = app.namespace
    app.close()
    assert not app.frames and app.namespace is None


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
