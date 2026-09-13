"""The chat equivalent of FileMetaCollection: account → project → chat.

No colour is assigned here: a project (a directory) wears the tint painted
on it in the shared file-meta store, a conversation the tint the user
painted on its row (``entry["tint"]``, absent until then) — the file
browser's rule, brush then picker.
"""

from src.lsd.gl_gui.model.dict_conversion import DictConversion


class ChatMetadata(DictConversion):
    def __init__(self):
        super().__init__()
        self.name = "Chat Metadata"
        self.accounts = {}

    def account(self, account_id):
        return self.accounts.setdefault(account_id, {"projects": {}})

    def project(self, account_id, project):
        projects = self.account(account_id)["projects"]
        if project not in projects:
            projects[project] = {"expanded": True, "order": len(projects), "chats": {}}
        return projects[project]

    def conversation(self, account_id, project, key):
        # Moving a conversation between projects moves its metadata entry too.
        target = self.project(account_id, project)
        if key not in target["chats"]:
            for entry in self.account(account_id)["projects"].values():
                if key in entry["chats"]:
                    target["chats"][key] = entry["chats"].pop(key)
                    break
            else:
                target["chats"][key] = {"order": len(target["chats"])}
        return target["chats"][key]

    def apply(self, account_id, chats):
        orders = {}
        for key, chat in chats.items():
            project = self.project(account_id, chat["project"])
            entry = self.conversation(account_id, chat["project"], key)
            # These are local overrides, not remotely editable chat fields.
            chat["__overrides__"] = {"tint": entry.get("tint")}
            if hasattr(chat, "applied_tint"):
                chat.applied_tint = entry.get("tint")
                chat.metadata = entry
            orders[key] = (project["order"], entry["order"])
        ordered = sorted(chats, key=lambda key: orders[key])
        if ordered != list(chats):        # re-insert only when the order moved (this runs every frame)
            for key in ordered:
                dict.__setitem__(chats, key, dict.pop(chats, key))
        if hasattr(chats, "applied_order"):
            chats.applied_order = list(chats)

    def collect(self, account_id, chats):
        positions = {}
        order_changed = list(chats) != getattr(chats, "applied_order", None)
        for key, chat in chats.items():
            project = chat["project"]
            entry = self.conversation(account_id, project, key)
            position = positions.get(project, 0)
            if order_changed:
                entry["order"] = position
            positions[project] = position + 1
            tint = chat.get("__overrides__", {}).get("tint")
            if tint is not None and tint != getattr(chat, "applied_tint", None):
                entry["tint"] = tuple(tint)
            if getattr(chat, "remote_id", None):
                entry["remote_id"] = chat.remote_id

    def local_key(self, account_id, remote_id):
        for project in self.account(account_id)["projects"].values():
            for key, entry in project["chats"].items():
                if entry.get("remote_id") == remote_id:
                    return key
        return remote_id


_app_metadata = None


def shared_metadata():
    """Resolve the persistent mirror at the model boundary, never in a view.

    In the studio it lives on the root model and is saved with the session.
    An app on melty has no root: every proxy then shares one process-wide
    instance, persisted to a JSON file when the app asked for it with
    `persistent_metadata(path)`."""
    global _app_metadata
    from src.lsd.gl_gui.melty import Melty
    root = getattr(getattr(Melty, "vis", None), "root", None)
    if root is None:
        if _app_metadata is None:
            _app_metadata = ChatMetadata()
        return _app_metadata
    if not hasattr(root, "chat_metadata"):
        root.chat_metadata = ChatMetadata()
    return root.chat_metadata


def persistent_metadata(path):
    """The app-wide ChatMetadata, loaded from ``path`` (a JSON file; missing
    or unreadable starts empty) and written back at interpreter exit. Call
    it once before the first chat proxy is created."""
    import atexit
    import json
    import os
    import pathlib
    global _app_metadata
    path = pathlib.Path(path).expanduser()
    metadata = shared_metadata()
    try:
        accounts = json.loads(path.read_text()).get("accounts", {})
    except (OSError, ValueError):
        accounts = {}
    for account in accounts.values():
        for project in account.get("projects", {}).values():
            project.pop("tint", None)          # a project's tint is the folder's, in the project-meta store
            for chat in project.get("chats", {}).values():
                if chat.get("tint") is not None:
                    chat["tint"] = tuple(chat["tint"])
                else:
                    chat.pop("tint", None)
    metadata.accounts.update(accounts)

    def save():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps({"accounts": metadata.accounts}, indent=1))
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError) as error:
            print(f"chat metadata: cannot write {path}: {error}")

    atexit.register(save)
    metadata.save = save
    return metadata
