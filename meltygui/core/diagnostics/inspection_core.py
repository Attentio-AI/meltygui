# fastinspect.py
from __future__ import annotations
import inspect
import weakref
from types import FunctionType, MethodType
from typing import Mapping

# Mapping of function -> (signature, code_object_id)
# WeakKeyDictionary avoids memory leaks when functions go out of scope.
_SIG_CACHE: "weakref.WeakKeyDictionary[object, tuple[inspect.Signature, int | None]]" = weakref.WeakKeyDictionary()


def _unwrap_callable(obj):
    """Normalize bound methods, functools.partial, and wrappers to a base callable."""
    # Bound method -> underlying function (keep method object as key if you prefer)
    if isinstance(obj, MethodType):
        return obj.__func__
    # functools.partial exposes .func
    func = getattr(obj, "func", None)
    if func is not None and callable(func):
        return func
    # Many wrappers set __wrapped__
    return inspect.unwrap(obj)


def _code_id(func) -> int | None:
    """Stable identifier for the current implementation of a Python function."""
    code = getattr(func, "__code__", None)
    return id(code) if code is not None else None  # For builtins/extension funcs returns None


def get_signature(callable_obj) -> inspect.Signature:
    """
    Fast, safe signature fetch with caching and auto-invalidating when the function's
    code object changes (i.e., after hot-reload).
    """
    base = _unwrap_callable(callable_obj)
    current_code_id = _code_id(base)

    cached = _SIG_CACHE.get(base)
    if cached is not None:
        sig, cached_code_id = cached
        if cached_code_id == current_code_id:
            return sig  # up-to-date

    # Compute once; this is the slow part we want to avoid doing repeatedly
    sig = inspect.signature(base)

    # Store in WeakKeyDictionary
    _SIG_CACHE[base] = (sig, current_code_id)

    # Bonus: stamp __signature__ so *future* inspect.signature(base) is O(1)
    try:
        setattr(base, "__signature__", sig)
    except Exception:
        # Not all callables allow setting attributes (e.g., many builtins)
        pass

    return sig


import inspect
from functools import partial
from types import FunctionType, MethodType
from typing import Mapping, Any


def set_runtime_defaults_in_place(fn: Any, new_defaults: Mapping[str, Any]) -> bool:
    """
    Change the *runtime* defaults of a callable so that calling it uses the new values.
    Supports:
      - plain Python functions
      - bound/unbound methods (updates the underlying function)
      - classmethod/staticmethod (via bound method's __func__)
      - functools.partial (you normally want to rebuild/patch the partial; see note)

    Returns True if runtime defaults were updated; False if unsupported (e.g., builtins).
    """
    # 1) Find the underlying Python function object we can mutate.
    base = fn
    # bound method -> underlying function
    if isinstance(base, MethodType):
        base = base.__func__
    # functools.partial (defaults live on the wrapped func; partial itself has args/keywords)
    if isinstance(base, partial):
        base = base.func
    # unwrap decorator wrappers
    base = inspect.unwrap(base)

    # Only pure Python functions have writable __defaults__/__kwdefaults__
    if not isinstance(base, FunctionType):
        return False

    sig = inspect.signature(base)

    # Build a new parameter list with updated defaults by name
    params = []
    for p in sig.parameters.values():
        if p.name in new_defaults and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) and not isinstance(p.default, inspect.Parameter):
            p = p.replace(default=new_defaults[p.name])
        params.append(p)

    # Positional defaults must be a trailing contiguous block
    pos = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    flags = [p.default is not inspect._empty for p in pos]
    try:
        first = next(i for i, f in enumerate(flags) if f)
        if not all(flags[i] for i in range(first, len(flags))):
            # Invalid layout for runtime defaults; keep function usable, but don't set.
            return False
        pos_defaults = tuple(p.default if not isinstance(p.default, inspect.Parameter) else p.default for p in pos[first:]) if pos else None
    except StopIteration:
        pos_defaults = None

    # Keyword-only defaults: free-form dict
    kwonly = [p for p in params if p.kind is p.KEYWORD_ONLY]
    kwdefaults = {p.name: p.default for p in kwonly if p.default is not inspect._empty} or None

    # 2) Mutate the function object in place
    try:
        base.__defaults__ = pos_defaults
    except Exception:
        return False
    try:
        base.__kwdefaults__ = kwdefaults
    except Exception:
        # Some functions do not allow this; that's okay for functions without kw-only defaults
        pass

    return True

def set_fn_defaults(fn, new_defaults: Mapping[str, Any]) -> bool:
    ok = set_runtime_defaults_in_place(fn, new_defaults)
    # Update the inspect signature to match (optional but nice)
    # try:
    #     sig = inspect.signature(fn)
    #     new_params = [(p.replace(default=new_defaults[p.name])
    #                    if p.name in new_defaults and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    #                    else p)
    #                   for p in sig.parameters.values()]
    #     new_sig = inspect.Signature(new_params, return_annotation=sig.return_annotation)
    #     # set on the exact object and the unwrapped base for robustness
    #     setattr(fn, "__signature__", new_sig)
    #     setattr(inspect.unwrap(fn), "__signature__", new_sig)
    #
    # except Exception:
    #     pass
    return ok


def get_params(callable_obj) -> Mapping[str, inspect.Parameter]:
    """Convenience: directly get the .parameters mapping (ordered)."""
    return get_signature(callable_obj).parameters


def invalidate(callable_obj=None):
    """
    Invalidate the cache for one callable (or all, if None).
    Useful if you mutate __code__ in-place (rare); otherwise hot-reload creates
    new function objects and the cache auto-updates by key.
    """
    if callable_obj is None:
        _SIG_CACHE.clear()
        return
    base = _unwrap_callable(callable_obj)
    try:
        del _SIG_CACHE[base]
    except KeyError:
        pass
