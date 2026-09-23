"""Shared signature discovery and ownership for injected view dependencies."""
import inspect
from typing import get_type_hints
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.state.view_reference import DrawStateSource


def parameter_annotations(func):
    """Resolve independently: one unrelated forward ref must not hide a link."""
    raw = inspect.unwrap(func)
    params = inspect.signature(raw).parameters
    # Resolve self-referential view annotations while @render_func is decorating.
    namespace = dict(getattr(raw, "__globals__", {}))
    if inspect.isfunction(raw):
        namespace[raw.__name__] = raw
    resolved = {}
    if any(isinstance(p.annotation, str) for p in params.values()):
        try:
            resolved = get_type_hints(raw, globalns=namespace)
        except (NameError, TypeError, SyntaxError, AttributeError):
            pass
    result = {}
    for name, param in params.items():
        annotation = param.annotation
        if isinstance(annotation, str) and name in resolved:
            annotation = resolved[name]
        elif isinstance(annotation, str):
            try:
                annotation = eval(annotation, namespace)
                if isinstance(annotation, str):
                    annotation = eval(annotation, namespace)
            except (NameError, TypeError, SyntaxError, AttributeError):
                pass
        result[name] = annotation
    return result


def state_parameters(func, annotations=None):
    """Only state parameters, never the input value or the view's own geometry."""
    if func is None:
        return {}
    annotations = parameter_annotations(func) if annotations is None else annotations
    params = inspect.signature(inspect.unwrap(func)).parameters
    return {name: annotation for name, annotation in annotations.items()
            if name not in ('input_value', 'draw_state')
            and params[name].kind not in (inspect.Parameter.VAR_KEYWORD,
                                         inspect.Parameter.VAR_POSITIONAL)
            and (isinstance(annotation, DrawStateSource)
                 or (inspect.isclass(annotation) and issubclass(annotation, DictConversion)
                     and params[name].default in (None, inspect.Parameter.empty)))}


def owned_state(draw_state, name, state_type):
    """Get the view's local instance; borrowed values never enter this store."""
    value = draw_state.misc.get(name)
    if not isinstance(value, state_type):
        value = state_type()
        draw_state.misc[name] = value
        if hasattr(value, '_owner_ds'):
            value._owner_ds = draw_state
    draw_state.misc_used.add(name)
    return value
