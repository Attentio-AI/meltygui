"""_recompile must heal a stale module twin missing a newer import.

Reproduces the @defaults NameError: the same file lives in sys.modules under
two identities; the twin imported BEFORE an import line was added to the file
never re-runs it, so re-exec'ing a span whose decorator/default uses the new
name (`@defaults(tint=...)`) raises NameError in that twin's globals. The fix
(_backfill_declared_imports) execs the file's declared-but-missing imports
into the module namespace and retries once.
"""

import os
import sys
import textwrap
import types

_root = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(_root, 'src'))
sys.path.insert(0, _root)

from meltygui.code.file_converters import _backfill_declared_imports
from meltygui.code.file_converters import _recompile


# The file's CURRENT text: imports `dumps` and decorates with a helper that
# uses it. The stale twin below was "imported" before those lines existed.
_FILE_TEXT = textwrap.dedent("""\
    from json import dumps
    from functools import lru_cache


    def target(x=1):
        return dumps({"x": x})
""")

_NEW_SPAN = textwrap.dedent("""\
    @lru_cache(maxsize=None)
    def target(x=2):
        return dumps({"x": x})
""")


def _stale_module(tmp_path):
    """A live module for the file WITHOUT the imports (pre-edit state)."""
    p = tmp_path / "twin_mod.py"
    p.write_text(_FILE_TEXT)
    mod = types.ModuleType("twin_mod_t")
    mod.__file__ = str(p)
    # Only the def early in this twin's life - no dumps, no lru_cache.
    exec(compile("def target(x=1):\n    return None\n", str(p), "exec"),
         vars(mod))
    return p, mod


def test_backfill_declared_imports(tmp_path):
    p, mod = _stale_module(tmp_path)
    ns = vars(mod)
    assert "dumps" not in ns and "lru_cache" not in ns
    assert _backfill_declared_imports(ns, str(p))
    assert "dumps" in ns and "lru_cache" in ns
    # Second call, nothing missing → nothing added.
    assert not _backfill_declared_imports(ns, str(p))


class _NullCache:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def test_recompile_retries_after_backfill(tmp_path, monkeypatch):
    from meltygui.melty import Melty
    if getattr(Melty, "cache", None) is None:
        monkeypatch.setattr(Melty, "cache", _NullCache(), raising=False)
    p, mod = _stale_module(tmp_path)
    func = vars(mod)["target"]
    assert "lru_cache" not in vars(mod)
    # Without the backfill this raised NameError('lru_cache') re-running the
    # decorator; with it the hotswap lands and the body resolves dumps too.
    _recompile(func, _NEW_SPAN, str(p))
    assert "lru_cache" in vars(mod) and "dumps" in vars(mod)
    assert func(3) == '{"x": 3}'
