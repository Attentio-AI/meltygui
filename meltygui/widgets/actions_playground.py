"""Actions window — renders Toggles' sibling Actions class (toggles.py), the
general-purpose stash for app triggers ("New file", "New Render Function").

Also registers the "Actions" global-search category: every function on the
Actions class is a hit, and activating one pops up the ActionRunner window —
its parameters (seeded from the function signature) as editable rows plus a
Run button that calls the function."""

import inspect
import types
from pathlib import Path

import meltygui_imgui as imgui

from meltygui.melty import Melty
from meltygui.modes import Modes
from meltygui.notifications import notify
from meltygui.toggles import Actions
from meltygui.utils.glfw_utils import request_render
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import Core
from meltygui.rendering.decorators.core_decoration import defaults
from meltygui.rendering.decorators.window_decoration import window
from meltygui.editor.source_ui import SearchHit
from meltygui.editor.source_ui import _category_tint
from meltygui.extensions import jump_to_symbol as _jump_to_symbol_def
from meltygui.views.new_core_view import draw_any
from meltygui.views.new_core_view import draw_button
from meltygui.views.new_core_view import draw_type
from meltygui.editor.text_editor import draw_text


@window
@render_func(auto_resize=True)
def draw_actions(input_value=None, draw_state=None, **kwargs):
    draw_type(Actions, name="Actions")


def _action_funcs():
    """(name, function, icon) for every public function on Actions, definition
    order. Reads vars() live each call so a hotswapped/added action shows up
    without any cache to invalidate. The icon is the @defaults(icon=...)
    registration — keyed by whatever object @defaults decorated, which is the
    staticmethod wrapper when it sits outside @staticmethod and the raw
    function when inside, so both keys are tried."""
    out = []
    for name, member in vars(Actions).items():
        if name.startswith("_"):
            continue
        fn = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
        if not isinstance(fn, types.FunctionType):
            continue
        icon = None
        for key in (member, fn):
            icon = Melty.default_kwargs_by_type.get(key, {}).get("icon")
            if icon:
                break
        out.append((name, fn, icon))
    return out


def _sig_label(name, fn):
    """`new_render_func(name='draw_other')` — the search-row label, so a hit
    shows what inputs the runner will ask for."""
    parts = []
    for pname, p in inspect.signature(fn).parameters.items():
        if p.default is not inspect.Parameter.empty:
            parts.append(f"{pname}={p.default!r}")
        else:
            parts.append(pname)
    return f"{name}({', '.join(parts)})"


def _seed_params(fn):
    """Initial editable values for a function's parameters: the signature
    default where there is one, a fresh instance of the annotation type
    otherwise (str -> "", int -> 0, ...), else an empty string."""
    params = {}
    for pname, p in inspect.signature(fn).parameters.items():
        if p.default is not inspect.Parameter.empty:
            params[pname] = p.default
        elif isinstance(p.annotation, type):
            try:
                params[pname] = p.annotation()
            except Exception:
                params[pname] = ""
        else:
            params[pname] = ""
    return params


def activate_action(fn):
    """A search hit's activation: an action with parameters opens the
    ActionRunner to fill them in; a parameterless one (screenshot,
    claude_terminal) just RUNS — there is nothing to ask, and a popup with
    only a Run button was a dead click."""
    if not inspect.signature(fn).parameters:
        try:
            result = fn()
        except Exception as e:
            notify(f"Actions.{fn.__name__} failed: {e}", tag="actions")
        else:
            notify(f"Actions.{fn.__name__} -> {result!r}" if result is not None
                   else f"Actions.{fn.__name__} ran", tag="actions")
        request_render()
        return
    open_action_runner(fn)


def open_action_runner(fn):
    """Point the ActionRunner window at `fn` and summon it next to the search
    window (the same come-to-you placement window hits use)."""
    ActionRunner.action = fn
    ActionRunner.params = _seed_params(fn)
    ActionRunner._focus_requested = True
    ds = Core.melty.open_window("ActionRunner")
    if ds is not None:
        gs = Core.melty.find_window("GlobalSearch")
        if gs is not None and gs.abs_left is not None:
            Core.melty.summon_window(ds, gs.abs_left, gs.abs_top)
        Core.melty.focused_ds = ds
        # The action and params changed outside the window's body - a cached
        # blit would keep showing the previous action.
        ds.invalidate()
    request_render()


def _close_action_runner():
    """Close the runner and release any text focus a param field holds — the
    shared exit for the title-row X, Esc (draw_main's root handler) and a
    successful run."""
    win = Core.melty.find_window("ActionRunner")
    if win is not None:
        win.closed = True
    Core.melty.text_focused_ds = None
    request_render()


def _run_action(input_value):
    """Call the runner's action with the edited params. A successful run
    closes the window; an error keeps it up (with a notification) so the
    inputs can be fixed."""
    fn = input_value.action
    try:
        result = fn(**dict(input_value.params))
    except Exception as e:
        notify(f"Actions.{fn.__name__} failed: {e}", tag="actions")
    else:
        notify(f"Actions.{fn.__name__} -> {result!r}" if result is not None
               else f"Actions.{fn.__name__} ran", tag="actions")
        _close_action_runner()


@render_func(show_bg=True, use_cache=True, selectable=False, align_header=False,
             with_header=None, auto_resize=True, temp=True)
def draw_action_runner(input_value, draw_state=None, enter_key_pressed=False, **kwargs):
    """The parameter-input popup for one Actions function: an editable row per
    parameter and a Run button. Enter (the auto-subscribed event param) runs
    it too, so a value can be typed and fired without reaching for the mouse."""
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


@window(view_func=draw_action_runner, mode=Modes.WINDOW_AUTO_FIT, always_on_top=True)
@defaults(tint=(0.92, 0.58, 0.25))
class ActionRunner:
    action = None  # the Actions function the window is parameterizing
    params = {}    # name -> value being edited, seeded from the signature
    _focus_requested = False  # one-shot: focus the first str param next frame


