import sys
import weakref
from copy import copy
from enum import Enum
from importlib import import_module
from typing import Any, Dict, Set, Optional, Union, Sequence


class DictConversion:

    is_class_dict = True
    outliner_expanded = False

    def __init__(self):
        # Using weakref to avoid circular references
        self._parent: Optional[weakref.ReferenceType] = None
        self._parent_key: Optional[Union[str, int]] = None
        self._children: Dict[Union[str, int], 'DictConversion'] = {}
        self.outliner_expanded = False

    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        # Initialize instance attributes
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

    def __setattr__(self, name: str, value: Any) -> None:
        # Handle special internal attributes normally
        if name.startswith('_'):
            super().__setattr__(name, value)
            return

        # Set up parent reference if value is DictConversion
        if isinstance(value, DictConversion):
            value._parent = weakref.ref(self)
            value._parent_key = name
            self._children[name] = value
        elif isinstance(value, (list, tuple)):
            # Handle lists/tuples containing DictConversion objects
            for i, item in enumerate(value):
                if isinstance(item, DictConversion):
                    item._parent = weakref.ref(self)
                    item._parent_key = f"[{i}]"
                    self._children[i] = item
        elif isinstance(value, dict):
            # Handle dictionaries containing DictConversion objects
            for k, v in value.items():
                if isinstance(v, DictConversion):
                    v._parent = weakref.ref(self)
                    v._parent_key = f"['{k}']"
                    self._children[k] = v

        super().__setattr__(name, value)

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
    def instantiate_from_class_path(class_path: str):
        parts = class_path.split('.')
        module = None
        for i in range(len(parts) - 1, 0, -1):
            try:
                if parts[0] != 'src':
                    parts.insert(0, 'src')
                if parts[1] != 'lsd':
                    parts.insert(1, 'lsd')
                module_path = '.'.join(parts[:i])
                module = import_module(f"{module_path}")
                break
            except ImportError:
                continue

        if module is None:
            return None

        obj = module
        for part in parts[i:]:
            obj = getattr(obj, part)
        return obj()

    @staticmethod
    def get_enum_value(class_path: str, value_name: str, value: Optional[int]):
        # First get the enum class
        parts = class_path.split('.')
        module = None
        for i in range(len(parts) - 1, 0, -1):
            try:
                if parts[0] != 'src':
                    parts.insert(0, 'src')
                if parts[1] != 'lsd':
                    parts.insert(1, 'lsd')
                module_path = '.'.join(parts[:i])
                module = sys.modules.get(module_path)
                if module is None:
                    # Only import if it's not already imported
                    module = import_module(module_path)
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
        if issubclass(obj, Enum):
            if value is not None:
                return obj(value)
            else:
                return obj[value_name]  # This gets the enum member
        else:
            raise ValueError(f"{class_path} is not an Enum class")

    def from_dict(self, object_dict, excluded=None):
        if excluded is None:
            excluded = []
        root_id = object_dict["root"]
        root_object = object_dict[root_id]
        object_dict.pop("root")

        instantiated_objects = {}

        def update_instance(unset_value, new_value, excluded):
            # Handle Enums
            if isinstance(unset_value, Enum) or (isinstance(unset_value, tuple) and
                                                 len(new_value) > 2 and new_value[2] == "Enum"):
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

            # Handle nested objects
            if isinstance(unset_value, DictConversion) or (isinstance(new_value, tuple) and new_value[0] in instantiated_objects):
                return instantiated_objects[new_value[0]]

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

                    parsed = update_instance(unset_value, new_value, excluded)
                    setattr(instance, key, parsed)

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
            classtype = DictConversion.get_full_class_path(self)
            shallow_parse["type"] = classtype
            if is_root:
                objects["root"] = id(self)
            objects[id(self)] = shallow_parse

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

