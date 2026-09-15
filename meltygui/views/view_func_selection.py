"""Render-function references and the caller's editable callee projection."""
import inspect
import sys

from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core


def resolve_view_func(value):
    """Resolve registered references without evaluating comment expressions."""
    if value is None:
        return None
    if inspect.ismethod(value):
        value = value.__func__
    if callable(value):
        name = getattr(value, "__name__", None)
        registered = Core.melty.render_funcs_by_name.get(name)
        if registered is not None and inspect.unwrap(registered) is inspect.unwrap(value):
            return registered
        from src.lsd.gl_gui.render_funcs import _LazyRenderFunc
        if isinstance(value, _LazyRenderFunc):
            return value._resolve()
        if hasattr(value, "__render_func__"):
            return value
        raise ValueError(f"Not a registered render function: {name or value!r}")
    if isinstance(value, str):
        reference = str(value)
        prefix = "__import__('src.lsd.gl_gui.render_funcs', fromlist=['RenderFuncs']).RenderFuncs."
        if reference.startswith(prefix):
            reference = reference[len(prefix):]
        parts = reference.split(".")
        if all(part.isidentifier() for part in parts):
            candidate = Core.melty.render_funcs_by_name.get(parts[-1])
            if candidate is not None:
                module = getattr(inspect.unwrap(candidate), "__module__", "")
                if len(parts) == 1 or ".".join(parts[:-1]) in ("RenderFuncs", "melty", module):
                    return candidate
        raise ValueError(f"Unknown render function: {value}")
    raise ValueError(f"Invalid render function reference: {value!r}")


def view_func_name(value):
    return getattr(value, "__name__", str(value)) if value is not None else "Default"


def view_reference_code(value, filename=None):
    """Use an existing binding, else an import expression valid in any module."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import CodeLine
    function = resolve_view_func(value)
    if function is None:
        return None
    from src.lsd.gl_gui.render_funcs import RenderFuncs
    for module in tuple(sys.modules.values()):
        if module is None or not filename or getattr(module, "__file__", None) != str(filename):
            continue
        for name, binding in tuple(vars(module).items()):
            if not name.isidentifier():
                continue
            if binding is function or binding is inspect.unwrap(function):
                return CodeLine(name)
        for name, binding in tuple(vars(module).items()):
            if name.isidentifier() and binding is RenderFuncs:
                return CodeLine(f"{name}.{function.__name__}")
    return CodeLine("__import__('src.lsd.gl_gui.render_funcs', "
                    f"fromlist=['RenderFuncs']).RenderFuncs.{function.__name__}")


def comment_view_func(input_value, kwargs):
    """Own comment wins over the enclosing slot, just like the kwargs splat."""
    selected = None
    collection = kwargs.get("collection")
    if isinstance(collection, dict) and "key" in kwargs:
        overrides = collection.get("__overrides__", {})
        if isinstance(overrides, dict):
            entry = overrides.get(f"__{kwargs['key']}__", {})
            if isinstance(entry, dict):
                selected = entry.get("view_func")
    if isinstance(input_value, dict):
        overrides = input_value.get("__overrides__", {})
        if isinstance(overrides, dict):
            selected = overrides.get("view_func", selected)
    return selected


def configured_view_func(input_value, kwargs, decoration=None, state_view=None):
    """Resolve selection before entering a wrapper's layout/cache scopes."""
    defaults = Core.melty.default_kwargs_by_type[kwargs.get("real_type", type(input_value))]
    from src.lsd.gl_gui.view.core_views.core_render import _codec_render_kwargs
    selected = _codec_render_kwargs(type(input_value)).get("view_func")
    if not isinstance(input_value, (dict, type)):
        selected = getattr(input_value, "view_func", selected)
    selected = (decoration or {}).get("view_func", selected)
    selected = defaults.get("view_func", selected)
    if isinstance(input_value, dict):
        decorators = input_value.get("decorators", {})
        parsed_defaults = decorators.get("defaults", {}) if isinstance(decorators, dict) else {}
        if isinstance(parsed_defaults, dict):
            selected = parsed_defaults.get("view_func", selected)
    collection = kwargs.get("collection")
    attributes = Core.melty.default_kwargs_by_attrib_type[type(collection)]
    selected = attributes.get(kwargs.get("key"), {}).get("view_func", selected)
    if state_view is not None:
        selected = state_view
    selected = kwargs.get("view_func") or selected
    modes = kwargs.get("mode")
    for mode in modes if isinstance(modes, tuple) else (modes,):
        if mode is not None:
            config = mode.get_config_for(input_value)
            if config is not None and config.kwargs:
                selected = config.kwargs.get("view_func", selected)
    selected = comment_view_func(input_value, kwargs) or selected
    return resolve_view_func(selected)


class CallerViewSource(dict):
    """The call's arguments plus a virtual view_func backed by its callee."""
    def __init__(self, parsed, filename=None, allow_direct=True):
        super().__init__(parsed)
        self.parsed = parsed
        self.filename = filename
        self.direct = False
        if allow_direct and "view_func" not in parsed:
            try:
                function = resolve_view_func(parsed.get("__callee__", getattr(parsed, "func_name", None)))
            except ValueError:
                function = None
            if function is not None:
                self.direct = True
                dict.__setitem__(self, "view_func", function)

    def __setitem__(self, key, value):
        if key == "view_func" and self.direct:
            if value is None:
                raise ValueError("A direct call needs a render function")
            self.parsed["__callee__"] = view_reference_code(value, self.filename)
            from src.lsd.gl_gui.view.core_conversion.bubbling import _notify
            _notify(self.parsed)
        else:
            self.parsed[key] = value
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self.pop(key)

    def pop(self, key, default=None):
        if key == "view_func" and self.direct:
            raise ValueError("A direct call needs a render function")
        self.parsed.pop(key, None)
        return super().pop(key, default)


class WindowViewSource(CallerViewSource):
    """Window decorator kwargs, with the decorated renderer as their default.

    The implicit view_func exists only in this projection. Parameter edits
    write through to the decorator parse; choosing another renderer creates
    an explicit view_func kwarg through the same path.
    """
    def __init__(self, parsed, function, filename=None):
        super().__init__(parsed, filename, allow_direct=False)
        self.function = function
        self.implicit_view_func = self.get("view_func") is None
        if self.implicit_view_func:
            dict.__setitem__(self, "view_func", function)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "view_func":
            self.implicit_view_func = value is None
            if self.implicit_view_func:
                dict.__setitem__(self, key, self.function)

    def pop(self, key, default=None):
        value = super().pop(key, default)
        if key == "view_func":
            self.implicit_view_func = True
            dict.__setitem__(self, key, self.function)
        return value
