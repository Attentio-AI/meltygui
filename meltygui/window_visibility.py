"""Visibility contract shared by nested Melty and GLFW child windows."""


def requested_window_closed(closed, kwargs, *, first_request=False):
    if first_request and 'open_requested' in kwargs:
        closed = True
    if kwargs.get('closed') is not None:
        closed = bool(kwargs['closed'])
    if kwargs.get('open_requested'):
        closed = False
    return closed


from meltygui.state.dict_conversion import DictConversion


class WindowOverrideState(DictConversion):
    """View-local source tracking; automatic geometry corrections aren't edits."""
    def __init__(self):
        super().__init__()
        self.position = None
        self.comment_closed = None
        self.pending_marker_closed = None
        self.native_kwargs = None


def override_state(draw_state):
    state = draw_state.misc.get('window_overrides')
    if state is None:
        state = WindowOverrideState()
        draw_state.misc['window_overrides'] = state
    return state


def resolved_window_kwargs(draw_state, kwargs):
    from meltygui.core.parameter_core import anywhere_value
    resolved = kwargs
    pending = getattr(draw_state, '_sa_pending', None) or {}
    for name in ('closed', 'window_pos'):
        if name in kwargs or name in pending:
            if resolved is kwargs:
                resolved = dict(kwargs)
            resolved[name] = anywhere_value(name, draw_state, live_kwargs=kwargs)
    return resolved


def adopt_window_position(draw_state, kwargs):
    """Adopt changed source positions, not the same value on every frame.

    In particular an off-screen rescue or pin collision stays local rather
    than snapping back each frame or writing itself into source code.
    """
    position = kwargs.get('window_pos')
    if position is None:
        return
    position = tuple(int(v) for v in position)
    state = override_state(draw_state)
    if state.position != position:
        state.position = position
        draw_state.window_pos = position


def user_window_closed(draw_state, closed):
    """Only a user's close/open action writes back to the driving source."""
    draw_state.locate_closed = bool(closed)
    draw_state.closed = bool(closed)


def user_window_position(draw_state, position):
    position = tuple(round(v) for v in position)
    draw_state.locate_window_pos = position
    draw_state.window_pos = position


def native_user_window_closed(draw_state, closed):
    # The native surface renders an inline root with different kwargs. Source
    # discovery for its controls must see the parent-side window request.
    state = override_state(draw_state)
    original = draw_state._kwargs
    if state.native_kwargs is not None:
        draw_state._kwargs = state.native_kwargs
    try:
        user_window_closed(draw_state, closed)
    finally:
        draw_state._kwargs = original


def native_user_window_position(draw_state, position):
    state = override_state(draw_state)
    original = draw_state._kwargs
    if state.native_kwargs is not None:
        draw_state._kwargs = state.native_kwargs
    try:
        draw_state.locate_window_pos = tuple(round(v) for v in position)
    finally:
        draw_state._kwargs = original


def marker_user_visibility(marker, closed):
    state = override_state(marker)
    window = getattr(marker, '_lv_window_ds', None)
    if window is None:
        state.pending_marker_closed = bool(closed)
    else:
        user_window_closed(window, closed)


def sync_marker_visibility(marker, comment_args):
    state = override_state(marker)
    closed = comment_args.pop('closed', None)
    closed = None if closed is None else bool(closed)
    if state.comment_closed != closed:
        state.comment_closed = closed
        marker._lv_open = None if closed is None else not closed
        window = getattr(marker, '_lv_window_ds', None)
        if window is not None:
            window.closed = bool(closed)


def window_edit_is_local(draw_state, parameter):
    source = (getattr(draw_state, '_sa_last_source', None) or {}).get(parameter)
    return source in (None, 'draw state')
