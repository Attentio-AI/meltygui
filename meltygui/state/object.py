from enum import Enum
from typing import Any, Dict


class DictConversion:
    @classmethod
    def _is_dataclass(cls, obj: Any) -> bool:
        """Check if an object is a custom dataclass (has attributes)."""
        return hasattr(obj, '__dict__')

    @classmethod
    def _is_enum(cls, obj: Any) -> bool:
        """Check if an object is an Enum."""
        return isinstance(obj, Enum)

    def to_dict(self) -> Dict:
        """
        Generic method to convert any class instance to a dictionary.
        Handles nested objects, enums, and basic types.
        """
        result = {}

        # Get all attributes that don't start with '_'
        for key, value in self.__dict__.items():
            if key.startswith('_'):
                continue

            # Handle None
            if value is None:
                result[key] = None
            # Handle enums
            elif isinstance(value, Enum):
                result[key] = value.name
            # Handle lists
            elif isinstance(value, list):
                result[key] = [
                    item.to_dict() if hasattr(item, 'to_dict') else item
                    for item in value
                ]
            # Handle nested objects with to_dict method
            elif hasattr(value, 'to_dict'):
                result[key] = value.to_dict()
            # Handle basic types
            else:
                result[key] = value

        return result

    @classmethod
    def from_dict(cls, data: Dict) -> Any:
        """
        Generic method to create a class instance from a dictionary.
        Handles nested objects, enums, and basic types.
        """
        if data is None:
            return None

        instance = cls()

        # Get all attributes that don't start with '_'
        valid_attrs = {
            key: value for key, value in cls.__dict__.items()
            if not key.startswith('_')
        }

        for key, value in data.items():
            if key not in instance.__dict__:
                continue

            # Get the type of the attribute from class definition
            attr_type = type(instance.__dict__[key])

            # Handle None
            if value is None:
                setattr(instance, key, None)
            # Handle enums
            elif isinstance(instance.__dict__[key], Enum):
                enum_class = type(instance.__dict__[key])
                setattr(instance, key, enum_class[value])
            # Handle lists
            elif isinstance(value, list):
                # Try to determine the type of list elements from an existing element
                current_list = instance.__dict__[key]
                if current_list and hasattr(current_list[0], 'from_dict'):
                    element_type = type(current_list[0])
                    setattr(instance, key, [
                        element_type.from_dict(item) if isinstance(item, dict) else item
                        for item in value
                    ])
                else:
                    setattr(instance, key, value)
            # Handle nested objects
            elif isinstance(value, dict) and hasattr(instance.__dict__[key], 'from_dict'):
                nested_obj = type(instance.__dict__[key]).from_dict(value)
                setattr(instance, key, nested_obj)
            # Handle basic types
            else:
                setattr(instance, key, value)

        return instance
