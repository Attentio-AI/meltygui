"""`draw_button` (view/button_view.py): its press, and its size when it wraps, is fixed or fills.

The app is one button at (10, 10), driven through the GL test host.
"""
import pytest

import meltygui_imgui as imgui
from editor_host import Host
from meltygui.view.button_view import draw_button
from meltygui.view.common.value_view import PAD, ROW

LEFT, TOP = 10.0, 10.0


def app(input_value=None, **size):
    return draw_button(input_value, name='button', left=LEFT, top=TOP, **size)


@pytest.fixture
def show():
    hosts = []

    def make(label, **size):
        hosts.append(Host(app, input_value=label, **size))
        hosts[-1].frame()
        return hosts[-1]
    yield make
    for host in hosts:
        host.close()


def size(host):
    body = draw_button.__gui_definition__[0]
    (node,) = [node for node, record in host.cache.records.items() if record[2] is body]
    return tuple(host.cache.graph.rect(node)[2:])


def test_a_wrapping_button_is_as_large_as_its_label(show):
    host = show('Wrap Text button', width=None, height=None)
    label = imgui.calc_text_size('Wrap Text button').x
    assert size(host) == pytest.approx((label + 2.0 * PAD, ROW), abs=1.0)
    host.click(LEFT + 5.0, TOP + 5.0)
    assert host.result[0] is False                      # the release; the press was the frame before
    assert host.frame((LEFT + 5.0, TOP + 5.0), True)[0] is True


def test_a_fixed_size_is_kept(show):
    host = show('Square button', width=100.0, height=100.0)
    assert size(host) == (100.0, 100.0)


def test_one_axis_can_wrap_alone(show):
    host = show('Run', width=80.0, height=None)
    assert size(host) == (80.0, ROW)
