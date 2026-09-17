"""Shared CUDA context ownership for rendering and GL interop.

Per-device kernel leases live as long as compiled kernels; the standalone display
stack entry has a separate lease so popping it cannot invalidate cached kernels.
Both use CUDA primary contexts, so Torch storage remains directly accessible.
"""
import atexit
from contextlib import contextmanager
import ctypes
import sys
import threading

from meltygui.core.melty import Melty
from meltygui.core.graphics.gl_state import is_gl_thread


class CudaContextRuntime:
    def __init__(self):
        self.contexts = {}
        self.kernels = {}
        self.legacy_kernels = None
        self.primary_context = None
        self.primary_thread = None
        self.last_logged = None
        self.driver_library = None


def runtime():
    if Melty.cuda_interop is None:
        Melty.cuda_interop = CudaContextRuntime()
    state = Melty.cuda_interop
    # Transfer the old cache's context leases without reacquiring resources.
    # Existing interop runtime objects gain the pool on first use after hotswap.
    if 'contexts' not in state.__dict__:
        state.contexts = {}
    state.contexts.update(sys.__dict__.pop('_lsd_cuda_march_contexts', {}))
    return state


def primary_context_for(device):
    """Retain a render-thread primary context for compiled kernels on a device."""
    if not is_gl_thread():
        raise RuntimeError('rendering CUDA contexts belong to the render thread')
    device = int(device)
    contexts = runtime().contexts
    if device not in contexts:
        import meltygui_pycuda.driver as cuda
        cuda.init()
        contexts[device] = cuda.Device(device).retain_primary_context()
    return contexts[device]


@contextmanager
def using_device(device):
    """Run on a tensor's GPU and restore PyCUDA and native caller contexts."""
    with using_context(primary_context_for(device)):
        yield


def ensure_primary_context(device):
    """Install the standalone render-thread stack entry when none exists."""
    if not is_gl_thread():
        return False
    import meltygui_pycuda.driver as cuda
    cuda.init()
    state = runtime()
    if cuda.Context.get_current() is not None or state.primary_context is not None:
        return False
    context = cuda.Device(int(device)).retain_primary_context()
    try:
        context.push()
    except Exception:
        detach_inactive_primary(context)
        raise
    state.primary_context = context
    state.primary_thread = threading.current_thread()
    atexit.register(_detach_primary)
    return True


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
