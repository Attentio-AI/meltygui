"""The chat equivalent of FileMetaCollection: account → project → chat."""
import colorsys

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
            # Stable after creation: the chosen colour is persisted, never re-derived.
            hue = (len(projects) * 0.61803398875 + 0.45) % 1.0
            projects[project] = {"tint": colorsys.hsv_to_rgb(hue, 0.55, 0.8),
                                 "expanded": True, "order": len(projects), "chats": {}}
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
                hue, saturation, value = colorsys.rgb_to_hsv(*target["tint"][:3])
                hue = (hue + len(target["chats"]) * 0.055) % 1.0
                target["chats"][key] = {"tint": colorsys.hsv_to_rgb(hue, saturation, value),
                                           "order": len(target["chats"])}
        return target["chats"][key]

    def apply(self, account_id, chats):
        orders = {}
        for key, chat in chats.items():
            project = self.project(account_id, chat["project"])
            entry = self.conversation(account_id, chat["project"], key)
            # These are local overrides, not remotely editable chat fields.
            chat["__overrides__"] = {"tint": entry["tint"]}
            if hasattr(chat, "applied_tint"):
                chat.applied_tint = entry["tint"]
                chat.metadata = entry
            orders[key] = (project["order"], entry["order"])
        for key in sorted(chats, key=lambda key: orders[key]):
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


def shared_metadata():
    """Resolve the persistent mirror at the model boundary, never in a view."""
    from src.lsd.gl_gui.melty import Melty
    root = getattr(getattr(Melty, "vis", None), "root", None)
    if root is None:
        return ChatMetadata()
    if not hasattr(root, "chat_metadata"):
        root.chat_metadata = ChatMetadata()
    return root.chat_metadata
