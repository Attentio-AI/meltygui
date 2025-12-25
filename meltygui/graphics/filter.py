"""
Filter - main interface for shader-based texture filtering.
"""

from typing import Dict, Any, Optional, List, Tuple
from contextlib import contextmanager
from functools import partial

import numpy as np

from src.shader_library.shader_manager.compiler import ProgramCompiler, CompiledProgram, ShaderCompilationError
from src.shader_library.shader_manager.executor import FilterExecutor
from src.shader_library.shader_manager.registry import get_registry

from src.shader_library.shader_manager import texture_min_max


class FilterChain:
    """
    A chainable sequence of filters.
    
    Usage:
        chain = filter.chain()
        result = (chain
            .brightness_contrast(brightness=0.2)
            .gaussian_blur(radius=3.0)
            .apply(texture_id))
    """
    
    def __init__(self, filter_instance: 'Filter'):
        """
        Initialize a filter chain.
        
        Args:
            filter_instance: The parent Filter instance
        """
        self._filter = filter_instance
        self._steps: list = []
    
    def __getattr__(self, name: str):
        """Add a filter step to the chain."""
        if name.startswith('_'):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        
        if not self._filter.has_shader(name):
            available = ', '.join(self._filter.list_shaders())
            raise AttributeError(
                f"No shader named '{name}'. Available shaders: {available}"
            )
        
        def add_step(**uniforms):
            self._steps.append((name, uniforms))
            return self
        
        return add_step
    
    def apply(self, texture_id: int, in_place: bool = False) -> int:
        """
        Apply the filter chain to a texture.
        
        Args:
            texture_id: Input texture ID
            in_place: If True, modify the input texture (final output)
            
        Returns:
            The output texture ID
        """
        if not self._steps:
            return texture_id
        
        current_texture = texture_id
        
        for i, (shader_name, uniforms) in enumerate(self._steps):
            is_last = (i == len(self._steps) - 1)
            
            if is_last and in_place:
                # Last step - render to original texture
                current_texture = self._filter.apply(
                    shader_name, current_texture,
                    in_place=True, **uniforms
                )
            else:
                # Intermediate step - render to new texture
                current_texture = self._filter.apply(
                    shader_name, current_texture,
                    in_place=False, **uniforms
                )
        
        return current_texture
    
    def clear(self) -> 'FilterChain':
        """Clear all steps from the chain."""
        self._steps.clear()
        return self
    
    def __len__(self) -> int:
        """Get the number of steps in the chain."""
        return len(self._steps)
    
    def __repr__(self) -> str:
        steps = ' -> '.join(name for name, _ in self._steps)
        return f"FilterChain([{steps}])"


class Filter:
    """
    Main interface for shader-based texture filtering.
    
    Provides attribute-style access to shader filters and manages
    shader lifecycle, compilation, and execution.
    
    Usage:
        filter = Filter()
        
        # Apply filters using attribute access
        texture_id = filter.brightness_contrast(texture_id, brightness=0.5)
        texture_id = filter.vignette(texture_id, in_place=True)
        
        # Chain multiple filters
        result = (filter.chain()
            .brightness_contrast(brightness=0.2)
            .gaussian_blur_h(radius=3.0)
            .apply(texture_id))
        
        # Clean up when done
        filter.cleanup()
    """
    
    def __init__(self, auto_compile: bool = True):
        """
        Initialize the Filter manager.

        Args:
            auto_compile: If True, compile shaders on first use.
        """
        self._auto_compile = auto_compile
        self._registry = get_registry()
        self._compiler = ProgramCompiler()
        self._executor = FilterExecutor(self._compiler)
        self._compiled: Dict[str, CompiledProgram] = {}

        # Dynamically generate methods for all registered shaders
        # This enables IDE autocomplete and type hints
        self._generate_shader_methods()

    def _generate_shader_methods(self) -> None:
        """
        Dynamically generate methods for all registered shaders.

        This creates actual methods on the instance for each shader,
        enabling IDE autocomplete and type hints.
        """
        for shader_name in self._registry.shaders.keys():
            # Create a closure to capture shader_name
            def make_method(name: str):
                def shader_method(
                    texture_id: int = 0,
                    in_place: bool = False,
                    output_texture: Optional[int] = None,
                    output_framebuffer: Optional[int] = None,
                    input_framebuffer: Optional[int] = None,
                    **uniforms
                ) -> int:
                    """
                    Apply this shader filter to a texture or framebuffer.

                    Args:
                        texture_id: Input texture ID (ignored if input_framebuffer is set)
                        in_place: If True, modify the input texture directly
                        output_texture: Optional specific output texture to render to
                        output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
                        input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
                        **uniforms: Uniform values to pass to the shader

                    Returns:
                        The output texture ID (same as input if in_place=True, 0 if output_framebuffer)
                    """
                    return self.apply(name, texture_id, in_place, output_texture, output_framebuffer, input_framebuffer, **uniforms)

                # Set the method name for better debugging
                shader_method.__name__ = name
                return shader_method

            # Attach the method to this instance
            setattr(self, shader_name, make_method(shader_name))

    def __getattr__(self, name: str):
        """
        Fallback attribute access for dynamically registered shaders.

        Note: Most shaders have real methods generated at __init__ time.
        This fallback handles edge cases like shaders registered after init.
        """
        # Don't intercept private attributes
        if name.startswith('_'):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        # Check if this shader exists
        if not self.has_shader(name):
            available = ', '.join(self.list_shaders())
            raise AttributeError(
                f"No shader named '{name}'. Available shaders: {available}"
            )

        # Return a callable that applies this filter
        return partial(self.apply, name)
    
    def apply(self, shader_name: str, texture_id: int = 0,
              in_place: bool = False,
              output_texture: Optional[int] = None,
              output_framebuffer: Optional[int] = None,
              input_framebuffer: Optional[int] = None,
              **uniforms) -> int:
        """
        Apply a shader filter to a texture or framebuffer.

        Args:
            shader_name: Name of the shader to apply
            texture_id: Input texture ID (ignored if input_framebuffer is set)
            in_place: If True, modify the input texture directly
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen).
                               If set, renders directly to framebuffer instead of creating output texture.
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen).
                              If set, reads from this framebuffer instead of texture_id.
            **uniforms: Uniform values to pass to the shader

        Returns:
            The output texture ID (same as input if in_place=True, 0 if output_framebuffer)
        """
        program = self._get_compiled(shader_name)
        if program is None:
            raise KeyError(f"Shader '{shader_name}' not found or failed to compile")
        
        # Fill in default uniform values
        full_uniforms = {}
        for uname, (_, default) in program.shader.uniforms.items():
            if default is not None:
                full_uniforms[uname] = default
        full_uniforms.update(uniforms)
        
        return self._executor.execute(
            program, texture_id, full_uniforms,
            in_place=in_place, output_texture=output_texture,
            output_framebuffer=output_framebuffer,
            input_framebuffer=input_framebuffer
        )
    
    def chain(self) -> FilterChain:
        """Create a new filter chain."""
        return FilterChain(self)
    
    def compile_shader(self, name: str) -> CompiledProgram:
        """
        Compile a shader by name.
        
        Args:
            name: The shader name (snake_case)
            
        Returns:
            The compiled program
            
        Raises:
            KeyError: If the shader is not registered
            ShaderCompilationError: If compilation fails
        """
        shader = self._registry.get_shader(name)
        if shader is None:
            raise KeyError(f"No shader registered with name '{name}'")
        
        shader_type = self._registry.get_type(shader.shader_type)
        if shader_type is None:
            raise KeyError(f"No shader type registered with name '{shader.shader_type}'")
        
        compiled = self._compiler.compile(shader, shader_type)
        self._compiled[name] = compiled
        return compiled
    
    def compile_all(self) -> Dict[str, CompiledProgram]:
        """
        Compile all registered shaders.
        
        Returns:
            Dict mapping shader names to compiled programs
        """
        results = {}
        for name in self._registry.shaders.keys():
            try:
                results[name] = self.compile_shader(name)
            except ShaderCompilationError as e:
                print(f"Warning: Failed to compile '{name}': {e}")
        return results
    
    def _get_compiled(self, name: str) -> Optional[CompiledProgram]:
        """
        Get a compiled program by name.
        
        If auto_compile is enabled and the shader isn't compiled yet,
        it will be compiled automatically.
        """
        if name not in self._compiled and self._auto_compile:
            if self._registry.get_shader(name) is not None:
                return self.compile_shader(name)
        return self._compiled.get(name)
    
    def has_shader(self, name: str) -> bool:
        """Check if a shader is registered."""
        return self._registry.get_shader(name) is not None
    
    def list_shaders(self) -> List[str]:
        """List all registered shader names."""
        return list(self._registry.shaders.keys())
    
    def list_shader_types(self) -> List[str]:
        """List all registered shader type names."""
        return list(self._registry.shader_types.keys())
    
    def get_shader_info(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a registered shader.
        
        Args:
            name: The shader name
            
        Returns:
            Dict with shader info, or None if not found
        """
        shader = self._registry.get_shader(name)
        if shader is None:
            return None
        
        return {
            'name': shader.name,
            'shader_type': shader.shader_type,
            'uniforms': {
                uname: {'type': utype.value, 'default': default}
                for uname, (utype, default) in shader.uniforms.items()
            },
            'compiled': name in self._compiled
        }
    
    @contextmanager
    def use_shader(self, name: str):
        """
        Context manager for using a shader program.

        Useful for manual rendering when you need more control.

        Usage:
            with filter.use_shader('brightness_contrast') as program:
                # Set up your own rendering...
                pass
        """
        from OpenGL import GL

        program = self._get_compiled(name)
        if program is None:
            raise KeyError(f"Shader '{name}' not found or failed to compile")

        try:
            GL.glUseProgram(program.program_id)
            yield program
        finally:
            GL.glUseProgram(0)

    def clear_texture_cache(self) -> None:
        """
        Clear all cached output textures.

        This frees up GPU memory by deleting all textures in the cache.
        Call this periodically if you're concerned about memory usage.
        """
        self._executor.clear_texture_cache()

    def clear_fbo_cache(self) -> None:
        """
        Clear all cached FBOs and temp textures.

        This frees up GPU resources by deleting all cached framebuffers
        and their associated temporary textures.
        """
        self._executor.clear_fbo_cache()

    def normalize(self, texture_id: int, in_place: bool = False,
                  output_texture: Optional[int] = None, min_value=None, max_value=None) -> int:
        """
        Normalize texture values to [0, 1] range by calculating min/max.

        This uses GPU-based parallel reduction to efficiently find the global
        min and max values without reading all pixels. The texture is progressively
        downsampled until reaching a 1x1 pixel, then that single pixel is read
        to get the min/max values.

        Args:
            texture_id: Input texture ID
            in_place: If True, modify the input texture directly
            output_texture: Optional specific output texture to render to

        Returns:
            The output texture ID (same as input if in_place=True)
        """
        # Lazy import to avoid requiring OpenGL at import time
        def _get_gl():
            from OpenGL import GL
            return GL

        GL = _get_gl()

        # Get texture dimensions
        GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id)
        reduction_textures = []  # Track temporary textures for cleanup

        if min_value is None or max_value is None:
            min_value, max_value = texture_min_max.get_texture_min_max(texture_id)
        try:
            # Apply normalization remap
            result = self.apply(
                'normalize_remap',
                texture_id,
                in_place=in_place,
                output_texture=output_texture,
                min_value=min_value,
                max_value=max_value
            )
            return result

        finally:
            # Clean up temporary reduction textures
            if reduction_textures:
                GL.glDeleteTextures(len(reduction_textures), reduction_textures)

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get statistics about all caches.

        Returns:
            Dict with the following keys:
            - 'textures': Dict mapping input_texture_id -> (output_texture_id, width, height)
            - 'fbos': Dict mapping (width, height) -> True (one FBO per size)
            - 'temp_textures': Dict mapping (width, height) -> True (one temp per size)
        """
        return {
            'textures': dict(self._executor._texture_cache),
            'fbos': {
                size: True
                for size in self._executor._fbo_cache.keys()
            },
            'temp_textures': {
                size: True
                for size in self._executor._temp_texture_cache.keys()
            }
        }

    def cleanup(self) -> None:
        """Clean up all OpenGL resources."""
        self._compiler.cleanup()
        self._executor.cleanup()
        self._compiled.clear()
    
    def __enter__(self) -> 'Filter':
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - cleanup resources."""
        self.cleanup()
    
    def __dir__(self):
        """List available attributes for tab completion."""
        return (list(self._registry.shaders.keys()) +
                ['apply', 'chain', 'compile_shader', 'compile_all',
                 'has_shader', 'list_shaders', 'list_shader_types',
                 'get_shader_info', 'use_shader', 'normalize',
                 'clear_texture_cache', 'clear_fbo_cache', 'get_cache_stats', 'cleanup'])
    
    def __repr__(self) -> str:
        n_shaders = len(self._registry.shaders)
        n_types = len(self._registry.shader_types)
        n_compiled = len(self._compiled)
        return f"Filter(shaders={n_shaders}, types={n_types}, compiled={n_compiled})"


import numpy as np
from OpenGL.GL import *
from OpenGL.GL import shaders
import ctypes


class TextureMinMax:
    """Compute shader-based min/max finder for OpenGL textures."""

    COMPUTE_SHADER_SOURCE = """
    #version 430
    layout(local_size_x = 16, local_size_y = 16) in;

    layout(rgba32f, binding = 0) readonly uniform image2D inputImage;

    layout(std430, binding = 1) buffer ResultBuffer {
        vec2 workgroupResults[];  // Each workgroup writes (min, max)
    };

    uniform ivec2 numWorkgroups;

    shared float localMin[256];
    shared float localMax[256];

    void main() {
        ivec2 pos = ivec2(gl_GlobalInvocationID.xy);
        ivec2 size = imageSize(inputImage);
        uint lid = gl_LocalInvocationIndex;

        // Initialize with extreme values
        float myMin = 1e38;
        float myMax = -1e38;

        if (pos.x < size.x && pos.y < size.y) {
            vec4 pixel = imageLoad(inputImage, pos);
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

    def __init__(self):
        self._program = None
        self._ssbo = None
        self._initialized = False

    def _ensure_initialized(self):
        """Lazy initialization of shader and buffer."""
        if self._initialized:
            return

        # Compile compute shader
        compute_shader = shaders.compileShader(
            self.COMPUTE_SHADER_SOURCE,
            GL_COMPUTE_SHADER
        )
        self._program = shaders.compileProgram(compute_shader)

        # Create SSBO (will resize as needed)
        self._ssbo = glGenBuffers(1)
        self._ssbo_size = 0

        self._initialized = True

    def get_min_max(self, texture_id: int, width: int, height: int) -> tuple[float, float]:
        """
        Compute min/max pixel values of an OpenGL texture.

        Args:
            texture_id: OpenGL texture ID (must be rgba32f format)
            width: Texture width in pixels
            height: Texture height in pixels

        Returns:
            Tuple of (min_value, max_value) across RGB channels
        """
        self._ensure_initialized()

        # Calculate workgroup dimensions
        workgroup_size = 16
        num_workgroups_x = (width + workgroup_size - 1) // workgroup_size
        num_workgroups_y = (height + workgroup_size - 1) // workgroup_size
        total_workgroups = num_workgroups_x * num_workgroups_y

        # Resize SSBO if needed (2 floats per workgroup: min and max)
        required_size = total_workgroups * 2 * 4  # 2 floats * 4 bytes
        if required_size > self._ssbo_size:
            glBindBuffer(GL_SHADER_STORAGE_BUFFER, self._ssbo)
            glBufferData(GL_SHADER_STORAGE_BUFFER, required_size, None, GL_DYNAMIC_READ)
            self._ssbo_size = required_size

        # Bind resources
        glUseProgram(self._program)
        glBindImageTexture(0, texture_id, 0, GL_FALSE, 0, GL_READ_ONLY, GL_RGBA32F)
        glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 1, self._ssbo)

        # Set uniforms
        loc = glGetUniformLocation(self._program, "numWorkgroups")
        glUniform2i(loc, num_workgroups_x, num_workgroups_y)

        # Dispatch compute shader
        glDispatchCompute(num_workgroups_x, num_workgroups_y, 1)
        glMemoryBarrier(GL_SHADER_STORAGE_BARRIER_BIT)

        # Read back results
        glBindBuffer(GL_SHADER_STORAGE_BUFFER, self._ssbo)
        results = np.empty(total_workgroups * 2, dtype=np.float32)
        glGetBufferSubData(GL_SHADER_STORAGE_BUFFER, 0, results.nbytes, results)

        # Final reduction on CPU (very fast for small arrays)
        results = results.reshape(-1, 2)
        min_val = float(np.min(results[:, 0]))
        max_val = float(np.max(results[:, 1]))

        return min_val, max_val

    def cleanup(self):
        """Delete OpenGL resources."""
        if self._ssbo is not None:
            glDeleteBuffers(1, [self._ssbo])
            self._ssbo = None
        if self._program is not None:
            glDeleteProgram(self._program)
            self._program = None
        self._initialized = False

    def __del__(self):
        # Note: OpenGL context must still be active for this to work
        # Prefer explicit cleanup() call
        pass


# Convenience functions for one global use
_global_instance = None


def get_texture_min_max(texture_id: int, width: int, height: int) -> tuple[float, float]:
    """
    Convenience function to get min/max of a texture.
    Uses a cached shader program for efficiency.
    """
    global _global_instance
    if _global_instance is None:
        _global_instance = TextureMinMax()
    return _global_instance.get_min_max(texture_id, width, height)


def cleanup_texture_min_max():
    """Cleanup global resources."""
    global _global_instance
    if _global_instance is not None:
        _global_instance.cleanup()
        _global_instance = None