import inspect


def exclude(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple)):
            from_args = args[0]
        else:
            from_args = set(args)
        already_excluded = getattr(cls, '__excluded_attrs__', set())
        merged_names = already_excluded.union(set(from_args))
        merged_names = merged_names.union(from_args)

        setattr(cls, '__excluded_attrs__', merged_names)
        return cls
    return decorator


global_hotkeys = {}

def no_save(*args, **kwargs):
    def decorator(cls):
        if len(args) == 1 and isinstance(args[0], (list, set, tuple)):
            from_args = args[0]
        else:
            from_args = set(args)
        already_excluded = getattr(cls, '__no_save__', set())
        merged_names = already_excluded.union(set(from_args))
        merged_names = merged_names.union(from_args)

        setattr(cls, '__no_save__', merged_names)
        return cls

    return decorator


def hotkey(key):
    """
    This is the decorator factory. It takes arguments for the decorator.
    """

    def actual_decorator(func):
        """
        This is the actual decorator. It takes the function to be decorated.
        """
        sig = inspect.signature(func)
        params = sig.parameters

        if isinstance(key, int):
            from src.lsd.gl_gui.model.core_model.new_core_model import Hotkey
            the_hotkey = Hotkey(key=key)
        else:
            the_hotkey = key


        def wrapper(*args, **kwargs):
            to_remove = []
            for name, arg in kwargs.items():
                if name not in params:
                    to_remove.append(name)
            for name in to_remove:
                kwargs.pop(name)

            for wanted_name, param in params.items():
                from src.lsd.gl_gui.melty import Melty
                if wanted_name in Melty.global_attrs and wanted_name not in kwargs:
                    kwargs[wanted_name] = Melty.global_attrs[wanted_name]

            result = func(*args, **kwargs)  # Call the original function
            return result

        if hotkey in global_hotkeys:
            print(f"Warning: hotkey '{hotkey}' is already registered to "
                  f"{global_hotkeys[hotkey].__name__}, overwriting with {func.__name__}")
        global_hotkeys[the_hotkey] = wrapper

        return wrapper

    return actual_decorator

