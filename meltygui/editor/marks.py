"""Per line marks the editor paints: errors, import fixes, live values, tints.

Each function turns one of the view's parameters into a dictionary keyed by
0 based document line. They run when the parameter object changes, never per
frame.
"""
from collections import namedtuple

# One live value on a line: its name, short text (None when not simple) and the value.
Value = namedtuple('Value', 'name text value')


def errors(found):
    """{line: message} from `[(line, message)]` (1 based lines), an exception,
    or a parse tree carrying error markers."""
    if not found:
        return {}
    if isinstance(found, BaseException):
        from meltygui.editor.syntax import tools
        found = tools()._exception_errors(found)
    marks = {}
    for line, message in found:
        line = max(int(line) - 1, 0)
        marks[line] = f'{marks[line]}; {message}' if line in marks else str(message)
    return marks


def fixes(found):
    """{line: [import statement]} from `{1 based line: [statement]}`."""
    return {max(int(line) - 1, 0): list(statements) for line, statements in (found or {}).items()}


def live(store, offset=0, moved=None):
    """{line: [Value]} from a live store (`code/live_view.py`). `offset` is
    the file line of the buffer's first line; `moved(line)` maps a recorded
    line to its current one (or None) while the text has drifted."""
    if store is None:
        return {}
    from meltygui.code.live_view import live_values_for
    from meltygui.editor.live_view_views import _split_line_key, _inline_value_text
    marks = {}
    for key, value in live_values_for(store).items():
        name, line = _split_line_key(key[-1])
        if name is None:
            continue
        line -= offset + 1
        if moved is not None:
            line = moved(line)
        if line is None or line < 0:
            continue
        marks.setdefault(line, []).append(Value(name, _inline_value_text(value), value))
    return marks


def tints(found, doc):
    """(blocks, washes) from definition tints: `blocks` are (head, last,
    indent, rgb); `washes` {line: [(column, length, rgb)]} for symbol uses.
    `found` is the (blocks, spans, ...) result of `roster_tints.collect_def_tints`
    or the legacy `_collect_def_tints`."""
    if not found:
        return [], {}
    blocks = [(head, last, doc.place(min(indent, len(doc.text)))[1], tuple(rgb[:3]))
              for head, indent, last, rgb in found[0]]
    washes = {}
    for start, stop, rgb, *_ in found[1]:
        line, column = doc.place(min(start, len(doc.text)))
        washes.setdefault(line, []).append((column, stop - start, tuple(rgb[:3])))
    return blocks, washes


def moved(found, line, delta):
    """`found` after `delta` lines were added (or removed) below `line`: marks
    follow their text until the caller supplies fresh ones."""
    if not found or not delta:
        return found
    gone = range(line + 1, line - delta + 1) if delta < 0 else ()
    return {(at + delta if at > line else at): mark for at, mark in found.items() if at not in gone}
