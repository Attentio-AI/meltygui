"""Restored views own constructor resources even when those fields aren't saved."""
from pathlib import Path
from threading import Lock

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.load_save_v2 import dumps, loads, _copy_plan, _default_for
from meltygui.core.rendering.core_decoration import no_save
from meltygui.state.path_icon_state import PathIconState


@no_save('lock', 'nested')
class RuntimeState(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = 'default'
        self.lock = Lock()
        self.nested = {'items': []}


class PlainState(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = 1
        self.items = []
        self._self = self


def test_restored_icon_views_cannot_evict_each_others_artwork():
    first, second = loads(dumps([PathIconState(), PathIconState()]))
    assert first.folders is not second.folders
    assert first.folders is not PathIconState.default_instance.folders
    folder = Path('/example/project')
    first.folders.entries[folder] = (None, {})
    first.folders.retain({folder})
    second.folders.retain(set())
    assert folder in first.folders.entries
    second.folders.close()
    assert not first.folders.closed


def test_resources_are_constructed_and_saved_aliases_are_preserved():
    original = RuntimeState()
    original.value = 'saved'
    first, alias, second = loads(dumps([original, original, RuntimeState()]))
    assert first is alias
    assert first.value == 'saved'
    assert first.lock is not second.lock
    assert first.lock is not RuntimeState.default_instance.lock
    first.nested['items'].append(1)
    assert second.nested['items'] == []
    assert RuntimeState.default_instance.nested['items'] == []


def test_plain_states_keep_fast_reconstruction_and_self_references():
    assert _copy_plan(PlainState, _default_for(PlainState)) is not False
    first, second = loads(dumps([PlainState(), PlainState()]))
    assert first._self is first and second._self is second
    first.items.append(1)
    assert second.items == []


def test_live_icon_state_detaches_legacy_template_cache():
    from types import SimpleNamespace
    state = PathIconState()
    shared = PathIconState.default_instance.folders
    state.folders = shared
    state.watch_owner = SimpleNamespace(folders=shared)
    state.ensure_owned_resources()
    assert state.folders is not shared
    assert state.watch_owner.folders is state.folders
    owned = state.folders
    state.ensure_owned_resources()
    assert state.folders is owned
