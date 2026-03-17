import time

import glfw
import imgui

from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header

_cursor_pos = 0
_selection_start = 0
_selection_end = 0
_is_focused = False
_cursor_blink_time = 0.0
_is_dragging = False
_double_click_time = 0.0
_last_click_pos = -1
_prev_keys_down = set()

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

# All typeable keys we track for press detection
_TYPEABLE_KEYS = set(_KEY_CHAR_MAP.keys())

# Special keys we also need to track
_SPECIAL_KEYS = {
    glfw.KEY_ENTER, glfw.KEY_KP_ENTER,
    glfw.KEY_BACKSPACE, glfw.KEY_DELETE, glfw.KEY_TAB,
    glfw.KEY_LEFT, glfw.KEY_RIGHT, glfw.KEY_UP, glfw.KEY_DOWN,
    glfw.KEY_HOME, glfw.KEY_END,
    glfw.KEY_PAGE_UP, glfw.KEY_PAGE_DOWN,
}

_ALL_TRACKED_KEYS = _TYPEABLE_KEYS | _SPECIAL_KEYS


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


def _char_pos_to_xy(text, index, origin_x, origin_y, line_height):
    line, col = _index_to_line_col(text, index)
    col_text = text[_get_line_start(text, index):index]
    x = origin_x + imgui.calc_text_size(col_text).x
    y = origin_y + line * line_height
    return x, y


def _xy_to_char_index(text, mx, my, origin_x, origin_y, line_height):
    lines = text.split('\n')
    line_num = int((my - origin_y) / line_height)
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


def _has_selection():
    return _selection_start != _selection_end


def _sel_range():
    return min(_selection_start, _selection_end), max(_selection_start, _selection_end)


def _delete_selection(text):
    lo, hi = _sel_range()
    return text[:lo] + text[hi:], lo


def _key_just_pressed(io, key):
    """Detect a key press this frame using io.keys_down and prev state tracking."""
    global _prev_keys_down
    return io.keys_down[key] and key not in _prev_keys_down


@render_func(show_bg=True, wrap=False, use_cache=True, with_header=draw_header, selectable=False, indent_size=30)
def draw_text(input_value: str, cursor_hover=False, left_mouse_drag=False, draw_state=None):
    global _cursor_pos, _selection_start, _selection_end, _is_focused
    global _cursor_blink_time, _double_click_time, _last_click_pos
    global _prev_keys_down

    changed = False
    original_input = input_value
    max_lines = 1000  # limit for performance; can be adjusted or removed
    input_value = '\n'.join(input_value.split('\n')[:max_lines])

    text = input_value
    io = imgui.get_io()
    line_height = imgui.get_text_line_height()

    left = imgui.get_cursor_screen_pos()[0]
    top = imgui.get_cursor_screen_pos()[1]

    origin_x = left
    origin_y = top

    # --- Invisible button for mouse interaction ---
    is_hovered = cursor_hover



    # --- Read current keyboard state ---
    current_keys = set()
    for key in _ALL_TRACKED_KEYS:
        if io.keys_down[key]:
            current_keys.add(key)
    just_pressed = current_keys - _prev_keys_down

    # --- Mouse handling ---
    if is_hovered and imgui.is_mouse_clicked(0):
        _is_focused = True
        _cursor_blink_time = time.time()
        click_pos = _xy_to_char_index(text, io.mouse_pos.x, io.mouse_pos.y,
                                       origin_x, origin_y, line_height)

        now = time.time()
        if (now - _double_click_time < 0.3
                and abs(click_pos - _last_click_pos) <= 1):
            _selection_start = _word_boundary_left(text, click_pos)
            _selection_end = _word_boundary_right(text, click_pos)
            _cursor_pos = _selection_end
            _double_click_time = 0
        else:
            _double_click_time = now
            _last_click_pos = click_pos
            _cursor_pos = click_pos
            if io.key_shift:
                _selection_end = click_pos
            else:
                _selection_start = click_pos
                _selection_end = click_pos

    elif not is_hovered and imgui.is_mouse_clicked(0):
        _is_focused = False

    if left_mouse_drag and imgui.is_mouse_down(0):
        drag_pos = _xy_to_char_index(text, left_mouse_drag.x, left_mouse_drag.y,
                                      origin_x, origin_y, line_height)
        _selection_end = drag_pos
        _cursor_pos = drag_pos
        _cursor_blink_time = time.time()

    # --- Keyboard handling ---
    if _is_focused:
        shift = io.key_shift
        ctrl = io.key_ctrl

        # --- Typed characters ---
        for key in just_pressed:
            if key in _KEY_CHAR_MAP and not ctrl:
                _cursor_blink_time = time.time()
                unshifted, shifted = _KEY_CHAR_MAP[key]
                ch = shifted if shift else unshifted

                if _has_selection():
                    text, _cursor_pos = _delete_selection(text)
                text = text[:_cursor_pos] + ch + text[_cursor_pos:]
                _cursor_pos += len(ch)
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos
                changed = True

        # --- Tab ---
        if glfw.KEY_TAB in just_pressed and not ctrl:
            _cursor_blink_time = time.time()
            insert = '    '
            if _has_selection():
                text, _cursor_pos = _delete_selection(text)
            text = text[:_cursor_pos] + insert + text[_cursor_pos:]
            _cursor_pos += len(insert)
            _selection_start = _cursor_pos
            _selection_end = _cursor_pos
            changed = True

        # --- Enter ---
        if glfw.KEY_ENTER in just_pressed or glfw.KEY_KP_ENTER in just_pressed:
            _cursor_blink_time = time.time()
            indent = _get_indent(text, _cursor_pos)
            if _has_selection():
                text, _cursor_pos = _delete_selection(text)
            insert = '\n' + ' ' * indent
            text = text[:_cursor_pos] + insert + text[_cursor_pos:]
            _cursor_pos += len(insert)
            _selection_start = _cursor_pos
            _selection_end = _cursor_pos
            changed = True

        # --- Backspace ---
        if glfw.KEY_BACKSPACE in just_pressed:
            _cursor_blink_time = time.time()
            if _has_selection():
                text, _cursor_pos = _delete_selection(text)
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos
                changed = True
            elif _cursor_pos > 0:
                if ctrl:
                    new_pos = _word_boundary_left(text, _cursor_pos)
                    text = text[:new_pos] + text[_cursor_pos:]
                    _cursor_pos = new_pos
                elif (_cursor_pos >= 4
                      and text[_cursor_pos - 4:_cursor_pos] == '    '):
                    text = text[:_cursor_pos - 4] + text[_cursor_pos:]
                    _cursor_pos -= 4
                else:
                    text = text[:_cursor_pos - 1] + text[_cursor_pos:]
                    _cursor_pos -= 1
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos
                changed = True

        # --- Delete ---
        if glfw.KEY_DELETE in just_pressed:
            _cursor_blink_time = time.time()
            if _has_selection():
                text, _cursor_pos = _delete_selection(text)
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos
                changed = True
            elif _cursor_pos < len(text):
                if ctrl:
                    new_pos = _word_boundary_right(text, _cursor_pos)
                    text = text[:_cursor_pos] + text[new_pos:]
                else:
                    text = text[:_cursor_pos] + text[_cursor_pos + 1:]
                changed = True

        # --- Left ---
        if glfw.KEY_LEFT in just_pressed:
            _cursor_blink_time = time.time()
            if ctrl:
                _cursor_pos = _word_boundary_left(text, _cursor_pos)
            elif _has_selection() and not shift:
                _cursor_pos = min(_selection_start, _selection_end)
            elif _cursor_pos > 0:
                _cursor_pos -= 1
            if shift:
                _selection_end = _cursor_pos
            else:
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos

        # --- Right ---
        if glfw.KEY_RIGHT in just_pressed:
            _cursor_blink_time = time.time()
            if ctrl:
                _cursor_pos = _word_boundary_right(text, _cursor_pos)
            elif _has_selection() and not shift:
                _cursor_pos = max(_selection_start, _selection_end)
            elif _cursor_pos < len(text):
                _cursor_pos += 1
            if shift:
                _selection_end = _cursor_pos
            else:
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos

        # --- Up ---
        if glfw.KEY_UP in just_pressed:
            _cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, _cursor_pos)
            if line > 0:
                _cursor_pos = _line_col_to_index(text, line - 1, col)
            else:
                _cursor_pos = 0
            if shift:
                _selection_end = _cursor_pos
            else:
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos

        # --- Down ---
        if glfw.KEY_DOWN in just_pressed:
            _cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, _cursor_pos)
            total_lines = text.count('\n')
            if line < total_lines:
                _cursor_pos = _line_col_to_index(text, line + 1, col)
            else:
                _cursor_pos = len(text)
            if shift:
                _selection_end = _cursor_pos
            else:
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos

        # --- Home ---
        if glfw.KEY_HOME in just_pressed:
            _cursor_blink_time = time.time()
            if ctrl:
                _cursor_pos = 0
            else:
                _cursor_pos = _get_line_start(text, _cursor_pos)
            if shift:
                _selection_end = _cursor_pos
            else:
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos

        # --- End ---
        if glfw.KEY_END in just_pressed:
            _cursor_blink_time = time.time()
            if ctrl:
                _cursor_pos = len(text)
            else:
                _cursor_pos = _get_line_end(text, _cursor_pos)
            if shift:
                _selection_end = _cursor_pos
            else:
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos

        # --- Ctrl+A ---
        if ctrl and glfw.KEY_A in just_pressed:
            _selection_start = 0
            _selection_end = len(text)
            _cursor_pos = len(text)

        # --- Ctrl+C ---
        if ctrl and glfw.KEY_C in just_pressed:
            if _has_selection():
                lo, hi = _sel_range()
                imgui.set_clipboard_text(text[lo:hi])

        # --- Ctrl+X ---
        if ctrl and glfw.KEY_X in just_pressed:
            if _has_selection():
                lo, hi = _sel_range()
                imgui.set_clipboard_text(text[lo:hi])
                text, _cursor_pos = _delete_selection(text)
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos
                changed = True

        # --- Ctrl+V ---
        if ctrl and glfw.KEY_V in just_pressed:
            _cursor_blink_time = time.time()
            clipboard = imgui.get_clipboard_text()
            if clipboard:
                if _has_selection():
                    text, _cursor_pos = _delete_selection(text)
                text = text[:_cursor_pos] + clipboard + text[_cursor_pos:]
                _cursor_pos += len(clipboard)
                _selection_start = _cursor_pos
                _selection_end = _cursor_pos
                changed = True

    # Update previous key state
    _prev_keys_down = current_keys

    # Clamp
    _cursor_pos = max(0, min(_cursor_pos, len(text)))
    _selection_start = max(0, min(_selection_start, len(text)))
    _selection_end = max(0, min(_selection_end, len(text)))

    # --- Drawing ---
    draw_list = imgui.get_window_draw_list()
    rect_min_x = left
    rect_min_y = draw_state.clip_rect[1]
    rect_max_x = left + draw_state.content_width
    rect_max_y = draw_state.clip_rect[3]

    draw_list.push_clip_rect(rect_min_x, rect_min_y, rect_max_x, rect_max_y, True)

    # Selection
    if _has_selection():
        sel_color = (102 << 24) | (204 << 16) | (102 << 8) | 51  # rgba(51, 102, 204, 0.4)
        lo, hi = _sel_range()
        lines = text.split('\n')
        line_abs_start = 0
        for line_idx, line_text in enumerate(lines):
            line_abs_end = line_abs_start + len(line_text)
            if line_abs_end >= lo and line_abs_start <= hi:
                sel_start_in_line = max(0, lo - line_abs_start)
                sel_end_in_line = min(len(line_text), hi - line_abs_start)
                sx = origin_x + imgui.calc_text_size(line_text[:sel_start_in_line]).x
                ex = origin_x + imgui.calc_text_size(line_text[:sel_end_in_line]).x
                sy = origin_y + line_idx * line_height
                if hi > line_abs_end and line_abs_end >= lo:
                    ex = origin_x + imgui.calc_text_size(line_text).x + imgui.calc_text_size(' ').x
                draw_list.add_rect_filled(sx, sy, ex, sy + line_height, sel_color)
            line_abs_start = line_abs_end + 1

    # Syntax highlighted text
    x = origin_x
    y = origin_y
    t_idx= 0
    for token, color_key in tokenize(text):
        color = COLORS[color_key]
        for ch in token:
            if ch == '\n':
                x = origin_x
                y += line_height
                continue
            if y + line_height >= rect_min_y and y <= rect_max_y:
                draw_list.add_text(x, y, color, ch)
            x += imgui.calc_text_size(ch).x
        t_idx += 1


    # Cursor
    if _is_focused and not _has_selection():
        if (time.time() - _cursor_blink_time) % 1.0 < 0.5:
            cx, cy = _char_pos_to_xy(text, _cursor_pos, origin_x, origin_y, line_height)
            cursor_color = 0xFFFFFFFF  # white
            draw_list.add_line(cx, cy, cx, cy + line_height, cursor_color, 1.0)

    draw_list.pop_clip_rect()

    if changed:
        text_height = imgui.calc_text_size(str(text) + " ")[1] + 2
    else:
        text_height = imgui.calc_text_size(str(input_value) + " ")[1] + 2

    imgui.dummy(draw_state.content_width, text_height)


    if changed:
        rebuilt_text = text + '\n'.join(original_input.split('\n')[max_lines:])
        return True, rebuilt_text
    return False, original_input