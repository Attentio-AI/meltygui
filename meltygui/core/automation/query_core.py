"""MCP Query window — the mcp_query.py collectors, run live in the studio.

The five MCP tools (`find_views`, `describe_view`, `hit_test`,
`param_sources`, `tile_cache`) are thin wrappers around one collector each:
the server hands `mcp_eval.request_call` a no-arg closure, the render thread
runs it between frames, and the answer is JSON-dumped back to the client.
This window is a second front end onto exactly those collectors — a
render_func body ALREADY runs on the render thread, so it calls
`mcp_query.collect_*` directly, with no queue and no server in the way. What
you read here is what an MCP client reads: the JSON tab dumps the answer the
way `run_query` does, so its bytes are the tool's bytes.

Pick a tool, edit its arguments, and the answer re-collects on every edit
(and on every Run press, since the studio moves under a fixed set of
arguments). The collector is looked up by NAME per run, so a hotswap of
mcp_query.py lands on the next frame — this window is the fast loop for
iterating on the collectors themselves.

`follow pointer` (hit_test only) re-collects at the live mouse position
every frame, which is the quickest way to sanity-check the BVH stack and the
subscription table: park the pointer over anything and read what claims it.
"""

import json
import time
import traceback

import meltygui_imgui as imgui

import meltygui.core.automation.mcp_query as mcp_query
from meltygui.core.melty import Melty
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window

# Tool name -> its arguments and their defaults, in the order the real tool
# declares them. This table IS the API: an argument dict is rendered by
# draw_collection, so adding an argument to a collector means adding it here
# and nothing else. The collector itself is resolved by name at call time
# (`collect_<tool>`), never bound here, so hotswapping mcp_query.py takes
# effect immediately instead of pinning the function objects this module
# imported. Same reason studio_data.py keeps _SOURCES module-level: a
# dictionary the window reads, not a tuning constant (those live in Toggles).
_TOOL_ARGS = {
    "find_views": {"func": "", "name": "", "window": "", "include_closed": False,
                   "limit": 50},
    "describe_view": {"view": "", "children_depth": 1},
    "hit_test": {"x": 0.0, "y": 0.0},
    "param_sources": {"view": "", "param": ""},
    "tile_cache": {"view": "", "history_frames": 200, "limit": 50},
}


from meltygui.state.query_state import MCPQueryState


def _seed_args(panel_state):
    """Reconcile the persisted arguments against `_TOOL_ARGS`.

    A tool or an argument added since the state was saved appears with its
    default; one that no longer exists is dropped. This is what lets a
    hotswap that adds an argument show it on the next frame.
    """
    args = panel_state.args
    for tool, defaults in _TOOL_ARGS.items():
        current = args.get(tool)
        if not isinstance(current, dict):
            current = args[tool] = {}
        for key, value in defaults.items():
            current.setdefault(key, value)
        for key in [key for key in current if key not in defaults]:
            del current[key]
    for tool in [tool for tool in args if tool not in _TOOL_ARGS]:
        del args[tool]
    if panel_state.tool not in _TOOL_ARGS:
        panel_state.tool = next(iter(_TOOL_ARGS))


def _pointer():
    """Where the input handler thinks the mouse is.

    The same numbers `collect_hit_test` reports as `pointer`, so a followed
    point reads `pointer_matches_point: true` and the subscription table is
    populated. imgui's own mouse position is the fallback for a frame before
    the handler has one.
    """
    handler = getattr(Melty, "event_handler", None)
    x = getattr(handler, "_cursor_x", None) if handler is not None else None
    y = getattr(handler, "_cursor_y", None) if handler is not None else None
    if x is None or y is None:
        x, y = imgui.get_mouse_pos()
    return round(float(x), 1), round(float(y), 1)


def _run(panel_state, tool, args):
    """Collect once, into the SAME result dict.

    The answer is copied into the held dict rather than replacing it so the
    result tree keeps its identity across runs — expanded rows stay expanded
    while you edit an argument, which is the whole point of following the
    "mutate in place" rule here. A collector that raises is reported in the
    window instead of taking the frame down with it: these walk live state
    and a half-built draw_state is exactly the case worth seeing.
    """
    collect = getattr(mcp_query, "collect_" + tool, None)
    started = time.perf_counter()
    if collect is None:
        answer, error = {}, f"mcp_query has no collect_{tool}"
    else:
        try:
            answer, error = collect(**args), None
        except Exception:
            answer, error = {}, traceback.format_exc()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    panel_state._result.clear()
    panel_state._result.update(answer)
    panel_state._error = error
    panel_state._json = json.dumps(answer, indent=1, default=str)
    panel_state._stamp = (f"{elapsed_ms:.2f} ms   ·   {len(panel_state._json)} B"
                          f"   ·   frame {Melty.frame_count}")


# disable_scroll=False: the window is a scrolling box of panes, and the
# @window code path defaults it to True.
from meltygui.view.query_view import draw_mcp_query
draw_mcp_query = window(icon='\uf188', display_name='MCP Query', tint=(0.1, 0.2, 0.28), initial={'width': 820, 'height': 700}, disable_scroll=False)(draw_mcp_query)
