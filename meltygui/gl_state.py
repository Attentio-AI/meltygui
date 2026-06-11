"""GL resource lifecycle for Melty views.

GLState is the auto-injected per-view owner of GL objects (shader programs,
FBOs, textures, buffers, VAOs). Declare `gl_state: GLState` in a @render_func
signature and the injection machinery (set_default in core_render) creates one
in draw_state.misc and hands the SAME instance back every frame — exactly like
`code_state: CodeState`. The injector also stamps `_owner_ds`, which is what
ties the instance to Melty's window lifecycle.

Every resource goes through `get(key, create, delete, deps)`:

    fb = gl_state.get("fbo", create=make_fbo, delete=del_fbo, deps=(w, h))

- same key + same deps        → cached value, no GL calls
- deps changed                → create() the replacement FIRST, then queue the
                                old one for deletion. If create() raises, the
                                OLD resource is kept (last-good semantics —
                                this is what lets a broken shader edit fall
                                back to the previous program).
- deps must be plain comparable values (tuples/strs/ints — no arrays).

Deletion never happens inline: deleters are queued and drained once per frame
by `flush_deletes()` (called from Melty.end_frame on the render thread with
the context current), because `release()` / `__del__` can fire from any
thread and GL calls off the main thread are invalid.

Lifecycle events (wired in melty.py):
- window delete  → GLState.on_window_deleted(window_ds): every live state
  whose owner draw_state sits under that window releases its resources. The
  draw_state (and the GLState in its misc) persists, so a re-created window
  lazily re-allocates on the next render.
- app shutdown   → GLState.shutdown_all()
- GC             → __del__ queues anything not yet released (safety net for
  draw_states that get pruned without a window-delete event).

The registries below survive hotswap: recompile execs into the existing
module dict, so re-running this module reuses the live containers instead of
orphaning queued deletions and tracked states.
"""

import threading
import weakref

import numpy as np
import OpenGL.GL as gl
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


def _persistent(name, factory):
    """Reuse a module global across hotswap re-exec (recompile execs into the
    existing module.__dict__), so live registries survive code reloads."""
    val = globals().get(name)
    return val if val is not None else factory()


_live_states = _persistent("_live_states", weakref.WeakSet)
_delete_queue = _persistent("_delete_queue", list)   # [(key, value, deleter)]
_queue_lock = _persistent("_queue_lock", threading.RLock)

# The thread that owns the GL context. NOT the Python main thread - the studio
# renders on its visualization thread while the main thread runs the chat
# loop. The render loop claims it every frame (end_frame → flush_deletes);
# before the first claim, whoever does GL work first is assumed to be it.
_gl_thread = _persistent("_gl_thread", lambda: None)


def is_gl_thread():
    """True on the GL-context thread (claiming it if nobody has yet). Guards
    every code path that would issue GL calls from converter/Background
    threads."""
    global _gl_thread
    if _gl_thread is None:
        _gl_thread = threading.current_thread()
        return True
    return threading.current_thread() is _gl_thread


def _scalar(x):
    """PyOpenGL scalar gets sometimes come back as length-1 arrays."""
    return int(x[0]) if hasattr(x, "__len__") else int(x)


class tight_unpack:
    """Save → canonical tight-row GL_UNPACK_* state → restore, around texture
    uploads. Leftover pitch state (ROW_LENGTH/SKIP_* from any other GL code in
    the process) shears our rows — the classic "every other row missing"
    corruption — and our ALIGNMENT=1 must not leak out either."""

    _PNAMES = ("GL_UNPACK_ALIGNMENT", "GL_UNPACK_ROW_LENGTH", "GL_UNPACK_IMAGE_HEIGHT",
               "GL_UNPACK_SKIP_ROWS", "GL_UNPACK_SKIP_PIXELS", "GL_UNPACK_SKIP_IMAGES")
    _CANON = (1, 0, 0, 0, 0, 0)

    def __enter__(self):
        self._saved = []
        for name, canon in zip(self._PNAMES, self._CANON):
            pname = getattr(gl, name)
            self._saved.append((pname, _scalar(gl.glGetIntegerv(pname))))
            gl.glPixelStorei(pname, canon)
        return self

    def __exit__(self, *exc):
        for pname, value in self._saved:
            gl.glPixelStorei(pname, value)
        return False


class GLTexture:
    """A GL texture handle plus the metadata shader_func needs to bind it:
    `target` decides sampler2D vs sampler3D at uniform-injection time."""

    def __init__(self, texture_id, target, shape=(), internal_format=0):
        self.texture_id = int(texture_id)
        self.target = int(target)
        self.shape = tuple(shape)
        self.internal_format = int(internal_format)

    def __repr__(self):
        kind = {gl.GL_TEXTURE_1D: "1d", gl.GL_TEXTURE_2D: "2d",
                gl.GL_TEXTURE_3D: "3d"}.get(self.target, hex(self.target))
        return f"GLTexture({kind} id={self.texture_id} shape={self.shape})"


class FBO:
    """Offscreen render target (color RGBA8 + depth 24). Context manager:
    `with fb:` binds the FBO and sets the viewport, restoring both on exit —
    the save/restore dance every render-to-texture pass needs inside an
    imgui frame."""

    def __init__(self, fbo, color: GLTexture, depth: GLTexture, width, height):
        self.fbo = int(fbo)
        self.color = color
        self.depth = depth
        self.width = int(width)
        self.height = int(height)
        self._prev_fbo = 0
        self._prev_viewport = None

    @property
    def texture_id(self):
        return self.color.texture_id

    def __enter__(self):
        self._prev_fbo = _scalar(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        self._prev_viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, self.width, self.height)
        return self

    def __exit__(self, *exc):
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._prev_fbo)
        pv = self._prev_viewport
        if pv is not None:
            gl.glViewport(int(pv[0]), int(pv[1]), int(pv[2]), int(pv[3]))
        return False

    def __repr__(self):
        return f"FBO(id={self.fbo} {self.width}x{self.height})"


class _Resource:
    __slots__ = ("value", "deleter", "deps")

    def __init__(self, value, deleter, deps):
        self.value = value
        self.deleter = deleter
        self.deps = deps


class GLState:

    # Stamped by core_render's set_default when the state is created into
    # draw_state.misc - the owning draw_state, whose parent_window chain is
    # how on_window_deleted decides when to release.
    _owner_ds = None

    def __init__(self):
        self._resources = {}
        _live_states.add(self)

    # ── core ────────────────────────────────────────────────────────────

    def get(self, key, create, delete=None, deps=None):
        """The cached resource for `key`, (re)created when `deps` differ from
        the cached generation. On create() failure the previous resource (and
        its deps) survive untouched and the exception propagates — callers
        that want last-good fallback catch it and use peek()."""
        rec = self._resources.get(key)
        if rec is not None and rec.deps == deps:
            return rec.value
        value = create()
        if rec is not None:
            self._queue(key, rec)
        self._resources[key] = _Resource(value, delete, deps)
        return value

    def peek(self, key):
        rec = self._resources.get(key)
        return rec.value if rec is not None else None

    def drop(self, key):
        """Queue one resource for deletion (forces re-create on next get)."""
        rec = self._resources.pop(key, None)
        if rec is not None:
            self._queue(key, rec)
        return rec is not None

    def release(self):
        """Queue every resource for deletion. The instance stays usable — a
        later get() simply re-allocates (closed windows can re-open)."""
        with _queue_lock:
            for key, rec in self._resources.items():
                if rec.deleter is not None:
                    _delete_queue.append((key, rec.value, rec.deleter))
        self._resources.clear()

    def __del__(self):
        # Any thread, any time (GC) - only queues, never touches GL.
        try:
            self.release()
        except Exception:
            pass

    @staticmethod
    def _queue(key, rec):
        if rec.deleter is None:
            return
        with _queue_lock:
            _delete_queue.append((key, rec.value, rec.deleter))

    # ── lifecycle hooks (called from pty.py) ──────────────────────────

    @staticmethod
    def flush_deletes():
        """Drain queued deleters. GL-thread only — GL calls are invalid
        elsewhere, so off-thread calls are a silent no-op (the queue keeps
        everything until a frame can run it)."""
        if not is_gl_thread():
            return 0
        n = 0
        while True:
            with _queue_lock:
                if not _delete_queue:
                    break
                key, value, deleter = _delete_queue.pop()
            try:
                deleter(value)
            except Exception as e:
                print(f"[gl_state] delete failed for {key!r}: {e}")
            n += 1
        return n

    @classmethod
    def on_window_deleted(cls, window_ds):
        """Release every live state owned by a draw_state under the deleted
        window (walking parent_window chains, which terminate at a top-level
        window or self-loop)."""
        if window_ds is None:
            return
        for state in list(_live_states):
            node = state._owner_ds
            hops = 0
            while node is not None and hops < 64:
                if node is window_ds:
                    state.release()
                    break
                nxt = getattr(node, "parent_window", None)
                if nxt is None or nxt is node:
                    break
                node = nxt
                hops += 1

    @classmethod
    def shutdown_all(cls):
        for state in list(_live_states):
            state.release()
        return cls.flush_deletes()

    @classmethod
    def stats(cls):
        states = list(_live_states)
        with _queue_lock:
            queued = len(_delete_queue)
        return {
            "states": len(states),
            "resources": sum(len(s._resources) for s in states),
            "queued_deletes": queued,
        }

    def __repr__(self):
        return f"GLState({len(self._resources)} resources: {sorted(map(str, self._resources))})"

    # ── conveniences ────────────────────────────────────────────────────

    def fbo(self, key, width, height):
        """Offscreen target, re-created on resize. RGBA8 color + 24-bit depth
        textures, matching the volume renderer's original FBO setup."""
        width, height = max(1, int(width)), max(1, int(height))

        def create():
            fbo = _scalar(gl.glGenFramebuffers(1))
            color_id = _scalar(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, color_id)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            depth_id = _scalar(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, depth_id)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_DEPTH_COMPONENT24, width, height, 0,
                            gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, None)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

            prev = _scalar(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, color_id, 0)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                                      gl.GL_TEXTURE_2D, depth_id, 0)
            status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, prev)
            if status != gl.GL_FRAMEBUFFER_COMPLETE:
                gl.glDeleteFramebuffers(1, [fbo])
                gl.glDeleteTextures([color_id, depth_id])
                raise RuntimeError(f"FBO incomplete: {hex(status)} ({width}x{height})")
            color = GLTexture(color_id, gl.GL_TEXTURE_2D, (height, width), gl.GL_RGBA8)
            depth = GLTexture(depth_id, gl.GL_TEXTURE_2D, (height, width), gl.GL_DEPTH_COMPONENT24)
            return FBO(fbo, color, depth, width, height)

        def delete(fb):
            gl.glDeleteFramebuffers(1, [fb.fbo])
            gl.glDeleteTextures([fb.color.texture_id, fb.depth.texture_id])

        return self.get(key, create, delete, deps=(width, height))

    def texture3d(self, key, data, nearest=True, version=None):
        """3D single-channel texture from a numpy array shaped (depth, height,
        width). The internal format strictly pairs with the data dtype:
        float16 → GL_R16F + GL_HALF_FLOAT, anything else converts to
        float32 → GL_R32F + GL_FLOAT. Allocation is NULL-pointer + the data
        staged through a PIXEL_UNPACK PBO — the proven upload recipe (direct
        client-memory TexImage was a historical source of row corruption).
        Pass `version` (any comparable token — a counter, id(tensor)) to
        force re-upload when the same-shaped data changes."""
        if data.dtype == np.float16:
            internal, gl_type = gl.GL_R16F, gl.GL_HALF_FLOAT
        else:
            internal, gl_type = gl.GL_R32F, gl.GL_FLOAT
            if data.dtype != np.float32:
                data = data.astype(np.float32)
        depth, height, width = (int(s) for s in data.shape)
        filt = gl.GL_NEAREST if nearest else gl.GL_LINEAR

        def create():
            import ctypes
            tex = _scalar(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_3D, tex)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_MIN_FILTER, filt)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_MAG_FILTER, filt)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
            payload = np.ascontiguousarray(data)
            with tight_unpack():
                # Allocate only - data arrives via the PBO below.
                gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, internal, width, height, depth, 0,
                                gl.GL_RED, gl_type, ctypes.c_void_p(0))
                pbo = _scalar(gl.glGenBuffers(1))
                gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, pbo)
                gl.glBufferData(gl.GL_PIXEL_UNPACK_BUFFER, payload.nbytes, payload,
                                gl.GL_STREAM_DRAW)
                gl.glTexSubImage3D(gl.GL_TEXTURE_3D, 0, 0, 0, 0, width, height, depth,
                                   gl.GL_RED, gl_type, None)
                gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
                # This defers the actual delete until the transfer completes.
                gl.glDeleteBuffers(1, [pbo])
            gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
            return GLTexture(tex, gl.GL_TEXTURE_3D, (depth, height, width), internal)

        def delete(tex):
            gl.glDeleteTextures([tex.texture_id])

        deps = ((depth, height, width), str(data.dtype), nearest, version)
        return self.get(key, create, delete, deps=deps)

    def texture1d(self, key, data, nearest=False, version=None):
        """1-D RGB texture from a flat [r,g,b, r,g,b, ...] float list (the LUT
        shape — anything reshapable to (n, 3) works). Linear-filtered and
        edge-clamped by default so a color ramp samples smoothly. The payload
        is tiny, so it uploads via plain client-memory TexImage1D — no PBO
        staging needed. Pass `version` (any comparable token, e.g. a content
        hash) to force re-upload when same-length data changes."""
        payload = np.ascontiguousarray(np.asarray(data, dtype=np.float32).reshape(-1, 3))
        n = int(payload.shape[0])
        filt = gl.GL_NEAREST if nearest else gl.GL_LINEAR

        def create():
            tex = _scalar(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_1D, tex)
            gl.glTexParameteri(gl.GL_TEXTURE_1D, gl.GL_TEXTURE_MIN_FILTER, filt)
            gl.glTexParameteri(gl.GL_TEXTURE_1D, gl.GL_TEXTURE_MAG_FILTER, filt)
            gl.glTexParameteri(gl.GL_TEXTURE_1D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            with tight_unpack():
                gl.glTexImage1D(gl.GL_TEXTURE_1D, 0, gl.GL_RGB32F, n, 0,
                                gl.GL_RGB, gl.GL_FLOAT, payload)
            gl.glBindTexture(gl.GL_TEXTURE_1D, 0)
            return GLTexture(tex, gl.GL_TEXTURE_1D, (n,), gl.GL_RGB32F)

        def delete(tex):
            gl.glDeleteTextures([tex.texture_id])

        return self.get(key, create, delete, deps=(n, nearest, version))

    def vao(self, key, build=None, deps=None):
        """A vertex array. `build()` runs once with the fresh VAO bound — it
        sets up VBOs/attribs and returns the buffer ids it generated so they
        get deleted with the VAO. `build=None` gives an empty VAO (core
        profile requires one bound even for attribute-less fullscreen
        triangles)."""

        def create():
            vao = _scalar(gl.glGenVertexArrays(1))
            gl.glBindVertexArray(vao)
            bufs = tuple(int(b) for b in (build() or ())) if build is not None else ()
            gl.glBindVertexArray(0)
            return (vao, bufs)

        def delete(value):
            vao, bufs = value
            for b in bufs:
                gl.glDeleteBuffers(1, [b])
            gl.glDeleteVertexArrays(1, [vao])

        return self.get(key, create, delete, deps=deps)[0]

    def buffer(self, key, data=None, nbytes=None, target=gl.GL_ARRAY_BUFFER,
               usage=gl.GL_DYNAMIC_DRAW, version=None):
        """A plain GL buffer, sized from `data` (numpy) or `nbytes`. This is
        the allocation the CUDA-interop step will register against."""
        if data is not None:
            data = np.ascontiguousarray(data)
            nbytes = data.nbytes

        def create():
            buf = _scalar(gl.glGenBuffers(1))
            gl.glBindBuffer(target, buf)
            gl.glBufferData(target, int(nbytes), data, usage)
            gl.glBindBuffer(target, 0)
            return buf

        def delete(buf):
            gl.glDeleteBuffers(1, [buf])

        return self.get(key, create, delete, deps=(int(nbytes), int(target), version))
