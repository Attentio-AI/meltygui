"""Render-thread CUDA compilation and process-lifetime kernel ownership.

Feature views supply CUDA source and entry points. Melty's CUDA runtime retains
modules alongside their primary contexts, including across definition hotswaps.
"""
import sys

from meltygui.core.graphics.cuda_context_core import runtime, using_device


def available():
    try:
        import meltygui_pycuda.driver
        import meltygui_pycuda as pycuda  # noqa: F401
        return True
    except Exception:
        return False


def host_compiler_flags():
    """Use an installed CUDA-12-compatible C++ compiler when available."""
    import shutil
    for version in ("12", "11", "10"):
        if shutil.which("gcc-" + version) and shutil.which("g++-" + version):
            return ["-ccbin", "g++-" + version]
    return ["-allow-unsupported-compiler"]


class CudaKernel:
    def __init__(self, source, module, functions):
        self.source = source
        self.module = module
        self.functions = functions


def _cache():
    state = runtime()
    if 'kernels' not in state.__dict__:
        state.kernels = {}
    # One-time runtime transfer, not import compatibility. Keep existing compiled
    # functions alive until their feature is next requested under its context.
    if 'legacy_kernels' not in state.__dict__ or state.legacy_kernels is None:
        state.legacy_kernels = {
            'voxels': sys.__dict__.pop('_lsd_cuda_march_kernels', {}),
            'lines': sys.__dict__.pop('_melty_cuda_line_kernels', {}),
        }
    return state.kernels


def kernel_functions(feature, device, source, names):
    """Compile a feature's entry points once per source/device.

    A failed replacement leaves the last good module owned by the runtime.
    Replacement and destruction happen with the owning device context active.
    Unchanged source after hotswap retains the same function objects.
    """
    device = int(device)
    names = tuple(names)
    # This also enforces the render-thread contract on hits and before adopting
    # old entries; none of the runtime dictionaries can be mutated by workers.
    with using_device(device):
        cache = _cache()
        key = (feature, device, names)
        entry = cache.get(key)
        if entry is not None and entry.source == source:
            return entry.functions
        legacy = runtime().legacy_kernels.get(feature, {}).get(device)
        if legacy is not None:
            if feature == 'voxels' and isinstance(legacy, dict) and legacy.get('__source__') == hash(source):
                entry = CudaKernel(source, None, {name: legacy[name] for name in names})
            elif feature == 'lines' and legacy[0] == source:
                entry = CudaKernel(source, legacy[1], dict(zip(names, legacy[2:])))
            if entry is not None and entry.source == source:
                cache[key] = entry
                runtime().legacy_kernels[feature].pop(device)
                return entry.functions
        import meltygui_pycuda.driver as cuda
        from meltygui_pycuda.compiler import SourceModule
        capability = cuda.Device(device).compute_capability()
        module = SourceModule(source, no_extern_c=True, arch='sm_%d%d' % capability,
                              options=['-O3'] + host_compiler_flags())
        try:
            functions = {name: module.get_function(name) for name in names}
        except Exception:
            del module
            raise
        cache[key] = CudaKernel(source, module, functions)
        runtime().legacy_kernels.get(feature, {}).pop(device, None)
        # Drop replaced and transferred references while the context is active.
        del entry, legacy
        return functions
