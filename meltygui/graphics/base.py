"""
Base classes for shader types and shaders.
"""

from abc import ABC
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from enum import Enum


class GLType(Enum):
    """OpenGL data types for shader attributes and uniforms."""
    FLOAT = 'float'
    VEC2 = 'vec2'
    VEC3 = 'vec3'
    VEC4 = 'vec4'
    MAT2 = 'mat2'
    MAT3 = 'mat3'
    MAT4 = 'mat4'
    INT = 'int'
    IVEC2 = 'ivec2'
    IVEC3 = 'ivec3'
    IVEC4 = 'ivec4'
    SAMPLER2D = 'sampler2D'
    BOOL = 'bool'


@dataclass
class ShaderType:
    """
    Defines a shader type (vertex shader structure).
    
    Attributes:
        name: The type name (inferred from class name)
        vertex_in: Input attributes {name: GLType}
        vertex_out: Output varyings {name: GLType}
        vertex_code: The vertex shader code template
    """
    name: str
    vertex_in: Dict[str, GLType] = field(default_factory=dict)
    vertex_out: Dict[str, GLType] = field(default_factory=dict)
    vertex_code: str = ""
    
    def generate_vertex_declarations(self) -> str:
        """Generate GLSL declarations for vertex shader inputs/outputs."""
        lines = []
        for attr_name, attr_type in self.vertex_in.items():
            lines.append(f"in {attr_type.value} {attr_name};")
        for var_name, var_type in self.vertex_out.items():
            lines.append(f"out {var_type.value} {var_name};")
        return '\n'.join(lines)


@dataclass
class Shader:
    """
    Defines a complete shader (fragment shader with optional vertex override).
    
    Attributes:
        name: The shader name (inferred from class name, converted to snake_case)
        shader_type: The ShaderType this shader uses
        fragment_code: The fragment shader code
        vertex_code: Optional vertex shader code override
        uniforms: Uniform definitions {name: (GLType, default_value)}
    """
    name: str
    shader_type: Optional[str] = None  # Name of shader type to use
    fragment_code: str = ""
    vertex_code: Optional[str] = None  # Override vertex code if provided
    uniforms: Dict[str, tuple] = field(default_factory=dict)  # {name: (GLType, default)}
    
    def get_uniform_declarations(self) -> str:
        """Generate GLSL uniform declarations."""
        lines = []
        for name, (gl_type, _) in self.uniforms.items():
            lines.append(f"uniform {gl_type.value} {name};")
        return '\n'.join(lines)


def camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    import re
    # Insert underscore before uppercase letters and convert to lowercase
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
