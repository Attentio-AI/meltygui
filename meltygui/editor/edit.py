"""Edit commands over a `Document`. Each takes the document first and changes
it through `Document.replace`; none draws or reads input.

Indentation, comment toggling and word rules are the pure helpers of
`text_editor.py`. They are handed the text near the caret (`Document.near`) or
the selected lines, never the whole file, so a command costs the same in a
long file as in a short one. To add a command, write it here and bind it in
`keys.py`.
"""
from meltygui.editor.syntax import tools

TAB = 4
# Brackets and quotes typed with a selection wrap it.
PAIRS = {'(': ')', '[': ']', '{': '}', '"': '"', "'": "'"}


def _near(doc, helper, index):
    """A legacy `helper(text, index) -> index` over the text around `index`."""
    text, base = doc.near(index)
    return base + helper(text, index - base)


# --- caret ---------------------------------------------------------------
def move(doc, index, select=False):
    doc.select(index, doc.anchor if select else None)


def left(doc, select=False, word=False):
    low, high = doc.selection
    if low != high and not select:
        return move(doc, low)
    move(doc, _near(doc, tools()._word_boundary_left, doc.caret) if word else doc.caret - 1, select)


def right(doc, select=False, word=False):
    low, high = doc.selection
    if low != high and not select:
        return move(doc, high)
    move(doc, _near(doc, tools()._word_boundary_right, doc.caret) if word else doc.caret + 1, select)


def home(doc, select=False, whole=False):
    """Line start: first the indent, then column 0. `whole` is the text start."""
    if whole:
        return move(doc, 0, select)
    line, column = doc.place(doc.caret)
    text = doc.line(line)
    indent = len(text) - len(text.lstrip(' '))
    move(doc, doc.start(line) + (0 if column == indent else indent), select)


def end(doc, select=False, whole=False):
    move(doc, doc.size if whole else doc.end(doc.line_of(doc.caret)), select)


def vertical(doc, rows, delta, select=False):
    """Move `delta` displayed rows, keeping the goal column across short lines."""
    line, column = doc.place(doc.caret)
    goal = column if doc.goal is None else doc.goal
    row = rows.row(line) + delta
    if row < 0:
        index = 0
    elif row >= rows.count:
        index = doc.size
    else:
        index = doc.index(rows.line(row), goal)
    move(doc, index, select)
    doc.goal = goal


def select_all(doc):
    doc.select(doc.size, 0)


def select_word(doc, index):
    helper = tools()
    doc.select(_near(doc, helper._select_unit_right, index), _near(doc, helper._select_unit_left, index))


def select_line(doc, index):
    line = doc.line_of(index)
    doc.select(min(doc.end(line) + 1, doc.size), doc.start(line))


def selected(doc):
    return doc.slice(*doc.selection)


# --- text ----------------------------------------------------------------
def insert(doc, text):
    """Type or paste `text` over the selection."""
    low, high = doc.selection
    if low != high and text in PAIRS:
        return doc.replace(low, high, text + doc.slice(low, high) + PAIRS[text], caret=high + 2)
    return doc.replace(low, high, text)


def paste(doc, text):
    """Multi line pastes keep their shape relative to the caret's indent."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    low, _ = doc.selection
    prefix = doc.slice(doc.start(doc.line_of(low)), low)
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
        return doc.replace(_near(doc, tools()._word_boundary_left, caret), caret, '')
    line, column = doc.place(caret)
    if column and not doc.slice(caret - column, caret).strip(' '):
        # In the indent: back to the previous indent stop.
        text, base = doc.near(caret)
        return doc.replace(caret - column + tools()._prev_indent_stop(text, caret - column - base, column), caret, '')
    return doc.replace(caret - 1, caret, '')


def delete(doc, word=False):
    low, high = doc.selection
    if low != high:
        return doc.replace(low, high, '')
    caret = doc.caret
    if caret >= doc.size:
        return None
    return doc.replace(caret, _near(doc, tools()._word_boundary_right, caret) if word else caret + 1, '', caret=caret)


def enter(doc, syntax=None, below=False):
    """A new line with the indent its context asks for: inside an open
    bracket, after a block opener, continuing a comment or a split string.
    `below` opens the line under the current one without splitting it."""
    helper = tools()
    low, high = doc.selection
    if low != high:
        doc.replace(low, high, '')
    at = doc.caret
    line = doc.line_of(at)
    stop = doc.end(line)
    if below:
        at = stop
    # The bracket helpers scan back from the caret: give them only the
    # statement, from its last line that starts at column 0.
    first = line
    while first and doc.slice(doc.start(first), doc.start(first) + 1) in (' ', '\n', ')', ']', '}', '#', ''):
        first -= 1
    base = doc.start(first)
    part, spot = doc.slice(base, stop), at - base
    indent = helper._open_bracket_indent(part, spot)
    if indent is None:
        opener = helper._unclosed_opener(part, helper._get_line_start(part, spot))
        indent = (helper._get_indent(part, opener) if opener is not None
                  else helper._get_indent(part, spot) + helper._block_open_extra(part, spot))
    tail = spot
    while tail < len(part) and part[tail] == ' ':
        tail += 1
    lead = ''
    if syntax is not None and syntax.live and tail < len(part):
        # The string and comment helpers read line starts and string states: the statement's.
        starts = [doc.start(found) - base for found in range(first, line + 1)]
        states = [syntax.state(doc, found) for found in range(first, line + 1)]
        split = helper._string_split(part, spot, len(part), starts, states)
        if split is not None:
            return rewrite(doc, split[0], base + split[1], base, part)
        comment = helper._comment_continuation(part, spot, len(part), starts, states)
        if comment is not None:
            indent, lead = comment
    return doc.replace(at, base + tail, '\n' + ' ' * indent + lead)


def differ(old, new):
    """(head, tail): how many characters two texts share at the start and,
    after that, at the end. Compares slices, so long texts cost a few
    comparisons, not one per character."""
    limit, step = min(len(old), len(new)), 4096
    head = 0
    while head + step <= limit and old[head:head + step] == new[head:head + step]:
        head += step
    while head < limit and old[head] == new[head]:
        head += 1
    limit -= head
    tail = 0
    while tail + step <= limit and old[len(old) - tail - step:len(old) - tail] == new[len(new) - tail - step:len(new) - tail]:
        tail += step
    while tail < limit and old[len(old) - 1 - tail] == new[len(new) - 1 - tail]:
        tail += 1
    return head, tail


def rewrite(doc, text, caret=None, base=0, old=None):
    """Apply a helper's result as the smallest replace: `text` is what `old`
    (the text at offset `base`; default: the whole document) became. Without
    a `caret` the caret stays on the text it was on."""
    if old is None:
        old = doc.text
    head, tail = differ(old, text)
    stop = base + len(old) - tail
    if caret is None:
        caret = doc.caret + (len(text) - len(old) if doc.caret >= stop else 0)
    return doc.replace(base + head, stop, text[head:len(text) - tail], caret=caret)


def _keep_selection(doc, change, low, high):
    """After a block command the shifted block stays selected."""
    if low != high:
        doc.select(high, low)
    return change


def _lines(doc, helper, **options):
    """Run a legacy `helper(text, low, high) -> (text, low, high)` over the
    selected lines only and keep them selected."""
    low, high = doc.selection
    base, stop = doc.start(doc.line_of(low)), doc.end(doc.line_of(high))
    part = doc.slice(base, stop)
    text, new_low, new_high = helper(part, low - base, high - base, **options)
    change = rewrite(doc, text, base + new_high, base, part)
    return _keep_selection(doc, change, base + new_low, base + (new_high if low != high else new_low))


def indent(doc, dedent=False):
    """Tab: indent the selected lines, or pad the caret to the next tab stop."""
    low, high = doc.selection
    if low == high and not dedent:
        column = doc.place(low)[1]
        return doc.replace(low, low, ' ' * (TAB - column % TAB))
    return _lines(doc, tools()._indent_lines, dedent=dedent)


def comment(doc):
    """Toggle `# ` on the selected lines."""
    return _lines(doc, tools()._toggle_comment)


def cut(doc):
    """Remove the selection, or the whole line without one. Returns the text."""
    low, high = doc.selection
    if low == high:
        line = doc.line_of(low)
        low, high = doc.start(line), min(doc.end(line) + 1, doc.size)
    text = doc.slice(low, high)
    doc.replace(low, high, '')
    return text


def set_token(doc, line, column, length, text):
    """Replace one token in place (inline widgets), keeping the caret where it was."""
    low = doc.start(line) + column
    caret = doc.caret
    if caret > low:
        caret = max(low, caret + len(text) - length)
    return doc.replace(low, low + length, text, caret=caret)
