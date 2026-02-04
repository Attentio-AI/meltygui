"""
OpenGL shader program compilation and management.
"""

from typing import Dict, Optional, Any, Tuple
from dataclasses import dataclass
import ctypes

from src.shader_library.shader_manager.base import ShaderType, Shader

# GLSL version header
GLSL_VERSION = "#version 330 core\n"


def _get_gl():
    """Lazy import of OpenGL."""
    from OpenGL import GL
    from OpenGL.GL import shaders
    return GL, shaders


@dataclass
class CompiledProgram:
    """A compiled shader program with its metadata."""
    program_id: int
    shader: Shader
    shader_type: ShaderType
    uniform_locations: Dict[str, int]
    attribute_locations: Dict[str, int]


class ProgramCompiler:
    """
    Compiles and manages OpenGL shader programs.
    """
    
    def __init__(self):
        """Initialize the program compiler."""
        self._programs: Dict[str, CompiledProgram] = {}
    
    def compile(self, shader: Shader, shader_type: ShaderType) -> CompiledProgram:
        """
        Compile a shader into an OpenGL program.
        
        Args:
            shader: The shader definition
            shader_type: The shader type definition
            
        Returns:
            CompiledProgram with the program ID and metadata
        """
        GL, shaders = _get_gl()
        
        # Generate full shader source code
        vertex_src = self._generate_vertex_source(shader, shader_type)
        fragment_src = self._generate_fragment_source(shader, shader_type)
        
        try:
            vertex_shader = shaders.compileShader(vertex_src, GL.GL_VERTEX_SHADER)
            fragment_shader = shaders.compileShader(fragment_src, GL.GL_FRAGMENT_SHADER)
            program_id = shaders.compileProgram(vertex_shader, fragment_shader)
        except shaders.ShaderCompilationError as e:
            raise ShaderCompilationError(f"Failed to compile shader '{shader.name}': {e}")
        
        # Get uniform locations
        uniform_locations = {}
        for name in shader.uniforms.keys():
            uniform_locations[name] = GL.glGetUniformLocation(program_id, name)
        uniform_locations['u_texture'] = GL.glGetUniformLocation(program_id, 'u_texture')
        uniform_locations['u_projection'] = GL.glGetUniformLocation(program_id, 'u_projection')
        uniform_locations['u_modelview'] = GL.glGetUniformLocation(program_id, 'u_modelview')
        
        # Get attribute locations
        attribute_locations = {}
        for name in shader_type.vertex_in.keys():
            attribute_locations[name] = GL.glGetAttribLocation(program_id, name)
        
        compiled = CompiledProgram(
            program_id=program_id,
            shader=shader,
            shader_type=shader_type,
            uniform_locations=uniform_locations,
            attribute_locations=attribute_locations
        )
        
        self._programs[shader.name] = compiled
        return compiled
    
    def _generate_vertex_source(self, shader: Shader, shader_type: ShaderType) -> str:
        """Generate the complete vertex shader source."""
        parts = [GLSL_VERSION]
        
        # Add input declarations
        for name, gl_type in shader_type.vertex_in.items():
            parts.append(f"in {gl_type.value} {name};")
        
        # Add output declarations (varyings)
        for name, gl_type in shader_type.vertex_out.items():
            parts.append(f"out {gl_type.value} {name};")
        
        # Add common uniforms
        parts.append("uniform mat4 u_projection;")
        parts.append("uniform mat4 u_modelview;")
        
        # Add the vertex code
        vertex_code = shader.vertex_code if shader.vertex_code else shader_type.vertex_code
        parts.append(vertex_code)
        
        return '\n'.join(parts)
    
    def _generate_fragment_source(self, shader: Shader, shader_type: ShaderType) -> str:
        """Generate the complete fragment shader source."""
        parts = [GLSL_VERSION]
        
        # Add input declarations (varyings from vertex shader)
        for name, gl_type in shader_type.vertex_out.items():
            parts.append(f"in {gl_type.value} {name};")
        
        # Add output declaration
        parts.append("out vec4 fragColor;")
        
        # Add texture sampler
        parts.append("uniform sampler2D u_texture;")
        
        # Add shader-specific uniforms
        for name, (gl_type, _) in shader.uniforms.items():
            parts.append(f"uniform {gl_type.value} {name};")
        
        # Add the fragment code
        parts.append(shader.fragment_code)
        
        return '\n'.join(parts)
    
    def get_program(self, name: str) -> Optional[CompiledProgram]:
        """Get a compiled program by shader name."""
        return self._programs.get(name)
    
    def delete_program(self, name: str) -> None:
        """Delete a compiled program."""
        if name in self._programs:
            GL, _ = _get_gl()
            compiled = self._programs[name]
            GL.glDeleteProgram(compiled.program_id)
            del self._programs[name]
    
    def cleanup(self) -> None:
        """Clean up all compiled programs."""
        for name in list(self._programs.keys()):
            self.delete_program(name)


class ShaderCompilationError(Exception):
    """Raised when shader compilation fails."""
    pass
