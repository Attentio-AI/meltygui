import os
import subprocess
import inspect
import ast
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Union
import importlib


def find_function_line(file_path: str, function_name: str) -> Optional[int]:
    """
    Find the line number where a function is defined in a Python file.

    Args:
        file_path: Path to the Python file
        function_name: Name of the function to find

    Returns:
        Line number where the function is defined, or None if not found
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Parse the AST to find function definitions
        tree = ast.parse(content)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                return node.lineno

    except Exception as e:
        print(f"Error parsing file: {e}")

    return None


def find_class_line(file_path: str, class_name: str) -> Optional[int]:
    """
    Find the line number where a class is defined in a Python file.

    Args:
        file_path: Path to the Python file
        class_name: Name of the class to find

    Returns:
        Line number where the class is defined, or None if not found
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Parse the AST to find class definitions
        tree = ast.parse(content)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node.lineno

    except Exception as e:
        print(f"Error parsing file: {e}")

    return None


def find_intellij_executable() -> Optional[str]:
    """
    Find IntelliJ executable, handling version-specific installations.

    Returns:
        Path to IntelliJ executable, or None if not found
    """
    import glob

    # Common installation patterns
    search_patterns = []

    if os.name == 'nt':  # Windows
        search_patterns = [
            r'C:\Program Files\JetBrains\IntelliJ IDEA*\bin\idea64.exe',
            r'C:\Program Files (x86)\JetBrains\IntelliJ IDEA*\bin\idea.exe',
            r'C:\Program Files\JetBrains\IntelliJ IDEA Community Edition*\bin\idea64.exe',
        ]
    elif os.name == 'posix':  # Linux/macOS
        search_patterns = [
            # Linux patterns
            '/opt/idea-*/bin/idea.sh',
            '/opt/intellij-idea-*/bin/idea.sh',
            '/usr/local/idea-*/bin/idea.sh',
            '/home/*/idea-*/bin/idea.sh',
            '/home/*/.local/share/JetBrains/Toolbox/apps/IDEA-U/ch-*/*/bin/idea.sh',
            '/home/*/.local/share/JetBrains/Toolbox/apps/IDEA-C/ch-*/*/bin/idea.sh',
            # Snap installations
            '/snap/intellij-idea-ultimate/current/bin/idea.sh',
            '/snap/intellij-idea-community/current/bin/idea.sh',
        ]

        if os.uname().sysname == 'Darwin':  # macOS
            search_patterns.extend([
                '/Applications/IntelliJ IDEA.app/Contents/MacOS/idea',
                '/Applications/IntelliJ IDEA CE.app/Contents/MacOS/idea',
                '/Users/*/Applications/IntelliJ IDEA.app/Contents/MacOS/idea',
                '/Applications/JetBrains Toolbox/IntelliJ IDEA.app/Contents/MacOS/idea',
            ])

    # Search for IntelliJ in common locations
    for pattern in search_patterns:
        matches = glob.glob(pattern)
        if matches:
            # Return the latest version if multiple found
            return max(matches)

    # Check environment variable
    intellij_home = os.environ.get('INTELLIJ_HOME')
    if intellij_home:
        idea_path = os.path.join(intellij_home, 'bin', 'idea.sh' if os.name == 'posix' else 'idea64.exe')
        if os.path.exists(idea_path):
            return idea_path

    return None


def open_in_intellij(file_path, line_number=None):
    # Use absolute path

    idea_path = find_intellij_executable()

    # Check if file exists
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return

    project_root = "/home/lukas/Desktop/latent-descent"

    if line_number:
        # Open project, then file with line number
        cmd = [f'{idea_path}', project_root, '--line', str(line_number), file_path]
    else:
        cmd = [f'{idea_path}', project_root, file_path]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def build_function_map(module) -> Dict[str, Dict[str, Any]]:
    """
    Build a function map from a module with function metadata.

    Args:
        module: The module to inspect

    Returns:
        Dictionary mapping function names to their metadata
    """
    # Get all functions from the module
    functions = inspect.getmembers(module, inspect.isfunction)

    # Filter to only functions defined in this module
    functions = [(name, obj) for name, obj in functions
                 if obj.__module__ == module.__name__]

    function_map = {}

    for name, func in functions:
        # Get the source file
        try:
            source_file = inspect.getsourcefile(func)
            source_lines, start_line = inspect.getsourcelines(func)

            function_map[name] = {
                'function': func,
                'file': source_file,
                'line': start_line,
                'module': module.__name__,
                'docstring': inspect.getdoc(func),
                'signature': str(inspect.signature(func))
            }
        except Exception as e:
            print(f"Error processing function {name}: {e}")

    return function_map


def build_class_map(module) -> Dict[str, Dict[str, Any]]:
    """
    Build a class map from a module with class metadata.

    Args:
        module: The module to inspect

    Returns:
        Dictionary mapping class names to their metadata
    """
    # Get all classes from the module
    classes = inspect.getmembers(module, inspect.isclass)

    # Filter to only classes defined in this module
    classes = [(name, obj) for name, obj in classes
               if obj.__module__ == module.__name__]

    class_map = {}

    for name, cls in classes:
        # Get the source file
        try:
            source_file = inspect.getsourcefile(cls)
            source_lines, start_line = inspect.getsourcelines(cls)

            class_map[name] = {
                'class': cls,
                'file': source_file,
                'line': start_line,
                'module': module.__name__,
                'docstring': inspect.getdoc(cls),
                'bases': [base.__name__ for base in cls.__bases__],
                'methods': [method for method in dir(cls) if not method.startswith('_')]
            }
        except Exception as e:
            print(f"Error processing class {name}: {e}")

    return class_map


def build_combined_map(module) -> Dict[str, Dict[str, Any]]:
    """
    Build a combined map of functions and classes from a module.

    Args:
        module: The module to inspect

    Returns:
        Dictionary mapping names to their metadata (functions and classes)
    """
    combined_map = {}

    # Add functions
    function_map = build_function_map(module)
    for name, info in function_map.items():
        info['type'] = 'function'
        combined_map[name] = info

    # Add classes
    class_map = build_class_map(module)
    for name, info in class_map.items():
        info['type'] = 'class'
        combined_map[name] = info

    return combined_map


def jump_to_function(root, function_name: str) -> bool:
    """
    Jump to a function in IntelliJ using the root's jump map.

    Args:
        root: The root object containing the _jump_to_map
        function_name: Name of the function to jump to

    Returns:
        True if successful, False otherwise
    """
    a_function_map = root.view_functions._jump_to_map
    return jump_to_item_by_map(a_function_map, function_name)


def jump_to_class(root, class_name: str) -> bool:
    """
    Jump to a class in IntelliJ using the root's jump map.

    Args:
        root: The root object containing the _jump_to_map
        class_name: Name of the class to jump to

    Returns:
        True if successful, False otherwise
    """
    a_class_map = root.datatypes._jump_to_map
    return jump_to_item_by_map(a_class_map, class_name)


def jump_to_item(root, item_name: str) -> bool:
    """
    Jump to a function or class in IntelliJ using the root's jump map.

    Args:
        root: The root object containing the _jump_to_map
        item_name: Name of the function or class to jump to

    Returns:
        True if successful, False otherwise
    """
    jump_map = root.view_functions._jump_to_map
    return jump_to_item_by_map(jump_map, item_name)


def jump_to_function_by_map(function_map: Dict[str, Dict[str, Any]], function_name: str) -> bool:
    """
    Jump to a function in IntelliJ using the function map.
    (Deprecated: Use jump_to_item_by_map instead)

    Args:
        function_map: Dictionary mapping function names to metadata
        function_name: Name of the function to jump to

    Returns:
        True if successful, False otherwise
    """
    return jump_to_item_by_map(function_map, function_name)


def jump_to_item_by_map(item_map: Dict[str, Dict[str, Any]], item_name: str) -> bool:
    """
    Jump to a function or class in IntelliJ using the item map.

    Args:
        item_map: Dictionary mapping item names to metadata
        item_name: Name of the function or class to jump to

    Returns:
        True if successful, False otherwise
    """
    if item_name not in item_map:
        return False

    item_info = item_map[item_name]
    file_path = item_info['file']
    line_number = item_info['line']

    return open_in_intellij(file_path, line_number=line_number)
