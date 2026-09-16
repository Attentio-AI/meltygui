"""Injected state for terminal views."""



class TerminalScreenState:
    """Ephemeral per-view state: scrollback offset and grid selection endpoints."""

    def __init__(self):
        self.scroll = 0          # lines scrolled up from the live buffer; 0 == bottom
        self.last_total = None   # composed line count last frame (to restore position)
        self.sel_anchor = None
        self.sel_active = None
