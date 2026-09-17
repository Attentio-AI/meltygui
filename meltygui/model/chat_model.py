"""Conversation ordering and activity operations over supplied mappings."""
import time


def is_active(chat, now=None, active_window=30):
    return bool(chat["running"] or chat.get("external_busy")) or ((now if now is not None else time.time())
                                     - (chat.get("updated") or 0.0)) < active_window


def age_cutoff(hours, now=None):
    """The epoch second before which a conversation is out of the filter,
    or None for no filter."""
    return None if not hours else (now if now is not None else time.time()) - hours * 3600


def recent_chat_time(chat):
    # Unsent drafts have no user message yet. The fixed creation time keeps
    # them visible at the top without allowing response activity to reorder them.
    return chat.get("last_user_at") or chat.get("created_at") or 0.0


def sidebar_visible(key, chat, cutoff, selected=None):
    """Whether a conversation stays in the filtered sidebar: recent enough,
    running (active now), or the open one (its transcript is showing)."""
    return (cutoff is None or is_active(chat) or key == selected
            or (chat.get("updated") or 0.0) >= cutoff)


def _reorder(chats, key, direction):
    keys = list(chats)
    index = keys.index(key)
    # Move within this project's group; keep other projects untouched.
    project = dict.__getitem__(chats, key)["project"]
    candidates = [other for other in keys if dict.__getitem__(chats, other)["project"] == project]
    position = candidates.index(key) + direction
    if not 0 <= position < len(candidates):
        return
    other_index = keys.index(candidates[position])
    keys[index], keys[other_index] = keys[other_index], keys[index]
    for item in keys:
        chats[item] = chats.pop(item)


def _apply_chat_drop(chats, keys, drop):
    """Dock drop indices refer to registered rows, including offscreen rows."""
    if drop is None or drop.kind != "reorder" or drop.key not in keys:
        return False
    project = chats.get(drop.key)["project"]
    group = [i for i, key in enumerate(keys) if chats.get(key)["project"] == project]
    if not min(group) <= drop.insert_index <= max(group) + 1:
        return False
    rows = {key: chats.get(key) for key in keys}
    if not drop.apply(rows):
        return False
    # Collapsed projects retain their order. Mutations are dict-only;
    # the proxy records order without archiving deleted-and-reinserted rows.
    reordered = iter(rows)
    order = [next(reordered) if key in rows else key for key in chats]
    for key in order:
        chats[key] = chats.pop(key)
    return True


def project_tint(project, file_metadata):
    from meltygui.models.file_meta import FileMeta
    """The tint painted on the project's directory in the shared file-meta
    store (what the file browser and the studio paint), or None."""
    if not project:
        return None
    return FileMeta.painted_tint(file_metadata.get(str(project)) if file_metadata is not None else None)


def conversation_folder_tint(project, file_metadata):
    """Header folder color; apps may supply a color for unpainted folders."""
    return project_tint(project, file_metadata)


def conversation_tint_setter(meta):
    """A writer for a conversation's own tint in the chat metadata: a tuple
    paints it, None (the picker's clear) unpaints it."""
    def write(value):
        if value is None:
            meta.pop("tint", None)
        else:
            meta["tint"] = tuple(value)
    return write
