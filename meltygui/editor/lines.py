"""draw_code_line_fast — ONE line of Python painted straight onto a draw list.

The editor's look without the editor: the same Darcula palette
(`text_editor.COLORS` through `tokenize`), the same definition BLOCK wash
behind a tinted class/def body and the same occurrence washes behind every
symbol whose definition carries a tint (`roster_tints.collect_def_tints`,
adjusted through the same `_bg_adjust` knobs), the same glyph-toward-wash
mix. No render_func, no draw_state, no wrapper: a plain function over a
draw list, so a list of code rows (the Ctrl+B picker, search results) costs
a few `add_text` calls per row instead of a one-line editor body each.

Two halves, deliberately split:
  • `CodeLineTints` resolves WHAT to paint behind a line (block tint +
    span washes) — the only part that touches the roster, memoized per
    (file, 64-line chunk) so a picker of N rows in one file runs the scan
    once per chunk, not once per row. Resolve it when the rows are BUILT
    (the picker open), never per frame.
  • `draw_code_line_fast` paints — cheap enough to run every frame.

Monospace contract: every caller pushes the editor's font
(`push_code_font`), so glyph x = col * char_w and consecutive same-colour
ASCII tokens merge into one `add_text` exactly as draw_text's fast path
does. Font Awesome icons are NOT monospaced: each is drawn in its own cell.
"""
import bisect

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.fonts import Font
from meltygui.runtime import Melty
from meltygui.toggles import Toggles


def push_code_font(font=Font.FONTAWESOME_MONO_19):
    """Push the editor's font (JetBrains Mono + Font Awesome) and measure it.
    Returns (pushed, char_w, line_h): pass `pushed` to `pop_code_font`,
    `char_w` / `line_h` to every `draw_code_line_fast` call in the batch."""
    pushed = False
    if Melty.font_mgr is not None:
        handle = Melty.font_mgr.get(font)
        if handle is not None:
            imgui.push_font(handle)
            pushed = True
    return pushed, imgui.calc_text_size("0").x, imgui.get_text_line_height()


def pop_code_font(pushed):
    if pushed:
        imgui.pop_font()


def _u32(rgb, alpha):
    return pack_color(rgb[0], rgb[1], rgb[2], alpha)


def _wash_factors():
    """(block, symbol, text) factor tuples for `_bg_adjust` — the exact
    triples draw_text builds from Toggles.TextEditor each frame."""
    min_b = Toggles.TextEditor.bg_min_brightness
    max_b = Toggles.TextEditor.bg_max_brightness
    return ((Toggles.TextEditor.bg_tint_saturation,
             Toggles.TextEditor.bg_tint_value, min_b, max_b),
            (Toggles.TextEditor.symbol_tint_saturation,
             Toggles.TextEditor.symbol_tint_value, min_b, max_b),
            (Toggles.TextEditor.text_tint_saturation,
             Toggles.TextEditor.text_tint_value, min_b, max_b))


def _split_emphasis(col, token, emphasis):
    """Pieces of `token` (starting at column `col`) as (start_col, text,
    bright) — cut at the boundaries of the `emphasis` column spans."""
    end = col + len(token)
    cuts = {col, end}
    for c0, c1 in emphasis:
        if col < c0 < end:
            cuts.add(c0)
        if col < c1 < end:
            cuts.add(c1)
    edges = sorted(cuts)
    out = []
    for a, b in zip(edges, edges[1:]):
        bright = any(c0 <= a < c1 for c0, c1 in emphasis)
        out.append((a, token[a - col:b - col], bright))
    return out


def draw_code_line_fast(draw_list, x, y, text, char_w, line_h, max_width=None,
                        block_tint=None, spans=(), line_open=None, alpha=1.0,
                        block_rounding=4.0, emphasis=None, dim_alpha=None):
    """Paint `text` (one line, no newline) at (x, y) with the editor's syntax
    colours. `char_w` / `line_h` come from `push_code_font` (the font must
    be pushed). `max_width` truncates by whole cells. `block_tint` (rgb)
    paints the definition block wash under the line — the enclosing tinted
    class/def — and `spans` = [(col_start, col_end, rgb, scale)] the
    occurrence washes, LINE-relative columns (see `CodeLineTints`).
    `line_open` is the lexer state at the line's start (None = code,
    'comment', or the (quote, kind) pair of a string the line begins
    inside). `alpha` fades everything (a context row). `emphasis` =
    [(col_start, col_end)] keeps those glyphs at full brightness and fades
    every other glyph by `dim_alpha` (default the diff-collapse preview
    fade, `Toggles.TextEditor.diff_preview_alpha`) — an EMPTY list dims the
    whole line, None dims nothing. Washes never dim. Returns the painted
    width in pixels."""
    from meltygui.editor.text import COLORS
    from meltygui.editor.text import _tokenize_from
    from meltygui.editor.text import _bg_adjust
    from meltygui.editor.text import _mix_packed
    from meltygui.editor.text import _fade_packed
    if max_width is not None:
        cells = max(0, int(max_width // char_w))
        if len(text) > cells:
            text = text[:cells]
    width = len(text) * char_w
    block_f, sym_f, text_f = _wash_factors()

    # ── block wash: the tinted definition body this line belongs to ──
    if block_tint is not None:
        rgb = _bg_adjust(tuple(block_tint[:3]), block_f)
        a = max(0.0, min(1.0, Toggles.TextEditor.def_block_alpha)) * alpha
        draw_list.add_rect_filled(x, y, x + max(len(text) + 1, 2) * char_w,
                                  y + line_h, _u32(rgb, a), block_rounding)

    # ── occurrence washes (+ their outline), same rects as the editor ──
    sym_a = Toggles.TextEditor.def_symbol_alpha * alpha
    ol_a = Toggles.TextEditor.def_symbol_outline_alpha * alpha
    ol_b = Toggles.TextEditor.def_symbol_outline_brightness
    ol_t = Toggles.TextEditor.def_symbol_outline_thickness
    mixes = []   # (col_start, col_end, rgb_adjusted_for_text, scale)
    for c0, c1, rgb, scale in spans:
        c0, c1 = max(0, c0), min(len(text), c1)
        if c1 <= c0:
            continue
        sa = _bg_adjust(tuple(rgb[:3]), sym_f)
        sx, ex = x + c0 * char_w, x + c1 * char_w
        draw_list.add_rect_filled(sx - 1, y + 1, ex + 1, y + line_h - 1,
                                  _u32(sa, sym_a * scale), 3.0)
        if ol_a > 0:
            ol = (min(1.0, sa[0] * ol_b), min(1.0, sa[1] * ol_b), min(1.0, sa[2] * ol_b))
            draw_list.add_rect(sx - 1, y + 1, ex + 1, y + line_h - 1,
                               _u32(ol, ol_a * scale), 3.0, thickness=ol_t)
        mixes.append((c0, c1, _bg_adjust(tuple(rgb[:3]), text_f), scale))

    # ── glyphs: tokens → palette, mixed by the wash under them ──
    mix_k = Toggles.TextEditor.def_text_tint_mix
    default = COLORS["default"]
    try:
        tokens = _tokenize_from(text, line_open)
    except Exception:
        tokens = [(text, "default")]
    if emphasis is not None and dim_alpha is None:
        dim_alpha = Toggles.TextEditor.diff_preview_alpha
    col = 0
    run_parts, run_x, run_col = None, 0.0, 0
    pieces = []   # (col, text, kind) - tokens cut at emphasis boundaries
    for token, kind in tokens:
        if not token:
            continue
        nl = token.find("\n")
        if nl != -1:
            token = token[:nl]
            if not token:
                break
        if kind == "clipped":
            col += len(token)
            continue
        if emphasis is not None:
            for pc, pt, bright in _split_emphasis(col, token, emphasis):
                pieces.append((pc, pt, kind, bright))
        else:
            pieces.append((col, token, kind, True))
        col += len(token)
        if nl != -1:
            break
    for col, token, kind, bright in pieces:
        color = COLORS.get(kind, default)
        for c0, c1, rgb, scale in mixes:
            if c0 <= col < c1:
                color = _mix_packed(color, rgb, mix_k * scale)
                break
        if alpha < 1.0:
            color = _fade_packed(color, alpha)
        if not bright:
            color = _fade_packed(color, dim_alpha)
        tx = x + col * char_w
        if kind == "icon":
            if run_parts is not None:
                draw_list.add_text(run_x, y, run_col, "".join(run_parts))
                run_parts = None
            # Icons aren't monospaced: one per standard cell, nudged 1px left.
            ix = tx
            for ch in token:
                draw_list.add_text(ix - 1, y, color, ch)
                ix += char_w
        elif token.isascii() and "\t" not in token:
            if run_parts is not None and run_col == color:
                run_parts.append(token)
            else:
                if run_parts is not None:
                    draw_list.add_text(run_x, y, run_col, "".join(run_parts))
                run_parts, run_x, run_col = [token], tx, color
        else:
            if run_parts is not None:
                draw_list.add_text(run_x, y, run_col, "".join(run_parts))
                run_parts = None
            draw_list.add_text(tx, y, color, token)
    if run_parts is not None:
        draw_list.add_text(run_x, y, run_col, "".join(run_parts))
    return width


class CodeLineTints:
    """Resolves (block_tint, spans) for lines of files, memoized per
    (file, text identity, 64-line chunk): the roster's windowed occurrence
    scan runs once per chunk touched, and every line of that chunk reads
    its washes from the result. Build one per row set (picker open, search
    landing); it holds the file texts it was given."""
    CHUNK = 64

    def __init__(self):
        self._chunks = {}   # (path, id(text), chunk_lo) -> (blocks, spans_by_line)
        self._texts = {}    # id(text) -> text (holds the id's)

    def tints(self, path, text, line):
        """`line` is 1-based. Returns (block_tint | None, [(col0, col1, rgb,
        scale)]) — columns relative to the RAW line (indent included); use
        `shift_spans` after stripping a display line."""
        if not text or line < 1:
            return None, []
        lo = ((line - 1) // self.CHUNK) * self.CHUNK
        key = (str(path), id(text), lo)
        got = self._chunks.get(key)
        if got is None:
            got = self._chunks[key] = self._scan(path, text, lo)
            self._texts[id(text)] = text
        blocks, by_line = got
        bl = line - 1
        block_tint, best_start = None, -1
        for start, _idx, end, tint in blocks:
            if start <= bl <= end and start > best_start:
                block_tint, best_start = tint, start
        return block_tint, by_line.get(bl, [])

    def _scan(self, path, text, lo):
        from meltygui.editor.roster_tints import collect_def_tints
        from meltygui.editor.text import _line_starts
        starts = _line_starts(text)
        hi = min(lo + self.CHUNK - 1, len(starts) - 1)
        try:
            blocks, spans, _lines, _names = collect_def_tints(
                text, 0, path, window=(lo, hi), hold_live=False)
        except Exception:
            return (), {}
        by_line = {}
        for s, e, rgb, scale in spans:
            # bisect on the memoized line starts: the line holding `s`.
            li = bisect.bisect_right(starts, s) - 1
            if li < lo or li > hi:
                continue
            ls = starts[li]
            by_line.setdefault(li, []).append((s - ls, e - ls, rgb, scale))
        return tuple(blocks), by_line


def shift_spans(spans, offset):
    """Line-relative spans after `offset` leading characters were stripped
    from the display text (spans that fell inside the strip are dropped)."""
    out = []
    for c0, c1, rgb, scale in spans:
        c0, c1 = c0 - offset, c1 - offset
        if c1 > 0:
            out.append((max(0, c0), c1, rgb, scale))
    return out
