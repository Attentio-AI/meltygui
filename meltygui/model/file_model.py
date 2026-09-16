"""File-tree I/O and metadata adaptation for mutable dictionary values.

These operations do not own windows, pollers, or render state. The core host
coordinates when disk snapshots and edits pass through them.
"""
import shutil
from pathlib import Path

from meltygui.code.bubbling import install_bubbling

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


def initialize_file_metadata(vis, root):
    skip_suffixes = {".pyc"}
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
        if p.suffix in skip_suffixes:
            continue
        meta.setdefault(str(p), FileMeta())

