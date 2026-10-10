"""Real ImGui input, private contexts and native texture replay together."""
import subprocess
import sys

import pytest
import meltygui_imgui as imgui
from conftest import _ensure_gl_context
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
from test_retained_gui_gpu import pixels


def test_standard_imgui_import_uses_the_same_native_runtime():
    code = '''
from meltygui import os_window
import sys
assert 'meltygui.core.rendering._gui_native' not in sys.modules
assert 'OpenGL.GL' not in sys.modules
import imgui
import imgui.core
import meltygui_imgui
assert imgui is meltygui_imgui
assert imgui.core is meltygui_imgui.core
assert imgui.__spec__.name == 'meltygui_imgui'
context = imgui.create_context()
assert meltygui_imgui.get_current_context() is context
imgui.destroy_context(context)
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, close_fds=False)
    assert result.returncode == 0, result.stderr


def test_upstream_runtime_loaded_first_is_rejected_explicitly():
    result = subprocess.run([sys.executable, '-c', '''
import sys, types
sys.modules['imgui'] = types.ModuleType('imgui')
try:
    from meltygui import os_window
except ImportError as error:
    assert 'before import imgui' in str(error)
else:
    raise AssertionError('mixed native runtimes must not be silently accepted')
'''], capture_output=True, text=True, close_fds=False)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def cache():
    _ensure_gl_context()
    host = imgui.get_current_context()
    io = imgui.get_io()
    io.mouse_pos = (-100, -100)
    for i in range(len(io.mouse_down)):
        io.mouse_down[i] = False
    for i in range(len(io.keys_down)):
        io.keys_down[i] = False
    io.key_ctrl = io.key_alt = io.key_shift = io.key_super = False
    io.mouse_wheel = io.mouse_wheel_horizontal = 0
    cache = RetainedGui()
    yield cache
    cache.close()
    assert imgui.get_current_context() is host


def frame(cache, position=(-100, -100), *, down=False, text='', keys=(), wheel=0, ctrl=False):
    io = imgui.get_io()
    io.delta_time = 1 / 60
    io.mouse_pos = position
    io.mouse_down[0] = down
    io.mouse_wheel = wheel
    io.key_ctrl = ctrl
    for i in range(len(io.keys_down)):
        io.keys_down[i] = i in keys
    for char in text:
        cache.imgui_input.character(ord(char))
    cache.imgui_input.process(io)
    cache.flush()


def click(cache, position):
    frame(cache, position)
    frame(cache, position, down=True)
    frame(cache, position)


def test_text_measures_draws_and_reuses_cached_texture(cache):
    calls = []

    @cache.gui(width=150, height=None)
    def hello():
        calls.append(cache.current)
        imgui.text('hello')

    hello()
    node = calls[0]
    width, height = cache.graph.info(node)['rect'][2:]
    assert height > 0
    assert pixels(cache.gpu.texture(node), int(width), int(height))[:, :, 3].max() > 0
    frame(cache)
    hello()
    frame(cache)
    assert calls == [node]


def test_button_release_and_capture_outside_view(cache):
    clicks = []

    @cache.gui(width=180, height=80)
    def view():
        if imgui.button('Click', 80, 25):
            clicks.append(True)

    view()
    click(cache, (20, 10))
    assert clicks == [True]
    frame(cache, (20, 10), down=True)
    frame(cache, (220, 100), down=True)
    frame(cache, (220, 100))
    assert clicks == [True]  # release outside cancels, capture does not stick
    click(cache, (20, 10))
    assert clicks == [True, True]


def test_checkbox_slider_and_text_editing(cache):
    model = {'checked': False, 'amount': .1, 'text': ''}
    rects = {}

    @cache.gui(width=300, height=150)
    def controls():
        _, model['checked'] = imgui.checkbox('Enabled', model['checked'])
        rects['check'] = tuple(imgui.get_item_rect_min())
        _, model['amount'] = imgui.slider_float('Amount', model['amount'], 0., 1.)
        rects['slider'] = tuple(imgui.get_item_rect_min())
        _, model['text'] = imgui.input_text('Text', model['text'], 128)
        rects['text'] = tuple(imgui.get_item_rect_min())

    controls()
    click(cache, (8, rects['check'][1] + 8))
    assert model['checked'] is True
    click(cache, (140, rects['slider'][1] + 8))
    assert model['amount'] > .4
    click(cache, (35, rects['text'][1] + 8))
    frame(cache, (35, rects['text'][1] + 8), text='hello')
    assert model['text'] == 'hello'
    cache.invalidate(controls)
    cache.flush()  # same host sample must not duplicate queued characters
    assert model['text'] == 'hello'
    backspace = imgui.get_io().key_map[imgui.KEY_BACKSPACE]
    frame(cache, keys=(backspace,))
    frame(cache)
    assert model['text'] == 'hell'


def test_nested_widget_input_does_not_execute_unrelated_views(cache):
    calls = {'parent': 0, 'child': 0, 'sibling': 0}
    value = [False]
    ids = {}

    @cache.gui(width=160, height=40)
    def child():
        calls['child'] += 1
        ids['child'] = cache.current
        _, value[0] = imgui.checkbox('Child', value[0])

    @cache.gui(width=160, height=40)
    def sibling():
        calls['sibling'] += 1
        imgui.text('unchanged')

    @cache.gui(width=200, height=180)
    def parent():
        calls['parent'] += 1
        imgui.dummy(10, 20)
        child()
        sibling()

    parent()
    x, y = cache._position(ids['child'])
    click(cache, (x + 8, y + 8))
    assert value[0] is True
    assert calls['parent'] == calls['sibling'] == 1
    before = calls.copy()
    frame(cache, (x + 8, y + 8))
    assert calls == before


def test_text_focus_moves_between_cached_contexts_and_retires(cache):
    texts, ids = {'a': '', 'b': ''}, {}

    @cache.gui(width=180, height=35)
    def editor(label='a'):
        ids[label] = cache.current
        _, texts[label] = imgui.input_text('Edit', texts[label], 128)

    @cache.gui(width=200, height=100)
    def parent():
        editor(key='a', label='a')
        editor(key='b', label='b')

    parent()
    a, b = (cache._position(ids[key]) for key in ('a', 'b'))
    click(cache, (a[0] + 10, a[1] + 10))
    frame(cache, text='one')
    click(cache, (b[0] + 10, b[1] + 10))
    frame(cache, text='two')
    assert texts == {'a': 'one', 'b': 'two'}
    assert cache.imgui_input.active == {ids['b']}
    click(cache, (a[0] + 10, a[1] + 10))
    frame(cache, text='X')
    assert len(texts['a']) == 4 and texts['b'] == 'two'
    cache.graph.retire(ids['a'])
    cache._retire()
    assert cache.imgui_input.focus is None
    assert not cache.imgui_input.active


def test_widget_changed_return_is_replayed_without_repeating_click(cache):
    model = {'value': False}
    executions, edits, ids = [], [], []

    @cache.gui(width=160, height=40)
    def editor(input_value):
        ids.append(cache.current)
        result = imgui.checkbox('Enabled', input_value)
        if result[0]:
            edits.append(result[1])
        return result

    @cache.gui(width=200, height=100)
    def parent():
        executions.append(True)
        changed, model['value'] = editor(model['value'])
        imgui.text(str(model['value']))

    parent()
    click(cache, (10, 10))
    assert model['value'] is True and edits == [True]
    assert len(executions) > 1
    before = len(executions)
    frame(cache, (10, 10))
    assert edits == [True] and len(executions) == before


def test_wheel_scrolls_imgui_child(cache):
    scroll = []

    @cache.gui(width=180, height=90)
    def content():
        imgui.begin_child('scroll', 160, 80)
        for i in range(30):
            imgui.text(f'Row {i}')
        scroll.append(imgui.get_scroll_y())
        imgui.end_child()

    content()
    frame(cache, (30, 30))
    frame(cache, (30, 30), wheel=-2)
    assert scroll[-1] > 0


def test_clipboard_paste_uses_the_host_callback(cache):
    value = ['']
    cache.imgui_input.clipboard = (lambda: 'pasted', lambda text: None)

    @cache.gui(width=180, height=40)
    def editor():
        _, value[0] = imgui.input_text('Edit', value[0], 128)

    editor()
    click(cache, (10, 10))
    paste = imgui.get_io().key_map[imgui.KEY_V]
    frame(cache, keys=(paste,), ctrl=True)
    assert value[0] == 'pasted'
    frame(cache)
    assert value[0] == 'pasted'


def test_portal_widgets_occlude_underlying_views(cache):
    clicks = []

    @cache.gui(width=180, height=60)
    def control(label='background'):
        if imgui.button(label, 100, 25):
            clicks.append(label)

    control(label='background')
    control(key='portal', label='portal', melty_window=True,
            initial={'window_pos': (0, 0)})
    click(cache, (10, 38))  # portal content starts after its 28px titlebar
    assert clicks == ['portal']
    assert cache.imgui_input.hit((10, 10)) is None  # titlebar is not a widget


def test_character_callback_delivers_to_its_backend_not_the_current_context():
    from types import SimpleNamespace
    from meltygui.core.windowing.backends.imgui_renderer import WindowRenderer
    host_chars, retained_chars = [], []
    backend = SimpleNamespace(io=SimpleNamespace(add_input_character=host_chars.append),
                              gui_character_callback=retained_chars.append)
    WindowRenderer.char_callback(backend, object(), ord('x'))
    assert host_chars == retained_chars == [ord('x')]
    backend.gui_character_callback = None
    WindowRenderer.char_callback(backend, object(), ord('y'))
    assert host_chars == [ord('x'), ord('y')] and retained_chars == [ord('x')]


def routed_drag(cache, position, button, *, double=False):
    from meltygui.core.input.input_handler import InputHandler
    handler = InputHandler()
    handler.begin_frame()
    action = 'double_dragged' if double else 'dragged'
    handler.register_hovered('os-window', [f'non_blocking_{button}_mouse_{action}'], priority=10000)
    cache.register_host_pointer(handler, position)
    if double:
        handler.feed_down(f'{button}_mouse', *position, t=.8)
        handler.process_frame()
        handler.feed_up(f'{button}_mouse', *position, t=.85)
        handler.process_frame()
    handler.feed_down(f'{button}_mouse', *position, t=1.)
    handler.process_frame()
    handler.feed_move(position[0] + 50, position[1] + 30, t=1.1)
    # The router deliberately uses the latest IO position during a held drag.
    previous = imgui.get_io().mouse_pos
    imgui.get_io().mouse_pos = (position[0] + 50, position[1] + 30)
    try:
        events, _ = handler.process_frame()
    finally:
        imgui.get_io().mouse_pos = previous
    return events


@pytest.mark.parametrize('button,double', [('left', False), ('right', False), ('right', True)])
def test_text_and_empty_cached_space_pass_window_gestures_through(cache, button, double):
    @cache.gui(width=180, height=90)
    def view():
        imgui.text('hello')

    view()
    for position in ((10, 8), (100, 70)):
        frame(cache, position)
        events = routed_drag(cache, position, button, double=double)
        assert 'os-window' in events
        assert ('gui-pointer', id(cache)) not in events


@pytest.mark.parametrize('position',[(10,8),(100,70)])
def test_minimal_native_gui_routes_background_drag_to_host_after_geometry_input(cache,monkeypatch,position):
    from test_gui_collision_layout import attach_native
    @cache.gui(width=180,height=90)
    def view():
        imgui.text('hello')
    view()
    native=attach_native(cache,180,90)
    before=native.last
    # The old routing tests did not attach native geometry or call its input
    # adapter, so they missed the collision graph claiming native background.
    frame(cache,position)
    monkeypatch.setattr(imgui,'is_mouse_clicked',lambda button:button==0)
    monkeypatch.setattr(imgui,'is_mouse_down',lambda button:button==0)
    monkeypatch.setattr(imgui,'is_mouse_released',lambda button:False)
    monkeypatch.setattr(imgui,'is_mouse_double_clicked',lambda button:False)
    cache.process_host_input(drag_position=position)
    events=routed_drag(cache,position,'left')
    assert 'os-window' in events
    assert ('gui-pointer',id(cache)) not in events
    assert cache.geometry.gesture is None
    assert native.last==before


@pytest.mark.parametrize('button,double,claimed', [('left', False, True),
                                                   ('right', False, False),
                                                   ('right', True, False)])
def test_widget_keeps_left_drag_but_does_not_swallow_native_resize(cache, button, double, claimed):
    @cache.gui(width=180, height=90)
    def view():
        imgui.button('Control', 100, 30)

    view()
    frame(cache, (10, 10))
    events = routed_drag(cache, (10, 10), button, double=double)
    assert (('gui-pointer', id(cache)) in events) is claimed
    assert ('os-window' in events) is not claimed


def test_leaving_widget_does_not_leave_cached_background_claimed(cache):
    calls = []
    @cache.gui(width=180, height=90)
    def view():
        calls.append(1)
        imgui.button('Control', 100, 30)

    view()
    frame(cache, (10, 10))
    frame(cache, (150, 70))
    frame(cache, (150, 70))  # settle ImGui's previous-frame hovered ID
    assert 'os-window' in routed_drag(cache, (150, 70), 'left')
    count = len(calls)
    frame(cache, (150, 70))
    assert len(calls) == count  # settling does not turn into continuous invalidation


def test_retained_region_claims_press_before_native_background_move(cache,monkeypatch):
    from test_gui_collision_layout import attach_native
    received=[]
    @cache.gui(width=180,height=90)
    def view(view_events=None):
        cache.region('click',(0,0,100,30))
        if view_events.get('click'):received.append(view_events['click'])
    view()
    native=attach_native(cache,180,90)
    before=native.last
    imgui.get_io().mouse_pos=(10,10)
    imgui.get_io().mouse_down[0]=True
    monkeypatch.setattr(imgui,'is_mouse_clicked',lambda button:button==0)
    monkeypatch.setattr(imgui,'is_mouse_down',lambda button:button==0)
    monkeypatch.setattr(imgui,'is_mouse_released',lambda button:False)
    monkeypatch.setattr(imgui,'is_mouse_double_clicked',lambda button:False)
    cache.process_host_input(drag_position=(10,10))
    cache.flush()
    assert cache.geometry.gesture is None
    assert native.last==before
    assert len(received)==1


def test_active_slider_keeps_left_drag_when_pointer_leaves_its_view(cache):
    value = [.5]
    @cache.gui(width=180, height=60)
    def view():
        _, value[0] = imgui.slider_float('Value', value[0], 0., 1.)

    view()
    frame(cache, (20, 10))
    frame(cache, (20, 10), down=True)
    frame(cache, (220, 90), down=True)
    assert ('gui-pointer', id(cache)) in routed_drag(cache, (220, 90), 'left')
    frame(cache, (220, 90))
    frame(cache, (220, 90))
    assert 'os-window' in routed_drag(cache, (220, 90), 'left')


def test_keyboard_focus_does_not_claim_a_later_right_drag(cache):
    @cache.gui(width=180, height=90)
    def view():
        imgui.input_text('Editor', '', 128)

    view()
    click(cache, (10, 10))
    io = imgui.get_io()
    io.mouse_down[1] = True
    try:
        frame(cache, (150, 70))
        assert 'os-window' in routed_drag(cache, (150, 70), 'right')
    finally:
        io.mouse_down[1] = False


def test_held_keys_follow_focus_and_release_in_cached_contexts(cache):
    ids, observed = {}, {}

    @cache.gui(width=160, height=40)
    def child(label='a'):
        ids[label] = cache.current
        io = imgui.get_io()
        observed[label] = (bool(io.keys_down[65]), bool(io.key_ctrl), io.key_map[imgui.KEY_A])
        imgui.text(label)

    @cache.gui(width=200, height=120)
    def parent():
        child(key='a', label='a')
        child(key='b', label='b')

    parent()
    a, b = (cache._position(ids[key]) for key in ('a', 'b'))
    pa, pb = (a[0] + 8, a[1] + 8), (b[0] + 8, b[1] + 8)
    frame(cache, pa, down=True, keys=(65,), ctrl=True)
    assert observed['a'][:2] == (True, True)
    assert observed['b'][:2] == (False, False)
    frame(cache, pb, keys=(65,), ctrl=True)
    frame(cache, pb, down=True, keys=(65,), ctrl=True)
    assert observed['a'][:2] == (False, False)
    assert observed['b'][:2] == (True, True)
    frame(cache, pb)
    assert observed['b'][:2] == (False, False)
    io = imgui.get_io()
    original = io.key_map[imgui.KEY_A]
    try:
        io.key_map[imgui.KEY_A] = 66
        for node in ids.values():
            cache.invalidate_id(node)
        frame(cache, pb)
        assert observed['a'][2] == observed['b'][2] == 66
    finally:
        io.key_map[imgui.KEY_A] = original
    cache.graph.retire(ids['b'])
    cache._retire()
    assert ids['b'] not in cache.imgui_input.delivered_keys
    assert ids['b'] not in cache.imgui_input.applied_key_maps
