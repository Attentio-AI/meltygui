"""Code model functions and supporting definitions."""



class UsagePickerModel:
    """What draw_text hands the picker window: the rows, the keyboard cursor
    (`index`), the hover-vs-keyboard highlight mode, and the row a mouse
    click picked (`picked`, consumed by draw_text). Plain object — draw_text
    change-gates the popover's repaint on (rows, index, kbd_mode)."""
    __slots__ = ("rows", "all_rows", "index", "kbd_mode", "picked", "_last_mouse",
                 "hover", "measured", "chrome_h")

    def __init__(self):
        self.rows = []
        self.all_rows = []
        self.index = 0
        self.kbd_mode = True
        self.picked = None
        self._last_mouse = None
        self.hover = None
        # (frame, row height) the body laid out) - the sizing reads the
        # wrapper's content rect only when it was measured this frame.
        self.measured = None
        # Window chrome above + below the rows (content rect - rows height),
        # learned from a window measure; sizes the rows without one.
        self.chrome_h = 8.0

    def set_rows(self, rows, best_index=0, max_rows=None):
        """Install `rows`; past `max_rows` the list is cut (never above the
        best row) and ends in a selectable "+ N more" row — `expand()`."""
        from meltygui.editor.usage_picker import UsageRow

        self.all_rows = rows
        if max_rows is not None and max_rows > 0 and len(rows) > max_rows:
            keep = max(max_rows, best_index + 1)
            if keep < len(rows):
                hidden = len(rows) - keep
                rows = rows[:keep] + [UsageRow("more", 0, None, 0,
                                               f"+ {hidden} more", label=str(hidden))]
        self.rows = rows
        self.index = max(0, min(best_index, len(rows) - 1)) if rows else 0
        self.kbd_mode = True
        self.picked = None
        self.hover = None

    def expand(self):
        """List every row (the "+ N more" row was picked); the cursor stays
        on the first newly listed row."""
        if self.rows is self.all_rows:
            return
        cut = len(self.rows) - 1
        self.rows = self.all_rows
        self.index = max(0, min(cut, len(self.rows) - 1))
        self.kbd_mode = True
        self.picked = None

    def signature(self):
        return (id(self.rows), self.index, self.kbd_mode, self.hover)

    def current(self):
        rows = self.rows
        return rows[self.index] if 0 <= self.index < len(rows) else None
