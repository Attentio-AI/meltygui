"""File and folder tints, read and written through `melty_meta` (a hidden `.melty` per folder).

    from meltygui.model.tint_model import tint, set_tint

    tint(path)              # (r, g, b) or (r, g, b, a), None when unpainted: a dict read after the first
    set_tint(path, colour)  # None unpaints; shows on the next frame, saved off the render thread

The one owner of tint I/O in meltygui: views, the codec layer and the editor ask here, nothing
else imports melty_meta for tints. A folder's `.melty` is loaded once and answers every file in
it (a name it does not list costs one `stat`, to tell a subfolder from an unpainted file); a
path's tint is then held. A watcher thread stats every `.melty` that was loaded, and the global
store, each `POLL_S`; when one changed (another app painted, a `git checkout`) that folder and
the tints read from it are dropped and the app repaints, as `FileMetaProxy` does for the old store. A write lands in memory at once and on
disk `SAVE_DELAY_S` later on a worker thread, so a dragged colour picker saves once; a failed
save is a notification, and the path shows what disk holds again.
"""
import atexit
import os
import threading
import time

import melty_meta

POLL_S = 0.5            # how often the watcher stats the `.melty` files read so far
SAVE_DELAY_S = 0.3      # how long a write waits for a newer colour of the same path
FAILED = (1.0, 0.45, 0.35)      # the notification tint of a failed save

_tints = globals().get('_tints', {})        # path string -> tint or None, as read or as set
_files = globals().get('_files', {})        # `.melty` path -> [its stamp when read, {path strings read from it}]
_loaded = globals().get('_loaded', {})      # folder -> (its fields, its `.melty` path), while that is unchanged
_shared = globals().get('_shared', [False]) # the global store's stamp when last checked; False: not yet
_pending = globals().get('_pending', {})    # path string -> colour waiting for the writer
_state = globals().get('_state', {'watch': None, 'timer': None, 'queued': False, 'refresh': False})
_lock = globals().get('_lock') or threading.RLock()     # the dicts above; melty_meta is safe from any thread


def tint(path):
    """The colour `path` is painted with, rgb or rgba, or None."""
    key = str(path)
    try:
        return _tints[key]
    except KeyError:
        return _read(key)


def set_tint(path, value):
    """Paint `path` with `value` (rgb or rgba); None, or alpha 0, unpaints it."""
    key = str(path)
    colour = _clean(value)
    with _lock:
        if key not in _pending and _tints.get(key, _tints) == colour:
            return                  # the picker sent what is already there
        _tints[key] = colour
        _pending[key] = value
        timer = _state['timer']
        if timer is not None:
            timer.cancel()
        timer = _state['timer'] = threading.Timer(SAVE_DELAY_S, _save)
        timer.daemon = True
        timer.start()
    _repaint()


def flush():
    """Save every pending write now (at exit; tests)."""
    with _lock:
        timer, _state['timer'] = _state['timer'], None
    if timer is not None:
        timer.cancel()
    _save()


def _clean(value):
    """A colour as the cache holds it: what a read of the saved file returns."""
    colour = melty_meta.painted(value) if value is not None else None
    return tuple(round(channel, melty_meta.DIGITS) for channel in colour) if colour else None


def _stamp(path):
    try:
        found = os.stat(path)
        return found.st_mtime_ns, found.st_size
    except OSError:
        return None


def _load(folder):
    """(A folder's fields, its `.melty` path), loaded once and watched until that changes."""
    held = _loaded.get(folder)
    if held is not None:
        return held
    file = os.path.join(folder, melty_meta.FILE)
    stamp = _stamp(file)            # before the read: a write after it shows on the next check
    if _shared[0] is False:
        _shared[0] = _stamp(melty_meta.store())
    try:
        data = melty_meta.load(folder or os.curdir)
    except (OSError, ValueError):
        data = {}
    held = data, file
    with _lock:
        _files.setdefault(file, [stamp, set()])
        _loaded[folder] = held
    _watch()
    return held


def _read(key):
    path = os.path.expanduser(key) if key[:1] == '~' else key
    parent, name = os.path.split(path)
    data, file = _load(parent)
    fields = data.get(melty_meta.FILES, {}).get(name)
    value = None
    if isinstance(fields, dict):
        value = fields.get(melty_meta.TINT)
    elif os.path.isdir(path):           # the one stat: a subfolder's tint is in its own `.melty`
        data, file = _load(path)
        value = data.get(melty_meta.TINT)
    value = melty_meta.painted(value) if value is not None else None
    with _lock:
        _files.setdefault(file, [None, set()])[1].add(key)
        if key in _pending:
            return _tints.get(key)
        _tints[key] = value
    return value


def _save():
    with _lock:
        _state['timer'] = None
        batch = dict(_pending)
    for key, value in batch.items():
        error = None
        try:
            melty_meta.set_tint(key, value)
        except (OSError, ValueError) as caught:
            error = caught
        path = os.path.expanduser(key)
        with _lock:
            _loaded.pop(path, None)         # the folder written: read it again, not what was loaded
            _loaded.pop(os.path.dirname(path), None)
            if _pending.get(key) is value:
                del _pending[key]
                _tints.pop(key, None)       # the next read takes what disk holds
        if error is not None:
            _failed(key, error)


def _failed(key, error):
    print(f'[tint] not saved: {key}: {error}')

    def show():
        from meltygui.core.diagnostics.notifications import notify
        notify(f'Tint not saved: {os.path.basename(key) or key}: {error}', tint=FAILED)

    _post(show)
    _repaint()


def _check():
    """Off the render thread: stat what was read; hand a change to the render thread."""
    with _lock:
        if _state.get('refresh'):
            return
        read = [(file, seen[0]) for file, seen in _files.items()]
        shared = _shared[0]
    if not read:
        return
    changed = [file for file, stamp in read if _stamp(file) != stamp]
    store = _stamp(melty_meta.store())
    if changed or store != shared:
        _state['refresh'] = True
        _post(lambda: _refresh(changed, store != shared, store))


def _refresh(changed, everything, store):
    """On the render thread: forget the tints read from changed files, then repaint."""
    with _lock:
        _state['refresh'] = False
        files = list(_files) if everything else changed
        for file in files:
            _loaded.pop(os.path.dirname(file), None)
            for key in _files.pop(file, (None, ()))[1]:
                if key not in _pending:
                    _tints.pop(key, None)
        _shared[0] = store
    _repaint()


def _watch():
    if _state['watch'] is not None:
        return
    try:
        import _hub_files           # iOS: the runtime says when its mirror changed, no timer
    except ImportError:
        _hub_files = None
    hook = getattr(_hub_files, 'watch', None)
    if hook is not None:
        _state['watch'] = _check
        hook(_check)
        return

    def loop():
        while True:
            time.sleep(POLL_S)
            _check()

    _state['watch'] = threading.Thread(target=loop, name='tint-watch', daemon=True)
    _state['watch'].start()


def _post(task):
    try:
        from meltygui.core.melty import Melty
        Melty.post_to_render(task)
    except Exception:
        task()


def _repaint():
    """Repaint every view once, on the render thread: a tint can show anywhere a path does."""
    with _lock:
        if _state['queued']:
            return
        _state['queued'] = True

    def deliver():
        with _lock:
            _state['queued'] = False
        try:
            from meltygui.core.melty import Melty
            if getattr(Melty, 'cache', None) is not None:
                Melty.cache.invalidate_all()
            from meltygui.core.windowing.glfw_utils import request_render
            request_render()
        except Exception:
            pass

    _post(deliver)


if not _state.get('atexit'):          # once per process, not once per hotswap
    _state['atexit'] = True
    atexit.register(lambda: flush())
