"""Experimental explicit-argument annotation backed by the full retained cache.

All declaration and cache methods come from RetainedGui. Only the native binding
uses CacheRenderer, which forwards caller arguments without dependency injection.
Internal geometry state remains owned by the production cache.
"""
from functools import wraps
import inspect
import textwrap

from meltygui.core.rendering import retained_gui_prototype as retained
from meltygui.core.rendering.gui_prototype import _new_native, _renderer_name, _window
from meltygui.core.rendering.gui_window_prototype import _GuiWindow


def bind_cache(native, func=None, *, inject=None, events=(), state_id=None,
               no_save=(), **decoration):
    if inject or events:
        raise TypeError('cache_gui forwards explicit arguments; injection/events parameters are not supported')
    if func is None:
        return lambda fn: bind_cache(native, fn, state_id=state_id, no_save=no_save, **decoration)
    from meltygui.core.rendering._gui_native import CacheRenderer
    identity = native.identities.renderer(_renderer_name(func, state_id))
    renderer = CacheRenderer(native, func, {}, [], [], False, identity, list(no_save))
    @wraps(func)
    def wrapper(input_value=None, **kwargs):
        return renderer(input_value, **kwargs)
    wrapper.__gui_native__ = renderer
    wrapper.decoration = decoration
    wrapper.no_save = tuple(no_save)
    return wrapper


class CacheOnlyRetained(retained.RetainedGui):
    pass


# Bind the unchanged production declaration method to a different native binder.
# All execution, selection, calibration and bookkeeping methods are inherited.
namespace = dict(retained.__dict__, _bind_native=bind_cache)
exec(compile(textwrap.dedent(inspect.getsource(retained.RetainedGui.gui)),
             '<cache-only production declaration>', 'exec'), namespace)
CacheOnlyRetained.gui = namespace['gui']


class CacheOnlyWindow(_GuiWindow):
    def __init__(self, *, graphics=True, state_registry=None):
        self.cache = CacheOnlyRetained(graphics=graphics, state_registry=state_registry)
        self.cache.auto_layout = True
        self.native = _new_native(graphics=graphics, identities=self.cache.identities)
        self.bindings = {}
        self.closed = False
        self.input_backend = None


def cache_gui(func=None, **options):
    """Use within CacheOnlyWindow; retain normal public GUI host dispatch."""
    if func is None:
        return lambda fn: cache_gui(fn, **options)
    @wraps(func)
    def wrapper(input_value=None, **kwargs):
        window = _window.get()
        if window is None or not isinstance(window.cache, CacheOnlyRetained):
            raise RuntimeError('Experimental cache_gui requires CacheOnlyWindow')
        return window.call(wrapper, input_value, kwargs)
    wrapper.__gui__ = True
    wrapper.__gui_definition__ = (func, options)
    return wrapper
