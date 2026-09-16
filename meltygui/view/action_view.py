"""Action view functions and supporting definitions."""
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window
from meltygui.core.runtime.toggles import Actions
import meltygui_imgui as imgui


@window
@render_func(auto_resize=True)
def draw_actions(input_value=None, draw_state=None, **kwargs):
    from meltygui.view.code_view import draw_type

    draw_type(Actions, name="Actions")


@render_func(show_bg=True, use_cache=True, selectable=False, align_header=False,
             with_header=None, auto_resize=True, temp=True)
def draw_action_runner(input_value, draw_state=None, enter_key_pressed=False, **kwargs):
    """The parameter-input popup for one Actions function: an editable row per
    parameter and a Run button. Enter (the auto-subscribed event param) runs
    it too, so a value can be typed and fired without reaching for the mouse."""
    from meltygui.editor.source_ui import _category_tint
    from meltygui.view.control_view import draw_button
    from meltygui.view.text_view import draw_text
    from meltygui.core.rendering.render_dispatch import draw_any
    from meltygui.core.automation.action_core import _action_funcs
    from meltygui.core.automation.action_core import _close_action_runner
    from meltygui.core.automation.action_core import _run_action

    fn = input_value.action
    if fn is None:
        # Nothing selected (fresh boot) - stay hidden until a search hit
        # points us at an action.
        if draw_state is not None:
            draw_state.closed = True
        return False, input_value
    tint = _category_tint("Actions")
    icon = next((i for n, f, i in _action_funcs() if f is fn), None)
    title = f"{icon}  Actions.{fn.__name__}" if icon else f"Actions.{fn.__name__}"
    imgui.text_colored(title, *tint, 1.0)
    # Close X on the title row - the auto-fit window mode has no header, so
    # the popup draws its own close affordance (Esc closes it too).
    imgui.same_line(position=max(0.0, (draw_state.content_width or 200) - 26))
    x_clicked, _ = draw_button("", label="", name="close_runner", tint=tint,
                               wrap=True, min_width=20, height=18,
                               show_header=False, show_name=False)
    if x_clicked:
        _close_action_runner()
        return False, input_value
    imgui.dummy(0, 4)
    # Params render individually (not via draw_collection) so the first
    # str param can take the one-shot focus grab - draw_text is the only
    # widget with a focus param. select_all_on_focus: the seeded default is
    # prefilled, so typing replaces it.
    focus_pending = input_value._focus_requested
    input_value._focus_requested = False
    focus_target = next((k for k, v in input_value.params.items()
                         if isinstance(v, str)), None)
    for pname, val in list(input_value.params.items()):
        if isinstance(val, str):
            focus = focus_pending and pname == focus_target
            changed, new_val = draw_text(val, name=pname, single_line=True,
                                         request_focus=focus,
                                         select_all_on_focus=focus)
        else:
            changed, new_val = draw_any(val, name=pname)
        if changed:
            input_value.params[pname] = new_val
    clicked, _ = draw_button(label="Run", name=f"run_{fn.__name__}", tint=tint,
                             show_header=False, show_name=False)
    if clicked or enter_key_pressed:
        _run_action(input_value)
    return False, input_value
