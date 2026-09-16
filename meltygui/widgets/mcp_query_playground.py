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

import meltygui.mcp_query as mcp_query
from meltygui.melty import Melty
from meltygui.state.dict_conversion import DictConversion
from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.window_decoration import window
from meltygui.views.headers import flat_button

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


class MCPQueryState(DictConversion):
    """The window's own state, injected 1:1 with its draw_state.

    Everything without a leading underscore persists between sessions, so the
    window reopens on the tool and the arguments it was left on. The ANSWER
    does not: `_result` / `_json` / `_error` are rebuilt by every run and
    would only bloat the session pickle.
    """

    def __init__(self):
        super().__init__()
        self.tool = "find_views"
        self.args = {}              # tool -> its argument dict (seeded from _TOOL_ARGS)
        self.as_json = False        # False: the answer as a tree. True: the tool's bytes.
        self.follow_pointer = False
        self._result = {}           # the SAME dict every run - see _run
        self._json = ""
        self._stamp = ""
        self._error = None
        self._signature = None


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
@window(icon="",                                   # fa bug
        display_name="MCP Query", tint=(0.10, 0.20, 0.28),
        initial={"width": 820, "height": 700}, disable_scroll=False)
@render_func(tint=(0.20, 0.60, 0.90), use_cache=True, selectable=False,
             show_bg=False)
def draw_mcp_query(input_value=None, draw_state=None,
                   panel_state: MCPQueryState = None):
    # ── knobs ────────────────────────────────────────────────────────────
    # [tint=(0.95, 0.75, 0.20)]
    button_height = 26                  # toolbar buttons; the rows size off this
    # [tint=(0.95, 0.75, 0.20)]
    json_height = 420                   # the JSON pane's own scroll box
    # [tint=(0.95, 0.75, 0.20)]
    corner_radius = 5.0
    # [tint=(0.55, 0.80, 0.45)]
    tool_color = (0.16, 0.42, 0.62)     # unselected tool buttons + Run
    # [tint=(0.85, 0.45, 0.35)]
    live_color = (0.62, 0.30, 0.16)     # follow pointer, while it is on
    icon_run = ""                 # fa bolt
    icon_tree = ""                # fa sitemap
    icon_json = ""                # fa code
    icon_pointer = ""             # fa mouse-pointer

    _seed_args(panel_state)
    tool = panel_state.tool
    args = panel_state.args[tool]
    rerun = False

    # ── one button per collector ─────────────────────────────────────────
    # A selected button is the same colour, lit: same shape, no second color
    # to keep in sync.
    for name in _TOOL_ARGS:
        selected = name == tool
        if flat_button(name, draw_state, f"mcp_tool_{name}", height=button_height,
                       color=tool_color, corner_radius=corner_radius,
                       tint_value=0.42 if selected else 0.14,
                       max_bg_brightness=0.55 if selected else 0.22,
                       text_value=1.15 if selected else 0.85):
            panel_state.tool = tool = name
            args = panel_state.args[tool]
            rerun = True
        imgui.same_line()

    # ── run + how to read the answer ─────────────────────────────────────
    if flat_button(f"{icon_run}  Run", draw_state, "mcp_run", height=button_height,
                   color=tool_color, corner_radius=corner_radius, tint_value=0.30):
        rerun = True
    imgui.same_line()
    view_label = f"{icon_tree}  Tree" if panel_state.as_json else f"{icon_json}  JSON"
    if flat_button(view_label, draw_state, "mcp_view_mode", height=button_height,
                   color=tool_color, corner_radius=corner_radius, tint_value=0.20):
        panel_state.as_json = not panel_state.as_json
    if tool == "hit_test":
        imgui.same_line()
        following = panel_state.follow_pointer
        if flat_button(f"{icon_pointer}  follow pointer", draw_state, "mcp_follow",
                       height=button_height, corner_radius=corner_radius,
                       color=live_color if following else tool_color,
                       tint_value=0.45 if following else 0.18,
                       max_bg_brightness=0.60 if following else 0.22):
            panel_state.follow_pointer = following = not following
            rerun = True
        # Following the pointer means re-collecting wherever it goes, which
        # is the `live=True` contract: the body has to run every frame to see
        # a pointer that is nowhere near this window. The collect itself is
        # skipped while the pointer holds steady, so a parked mouse costs one
        # immediate run and nothing else. Off by default - rule 12.
        if following:
            draw_state.invalidate()
            pointer = _pointer()
            if pointer != (args["x"], args["y"]):
                args["x"], args["y"] = pointer
                rerun = True

    # ── arguments: the dict IS the form ──────────────────────────────────
    changed, _ = RenderFuncs.draw_collection(
        args, name=f"{tool} arguments", width=draw_state.content_width,
        show_bg=True, show_add_delete=False, initial={"expanded": True})

    # Re-collect on any edit, and whenever the arguments differ from the ones
    # the held answer was collected with (a tool switch, a hot-swapped
    # default). The signature is a repr of a tuple of scalars - never a
    # digest of the ANSWER, which is the expensive half.
    signature = repr((tool, sorted(args.items(), key=lambda item: item[0])))
    if rerun or changed or signature != panel_state._signature:
        _run(panel_state, tool, args)
        panel_state._signature = signature

    # ── the answer ───────────────────────────────────────────────────────
    if panel_state._stamp:
        imgui.text_disabled(panel_state._stamp)
    if panel_state._error:
        RenderFuncs.draw_text(panel_state._error, name="collector raised",
                              show_name=True, focusable=False, wrap=True,
                              autocomplete=False, show_jump_bar=False,
                              show_file_header=False)
    if panel_state.as_json:
        # focusable=False: this is the tool's output, not a buffer to type in.
        RenderFuncs.draw_text(panel_state._json, name=f"{tool} json",
                              show_name=True, height=json_height, focusable=False,
                              autocomplete=False, show_jump_bar=False,
                              show_file_header=False)
    else:
        RenderFuncs.draw_collection(panel_state._result, name=f"{tool} result",
                                    width=draw_state.content_width, show_bg=True,
                                    show_add_delete=False,
                                    initial={"expanded": True})
    return False, None
