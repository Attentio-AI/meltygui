"""`draw_enum` (view/common/enum_view.py): what `draw_any` routes to it, a press on a segment, a
`Flag`, the dropdown of a row that does not fit, and the read only views.

The app is one enum at (10, 10), driven through the GL test host.
"""
from enum import Enum, Flag, IntEnum, IntFlag, StrEnum, auto

import pytest

import meltygui_imgui as imgui
from editor_host import Host
from meltygui.core.rendering.gui_any import default_for
from meltygui.view.common.choice_view import options
from meltygui.view.common.enum_view import draw_enum, label, lit, pick
from meltygui.view.common.value_view import PAD, ROW

LEFT, TOP = 10.0, 10.0


class Mode(Enum):
    FAST = 1
    SLOW_AND_SURE = 2
    OFF = 3


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class Word(StrEnum):
    YES = auto()
    NO = auto()


class Sides(Flag):
    LEFT = auto()
    RIGHT = auto()
    TOP = auto()


class Bits(IntFlag):
    READ = 4
    WRITE = 2


def app(input_value=None, **arguments):
    return draw_enum(input_value, name='enum', left=LEFT, top=TOP, **arguments)


@pytest.fixture
def show():
    hosts = []

    def make(value, **arguments):
        hosts.append(Host(app, input_value=value, **arguments))
        hosts[-1].frame()
        return hosts[-1]
    yield make
    for host in hosts:
        host.close()


def middle(kind, member):
    """The surface position of the middle of `member`'s segment."""
    x = LEFT
    for other in kind:
        wide = imgui.calc_text_size(label(other)).x + 2.0 * PAD
        if other is member:
            return x + wide * 0.5, TOP + ROW * 0.5
        x += wide


def press(host, kind, member):
    """The result of the frame the left button goes down over `member`'s segment."""
    at = middle(kind, member)
    result = host.frame(at, True)
    host.frame(at, False)
    return result


def size(host):
    body = draw_enum.__gui_definition__[0]
    (node,) = [node for node, record in host.cache.records.items() if record[2] is body]
    return tuple(host.cache.graph.rect(node)[2:])


def test_every_kind_of_enum_is_routed_here():
    for value in (Mode.FAST, Level.LOW, Word.YES, Sides.LEFT, Bits.READ):
        assert default_for(value) is draw_enum


def test_labels_and_what_a_press_means():
    assert label(Mode.SLOW_AND_SURE) == 'Slow and sure'
    assert lit(Mode.FAST, Mode.FAST) and not lit(Mode.FAST, Mode.OFF)
    assert pick(Mode.FAST, Mode.OFF) is Mode.OFF
    both = Sides.LEFT | Sides.TOP
    assert lit(both, Sides.TOP) and not lit(both, Sides.RIGHT)
    assert pick(both, Sides.TOP) is Sides.LEFT and pick(both, Sides.RIGHT) == Sides.LEFT | Sides.RIGHT | Sides.TOP


def test_a_press_picks_the_member_under_it(show):
    host = show(Mode.FAST, width=400.0)
    assert press(host, Mode, Mode.OFF) == (True, Mode.OFF)
    assert press(host, Mode, Mode.OFF) == (False, Mode.OFF)             # the member it already is
    assert press(host, Mode, Mode.SLOW_AND_SURE) == (True, Mode.SLOW_AND_SURE)


def test_an_int_enum_stays_one(show):
    host = show(Level.LOW, width=400.0)
    changed, value = press(host, Level, Level.HIGH)
    assert changed and value is Level.HIGH


def test_a_press_flips_a_member_of_a_flag(show):
    host = show(Sides.LEFT, width=400.0)
    assert press(host, Sides, Sides.TOP) == (True, Sides.LEFT | Sides.TOP)
    assert press(host, Sides, Sides.LEFT) == (True, Sides.TOP)
    assert press(host, Sides, Sides.TOP) == (True, Sides(0))


def test_read_only_takes_no_press(show):
    host = show(Mode.FAST, width=400.0, editable=False)
    assert press(host, Mode, Mode.OFF) == (False, Mode.FAST)


def test_a_wrapping_width_is_the_row(show):
    host = show(Mode.FAST, width=None)
    wide = sum(imgui.calc_text_size(label(member)).x + 2.0 * PAD for member in Mode)
    assert size(host) == pytest.approx((wide, ROW), abs=1.0)


def test_a_row_that_does_not_fit_is_a_dropdown(show):
    host = show(Mode.FAST, width=80.0)
    host.click(LEFT + 10.0, TOP + 10.0)
    host.frame()
    body = options.__gui_definition__[0]
    (found,) = [node for node, record in host.cache.records.items() if record[2] is body]
    x, y = host.cache.position(found)
    result = host.frame((x + 10.0, y + 2.0 * ROW + ROW * 0.5), True)
    assert result == (True, Mode.OFF)
