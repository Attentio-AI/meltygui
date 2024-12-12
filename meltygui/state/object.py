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
            if parsed is not None:
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

    def update_from_dict(self, update_dict: dict, visited: set = None) -> None:
        """
        Update object attributes recursively from a dictionary, properly handling nested objects and enums.
        """
        if visited is None:
            visited = set()

        obj_id = id(self)
        if obj_id in visited or not update_dict:
            return
        visited.add(obj_id)

        for key, new_value in update_dict.items():
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
                    current_value.update_from_dict(new_value, visited)

                # Handle lists
                elif isinstance(current_value, list) and isinstance(new_value, list):
                    self._update_list(current_value, new_value, visited)

                # Handle dictionaries
                elif isinstance(current_value, dict) and isinstance(new_value, dict):
                    self._update_dict(current_value, new_value, visited)

                # Direct update for non-container types
                else:
                    setattr(self, key, new_value)

            except Exception as e:
                print(f"Error updating {key}: {str(e)}")

    def _update_list(self, current_list: list, new_list: list, visited: set) -> None:
        """Helper method to update list items recursively."""
        for i, new_item in enumerate(new_list):
            if i < len(current_list):
                current_item = current_list[i]

                # Handle Enums in lists
                if isinstance(current_item, Enum):
                    if isinstance(new_item, str):
                        enum_type = type(current_item)
                        current_list[i] = enum_type[new_item]
                    elif isinstance(new_item, Enum):
                        current_list[i] = new_item
                    continue

                # Handle updatable objects
                if hasattr(current_item, 'update_from_dict') and isinstance(new_item, dict):
                    current_item.update_from_dict(new_item, visited)

                # Handle nested lists
                elif isinstance(current_item, list) and isinstance(new_item, list):
                    self._update_list(current_item, new_item, visited)

                # Handle nested dicts
                elif isinstance(current_item, dict) and isinstance(new_item, dict):
                    self._update_dict(current_item, new_item, visited)

                # Direct update
                else:
                    current_list[i] = new_item
            else:
                # Handle new items
                if isinstance(new_item, dict):
                    if current_list and hasattr(current_list[0], 'update_from_dict'):
                        item_type = type(current_list[0])
                        new_instance = item_type.__new__(item_type)
                        new_instance.__init__(**new_item)
                        current_list.append(new_instance)
                    else:
                        current_list.append(new_item)
                else:
                    current_list.append(new_item)

    def _update_dict(self, current_dict: dict, new_dict: dict, visited: set) -> None:
        """Helper method to update dictionary values recursively."""
        for key, new_value in new_dict.items():
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
                current_value.update_from_dict(new_value, visited)

            # Handle nested lists
            elif isinstance(current_value, list) and isinstance(new_value, list):
                self._update_list(current_value, new_value, visited)

            # Handle nested dicts
            elif isinstance(current_value, dict) and isinstance(new_value, dict):
                self._update_dict(current_value, new_value, visited)

            # Direct update
            else:
                current_dict[key] = new_value