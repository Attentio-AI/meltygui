"""Opt-in @gui functions hosted by the existing @glfw_window lifecycle."""
from contextvars import ContextVar
from functools import wraps
import inspect

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.injected_state import parameter_annotations
from meltygui.core.rendering.gui_imgui_import import enable_imgui_import

enable_imgui_import()


_window = ContextVar('prototype_gui_window', default=None)


def os_window(func=None, **decoration):
    """An OS-window @gui root: shorthand for ``gui(glfw_window=True, ...)``."""
    return gui(func, **{**decoration, 'glfw_window': True})


def gui(func=None, *, glfw_window=False, **decoration):
    """Declare a view; its enclosing @glfw_window owns native state and caches.

    ``input_value`` is optional in the function signature. Returning None means
    unchanged, preserving any caller-supplied input. ``glfw_window=True`` hosts
    this function as an OS window; ``@os_window`` is the convenient spelling.
    ``use_cache=False`` draws an immediate host/toolbar body. Other views retain
    their output and execute only when invalidated. No application runtime setup.
    """
    if func is None:
        return lambda function: gui(function, glfw_window=glfw_window, **decoration)
    if glfw_window:
        from meltygui.core.runtime.app import glfw_window as host_window
        # The existing host defines its options; do not maintain a second list.
        host_options = {name: decoration.pop(name) for name, parameter in
                        inspect.signature(host_window).parameters.items()
                        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and name in decoration}
        view = gui(func, **decoration)
        return host_window(view, **host_options, **{
            name: value for name, value in decoration.items()
            if name not in ('use_cache', 'live', 'inject', 'events')})

    @wraps(func)
    def wrapper(input_value=None, **kwargs):
        window = _window.get()
        if window is None:
            raise RuntimeError('@gui must be called inside @os_window (or a @glfw_window hosted @gui root)')
        return window.call(wrapper, input_value, kwargs)

    wrapper.__gui__ = True
    wrapper.__gui_definition__ = (func, decoration)
    return wrapper


def _new_native(*, graphics=True):
    """Low-level engine constructor, also used by the isolated wrapper benchmark."""
    try:
        from meltygui.core.rendering._gui_native import Runtime
    except ImportError as error:
        raise RuntimeError('Build the experiment with .venv/bin/python tools/build_gui_prototype.py') from error
    if graphics:
        import meltygui_imgui as imgui
    else:
        imgui = None
    return Runtime(imgui)


def _bind_native(native, func=None, *, inject=None, events=(), **decoration):
    """Compile the Python signature for Rust dispatch, once per function/engine."""
    if func is None:
        return lambda function: _bind_native(native, function, inject=inject, events=events, **decoration)
    from meltygui.core.rendering._gui_native import Renderer
    factories = dict(inject or {})

    def compile_plan():
        signature = inspect.signature(func)
        annotations = parameter_annotations(func)
        parameters = []
        var_kwargs = False
        for name, parameter in signature.parameters.items():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                var_kwargs = True
                continue
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL):
                raise TypeError('@gui prototype accepts named parameters and **kwargs, not positional-only or *args')
            factory = factories.get(name)
            annotation = annotations.get(name)
            if (factory is None and name not in ('input_value', 'draw_state')
                    and inspect.isclass(annotation) and issubclass(annotation, DictConversion)
                    and parameter.default in (None, inspect.Parameter.empty)):
                factory = annotation
            parameters.append((name, parameter.default is not inspect.Parameter.empty,
                               None if parameter.default is inspect.Parameter.empty else parameter.default,
                               factory))
        return Renderer(native, func, decoration, parameters, list(events), var_kwargs)

    renderer = compile_plan()
    code = func.__code__

    @wraps(func)
    def wrapper(input_value=None, **kwargs):
        nonlocal code
        if func.__code__ is not code:
            renderer.reconfigure(compile_plan())
            code = func.__code__
        return renderer(input_value, **kwargs)

    wrapper.__gui_native__ = renderer
    wrapper.decoration = decoration
    return wrapper
