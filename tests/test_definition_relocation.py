"""A definition's identity survives moves; its globals follow its source."""
import inspect
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from meltygui.code.file_converters import _recompile, _recompile_module, stamp_module_baseline
from meltygui.core.definition_hotswap import patch_function
from meltygui.core.melty import Melty


SOURCE = '''from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion
VALUE = "initial"
class FeatureState(DictConversion):
    setting = 3
    def __init__(self):
        super().__init__()
        self.held = "live"
    def read(self):
        return VALUE, self.held
    @classmethod
    def owner(cls):
        return cls, VALUE
    @staticmethod
    def configured():
        return VALUE
@render_func(tint=(0.2, 0.4, 0.6))
def draw_relocation_test(input_value: str, state: FeatureState = None):
    return False, (VALUE, state)
def plain():
    return VALUE
'''


@pytest.fixture
def load_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(Melty, 'cache', Mock())
    monkeypatch.setattr(Melty, 'relocated_functions', {})
    names = []

    def load(name, source=SOURCE):
        path = tmp_path / (name + '.py')
        path.write_text(source)
        module = types.ModuleType(name)
        module.__file__ = str(path)
        monkeypatch.setitem(sys.modules, name, module)
        exec(compile(source, str(path), 'exec'), vars(module))
        stamp_module_baseline(module, source)
        names.append(name)
        return module
    return load


def test_module_hotswap_adds_literal_instance_fields_without_rerunning_init(load_modules):
    module = load_modules('constructor_fields_hotswap')
    first, second = module.FeatureState(), module.FeatureState()
    first.held = 'edited'
    source = SOURCE.replace('self.held = "live"',
        'self.held = "new default"\n        self.pending = []\n'
        '        self.computed = do_not_run()')
    source += '\ndef do_not_run():\n    raise AssertionError("constructor rerun")\n'
    assert _recompile_module(module, source, module.__file__) is None
    assert first.held == 'edited' and second.held == 'live'
    assert first.pending == second.pending == []
    assert first.pending is not second.pending
    assert not hasattr(first, 'computed')


@pytest.mark.parametrize('edit_destination_first', [False, True])
def test_relocation_preserves_identity_globals_and_subsequent_edits(load_modules, edit_destination_first):
    original = load_modules('relocation_original_test')
    wrapper = original.draw_relocation_test
    plain = original.plain
    state_type = original.FeatureState
    state = state_type()
    bound = state.read
    state_type.setting = 97
    destination_source = SOURCE.replace('VALUE = "initial"', 'VALUE = "destination"')
    destination = load_modules('relocation_destination_test', destination_source)
    if edit_destination_first:
        assert _recompile_module(destination, destination_source, destination.__file__) is None
    original_aliases = 'from relocation_destination_test import FeatureState, draw_relocation_test, plain\n'
    assert _recompile_module(original, original_aliases, original.__file__) is None

    assert original.FeatureState is destination.FeatureState is state_type
    assert original.draw_relocation_test is destination.draw_relocation_test is wrapper
    assert original.plain is destination.plain is plain
    assert state_type.setting == 97
    assert state_type().held == 'live'  # zero-argument super() still uses the live class
    assert bound() == ('destination', 'live')
    assert state_type.owner() == (state_type, 'destination')
    assert state_type.configured() == 'destination'
    assert plain() == 'destination'
    assert inspect.unwrap(wrapper).__globals__ is vars(destination)
    assert inspect.unwrap(wrapper).__annotations__['state'] is state_type
    assert Melty.render_funcs_by_name['draw_relocation_test'] is wrapper

    # A module edit at the NEW path changes the old references, including an
    # already-bound method, while keeping the injected state class canonical.
    changed = destination_source.replace('"destination"', '"edited"')
    Path(destination.__file__).write_text(changed)
    assert _recompile_module(destination, changed, destination.__file__) is None
    assert plain() == 'edited'
    assert bound() == ('edited', 'live')
    assert original.draw_relocation_test is destination.draw_relocation_test is wrapper
    assert inspect.unwrap(wrapper).__annotations__['state'] is state_type
    assert state_type.setting == 97
    # Recompiling the compatibility module is idempotent.
    assert _recompile_module(original, original_aliases, original.__file__) is None
    assert plain() == 'edited'

    span = '''@render_func(tint=(0.2, 0.4, 0.6))
def draw_relocation_test(input_value: str, state: FeatureState = None):
    return True, (VALUE + " span", state)
'''
    assert _recompile(wrapper, span, destination.__file__) is None
    assert inspect.unwrap(wrapper)('value', state) == (True, ('edited span', state))
    assert state.held == 'live'


def test_same_named_unrelated_function_and_closure_state(load_modules):
    original = load_modules('closure_original', '''VALUE = "old"
def make():
    values = []
    def collect(value):
        values.append(value)
        return VALUE, values
    return collect
collect = make()
''')
    destination = load_modules('closure_destination', Path(original.__file__).read_text().replace('"old"','"new"'))
    unrelated = load_modules('closure_unrelated', 'def collect(value):\n    return "unrelated"\n')
    held = original.collect
    old_values = held(1)[1]
    undo = patch_function(held, destination.collect)
    assert held(2) == ('new', [1, 2])
    assert held(3)[1] is old_values
    assert unrelated.collect(0) == 'unrelated'
    undo()
    assert held(4) == ('old', [1, 2, 3, 4])


def test_relocation_refreshes_existing_imports_and_injection_closure(load_modules):
    original = load_modules('relocation_source')
    destination = load_modules('relocation_target')
    consumer = load_modules('relocation_consumer', '''from relocation_target import FeatureState, draw_relocation_test
from meltygui.core.core_render import render_func
@render_func(tint=(0.1, 0.2, 0.3))
def consumer_view(input_value: str, state: FeatureState = None):
    return False, state
''')
    state_type = original.FeatureState
    assert _recompile_module(original, 'from relocation_target import FeatureState, draw_relocation_test, plain\n', original.__file__) is None
    assert consumer.FeatureState is state_type
    assert consumer.draw_relocation_test is original.draw_relocation_test
    assert inspect.unwrap(consumer.consumer_view).__annotations__['state'] is state_type
    cells = dict(zip(consumer.consumer_view.__code__.co_freevars,
                     (c.cell_contents for c in consumer.consumer_view.__closure__)))
    assert dict(cells['_default_plan'])['state'] is state_type
    helper = cells['_auto_state_params']
    helper_cells = dict(zip(helper.__code__.co_freevars,
                            (c.cell_contents for c in helper.__closure__)))
    assert helper_cells['name_to_param_type']['state'] is state_type


def test_relocated_definition_runtime_error_rolls_back(load_modules):
    from meltygui.code import hotswap_guard

    original = load_modules('rollback_source', 'VALUE = "live"\ndef plain():\n    return VALUE\n')
    destination = load_modules('rollback_target', 'VALUE = "moved"\ndef plain():\n    raise RuntimeError("bad move")\n')
    held = original.plain
    assert _recompile_module(original, 'from rollback_target import plain\n', original.__file__) is None
    try:
        held()
    except RuntimeError as error:
        assert hotswap_guard.handle_exception(error)
    else:
        pytest.fail('The new implementation must be exercised before rollback')
    assert original.plain is destination.plain is held
    assert held() == 'live'
    assert id(held) not in Melty.relocated_functions


def test_relocation_preserves_distinct_metadata_across_many_consumers(load_modules):
    original = load_modules('metadata_source')
    destination = load_modules('metadata_target')
    consumer_source = 'from metadata_target import FeatureState\n'
    for index in range(128):
        consumer_source += (
            f'def consumer_{index}(value=(FeatureState, {index}), *, kind=FeatureState):\n'
            '    return value, kind\n')
    consumer = load_modules('metadata_consumers', consumer_source)
    assert _recompile_module(original, 'from metadata_target import FeatureState, draw_relocation_test, plain\n', original.__file__) is None
    for index in range(128):
        function = vars(consumer)[f'consumer_{index}']
        assert function() == ((original.FeatureState, index), original.FeatureState)
        assert function.__annotations__ == {}
        assert isinstance(function.__annotations__, dict)
    assert destination.FeatureState is original.FeatureState


def test_hotswap_updates_mapping_bases_and_keeps_live_base_identity(load_modules):
    source = '''class MappingMethods:
    def describe(self):
        return "before", self["held"]
class Files(dict):
    pass
'''
    module = load_modules('mapping_inheritance_test', source)
    held_class, held_base = module.Files, module.MappingMethods
    value = module.Files(held="runtime")
    revised = source.replace('class Files(dict):', 'class Files(MappingMethods, dict):')
    assert _recompile_module(module, revised, module.__file__) is None
    assert module.Files is held_class and module.MappingMethods is held_base
    assert module.Files.__bases__ == (held_base, dict)
    assert isinstance(value, held_base)
    assert value.describe() == ("before", "runtime")
    revised = revised.replace('"before"', '"after"')
    assert _recompile_module(module, revised, module.__file__) is None
    assert value.describe() == ("after", "runtime")
    assert module.Files.__bases__[0] is held_base


def test_mapping_base_change_rolls_back_without_losing_instances():
    from meltygui.code.file_converters import _snapshot_class, _hotswap_class
    class Methods:
        def read(self):
            return self["held"]
    class Before(dict):
        pass
    class After(Methods, dict):
        pass
    held = Before(held="value")
    snapshot = _snapshot_class(Before)
    _hotswap_class(Before, After)
    assert held.read() == "value"
    _hotswap_class(Before, snapshot, force=True)
    assert Before.__bases__ == (dict,)
    assert held["held"] == "value" and not hasattr(held, "read")


def test_class_only_hotswap_binds_methods_and_wrappers_to_live_module(load_modules):
    from meltygui.code.file_converters import _recompile_class
    source = '''import functools
from meltygui.core.runtime.lifecycle import module_is_live
VALUE = "first"
def decorate(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper
class State:
    @decorate
    def read(self):
        return VALUE, module_is_live(globals())
    @property
    def value(self):
        return VALUE
    @staticmethod
    def namespace():
        return globals()
'''
    module = load_modules('class_namespace_test', source)
    held = module.State()
    class_source = source[source.index('class State:'):]
    assert _recompile_class(module.State, class_source, module.__file__) is None
    module.VALUE = "changed"
    assert held.read() == ("changed", True)
    assert held.value == "changed"
    assert held.namespace() is vars(module)
