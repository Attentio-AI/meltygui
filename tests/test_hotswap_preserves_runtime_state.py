"""
Hotswap applies SOURCE edits and preserves RUNTIME state.

Reproduces the meltygui.py hotswap failure (2026-08-23): `class Melty` keeps all
runtime state in class attributes (`cache = None`, ...), and `_hotswap_class`
copied every freshly-compiled initializer over the live class, so a swap reset
`Melty.cache` to None and the next frame died in `Melty.cache.invalidate_up`.
meltygui.py also ends with `Core.melty = Melty`, which the re-exec bound to the
THROWAWAY class.

The rule: a plain data attribute (class or module level) keeps its live value
when its source expression is unchanged against the baseline stamped by
`stamp_hotswap_baselines` / the previous swap; a changed expression applies.
Without a baseline, a compiled None / empty container over a populated live
value is kept.
"""

import importlib.util
import os
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

_root = os.path.join(os.path.dirname(__file__), '..')

from meltygui.melty import Melty
from meltygui.code.file_converters import _recompile_module
from meltygui.code.file_converters import _recompile_class
from meltygui.code.file_converters import stamp_module_baseline
from meltygui.code.file_converters import _attr_source_map

SOURCE_V1 = """\
import types

class Backend:
    pass

class Single:
    cache = None
    flag = True
    items = []
    limit = 5
    backend = Backend()

    class Inner:
        x = 1
        cache = None

    def describe(self):
        return "v1"

registry = {}
handler = Backend()
holder = types.SimpleNamespace()
holder.obj = Single
"""

SOURCE_V2 = SOURCE_V1.replace("limit = 5", "limit = 6") \
                     .replace('return "v1"', 'return "v2"') \
                     .replace("x = 1", "x = 2")


class _StubCache:
    """Stands in for Melty.cache: no-op invalidations, an empty draw_state table."""
    key_to_draw_state = {}

    def __getattr__(self, name):
        if name.startswith("invalidate"):
            return lambda *a, **k: None
        raise AttributeError(name)


def _load(source, name):
    d = tempfile.mkdtemp()
    path = Path(d) / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod, path


class HotswapPreservesRuntimeState(unittest.TestCase):

    def setUp(self):
        self._cache = getattr(Melty, "cache", None)
        if self._cache is None:
            Melty.cache = _StubCache()

    def tearDown(self):
        Melty.cache = self._cache

    def test_attr_source_map_keys(self):
        m = _attr_source_map(SOURCE_V1)
        self.assertEqual(m["Single"]["cache"], "None")
        self.assertEqual(m["Single.Inner"]["x"], "1")
        self.assertEqual(m[""]["registry"], "{}")
        self.assertNotIn("obj", m[""])   # attribute target is not a Name

    def test_runtime_state_survives_with_baseline(self):
        mod, path = _load(SOURCE_V1, "hs_baseline")
        stamp_module_baseline(mod, SOURCE_V1)
        Single = mod.Single
        marker = object()
        Single.cache = marker
        Single.flag = False
        Single.items.append(1)
        items, backend, registry, handler, inner = (
            Single.items, Single.backend, mod.registry, mod.handler, Single.Inner)
        registry["a"] = 1
        inner_marker = object()
        Single.Inner.cache = inner_marker

        err = _recompile_module(mod, SOURCE_V2, str(path))
        self.assertIsNone(err)

        self.assertIs(mod.Single, Single)
        self.assertIs(Single.cache, marker)            # None-initialized, runtime-filled
        self.assertIs(Single.flag, False)              # non-empty initializer, runtime-flipped
        self.assertIs(Single.items, items)
        self.assertEqual(Single.items, [1])
        self.assertIs(Single.backend, backend)         # empty initializer, kept
        self.assertEqual(Single.limit, 6)              # edited expression applies
        self.assertEqual(Single().describe(), "v2")    # methods patched
        self.assertIs(mod.registry, registry)
        self.assertEqual(mod.registry, {"a": 1})
        self.assertIs(mod.handler, handler)
        self.assertIs(mod.holder.obj, Single)          # `holder.obj = Single` re-pointed to the live class
        self.assertIs(Single.Inner, inner)             # nested identity survives
        self.assertEqual(Single.Inner.x, 2)
        self.assertIs(Single.Inner.cache, inner_marker)

    def test_edited_expression_beats_runtime_value(self):
        mod, path = _load(SOURCE_V1, "hs_edit_wins")
        stamp_module_baseline(mod, SOURCE_V1)
        mod.Single.limit = 99
        self.assertIsNone(_recompile_module(mod, SOURCE_V2, str(path)))
        self.assertEqual(mod.Single.limit, 6)

    def test_no_baseline_heuristic_then_stamped(self):
        mod, path = _load(SOURCE_V1, "hs_nobase")
        Single = mod.Single
        marker = object()
        Single.cache = marker
        Single.flag = False
        self.assertIsNone(_recompile_module(mod, SOURCE_V2, str(path)))
        self.assertIs(Single.cache, marker)   # None → populated: kept by the heuristic
        self.assertIs(Single.flag, True)      # no baseline: a non-empty initializer applies
        # The swap stamped a baseline, so the next unchanged swap keeps runtime state.
        Single.flag = False
        self.assertIsNone(_recompile_module(mod, SOURCE_V2, str(path)))
        self.assertIs(Single.flag, False)

    def test_class_span_recompile_keeps_state(self):
        mod, path = _load(SOURCE_V1, "hs_span")
        stamp_module_baseline(mod, SOURCE_V1)
        Single = mod.Single
        marker = object()
        Single.cache = marker
        span = textwrap.dedent("""\
            class Single:
                cache = None
                flag = True
                items = []
                limit = 7
                backend = Backend()

                class Inner:
                    x = 1
                    cache = None

                def describe(self):
                    return "span"
            """)
        self.assertIsNone(_recompile_class(Single, span, str(path)))
        self.assertIs(Single.cache, marker)
        self.assertEqual(Single.limit, 7)
        self.assertEqual(Single().describe(), "span")

    def test_real_melty_module_swap(self):
        import meltygui.melty as melty_mod
        from meltygui.rendering.decorators.core_decoration import Core
        path = Path(melty_mod.__file__)
        source = path.read_text(encoding="utf-8")
        stamp_module_baseline(melty_mod, source)
        sentinel = _StubCache()
        Melty.cache = sentinel
        Melty.annotation_mode = False
        Melty.scroll_version = 4242
        err = _recompile_module(melty_mod, source, str(path))
        self.assertIsNone(err)
        self.assertIs(melty_mod.Melty, Melty)
        self.assertIs(Melty.cache, sentinel)
        self.assertIs(Melty.annotation_mode, False)
        self.assertEqual(Melty.scroll_version, 4242)
        self.assertIs(Core.melty, Melty)


if __name__ == "__main__":
    unittest.main()
