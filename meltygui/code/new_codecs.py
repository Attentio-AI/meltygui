import ast
import difflib
import hashlib
import inspect
import os
import re
import sys
import textwrap
import tokenize
import types
from dataclasses import dataclass
from enum import EnumType
from pathlib import Path

from src.lsd.gl_gui.view.core_conversion.address import (
    Address, _evict_linecache, shift_sibling_linenos, is_editable_source,
    is_writable_file)
from src.lsd.gl_gui.view.core_conversion.chain_converters import (
    _ensure_import_lines, _resolve_call_address, _split_span_at_call, DiskSpanText)
from src.lsd.gl_gui.view.core_conversion.bubbling import base_of_bubbling
from src.lsd.gl_gui.view.core_conversion.file_converters import _detect_newline
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core

from src.lsd.gl_gui.melty import Melty, FileWatch
from src.lsd.gl_gui.perf_trace import trace_rl as _ptrace_rl
from src.shader_library.shader_manager.texture_manager import PIL_TO_GL_FORMAT, PendingTexture

# No GL imports here: ImageCodec.load only DECODES (background thread); the GL
# upload runs on the UI thread via PendingTexture.pending_upload.
from PIL import Image
import io
import mimetypes

NO_DATA = object()

extension_to_codec = {}
type_to_codec = {}


class SaveConflict:
    """Returned by codec.save when the on-disk span no longer matches what was
    last loaded/saved — an external write landed under the (debounced) save.
    Splicing anyway would replace the WRONG lines, so the write is refused;
    code_file_io keeps the edit pending and surfaces the Load / Keep-mine
    conflict UI instead."""

    def __init__(self, reason):
        self.reason = reason

    def __repr__(self):
        return f"SaveConflict({self.reason!r})"


def _span_fingerprint(lines):
    """Content hash of a span's lines, line-ending agnostic — the same span
    fingerprints identically whether the lines came from linecache (keep "\\n"),
    a CRLF file split, or a plain split. Used to verify at save time that the
    on-disk span still holds what we last loaded/saved before splicing over it."""
    h = hashlib.sha1()
    for line in lines:
        h.update(line.rstrip("\r\n").encode("utf-8", "surrogatepass"))
        h.update(b"\n")
    return h.hexdigest()


def _source_and_newline(address, source_text=None):
    """The file's full text + dominant newline for a codec load.

    Normally reads disk. `source_text` (the EXACT text an in-process write just
    produced, handed over by FileWatch.get_self_write_text) is used instead — a
    self-write sync that skips the disk round trip AND the "Loading…" event. The
    span slice + `_span_fingerprint` downstream are identical either way, since
    the recorded text IS what codec.save wrote to the file."""
    if source_text is not None:
        return source_text, _detect_newline(source_text.encode("utf-8", "surrogatepass"))
    data = address.path.read_bytes()
    newline = _detect_newline(data)
    try:
        return data.decode("utf-8"), newline
    except UnicodeDecodeError:
        return data.decode("latin-1"), newline


def _block_is_function(source_lines, name):
    """Does this getsourcelines block actually contain `def <name>`?

    inspect.findsource trusts co_firstlineno and walks BACKWARD to the nearest
    def-looking line — after an EXTERNAL edit shifted the file (nothing patches
    live linenos for external writes), that lands on a DIFFERENT function or the
    file head, silently. Loading/saving through that wrong span is the
    file-mangling bug, so verify the block names the function we asked for."""
    if not name.isidentifier():  # <lambda> & friends - can't verify
        return True
    pat = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b")
    return any(pat.match(line) for line in source_lines)


def _reanchor_function(unwrapped, source_file):
    """Re-find a function whose co_firstlineno went stale (an external edit
    shifted the file) by ast-walking the CURRENT file for its __qualname__.

    Returns (start0, end0, span_lines) — 0-based [start, end) covering the
    decorators + def — and HEALS the live code object's co_firstlineno (set to
    the first decorator line, the compile convention) so subsequent resolves,
    recompiles, and sibling shifts work from truthful coordinates again.
    Returns None when the file doesn't parse (mid-edit), the function is nested
    (`<locals>` — its def isn't addressable by a body walk), or the qualname
    path isn't found."""
    qual = unwrapped.__qualname__
    if "<locals>" in qual or not unwrapped.__name__.isidentifier():
        return None
    try:
        data = Path(source_file).read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return None

    node, body = None, tree.body
    parts = qual.split(".")
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        found = None
        for child in body:
            if last and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name == part:
                found = child
                break
            if not last and isinstance(child, ast.ClassDef) and child.name == part:
                found = child
                break
        if found is None:
            return None
        node, body = found, getattr(found, "body", [])

    decos = getattr(node, "decorator_list", [])
    first_lineno = min([d.lineno for d in decos] + [node.lineno])
    start0, end0 = first_lineno - 1, node.end_lineno
    span_lines = text.splitlines()[start0:end0]
    try:
        unwrapped.__code__ = unwrapped.__code__.replace(co_firstlineno=first_lineno)
    except (AttributeError, TypeError, ValueError):
        pass  # read-only code object - the span is still right for this run
    return start0, end0, span_lines

# Library-source guard is in address.py (is_editable_source) so the codec and
# the older file_converters save paths share one gate. Local alias for brevity.
_is_editable_source = is_editable_source


@dataclass(frozen=True)
class CallSite:
    """The dispatch key for `CallerCodec`: where a function is CALLED, not where
    it's defined. A function object resolves to its `def` span; a `CallSite`
    resolves to the call STATEMENT at `(filename, lineno)`.

    A plain `(filename, lineno)` tuple can't be type-dispatched (it's just a
    tuple), so we wrap it in a dedicated type and register the codec `for_type`.
    This is the pattern future targeted codecs follow too — e.g. a `ClassAttribute`
    type pointing a codec at a single `name = value` line in a class body.

    Construct from `caller_site(get_live_frames())` (which already strips the
    render-dispatch frames), then hand the `CallSite` to `code_file_io`."""
    filename: str
    lineno: int


@dataclass(frozen=True)
class Decorations:
    """The dispatch key for `DecorationsCodec`: the DECORATOR block above a class
    or function, edited on its own.

    `Decorations(Toggles)` resolves to just the `@window(...)` line(s) above
    `class Toggles:` — not the class body (that's `TypeCodec`) and not a call site
    (that's `CallSite`). Same family pattern as `CallSite`: wrap the target so the
    codec can be type-dispatched, then hand it to `code_file_io`.

    `target` is the live class or function object whose decorators are edited. The
    codec slices out the decorator lines for editing and re-runs the decorators by
    recompiling the WHOLE object (see `_recompile_decorations`)."""
    target: object


def _resync_module_linenos(address, old_lines, new_lines):
    """After a WHOLE-FILE save, shift live co_firstlineno's to match the new text.

    A SPANNED save knows its single (after_lineno, delta) and calls
    shift_sibling_linenos directly; a whole-file edit can change line counts
    anywhere, so walk the old→new diff and apply one shift per line-count
    change, bottom-up so earlier shifts don't disturb later regions. Without
    this, views rendering LIVE objects from this file (FunctionCodec /
    TypeCodec) resolve stale spans after the save and snap to the nearest def."""
    resync_file_linenos(address.path, old_lines, new_lines)


def resync_file_linenos(path, old_lines, new_lines):
    """Shift live co_firstlineno's in every module loaded from `path` (resolved
    Path) so they match `new_lines`, given they currently match `old_lines`.

    The address-free body of _resync_module_linenos: also used by
    PendingSave.resolve_external, which moves live coordinates from the
    last-synced text to the current (externally written) disk WITHOUT any
    disk write of its own.

    The same file can be materialized under several module names (src.lsd.…
    and lsd.… import roots both exist here), each with its OWN function
    objects — shift every matching module, deduped by identity."""
    opcodes = [(i2, (j2 - j1) - (i2 - i1))
               for tag, i1, i2, j1, j2 in
               difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
               if tag != "equal" and (j2 - j1) != (i2 - i1)]
    if not opcodes:
        return
    seen, modules = set(), []
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        try:
            if f and id(m) not in seen and Path(f).resolve() == path:
                seen.add(id(m))
                modules.append(m)
        except (OSError, ValueError):
            continue
    for module in modules:
        for after_lineno, delta in sorted(opcodes, reverse=True):
            shift_sibling_linenos(module, path,
                                  after_lineno=after_lineno, delta=delta,
                                  include_saved=True)


def register_codec(cls=None, **kwargs):
    def wrap(cls):
        if "ext" in kwargs:
            exts = kwargs["ext"]
            if isinstance(exts, str):
                exts = (exts,)   # a bare "png" once iterates CHAR BY CHAR
            for ext in exts:
                # Normalize to Path.suffix's shape (".png", lowercase) so
                # registration matches lookup regardless of how it was written.
                if not ext.startswith("."):
                    ext = "." + ext
                extension_to_codec[ext.lower()] = cls

        if "for_type" in kwargs:
            if isinstance(kwargs["for_type"], tuple):
                for t in kwargs["for_type"]:
                    type_to_codec[t] = cls
            else:
                type_to_codec[kwargs["for_type"]] = cls
        return cls

    if cls is None:
        return wrap

    return wrap(cls)

class Codec:
    name = "Base Codec"
    # Base render kwargs for views of this codec's data - the codec IS the
    # data source, so source-level appearance lives here. core_render merges
    # these in as the LOWEST priority layer (args = codec_kwargs | kwargs;
    # everything overrides here) - EXCEPT `tint`, which never propagates:
    # it is the source COLOR-CODE, consumed explicitly by provenance views
    # (the context menu's draw_param_matrix), not an optional wash.
    render_kwargs = {}
    # Optional view override for this codec's loaded data. None (the default)
    # means "use the type": a str lands in the text view the caller wired,
    # anything else goes through draw_any to the type's default renderer
    # (is_default_for) - so a codec whose load() returns a type that already
    # has a view needs nothing here. Set it only for a type without a default
    # view or to pin a non-default one. See code_file_io's _set_view.
    view_func = None
    # Whether edits to the loaded value round-trip to the file. False (images,
    # mainly - anything save() refuses) makes code_file_io ignore view edits
    # (no dirty / save runner) and reload external changes immediately instead
    # of parking on the merge/conflict banner: nothing local can be saved.
    editable = True
    # Tab glyph (unicode) for files of this type - the editor's tab bar
    # falls back to it if the file's FileMeta entry carries no icon.
    icon = None

    @staticmethod
    def show_code_buttons(address):
        """The codec's "this is Python source" switch for code_file_io: the
        Run (hotswap) and Index (jedi) buttons, their hotkeys, AND the error
        checking (the code-host parse + syntax/lint/runtime highlights) all
        ride it. Only meaningful where the loaded text is Python, so the base
        says no; TypeCodec (live Python objects) says yes; TextFileCodec says
        yes for .py paths only."""
        return False

    @classmethod
    def claims(cls, path):
        """Extension routing's content veto: codec_for_path asks the
        extension-matched codec whether the file's BYTES actually decode as
        what the extension promises. Declining (False) drops the file through
        to the content sniff instead — a zero-byte or corrupt "image.png"
        becomes editable text / a binary summary rather than a load() that
        throws on every watch-triggered reload. The base accepts everything;
        only codecs whose load() can reject content (ImageCodec) override."""
        return True

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        return NO_DATA

    @staticmethod
    def load(path, **kwargs):
        return NO_DATA

    @staticmethod
    def save(data, file_path, **kwargs):
        return False

    # ── Per-file attributes (AppModel.file_meta_collection) ────────────────
    # The codec is an input source (the _ViewSource row / SourcePriority.
    # CODEC). Its class-level render_kwargs are in-memory and codec-wide;
    # per-FILE attributes instead land in FileMetaCollection.file_meta - the
    # DictConversion mirroring the file tree - keyed by the file this
    # element's Address resolves to. Living on AppModel makes persistence
    # automatic (rides the root save), and the folder tree re-applies thes
    # as meta on every run (folder_files._apply_meta).

    @classmethod
    def file_meta_key(cls, draw_state):
        """The file-meta dict key (path string) for the file this element
        belongs to, memoized on the draw_state's `_file_meta` field. O(1) BY
        DESIGN — this runs in the wrapper's per-render codec layer, so no
        ancestor walk: the ds's OWN codec-stamped Address resolves it, else
        the key propagates one hop from the parent's memo (parents render
        before children, so a subtree under a resolved view fills in top-down
        across frames; an unbounded walk here froze startup — cycles aside,
        it was O(depth) per view per frame across every codec subtree)."""
        key = getattr(draw_state, "_file_meta", None)
        if isinstance(key, str):
            return key
        path = getattr(getattr(draw_state, "_address", None), "path", None)
        if path:
            key = str(path)
        else:
            parent = getattr(draw_state, "_parent", None)
            pkey = getattr(parent, "_file_meta", None) if parent is not None else None
            key = pkey if isinstance(pkey, str) else None
        if key is not None:
            draw_state._file_meta = key
        return key

    @classmethod
    def file_meta_entry(cls, draw_state, create=False):
        """This file's params dict in AppModel.file_meta_collection.file_meta,
        or None (no root yet / no file resolves / no entry and not create).
        create=True materializes the entry (and backfills the collection onto
        a pre-field root, same self-heal as folder_files._file_meta)."""
        from src.lsd.gl_gui.melty import Melty
        root = getattr(getattr(Melty, "vis", None), "root", None)
        if root is None:
            return None
        col = getattr(root, "file_meta_collection", None)
        if col is None:
            from src.lsd.gl_gui.model.app_model import FileMetaCollection
            col = root.file_meta_collection = FileMetaCollection()
        if not isinstance(getattr(col, "file_meta", None), dict):
            col.file_meta = {}
        path = cls.file_meta_key(draw_state)
        if path is None:
            return None
        entry = col.file_meta.get(path)
        if not isinstance(entry, dict):
            if not create:
                return None
            from src.lsd.gl_gui.model.app_model import FileMeta
            entry = col.file_meta[path] = FileMeta()
        return entry

    @classmethod
    def update_file_meta(cls, draw_state, key, value):
        """Stamp a per-file attribute into the correct file's metadata entry.
        Returns True when the write landed (a file resolved), False when it
        couldn't — the caller falls back to codec-wide behavior."""
        entry = cls.file_meta_entry(draw_state, create=True)
        if entry is None:
            return False
        entry[key] = value
        return True


@register_codec(for_type=(type, EnumType))
class TypeCodec(Codec):
    # Class source - where @defaults lives: dark grey/blue.
    render_kwargs = {"tint": (0.10, 0.12, 0.22, 0.40)}

    @staticmethod
    def show_code_buttons(address):
        return True   # the data IS live Python source - Run/Index apply

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        if isinstance(input_value, type) and input_value.__module__ not in ('builtins', '_collections_abc'):
            # A runtime-generated bubbling subclass has no source - resolve its base.
            unwrapped = base_of_bubbling(input_value)
            try:
                source_file = inspect.getfile(unwrapped)
            except TypeError:
                return None

            # Refuse library source - we only ever edit this project's own code.
            if not _is_editable_source(source_file):
                return None

            if draw_state is not None:
                FileWatch.register_draw_state(draw_state, Path(source_file))
        else:
            return None

        try:
            mtime = Path(source_file).stat().st_mtime
        except OSError:
            mtime = None
        cached = getattr(draw_state, '_addr_cache', None)
        if cached is not None and cached[0] is input_value and cached[1] == mtime:
            return cached[2]

        # Timeline: WHY the cached address lookup stale - this miss path pays a
        # whole Python getsourcelines call on the render thread, so a miss
        # per keystroke (e.g. the trailing disk save moving mtime) is a big hitch.
        _why = ("cold" if cached is None
                else "identity" if cached[0] is not input_value
                else f"mtime {cached[1]}->{mtime}")
        _ptrace_rl(("addr-resolve", id(draw_state)), f"addr re-resolve ({_why})",
                   target=getattr(input_value, '__name__', '?'))
        _evict_linecache(source_file)
        try:
            source_lines, start_lineno = inspect.getsourcelines(unwrapped)
        except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
            if hasattr(draw_state, '_addr_cache') and draw_state._addr_cache is not None:
                return draw_state._addr_cache[2]

            print(f"[editable_source] could not resolve {getattr(input_value, '__name__', input_value)}: {e}")
            return None

        address = Address(Path(source_file), start_lineno - 1,
                          start_lineno - 1 + len(source_lines),
                          source=unwrapped, watcher_ds=draw_state)
        address._span_fp = _span_fingerprint(source_lines)
        draw_state._addr_cache = (input_value, mtime, address)
        return address


    @classmethod
    def load(cls, address, source_text=None, **kwargs):
        # In-memory overlay: a queued-but-unflushed edit for this span (saves
        # defer to shutdown) is the freshest text; return it as the now-stale
        # disk content, and skip the disk read below. An explicit source_text
        # (a verified reload copy of exact disk content) still takes the slice
        # path below. See PendingSave.pending_text_for.
        if source_text is None:
            from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
            pending = PendingSave.pending_text_for(address)
            if pending is not None:
                # Re-baseline the save conflict guard against the app's OWN writes.
                # _span_fp (set at first load) means "the disk content this pending
                # edit was derived from". When ANOTHER surface edits the same span
                # it writes via codec.save, and this view's pending text is
                # rebased onto the new disk by the post-write reload - but _span_fp
                # stayed frozen, so save reads the app's own sibling write as an
                # EXTERNAL conflict and refuses it (the multi-surface false
                # conflict). When the new disk is an in-process write
                # (is_self_write), it is exactly what the rebased pending sits on,
                # so sync _span_fp to match. A genuine EXTERNAL write leaves
                # is_self_write False, the baseline stays stale, and the guard
                # still fires - a real conflict.
                if FileWatch.is_self_write(address.path):
                    try:
                        text, newline = _source_and_newline(address)
                        lines = text.split(newline)
                        span = (lines if address.start is None
                                else lines[address.start:address.end])
                        address._span_fp = _span_fingerprint(span)
                    except OSError:
                        pass
                return pending
        # Launch-baseline overlay (whole-file loads): PENDING is the studio's
        # copy. A file that drifted on disk since the studio last knew it
        # (ExternalChanges.original - seeded from core_project.py'
        # launch preload via the first-time code_cache pop) opens as that
        # LAST-KNOWN text, not the new disk content: external changes are
        # never auto-pulled into pending; they stay visible through the git
        # file_system proxy / compare until manually merged. The baseline
        # fingerprint arms the save conflict guard, so saving this buffer
        # refuses against the newer disk and routes through the merge
        # surface. Span loads skip it - the baseline's line numbering can't
        # be trusted for a post-drift span address; self-writes skip it
        # - the disk already IS the studio's own text.
        if source_text is None and address.start is None \
                and not FileWatch.is_self_write(address.path):
            from src.lsd.gl_gui.view.core_views.external_changes import \
                ExternalChanges
            try:
                _res = str(address.path.resolve())
            except OSError:
                _res = None
            base = ExternalChanges.originals.get(_res) if _res else None
            if isinstance(base, str) and base:
                address._span_fp = _span_fingerprint(base.split("\n"))
                return base
        text, newline = _source_and_newline(address, source_text)
        lines = text.split(newline)
        span_lines = lines[address.start:address.end]
        # Remember what the span held when it was loaded; save verifies the disk
        # still holds this before splicing over it (see save's conflict guard).
        address._span_fp = _span_fingerprint(span_lines)
        out = newline.join(span_lines)
        # Plain disk read (no pending overlay reached this path): stamp the
        # text with the mtime it reflects so the CST's cache can serve /
        # store its parse with no content comparison (see DiskSpanText). Any
        # edit decays it to plain str. An explicit source_text is a VERIFIED
        # self-write of exact disk content, so it carries provenance too.
        if source_text is None or FileWatch.is_self_write(address.path):
            try:
                stamped = DiskSpanText(out)
                stamped._disk_mtime = address.path.stat().st_mtime
                stamped._disk_span = (os.path.realpath(str(address.path)),
                                      address.start, address.end)
                # Value-carried provenance: the loaded text knows its codec,
                # so rendering it OUTSIDE this codec's own subtree (the
                # the an editor drawing host["value"] with draw_text)
                # still re-establishes the codec context - and with it the
                # per-file kwargs layer. See core_render's codec_scope.
                stamped._codec = cls
                return stamped
            except OSError:
                pass
        return out


    @staticmethod
    def save(address, data, ensure_import=None, force=False, **kwargs):
        """Write code_str back into the file at the Address's span — the synchronous
            body of the old @render_func(background=True) _do_save, minus the Pending.

            ensure_import=(module, name) inserts a missing import in the SAME write so a
            synthesized decorator (e.g. @defaults) resolves. Updates the Address span in
            place and shifts siblings so this frame's Address stays valid; siblings heal
            on the next mtime-driven re-resolve.

            Refuses the write (returns SaveConflict) when the on-disk span no longer
            fingerprints to what was last loaded/saved there — an external program
            wrote the file under us, and splicing would land on the wrong lines.
            force=True (the user's explicit "Keep mine") skips that guard."""
        # Defense in depth: never write to library source even if an Address
        # somehow points outside the project (resolve_address should already have
        # refused it). A bad span splice bug once corrupted libcst's own source.
        # `_allow_write` is the whole-file opt-out: TextFileCodec stamps it on
        # Addresses it resolved through the gentler is_writable_source gate
        # (folder windows mount paths outside the project), so whole-file saves
        # there pass while code codecs stay pinned to the project tree.
        if not (_is_editable_source(address.path)
                or getattr(address, "_allow_write", False)):
            print(f"[codec.save] refusing to write library source: {address.path}")
            return False
        full = address.path.read_bytes()
        newline = _detect_newline(full)
        try:
            text = full.decode("utf-8")
        except UnicodeDecodeError:
            text = full.decode("latin-1")
        lines = text.split(newline)
        # `data` must use the SAME span convention as load(): a span of N lines is
        # N elements joined by N-1 newlines, with no trailing newline. If the
        # editor (or a cst round-trip) hands us a trailing newline, splitting it
        # yields a phantom empty element - which both splices a spurious blank line
        # into the file AND inflates the line-count delta, so siblings below
        # over-shift by one. That drift accumulates per save until findsource's
        # backward walk lands on the wrong def and the view loses its reference.
        # Stick to the load convention: strip exactly one trailing newline.
        if data.endswith(newline):
            data = data[:-len(newline)]
        new_lines = data.split(newline)

        old_start, old_end = address.start, address.end

        # ─── Verify before splice ──────────────────────────────────────────────
        # The span coordinates were resolved on the render thread, possibly
        # hundreds of ms before this (debounced, background) save. If anything
        # else wrote the file in between, our coordinates index DIFFERENT
        # content and splicing would mangle the result (duplicate the function,
        # tear lines). Check the span still holds what we last loaded/saved;
        # on mismatch abort, and let the conflict UI sort it out.
        expected_fp = getattr(address, "_span_fp", None)
        if not force and expected_fp is not None:
            on_disk = lines if old_start is None else lines[old_start:old_end]
            if _span_fingerprint(on_disk) != expected_fp:
                print(f"[codec.save] refused: {address.path.name}"
                      f"[{old_start}:{old_end}] changed on disk since load")
                return SaveConflict(f"{address.path.name} changed on disk")

        if old_start is None:  # whole-file (module) address
            old_lines = lines  # pre-splice contents, for the lineno resync below
            lines = new_lines
        else:
            lines[old_start:old_end] = new_lines

        inserted = 0
        insert_idx = None
        if ensure_import is not None:
            # Three shapes: (module, name) - the legacy @defaults;; a full
            # statement string ("import numpy as np" - the editor's
            # missing-import quick-fix); or a list of either (several fixes
            # queued on one entry before the flush).
            _eis = (ensure_import if isinstance(ensure_import, list)
                    else [ensure_import])
            for _ei in _eis:
                if isinstance(_ei, str):
                    lines, _ins, _idx = _ensure_import_lines(lines, _ei)
                else:
                    lines, _ins, _idx = _ensure_import_lines(lines, _ei[0], _ei[1])
                inserted += _ins
                if _idx is not None:
                    insert_idx = _idx if insert_idx is None else min(insert_idx, _idx)

        final_text = newline.join(lines)

        # Patch live co_firstlineno's BEFORE the write, not after. Other views of
        # this file reload on the mtime bump (FileWatch dispatch / auto_load_edits
        # on the render thread) and re-resolve our span - resolve_address caches
        # per (input, mtime), so a resolve that races a post-write shift sees the
        # NEW file with the OLD linenos, walks findsource back to the wrong def,
        # and pins that wrong span for the new mtime (nothing busts it until the
        # NEXT save). Pre-write updates are safe in the other direction: until the
        # write below bumps mtime, the resolve is pulled from the cache, so the
        # transient "old file + new linenos" state is never observed.
        if old_start is not None:
            resolved_old_end = old_end if old_end is not None else old_start + len(new_lines)
            new_end = old_start + len(new_lines)
            delta = new_end - resolved_old_end
            address.end = new_end
            if inserted:  # import landed above our span
                address.start = old_start + inserted
                address.end = new_end + inserted
            # The shift anchor is normally the span's live source; an entry
            # whose source is deliberately NON-code (like auto-import insertion
            # - a plain marker string so recompile_all never hotswaps it) can
            # carry the module to shift as `_shift_source` instead.
            _shift_src = getattr(address, "_shift_source", None) or address.source
            # Shift siblings below for the body span change (original coords)...
            shift_sibling_linenos(_shift_src, address.path,
                                  after_lineno=resolved_old_end, delta=delta)
            # ...then the import insert moved our def AND everything below it down,
            # so shift the saved source too (include_saved) - else its own
            # co_firstlineno goes stale and the next resolve walks back to line 0.
            if inserted and insert_idx is not None:
                shift_sibling_linenos(_shift_src, address.path,
                                      after_lineno=insert_idx, delta=inserted,
                                      include_saved=True)
        else:
            # Whole-file address: the splice gave us a single delta, so resync
            # live line numbers from the old→new diff (see the helper).
            _resync_module_linenos(address, old_lines, lines)

        # Stamp the watch hash BEFORE writing so our own write doesn't read back as a
        # stale external change.
        FileWatch.set_hash_from_content(address.path, final_text, draw_state=address._watcher_ds)
        address.path.write_text(final_text, encoding="utf-8")
        # The on-disk span is now exactly what we wrote - refresh the conflict
        # guard's baseline so the next save verifies against THIS write.
        address._span_fp = _span_fingerprint(new_lines if old_start is not None else lines)

        # Serve any post-write resolve from cache. This save kept address.start/end
        # truthful, while a getsourcelines re-resolve of the file we JUST wrote
        # can silently TRUNCATE: a broken (column-0) line in the saved buffer ends
        # inspect's block scan at the dedent with no exception, so the raise-guard
        # in resolve_address never fires - and the next save would splice the full
        # buffer into the short span, truncating the tail. Bumping the cached mtime
        # to the written value keeps the save-maintained address authoritative; a
        # GENUINE external write bumps mtime again and still re-resolves.
        ds = address._watcher_ds
        cached = getattr(ds, "_addr_cache", None) if ds is not None else None
        if cached is not None and cached[2] is address:
            try:
                ds._addr_cache = (cached[0], address.path.stat().st_mtime, address)
            except OSError:
                pass
        return False

# Register AFTER TypeCodec so EnumType re-matches here: an enum class (Mode,
# Toggles-style flag enums, etc) is an EnumMeta instance, so its MRO hits
# EnumType before type. Same render/load/save as any class - the subclass only
# claims the source render, so mode-backed entries (the inputs tab's mode
# column) are distinguishable from plain class source at a glance.
@register_codec(for_type=EnumType)
class ModeCodec(TypeCodec):
    name = "Enum / Mode"
    # Mode entry kwargs - the mode column of the inputs matrix: purple.
    render_kwargs = {"tint": (0.36, 0.16, 0.50, 0.50)}


@register_codec(for_type=types.FunctionType)

class FunctionCodec(TypeCodec):
    name = "Python Function"
    # The render function's own source (def + body): green.
    render_kwargs = {"tint": (0.04, 0.45, 0.12, 0.0)}

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        if isinstance(input_value, types.FunctionType):
            unwrapped = inspect.unwrap(input_value)
            try:
                source_file = inspect.getfile(unwrapped)
            except TypeError:
                return None

            # Refuse library source - we only ever edit this project's own code.
            if not _is_editable_source(source_file):
                return None

            if draw_state is not None:
                FileWatch.register_draw_state(draw_state, Path(source_file))
        else:
            return None

        try:
            mtime = Path(source_file).stat().st_mtime
        except OSError:
            mtime = None
        cached = getattr(draw_state, '_addr_cache', None)
        if cached is not None and cached[0] is input_value and cached[1] == mtime:
            return cached[2]

        # Same miss-reason timeline as TypeCodec.resolve_address above.
        _why = ("cold" if cached is None
                else "identity" if cached[0] is not input_value
                else f"mtime {cached[1]}->{mtime}")
        _ptrace_rl(("addr-resolve", id(draw_state)), f"addr re-resolve ({_why})",
                   target=getattr(input_value, '__name__', '?'))
        _evict_linecache(source_file)
        try:
            source_lines, start_lineno = inspect.getsourcelines(unwrapped)

        except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
            if draw_state._addr_cache is not None:
                return draw_state._addr_cache[2]

            print(f"[editable_source] could not resolve {getattr(input_value, '__name__', input_value)}: {e}")
            return None

        # The function may resolve to a DIFFERENT line than its cached address because
        # that's the legitimate sibling-shift case: editing another function in the
        # same file moved this one up/down, and codec.save already patched this
        # function's co_firstlineno (via shift_sibling_linenos) so getsourcelines
        # now returns the truthful new span. Trust it and re-cache the address;
        # rejecting it here would strand any OTHER editor window open on the same
        # file (they'd lose their reference the instant a sibling is edited).
        #
        # But an EXTERNAL edit shifts the file without patching anybody's lineno -
        # findsource then walks back from the stale lineno to whatever def-head
        # line precedes it and hands us a block that ISN'T this function (just the
        # wrong head). Verify the block matches us; if not, re-anchor by ast on the
        # current line (which also heals co_firstlineno), falling back to the LAST
        # good address when the file is mid-edit and won't parse.
        start0 = start_lineno - 1
        span_lines = source_lines
        if not _block_is_function(source_lines, unwrapped.__name__):
            span = _reanchor_function(unwrapped, source_file)
            if span is None:
                if cached is not None:
                    return cached[2]
                print(f"[editable_source] {getattr(input_value, '__name__', input_value)} "
                      f"not found at its recorded line and could not be re-anchored")
                return None
            start0, end0, span_lines = span
            print(f"[editable_source] re-anchored {unwrapped.__name__} to "
                  f"{Path(source_file).name}:{start0 + 1} after external edit")
        elif span_lines and span_lines[0].lstrip().startswith(("def ", "async def")):
            # A DEF-ANCHORED span (a span-recompile sets co_firstlineno to the
            # def line; a full-module compile sets it to the first decorator)
            # EXCLUDES the decorator lines entirely - the @render_func /
            # @defaults source then parses EMPTY and its inputint row shows
            # nothing (the function_dropdown missing-tint-source bug). Re-anchor
            # by ast, which returns the decorator-INCLUSIVE span and heals
            # co_firstlineno; a genuinely undecorated function re-anchors to the
            # wrong lines, so only accept the result when it actually gained a
            # decorator. Cached per (ref, mtime) like the rest of resolve.
            _dspan = _reanchor_function(unwrapped, source_file)
            if (_dspan is not None and _dspan[2]
                    and _dspan[2][0].lstrip().startswith("@")):
                start0, _dend0, span_lines = _dspan

        address = Address(Path(source_file), start0, start0 + len(span_lines),
                          source=input_value, watcher_ds=draw_state)
        address._span_fp = _span_fingerprint(span_lines)
        draw_state._addr_cache = (input_value, mtime, address)
        return address


@register_codec(for_type=CallSite)
class CallerCodec(TypeCodec):
    """Edit the call EXPRESSION at a CallSite — just `foo(...)`, not the statement
    around it.

    `resolve_address` ast-walks the file for the outermost Call covering the site's
    line (attaching its column span as `_call_cols` and the enclosing function as
    `.source`). `load`/`save` then slice out the bare call using those columns and
    keep the surrounding text (e.g. the `if `/`[0]:` of `if button(...)[0]:`, or the
    `return ` of `return foo()`) as prefix/suffix to splice an edit back between.

    Why the call expression and not the whole line (like FunctionCodec's def span):
    a statement HEADER (`if foo():`, `for x in foo():`) or a non-module-level
    statement is not independently parseable — wrapping it in a dummy function
    doesn't help (`if foo():` still needs a body). A bare call expression always
    parses, surfaces as an editable CallParse, and round-trips cleanly. The
    prefix/suffix are stable (the user edits only the call), so they stay valid even
    through half-typed states where the call's columns drift."""
    name = "Python Call Site"
    # Caller kwargs at the call site: the call is teal.
    render_kwargs = {"tint": (0.05, 0.14, 0.20, 0.60)}

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        if not isinstance(input_value, CallSite):
            return None
        filename, lineno = input_value.filename, input_value.lineno
        if filename is None or lineno is None:
            return None

        # Refuse library source - we only ever edit this project's own code.
        if not _is_editable_source(filename):
            return None

        path = Path(filename)
        if draw_state is not None:
            FileWatch.register_draw_state(draw_state, path)

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        # Cache by (filename, lineno, mtime) - a fresh CallSite is passed each frame,
        # so compare by VALUE, not identity (FunctionCodec can use `is` on the stable
        # function object; we don't). Re-resolves only when the file changes.
        cached = getattr(draw_state, '_addr_cache', None)
        if cached is not None and cached[0] == (filename, lineno) and cached[1] == mtime:
            return cached[2]

        _evict_linecache(str(path))
        # ast.walk + loop (in C, single-digit ms) finds the call span and attaches
        # the enclosing function as .source - see _resolve_call_address. NOT libcst's
        # PositionTracker, which is O(whole file) pure-Python and stalls the loop.
        address = _resolve_call_address((filename, lineno))

        # _resolve_call_address NEVER raises and never returns None - when ast.parse
        # fails (the file is mid-edit / syntactically invalid as the user types), it
        # returns a degenerate ONE-LINE fallback span with no `_call_cols`. Saving
        # the multi-line buffer into that 1-line span splices the extra lines IN,
        # appending a copy of the call's continuation lines on every keystroke-save.
        # So treat a missing `_call_cols` as "could not resolve" and keep the last
        # good address - its span matches what our last save wrote, so the next save
        # replaces in place. Mirrors FunctionCodec's getsourcelines-raised guard;
        # there, resolve error surfaces as an exception, here as a flag and span.
        resolved = isinstance(address, Address) and getattr(address, "_call_cols", None) is not None
        if not resolved:
            return cached[2] if cached is not None else None

        # _resolve_call_address sets the call's prefix/suffix on every valid resolve.
        # As a safety net (e.g. if its inner split ever fails), carry forward the
        # last good ones - they're stable (the user edits only the call, never the
        # `if `/`[0]:` around it), so they stay valid across re-resolves.
        if (not hasattr(address, "_call_prefix")
                and cached is not None and hasattr(cached[2], "_call_prefix")):
            address._call_prefix = cached[2]._call_prefix
            address._call_suffix = cached[2]._call_suffix

        address._watcher_ds = draw_state
        if draw_state is not None:
            draw_state._addr_cache = ((filename, lineno), mtime, address)
        return address

    @staticmethod
    def load(address, source_text=None, **kwargs):
        """Load just the call EXPRESSION (not the whole statement). Slice the bare
        call out of its line span using `_call_cols`, and stash the surrounding
        prefix/suffix on the address so save can splice an edit back between them.
        The bare call always parses; the statement around it may not."""
        text, newline = _source_and_newline(address, source_text)
        span_lines = text.split(newline)[address.start:address.end]
        # Conflict-guard baseline (see TypeCodec.save): the FULL span as loaded -
        # save rebuilds prefix + call + suffix, so it verifies at line level.
        address._span_fp = _span_fingerprint(span_lines)
        cols = getattr(address, "_call_cols", None)
        if cols is not None and span_lines:
            prefix, call_text, suffix = _split_span_at_call(span_lines, cols[0], cols[1], newline)
            address._call_prefix = prefix
            address._call_suffix = suffix
            return call_text
        # No column info (resolution was a line fallback) - no span, no splice.
        address._call_prefix = ""
        address._call_suffix = ""
        return newline.join(span_lines)

    @staticmethod
    def save(address, data, ensure_import=None, **kwargs):
        """Splice the edited call expression back between its stored prefix/suffix
        (captured by load), reconstructing the full statement, then write the whole
        span via TypeCodec.save (which handles the line splice + sibling shift)."""
        prefix = getattr(address, "_call_prefix", "")
        suffix = getattr(address, "_call_suffix", "")
        newline = _detect_newline(address.path.read_bytes())
        # Drop one trailing newline the editor / cst round-trip may append, so the
        # suffix doesn't get pushed onto a phantom next line.
        if data.endswith(newline):
            data = data[:-len(newline)]
        elif data.endswith("\n"):
            data = data[:-1]
        full = prefix + data + suffix
        return TypeCodec.save(address=address, data=full, ensure_import=ensure_import, **kwargs)


def _resolve_decoration_span(target):
    """((path, deco_start, deco_end), unwrapped) for the DECORATOR block above a
    class/function — 0-based [start, end) file lines covering only the `@...`
    lines, NOT the def/class or its body.

    ast-parses the dedented object source (decorators + def, always parseable) to
    get each decorator's precise line span, so a multi-line `@deco(\n ...\n)` is
    covered exactly. When the object has NO decorators, returns a zero-length span
    pinned to the def/class line, so a save splices a brand-new decorator in right
    above it (the "+ add" path). Raises on an unreadable/invalid file (caller keeps
    the last good address, mirroring FunctionCodec)."""
    unwrapped = inspect.unwrap(target) if isinstance(target, types.FunctionType) else target
    source_file = inspect.getfile(unwrapped)
    _evict_linecache(source_file)
    # getsourcelines starts at the FIRST decorator (1-based start_lineno) for a
    # decorated object, or at the def/class line when undecorated.
    source_lines, start_lineno = inspect.getsourcelines(unwrapped)
    tree = ast.parse(textwrap.dedent("".join(source_lines)))
    node = tree.body[0]  # the def/class - block coords are 1-based, line 1 = source_lines[0]
    decos = getattr(node, "decorator_list", [])
    base0 = start_lineno - 1  # file line (0-based) of source_lines[0]
    if decos:
        first = min(d.lineno for d in decos)
        last = max(getattr(d, "end_lineno", d.lineno) for d in decos)
        deco_start, deco_end = base0 + (first - 1), base0 + last
    else:
        pos = base0 + (node.lineno - 1)  # the def/class line - empty span above it
        deco_start = deco_end = pos
    return (Path(source_file), deco_start, deco_end), unwrapped


@register_codec(for_type=Decorations)
class DecorationsCodec(TypeCodec):
    """Edit the DECORATOR block above a class or function — just the `@...` lines.

    Like `CallerCodec`, only `resolve_address` differs from `TypeCodec`: it points
    the Address at the decorator lines instead of the whole def/class. `load`/`save`
    are inherited and operate on that line span as text (the decorators are full
    lines, so no column splicing is needed — unlike a call embedded in a larger
    statement). `.source` is the wrapped target object, so the inherited save shifts
    siblings correctly; recompile re-runs the decorators by rebuilding the whole
    object (`_recompile_decorations`).

    An undecorated target resolves to a zero-length span pinned above its def/class
    line: load returns "", and saving typed text splices a fresh decorator in."""
    name = "Python Decorations"
    # Decorator blocks (@window / @render_func / @defaults): lighter blue.
    render_kwargs = {"tint": (0.20, 0.42, 0.75, 0.50)}

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        if not isinstance(input_value, Decorations):
            return None
        # A runtime-generated bubbling subclass has no source; resolve its base.
        target = base_of_bubbling(input_value.target)
        if not isinstance(target, (type, types.FunctionType)):
            return None
        try:
            source_file = inspect.getfile(
                inspect.unwrap(target) if isinstance(target, types.FunctionType) else target)
        except TypeError:
            return None
        # Refuse library source - we only ever edit this project's own code.
        if not _is_editable_source(source_file):
            return None

        path = Path(source_file)
        if draw_state is not None:
            FileWatch.register_draw_state(draw_state, path)

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        # Cache by (target, mtime): a fresh Decorations wraps the SAME stable target
        # each frame, so compare the target by identity (like FunctionCodec). Re-
        # resolves only if the file changes.
        cached = getattr(draw_state, "_addr_cache", None)
        if cached is not None and cached[0] is target and cached[1] == mtime:
            return cached[2]

        try:
            (p, start, end), _unwrapped = _resolve_decoration_span(target)
        except (OSError, TypeError, tokenize.TokenError, SyntaxError, ValueError) as e:
            # File mid-edit / unreadable - keep the last good address so a save
            # replaces in-place rather than writing to a bad span. Mirrors
            # FunctionCodec's getsourcelines fallback.
            if cached is not None:
                return cached[2]
            print(f"[DecorationsCodec] could not resolve {getattr(target, '__name__', target)}: {e}")
            return None

        address = Address(p, start, end, source=target, watcher_ds=draw_state)
        if draw_state is not None:
            draw_state._addr_cache = (target, mtime, address)
        return address


@register_codec(for_type=types.ModuleType)
class ModuleCodec(TypeCodec):
    name = "Python Module"
    # No source highlighting yet (don't inherit TypeCodec's class blue tint).
    render_kwargs = {}

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        if not isinstance(input_value, types.ModuleType):
            return None
        source_file = Path(input_value.__file__)
        # Refuse library source - we only ever edit this project's own code.
        if not _is_editable_source(source_file):
            return None
        if draw_state is not None:
            FileWatch.register_draw_state(draw_state, source_file)
        return Address(source_file, source=input_value, watcher_ds=draw_state)


@register_codec(ext=(".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml",
                     ".sh", ".cfg", ".ini", ".glsl", ".frag", ".vert"))
class TextFileCodec(TypeCodec):
    """Whole-file editing for a plain text Path — the no-span Address case.

    A spanless Address (start/end None) already means "the whole file" to the
    inherited TypeCodec load/save, so this codec is just address resolution:
    point at the file, register the watcher, cache by mtime."""
    name = "Text File"

    @staticmethod
    def show_code_buttons(address):
        # One codec serves any text extension - only .py is runnable/importable.
        return address is not None and address.path.suffix.lower() == ".py"

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        path = Path(str(input_value))
        # Whole-file text editing follows the gentler gate: folder windows
        # mount dirs outside the project, so anywhere under $HOME works (for
        # library installs) - not just the project tree.
        if not is_writable_file(path) or not path.is_file():
            return None
        if draw_state is not None:
            FileWatch.register_draw_state(draw_state, path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        cached = getattr(draw_state, '_addr_cache', None)
        if cached is not None and cached[0] == input_value and cached[1] == mtime:
            return cached[2]
        address = Address(path, source=input_value, watcher_ds=draw_state)
        address._allow_write = True   # resolved via is_writable_file - see save() guard
        if draw_state is not None:
            draw_state._addr_cache = (input_value, mtime, address)
        return address


def _resolve_plain_file(input_value, draw_state, **kwargs):
    """Shared resolve for read-only plain-file codecs (images, binaries):
    is_writable_file gate, file watch, (input, mtime) address cache — the same
    shape as TextFileCodec.resolve_address."""
    path = Path(str(input_value))
    if not is_writable_file(path) or not path.is_file():
        return None
    if draw_state is not None:
        FileWatch.register_draw_state(draw_state, path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cached = getattr(draw_state, '_addr_cache', None)
    if cached is not None and cached[0] == input_value and cached[1] == mtime:
        return cached[2]
    address = Address(path, source=input_value, watcher_ds=draw_state)
    if draw_state is not None:
        draw_state._addr_cache = (input_value, mtime, address)
    return address


@register_codec(ext=(".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tga"))
class ImageCodec(Codec):
    """Image Path → PendingTexture. load() runs on code_file_io's background
    thread, so it only DECODES (PIL, safe off-thread) and returns a
    PendingTexture; core_render's wrapper calls pending_upload() on the GL
    thread the first frame it renders, and draw_pending_texture (the type's
    default renderer) draws the uploaded texture with zoom/pan. Read-only
    (editable=False): view edits never dirty the host and save() refuses."""

    name = "Image"
    # PendingTexture has a default renderer (draw_pending_texture is
    # register_default_for_type), so no draw_func: type routing finds it.
    editable = False
    icon = "\uf03e"   # FA image
    resolve_address = staticmethod(_resolve_plain_file)

    # claims() runs in the render thread (codec_for_path is re-ried ever
    # frame), so the PIL header probe is memoized per path and only re-runs
    # when mtime or size moves - the same staleness key resolve_address uses.
    _claims_cache = {}

    @classmethod
    def claims(cls, path):
        try:
            s = path.stat()
        except OSError:
            # Missing/unreadable: accept and let resolve_address return None -
            # claims veto is only about content, not existence.
            return True
        cached = cls._claims_cache.get(str(path))
        if cached is not None and cached[:2] == (s.st_mtime, s.st_size):
            return cached[2]
        try:
            # Lazy open parses just the header - cheap, no pixel decode.
            with Image.open(path):
                ok = True
        except Exception:
            ok = False
        cls._claims_cache[str(path)] = (s.st_mtime, s.st_size, ok)
        return ok

    @staticmethod
    def load(address, **kwargs):
        path = address.path
        key = str(path)
        raw = path.read_bytes()

        image = Image.open(io.BytesIO(raw))
        if image.mode == "P":
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        elif image.mode == "1":
            image = image.convert("L")
        elif image.mode not in PIL_TO_GL_FORMAT:
            image = image.convert("RGBA")
        image = image.transpose(Image.FLIP_TOP_BOTTOM)

        width, height = image.size
        pending = PendingTexture(name=key, tex_width=width, tex_height=height,
                                 gl_format=PIL_TO_GL_FORMAT[image.mode],
                                 data=image.tobytes())
        # Registers with the manager (dedupes against an already-uploaded
        # texture for this path); the GL upload itself happens on the render
        # thread via the wrapper's pending_upload() hook.
        Core.melty.texture_manager.put_pending(key, pending)
        return pending

    @staticmethod
    def save(*args, **kwargs):
        return False


class BinaryFileCodec(Codec):
    """Read-only fallback for files no other codec claims (unknown extension /
    no extension, content sniffed as binary): show metadata and a short hex
    preview instead of "No codec". Reached via codec_for_path, never the
    extension registry. save() refuses — the summary is a VIEW of the bytes,
    and writing it back would replace the file with its own hexdump."""

    name = "Binary File"
    # The summary is a str, so the caller's text view renders it - but it is
    # a VIEW of the bytes, never written back.
    editable = False
    resolve_address = staticmethod(_resolve_plain_file)

    PREVIEW_BYTES = 256

    @staticmethod
    def load(address, **kwargs):
        path = address.path
        size = path.stat().st_size
        kind, _ = mimetypes.guess_type(path.name)
        head = path.open("rb").read(BinaryFileCodec.PREVIEW_BYTES)

        lines = [f"{path.name}  —  {size:,} bytes  ({kind or 'unknown type'})", ""]
        for off in range(0, len(head), 16):
            row = head[off:off + 16]
            hx = " ".join(f"{b:02x}" for b in row)
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append(f"{off:08x}  {hx:<47}  {ascii_}")
        if size > len(head):
            lines.append(f"… {size - len(head):,} more bytes")
        return "\n".join(lines)

    @staticmethod
    def save(*args, **kwargs):
        return False


def codec_for_path(path):
    """Every real file resolves to SOME codec: registered extension first,
    else sniff the head — NUL-free utf-8 edits as text (TextFileCodec),
    anything else gets the read-only binary summary. This is what lets the
    folder windows mount a stress-test directory full of extensionless blobs
    without a wall of "No codec" rows.

    The extension match is subject to the codec's content veto (claims): a
    file whose bytes don't decode as the extension promises (an empty or
    corrupt .png) falls through to the sniff instead of being routed to a
    load() that can only throw."""
    if isinstance(path, str) and len(path) > 255:
        return None

    codec = extension_to_codec.get(path.suffix.lower())
    if codec is not None and codec.claims(path):
        return codec
    if not path.is_file():
        return None
    try:
        head = path.open("rb").read(4096)
    except OSError:
        return None
    if b"\0" in head:
        return BinaryFileCodec
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte char split at the 4096 boundary technically lands here -
        # acceptable: the file still renders, just as the binary summary.
        return BinaryFileCodec
    return TextFileCodec


def asset_extensions():
    """Registered extensions whose codec loads something OTHER than editor
    text — images today, a .npy codec tomorrow. This is the non-Python file
    set global search's Code tab lists beside the loaded modules: register
    a codec with `ext=` and its files become searchable/openable, nothing
    else to wire. Lowercase, dotted (".png"), as the registry stores them."""
    return {ext for ext, codec in extension_to_codec.items()
            if codec is not TextFileCodec}
