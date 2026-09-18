"""Conversation ordering and activity operations over supplied mappings."""
import colorsys
import hashlib
import os
import time


# Status colors of the turn badge, the banner and the token badge.
GREEN = (0.25, 0.85, 0.48)
AMBER = (1.0, 0.72, 0.25)
YELLOW = (1.0, 0.78, 0.20)
RED = (1.0, 0.30, 0.28)
BLUE = (0.45, 0.7, 1.0)
MUTED = (0.65, 0.68, 0.74)


def is_active(chat, now=None):
    """Explicit turn ownership: only backend state counts as work, never a
    recently modified conversation."""
    return bool(chat.get("running") or chat.get("external_busy")
                or getattr(chat, "turn_id", None)
                or "send" in getattr(chat, "inflight", ()))


def pending_requests(chat):
    return [request for request in chat.get("requests", {}).values()
            if "answer" not in request]


def turn_status(chat):
    """Whose turn it is: a compact badge (sidebar rows), an explanatory banner
    (under the transcript) and its color."""
    requests = pending_requests(chat)
    if requests:
        if any(request.get("kind") != "approval" for request in requests):
            return "Reply needed", "Response requested · Answer below", AMBER
        return "Approval needed", "Approval requested · Review below", AMBER
    if getattr(chat, "error", None):
        return "Error", "Turn needs attention · See error below", AMBER
    if getattr(chat, "loading", False) or not getattr(chat, "loaded", True):
        return "", "Loading conversation…", MUTED
    if is_active(chat):
        if chat.get("external_busy") and not chat.get("running"):
            return "Working", "Working in another session…", BLUE
        return "Working", "Chatbot is working…", BLUE
    if chat.get("locked"):
        return "Read only", "Conversation is read only", MUTED
    if chat.get("queued_messages") and not chat.get("queue_paused"):
        return "Queued", "Next message queued…", BLUE
    messages = chat.get("messages", {})
    if messages:
        last = next(reversed(messages.values()))
        finished = last.get("role") == "assistant" and last.get("status") == "completed"
        return "Your turn", ("Your turn · Response finished" if finished else
                             "Your turn · Ready for your message"), GREEN
    return "", "Ready · Send a message to start", MUTED


def token_badge(chat):
    """The conversation header's context usage: label, color, tooltip. The
    backends report `context_tokens` (and Codex the model's `context_window`)."""
    count = chat.get("context_tokens")
    if count is None:
        if not chat.get("messages") and getattr(chat, "loaded", True):
            count = 0
        else:
            return "Tokens —", MUTED, "Token usage has not been reported yet."
    limit = chat.get("context_window") or 200_000
    ratio = count / limit
    tint = GREEN if ratio < 0.5 else YELLOW if ratio < 0.8 else RED
    detail = (f"{count:,} context tokens of {limit:,} ({ratio:.0%})." if chat.get("context_window") else
              f"{count:,} context tokens. Color thresholds: yellow at 100,000; red at 160,000.")
    return f"{count:,} tokens", tint, detail


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


def folder_tint(project):
    """A stable color for a directory without a painted tint."""
    if not project:
        return (0.6, 0.6, 0.6)
    path = os.path.abspath(os.path.expanduser(project))
    digest = hashlib.sha256(os.fsencode(path)).digest()
    hue = int.from_bytes(digest[:4], "big") / 2**32
    return colorsys.hsv_to_rgb(hue, 0.48, 0.65)


def conversation_folder_tint(project, file_metadata=None):
    """The folder color shared by the conversation header and the recent list:
    the directory's painted tint, else its stable own."""
    return project_tint(project, file_metadata) or folder_tint(project)


def conversation_tint_setter(meta):
    """A writer for a conversation's own tint in the chat metadata: a tuple
    paints it, None (the picker's clear) unpaints it."""
    def write(value):
        if value is None:
            meta.pop("tint", None)
        else:
            meta["tint"] = tuple(value)
    return write


def chat_project(input_value):
    """The directory a chat view's new conversations start in, from the view's
    input value; None leaves it to the chat's own rules. A path gives its
    project root; any other model is the application's to read, through the
    `chat_project` extension service (meltygui_pro: an editor's open files
    give the selected tab's project)."""
    from meltygui.core.runtime import extensions
    if isinstance(input_value, (str, os.PathLike)):
        from meltygui.code.fileref import project_root_of
        root = project_root_of(input_value)
        return str(root) if root else None
    project = extensions.call("chat_project", input_value) if input_value is not None else None
    return str(project) if project else None
