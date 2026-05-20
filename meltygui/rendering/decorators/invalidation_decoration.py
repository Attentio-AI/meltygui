import functools
from typing import Any

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.utils.custom_views import print_stack_trace
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import auto_eval


def live(cls):
    # Get excluded attrs from class (if defined)
    excluded = getattr(cls, '__excluded_attrs__', set())
    deep_refresh_names = getattr(cls, '__deep_refresh__', set())
    invalidate_all = getattr(cls, '__invalidate_all__', set())

    setattr(cls, '__melty__', True)

    # Add internal flag to excluded set
    init_flag = f'__{cls.__name__}_initializing__'
    excluded = excluded | {init_flag}

    # Store the original methods
    original_setattr = cls.__setattr__ if hasattr(cls, '__setattr__') else object.__setattr__
    original_init = cls.__init__

    @functools.wraps(original_setattr)
    def new_setattr(self, name: str, value: Any) -> None:

        # Set the attribute using the original __setattr__
        original_value = getattr(self, name, None)
        if original_setattr == object.__setattr__:
            object.__setattr__(self, name, value)
        else:
            original_setattr(self, name, value)

        if Melty.silence_invalidate:
            return
        # Check if we're initializing
        initializing = getattr(self, init_flag, False)

        excluded = getattr(self, '__excluded_attrs__', set())
        deep_refresh_names = getattr(self, '__deep_refresh__', set())
        invalidate_all = getattr(self, '__invalidate_all__', set())
        do_deep_refresh = name in deep_refresh_names
        visible = name not in excluded
        visible = visible or do_deep_refresh
        #
        if name in invalidate_all:
            if Melty.init_complete():
                print(f"Invalidate all called due to change in {name}")
                Melty.cache.invalidate_all()
                return

        # Call invalidate() if:
        # - not currently initializing
        # - attribute is not excluded
        # - object has invalidate method
        try:
            if value != original_value:
                if not initializing and visible and not name.startswith('_') \
                        and name != "driver" and Melty.frame_count > 3:
                    Melty.last_attr = name
                    if do_deep_refresh:
                        Melty.cache.invalidate_up_by_obj(obj=self, max_depth=2, force=True)
                    else:
                        Melty.cache.invalidate_by_obj(self, name)
        except Exception as e:
            pass

    @functools.wraps(original_init)
    def new_init(self, *args, **kwargs):
        for name in dir(self.__class__):
            attr = getattr(self.__class__, name, None)
            if callable(attr) and getattr(attr, '_add_to_dict', False):
                self.__dict__[name] = getattr(self, name)
            else:
                if isinstance(attr, auto_eval):
                    self.__dict__[name] = attr.fget
                    self.__dict__[name] = attr.fget.__get__(self, self.__class__)
        # Set initialization flag
        object.__setattr__(self, init_flag, True)
        try:
            # Call original __init__
            original_init(self, *args, **kwargs)
        finally:
            # Clear initialization flag
            object.__setattr__(self, init_flag, False)

    # Monkey patch the class
    cls.__setattr__ = new_setattr
    cls.__init__ = new_init

    return cls