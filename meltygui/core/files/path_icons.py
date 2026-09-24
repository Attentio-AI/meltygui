"""Directory subscriptions owned by an individual path icon view."""
from types import SimpleNamespace
from functools import wraps
from meltygui.core.files import file_explorer_core as explorer


class IconInvalidator:
    """An icon owner's subscription distinguishes file changes from repaints."""
    def __init__(self, draw_state, folders):
        self.draw_state = draw_state
        self.folders = folders

    def invalidate(self):
        self.folders.mark_dirty()
        self.draw_state.invalidate()


def sync_icon_watches(draw_state, state, directories):
    if getattr(state, 'watch_owner', None) is None:
        state.watch_owner = IconInvalidator(draw_state, state.folders)
        # Transfer live subscriptions when the new owner is introduced by hotswap.
        for directory in state.watches:
            owners = explorer._WATCHERS.get(directory)
            if owners is not None:
                owners.discard(draw_state)
                owners.add(state.watch_owner)
    for directory in directories - state.watches.keys():
        holder = state.watches[directory] = SimpleNamespace(_watched=None)
        explorer.watch_directory(state.watch_owner, holder, directory)
    for directory in state.watches.keys() - directories:
        del state.watches[directory]
        owners = explorer._WATCHERS.get(directory)
        if owners is not None:
            owners.discard(state.watch_owner)
            if not owners:
                del explorer._WATCHERS[directory]
                explorer.FileWatch.unwatch_dir(directory)


def cleanup_path_icon(draw_state):
    state = draw_state.misc.get('icon_state')
    if state is not None:
        state.folders.close()
        sync_icon_watches(draw_state, state, set())


def dispatch_icon_loads(draw_state, state):
    from meltygui.core.runtime.background import Background
    from meltygui.core.melty import Melty
    state.folders.dispatch(lambda: Melty.post_to_render(draw_state.invalidate),
                           submit=Background.submit_io, check_stale=False)


def with_path_icons(func):
    """Batch plain icon painting under its existing view's resource lifecycle."""
    @wraps(func)
    def draw(*args, **kwargs):
        from meltygui.core.rendering.injected_state import owned_state
        from meltygui.state.path_icon_state import PathIconState
        draw_state = kwargs['draw_state']
        state = kwargs.get('icon_state')
        if state is None:
            state = owned_state(draw_state, 'icon_state', PathIconState)
            kwargs['icon_state'] = state
        if not hasattr(state, 'requested'):
            state.requested = set()
        state.folders.consume(state.folders.requested)
        state.requested.clear()
        try:
            return func(*args, **kwargs)
        finally:
            state.folders.retain(state.requested)
            sync_icon_watches(draw_state, state, state.folders.watch_directories())
            dispatch_icon_loads(draw_state, state)
    return draw
