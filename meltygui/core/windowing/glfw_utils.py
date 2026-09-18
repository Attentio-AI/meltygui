import os
import sys
import re
import io
import shutil
import subprocess
import pprint
import inspect
import threading
import traceback
import json
import linecache
import time
from contextlib import contextmanager
from pathlib import Path

import meltygui.core.windowing.window_api as glfw

from meltygui.core.runtime.toggles import Toggles
from meltygui.core.rendering.core_decoration import Core

# ── Module roots for user code detection ─────────────────
_MODULE_ROOTS = ["src/lsd/"]

# Sane bounds on the UI scale (Toggles.UIScale). The scale multiplies font
# atlas sizes, so a stray number from a live edit is an expensive error - a
# huge factor bakes a giant atlas, a tiny one rasterizes unreadable fonts.
# A value outside these bounds is treated as an accident, NOT an intent: the
# guard falls back to 1.0 rather than pinning the UI at an extreme.
UI_SCALE_MIN = 0.5
UI_SCALE_MAX = 3.0


def clamp_ui_scale(value) -> float:
    """Sanitize a candidate ui scale: a float within
    [UI_SCALE_MIN, UI_SCALE_MAX] passes through; anything crazy — out of
    bounds, None, 0, NaN, non-numeric — is interpreted as 1.0 (a wild value
    is a live-edit artifact or typo, and rebuilding the font atlas at 20x
    would only amplify the accident). The single guard every ui-scale
    consumer goes through."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    if v != v or v < UI_SCALE_MIN or v > UI_SCALE_MAX:
        return 1.0
    return v


# ── Native cursor size and theme ───────────────────────────

# Full path on purpose: a bare name makes subprocess fall back to fork() of
# the CUDA/GL/torch address space (see claude_terminals) - posix_spawn needs
# an absolute path and close_fds=False.
_GSETTINGS = shutil.which("gsettings") or "/usr/bin/gsettings"
_DESKTOP_INTERFACE_SCHEMA = "org.gnome.desktop.interface"


def _gsettings_get(key):
    """`gsettings get org.gnome.desktop.interface <key>` as its raw stdout
    (stripped), None if the tool is missing, fails or times out."""
    if not os.path.exists(_GSETTINGS):
        return None
    try:
        result = subprocess.run([_GSETTINGS, "get", _DESKTOP_INTERFACE_SCHEMA, key],
                                capture_output=True, text=True, timeout=3,
                                close_fds=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def export_desktop_cursor_env():
    """Make GLFW's native cursors the DESKTOP's size and theme.

    On Wayland GLFW sizes every cursor it shows — the arrow it pushes on
    pointer-enter as much as the shapes gl_gui/mouse_cursor.py sets — from
    XCURSOR_SIZE / XCURSOR_THEME, read ONCE by the first glfw.init() in the
    process (wl_init.c loadCursorTheme: 16 px when unset). GNOME Wayland
    sessions export neither, so the studio's cursors came out at the theme
    image nearest 16 px (22 px Bibata) while the desktop draws 32. This reads
    GNOME's own settings and exports them; anything already in the
    environment wins (a user's export is an intent). Must run before the
    FIRST glfw.init() of the process: GLFW init is process-wide, the
    launcher's init_gui does it and the studio's later init is a no-op.
    Returns {var: value} of what it exported (empty = nothing to do).
    """
    exported = {}
    if not os.environ.get("WAYLAND_DISPLAY"):
        return exported  # X11: GLFW asks Xcursor, which GNOME configures itself
    if not os.environ.get("XCURSOR_SIZE"):
        raw = _gsettings_get("cursor-size")
        try:
            size = int(raw) if raw is not None else 0
        except ValueError:
            size = 0
        if size > 0:
            os.environ["XCURSOR_SIZE"] = str(size)
            exported["XCURSOR_SIZE"] = str(size)
    if not os.environ.get("XCURSOR_THEME"):
        raw = _gsettings_get("cursor-theme")
        theme = raw.strip("'\"") if raw else ""
        if theme:
            os.environ["XCURSOR_THEME"] = theme
            exported["XCURSOR_THEME"] = theme
    return exported


def apply_wayland_frame_hint():
    """Toggles.Melty.wayland_native_frame → tell GLFW to skip libdecor.

    GNOME has no server-side decorations, so on native Wayland GLFW gives
    the window to libdecor, whose cairo plugin repaints the title bar and
    shadow on the CPU for every resize configure (tens of ms a step at the
    studio's size — the 2 fps OS-window resize). The WAYLAND_DISABLE_LIBDECOR
    init hint makes GLFW draw its own fallback frame instead (caption strip
    + borders, compositor-driven move/resize, <1 ms a step, no buttons —
    titlebar.py draws those). Init hints only count before the FIRST
    glfw.init() of the process, the launcher's, so this runs beside
    export_desktop_cursor_env at both init sites; the outcome is recorded
    process-wide (sys._lsd_wayland_libdecor_disabled) so
    titlebar.backend_supported reads what the process actually got, not the
    live toggle. Returns True when the hint was applied by this call."""
    if getattr(sys, "_lsd_wayland_libdecor_disabled", None) is not None:
        return False  # decided at the first init - later inits are no-ops
    applied = False
    if os.environ.get("WAYLAND_DISPLAY") and Toggles.Melty.wayland_native_frame:
        try:
            glfw.init_hint(glfw.WAYLAND_LIBDECOR, glfw.WAYLAND_DISABLE_LIBDECOR)
            applied = True
        except AttributeError:
            pass  # pre-3.4 pyglfw: no hint, libdecor stays
    sys._lsd_wayland_libdecor_disabled = applied
    return applied


def wayland_native_frame_active():
    """True when this process's GLFW runs Wayland windows without libdecor
    (apply_wayland_frame_hint took effect at the first init)."""
    return bool(getattr(sys, "_lsd_wayland_libdecor_disabled", False))


def _is_user_code(filepath):
    """Check if a file is inside the user's module."""
    rel = _rel_path(filepath)
    return any(root in rel for root in _MODULE_ROOTS)


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
    _value_lexer = PythonLexer(stripnl=True, stripall=True, ensurenl=False)
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
_BG_DARK = "\033[48;2;22;22;22m"
_NO_BG = "\033[49m"
_RED_FG = "\033[38;2;255;85;85m"
_TABLE_LINE = "\033[38;2;60;60;60m"
_TABLE_LINE_RED = "\033[38;2;100;40;40m"

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


# ── Caller location ─────────────────────────────────────

def _find_caller():
    """
    Walk the stack to find where print_stack_trace was called.
    Returns (rel_path, lineno) or None.
    """
    my_file = os.path.abspath(__file__)
    for info in inspect.stack():
        filename = info[1]
        funcname = info[3]
        lineno = info[2]
        if os.path.abspath(filename) == my_file:
            continue
        if funcname in ('__exit__', 'flush', 'write_section', 'write_header',
                        'write_footer', '_task'):
            continue
        return (_rel_path(filename), lineno)
    return None


def _caller_link():
    """Build a clickable 'edit watches' link to the call site."""
    caller = _find_caller()
    if not caller:
        return ""
    return f"  {_DIM}watches \u2192 File \"{_BLUE}{caller[0]}{_DIM}\", line {caller[1]}{_RESET}"


# ── Function argument extraction ─────────────────────────

def _get_func_args(filename, lineno, funcname, local_vars):
    """
    Get function argument names from the source.
    Searches backward from current line for the def statement
    and parses argument names from the signature.
    """
    try:
        lines = linecache.getlines(filename)
        for j in range(min(lineno - 1, len(lines) - 1), max(lineno - 50, -1), -1):
            line = lines[j].strip()
            if line.startswith(f"def {funcname}(") or line.startswith(f"def {funcname} ("):
                # Gather full signature if it spans multiple lines
                sig = line
                k = j + 1
                while ')' not in sig and k < len(lines):
                    sig += " " + lines[k].strip()
                    k += 1
                # Parse arg names from signature
                match = re.search(r'def\s+\w+\s*\(([^)]*)\)', sig)
                if match:
                    params = match.group(1)
                    args = []
                    for param in params.split(','):
                        param = param.strip()
                        if not param or param == '/':
                            continue
                        name = re.match(r'\*{0,2}\s*(\w+)', param)
                        if name:
                            n = name.group(1)
                            if n not in ('self', 'cls') and n in local_vars:
                                args.append(n)
                    return args
    except Exception:
        pass
    return []


# ── Syntax highlighting ──────────────────────────────────

def _highlight(code, lineno=None):
    """Syntax-highlight a line of Python with editor-style background and line number."""
    if _pygments_available:
        colored = _pyg_highlight(code, _python_lexer, _terminal_formatter).rstrip('\n')
        colored = _color_kwargs(colored, code, bg=_NO_BG)
    else:
        colored = f"{_YELLOW}{code}{_RESET}"

    if lineno is not None:
        gutter = f"{_NO_BG}{_DIM} {lineno:>4} {_RESET}"
    else:
        gutter = ""

    visible = len(code)
    pad_width = max(80 - visible, 4)
    return f"{gutter}{_NO_BG} {colored}{' ' * pad_width}{_RESET}"


def _highlight_inline(code):
    """Syntax-highlight a short code snippet without background or gutter."""
    if not _pygments_available:
        return f"{_GREEN}{code}{_RESET}"
    result = _pyg_highlight(code, _python_lexer, _terminal_formatter).rstrip('\n')
    return _color_kwargs(result, code)


def _color_kwargs(highlighted, original, bg=None):
    """Post-process to color keyword argument names red."""
    restore = f"{_RESET}{bg}" if bg else _RESET
    for match in re.finditer(r'(\b\w+)(?=\s*=[^=])', original):
        name = match.group(1)
        if name in ('if', 'else', 'elif', 'return', 'yield', 'not',
                    'and', 'or', 'in', 'is', 'lambda', 'True', 'False', 'None'):
            continue
        highlighted = re.sub(
            rf'(?<!\033\[38;2;255;85;85m)(\033\[[\d;]*m)*({re.escape(name)})(\033\[[\d;]*m)*(?=\s*=[^=])',
            rf'\1{_RED_FG}{name}{restore}\3',
            highlighted,
            count=1
        )
    return highlighted


# ── Rich table rendering ─────────────────────────────────

def _rich():
    """(Table, Text, Console) from rich, or None. Imported on first use: rich
    costs ~10 ms and only the watch-table printer needs it."""
    try:
        from rich.table import Table
        from rich.text import Text
        from rich.console import Console
    except ImportError:
        return None
    return Table, Text, Console


def _render_watch_table(file_line, code_line, watch_rows, error=False):
    """
    Render a frame with watches using rich table.
    Only called when watch_rows is non-empty.
    """
    rich = _rich()
    if rich is None:
        return _render_frame_simple(file_line, code_line, watch_rows)
    RichTable, RichText, RichConsole = rich

    border_style = "rgb(100,40,40)" if error else "rgb(50,50,55)"

    if error:
        row_a = "on rgb(45,30,30)"
        row_b = "on rgb(50,35,35)"
        name_col = "on rgb(55,35,35)"
    else:
        row_a = "on rgb(19,19,19)"
        row_b = "on rgb(23,23,23)"
        name_col = "on rgb(28,28,28)"

    table = RichTable(
        show_header=False,
        show_edge=False,
        show_lines=False,
        border_style=border_style,
        pad_edge=True,
        padding=(0, 1),
        expand=False,
        row_styles=[row_a, row_b],
    )

    table.add_column(overflow="ellipsis", max_width=40, no_wrap=True, style=name_col)
    table.add_column(overflow="ellipsis", max_width=12, no_wrap=False)
    table.add_column(overflow="ellipsis", max_width=60, no_wrap=False)
    table.add_column(overflow="ellipsis", max_width=120, no_wrap=True)

    for name, typ, val, link in watch_rows:
        table.add_row(
            RichText.from_ansi(name),
            RichText.from_ansi(typ),
            RichText.from_ansi(val),
            RichText.from_ansi(link),
        )

    table_buf = io.StringIO()
    console = RichConsole(
        file=table_buf, highlight=False, markup=False,
        width=200, force_terminal=True
    )
    console.print(table, end="")
    watch_block = table_buf.getvalue()

    buf = []
    buf.append(f"  {file_line}")
    if code_line:
        buf.append(f"    {code_line}")
    for line in watch_block.rstrip('\n').split('\n'):
        buf.append(f"      {line}")

    return "\n".join(buf) + "\n"


def _render_frame_simple(file_line, code_line, watch_rows):
    """Fallback renderer without rich."""
    buf = []
    buf.append(f"  {file_line}")
    if watch_rows:
        for name, typ, val, link in watch_rows:
            buf.append(f"      {name}  {typ}  {val}  {link}")
    if code_line:
        buf.append(f"    {code_line}")
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
        code = "\u2500"

        if text:
            pad = size - len(text) - 4
            return f"{self.color}{_BLACK}{_BOLD} \u258c {text} {code * max(pad, 0)} {_RESET}"
        return f"{self.color}{_BLACK}{_BOLD} {code * size} {_RESET}"

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


# ── Watch resolution helper ──────────────────────────────

def _resolve_watch(expr, filename, lineno, local_vars,
                   max_str_len, max_items, max_depth, max_output):
    """
    Resolve a single watch expression into a table row tuple,
    or return None if the expression can't be resolved.
    """
    funcs, path = _parse_watch(expr)
    root = _get_root_name(path)
    if root not in local_vars:
        return None
    success, value = _resolve_path(path, local_vars)
    if not success:
        return None
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
        formatted_value = formatted_value[:cut] + f"\u2026({len(formatted_value)}ch)"

    if _pygments_available:
        formatted_value = _pyg_highlight(formatted_value, _python_lexer, _terminal_formatter).rstrip('\n')

    name_cell = _highlight_inline(expr)
    type_cell = f"{_DIM}{type(value).__name__}{_RESET}"
    value_cell = f"{_MAGENTA}{formatted_value}{_RESET}"
    def_line = _find_assignment(filename, lineno, root)
    link_cell = _make_link(filename, def_line)

    return (name_cell, type_cell, value_cell, link_cell)


# ── Main entry point ─────────────────────────────────────

stacks_printed_this_frame = 0
this_frame_number = 0


def print_stack_trace(size=None, skip=0, stack=None, frames=None, watch=None,
                      max_str_len=200, max_items=5, max_depth=2, max_output=200,
                      exception=None, e=None, section=None, group=None, file=None,
                      print_args=True,
                      ignore_functions=("wrapper")):
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
        print_args:  Auto-add function arguments to watches (default True).
        ignore_functions: Frame function names to drop from the trace (repetitive
                     plumbing). The error frame is never dropped, even if its name
                     matches. Pass None/[] to keep all frames.
    """
    global stacks_printed_this_frame
    global this_frame_number
    if e is not None and exception is None:
        exception = e

    # Every printed stack trace marks the session as crashed - the launcher
    # reads this sentinel at backup time and logs the log entry. On sys
    # (not a module global) because this module is recompiled per run while
    # the launcher reads from its own import identity. First error remains
    # (root cause); count keeps ticking. Stamped BEFORE the per-frame rate
    # limit so suppressed traces still register.
    try:
        rec = getattr(sys, '_lsd_session_crash', None)
        if rec is None:
            rec = {"error": None, "count": 0}
            sys._lsd_session_crash = rec
        rec["count"] += 1
        if rec["error"] is None:
            if exception is not None:
                rec["error"] = f"{type(exception).__name__}: {exception}"
            else:
                rec["error"] = f"trace from {sys._getframe(1).f_code.co_name}()"
    except Exception:
        pass

    if this_frame_number != Core.melty.frame_count:
        this_frame_number = Core.melty.frame_count
        stacks_printed_this_frame = 0

    if stacks_printed_this_frame > 2:
        return

    stacks_printed_this_frame += 1

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

    watch_paths = list(watch) if watch else []
    watch_paths.append("watch")

    if group and section:
        link = _caller_link()
        group.write_section(f"{section}{link}")
    elif not group:
        thread_name = threading.current_thread().name
        bar_color = _CYAN
        title = "Exception Trace" if exception else "Stack Trace"
        link = _caller_link()
        color_a = '\u2500'
        buf.write(f"{_BOLD}{bar_color}{color_a * 60}{_RESET}\n")
        buf.write(f"{_BOLD}{bar_color}{title}{_RESET} {_DIM}[{thread_name}]{_RESET}{link}\n")
        buf.write(f"{_BOLD}{bar_color}{color_a * 60}{_RESET}\n")

    # Find the last user-code frame in an exception trace
    error_frame_idx = None
    if exception is not None:
        for j in range(len(frames) - 1, -1, -1):
            if _is_user_code(frames[j][0]):
                error_frame_idx = j
                break

    # The full frame list (before the plumbing filter) goes into the saved
    # report, so the Crash Reports window can open it as a stack trace view
    # and with each user-code frame's locals as display strings, the values
    # the view's live-value markers show (the same truncation as the watch
    # table; live objects can't be saved).
    # Gated like the Context menu's Code-tab capture: locals of any PROJECT
    # frame (tests included), never library code.
    from meltygui.code.fileref import is_editable_source
    report_frames = []
    for frame in frames or ():
        scope = None
        if frame[4] is not None and is_editable_source(frame[0]):
            scope = _snapshot_locals(frame[4], max_str_len, max_items, max_depth, max_output)
        report_frames.append((frame[0], frame[1], frame[2], scope))

    # Drop repetitive plumbing frames, but never the error frame itself.
    if ignore_functions:
        error_frame = frames[error_frame_idx] if error_frame_idx is not None else None
        near_error = False
        error_frame_idx = (
            next((k for k, frame in enumerate(frames) if frame is error_frame), None)
            if error_frame is not None else None
        )

        if error_frame_idx is None:
            error_frame_idx = len(frames) - 1

        frames = [
            frame for k, frame in enumerate(frames)
            if (k == error_frame_idx) or abs(k - error_frame_idx) < 5 or frame[2] not in ignore_functions
        ]

    if frames:
        for i, (filename, lineno, funcname, line_text, local_vars) in enumerate(frames):
            rel = filename
            is_mine = _is_user_code(filename)
            is_error_frame = i == error_frame_idx

            if is_mine:
                if is_error_frame:
                    file_line = (
                        f"{_RED}File \"{rel}\", line {lineno},"
                        f" in {_BOLD}{funcname}{_RESET}"
                    )
                else:
                    file_line = (
                        f"{_DIM}File \"{rel}\", line {lineno},"
                        f" in {_RESET}{_BOLD}{_WHITE}{funcname}{_RESET}"
                    )
            else:
                file_line = (
                    f"{_DIM}File \"{rel}\", line {lineno},"
                    f" in {funcname}{_RESET}"
                )

            code_line = ""
            if line_text:
                if is_mine:
                    code_line = _highlight(line_text.strip(), None)
                else:
                    code_line = f"{_DIM}{line_text.strip()}{_RESET}"

            # Resolve watch
            table_rows = []
            if is_mine and local_vars is not None:
                # Build effective watch list: auto args + explicit watches
                effective_watches = []
                if print_args:
                    func_args = _get_func_args(filename, lineno, funcname, local_vars)
                    existing_roots = {_get_root_name(_parse_watch(w)[1]) for w in watch_paths}
                    for arg in func_args:
                        if arg not in existing_roots:
                            effective_watches.append(arg)
                effective_watches.extend(watch_paths)

                for expr in effective_watches:
                    # A watch resolves against live objects (truncate, repr,
                    # pformat) and can throw on any of them - one bad value
                    # must cost its row, not the whole trace.
                    try:
                        row = _resolve_watch(expr, filename, lineno, local_vars,
                                             max_str_len, max_items, max_depth, max_output)
                    except Exception as watch_err:
                        row = (_highlight_inline(expr), f"{_DIM}?{_RESET}",
                               f"{_RED}<unprintable {type(watch_err).__name__}: {watch_err}>{_RESET}",
                               _make_link(filename, lineno))
                    if row is not None:
                        table_rows.append(row)

            if table_rows:
                try:
                    buf.write(_render_watch_table(file_line, code_line, table_rows,
                                                  error=is_error_frame))
                except Exception:
                    buf.write(_render_frame_simple(file_line, code_line, table_rows))
            else:
                buf.write(f"  {file_line}\n")
                if code_line:
                    buf.write(f"    {code_line}\n")

    # Compilation errors (SyntaxError and friends) store the real error location
    # on the exception itself, rather in the traceback frames - append it.
    if isinstance(exception, SyntaxError) and exception.filename and exception.lineno:
        err_file = exception.filename
        err_line = exception.lineno
        text = exception.text
        if text is None:
            text = linecache.getline(err_file, err_line)
        is_mine = _is_user_code(err_file)
        file_line = (
            f"{_RED}File \"{err_file}\", line {err_line}{_RESET}"
            if is_mine else
            f"{_DIM}File \"{err_file}\", line {err_line}{_RESET}"
        )
        buf.write(f"  {file_line}\n")
        if text:
            stripped = text.rstrip("\n")
            code_line = (
                _highlight(stripped.strip(), None) if is_mine
                else f"{_DIM}{stripped.strip()}{_RESET}"
            )
            buf.write(f"    {code_line}\n")
            # Caret pointing at the error column, mirroring Python's own format.
            if exception.offset:
                indent = len(stripped) - len(stripped.lstrip())
                caret_col = max(exception.offset - 1 - indent, 0)
                buf.write(f"    {' ' * caret_col}{_RED}^{_RESET}\n")

    if exception is not None:
        buf.write(f"  {_RED}{_BOLD}{type(exception).__name__}: {exception}{_RESET}\n")

    if not group:
        bar_color = _CYAN
        code = '\u2500'
        buf.write(f"{_BOLD}{bar_color}{code * 60}{_RESET}\n")

    _dispatch(buf, group, file)

    # A grouped trace is one section of the group's own output and the group
    # flushes as a whole, so only standalone traces become report files.
    if not group:
        # A plain trace (no exception) is titled by the function it was
        # printed from, its innermost frame - not just "stack trace".
        plain_label = (f"stack trace from {report_frames[-1][2]}()"
                       if exception is None and report_frames else None)
        save_crash_report(buf.getvalue(), exception=exception, frames=report_frames,
                          error=plain_label)

    if stacks_printed_this_frame > 2:
        RED_BOLD = "\033[1m\033[31m"
        print(f"{RED_BOLD} Not printing [{stacks_printed_this_frame}] stacks {_RESET}")
        return


# ── Crash report files ───────────────────────────────────

# A saved frame keeps at most this many locals (the first bound win).
# [tint=(0.994, 0.872, 0.0)]
REPORT_LOCALS_PER_FRAME = 80


def _snapshot_locals(local_vars, max_str_len, max_items, max_depth, max_output):
    """`{name: display string}` of a frame's locals for the saved report —
    dunder names and modules dropped, every value rendered through the
    watch table's `_truncate` + pformat, one bad value costing only its
    entry (repr can raise on anything)."""
    import types as _types
    snapshot = {}
    for name, value in list(local_vars.items())[:REPORT_LOCALS_PER_FRAME]:
        if name.startswith("__") or isinstance(value, _types.ModuleType):
            continue
        try:
            text = pprint.pformat(_truncate(value, max_str_len, max_items, max_depth), width=60)
        except Exception as exc:
            text = f"<unprintable {type(exc).__name__}>"
        if max_output and len(text) > max_output:
            text = text[:max_output] + f"\u2026({len(text)}ch)"
        snapshot[name] = text
    return snapshot

def crash_reports_dir():
    """Where print_stack_trace writes its reports (Toggles.CrashReports.directory)."""
    return Path(os.path.expanduser(Toggles.CrashReports.directory))


# The last millisecond stamp handed out and how many reports shared it -
# every name carries a 3-digit sequence within its millisecond so names stay
# unique AND sort in write order whatever the error label (probing the disk
# instead let a pruned base name be reused and sort as the oldest report).
_last_report_stamp = [None, 0]


def _report_stem(exception):
    """`2026-09-04_14-03-22-517-000_ZeroDivisionError` — sorts by time as
    text, and the error name makes the file list readable on its own."""
    now = time.time()
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now))
    stamp = f"{stamp}-{int((now - int(now)) * 1000):03d}"
    label = type(exception).__name__ if exception is not None else "trace"
    label = re.sub(r"[^A-Za-z0-9_]+", "_", label)[:60]
    with _print_lock:
        if _last_report_stamp[0] == stamp:
            _last_report_stamp[1] += 1
        else:
            _last_report_stamp[0], _last_report_stamp[1] = stamp, 0
        sequence = _last_report_stamp[1]
    return f"{stamp}-{sequence:03d}_{label}"


def git_head_commit(root=None):
    """`(sha, branch)` of the checkout at `root` (the project root), read
    straight off `.git` — HEAD → its ref → loose ref file or packed-refs —
    with no subprocess (a git call per crash would be a posix_spawn on the
    render thread for a value that changes once per commit). (None, None)
    when there is no git checkout; memoized on HEAD's and the ref file's
    mtimes so a commit or checkout is seen on the next report."""
    from meltygui.core.runtime.paths import application_root
    root = Path(root) if root is not None else application_root()
    try:
        git_dir = root / ".git"
        if git_dir.is_file():                     # a worktree: `gitdir: <path>`
            pointer = git_dir.read_text().strip()
            if pointer.startswith("gitdir:"):
                git_dir = Path(pointer.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = root / git_dir
        head_path = git_dir / "HEAD"
        head = head_path.read_text().strip()
        if not head.startswith("ref:"):
            return head[:40], None                # detached HEAD
        ref = head.split(":", 1)[1].strip()
        branch = ref.rsplit("/", 1)[-1]
        ref_path = git_dir / ref
        if ref_path.is_file():
            return ref_path.read_text().strip()[:40], branch
        # common dir for worktrees: refs live in the main repo's .git
        common = git_dir / "commondir"
        if common.is_file():
            main_dir = (git_dir / common.read_text().strip()).resolve()
            candidate = main_dir / ref
            if candidate.is_file():
                return candidate.read_text().strip()[:40], branch
            git_dir = main_dir
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:40], branch
    except OSError:
        pass
    return None, None


def save_crash_report(text, exception=None, thread_name=None, frames=None, error=None):
    """Write one printed trace as an ANSI-stripped text file under
    crash_reports_dir() and return its path (None when saving is off or
    the write failed — a crash report must never raise into the trace
    that produced it). The first lines are a `key: value` header the
    Crash Reports window reads without loading the whole file — `commit`
    the checkout's HEAD sha and branch (git_head_commit), `frames`
    is the trace's (path, lineno, function) list as JSON, outermost first,
    which the window hands to draw_stack_trace, and `locals` the matching
    list of per-frame {name: display string} snapshots (null for frames
    without one) the view shows as values; the printed trace follows after
    a blank line. `frames` entries may carry the snapshot as a 4th item.
    Oldest files past Toggles.CrashReports.max_reports are deleted."""
    if not Toggles.CrashReports.auto_save:
        return None
    try:
        directory = crash_reports_dir()
        directory.mkdir(parents=True, exist_ok=True)
        thread_name = thread_name or threading.current_thread().name
        if error is None:
            error = (f"{type(exception).__name__}: {exception}" if exception is not None
                     else "stack trace")
        header = (f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"thread: {thread_name}\n"
                  f"error: {error.splitlines()[0] if error else ''}\n")
        commit, branch = git_head_commit()
        if commit:
            header += f"commit: {commit}{(' ' + branch) if branch else ''}\n"
        if frames:
            frame_rows = [[str(frame[0]), int(frame[1]), str(frame[2])] for frame in frames]
            header += f"frames: {json.dumps(frame_rows)}\n"
            scopes = [frame[3] if len(frame) > 3 and isinstance(frame[3], dict) else None
                      for frame in frames]
            if any(scope for scope in scopes):
                header += f"locals: {json.dumps(scopes, ensure_ascii=False)}\n"
        header += "\n"
        path = directory / f"{_report_stem(exception)}.txt"
        path.write_text(header + _ANSI_RE.sub("", text), encoding="utf-8")
        _prune_crash_reports(directory)
        try:
            from meltygui.model.trace_report_model import reports_changed
            reports_changed()
        except Exception:
            pass                                  # window's not loaded yet: nothing to repaint
        return path
    except Exception:
        return None


def _prune_crash_reports(directory):
    limit = Toggles.CrashReports.max_reports
    if not limit or limit <= 0:
        return
    files = sorted(directory.glob("*.txt"))       # names sort by time
    for stale in files[:max(0, len(files) - limit)]:
        try:
            stale.unlink()
        except OSError:
            pass


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
            return value[:max_str_len] + f"\u2026({len(value)}ch)"
        return value
    if isinstance(value, bytes):
        if max_str_len and len(value) > max_str_len:
            return value[:max_str_len] + f"\u2026({len(value)}b)".encode()
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
            truncated[f"\u2026+{remaining}"] = "\u2026"
        return truncated
    if isinstance(value, (list, tuple)):
        items = list(value)
        show = max_items or len(items)
        truncated = [_truncate(item, max_str_len, max_items, max_depth, next_depth)
                     for item in items[:show]]
        remaining = len(items) - show
        if remaining > 0:
            truncated.append(f"\u2026+{remaining}")
        if not isinstance(value, tuple):
            return truncated
        try:
            # namedtuples take positional fields, not an iterable \u2014 and a
            # truncated one no longer has the right arity at all
            if hasattr(value, '_fields'):
                if len(truncated) == len(value):
                    return type(value)(*truncated)
                return tuple(truncated)
            return type(value)(truncated)
        except Exception:
            return tuple(truncated)
    if isinstance(value, (set, frozenset)):
        items = list(value)
        show = max_items or len(items)
        truncated = {_truncate(item, max_str_len, max_items, max_depth, next_depth)
                     for item in items[:show]}
        remaining = len(items) - show
        if remaining > 0:
            truncated.add(f"\u2026+{remaining}")
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
frames_left = 0

# Which OS windows a request is for (surface.py draws only those; the studio,
# with no Surface, never reads this). A request made on the render thread while
# a Surface's frame or GLFW callback runs (render_scope) is that surface's. Any
# other one - a worker thread, the app loop between frames, a view reporting
# `changed` (note_shared_change) - cannot be attributed and is every surface's:
# each surface compares the generation it last drew at.
render_scope = None
_render_thread_id = threading.main_thread().ident
_open_surfaces = set()
_requested_surfaces = set()
_all_surfaces_generation = 0


def register_surface(surface):
    _open_surfaces.add(surface)


def forget_surface(surface):
    _open_surfaces.discard(surface)
    _requested_surfaces.discard(surface)


def note_shared_change():
    """A view reported `changed`: a value any OS window may show was edited.
    Every surface draws a frame, the other windows' included (hence the wake:
    the edit's own request, if any, was only the editing window's)."""
    global _all_surfaces_generation
    _all_surfaces_generation += 1
    if len(_open_surfaces) > 1:
        _wake_render_loop()


def request_surface_render(surface):
    """A frame of `surface`, whichever window's frame or callback is running:
    a parent handing its child window a new value, a child's result waiting
    for its parent."""
    global render_scope
    if threading.get_ident() != _render_thread_id:
        request_render()
        return
    interrupted_scope, render_scope = render_scope, surface
    try:
        request_render()
    finally:
        render_scope = interrupted_scope


def take_surface_request(surface, drawn_generation):
    """Consume what was requested of `surface` since it last asked: returns
    (requested, generation) - the generation is what the surface passes back
    next time. Render thread only."""
    requested = surface in _requested_surfaces
    _requested_surfaces.discard(surface)
    generation = _all_surfaces_generation
    return requested or generation != drawn_generation, generation


def _wake_render_loop():
    _needs_render.set()
    try:
        glfw.post_empty_event()
    except Exception:
        pass  # glfw torn down mid-call (shutdown/restart) - nothing to wake


# [tint=(0.191, 0.328, 0.191), show_tint=True]
def request_render(for_frames: int | None = None):
    # Can be called from ANY thread - including worker threads (PTY readers, or
    # claude-session poller) that start at import, before glfw.init() and the main
    # window exist. The glfw.get_current_context() guard below itself calls INTO glfw,
    # which raises GLFWError "The GLFW library is not initialized" when called before
    # init - i.e. the guard check is what produces the error. So gate on the GLFW
    # window FIRST, a pure-Python object-attr check (None until create_window), no glfw
    # call. Lazy import because Meltygui imports this module (circular at top level); meltygui
    # is fully loaded by the time any thread calls request_render at start.
    from meltygui.core.melty import Melty
    if Melty.glfw_window is None:
        return
    # NOTE: no glfw.get_current_context() readiness check here - it returns the
    # context current on the CALLING thread, which is None on every worker
    # thread, so it silently dropped exactly the cross-thread wakes this
    # function exists for (an idle loop blocks in glfw.wait_events; a
    # task completion must post_empty_event to produce a frame). The
    # window-exists gate above covers pre-init; the try/except at the bottom
    # covers mid-shutdown teardown.

    global frames_left
    if frames_left > 0:
        frames_left -= 1

    if for_frames is not None:
        frames_left = for_frames

    # "request_render" notify function: every call toasts its caller's stack
    # (click → open the call site in the editor), gated like the invalidate
    # column on InvalidateTracker.enable (E hotkey). notify() collapses
    # repeats of one call site into one counted entry, and is never urgent here
    # - an urgent notify would call back into request_render.
    if (Toggles.InvalidateTracker.enable or Toggles.InvalidateTracker.invalidate_request_render
            or Toggles.InvalidateTracker.invalidate_stack_trace):
        from meltygui.core.diagnostics.notifications import notify
        from meltygui.core.diagnostics.notifications import capture_stack
        stack = capture_stack(skip_files=("glfw_utils.py",), skip_funcs=("request_render",))
        if stack:
            fn = stack[-1][2]
            notify(f"request_render  [{fn}]  {threading.current_thread().name}",
                   tint=(0.4, 0.8, 1.0), tag="request_render", stack=stack, urgent=False)

    global _all_surfaces_generation
    scope = render_scope
    if scope is not None and threading.get_ident() == _render_thread_id:
        _requested_surfaces.add(scope)
    else:
        _all_surfaces_generation += 1
    _wake_render_loop()