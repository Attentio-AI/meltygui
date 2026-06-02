import numpy as np
from OpenGL.GL import *
from OpenGL.GL import shaders
import ctypes

# Module-level state
_program = None
_ssbo = None
_ssbo_size = 0

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


def _ensure_initialized():
    """Lazy initialization of shader and buffer."""
    global _program, _ssbo, _ssbo_size

    if _program is not None:
        return

    compute_shader = shaders.compileShader(_COMPUTE_SHADER_SOURCE, GL_COMPUTE_SHADER)
    _program = shaders.compileProgram(compute_shader)
    _ssbo = glGenBuffers(1)
    _ssbo_size = 0


def get_texture_min_max(texture_id: int) -> tuple[float, float]:
    """
    Compute min/max pixel values of an OpenGL texture.

    Args:
        texture_id: OpenGL texture ID (any format - will be converted to float)

    Returns:
        Tuple of (min_value, max_value) across RGB channels
    """
    global _ssbo_size

    _ensure_initialized()

    # Query texture dimensions
    glBindTexture(GL_TEXTURE_2D, texture_id)
    width = glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH)
    height = glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT)

    # Calculate workgroup dimensions
    workgroup_size = 4
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


def cleanup():
    """Delete OpenGL resources. Call before destroying GL context."""
    global _program, _ssbo, _ssbo_size

    if _ssbo is not None:
        glDeleteBuffers(1, [_ssbo])
        _ssbo = None
    if _program is not None:
        glDeleteProgram(_program)
        _program = None
    _ssbo_size = 0