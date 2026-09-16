"""Header core functions and supporting definitions."""



def _jump_to_view_source(draw_state):
    """Open the code editor on the def of the function that renders
    `draw_state` — the raw function under the @render_func wrapper, resolved
    the way the context menu's inputs tab does (`inspect.unwrap`). A view
    with no source file (a lambda, a C function) does nothing."""
    import inspect
    from pathlib import Path
    view_fn = getattr(draw_state, "_view_func", None)
    if view_fn is None:
        return
    try:
        view_fn = inspect.unwrap(view_fn)
        fn_file = inspect.getsourcefile(view_fn)
    except (TypeError, ValueError):
        return
    if not fn_file:
        return
    from meltygui.extensions import jump_to_symbol as _jump_to_symbol_def
    _jump_to_symbol_def(view_fn, Path(fn_file))
