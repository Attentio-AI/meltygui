import ast
import inspect
import textwrap
import tokenize
import types
from dataclasses import dataclass
from enum import EnumType
from pathlib import Path

from src.lsd.gl_gui.view.core_conversion.address import Address, _evict_linecache, shift_sibling_linenos, is_editable_source
from src.lsd.gl_gui.view.core_conversion.chain_converters import (
    _ensure_import_lines, _resolve_call_address, _split_span_at_call)
from src.lsd.gl_gui.view.core_conversion.bubbling import base_of_bubbling
from src.lsd.gl_gui.view.core_conversion.file_converters import _detect_newline
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core

from src.lsd.gl_gui.melty import Melty, FileWatch
from src.shader_library.shader_manager.texture_manager import PIL_TO_GL_FORMAT, GL_TO_PIL_MODE, PendingTexture

GL_TO_PIL_MODE = {v: k for k, v in PIL_TO_GL_FORMAT.items()}

from OpenGL.GL import (
    glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
    GL_TEXTURE_2D, GL_UNSIGNED_BYTE,
    GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER, GL_LINEAR,
    GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE,
)

from PIL import Image
import io

NO_DATA = object()

extension_to_codec = {}
type_to_codec = {}

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


def register_codec(cls=None, **kwargs):
    def wrap(cls):
        if "ext" in kwargs:
            for ext in kwargs["ext"]:
                extension_to_codec[ext] = cls

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
    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        return NO_DATA

    @staticmethod
    def load(path, **kwargs):
        return NO_DATA

    @staticmethod
    def save(data, file_path, **kwargs):
        return False


@register_codec(for_type=(type, EnumType))
class TypeCodec(Codec):
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
        draw_state._addr_cache = (input_value, mtime, address)
        return address


    @staticmethod
    def load(address, **kwargs):
        data = address.path.read_bytes()
        newline = _detect_newline(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        lines = text.split(newline)
        return newline.join(lines[address.start:address.end])


    @staticmethod
    def save(address, data, ensure_import=None, **kwargs):
        """Write code_str back into the file at the Address's span — the synchronous
            body of the old @render_func(background=True) _do_save, minus the Pending.

            ensure_import=(module, name) inserts a missing import in the SAME write so a
            synthesized decorator (e.g. @defaults) resolves. Updates the Address span in
            place and shifts siblings so this frame's Address stays valid; siblings heal
            on the next mtime-driven re-resolve."""
        # Defense in depth: never write to library source even if an Address
        # somehow points outside the project (resolve_address should already have
        # refused it). A bad span splice bug once corrupted libcst's own source.
        if not _is_editable_source(address.path):
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
        if old_start is None:  # whole-file (module) address
            lines = new_lines
        else:
            lines[old_start:old_end] = new_lines

        inserted = 0
        insert_idx = None
        if ensure_import is not None:
            lines, inserted, insert_idx = _ensure_import_lines(lines, ensure_import[0], ensure_import[1])

        final_text = newline.join(lines)
        # Stamp a watch hash BEFORE writing so our own write isn't read back as a
        # stale external change.
        FileWatch.set_hash_from_content(address.path, final_text, draw_state=address._watcher_ds)
        address.path.write_text(final_text, encoding="utf-8")

        if old_start is not None:
            resolved_old_end = old_end if old_end is not None else old_start + len(new_lines)
            new_end = old_start + len(new_lines)
            delta = new_end - resolved_old_end
            address.end = new_end
            if inserted:  # import landed above our span
                address.start = old_start + inserted
                address.end = new_end + inserted
            address._hash = address._compute_hash()
            # Shift siblings below for the body span change (original coords)...
            shift_sibling_linenos(address.source, address.path,
                                  after_lineno=resolved_old_end, delta=delta)
            # ...then the import insert moved our def AND everything below it down,
            # so shift the saved source too (include_saved) - else its own
            # co_firstlineno goes stale and the next resolve walks back to line 0.
            if inserted and insert_idx is not None:
                shift_sibling_linenos(address.source, address.path,
                                      after_lineno=insert_idx, delta=inserted,
                                      include_saved=True)
        else:
            address._hash = address._compute_hash()
        return False

@register_codec(for_type=types.FunctionType)
class FunctionCodec(TypeCodec):
    name = "Python Function"

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
        address = Address(Path(source_file), start_lineno - 1,
                          start_lineno - 1 + len(source_lines),
                          source=input_value, watcher_ds=draw_state)
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
    def load(address, **kwargs):
        """Load just the call EXPRESSION (not the whole statement). Slice the bare
        call out of its line span using `_call_cols`, and stash the surrounding
        prefix/suffix on the address so save can splice an edit back between them.
        The bare call always parses; the statement around it may not."""
        data = address.path.read_bytes()
        newline = _detect_newline(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        span_lines = text.split(newline)[address.start:address.end]
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


@register_codec(ext="png")
class PngCodec(Codec):

    name = "PNG Image"

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        return Address(input_value)

    @staticmethod
    def load(input_value, **kwargs):
        """
        Load PNG bytes. Returns texture ID if on GL thread and cached,
        otherwise returns PendingTexture for deferred upload.
        """
        path = input_value
        # Check cache first
        if path:
            cached = Core.melty.texture_manager.get(path)  # get also finalizes pending
            if cached is not None:
                Core.melty.texture_manager.acquire(path)
                return cached

        """
        Read raw bytes for a file, honoring max_file_size placeholders.
        Returns bytes, max_size_placeholder, or None on error.
        """
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            raw = None

        # Decode image (safe on any thread)
        image = Image.open(io.BytesIO(raw))

        if image.mode == "P":
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        elif image.mode == "1":
            image = image.convert("L")
        elif image.mode not in PIL_TO_GL_FORMAT:
            image = image.convert("RGBA")

        image = image.transpose(Image.FLIP_TOP_BOTTOM)

        width, height = image.size
        image_data = image.tobytes()

        gl_format = PIL_TO_GL_FORMAT[image.mode]

        texture_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, texture_id)
        glTexImage2D(
            GL_TEXTURE_2D, 0, gl_format,
            width,height, 0,
            gl_format, GL_UNSIGNED_BYTE, image_data
        )
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)

        glBindTexture(GL_TEXTURE_2D, 0)
        return texture_id

    @staticmethod
    def save(data, file_path, **kwargs):
        if hasattr(data, "save"):
            data.save(file_path)
            return True
        return False