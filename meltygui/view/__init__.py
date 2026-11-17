"""Reusable feature render functions.

`meltygui.view.<module>` loads that module when it is first asked for; importing this package loads
none. The IDE completes the modules from __init__.pyi, which `stubs.py` (repository root) writes.
"""
import importlib


def __getattr__(name):
    path = f'{__name__}.{name}'
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError as error:
        if error.name != path:
            raise
        raise AttributeError(f'{__name__} has no {name!r}') from None
