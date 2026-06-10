"""Live function apply: cst_dict edits drive the live function ahead of any
save/recompile (live_apply_edits in chain_converters.py).

Parameter-default edits land on __defaults__ / __kwdefaults__; body-level
`name = <literal>` edits are patched into co_consts via code.replace (no
compile). Ambiguous bindings — reassigned names, computed values, const
slots shared by literal dedup — must be left alone for the real recompile.

Run from repo root with the project venv:
    venv/bin/python -m pytest tests/test_live_function_apply.py -s
or standalone:
    venv/bin/python tests/test_live_function_apply.py
"""
import functools
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import libcst as cst
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import cst_module_to_dict
from src.lsd.gl_gui.view.core_conversion.chain_converters import live_apply_edits


FUNC_SRC = '''\
def my_func(count, scale=2.0, *, label="x", flag=False):
    rate = 0.5
    shared = 7
    other = 7
    computed = count * 2
    twice = 1
    twice = 2
    return rate
'''


def _make_func():
    def my_func(count, scale=2.0, *, label="x", flag=False):
        rate = 0.5
        shared = 7
        other = 7
        computed = count * 2
        twice = 1
        twice = 2
        return (rate, shared, other, twice)
    return my_func


def _parse(src):
    return cst_module_to_dict(cst.parse_module(src))


def test_param_defaults_land_live():
    fn = _make_func()
    gp = _parse(FUNC_SRC)
    gp["my_func"]["parameters"]["scale"] = 3.5
    gp["my_func"]["parameters"]["label"] = "y"
    gp["my_func"]["parameters"]["flag"] = True

    live_apply_edits(fn, gp)

    assert fn.__defaults__ == (3.5,)
    assert fn.__kwdefaults__ == {"label": "y", "flag": True}


def test_const_local_patched_ambiguous_skipped():
    fn = _make_func()
    gp = _parse(FUNC_SRC)
    locals_ = gp["my_func"]["locals"]
    assert locals_["rate"] == 0.5 and locals_["shared"] == 7

    locals_["rate"] = 0.9      # literal const - patched
    locals_["shared"] = 99     # 7 is deduped with `other` - shared slot, skipped
    locals_["computed"] = 5    # computed, not a literal value - skipped
    locals_["twice"] = 3       # reassigned name (twice / twice#1) - skipped

    live_apply_edits(fn, gp)

    rate, shared, other, twice = fn(1)
    assert rate == 0.9
    assert shared == 7 and other == 7
    assert twice == 2


def test_wrapped_function_unwraps():
    inner = _make_func()

    @functools.wraps(inner)
    def my_func(*a, **kw):
        return inner(*a, **kw)

    gp = _parse(FUNC_SRC)
    gp["my_func"]["parameters"]["scale"] = 9.0
    live_apply_edits(my_func, gp)
    assert inner.__defaults__ == (9.0,)


CLASS_SRC = '''\
class Widget:
    alpha = 0.5

    def render(self, size=10, *, pad=2):
        inset = 3
        return inset
'''


def test_method_defaults_and_locals_via_class_parse():
    class Widget:
        alpha = 0.5

        def render(self, size=10, *, pad=2):
            inset = 3
            return inset

    gp = _parse(CLASS_SRC)
    inner = gp["Widget"]
    inner["alpha"] = 0.9
    inner["render"]["parameters"]["size"] = 20
    inner["render"]["parameters"]["pad"] = 5
    inner["render"]["locals"]["inset"] = 8

    live_apply_edits(Widget, gp)

    assert Widget.alpha == 0.9
    assert Widget.render.__defaults__ == (20,)
    assert Widget.render.__kwdefaults__ == {"pad": 5}
    assert Widget().render() == 8


MODULE_SRC = '''\
GLOBAL_RATE = 1.5

def helper(steps=4):
    margin = 6
    return margin

class Box:
    depth = 2
'''


def test_module_parse_dispatches_all_kinds():
    mod = types.ModuleType("live_apply_fake_mod")
    exec(compile(MODULE_SRC, "<live_apply_fake_mod>", "exec"), mod.__dict__)
    gp = _parse(MODULE_SRC)

    gp["GLOBAL_RATE"] = 2.5
    gp["helper"]["parameters"]["steps"] = 7
    gp["helper"]["locals"]["margin"] = 11
    gp["Box"]["depth"] = 9

    live_apply_edits(mod, gp)

    assert mod.GLOBAL_RATE == 2.5
    assert mod.helper.__defaults__ == (7,)
    assert mod.helper() == 11
    assert mod.Box.depth == 9


def test_no_default_param_untouched():
    fn = _make_func()
    gp = _parse(FUNC_SRC)
    # `count` has no default - editing nothing else must not add one.
    live_apply_edits(fn, gp)
    assert fn.__defaults__ == (2.0,)
    assert fn.__kwdefaults__ == {"label": "x", "flag": False}


if __name__ == "__main__":
    test_param_defaults_land_live()
    test_const_local_patched_ambiguous_skipped()
    test_wrapped_function_unwraps()
    test_method_defaults_and_locals_via_class_parse()
    test_module_parse_dispatches_all_kinds()
    test_no_default_param_untouched()
    print("all live-function-apply tests passed")
