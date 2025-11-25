"""
Generate type stub files (.pyi) for IDE autocomplete support.

This module generates stub files that IDEs can read to provide
autocomplete and type hints for dynamically generated shader methods.
"""

from typing import Dict, Any, Tuple
from pathlib import Path
from .registry import ShaderRegistry, get_registry
from .base import GLType


def _gltype_to_python_type(gltype: GLType) -> str:
    """Map GLType to Python type hint string."""
    type_map = {
        GLType.FLOAT: 'float',
        GLType.INT: 'int',
        GLType.BOOL: 'bool',
        GLType.VEC2: 'Tuple[float, float]',
        GLType.VEC3: 'Tuple[float, float, float]',
        GLType.VEC4: 'Tuple[float, float, float, float]',
        GLType.IVEC2: 'Tuple[int, int]',
        GLType.IVEC3: 'Tuple[int, int, int]',
        GLType.IVEC4: 'Tuple[int, int, int, int]',
        GLType.MAT3: 'Tuple[float, ...]',
        GLType.MAT4: 'Tuple[float, ...]',
    }
    return type_map.get(gltype, 'Any')


def _format_default_value(value: Any) -> str:
    """Format a default value for use in type stub."""
    if isinstance(value, str):
        return f'"{value}"'
    elif isinstance(value, (list, tuple)):
        return str(tuple(value))
    elif isinstance(value, bool):
        return str(value)
    elif isinstance(value, (int, float)):
        return str(value)
    else:
        return repr(value)


def generate_filter_stub(output_path: str = None) -> str:
    """
    Generate a type stub file for the Filter class.

    Args:
        output_path: Optional path to write the stub file.
                    If None, returns the stub content as a string.

    Returns:
        The generated stub content
    """
    registry = get_registry()

    # Header
    lines = [
        '"""',
        'Type stub file for Filter class.',
        '',
        'This file is auto-generated to provide IDE autocomplete for',
        'dynamically created shader methods.',
        '',
        'To regenerate: python -m shader_manager.stub_generator',
        '"""',
        '',
        'from typing import Optional, Dict, Any, Tuple, List',
        'from contextlib import AbstractContextManager',
        'from .compiler import CompiledProgram',
        'from .registry import ShaderRegistry',
        '',
        '',
        'class Filter(AbstractContextManager):',
        '    """',
        '    Main interface for shader-based texture filtering.',
        '    ',
        '    All registered shaders are available as methods for IDE autocomplete.',
        '    """',
        '    ',
        '    def __init__(self, auto_compile: bool = True) -> None: ...',
        '    ',
    ]

    # Generate method stubs for each registered shader
    for shader_name, shader in sorted(registry.shaders.items()):
        # Build uniform parameter hints from shader definition
        uniform_params = []
        uniform_docs = []

        for uname, (utype, default) in sorted(shader.uniforms.items()):
            python_type = _gltype_to_python_type(utype)
            default_str = _format_default_value(default)
            uniform_params.append(f'        {uname}: {python_type} = {default_str},')
            uniform_docs.append(f'            {uname}: Shader uniform ({python_type})')

        # Build method signature
        method_lines = [
            f'    def {shader_name}(',
            '        self,',
            '        texture_id: int,',
            '        in_place: bool = False,',
        ]

        # Add uniform parameters
        method_lines.extend(uniform_params)

        method_lines.append('        output_texture: Optional[int] = None,')

        # Close signature
        method_lines.append('    ) -> int:')

        # Add docstring
        method_lines.extend([
            '        """',
            f'        Apply {shader_name} shader to a texture.',
            '        ',
            '        Args:',
            '            texture_id: Input texture ID',
            '            in_place: If True, modify the input texture directly',
            '            output_texture: Optional specific output texture to render to',
        ])

        # Add uniform documentation
        if uniform_docs:
            method_lines.extend(uniform_docs)

        method_lines.extend([
            '        ',
            '        Returns:',
            '            Output texture ID',
            '        """',
            '        ...',
            '    ',
        ])

        lines.extend(method_lines)

    # # Add the rest of the Filter class methods
    # lines.extend([
    #     '    def apply(',
    #     '        self,',
    #     '        shader_name: str,',
    #     '        texture_id: int,',
    #     '        in_place: bool = False,',
    #     '        output_texture: Optional[int] = None,',
    #     '        **uniforms',
    #     '    ) -> int: ...',
    #     '    ',
    #     '    def chain(self) -> FilterChain: ...',
    #     '    ',
    #     '    def compile_shader(self, name: str) -> CompiledProgram: ...',
    #     '    ',
    #     '    def compile_all(self) -> Dict[str, CompiledProgram]: ...',
    #     '    ',
    #     '    def has_shader(self, name: str) -> bool: ...',
    #     '    ',
    #     '    def list_shaders(self) -> List[str]: ...',
    #     '    ',
    #     '    def list_shader_types(self) -> List[str]: ...',
    #     '    ',
    #     '    def get_shader_info(self, name: str) -> Optional[Dict[str, Any]]: ...',
    #     '    ',
    #     '    def use_shader(self, name: str) -> AbstractShaderProgram: ...',
    #     '    ',
    #     '    def clear_texture_cache(self) -> None: ...',
    #     '    ',
    #     '    def clear_fbo_cache(self) -> None: ...',
    #     '    ',
    #     '    def get_cache_stats(self) -> Dict[str, Any]: ...',
    #     '    ',
    #     '    def cleanup(self) -> None: ...',
    #     '    ',
    #     '    def __enter__(self) -> Filter: ...',
    #     '    ',
    #     '    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...',
    #     '',
    #     '',
    #     'class FilterChain:',
    #     '    """Builder pattern for chaining multiple filters."""',
    #     '    ',
    #     '    def __init__(self, filter_instance: Filter) -> None: ...',
    #     '    ',
    # ])
    #
    # # Add chain methods for each chain
    # for shader_name, shader in sorted(registry.shaders.items()):
    #     # Build uniform parameters for chain method
    #     chain_params = []
    #     for uname, (utype, default) in sorted(shader.uniforms.items()):
    #         python_type = _gltype_to_python_type(utype)
    #         default_str = _format_default_value(default)
    #         chain_params.append(f'{uname}: {python_type} = {default_str}')
    #
    #     # Build method signature
    #     if chain_params:
    #         params_str = ', '.join(chain_params)
    #         lines.extend([
    #             f'    def {shader_name}(self, {params_str}) -> FilterChain: ...',
    #             '    ',
    #         ])
    #     else:
    #         lines.extend([
    #             f'    def {shader_name}(self) -> FilterChain: ...',
    #             '    ',
    #         ])
    #
    # lines.extend([
    #     '    def apply(self, texture_id: int, in_place: bool = False) -> int: ...',
    #     '    ',
    #     '    def clear(self) -> FilterChain: ...',
    #     '    ',
    #     '    def __call__(self) -> int: ...',
    #     '',
    # ])

    stub_content = '\n'.join(lines)

    # Write to file if path provided
    if output_path:
        path = Path(output_path)
        path.write_text(stub_content)
        print(f"Generated stub file: {path}")

    return stub_content


def main():
    """Generate stub file when run as a module."""
    import os
    import sys

    # Ensure shaders are registered by importing from parent package
    parent_dir = os.path.dirname(os.path.dirname(__file__))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    # Import main package which triggers shader registration
    try:
        from shader_library import Filter  # noqa: F401
    except ImportError:
        # Fallback: try importing __init__ directly
        import importlib.util
        init_path = os.path.join(os.path.dirname(__file__), '..', '__init__.py')
        spec = importlib.util.spec_from_file_location("shader_library", init_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    # Generate stub file next to filter.py
    stub_path = os.path.join(os.path.dirname(__file__), 'filter.pyi')
    generate_filter_stub(stub_path)


if __name__ == '__main__':
    main()
