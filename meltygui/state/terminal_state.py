"""Injected state for terminal views."""



from meltygui.core.conversion.dict_conversion import DictConversion


class TerminalScreenState(DictConversion):
    """Ephemeral per-view state: scrollback offset and grid selection endpoints."""

    def __init__(self):
        super().__init__()
        self.pointer = None
        self.scroll = 0          # lines scrolled up from the live buffer; 0 == bottom
        self.last_total = None   # composed line count last frame (to restore position)
        self.sel_anchor = None
        self.sel_active = None


def _norm(a, b):
    if a is None or b is None:
        return None, None
    return (a, b) if a <= b else (b, a)
