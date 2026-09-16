"""
Generate type stub files (.pyi) for IDE autocomplete support.

This module generates stub files that IDEs can read to provide
autocomplete and type hints for dynamically generated shader methods.
"""
from pathlib import Path
from typing import Any

from meltygui.graphics.base import GLType
from meltygui.graphics.registry import get_registry


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
        'from .shader_compiler import CompiledProgram',
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
            '        texture_id: int = 0,',
            '        in_place: bool = False,',
        ]

        # Add uniform parameters
        method_lines.extend(uniform_params)

        method_lines.append('        output_texture: Optional[int] = None,')
        method_lines.append('        output_framebuffer: Optional[int] = None,')
        method_lines.append('        input_framebuffer: Optional[int] = None,')
        method_lines.append('        output_size: Optional[Tuple[int, int]] = None,')

        # Close signature
        method_lines.append('    ) -> int:')

        # Add docstring
        method_lines.extend([
            '        """',
            f'        Apply {shader_name} shader to a texture or framebuffer.',
            '        ',
            '        Args:',
            '            texture_id: Input texture ID (ignored if input_framebuffer set)',
            '            in_place: If True, modify the input texture directly',
        ])

        # Add uniform documentation
        if uniform_docs:
            method_lines.extend(uniform_docs)

        method_lines.extend([
            '            output_texture: Optional specific output texture to render to',
            '            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)',
            '            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)',
            '            output_size: Optional (width, height) render resolution; runs the shader',
            '                at a lower resolution while sampling the full-res input',
            '        ',
            '        Returns:',
            '            Output texture ID (0 if rendering to framebuffer)',
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

    from meltygui.graphics import Filter  # noqa: F401

    # Generate stub file next to filter.py
    stub_path = os.path.join(os.path.dirname(__file__), 'filter.pyi')
    generate_filter_stub(stub_path)


if __name__ == '__main__':
    main()
