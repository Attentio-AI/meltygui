from typing import Any


class Meta:
    default = None

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.visible_in_ui = True
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_object
        self.view_function = draw_object
        self.default_value = None
        self.datatype = Any
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
        default.datatype = type(value) if value is not None else Any
        return default
