"""
load_save_v2 — native-pickle load/save (Strategy B prototype).

Goal: reproduce the object graph that DictConversion.to_dict/from_dict produce —
including the SHARING TOPOLOGY (aliases + cycles) — but via pickle, in one C-driven
pass, and general enough to also serialize plain (non-DictConversion) Python objects.

It is NOT vanilla pickle. A custom Pickler/Unpickler pair re-hosts the four
resilience layers the dict system provides, so that none of pickle's sharp edges
(see module docstring of dict_conversion.py) bite:

  1. EXCLUSION   — drop _-prefixed + @exclude/@no_save attrs (mirrors to_dict's
                   selection); stub external resources (tensors/Module/GL/weakref)
                   to None via persistent_id. The serialized scope therefore equals
                   to_dict's scope: config/UI state, no GPU payload.
  2. SEEDING     — reconstruct via cls.__new__ + a cheap default-seed (__post_init__,
                   __field_defaults__) then OVERLAY saved state. Skips the slow cls()
                   path while still giving newly-added fields their class defaults.
  3. SCHEMA-SAFE — because state is overlaid onto a defaulted instance, adding /
                   removing / renaming a field degrades exactly like the current
                   system (new field -> default; removed field -> ignored).
  4. RESILIENCE  — enums reduced BY NAME (values churn here), callables by registry/
                   module reference, classes resolved through ClassUtility's fuzzy
                   finder so a moved/nested class still loads.

Post-load, load() walks the restored graph and fires on_load(vis, root), mirroring
from_dict's final pass.
"""
import copy
import io
import logging
import os
import pickle
import sys
import types
import weakref
from enum import Enum

# These imports are heavy but already resolved whenever the app is running.
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.model.dict_conversion_util import ClassUtility

log = logging.getLogger("load_save_v2")


# ────────────────────────────────────────────────────────────────────────────
# external-resource detection (stubbed to None via persistent_id)
# ────────────────────────────────────────────────────────────────────────────
# Names mirror the three existing exclude lists (compute_hash, deepcopy_exclude,
# the lsd_studio save call). We detect by TYPE so a stray handle anywhere in the
# graph is caught regardless of attribute name.
_EXTERNAL_TYPE_NAMES = {
    "Tensor", "Parameter",                       # torch
    "Module",                                    # nn.Module (matched via mro search)
    "RegisteredBuffer", "GLBuffer", "GLTexture", "Framebuffer",
    "DeviceAllocation", "RegisteredImage",       # pycuda
    "TensorXYRenderer", "VolumeRendererFBO",     # GL renderers (xy_renderer/xyz_renderer)
    "GLState",
}
_EXTERNAL_MODULE_HINTS = ("pycuda", "OpenGL", "glfw")


_WEAK_TYPES = (weakref.ReferenceType, weakref.ProxyType, weakref.CallableProxyType,
               weakref.WeakSet, weakref.WeakValueDictionary, weakref.WeakKeyDictionary)

# persistent_id runs on each object pickled - the type->bool verdict is memoized.
# WeakKeyDictionary so hotswapped/GC'd classes don't retain stale verdicts (M3).
_external_cache = weakref.WeakKeyDictionary()


def _torch_types():
    """Lazily resolve (Tensor, nn.Module) for a direct, alias-proof issubclass
    check alongside the string match (L3). Cached; torch is already imported when
    the app runs."""
    cached = getattr(_torch_types, "_cache", False)
    if cached is False:
        try:
            import torch
            cached = (torch.Tensor, torch.nn.Module)
        except Exception:
            cached = None
        _torch_types._cache = cached
    return cached


def _compute_is_external(t):
    if issubclass(t, _WEAK_TYPES):
        return True
    tt = _torch_types()
    if tt is not None and issubclass(t, tt):     # direct torch.Tensor / nn.Module
        return True
    if t.__name__ in _EXTERNAL_TYPE_NAMES:
        return True
    mod = getattr(t, "__module__", "") or ""
    if any(h in mod for h in _EXTERNAL_MODULE_HINTS):
        return True
    if mod in ("threading", "_thread", "queue", "multiprocessing"):
        return True
    for base in t.__mro__:               # torch tensors / modules / tokenizers (string fallback)
        bn, bm = base.__name__, getattr(base, "__module__", "") or ""
        if bn == "Tensor" and bm.startswith("torch"):
            return True
        if bm.startswith("torch.nn.modules"):
            return True
        if "PreTrainedTokenizer" in bn:
            return True
    return False


def _is_external(obj):
    """True for resources that must never be pickled inline (restored as None)."""
    t = type(obj)
    v = _external_cache.get(t)
    if v is None:
        v = _compute_is_external(t)
        try:
            _external_cache[t] = v
        except TypeError:        # non-weakreferenceable type key - we don't cache
            pass
    return v


_DATA_PRIMITIVE = (int, float, bool, complex, str, bytes, bytearray, type(None))


def _is_foreign(obj):
    """Catch-all for objects that are neither our data nor picklable Python state:
    C-extension handles (glfw windows, GL/imgui/pybind objects with a raw ``_ptr``),
    etc. They're stubbed to None — mirroring to_dict's "unknown non-primitive -> None"
    fallthrough, which is exactly why the current save survives a live root full of
    live handles.

    A plain Python object WITH a real instance ``__dict__`` is NOT foreign: the
    general-purpose reduce path handles it (so v2 works on arbitrary classes, not
    just DictConversion). Only handle-like objects with no picklable ``__dict__``
    are dropped."""
    if isinstance(obj, _DATA_PRIMITIVE):
        return False
    if isinstance(obj, (list, tuple, dict, set, frozenset)):
        return False
    if isinstance(obj, (Enum, type)):
        return False
    if isinstance(obj, DictConversion):
        return False
    if callable(obj):                       # functions/methods/handles -> reducer_override
        return False
    return not isinstance(getattr(obj, "__dict__", None), dict)


_HEAPTYPE = 1 << 9                           # Py_TPFLAGS_HEAPTYPE


def _unsafe_to_reconstruct(obj):
    """Final safety net for the generic reduce path: instances of NON-HEAP
    C-extension types can't be safely ``cls.__new__()``'d on load (weakref
    proxies, datetime, pybind/cython objects, …). Everything we genuinely handle
    is excluded here; the rest are dropped to None, mirroring to_dict dropping
    unknown non-primitives. Prevents 'object.__new__(X) is not safe' load crashes
    from any C object that slipped past the specific filters."""
    t = type(obj)
    if t.__module__ == "builtins":
        return False                        # int/str/list/tuple/dict/fn/... native
    if isinstance(obj, (Enum, type, DictConversion)):
        return False
    if isinstance(obj, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)):
        return False
    try:
        return not (t.__flags__ & _HEAPTYPE)
    except Exception:
        return False


_unpicklable_class_cache = weakref.WeakKeyDictionary()


def _compute_unpicklable_class(obj):
    mod = getattr(obj, "__module__", None)
    qn = getattr(obj, "__qualname__", None)
    if not mod or not qn or "<locals>" in qn:
        return True
    m = sys.modules.get(mod)
    if m is None:
        return True
    target = m                              # walk the qualname (handles nesting)
    for part in qn.split("."):
        target = getattr(target, part, None)
        if target is None:
            return True
    return target is not obj                # found a DIFFERENT object -> not referenceable


def _is_unpicklable_class(obj):
    """A class object pickle can't reference by qualname — dynamically generated
    (the bubbling converter classes: Bubbling_GeneralParse etc.) or `<locals>`.
    Stub to None for now (the bubbling classes are runtime-built; long-term they
    could be made pickleable). Mirrors pickle's own findability test."""
    if not isinstance(obj, type):
        return False
    v = _unpicklable_class_cache.get(obj)
    if v is None:
        v = _compute_unpicklable_class(obj)
        try:
            _unpicklable_class_cache[obj] = v
        except TypeError:
            pass
    return v


# ────────────────────────────────────────────────────────────────────────────
# schema-safe reconstruction
# ────────────────────────────────────────────────────────────────────────────
# Hardcoded suppression set, from from_dict's BASE_EXCLUDED (dict_conversion 72-75).
# The non-underscore members matter (underscore'd are caught by the _-rule):
#   class_names / hash / outliner_expanded_h / modules_imported.
# NOTE (recon correction): @exclude / __excluded_attrs__ does NOT suppress
# serialization - it only suppresses @live invalidation. The real serialization
# suppressors are __no_save__ (@no_save) and the instance's `excluded` attr
# (to_dict 232-249). So `id`/`tint`/`name` (which are only in the base @exclude)
# ARE serialized today - and will be here too, which also keeps `id` STABLE across
# the round-trip (resolving recon open-risk 6.5).
_BASE_SUPPRESS = frozenset({
    "class_names", "hash", "outliner_expanded_h", "modules_imported",
    "_parent", "_parent_key", "_children",
})

# The runtime-only field names the studio drops at its to_dict call site
# (lsd_studio.py:8567). The save call must pass this (or its own list) as
# `excluded=` so v2 doesn't serialize live GPU/GL-adjacent state / caches.
STUDIO_SAVE_EXCLUDED = frozenset({
    "search_results", "previous_mouse_x", "previous_mouse_y", "last_mouse_x", "last_mouse_y",
    "root", "ui_stack", "view_state", "tooltip_position", "tooltip_value", "state",
    "start_render_time", "selected_index", "tooltip_height", "total_render_time",
    "selected_box", "cuda_buffer", "buffer", "all_settings", "labels",
    "layer_hashes", "sub_layer_hashes",
})


def _suppressed(obj, excluded=()):
    s = set(_BASE_SUPPRESS)
    if excluded:
        s |= set(excluded)                            # caller-specific field drops (C1)
    ns = getattr(type(obj), "__no_save__", None)
    if ns:
        s |= set(ns)
    inst_excl = getattr(obj, "excluded", None)        # to_dict reads self.excluded
    if inst_excl:
        try:
            s |= set(inst_excl)
        except TypeError:
            pass
    return s


_MISSING = object()


def _equals_default(v, dv):
    """to_dict's omit rules: scalar/enum/tuple/None equal to default; an EMPTY
    container whose default is also empty; a DictConversion that IS the default
    (identity). Non-empty containers are always kept."""
    if isinstance(v, (int, float, str, bool, bytes, Enum, tuple, type(None))):
        try:
            return bool(v == dv)
        except Exception:
            return False
    if isinstance(v, (dict, list, set)):
        return len(v) == 0 and isinstance(dv, (dict, list, set)) and len(dv) == 0
    if isinstance(v, DictConversion):
        return v is dv
    return False


def _save_state(obj, excluded=()):
    """The attributes load/save owns, mirroring to_dict's selection AND its
    DELTA-FROM-DEFAULT encoding: public (non-_) ∩ not (BASE_EXCLUDED ∪
    caller-excluded ∪ __no_save__ ∪ self.excluded), with any field equal to the
    class default OMITTED. cls() reconstruction seeds the omitted fields back to
    their defaults on load, so the omission is lossless.

    Two reasons this matters (a full snapshot ballooned the file ~20x vs the
    legacy .ini and let runtime state accrete):
      * smaller blob — only diverging fields are written;
      * iterate the DEFAULT instance's public keys (like to_dict), so attributes
        that exist on `obj` but not on a fresh default (runtime-injected) are NOT
        serialized and can't accumulate across save/load cycles."""
    sup = _suppressed(obj, excluded)
    cls = type(obj)
    default = getattr(cls, "default_instance", None)
    if default is None:
        try:
            default = cls()
        except Exception:
            default = None
    # Iterate the default's public keys (to_dict parity); fall back to obj's own.
    src = default if default is not None else obj
    out = {}
    for k in list(vars(src).keys()):
        if k.startswith("_") or k in sup:
            continue
        v = getattr(obj, k, None)
        if isinstance(v, types.MethodType):          # @live-injected bound methods
            continue
        if default is not None:
            dv = getattr(default, k, _MISSING)
            if dv is not _MISSING and _equals_default(v, dv):
                continue
        out[k] = v
    return out


# The @live new_init injection plan (which names to copy into __dict__) depends
# only on the class - cache the dir(cls) walk ONCE per class, not per instance.
# WeakKeyDictionary so hotswapped classes don't pin stale plans (M3).
_live_inject_cache = weakref.WeakKeyDictionary()


def _live_inject_plan(cls):
    plan = _live_inject_cache.get(cls)
    if plan is None:
        plan = []
        try:
            from src.lsd.gl_gui.view.core_views.decoration.core_decoration import auto_eval
        except ImportError:
            auto_eval = ()                       # module absent - empty plan (L1)
        for name in dir(cls):
            try:
                attr = getattr(cls, name, None)
                if callable(attr) and getattr(attr, "_add_to_dict", False):
                    plan.append(("method", name))
                elif auto_eval and isinstance(attr, auto_eval) and attr.fget is not None:
                    plan.append(("auto_eval", name, attr))
            except Exception:
                continue                         # a single bad descriptor shouldn't nuke the rest
        try:
            _live_inject_cache[cls] = plan
        except TypeError:
            pass
    return plan


def _seed_defaults(obj):
    """Cheaply bring a __new__'d instance up to 'freshly constructed' defaults,
    WITHOUT the slow cls() path, so saved state can overlay and new fields default.

    Replicates the construction side-effects pickle's __new__ skips (recon §2):
      1. re-arm the @live init guard (so __post_init__'s setattrs don't invalidate)
      2. @live new_init descriptor injection (_add_to_dict methods + auto_eval)
      3. __post_init__ scaffolding (_parent/_children/id/hash/tint/_exclude_attrs)
      4. __field_defaults__ backfill (FieldMeta class-body field defaults)
      5. _instances registration
    Class-body field defaults not backfilled still resolve via the class attribute,
    so getattr(obj, new_field) returns the default regardless — schema-add safe.
    """
    cls = type(obj)
    # Must match @live new_setattr's guard name exactly (invalidation_decoration.py:19)
    # so __post_init__'s setattrs are suppressed. (M4's id(cls) rename would desync
    # this from @live's naming scheme; the identically-named-nested-class collision is
    # @live's documented behavior, not ours to change unilaterally.)
    init_flag = f"__{cls.__name__}_initializing__"
    object.__setattr__(obj, init_flag, True)
    try:
        # (2) @live new_init loop: inject _add_to_dict methods + auto_eval descrs
        # (plan is cached per-class so this is a short list walk, not dir(cls))
        for entry in _live_inject_plan(cls):
            try:
                if entry[0] == "method":
                    obj.__dict__[entry[1]] = getattr(obj, entry[1])
                else:
                    obj.__dict__[entry[1]] = entry[2].fget.__get__(obj, cls)
            except Exception:
                pass
        # (3) __post_init__
        post = getattr(obj, "__post_init__", None)
        if callable(post):
            try:
                post()
            except Exception:
                pass
        # (4) field defaults. Deepcopy MUTABLE containers so seeded instances don't
        # alias one shared class-default list/dict (H5 - FieldMeta.__call__ has the
        # same original bug; fix it once default-diffing omits these on save).
        for k, v in getattr(cls, "__field_defaults__", {}).items():
            if k not in obj.__dict__:
                if isinstance(v, (dict, list, set)):
                    try:
                        v = copy.deepcopy(v)
                    except Exception:
                        pass
                try:
                    object.__setattr__(obj, k, v)
                except Exception:
                    pass
        # (5) instance registry
        insts = getattr(cls, "_instances", None)
        if insts is not None:
            try:
                insts.add(obj)
            except Exception:
                pass
    finally:
        object.__setattr__(obj, init_flag, False)


def _reconstruct(cls):
    """Construct a FULLY-initialized default instance — exactly like from_dict's
    cls() — then let pickle overlay the saved public state. Running the real
    __init__ (via the metaclass __call__) is what makes the EXISTING schema
    handling work: every attribute __init__/__post_init__/FieldMeta would set is
    present, including underscore attrs set ONLY in __init__ (e.g.
    HyperparameterCollection._snapshot_visible) that aren't in the saved state and
    that the cheap __new__+__post_init__ path would miss. Pickle memoizes this
    object before applying state, so cycles/aliases still resolve correctly.

    Falls back to the cheap __new__ + seed only if cls() raises (an __init__ that
    needs args or throws) — better a partially-seeded object than a failed load."""
    try:
        return cls()
    except Exception:
        obj = cls.__new__(cls)
        _seed_defaults(obj)
        return obj


# ────────────────────────────────────────────────────────────────────────────
# enum-by-name + callable-by-reference (refactor-safe)
# ────────────────────────────────────────────────────────────────────────────
def _enum_ref(e):
    """Reduce an enum member by NAME against its by-reference class.

    The class object itself is pickled by reference (resolved through find_class,
    same path as every other class) so its IDENTITY is preserved — avoiding the
    dual-module-identity mismatch get_enum_value's string re-resolution caused.
    Name-first (values churn) with a value fallback for renamed members."""
    cls = type(e)
    v = e.value if isinstance(e.value, (int, float, str, bool, type(None))) else None
    return (_resolve_enum, (cls, e.name, v))


def _resolve_enum(cls, name, value):
    try:
        return cls[name]                      # name-first, cls identity preserved
    except KeyError:
        if value is not None:
            try:
                return cls(value)             # value fallback (renamed member)
            except ValueError:
                pass
        # Both lookups failed (member renamed AND value remapped/dropped). Current
        # system also degrades to None here - but make it VISIBLE (H2) so silent
        # field loss is diagnosable. Migration contract: keep deleted enum names as
        # stub aliases for back-compat.
        log.warning("load_save_v2: enum %s has no member %r (value=%r) — loading as None",
                    getattr(cls, "__qualname__", cls), name, value)
        return None


def _callable_ref(fn):
    """A function/handle pickle can't take by plain reference -> dict_conversion's
    resolvable marker (handles render-func registry handles + module refs)."""
    return (_resolve_callable, (DictConversion.serialize_callable(fn),))


def _resolve_callable(ref):
    if ref is None:
        return None
    return DictConversion.resolve_callable(ref)


def _needs_callable_ref(fn):
    """True only when pickle's default save_global would FAIL for this function:
    a lambda/<locals>, or — the hotswap case — the live object is no longer the
    same object as module.qualname (a recompiled @render_func). A NORMAL function
    whose module.qualname IS itself must return False, so it's saved by reference
    (save_global), NOT via a serialize_callable reduce. (Routing every function
    through serialize_callable is infinite recursion: the reduce's own callable
    `_resolve_callable` is itself a function that would route through
    serialize_callable, forever.)"""
    qn = getattr(fn, "__qualname__", "") or ""
    mod = getattr(fn, "__module__", None)
    if not mod or "<locals>" in qn or "<lambda>" in qn:
        return True
    m = sys.modules.get(mod)
    if m is None:
        return False        # not imported here; let save_global import + resolve it
    target = m
    try:
        for part in qn.split("."):
            target = getattr(target, part)
    except AttributeError:
        return True          # not findable by qualname -> save_global would fail
    return target is not fn   # identity mismatch (hotswapped) -> need a name ref


# ────────────────────────────────────────────────────────────────────────────
# Pickler / Unpickler
# ────────────────────────────────────────────────────────────────────────────
class _PicklerOverrides:
    # Shared by the C pickler (fast) and the pure-Python pickler (deep-chain safe).
    def __init__(self, *a, excluded=None, **k):
        super().__init__(*a, **k)
        # caller-level field-level drops (the studio's read-only list) - C1
        self._excluded = frozenset(excluded) if excluded else ()

    def persistent_id(self, obj):
        # Positive rule mirroring to_dict: KEEP primitives, enums, enums,
        # referenceable classes, callables, and our own DictConversion. DROP
        # EVERYTHING ELSE to None. to_dict only ever deep-serializes
        # DictConversion and lets all other object fall through to None... so a
        # live root full of helper objects / C handles / weakref proxies / etc
        # serializes cleanly without per-type whack-a-mole.
        if obj is None or isinstance(obj, (bool, int, float, complex, str, bytes, bytearray)):
            return None
        if isinstance(obj, (list, tuple, dict, set, frozenset)):
            return None
        if isinstance(obj, Enum):
            return None
        # External resources / weakrefs/proxies. Checked via type(obj) (proxy-safe:
        # a weakref proxy masquerades as its referent through __class__) BEFORE the
        # DictConversion check below, so a proxy-to-DictConversion is still dropped.
        if _is_external(obj):
            return "DROP"
        if isinstance(obj, type):
            return "DROP" if _is_unpicklable_class(obj) else None
        # Real functions/methods FIRST - kept (reducer_override / save_global by ref).
        # MUST precede the unpicklable-class check: type(a_function) is the C type
        # `function`, not exposed as builtins.function, so that check would wrongly
        # flag every function (incl. our own reduce callables) -> infinite-recursion
        # /'NoneType not type'. So whitelist genuine functions here.
        if isinstance(obj, (types.FunctionType, types.BuiltinFunctionType,
                            types.MethodType)):
            return None
        # ANY object whose class can't be referenced by qualname can't be
        # reconstructed -> drop it. Crucially this is BEFORE the callable-keep below,
        # because bubbling converter dicts are CALLABLE (they have __call__) yet
        # their class is dynamic: without this check'd reach pickle's default NEWOBJ
        # reduction, whose class arg then drops to None -> "NEWOBJ class argument
        # must be a type, not NoneType" on load.
        if _is_unpicklable_class(type(obj)):
            return "DROP"
        # Other callables whose class IS referenceable (e.g. _LazyRenderFunc) -> keep.
        if callable(obj):
            return None
        if isinstance(obj, DictConversion):
            return None                         # -> generic reduce in reducer_override
        return "DROP"                           # any other type -> stub to None (to_dict parity)

    def reducer_override(self, obj):
        # enums BY NAME
        if isinstance(obj, Enum):
            return _enum_ref(obj)
        # Functions/methods: ALWAYS reference by name (serialize_callable), never
        # by pickle's default save_global. save_global does an IDENTITY check
        # (the live object must equal module.qualname), which FAILS for any
        # hotswapped @render_func - the live function in the graph is a stale
        # object while the module attribute points to the recompiled one
        # ("not the same object as ...text_editor.draw_text"). Name resolution
        # via resolve_callable re-binds to the CURRENT object, surviving hotswap.
        if isinstance(obj, (types.FunctionType, types.BuiltinFunctionType,
                            types.MethodType)):
            if _needs_callable_ref(obj):                 # only when save_global fails
                ref = DictConversion.serialize_callable(obj)
                if ref is not None:
                    return (_resolve_callable, (ref,))
            return NotImplemented                        # normal fn -> save_global (no recursion)
        # Container SUBCLASSES (RenderHost / _BubblingDict are dict subclasses) ->
        # serialize as the PLAIN container plus CONTENTS, mirroring to_dict
        # (which recurses any dict/list/set as a plain one, dropping the subclass
        # type). pickle's DEFAULT for a container subclass is NEWOBJ(our_type),
        # which crashes when the type is DYNAMIC - it gets dropped to None ->
        # "NEWOBJ class argument must be a type, not NoneType". Items are pickled
        # recursively, so nested DictConversions/primitives work. (DictConversion
        # is skipped so its own reducer still runs.)
        if not isinstance(obj, DictConversion):
            t = type(obj)
            if t is not dict and isinstance(obj, dict):
                return (dict, (), None, None, iter(list(obj.items())))
            if t is not list and isinstance(obj, list):
                return (list, (), None, iter(list(obj)), None)
            if t is not set and isinstance(obj, set):
                return (set, (list(obj),))
            if t is not frozenset and isinstance(obj, frozenset):
                return (frozenset, (list(obj),))
        # Any OTHER callable that isn't a class (e.g. _LazyRenderFunc registry
        # handles, render_func wrappers): decide by whether serialize_callable can
        # produce a resolvable marker, not just a class-name substring (M1). If it
        # can't be referenced, fall through (pickle will error loudly, which is
        # correct - we don't silently drop any unknown callable).
        if callable(obj) and not isinstance(obj, type) and type(obj).__module__ != "builtins":
            if DictConversion.serialize_callable(obj) is not None:
                return _callable_ref(obj)
        # our domain objects -> cycles-safe reduce with filtered state (3-tuple
        # form so pickle memoizes the instance before applying state -> cycles OK).
        # Restricted to DictConversion (persistent_id has already dropped every
        # other object to None), mirroring to_dict's "only DictConversion is
        # deep-serialized" rule.
        if isinstance(obj, DictConversion):
            return (_reconstruct, (type(obj),), _save_state(obj, self._excluded))
        return NotImplemented


# Concrete picklers: C (fast) for the common case, pure-Python for deep chains
# (its recursion is plain Python recursion, controllable by setrecursionlimit).
class LSDPickler(_PicklerOverrides, pickle.Pickler):
    pass


class LSDPicklerPy(_PicklerOverrides, pickle._Pickler):
    pass


class _UnpicklerOverrides:
    def persistent_load(self, pid):
        if pid == "DROP":
            return None
        raise pickle.UnpicklingError(f"unknown persistent id {pid!r}")

    def find_class(self, module, name):
        # Try the normal path first (fast, correct when nothing moved).
        try:
            return super().find_class(module, name)
        except Exception:
            pass
        # Fuzzy fallback via ClassUtility - handles moved / nested / re-rooted
        # classes the same way instantiate_from_class_path does.
        try:
            ClassUtility().initialize_class_names("src")
        except Exception:
            pass
        obj = DictConversion.instantiate_from_class_path(f"{module}.{name}")
        if obj is not None:
            return type(obj)
        # last resort: re-raise the strict error for visibility
        return super().find_class(module, name)


class LSDUnpickler(_UnpicklerOverrides, pickle.Unpickler):
    pass


class LSDUnpicklerPy(_UnpicklerOverrides, pickle._Unpickler):
    pass


# ────────────────────────────────────────────────────────────────────────────
# public API
# ────────────────────────────────────────────────────────────────────────────
import threading

# The live graph is far deeper than Python's default 1000 recursion limit
# (the studio's to_dict guards against up to 10000, and unlike to_dict - which
# flattens DictConversions into a flat hash-table - pickle recurses the full
# reference chain). Deep recursion needs BOTH a large Python limit and a large
# thread stack, so run save/load on a worker thread with a big stack.
_RECURSION_LIMIT = 1_000_000
_STACK_SIZE = 256 * 1024 * 1024          # worker stack for the rare deep-graph
                                         # pure-Python fallback (only allocated then)


def _in_big_stack(fn):
    """Run fn() on a thread with a large stack + high recursion limit, so deep
    object graphs don't overflow the (small) caller-thread stack."""
    box = {}

    def run():
        old = sys.getrecursionlimit()
        sys.setrecursionlimit(_RECURSION_LIMIT)
        try:
            box["v"] = fn()
        except BaseException as e:          # propagate to the caller thread
            box["e"] = e
        finally:
            sys.setrecursionlimit(old)

    prev = None
    try:
        prev = threading.stack_size(_STACK_SIZE)
    except (ValueError, RuntimeError):
        prev = None
    t = threading.Thread(target=run, name="load_save_v2")
    t.start()
    t.join()
    if prev is not None:
        try:
            threading.stack_size(prev)
        except Exception:
            pass
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _dump_with(PicklerCls, obj, excluded):
    buf = io.BytesIO()
    PicklerCls(buf, protocol=5, excluded=excluded).dump(obj)
    return buf.getvalue()


def dumps(obj, excluded=None):
    """Pickle `obj` to bytes. `excluded` is the caller-level field-name drop list
    (pass the same list the studio gives to_dict, e.g. STUDIO_SAVE_EXCLUDED) so
    runtime-only buffers/caches aren't serialized (C1).

    Cycles are fine (pickle's memo handles them). Common case: the fast C pickler
    runs DIRECTLY on the calling thread — it self-limits at Python 3.12's
    C-recursion guard and raises a CATCHABLE RecursionError (no segfault). Only a
    genuinely deep reference chain falls back to the pure-Python pickler on a
    big-stack thread."""
    try:
        return _dump_with(LSDPickler, obj, excluded)
    except RecursionError:
        log.warning("load_save_v2: deep graph exceeded C pickler limit — "
                    "falling back to pure-Python pickler")
        return _in_big_stack(lambda: _dump_with(LSDPicklerPy, obj, excluded))


def loads(data, *, vis=None, root=None, run_on_load=True):
    # The pickle format is stable, so the C unpickler reads either pickler's bytes;
    # fall back to the pure-Python unpickler only if depth overflows the C one.
    try:
        obj = LSDUnpickler(io.BytesIO(data)).load()
    except RecursionError:
        log.warning("load_save_v2: deep graph exceeded C unpickler limit — "
                    "falling back to pure-Python unpickler")
        obj = _in_big_stack(lambda: LSDUnpicklerPy(io.BytesIO(data)).load())
    if run_on_load:
        _post_load(obj, vis=vis, root=root if root is not None else obj)
    return obj


def save(obj, path, excluded=None):
    """Atomic save: serialize FULLY first (so a dump failure leaves the existing
    pkl untouched — never a truncated/empty file), then temp-write + rename so a
    reader never sees a half-written pkl (which would EOFError on load)."""
    data = dumps(obj, excluded=excluded)          # may raise -> leave untouched
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)                          # atomic on POSIX


def load(path, *, vis=None, run_on_load=True):
    with open(path, "rb") as f:
        return loads(f.read(), vis=vis, run_on_load=run_on_load)


def _post_load(graph_root, *, vis=None, root=None):
    """Replicate from_dict's final pass (which __setstate__ can't, lacking context):
      1. collect every DictConversion into root._instantiated_objects (id->inst),
      2. fire on_load(vis, root) on each (mirrors dict_conversion 207-221).
    Studio-specific fixups (_parent_tensor_frame rebind, save_config pruning,
    draw_state_registry validation) belong in the eventual studio swap, not here."""
    seen = set()
    instantiated = {}
    stack = [graph_root]
    while stack:
        o = stack.pop()
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(o, dict):
            stack.extend(o.values())
            continue
        if isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
            continue
        d = getattr(o, "__dict__", None)
        if d is None:
            continue
        if isinstance(o, DictConversion):
            instantiated[getattr(o, "id", oid)] = o
        stack.extend(list(d.values()))

    if isinstance(root, DictConversion):
        try:
            object.__setattr__(root, "_instantiated_objects", instantiated)
        except Exception:
            pass

    for inst in instantiated.values():
        cb = getattr(inst, "on_load", None)
        if callable(cb):
            try:
                cb(vis=vis, root=root)
            except Exception:
                pass
