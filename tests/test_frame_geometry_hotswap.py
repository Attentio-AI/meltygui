"""Geometry refactors remain usable by a process running the previous schema."""
import inspect
import sys
import types
from unittest.mock import MagicMock

from meltygui.code.file_converters import _recompile_module
from meltygui.core.melty import Melty
from meltygui.core.windowing.os_frame import Context


def test_context_refactor_preserves_live_class_layout(monkeypatch):
    # This is the checkpoint's slot layout. Keeping its class identity must
    # still allow the refactored constructor to run after an in-place edit.
    source = '''class Context:
    __slots__ = ("axis", "base", "os_near0", "os_far0", "lists", "specs", "walls",
                 "drags", "os_ids", "shifted", "move")
    def __init__(self, axis):
        self.axis = axis
        self.base = 25.
        self.shifted = ()
'''
    module = types.ModuleType('_frame_context_hotswap_test')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(Melty, 'cache', MagicMock())
    exec(source, module.__dict__)
    live_class = module.Context
    active = live_class('x')
    edited = inspect.getsource(Context)
    assert _recompile_module(module, edited, '/tmp/frame_context_hotswap_test.py') is None
    assert module.Context is live_class
    assert active.axis == 'x' and active.base == 25.
    fresh = live_class('y')
    assert fresh.axis == 'y' and fresh.base == 0.
