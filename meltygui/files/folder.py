"""
Folder files — a directory tree rendered through a RenderHost.

`folder_io` is the plain stateful wrapper — it knows nothing about RenderHost.
It follows the same shape as `code_file_io` / `claude_terminals_io`:

    discover()  →  view_func(dict)  →  apply()              (folder_io)

except discover/apply are RECURSIVE: it reconciles a persistent nested dict
against the directory tree on disk — subfolders become nested dicts, files
become Path leaves. RenderHost wraps it so the program just sees "a dict
shaped like the folder". File CONTENT never flows through here: each Path
leaf renders via Mode.FILE_TREE → code_file_io, whose TextFileCodec owns that
file's whole-file load/edit/save round-trip — the recursion is pure structure.

The dict is mutable the way the terminals dict is: a key the user deletes
deletes its file (claude_terminals kills the tmux session, we unlink); a key
the user adds creates one (a held dict → mkdir, a held str → its contents).
A background poller re-runs discovery when the tree changes on disk (the
wrapper's body is blit-cached, so it wouldn't otherwise notice a file that
appeared/vanished with no edit to invalidate it).
"""

import shutil
import threading
import time
from pathlib import Path

import imgui

from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

ROOT = Path(__file__).parent


def _scan(folder):
    """Disk → the held shape: {name: Path} for files, {name: {…}} for dirs."""
    out = {}
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return out
    for p in children:
        if p.name.startswith(".") or p.name == "__pycache__":
            continue
        out[p.name] = _scan(p) if p.is_dir() else p
    return out


def _create(path, value):
    """A key the user ADDED → put it on disk. A held dict → mkdir (its children
    create themselves when the reconcile descends); a held str → a file with
    those contents; a held Path that still exists elsewhere → a MOVE (a renamed
    key is the old Path re-appearing under the new name)."""
    try:
        if isinstance(value, dict):
            path.mkdir(exist_ok=True)
            return {}
        if isinstance(value, Path) and value.exists() and value != path:
            value.rename(path)
        elif not path.exists():
            path.write_text(value if isinstance(value, str) else "")
    except OSError:
        pass
    return path


def _delete(path):
    """A key the user DELETED → remove from disk (rmtree for a folder)."""
    try:
        shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
    except OSError:
        pass


def _reconcile(store, disk, folder, seen):
    """Mirror `store` (the held tree) ⇄ `disk` (the poller's snapshot of
    `folder`), in place, recursively. A path the store has never SEEN is disk's
    to give (new on disk → hold it); a path it HAS seen is the user's to take
    away (key deleted → file deleted, key added → file created). Creates run
    first so a key renamed within a folder MOVES its file before the old name's
    delete fires. Every disk-side effect is stamped straight into `disk`, so
    the snapshot never lags our own writes — the poller only ever reports
    EXTERNAL changes."""
    for name, value in list(store.items()):
        if name not in disk and (folder / name) not in seen:        # user added
            disk[name] = _create(folder / name, value)
            if not isinstance(value, dict):
                store[name] = disk[name]
    for name in sorted(set(disk) | set(store)):
        path = folder / name
        if name not in store:
            if path in seen:                                        # user deleted
                _delete(path)
                disk.pop(name)
                seen.discard(path)
                continue
            store[name] = {} if isinstance(disk[name], dict) else disk[name]   # new on disk
        elif name not in disk:                                      # vanished from disk
            store.pop(name)
            seen.discard(path)
            continue
        seen.add(path)
        if isinstance(store[name], dict):
            sub = disk[name] if isinstance(disk[name], dict) else {}
            _reconcile(store[name], sub, path, seen)


# ── the stateful wrapper: discover -> view_func(dict) -> apply ──────────────────
# Shaped like claude_terminals_io. It does not know about RenderHost - the host
# gives it `view_func` and holds whatever dict it passes through.
@render_func(use_cache=True, selectable=False, show_bg=False)
def folder_io(input_value, draw_state, view_func=None, root=None, external_change=False, **kwargs):
    # ── IN: reconcile the HELD tree IN PLACE against the disk snapshot - same
    # reason as claude_terminals_io: `input_value` IS the dict the host holds
    # and the @window reads; a separate store would leave it a stale copy.
    global _disk_tree
    if _disk_tree is None:
        _disk_tree = _scan(root)
    store = input_value if isinstance(input_value, dict) else {}
    seen = getattr(draw_state, "_seen_paths", None)
    if seen is None:
        seen = draw_state._seen_paths = set()
    _reconcile(store, _disk_tree, root, seen)

    # ── VIEW: hand the tree to the host's view func (which materializes + renders)
    edited, value = view_func(input_value=store, external_change=False, **kwargs)

    imgui.text(str(root))
    return edited, value


# ── the proxy: to the program it's just a dict shaped like the folder ───────────
files_proxy = RenderHost(io_function=folder_io, input_value=None,
                         name="Folder Files", root=ROOT)


_disk_tree = None       # the poller's nested snapshot of ROOT ({name: Path | dict})
_poller_running = False
_window_ds = None       # draw_folder_files' draw_state, stashed each render


# ── the renderer: draw the held tree; user leaves edit via Mode.FILE_TREE ───────
@window(input_value=files_proxy, tint=(0.69, 0.80, 0.92), disable_scroll=False, mode=Modes.WINDOW)
@render_func(show_bg=False, use_cache=True, selectable=False)
def draw_folder_files(input_value, draw_state, **kwargs):
    global _poller_running, _window_ds
    if not _poller_running:
        threading.Thread(target=_poll_loop, daemon=True, name="folder-files-poller").start()
        _poller_running = True
    _window_ds = draw_state

    # The tree is held one LEVEL UP under value name ("value") - same as
    # claude_terminals: draw_collection on the proxy itself would render the
    # single {"value": tree} key, not the files.
    tree = input_value.get("value") if isinstance(input_value, dict) else {}
    RenderFuncs.draw_collection(tree if tree is not None else {}, name=ROOT.name,
                                show_add_delete=True, new_item_type=str, temp=True, width=draw_state.content_width, 
                                disable_scroll=True, show_bg=True,
                                child_kwargs={"show_bg": True, "bg_offset":-4, "child_kwargs":{"show_bg":False,
                                                                               "is_tree":False,
                                                                               "expanded":True}, "mode": Modes.FILE_TREE})
    return False, None


# ── discovery poller: the window's body is blit-cached, so it wouldn't see a
# file that appeared/vanished with no edit to invalidate it. Re-scan and, on a
# CHANGE to the tree, re-run io and re-render the @window.
def _poll_loop():
    global _disk_tree
    while True:
        if Core.melty.frame_count < 4:
            time.sleep(2)
        try:
            cur = _scan(ROOT)
            if cur != _disk_tree:
                _disk_tree = cur
                for ds in (getattr(files_proxy, "_wrapper_draw_state", None),
                           getattr(files_proxy, "_draw_state", None), _window_ds):
                    if ds is not None:
                        ds.invalidate()
                request_render()
        except Exception:
            pass
        time.sleep(1.0)