"""`Readers`: a model's worker wakes the views that read its values (hidden GL window, reserved desktop)."""
import threading

import meltygui_imgui as imgui

from editor_host import Host
from meltygui.core.rendering.gui_prototype import gui
from meltygui.core.rendering.gui_readers import Readers


class Model:
    def __init__(self):
        self.readers, self.held = Readers(), 'old'

    @property
    def value(self):
        self.readers.note()
        return self.held

    def swap(self, value):
        self.held = value
        self.readers.wake()


@gui(width=120, height=30)
def leaf(input_value: str):
    imgui.text(input_value)


def test_a_cached_view_runs_again_when_a_worker_swaps_the_value_it_read():
    model, ran = Model(), []

    @gui(width=300, height=200)
    def root(input_value, width=0., height=0.):
        ran.append(model.value)
        leaf(model.value, name='leaf')

    host = Host(root, input_value=None, name='root')
    try:
        host.frame()
        assert ran == ['old'], 'a cached view does not run on a frame nothing asked it for'
        worker = threading.Thread(target=model.swap, args=('new',))
        worker.start()
        worker.join()
        host.frame()
        host.frame()
        assert ran == ['old', 'new'], 'the reader ran once more and passed the new value on'
        assert len(model.readers.found) == 1, 'noted once, however often it reads'
    finally:
        host.close()
    model.swap('late')
    assert not model.readers.found, 'a reader whose host closed is forgotten'


def test_reading_outside_a_view_notes_nothing():
    model = Model()
    assert model.value == 'old' and not model.readers.found
    model.swap('new')       # nothing to wake: only a frame is asked for
    assert model.value == 'new'
