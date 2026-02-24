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

from collections import deque
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Sentinel cached for type pairs with no conversion path,
# so we don't re-run BFS every frame.
_NO_PATH = object()


def convert(value: Any, target: type[T], *, registry) -> T:
    """Convert *value* to *target* type using the registry.

    Tries a direct converter first, then searches for a multi-hop path
    through intermediate types.  Raises TypeError if no path exists.
    """
    if isinstance(value, target):
        return value  # type: ignore[return-value]

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

    chain = find_chain(type(value), target, registry=registry)
    return chain(value)


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

    print(f"Finding conversion path from {source.__name__} to {target.__name__}...")

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