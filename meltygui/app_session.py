"""The persisted session of a melty app: its draw states between runs.

The studio keeps every view's draw_state in `AppModel.draw_state_registry`
and pickles the whole model through load_save_v2 at exit, so column edges,
scroll positions, selections, injected state objects (`FileExplorerState`,
`TextEditorState`, ...) and nested-window z-order come back the next boot.
A `@glfw_window` app has no AppModel; this module gives it the same thing
with nothing to add to the app: `AppSession` is the model — the three
fields Melty reads off the studio's root, plus `app_state`, the objects
the app itself keeps through `melty.persisted(name, factory)` (the code
editor's `OpenFiles`) — loaded by `app._init_melty`
BEFORE the first Surface (each surface's `Melty.vis.root` IS the session,
so `get_draw_state`, `note_window_seen` and `adopt_registered_windows` find
their stores where they look for them), and written by `app.run` on the
way out. The file: `$XDG_STATE_HOME/<app_id>/session.pkl` (default
`~/.local/state`), one per app id; two instances of one app share it and
the last to exit wins.

Same pickler, same rules as the studio: `_`-prefixed and `@no_save` fields
are dropped, GL / tensor handles stub to None, an unused draw_state counts
down `dlt_count` across saves and is pruned once it reaches 0 (the slot
loads as None; `prune` clears it). The studio's `Melty.on_load` callbacks
(reopen editor tabs, walk the src tree for file meta) are NOT fired here:
they are the studio model's lifecycle, not a view-state one.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

from src.lsd.gl_gui.model.dict_conversion import DictConversion


class AppSession(DictConversion):
    """What an app persists. The field names are the studio root's, because
    Melty reads them by name off `vis.root`."""

    def __init__(self):
        super().__init__()
        # unique (the id-stack hash, same between runs) -> DrawState
        self.draw_state_registry = {}
        # Melty.registered_windows, adopted by Melty.adopt_registered_windows:
        # the dict order is the nested-window z-order.
        self.registered_windows = {}
        # Window names in first-seen order (Melty.note_window_seen).
        self.render_windows = []
        # The app's own persisted objects (app.persisted): name -> a
        # DictConversion lets app objects through the dict it hands over
        # and keeps between runs, like a studio model field.
        self.app_state = {}
        # Global search's pick counts / last query (model.global_search_store
        # .GlobalSearchStore), created by the search view on first use when
        # the app enabled it (melty.global_search). Declared here because
        # the pickler keeps only the fields the model declares at init.
        self.global_search_store = None


def session_dir(app_id):
    """`$XDG_STATE_HOME/<app_id>` (default `~/.local/state/<app_id>`): the
    XDG home for state that should survive a restart but is not config."""
    base = os.environ.get('XDG_STATE_HOME') or pathlib.Path.home() / '.local' / 'state'
    return pathlib.Path(base) / app_id


def session_path(app_id):
    return session_dir(app_id) / 'session.pkl'


def load(app_id):
    """The app's saved session, or a fresh one. A file that fails to load is
    moved aside (`session.pkl.broken-<time>`) so the next save does not
    overwrite the evidence and the app still starts."""
    path = session_path(app_id)
    if not path.exists():
        return AppSession()
    from src.lsd.gl_gui.utils import load_save_v2
    try:
        session = load_save_v2.load(str(path), vis=None, run_on_load=True)
    except Exception as error:
        broken = path.with_name(f'{path.name}.broken-{time.strftime("%Y%m%d-%H%M%S")}')
        try:
            path.replace(broken)
        except OSError:
            broken = None
        print(f'{app_id}: session {path} failed to load ({type(error).__name__}: {error})'
              + (f'; moved to {broken}' if broken else ''), file=sys.stderr)
        return AppSession()
    if not isinstance(session, AppSession):
        print(f'{app_id}: session {path} holds a {type(session).__name__}, starting fresh',
              file=sys.stderr)
        return AppSession()
    prune(session)
    return session


def prune(session):
    """Drop registry slots that are not draw states: a draw_state whose
    countdown ran out pickles as None (see load_save_v2.persistent_id), and
    anything else is a foreign object. Returns the number removed."""
    from src.lsd.gl_gui.model.core_model.draw_state import DrawState
    registry = session.draw_state_registry
    if not isinstance(registry, dict):
        session.draw_state_registry = {}
        return 0
    dead = [key for key, draw_state in registry.items() if not isinstance(draw_state, DrawState)]
    for key in dead:
        registry.pop(key)
    for draw_state in registry.values():
        # A context menu never survives a restart: the flag is @no_save
        # now, but a session written before that carries it, and an open
        # menu drawn from frame 0 (under its spawner) captures a black
        # tile that the first real open then serves (09-13).
        if getattr(draw_state, 'context_menu_open', False):
            draw_state.context_menu_open = False
    if not isinstance(session.registered_windows, dict):
        session.registered_windows = {}
    if not isinstance(session.render_windows, list):
        session.render_windows = []
    if not isinstance(getattr(session, 'app_state', None), dict):
        session.app_state = {}
    return len(dead)


def save(session, app_id):
    """Write the session (atomic: load_save_v2.save). Returns the path, or
    None when the write failed (reported, never raised: it runs on the
    app's exit path)."""
    from src.lsd.gl_gui.utils import load_save_v2
    path = session_path(app_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        prune(session)
        load_save_v2.save(session, str(path), excluded=list(load_save_v2.STUDIO_SAVE_EXCLUDED))
    except Exception as error:
        print(f'{app_id}: session save to {path} failed ({type(error).__name__}: {error})',
              file=sys.stderr)
        return None
    return path
