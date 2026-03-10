"""
Automatic converter chaining via shortest-path search.

Given a registry of direct A→B converters, this module finds multi-hop
paths (A→X→…→B) and composes them into a single callable.  The shortest
path (fewest hops) is always chosen to minimise data loss.

Usage:
    result = convert(my_obj, str, registry=Melty)
    chain  = find_chain(MyType, str, registry=Melty)   # returns the composed fn
"""

from __future__ import annotations

import types
from collections import deque
from enum import Enum
from typing import Any, Callable, TypeVar

from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace

T = TypeVar("T")

# Sentinel cached for type pairs with no conversion path,
# so we don't re-run BFS every frame.
_NO_PATH = object()
NO_VALUE = object()


class PendingState(Enum):
    BACKGROUND = "background"
    CONFIRM = "blocking"
    BROKEN_PATH = "broken_path"
    ERROR = "error"


# ── Pending wrapper ──────────────────────────────────────────────────────────

class Pending:
    """Wraps a converter result that hasn't been fully applied yet.

    Converters that support deferred execution (file I/O, network calls,
    etc.) return Pending(partial_result) when apply=False.  The UI can
    inspect pending.wrapped for metadata, then call convert() again with
    apply=True when ready.

    When a Pending is encountered mid-chain, the chain stops immediately
    and returns the Pending as-is.  The caller can inspect the partial
    result and re-run with apply=True to complete.

    Usage:
        result = convert(path, dict, registry=R, apply=False)
        if isinstance(result, Pending):
            print(result.wrapped["name"])   # metadata available
            # later:
            full = convert(path, dict, registry=R, apply=True)
    """
    __slots__ = ("wrapped", "status", "state", "originated")

    def __init__(self, originated: types.FunctionType | type, wrapped=NO_VALUE, status="pending", state=PendingState.CONFIRM):
        self.wrapped = wrapped
        self.status = status
        self.state = state
        self.originated = originated

    def __repr__(self):
        return f"Pending({self.wrapped!r})"

    def __bool__(self):
        return self.wrapped is not None


# ── Apply introspection ──────────────────────────────────────────────────────

_accepts_apply_cache: dict[Callable, bool] = {}


def _accepts_apply(fn: Callable) -> bool:
    """Check (cached) whether a converter function accepts an 'apply' kwarg."""
    return True
    # cached = _accepts_apply_cache.get(fn)
    # if cached is not None:
    #     return cached
    # import inspect
    # try:
    #     sig = inspect.signature(fn)
    #     result = "apply" in sig.parameters
    # except (ValueError, TypeError):
    #     result = False
    # _accepts_apply_cache[fn] = result
    # return result
    #




def _is_path_type(item) -> bool:
    """True if item is a type (waypoint in a path), False if callable (edge)."""
    return isinstance(item, type)


def _find_converter(converters: dict, from_type: type, to_type: type) -> Callable | None:
    """Look up a converter with MRO fallback."""
    fn = converters.get((from_type, to_type))
    if fn is None and hasattr(from_type, "__mro__"):
        for ancestor in from_type.__mro__[1:]:
            fn = converters.get((ancestor, to_type))
            if fn is not None:
                break
    return fn


def convert(value: Any, target: type[T] = None, *, registry, path: list | None = None, apply: bool = False, cache_id=None) -> T:
    """Convert *value* to *target* type using the registry.

    If *path* is provided, follows it exactly.  Path items can be:
      - types (waypoints):    looked up in the registry
      - callables (edges):    called directly on the current value

    Examples:
        # All types — registry lookup between each consecutive pair
        convert(obj, dict, registry=R, path=[str, cst.Module, dict])

        # All functions — called in sequence
        convert(obj, registry=R, path=[my_parser, my_formatter])

        # Mixed — types trigger registry lookup, functions called directly
        convert(obj, dict, registry=R, path=[str, my_custom_parser, dict])

    If *apply* is False (default), converters that support deferred
    execution may return Pending(partial_result).  Pass apply=True
    to force full execution (e.g. file writes, network calls).

    If any link in the chain returns Pending, the chain stops
    immediately and returns that Pending.  The caller can inspect
    the partial result and re-run with apply=True to complete.

    Otherwise, tries a direct converter first, then BFS for a multi-hop
    path through intermediate types.  Raises TypeError if no path exists.
    """
    if target is not None:
        if isinstance(value, target):
            return value  # type: ignore[return-value]

    if cache_id is None:
        from src.lsd.gl_gui.melty import Melty
        cache_id = Melty.unique_stack[-1] if Melty.unique_stack else None

    if path is not None:
        return _run_explicit_path(value, target, cache_id=cache_id, path=path, registry=registry, apply=apply)



    # Value-aware shortcut: dicts produced by object_to_dict carry a
    # __meta__ key with the original class info.  The graph can't know
    # that dict→object will produce the right type at runtime, so we
    # try it here before falling into BFS.
    converters = getattr(registry, "_converters", {})
    dict_to_obj = converters.get((dict, object))

    if dict_to_obj is not None:
        meta_dict = None

        if isinstance(value, dict) and "__meta__" in value:
            meta_dict = value
        else:
            # The value might be one hop away from a __meta__ dict
            # (e.g. a list of pairs that returned from dict_to_list).
            # Try converting to dict first and check.
            try:
                to_dict_fn = find_chain(type(value), dict, registry=registry)
                candidate = to_dict_fn(value)
                if isinstance(candidate, dict) and "__meta__" in candidate:
                    meta_dict = candidate
            except (TypeError, Exception):
                pass

        if meta_dict is not None:
            try:
                result = dict_to_obj(meta_dict)
                if isinstance(result, target):
                    return result  # type: ignore[return-value]
                # Result isn't the target yet, but it's more specific
                # than `object` - try one more conversion from here.
                if not isinstance(result, type(value)):  # avoid loops
                    try:
                        final = convert(result, target, registry=registry)
                        return final
                    except (TypeError, ValueError):
                        pass
            except Exception:
                pass

    chain_fn = find_chain(type(value), target, registry=registry)

    # If we need to pass apply, decompose the chain and walk step by step
    steps = getattr(chain_fn, "__converter_chain__", None)
    if steps is not None:
        return _run_chain_steps(value, steps,cache_id=cache_id, apply=apply)
    # Direct call (single hop)
    return chain_fn(value, cache_id=cache_id, apply=apply)


def _run_explicit_path(value: Any, target: type, *, path: list, registry, apply: any, cache_id=None) -> Any:
    """Follow an explicit path of types and/or converter functions.

    Each path item is either:
      - a type:     a waypoint — if the value isn't already this type,
                    look up a converter from type(value) → this type
      - a callable: an explicit edge — called directly on the value

    Examples:
        path=[str, cst.Module, dict]
          → ensure value is str, convert str→Module, convert Module→dict

        path=[my_parser, my_formatter]
          → call my_parser(value), then my_formatter(result)

        path=[str, my_custom_parser, dict]
          → ensure value is str, call my_custom_parser, convert →dict

    If any step returns Pending, the chain stops and returns it.
    Raises TypeError if a type→type edge is missing from the registry.
    """
    converters = getattr(registry, "_converters", {})
    result = value
    last_fn = None
    fn_name = "N/A"

    for i, item in enumerate(path):


        if _is_path_type(item):
            # Type waypoint - skip if already there, otherwise look up
            if isinstance(result, item):
                continue

            fn = _find_converter(converters, type(result), item)
            if fn is None:
                # print(last_fn.__name__ if last_fn else "N/A", item.__name__)
                # raise TypeError(
                #     f"No converter registered for {type(result).__name__!r} → "
                #     f"{item.__name__!r} (step {i + 1} of explicit path)"
                # )
                print_stack_trace(watch=["path", "target", "result", "item", "apply",  "value", "cache_id", "last_fn", "fn_name"])
                print(f"Missing converter for {type(result).__name__!r} -> {item.__name__!r} at step {i + 1} of explicit path")

                return Pending(originated=fn, state=PendingState.BROKEN_PATH, wrapped=item, status=f"Missing converter for {type(result).__name__!r} -> {item.__name__!r} at step {i + 1} of explicit path")

            # if apply is not False and apply is not None:
            #     if fn.__name__ == apply.__name__:
            #         do_apply = True
            #         print(f"Applying at step {i + 1} of explicit path via {getattr(item, '__name__', repr(item))}")
            #     else:
            #         do_apply = False
            #         print(
            #             f"Not applying at step {i + 1} of explicit path: apply={apply}, item={fn} ")
            # else:
            #     do_apply = False
            last_fn = fn
            result = fn(result, cache_id=cache_id, apply=apply)
        else:
            # Callable edge - skip if already the target type
            type_info = getattr(registry, "_converter_to_type", {}).get(item)
            if type_info is not None:
                _, to_type = type_info
                if isinstance(result, to_type):
                    result = value
                    continue


            result = item(result, cache_id=cache_id, apply=apply)

        if isinstance(result, Pending):
            return result

    return result


def _run_chain_steps(value: Any, steps: list[Callable], *, apply: bool, cache_id=None) -> Any:
    """Walk a list of converter functions, passing apply and handling Pending.

    If any step returns Pending, stops immediately and returns it.
    """
    result = value

    if apply:
        pass

    for fn in steps:
        result = fn(result, cache_id=cache_id, apply=apply)
        if isinstance(result, Pending):
            return result

    return result


def find_chain(source: type, target: type, *, registry) -> Callable:
    """Return a single callable that converts *source* → *target*.

    Builds a directed graph from every converter in the registry, runs
    BFS to find the shortest path, composes the converters along that
    path into a single function, and caches the result.

    The graph and all discovered chains are cached on the registry itself.
    Call invalidate_cache() after registering new converters at runtime.

    Raises TypeError if no path can be found.
    """
    # ── Cache lookup ─────────────────────────────────────────────────

    if not hasattr(registry, "_chain_cache"):
        registry._chain_cache = {}
    key = (source, target)
    if key in registry._chain_cache:
        cached = registry._chain_cache[key]
        if cached is _NO_PATH:
            raise TypeError(
                f"No conversion path from {source.__name__!r} to {target.__name__!r} (cached)"
            )
        return cached

    converters = getattr(registry, "_converters", {})

    # ── Direct lookup with MRO fallback ──────────────────────────────
    #
    # Check for an exact (source, target) converter first, then walk the
    # source's MRO so that e.g. ValueError finds Exception→dict.

    direct = converters.get((source, target))
    if direct is None and hasattr(source, "__mro__"):
        for ancestor in source.__mro__[1:]:
            direct = converters.get((ancestor, target))
            if direct is not None:
                break

    if direct is not None:
        registry._chain_cache[key] = direct
        return direct

    # ── Build the type graph ─────────────────────────────────────────
    #
    # Adjacency list built from every registered (from_type, to_type)
    # pair.  Cached on the registry so it's only built once.

    if not hasattr(registry, "_graph_cache") or registry._graph_cache is None:
        graph: dict[type, list[type]] = {}
        for (from_type, to_type) in converters.keys():
            graph.setdefault(from_type, []).append(to_type)
        registry._graph_cache = graph
    graph = registry._graph_cache

    # ── Find starting types via MRO ───────────────────────────────
    #
    # If source isn't directly in the graph, we can still start from any
    # registered ancestor type.  Sort by MRO distance so BFS explores
    # the most specific converters first (ValueError before Exception
    # before object).

    all_from_types = set(graph.keys())
    start_entries: list[tuple[int, type]] = []
    for rt in all_from_types:
        if rt == source:
            start_entries.append((0, rt))
        elif isinstance(source, type) and isinstance(rt, type) and issubclass(source, rt):
            try:
                distance = source.__mro__.index(rt)
            except ValueError:
                distance = 999
            start_entries.append((distance, rt))
    start_entries.sort(key=lambda x: x[0])
    start_types = [t for _, t in start_entries] if start_entries else [source]

    # ── BFS ──────────────────────────────────────────────────────────
    #
    # Standard breadth-first search over the type graph.  Each queue
    # entry is a full path (list of types) so we can reconstruct the
    # chain when we hit the target.

    queue: deque[list[type]] = deque()
    visited: set[type] = {source}
    for st in start_types:
        # When starting from an MRO ancestor, begin the path there
        # directly - the value IS an instance of the ancestor, so no
        # converter is needed for that step.
        queue.append([st])
        visited.add(st)

    path: list[type] | None = None
    while queue:
        current_path = queue.popleft()
        current = current_path[-1]

        if current == target or (isinstance(current, type) and issubclass(current, target)):
            path = current_path
            break

        for neighbour in graph.get(current, []):
            if neighbour not in visited:
                visited.add(neighbour)
                new_path = current_path + [neighbour]
                if neighbour == target:
                    path = new_path
                    break
                queue.append(new_path)
        if path is not None:
            break

    if path is None:
        registry._chain_cache[key] = _NO_PATH
        raise TypeError(
            f"No conversion path from {source.__name__!r} to {target.__name__!r}"
        )

    # ── Collect edge converters ──────────────────────────────────────

    edge_fns: list[Callable] = []
    for i in range(len(path) - 1):
        step_from, step_to = path[i], path[i + 1]
        fn = converters.get((step_from, step_to))
        if fn is None and hasattr(step_from, "__mro__"):
            for ancestor in step_from.__mro__[1:]:
                fn = converters.get((ancestor, step_to))
                if fn is not None:
                    break
        assert fn is not None, f"BFS found edge {step_from} → {step_to} but lookup failed"
        edge_fns.append(fn)

    # ── Compose into a single callable ───────────────────────────────

    if len(edge_fns) == 1:
        registry._chain_cache[key] = edge_fns[0]
        return edge_fns[0]

    fns = list(edge_fns)  # bind for closure

    def composed(value: Any) -> Any:
        result = value
        for fn in fns:
            result = fn(result)
        return result

    type_names = [source.__name__]
    # If the path starts from an MRO ancestor, show the actual start
    if path[0] != source:
        type_names.append(path[0].__name__)
    for fn in edge_fns:
        ret = getattr(fn, "__annotations__", {}).get("return")
        if isinstance(ret, type):
            type_names.append(ret.__name__)
        elif isinstance(ret, str):
            type_names.append(ret)
        else:
            type_names.append("?")

    chain_str = " → ".join(type_names)
    composed.__name__ = f"chain[{chain_str}]"
    composed.__qualname__ = composed.__name__
    composed.__doc__ = (
        f"Auto-composed converter: {chain_str}\n"
        f"Hops: {len(edge_fns)}\n"
        f"Steps: {', '.join(getattr(fn, '__name__', '?') for fn in edge_fns)}"
    )
    composed.__converter_chain__ = edge_fns  # type: ignore[attr-defined]
    composed.__converter_path__ = chain_str  # type: ignore[attr-defined]

    registry._chain_cache[key] = composed
    return composed


def invalidate_cache(registry) -> None:
    """Call this after registering new converters at runtime to rebuild
    the graph and clear cached chains.
    """
    registry._chain_cache = {}
    registry._graph_cache = None


def invert_path(path: list, *, registry) -> list:
    """Return the reverse of a mixed path, swapping each callable for its inverse.

    Types are kept as-is (just reversed).  Callables are replaced with
    their inverse via registry.converter_flags[fn].

    Example:
        path = [str, cst.Module, my_custom_fn, dict]
        invert_path(path, registry=Melty)
        → [dict, inverse_of_my_custom_fn, cst.Module, str]

    Raises KeyError if a callable has no registered inverse.
    """
    flags = getattr(registry, "converter_flags", {})
    result = []
    for item in reversed(path):
        if _is_path_type(item):
            result.append(item)
        else:
            inverse = flags.get(item, {}).get("inverse_of", None)
            if inverse is None:
                raise KeyError(
                    f"No inverse registered for {getattr(item, '__name__', repr(item))}. "
                    f"Register with @converter(registry=..., inverse_of=...)"
                )
            result.append(inverse)
    return result


def explain_chain(source: type, target: type, *, registry) -> str:
    """Return a human-readable explanation of how source → target would
    be converted, without actually performing the conversion.
    """
    if isinstance(source, type) and issubclass(source, target):
        return f"{source.__name__} is already a subclass of {target.__name__}"

    try:
        chain_fn = find_chain(source, target, registry=registry)
    except TypeError as e:
        return str(e)

    path = getattr(chain_fn, "__converter_path__", None)
    steps = getattr(chain_fn, "__converter_chain__", [])

    lines = [f"Path: {path or '(direct)'}"]
    for i, fn in enumerate(steps, 1):
        lines.append(f"  Step {i}: {getattr(fn, '__name__', '?')}")
    return "\n".join(lines)


def all_reachable_from(source: type, *, registry) -> set[type]:
    """Return every type reachable from *source* through any chain of
    converters.  Walks the full graph via BFS from source (including
    MRO-matched ancestors) and collects every type visited.
    """
    converters = getattr(registry, "_converters", {})

    graph: dict[type, list[type]] = {}
    for (from_type, to_type) in converters.keys():
        graph.setdefault(from_type, []).append(to_type)

    start_types = [source]
    for rt in graph.keys():
        if rt != source and isinstance(source, type) and isinstance(rt, type) and issubclass(source, rt):
            start_types.append(rt)

    visited: set[type] = set(start_types)
    visited.add(source)
    queue = deque(start_types)

    while queue:
        current = queue.popleft()
        for neighbour in graph.get(current, []):
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append(neighbour)

    visited.discard(source)
    return visited


def all_paths(source: type, target: type, *, registry, max_depth: int = 6) -> list[list[type]]:
    """Return ALL paths from source → target up to max_depth hops,
    sorted shortest-first.  Useful for understanding why a particular
    chain was chosen and what alternatives exist.
    """
    converters = getattr(registry, "_converters", {})

    graph: dict[type, list[type]] = {}
    for (from_type, to_type) in converters.keys():
        graph.setdefault(from_type, []).append(to_type)

    start_types = [source]
    for rt in graph.keys():
        if rt != source and isinstance(source, type) and isinstance(rt, type) and issubclass(source, rt):
            start_types.append(rt)

    results: list[list[type]] = []

    def dfs(path: list[type], visited: set[type]) -> None:
        if len(path) > max_depth + 1:
            return
        current = path[-1]
        if current == target and len(path) > 1:
            results.append(list(path))
            return
        for neighbour in graph.get(current, []):
            if neighbour not in visited:
                visited.add(neighbour)
                path.append(neighbour)
                dfs(path, visited)
                path.pop()
                visited.discard(neighbour)

    for st in start_types:
        initial = [source] if st == source else [source, st]
        dfs(initial, set(initial))

    results.sort(key=len)
    return results