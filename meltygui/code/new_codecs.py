import inspect
import tokenize
import types
from pathlib import Path

from src.lsd.gl_gui.view.core_conversion.address import Address, _evict_linecache, shift_sibling_linenos
from src.lsd.gl_gui.view.core_conversion.chain_converters import _ensure_import_lines
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

def register_codec(cls=None, **kwargs):
    def wrap(cls):
        if "ext" in kwargs:
            for ext in kwargs["ext"]:
                extension_to_codec[ext] = cls

        if "for_type" in kwargs:
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


@register_codec(for_type=type)
class TypeCodec(Codec):
    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        if isinstance(input_value, type) and input_value.__module__ not in ('builtins', '_collections_abc'):
            unwrapped = input_value
            try:
                source_file = inspect.getfile(unwrapped)
            except TypeError:
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

        address = Address(Path(source_file), start_lineno - 1,
                          start_lineno - 1 + len(source_lines),
                          source=input_value, watcher_ds=draw_state)
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
        full = address.path.read_bytes()
        newline = _detect_newline(full)
        try:
            text = full.decode("utf-8")
        except UnicodeDecodeError:
            text = full.decode("latin-1")
        lines = text.split(newline)
        new_lines = data.split(newline)

        old_start, old_end = address.start, address.end
        if old_start is None:  # whole-file (module) address
            lines = new_lines
        else:
            lines[old_start:old_end] = new_lines

        inserted = 0
        if ensure_import is not None:
            lines, inserted = _ensure_import_lines(lines, ensure_import[0], ensure_import[1])

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
            shift_sibling_linenos(address.source, address.path,
                                  after_lineno=resolved_old_end, delta=delta)
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

        address = Address(Path(source_file), start_lineno - 1,
                          start_lineno - 1 + len(source_lines),
                          source=input_value, watcher_ds=draw_state)
        draw_state._addr_cache = (input_value, mtime, address)
        return address


@register_codec(for_type=types.ModuleType)
class ModuleCodec(TypeCodec):
    name = "Python Module"

    @staticmethod
    def resolve_address(input_value, draw_state=None, **kwargs):
        if not isinstance(input_value, types.ModuleType):
            return None
        source_file = Path(input_value.__file__)
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