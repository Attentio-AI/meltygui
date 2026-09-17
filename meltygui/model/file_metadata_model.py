"""Operations over supplied per-file dictionaries; no shared-store discovery."""
from meltygui.models.file_meta import FileMeta


def set_row_tint(meta, path, value):
    """Write `value` (an rgb(a) tuple) as the tint of `path` in the shared
    file-meta store, creating the entry only now — the listing never
    setdefault()s entries for the files it merely shows. None (the picker's
    clear) removes the tint, and the entry too when it holds nothing else,
    so an unpainted file leaves no trace in file_meta.pkl."""
    key = str(path)
    entry = meta.get(key)
    if value is None:
        if isinstance(entry, dict) and dict.__contains__(entry, "tint"):
            del entry["tint"]
            if dict.__len__(entry) == 0:
                del meta[key]
        return
    if not isinstance(entry, dict):
        entry = meta[key] = FileMeta()
    entry["tint"] = tuple(value)


def ordered_rows(rows, meta):
    """`rows` ([(Path, is_dir)], natural order) sorted by the `order` stamps
    in the meta store, the studio's rule (folder_files._apply_meta):
    stamped rows first by their number, unstamped ones after in natural
    order. No stamps at all: `rows` itself."""
    if meta is None:
        return rows
    orders = {}
    for i, (path, _is_dir) in enumerate(rows):
        entry = meta.get(str(path))
        if isinstance(entry, dict):
            order = entry.get("order")
            if isinstance(order, (int, float)):
                orders[i] = order
    if not orders:
        return rows
    indexed = sorted(range(len(rows)), key=lambda i: (orders.get(i, float("inf")), i))
    return [rows[i] for i in indexed]


def set_row_order(meta, paths):
    """Stamp `order` = position into the meta entry of every path of a
    directory (created for the rows that have none — a reorder is the
    user's explicit edit of the folder, like painting it)."""
    for i, path in enumerate(paths):
        key = str(path)
        entry = meta.get(key)
        if not isinstance(entry, dict):
            entry = meta[key] = FileMeta()
        if entry.get("order") != i:
            entry["order"] = i


def apply_row_drop(rows, drag_keys, first_visible, drop):
    """The directory's new order after `drop` (a DropEvent from on_drop, or
    None): `drag_keys` are the rows that registered a drag handle this run
    — the visible ones, a contiguous slice of `rows` starting at
    `first_visible` — and the event's indices count in that slice. Returns
    the complete [Path] order to stamp, or None when nothing moved (no
    drop, a drop back in place, a cross-collection kind: rows only reorder
    here)."""
    if drop is None or drop.kind != "reorder" or not drag_keys:
        return None
    keys = list(drag_keys)
    if not drop.apply(keys):
        return None
    paths = [path for path, _is_dir in rows]
    return paths[:first_visible] + keys + paths[first_visible + len(drag_keys):]


