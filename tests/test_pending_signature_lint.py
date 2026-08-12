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
    import imgui
    import json


    def window(**kw):            # exec stub — the lint matches decorators
        def wrap(fn):            # by NAME, so behavior here is irrelevant
            return fn
        return wrap


    def render_func(**kw):
        def wrap(fn):
            return fn
        return wrap


    def helper(a, b=1):
        return a + b


    def typed_helper(name: str, count: int = 0, ratio: float = 1.0):
        return name * count


    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y


    @property
    def decorated(a, b, c):
        return a


    @window(tint=(0.0, 0.335, 0.772, 1.0))
    def windowed_button(label, draw_state, view_id, width=None, **kwargs):
        return label


    @render_func(use_cache=True)
    def draw_thing(input_value=None, draw_state=None, speed=1.0):
        return input_value
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


def test_span_call_window_decorator_is_transparent(module_file):
    # @window only registers and returns the def unchanged - the real
    # signature applies, so a bare call flags its missing required args
    # (the flat_button() case) while a full positional call stays silent.
    lint = _span_lint("def caller(draw_state=None):\n"
                      "    windowed_button()\n", module_file)
    assert any("missing required argument" in msg and "'label'" in msg
               and "'view_id'" in msg for _ln, msg in lint), lint
    assert _span_lint("def caller(draw_state=None):\n"
                      "    windowed_button('x##1', draw_state, 'btn',\n"
                      "                    width=20, extra=1)\n",
                      module_file) == []


def test_span_call_render_func_wrapper_convention(module_file):
    # @render_func replaces the def with wrapper(input_value=None, **kwargs):
    # >1 positional is a guaranteed TypeError; any kwargs (wrapper-level
    # included) and missing "required" params are silent (modes/defaults fill
    # them), so only the positional shape reports.
    lint = _span_lint("def caller():\n    draw_thing('a', 'b')\n", module_file)
    assert any("takes 1 positional argument but 2 were given" in msg
               for _ln, msg in lint), lint
    assert _span_lint("def caller():\n"
                      "    draw_thing('a', mode=None, name='x', speed=2.0)\n"
                      "    draw_thing()\n", module_file) == []


def test_span_dotted_call_through_module_alias(module_file):
    # The module's own `import json` resolves the base; the chain uses the
    # LIVE signature, so json.dumps() flags its missing required arg while a
    # valid call stays silent.
    lint = _span_lint("def caller():\n    json.dumps()\n", module_file)
    assert any("dumps() missing required argument" in msg and "'obj'" in msg
               for _ln, msg in lint), lint
    assert _span_lint("def caller():\n    json.dumps({'a': 1}, indent=2)\n",
                      module_file) == []
    # A base the buffer itself binds must never match the module's import.
    assert _span_lint("def caller(json):\n    json.dumps()\n",
                      module_file) == []


def test_signature_table_keeps_last_good_on_broken_pending(module_file,
                                                           monkeypatch):
    # Mid-keystroke the file's pending text is unparseable; the table must
    # serve the last good parse instead of blanking every marker (the
    # flash-then-vanish bug).
    span = "def caller():\n    helper(1, 2, 3)\n"
    assert _span_lint(span, module_file)          # seed a good table
    monkeypatch.setattr(PendingSave, "pending_gen_for",
                        classmethod(lambda cls, p: 2))
    monkeypatch.setattr(PendingSave, "current_file_text",
                        classmethod(lambda cls, p: "def broken(:\n"))
    # Age the table entry below the freshness floor so the broken text is
    # actually re-read (the floor would otherwise serve the good table anyway).
    for k, hit in list(code_checks._file_sig_cache.items()):
        code_checks._file_sig_cache[k] = (hit[0], hit[1], hit[2], hit[3] - 5)
    lint = _span_lint(span, module_file)
    assert any("takes 2 positional arguments but 3 were given" in msg
               for _ln, msg in lint), lint


def test_incremental_region_relint_sees_sibling_defs(module_file):
    # The incremental whole-file lint re-lints ONLY the edited top-level
    # block: a sibling def (helper/flat_button) is neither in the region's
    # scopes nor live-resolvable, so without the pending-table fallback the
    # finding silently vanished on the first keystroke after the seed pass
    # (the live-studio flat_button case).
    full = module_file.read_text() + textwrap.dedent("""\


        def big_caller():
            x = 1
            return x
    """)
    module_file.write_text(full)
    _clear_caches()
    code_checks._inc_lint_state.clear()
    code_checks.check_source_incremental(full, path=str(module_file))  # seed
    edited = full.replace("x = 1", "x = 1\n    helper(1, 2, 3)")
    lint = code_checks.check_source_incremental(edited, path=str(module_file))
    assert any("takes 2 positional arguments but 3 were given" in msg
               for _ln, msg in lint), lint
    # And through the module's own import, same region blindness
    edited2 = edited.replace("return x", "json.dumps()\n    return x")
    lint2 = code_checks.check_source_incremental(edited2, path=str(module_file))
    assert any("dumps() missing required argument" in msg
               for _ln, msg in lint2), lint2
    code_checks._inc_lint_state.clear()


def test_star_splat_of_literal_tuple_counts(module_file):
    # f(*(0, 0)) has a knowable positional count; a splatted NAME is
    # unknowable (none) as before.
    lint = _span_lint("def caller():\n    helper(*(1, 2, 3))\n", module_file)
    assert any("takes 2 positional arguments but 3 were given" in msg
               for _ln, msg in lint), lint
    assert _span_lint("def caller():\n    helper(*(1, 2))\n",
                      module_file) == []
    assert _span_lint("def caller(xs):\n    helper(*xs)\n",
                      module_file) == []


def test_doc_signature_alias_name(module_file):
    # pyimgui aliases share the canonical signature's doc line
    # (set_cursor_position's doc reads "set_cursor_pos(local_pos)") - the
    # doc parser accepts the alias and reports under the caller's spelling.
    import imgui
    spec = code_checks._spec_from_doc("set_cursor_position",
                                      imgui.set_cursor_position.__doc__)
    assert spec is not None and spec.named == ["local_pos"], spec
    lint = _span_lint("def caller():\n    imgui.set_cursor_position(0, 0)\n",
                      module_file)
    assert any("set_cursor_position() takes 1 positional argument "
               "but 2 were given" in msg for _ln, msg in lint), lint
    lint2 = _span_lint("def caller():\n    imgui.same_line(1.0, -1.0, 3)\n",
                       module_file)
    assert any("takes 2 positional arguments but 3 were given" in msg
               for _ln, msg in lint2), lint2


def test_literal_type_mismatch(module_file):
    # Declared types from doc C types (imgui) and source annotations; only
    # LITERAL args are judged - and literals with required params flag even
    # though Python would coerce (same_line(False) is probably a bug).
    lint = _span_lint("def caller():\n    imgui.same_line(False)\n",
                      module_file)
    assert any("same_line() expected float for 'position', got bool" in msg
               for _ln, msg in lint), lint
    assert _span_lint("def caller(x):\n"
                      "    imgui.same_line(0)\n"       # int → float fine
                      "    imgui.same_line(0.5)\n"
                      "    imgui.same_line(x)\n",      # x never judged
                      module_file) == []
    lint2 = _span_lint("def caller():\n    typed_helper(0)\n", module_file)
    assert any("expected str for 'name', got int" in msg
               for _ln, msg in lint2), lint2
    lint3 = _span_lint("def caller():\n    typed_helper('x', count=True)\n",
                       module_file)
    assert any("expected int for 'count', got bool" in msg
               for _ln, msg in lint3), lint3
    assert _span_lint("def caller():\n"
                      "    typed_helper('x', count=2, ratio=3)\n"
                      "    typed_helper('x', count=2.0)\n"    # integral float
                      "    typed_helper('x', count=None)\n",  # None: silent
                      module_file) == []
    lint4 = _span_lint("def caller():\n    typed_helper('x', count=2.5)\n",
                       module_file)
    assert any("expected int for 'count', got float" in msg
               for _ln, msg in lint4), lint4


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
