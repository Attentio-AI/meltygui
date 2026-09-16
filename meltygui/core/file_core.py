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

import sys
import threading

from meltygui.lifecycle import module_is_live
import time
from pathlib import Path

import meltygui_imgui as imgui
from meltygui.melty import Melty
from meltygui.modes import Modes
from meltygui.view.file_view import draw_file_tree
from meltygui.view.file_view import draw_file_metadata
from meltygui.utils.glfw_utils import request_render
from meltygui.code.render_host import RenderHost
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import Core

from meltygui.model.file_model import _scan
from meltygui.model.file_model import _create
from meltygui.model.file_model import _delete
from meltygui.model.file_model import _reconcile
from meltygui.model.file_model import _collect
from meltygui.model.file_model import _file_meta
from meltygui.model.file_model import _apply_meta
from meltygui.model.file_model import _collect_meta

# Keep the original default folder when the implementation moves packages.
ROOT = Path(__file__).resolve().parents[1] / "files"
from meltygui.paths import application_root
TEST_FOLDER = application_root()


# ── persisted per-file metadata (tint / expanded / order ...) ─────────────────────
# The tree holds display params the framework ignores: each file dict holds
# __overrides__['__<name>__'] = {param: value}, which core_render feeds into the
# child row's view (same mechanism as '# [tint=...]' comment overrides). The
# durable copy lives in AppModel.file_meta_collection.file_meta, keyed by
# absolute path string, so it persists with the next save. folder_io syncs both
# ways every run: APPLY meta → tree __overrides__ + key order before render,
# COLLECT tree → meta after render (UI edits land in __overrides__ via
# _LazyOverrideEntry / bubbling, drag reorders land as tree key order).


# ── startup: materialize the metadata tree for the whole module root ────────────
# Same lifecycle moment as the open-files restore (Melty.on_load): once the
# app model exists, every file and folder under the module root gets its
# entry in file_meta, so the debug view shows the full tree and attributes
# can attach anywhere without waiting for a first write. setdefault-only -
# existing values (tint, order) are never touched.
from meltygui.model.file_model import initialize_file_metadata as _init_file_meta

# ── debug window: the persisted per-file metadata tree ──────────────────────────
def file_meta_debug(_, draw_state=None):
    """Raw view of AppModel.file_meta_collection.file_meta — the path-keyed
    params store the folder tree and the codec layer read/write (tint, order,
    …). Rendered as a plain editable dict: edits land directly in the store
    (bubble-free plain dicts, so a manual touch persists on the next root
    save; deleting an entry clears that file's attributes)."""
    meta = _file_meta()
    if meta is None:
        imgui.text("No app model loaded")
        return
    if not meta:
        imgui.text("No file metadata yet")
        return
    return draw_file_metadata(meta)


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

    # ── META IN: persisted per-file params → the tree's __overrides__ + key
    # order. On a change (first load, or the store edited elsewhere) the cached
    # rows below still hold the old capture - invalidate this subtree so they
    # repaint with the fresh kwargs.
    meta = _file_meta()
    if meta is not None and _apply_meta(store, root, meta):
        tid = getattr(draw_state, "_tile_id", None)
        if tid is not None and Melty.cache is not None:
            Melty.cache.invalidate_up(tid, force=True, max_depth=8)
        request_render()

    # ── VIEW: hand the tree to the host's view func (which materializes + renders)
    edited, value = view_func(input_value=store, external_change=False, **kwargs)

    # ── META OUT: UI edits landed in __overrides__ (bubbling re-ran this body);
    # drag reorders changed key order. Mirror both into the persisted store.
    if meta is not None:
        _collect_meta(store, root, meta)

    imgui.text(str(root))
    return edited, value


# ── the proxy: to the program it's just a dict shaped like the folder ───────────
_previous_module = sys.modules.get("meltygui.files.folder_files")
_previous_state = vars(_previous_module) if _previous_module is not None else {}

files_proxy = (_previous_state["files_proxy"] if "files_proxy" in _previous_state
               else RenderHost(io_function=folder_io, input_value=None,
                               name="Folder Files", root=ROOT))

test_folder_proxy = (_previous_state["test_folder_proxy"] if "test_folder_proxy" in _previous_state
                     else RenderHost(io_function=folder_io, input_value=None,
                                     name="TestFolderProxy", root=TEST_FOLDER))

# ── per-root state: every root gets its own snapshot, window draw_state, and
# poller entry. (These were single globals once: the second window reconciled
# against the first root's snapshot and rendered ROOT's files.)
_disk_trees = _previous_state.get("_disk_trees", {})        # root -> the poller's nested snapshot ({name: Path | dict})
_window_dss = _previous_state.get("_window_dss", {})        # root -> that root's @window draw_state, stashed each render
_proxies = _previous_state.get("_proxies", {ROOT: files_proxy, TEST_FOLDER: test_folder_proxy})   # poller targets
_poller_running = _previous_state.get("_poller_running", False)


def folder_proxy(root, name):
    """A RenderHost over folder_io for `root`, registered with the poller —
    the reusable entry point for other views (e.g. playground.file_tree)."""
    proxy = _proxies.get(root)
    if proxy is None:
        proxy = _proxies[root] = RenderHost(io_function=folder_io, input_value=None,
                                            name=name, root=root)
    return proxy


def watch_folder(root, draw_state):
    """Per-frame from a folder window's body: start the (single, all-roots)
    poller and stash this root's draw_state so a disk change re-renders it."""
    global _poller_running
    if not _poller_running:
        threading.Thread(target=_poll_loop, daemon=True, name="folder-files-poller").start()
        _poller_running = True
    _window_dss[root] = draw_state


def _draw_tree(input_value, draw_state, root):
    """Shared @window body: watch the root and draw the held tree."""
    watch_folder(root, draw_state)

    # The tree is held one LEVEL UP under value name ("value") - same as
    # claude_terminals: draw_collection on the proxy itself would render the
    # single {"value": tree} key, not the files.
    tree = input_value.get("value") if isinstance(input_value, dict) else {}
    return draw_file_tree(tree if tree is not None else {}, root=root)


# ── discovery poller: the wrappers' bodies are blit-cached, so they wouldn't
# show a file that appeared/vanished with no way to render them. One
# thread sweeps EVERY registered root; on a change to a tree, swap in the new
# snapshot AND re-render that root's @window.
def _poll_loop():
    while module_is_live(globals()):   # exits once script restart purges this module
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
