"""Stack trace playground — a window for exercising draw_stack_trace.

Two capture buttons: "Raise & catch" runs a small three-deep helper chain
that raises, catches the ValueError, and renders it — the trace reads main →
render loop → this window → _alpha → _beta → _gamma with the raising line
last. "Capture stack" snapshots the CURRENT live stack (no exception) as
FrameSummary rows — the button-click path itself, straight down from main.
"""

import traceback

from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Tint
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.stack_trace_view import draw_stack_trace


class StackTracePlayground:
    # The captured exception / FrameSummary rows. A class attribute, not
    # draw_state.misc: misc is serialized and an exception (with its live
    # traceback frames) must never reach the pickle. Hotswap keeps the live
    # value (unchanged source expression), so a capture survives edits here.
    captured = None
    status = ""


# ── Sample call chain - three frames deep so the trace has some shape ────────

def _alpha():
    greeting = "hello from the playground"
    return _beta(greeting)


def _beta(greeting):
    numbers = [1, 2, 3]
    return _gamma(greeting, numbers)


def _gamma(greeting, numbers):
    raise ValueError(f"sample failure: {greeting!r} over {numbers!r}")


def capture_sample_exception():
    try:
        _alpha()
    except ValueError as exc:
        return exc


@window(disable_scroll=False, icon=None, display_name="Stack Trace Playground",
        tint=(0.09, 0.035, 0.03))
@render_func(tint=(0.9, 0.35, 0.28))
def stack_trace_playground(draw_state=None):
    # [tint=(0.9, 0.35, 0.28)]
    button_height = 26

    if RenderFuncs.button(" Raise & catch", width=130, height=button_height,
                          name="stack playground raise")[0]:
        StackTracePlayground.captured = capture_sample_exception()
        StackTracePlayground.status = "caught ValueError from _gamma"
    if RenderFuncs.button(" Capture stack", width=130, height=button_height,
                          name="stack playground capture")[0]:
        # FrameSummary rows, not live frames: a held frame object would pin
        # this whole render pass's locals for the session.
        StackTracePlayground.captured = traceback.extract_stack()
        StackTracePlayground.status = "captured the live stack at the click"
    if RenderFuncs.button(" Clear", width=90, height=button_height,
                          name="stack playground clear")[0]:
        StackTracePlayground.captured = None
        StackTracePlayground.status = ""

    if StackTracePlayground.captured is None:
        RenderFuncs.draw_text("No capture yet — Raise & catch runs a sample "
                              "three-deep failure; Capture stack snapshots "
                              "the live stack under this click.",
                              name="stack playground hint", show_bg=False,
                              editable=False, tint=Tint.subtle_text())
        return False, None

    if StackTracePlayground.status:
        RenderFuncs.draw_text(StackTracePlayground.status,
                              name="stack playground status", show_bg=False,
                              single_line=True, editable=False,
                              syntax_highlight=False, tint=Tint.subtle_text())
    draw_stack_trace(StackTracePlayground.captured,
                     name="stack playground trace")
    return False, None
