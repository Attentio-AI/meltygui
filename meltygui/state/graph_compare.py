"""
graph_compare — structural + sharing-topology equality for object graphs.

This is the acceptance oracle for the load/save work: it proves that a
round-tripped graph reproduces the original. "Reproduces" means three things,
in increasing strength:

  1. same class at every node,
  2. same *serialized* public field values (the fields load/save round-trips),
  3. same SHARING TOPOLOGY — if two paths in graph A reach one shared object,
     the same two paths in graph B must reach one shared object too (and cycles
     close at the same place). This is the "object reference tree" property the
     whole exercise is about; plain field-equality misses it.

It deliberately compares only the fields that load/save is responsible for
(public, non-excluded, non-underscore) so that dropped transient state
(_parent weakrefs, GL handles, lambdas) doesn't register as a difference.
"""
from enum import Enum

_PRIMITIVE = (int, float, bool, str, bytes, type(None))


# Single source of truth: reuse v2's exact suppression set so the oracle can't
# drift from what's actually serialized (v1). @exclude/__excluded_attrs__ is NOT
# here (it only gates @live invalidation), so type/name/id is compared.
from meltygui.state.load_save_v2 import _suppressed as _v2_suppressed


def _public_keys(obj, excluded=()):
    """The attribute names load/save is responsible for: public, not suppressed.
    `excluded` mirrors the caller-level drop list passed to v2.dumps, so fields the
    save deliberately omits aren't flagged as round-trip diffs.

    Iterates the DEFAULT instance's keys, matching v2._save_state's delta encoding
    (which serializes only fields present on a fresh default). Fields that exist on
    `obj` but not on the default are runtime-injected and intentionally NOT
    round-tripped (to_dict drops them too), so they must not register as diffs."""
    excl = _v2_suppressed(obj, excluded)
    cls = type(obj)
    default = getattr(cls, "default_instance", None)
    src = default if default is not None else obj
    return sorted(k for k in vars(src) if not k.startswith("_") and k not in excl)


class Diff(list):
    """A list of human-readable difference strings; truthy iff graphs differ."""
    def report(self, limit=40):
        if not self:
            return "graphs are structurally equivalent (incl. sharing topology)"
        head = self[:limit]
        extra = f"\n  ... and {len(self) - limit} more" if len(self) > limit else ""
        return f"{len(self)} difference(s):\n  " + "\n  ".join(head) + extra


def compare(a, b, *, ignore=(), excluded=()):
    """Return a Diff describing how graph ``b`` deviates from graph ``a``.

    ``ignore`` is a set of attribute names skipped everywhere (e.g. {'id'} when
    ids are intentionally regenerated on load).
    ``excluded`` is the caller-level field-drop list passed to v2.dumps — those
    fields aren't serialized, so they're not compared.
    """
    diffs = Diff()
    ignore = set(ignore)
    excluded = frozenset(excluded)

    # identity maps: object in A -> object in B that it first paired with.
    # En catch sharing-topology divergence: if A-node X pairs with B-node Y once,
    # any later encounter of X must again pair with Y (and vice-versa), else the
    # aliasing/cycle structure differs between the two graphs.
    a_to_b = {}
    b_to_a = {}

    def walk(x, y, path):
        # --- primitives & enums: value equality ---
        # Compare enums by class QUALNAME + member name (not class identity): a
        # load/save round-trip can legitimately land an equivalent class object
        # under a different module identity; what matters is same enum, same member.
        if isinstance(x, Enum) or isinstance(y, Enum):
            xt = type(x).__qualname__ if isinstance(x, Enum) else f"<non-enum {type(x).__name__}>"
            yt = type(y).__qualname__ if isinstance(y, Enum) else f"<non-enum {type(y).__name__}>"
            if xt != yt or getattr(x, "name", None) != getattr(y, "name", None):
                diffs.append(f"{path}: enum {xt}.{getattr(x,'name','?')} != {yt}.{getattr(y,'name','?')}")
            return
        if isinstance(x, _PRIMITIVE) or isinstance(y, _PRIMITIVE):
            if type(x) is not type(y) or x != y:
                diffs.append(f"{path}: {x!r} ({type(x).__name__}) != {y!r} ({type(y).__name__})")
            return

        # --- sharing topology: enforce for consistent bijection on non-primitives ---
        xid, yid = id(x), id(y)
        if xid in a_to_b:
            if a_to_b[xid] is not y:
                diffs.append(f"{path}: sharing diverged — A node reused but B node differs "
                             f"(A first paired at a different B object)")
            return  # already fully compared on first encounter
        if yid in b_to_a and b_to_a[yid] is not x:
            diffs.append(f"{path}: sharing diverged — B node is shared but A node is not")
            return
        a_to_b[xid] = y
        b_to_a[yid] = x

        # --- callables / types: compare by reference identity of name ---
        if callable(x) or isinstance(x, type):
            xn = getattr(x, "__qualname__", repr(x))
            yn = getattr(y, "__qualname__", repr(y))
            if xn != yn:
                diffs.append(f"{path}: callable/type {xn} != {yn}")
            return

        # --- containers ---
        if isinstance(x, dict) or isinstance(y, dict):
            if not (isinstance(x, dict) and isinstance(y, dict)):
                diffs.append(f"{path}: dict vs non-dict ({type(x).__name__}/{type(y).__name__})")
                return
            xk, yk = set(x.keys()), set(y.keys())
            if xk != yk:
                only_a = sorted(map(repr, xk - yk))[:6]
                only_b = sorted(map(repr, yk - xk))[:6]
                diffs.append(f"{path}: dict keys differ  only-A={only_a}  only-B={only_b}")
            for k in xk & yk:
                walk(x[k], y[k], f"{path}[{k!r}]")
            return
        if isinstance(x, (list, tuple)) or isinstance(y, (list, tuple)):
            if type(x) is not type(y):
                diffs.append(f"{path}: seq type {type(x).__name__} != {type(y).__name__}")
                return
            if len(x) != len(y):
                diffs.append(f"{path}: length {len(x)} != {len(y)}")
            for i in range(min(len(x), len(y))):
                walk(x[i], y[i], f"{path}[{i}]")
            return
        if isinstance(x, (set, frozenset)) or isinstance(y, (set, frozenset)):
            if x != y:
                diffs.append(f"{path}: set contents differ")
            return

        # --- objects by attributes ---
        if type(x) is not type(y):
            diffs.append(f"{path}: class {type(x).__name__} != {type(y).__name__}")
            return
        if not hasattr(x, "__dict__"):
            if x != y:
                diffs.append(f"{path}: {type(x).__name__} {x!r} != {y!r}")
            return
        ka = [k for k in _public_keys(x, excluded) if k not in ignore]
        kb = [k for k in _public_keys(y, excluded) if k not in ignore]
        if ka != kb:
            sa, sb = set(ka), set(kb)
            diffs.append(f"{path} <{type(x).__name__}>: fields differ  "
                         f"only-A={sorted(sa - sb)}  only-B={sorted(sb - sa)}")
        for k in ka:
            if k in ignore or k not in kb:
                continue
            walk(getattr(x, k), getattr(y, k), f"{path}.{k}")

    walk(a, b, "root")
    return diffs


def count_nodes(obj):
    """How many distinct non-primitive objects the graph reaches (sanity sizing)."""
    seen = set()

    def walk(o):
        if isinstance(o, _PRIMITIVE) or isinstance(o, Enum):
            return
        if id(o) in seen:
            return
        seen.add(id(o))
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple, set, frozenset)):
            for v in o:
                walk(v)
        elif hasattr(o, "__dict__"):
            for k in _public_keys(o):
                walk(getattr(o, k))

    walk(obj)
    return len(seen)
