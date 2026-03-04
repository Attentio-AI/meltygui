import os
import sys
import re
import io
import pprint
import inspect
import threading
import traceback
import linecache
from contextlib import contextmanager

import glfw

from src.lsd.gl_gui.toggles import Toggles

# ── Syntax highlighting (IntelliJ Darcula) ───────────────
try:
    from pygments import highlight as _pyg_highlight
    from pygments.lexers import PythonLexer
    from pygments.formatters import TerminalTrueColorFormatter
    from pygments.style import Style
    from pygments.token import (
        Token, Keyword, Name, Comment, String, Number,
        Operator, Punctuation, Literal, Generic, Error
    )


    class DarculaIntelliJ(Style):
        background_color = "#2b2b2b"
        styles = {
            Token: "#a9b7c6",
            Comment: "italic #808080",
            Comment.Preproc: "#808080",
            Keyword: "#cc7832",
            Keyword.Constant: "#cc7832",
            Keyword.Namespace: "#cc7832",
            Keyword.Type: "#cc7832",
            Name: "#a9b7c6",
            Name.Builtin: "#8888c6",
            Name.Builtin.Pseudo: "#94558d",
            Name.Function: "#ffc66d",
            Name.Function.Magic: "#ffc66d",
            Name.Class: "#a9b7c6",
            Name.Decorator: "#bbb529",
            Name.Exception: "#a9b7c6",
            Name.Variable: "#a9b7c6",
            Name.Attribute: "#9876aa",
            Name.Tag: "#e8bf6a",
            String: "#6a8759",
            String.Doc: "italic #629755",
            String.Escape: "#cc7832",
            String.Interpol: "#cc7832",
            String.Regex: "#6a8759",
            Number: "#6897bb",
            Number.Float: "#6897bb",
            Number.Integer: "#6897bb",
            Operator: "#a9b7c6",
            Operator.Word: "#cc7832",
            Punctuation: "#a9b7c6",
            Literal: "#6a8759",
            Generic.Deleted: "#ff5555",
            Generic.Inserted: "#6a8759",
            Generic.Error: "#ff5555",
            Generic.Emph: "italic",
            Generic.Strong: "bold",
            Error: "#ff5555",
        }


    _pygments_available = True
    _python_lexer = PythonLexer()
    _terminal_formatter = TerminalTrueColorFormatter(style=DarculaIntelliJ)
except ImportError:
    _pygments_available = False

# ── ANSI codes ───────────────────────────────────────────
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"
_WHITE = "\033[97m"
_RED = "\033[31m"
_BLACK = "\033[30m"
_BG_DARK = "\033[48;2;43;43;43m"  # #2B2B2B Darcula background

_BG_COLORS = [
    "\033[41m", "\033[42m", "\033[43m",
    "\033[44m", "\033[45m", "\033[46m",
]

_IDE = "intellij"

_IDE_SCHEMES = {
    "idea": "idea://open?file={path}&line={line}",
    "pycharm": "pycharm://open?file={path}&line={line}",
    "fleet": "fleet://open?file={path}&line={line}",
    "goland": "goland://open?file={path}&line={line}",
    "webstorm": "webstorm://open?file={path}&line={line}",
    "vscode": "vscode://file/{path}:{line}",
    "traceback": None,
    "intellij": None,
}

_INDENT = "  "

_print_lock = threading.Lock()
_job_counter = 0
_job_counter_lock = threading.Lock()
_ANSI_RE = re.compile(r'\033\[[0-9;]*m|\033\]8;[^\033]*\033\\')


def _next_job_color():
    global _job_counter
    with _job_counter_lock:
        color = _BG_COLORS[_job_counter % len(_BG_COLORS)]
        _job_counter += 1
    return color


def _visible_len(s):
    """String length ignoring ANSI escape codes."""
    return len(_ANSI_RE.sub('', s))


def _pad(s, width):
    """Pad a string with ANSI codes to a visible width."""
    return s + " " * max(0, width - _visible_len(s))


def _highlight(code, lineno=None):
    """Syntax-highlight a line of Python with editor-style background and line number."""
    if _pygments_available:
        colored = _pyg_highlight(code, _python_lexer, _terminal_formatter).rstrip('\n')
    else:
        colored = f"{_YELLOW}{code}{_RESET}"

    if lineno is not None:
        gutter = f"{_BG_DARK}{_DIM} {lineno:>4} {_RESET}"
    else:
        gutter = ""

    visible = len(code)
    pad_width = max(80 - visible, 4)
    return f"{gutter}{_BG_DARK} {colored}{' ' * pad_width}{_RESET}"


def _highlight_inline(code):
    """Syntax-highlight a short code snippet without background or gutter."""
    if not _pygments_available:
        return f"{_GREEN}{code}{_RESET}"
    return _pyg_highlight(code, _python_lexer, _terminal_formatter).rstrip('\n')


# ── Table rendering ──────────────────────────────────────

# Column background shades - for alternation
_BG_COL_A = "\033[48;2;46;46;46m"  # slightly lighter
_BG_COL_B = "\033[48;2;38;38;38m"  # slightly darker

_TABLE_LINE = "\033[38;2;60;60;60m"  # dark grey for box-drawing chars


def _render_frame_table(file_line, code_line, watch_rows, indent=None):
    """
    Render a frame as a unified table:
    - File line as a spanning top row
    - Watch variables as columns
    - Code line as a spanning bottom row
    """
    pad_str = indent if indent else _INDENT

    # Calculate watch column widths
    if watch_rows:
        cols = list(zip(*watch_rows))
        widths = [max(_visible_len(cell) for cell in col) for col in cols]
        inner_width = sum(widths) + 3 * len(widths) - 1  # cells + padding + separators
    else:
        inner_width = max(_visible_len(file_line), _visible_len(code_line)) + 2

    # Ensure spanning rows fit
    inner_width = max(inner_width, _visible_len(file_line) + 2, _visible_len(code_line) + 2)

    buf = []

    # Top border
    buf.append(f"{pad_str}{_TABLE_LINE}┌{'─' * inner_width}┐{_RESET}")

    # File line - spanning row
    file_pad = inner_width - _visible_len(file_line) - 1
    buf.append(f"{pad_str}{_TABLE_LINE}│{_RESET} {file_line}{' ' * file_pad}{_TABLE_LINE}│{_RESET}")

    if watch_rows:
        # Separator between file line and watch rows
        sep = f"─{'─' * widths[0]}─"
        for w in widths[1:]:
            sep += f"┬─{'─' * w}─"
        # Pad separator to full width
        sep_visible = _visible_len(sep)
        if sep_visible < inner_width:
            sep += "─" * (inner_width - sep_visible)
        buf.append(f"{pad_str}{_TABLE_LINE}├{sep}┤{_RESET}")

        # Watch rows
        for name, typ, val, link in watch_rows:
            row = (
                f" {_pad(name, widths[0])} "
                f"{_TABLE_LINE}│{_RESET} {_pad(typ, widths[1])} "
                f"{_TABLE_LINE}│{_RESET} {_pad(val, widths[2])} "
                f"{_TABLE_LINE}│{_RESET} {_pad(link, widths[3])} "
            )
            row_pad = inner_width - _visible_len(row)
            buf.append(f"{pad_str}{_TABLE_LINE}│{_RESET}{row}{' ' * row_pad}{_TABLE_LINE}│{_RESET}")

        # Separator between watch rows and code line
        sep = f"─{'─' * widths[0]}─"
        for w in widths[1:]:
            sep += f"┴─{'─' * w}─"
        sep_visible = _visible_len(sep)
        if sep_visible < inner_width:
            sep += "─" * (inner_width - sep_visible)
        buf.append(f"{pad_str}{_TABLE_LINE}├{sep}┤{_RESET}")
    else:
        # Simple separator
        buf.append(f"{pad_str}{_TABLE_LINE}├{'─' * inner_width}┤{_RESET}")

    # Code line - spanning row
    code_pad = inner_width - _visible_len(code_line) - 1
    buf.append(f"{pad_str}{_TABLE_LINE}│{_RESET} {code_line}{' ' * max(code_pad, 0)}{_TABLE_LINE}│{_RESET}")

    # Bottom border
    buf.append(f"{pad_str}{_TABLE_LINE}└{'─' * inner_width}┘{_RESET}")

    return "\n".join(buf) + "\n"
def _render_watch_table(rows, indent=None):
    """
    Render watched variables as a compact box-drawing table.
    rows: list of (name_styled, type_styled, value_styled, link_styled)
    """
    if not rows:
        return ""

    pad_str = indent if indent else _INDENT

    cols = list(zip(*rows))
    widths = [max(_visible_len(cell) for cell in col) for col in cols]

    buf = []

    buf.append(
        f"{pad_str}{_TABLE_LINE}┌─{'─' * widths[0]}─┬─{'─' * widths[1]}─┬─{'─' * widths[2]}─┬─{'─' * widths[3]}─┐{_RESET}"
    )

    for name, typ, val, link in rows:
        buf.append(
            f"{pad_str}{_TABLE_LINE}│{_RESET} {_pad(name, widths[0])} "
            f"{_TABLE_LINE}│{_RESET} {_pad(typ, widths[1])} "
            f"{_TABLE_LINE}│{_RESET} {_pad(val, widths[2])} "
            f"{_TABLE_LINE}│{_RESET} {_pad(link, widths[3])} {_TABLE_LINE}│{_RESET}"
        )

    buf.append(
        f"{pad_str}{_TABLE_LINE}└─{'─' * widths[0]}─┴─{'─' * widths[1]}─┴─{'─' * widths[2]}─┴─{'─' * widths[3]}─┘{_RESET}"
    )

    return "\n".join(buf) + "\n"

# ── Trace group ──────────────────────────────────────────

class TraceGroup:
    """Buffers multiple print_stack_trace calls and flushes atomically."""
    bar_size_outer = 58
    bar_size_inner = 30

    def __init__(self, label, color, **meta):
        self.buf = io.StringIO()
        self.label = label
        self.color = color
        self.meta = meta

    def _bar(self, text="", size=None):
        size = size or self.bar_size_inner
        if text:
            pad = size - len(text) - 4
            return f"{self.color}{_BLACK}{_BOLD} ▌ {text} {'─' * max(pad, 0)} {_RESET}"
        return f"{self.color}{_BLACK}{_BOLD} {'─' * size} {_RESET}"

    def write_header(self):
        self.buf.write(f"\n{self._bar(self.label, size=self.bar_size_outer)}\n")
        if self.meta:
            meta = "  ".join(f"{k}={v}" for k, v in self.meta.items())
            self.buf.write(f"{self.color} {_RESET}{_DIM}  {meta}{_RESET}\n")

    def write_section(self, name):
        self.buf.write(f"{self._bar(name)}\n")

    def write_footer(self):
        self.buf.write(f"{self._bar(size=self.bar_size_outer)}\n\n")

    def flush(self, dest=None):
        dest = dest or sys.stdout
        with _print_lock:
            dest.write(self.buf.getvalue())
            dest.flush()


@contextmanager
def trace_group(label, **meta):
    g = TraceGroup(label, _next_job_color(), **meta)
    g.write_header()
    try:
        yield g
    finally:
        g.write_footer()
        g.flush()


# ── Function resolution for watch expressions ────────────

def _resolve_func(name):
    import builtins
    if hasattr(builtins, name):
        return getattr(builtins, name)
    if "." in name:
        parts = name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod_path = ".".join(parts[:i])
            attr_path = parts[i:]
            try:
                import importlib
                obj = importlib.import_module(mod_path)
                for attr in attr_path:
                    obj = getattr(obj, attr)
                return obj
            except (ImportError, AttributeError):
                continue
    for mod in sys.modules.values():
        if mod and hasattr(mod, name):
            return getattr(mod, name)
    return None


def _parse_watch(expr):
    funcs = []
    while True:
        match = re.match(r'^([\w.]+)\((.+)\)$', expr)
        if match:
            func_name = match.group(1)
            if _resolve_func(func_name) is not None:
                funcs.append(func_name)
                expr = match.group(2)
                continue
        break
    return funcs, expr


# ── Main entry point ─────────────────────────────────────

def print_stack_trace(size=None, skip=-1, stack=None, frames=None, watch=None,
                      max_str_len=80, max_items=2, max_depth=3, max_output=80,
                      exception=None, section=None, group=None, file=None):
    """
    Print a stack trace with optional variable watching.

    Args:
        size:        Max number of frames to show (None = all).
        skip:        How many trailing frames to skip (-1 skips this function).
        stack:       Pre-extracted stack to use instead of the current one.
        frames:      Pre-captured live frames (from get_live_frames).
        watch:       List of variable names, dotted paths, or func(path) expressions.
        max_str_len: Truncate strings beyond this length (None = no limit).
        max_items:   Max items in lists/dicts/sets (None = no limit).
        max_depth:   Max nesting depth before summary (None = no limit).
        max_output:  Hard cap on final formatted string per variable (None = no limit).
        exception:   Exception object — extracts frames and prints at the end.
        section:     Section label when used inside a trace_group.
        group:       TraceGroup instance — buffers output into the group.
        file:        Output stream override.
    """
    buf = io.StringIO()

    if exception is not None:
        frames = get_exception_frames(exception)
        skip = None

    if frames is None and stack is None:
        frames = get_live_frames(skip_count=1)
    elif frames is None and stack is not None:
        formatted = traceback.format_list(
            stack[:skip] if size is None else stack[-size:skip]
        )
        for frame in formatted:
            buf.write(frame)
        _dispatch(buf, group, file)
        return

    if skip == -1:
        frames = frames[:-1]
    elif skip is not None and skip < -1:
        frames = frames[:skip]

    if size is not None:
        frames = frames[-size:]

    watch_paths = watch if watch else []

    if group and section:
        group.write_section(section)

    if not group:
        thread_name = threading.current_thread().name
        bar_color = _CYAN
        title = "Exception Trace" if exception else "Stack Trace"
        buf.write(f"{_BOLD}{bar_color}{'─' * 60}{_RESET}\n")
        buf.write(f"{_BOLD}{bar_color}{title}{_RESET} {_DIM}[{thread_name}]{_RESET}\n")
        buf.write(f"{_BOLD}{bar_color}{'─' * 60}{_RESET}\n")

    for i, (filename, lineno, funcname, line_text, local_vars) in enumerate(frames):
        rel = _rel_path(filename)
        file_line = (
            f"File \"{_DIM}{rel}{_RESET}\", line {_WHITE}{lineno}{_RESET},"
            f" in {_BOLD}{_WHITE}{funcname}{_RESET}"
        )

        code_line = _highlight(line_text.strip(), lineno) if line_text else ""

        # Variable watches
        table_rows = []
        if watch_paths and local_vars is not None:
            for expr in watch_paths:
                funcs, path = _parse_watch(expr)
                root = _get_root_name(path)
                if root not in local_vars:
                    continue
                success, value = _resolve_path(path, local_vars)
                if not success:
                    continue
                try:
                    for func_name in reversed(funcs):
                        value = _resolve_func(func_name)(value)
                except Exception as ex:
                    value = f"<{func_name}() raised {type(ex).__name__}: {ex}>"

                display_val = _truncate(value, max_str_len, max_items, max_depth)
                formatted_value = pprint.pformat(display_val, width=50)
                if max_output and len(formatted_value) > max_output:
                    cut = formatted_value[:max_output].rfind('\n')
                    if cut == -1:
                        cut = max_output
                    formatted_value = formatted_value[:cut] + f"…({len(formatted_value)}ch)"
                formatted_value = formatted_value.replace("\n", f"{_YELLOW}\\n{_MAGENTA}")

                name_cell = _highlight_inline(expr)
                type_cell = f"{_DIM}{type(value).__name__}{_RESET}"
                value_cell = f"{_MAGENTA}{formatted_value}{_RESET}"
                def_line = _find_assignment(filename, lineno, root)
                link_cell = _make_link(filename, def_line)

                table_rows.append((name_cell, type_cell, value_cell, link_cell))

        if code_line or table_rows:
            buf.write(_render_frame_table(file_line, code_line, table_rows))
        else:
            buf.write(f"{_INDENT}{file_line}\n")



    if exception is not None:
        buf.write(f"{_INDENT}{_RED}{_BOLD}{type(exception).__name__}: {exception}{_RESET}\n")

    if not group:
        bar_color = _CYAN
        buf.write(f"{_BOLD}{bar_color}{'─' * 60}{_RESET}\n")

    _dispatch(buf, group, file)


# ── Path helpers ─────────────────────────────────────────

def _rel_path(filepath):
    """Get a relative path with ./ prefix."""
    try:
        rel = os.path.relpath(filepath)
        if not rel.startswith("."):
            rel = "./" + rel
        return rel
    except ValueError:
        return os.path.abspath(filepath)


def _make_link(filepath, line):
    """Short clickable link for the table's link column."""
    rel = _rel_path(filepath)
    line = line or 1

    if _IDE in ("intellij", "traceback"):
        return f"{_DIM}File \"{_BLUE}{rel}{_DIM}\", line {line}{_RESET}"

    abs_path = os.path.abspath(filepath)
    if _IDE and _IDE in _IDE_SCHEMES:
        uri = _IDE_SCHEMES[_IDE].format(path=abs_path, line=line)
    else:
        uri = f"file://{abs_path}:{line}"
    return f"\033]8;;{uri}\033\\{_DIM}:{line}{_RESET}\033]8;;\033\\"


# ── Output dispatch ──────────────────────────────────────

def _dispatch(buf, group=None, file=None):
    output = buf.getvalue()
    if group:
        group.buf.write(output)
    elif file:
        with _print_lock:
            file.write(output)
            file.flush()
    else:
        with _print_lock:
            sys.stdout.write(output)
            sys.stdout.flush()


# ── Frame extraction ─────────────────────────────────────

def get_live_frames(skip_count=0):
    """Walk the call stack and return frame info with local variables."""
    raw_frames = inspect.stack()
    results = []
    for frame_info in raw_frames[skip_count + 1:]:
        frame_obj = frame_info[0]
        results.append((
            frame_info[1],
            frame_info[2],
            frame_info[3],
            (frame_info[4] or [""])[0],
            dict(frame_obj.f_locals),
        ))
    results.reverse()
    return results


def get_exception_frames(e):
    """Extract live frames from a caught exception's traceback."""
    tb = e.__traceback__
    if tb is None:
        return []
    results = []
    while tb is not None:
        frame_obj = tb.tb_frame
        results.append((
            frame_obj.f_code.co_filename,
            tb.tb_lineno,
            frame_obj.f_code.co_name,
            linecache.getline(frame_obj.f_code.co_filename, tb.tb_lineno),
            dict(frame_obj.f_locals),
        ))
        tb = tb.tb_next
    return results


# ── Watch resolution ─────────────────────────────────────

_ATTR = 'attr'
_INDEX = 'index'


def _tokenize_path(path):
    """
    Break a path string into tagged access tokens.

    "draw_state.name"       -> [("attr", "draw_state"), ("attr", "name")]
    "my_dict['key']"        -> [("attr", "my_dict"), ("index", "key")]
    "items[0].name"         -> [("attr", "items"), ("index", 0), ("attr", "name")]
    "nested['a']['b'].val"  -> [("attr", "nested"), ("index", "a"), ("index", "b"), ("attr", "val")]
    """
    tokens = []
    for part in re.split(r'\.', path):
        segments = re.split(r'(\[[^\]]*\])', part)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if seg.startswith("[") and seg.endswith("]"):
                inner = seg[1:-1].strip()
                try:
                    tokens.append((_INDEX, int(inner)))
                except ValueError:
                    tokens.append((_INDEX, inner.strip("\"'")))
            else:
                tokens.append((_ATTR, seg))
    return tokens


def _resolve_path(path, local_vars):
    """
    Walk a dotted/indexed path against local variables.
    Dot access tries getattr first, then obj[key].
    Bracket access uses obj[key] only.
    """
    tokens = _tokenize_path(path)
    if not tokens:
        return False, None
    _, root = tokens[0]
    if root not in local_vars:
        return False, None
    obj = local_vars[root]
    for kind, token in tokens[1:]:
        try:
            if kind == _ATTR:
                try:
                    obj = getattr(obj, token)
                except AttributeError:
                    obj = obj[token]
            else:
                obj = obj[token]
        except (AttributeError, IndexError, KeyError, TypeError):
            return False, None
    return True, obj


def _get_root_name(path):
    """Extract the top-level variable name from a dotted/indexed path."""
    tokens = _tokenize_path(path)
    return tokens[0][1] if tokens else path


def _find_assignment(filepath, current_line, var_name, search_range=200):
    """Search backward from current_line to find where var_name is assigned."""
    try:
        with open(filepath, 'r') as f:
            lines = f.readlines()
    except (OSError, IOError):
        return None
    escaped = re.escape(var_name)
    patterns = [
        rf'^\s*{escaped}\s*=[^=]',
        rf'^\s*{escaped}\s*:',
        rf'^\s*for\s+{escaped}\s+in\s',
        rf'[\(,]\s*{escaped}\s*[,\)=:]',
        rf'^\s*with\s+.*\bas\s+{escaped}',
        rf'^\s*{escaped}\s*[+\-*|&^]='
    ]
    compiled = [re.compile(p) for p in patterns]
    start = min(current_line - 1, len(lines)) - 1
    stop = max(start - search_range, 0)
    for i in range(start, stop, -1):
        for pat in compiled:
            if pat.search(lines[i]):
                return i + 1
    return None


# ── Value truncation ─────────────────────────────────────

def _truncate(value, max_str_len=120, max_items=5, max_depth=3, _current_depth=0):
    """Recursively truncate values for display."""
    if max_depth is not None and _current_depth >= max_depth:
        return _summarize(value)
    next_depth = _current_depth + 1
    if isinstance(value, str):
        if max_str_len and len(value) > max_str_len:
            return value[:max_str_len] + f"…({len(value)}ch)"
        return value
    if isinstance(value, bytes):
        if max_str_len and len(value) > max_str_len:
            return value[:max_str_len] + f"…({len(value)}b)".encode()
        return value
    if isinstance(value, dict):
        items = list(value.items())
        show = max_items or len(items)
        truncated = {
            _truncate(k, max_str_len, max_items, max_depth, next_depth):
                _truncate(v, max_str_len, max_items, max_depth, next_depth)
            for k, v in items[:show]
        }
        remaining = len(items) - show
        if remaining > 0:
            truncated[f"…+{remaining}"] = "…"
        return truncated
    if isinstance(value, (list, tuple)):
        items = list(value)
        show = max_items or len(items)
        truncated = [_truncate(item, max_str_len, max_items, max_depth, next_depth)
                     for item in items[:show]]
        remaining = len(items) - show
        if remaining > 0:
            truncated.append(f"…+{remaining}")
        return type(value)(truncated) if isinstance(value, tuple) else truncated
    if isinstance(value, (set, frozenset)):
        items = list(value)
        show = max_items or len(items)
        truncated = {_truncate(item, max_str_len, max_items, max_depth, next_depth)
                     for item in items[:show]}
        remaining = len(items) - show
        if remaining > 0:
            truncated.add(f"…+{remaining}")
        return truncated
    if hasattr(value, 'shape'):
        return _summarize(value)
    return value


def _summarize(value):
    """One-line summary for values too deep or complex to expand."""
    t = type(value).__name__
    if hasattr(value, 'shape'):
        return f"<{t} {value.shape} {getattr(value, 'dtype', '?')}>"
    if isinstance(value, dict):
        return f"<dict {len(value)}keys>"
    if isinstance(value, (list, tuple)):
        return f"<{t} len={len(value)}>"
    if isinstance(value, (set, frozenset)):
        return f"<{t} len={len(value)}>"
    if isinstance(value, str):
        return f"<str {len(value)}ch>"
    if isinstance(value, bytes):
        return f"<bytes {len(value)}b>"
    return f"<{t}>"


# ── GLFW management ───────────────────────────────────────

_needs_render = threading.Event()


def request_render():
    from src.lsd.gl_gui.melty import Melty
    if Toggles.invalidate_stack_trace:
        if Melty.frame_count > 0 and Melty.frame_count % 10 == 0:
            print_stack_trace(size=5)
    _needs_render.set()
    glfw.post_empty_event()