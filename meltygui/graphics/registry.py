"""
Shader and shader type registry with decorator-based registration.
"""

from typing import Dict, Type, Any, Optional, Callable
from dataclasses import dataclass, field

from src.shader_library.shader_manager.base import ShaderType, Shader, camel_to_snake, GLType


class ShaderRegistry:
    """
    Central registry for shader types and shaders.
    
    This is a singleton that holds all registered shader types and shaders.
    """
    _instance: Optional['ShaderRegistry'] = None
    
    def __new__(cls) -> 'ShaderRegistry':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._shader_types: Dict[str, ShaderType] = {}
            cls._instance._shaders: Dict[str, Shader] = {}
        return cls._instance
    
    @property
    def shader_types(self) -> Dict[str, ShaderType]:
        return self._shader_types
    
    @property
    def shaders(self) -> Dict[str, Shader]:
        return self._shaders
    
    def register_type(self, shader_type: ShaderType) -> None:
        """Register a shader type."""
        self._shader_types[shader_type.name] = shader_type
    
    def register_shader(self, shader: Shader) -> None:
        """Register a shader."""
        self._shaders[shader.name] = shader
    
    def get_type(self, name: str) -> Optional[ShaderType]:
        """Get a shader type by name."""
        return self._shader_types.get(name)
    
    def get_shader(self, name: str) -> Optional[Shader]:
        """Get a shader by name."""
        return self._shaders.get(name)
    
    def clear(self) -> None:
        """Clear all registrations (mainly for testing)."""
        self._shader_types.clear()
        self._shaders.clear()


# Global registry instance

_registry = ShaderRegistry()


def get_registry() -> ShaderRegistry:
    """Get the global shader registry."""
    return _registry


def register_shader_type(cls: Type) -> Type:
    """
    Decorator to register a shader type.
    
    The type name is inferred from the class name (converted to snake_case).
    
    Example:
        @register_shader_type
        class Standard:
            vertex_in = {'position': GLType.VEC3, 'texcoord': GLType.VEC2}
            vertex_out = {'v_texcoord': GLType.VEC2}
            vertex_code = '''
                void main() {
                    v_texcoord = texcoord;
                    gl_Position = vec4(position, 1.0);
                }
            '''
    """
    name = camel_to_snake(cls.__name__)
    
    # Extract attributes from class
    vertex_in = getattr(cls, 'vertex_in', {})
    vertex_out = getattr(cls, 'vertex_out', {})
    vertex_code = getattr(cls, 'vertex_code', '')
    
    # Convert string type names to GLType if needed
    vertex_in = _normalize_type_dict(vertex_in)
    vertex_out = _normalize_type_dict(vertex_out)
    
    shader_type = ShaderType(
        name=name,
        vertex_in=vertex_in,
        vertex_out=vertex_out,
        vertex_code=vertex_code
    )
    
    _registry.register_type(shader_type)
    
    # Store the ShaderType on the class for reference
    cls._shader_type = shader_type
    
    return cls


def register_shader(cls: Type = None, *, shader_type: str = 'standard') -> Callable:
    """
    Decorator to register a shader.
    
    The shader name is inferred from the class name (converted to snake_case).
    
    Can be used with or without arguments:
        @register_shader
        class BrightnessContrast:
            ...
        
        @register_shader(shader_type='fullscreen')
        class GaussianBlur:
            ...
    
    Example:
        @register_shader
        class BrightnessContrast:
            uniforms = {
                'brightness': (GLType.FLOAT, 0.0),
                'contrast': (GLType.FLOAT, 1.0),
            }
            fragment_code = '''
                void main() {
                    vec4 color = texture(u_texture, v_texcoord);
                    color.rgb = (color.rgb - 0.5) * contrast + 0.5 + brightness;
                    fragColor = color;
                }
            '''
    """
    def decorator(cls: Type) -> Type:
        name = camel_to_snake(cls.__name__)
        
        # Extract attributes from class
        uniforms = getattr(cls, 'uniforms', {})
        fragment_code = getattr(cls, 'fragment_code', '')
        vertex_code = getattr(cls, 'vertex_code', None)
        type_name = getattr(cls, 'shader_type', shader_type)
        
        # Normalize uniforms
        uniforms = _normalize_uniforms(uniforms)
        
        shader = Shader(
            name=name,
            shader_type=type_name,
            fragment_code=fragment_code,
            vertex_code=vertex_code,
            uniforms=uniforms
        )
        
        _registry.register_shader(shader)
        
        # Store the Shader on the class for reference
        cls._shader = shader
        
        return cls
    
    # Handle both @register_shader and @register_shader() syntax
    if cls is not None:
        return decorator(cls)
    return decorator


def _normalize_type_dict(d: Dict[str, Any]) -> Dict[str, GLType]:
    """Convert string type names to GLType enum values."""
    result = {}
    for name, type_val in d.items():
        if isinstance(type_val, str):
            type_val = GLType(type_val)
        elif isinstance(type_val, GLType):
            pass
        else:
            print(f"Warning: Unexpected type for {name}: {type_val}")
        result[name] = type_val
    return result


def _normalize_uniforms(uniforms: Dict[str, Any]) -> Dict[str, tuple]:
    """Normalize uniform definitions to (GLType, default) tuples."""
    result = {}
    for name, value in uniforms.items():
        if isinstance(value, tuple):
            gl_type, default = value
            if isinstance(gl_type, str):
                gl_type = GLType(gl_type)
            result[name] = (gl_type, default)
        elif isinstance(value, GLType):
            result[name] = (value, None)
        else:
            print(f"Warning: Unexpected uniform definition for {name}: {value}")
    return result
