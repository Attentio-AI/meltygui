"""Opt-in @gui functions hosted by the existing @glfw_window lifecycle."""
from contextvars import ContextVar
from functools import wraps
import inspect
import types
from typing import Any, get_origin

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.injected_state import parameter_annotations
from meltygui.core.rendering.gui_imgui_import import enable_imgui_import

enable_imgui_import()

# What a view is called with when no value is passed to it: not None, which a caller may pass.
# Python's own marker for an empty parameter, so the native side finds the same object (`absent`, lib.rs).
ABSENT = inspect.Parameter.empty

# Lever: the names a call can carry for the framework itself. A view whose first parameter is one of
# them takes no value: nothing reaches it by position, and what it returns is not kept as its state.
# Add a name here when the framework starts to read it from a call.
FILLED = frozenset((
    'width', 'height', 'left', 'top', 'scroll_offset', 'content_offset', 'min_width', 'max_width',
    'min_height', 'max_height', 'draw_state', 'cache', 'event', 'view_events', 'unique', 'depth',
    'key', 'name', 'initial', 'popup', 'melty_window', 'glfw_window', 'scroll', 'tint', 'movable',
    'resizable', 'return_extras', 'window_pos', 'canvas'))


# Lever: classes that are an annotation and build nothing (`constructor`).
UNBUILT = frozenset((object, Any, type, type(None), types.UnionType, inspect.Parameter.empty))

# Lever: what runs after a view's body, while the view is still the one executing, by view name
# (`_renderer_name`). It is how a feature with a decorator of its own adds to a view without being
# part of `@gui`: `@context_menu` (core/input/context_menu.py) declares its popup here, as a child of
# the view. A view with no row here runs the body it always did (`followed`).
AFTER = {}

_window = ContextVar('prototype_gui_window', default=None)


def constructor(annotation):
    """The class that builds a parameter annotated `annotation` when nobody passes it: `int` gives 0,
    `list[int]` a list, a class an instance of it. None where there is nothing to build: no
    annotation, `object`, `Any`, a union, an abstract class or a protocol."""
    kind = get_origin(annotation) or annotation
    if not inspect.isclass(kind) or kind in UNBUILT or inspect.isabstract(kind) or getattr(kind, '_is_protocol', False):
        return None
    return kind


def needs_arguments(function):
    """Whether a call of `function` must pass something. A C type that keeps its signature to
    itself (`int`, `dict`) is taken to need nothing."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(parameter.default is parameter.empty
               and parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD) for parameter in parameters)


class Ask:
    """What a parameter resolves to when nobody passed it and its type cannot be built without
    arguments: the view asks the user for them (`asking`)."""
    __slots__ = ('constructor',)

    def __init__(self, constructor):
        self.constructor = constructor


def asking(func, asked, hidden):
    """The body of a view whose parameters `asked` have a type that needs arguments. While one of
    them is an `Ask`, the view shows `draw_function` of that type in its own place; what its button
    builds is written to the view's state (`draw_state.<name>`), and the view's own body runs from
    the next frame on. `hidden`: the names among `draw_state` and `cache` that are no parameters of
    `func`; the plan added them for this."""
    @wraps(func)
    def body(**arguments):
        draw_state = arguments['draw_state']
        cache = arguments.get('cache')
        for name in hidden:
            del arguments[name]
        waiting = False
        for name in asked:
            pending = arguments[name]
            if pending.__class__ is Ask:
                from meltygui.view.function_view import draw_function
                built, _, form = draw_function(pending.constructor, name=name, return_extras=True)
                if built:
                    value = form.result
                    del form.result                 # the view's from here on, not the form's too
                    setattr(draw_state, name, value)
                    if cache is not None:
                        cache.wake_next(cache.current)      # a state write in a view's own run does not run it again
                waiting = True
        if waiting:
            return False, None
        return func(**arguments)
    return body


def followed(body, after, hidden):
    """The body of a view with a row in `AFTER`: `after(cache)` runs once the body has returned, so
    what it declares is a child of the view. An immediate view outside a window has no cache and is
    skipped; under a window it is called too, with the window's cache, and is not the view on
    `cache.stack`: the step has to tell (`attached`, core/input/context_menu.py).
    `hidden`: `cache` is no parameter of the body; the plan added it for this."""
    @wraps(body)
    def run(**arguments):
        cache = arguments.get('cache')
        if hidden:
            del arguments['cache']
        result = body(**arguments)
        if cache is not None:
            after(cache)
        return result
    return run


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
    write), then the defaults. A parameter without a default is built by its
    annotation, once, and kept as state (``count: int`` starts at 0); a type that
    needs arguments is asked for with ``draw_function`` in the view's place, and
    a parameter without a type is None. A first parameter the framework fills (``FILLED``:
    ``width``, ``draw_state`` ...) is not a value. ``draw_state.<name>`` reads
    every parameter, a passed one included; a passed value is read through to
    the caller's, never copied to the state or saved. ``glfw_window=True`` hosts
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
        asked = []
        var_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
        first = positional(func)[1]
        for name, parameter in signature.parameters.items():
            if name == 'window_pos':
                raise TypeError('@gui parameters use left and top, not window_pos')
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                continue
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL):
                raise TypeError('@gui prototype accepts named parameters and **kwargs, not positional-only or *args')
            factory = factories.get(name)
            annotation = annotations.get(name)
            if (factory is None and name != 'draw_state'
                    and inspect.isclass(annotation) and issubclass(annotation, DictConversion)
                    and parameter.default in (None, inspect.Parameter.empty)):
                factory = annotation
            # No parameter needs to be passed. Without a default its type builds it, once per view
            # (the factory; kept in the state like an injected one). A type that needs arguments is
            # asked for (`asking`), and without a type the parameter is None. A name the framework
            # fills is the framework's to supply.
            default = parameter.default
            kind = None
            if default is inspect.Parameter.empty:
                default = None
                if factory is None and name not in FILLED:
                    kind = constructor(annotation)
                if kind is not None and needs_arguments(kind):
                    default = Ask(kind)
                    asked.append(name)
                elif kind is not None:
                    factory = kind
            supplied = parameter.default is not inspect.Parameter.empty or name not in FILLED
            parameters.append((name, supplied, default, factory, kind))
        body = func
        after = AFTER.get(_renderer_name(func))
        hidden = ()
        if (asked or after is not None) and not var_kwargs:
            # (`cache` has a default here: an immediate view outside a window has none, and runs every frame.)
            wanted = ('draw_state', 'cache') if asked else ('cache',)
            hidden = tuple(name for name in wanted if name not in signature.parameters)
            parameters.extend((name, name == 'cache', None, None, None) for name in hidden)
        if asked:
            body = asking(func, tuple(asked), hidden)
        if after is not None:
            # (`asking` takes the hidden names off before the view's own body; else `followed` does.)
            body = followed(body, after, 'cache' in hidden and not asked)
        return Renderer(native, body, decoration, parameters, list(events), var_kwargs, identity,
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
