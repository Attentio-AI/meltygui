import os
import sys
import threading
import traceback

import glfw

import traceback
import sys
import pprint
import inspect
import re

import io
import sys
import traceback
import sys
import pprint
import inspect
import threading
import io
import os
import re
from contextlib import contextmanager

from src.lsd.gl_gui.toggles import Toggles

# ANSI escape codes
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

_BG_COLORS = [
    "\033[41m",  # red bg
    "\033[42m",  # green bg
    "\033[43m",  # yellow bg
    "\033[44m",  # blue bg
    "\033[45m",  # magenta bg
    "\033[46m",  # cyan bg
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


def _next_job_color():
    global _job_counter
    with _job_counter_lock:
        color = _BG_COLORS[_job_counter % len(_BG_COLORS)]
        _job_counter += 1
    return color


class TraceGroup:
    """Buffers multiple print_stack_trace calls and flushes atomically."""

    bar_size_outer = 58
    bar_size_inner = 30
    def __init__(self, label, color, **meta):
        self.buf = io.StringIO()
        self.label = label
        self.color = color
        self.meta = meta

    def _bar(self, text="", size=bar_size_inner):
        if text:
            pad = size - len(text) - 4
            return f"{self.color}{_BLACK}{_BOLD} ▌ {text} {'─' * max(pad, 0)} {_RESET}"
        return f"{self.color}{_BLACK}{_BOLD} {'─' * size} {_RESET}"

    def write_header(self):
        meta = ""
        if self.meta:
            meta = "  " + "  ".join(f"{k}={v}" for k, v in self.meta.items())
        self.buf.write(f"\n{self._bar(self.label, size=TraceGroup.bar_size_outer)}\n")
        if meta:
            self.buf.write(f"{self.color} {_RESET}{_DIM}{meta}{_RESET}\n")

    def write_section(self, name):
        self.buf.write(f"{self._bar(name)}\n")

    def write_footer(self):
        self.buf.write(f"{self._bar(size=TraceGroup.bar_size_outer)}\n\n")

    def flush(self, dest=None):
        dest = dest or sys.stdout
        with _print_lock:
            dest.write(self.buf.getvalue())
            dest.flush()


@contextmanager
def trace_group(label, **meta):
    """
    Context manager that groups multiple print_stack_trace calls visually.

    Usage:
        with trace_group("JOB user_42", hash=h) as g:
            print_stack_trace(frames=frames, section="Caller", group=g, watch=[...])
            print_stack_trace(exception=e, section="Exception", group=g, watch=[...])
    """
    g = TraceGroup(label, _next_job_color(), **meta)
    g.write_header()
    try:
        yield g
    finally:
        g.write_footer()
        g.flush()


def _resolve_func(name):
    """
    Resolve a function name to a callable.

    Tries in order:
      1. Python builtins (str, len, type, etc.)
      2. Dotted module path (json.dumps, os.path.basename, etc.)
      3. Already-imported modules in sys.modules
    """
    import builtins

    # 1. Builtin
    if hasattr(builtins, name):
        return getattr(builtins, name)

    # 2. Dotted path - walk from leftmost module
    if "." in name:
        parts = name.split(".")
        # Try progressively longer module paths
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

    # 3. Top-level module with a single function
    # e.g. someone has "myfunc" that's in sys.modules somehow
    for mod in sys.modules.values():
        if mod and hasattr(mod, name):
            return getattr(mod, name)

    return None


def _parse_watch(expr):
    """
    Parse wrapper functions off a watch expression.

    "str(my_dict.item)"                -> (["str"], "my_dict.item")
    "json.dumps(config)"               -> (["json.dumps"], "config")
    "os.path.basename(filepath)"       -> (["os.path.basename"], "filepath")
    "len(sorted(items))"               -> (["len", "sorted"], "items")
    "draw_state.name"                  -> ([], "draw_state.name")
    """
    funcs = []
    while True:
        # Match func_name(...) where func_name can be dotted like json.dumps
        match = re.match(r'^([\w.]+)\((.+)\)$', expr)
        if match:
            func_name = match.group(1)
            resolved = _resolve_func(func_name)
            if resolved is not None:
                funcs.append(func_name)
                expr = match.group(2)
                continue
        break
    return funcs, expr

def print_stack_trace(size=None, skip=-1, stack=None, frames=None, watch=None,
                      max_str_len=120, max_items=2, max_depth=3, max_output=120,
                      exception=None, section=None, group=None, file=None):
    """
    Print a stack trace with optional variable watching.

    Args:
        size:        Max number of frames to show (None = all).
        skip:        How many trailing frames to skip (-1 skips this function).
        stack:       Pre-extracted stack to use instead of the current one.
        frames:      Pre-captured live frames (from get_live_frames).
        watch:       List of variable names or dotted paths to dump.
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

    thread_name = threading.current_thread().name
    bar_color = f"{group.color}{_BLACK}" if group else _CYAN

    if not group:
        # Standalone - write the full banner
        buf.write(f"{_BOLD}{bar_color}{'─' * 60}{_RESET}\n")
        title = "Exception Trace" if exception else "Stack Trace"
        buf.write(f"{_BOLD}{bar_color}{title}{_RESET} {_DIM}[{thread_name}]{_RESET}\n")
        buf.write(f"{_BOLD}{bar_color}{'─' * 60}{_RESET}\n")

    # ... frames loop unchanged ...

    if exception is not None:
        buf.write(f"  {_RED}{_BOLD}{type(exception).__name__}: {exception}{_RESET}\n")

    if not group:
        buf.write(f"{_BOLD}{bar_color}{'─' * 60}{_RESET}\n")

    # Section header if inside a group, otherwise standalone banner


    for i, (filename, lineno, funcname, line_text, local_vars) in enumerate(frames):
        styled_path = _style_path(filename)
        buf.write(
            f"  File \"{styled_path}\", line {_WHITE}{lineno}{_RESET},"
            f" in {_BOLD}{_WHITE}{funcname}{_RESET}\n"
        )
        if line_text:
            buf.write(f"    {_YELLOW}{line_text.strip()}{_RESET}\n")

        if not watch_paths or local_vars is None:
            continue

        found = {}
        for expr in watch_paths:
            funcs, path = _parse_watch(expr)
            root = _get_root_name(path)
            if root in local_vars:
                success, value = _resolve_path(path, local_vars)
                if success:
                    try:
                        for func_name in reversed(funcs):
                            value = _resolve_func(func_name)(value)
                        found[expr] = value
                    except Exception as ex:
                        found[expr] = f"<{func_name}() raised {type(ex).__name__}: {ex}>"


        if not found:
            continue

        for path, value in found.items():
            display_val = _truncate(value, max_str_len, max_items, max_depth)
            formatted_value = pprint.pformat(display_val, width=50)

            if max_output and len(formatted_value) > max_output:
                cut = formatted_value[:max_output].rfind('\n')
                if cut == -1:
                    cut = max_output
                formatted_value = formatted_value[:cut] + f"\n  …truncated ({len(formatted_value)} chars)"

            root = _get_root_name(path)
            def_line = _find_assignment(filename, lineno, root)
            clickable_name = _osc8_link(filename, def_line, f"{path}")
            indent_size = 8
            if "\n" in formatted_value:
                indented = "\n".join(
                    f"{' '* indent_size * 2}{line}" for line in formatted_value.splitlines()
                )
                buf.write(
                    f"{' ' * indent_size}{_GREEN}⯈ {_BOLD}{clickable_name}{_RESET}"
                    f" {_DIM}{type(value).__name__}{_RESET}\n"
                )
                buf.write(f"{_MAGENTA}{indented}{_RESET}\n")
            else:
                buf.write(
                    f"{' ' * indent_size}{_GREEN}⯈ {_BOLD}{clickable_name}{_RESET}"
                    f" {_DIM}{type(value).__name__}{_RESET}"
                    f" = {_MAGENTA}{formatted_value}{_RESET}\n"
                )

    if exception is not None:
        buf.write(f"  {_RED}{_BOLD}{type(exception).__name__}: {exception}{_RESET}\n")

    if not group:
        buf.write(f"{_BOLD}{bar_color}{'─' * 60}{_RESET}\n")


    _dispatch(buf, group, file)


def _dispatch(buf, group=None, file=None):
    """Route output to a trace group's buffer, or flush directly."""
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


# ─── helpers ──────────────────────────────────────────────

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
        lineno = tb.tb_lineno
        filename = frame_obj.f_code.co_filename
        funcname = frame_obj.f_code.co_name
        line_text = ""
        try:
            import linecache
            line_text = linecache.getline(filename, lineno)
        except Exception:
            pass
        results.append((filename, lineno, funcname, line_text, dict(frame_obj.f_locals)))
        tb = tb.tb_next
    return results


def _style_path(filepath):
    """Dim the leading directories, brighten the filename."""
    parts = filepath.replace("\\", "/").rsplit("/", 1)
    if len(parts) == 2:
        return f"{_DIM}{parts[0]}/{_RESET}{_CYAN}{parts[1]}{_RESET}"
    return f"{_CYAN}{filepath}{_RESET}"


def _osc8_link(filepath, line, text):
    """Wrap text in a clickable reference."""
    abs_path = os.path.abspath(filepath)
    line = line or 1

    if _IDE == "traceback":
        return f"{text} {_DIM}(File \"{abs_path}\", line {line}){_RESET}{_GREEN}{_BOLD}"

    if _IDE == "intellij":
        # IntelliJ reliably clicks absolute paths and File "...", line N format
        return f"{text} {_DIM}({abs_path}:{line}){_RESET}{_GREEN}{_BOLD}"

    if _IDE and _IDE in _IDE_SCHEMES:
        uri = _IDE_SCHEMES[_IDE].format(path=abs_path, line=line)
    else:
        uri = f"file://{abs_path}:{line}"

    return f"\033]8;;{uri}\033\\{text}\033]8;;\033\\"


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


def _truncate(value, max_str_len=120, max_items=5, max_depth=3, _current_depth=0):
    """Recursively truncate values for display."""
    if max_depth is not None and _current_depth >= max_depth:
        return _summarize(value)

    next_depth = _current_depth + 1

    if isinstance(value, str):
        if max_str_len and len(value) > max_str_len:
            return value[:max_str_len] + f"…({len(value)} chars)"
        return value

    if isinstance(value, bytes):
        if max_str_len and len(value) > max_str_len:
            return value[:max_str_len] + f"…({len(value)} bytes)".encode()
        return value

    if isinstance(value, dict):
        items = list(value.items())
        truncated = {
            _truncate(k, max_str_len, max_items, max_depth, next_depth):
                _truncate(v, max_str_len, max_items, max_depth, next_depth)
            for k, v in items[:max_items]
        } if max_items else {
            _truncate(k, max_str_len, max_items, max_depth, next_depth):
                _truncate(v, max_str_len, max_items, max_depth, next_depth)
            for k, v in items
        }
        remaining = len(items) - (max_items or len(items))
        if remaining > 0:
            truncated[f"…+{remaining} more"] = "…"
        return truncated

    if isinstance(value, (list, tuple)):
        items = list(value)
        show = max_items or len(items)
        truncated = [_truncate(item, max_str_len, max_items, max_depth, next_depth)
                     for item in items[:show]]
        remaining = len(items) - show
        if remaining > 0:
            truncated.append(f"…+{remaining} more")
        if isinstance(value, tuple):
            return tuple(truncated)
        return truncated

    if isinstance(value, (set, frozenset)):
        items = list(value)
        show = max_items or len(items)
        truncated = {_truncate(item, max_str_len, max_items, max_depth, next_depth)
                     for item in items[:show]}
        remaining = len(items) - show
        if remaining > 0:
            truncated.add(f"…+{remaining} more")
        return truncated

    if hasattr(value, 'shape'):
        return _summarize(value)

    return value


def _summarize(value):
    """One-line summary for values too deep or complex to expand."""
    t = type(value).__name__
    if hasattr(value, 'shape'):
        dtype = getattr(value, 'dtype', '?')
        return f"<{t} shape={value.shape} dtype={dtype}>"
    if isinstance(value, dict):
        return f"<dict {len(value)} keys>"
    if isinstance(value, (list, tuple)):
        return f"<{t} len={len(value)}>"
    if isinstance(value, (set, frozenset)):
        return f"<{t} len={len(value)}>"
    if isinstance(value, str):
        return f"<str len={len(value)}>"
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    return f"<{t}>"


# Token tag to distinguish access types
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

    Dot access  (.name)    -> tries getattr first, then obj[key]
    Bracket access ([key]) -> obj[key] only

    Returns (True, value) on success, (False, None) on any failure.
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
                # Try attribute first, fall back to key lookup
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
    if tokens:
        return tokens[0][1]
    return path

def _flush(buf, file=None):
    """Write the entire buffer atomically under a lock."""
    output = buf.getvalue()
    dest = file or sys.stdout
    with _print_lock:
        dest.write(output)
        dest.flush()


def _shorten_path(filepath, max_parts=3):
    """Show only the last N parts of a file path."""
    parts = filepath.replace("\\", "/").split("/")
    if len(parts) <= max_parts:
        return filepath
    return "…/" + "/".join(parts[-max_parts:])


def request_render():
    # stack = traceback.extract_stack()
    # from src.lsd.gl_gui.melty import Melty
    # Melty.last_request_render = stack[-2].name
    from src.lsd.gl_gui.melty import Melty
    if Toggles.invalidate_stack_trace:
        if Melty.frame_count > 0 and Melty.frame_count % 10 == 0:
            print_stack_trace(size=5)
    _needs_render.set()
    glfw.post_empty_event()


_needs_render = threading.Event()
