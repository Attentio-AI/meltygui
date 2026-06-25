"""
pkl_inspect — make a binary load_save_v2 .pkl debuggable.

  python -m src.lsd.gl_gui.utils.pkl_inspect [path/to/custom.pkl] [--depth N]

Loads the pkl and prints the object tree with, per field, the SERIALIZED size
(re-pickling just that subtree) and node count — sorted biggest-first, so you can
see exactly what dominates the file and spot anything that's growing.
"""
import sys
from enum import Enum

from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.model.dict_conversion_util import ClassUtility
from src.lsd.gl_gui.utils import load_save_v2 as v2
from src.lsd.gl_gui.utils import graph_compare as gc

_PRIM = (int, float, bool, str, bytes, type(None))


def _size(v):
    try:
        return len(v2.dumps(v, excluded=list(v2.STUDIO_SAVE_EXCLUDED)))
    except Exception:
        return -1


def _children(o):
    """(label, value) pairs one level down — public fields / dict items / seq items."""
    if isinstance(o, dict):
        return [(f"[{k!r}]", val) for k, val in o.items()]
    if isinstance(o, (list, tuple)):
        return [(f"[{i}]", val) for i, val in enumerate(o)]
    if isinstance(o, DictConversion) or hasattr(o, "__dict__"):
        return [(f".{k}", getattr(o, k, None)) for k in gc._public_keys(o)]
    return []


def _kind(o):
    if isinstance(o, Enum):
        return f"{type(o).__name__}.{o.name}"
    if isinstance(o, _PRIM):
        return type(o).__name__
    n = ""
    if isinstance(o, dict):
        n = f"({len(o)})"
    elif isinstance(o, (list, tuple, set)):
        n = f"({len(o)})"
    return f"{type(o).__name__}{n}"


def dump_tree(o, depth=2, label="root", indent=0, min_kb=0.0):
    sz = _size(o)
    nodes = gc.count_nodes(o) if not isinstance(o, _PRIM) else 0
    pad = "  " * indent
    szs = f"{sz/1024:8.1f} KB" if sz >= 0 else "   (n/a)"
    print(f"{szs}  {nodes:>7} nodes  {pad}{label}: {_kind(o)}")
    if indent >= depth or isinstance(o, _PRIM) or isinstance(o, Enum):
        return
    kids = _children(o)
    # sort children by serialized size, biggest first; skip tiny ones past depth 0
    sized = sorted(((_size(v), lbl, v) for lbl, v in kids), key=lambda t: -t[0])
    for s, lbl, v in sized:
        if s / 1024.0 < min_kb:
            continue
        dump_tree(v, depth, lbl, indent + 1, min_kb)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else "/home/lukas/Desktop/latent-descent/custom.pkl"
    depth = 2
    for a in sys.argv[1:]:
        if a.startswith("--depth"):
            depth = int(a.split("=")[-1]) if "=" in a else int(sys.argv[sys.argv.index(a) + 1])
    ClassUtility().initialize_class_names("src")
    sys.setrecursionlimit(1_000_000)
    import os
    print(f"file: {path}  ({os.path.getsize(path)/1024/1024:.2f} MB on disk)\n")
    root = v2.load(path, run_on_load=False)
    print(f"{'serialized':>11}  {'graph':>7}        field: type\n{'-'*70}")
    dump_tree(root, depth=depth, min_kb=1.0)
    print("\n(sizes are each subtree re-pickled in isolation; shared objects counted "
          "under each referrer, so children can sum to more than the parent)")


if __name__ == "__main__":
    main()
