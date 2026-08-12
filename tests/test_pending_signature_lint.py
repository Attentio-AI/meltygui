"""Call-signature lint resolved through PENDING file text (code_checks).

Covers the span-buffer call pass (_check_call_span) and the pending-truth
signature source (_signature_table / _pending_spec_for): a signature edited in
another view (queued in PendingSave, not yet compiled or written to disk) must
flag wrong call sites immediately, and the live/disk signature is only trusted
when the defining file has no pending edits.
"""

import importlib.util
import sys
import textwrap

import pytest

from src.lsd.gl_gui.view.core_conversion import code_checks
from src.lsd.gl_gui.view.core_views.pending_save import PendingSave


MODULE_SRC = textwrap.dedent("""\
    def helper(a, b=1):
        return a + b


    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y


    @property
    def decorated(a, b, c):
        return a
""")


def _clear_caches():
    # Both file caches carry a 1s freshness floor - tests that change the
    # pending answer mid-run must drop them or they serve the previous value.
    code_checks._file_sig_cache.clear()
    code_checks._file_binds_cache.clear()


@pytest.fixture
def module_file(tmp_path):
    p = tmp_path / "sig_mod.py"
    p.write_text(MODULE_SRC)
    _clear_caches()
    yield p
    _clear_caches()


def _span_lint(span_text, path):
    return code_checks.check_source(span_text, path=str(path),
                                    only_missing_imports=True)


def test_span_call_too_many_positionals(module_file):
    lint = _span_lint("def caller():\n    helper(1, 2, 3)\n", module_file)
    assert any("takes 2 positional arguments but 3 were given" in msg
               for _ln, msg in lint), lint


def test_span_call_unknown_kwarg(module_file):
    lint = _span_lint("def caller():\n    helper(1, bogus=2)\n", module_file)
    assert any("unexpected keyword argument 'bogus'" in msg
               for _ln, msg in lint), lint


def test_span_call_missing_required(module_file):
    lint = _span_lint("def caller():\n    helper()\n", module_file)
    assert any("missing required argument" in msg and "'a'" in msg
               for _ln, msg in lint), lint


def test_span_call_clean_and_class(module_file):
    assert _span_lint("def caller():\n    helper(1)\n"
                      "    Point(1, 2)\n", module_file) == []
    lint = _span_lint("def caller():\n    Point(1)\n", module_file)
    assert any("missing required argument" in msg and "'y'" in msg
               for _ln, msg in lint), lint


def test_span_call_decorated_def_stays_silent(module_file):
    # A decorator can change the signature arbitrarily - err on silence.
    assert _span_lint("def caller():\n    decorated()\n", module_file) == []


def test_span_call_shadowed_name_stays_silent(module_file):
    # The span rebinds helper locally - the module def must not apply.
    assert _span_lint("def caller(helper):\n    helper(1, 2, 3)\n",
                      module_file) == []


def test_span_toggle_off_restores_old_behavior(module_file, monkeypatch):
    from src.lsd.gl_gui.toggles import Toggles
    monkeypatch.setattr(Toggles.TextEditor, "lint_span_calls", False)
    assert _span_lint("def caller():\n    helper(1, 2, 3)\n",
                      module_file) == []


def test_span_call_pending_signature_wins(module_file, monkeypatch):
    # Simulate an unsaved edit: helper takes a required third arg. The
    # live/disk def still takes (a, b=1); the pending text is the truth.
    pending = MODULE_SRC.replace("def helper(a, b=1):", "def helper(a, b, c):")
    monkeypatch.setattr(PendingSave, "pending_gen_for",
                        classmethod(lambda cls, p: 1))
    monkeypatch.setattr(PendingSave, "current_file_text",
                        classmethod(lambda cls, p: pending))
    _clear_caches()
    lint = _span_lint("def caller():\n    helper(1, 2)\n", module_file)
    assert any("missing required argument" in msg and "'c'" in msg
               for _ln, msg in lint), lint
    # And the previously-wrong call is now clean.
    _clear_caches()
    assert _span_lint("def caller():\n    helper(1, 2, 3)\n",
                      module_file) == []


def test_span_import_hop_checks_live_callee(module_file, tmp_path):
    # The file imports helper2 from a sibling; the span calls it wrong. The
    # callee resolves through sys.modules (no pending edits → live text).
    sib = tmp_path / "sig_sibling.py"
    sib.write_text("def helper2(x):\n    return x\n")
    spec = importlib.util.spec_from_file_location("sig_sibling_t", sib)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sig_sibling_t"] = mod
    try:
        spec.loader.exec_module(mod)
        module_file.write_text(MODULE_SRC
                               + "from sig_sibling_t import helper2\n")
        _clear_caches()
        lint = _span_lint("def caller():\n    helper2(1, 2)\n", module_file)
        assert any("takes 1 positional argument but 2 were given" in msg
                   for _ln, msg in lint), lint
    finally:
        sys.modules.pop("sig_sibling_t", None)


def test_pending_spec_for_unwraps_and_answers_from_pending(module_file,
                                                           monkeypatch):
    # Whole-file/live path: _object_spec upgrades a live function to its
    # owning file's pending text when that file has unsaved edits.
    spec = importlib.util.spec_from_file_location("sig_mod_t", module_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    handled, _ = code_checks._pending_spec_for(mod.helper)
    assert not handled          # no pending edits → live introspection

    pending = MODULE_SRC.replace("def helper(a, b=1):", "def helper(a, b, c):")
    monkeypatch.setattr(PendingSave, "pending_gen_for",
                        classmethod(lambda cls, p: 1))
    monkeypatch.setattr(PendingSave, "current_file_text",
                        classmethod(lambda cls, p: pending))
    _clear_caches()
    handled, pspec = code_checks._pending_spec_for(mod.helper)
    assert handled and pspec is not None
    assert pspec.required == {"a", "b", "c"}
