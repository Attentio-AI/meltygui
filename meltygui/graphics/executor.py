"""
Filter execution engine - applies shaders to textures.
"""

from typing import Dict, Any, Optional, Tuple
import ctypes

from src.shader_library.shader_manager.base import GLType
from src.shader_library.shader_manager.compiler import ProgramCompiler, CompiledProgram


def _get_gl():
    """Lazy import of OpenGL."""
    from OpenGL import GL
    return GL


def _get_numpy():
    """Lazy import of numpy."""
    import numpy as np
    return np


class FilterExecutor:
    """
    Executes shader filters on textures.
    
    Handles framebuffer setup, texture binding, and uniform passing.
    """
    
    def __init__(self, compiler: ProgramCompiler):
        """
        Initialize the filter executor.

        Args:
            compiler: The program compiler instance
        """
        self._compiler = compiler

        # OpenGL resources
        self._quad_vao = None
        self._quad_vbo = None
        self._initialized = False

        # FBO cache: maps (width, height) -> FBO ID
        self._fbo_cache: Dict[Tuple[int, int], int] = {}

        # Temp texture cache for in-place operations: maps (width, height) -> texture ID
        self._temp_texture_cache: Dict[Tuple[int, int], int] = {}

        # Output texture cache: maps input_texture_id -> (output_texture_id, width, height)
        # Each input texture gets its own persistent output texture
        self._texture_cache: Dict[int, Tuple[int, int, int]] = {}
    
    def _ensure_initialized(self) -> None:
        """Lazily initialize OpenGL resources."""
        if not self._initialized:
            self._setup_quad()
            self._initialized = True
    
    def _setup_quad(self) -> None:
        """Set up the fullscreen quad for rendering."""
        GL = _get_gl()
        np = _get_numpy()
        
        # Fullscreen quad vertices: position (x, y, z) and texcoord (u, v)
        quad_vertices = np.array([
            # Position   # TexCoord
            -1.0, -1.0, 0.0,  0.0, 0.0,
             1.0, -1.0, 0.0,  1.0, 0.0,
             1.0,  1.0, 0.0,  1.0, 1.0,
            -1.0, -1.0, 0.0,  0.0, 0.0,
             1.0,  1.0, 0.0,  1.0, 1.0,
            -1.0,  1.0, 0.0,  0.0, 1.0,
        ], dtype=np.float32)
        
        self._quad_vao = GL.glGenVertexArrays(1)
        self._quad_vbo = GL.glGenBuffers(1)
        
        GL.glBindVertexArray(self._quad_vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._quad_vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, quad_vertices.nbytes, quad_vertices, GL.GL_STATIC_DRAW)
        
        # Position attribute (location 0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 20, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(0)
        
        # TexCoord attribute (location 1)
        GL.glVertexAttribPointer(1, 2, GL.GL_FLOAT, GL.GL_FALSE, 20, ctypes.c_void_p(12))
        GL.glEnableVertexAttribArray(1)
        
        GL.glBindVertexArray(0)
    
    def execute(self, program: CompiledProgram, texture_id: int,
                uniforms: Dict[str, Any], in_place: bool = False,
                output_texture: Optional[int] = None,
                output_framebuffer: Optional[int] = None,
                input_framebuffer: Optional[int] = None) -> int:
        """
        Execute a shader filter on a texture or framebuffer.

        Args:
            program: The compiled shader program
            texture_id: Input texture ID (ignored if input_framebuffer is set)
            uniforms: Uniform values to pass to the shader
            in_place: If True, render back to the input texture
            output_texture: Optional output texture ID (creates new if None and not in_place)
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen).
                               If set, renders directly to this framebuffer instead of creating
                               an output texture. Returns 0 when using this mode.
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen).
                              If set, reads from this framebuffer instead of texture_id.
                              Will attempt to use the framebuffer's texture attachment if available.

        Returns:
            The output texture ID (or 0 if rendering to output_framebuffer)
        """
        self._ensure_initialized()
        GL = _get_gl()

        original_framebuffer = GL.glGetIntegerv(GL.GL_FRAMEBUFFER_BINDING)
        original_texture = GL.glGetIntegerv(GL.GL_TEXTURE_BINDING_2D)
        original_program = GL.glGetIntegerv(GL.GL_CURRENT_PROGRAM)
        original_vao = GL.glGetIntegerv(GL.GL_VERTEX_ARRAY_BINDING)

        # Handle input framebuffer - get texture from framebuffer if specified
        if input_framebuffer is not None:
            texture_id, width, height = self._get_texture_from_framebuffer(input_framebuffer)
        else:
            # Get texture dimensions from texture_id
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id)
            width = GL.glGetTexLevelParameteriv(GL.GL_TEXTURE_2D, 0, GL.GL_TEXTURE_WIDTH)
            height = GL.glGetTexLevelParameteriv(GL.GL_TEXTURE_2D, 0, GL.GL_TEXTURE_HEIGHT)

        # Handle direct framebuffer rendering (e.g., to main screen)
        if output_framebuffer is not None:
            # Render directly to the specified framebuffer
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, output_framebuffer)
            GL.glViewport(0, 0, width, height)
            out_tex = 0  # No output texture when rendering to framebuffer
        else:
            # Normal texture-based rendering
            # Get or create FBO and temp texture for this size
            fbo, temp_texture = self._get_fbo(width, height)

            # Determine output texture
            if in_place:
                # We need to render to a temp texture, then copy back
                out_tex = temp_texture
            elif output_texture is not None:
                out_tex = output_texture
            else:
                # Get cached output texture for this input (automatic per-texture caching)
                out_tex = self._get_cached_texture(texture_id, width, height)

            # Bind FBO with output texture
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                       GL.GL_TEXTURE_2D, out_tex, 0)

            # Set viewport
            GL.glViewport(0, 0, width, height)
        
        # Clear
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        
        # Use shader program
        GL.glUseProgram(program.program_id)
        
        # Bind input texture
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id)
        GL.glUniform1i(program.uniform_locations['u_texture'], 0)

        # Auto-populate texture_size if it's a required uniform and not explicitly provided
        if 'texture_size' in program.shader.uniforms and 'texture_size' not in uniforms:
            uniforms = {**uniforms, 'texture_size': (float(width), float(height))}

        # Set uniforms
        self._set_uniforms(program, uniforms)
        
        # Draw fullscreen quad
        GL.glBindVertexArray(self._quad_vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 6)
        GL.glBindVertexArray(0)
        
        # Unbind
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, original_framebuffer)
        GL.glBindTexture(GL.GL_TEXTURE_2D, original_texture)
        GL.glUseProgram(original_program)
        GL.glBindVertexArray(original_vao)

        # If rendering to framebuffer directly, we're done
        if output_framebuffer is not None:
            return 0

        # If in_place, copy result back to input texture
        if in_place:
            self._copy_texture(out_tex, texture_id, width, height)
            return texture_id

        return out_tex
    
    def _set_uniforms(self, program: CompiledProgram, uniforms: Dict[str, Any]) -> None:
        """Set uniform values."""
        for name, value in uniforms.items():
            if name not in program.uniform_locations:
                continue
            
            loc = program.uniform_locations[name]
            if loc < 0:
                continue
            
            gl_type, _ = program.shader.uniforms.get(name, (None, None))
            if gl_type is None:
                continue
            
            self._set_uniform_value(loc, gl_type, value)
    
    def _set_uniform_value(self, location: int, gl_type: GLType, value: Any) -> None:
        """Set a single uniform value."""
        GL = _get_gl()
        
        if gl_type == GLType.FLOAT:
            GL.glUniform1f(location, float(value))
        elif gl_type == GLType.VEC2:
            GL.glUniform2f(location, *value)
        elif gl_type == GLType.VEC3:
            GL.glUniform3f(location, *value)
        elif gl_type == GLType.VEC4:
            GL.glUniform4f(location, *value)
        elif gl_type == GLType.INT:
            GL.glUniform1i(location, int(value))
        elif gl_type == GLType.BOOL:
            GL.glUniform1i(location, 1 if value else 0)
        elif gl_type == GLType.MAT4:
            GL.glUniformMatrix4fv(location, 1, GL.GL_FALSE, value)
    
    def _get_fbo(self, width: int, height: int) -> Tuple[int, int]:
        """
        Get or create an FBO and temp texture for the given size.

        Args:
            width: Texture width
            height: Texture height

        Returns:
            Tuple of (fbo_id, temp_texture_id)
        """
        GL = _get_gl()
        size = (width, height)

        # Get or create FBO for this size
        if size not in self._fbo_cache:
            self._fbo_cache[size] = GL.glGenFramebuffers(1)

        # Get or create temp texture for this size
        if size not in self._temp_texture_cache:
            self._temp_texture_cache[size] = self._create_texture(width, height)

        return self._fbo_cache[size], self._temp_texture_cache[size]
    
    def _create_texture(self, width: int, height: int) -> int:
        """Create a new texture."""
        GL = _get_gl()

        texture = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, width, height, 0,
                        GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        return texture

    def _get_cached_texture(self, input_texture_id: int, width: int, height: int) -> int:
        """
        Get or create an output texture for the given input texture.

        Each input texture ID gets its own persistent output texture.
        If the size changes, the old texture is deleted and a new one is created.

        Args:
            input_texture_id: The input texture ID
            width: Desired texture width
            height: Desired texture height

        Returns:
            Output texture ID
        """
        GL = _get_gl()

        # Check if we have a cached texture for this input
        if input_texture_id in self._texture_cache:
            output_texture_id, cached_width, cached_height = self._texture_cache[input_texture_id]

            # Check if size matches
            if cached_width == width and cached_height == height:
                return output_texture_id

            # Size changed - delete old texture
            GL.glDeleteTextures(1, [output_texture_id])

        # Create new texture and cache it
        new_texture = self._create_texture(width, height)
        self._texture_cache[input_texture_id] = (new_texture, width, height)
        return new_texture

    def _get_texture_from_framebuffer(self, framebuffer_id: int) -> Tuple[int, int, int]:
        """
        Get a texture from a framebuffer's color attachment, or create one from its contents.

        Args:
            framebuffer_id: The framebuffer to read from (0 for main screen)

        Returns:
            Tuple of (texture_id, width, height)
        """
        GL = _get_gl()

        # Save current bindings
        original_fbo = GL.glGetIntegerv(GL.GL_FRAMEBUFFER_BINDING)
        original_read_fbo = GL.glGetIntegerv(GL.GL_READ_FRAMEBUFFER_BINDING)

        # Bind the framebuffer
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, framebuffer_id)

        # Get viewport dimensions (works for both 0 and FBOs)
        viewport = GL.glGetIntegerv(GL.GL_VIEWPORT)
        width = viewport[2]
        height = viewport[3]

        # Try to get the texture attachment (for non-zero FBOs)
        texture_id = None
        if framebuffer_id != 0:
            try:
                # Check what's attached to COLOR_ATTACHMENT0
                attachment_type = GL.glGetFramebufferAttachmentParameteriv(
                    GL.GL_FRAMEBUFFER,
                    GL.GL_COLOR_ATTACHMENT0,
                    GL.GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE
                )

                # If it's a texture, get the texture name
                if attachment_type == GL.GL_TEXTURE:
                    texture_id = GL.glGetFramebufferAttachmentParameteriv(
                        GL.GL_FRAMEBUFFER,
                        GL.GL_COLOR_ATTACHMENT0,
                        GL.GL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME
                    )
            except:
                # Attachment query failed, we'll copy instead
                texture_id = None

        # If we got a texture attachment, use it directly
        if texture_id is not None:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, original_fbo)
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, original_read_fbo)
            return (texture_id, width, height)

        # Otherwise, copy framebuffer contents to a temporary texture
        # Create or reuse temp texture for this size
        cache_key = f"fbo_input_{framebuffer_id}_{width}_{height}"
        if not hasattr(self, '_fbo_input_cache'):
            self._fbo_input_cache = {}

        if cache_key in self._fbo_input_cache:
            temp_texture = self._fbo_input_cache[cache_key]
        else:
            temp_texture = self._create_texture(width, height)
            self._fbo_input_cache[cache_key] = temp_texture

        # Copy framebuffer contents to the texture
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, framebuffer_id)
        GL.glBindTexture(GL.GL_TEXTURE_2D, temp_texture)
        GL.glCopyTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, 0, 0, width, height, 0)

        # Restore bindings
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, original_fbo)
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, original_read_fbo)

        return (temp_texture, width, height)

    def clear_texture_cache(self) -> None:
        """Clear all cached output textures."""
        GL = _get_gl()

        for output_texture_id, width, height in self._texture_cache.values():
            GL.glDeleteTextures(1, [output_texture_id])

        self._texture_cache.clear()

    def clear_fbo_cache(self) -> None:
        """Clear all cached FBOs and temp textures."""
        GL = _get_gl()

        # Clean up all cached FBOs
        for fbo_id in self._fbo_cache.values():
            GL.glDeleteFramebuffers(1, [fbo_id])
        self._fbo_cache.clear()

        # Clean up all temp textures
        for texture_id in self._temp_texture_cache.values():
            GL.glDeleteTextures(1, [texture_id])
        self._temp_texture_cache.clear()
    
    def _copy_texture(self, src: int, dst: int, width: int, height: int) -> None:
        """Copy texture data from src to dst."""
        GL = _get_gl()
        
        # Bind source to read framebuffer
        read_fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, read_fbo)
        GL.glFramebufferTexture2D(GL.GL_READ_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                   GL.GL_TEXTURE_2D, src, 0)
        
        # Bind destination to draw framebuffer
        draw_fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, draw_fbo)
        GL.glFramebufferTexture2D(GL.GL_DRAW_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                   GL.GL_TEXTURE_2D, dst, 0)
        
        # Blit
        GL.glBlitFramebuffer(0, 0, width, height, 0, 0, width, height,
                              GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
        
        # Cleanup
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        GL.glDeleteFramebuffers(2, [read_fbo, draw_fbo])
    
    def cleanup(self) -> None:
        """Clean up all resources."""
        GL = _get_gl()

        if self._initialized:
            if self._quad_vao is not None:
                GL.glDeleteVertexArrays(1, [self._quad_vao])
            if self._quad_vbo is not None:
                GL.glDeleteBuffers(1, [self._quad_vbo])

        # Clean up input framebuffer cache
        if hasattr(self, '_fbo_input_cache'):
            for texture_id in self._fbo_input_cache.values():
                GL.glDeleteTextures(1, [texture_id])
            self._fbo_input_cache.clear()

        # Clean up all caches
        self.clear_fbo_cache()
        self.clear_texture_cache()
