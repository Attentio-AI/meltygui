"""Shared source and file presentation helpers."""
import colorsys
from pathlib import Path
from collections import namedtuple

def _tab_text_color(tint, brightness, saturation, min_brightness=0.0):
    """Tab-label color: the tab's tint with its hsv saturation and value
    scaled by the Toggles.CodeEditor tab text knobs. Computed directly and
    handed to flat_button as text_color — its own text pipeline mixes only
    `factor` worth of theme color into the raw tint, which compressed these
    knobs to a ~10% effect. min_brightness floors the scaled value so dark
    tints stay legible."""
    h, s, v = colorsys.rgb_to_hsv(*tint[:3])
    return colorsys.hsv_to_rgb(h, min(max(s * saturation, 0.0), 1.0),
                               min(max(v * brightness, min_brightness), 1.0))



def _file_meta_tint(path):
    """The tint the user painted on this file (FileMeta — same store the
    editor tabs and folder tree read), or None."""
    from meltygui.models.file_meta import FileMeta
    from meltygui.models.file_meta import file_meta_store
    tint = FileMeta.painted_tint(file_meta_store().get(str(path)))
    return tuple(tint[:3]) if tint else None


class _RowSpan:
    """jump_to shim for a one-line search-row buffer: draw_text reads `.start`
    (0-based file line of the buffer's first line) to offset every tree-derived
    wash into buffer space. `path` is the row's file so the ROSTER tint path
    (text_editor._def_tints in roster mode needs a view_path to resolve the
    row's symbols — calls like `draw_any(...)` wash in their definition's
    tint); `pending_coords=True` tells the editor our line numbers are
    already PENDING coordinates so it must NOT run the disk→pending delta
    bridge a second time. `source` mirrors Address's slot (None) for any
    duck-typed reader; the jump-to BAR itself is off (show_jump_bar=False)."""
    __slots__ = ("start", "path", "end", "source", "pending_coords")

    def __init__(self, start, path=None):
        self.start = start
        self.end = start + 1
        self.path = path
        self.pending_coords = True
        self.source = None


def _row_code_hosts(path):
    """(code_dict, dict_host) for a search row's file from the SHARED
    code-host cache — the same parse the editor renders with. Creating a host
    is cheap; its whole-file parse runs in the background and rows repaint
    when it lands (notify_on_change on the row's ds). Only called for rows
    actually drawn (≤ max_visible), and hosts are cached across queries.
    Non-Python files get (None, None) — plain syntax colors."""
    if not str(path).endswith(".py"):
        return None, None
    try:
        from meltygui.code.new_converters import code_hosts_for
        _str_host, dict_host = code_hosts_for(Path(path))
        code_dict = dict_host._held()
        return (code_dict if isinstance(code_dict, dict) else None), dict_host
    except Exception:
        traceback.print_exc()
        return None, None


SearchHit = namedtuple("SearchHit", "label tint activate kind match state keep_open set_state icon parts goto code_row file sym terms", defaults=("", None, None, False, None, None, None, None, None, None, None, None))

def _category_tint(kind):
    return {"Actions": (0.92, 0.58, 0.25), "Functions": (0.55, 0.72, 0.85), "Classes": (0.72, 0.62, 0.35)}.get(kind, (0.55, 0.72, 0.85))
