import time

import glfw
import imgui

from src.lsd.gl_gui.model.core_model.draw_state import Anchor, Pin
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_footer
from src.lsd.gl_gui.melty import Melty, SearchTerm
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.jump_to import draw_jump_to


def _hex(h):
    """Convert '#rrggbb' to imgui packed u32 color (ABGR format)."""
    r = int(h[1:3], 16)
    g = int(h[3:5], 16)
    b = int(h[5:7], 16)
    a = 255
    # ImGui uses ABGR packing for color u32
    return (a << 24) | (b << 16) | (g << 8) | r

COLORS = {
    'default':       _hex('#a9b7c6'),  # Token (from Darcula)
    'keyword':       _hex('#cc7832'),  # Keyword
    'keyword_const': _hex('#cc7832'),  # Keyword.Constant (True/False/None)
    'operator_word': _hex('#cc7832'),  # Operator.Word (and, or, not, in, is)
    'builtin_pseudo': _hex('#94558d'), # Name.Builtin.Pseudo (self, cls)
    'decorator':     _hex('#bbb529'),  # Name.Decorator
    'string':        _hex('#6a8759'),  # String
    'string_doc':    _hex('#629755'),  # String.Doc (docstrings)
    'comment':       _hex('#808080'),  # Comment
    'number':        _hex('#6897bb'),  # Number
}

KEYWORDS = {'def', 'class', 'if', 'else', 'elif', 'for', 'while',
            'return', 'import', 'from', 'with', 'as', 'try', 'except',
            'finally', 'raise', 'yield', 'pass', 'break', 'continue',
            'lambda', 'global', 'nonlocal', 'del', 'assert', 'async', 'await'}

KEYWORD_CONSTS = {'True', 'False', 'None'}

OPERATOR_WORDS = {'and', 'or', 'not', 'in', 'is'}

BUILTIN_PSEUDO = {'self', 'cls'}

WORD_DELIMITERS = ' \t\n\r,.;:!?()[]{}\'\"=+-*/<>@#$%^&|~`\\'

# GLFW key to character mappings (unshifted, shifted)
_KEY_CHAR_MAP = {
    glfw.KEY_SPACE: (' ', ' '),
    glfw.KEY_APOSTROPHE: ("'", '"'),
    glfw.KEY_COMMA: (',', '<'),
    glfw.KEY_MINUS: ('-', '_'),
    glfw.KEY_PERIOD: ('.', '>'),
    glfw.KEY_SLASH: ('/', '?'),
    glfw.KEY_0: ('0', ')'),
    glfw.KEY_1: ('1', '!'),
    glfw.KEY_2: ('2', '@'),
    glfw.KEY_3: ('3', '#'),
    glfw.KEY_4: ('4', '$'),
    glfw.KEY_5: ('5', '%'),
    glfw.KEY_6: ('6', '^'),
    glfw.KEY_7: ('7', '&'),
    glfw.KEY_8: ('8', '*'),
    glfw.KEY_9: ('9', '('),
    glfw.KEY_SEMICOLON: (';', ':'),
    glfw.KEY_EQUAL: ('=', '+'),
    glfw.KEY_LEFT_BRACKET: ('[', '{'),
    glfw.KEY_BACKSLASH: ('\\', '|'),
    glfw.KEY_RIGHT_BRACKET: (']', '}'),
    glfw.KEY_GRAVE_ACCENT: ('`', '~'),
}

# Add letter keys A-Z
for _i in range(26):
    _key = glfw.KEY_A + _i
    _ch = chr(ord('a') + _i)
    _KEY_CHAR_MAP[_key] = (_ch, _ch.upper())

# Keys the editor repeats when held: every typed char plus certain navigation/edit
# keys. Used to supplement frame_key_events with imgui's synthesized auto-repeat
# (see draw_text) so held keys repeat even when the platform's GLFW backend
# doesn't generate REPEAT actions.
_REPEATABLE_KEYS = set(_KEY_CHAR_MAP) | {
    glfw.KEY_BACKSPACE, glfw.KEY_DELETE, glfw.KEY_ENTER, glfw.KEY_KP_ENTER,
    glfw.KEY_TAB, glfw.KEY_LEFT, glfw.KEY_RIGHT, glfw.KEY_UP, glfw.KEY_DOWN,
    glfw.KEY_HOME, glfw.KEY_END,
}

def tokenize(text):
    """Yields (text, color_key) tuples with Darcula-style token categories."""
    i = 0
    n = len(text)

    while i < n:
        # --- Comments ---
        if text[i] == '#':
            end = text.find('\n', i)
            end = end if end != -1 else n
            yield text[i:end], 'comment'
            i = end

        # --- Decorators ---
        elif text[i] == '@' and (i == 0 or text[i - 1] in '\n '):
            end = i + 1
            while end < n and (text[end].isalnum() or text[end] in '_.'):
                end += 1
            yield text[i:end], 'decorator'
            i = end

        # --- Triple-quoted strings ---
        elif i + 2 < n and text[i:i + 3] in ('"""', "'''"):
            quote = text[i:i + 3]
            end = text.find(quote, i + 3)
            end = end + 3 if end != -1 else n
            yield text[i:end], 'string_doc'
            i = end

        # --- String prefixes (f", r", b", rb", etc.) ---
        elif (text[i] in 'fFrRbBuU'
              and i + 1 < n
              and (text[i + 1] in ('"', "'")
                   or (i + 2 < n and text[i + 1] in 'fFrRbBuU' and text[i + 2] in ('"', "'")))):
            prefix_end = i + 1
            if prefix_end < n and text[prefix_end] in 'fFrRbBuU':
                prefix_end += 1
            quote = text[prefix_end]
            if prefix_end + 2 < n and text[prefix_end:prefix_end + 3] in ('"""', "'''"):
                triple = text[prefix_end:prefix_end + 3]
                end = text.find(triple, prefix_end + 3)
                end = end + 3 if end != -1 else n
            else:
                end = prefix_end + 1
                escaped = False
                while end < n:
                    if escaped:
                        escaped = False
                        end += 1
                        continue
                    if text[end] == '\\':
                        escaped = True
                        end += 1
                        continue
                    if text[end] == quote:
                        end += 1
                        break
                    end += 1
            yield text[i:end], 'string'
            i = end

        # --- Strings ---
        elif text[i] in ('"', "'"):
            quote = text[i]
            end = i + 1
            escaped = False
            while end < n:
                if escaped:
                    escaped = False
                    end += 1
                    continue
                if text[end] == '\\':
                    escaped = True
                    end += 1
                    continue
                if text[end] == quote:
                    end += 1
                    break
                end += 1
            yield text[i:end], 'string'
            i = end

        # --- Words ---
        elif text[i].isalpha() or text[i] == '_':
            end = i
            while end < n and (text[end].isalnum() or text[end] == '_'):
                end += 1
            word = text[i:end]

            if word in KEYWORD_CONSTS:
                yield word, 'keyword_const'
            elif word in OPERATOR_WORDS:
                yield word, 'operator_word'
            elif word in KEYWORDS:
                yield word, 'keyword'
            elif word in BUILTIN_PSEUDO:
                yield word, 'builtin_pseudo'
            else:
                yield word, 'default'
            i = end

        # --- Numbers ---
        elif text[i].isdigit() or (text[i] == '.' and i + 1 < n and text[i + 1].isdigit()):
            end = i
            if end + 1 < n and text[end] == '0' and text[end + 1] in 'xXoObB':
                end += 2
                while end < n and text[end] in '0123456789abcdefABCDEF_':
                    end += 1
            else:
                has_dot = False
                while end < n and (text[end].isdigit() or text[end] in '._eE'):
                    if text[end] == '.':
                        if has_dot:
                            break
                        has_dot = True
                    if text[end] in 'eE' and end + 1 < n and text[end + 1] in '+-':
                        end += 1
                    end += 1
            yield text[i:end], 'number'
            i = end

        # --- Everything else (operators, punctuation, whitespace) ---
        else:
            yield text[i], 'default'
            i += 1


def _index_to_line_col(text, index):
    line = text[:index].count('\n')
    last_nl = text.rfind('\n', 0, index)
    col = index - (last_nl + 1) if last_nl != -1 else index
    return line, col


def _line_col_to_index(text, line, col):
    idx = 0
    for _ in range(line):
        nl = text.find('\n', idx)
        if nl == -1:
            return len(text)
        idx = nl + 1
    line_end = text.find('\n', idx)
    line_end = line_end if line_end != -1 else len(text)
    return min(idx + col, line_end)


def _get_line_start(text, index):
    nl = text.rfind('\n', 0, index)
    return nl + 1 if nl != -1 else 0


def _get_line_end(text, index):
    nl = text.find('\n', index)
    return nl if nl != -1 else len(text)


def _mono_char_w():
    """Glyph advance for the monospace editor font (current imgui font)."""
    return imgui.calc_text_size("0").x


def _char_pos_to_xy(text, index, origin_x, origin_y, line_px):
    line, col = _index_to_line_col(text, index)
    x = origin_x + col * _mono_char_w()
    y = origin_y + line * line_px
    return x, y


def _xy_to_char_index(text, mx, my, origin_x, origin_y, line_px):
    lines = text.split('\n')
    line_num = int((my - origin_y) / line_px)
    line_num = max(0, min(line_num, len(lines) - 1))

    line_text = lines[line_num]
    char_w = _mono_char_w()
    col = round((mx - origin_x) / char_w) if char_w else 0
    col = max(0, min(col, len(line_text)))

    abs_idx = 0
    for l in range(line_num):
        abs_idx += len(lines[l]) + 1
    abs_idx += col
    return min(abs_idx, len(text))


def _word_boundary_left(text, pos):
    if pos <= 0:
        return 0
    pos -= 1
    while pos > 0 and text[pos] in WORD_DELIMITERS:
        pos -= 1
    while pos > 0 and text[pos - 1] not in WORD_DELIMITERS:
        pos -= 1
    return pos


def _word_boundary_right(text, pos):
    n = len(text)
    if pos >= n:
        return n
    while pos < n and text[pos] in WORD_DELIMITERS:
        pos += 1
    while pos < n and text[pos] not in WORD_DELIMITERS:
        pos += 1
    return pos


# Brackets select one-at-a-time even when adjacent (so '{ ['' etc. are
# separately selectable), while runs of other punctuation (==, +=, ...) group.
SOLO_CHARS = '(){}[]'


def _char_class(c):
    """Character class for double-click selection units. Runs of the same class
    select together; newlines and brackets are one char per unit."""
    if c in SOLO_CHARS:
        return 'solo'
    if c.isalnum() or c == '_':
        return 'word'
    if c == '\n':
        return 'nl'
    if c in ' \t\r':
        return 'space'
    return 'punct'


def _select_unit_left(text, pos):
    """Start of the click-selection unit containing `pos`.

    Unlike _word_boundary_left (word navigation, which skips over punctuation to
    the next identifier), this anchors on the character under the caret and
    grows a run of its own class — so a lone '{' or '(', a run of operators, a
    blank-line newline, or a stretch of spaces is each selectable on its own.
    """
    n = len(text)
    if pos <= 0:
        return 0
    i = pos if pos < n else pos - 1
    cls = _char_class(text[i])
    if cls in ('nl', 'solo'):
        return i  # a single newline (blank line) or bracket is its own unit
    start = i
    while start > 0 and _char_class(text[start - 1]) == cls:
        start -= 1
    return start


def _select_unit_right(text, pos):
    """End of the click-selection unit containing `pos` (see _select_unit_left)."""
    n = len(text)
    if pos >= n:
        return n
    cls = _char_class(text[pos])
    if cls in ('nl', 'solo'):
        return pos + 1
    end = pos
    while end < n and _char_class(text[end]) == cls:
        end += 1
    return end


def _get_indent(text, index):
    ls = _get_line_start(text, index)
    indent = 0
    while ls + indent < len(text) and text[ls + indent] == ' ':
        indent += 1
    return indent


def _indent_lines(text, lo, hi, dedent):
    """Indent (or dedent) every line covered by [lo, hi] by one tab stop.
    Returns (new_text, new_lo, new_hi). For selections, new_lo snaps to the
    start of the first affected line so the whole shifted block stays highlighted."""
    indent = '    '
    has_sel = lo != hi

    old_lines = text.split('\n')
    old_starts = [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line) + 1)

    def line_of(pos):
        L = 0
        while L + 1 < len(old_lines) and old_starts[L + 1] <= pos:
            L += 1
        return L

    line_lo = line_of(lo)
    line_hi = line_of(hi - 1) if has_sel else line_lo

    new_lines = list(old_lines)
    delta = [0] * len(old_lines)  # positive = chars removed, negative = chars added
    for i in range(line_lo, line_hi + 1):
        if dedent:
            n = 0
            while n < len(indent) and n < len(new_lines[i]) and new_lines[i][n] == ' ':
                n += 1
            new_lines[i] = new_lines[i][n:]
            delta[i] = n
        else:
            new_lines[i] = indent + new_lines[i]
            delta[i] = -len(indent)

    new_text = '\n'.join(new_lines)
    new_starts = [0]
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line) + 1)

    def adjust(pos):
        L = line_of(pos)
        old_col = pos - old_starts[L]
        d = delta[L]
        if d > 0:
            new_col = max(0, old_col - d)
        else:
            new_col = old_col - d  # d negative, so subtract → add
        new_col = min(new_col, len(new_lines[L]))
        return new_starts[L] + new_col

    if has_sel:
        return new_text, new_starts[line_lo], adjust(hi)
    return new_text, adjust(lo), adjust(hi)


def _toggle_comment(text, lo, hi):
    """Toggle '# ' Python comments on lines covered by [lo, hi].
    Returns (new_text, new_lo, new_hi). Empty lines are skipped. If every
    non-empty affected line is already commented, uncomments them all;
    otherwise comments them at the min-indent column for visual alignment."""
    has_sel = lo != hi

    old_lines = text.split('\n')
    old_starts = [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line) + 1)

    def line_of(pos):
        L = 0
        while L + 1 < len(old_lines) and old_starts[L + 1] <= pos:
            L += 1
        return L

    line_lo = line_of(lo)
    line_hi = line_of(hi - 1) if has_sel else line_lo

    def indent_of(line):
        return len(line) - len(line.lstrip(' '))

    non_empty = [i for i in range(line_lo, line_hi + 1) if old_lines[i].strip()]
    if not non_empty:
        return text, lo, hi

    all_commented = all(old_lines[i].lstrip(' ').startswith('#') for i in non_empty)
    min_indent = min(indent_of(old_lines[i]) for i in non_empty)

    new_lines = list(old_lines)
    edits = {}  # line_idx -> (col, delta): chars inserted (>0) or removed (<0) at col
    for i in non_empty:
        line = old_lines[i]
        if all_commented:
            ind = indent_of(line)
            after = line[ind:]
            if after.startswith('# '):
                new_lines[i] = line[:ind] + after[2:]
                edits[i] = (ind, -2)
            else:
                new_lines[i] = line[:ind] + after[1:]
                edits[i] = (ind, -1)
        else:
            new_lines[i] = line[:min_indent] + '# ' + line[min_indent:]
            edits[i] = (min_indent, 2)

    new_text = '\n'.join(new_lines)
    new_starts = [0]
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line) + 1)

    def adjust(pos):
        L = line_of(pos)
        old_col = pos - old_starts[L]
        if L in edits:
            col, d = edits[L]
            if d > 0:
                new_col = old_col + d if old_col >= col else old_col
            else:
                if old_col <= col:
                    new_col = old_col
                else:
                    new_col = max(col, old_col + d)
        else:
            new_col = old_col
        new_col = min(new_col, len(new_lines[L]))
        return new_starts[L] + new_col

    if has_sel:
        return new_text, new_starts[line_lo], adjust(hi)
    return new_text, adjust(lo), adjust(hi)


def _has_selection(ds):
    return ds.text_selection_start != ds.text_selection_end


def _sel_range(ds):
    return min(ds.text_selection_start, ds.text_selection_end), max(ds.text_selection_start, ds.text_selection_end)


def _delete_selection(text, ds):
    lo, hi = _sel_range(ds)
    return text[:lo] + text[hi:], lo


def _find_matches(text, term):
    """Case-insensitive, non-overlapping substring match ranges (start, end)."""
    matches = []
    term = str(term) if term else ""
    if not term:
        return matches
    low_text = text.lower()
    low_term = term.lower()
    start = 0
    while True:
        idx = low_text.find(low_term, start)
        if idx == -1:
            break
        matches.append((idx, idx + len(low_term)))
        start = idx + len(low_term)
    return matches


def _scroll_into_view(ds, top_abs, bottom_abs, margin=40.0):
    """Scroll the nearest scrollable ancestor (or the view itself) so the
    screen-space band [top_abs, bottom_abs] is visible.

    The editor doesn't always own its scrollbar — when rendered in a fixed
    window it scrolls itself, but in the code chain a parent container scrolls
    (and the editor's own scroll_offset is forced to 0). Walking up _parent to
    the node whose scroll_visible is set, then nudging that node's scroll_offset
    by the on-screen overflow, scrolls the right thing in both layouts.
    """
    node = ds
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if getattr(node, 'scroll_visible', False):
            view_top = node.abs_top + (node.header_height or 0)
            view_bottom = node.abs_top + (node.height or 0) - (node.footer_height or 0)
            sx, sy = node.scroll_offset
            if top_abs < view_top + margin:
                node.scroll_offset = (sx, sy - (view_top + margin - top_abs))
                request_render()
            elif bottom_abs > view_bottom - margin:
                node.scroll_offset = (sx, sy + (bottom_abs - (view_bottom - margin)))
                request_render()
            return
        nxt = node._parent
        node = nxt if nxt is not node else None


def _code_tree_errors(code_tree):
    """(line, message) parse-error markers carried by the routed code_tree, if
    any. code_tree is the GeneralParse round-tripped in via the chain; a failed
    parse may surface as a ParseError (a dict subclass) exposing .line/.error or
    __line__/__error__ keys. Duck-typed to dodge an import cycle with
    libcst_conversion."""
    if code_tree is None:
        return []
    line = getattr(code_tree, 'line', None)
    err = getattr(code_tree, 'error', None)
    if line and err:
        return [(int(line), str(err))]
    if isinstance(code_tree, dict) and code_tree.get('__error__'):
        return [(int(code_tree.get('__line__') or 1), str(code_tree['__error__']))]
    return []


def _describe_code_tree(code_tree):
    """One-line readout of what round-tripped into draw_text as code_tree, for
    the debug indicator."""
    if code_tree is None:
        return "None"
    errs = _code_tree_errors(code_tree)
    if errs:
        return f"ParseError @ line {errs[0][0]}: {errs[0][1][:40]}"
    name = type(code_tree).__name__
    if isinstance(code_tree, dict):
        return f"{name} ({len(code_tree)} keys)"
    return name


@render_func(show_bg=True, wrap=False, use_cache=True, with_header=draw_header, shadow=True, with_footer=draw_footer,
             selectable=False, searchable=True, bg_offset=-100)
def draw_text(input_value: str,
              left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
              horizontal_scroll_drag=False, search_text="",
              single_line=False,
              draw_state=None, request_focus=False,
              line_height=1.2, font: Font=Font.JETBRAINS_MONO_19, jump_to=None,
              code_tree=None):
    ds = draw_state

    # Jump-to-source button drawn inline at the top (before the monospace font
    # push, so it has the normal UI font), above the text body.
    if jump_to is not None:
        draw_jump_to(jump_to)

    # Debug indicator for the routed code_tree: makes the str→cst→str round trip
    # visible - shows the type/size that arrived, or a red ParseError + line.
    _ct_errors = None
    if code_tree is not None:
        _ct_errors = _code_tree_errors(code_tree)
        if _ct_errors:
            imgui.text_colored(f"code_tree: {_describe_code_tree(code_tree)}", 0.9, 0.3, 0.3, 1.0)
        else:
            imgui.text_colored(f"code_tree: {_describe_code_tree(code_tree)}", 0.55, 0.55, 0.62, 1.0)

    _font_pushed = False
    if font is not None and Melty.font_mgr is not None:
        _font_handle = Melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True

    # Character advance. Every caller uses JetBrains Mono (monospace), so one
    # character advance lets us position and measure text by character count
    # instead of calling imgui.calc_text_size per glyph/slice each frame.
    char_w = imgui.calc_text_size("0").x

    changed = False
    original_input = input_value
    max_lines = 1000  # limit for performance; can be adjusted or removed
    input_value = '\n'.join(input_value.split('\n')[:max_lines])

    text = input_value
    io = imgui.get_io()
    line_px = imgui.get_text_line_height() * line_height

    left = imgui.get_cursor_screen_pos()[0]
    top = imgui.get_cursor_screen_pos()[1]

    # Right-click drag pans both axes. Vertical uses the framework's
    # scroll_offset (the framework skips writing it while button 2 is down,
    # so our edits aren't clobbered mid-drag). Horizontal uses our own
    # text_h_scroll since the framework only manages vertical scroll.
    if horizontal_scroll_drag:
        ds.text_h_scroll -= horizontal_scroll_drag.dx
        sx, sy = ds.scroll_offset
        ds.scroll_offset = (sx, sy - horizontal_scroll_drag.dy)

    origin_x = left - ds.text_h_scroll
    origin_y = top

    # Keystrokes come from the GLFW-callback queue (Melty.frame_key_events:
    # ordered (glfw_key, mods) for PRESS/REPEAT this frame), so nothing is
    # dropped on slow frames the way imgui.is_key_pressed (current frame only)
    # would. But GLFW doesn't emit REPEAT actions on any platform, so in the
    # focused editor we supplement the queue with imgui's synthesized auto-repeat
    # (io.key_repeat_delay/rate) for held keys - skipping any key GLFW already
    # reported this frame so we never double-input. `pressed(k)` is membership;
    # the per-char loop iterates in order.
    _frame_keys = list(Melty.frame_key_events)
    if Melty.text_focused_ds is ds:
        _glfw_this_frame = {k for k, _m in _frame_keys}
        _repeat_mods = ((glfw.MOD_SHIFT if io.key_shift else 0)
                        | (glfw.MOD_CONTROL if io.key_ctrl else 0)
                        | (glfw.MOD_ALT if getattr(io, 'key_alt', False) else 0))
        _any_down = False
        for _rk in _REPEATABLE_KEYS:
            if imgui.is_key_down(_rk):
                _any_down = True
            if _rk not in _glfw_this_frame and imgui.is_key_pressed(_rk, repeat=True):
                _frame_keys.append((_rk, _repeat_mods))
        # The loop otherwise sleeps on wait_events between GLFW events; keep it
        # rendering while a key is held so imgui's repeat cadence is sampled.
        if _any_down:
            request_render()
    _fired = {k for k, _m in _frame_keys}
    pressed = lambda k: k in _fired

    # --- Mouse handling ---
    is_focused = Melty.text_focused_ds is ds
    # A rebuilt cache can hand us a fresh draw_state object for the same tile;
    # rebind focus by tile id so a cache hit doesn't silently drop it.
    if (not is_focused and Melty.text_focused_ds is not None
            and getattr(Melty.text_focused_ds, '_tile_id', None) == ds._tile_id):
        Melty.text_focused_ds = ds
        is_focused = True
    if request_focus:
        Melty.text_focused_ds = ds
        is_focused = True

    if left_mouse_down:
        Melty.text_focused_ds = ds
        is_focused = True
        ds.text_cursor_blink_time = time.time()
        click_pos = _xy_to_char_index(text, io.mouse_pos.x, io.mouse_pos.y,
                                       origin_x, origin_y, line_px)

        now = time.time()
        within_window = (now - ds.text_double_click_time < 0.3
                         and abs(click_pos - ds.text_last_click_pos) <= 1)
        ds.text_click_count = ds.text_click_count + 1 if within_window else 1
        ds.text_double_click_time = now
        ds.text_last_click_pos = click_pos

        if ds.text_click_count == 2:
            ds.text_drag_mode = 'word'
            ds.text_selection_start = _select_unit_left(text, click_pos)
            ds.text_selection_end = _select_unit_right(text, click_pos)
            ds.text_cursor_pos = ds.text_selection_end
            ds.text_drag_anchor_lo = ds.text_selection_start
            ds.text_drag_anchor_hi = ds.text_selection_end
        elif ds.text_click_count >= 3:
            ds.text_drag_mode = 'line'
            line_start = _get_line_start(text, click_pos)
            line_end = _get_line_end(text, click_pos)
            if line_end < len(text):
                line_end += 1  # include trailing newline so delete deletes the line
            ds.text_selection_start = line_start
            ds.text_selection_end = line_end
            ds.text_cursor_pos = ds.text_selection_end
            ds.text_drag_anchor_lo = line_start
            ds.text_drag_anchor_hi = line_end
        else:
            ds.text_drag_mode = 'char'
            ds.text_cursor_pos = click_pos
            if io.key_shift:
                ds.text_selection_end = click_pos
            else:
                ds.text_selection_start = click_pos
                ds.text_selection_end = click_pos
            ds.text_drag_anchor_lo = ds.text_selection_start
            ds.text_drag_anchor_hi = ds.text_selection_end

    # Extend the selection on cursor motion, and also every frame the button is
    # held (left_mouse_held) once a drag is underway - so holding the cursor
    # past the top/bottom edge keeps auto-scrolling and selecting more text,
    # not just while the mouse is moving.
    if left_mouse_drag or (left_mouse_held and ds.text_drag_mode):
        mx = left_mouse_drag.x if left_mouse_drag else io.mouse_pos.x
        my = left_mouse_drag.y if left_mouse_drag else io.mouse_pos.y
        # Auto-scroll when the cursor hs/passes the view's top or bottom edge
        # so the selection can reach text outside the viewport. No-ops when the
        # cursor is comfortably inside.
        _scroll_into_view(ds, my, my)
        drag_pos = _xy_to_char_index(text, mx, my,
                                      origin_x, origin_y, line_px)
        anchor_lo = ds.text_drag_anchor_lo
        anchor_hi = ds.text_drag_anchor_hi
        if ds.text_drag_mode in ('word', 'line') and (anchor_lo != anchor_hi):
            # Snap the moving end to the word/line boundary under the mouse,
            # then merge with the anchor span so the originally-selected
            # word/line stays fully highlighted while dragging either way.
            if ds.text_drag_mode == 'word':
                edge_lo = _select_unit_left(text, drag_pos)
                edge_hi = _select_unit_right(text, drag_pos)
            else:
                edge_lo = _get_line_start(text, drag_pos)
                edge_hi = _get_line_end(text, drag_pos)
                if edge_hi < len(text):
                    edge_hi += 1
            if drag_pos < anchor_lo:
                # Extending left: anchor's far (right) edge is the fixed end.
                ds.text_selection_start = anchor_hi
                ds.text_selection_end = edge_lo
                ds.text_cursor_pos = edge_lo
            else:
                # At/right of the anchor: anchor's left edge is fixed.
                ds.text_selection_start = anchor_lo
                ds.text_selection_end = max(anchor_hi, edge_hi)
                ds.text_cursor_pos = ds.text_selection_end
        else:
            ds.text_selection_end = drag_pos
            ds.text_cursor_pos = drag_pos
        ds.text_cursor_blink_time = time.time()

    # --- Keyboard handling ---
    if is_focused:
        shift = io.key_shift
        ctrl = io.key_ctrl

        # --- Typed characters --- drained in order, using each key event's own
        # modifiers so fast shift-typing across a slow frame stays shifted.
        for _fk, _fmods in _frame_keys:
            if _fmods & glfw.MOD_CONTROL:
                continue
            _cm = _KEY_CHAR_MAP.get(_fk)
            if _cm is None:
                continue
            ds.text_cursor_blink_time = time.time()
            ch = _cm[1] if (_fmods & glfw.MOD_SHIFT) else _cm[0]
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
            text = text[:ds.text_cursor_pos] + ch + text[ds.text_cursor_pos:]
            ds.text_cursor_pos += len(ch)
            ds.text_selection_start = ds.text_cursor_pos
            ds.text_selection_end = ds.text_cursor_pos
            changed = True

        # --- Tab / Shift+Tab ---
        if pressed(glfw.KEY_TAB) and not ctrl:
            ds.text_cursor_blink_time = time.time()
            if shift or _has_selection(ds):
                if _has_selection(ds):
                    lo, hi = _sel_range(ds)
                else:
                    lo = hi = ds.text_cursor_pos
                text, new_lo, new_hi = _indent_lines(text, lo, hi, dedent=shift)
                ds.text_selection_start = new_lo
                ds.text_selection_end = new_hi
                ds.text_cursor_pos = new_hi
            else:
                insert = '    '
                text = text[:ds.text_cursor_pos] + insert + text[ds.text_cursor_pos:]
                ds.text_cursor_pos += len(insert)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
            changed = True

        # --- Enter --- (skipped for single-line fields like the search box,
        # where Enter is reserved for find-next / Shift+Enter find-prev).
        # Ctrl+Enter is reserved for recompile (general_go_to_address), so we
        # don't insert a newline when Ctrl is held.
        if (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and not single_line and not ctrl:
            ds.text_cursor_blink_time = time.time()
            indent = _get_indent(text, ds.text_cursor_pos)
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
            insert = '\n' + ' ' * indent
            text = text[:ds.text_cursor_pos] + insert + text[ds.text_cursor_pos:]
            ds.text_cursor_pos += len(insert)
            ds.text_selection_start = ds.text_cursor_pos
            ds.text_selection_end = ds.text_cursor_pos
            changed = True

        # --- Backspace ---
        if pressed(glfw.KEY_BACKSPACE):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            elif ds.text_cursor_pos > 0:
                if ctrl:
                    # Same granular behaviour as ctrl-click selection so a
                    # ctrl-backspace stops at a bracket/operator instead of
                    # eating a whole run of '{(' etc.
                    new_pos = _select_unit_left(text, ds.text_cursor_pos)
                    text = text[:new_pos] + text[ds.text_cursor_pos:]
                    ds.text_cursor_pos = new_pos
                elif (ds.text_cursor_pos >= 4
                      and text[ds.text_cursor_pos - 4:ds.text_cursor_pos] == '    '):
                    text = text[:ds.text_cursor_pos - 4] + text[ds.text_cursor_pos:]
                    ds.text_cursor_pos -= 4
                else:
                    text = text[:ds.text_cursor_pos - 1] + text[ds.text_cursor_pos:]
                    ds.text_cursor_pos -= 1
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Delete ---
        if pressed(glfw.KEY_DELETE):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            elif ds.text_cursor_pos < len(text):
                if ctrl:
                    new_pos = _select_unit_right(text, ds.text_cursor_pos)
                    text = text[:ds.text_cursor_pos] + text[new_pos:]
                else:
                    text = text[:ds.text_cursor_pos] + text[ds.text_cursor_pos + 1:]
                changed = True

        # --- Left ---
        if pressed(glfw.KEY_LEFT):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = _word_boundary_left(text, ds.text_cursor_pos)
            elif _has_selection(ds) and not shift:
                ds.text_cursor_pos = min(ds.text_selection_start, ds.text_selection_end)
            elif ds.text_cursor_pos > 0:
                ds.text_cursor_pos -= 1
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Right ---
        if pressed(glfw.KEY_RIGHT):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = _word_boundary_right(text, ds.text_cursor_pos)
            elif _has_selection(ds) and not shift:
                ds.text_cursor_pos = max(ds.text_selection_start, ds.text_selection_end)
            elif ds.text_cursor_pos < len(text):
                ds.text_cursor_pos += 1
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Up ---
        if pressed(glfw.KEY_UP):
            ds.text_cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, ds.text_cursor_pos)
            if line > 0:
                ds.text_cursor_pos = _line_col_to_index(text, line - 1, col)
            else:
                ds.text_cursor_pos = 0
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Down ---
        if pressed(glfw.KEY_DOWN):
            ds.text_cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, ds.text_cursor_pos)
            total_lines = text.count('\n')
            if line < total_lines:
                ds.text_cursor_pos = _line_col_to_index(text, line + 1, col)
            else:
                ds.text_cursor_pos = len(text)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Home ---
        if pressed(glfw.KEY_HOME):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = 0
            else:
                ds.text_cursor_pos = _get_line_start(text, ds.text_cursor_pos)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- End ---
        if pressed(glfw.KEY_END):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = len(text)
            else:
                ds.text_cursor_pos = _get_line_end(text, ds.text_cursor_pos)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Ctrl+A ---
        if ctrl and pressed(glfw.KEY_A):
            ds.text_selection_start = 0
            ds.text_selection_end = len(text)
            ds.text_cursor_pos = len(text)

        # --- Ctrl+C ---
        if ctrl and pressed(glfw.KEY_C):
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
                imgui.set_clipboard_text(text[lo:hi])

        # --- Ctrl+X ---
        if ctrl and pressed(glfw.KEY_X):
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
                imgui.set_clipboard_text(text[lo:hi])
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Ctrl+V ---
        if ctrl and pressed(glfw.KEY_V):
            ds.text_cursor_blink_time = time.time()
            clipboard = imgui.get_clipboard_text()
            if clipboard:
                if _has_selection(ds):
                    text, ds.text_cursor_pos = _delete_selection(text, ds)
                text = text[:ds.text_cursor_pos] + clipboard + text[ds.text_cursor_pos:]
                ds.text_cursor_pos += len(clipboard)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Ctrl+/ (toggle line comment) ---
        if ctrl and pressed(glfw.KEY_SLASH):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
            else:
                lo = hi = ds.text_cursor_pos
            text, new_lo, new_hi = _toggle_comment(text, lo, hi)
            ds.text_selection_start = new_lo
            ds.text_selection_end = new_hi
            ds.text_cursor_pos = new_hi
            changed = True

    # Clamp
    ds.text_cursor_pos = max(0, min(ds.text_cursor_pos, len(text)))
    ds.text_selection_start = max(0, min(ds.text_selection_start, len(text)))
    ds.text_selection_end = max(0, min(ds.text_selection_end, len(text)))

    # --- Find-in-text search ---
    # The term arrives either forwarded from an ancestor search owner (as a
    # SearchTerm carrying the shared cross-view session) or, when this editor
    # hosts the find UI itself, on ds.search_text with a session it pushed.
    # We search locally, register our matches to the session so every view
    # combines into one global set, and scroll to the global-current match
    # when it lands in this view.
    search_term = search_text or (ds.search_text if ds.search_active else "")
    search_matches = _find_matches(text, search_term)

    # Register this view's results into the shared session during render. On a
    # full-search frame (scroll_to) all views claim in order and offsets are
    # synchronized - pick the global-current local match and latch it; on
    # subsequent repaints reuse the latch so the highlight doesn't jump.
    if isinstance(search_term, SearchTerm):
        session = search_term
    elif ds.search_active and ds._search_session is not None:
        session = ds._search_session
    else:
        session = None

    local_count = len(search_matches)
    if session is not None:
        if session.scroll_to:
            _base, current_local = session.claim(local_count)
            ds._search_active_local = current_local
            should_scroll = current_local is not None
        else:
            session.claim(local_count)
            current_local = ds._search_active_local
            if current_local is not None and current_local >= local_count:
                current_local = None
            should_scroll = False
    else:
        current_local = None
        should_scroll = False

    if should_scroll:
        ms, me = search_matches[current_local]
        line, _col = _index_to_line_col(text, ms)
        # Vertical: scroll the editor (or its scroll parent) so the match
        # line is on screen. origin_y is the content top at the current scroll.
        match_top_abs = origin_y + line * line_px
        _scroll_into_view(ds, match_top_abs, match_top_abs + line_px)

        # Horizontal: default back to the line start (h_scroll 0) while paging
        # through results, scrolling to only when the match wouldn't fit.
        line_start = _get_line_start(text, ms)
        match_x = (ms - line_start) * char_w
        match_x_end = (me - line_start) * char_w
        edge_padding = 20.0
        if ds.content_width > 0:
            if match_x_end <= ds.content_width - edge_padding:
                ds.text_h_scroll = 0.0
            else:
                # Pin the match's end to the right edge so we scroll the least
                # amount needed to reveal it, instead of dragging it to the left.
                ds.text_h_scroll = max(0.0, match_x_end - ds.content_width + edge_padding)
        request_render()

    # --- Horizontal auto-scroll ---
    # Only kicks in when the cursor moved this frame, so middle-drag pans
    # are not snapped back. Brings the cursor into view on a single line.
    visible_width = draw_state.content_width
    if ds.text_cursor_pos != ds.text_prev_cursor_pos and visible_width > 0:
        line_start = _get_line_start(text, ds.text_cursor_pos)
        cursor_logical_x = (ds.text_cursor_pos - line_start) * char_w
        edge_padding = 20.0
        if cursor_logical_x - ds.text_h_scroll < edge_padding:
            ds.text_h_scroll = max(0.0, cursor_logical_x - edge_padding)
        elif cursor_logical_x - ds.text_h_scroll > visible_width - edge_padding:
            ds.text_h_scroll = cursor_logical_x - visible_width + edge_padding
    ds.text_prev_cursor_pos = ds.text_cursor_pos

    # Clamp h_scroll to content bounds; the longest line drives the limit.
    max_line_width = max((len(l) for l in text.split('\n')), default=0) * char_w
    max_h_scroll = max(0.0, max_line_width - visible_width + 50.0)
    ds.text_h_scroll = max(0.0, min(ds.text_h_scroll, max_h_scroll))
    origin_x = left - ds.text_h_scroll

    # --- Drawing ---
    draw_list = imgui.get_window_draw_list()
    rect_min_x = left
    rect_min_y = draw_state.abs_clip_rect[1]
    rect_max_x = left + draw_state.content_width
    rect_max_y = draw_state.abs_clip_rect[3]

    # draw_list.push_clip_rect(rect_min_x, rect_min_y, rect_max_x, rect_max_y, False)

    # Selection
    if _has_selection(ds):
        sel_color = (102 << 24) | (204 << 16) | (102 << 8) | 51  # rgba(51, 102, 204, 0.4)
        lo, hi = _sel_range(ds)
        lines = text.split('\n')
        line_abs_start = 0
        for line_idx, line_text in enumerate(lines):
            line_abs_end = line_abs_start + len(line_text)
            sy = origin_y + line_idx * line_px
            if (line_abs_end >= lo and line_abs_start <= hi
                    and sy + line_px >= rect_min_y and sy <= rect_max_y):
                sel_start_in_line = max(0, lo - line_abs_start)
                sel_end_in_line = min(len(line_text), hi - line_abs_start)
                sx = origin_x + sel_start_in_line * char_w
                ex = origin_x + sel_end_in_line * char_w
                if hi > line_abs_end and line_abs_end >= lo:
                    ex = origin_x + (len(line_text) + 1) * char_w  # +1 for trailing newline
                draw_list.add_rect_filled(sx, sy, ex, sy + line_px, sel_color)
            line_abs_start = line_abs_end + 1

    # Search match highlights (drawn behind the text so glyphs stay readable).
    # The active match gets a stronger fill plus an outline; the others are faint.
    if search_matches:
        match_bg = (89 << 24) | (80 << 16) | (200 << 8) | 230   # faint yellow
        cur_bg = (150 << 24) | (60 << 16) | (170 << 8) | 240    # active fill
        cur_border = (255 << 24) | (90 << 16) | (200 << 8) | 255  # active outline
        for m_idx, (ms, me) in enumerate(search_matches):
            m_line, _ = _index_to_line_col(text, ms)
            m_ls = _get_line_start(text, ms)
            sx = origin_x + (ms - m_ls) * char_w
            ex = origin_x + (me - m_ls) * char_w
            sy = origin_y + m_line * line_px
            ey = sy + line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            if m_idx == current_local:
                draw_list.add_rect_filled(sx, sy, ex, ey, cur_bg)
                draw_list.add_rect(sx, sy, ex, ey, cur_border)
            else:
                draw_list.add_rect_filled(sx, sy, ex, ey, match_bg)

    # Parse-error line highlight from the routed code tree (debugging the
    # str→cstr→str round trip): a translucent red wash spanning the offending
    # line, drawn behind the glyphs so the code stays readable.
    if _ct_errors:
        err_bg = (110 << 24) | (40 << 16) | (40 << 8) | 210  # translucent red (ABGR)
        for err_line, _msg in _ct_errors:
            ey0 = origin_y + (err_line - 1) * line_px
            ey1 = ey0 + line_px
            if ey1 < rect_min_y or ey0 > rect_max_y:
                continue
            draw_list.add_rect_filled(origin_x - 4, ey0, origin_x + visible_width, ey1, err_bg)

    # Syntax highlighting text. Tokens are cached by text value, so unchanged
    # content (scrolling, cursor blink, hover repaints) skip re-tokenizing and
    # only pay a C-level str compare. Each token is drawn one line-segment at a
    # time with a single add_text call rather than one call per glyph.
    if getattr(ds, '_tok_cache_text', None) == text:
        tokens = ds._tok_cache
    else:
        tokens = list(tokenize(text))
        ds._tok_cache_text = text
        ds._tok_cache = tokens

    x = origin_x
    y = origin_y
    for token, color_key in tokens:
        color = COLORS[color_key]
        start = 0
        while True:
            nl = token.find('\n', start)
            seg = token[start:nl] if nl != -1 else token[start:]
            if seg and y + line_px >= rect_min_y and y <= rect_max_y:
                draw_list.add_text(x, y, color, seg)
            if nl == -1:
                x += len(seg) * char_w
                break
            x = origin_x
            y += line_px
            start = nl + 1

    # Cursor. Drawn at the caret even while a selection exists, so the active
    # (moving) edge of a drag or shift-selection shows where delete and arrow
    # keys will act from - text_cursor_pos already tracks that location.
    if is_focused:
        if (time.time() - ds.text_cursor_blink_time) % 1.0 < 0.5:
            cx, cy = _char_pos_to_xy(text, ds.text_cursor_pos, origin_x, origin_y, line_px)
            cursor_color = 0xFFFFFFFF  # white
            draw_list.add_line(cx, cy, cx, cy + line_px, cursor_color, 1.0)

    # draw_list.pop_clip_rect()

    if changed:
        text_height = (text.count('\n') + 1) * line_px + 2
    else:
        text_height = (input_value.count('\n') + 1) * line_px + 2

    imgui.dummy(draw_state.width, text_height)

    if _font_pushed:
        imgui.pop_font()

    if changed:
        rebuilt_text = text + '\n'.join(original_input.split('\n')[max_lines:])
        return True, rebuilt_text
    return False, original_input