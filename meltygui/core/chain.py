"""
Chain executor for the unified render pipeline.

A chain is a flat list of @render_func nodes.  Data flows left to right:

    [cls_to_cst, cst_to_dict, draw_collection, dict_to_cst, cst_to_cls]
     load side ──────────────> render <────────────── save side

Every node has the same calling convention:
    changed, value = func(input_value, **kwargs)

The executor walks the chain, passing the output of each node as the
input to the next.  Nodes that haven't changed return cached results
instantly.  The chain stops propagating when a node returns changed=False
(no downstream work needed), UNLESS the node has never run (first frame).

Usage:
    draw_any(my_class, chain=[cls_to_cst, cst_to_dict, draw_collection,
                               dict_to_cst, cst_to_cls])
"""

from meltygui.core.path_finder import Pending


def _step(func, value, **kwargs):
    """Execute one chain node.  Returns (changed, value, func_name)."""
    result = func(value, **kwargs)

    if isinstance(result, tuple) and len(result) == 2:
        changed, value = result
    else:
        changed = False
        value = result

    name = getattr(func, '__name__', None) or getattr(func, '__qualname__', repr(func))
    return changed, value, name


# Nodes a real user edit flows through. If a chain ends changed=True and NONE
# of these returned True, the change came from converters alone - exactly the
# "round-trip mutated state on its own" case we want to surface. Loose by name
# (matches whatever render_func wrappers expose) and overinclusive on purpose:
# false-positets are worse than the occasional missed case.
_INTERACTION_NODES = frozenset({
    "focus", "draw_collection", "draw_tuple", "draw_text", "button",
    "draw_int", "draw_str", "draw_float", "draw_bool", "draw_enum_tabs",
    "draw_dropdown", "draw_tuple_float",
})


def run_chain(chain, input_value, **kwargs):
    """Execute a chain of render_func nodes left to right.

    Each node is called as: changed, value = node(value, **kwargs)

    Returns (changed, value) — the output of the last node that ran.

    Propagation rules:
      - A node always runs if it has never produced a result (first frame).
      - After first frame, a node runs only if upstream returned changed=True.
      - If a node returns a Pending, the chain stops and returns the Pending.
    """
    value = input_value
    changed = True  # first frame always runs

    trace = []  # (name, changed_returned, value_type) - for round-trip diagnostics

    for func in chain:
        if not changed:
            break

        changed, value, name = _step(func, value, **kwargs)
        trace.append((name, changed, type(value).__name__))

        # Pending stops the chain: downstream can't run without data
        if isinstance(value, Pending):
            return False, value

    # ── Round-trip change detection ──────────────────────────────────────────
    # The chain ends changed=True iff a save-side node will (or might, via the
    # save_pending latch) write back. A real user edit always flows through an
    # interaction node (focus, the picker, etc.). If we get here with changed=True
    # but no interaction node returned True, only converters did - meaning a
    # round-trip mutated the value on its own. Logs only, behavior unchanged.
    if changed:
        interaction_fired = any(
            ch and n in _INTERACTION_NODES for n, ch, _ in trace)
        if not interaction_fired:
            converter_changes = [(n, vt) for n, ch, vt in trace if ch]
            if converter_changes:
                _log_round_trip_change(trace, converter_changes, kwargs)

    return changed, value


def _log_round_trip_change(trace, converter_changes, kwargs):
    """Dump a prominent record of a converter-only changed=True flow."""
    import sys
    ds = kwargs.get("draw_state")
    ds_id = getattr(ds, "id", None) or getattr(ds, "_tile_id", None)
    ds_name = getattr(ds, "name", None)
    marked = "  →  ".join(
        (f"\033[91m{n}*\033[0m" if ch else n) for n, ch, _ in trace)
    print(
        f"\n\033[93;1m[CHAIN ROUND-TRIP CHANGE — no user input]\033[0m "
        f"chain returned changed=True with no interaction node firing.",
        file=sys.stderr)
    print(f"  draw_state: name={ds_name!r} id={ds_id}", file=sys.stderr)
    print(f"  trace (* = node returned changed=True):", file=sys.stderr)
    print(f"    {marked}", file=sys.stderr)
    print(f"  suspect converters: "
          f"{[n for n, _ in converter_changes]}", file=sys.stderr)
    print(f"  → a downstream save may fire without user input", file=sys.stderr)
