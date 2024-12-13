from copy import copy
from enum import Enum
from typing import Any, Dict, Set


class DictConversion:
    @classmethod
    def _is_dataclass(cls, obj: Any) -> bool:
        """Check if an object is a custom dataclass (has attributes)."""
        return hasattr(obj, '__dict__')

    @classmethod
    def _is_enum(cls, obj: Any) -> bool:
        """Check if an object is an Enum."""
        return isinstance(obj, Enum)

    def to_dict(self, excluded=None) -> Dict:
        """
        Generic method to convert any class instance to a dictionary.
        Handles nested objects, enums, and basic types.
        """
        result = {}
        # Get all attributes that don't start with '_'
        for key, value in self.__dict__.items():
            if key.startswith('_') or (excluded and key in excluded):
                continue

            parsed = self.parse_value(result, key, value, excluded)
            result[key] = parsed

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

    def parse_value(self, result, key, value, excluded):
        # Handle None
        if value is None:
            return None
        # Handle enums
        elif isinstance(value, Enum):
            return value.name
        # Handle lists
        elif isinstance(value, Dict):
            # Loop through the dictionary and convert each item
            inner_dict = {}
            for sub_key, sub_value in value.items():
                inner_dict[sub_key] = self.parse_value(inner_dict, sub_key, sub_value, excluded)
            return inner_dict
        elif isinstance(value, list):
            inner_list = []
            for item in value:
                inner_list.append(
                    self.parse_value(result, key, item, excluded)
                )
            return inner_list
        # Handle nested objects with to_dict method
        elif hasattr(value, 'to_dict'):
            return value.to_dict(excluded=excluded)
        # Handle basic types
        # elif not isinstance(value, DictConversion):
        #     return
        else:
            if self.is_primitive(value):
                return value

    def update_from_dict(self, target_view, update_dict: dict, visited: set = None, excluded=None) -> None:
        """
        Update object attributes recursively from a dictionary, properly handling nested objects and enums.
        """
        # Create a new visited set on the root call
        is_root = visited is None
        if is_root:
            visited = set()

        obj_id = id(target_view)
        if obj_id in visited or not update_dict:
            return
        visited.add(obj_id)

        try:
            for key, new_value in update_dict.items():
                if key.startswith('_') or (excluded and key in excluded):
                    continue

                try:
                    current_value = getattr(target_view, key, None)

                    # Handle Enums
                    if isinstance(current_value, Enum):
                        if isinstance(new_value, str):
                            # Convert string to enum value
                            enum_type = type(current_value)
                            setattr(target_view, key, enum_type[new_value])
                        elif isinstance(new_value, Enum):
                            setattr(target_view, key, new_value)
                        continue

                    # Handle nested objects
                    if hasattr(current_value, 'update_from_dict') and isinstance(new_value, dict):
                        current_value.update_from_dict(current_value, new_value, visited, excluded)

                    # Handle lists
                    elif isinstance(current_value, list) and isinstance(new_value, list):
                        target_view._update_list(target_view, copy(current_value), copy(new_value), visited, excluded)

                    # Handle dictionaries
                    elif isinstance(current_value, dict) and isinstance(new_value, dict):
                        target_view._update_dict(target_view, copy(current_value), copy(new_value), visited, excluded)

                    # Direct update for non-container values
                    else:
                        setattr(target_view, key, new_value)

                except Exception as e:
                    print(f"Error updating {key}: {str(e)}")
        finally:
            # Clean up visited set when we're done with the root update
            if is_root:
                visited.clear()

    def _update_list(self, target_view, current_list: list, new_list: list, visited: set, excluded=None) -> None:
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
                current_item.update_from_dict(current_item, new_item, visited, excluded)
            # Handle nested lists
            elif isinstance(current_item, list) and isinstance(new_item, list):
                target_view._update_list(target_view, current_item, new_item, visited, excluded)
            # Handle nested dicts
            elif isinstance(current_item, dict) and isinstance(new_item, dict):
                target_view._update_dict(target_view, current_item, new_item, visited, excluded)
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
                        new_instance.update_from_dict(new_instance, new_item, visited, excluded)
                        current_list.append(new_instance)
                    except Exception as e:
                        raise Exception(f"Failed to create new instance of {item_type.__name__}: {str(e)}")
                else:
                    current_list.append(new_item)

        # Remove any extra items if new list is shorter
        while len(current_list) > new_length:
            current_list.pop()

    def _update_dict(self, target_view, current_dict: dict, new_dict: dict, visited: set, excluded=None) -> None:
        """Helper method to update dictionary values recursively."""
        for key, new_value in new_dict.items():
            if key.startswith('_') or (excluded and key in excluded):
                continue

            current_value = current_dict.get(key)

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
                current_value.update_from_dict(current_value, new_value, visited, excluded)

            # Handle nested lists
            elif isinstance(current_value, list) and isinstance(new_value, list):
                target_view._update_list(target_view, current_value, new_value, visited, excluded)

            # Handle nested dicts
            elif isinstance(current_value, dict) and isinstance(new_value, dict):
                target_view._update_dict(target_view, current_value, new_value, visited, excluded)

            # Direct update
            else:
                current_dict[key] = new_value