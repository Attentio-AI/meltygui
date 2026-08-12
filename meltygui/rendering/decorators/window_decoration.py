

_register = None
_pending = []


def set_window_registrar(register_fn):
    global _register
    _register = register_fn
    for cls, kwargs in _pending:
        register_fn(cls, kwargs)
    _pending.clear()

#[tint=(0.0, 0.541, 0.85)]
def window(cls=None, **kwargs):
    def wrap(cls):
        if _register is None:
            _pending.append((cls, kwargs))
        else:
            _register(cls, kwargs)
        return cls

    if cls is None:
        return wrap
    return wrap(cls)