"""
core_syntax — the libcst-free cst_dict with a text residual.

  * identity: every src file parses and comes back byte-identical (no edits)
  * leaf edits replace only the value span, styled on the old text
  * structural edits (add / delete / reorder) are text surgery on extents
  * comments and `# [k=v]` override comments round-trip and edit
  * dict shape matches the libcst converter key-for-key on curated snippets
  * update_in_place keeps unchanged value objects by identity
"""

import ast
import unittest
from pathlib import Path

import conftest  # noqa: F401

from src.lsd.gl_gui.view.core_conversion.core_syntax import (
    parse_to_dict, general_parse_to_str, diff, update_in_place, CoreSyntaxError,
    ORIGIN_KEY, values_equal, render)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    Comment, CodeLine, ClassParse, EnumParse, FunctionParse, CallParse, DecorationParse,
    Conditional, Loop, Try, Except, NO_DEFAULT, cst_module_to_dict, GeneralParse)
import libcst as cst

SRC = Path(__file__).resolve().parents[1] / "src"


def roundtrip(text):
    gp = parse_to_dict(text)
    return gp, general_parse_to_str(gp)


SAMPLE = '''\
"""Module doc."""
import os

# [tint=(0.1, 0.2, 0.3)]
class Toggles:
    """Settings."""
    # the speed knob
    speed = 3.0
    name = 'abc'  # trailing note
    flag = True
    # [tint=(1.0, 0.0, 0.0)]
    tint = (0.5, 0.25, 1.0)

    class TextEditor:
        enable_live_view = False
        max_lines = 100


@register(name="plugin", version=2)
def my_func(a, b=1, c="x", *, d=None):
    local_one = 5
    local_one = 6
    if a:
        x = 0.2
    elif b:
        x = 0.05
    else:
        x = 0.01
    for i in range(0, 10, 2):
        y = i * 2
    try:
        z = 1
    except ValueError as e:
        z = 2
    finally:
        z = 3
    configure(1, debug=True)
    return local_one


configure(debug=False)
'''


class TestIdentity(unittest.TestCase):
    def test_sample_identity(self):
        gp, out = roundtrip(SAMPLE)
        self.assertEqual(out, SAMPLE)
        self.assertEqual(diff(gp), [])

    def test_whole_src_tree_identity(self):
        files = sorted(SRC.rglob("*.py"))
        self.assertGreater(len(files), 50)
        checked = 0
        for path in files:
            if "site-packages" in str(path) or "venv" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            try:
                ast.parse(text)
            except SyntaxError:
                continue
            gp = parse_to_dict(text, file_path=path)
            self.assertEqual(diff(gp), [], f"{path} produced edits with no changes")
            self.assertEqual(general_parse_to_str(gp), text, f"{path} did not round-trip")
            checked += 1
        self.assertGreater(checked, 50)

    def test_self_assignment_is_no_edit(self):
        gp = parse_to_dict(SAMPLE)
        gp["Toggles"]["speed"] = 3.0
        gp["Toggles"]["name"] = "abc"
        gp["Toggles"]["tint"] = (0.5, 0.25, 1.0)
        gp["my_func"]["parameters"]["c"] = "x"
        self.assertEqual(diff(gp), [])

    def test_float32_noise_is_no_edit(self):
        gp = parse_to_dict("x = 1.6\n")
        import struct
        noisy = struct.unpack("f", struct.pack("f", 1.6))[0]
        gp["x"] = noisy
        self.assertEqual(diff(gp), [])


class TestShape(unittest.TestCase):
    def test_types_and_keys(self):
        gp = parse_to_dict(SAMPLE)
        self.assertIsInstance(gp, GeneralParse)
        self.assertIsInstance(gp["Toggles"], ClassParse)
        self.assertIsInstance(gp["Toggles"]["TextEditor"], ClassParse)
        self.assertIsInstance(gp["my_func"], FunctionParse)
        self.assertEqual(gp["Toggles"]["speed"], 3.0)
        self.assertEqual(gp["Toggles"]["tint"], (0.5, 0.25, 1.0))
        # The class's own `# [tint=...]` plus the namespaced field override below it.
        self.assertEqual(gp["Toggles"]["__overrides__"],
                         {"tint": (0.1, 0.2, 0.3), "__tint__": {"tint": (1.0, 0.0, 0.0)}})
        params = gp["my_func"]["parameters"]
        self.assertEqual(list(params), ["a", "b", "c", "d"])
        self.assertIs(params["a"], NO_DEFAULT)
        self.assertEqual(params["b"], 1)
        self.assertIsNone(params["d"])
        dec = gp["my_func"]["decorators"]["register"]
        self.assertIsInstance(dec, DecorationParse)
        self.assertEqual(dec["name"], "plugin")
        loc = gp["my_func"]["locals"]
        self.assertEqual(loc["local_one"], 5)
        self.assertEqual(loc["local_one#1"], 6)
        self.assertIsInstance(loc["if##0"], Conditional)
        self.assertEqual(loc["if##0"]["##if"], CodeLine("a"))
        self.assertEqual(loc["if##0"]["x"], 0.2)
        self.assertEqual(loc["elif##0"]["x"], 0.05)
        self.assertEqual(loc["else##0"]["x"], 0.01)
        self.assertIsInstance(loc["for i in range(0, 10, 2)"], Loop)
        self.assertEqual(loc["for i in range(0, 10, 2)"]["range"], [0, 10, 2])
        self.assertIsInstance(loc["try"], Try)
        self.assertIsInstance(loc["except ValueError as e"], Except)
        self.assertEqual(loc["finally"]["z"], 3)
        self.assertIsInstance(loc["configure()"], CallParse)
        self.assertEqual(loc["configure()"]["arg0"], 1)
        self.assertEqual(loc["configure()"]["debug"], True)
        self.assertEqual(gp["configure()"]["debug"], False)
        comments = [k for k in gp["Toggles"] if isinstance(k, Comment)]
        self.assertEqual([str(c) for c in comments], ["# the speed knob", "# trailing note"])
        self.assertEqual(comments[1].inline, "name")

    def test_field_override(self):
        gp = parse_to_dict(SAMPLE)
        # `# [tint=(1.0, 0.0, 0.0)]` above the primitive `tint` field is namespaced.
        ov = gp["Toggles"]["__overrides__"]
        self.assertEqual(ov["__tint__"], {"tint": (1.0, 0.0, 0.0)})

    def test_spans_present(self):
        gp = parse_to_dict(SAMPLE)
        self.assertEqual(gp["Toggles"].span.start_line, 5)
        self.assertIn("speed", gp["Toggles"]._child_spans)
        self.assertEqual(gp["Toggles"]._child_spans["speed"].start_line, 8)

    def test_enum_parse(self):
        gp = parse_to_dict("import enum\nclass Color(enum.Enum):\n    RED = 1\n")
        self.assertIsInstance(gp["Color"], EnumParse)

    def test_init_fields(self):
        gp = parse_to_dict("class A:\n    x = 1\n    def __init__(self):\n        self.y = 2\n        self.x = 9\n")
        self.assertEqual(gp["A"]["x"], 1)
        self.assertEqual(gp["A"]["y"], 2)
        self.assertNotIn("__init__", gp["A"])


class TestLeafEdits(unittest.TestCase):
    def test_int_and_float(self):
        gp = parse_to_dict("a = 1\nb = 0.25\n")
        gp["a"] = 2
        gp["b"] = 0.5
        # Floats render through the shared _float_to_str - min two decimals, as today.
        self.assertEqual(general_parse_to_str(gp), "a = 2\nb = 0.50\n")

    def test_float_precision_capped_to_old(self):
        gp = parse_to_dict("b = 0.25\n")
        gp["b"] = 0.3333333333
        self.assertEqual(general_parse_to_str(gp), "b = 0.33\n")

    def test_str_keeps_quote_style(self):
        gp = parse_to_dict("s = 'abc'\nt = \"de\"\n")
        gp["s"] = "x'y"
        gp["t"] = "new"
        self.assertEqual(general_parse_to_str(gp), "s = 'x\\'y'\nt = \"new\"\n")

    def test_nested_class_var(self):
        gp = parse_to_dict(SAMPLE)
        gp["Toggles"]["TextEditor"]["max_lines"] = 250
        out = general_parse_to_str(gp)
        self.assertIn("        max_lines = 250\n", out)
        self.assertEqual(out.replace("max_lines = 250", "max_lines = 100"), SAMPLE)

    def test_tuple_element_edit_keeps_layout(self):
        text = "tint = (\n    0.1,\n    0.2,\n    0.3,\n)\n"
        gp = parse_to_dict(text)
        gp["tint"] = (0.1, 0.9, 0.3)
        self.assertEqual(general_parse_to_str(gp), "tint = (\n    0.1,\n    0.90,\n    0.3,\n)\n")

    def test_tuple_whole_replace_keeps_multiline_style(self):
        text = "tint = (\n    0.1,\n    0.2,\n)\n"
        gp = parse_to_dict(text)
        gp["tint"] = (0.1, 0.2, 0.3)
        self.assertEqual(general_parse_to_str(gp), "tint = (\n    0.1,\n    0.2,\n    0.30,\n)\n")

    def test_param_default_and_local(self):
        gp = parse_to_dict(SAMPLE)
        gp["my_func"]["parameters"]["b"] = 7
        gp["my_func"]["locals"]["local_one#1"] = 60
        gp["my_func"]["locals"]["if##0"]["x"] = 0.9
        out = general_parse_to_str(gp)
        self.assertIn("def my_func(a, b=7, c=\"x\", *, d=None):", out)
        self.assertIn("    local_one = 60\n", out)
        self.assertIn("        x = 0.90\n", out)

    def test_param_gains_default(self):
        gp = parse_to_dict("def f(a, b=1):\n    pass\n")
        gp["f"]["parameters"]["a"] = 3
        self.assertEqual(general_parse_to_str(gp), "def f(a=3, b=1):\n    pass\n")

    def test_param_loses_default(self):
        gp = parse_to_dict("def f(a, b=1):\n    pass\n")
        gp["f"]["parameters"]["b"] = NO_DEFAULT
        self.assertEqual(general_parse_to_str(gp), "def f(a, b):\n    pass\n")

    def test_invalid_param_order_is_refused(self):
        gp = parse_to_dict("def f(a, b=1):\n    pass\n")
        gp["f"]["parameters"]["a"] = 3
        gp["f"]["parameters"]["b"] = NO_DEFAULT
        with self.assertRaises(CoreSyntaxError):
            general_parse_to_str(gp)

    def test_decorator_and_call_kwargs(self):
        gp = parse_to_dict(SAMPLE)
        gp["my_func"]["decorators"]["register"]["version"] = 3
        gp["configure()"]["debug"] = True
        gp["my_func"]["locals"]["configure()"]["arg0"] = 42
        out = general_parse_to_str(gp)
        self.assertIn('@register(name="plugin", version=3)', out)
        self.assertIn("configure(debug=True)\n", out)
        self.assertIn("    configure(42, debug=True)\n", out)

    def test_codeline_replace(self):
        gp = parse_to_dict("x = some_call() + 1\n")
        self.assertEqual(gp["x"], CodeLine("some_call() + 1"))
        gp["x"] = CodeLine("other()")
        self.assertEqual(general_parse_to_str(gp), "x = other()\n")

    def test_condition_edit(self):
        gp = parse_to_dict("def f(a):\n    if a > 1:\n        x = 1\n")
        gp["f"]["locals"]["if##0"]["##if"] = CodeLine("a > 2")
        self.assertEqual(general_parse_to_str(gp), "def f(a):\n    if a > 2:\n        x = 1\n")

    def test_range_arg_edit(self):
        gp = parse_to_dict("def f():\n    for i in range(0, 10):\n        y = i\n")
        gp["f"]["locals"]["for i in range(0, 10)"]["range"] = [0, 20]
        self.assertEqual(general_parse_to_str(gp), "def f():\n    for i in range(0, 20):\n        y = i\n")

    def test_type_change_replaces_whole_span(self):
        gp = parse_to_dict("x = (1, 2)\n")
        gp["x"] = 5
        self.assertEqual(general_parse_to_str(gp), "x = 5\n")

    def test_syntax_check(self):
        gp = parse_to_dict("x = 1\n")
        gp["x"] = CodeLine("(")
        with self.assertRaises(CoreSyntaxError):
            general_parse_to_str(gp)


class TestStructuralEdits(unittest.TestCase):
    def test_duplicate_binding_edits_last_and_keeps_first(self):
        text = "COLORS = 1\nCOLORS = 2\nx = 3\n"
        gp = parse_to_dict(text)
        self.assertEqual(gp["COLORS"], 2)
        gp["COLORS"] = 9
        self.assertEqual(general_parse_to_str(gp), "COLORS = 1\nCOLORS = 9\nx = 3\n")
        # a reorder around a shadowed binding keeps it in its slot
        gp = parse_to_dict(text)
        v = gp.pop("COLORS")
        gp["COLORS"] = v
        self.assertEqual(general_parse_to_str(gp), "COLORS = 1\nx = 3\nCOLORS = 2\n")

    def test_property_setter_pair(self):
        text = ("class M:\n    @property\n    def b(self):\n        return 1\n\n"
                "    @b.setter\n    def b(self, v):\n        x = 1\n")
        gp = parse_to_dict(text)
        self.assertEqual(diff(gp), [])
        gp["M"]["b"]["locals"]["x"] = 2
        self.assertEqual(general_parse_to_str(gp), text.replace("x = 1", "x = 2"))

    def test_add_class_var_at_position(self):
        gp = parse_to_dict("class A:\n    a = 1\n    b = 2\n")
        cls = gp["A"]
        # insert `c` between a and b by rebuilding dict order
        items = list(cls.items())
        cls.clear()
        for k, v in items:
            cls[k] = v
            if k == "a":
                cls["c"] = 3
        self.assertEqual(general_parse_to_str(gp), "class A:\n    a = 1\n    c = 3\n    b = 2\n")

    def test_append_class_var(self):
        gp = parse_to_dict("class A:\n    a = 1\n\n\nclass B:\n    pass\n")
        gp["A"]["z"] = "new"
        self.assertEqual(general_parse_to_str(gp), "class A:\n    a = 1\n    z = 'new'\n\n\nclass B:\n    pass\n")

    def test_delete_class_var_takes_its_comment(self):
        gp = parse_to_dict("class A:\n    a = 1\n\n    # about b\n    b = 2\n    c = 3\n")
        del gp["A"]["b"]
        self.assertEqual(general_parse_to_str(gp), "class A:\n    a = 1\n    c = 3\n")

    def test_reorder_class_vars_keeps_rhythm(self):
        text = "class A:\n    a = 1\n\n    b = 2\n    c = 3\n"
        gp = parse_to_dict(text)
        cls = gp["A"]
        b = cls.pop("b")
        cls["b"] = b       # a, c, b
        self.assertEqual(general_parse_to_str(gp), "class A:\n    a = 1\n\n    c = 3\n    b = 2\n")

    def test_reorder_with_edit_inside_moved_member(self):
        text = "class A:\n    a = 1\n    b = 2\n"
        gp = parse_to_dict(text)
        cls = gp["A"]
        a = cls.pop("a")
        cls["a"] = 10
        self.assertEqual(general_parse_to_str(gp), "class A:\n    b = 2\n    a = 10\n")

    def test_reorder_params(self):
        gp = parse_to_dict("def f(a=1, b=2, c=3):\n    pass\n")
        p = gp["f"]["parameters"]
        a = p.pop("a")
        p["a"] = a
        self.assertEqual(general_parse_to_str(gp), "def f(b=2, c=3, a=1):\n    pass\n")

    def test_add_and_remove_call_kwarg(self):
        gp = parse_to_dict("configure(debug=True, level=3)\n")
        call = gp["configure()"]
        del call["level"]
        call["name"] = "x"
        self.assertEqual(general_parse_to_str(gp), "configure(debug=True, name='x')\n")

    def test_remove_all_call_kwargs(self):
        gp = parse_to_dict("configure(debug=True)\n")
        del gp["configure()"]["debug"]
        self.assertEqual(general_parse_to_str(gp), "configure()\n")

    def test_dict_literal_pairs(self):
        text = "kw = {'a': 1, 'b': 2}\n"
        gp = parse_to_dict(text)
        d = gp["kw"]
        d["b"] = 20
        d["c"] = 3
        del d["a"]
        self.assertEqual(general_parse_to_str(gp), "kw = {'b': 20, 'c': 3}\n")

    def test_list_grow_and_shrink(self):
        gp = parse_to_dict("xs = [1, 2, 3]\n")
        gp["xs"] = [1, 2, 3, 4]
        self.assertEqual(general_parse_to_str(gp), "xs = [1, 2, 3, 4]\n")
        gp = parse_to_dict("xs = [1, 2, 3]\n")
        gp["xs"] = [1]
        self.assertEqual(general_parse_to_str(gp), "xs = [1]\n")

    def test_add_module_level_call(self):
        gp = parse_to_dict("x = 1\n")
        gp["y"] = 2
        self.assertEqual(general_parse_to_str(gp), "x = 1\ny = 2\n")

    def test_delete_function_local(self):
        gp = parse_to_dict("def f():\n    a = 1\n    b = 2\n    return a\n")
        del gp["f"]["locals"]["b"]
        self.assertEqual(general_parse_to_str(gp), "def f():\n    a = 1\n    return a\n")


class TestComments(unittest.TestCase):
    def test_edit_leading_comment(self):
        gp = parse_to_dict("class A:\n    # old text\n    a = 1\n")
        key = next(k for k in gp["A"] if isinstance(k, Comment))
        gp["A"][key] = Comment("# new text")
        self.assertEqual(general_parse_to_str(gp), "class A:\n    # new text\n    a = 1\n")

    def test_edit_multiline_comment_group(self):
        gp = parse_to_dict("class A:\n    # one\n    # two\n    a = 1\n")
        key = next(k for k in gp["A"] if isinstance(k, Comment))
        self.assertEqual(str(key), "# one\n# two")
        gp["A"][key] = Comment("# uno\n# dos\n# tres")
        self.assertEqual(general_parse_to_str(gp), "class A:\n    # uno\n    # dos\n    # tres\n    a = 1\n")

    def test_edit_trailing_comment(self):
        gp = parse_to_dict("a = 1  # old\n")
        key = next(k for k in gp if isinstance(k, Comment))
        self.assertEqual(key.inline, "a")
        gp[key] = Comment("# new", inline="a")
        self.assertEqual(general_parse_to_str(gp), "a = 1  # new\n")

    def test_delete_comment(self):
        gp = parse_to_dict("class A:\n    # gone\n    a = 1\n")
        key = next(k for k in gp["A"] if isinstance(k, Comment))
        del gp["A"][key]
        self.assertEqual(general_parse_to_str(gp), "class A:\n    a = 1\n")

    def test_add_comment_before_member(self):
        gp = parse_to_dict("class A:\n    a = 1\n    b = 2\n")
        cls = gp["A"]
        items = list(cls.items())
        cls.clear()
        c = Comment("# about b")
        for k, v in items:
            if k == "b":
                cls[c] = c
            cls[k] = v
        self.assertEqual(general_parse_to_str(gp), "class A:\n    a = 1\n    # about b\n    b = 2\n")

    def test_edit_class_override(self):
        gp = parse_to_dict(SAMPLE)
        gp["Toggles"]["__overrides__"]["tint"] = (0.9, 0.8, 0.7)
        out = general_parse_to_str(gp)
        self.assertIn("# [tint=(0.90, 0.80, 0.70)]\nclass Toggles:", out)

    def test_add_class_override(self):
        gp = parse_to_dict("class A:\n    a = 1\n")
        gp["A"]["__overrides__"] = {"tint": (0.1, 0.2, 0.3)}
        self.assertEqual(general_parse_to_str(gp), "# [tint=(0.10, 0.20, 0.30)]\nclass A:\n    a = 1\n")

    def test_add_override_on_decorated_nested_class(self):
        gp = parse_to_dict("class A:\n    @dec\n    class B:\n        a = 1\n")
        gp["A"]["B"]["__overrides__"] = {"z_offset": 2}
        self.assertEqual(general_parse_to_str(gp),
                         "class A:\n    # [z_offset=2]\n    @dec\n    class B:\n        a = 1\n")

    def test_remove_class_override(self):
        gp = parse_to_dict("# [tint=(0.1, 0.2, 0.3)]\nclass A:\n    a = 1\n")
        gp["A"]["__overrides__"] = {}
        self.assertEqual(general_parse_to_str(gp), "class A:\n    a = 1\n")

    def test_field_override_edit_and_add(self):
        gp = parse_to_dict("class A:\n    # [tint=(1.0, 0.0, 0.0)]\n    a = 1\n    b = 2\n")
        ov = gp["A"]["__overrides__"]
        ov["__a__"]["tint"] = (0.0, 1.0, 0.0)
        ov["__b__"] = {"min_value": 0}
        self.assertEqual(general_parse_to_str(gp),
                         "class A:\n    # [tint=(0.00, 1.00, 0.00)]\n    a = 1\n    # [min_value=0]\n    b = 2\n")

    def test_multiline_override_keeps_lines(self):
        text = "class A:\n    # [tint=(0.1, 0.2, 0.3),\n    # bg_offset=5]\n    a = 1\n"
        gp = parse_to_dict(text)
        gp["A"]["__overrides__"]["__a__"]["bg_offset"] = 6
        self.assertEqual(general_parse_to_str(gp),
                         "class A:\n    # [tint=(0.10, 0.20, 0.30),\n    # bg_offset=6]\n    a = 1\n")


class TestOffsets(unittest.TestCase):
    def test_non_ascii_before_edit(self):
        text = "icon = '\uf054'\nspeed = 1\nname = 'ü'  # ✓ ok\nnext_val = 2\n"
        gp = parse_to_dict(text)
        gp["speed"] = 2
        gp["next_val"] = 3
        self.assertEqual(general_parse_to_str(gp),
                         "icon = '\uf054'\nspeed = 2\nname = 'ü'  # ✓ ok\nnext_val = 3\n")

    def test_crlf(self):
        text = "a = 1\r\nb = 2\r\n"
        gp = parse_to_dict(text)
        gp["b"] = 3
        gp["c"] = 4
        self.assertEqual(general_parse_to_str(gp), "a = 1\r\nb = 3\r\nc = 4\r\n")

    def test_no_trailing_newline_append(self):
        gp = parse_to_dict("a = 1")
        gp["b"] = 2
        self.assertEqual(general_parse_to_str(gp), "a = 1\nb = 2\n")


class TestParity(unittest.TestCase):
    """Same keys, same primitive values as the libcst converter."""

    SNIPPETS = [
        SAMPLE,
        "x = 1\ny: int = 2\nz = 'a'\n",
        "class A:\n    a = (1, 2)\n    b = [1, 'x']\n    c = {'k': 1}\n    d = -3.5\n    e = None\n",
        "def f(a, b=2, *args, c=3, **kw):\n    x = 1\n    x = 2\n    x = 3\n    return x\n",
        "def g():\n    if a:\n        p = 1\n    if b:\n        q = 2\n    else:\n        q = 3\n",
        "def h():\n    for a in items:\n        v = 1\n    for a in items:\n        v = 2\n",
        "def t():\n    try:\n        a = 1\n    except (OSError, ValueError):\n        a = 2\n    else:\n        a = 3\n",
        "class A:\n    def __init__(self):\n        self.x = 1\n        self.y = 'q'\n\n    def m(self, k=1):\n        z = 2\n",
        "@window(name='w')\n@other\nclass W:\n    x = 1\n",
        "foo(1, 2, k=3)\nfoo(k=4)\n",
        "def f(a, b):\n    pass\n\nf(1, 2)\n",
    ]

    @staticmethod
    def _compare(a, b, where):
        ka = [k for k in a if not (isinstance(k, str) and k.startswith("__"))]
        kb = [k for k in b if not (isinstance(k, str) and k.startswith("__"))]
        assert [str(k) for k in ka] == [str(k) for k in kb], f"{where}: {ka} != {kb}"
        for k in ka:
            va, vb = a[k], b[k]
            assert type(va) is type(vb) or (isinstance(va, dict) and isinstance(vb, dict)), \
                f"{where}.{k}: {type(va)} vs {type(vb)}"
            if isinstance(va, dict):
                TestParity._compare(va, vb, f"{where}.{k}")
            elif isinstance(va, Comment):
                assert str(va) == str(vb) and va.inline == vb.inline, f"{where}.{k}"
            else:
                assert values_equal(va, vb), f"{where}.{k}: {va!r} vs {vb!r}"

    def test_snippets(self):
        for i, text in enumerate(self.SNIPPETS):
            with self.subTest(i=i):
                ours = parse_to_dict(text)
                theirs = cst_module_to_dict(cst.parse_module(text))
                self._compare(ours, theirs, f"snippet {i}")


class TestUpdateInPlace(unittest.TestCase):
    def test_keeps_unchanged_objects(self):
        gp = parse_to_dict(SAMPLE)
        toggles = gp["Toggles"]
        editor = toggles["TextEditor"]
        tint = toggles["tint"]
        new_text = SAMPLE.replace("speed = 3.0", "speed = 4.0")
        update_in_place(gp, new_text)
        self.assertIs(gp["Toggles"], toggles)
        self.assertIs(gp["Toggles"]["TextEditor"], editor)
        self.assertIs(gp["Toggles"]["tint"], tint)
        self.assertEqual(gp["Toggles"]["speed"], 4.0)
        self.assertEqual(gp[ORIGIN_KEY].text, new_text)
        # and the round trip is against the NEW residual
        self.assertEqual(general_parse_to_str(gp), new_text)
        gp["Toggles"]["speed"] = 5.0
        self.assertEqual(general_parse_to_str(gp), SAMPLE.replace("speed = 3.0", "speed = 5.00"))

    def test_kept_object_is_the_diff_baseline(self):
        gp = parse_to_dict("x = (1, 2)\n")
        t = gp["x"]
        update_in_place(gp, "x = (1, 2)\ny = 3\n")
        self.assertIs(gp["x"], t)
        self.assertEqual(diff(gp), [])


if __name__ == "__main__":
    unittest.main()
