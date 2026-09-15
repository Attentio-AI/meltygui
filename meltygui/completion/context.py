"""Built-in FIM context sources + the editor-side helpers that build an
`EditorView`. Every source is a `@fim_context_source` function
`(view: EditorView) -> Iterable[ContextItem]`; the fitter in `fim.py`
dedupes, budgets and orders them. Adding a source ("tests that call this
function", "git blame for these lines") is one decorated function.

All file text is PENDING truth (`PendingSave.current_file_text` through the
roster's `file_text`), never disk.

Tiers (see fim.py): stable items don't change while typing inside one
function (definitions from other files, the enclosing class outline, this
file's imports, observed runtime types); run items change per RUN (live
values); volatile items ride next to the buffer (definitions of symbols
on the caret line).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from meltygui.completion.service import ContextItem
from meltygui.completion.service import EditorView
from meltygui.completion.service import fim_context_source


_DEF_LINE_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
_SKIP_CHAINS = frozenset({"self", "cls", "True", "False", "None", "import", "from", "def",
                          "class", "return", "if", "else", "elif", "for", "in", "not", "and",
                          "or", "is", "while", "try", "except", "finally", "with", "as",
                          "pass", "break", "continue", "lambda", "yield", "raise", "del",
                          "global", "nonlocal", "assert", "async", "await"})
_IMPORT_RE = re.compile(r"^(?:import\s+\S|from\s+\S+\s+import\s)")
_ASSIGN_TARGET_RE_T = r"(?:^|[\s,(\[])%s\s*(?::[^=\n]*)?=(?!=)|\bfor\s+%s\b|\bas\s+%s\b|\b%s\s*:=|\bdef\s+\w+\([^)]*\b%s\b"


# ──────────────────────────────────────────────────────────────────────────
# Editor-side helpers
# ──────────────────────────────────────────────────────────────────────────

def enclosing_def_line(text: str, cursor: int) -> int:
    """0-based buffer line of the nearest `def`/`class` at or above the
    caret (-1 if none) — the stable-tier membership key's scope part."""
    line = text.count("\n", 0, cursor)
    lines = text.split("\n")
    for i in range(min(line, len(lines) - 1), -1, -1):
        if _DEF_LINE_RE.match(lines[i]):
            return i
    return -1


def file_head_tail(text: str, address):
    """(head, tail): the PENDING file text before and after the editor
    buffer `text` for the span at `address` (same splice convention as
    libcst_conversion._full_file_context, so head + text + tail is the file
    as a save would land it). ("", "") when there is no file context."""
    try:
        if (address is None or getattr(address, "path", None) is None
                or getattr(address, "start", None) is None):
            return "", ""
        from meltygui.editor.pending_save import PendingSave
        file_text = PendingSave.current_file_text(address.path)
        if file_text is None:
            return "", ""
        lines = file_text.split("\n")
        start = address.start
        if not (0 <= start <= len(lines)):
            return "", ""
        end = address.end if address.end is not None else start
        end = max(start, min(end, len(lines)))
        buf = text[:-1] if text.endswith("\n") else text
        head = "\n".join(lines[:start]) + ("\n" if start > 0 else "")
        spliced = head + buf + (("\n" + "\n".join(lines[end:])) if lines[end:] else "")
        return head, spliced[len(head) + len(text):]
    except Exception:
        return "", ""


def span_function(path, span_start: int):
    """The live function object whose def starts the span (the live-value
    store owner), via the same resolver the markers use."""
    if path is None:
        return None
    try:
        from meltygui.code.chain_converters import _enclosing_function
        return _enclosing_function(str(path), span_start + 1)
    except Exception:
        return None


def language_of(path) -> str:
    ext = os.path.splitext(str(path or ""))[1].lower()
    return {".py": "python", ".js": "javascript", ".ts": "typescript", ".glsl": "glsl",
            ".frag": "glsl", ".vert": "glsl", ".cu": "cuda", ".md": "markdown",
            ".json": "json", ".toml": "toml", ".ini": "ini", ".sh": "bash"}.get(ext, "text")


def editor_view(text: str, cursor: int, address, fn=None) -> EditorView:
    """Build the EditorView for an editor buffer + its span address (cheap:
    the whole-file split and the function resolve are lazy)."""
    path = str(address.path) if address is not None and getattr(address, "path", None) else None
    version = None
    if path is not None:
        try:
            from meltygui.editor.text import _pending_gen_of
            version = _pending_gen_of(path)
        except Exception:
            version = None
    return EditorView(path=path, text=text, cursor=cursor, address=address,
                      fn=fn, language=language_of(path), version=version)


# ──────────────────────────────────────────────────────────────────────────
# Value summaries (live values) - strings only, the value is never stored
# ──────────────────────────────────────────────────────────────────────────

def _shorten(s: str, n: int) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n - 1] + "…"


def summarize_value(v, stats: bool = False, maxlen: int = 80) -> str:
    """One-line, cheap summary of a runtime value for the model. Tensors/
    arrays report shape·dtype·device (stats — a GPU reduce — only when
    asked); scalars/strings verbatim (truncated); containers by length and
    a few members; anything else by type."""
    try:
        if v is None or isinstance(v, (bool, int, float, complex)):
            return repr(v)
        if isinstance(v, (str, bytes)):
            return _shorten(repr(v), maxlen)
        shape = getattr(v, "shape", None)
        dtype = getattr(v, "dtype", None)
        if shape is not None and dtype is not None:
            try:
                shp = ", ".join(str(int(d)) for d in tuple(shape))
            except Exception:
                shp = str(shape)
            dt = str(dtype).replace("torch.", "")
            dev = getattr(v, "device", None)
            dev_s = f" {dev}" if dev is not None and str(dev) != "cpu" else ""
            out = f"{type(v).__name__}[{shp}] {dt}{dev_s}"
            if stats:
                try:
                    numel = int(getattr(v, "numel", lambda: 0)() or getattr(v, "size", 0))
                    if 0 < numel <= 50_000_000 and "bool" not in dt:
                        f = v.float() if hasattr(v, "float") else v
                        out += f" min={float(f.min()):.3g} mean={float(f.mean()):.3g} max={float(f.max()):.3g}"
                except Exception:
                    pass
            return out
        if isinstance(v, dict):
            keys = list(v.keys())[:4]
            ks = ", ".join(_shorten(repr(k), 16) for k in keys)
            return f"dict[{len(v)}]" + (f" {{{ks}{', …' if len(v) > 4 else ''}}}" if keys else "")
        if isinstance(v, (list, tuple, set, frozenset)):
            items = list(v)[:3]
            inner = ", ".join(_shorten(summarize_value(x, False, 24), 24) for x in items)
            return f"{type(v).__name__}[{len(v)}]" + (f" [{inner}{', …' if len(v) > 3 else ''}]" if items else "")
        return type(v).__name__
    except Exception:
        return type(v).__name__


# ──────────────────────────────────────────────────────────────────────────
# Sources
# ──────────────────────────────────────────────────────────────────────────

def _whole_file(view: EditorView) -> str:
    return view.file_head + view.text + view.file_tail


def _word_set(text: str) -> set:
    return set(re.findall(r"[A-Za-z_]\w*", text))


@fim_context_source("outline", tier="stable", order=10)
def outline_source(view: EditorView):
    """This file's imports (col-0 import/from lines, parenthesised
    continuations included) — the names the model may use unqualified."""
    if view.path is None:
        return
    lines = _whole_file(view).split("\n")
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if _IMPORT_RE.match(ln):
            block = [ln]
            if "(" in ln and ")" not in ln:
                while i + 1 < len(lines) and ")" not in lines[i]:
                    i += 1
                    block.append(lines[i])
            out.append("\n".join(block))
        i += 1
    if out:
        yield ContextItem("outline", ("outline", view.path), "\n".join(out),
                          tier="stable", score=0.5, path=view.path, line=1,
                          version=view.version)


def _table(view: EditorView):
    """(roster, FileTable) for the file: the roster's cached PENDING-text
    table (content-free key, so repeated assemblies are free). The buffer's
    last few unsaved keystrokes aren't in the table yet — fine, the chain
    SCAN runs over the live buffer and resolves against it. Never the
    roster's `live_text=` overlay: that re-parses the span alone and a
    method loses its class parent. Falls back to a one-off parse of the
    spliced file when the roster has no table for the path."""
    import meltygui.code.symbol_roster as roster
    tbl = getattr(view, "_table", None)
    if tbl is None:
        try:
            tbl = roster.table_for(view.path)
        except Exception:
            tbl = None
        if tbl is None:
            tbl = roster.extract_table(view.path, _whole_file(view), with_tints=False)
        view._table = tbl
    return roster, tbl


def _window(view: EditorView, lines: int):
    """(start_char, end_char, start_line) of the ±`lines` window around the
    caret — the only part of a huge buffer the per-request scans look at.
    Found by newline walks, no full split."""
    text = view.text
    cur = view.cursor
    ls = text.rfind("\n", 0, cur) + 1
    start_line = view.caret_line
    s = ls
    for _ in range(lines):
        if s <= 0:
            break
        s = text.rfind("\n", 0, s - 1) + 1
        start_line -= 1
    e = text.find("\n", cur)
    e = len(text) if e < 0 else e + 1
    for _ in range(lines):
        if e >= len(text):
            break
        nxt = text.find("\n", e)
        e = len(text) if nxt < 0 else nxt + 1
    return s, e, max(0, start_line)


@fim_context_source("enclosing", tier="stable", order=20)
def enclosing_source(view: EditorView):
    """Skeleton of the class enclosing the span's first def — class vars and
    sibling method signatures — so `self.` completions see the surface."""
    if view.path is None:
        return
    try:
        roster, tbl = _table(view)
    except Exception:
        return
    if tbl is None:
        return
    first = view.span_start + 1
    scope = tbl.scope_at(first + 1) or tbl.scope_at(first)
    cls_q = tbl.enclosing_class(scope) if scope is not None else None
    if cls_q is None:
        return
    cls = tbl.by_qualname.get(cls_q)
    if cls is None:
        return
    src = roster.file_text(view.path).split("\n")
    rows = [src[cls.line - 1].rstrip() if 0 < cls.line <= len(src) else f"class {cls.name}:"]
    own_q = scope.qualname if scope is not None else None
    for e in tbl.entries:
        if e.parent != cls_q or e.qualname == own_q:
            continue
        if e.kind == "var":
            if 0 < e.line <= len(src):
                rows.append("    " + src[e.line - 1].strip())
        else:
            sig = e.sig or (src[e.line - 1].strip() if 0 < e.line <= len(src) else f"def {e.name}(...)")
            rows.append("    " + sig.strip().rstrip(":") + ": ...")
    if len(rows) > 1:
        yield ContextItem("enclosing", ("enclosing", view.path, cls_q), "\n".join(rows),
                          tier="stable", score=2.0, path=view.path, line=cls.line,
                          end=cls.end, version=view.version)


@fim_context_source("definition", tier="stable", order=30)
def definition_source(view: EditorView):
    """Source of every definition the span references, resolved textually
    through the roster (pending truth, closures/self./imports honoured).
    Definitions referenced on the caret line go to the volatile tier; the
    rest are stable. `data` carries the signature line for budget
    degradation."""
    if view.path is None:
        return
    try:
        roster, tbl = _table(view)
    except Exception:
        return
    if tbl is None:
        return
    from meltygui.toggles import Toggles
    text = view.text
    caret_line = view.caret_line
    own_lo = view.span_start + 1
    own_hi = view.span_start + text.count("\n") + 1
    # Scan only a window around the caret (a whole-file buffer has
    # thousands of chains; each resolve is a roster walk), and resolve each
    # unique (chain, scope) once - a window sees the same names a lot.
    ws, we, wline = _window(view, Toggles.Fim.scan_lines)
    occurrences = {}   # (chain, scope qualname) -> [scope, count, best_prox, on_caret]
    ln = wline
    pos = ws
    for s, e, chain in roster.iter_chains(text, ws, we):
        ln += text.count("\n", pos, s)
        pos = s
        if chain in _SKIP_CHAINS:
            continue
        sc = tbl.scope_at(ln + 1 + view.span_start)
        k = (chain, sc.qualname if sc is not None else None)
        prox = abs(ln - caret_line)
        o = occurrences.get(k)
        if o is None:
            occurrences[k] = [sc, 1, prox, ln == caret_line]
        else:
            o[1] += 1
            o[2] = min(o[2], prox)
            o[3] = o[3] or ln == caret_line
    hits = {}          # ident -> [entry, count, best_prox, on_caret_line]
    for (chain, _sq), (sc, count, prox, on_caret) in occurrences.items():
        try:
            res = roster.resolve_prefixes(view.path, chain, tbl, scope=sc)
        except Exception:
            continue
        if not res:
            continue
        ent, _n = res[-1]
        if ent.path == view.path and own_lo <= ent.line <= own_hi:
            continue       # defined inside the span itself
        h = hits.get(ent.ident)
        if h is None:
            hits[ent.ident] = [ent, count, prox, on_caret]
        else:
            h[1] += count
            h[2] = min(h[2], prox)
            h[3] = h[3] or on_caret
    max_lines = Toggles.Fim.definition_max_lines
    for ident, (ent, count, prox, on_caret) in hits.items():
        try:
            src = roster.file_text(ent.path).split("\n")
        except Exception:
            continue
        lo, hi = ent.line - 1, max(ent.line, ent.end)
        if not (0 <= lo < len(src)):
            continue
        block = src[lo:hi]
        sig = (ent.sig or block[0]).strip()
        if len(block) > max_lines:
            block = block[:max_lines] + ["    ..."]
        score = count * (4.0 if on_caret else 2.0 if prox <= 3 else 1.0)
        score *= {"class": 1.2, "def": 1.0, "var": 0.6}.get(ent.kind, 1.0)
        yield ContextItem("definition", ("definition", ent.path, ent.qualname),
                          "\n".join(block), tier="volatile" if on_caret else "stable",
                          score=score, path=ent.path, line=ent.line, end=hi,
                          version=view.version, data=sig)


@fim_context_source("runtime_types", tier="stable", order=40)
def runtime_types_source(view: EditorView):
    """Observed runtime types of the span's names (FuncsMetadata) — tells
    the model what un-annotated params are."""
    if view.fn is None:
        return
    try:
        from meltygui.func_metadata import FuncsMetadata
        from meltygui.func_metadata import _meta_key
        slot = FuncsMetadata.metadata.get(_meta_key(view.fn))
    except Exception:
        return
    if not slot:
        return
    from meltygui.toggles import Toggles
    ws, we, _ = _window(view, Toggles.Fim.scan_lines)
    words = _word_set(view.text[ws:we])
    rows = []
    for name in sorted(slot):
        if name not in words or name.startswith("__"):
            continue
        meta = slot[name]
        t = getattr(meta, "type", None)
        if t is None:
            continue
        mod = getattr(t, "__module__", "") or ""
        tn = t.__name__ if mod in ("builtins", "") else f"{mod.rsplit('.', 1)[-1]}.{t.__name__}"
        rows.append(f"{name}: {tn}")
    if rows:
        q = getattr(view.fn, "__qualname__", "?")
        yield ContextItem("runtime_types", ("runtime_types", view.path, q),
                          "# observed at runtime:\n# " + ", ".join(rows),
                          tier="stable", score=3.0, path=view.path, version=view.version)


def _parse_live_key(key_path):
    """(line, name) from a store key whose tail is `line:N#name`."""
    tail = key_path[-1] if isinstance(key_path, tuple) and key_path else key_path
    if not isinstance(tail, str) or not tail.startswith("line:"):
        return None, None
    body = tail[5:]
    num, _, name = body.partition("#")
    try:
        return int(num), (name or None)
    except ValueError:
        return None, None


def _binding_line(lines, name, guess, radius=25):
    """Nearest buffer line to `guess` that binds `name` (assignment / for /
    with-as / walrus / def param), or None."""
    if not name:
        return None
    pat = re.compile(_ASSIGN_TARGET_RE_T % ((re.escape(name),) * 5))
    best = None
    for d in range(radius + 1):
        for cand in (guess - d, guess + d) if d else (guess,):
            if 0 <= cand < len(lines) and pat.search(lines[cand]):
                return cand
    return best


@fim_context_source("live_values", tier="run", order=50)
def live_values_source(view: EditorView):
    """Values from the LAST RUN of the span's function (`__live_values__`,
    `line:N#name` keys), summarized and anchored to the buffer line that
    binds the name. `data` = {buffer line: summary} for inline
    annotation; `text` = the same as a block for providers that can't
    inline. Values are summarized on the spot and never retained."""
    if view.fn is None:
        return
    from meltygui.toggles import Toggles
    try:
        from meltygui.code.live_view import live_values_for
        store = live_values_for(view.fn)
    except Exception:
        return
    if not store:
        return
    try:
        import inspect as _inspect
        labels = getattr(_inspect.unwrap(view.fn), "__live_labels__", None) or {}
    except Exception:
        labels = {}
    lines = view.text.split("\n")
    delta = 0
    try:
        from meltygui.editor.text import _pending_line_delta
        delta = _pending_line_delta(view.path, view.span_start) if view.path else 0
    except Exception:
        delta = 0
    found = {}
    for key_path, value in store.items():
        disk_line, name = _parse_live_key(key_path)
        if disk_line is None:
            continue
        name = labels.get(key_path, name) or name
        guess = disk_line - 1 + delta - view.span_start
        bl = _binding_line(lines, name, guess)
        if bl is None:
            continue
        summary = summarize_value(value, stats=Toggles.Fim.live_value_stats)
        prev = found.get(bl)
        entry = f"{name} = {summary}" if name else summary
        found[bl] = entry if prev is None else f"{prev}; {entry}"
    del store
    if not found:
        return
    caret = view.caret_line
    keep = sorted(found, key=lambda bl: abs(bl - caret))[:Toggles.Fim.live_values_max]
    data = {bl: found[bl] for bl in sorted(keep)}
    text = "# values from the last run:\n" + "\n".join(
        f"# L{bl + 1}: {s}" for bl, s in data.items())
    q = getattr(view.fn, "__qualname__", "?")
    yield ContextItem("live_values", ("live_values", view.path, q), text,
                      tier="run", score=3.0, path=view.path, data=data)
