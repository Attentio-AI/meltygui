"""Opt-in @gui functions hosted by the existing @glfw_window lifecycle."""
from contextvars import ContextVar
from functools import wraps
import inspect

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.injected_state import parameter_annotations
from meltygui.core.rendering.gui_imgui_import import enable_imgui_import

enable_imgui_import()

try:
    # What a view is called with when no value is passed to it: not None, which a caller may pass.
    from meltygui.core.rendering._gui_native import ABSENT
except ImportError:     # not built: `_new_native` says how, when the first view is drawn
    ABSENT = object()

# Lever: the names a call can carry for the framework itself. A view whose first parameter is one of
# them takes no value: nothing reaches it by position, and what it returns is not kept as its state.
# Add a name here when the framework starts to read it from a call.
FILLED = frozenset((
    'width', 'height', 'left', 'top', 'scroll_offset', 'content_offset', 'min_width', 'max_width',
    'min_height', 'max_height', 'draw_state', 'cache', 'event', 'view_events', 'unique', 'depth',
    'key', 'name', 'initial', 'popup', 'melty_window', 'glfw_window', 'scroll', 'tint', 'movable',
    'resizable', 'return_extras', 'window_pos'))


_window = ContextVar('prototype_gui_window', default=None)


def positional(func):
    """A view's parameter names in order, and the one that is its value: the first, which a value
    passed by position goes to and a returned edit is about (None: the framework fills it)."""
    names = tuple(name for name, parameter in inspect.signature(func).parameters.items()
                  if parameter.kind is not inspect.Parameter.VAR_KEYWORD)
    return names, names[0] if names and names[0] not in FILLED else None


def bind_positional(func, names, first, more, kwargs):
    """The values after the first go to the parameters after the first by name, as Python binds them."""
    if first is None or len(more) >= len(names):
        raise TypeError(f'{func.__name__}() takes {len(names) if first else 0} values by position, '
                        f'{len(more) + 1} were given')
    for name, value in zip(names[1:], more):
        if name in kwargs:
            raise TypeError(f'{func.__name__}() got multiple values for {name!r}')
        kwargs[name] = value


def _check_position_arguments(arguments, no_save=()):
    if ('window_pos' in arguments or 'window_pos' in (arguments.get('initial') or {})
            or 'window_pos' in no_save):
        raise TypeError('@gui uses relative left and top, not window_pos')


def os_window(func=None, **decoration):
    """An OS-window @gui root: shorthand for ``gui(glfw_window=True, ...)``."""
    return gui(func, **{**decoration, 'glfw_window': True})


def gui(func=None, *, glfw_window=False, **decoration):
    """Declare a view; its enclosing @glfw_window owns native state and caches.

    The first parameter is the view's value, under any name: what a caller passes
    by position goes to it (further values go to the next parameters, as in Python)
    and the returned ``(changed, value)`` is about it. Returning None means
    unchanged. Passed nothing, it resolves like every parameter, from the view's
    state (``draw_state.<name> = value``, or a returned edit, which is the same
    write), then the defaults. A first parameter the framework fills (``FILLED``:
    ``width``, ``draw_state`` ...) is not a value. ``glfw_window=True`` hosts
    this function as an OS window; ``@os_window`` is the convenient spelling.
    ``use_cache=False`` draws an immediate host/toolbar body. Other views retain
    their output and execute only when invalidated. No application runtime setup.
    With ``Toggles.Rust.auto_cache`` enabled, retained commands share a pass until
    measured work and reuse justify optional raster storage. ``use_cache=True``
    is normal policy eligibility; disabling the global toggle forces the legacy
    per-view raster representation without changing state or input identity.

    Identity uses the logical parent, renderer, then ``key``, nonempty ``name``,
    or an anonymous occurrence index, in that order. Named/keyed calls do not
    consume occurrences. Input values never participate. Keys accept str, None,
    signed 128-bit int, or tuples of these. ``state_id="..."`` overrides the
    renderer's module/qualified name to preserve identity through source moves.
    Declared parameters persist with the state object; ``no_save=("field",)``
    excludes transient fields. Relative placement, size and scroll are ordinary
    state parameters; framework context and derived measurements are transient.

    ``scroll=True`` makes a cached viewport over child @gui views. Child
    ``left`` and ``top`` are unscrolled content coordinates. Each axis resolves
    independently; an omitted axis follows the existing flow/default placement.
    ``initial={"left": ..., "top": ...}`` seeds placement once. Raw ImGui drawing
    in the viewport body is fixed chrome; put scrolling content in child views.

    ``popup=<open event>`` on a call shows the view as a temporary popup (a
    dropdown list, a picker): open from the run the event is true in until a
    press outside it, Escape or its own ``cache.focus.close()``. The caller
    keeps no open flag; rules and levers are core/input/focus.py.
    """
    _check_position_arguments(decoration, decoration.get('no_save', ()))
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
            if name not in ('use_cache', 'live', 'inject', 'events', 'state_id', 'no_save')})

    @wraps(func)
    def wrapper(input_value=ABSENT, /, *more, **kwargs):
        window = _window.get()
        if window is None:
            raise RuntimeError('@gui must be called inside @os_window (or a @glfw_window hosted @gui root)')
        try:
            # An already bound view dispatches straight to its binding.
            bound = window.bindings[wrapper]
        except KeyError:
            # First use, or a closed window whose bindings were released.
            return window.call(wrapper, input_value, kwargs, more)
        if more:
            return bound(input_value, *more, **kwargs)
        return bound(input_value, **kwargs)

    wrapper.__gui__ = True
    wrapper.__gui_definition__ = (func, decoration)
    return wrapper


def _renderer_name(func, state_id=None):
    return state_id if state_id is not None else f'{func.__module__}:{func.__qualname__}'


def _new_native(*, graphics=True, identities=None):
    """Low-level engine constructor, also used by the isolated wrapper benchmark."""
    try:
        from meltygui.core.rendering._gui_native import Runtime
    except ImportError as error:
        raise RuntimeError('Build the experiment with .venv/bin/python tools/build_gui_prototype.py') from error
    if graphics:
        import meltygui_imgui as imgui
    else:
        imgui = None
    return Runtime(imgui, identities)


def _bind_native(native, func=None, *, inject=None, events=(), state_id=None, no_save=(), **decoration):
    """Compile the Python signature for Rust dispatch, once per function/engine."""
    _check_position_arguments(decoration, no_save)
    if func is None:
        return lambda function: _bind_native(native, function, inject=inject, events=events,
                                            state_id=state_id, no_save=no_save, **decoration)
    from meltygui.core.rendering._gui_native import Renderer
    factories = dict(inject or {})
    identity = native.identities.renderer(_renderer_name(func, state_id))

    def compile_plan():
        signature = inspect.signature(func)
        annotations = parameter_annotations(func)
        parameters = []
        var_kwargs = False
        first = positional(func)[1]
        for name, parameter in signature.parameters.items():
            if name == 'window_pos':
                raise TypeError('@gui parameters use left and top, not window_pos')
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                var_kwargs = True
                continue
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL):
                raise TypeError('@gui prototype accepts named parameters and **kwargs, not positional-only or *args')
            factory = factories.get(name)
            annotation = annotations.get(name)
            if (factory is None and name != 'draw_state'
                    and inspect.isclass(annotation) and issubclass(annotation, DictConversion)
                    and parameter.default in (None, inspect.Parameter.empty)):
                factory = annotation
            # A value need not be passed: without a default of its own it is None.
            parameters.append((name, parameter.default is not inspect.Parameter.empty or name == first,
                               None if parameter.default is inspect.Parameter.empty else parameter.default,
                               factory))
        return Renderer(native, func, decoration, parameters, list(events), var_kwargs, identity,
                        list(no_save) + list(events), first is not None)

    renderer = compile_plan()
    code = func.__code__
    names, first = positional(func)

    def refresh():
        """Recompile the parameter plan after the function's code was replaced."""
        nonlocal code, names, first
        if func.__code__ is not code:
            renderer.reconfigure(compile_plan())
            code = func.__code__
            names, first = positional(func)

    @wraps(func)
    def wrapper(input_value=ABSENT, /, *more, **kwargs):
        if func.__code__ is not code:
            refresh()
        if more:
            bind_positional(func, names, first, more, kwargs)
        return renderer(input_value, **kwargs)

    wrapper.__gui_native__ = renderer
    # Retained captures call the native renderer directly and refresh through this.
    wrapper.__gui_refresh__ = refresh
    wrapper.decoration = decoration
    wrapper.no_save = tuple(no_save)
    return wrapper
