import time

import glfw
import imgui

from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_footer
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.fonts import Font


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


def _char_pos_to_xy(text, index, origin_x, origin_y, line_px):
    line, col = _index_to_line_col(text, index)
    col_text = text[_get_line_start(text, index):index]
    x = origin_x + imgui.calc_text_size(col_text).x
    y = origin_y + line * line_px
    return x, y


def _xy_to_char_index(text, mx, my, origin_x, origin_y, line_px):
    lines = text.split('\n')
    line_num = int((my - origin_y) / line_px)
    line_num = max(0, min(line_num, len(lines) - 1))

    line_text = lines[line_num]
    best_idx = 0
    for i in range(len(line_text) + 1):
        cx = origin_x + imgui.calc_text_size(line_text[:i]).x
        if cx > mx:
            if i > 0:
                prev_cx = origin_x + imgui.calc_text_size(line_text[:i - 1]).x
                if mx - prev_cx < cx - mx:
                    best_idx = i - 1
                else:
                    best_idx = i
            break
        best_idx = i

    abs_idx = 0
    for l in range(line_num):
        abs_idx += len(lines[l]) + 1
    abs_idx += best_idx
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


@render_func(show_bg=True, wrap=False, use_cache=True, with_header=draw_header, shadow=True, with_footer=draw_footer, selectable=False, searchable=True, bg_offset=-100)
def draw_text(input_value: str, cursor_hover=False, left_mouse_clicked=False, left_mouse_up=False,
              left_mouse_down=False, left_mouse_drag=False, horizontal_scroll_drag=False,
              draw_state=None, request_focus=False, line_height=1.2, font: Font=Font.JETBRAINS_MONO_19):
    ds = draw_state

    _font_pushed = False
    if font is not None and Melty.font_mgr is not None:
        _font_handle = Melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True

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

    if left_mouse_clicked:
        print("Left mouse clicked on text editor")

    # imgui.is_key_pressed(..., repeat=True) uses io.key_repeat_delay/rate
    # so held keys auto-repeat at the OS-propecified cadence.
    pressed = lambda k: imgui.is_key_pressed(k, repeat=True)

    # --- Mouse handling ---
    is_focused = Melty.text_focused_ds is ds
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
            ds.text_selection_start = _word_boundary_left(text, click_pos)
            ds.text_selection_end = _word_boundary_right(text, click_pos)
            ds.text_cursor_pos = ds.text_selection_end
        elif ds.text_click_count >= 3:
            line_start = _get_line_start(text, click_pos)
            line_end = _get_line_end(text, click_pos)
            if line_end < len(text):
                line_end += 1  # include trailing newline so delete deletes the line
            ds.text_selection_start = line_start
            ds.text_selection_end = line_end
            ds.text_cursor_pos = ds.text_selection_end
        else:
            ds.text_cursor_pos = click_pos
            if io.key_shift:
                ds.text_selection_end = click_pos
            else:
                ds.text_selection_start = click_pos
                ds.text_selection_end = click_pos

    if left_mouse_drag:
        drag_pos = _xy_to_char_index(text, left_mouse_drag.x, left_mouse_drag.y,
                                      origin_x, origin_y, line_px)
        ds.text_selection_end = drag_pos
        ds.text_cursor_pos = drag_pos
        ds.text_cursor_blink_time = time.time()

    # --- Keyboard handling ---
    if is_focused:
        shift = io.key_shift
        ctrl = io.key_ctrl

        # --- Typed characters ---
        if not ctrl:
            for key, (unshifted, shifted) in _KEY_CHAR_MAP.items():
                if pressed(key):
                    ds.text_cursor_blink_time = time.time()
                    ch = shifted if shift else unshifted

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

        # --- Enter ---
        if pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER):
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
                    new_pos = _word_boundary_left(text, ds.text_cursor_pos)
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
                    new_pos = _word_boundary_right(text, ds.text_cursor_pos)
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

    # --- Horizontal auto-scroll ---
    # Only kicks in when the cursor moved this frame, so middle-drag pans
    # are not snapped back. Brings the cursor into view on a single line.
    visible_width = draw_state.content_width
    if ds.text_cursor_pos != ds.text_prev_cursor_pos and visible_width > 0:
        line_start = _get_line_start(text, ds.text_cursor_pos)
        cursor_logical_x = imgui.calc_text_size(text[line_start:ds.text_cursor_pos]).x
        edge_padding = 20.0
        if cursor_logical_x - ds.text_h_scroll < edge_padding:
            ds.text_h_scroll = max(0.0, cursor_logical_x - edge_padding)
        elif cursor_logical_x - ds.text_h_scroll > visible_width - edge_padding:
            ds.text_h_scroll = cursor_logical_x - visible_width + edge_padding
    ds.text_prev_cursor_pos = ds.text_cursor_pos

    # Clamp h_scroll to content bounds. calc_text_size on multi-line text
    # returns the longest line's width, which is what we want.
    max_line_width = imgui.calc_text_size(text).x
    max_h_scroll = max(0.0, max_line_width - visible_width + 50.0)
    ds.text_h_scroll = max(0.0, min(ds.text_h_scroll, max_h_scroll))
    origin_x = left - ds.text_h_scroll

    # --- Drawing ---
    draw_list = imgui.get_window_draw_list()
    rect_min_x = left
    rect_min_y = draw_state.abs_clip_rect[1]
    rect_max_x = left + draw_state.content_width
    rect_max_y = draw_state.abs_clip_rect[3]

    draw_list.push_clip_rect(rect_min_x, rect_min_y, rect_max_x, rect_max_y, True)

    # Selection
    if _has_selection(ds):
        sel_color = (102 << 24) | (204 << 16) | (102 << 8) | 51  # rgba(51, 102, 204, 0.4)
        lo, hi = _sel_range(ds)
        lines = text.split('\n')
        line_abs_start = 0
        for line_idx, line_text in enumerate(lines):
            line_abs_end = line_abs_start + len(line_text)
            if line_abs_end >= lo and line_abs_start <= hi:
                sel_start_in_line = max(0, lo - line_abs_start)
                sel_end_in_line = min(len(line_text), hi - line_abs_start)
                sx = origin_x + imgui.calc_text_size(line_text[:sel_start_in_line]).x
                ex = origin_x + imgui.calc_text_size(line_text[:sel_end_in_line]).x
                sy = origin_y + line_idx * line_px
                if hi > line_abs_end and line_abs_end >= lo:
                    ex = origin_x + imgui.calc_text_size(line_text).x + imgui.calc_text_size(' ').x
                draw_list.add_rect_filled(sx, sy, ex, sy + line_px, sel_color)
            line_abs_start = line_abs_end + 1

    # Syntax highlighted text
    x = origin_x
    y = origin_y
    t_idx = 0
    for token, color_key in tokenize(text):
        color = COLORS[color_key]
        for ch in token:
            if ch == '\n':
                x = origin_x
                y += line_px
                continue
            if y + line_px >= rect_min_y and y <= rect_max_y:
                draw_list.add_text(x, y, color, ch)
            x += imgui.calc_text_size(ch).x
        t_idx += 1

    # Cursor
    if is_focused and not _has_selection(ds):
        if (time.time() - ds.text_cursor_blink_time) % 1.0 < 0.5:
            cx, cy = _char_pos_to_xy(text, ds.text_cursor_pos, origin_x, origin_y, line_px)
            cursor_color = 0xFFFFFFFF  # white
            draw_list.add_line(cx, cy, cx, cy + line_px, cursor_color, 1.0)

    draw_list.pop_clip_rect()

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