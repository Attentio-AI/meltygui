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
TEST_FOLDER = Path("/home/lukas/test_folder")


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


def _create(path, value, pending):
    """A key the user ADDED → put it on disk. A held dict → mkdir (its children
    create themselves from the same plan); a held str → a file with those
    contents; a held Path that still exists elsewhere → a MOVE (a dragged or
    renamed key is the old Path re-appearing under a new name). A STALE held
    Path (its file already gone) pairs by basename to a pending delete — the
    two halves of a move whose delete side was planned first (e.g. undoing a
    cross-folder drag re-inserts the OLD Path while the file sits at the NEW
    one) — and moves that file instead of writing an empty husk."""
    try:
        if isinstance(value, dict):
            path.mkdir(exist_ok=True)
            return
        if isinstance(value, Path):
            if value.exists() and value != path:
                value.rename(path)
                pending.discard(value)
                return
            if not path.exists():
                # value == path covers undo: the OLD Path re-inserted at its
                # old home while the file sits at the move's target.
                twin = next((p for p in sorted(pending)
                             if p.name == value.name and p.is_file()), None)
                if twin is not None:
                    twin.rename(path)
                    pending.discard(twin)
                    return
        if not path.exists():
            path.write_text(value if isinstance(value, str) else "")
    except OSError:
        pass


def _delete(path):
    """A key the user DELETED → remove from disk (rmtree for a folder)."""
    try:
        shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
    except OSError:
        pass


def _reconcile(store, disk, folder, seen):
    """Mirror `store` (the held tree) ⇄ `disk` (the poller's snapshot of
    `folder`), in place, in two phases. Phase 1 (_collect) walks the whole
    tree gathering creates and deletes, stamping every planned effect straight
    into `disk` so the snapshot never lags our own writes — the poller only
    ever reports EXTERNAL changes. Phase 2 executes EVERY create before ANY
    delete, tree-wide: a file dragged between folders is renamed (a MOVE)
    before the old key's delete fires no matter which folder the walk visits
    first. (The old per-folder ordering destroyed the file's content whenever
    the source folder reconciled before the target.) A delete consumed by a
    move pairing is skipped; the rest run last, so a moved folder's old
    skeleton is removed only after its children have been renamed out."""
    creates, deletes = [], []
    _collect(store, disk, folder, seen, creates, deletes)
    pending = set(deletes)
    for path, value in creates:
        _create(path, value, pending)
    for path in deletes:
        if path in pending:
            _delete(path)


def _collect(store, disk, folder, seen, creates, deletes):
    """Phase 1 of _reconcile: the recursive walk. A path the store has never
    SEEN is disk's to give (new on disk → hold it); a path it HAS seen is the
    user's to take away (key deleted → plan a delete, key added → plan a
    create). Store/disk/seen bookkeeping happens here — only the disk side
    effects are deferred to the plan."""
    for name, value in list(store.items()):
        if name not in disk and (folder / name) not in seen:        # user added
            creates.append((folder / name, value))
            disk[name] = {} if isinstance(value, dict) else folder / name
            if not isinstance(value, dict):
                store[name] = disk[name]
    for name in sorted(set(disk) | set(store)):
        path = folder / name
        if name not in store:
            if path in seen:                                        # user deleted
                deletes.append(path)
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
            _collect(store[name], sub, path, seen, creates, deletes)


# ── the stateful wrapper: discover -> view_func(dict) -> apply ──────────────────
# Shaped like claude_terminals_io. It does not know about RenderHost - the host
# gives it `view_func` and holds whatever dict it passes through.
@render_func(use_cache=True, selectable=False, show_bg=False)
def folder_io(input_value, draw_state, view_func=None, root=None, external_change=False, **kwargs):
    # ── IN: reconcile the HELD tree IN PLACE against the disk snapshot - same
    # reason as claude_terminals_io: `input_value` IS the dict the host holds
    # and the @window reads; a separate store would leave it a stale copy.
    # The snapshot is PER ROOT: the single shared global handed the second
    # window the first root's tree - the the store saw every ROOT entry as
    # "new on disk", complete with Path leaves pointing into the wrong folder.
    disk = _disk_trees.get(root)
    if disk is None:
        disk = _disk_trees[root] = _scan(root)
    store = input_value if isinstance(input_value, dict) else {}
    seen = getattr(draw_state, "_seen_paths", None)
    if seen is None:
        seen = draw_state._seen_paths = set()
    _reconcile(store, disk, root, seen)

    # ── VIEW: hand the tree to the host's view func (which materializes + renders)
    edited, value = view_func(input_value=store, external_change=False, **kwargs)

    imgui.text(str(root))
    return edited, value


# ── the proxy: to the program it's just a dict shaped like the folder ───────────
files_proxy = RenderHost(io_function=folder_io, input_value=None,
                         name="Folder Files", root=ROOT)

test_folder_proxy = RenderHost(io_function=folder_io, input_value=None,
                         name="TestFolderProxy", root=TEST_FOLDER)

# ── per-root state: every root gets its own snapshot, window draw_state, and
# poller entry. (These were single globals once: the second window reconciled
# against the first root's snapshot and rendered ROOT's files.)
_disk_trees = {}        # root -> the poller's nested snapshot ({name: Path | dict})
_window_dss = {}        # root -> that root's @window draw_state, stashed each render
_proxies = {ROOT: files_proxy, TEST_FOLDER: test_folder_proxy}   # poller targets
_poller_running = False


def _draw_tree(input_value, draw_state, root):
    """Shared @window body: start the (single, all-roots) poller, stash this
    root's draw_state for it, and draw the held tree."""
    global _poller_running
    if not _poller_running:
        threading.Thread(target=_poll_loop, daemon=True, name="folder-files-poller").start()
        _poller_running = True
    _window_dss[root] = draw_state

    # The tree is held one LEVEL UP under value name ("value") - same as
    # claude_terminals: draw_collection on the proxy itself would render the
    # single {"value": tree} key, not the files.
    tree = input_value.get("value") if isinstance(input_value, dict) else {}
    RenderFuncs.draw_collection(tree if tree is not None else {}, name=root.name,
                                show_add_delete=True, new_item_type=str, temp=True, width=draw_state.content_width,
                                disable_scroll=True, show_bg=True,
                                child_kwargs={"show_bg": True, "bg_offset": -4, "child_kwargs": {"show_bg": False,
                                                                                                 "is_tree": False,
                                                                                                 "expanded": True},
                                              "mode": Modes.FILE_TREE})


# ── the renderers: draw each held tree; draw leaves edit in Mode.FILE_TREE ───
@window(input_value=files_proxy, tint=(0.11, 0.38, 0.72), disable_scroll=False, mode=Modes.WINDOW)
@render_func(show_bg=True, use_cache=True, selectable=False)
def draw_folder_files(input_value, draw_state, **kwargs):
    _draw_tree(input_value, draw_state, ROOT)
    return False, None


@window(input_value=test_folder_proxy, tint=(0.39, 0.356, 0.33), disable_scroll=False, mode=Modes.WINDOW)
@render_func(show_bg=False, use_cache=True, selectable=False)
def draw_test_folders(input_value, draw_state, **kwargs):
    _draw_tree(input_value, draw_state, TEST_FOLDER)
    return False, None


# ── discovery poller: the wrappers' bodies are blit-cached, so they wouldn't
# show a file that appeared/vanished with no way to render them. One
# thread sweeps EVERY registered root; on a change to a tree, swap in the new
# snapshot AND re-render that root's @window.
def _poll_loop():
    while True:
        if Core.melty.frame_count < 4:
            time.sleep(2)
        for root, proxy in list(_proxies.items()):
            try:
                cur = _scan(root)
                if cur != _disk_trees.get(root):
                    _disk_trees[root] = cur
                    for ds in (getattr(proxy, "_wrapper_draw_state", None),
                               getattr(proxy, "_draw_state", None),
                               _window_dss.get(root)):
                        if ds is not None:
                            ds.invalidate()
                    request_render()
            except Exception:
                pass
        time.sleep(1.0)