"""Rollback controls for the isolated empty-layout transaction experiment."""
import pytest

from meltygui.core.rendering.retained_gui_prototype import RetainedGui


@pytest.mark.parametrize('introduced', ['columns', 'rows', 'portal', 'nested'])
@pytest.mark.parametrize('native_state', ['virgin', 'residual-edge', 'retired-edge'])
def test_plain_owner_first_geometry_failure_rolls_back(introduced, native_state):
    cache = RetainedGui(graphics=False)
    if native_state != 'virgin':
        cache.geometry.axes[0].edge(987654, 'native-owned-edge', 17)
        if native_state == 'retired-edge':
            cache.geometry.axes[0].remove(987654)
    mode = ['plain']
    ids = {}

    @cache.gui(width=100, height=60)
    def child(input_value=None):
        if introduced == 'nested':
            cache.geometry.declare(2, axis=1, key='new-child-rows', mins=0)
        return False, input_value

    @cache.gui(width=300, height=200)
    def root(input_value=None):
        ids['root'] = cache.current
        if mode[0] != 'plain':
            if introduced in ('columns', 'rows'):
                layout = cache.geometry.declare(2, axis=int(introduced == 'rows'), key='first', mins=0)
                with layout.cell(0):
                    child()
            elif introduced == 'portal':
                child(melty_window=True, initial={'left': 10, 'top': 20})
            else:
                child()
            if mode[0] == 'fail':
                raise ValueError('first geometry failed')
        return False, input_value

    try:
        root()
        before = [axis.stats() for axis in cache.geometry.axes]
        mode[0] = 'fail'
        cache.invalidate_id(ids['root'])
        with pytest.raises(ValueError, match='first geometry failed'):
            root()
        geometry = cache.geometry
        assert not geometry.layouts and not geometry.frames and not geometry.bindings and not geometry.windows
        assert not geometry.transactions and not geometry.staged
        assert [axis.stats() for axis in geometry.axes] == before
        mode[0] = 'success'
        root()
        assert geometry.windows if introduced == 'portal' else geometry.layouts
    finally:
        cache.close()


def test_native_host_keeps_original_transaction_snapshots():
    from types import SimpleNamespace
    from meltygui.core.rendering.gui_window_prototype import _GuiWindow
    host = _GuiWindow(graphics=False)
    adapter = SimpleNamespace(
        prototype_snapshot=lambda: (((0., 300.), (0., 200.)), ((0., 300.), (0., 200.)),
                                    'walls', 1, ((), ())),
        prototype_commit=lambda pairs: None,
        applied_origin=lambda axis: 0.,
    )
    host.solve_native(adapter)
    geometry = host.cache.geometry
    before = [axis.stats() for axis in geometry.axes]

    @host.cache.gui(width=300, height=200)
    def view(input_value=None):
        # Hosted views must retain native walls, serials and rollback snapshots.
        # The owner's snapshot is taken before its first layout mutation.
        geometry.declare(2, axis=0, key='introduced', mins=0)
        assert isinstance(geometry.transactions[host.cache.current], tuple)
        raise ValueError('hosted geometry failed')

    try:
        with pytest.raises(ValueError, match='hosted geometry failed'):
            view()
        assert [axis.stats() for axis in geometry.axes] == before
        assert geometry.native is not None
        assert not geometry.layouts and not geometry.frames and not geometry.transactions
    finally:
        host.close()
