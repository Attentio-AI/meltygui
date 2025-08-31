import importlib
import inspect
import os
import sys
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from src.lsd.gl_gui.utils.custom_views import print_stack_trace


def find_repo_root(start_path: Path | str = None) -> Path:
    # Check if __file is defined
    if '__file__' in globals():
        if start_path is None:
            start_path = Path(os.path.dirname(os.path.abspath(__file__)))

    # If start_path is not provided, use the current working directory
    if start_path is None:
        start_path = os.getcwd()

    current = Path(start_path).resolve()

    while current != current.parent:
        if (current / '.git').exists():
            return current
        current = current.parent

    # If we reach here, we didn't find a .git directory, use module path
    module_path = Path(__file__).resolve().parent
    print(f"Warning: No .git directory found. Using module path: {module_path}")
    return module_path.parent if module_path.parent.exists() else module_path


def find_nested_classes(parent_class: type, parent_path: str) -> List[Tuple[str, type]]:
    """
    Recursively find all nested classes within a class.

    Args:
        parent_class: The parent class to search in
        parent_path: The full path of the parent class

    Returns:
        A list of tuples (full_class_path, class_object) for nested classes
    """
    nested_classes = []

    # Check all attributes of the class
    for name, obj in parent_class.__dict__.items():
        # Skip special methods, private attributes, and non-classes
        if name.startswith('__'):
            continue

        if not isinstance(obj, type):
            continue

        # Build the path for this nested class
        class_path = f"{parent_path}.{name}"
        nested_classes.append((class_path, obj))

        # Recursively find classes nested within this class
        inner_classes = find_nested_classes(obj, class_path)
        nested_classes.extend(inner_classes)

    return nested_classes

def singleton(cls):
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance


@singleton
class ClassUtility:

    def __init__(self):
        self.class_names: Optional[Dict[str, str]] = None
        self.modules_imported = set()
        self.root = None
        self.initialized_modules = {}

    def initialize_class_names(self, root=None):
        if self.root is not None:
            root = self.root

        if root is not None:
            self.root = root

        if root == "" or root is None:
            package_name = ""
        else:
            package_name = f"{root}"

        if package_name in self.modules_imported:
            return

        self.modules_imported.add(package_name)

        if self.class_names is None:
            self.class_names = {}

        repo_root = find_repo_root()
        # Check if repo_root/root exists

        root_dir = f"{find_repo_root()}/{root}"
        if not os.path.exists(root_dir):
            root_dir = f"{repo_root}/src/{root}"

        if root is None:
            root_dir = f"{repo_root}/src/"

        class_names_list = ClassUtility().find_all_classes(str(root_dir), package_name)
        for the_class_name, the_class_path in class_names_list:
            self.class_names[the_class_name] = the_class_path



    @staticmethod
    def find_all_classes(root_dir: str, package_name: str = None) -> List[Tuple[str, str]]:
        """
        Find all classes in a package directory structure, including nested directories and nested classes.

        Args:
            root_dir: The root directory to search in
            package_name: Optional base package name prefix

        Returns:
            A list of tuples (full_class_path, class_object)
        """
        classes = []
        visited_modules = set()

        # Check if path exists
        if not os.path.exists(root_dir):
            root_dir = os.path.abspath(f"./{root_dir}")

        if not os.path.isdir(root_dir):
            print(f"Error: {root_dir} is not a directory")
            # Print stack
            print_stack_trace()
            return classes

        # Make sure the root directory is in the Python path
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
            # Also add parent directory to handle package imports
            parent_dir = os.path.dirname(root_dir)
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)

        # Walk through all Python files in the directory structure
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Skip __pycache__ and other hidden directories
            dirnames[:] = [d for d in dirnames if not d.startswith('__') and not d.startswith('.')]

            # Get the relative path from the root directory
            rel_path = os.path.relpath(dirpath, root_dir)

            # Convert directory path to module path
            if rel_path == '.':
                module_prefix = package_name or os.path.basename(root_dir)
            else:
                module_prefix = f"{package_name or os.path.basename(root_dir)}.{rel_path.replace(os.sep, '.')}"

            # Process Python files in this directory
            for filename in filenames:
                if filename.endswith('.py') and not filename.startswith('__'):
                    module_name = f"{module_prefix}.{filename[:-3]}"

                    # Skip already visited modules
                    if module_name in visited_modules:
                        continue

                    visited_modules.add(module_name)
                    if "app_model" in module_name:
                        pass
                    try:
                        # Import the module
                        module = importlib.import_module(module_name)
                        # importlib.reload(module)

                        # Find top-level classes in this module and add them
                        for name, obj in inspect.getmembers(module, inspect.isclass):
                            # Only include classes defined in this module (not imported)
                            # Add class
                            if obj.__module__ == module_name:
                                class_path = f"{module_name}.{name}"
                                parts = class_path.split('.')
                                classes.append((parts[-1], class_path))

                                # Now find nested classes
                                nested_classes = find_nested_classes(obj, class_path)
                                if len(nested_classes) > 0:
                                    for nested_class_path, nested_class in nested_classes:
                                        nested_parts = nested_class_path.split('.')
                                        nested_key = f"{nested_parts[-2]}.{nested_parts[-1]}"
                                        classes.append((nested_key, nested_class_path))

                    except (ImportError, ModuleNotFoundError) as e:
                        print(f"Error importing {module_name}: {e}")

        return classes
