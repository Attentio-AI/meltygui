"""The UI path language: leaf-anchored selectors over the live draw_state
tree, used by change_value / orchestration cues to name a target the way a
person would — by the minimal distinguishing tail of its ancestry.

A PATH is a sequence of SEGMENTS matched as a SUBSEQUENCE of the target's
ancestor chain, anchored at the leaf: the last segment matches the target
itself, the earlier ones match ancestors in order with any depth of
unnamed/unmatched levels between (descendant matching is the default, so
nesting is never the caller's problem unless it is the distinguisher).

Segment kinds — each one is a data-description level:
    "alpha"          name (display name, the "##"-stripped str(ds.name))
    1                ordinal: the nth match (visual order: abs_top, abs_left)
                     of the REMAINING path within the current scope —
                     "Loras/[1]/alpha" = the 2nd alpha under Loras. (A
                     per-node "nth child of its parent" reading was tried
                     and rejected: single-child wrapper levels made it
                     match nearly everything.)
    Key("33071d")    exact full name (no "##" strip) — collection keys
    Lora             type of the value the draw_state renders
    callable         predicate fn(ds) -> bool

Resolution requires UNIQUENESS: zero matches raise NoMatch, several raise
Ambiguous listing each candidate's minimal_path — the error is the
disambiguation. String sugar: "Loras/[1]/alpha" parses to ("Loras", 1,
"alpha"); tuples are the full-power form (Key/type/callable segments).

Everything takes an optional `universe` (iterable of draw_states) so the
resolver is headless-testable on fakes; the default universe is the live
cache (Melty.cache.key_to_draw_state), with draw_states under a CLOSED
window filtered out (their tiles persist across sessions but are not on
screen). Persisted-but-collapsed subtrees DO resolve — deciding whether a
resolved target is actually hittable is the gate check's job, not the
resolver's.
"""


class Key:
    """Exact full-name segment (collection keys): matches str(ds.name)
    verbatim, no "##" stripping."""

    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def __repr__(self):
        return f"Key({self.key!r})"


class NoMatch(LookupError):
    def __init__(self, path, hint=""):
        self.path = path
        super().__init__(f"no draw_state matches {format_path(path)}"
                         + (f" ({hint})" if hint else ""))


class Ambiguous(LookupError):
    def __init__(self, path, candidates):
        self.path = path
        self.candidates = candidates
        options = ", ".join(format_path(c) for c in candidates[:8])
        super().__init__(f"{format_path(path)} matches {len(candidates)} targets — "
                         f"use one of: {options}")


def format_path(path):
    if isinstance(path, str):
        return repr(path)
    parts = []
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        elif isinstance(segment, Key):
            parts.append(f"{{{segment.key}}}")
        elif isinstance(segment, type):
            parts.append(segment.__name__)
        elif callable(segment):
            parts.append(getattr(segment, "__name__", "<predicate>"))
        else:
            parts.append(str(segment))
    return "/".join(parts)


def parse(path):
    """String sugar -> segment tuple. "Loras/[1]/alpha" -> ("Loras", 1,
    "alpha"). Tuples/lists pass through. Key/type/callable segments exist
    only in tuple form."""
    if not isinstance(path, str):
        return tuple(path)
    segments = []
    for raw in path.split("/"):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            segments.append(int(raw[1:-1]))
        elif raw.startswith("{") and raw.endswith("}"):
            segments.append(Key(raw[1:-1]))
        else:
            segments.append(raw)
    if not segments:
        raise ValueError("empty path")
    return tuple(segments)


def display_name(ds):
    name = getattr(ds, "name", None)
    return str(name).split("##")[0] if name is not None else ""


def full_name(ds):
    name = getattr(ds, "name", None)
    return str(name) if name is not None else ""


def ancestor_chain(ds, cap=64):
    """ds's ancestors, nearest first (ds itself excluded). Follows `_parent`;
    the root's _parent is ITSELF, which terminates the walk."""
    chain = []
    node = getattr(ds, "_parent", None)
    while node is not None and node is not ds and len(chain) < cap:
        chain.append(node)
        parent = getattr(node, "_parent", None)
        if parent is node:
            break
        node, ds = parent, node
    return chain


def _under_closed_window(ds):
    node, steps = ds, 0
    while node is not None and steps < 64:
        if getattr(node, "closed", False):
            return True
        window = getattr(node, "parent_window", None)
        if window is None or window is node:
            return False
        node, steps = window, steps + 1
    return False


def default_universe():
    from src.lsd.gl_gui.melty import Melty
    cache = getattr(Melty, "cache", None)
    if cache is None:
        return []
    return [ds for ds in cache.key_to_draw_state.values()
            if ds is not None and not _under_closed_window(ds)]


def segment_matches(segment, ds):
    if isinstance(segment, str):
        return display_name(ds) == segment
    if isinstance(segment, (bool, int)):   # ordinals are handled by the caller
        return False
    if isinstance(segment, Key):
        return full_name(ds) == segment.key
    if isinstance(segment, type):
        value = getattr(ds, "_raw_input_value", None)
        return value is not None and isinstance(value, segment)
    if callable(segment):
        try:
            return bool(segment(ds))
        except Exception:
            return False
    return False


def _visual_order(matches):
    return sorted(matches, key=lambda ds: (getattr(ds, "abs_top", 0) or 0,
                                           getattr(ds, "abs_left", 0) or 0))


def _matches_in_scope(segment, scope, universe):
    """Every universe node matching `segment` with `scope` among its
    ancestors (scope None = anywhere). One hop of the path = descendant
    matching at any depth, which is what makes nesting the caller's
    non-problem."""
    found = []
    for ds in universe:
        if not segment_matches(segment, ds):
            continue
        if scope is not None and scope is not ds \
                and not any(a is scope for a in ancestor_chain(ds)):
            continue
        found.append(ds)
    return found


def _resolve_set(segments, scopes, universe):
    """Segment-at-a-time descent (equivalent to subsequence matching for
    named segments). An int segment indexes the VISUALLY-ORDERED result of
    the remaining path within the current scopes and returns that single
    node — "the nth ⟨rest⟩ here"."""
    if not segments:
        return scopes
    head, rest = segments[0], segments[1:]
    if isinstance(head, int) and not isinstance(head, bool):
        results = _visual_order(_resolve_set(rest, scopes, universe))
        return [results[head]] if 0 <= head < len(results) else []
    next_scopes, seen = [], set()
    for scope in scopes:
        for ds in _matches_in_scope(head, scope, universe):
            if id(ds) not in seen:
                seen.add(id(ds))
                next_scopes.append(ds)
    return _resolve_set(rest, next_scopes, universe)


def find_all(path, within=None, universe=None):
    segments = parse(path)
    if universe is None:
        universe = default_universe()
    return _resolve_set(segments, [within], list(universe))


def resolve(path, within=None, universe=None):
    """The path's unique live target, or a teaching error: NoMatch names the
    path, Ambiguous lists each candidate as its own minimal_path so the
    error message IS the disambiguation."""
    matches = find_all(path, within=within, universe=universe)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NoMatch(parse(path))
    if universe is None:
        universe = default_universe()
    raise Ambiguous(parse(path),
                    [minimal_path(ds, universe=universe) for ds in matches])


def name_chain(ds, cap=64):
    """Root-first display names of ds's ancestry + itself, unnamed levels
    skipped. This is what cues CAPTURE (maximal); minimal_path trims LATE."""
    names = [display_name(node) for node in reversed(ancestor_chain(ds, cap))]
    names.append(display_name(ds))
    return tuple(name for name in names if name)


def minimal_path(ds, universe=None):
    """Shortest name-suffix of ds's chain that resolves uniquely in
    `universe` — computed against the tree it must disambiguate within,
    never at capture time. Falls back to the full chain when even that is
    ambiguous (twins: caller needs an ordinal segment)."""
    if universe is None:
        universe = default_universe()
    universe = list(universe)
    chain = name_chain(ds)
    if not chain:
        return chain
    for length in range(1, len(chain) + 1):
        suffix = chain[-length:]
        matches = find_all(suffix, universe=universe)
        if len(matches) == 1:
            return suffix
    return chain
