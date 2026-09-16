"""Query view functions and supporting definitions."""
from meltygui.rendering.core_render import render_func
from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.state.query_state import MCPQueryState
import meltygui_imgui as imgui


@render_func(tint=(0.20, 0.60, 0.90), use_cache=True, selectable=False,
             show_bg=False)
def draw_mcp_query(input_value=None, draw_state=None,
                   panel_state: MCPQueryState = None):
    # ── knobs ────────────────────────────────────────────────────────────
    # [tint=(0.95, 0.75, 0.20)]
    from meltygui.view.header_view import flat_button
    from meltygui.core.query_core import _TOOL_ARGS
    from meltygui.core.query_core import _pointer
    from meltygui.core.query_core import _run
    from meltygui.core.query_core import _seed_args

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
