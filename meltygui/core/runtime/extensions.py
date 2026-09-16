"""Optional application services; the UI toolkit never imports their providers.

Providers register callbacks when imported. Lookups happen at call time so live
reloads replace callbacks without pinning an earlier function or module.
"""
_services = globals().get('_services', {})


def register(name, callback):
    _services[name] = callback


def get(name):
    return _services.get(name)


def call(name, *args, **kwargs):
    callback = get(name)
    return callback(*args, **kwargs) if callback is not None else None


def source_folders():
    return call('source_folders') or ()


def source_window(instance=0):
    return call('source_window', instance)


def source_views():
    return call('source_views') or {}


def open_source(path, line_number=None, **kwargs):
    callback = get('source_open')
    if callback is not None:
        return callback(path, line_number=line_number, **kwargs)
    from meltygui.editor.source_preview import open_source_preview
    return open_source_preview(path, line_number, kwargs.get('token'))


def jump_to_symbol(obj, path):
    import inspect
    try:
        line = inspect.getsourcelines(inspect.unwrap(obj))[1] if obj is not None else None
    except (TypeError, OSError):
        line = None
    return open_source(path, line_number=line, token=getattr(obj, '__name__', None))
