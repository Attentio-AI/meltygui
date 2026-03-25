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

from src.lsd.gl_gui.view.core_conversion.path_finder import Pending


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

    for func in chain:
        if not changed:
            break

        changed, value, _ = _step(func, value, **kwargs)

        # Pending stops the chain: downstream can't run without data
        if isinstance(value, Pending):
            return False, value

    return changed, value
