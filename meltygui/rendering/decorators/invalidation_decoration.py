import functools
from typing import Any

from src.lsd.gl_gui.utils.custom_views import print_stack_trace
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import auto_eval, Core
from src.lsd.gl_gui.view.invalidation_tracker import Note


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
        # Prefix-routed writes: a view view opts in by declaring LOCATE_PREFIX
        # and a `_locate_set(name, value)` handler (DrawState does it for the
        # set_*where accessors). The prefix is read off type(self), not the
        # decorated class; @live sits on the DictConversion base, so a
        # decoration attribute lookup would never see the subclass's override.
        # Names that don't start with 'l' pay one slice string check and nothing
        # else; that cheapness is why the logic lives here instead of in a
        # __setattr__ of its own, which would add a Python frame to every
        # attribute write in the app.
        if name[:1] == 'l':
            locate_prefix = getattr(type(self), 'LOCATE_PREFIX', None)
            if locate_prefix and name.startswith(locate_prefix):
                self._locate_set(name, value)
                return
        melty = Core.melty
        if melty.silence_invalidate or melty.frame_count < 2:
            # Silenced - the render wrapper's own bookkeeping writes land here
            # ~100x per render_func call, so do nothing with the raw data.
            # scroll_offset is the one attr with a side effect even while
            # silenced: a real change bumps the version which keys the
            # _ancestor_scroll memo (see DrawState._ancestor_scroll).
            if name == 'scroll_offset' and getattr(self, name, None) != value:
                melty.scroll_version = getattr(melty, 'scroll_version', 0) + 1
            try:
                original_setattr(self, name, value)
            except Exception as e:
                print(f"Error setting attribute {name} on {self}: {e}")
                print_stack_trace(e=e)
            return

        # Set the attribute using the original __setattr__
        original_value = None
        try:
            original_value = getattr(self, name, None)
            original_setattr(self, name, value)
        except Exception as e:
            print(f"Error setting attribute {name} on {self}: {e}")
            print_stack_trace(e=e)

        if name == 'scroll_offset' and value != original_value:
            melty.scroll_version = getattr(melty, 'scroll_version', 0) + 1

        # Check if we're initializing
        initializing = getattr(self, init_flag, False)

        excluded = getattr(self, '__excluded_attrs__', set())
        deep_refresh_names = getattr(self, '__deep_refresh__', set())
        invalidate_all = getattr(self, '__invalidate_all__', set())
        do_deep_refresh = name in deep_refresh_names
        visible = name not in excluded
        visible = visible or do_deep_refresh

        if name in invalidate_all:
            if Core.melty.init_complete():
                print(f"Invalidate all called due to change in {name}")
                Core.melty.cache.invalidate_all()
                return

        # Call invalidate() if:
        # - not currently initializing
        # - attribute is not excluded
        # - object has invalidate method
        try:
            # if name == "clipped" or name == "fully_clipped":
            #     changed = value != original_value and value
            # else:
            changed = value != original_value

            if changed:
                # if Core.melty.frame_count > 10 and Core.melty.frame_count % 20 == 0 and name == "width":
                #     print_stack_trace()

                if not initializing and visible and not name.startswith('_') \
                        and name != "driver" and Core.melty.frame_count > 2:
                    Core.melty.last_attr = name
                    from src.lsd.gl_gui.view.attribute_churn import AttributeChurnMonitor
                    AttributeChurnMonitor.record(type(self).__name__, name)
                    if do_deep_refresh:
                        note = Note(name=name, reason="(deep) invalidate_up_by_obj", tint=(0, 0, 1))
                        Core.melty.cache.invalidate_up_by_obj(obj=self, max_depth=2, force=True, note=note)
                    else:
                        note = Note(name=name, reason="invalidate_by_obj", tint=(0, 0, 1))
                        Core.melty.cache.invalidate_by_obj(self, name, note=note)

                    from src.lsd.gl_gui.toggles import Toggles
                    if Toggles.InvalidateTracker.attrib_change_stack_trace:
                        print_stack_trace()




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