"""Layout model functions and supporting definitions."""



class Columns(dict):
    """Marker dict: values render side by side (draw_columns is its default
    renderer), so column layouts nest by data:
    Columns({"a": ..., "b": Columns({...})})."""


class Rows(dict):
    """Marker dict: values render stacked top to bottom (draw_rows is its
    default renderer) with draggable edges between them — the row-axis twin
    of Columns, and the two nest freely by data:
    Rows({"top": Columns({...}), "bottom": ...})."""
