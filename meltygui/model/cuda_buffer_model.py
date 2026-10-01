"""Strided CUDA buffers for renderers, independent of any tensor framework.

Views only change metadata. Only explicitly named small image/LUT allocation
helpers allocate memory; no conversion ever materializes a source buffer.
"""
import math
from dataclasses import dataclass

ITEM_SIZES = {'float16': 2, 'float32': 4, 'float64': 8, 'bfloat16': 2,
              'int8': 1, 'uint8': 1, 'bool': 1, 'int16': 2, 'int32': 4, 'int64': 8}


@dataclass(frozen=True)
class CudaDevice:
    index: int
    type: str = 'cuda'

    def __str__(self):
        return f'cuda:{self.index}'


class CudaBuffer:
    is_cuda = True
    _version = 0

    def __init__(self, pointer, shape, strides, dtype, device, owner, available=lambda: True):
        self.pointer, self.shape, self.strides = pointer, tuple(shape), tuple(strides)
        self.dtype = str(dtype).removeprefix('torch.')
        if self.dtype not in ITEM_SIZES:
            raise ValueError(f'Unsupported shared CUDA dtype: {dtype}; the tensor was not copied')
        self.device = device if isinstance(device, CudaDevice) else CudaDevice(int(device))
        self.owner = owner
        self.available = available

    def resolve(self):
        if 'available' not in vars(self):
            self.available = getattr(self, '_available', lambda: True)
        if not self.available():
            raise RuntimeError('The process owning this CUDA buffer is unavailable; the tensor was not copied')
        return self

    def data_ptr(self):
        self.resolve()
        return self.pointer

    def stride(self):
        return self.strides

    def dim(self):
        return len(self.shape)

    @property
    def ndim(self):
        return len(self.shape)

    def numel(self):
        return math.prod(self.shape)

    def element_size(self):
        return ITEM_SIZES[self.dtype]

    def detach(self):
        return self

    def __getstate__(self):
        return dict(shape=self.shape, strides=self.strides, dtype=self.dtype)

    def __setstate__(self, state):
        # GPU addresses and process leases are never valid across app sessions.
        CudaBuffer.__init__(self, 0, state['shape'], state['strides'], state['dtype'],
                            CudaDevice(-1), None, available=lambda: False)

    def unsqueeze(self, axis):
        axis = axis if axis >= 0 else self.dim() + axis + 1
        stride = self.strides[axis] * self.shape[axis] if axis < self.dim() else 1
        return CudaBuffer(self.pointer, self.shape[:axis] + (1,) + self.shape[axis:],
                          self.strides[:axis] + (stride,) + self.strides[axis:], self.dtype, self.device, self.owner, self.available)

    def permute(self, *axes):
        if sorted(axes) != list(range(self.dim())):
            raise ValueError('Axes must be a permutation')
        return CudaBuffer(self.pointer, tuple(self.shape[i] for i in axes),
                          tuple(self.strides[i] for i in axes), self.dtype, self.device, self.owner, self.available)

    def __getitem__(self, indices):
        indices = indices if isinstance(indices, tuple) else (indices,)
        indices += (slice(None),) * (self.dim() - len(indices))
        if len(indices) != self.dim():
            raise IndexError('Too many buffer indices')
        offset, shape, strides = 0, [], []
        for size, stride, index in zip(self.shape, self.strides, indices):
            if isinstance(index, slice):
                start, stop, step = index.indices(size)
                offset += start * stride
                shape.append(len(range(start, stop, step)))
                strides.append(stride * step)
            else:
                index = int(index)
                if index < 0:
                    index += size
                if not 0 <= index < size:
                    raise IndexError(index)
                offset += index * stride
        return CudaBuffer(self.pointer + offset * self.element_size(), shape, strides,
                          self.dtype, self.device, self.owner, self.available)


def empty_image_buffer(shape, dtype, device):
    """Small renderer-owned outputs/workspaces; never used to convert the source."""
    import meltygui_pycuda.driver as cuda
    from meltygui.core.graphics.cuda_context_core import using_device
    device = device if isinstance(device, CudaDevice) else CudaDevice(int(device.index or 0))
    strides = [math.prod(shape[i + 1:]) for i in range(len(shape))]
    with using_device(device.index):
        allocation = cuda.mem_alloc(math.prod(shape) * ITEM_SIZES[dtype])
    return CudaBuffer(int(allocation), shape, strides, dtype, device, allocation)


def parameter_buffer(values, device):
    import numpy as np
    import meltygui_pycuda.driver as cuda
    from meltygui.core.graphics.cuda_context_core import using_device
    array = np.asarray(values, dtype=np.float32)
    buffer = empty_image_buffer(array.shape, 'float32', device)
    with using_device(buffer.device.index):
        cuda.memcpy_htod(buffer.data_ptr(), array)
    return buffer
