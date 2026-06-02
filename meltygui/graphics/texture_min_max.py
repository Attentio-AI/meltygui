import numpy as np
from OpenGL.GL import *
from OpenGL.GL import shaders
import ctypes

# Module-level state
_program = None
_ssbo = None
_ssbo_size = 0

# Stage-2 (final reduction) state. _reduce_program collapses the per-workgroup
# partials into a single (min, max) written into a 1x1 RGBA32F texture via
# imageStore, so the value never leaves the GPU (no glMapBufferRange readback /
# CPU-GPU sync stall). _result_textures holds one such texture per input texture
# id - keyed so two normalize calls in the same frame don't write/read the same
# result texel.
_reduce_program = None
_result_textures = {}

_COMPUTE_SHADER_SOURCE = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D inputTexture;

layout(std430, binding = 1) buffer ResultBuffer {
    vec2 workgroupResults[];  // Each workgroup writes (min, max)
};

uniform ivec2 numWorkgroups;

shared float localMin[256];
shared float localMax[256];

void main() {
    ivec2 pos = ivec2(gl_GlobalInvocationID.xy);
    ivec2 size = textureSize(inputTexture, 0);
    uint lid = gl_LocalInvocationIndex;

    // Initialize with extreme values
    float myMin = 1e38;
    float myMax = -1e38;

    if (pos.x < size.x && pos.y < size.y) {
        vec4 pixel = texelFetch(inputTexture, pos, 0);
        // Get min/max across RGB channels (ignore alpha)
        myMin = min(min(pixel.r, pixel.g), pixel.b);
        myMax = max(max(pixel.r, pixel.g), pixel.b);
    }

    localMin[lid] = myMin;
    localMax[lid] = myMax;
    barrier();

    // Parallel reduction in shared memory
    for (uint s = 128; s > 0; s >>= 1) {
        if (lid < s) {
            localMin[lid] = min(localMin[lid], localMin[lid + s]);
            localMax[lid] = max(localMax[lid], localMax[lid + s]);
        }
        barrier();
    }

    // Workgroup leader writes result
    if (lid == 0) {
        uint workgroupIndex = gl_WorkGroupID.y * numWorkgroups.x + gl_WorkGroupID.x;
        workgroupResults[workgroupIndex] = vec2(localMin[0], localMax[0]);
    }
}
"""

# Stage 2: a single workgroup reduces the per-workgroup partials (binding 1, the
# same SSBO stage 1 filled) down to one (min, max) and stores it into the 0,0
# texel of a 1x1 RGBA32F texture. The normalize_remap fragment shader just samples
# that texel - the range is read straight off the GPU, never mapped to the CPU.
_REDUCE_SHADER_SOURCE = """
#version 430
layout(local_size_x = 256) in;

layout(std430, binding = 1) readonly buffer ResultBuffer {
    vec2 workgroupResults[];
};

layout(rgba32f, binding = 0) writeonly uniform image2D outImage;

uniform int count;

shared float localMin[256];
shared float localMax[256];

void main() {
    uint lid = gl_LocalInvocationIndex;

    float myMin = 1e38;
    float myMax = -1e38;
    for (uint i = lid; i < uint(count); i += 256u) {
        myMin = min(myMin, workgroupResults[i].x);
        myMax = max(myMax, workgroupResults[i].y);
    }

    localMin[lid] = myMin;
    localMax[lid] = myMax;
    barrier();

    for (uint s = 128u; s > 0u; s >>= 1u) {
        if (lid < s) {
            localMin[lid] = min(localMin[lid], localMin[lid + s]);
            localMax[lid] = max(localMax[lid], localMax[lid + s]);
        }
        barrier();
    }

    if (lid == 0u) {
        imageStore(outImage, ivec2(0, 0), vec4(localMin[0], localMax[0], 0.0, 1.0));
    }
}
"""


def _ensure_initialized():
    """Lazy initialization of shader and buffer."""
    global _program, _ssbo, _ssbo_size

    if _program is not None:
        return

    compute_shader = shaders.compileShader(_COMPUTE_SHADER_SOURCE, GL_COMPUTE_SHADER)
    _program = shaders.compileProgram(compute_shader)
    _ssbo = glGenBuffers(1)
    _ssbo_size = 0


def _ensure_reduce_initialized():
    """Lazy initialization of the stage-2 reduction program."""
    global _reduce_program

    if _reduce_program is not None:
        return

    reduce_shader = shaders.compileShader(_REDUCE_SHADER_SOURCE, GL_COMPUTE_SHADER)
    _reduce_program = shaders.compileProgram(reduce_shader)


def _dispatch_stage1(texture_id: int) -> tuple[int, int]:
    """Run the per-workgroup reduction into the SSBO (no readback).

    Returns (total_workgroups, required_size) describing the SSBO contents, so
    callers can either map it (CPU path) or reduce it further on the GPU.
    """
    global _ssbo_size

    _ensure_initialized()

    # Query texture dimensions
    glBindTexture(GL_TEXTURE_2D, texture_id)
    width = glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH)
    height = glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT)

    # Calculate workgroup dimensions
    workgroup_size = 16
    num_workgroups_x = (width + workgroup_size - 1) // workgroup_size
    num_workgroups_y = (height + workgroup_size - 1) // workgroup_size
    total_workgroups = num_workgroups_x * num_workgroups_y

    # Resize SSBO if needed (2 floats per workgroup: min and max)
    required_size = total_workgroups * 2 * 4
    if required_size > _ssbo_size:
        glBindBuffer(GL_SHADER_STORAGE_BUFFER, _ssbo)
        glBufferData(GL_SHADER_STORAGE_BUFFER, required_size, None, GL_DYNAMIC_READ)
        _ssbo_size = required_size

    # Bind resources
    glUseProgram(_program)

    # Bind texture to texture unit 0
    glActiveTexture(GL_TEXTURE0)
    glBindTexture(GL_TEXTURE_2D, texture_id)
    glUniform1i(glGetUniformLocation(_program, "inputTexture"), 0)

    glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 1, _ssbo)

    # Set uniforms
    loc = glGetUniformLocation(_program, "numWorkgroups")
    glUniform2i(loc, num_workgroups_x, num_workgroups_y)

    # Dispatch compute shader
    glDispatchCompute(num_workgroups_x, num_workgroups_y, 1)
    glMemoryBarrier(GL_SHADER_STORAGE_BARRIER_BIT)

    return total_workgroups, required_size


def get_texture_min_max(texture_id: int) -> tuple[float, float]:
    """
    Compute min/max pixel values of an OpenGL texture (CPU readback).

    Reads the result back to the CPU via glMapBufferRange — this synchronizes
    the GPU and stalls. Prefer compute_min_max_to_texture() in hot paths where
    the value only needs to feed another GPU pass.

    Args:
        texture_id: OpenGL texture ID (any format - will be converted to float)

    Returns:
        Tuple of (min_value, max_value) across RGB channels
    """
    total_workgroups, required_size = _dispatch_stage1(texture_id)

    # Read back results with glMapBufferRange
    glBindBuffer(GL_SHADER_STORAGE_BUFFER, _ssbo)
    ptr = glMapBufferRange(GL_SHADER_STORAGE_BUFFER, 0, required_size, GL_MAP_READ_BIT)
    results = np.ctypeslib.as_array(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_float)),
                                    shape=(total_workgroups * 2,)).copy()
    glUnmapBuffer(GL_SHADER_STORAGE_BUFFER)

    # Final reduction on CPU
    results = results.reshape(-1, 2)
    min_val = float(np.min(results[:, 0]))
    max_val = float(np.max(results[:, 1]))

    return min_val, max_val


def _get_result_tex(input_texture_id: int) -> int:
    """Get or create the 1x1 RGBA32F texture that holds (min, max) for an input."""
    tex = _result_textures.get(input_texture_id)
    if tex is not None:
        return tex

    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tex)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA32F, 1, 1, 0, GL_RGBA, GL_FLOAT, None)
    # texelFetch ignores filtering, but NEAREST makes a float texture valid as a
    # sampler everywhere (linear filtering of 32F is an optional GL extension).
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    _result_textures[input_texture_id] = tex
    return tex


def compute_min_max_to_texture(input_texture_id: int) -> int:
    """
    Compute (min, max) of a texture entirely on the GPU and store it in the 0,0
    texel of a 1x1 RGBA32F texture (.r = min, .g = max). No CPU readback.

    Returns the result texture id, ready to be sampled by a downstream shader.
    """
    total_workgroups, _ = _dispatch_stage1(input_texture_id)

    _ensure_reduce_initialized()
    out_tex = _get_result_tex(input_texture_id)

    glUseProgram(_reduce_program)
    glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 1, _ssbo)
    glBindImageTexture(0, out_tex, 0, GL_FALSE, 0, GL_WRITE_ONLY, GL_RGBA32F)
    glUniform1i(glGetUniformLocation(_reduce_program, "count"), total_workgroups)
    glDispatchCompute(1, 1, 1)
    # Make the imageStore visible before the sampler fetch in the following draw.
    glMemoryBarrier(GL_SHADER_IMAGE_ACCESS_BARRIER_BIT | GL_TEXTURE_FETCH_BARRIER_BIT)

    return out_tex


def set_min_max_texture(input_texture_id: int, min_value: float, max_value: float) -> int:
    """
    Write an explicit (min, max) into the 1x1 result texture (no GPU reduction,
    no readback — a tiny upload). Returns the result texture id so the explicit
    path feeds normalize_remap through the same sampler as the computed path.
    """
    out_tex = _get_result_tex(input_texture_id)
    data = np.array([min_value, max_value, 0.0, 1.0], dtype=np.float32)
    glBindTexture(GL_TEXTURE_2D, out_tex)
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 1, 1, GL_RGBA, GL_FLOAT, data)
    return out_tex


def cleanup():
    """Delete OpenGL resources. Call before destroying GL context."""
    global _program, _ssbo, _ssbo_size, _reduce_program, _result_textures

    if _ssbo is not None:
        glDeleteBuffers(1, [_ssbo])
        _ssbo = None
    if _program is not None:
        glDeleteProgram(_program)
        _program = None
    if _reduce_program is not None:
        glDeleteProgram(_reduce_program)
        _reduce_program = None
    if _result_textures:
        glDeleteTextures(list(_result_textures.values()))
        _result_textures = {}
    _ssbo_size = 0
