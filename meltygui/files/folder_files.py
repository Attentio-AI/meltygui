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

from meltygui.lifecycle import module_is_live
import time
from pathlib import Path

import meltygui_imgui as imgui
from meltygui.melty import Melty
from meltygui.modes import Modes
from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.utils.glfw_utils import request_render
from meltygui.code.bubbling import install_bubbling
from meltygui.code.render_host import RenderHost
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import Core
from meltygui.rendering.decorators.window_decoration import window

ROOT = Path(__file__).parent
from meltygui.paths import application_root
TEST_FOLDER = application_root()


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
    """A key the user DELETED → remove from disk (rmtree for a folder).
    Toggles.FileSafety.block_file_delete gates ALL disk deletes (read live);
    the poller re-discovers the surviving file and restores its key."""
    from meltygui.toggles import Toggles
    if Toggles.FileSafety.block_file_delete:
        print(f"[folder_files] delete blocked (Toggles.FileSafety.block_file_delete): {path}")
        return
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
        if name == "__overrides__":                                 # view metadata, not a file
            continue
        if name not in disk and (folder / name) not in seen:        # user added
            creates.append((folder / name, value))
            disk[name] = {} if isinstance(value, dict) else folder / name
            if not isinstance(value, dict):
                store[name] = disk[name]
    for name in sorted(set(disk) | set(store)):
        if name == "__overrides__":
            continue
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


# ── persisted per-file metadata (tint / expanded / order ...) ─────────────────────
# The tree holds display params the framework ignores: each file dict holds
# __overrides__['__<name>__'] = {param: value}, which core_render feeds into the
# child row's view (same mechanism as '# [tint=...]' comment overrides). The
# durable copy lives in AppModel.file_meta_collection.file_meta, keyed by
# absolute path string, so it persists with the next save. folder_io syncs both
# ways every run: APPLY meta → tree __overrides__ + key order before render,
# COLLECT tree → meta after render (UI edits land in __overrides__ via
# _LazyOverrideEntry / bubbling, drag reorders land as tree key order).

def _file_meta(root=None):
    """The shared path→params store (file_meta.file_meta_store()) — what
    AppModel.file_meta_collection.file_meta is too. `root` is accepted for
    the on_load callers and ignored: the store exists before any model."""
    from meltygui.models.file_meta import file_meta_store
    return file_meta_store()


def _apply_meta(tree, folder, meta):
    """Meta → tree, recursively: stamp __overrides__ entries and sort keys by
    stored `order`. All writes are inbound state, not user edits — dunder-key
    stores are raw (no dirty mark) and reorders use raw dict ops — so applying
    never dirties the host or triggers a save. Returns True if anything
    changed (caller invalidates the subtree so cached rows repaint)."""
    changed = False
    names = [n for n in tree if n != "__overrides__"]
    desired, orders = {}, {}
    for n in names:
        entry = meta.get(str(folder / n))
        if not isinstance(entry, dict):
            continue
        params = {k: v for k, v in entry.items()
                  if k not in ("order", "project", "environment") and not (isinstance(k, str) and k.startswith("__"))
                  # unpainted (alpha-0) tint: no override, the row keeps its own
                  and not (k == "tint" and isinstance(v, (tuple, list))
                           and len(v) >= 4 and not v[3])}
        if params:
            desired[f"__{n}__"] = params
        if isinstance(entry.get("order"), (int, float)):
            orders[n] = entry["order"]
    current = tree.get("__overrides__")
    if desired:
        if current != desired:
            # Wrap entries in the host's bubbling for the raw dunder store,
            # or later UI edits to an existing entry would show but not save
            # (see _LazyOverrideEntry's docstring).
            broot = getattr(tree, "_bubble_root", None)
            if broot is not None:
                desired = install_bubbling(desired, broot)
            tree["__overrides__"] = desired
            changed = True
    elif isinstance(current, dict) and current:
        dict.pop(tree, "__overrides__", None)
        changed = True
    if orders:
        want = sorted(names, key=lambda n: (orders.get(n, float("inf")), n))
        if names != want:
            for n in want:
                dict.__setitem__(tree, n, dict.pop(tree, n))
            changed = True
    for n in names:
        child = tree.get(n)
        if isinstance(child, dict):
            changed |= _apply_meta(child, folder / n, meta)
    return changed


def _collect_meta(tree, folder, meta):
    """Tree → meta, recursively: read each child's __overrides__ params back
    into the persisted store, and capture drag reordering as `order` stamps.
    Order is stamped only once a folder's key order diverges from the natural
    sorted order (or was stamped before) — an untouched folder saves nothing."""
    names = [n for n in tree if n != "__overrides__"]
    ovs = tree.get("__overrides__")
    ovs = ovs if isinstance(ovs, dict) else {}
    stamp_order = (names != sorted(names)
                   or any(isinstance(meta.get(str(folder / n)), dict)
                          and "order" in meta[str(folder / n)] for n in names))
    for i, n in enumerate(names):
        path = str(folder / n)
        entry_src = ovs.get(f"__{n}__")
        entry = {k: v for k, v in entry_src.items()
                 if not (isinstance(k, str) and k.startswith("__"))} \
            if isinstance(entry_src, dict) else {}
        old = meta.get(path)
        if stamp_order:
            entry["order"] = i
        elif isinstance(old, dict) and "order" in old:
            entry["order"] = old["order"]
        if entry:
            if old != entry:
                from meltygui.models.file_meta import FileMeta
                meta[path] = FileMeta(entry)
        elif old is not None:
            meta.pop(path, None)
        child = tree.get(n)
        if isinstance(child, dict):
            _collect_meta(child, folder / n, meta)


# ── startup: materialize the metadata tree for the whole module root ────────────
# Same lifecycle moment as the open-files restore (Melty.on_load): once the
# app model exists, every file and folder under the module root gets its
# entry in file_meta, so the debug view shows the full tree and attributes
# can attach anywhere without waiting for a first write. setdefault-only -
# existing values (tint, order) are never touched.
_META_SKIP_SUFFIXES = {".pyc"}


@Melty.on_load
def _init_file_meta(vis, root):
    from meltygui.models.file_meta import FileMeta
    from meltygui.toggles import Toggles
    meta = _file_meta(root)
    if meta is None:
        return
    # Upgrade entries deserialized as plain dicts (older saves / from_dict)
    # to FileMeta, keeping their stored values.
    # Retroactive (09-02): stored tints that were never a user's pick - the
    # old bluish-grey class default, the tab tint's fallback colour (which got
    # written back onto 184 files), or an alpha-0 default — are dropped, so
    # those files read as unpainted (FileMeta.tint, black background).
    unpainted = {tuple(round(c, 3) for c in FileMeta._LEGACY_DEFAULT_TINT[:3]),
                 tuple(round(c, 3) for c in Toggles.CodeEditor.tab_tint_fallback[:3])}
    for key, entry in list(meta.items()):
        if isinstance(entry, dict) and not isinstance(entry, FileMeta):
            meta[key] = FileMeta(entry)
        stored = dict.get(meta[key], "tint") if isinstance(meta[key], dict) else None
        if stored is None:
            continue
        if (FileMeta.painted_tint({"tint": stored}) is None
                or tuple(round(c, 3) for c in stored[:3]) in unpainted):
            dict.pop(meta[key], "tint", None)
            meta.touch(key)      # raw dict op: tell the shared store
    from meltygui.paths import PACKAGE_ROOT
    module_root = PACKAGE_ROOT   # .../src
    for p in module_root.rglob("*"):
        rel = p.relative_to(module_root).parts
        if any(part == "__pycache__" or part.startswith(".") for part in rel):
            continue
        if p.suffix in _META_SKIP_SUFFIXES:
            continue
        meta.setdefault(str(p), FileMeta())


# ── debug window: the persisted per-file metadata tree ──────────────────────────
@window(disable_scroll=False, use_cache=True, tint=(0.18, 0.11, 0.11))
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
    RenderFuncs.draw_collection(meta, name="file_meta", is_tree=True,
                                show_add_delete=True)


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
    RenderFuncs.draw_collection(tree if tree is not None else {}, name=root.name,
                                show_add_delete=True, new_item_type=str, temp=True, width=draw_state.content_width,
                                disable_scroll=True, show_bg=True,
                                child_kwargs={"show_bg": True, "bg_offset": -4, "child_kwargs": {"show_bg": False,
                                                                                                 "is_tree": False,
                                                                                                 "expanded": True},
                                              "mode": Modes.FILE_TREE})


# ── the renderers: draw each held tree; draw leaves edit in Mode.FILE_TREE ───
@window(input_value=files_proxy, tint=(0.36, 0.46, 0.59), disable_scroll=False, mode=Modes.WINDOW)
@render_func(show_bg=True, use_cache=True, shadow=True, selectable=False)
def draw_folder_files(input_value, draw_state, **kwargs):
    _draw_tree(input_value, draw_state, ROOT)
    return False, None


@window(input_value=test_folder_proxy, tint=(0.84, 0.933, 0.98), bg_offset=4, disable_scroll=False, mode=Modes.WINDOW)
@render_func(show_bg=False, use_cache=True, selectable=False)
def draw_test_folders(input_value, draw_state, **kwargs):
    _draw_tree(input_value, draw_state, TEST_FOLDER)
    return False, None


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