"""Live-view store ownership across the def-run path (the 16 GB leak):

- the instrumented twin publishes to the SAME object run_capture is open on,
  even when the (file, line) resolver would pick another function object
  (module fn vs. parked exec twin sharing a def line);
- adopt_live_store moves a store to the successor function, so re-exec'd
  twins never each keep a generation of stacked tensors;
- run after run there is exactly one store-owning function and the store
  holds only the latest run's stacks (stale keys pruned, accumulators reset).
"""
import gc
import inspect
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import torch

from src.lsd.gl_gui.view.core_conversion import chain_converters as cc
from src.lsd.gl_gui.view.core_conversion.live_instrument import run_instrumented
from src.lsd.gl_gui.view.core_conversion.live_view import (
    adopt_live_store, live_values_for, run_capture, current_run_owner)

MOD_SRC = '''
import torch

def pass_fn(seq_len=22, extra=False):
    base = torch.zeros(1, seq_len, 8)
    for layer in range(4):
        normed = base + layer
        scores = torch.zeros(3, seq_len, seq_len)
    if extra:
        tmp_name = base * 2
    return seq_len
'''


def _load_module(tmp_path):
    path = tmp_path / "lvowner_mod.py"
    path.write_text(MOD_SRC)
    import importlib.util
    spec = importlib.util.spec_from_file_location("lvowner_mod", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lvowner_mod"] = mod
    spec.loader.exec_module(mod)
    return mod, path


def _exec_twin(mod):
    """The def-run path's exec'd twin: same def line, parked beside the real
    function under _fnrun_live_<name>."""
    src = inspect.getsource(mod.pass_fn)
    ns = dict(vars(mod))
    first = mod.pass_fn.__code__.co_firstlineno
    exec(compile("\n" * (first - 1) + src, mod.__file__, "exec"), ns)
    fn = ns["pass_fn"]
    fn.__fnrun_exec__ = True
    return fn


def _purge_resolver(path):
    for k in [k for k in cc._ENCLOSING_FN_CACHE if k[0] == str(path.resolve())]:
        del cc._ENCLOSING_FN_CACHE[k]


def _store_owners(path):
    return [o for o in gc.get_objects()
            if isinstance(o, types.FunctionType)
            and "__live_values__" in (o.__dict__ or {})
            and o.__code__.co_filename == str(path)]


def test_twin_publishes_to_run_capture_owner(tmp_path):
    mod, path = _load_module(tmp_path)
    twin = _exec_twin(mod)
    mod.__dict__["_fnrun_live_pass_fn"] = twin
    _purge_resolver(path)
    # Resolver picks ONE of the two same-line functions; the run must
    # publish to the object run_capture is on regardless.
    run_instrumented(twin, seq_len=5)
    assert live_values_for(twin), "run owner got no store"
    assert not live_values_for(mod.pass_fn), "module fn received publishes"
    assert current_run_owner() is None


def test_resolver_prefers_parked_exec_twin_on_tie(tmp_path):
    mod, path = _load_module(tmp_path)
    twin = _exec_twin(mod)
    mod.__dict__["_fnrun_live_pass_fn"] = twin
    _purge_resolver(path)
    line = mod.pass_fn.__code__.co_firstlineno + 2
    assert cc._enclosing_function(str(path), line) is twin


def test_adopt_live_store_moves_once():
    def a():
        pass

    def b():
        pass
    a.__live_values__ = {("k",): 1}
    a.__live_accum__ = {("k",): {"buf": None}}
    adopt_live_store(a, b)
    assert b.__live_values__ == {("k",): 1} and "__live_values__" not in a.__dict__
    assert "__live_accum__" in b.__dict__
    # new already has a store → old's is dropped, new's untouched
    c = types.FunctionType(a.__code__, {})
    c.__live_values__ = {("other",): 2}
    adopt_live_store(b, c)
    assert c.__live_values__ == {("other",): 2} and "__live_values__" not in b.__dict__


def test_reexec_per_edit_keeps_one_owner_and_no_stale_stacks(tmp_path):
    mod, path = _load_module(tmp_path)
    sizes = []
    for i, (seq, extra) in enumerate([(5, False), (9, True), (7, False), (5, False)]):
        fn = _exec_twin(mod)
        adopt_live_store(mod.__dict__.get("_fnrun_live_pass_fn"), fn)
        adopt_live_store(mod.pass_fn, fn)
        mod.__dict__["_fnrun_live_pass_fn"] = fn
        _purge_resolver(path)
        run_instrumented(fn, seq_len=seq, extra=extra)
        owners = _store_owners(path)
        assert owners == [fn], f"run {i}: {len(owners)} store owners"
        st = live_values_for(fn)
        normed = st[("line:7#normed",)]
        assert tuple(normed.shape) == (4, 1, seq, 8)      # fresh stack, no list fallback
        assert (("line:10#tmp_name",) in st) == extra     # stale key pruned when the line stops running
        sizes.append(sum(v.untyped_storage().nbytes() for v in st.values()
                         if torch.is_tensor(v)))
    # store size tracks the CURRENT run, not the history
    assert sizes[3] == sizes[0]
