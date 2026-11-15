"""A call that differs from the remembered one only in the parameters exempt from a new
declaration (`PLACED` in retained_gui_prototype.py) is placed again. It has to give what the full
resolution gives: the same rectangle, arguments and body runs."""
from collections import Counter

import pytest

import meltygui.core.rendering.retained_gui_prototype as retained
from meltygui.core.rendering.retained_gui_prototype import PLACED, RetainedGui

# Each step changes what the parent passes to its child.
STEPS = (
    dict(width=220.0),                              # a resize
    dict(left=40.0, top=12.0),                      # a move
    dict(width=0.2),                                # no room left: floored
    dict(width=180.0, height=5.0),                  # under min_height: clamped
    dict(label='other'),                            # not exempt: declared anew
    dict(width=181.0, label='third'),               # both kinds at once
    dict(height=None),                              # left to the view: declared anew
    dict(height=64.0),
)


def run(exempt, monkeypatch):
    """The child after each step, and which steps placed it again without a full declaration."""
    again = []
    settle = retained._settle
    def spy(func, state, caller, initial, *rest):
        if func.__name__ == 'child':
            again.append(initial is retained.NOTHING)       # only the short path passes this one
        return settle(func, state, caller, initial, *rest)
    monkeypatch.setattr(retained, '_settle', spy)
    cache = RetainedGui(graphics=False)
    cache.declared.exempt(exempt)
    calls = Counter()
    ids = {}
    passed = dict(left=10.0, top=5.0, width=200.0, height=50.0, label='first')

    @cache.gui(min_height=20)
    def child(input_value, label='', width=0.0, height=0.0):
        calls['child'] += 1
        ids['child'] = cache.current
        return False, input_value

    @cache.gui(width=400, height=300)
    def parent(input_value):
        calls['parent'] += 1
        child('value', name='child', **passed)
        return False, input_value

    seen = []
    try:
        parent(None)
        for step in STEPS:
            passed.update(step)
            cache.invalidate(parent)
            parent(None)
            cache.flush()
            node = ids['child']
            arguments = {name: value for name, value in cache.invocations[node][1].items()}
            seen.append((dict(calls), cache.graph.info(node)['rect'], arguments))
        passed.update(width=float('nan'))
        cache.invalidate(parent)
        with pytest.raises(ValueError, match='child'):
            parent(None)
    finally:
        cache.close()
    return seen, again[1:len(STEPS) + 1]


def test_a_view_placed_again_is_what_a_full_declaration_makes_it(monkeypatch):
    placed, short = run(PLACED, monkeypatch)
    full, never = run((), monkeypatch)
    assert placed == full
    assert short == [True, True, True, True, False, False, False, False]
    assert never == [False] * len(STEPS)
    assert placed[0][1] == (10.0, 5.0, 220.0, 50.0)
    assert placed[1][1] == (40.0, 12.0, 220.0, 50.0) and placed[1][0] == placed[0][0] | {'parent': 3}
    assert placed[2][1][2] == 1.0
    assert placed[3][1][3] == 20.0
