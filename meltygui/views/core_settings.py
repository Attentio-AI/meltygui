import inspect
from functools import wraps
from typing import Any


def meta_preset(func, *o_args, **o_kwargs):
    sig = inspect.signature(func)
    params = sig.parameters
    wanted_params = list(params.keys())

    @wraps(func)
    def wrapper(*args, **kwargs):
        first_arg = args[0] if args else None

        if 'for_type' in kwargs and not isinstance(first_arg, type):
            def class_wrapper(cls):
                inner_args = args[1:]
                return wrapper(cls, *inner_args, **kwargs)
            return class_wrapper

        if isinstance(first_arg, type):
            if 'default_value' in kwargs:
                default_value = kwargs['default_value']
            else:
                default_value = None
            for_type = kwargs.get('for_type', None)
            kwargs.pop('for_type', None)
            kwargs.pop('default_value', None)
            args = args[1:] if len(args) > 1 else ()

            retrieved_meta = func(default_value=default_value, *args, **kwargs)
            if for_type is not None:
                first_arg.default_meta_for = getattr(first_arg, 'default_meta_for', {})
                first_arg.default_meta_for[for_type] = retrieved_meta
            else:
                first_arg.meta = retrieved_meta

            return first_arg
        else:
            arg_idx = 0
            # Clean up kwargs to only what the function wants
            for wanted_param in wanted_params:
                if wanted_param not in kwargs:
                    kwargs[wanted_param] = args[arg_idx] if arg_idx < len(args) else None
                arg_idx += 1

            retrieved_meta = func(**kwargs)
            return retrieved_meta
    return wrapper
