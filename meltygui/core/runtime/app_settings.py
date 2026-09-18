"""An app's settings: the plain dict `@glfw_window(settings=...)` was given,
kept between runs and edited in a settings window the title bar's cog opens.

    SETTINGS = {'font_size': 14, 'editor': {'tab_width': 4, 'wrap': False}}

    @glfw_window(name='Editor', settings=SETTINGS)
    def editor():
        size = SETTINGS['font_size']          # the same dict, live

The decorator's dict IS the settings: it is loaded INTO (in place, so every
reference the app holds sees the saved values before the loop starts) and
the app reads it like any dict. The dict's own values are the defaults and
the schema: a saved value lands only on a key the dict has, with the same
type (a bool never lands on an int, a str never on a nested dict), and a
nested dict is a sub-folder merged the same way — so a setting removed or
renamed in the code simply disappears from the window and the file, and a
new one shows up with its default. The file:
`$XDG_CONFIG_HOME/<app_id>/settings.json` (default `~/.config`), one per
app id, a section per window name (an app with two settings windows shares
the file). Written when the settings window changes a value and on the
loop's exit; a file that fails to parse is moved aside
(`settings.json.broken-<time>`), never overwritten in place.

The window is `draw_any(settings, glfw_window=True)`: the dict drawn by the
collection view as a child OS window of the app's, nested dicts as its
collapsible folders, opened by the title bar's cog (titlebar "settings"
control -> AppSettings.open_requested) and closed by its own chrome.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

# The settings window's content size when it opens. Change here for every app.
WINDOW_SIZE = (520, 640)
# The window's name: its OS title and the draw_any call's name.
WINDOW_NAME = 'Settings'


def settings_dir(app_id):
    """`$XDG_CONFIG_HOME/<app_id>` (default `~/.config/<app_id>`): the XDG
    home for what the user configures, unlike the state dir of the session
    (app_session.session_dir)."""
    base = os.environ.get('XDG_CONFIG_HOME') or pathlib.Path.home() / '.config'
    return pathlib.Path(base) / app_id


def settings_path(app_id):
    return settings_dir(app_id) / 'settings.json'


def merge_saved(defaults, saved):
    """Copy `saved` INTO `defaults` in place, keeping `defaults` as the
    schema: a key the defaults lack is dropped, a value whose type differs
    from the default's is ignored (an int default accepts a float and the
    reverse; bool is not a number here; a tuple default takes the list JSON
    made of it), a nested dict recurses. Returns `defaults` (the same
    object)."""
    if not isinstance(saved, dict):
        return defaults
    for key, default in defaults.items():
        if key not in saved:
            continue
        value = saved[key]
        if isinstance(default, dict):
            merge_saved(default, value)
        elif isinstance(default, tuple) and isinstance(value, list):
            defaults[key] = tuple(value)        # JSON has no tuples: a colour comes back as a list
        elif _same_kind(default, value):
            defaults[key] = value
    return defaults


def _same_kind(default, value):
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    if default is None:
        return True
    return isinstance(value, type(default))


def read_file(path):
    """The whole settings file as a dict ({} when there is none). A file
    that fails to parse is moved aside so the next save does not overwrite
    the evidence and the app still starts with its defaults."""
    try:
        with open(path, encoding='utf-8') as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        broken = path.with_name(f'{path.name}.broken-{int(time.time())}')
        print(f'meltygui: cannot read settings {path}: {error}; moved to {broken}', file=sys.stderr)
        try:
            os.replace(path, broken)
        except OSError:
            pass
        return {}
    return data if isinstance(data, dict) else {}


def write_file(path, data):
    """Write `data` as JSON through a temp file + rename, so a crash mid-write
    leaves the previous file whole. Returns the path, or None on failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f'{path.name}.tmp-{os.getpid()}')
        with open(temp, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, sort_keys=False)
            stream.write('\n')
        os.replace(temp, path)
    except (OSError, TypeError, ValueError) as error:
        print(f'meltygui: cannot write settings {path}: {error}', file=sys.stderr)
        return None
    return path


def persists_itself(values):
    """Whether `values` is a CodeDict: a class mirrored as a dict, whose
    write_to sinks keep every edit themselves (the launch overrides file for
    the user's choice, the live class for the running app). The settings file
    then has nothing to load into it or save from it. Looked up through
    sys.modules so an app with plain-dict settings never imports the model."""
    module = sys.modules.get('meltygui.model.code_dict_model')
    return module is not None and isinstance(values, module.CodeDict)


class AppSettings:
    """One window's settings: `values` is the decorator's dict (identity
    preserved), `section` the window name keying it in the file. A CodeDict
    (`persists_itself`) is drawn the same way and persists through its own
    sinks instead of the file."""

    def __init__(self, values, section, path):
        if not isinstance(values, dict):
            raise TypeError(f'@glfw_window(settings=...) takes a dict of defaults, not {type(values).__name__}')
        self.values = values
        self.section = section
        self.path = pathlib.Path(path)
        self.open_requested = False     # the cog was clicked: the window opens on the next draw
        self.load()

    def load(self):
        """Merge the file's section into the dict in place (merge_saved)."""
        if not persists_itself(self.values):
            merge_saved(self.values, read_file(self.path).get(self.section))
        return self.values

    def save(self):
        """Write the dict as the file's section, keeping the other windows'
        sections as they are on disk."""
        if persists_itself(self.values):
            from meltygui.core.runtime import launch_override
            launch_override.flush()
            return self.path
        data = read_file(self.path)
        data[self.section] = self.values
        return write_file(self.path, data)

    def request_open(self):
        """The title bar's cog: open (or raise) the settings window on the
        next frame."""
        from meltygui.core.windowing.glfw_utils import request_render
        self.open_requested = True
        request_render()

    def draw(self):
        """Every frame, after the window's body (app._searchable_body): the
        settings window's lifecycle call. A child OS window of the active
        surface that starts closed, opens on `open_requested`, closes with
        its own chrome; an edit in it saves the file."""
        from meltygui.core.rendering.render_dispatch import draw_any
        changed, _ = draw_any(self.values, name=WINDOW_NAME, glfw_window=True,
                              open_requested=self.open_requested, window_size=WINDOW_SIZE)
        self.open_requested = False
        if changed:
            self.save()
        return changed
