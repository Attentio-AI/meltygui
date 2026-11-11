"""Edit commands over a `Document`. Each takes the document first and changes
it through `Document.replace`; none draws or reads input.

Indentation, comment toggling and word rules are the pure helpers of
`text_editor.py`. To add a command, write it here and bind it in `keys.py`.
"""
from meltygui.editor.syntax import tools

TAB = 4
# Brackets and quotes typed with a selection wrap it.
PAIRS = {'(': ')', '[': ']', '{': '}', '"': '"', "'": "'"}


# --- caret ---------------------------------------------------------------
def move(doc, index, select=False):
    doc.select(index, doc.anchor if select else None)


def left(doc, select=False, word=False):
    low, high = doc.selection
    if low != high and not select:
        return move(doc, low)
    move(doc, tools()._word_boundary_left(doc.text, doc.caret) if word else doc.caret - 1, select)


def right(doc, select=False, word=False):
    low, high = doc.selection
    if low != high and not select:
        return move(doc, high)
    move(doc, tools()._word_boundary_right(doc.text, doc.caret) if word else doc.caret + 1, select)


def home(doc, select=False, whole=False):
    """Line start: first the indent, then column 0. `whole` is the text start."""
    if whole:
        return move(doc, 0, select)
    line, column = doc.place(doc.caret)
    text = doc.line(line)
    indent = len(text) - len(text.lstrip(' '))
    move(doc, doc.start(line) + (0 if column == indent else indent), select)


def end(doc, select=False, whole=False):
    move(doc, len(doc.text) if whole else doc.end(doc.line_of(doc.caret)), select)


def vertical(doc, rows, delta, select=False):
    """Move `delta` displayed rows, keeping the goal column across short lines."""
    line, column = doc.place(doc.caret)
    goal = column if doc.goal is None else doc.goal
    row = rows.row(line) + delta
    if row < 0:
        index = 0
    elif row >= rows.count:
        index = len(doc.text)
    else:
        index = doc.index(rows.line(row), goal)
    move(doc, index, select)
    doc.goal = goal


def select_all(doc):
    doc.select(len(doc.text), 0)


def select_word(doc, index):
    helper = tools()
    doc.select(helper._select_unit_right(doc.text, index), helper._select_unit_left(doc.text, index))


def select_line(doc, index):
    line = doc.line_of(index)
    doc.select(min(doc.end(line) + 1, len(doc.text)), doc.start(line))


def selected(doc):
    low, high = doc.selection
    return doc.text[low:high]


# --- text ----------------------------------------------------------------
def insert(doc, text, merge=False):
    """Type or paste `text` over the selection."""
    low, high = doc.selection
    if low != high and text in PAIRS:
        return doc.replace(low, high, text + doc.text[low:high] + PAIRS[text], caret=high + 2)
    return doc.replace(low, high, text, merge=merge and low == high)


def paste(doc, text):
    """Multi line pastes keep their shape relative to the caret's indent."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    low, _ = doc.selection
    prefix = doc.text[doc.start(doc.line_of(low)):low]
    if '\n' in text and not prefix.strip():
        text = tools()._reindent_paste(text, prefix)
    return insert(doc, text)


def backspace(doc, word=False):
    low, high = doc.selection
    if low != high:
        return doc.replace(low, high, '')
    caret = doc.caret
    if not caret:
        return None
    if word:
        return doc.replace(tools()._word_boundary_left(doc.text, caret), caret, '')
    line, column = doc.place(caret)
    if column and not doc.text[doc.start(line):caret].strip(' '):
        # In the indent: back to the previous indent stop.
        return doc.replace(doc.start(line) + tools()._prev_indent_stop(doc.text, doc.start(line), column), caret, '')
    return doc.replace(caret - 1, caret, '')


def delete(doc, word=False):
    low, high = doc.selection
    if low != high:
        return doc.replace(low, high, '')
    caret = doc.caret
    if caret >= len(doc.text):
        return None
    return doc.replace(caret, tools()._word_boundary_right(doc.text, caret) if word else caret + 1, '', caret=caret)


def enter(doc, syntax=None, below=False):
    """A new line with the indent its context asks for: inside an open
    bracket, after a block opener, continuing a comment or a split string.
    `below` opens the line under the current one without splitting it."""
    helper = tools()
    low, high = doc.selection
    if low != high:
        doc.replace(low, high, '')
    text, at = doc.text, doc.caret
    stop = doc.end(doc.line_of(at))
    if below:
        at = stop
    # The bracket helpers scan back from the caret: give them only the
    # statement, from its last line that starts at column 0.
    line = doc.line_of(at)
    while line and text[doc.start(line):doc.start(line) + 1] in ' \n)]}#':
        line -= 1
    base = doc.start(line)
    part, spot = text[base:stop], at - base
    indent = helper._open_bracket_indent(part, spot)
    if indent is None:
        opener = helper._unclosed_opener(part, helper._get_line_start(part, spot))
        indent = (helper._get_indent(part, opener) if opener is not None
                  else helper._get_indent(part, spot) + helper._block_open_extra(part, spot))
    tail = at
    while tail < stop and text[tail] == ' ':
        tail += 1
    lead = ''
    if syntax is not None and syntax.live and tail < stop:
        syntax.update(text)
        split = helper._string_split(text, at, stop, syntax.starts, syntax.open)
        if split is not None:
            return _rewrite(doc, split[0], split[1])
        comment = helper._comment_continuation(text, at, stop, syntax.starts, syntax.open)
        if comment is not None:
            indent, lead = comment
    return doc.replace(at, tail, '\n' + ' ' * indent + lead)


def _rewrite(doc, text, caret=None):
    """Apply a helper's whole-text result as the smallest replace. Without a
    `caret` the caret stays on the text it was on."""
    old = doc.text
    head = 0
    limit = min(len(old), len(text))
    while head < limit and old[head] == text[head]:
        head += 1
    tail = 0
    while tail < limit - head and old[len(old) - 1 - tail] == text[len(text) - 1 - tail]:
        tail += 1
    if caret is None:
        caret = doc.caret + (len(text) - len(old) if doc.caret >= len(old) - tail else 0)
    return doc.replace(head, len(old) - tail, text[head:len(text) - tail], caret=caret)


def _keep_selection(doc, change, low, high):
    """After a block command the shifted block stays selected."""
    if low != high:
        doc.select(high, low)
    return change


def indent(doc, dedent=False):
    """Tab: indent the selected lines, or pad the caret to the next tab stop."""
    low, high = doc.selection
    if low == high and not dedent:
        column = doc.place(low)[1]
        return doc.replace(low, low, ' ' * (TAB - column % TAB))
    text, new_low, new_high = tools()._indent_lines(doc.text, low, high, dedent)
    return _keep_selection(doc, _rewrite(doc, text, new_high), new_low, new_high if low != high else new_low)


def comment(doc):
    """Toggle `# ` on the selected lines."""
    low, high = doc.selection
    text, new_low, new_high = tools()._toggle_comment(doc.text, low, high)
    return _keep_selection(doc, _rewrite(doc, text, new_high), new_low, new_high if low != high else new_low)


def cut(doc):
    """Remove the selection, or the whole line without one. Returns the text."""
    low, high = doc.selection
    if low == high:
        line = doc.line_of(low)
        low, high = doc.start(line), min(doc.end(line) + 1, len(doc.text))
    text = doc.text[low:high]
    doc.replace(low, high, '')
    return text


def set_token(doc, line, column, length, text):
    """Replace one token in place (inline widgets), keeping the caret where it was."""
    low = doc.start(line) + column
    caret = doc.caret
    if caret > low:
        caret = max(low, caret + len(text) - length)
    return doc.replace(low, low + length, text, caret=caret)
