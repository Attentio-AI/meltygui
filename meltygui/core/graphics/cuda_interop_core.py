"""CUDA context selection and registered-buffer operations for OpenGL interop.

The rendering context determines the device. Never register GL resources from an
unrelated CUDA device, even when Torch's current device happens to be that device.
Tensor adaptation and texture ownership live in model/cuda_texture_model.py.
"""
from contextlib import contextmanager
import ctypes

from meltygui.core.graphics import cuda_context_core
from meltygui.core.graphics.gl_state import (
    ResourceDeletionDeferred, current_context, is_gl_thread,
)


def log_once(message):
    state = cuda_context_core.runtime()
    if message != state.last_logged:
        print(f"[cuda_interop] {message}")
        state.last_logged = message


def gl_devices():
    """CUDA ordinals associated with the current GL context, or none.

    PyCUDA does not expose cuGLGetDevices; use the CUDA driver's public API.
    The versioned symbol and enum values are declared in CUDA's cudaGL.h.
    """
    if not is_gl_thread() or current_context() is None:
        return ()
    import meltygui_pycuda.driver as cuda
    cuda.init()
    capacity = cuda.Device.count()
    devices = (ctypes.c_int * capacity)()
    count = ctypes.c_uint()
    # CU_GL_DEVICE_LIST_ALL: all GPUs used by the current GL context.
    result = cuda_context_core._driver_library().cuGLGetDevices_v2(ctypes.byref(count), devices, capacity, 1)
    if result != 0:
        return ()
    return tuple(devices[:count.value])


def cuda_ready():
    """Whether GL interop is safe in the current thread and context pair."""
    if not is_gl_thread() or current_context() is None:
        return False
    try:
        import meltygui_pycuda.driver as cuda
        import meltygui_pycuda.gl
        context = cuda.Context.get_current()
        if context is None or context.handle != cuda_context_core._native_context():
            return False
        device = cuda_context_core.current_device_index()
        return device is not None and device in gl_devices()
    except Exception:
        return False


def ensure_context():
    """For standalone/test setup, retain the GL device's primary context.

    An existing compatible context is left alone. An incompatible one is
    rejected, not silently replaced. Only the context retained here is detached.
    """
    if not is_gl_thread() or current_context() is None:
        return False
    try:
        import meltygui_pycuda.driver as cuda
        import meltygui_pycuda.gl
        devices = gl_devices()
        if not devices:
            return False
        if cuda.Context.get_current() is not None:
            return cuda_ready()
        return cuda_context_core.ensure_primary_context(devices[0])
    except Exception as error:
        log_once(f"ensure_context failed: {error}")
        return False


def register_buffer(buffer):
    if not cuda_ready():
        raise RuntimeError("CUDA registration requires a compatible current GL/CUDA context pair")
    from meltygui_pycuda import gl as cuda_gl
    return cuda_gl.RegisteredBuffer(buffer, cuda_gl.graphics_map_flags.WRITE_DISCARD)


@contextmanager
def interop_context():
    """Activate a GL-compatible context for one adapter operation.

    Torch can change the native context while PyCUDA still records the rendering
    context. Reactivate that context locally, validate its GL device, and restore
    the caller's native context afterward. A genuinely incompatible PyCUDA context
    is rejected. Standalone callers with no PyCUDA context get lazy setup.
    """
    if not is_gl_thread() or current_context() is None:
        yield None
        return
    try:
        import meltygui_pycuda.driver as cuda
        import meltygui_pycuda.gl
        context = cuda.Context.get_current()
        if context is None and ensure_context():
            context = cuda.Context.get_current()
    except Exception:
        context = None
    if context is None:
        yield None
        return
    with cuda_context_core.using_context(context):
        yield context if cuda_ready() else None


def copy_to_buffer(registered, context, pointer, nbytes):
    """Copy ready device storage into a mapped GL buffer and unmap on failure."""
    import meltygui_pycuda.driver as cuda
    with cuda_context_core.using_context(context):
        mapping = registered.map()
        try:
            destination, capacity = mapping.device_ptr_and_size()
            if capacity < nbytes:
                raise ValueError(f"CUDA buffer has {capacity} bytes; upload needs {nbytes}")
            cuda.memcpy_dtod(destination, pointer, nbytes)
            cuda.Context.synchronize()
        finally:
            mapping.unmap()


def unregister_buffer(registered, context):
    try:
        with cuda_context_core.using_context(context):
            registered.unregister()
    except Exception as error:
        log_once(f"unregister deferred: {error}")
        raise ResourceDeletionDeferred from error
