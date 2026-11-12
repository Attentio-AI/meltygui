"""A view follows its caller like an immediate mode widget, however it is hosted.

A retained view keeps the edit it returned as its live input, so the caller
has time to pass it back. A caller that still passes its old value `SETTLE`
frames on is run again and what it passes is shown (retained.rs, `settle`).
The cases below are the same for a root, a view under retained parents, a
Melty window (a portal, drawn deferred) and Melty windows inside each other:
after a few frames the value shown is the one the caller passes.
"""
import pytest

from meltygui.core.rendering.retained_gui_prototype import RetainedGui

SETTLE = RetainedGui(graphics=False).graph.SETTLE
# Frames after which every case below must have settled: the wait, the caller's run, the view's.
LATER = SETTLE + 3


class Rig:
    """A caller chain around one editable `field`, driven frame by frame.

    `shape` is where the field stands: 'root', 'child' (under a retained
    panel), 'deep' (two panels), 'window' (a Melty window declared by a
    panel), 'nested' (a Melty window in a Melty window), 'scroll' (a tile of
    a scrolling panel). The value flows down as each view's input from an
    uncached root, as in an app; `take(value)` is the caller's policy for a
    returned edit: it returns what the caller holds from then on.
    """
    def __init__(self, shape, value, take):
        self.cache = cache = RetainedGui(graphics=False)
        self.value, self.take = value, take
        self.seen, self.runs = [], {'field': 0, 'panel': 0, 'outer': 0}
        rig = self

        @cache.gui(width=120, height=30)
        def field(input_value, view_events=None):
            rig.runs['field'] += 1
            rig.seen.append(input_value)
            if 'edit' in view_events:
                return True, view_events['edit'](input_value)
            return False, input_value

        @cache.gui(width=300, height=200, scroll=shape == 'scroll')
        def panel(input_value):
            rig.runs['panel'] += 1
            return field(input_value, key='field', melty_window=shape in ('window', 'nested'), left=4, top=4)

        @cache.gui(width=400, height=300)
        def outer(input_value):
            rig.runs['outer'] += 1
            return panel(input_value, key='panel', melty_window=shape == 'nested', left=4, top=4)

        self.root = {'root': field, 'child': panel, 'scroll': panel, 'window': panel}.get(shape, outer)
        self.frame()

    def node(self, name='field'):
        return next(node for node, record in self.cache.records.items() if record[2].__name__ == name)

    def frame(self, count=1):
        """Frames of an app: the root body runs each one and passes what the caller holds."""
        for _ in range(count):
            with self.cache.frame():
                changed, value = self.root(self.value)
                if changed:
                    self.value = self.take(value)
            self.cache.flush()
        # What the view holds: its last return, which is also what it paints from.
        return self.cache.graph.result(self.node(), False)[1]

    def edit(self, change):
        self.cache.send(self.node(), 'edit', change)
        return self.frame()

    def idle(self):
        """No frame is owed and a further frame runs nothing."""
        before = dict(self.runs)
        owed = self.cache.graph.settle()
        self.frame(2)
        return not owed and self.runs == before

    def close(self):
        self.cache.close()


SHAPES = ['root', 'child', 'deep', 'window', 'nested', 'scroll']
plus = lambda value: value + 1


@pytest.fixture
def rig(request):
    made = []
    yield lambda *arguments: made.append(Rig(*arguments)) or made[-1]
    for one in made:
        one.close()


@pytest.mark.parametrize('shape', SHAPES)
def test_a_caller_that_takes_the_edit_keeps_it_and_nothing_runs_again(rig, shape):
    rig = rig(shape, 10, lambda value: value)
    assert rig.edit(plus) == 11
    assert rig.frame(LATER) == 11 and rig.value == 11
    assert rig.idle()


@pytest.mark.parametrize('shape', SHAPES)
def test_a_caller_that_keeps_passing_its_value_is_followed_within_a_few_frames(rig, shape):
    rig = rig(shape, 10, lambda value: 10)
    assert rig.edit(plus) == 11          # shown at once: the caller may not have run yet
    assert rig.frame(LATER) == 10        # the caller never took it: its value shows
    assert rig.idle()
    # And again from there: the view is not left in a state of its own.
    assert rig.edit(plus) == 11
    assert rig.frame(LATER) == 10 and rig.idle()


@pytest.mark.parametrize('shape', SHAPES)
def test_edits_frame_after_frame_are_never_lost_to_a_caller_that_is_behind(rig, shape):
    rig = rig(shape, 0, lambda value: 0)
    for step in range(1, 9):
        assert rig.edit(plus) == step    # typing: each edit goes on from the last, the wait starts again
    assert rig.frame(LATER) == 0 and rig.idle()


@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('delay', [1, SETTLE - 1, SETTLE + 2])
def test_a_caller_that_takes_the_edit_late_ends_on_the_edit(rig, shape, delay):
    held = []
    rig = rig(shape, 10, lambda value: held.append(value) or 10)
    assert rig.edit(plus) == 11
    shown = [rig.frame() for _ in range(delay)]
    if delay < SETTLE:
        assert set(shown) == {11}        # inside the wait nothing flickers
    rig.value = held[0]                  # a model that answered late
    assert rig.frame(LATER) == 11 and rig.idle()


@pytest.mark.parametrize('shape', SHAPES)
def test_a_caller_that_changes_the_edit_is_shown_its_own_value(rig, shape):
    rig = rig(shape, 10, lambda value: value * 2)
    assert rig.edit(plus) == 11
    assert rig.frame(LATER) == 22 and rig.idle()


@pytest.mark.parametrize('shape', SHAPES)
def test_a_caller_replacing_the_value_is_followed_at_once(rig, shape):
    rig = rig(shape, 10, lambda value: 10)
    rig.edit(plus)
    rig.value = 50
    assert rig.frame() == 50
    assert rig.frame(LATER) == 50 and rig.idle()


@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('value,change', [('text', lambda value: value + '!'), (1.5, lambda value: value * 2),
                                          (True, lambda value: not value), ((1, 2), lambda value: value + (3,)),
                                          ([1, 2], lambda value: value + [3])])
def test_every_kind_of_value_follows_its_caller(rig, shape, value, change):
    static = rig(shape, value, lambda edited: value)
    assert static.edit(change) == change(value)
    assert static.frame(LATER) == value and static.idle()
    taking = rig(shape, value, lambda edited: edited)
    taking.edit(change)
    assert taking.frame(LATER) == change(value) and taking.idle()


@pytest.mark.parametrize('shape', SHAPES)
def test_a_value_edited_in_place_is_the_callers_own_object_and_stays(rig, shape):
    model = {'count': 1}
    rig = rig(shape, model, lambda value: model)

    def bump(value):
        value['count'] += 1
        return value
    assert rig.edit(bump) is model
    assert rig.frame(LATER) is model and model == {'count': 2} and rig.idle()


@pytest.mark.parametrize('shape', ['child', 'deep', 'window', 'nested', 'scroll'])
def test_a_parent_that_keeps_the_edit_in_a_model_is_not_reverted(rig, shape):
    """The parents do not run every frame. One that wrote the edit into its
    model passes the model's value when it is asked again: the edit stays."""
    model = {'name': 'a'}
    cache = RetainedGui(graphics=False)
    runs = []

    @cache.gui(width=120, height=30)
    def field(input_value, view_events=None):
        if 'edit' in view_events:
            return True, input_value + 'b'
        return False, input_value

    @cache.gui(width=300, height=200, scroll=shape == 'scroll')
    def panel(input_value):
        runs.append(input_value['name'])
        changed, input_value['name'] = field(input_value['name'], melty_window=shape in ('window', 'nested'))
        return changed, input_value

    @cache.gui(width=400, height=300)
    def outer(input_value):
        return panel(input_value, melty_window=shape == 'nested')

    root = panel if shape != 'deep' and shape != 'nested' else outer
    try:
        def frame(count=1):
            for _ in range(count):
                with cache.frame():
                    root(model)
                cache.flush()
        frame()
        node = next(node for node, record in cache.records.items() if record[2].__name__ == 'field')
        cache.send(node, 'edit')
        frame(LATER)
        assert model == {'name': 'ab'} and cache.graph.result(node, False)[1] == 'ab'
        before = len(runs)
        frame(3)
        assert len(runs) == before and not cache.graph.settle()
    finally:
        cache.close()


def test_two_views_of_one_value_agree_after_an_edit_the_caller_did_not_take():
    cache = RetainedGui(graphics=False)
    shown = {}

    @cache.gui(width=120, height=30)
    def field(input_value, view_events=None, name=None):
        shown[name] = input_value + ('edit' in view_events)     # what it holds after this run
        return ('edit' in view_events), shown[name]

    try:
        def frame(count=1):
            for _ in range(count):
                with cache.frame():
                    field(5, name='a')
                    field(5, name='b', left=200)
                cache.flush()
        frame()
        first = min(cache.records)
        cache.send(first, 'edit')
        frame()
        assert sorted(shown.values()) == [5, 6]
        frame(LATER)
        assert shown == {'a': 5, 'b': 5} and not cache.graph.settle()
    finally:
        cache.close()


def test_a_view_no_longer_declared_does_not_keep_frames_coming():
    cache = RetainedGui(graphics=False)

    @cache.gui(width=120, height=30)
    def field(input_value, view_events=None):
        return ('edit' in view_events), input_value + ('edit' in view_events)

    try:
        with cache.frame():
            field(1)
        cache.flush()
        cache.send(min(cache.records), 'edit')
        cache.flush()
        for _ in range(LATER):
            with cache.frame():
                pass                 # the root is gone
            cache.flush()
        assert not cache.graph.settle()
    finally:
        cache.close()
