import importlib
import inspect
import os
import sys
import uuid
import weakref
from ast import literal_eval
from copy import copy, deepcopy
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Union, List, Tuple

import torch
from torch import Tensor, nn
from transformers import PreTrainedTokenizerBase, LlamaTokenizerFast

from src.lsd.gl_gui.model.class_utill import ClassUtility
from src.lsd.gl_gui.model.global_undo_redo_manager import TrackedList, TrackedDict, TrackedSet, GlobalUndoRedoManager
from src.lsd.gl_gui.utils.custom_views import print_stack_trace

class DictConversion:

    def save(self, save_file: str):
        view_dict = self.to_dict()
        path_dir = os.path.dirname(save_file)
        os.makedirs(path_dir, exist_ok=True)
        with open(save_file, "w") as f:
            f.write(str(view_dict["objects"]))

    @staticmethod
    def load(cls, path: str):
        """
        Load a DictConversion object from a file.

        example usage: DictConversion.load(ServerModel, "/server/lsd-server.ini")
        """
        ClassUtility().initialize_class_names(ClassUtility().root)

        # Add cls to the class names if not already present
        # This is to ensure that the class can be found in the class_names dictionary
        if cls.__name__ not in ClassUtility().class_names:
            ClassUtility().class_names[cls.__name__] = cls.__module__ + "." + cls.__name__

        # Create instance of the dict conversion class
        # and load the class names from the server

        # Check if the class is a subclass of DictConversion
        if not issubclass(cls, DictConversion):
            raise TypeError(f"{cls.__name__} is not a subclass of DictConversion")
        instance = cls()

        save_file = f'{path}'

        if os.path.exists(save_file):
            with open(save_file, "r") as f:
                view_dict = f.read()
                loaded_dict = literal_eval(view_dict)
                server_model = instance.from_dict(loaded_dict)
                return server_model
        else:
            print(f"Error: {save_file} does not exist")
            return None

    is_class_dict = True
    outliner_expanded = False


    def compute_hash(self, exclude=None, memo=None, depth=0, do_print=False):
        """
        Create a hash of the instance's content with custom attribute exclusions.
        Recursively handles DictConversion objects, collections, and primitive types.

        Args:
            exclude: List of attribute names to exclude from hashing.
            memo: Dictionary of already-processed objects to avoid infinite recursion.
            depth: Current recursion depth for debugging.
            do_print: Whether to print debug information.

        Returns:
            A 16-bit float value representing the instance's content.
        """
        import hashlib

        if exclude is None:
            exclude = set()

        if memo is None:
            memo = {}

        # Check if self is already in memo to avoid infinite recursion
        if id(self) in memo:
            if memo[id(self)] != "processing":
                return memo[id(self)]

        # Add self to memo temporarily with a temporary value
        # This is crucial to break recursion cycles
        memo[id(self)] = "processing"  # Temporary value

        # Create a string builder for this object
        content_str = f"{self.__class__.__name__}:"

        # Create set of attributes to exclude
        excluded_attrs = {'outliner_expanded', 'expanded', 'hash', '_parent', '_children', "tensor", "tensor_b", "tensor_c", 'buffer', 'ctx',
                          'texture', "texture3D", "cuda_buffer", "xy_renderer", "xyz_renderer"}
        if exclude:
            for excl in exclude:
                excluded_attrs.add(excl)


        # Add all non-excluded attributes to the string representation
        for key, value in self.__dict__.items():
            # Skip private attributes (starting with underscore)
            if key.startswith('_') or key in excluded_attrs or value is None:
                continue

            # Get string representation of the value
            value_str = self._hash_value_to_str(value, exclude, memo, depth, do_print)
            content_str += f"{value_str}"

        # Calculate hash
        hash_result = hashlib.sha256(content_str.encode('utf-8')).hexdigest()

        # Convert the hash to a 16-bit float (Float16)
        # Take the first 4 hex chars (16 bits) and convert to integer, then normalize to float16 range
        hash_int = int(hash_result[:4], 16)

        # Float16 has 1 sign bit, 5 exponent bits, and 10 mantissa bits
        # We'll use the range -65504 to +65504 (max range for float16)
        float_value = (hash_int / 0xFFFF) * 65504 * 2 - 65504

        # Update the memo with the final value
        memo[id(self)] = float_value

        if do_print:
            print(
                f"Depth: {depth}, Class: {self.__class__.__name__}, Hash: {hash_result[:8]}..., Float16: {float_value}")

        self.hash = float_value
        return float_value

    def _hash_value_to_str(self, value, exclude=None, memo=None, depth=0, do_print=False):
        """
        Helper method to convert a value to a string representation based on its type.

        Args:
            value: The value to convert to string
            exclude: List of attribute names to exclude from hashing
            memo: Dictionary of already-processed objects
            depth: Current recursion depth for debugging
            do_print: Whether to print debug information

        Returns:
            A string representation of the value
        """
        depth += 1

        if memo is None:
            memo = {}

        if do_print:
            if hasattr(torch, 'cuda') and torch.cuda.is_available():
                mem_str = ""
                for i in range(torch.cuda.device_count()):
                    mem_alloc = torch.cuda.memory_allocated(i) / 1024 ** 3
                    mem_str += f"GPU {i}: {mem_alloc:.2f} GB\n"
                print(f"Depth: {depth}, Value: {value}, Type: {type(value)}, Memory: {mem_str}")
            else:
                print(f"Depth: {depth}, Value: {value}, Type: {type(value)}")

        # Check if value is already in memo - crucial for avoiding infinite recursion
        if id(value) in memo:
            return f"ref:{value}"  # Return a reference indicator instead of recursing

        # Handle None
        if value is None:
            return ""

        # Handle tensors and other special objects by returning their type and shape/identity
        if hasattr(value, "__class__") and value.__class__.__name__ == "Tensor":
            try:
                tensor_repr = f"Tensor:shape={list(value.shape)}:dtype={value.dtype}"
            except:
                tensor_repr = f"Tensor:{id(value)}"
            memo[id(value)] = tensor_repr
            return tensor_repr

        if hasattr(value, "__class__") and "PreTrainedTokenizerBase" in str(value.__class__.__mro__):
            tokenizer_repr = f"Tokenizer:{value.__class__.__name__}"
            memo[id(value)] = tokenizer_repr
            return tokenizer_repr

        if hasattr(value, "__class__") and "Module" in str(value.__class__.__mro__):
            module_repr = f"Module:{value.__class__.__name__}"
            memo[id(value)] = module_repr
            return module_repr

        # Handle DictConversion objects - pass the depth parameter correctly
        if hasattr(value, "compute_hash"):
            memo[id(value)] = "processing"  # Add immediately to avoid recursion
            result = value.compute_hash(exclude, memo, depth, do_print)
            memo[id(value)] = result  # Update with actual result
            return str(result)

        # Handle Enums
        if hasattr(value, "__class__") and hasattr(value.__class__,
                                                   "__module__") and "enum" in value.__class__.__module__:
            try:
                enum_repr = f"Enum:{value.__class__.__name__}.{value.name}"
            except:
                enum_repr = f"Enum:{value.__class__.__name__}"
            memo[id(value)] = enum_repr
            return enum_repr

        # Handle lists
        if isinstance(value, list):
            memo[id(value)] = "list:processing"  # Add immediately to avoid recursion
            items_str = "["
            for item in value:
                items_str += self._hash_value_to_str(item, exclude, memo, depth, do_print) + ","
            items_str += "]"
            memo[id(value)] = items_str
            return items_str

        # Handle tuples
        if isinstance(value, tuple):
            memo[id(value)] = "tuple:processing"  # Add immediately to avoid recursion
            items_str = "("
            for item in value:
                items_str += self._hash_value_to_str(item, exclude, memo, depth, do_print) + ","
            items_str += ")"
            memo[id(value)] = items_str
            return items_str

        # Handle dictionaries
        if isinstance(value, dict):
            memo[id(value)] = "dict:processing"  # Add immediately to avoid recursion
            items_str = "{"
            for k, v in value.items():
                if k.startswith('_') or k in exclude:
                    continue
                # Convert the key to string representation
                # Get value string representation
                val_str = self._hash_value_to_str(v, exclude, memo, depth, do_print)
                items_str += f"{val_str}"
            items_str += "}"
            memo[id(value)] = items_str
            return items_str

        if isinstance(value, set):
            memo[id(value)] = "set:processing"  # Add immediately to avoid recursion
            items_str = "{"
            for item in value:
                items_str += str(item) + ","
            items_str += "}"
            memo[id(value)] = items_str
            return items_str

        # Handle primitive types (int, float, str, bool)
        if isinstance(value, (int, float, str, bool)):
            result = str(value)
            memo[id(value)] = result
            return result

        # Any other types - use their string representation
        other_repr = f"{str(type(value).__name__)}"  # Just use type to prevent recursion
        memo[id(value)] = other_repr
        return other_repr


    def deepcopy_exclude(self, exclude=None, memo=None, depth=0, do_print=False):
        """
        Create a deep copy of the instance with custom attribute exclusions.
        Recursively handles DictConversion objects, collections, and primitive types.

        Args:
            exclude: List of attribute names to exclude from copying.
            memo: Dictionary of already-copied objects to avoid infinite recursion.

        Returns:
            A new instance with deep-copied attributes except for excluded ones.
        """
        if memo is None:
            memo = {}

        # Check if self is already in memo to avoid infinite recursion
        if id(self) in memo:
            return memo[id(self)]

        # Create a new instance of the same class
        result = self.__class__.__new__(self.__class__)

        # Add the new object to memo to avoid infinite recursion
        memo[id(self)] = result

        # Initialize new parent and children tracking attributes
        result._parent = None
        result._parent_key = None
        result._children = {}

        # Create set of attributes to exclude
        excluded_attrs = {'class_names', '_parent', '_children', "tensor", "tensor_b", "tensor_c", 'buffer', 'ctx',
                          'texture', "texture3D", "cuda_buffer", "xy_renderer", "xyz_renderer"}
        if exclude:
            excluded_attrs.update(exclude)

        # Copy all attributes except excluded ones
        for key, value in self.__dict__.items():
            if key not in excluded_attrs:
                # Deep copy the value with appropriate handling based on type
                copied_value = self._deepcopy_value(value, exclude, memo, do_print)
                setattr(result, key, copied_value)
            else:
                # For excluded attributes, just set them to None
                setattr(result, key, None)


        return result

    def _deepcopy_value(self, value, exclude=None, memo=None, depth=0, do_print=False):
        """
        Helper method to deep copy a value based on its type.

        Args:
            value: The value to deep copy
            exclude: List of attribute names to exclude from copying
            memo: Dictionary of already-copied objects

        Returns:
            A deep copy of the value
        """
        depth += 1

        if memo is None:
            memo = {}

        if do_print:
            mem_str = ""
            for i in range(torch.cuda.device_count()):
                mem_alloc = torch.cuda.memory_allocated(i) / 1024 ** 3
                mem_str += f"GPU {i}: {mem_alloc:.2f} GB\n"

            print(f"Depth: {depth}, Value: {value}, Type: {type(value)}, Memory: {mem_str}")

        # Check if value is already in memo
        if id(value) in memo:
            return memo[id(value)]

        # Handle None
        if value is None:
            return None

        if isinstance(value, Tensor):
            return value

        if isinstance(value, Tensor):
            return value

        if isinstance(value, LlamaTokenizerFast):
            return value

        if isinstance(value, PreTrainedTokenizerBase):
            return value

        if isinstance(value, nn.Module):
            return value

        # Handle DictConversion objects
        if isinstance(value, DictConversion) or hasattr(value, "to_dict"):
            return value.deepcopy_exclude(exclude, memo, depth)

        # Handle Enums (should be copied by value, not deep copied)
        if isinstance(value, Enum):
            return value

        # Handle lists
        if isinstance(value, list):
            new_list = []
            memo[id(value)] = new_list
            for item in value:
                new_list.append(self._deepcopy_value(item, exclude, memo, depth, do_print))
            return new_list

        # Handle tuples
        if isinstance(value, tuple):
            items = [self._deepcopy_value(item, exclude, memo, depth, do_print) for item in value]
            result = tuple(items)
            memo[id(value)] = result
            return result

        # Handle dictionaries
        if isinstance(value, dict):
            new_dict = {}
            memo[id(value)] = new_dict
            for k, v in value.items():
                # The keys are immutable, so we don't need to copy them
                new_dict[k] = self._deepcopy_value(v, exclude, memo, depth, do_print)
            return new_dict

        if isinstance(value, set):
            return deepcopy(value, memo)

        # Handle primitive types (int, float, str, bool)
        if isinstance(value, (int, float, str, bool)):
            return value

        # print(f"Should't get here: {value.__class__} {isinstance(value, DictConversion)}")
        return deepcopy(value, memo)

    def __init__(self):
        # Using weakref to avoid circular references
        self.id = str(uuid.uuid4())
        self.hash = None
        self._parent: Optional[weakref.ReferenceType] = None
        self._parent_key: Optional[Union[str, int]] = None
        self._children: Dict[Union[str, int], 'DictConversion'] = {}
        self.outliner_expanded = False
        self._history_manager = GlobalUndoRedoManager.get_instance()
        self._exclude_attrs = {'_history_manager', '_exclude_attrs', '_parameters',
                               '_buffers', '_modules', 'training'}


    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        # Initialize instance attributes
        instance._history_manager = GlobalUndoRedoManager.get_instance()
        instance.hash = None

        instance._parent = None
        instance._parent_key = None
        instance._children = {}
        return instance

    def get_path(self) -> str:
        """
        Returns the path to this object by walking up the parent chain.
        """
        path_components = []
        current = self

        while current._parent is not None:
            parent = current._parent()
            if parent is None:  # weak reference expired
                raise ValueError("Parent reference expired")

            path_components.append(current._parent_key)
            current = parent

        return ''.join(reversed([comp if comp.startswith('[') else f'.{comp}'
                                 for comp in path_components])).lstrip('.')

    def _wrap_container(self, value, attr_name):
        """Wrap container types with tracked versions."""
        if isinstance(value, list):
            return TrackedList(self, attr_name, value)
        elif isinstance(value, dict):
            return TrackedDict(self, attr_name, value)
        elif isinstance(value, set):
            return TrackedSet(self, attr_name, value)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        # Handle special internal attributes normally
        if name.startswith('_'):
            super().__setattr__(name, value)
            return

        # Skip history tracking if disabled or for special attributes
        if self._history_manager.disabled:
            super().__setattr__(name, value)
            return

        # Check if weak reference is still valid
        # Uncomment if needed
        # if weak self.is_still_valid(value):
        #     super().__setattr__(name, value)
        #     return

        try:
            # Get old value if it exists for history tracking
            old_value = None
            if hasattr(self, name):
                old_value = getattr(self, name)

                # Skip if value isn't changing
                if old_value is value:
                    return

                # Deep copy for non-primitive types
                if isinstance(old_value, (dict, list, set)) or isinstance(old_value, DictConversion):
                    old_value = deepcopy(old_value)

            # Wrap container types for tracking
            wrapped_value = self._wrap_container(value, name)

            # Set up parent reference if value is DictConversion
            if isinstance(wrapped_value, DictConversion):
                wrapped_value._parent = weakref.ref(self)
                wrapped_value._parent_key = name
                self._children[name] = wrapped_value

                # Ensure nested DictConversion objects use the same history manager
                wrapped_value._history_manager = self._history_manager

            elif isinstance(wrapped_value, (list, tuple)):
                # Handle lists/tuples of DictConversion objects
                for i, item in enumerate(wrapped_value):
                    if isinstance(item, DictConversion):
                        item._parent = weakref.ref(self)
                        item._parent_key = f"{name}[{i}]"
                        self._children[f"{name}[{i}]"] = item

                        # Ensure they use the same history manager
                        item._history_manager = self._history_manager

            elif isinstance(wrapped_value, dict):
                # Handle dictionaries containing DictConversion objects
                for k, v in wrapped_value.items():
                    if isinstance(v, DictConversion):
                        v._parent = weakref.ref(self)
                        v._parent_key = f"{name}['{k}']"
                        self._children[f"{name}['{k}']"] = v

                        # Ensure they use the same history manager
                        v._history_manager = self._history_manager

            # Make the actual change
            super().__setattr__(name, wrapped_value)

            # Record the change if it's not a tracked container itself
            # (tracked containers record their own changes)
            if not any(isinstance(wrapped_value, t) for t in (TrackedList, TrackedDict, TrackedSet)):
                if hasattr(self, '_history_manager'):
                    self._history_manager.record_change(
                        self,
                        name,
                        old_value,
                        deepcopy(wrapped_value) if isinstance(wrapped_value, (dict, list, set, DictConversion)) else wrapped_value
                    )

        except Exception as e:
            # If something goes wrong, still apply the change
            super().__setattr__(name, value)
            raise e


    # Global undo/redo methods that delegate to the global manager
    def undo(self):
        """Undo the last change across all tracked objects."""
        return self._history_manager.undo()

    def redo(self):
        """Redo the last undone change across all tracked objects."""
        return self._history_manager.redo()

    def can_undo(self):
        """Check if there are changes to undo."""
        return self._history_manager.can_undo()

    def can_redo(self):
        """Check if there are changes to redo."""
        return self._history_manager.can_redo()


    def __getitem__(self, key: Union[str, int]) -> Any:
        # First check if this key is directly in __dict__
        if isinstance(key, str) and hasattr(self, key):
            return getattr(self, key)

        # Then check if it's in __dict__ as a list/sequence attribute
        for attr_name, attr_value in self.__dict__.items():
            if isinstance(attr_value, (list, tuple)) and isinstance(key, int):
                if key < len(attr_value):
                    return attr_value[key]
                raise IndexError(f"Index {key} out of range for sequence of length {len(attr_value)}")
            elif isinstance(attr_value, dict) and key in attr_value:
                return attr_value[key]

        raise TypeError(f"'{self.__class__.__name__}' object has no sequence or mapping with key/index '{key}'")

    def __setitem__(self, key: Union[str, int], value: Any) -> None:
        if isinstance(value, DictConversion):
            value._parent = weakref.ref(self)
            value._parent_key = f"['{key}']" if isinstance(key, str) else f"[{key}]"
            self._children[key] = value

        # Try to find appropriate sequence/mapping to set the item
        for attr_name, attr_value in self.__dict__.items():
            if isinstance(attr_value, (list, tuple)) and isinstance(key, int):
                if isinstance(attr_value, tuple):
                    # Convert tuple to list if needed
                    setattr(self, attr_name, list(attr_value))
                    attr_value = getattr(self, attr_name)
                if key < len(attr_value):
                    attr_value[key] = value
                    return
            elif isinstance(attr_value, dict) and key in attr_value:
                attr_value[key] = value
                return

        # If we didn't find a place to set it, treat it as a new attribute
        setattr(self, str(key), value)

    def get(self, path: str) -> Any:
        """
        Retrieves a value using a path string.
        Example paths: "attr1.attr2", "attr1[0]", "attr1['key']"
        """
        current = self
        if not path:
            return current

        # Split path into components, preserving nested structure
        parts = []
        current_part = ''
        brackets = 0

        for char in path:
            if char == '[':
                brackets += 1
                if brackets == 1 and current_part:
                    parts.append(current_part)
                    current_part = '['
                else:
                    current_part += char
            elif char == ']':
                brackets -= 1
                current_part += char
                if brackets == 0:
                    parts.append(current_part)
                    current_part = ''
            elif char == '.' and brackets == 0:
                if current_part:
                    parts.append(current_part)
                current_part = ''
            else:
                current_part += char

        if current_part:
            parts.append(current_part)

        for part in parts:
            if part.startswith('['):
                # Handle array/dict access
                idx = part[1:-1].strip("'\"")  # Remove quotes if present
                try:
                    current = current[int(idx) if idx.isdigit() else idx]
                except (TypeError, ValueError, KeyError, IndexError) as e:
                    raise TypeError(f"Could not access '{part}' in path '{path}': {str(e)}")
            else:
                # Handle attribute access
                try:
                    current = getattr(current, part)
                except AttributeError as e:
                    raise AttributeError(f"Could not access '{part}' in path '{path}': {str(e)}")

        return current

    def get_path_of_attribute(self, attr_value: Any) -> str:
        """
        Returns the path to find an attribute value within the nested structure.
        Uses parent references for efficient path construction.
        """
        if not isinstance(attr_value, DictConversion):
            # Search immediate children first
            for name, value in self.__dict__.items():
                if not name.startswith('_'):
                    if value is attr_value:
                        return name

            # Search in nested structures
            path = self._find_path(attr_value)
            if path is not None:
                return path
            raise ValueError("Attribute not found in nested structure")

        # If attr_value is a DictConversion, build path from parent references
        path_components = []
        current = attr_value

        while current is not None and current is not self:
            if current._parent is None:
                raise ValueError("Attribute not found in nested structure")

            parent = current._parent()
            if parent is None:  # weak reference expired
                raise ValueError("Parent reference expired")

            path_components.append(current._parent_key)
            current = parent

        if current is not self:
            raise ValueError("Attribute not found in nested structure")

        return ''.join(reversed([comp if comp.startswith('[') else f'.{comp}'
                                 for comp in path_components])).lstrip('.')

    def _find_path(self, target: Any, current_path: str = '') -> Optional[str]:
        """Helper method to find path for non-DictConversion values"""
        # Search in sequences
        if isinstance(self, (list, tuple)):
            for i, value in enumerate(self):
                new_path = f"{current_path}[{i}]"
                if value is target:
                    return new_path
                if isinstance(value, DictConversion):
                    result = value._find_path(target, new_path)
                    if result is not None:
                        return result

        # Search in dicts
        if isinstance(self, dict):
            for key, value in self.items():
                new_path = f"{current_path}['{key}']"
                if value is target:
                    return new_path
                if isinstance(value, DictConversion):
                    result = value._find_path(target, new_path)
                    if result is not None:
                        return result

        # Search in object attributes
        for key, value in self.__dict__.items():
            if key.startswith('_'):
                continue
            new_path = f"{current_path}.{key}" if current_path else key
            if value is target:
                return new_path
            if isinstance(value, DictConversion):
                result = value._find_path(target, new_path)
                if result is not None:
                    return result

        return None

    @classmethod
    def _is_dataclass(cls, obj: Any) -> bool:
        """Check if an object is a custom dataclass (has attributes)."""
        return hasattr(obj, '__dict__')

    @classmethod
    def _is_enum(cls, obj: Any) -> bool:
        """Check if an object is an Enum."""
        return isinstance(obj, Enum)



    @staticmethod
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

            # Build full path for the nested class
            class_path = f"{parent_path}.{name}"
            nested_classes.append((class_path, obj))

            # Recursively find classes nested within this class
            inner_classes = DictConversion.find_nested_classes(obj, class_path)
            nested_classes.extend(inner_classes)

        return nested_classes

    @staticmethod
    def instantiate_from_class_path(class_path: str, last_try=False):
        parts = class_path.split('.')
        module = None

        class_name = parts[-1]
        try:
            # for i in range(len(parts) - 1):
            #     if parts[i] == "tensorview.app_model":
            #         parts[i] = "model"

            for i in range(len(parts) - 1, 0, -1):
                try:
                    if parts[
                        0] != ClassUtility().root and ClassUtility().root is not None and ClassUtility().root != "":
                        parts.insert(0, ClassUtility().root)
                    module_path = '.'.join(parts[:i])

                    if module_path in sys.modules:
                        module = sys.modules[module_path]
                    else:
                        module = importlib.import_module(module_path)

                    break
                except ImportError:
                    continue

            if module is None:
                return None

            obj = module
            for part in parts[i:]:
                obj = getattr(obj, part)
            return obj()
        except Exception as e:
            target_class_name = parts[-1]
            target_class_parent = parts[-2]
            target_class_combine = f"{target_class_parent}.{target_class_name}"
            if last_try:
                # Print stack trace for debugging
                import traceback
                traceback.print_exc()

                print(f"Error instantiating {class_path}: {str(e)}")
                return None

            ClassUtility().initialize_class_names()

            if target_class_combine in ClassUtility().class_names:
                found_class_path = ClassUtility().class_names[target_class_combine]
                return DictConversion.instantiate_from_class_path(found_class_path, last_try=True)
            elif target_class_name in ClassUtility().class_names:
                found_class_path = ClassUtility().class_names[target_class_name]
                return DictConversion.instantiate_from_class_path(found_class_path, last_try=True)
            else:
                print(f"Class {target_class_name} not found in known classes.")

            return None

    @staticmethod
    def get_enum_value(class_path: str, value_name: str, value: Optional[int], last_try=False):
        # First get the enum class
        parts = class_path.split('.')
        module = None
        class_name = parts[-1]
        parent_name = parts[-2]
        combined_name = f"{parent_name}.{class_name}"

        ClassUtility().initialize_class_names()

        class_parent = f"{parent_name}.{class_name}"
        if class_name == "AnchorPair":
            print("Debugging AnchorPair")
        if class_parent in ClassUtility().class_names:
            class_path = ClassUtility().class_names[class_parent]
            parts = class_path.split('.')

        elif class_name in ClassUtility().class_names:
            class_path = ClassUtility().class_names[class_name]
            parts = class_path.split('.')

        # for i in range(len(parts) - 1):
        #     if parts[i] == "tensorview":
        #         parts[i] = "model.app_model"

        for i in range(len(parts) - 1, 0, -1):
            try:
                if parts[0] != ClassUtility().root and ClassUtility().root is not None and ClassUtility().root != "":
                    parts.insert(0, ClassUtility().root)

                module_path = '.'.join(parts[:i])
                module = sys.modules.get(module_path)
                if module is None:
                    # Only import if it's not already imported
                    module = importlib.import_module(module_path)
                break
            except ImportError:
                print(f"Error importing {module_path}")
                continue

        if module is None:
            return None
        # Get the enum class
        obj = module
        for part in parts[i:]:
            obj = getattr(obj, part)

        # Get the specific enum value
        try:
            if issubclass(obj, Enum):
                if value is not None:
                    return obj(value)
                else:
                    return obj[value_name]  # This gets the enum value
            else:
                raise ValueError(f"{class_path} is not an Enum class")
        except Exception as e:
            if last_try:
                # Print stack trace for debugging
                import traceback
                traceback.print_exc()

                print(f"Error getting enum value for {class_path}: {str(e)}")
                return None

            ClassUtility().initialize_class_names()

            if value_name in ClassUtility().class_names:
                found_class_path = ClassUtility().class_names[value_name]
                return DictConversion.get_enum_value(found_class_path, value_name, value, last_try=True)
            elif combined_name in ClassUtility().class_names:
                found_class_path = ClassUtility().class_names[combined_name]
                return DictConversion.get_enum_value(found_class_path, combined_name, value, last_try=True)

    def from_dict(self, object_dict, excluded=None, class_root=None):
        if class_root is not None:
            ClassUtility().initialize_class_names(class_root)
        if excluded is None:
            excluded = []
        root_id = object_dict["root"]
        object_dict.pop("root")
        excluded.extend(["class_names", "_parent", "_parent_key", "_children", "hash",
                         "outliner_expanded", "modules_imported"])

        instantiated_objects = {}

        def update_instance(unset_value, new_value, excluded):
            # Handle Enums
            # Handle nested objects
            # and unset_value['type'] == "src.gsd.glue.tensorview.GenPrompt"
            if unset_value is None and new_value is None:
                return None
            if isinstance(new_value, dict) and "type" in new_value:
                print("Found GenPrompt type")
            if isinstance(unset_value, DictConversion) or (
                    isinstance(new_value, tuple) and new_value[0] in instantiated_objects):
                if new_value is None:
                    return unset_value
                return instantiated_objects[new_value[0]]

            if isinstance(unset_value, Enum) or (isinstance(unset_value, tuple) and
                                                 (new_value is not None and len(new_value) > 2 and new_value[2] == "Enum")):
                if isinstance(new_value, tuple):
                    # # Convert tuple to enum value
                    if isinstance(new_value[0], int):
                        return unset_value
                    if len(new_value) > 3:
                        enum_obj = DictConversion.get_enum_value(new_value[1], new_value[0], new_value[3])
                    else:
                        enum_obj = DictConversion.get_enum_value(new_value[1], new_value[0], None)
                    # enum_type = type(enum_value)
                    # setattr(instance, key, enum_type[new_value[0]])
                    return enum_obj
                    # return None

                if isinstance(new_value, str):
                    # Convert string to enum value
                    enum_type = type(unset_value)
                    # setattr(instance, key, enum_type[new_value])
                    return enum_type[new_value]
                elif isinstance(new_value, Enum):
                    # setattr(instance, key, new_value)
                    return new_value
                return unset_value

            # Is tuple
            elif isinstance(unset_value, tuple):
                if unset_value[0] in instantiated_objects:
                    return instantiated_objects[unset_value[0]]
                else:
                    return new_value

            # Handle lists
            elif isinstance(unset_value, list) and isinstance(new_value, list):
                if len(unset_value) > 0:
                    type_ref = unset_value[0]
                else:
                    type_ref = None
                unset_value.clear()
                for i, item in enumerate(new_value):
                    if type_ref is not None:
                        new_item = update_instance(item, item, excluded)
                    else:
                        new_item = update_instance(item, item, excluded)
                    unset_value.append(new_item)
                return unset_value

            # Handle dictionaries
            elif isinstance(unset_value, dict) and isinstance(new_value, dict):
                first_key = next(iter(unset_value.keys()), None)
                first_value = unset_value.get(first_key, None)

                if "parse_direct" in new_value:
                    return new_value

                unset_value.clear()
                for a_key, a_value in new_value.items():
                    if first_key is not None:
                        unset_value[a_key] = update_instance(first_value, a_value, excluded)
                    else:
                        unset_value[a_key] = update_instance(a_value, a_value, excluded)

                return unset_value
            # Direct update for non-container types
            else:
                return new_value

        for okey, ovalue in object_dict.items():
            if excluded is None or okey not in excluded:
                class_path = object_dict[okey]["type"]
                instance = DictConversion.instantiate_from_class_path(class_path)
                instantiated_objects[okey] = instance

        root = instantiated_objects[root_id]
        for okey, ovalue in object_dict.items():
            instance = instantiated_objects[okey]

            for key, new_value in ovalue.items():
                if key not in excluded:
                    unset_value = getattr(instance, key, None)

                    try:
                        parsed = update_instance(unset_value, new_value, excluded)
                        setattr(instance, key, parsed)
                    except (KeyError, AttributeError) as e:
                        print(f"KeyError: {key} not found in instance {instance}. Should not name attributes \"type\"")
                        continue

        return root

    @staticmethod
    def get_full_class_path(obj):
        cls = obj.__class__
        module = cls.__module__
        qualname = cls.__qualname__  # This already contains the full nested path

        # Combine module with qualname
        return f"{module}.{qualname}"
        # cls = obj.__class__
        # module = cls.__module__
        #
        # # Get the full class path by walking through any nested classes
        # try:
        #     class_parts = []
        #     while cls:
        #         class_parts.append(cls.__name__)
        #         # Get the enclosing class if it exists
        #         cls = cls.__qualname__.rsplit('.', 1)[0] if '.' in cls.__qualname__ else None
        #         if cls:
        #             # Convert string class name to actual class
        #             cls = getattr(sys.modules[module], cls)
        # except (AttributeError, KeyError):
        #     print(f"Error getting full class path for {obj}")
        #     print(f"Module: {module}, Class: {cls}")
        #     return None
        #
        # # Reverse the parts since we collected them from inner to outer
        # class_path = '.'.join([module] + class_parts[::-1])
        # return class_path


    def to_dict(self, excluded=None, objects=None, shallow=False, use_references=False) -> Dict:
        """
        Generic method to convert any class instance to a dictionary.
        Handles nested objects, enums, and basic types.
        """
        if hasattr(self, 'excluded'):
            if excluded is None:
                excluded = self.excluded
            else:
                excluded = excluded.union(self.excluded)
        result = {}
        is_root = False
        if objects is None:
            objects = {}
            is_root = True

        if not shallow:
            shallow_parse = self.to_dict(excluded=excluded, objects=objects, shallow=True, use_references=False)
            shallow_parse["is_root"] = is_root
            # Class path
            if hasattr(self, 'id'):
                object_id = self.id
            else:
                object_id = id(self)

            classtype = DictConversion.get_full_class_path(self)
            shallow_parse["type"] = classtype
            if is_root:
                objects["root"] = object_id


            objects[object_id] = shallow_parse

        # Get all attributes that don't start with '_'
        for key, value in self.__dict__.items():
            if key.startswith('_') or (excluded and key in excluded):
                continue

            if not shallow:
                parsed = self.parse_value(result, objects, key, value, excluded)
                result[key] = parsed
            else:
                shallow_parse = self.parse_value(result, objects, key, value, excluded, shallow=True)
                result[key] = shallow_parse

        if is_root:
            result["objects"] = objects

        if use_references:
            return objects

        return result

    def is_primitive(self, value: Any) -> bool:
        """Check if a value is a primitive type."""
        primitive_types = (
            int, float, str, bool,
            type(None),  # NoneType
        )
        # Check direct primitive types
        if isinstance(value, primitive_types):
            return True
        # Check built-in collections with primitive contents
        if isinstance(value, (list, dict, set, tuple)):
            return True
        # Handle enums separately since they're technically classes
        if isinstance(value, Enum):
            return True
        return False

    def is_class(self, value: Any) -> bool:
        """Check if a value is a class instance (non-primitive)."""
        return not self.is_primitive(value)

    def parse_value(self, result, objects, key, value, excluded, shallow=False):
        # Handle None
        if value is None:
            return None
        # Handle enums
        if isinstance(value, tuple):
            return value
        elif isinstance(value, Enum):
            classtype = DictConversion.get_full_class_path(value)
            results = (value.name, classtype, "Enum", value.value)
            return results
        # Handle lists
        elif isinstance(value, Dict):
            # Loop through the dictionary and convert each item
            inner_dict = {}
            for sub_key, sub_value in value.items():
                if excluded and sub_key in excluded:
                    continue

                inner_dict[sub_key] = self.parse_value(inner_dict, objects, sub_key, sub_value, excluded, shallow)
            return inner_dict
        elif isinstance(value, list):
            inner_list = []
            for item in value:
                inner_list.append(
                    self.parse_value(result, objects, key, item, excluded, shallow)
                )
            return inner_list
        # Handle nested objects with to_dict method
        elif hasattr(value, 'to_dict'):
            if shallow:
                classtype = DictConversion.get_full_class_path(value)
                if hasattr(value, 'id'):
                    results = (value.id, classtype)
                else:
                    results = (id(value), classtype)
            else:
                results = value.to_dict(excluded=excluded, objects=objects, shallow=False, use_references=False)
            return results
        # Handle basic types
        # elif not isinstance(value, DictConversion):
        #     return
        else:
            if self.is_primitive(value):
                return value

    def update_from_dict_references(self, update_dict: dict, visited: set = None, excluded=None) -> None:
        """
        Update object attributes recursively from a dictionary, properly handling nested objects and enums.
        """
        # Create a new visited set for the root call
        is_root = visited is None
        if is_root:
            visited = set()

        obj_id = id(self)
        if obj_id in visited or not update_dict:
            return
        visited.add(obj_id)

        try:
            for key, new_value in update_dict.items():
                if excluded and key in excluded:
                    continue

                try:
                    current_value = getattr(self, key, None)

                    # Handle Enums
                    if isinstance(current_value, Enum):
                        if isinstance(new_value, str):
                            # Convert string to enum value
                            enum_type = type(current_value)
                            setattr(self, key, enum_type[new_value])
                        elif isinstance(new_value, Enum):
                            setattr(self, key, new_value)
                        continue

                    # Handle nested objects
                    if hasattr(current_value, 'update_from_dict') and isinstance(new_value, dict):
                        current_value.update_from_dict(new_value, visited, excluded)

                    # Handle lists
                    elif isinstance(current_value, list) and isinstance(new_value, list):
                        self._update_list(current_value, [], visited, excluded)

                    # Handle dictionaries
                    elif isinstance(current_value, dict) and isinstance(new_value, dict):
                        self._update_dict(current_value, copy(new_value), visited, excluded)
                        to_delete = []
                        for a_key in current_value.keys():
                            if a_key not in new_value:
                                to_delete.append(a_key)
                        for a_key in to_delete:
                            del current_value[a_key]

                    # Direct update for non-container types
                    else:
                        setattr(self, key, new_value)

                except Exception as e:
                    print(f"Error updating {key}: {str(e)}")
                    # Print stack trace for debugging
                    import traceback
                    traceback.print_exc()
        finally:
            # Clean up visited set when we're done with the root update
            if is_root:
                visited.clear()

    def update_from_dict(self, update_dict: dict, visited: set = None, excluded=None) -> None:
        """
        Update object attributes recursively from a dictionary, properly handling nested objects and enums.
        """
        # Create a new visited set on the root call
        is_root = visited is None
        if is_root:
            visited = set()

        obj_id = id(self)
        if obj_id in visited or not update_dict:
            return
        visited.add(obj_id)

        try:
            for key, new_value in update_dict.items():
                if excluded and key in excluded:
                    continue

                try:
                    current_value = getattr(self, key, None)

                    # Handle Enums
                    if isinstance(current_value, Enum):
                        if isinstance(new_value, str):
                            # Convert string to enum value
                            enum_type = type(current_value)
                            setattr(self, key, enum_type[new_value])
                        elif isinstance(new_value, Enum):
                            setattr(self, key, new_value)
                        continue

                    # Handle nested objects
                    if hasattr(current_value, 'update_from_dict') and isinstance(new_value, dict):
                        current_value.update_from_dict(new_value, visited, excluded)

                    # Handle lists
                    elif isinstance(current_value, list) and isinstance(new_value, list):
                        self._update_list(current_value, copy(new_value), visited, excluded)

                    # Handle dictionaries
                    elif isinstance(current_value, dict) and isinstance(new_value, dict):
                        self._update_dict(current_value, copy(new_value), visited, excluded)
                        to_delete = []
                        for a_key in current_value.keys():
                            if a_key not in new_value:
                                to_delete.append(a_key)
                        for a_key in to_delete:
                            del current_value[a_key]

                    # Direct update for non-container values
                    else:
                        setattr(self, key, new_value)

                except Exception as e:
                    print(f"Error updating {key}: {str(e)}")
                    # Print stack trace for debugging
                    import traceback
                    traceback.print_exc()
        finally:
            # Clean up visited set when we're done with the root update
            if is_root:
                visited.clear()

    def _update_list(self, current_list: list, new_list: list, visited: set, excluded=None) -> None:
        """Helper method to update list items recursively."""
        # Determine the length difference
        current_length = len(current_list)
        new_length = len(new_list)

        # Update existing items
        for i in range(min(current_length, new_length)):
            current_item = current_list[i]
            new_item = new_list[i]

            # Handle Enums
            if isinstance(current_item, Enum):
                if isinstance(new_item, str):
                    enum_type = type(current_item)
                    current_list[i] = enum_type[new_item]
                elif isinstance(new_item, Enum):
                    current_list[i] = new_item
                continue

            # Handle updatable objects
            if hasattr(current_item, 'update_from_dict') and isinstance(new_item, dict):
                current_item.update_from_dict(new_item, visited, excluded)
            # Handle nested lists
            elif isinstance(current_item, list) and isinstance(new_item, list):
                self._update_list(current_item, new_item, visited, excluded)
            # Handle nested dicts
            elif isinstance(current_item, dict) and isinstance(new_item, dict):
                self._update_dict(current_item, new_item, visited, excluded)
            # Direct update
            else:
                current_list[i] = new_item

        # Handle any additional items in new_list
        if new_length > current_length:
            # Determine the type of items in the list if any exist
            item_type = None
            if current_list:
                for item in current_list:
                    if hasattr(item, 'update_from_dict'):
                        item_type = type(item)
                        break

            # Add new items
            for i in range(current_length, new_length):
                new_item = new_list[i]
                if isinstance(new_item, dict) and item_type is not None:
                    try:
                        # Create new instance without initialization
                        new_instance = item_type.__new__(item_type)
                        # Initialize with empty/default values
                        if hasattr(new_instance, '__init__'):
                            new_instance.__init__()
                        # Then update with the dictionary values
                        new_instance.update_from_dict(new_item, visited, excluded)
                        current_list.append(new_instance)
                    except Exception as e:
                        raise Exception(f"Failed to create new instance of {item_type.__name__}: {str(e)}")
                else:
                    current_list.append(new_item)

        # Remove any extra items if new list is shorter
        while len(current_list) > new_length:
            current_list.pop()

    def _update_dict(self, current_dict: dict, new_dict: dict, visited: set, excluded=None) -> None:
        """Helper method to update dictionary values recursively."""
        not_in_new_dict = set()
        for key, new_value in new_dict.items():
            if (excluded and key in excluded):
                continue

            current_value = current_dict.get(key)
            if current_value is None:
                # Get any existing value to use as a template
                any_key = next(iter(current_dict.keys()), None)
                current_value = current_dict.get(any_key, None)


            # Handle Enums in dictionaries
            if isinstance(current_value, Enum):
                if isinstance(new_value, str):
                    enum_type = type(current_value)
                    current_dict[key] = enum_type[new_value]
                elif isinstance(new_value, Enum):
                    current_dict[key] = new_value
                continue

            # Handle updatable objects
            if hasattr(current_value, 'update_from_dict') and isinstance(new_value, dict):
                item_type = None
                try:
                    item_type = type(current_value)

                    if item_type is not None:
                        # Create new instance without initialization
                        new_instance = item_type.__new__(item_type)
                        # Initialize with empty/default values
                        if hasattr(new_instance, '__init__'):
                            new_instance.__init__()
                        # Then update with the dictionary values
                        new_instance.update_from_dict(new_value, visited, excluded)
                        current_dict[key] = new_instance
                except Exception as e:
                    raise Exception(f"Failed to create new instance of {item_type.__name__}: {str(e)}")

            # Handle nested lists
            elif isinstance(current_value, list) and isinstance(new_value, list):
                self._update_list(current_value, new_value, visited, excluded)

            # Handle nested dicts
            elif isinstance(current_value, dict) and isinstance(new_value, dict):
                self._update_dict(current_value, new_value, visited, excluded)

            # Direct update
            else:
                current_dict[key] = new_value

