"""
Shader management system - core infrastructure for the Melty shader library.
"""

from .filter import Filter, FilterChain
from .registry import register_shader, register_shader_type, get_registry
from .base import ShaderType, Shader, GLType
from .compiler import ProgramCompiler, CompiledProgram, ShaderCompilationError
from .executor import FilterExecutor

__all__ = [
    'Filter',
    'FilterChain',
    'register_shader',
    'register_shader_type',
    'get_registry',
    'ShaderType',
    'Shader',
    'GLType',
    'ProgramCompiler',
    'CompiledProgram',
    'ShaderCompilationError',
    'FilterExecutor',
]
