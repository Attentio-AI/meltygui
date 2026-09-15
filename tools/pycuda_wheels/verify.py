#!/usr/bin/env python3
"""Verify an installed wheel: CUDA kernels on every GPU and a real CUDA/GL buffer transfer."""
import ctypes
import json
import os
os.environ['PYOPENGL_PLATFORM'] = 'egl'
import numpy as np
import pycuda
import pycuda.driver as cuda
import pycuda.gl as cuda_gl
from pycuda.compiler import SourceModule
from OpenGL import EGL, GL


def main():
    cuda.init()
    results = {'version': pycuda.VERSION_TEXT, 'module': pycuda.__file__, 'devices': []}
    for index in range(cuda.Device.count()):
        device = cuda.Device(index)
        context = device.make_context()
        try:
            values = np.arange(16, dtype=np.float32)
            module = SourceModule('__global__ void twice(float *x) { x[threadIdx.x] *= 2; }',
                                  options=['-ccbin', os.environ.get('CUDAHOSTCXX', '/usr/bin/g++-11')])
            module.get_function('twice')(cuda.InOut(values), block=(16, 1, 1))
            np.testing.assert_array_equal(values, np.arange(16, dtype=np.float32) * 2)
            results['devices'].append({'index': index, 'name': device.name(), 'kernel': 'passed'})
        finally:
            context.pop()
            context.detach()
    # EGL device displays avoid touching the desktop or requiring a window server.
    query_devices = ctypes.CFUNCTYPE(ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(ctypes.c_int))(EGL.eglGetProcAddress(b'eglQueryDevicesEXT'))
    get_display = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_int))(EGL.eglGetProcAddress(b'eglGetPlatformDisplayEXT'))
    devices, count = (ctypes.c_void_p * 16)(), ctypes.c_int()
    assert query_devices(16, devices, ctypes.byref(count))
    failures = []
    for device in devices[:count.value]:
        display = ctypes.cast(get_display(0x313F, device, None), EGL.EGLDisplay)
        context = surface = cuda_context = registration = None
        buffer = None
        try:
            major, minor = EGL.EGLint(), EGL.EGLint()
            assert EGL.eglInitialize(display, major, minor)
            assert EGL.eglBindAPI(EGL.EGL_OPENGL_API)
            attrs = (EGL.EGLint * 7)(EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT,
                                      EGL.EGL_RENDERABLE_TYPE, EGL.EGL_OPENGL_BIT,
                                      EGL.EGL_RED_SIZE, 8, EGL.EGL_NONE)
            configs, found = (EGL.EGLConfig * 1)(), EGL.EGLint()
            assert EGL.eglChooseConfig(display, attrs, configs, 1, found) and found.value
            surface = EGL.eglCreatePbufferSurface(display, configs[0],
                (EGL.EGLint * 5)(EGL.EGL_WIDTH, 16, EGL.EGL_HEIGHT, 16, EGL.EGL_NONE))
            context = EGL.eglCreateContext(display, configs[0], EGL.EGL_NO_CONTEXT, (EGL.EGLint * 1)(EGL.EGL_NONE))
            assert EGL.eglMakeCurrent(display, surface, surface, context)
            driver = ctypes.CDLL('libcuda.so.1')
            cuda_devices, cuda_count = (ctypes.c_int * 16)(), ctypes.c_uint()
            assert driver.cuGLGetDevices(ctypes.byref(cuda_count), cuda_devices, 16, 1) == 0
            assert cuda_count.value
            cuda_context = cuda_gl.make_context(cuda.Device(cuda_devices[0]))
            values = np.arange(16, dtype=np.float32)
            buffer = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buffer)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, values.nbytes, None, GL.GL_DYNAMIC_DRAW)
            registration = cuda_gl.RegisteredBuffer(int(buffer))
            mapping = registration.map()
            try:
                pointer, size = mapping.device_ptr_and_size()
                assert size == values.nbytes
                cuda.memcpy_htod(pointer, values)
            finally:
                mapping.unmap()
            restored = np.frombuffer(GL.glGetBufferSubData(GL.GL_ARRAY_BUFFER, 0, values.nbytes), dtype=np.float32)
            np.testing.assert_array_equal(restored, values)
            results['opengl'] = {'renderer': GL.glGetString(GL.GL_RENDERER).decode(),
                                 'cuda_device': cuda_devices[0], 'buffer_roundtrip': 'passed'}
            break
        except Exception as error:
            failures.append(str(error))
        finally:
            if registration is not None:
                registration.unregister()
            if buffer is not None:
                GL.glDeleteBuffers(1, [buffer])
            if cuda_context is not None:
                cuda_context.pop()
                cuda_context.detach()
            EGL.eglMakeCurrent(display, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, EGL.EGL_NO_CONTEXT)
            if context:
                EGL.eglDestroyContext(display, context)
            if surface:
                EGL.eglDestroySurface(display, surface)
            EGL.eglTerminate(display)
    if 'opengl' not in results:
        raise RuntimeError('No CUDA/GL interop display passed: ' + '; '.join(failures))
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
