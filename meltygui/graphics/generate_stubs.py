#!/usr/bin/env python3
"""
Standalone script to generate type stub files.

Run from the shader_library directory:
    python3 generate_stubs.py
"""

import sys
import os

# Add parent directory to path for imports
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, parent_dir)

# Import to register all shaders
from src.shader_library.shader_manager.stub_generator import generate_filter_stub

# Generate the stub file
stub_path = os.path.join(os.path.dirname(__file__), '', 'filter.pyi')
content = generate_filter_stub(stub_path)

lines = len(content.splitlines())
print(f"✓ Generated {stub_path}")
print(f"✓ {lines} lines")
print(f"✓ Shaders included: {content.count('def ') - 20}")  # Subtract non-shader methods
