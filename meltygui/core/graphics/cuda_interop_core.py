"""CUDA context selection and registered-buffer operations for OpenGL interop.

The rendering context determines the device. Never register GL resources from an
unrelated CUDA device, even when Torch's current device happens to be that device.
Tensor adaptation and texture ownership live in model/cuda_texture_model.py.
"""
import atexit
from contextlib import contextmanager
import ctypes
import threading

from meltygui.core.melty import Melty
from meltygui.core.graphics.gl_state import (
    ResourceDeletionDeferred, current_context, is_gl_thread,
)


class CudaInteropRuntime:
    def __init__(self):
        self.primary_context = None
        self.primary_thread = None
        self.last_logged = None
        self.driver_library = None


def runtime():
    if Melty.cuda_interop is None:
        Melty.cuda_interop = CudaInteropRuntime()
    return Melty.cuda_interop


def log_once(message):
    state = runtime()
    if message != state.last_logged:
        print(f"[cuda_interop] {message}")
        state.last_logged = message


def _driver_library():
    state = runtime()
    if state.driver_library is None:
        library = ctypes.CDLL('libcuda.so.1')
        query = library.cuGLGetDevices_v2
        query.argtypes = (ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_int),
                          ctypes.c_uint, ctypes.c_uint)
        query.restype = ctypes.c_int
        for name in ('cuCtxGetCurrent', 'cuCtxPopCurrent_v2'):
            function = getattr(library, name)
            function.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
            function.restype = ctypes.c_int
        library.cuCtxSetCurrent.argtypes = (ctypes.c_void_p,)
        library.cuCtxSetCurrent.restype = ctypes.c_int
        state.driver_library = library
    return state.driver_library


def _native_context():
    context = ctypes.c_void_p()
    result = _driver_library().cuCtxGetCurrent(ctypes.byref(context))
    if result != 0:
        raise RuntimeError(f"cuCtxGetCurrent failed: {result}")
    return context.value


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
    result = _driver_library().cuGLGetDevices_v2(ctypes.byref(count), devices, capacity, 1)
    if result != 0:
        return ()
    return tuple(devices[:count.value])


def current_device_index():
    """The CUDA ordinal of the context current on this thread, or None."""
    import meltygui_pycuda.driver as cuda
    if cuda.Context.get_current() is None:
        return None
    bus = cuda.Context.get_device().pci_bus_id()
    for index in range(cuda.Device.count()):
        if cuda.Device(index).pci_bus_id() == bus:
            return index
    return None


def cuda_ready():
    """Whether GL interop is safe in the current thread and context pair."""
    if not is_gl_thread() or current_context() is None:
        return False
    try:
        import meltygui_pycuda.driver as cuda
        import meltygui_pycuda.gl
        context = cuda.Context.get_current()
        if context is None or context.handle != _native_context():
            return False
        device = current_device_index()
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
        state = runtime()
        if state.primary_context is not None:
            # Someone removed our stack entry; do not acquire a second lease.
            return False
        context = cuda.Device(devices[0]).retain_primary_context()
        try:
            context.push()
        except Exception:
            detach_inactive_primary(context)
            raise
        state.primary_context = context
        state.primary_thread = threading.current_thread()
        atexit.register(_detach_primary)
        return True
    except Exception as error:
        log_once(f"ensure_context failed: {error}")
        return False


def _detach_primary():
    state = runtime()
    if state.primary_context is None:
        return
    import meltygui_pycuda.driver as cuda
    if (threading.current_thread() is not state.primary_thread
            or cuda.Context.get_current() != state.primary_context):
        # Popping someone else's stack entry corrupts its owner's lifetime.
        return
    cuda.Context.pop()
    detach_inactive_primary(state.primary_context)
    state.primary_context = None
    state.primary_thread = None


def detach_inactive_primary(context):
    """Release an owned primary-context lease AFTER removing its PyCUDA entry.

    PyCUDA's inactive-context detach pushes the native context and assumes that
    releasing it also pops it. cuDevicePrimaryCtxRelease explicitly does not pop
    (cuda.h); balance that native push without touching PyCUDA's restored stack.
    """
    context.detach()
    popped = ctypes.c_void_p()
    result = _driver_library().cuCtxPopCurrent_v2(ctypes.byref(popped))
    if result != 0:
        raise RuntimeError(f"cuCtxPopCurrent after primary release failed: {result}")


@contextmanager
def using_context(context):
    """Temporarily restore a registration's CUDA context, preserving the stack."""
    import meltygui_pycuda.driver as cuda
    previous_native = _native_context()
    pushed = cuda.Context.get_current() != context or previous_native != context.handle
    if pushed:
        context.push()
    try:
        yield
    finally:
        if pushed:
            cuda.Context.pop()
        # Torch may also switch the native context inside an already-current
        # scope, where no PyCUDA push/pop was needed on entry.
        if _native_context() != previous_native:
            result = _driver_library().cuCtxSetCurrent(previous_native)
            if result != 0:
                raise RuntimeError(f"restoring CUDA context failed: {result}")


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
    with using_context(context):
        yield context if cuda_ready() else None


def copy_to_buffer(registered, context, pointer, nbytes):
    """Copy ready device storage into a mapped GL buffer and unmap on failure."""
    import meltygui_pycuda.driver as cuda
    with using_context(context):
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
        with using_context(context):
            registered.unregister()
    except Exception as error:
        log_once(f"unregister deferred: {error}")
        raise ResourceDeletionDeferred from error
