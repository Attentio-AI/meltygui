"""Live class-var apply: cst_dict edits drive the live type ahead of any
save/recompile (_live_apply_class_vars in chain_converters.py).

Plain class-var values land via setattr; methods/nested dicts, CodeLine
(unparsed source — a str subclass), comments, and __init__ instance attrs
must never leak onto the class.

Run from repo root with the project venv:
    venv/bin/python -m pytest tests/test_live_class_apply.py -s
or standalone:
    venv/bin/python tests/test_live_class_apply.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import libcst as cst
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import cst_module_to_dict
from src.lsd.gl_gui.view.core_conversion.chain_converters import _live_apply_class_vars


CLASS_SRC = '''\
class Toggles:
    profile_mode = False
    alpha = 0.5
    tint = (1.0, 0.0, 1.0)
    label = "hello"
    weird = some_unresolved_call(1, 2)[3]

    def method(self):
        return self.alpha

    def __init__(self):
        self.inst_only = 42
'''


def _make_class():
    class Toggles:
        profile_mode = False
        alpha = 0.5
        tint = (1.0, 0.0, 1.0)
        label = "hello"
        weird = None

        def method(self):
            return self.alpha

        def __init__(self):
            self.inst_only = 42
    return Toggles


def _parse():
    return cst_module_to_dict(cst.parse_module(CLASS_SRC))


def test_edited_vars_land_on_live_class():
    cls = _make_class()
    gp = _parse()
    inner = gp["Toggles"]
    inner["alpha"] = 0.9
    inner["tint"] = (0.0, 1.0, 0.0)
    inner["profile_mode"] = True

    _live_apply_class_vars(cls, gp)

    assert cls.alpha == 0.9
    assert cls.tint == (0.0, 1.0, 0.0)
    assert cls.profile_mode is True
    assert cls.label == "hello"  # unedited stays


def test_codeline_methods_and_instance_attrs_skipped():
    cls = _make_class()
    gp = _parse()
    inner = gp["Toggles"]
    inner["inst_only"] = 99  # surfaced from __init__, not a class var

    _live_apply_class_vars(cls, gp)

    # CodeLine value (unparseable source) never overwrites the live attr
    assert cls.weird is None
    # method is a callable, not a parsed dict
    assert callable(cls.__dict__["method"])
    # __init__ self-assignments don't become class vars
    assert not hasattr(cls, "inst_only")


def test_unknown_class_name_is_noop():
    cls = _make_class()
    _live_apply_class_vars(cls, {"Other": {"alpha": 1.0}})
    assert cls.alpha == 0.5


if __name__ == "__main__":
    test_edited_vars_land_on_live_class()
    test_codeline_methods_and_instance_attrs_skipped()
    test_unknown_class_name_is_noop()
    print("all live-class-apply tests passed")
