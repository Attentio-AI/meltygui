"""Injected state for trace views."""
from meltygui.rendering.decorators.core_decoration import no_save
from meltygui.state.dict_conversion import DictConversion


@no_save("panes", "trace_obj", "trace_sig", "index_armed",
         "scroll_bottom_pending")
class StackTraceState(DictConversion):
    """Injected per-view state (`trace_state: StackTraceState = None`). All
    fields are derived from the input trace and hold live objects (frame
    locals, LocalValueStores, parses), so nothing here persists — a fresh
    session re-resolves from the input."""

    def __init__(self):
        super().__init__()
        self.panes = None              # [_Pane | None(elision)] per frame
        self.trace_obj = None          # the input the panes were built from
        self.trace_sig = None          # (project_only, max_frames)
        self.index_armed = None        # paths whose span_index runner armed
        # Fresh capture → open at the BOTTOM (the view function the trace
        # bottoms out in); pinned until the bottom pane has really rendered.
        self.scroll_bottom_pending = False


class CrashReportsPanelState(DictConversion):
    """Which reports are expanded — file name → True — persisted with the
    window's draw_state (injected as `panel_state: CrashReportsPanelState`,
    the TabState pattern) so the window reopens the way it was left."""

    def __init__(self):
        super().__init__()
        self.open = {}
