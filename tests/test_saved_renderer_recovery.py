"""A temporarily missing renderer must recover after a placeholder was saved."""
import sys
from types import ModuleType

from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.conversion.missing_saved_class import missing_saved_class
from meltygui.model.tile_model import Split, Tile


def test_saved_missing_renderer_recovers_without_losing_tile_state(monkeypatch):
    module = ModuleType('recovered_tile_renderer')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    placeholder = missing_saved_class('recovered_tile_renderer.draw_editor')
    shared = {'tabs': ['one.py', 'two.py']}
    tree = Split(children=[Tile('left', render_func=placeholder, input_value=shared),
                           Tile('right', render_func=placeholder, input_value=shared)])
    original_ids = [tile.id for tile in tree.children]
    saved = dumps(tree)
    unresolved = loads(saved)
    assert unresolved.children[0].render_func is placeholder
    saved = dumps(unresolved)
    exec('def draw_editor(input_value, **kwargs):\n    return False, input_value\n', module.__dict__)
    restored = loads(saved)
    assert [tile.id for tile in restored.children] == original_ids
    assert restored.children[0].input_value is restored.children[1].input_value
    for tile in restored.children:
        assert tile.render_func is module.draw_editor
        assert tile.render_func(tile.input_value, unique_name='editor') == (False, shared)
    assert loads(dumps(restored)).children[0].render_func is module.draw_editor


def test_saved_missing_class_still_restores_instances(monkeypatch):
    module = ModuleType('recovered_tile_model')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    placeholder = missing_saved_class('recovered_tile_model.Model')
    value = placeholder()
    value.content = 'preserved'
    saved = dumps(value)
    exec('from meltygui.core.conversion.dict_conversion import DictConversion\nclass Model(DictConversion):\n    pass\n', module.__dict__)
    restored = loads(saved)
    assert isinstance(restored, module.Model)
    assert restored.content == 'preserved'
