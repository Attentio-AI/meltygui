from enum import Enum
from typing import Any

from src.lsd.gl_gui.melty import Melty

class Meta:
    default = None
    field_type = None
    collection_type = None

    @staticmethod
    def get_child_meta(cls, field_name, value=None):
        child_meta = getattr(cls, f"{field_name}_meta", None)
        value_type = type(value)
        if cls is not None:
            if child_meta is None:
                if hasattr(cls, 'default_meta_for'):
                    child_meta = cls.default_meta_for.get(value_type, None)
        if child_meta is None:
            if hasattr(type(value), 'meta'):
                child_meta = type(value).meta
        if child_meta is None:
            if isinstance(value, Enum):
                type_default = Melty.type_defaults.get(Enum, None)
            else:
                if isinstance(field_name, str) and field_name in Melty.type_defaults:
                    type_default = Melty.type_defaults.get(field_name, None)
                else:
                    type_default = next(
                        (Melty.type_defaults[cls] for cls in value_type.__mro__ if cls in Melty.type_defaults),
                        None
                    )
                    if type_default is None:
                        # Try string name instead
                        type_default = next(
                            (Melty.type_defaults[name] for name in
                             (cls.__name__ for cls in value_type.__mro__)
                             if name in Melty.type_defaults),
                            None)
            child_meta = Meta.get_new_defaults(value=value)
            child_meta.name = field_name
            if type_default is not None:
                child_meta = type_default
        """Get Meta object for a given field, or default."""
        return child_meta

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.visible_in_ui = True
        self.view_function = None
        self.is_meta = True

        self.__dict__.update(kwargs)

    @classmethod
    def get_default(cls, default_value=None):
        """Return the global default Meta object."""
        if cls.default is None:
            cls.default = Meta.get_new_defaults(value=default_value)
        return cls.default

    @classmethod
    def get_new_defaults(cls, value=None, *args, **kwargs):
        """Return the global default Meta object."""
        default = Meta(*args, **kwargs)
        default.value = value
        return default
