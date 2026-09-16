"""File labels shared by the file UI and chat change summaries."""
from pathlib import Path
import meltygui_imgui as imgui
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles, Tint
from meltygui.hdr_color import pack_color
from meltygui.core.cache.tile_cache import add_shadow
from meltygui.editor.source_ui import _tab_text_color

_ELLIPSIZE_MEMO = globals().get("_ELLIPSIZE_MEMO", {})


def _ellipsize_search(text, max_width):
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if imgui.calc_text_size(text[:mid] + "\u2026").x <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + "\u2026"


def _ellipsize(text, max_width):
    """`text` cut to fit `max_width` pixels in the current font with a
    trailing ellipsis; unchanged when it already fits."""
    # Memoized per (text, width, font scale): the commit-file column runs
    # this for each row every frame, and each miss is a calc_text_size
    # binary search.
    memo_key = (text, max_width, Melty.ui_scale)
    hit = _ELLIPSIZE_MEMO.get(memo_key)
    if hit is not None:
        return hit
    if max_width <= 0 or imgui.calc_text_size(text).x <= max_width:
        result = text
    else:
        result = _ellipsize_search(text, max_width)
    if len(_ELLIPSIZE_MEMO) > 4096:
        _ELLIPSIZE_MEMO.clear()
    _ELLIPSIZE_MEMO[memo_key] = result
    return result
