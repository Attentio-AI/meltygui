"""Verify live identities survive definition moves between modules."""
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import Mock

from meltygui.core.melty import Melty
from meltygui.code.file_converters import _recompile_module, stamp_module_baseline

source = '''from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion
class FeatureState(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = "initial"
@render_func(tint=(0.2, 0.4, 0.6))
def draw_relocation_probe(input_value: str, state: FeatureState = None):
    return False, input_value
'''
with tempfile.TemporaryDirectory() as directory:
    modules = []
    for name in ('relocation_original', 'relocation_destination'):
        path = Path(directory) / (name + '.py')
        path.write_text(source)
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(source, str(path), 'exec'), module.__dict__)
        modules.append(module)
    original, destination = modules
    state_type = original.FeatureState
    state = state_type()
    state.value = 'live value'
    view = original.draw_relocation_probe
    stamp_module_baseline(original, source)
    Melty.cache = Mock()
    relocated = 'from relocation_destination import FeatureState, draw_relocation_probe\n'
    error = _recompile_module(original, relocated, original.__file__)
    assert error is None, error
    print('Original state class preserved:', original.FeatureState is state_type)
    print('Destination state class is the live class:', destination.FeatureState is state_type)
    print('Original wrapper preserved:', original.draw_relocation_probe is view)
    print('Destination wrapper is the live wrapper:', destination.draw_relocation_probe is view)
    print('Existing instance value:', state.value)
    assert destination.FeatureState is state_type, 'Destination export is a second class identity'
    assert destination.draw_relocation_probe is view, 'Destination export is a second wrapper identity'
