"""CUDA decoding of tensor primitive dtypes without packing source storage."""

DTYPE_CODES = {
    "torch.float32": 0, "torch.float16": 1, "torch.bfloat16": 2,
    "torch.float64": 3, "torch.int8": 4, "torch.uint8": 5, "torch.bool": 5,
    "torch.int16": 6, "torch.int32": 7, "torch.int64": 8,
}

CUDA_LOAD_SOURCE = r"""
#include <cuda_fp16.h>

__device__ __forceinline__ float load_at(const unsigned char* __restrict__ d,
                                         int dtype, long long e) {
    switch (dtype) {
        case 0: return ((const float*)d)[e];
        case 1: return __half2float(((const __half*)d)[e]);
        case 2: { unsigned short u = ((const unsigned short*)d)[e];
                  return __uint_as_float(((unsigned)u) << 16); }
        case 3: return (float)((const double*)d)[e];
        case 4: return (float)((const signed char*)d)[e];
        case 5: return (float)d[e];
        case 6: return (float)((const short*)d)[e];
        case 7: return (float)((const int*)d)[e];
        case 8: return (float)((const long long*)d)[e];
    }
    return 0.0f;
}

"""


def dtype_code(t):
    code = DTYPE_CODES.get(str(t.dtype))
    if code is None:
        raise ValueError(f"unsupported CUDA tensor dtype {t.dtype}")
    return code
