"""Account model functions and supporting definitions."""
import json
import os
import stat
import threading


class AccountStore(dict):
    """id -> account dict. `load()` reads the JSON; every mutation goes
    through `set_field` / `add` / `remove` so the file stays in sync and
    the window repaints. Runtime-only keys start with '_' and are never
    written."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self.loaded = False
        self.error = None

    def load(self):
        from meltygui.accounts.internet_accounts import ACCOUNTS_PATH
        from meltygui.accounts.internet_accounts import KINDS

        with self._lock:
            self.clear()
            try:
                if ACCOUNTS_PATH.exists():
                    data = json.loads(ACCOUNTS_PATH.read_text())
                    for entry in data.get("accounts", []):
                        if isinstance(entry, dict) and entry.get("id") and entry.get("kind") in KINDS:
                            self[entry["id"]] = entry
                self.error = None
            except Exception as error:
                self.error = f"accounts.json: {error}"
            self.loaded = True
        self.ensure_kinds()
        return self

    def ensure_kinds(self):
        # default accounts so every kind has a row to act on: the id is the
        # kind name ("anthropic", "copilot", "ollama"); sessions asking for
        # account="default" resolve to it (see `account`).
        from meltygui.accounts.internet_accounts import KINDS

        for kind in KINDS.values():
            if not any(entry.get("kind") == kind.name for entry in self.values()):
                self.add(kind.name, account_id=kind.name, save=False)
            for entry in self.of_kind(kind.name):
                for field in kind.fields:
                    entry.setdefault(field.name, field.default)

    def save(self):
        from meltygui.accounts.internet_accounts import ACCOUNTS_PATH

        with self._lock:
            data = {"accounts": [{key: value for key, value in entry.items()
                                  if not key.startswith("_")}
                                 for entry in self.values()]}
            try:
                ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = ACCOUNTS_PATH.with_suffix(".json.tmp")
                with open(tmp, "w") as file:
                    file.write(json.dumps(data, indent=2))
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(tmp, ACCOUNTS_PATH)
                self.error = None
            except Exception as error:
                self.error = f"accounts.json: {error}"

    def add(self, kind_name, account_id=None, save=True, **fields):
        from meltygui.accounts.internet_accounts import KINDS
        from meltygui.accounts.internet_accounts import accounts_changed

        kind = KINDS[kind_name]
        if account_id is None:
            suffix = 2
            while f"{kind_name}-{suffix}" in self:
                suffix += 1
            account_id = f"{kind_name}-{suffix}"
        entry = {"id": account_id, "kind": kind_name,
                 "label": fields.pop("label", None) or kind.default_label(account_id)}
        for field in kind.fields:
            entry[field.name] = fields.get(field.name, field.default)
        self[account_id] = entry
        if save:
            self.save()
        accounts_changed()
        return entry

    def remove(self, account_id):
        """Drop an account. Its live sessions go first — computed while it
        is still in the store, so a removed DEFAULT row also drops the
        sessions pooled under "default" (built on its credentials; the next
        row of the kind becomes the default, see `default_account`)."""
        from meltygui.accounts.internet_accounts import KINDS
        from meltygui.accounts.internet_accounts import _drop_sessions_for
        from meltygui.accounts.internet_accounts import accounts_changed

        entry = self.get(account_id)
        if entry is None:
            return
        KINDS[entry["kind"]].close(entry)
        _drop_sessions_for(entry)
        self.pop(account_id, None)
        self.save()
        accounts_changed()

    def set_field(self, account_id, field, value, reprobe=True):
        from meltygui.accounts.internet_accounts import _drop_sessions_for
        from meltygui.accounts.internet_accounts import accounts_changed

        entry = self.get(account_id)
        if entry is None or entry.get(field) == value:
            return
        entry[field] = value
        if reprobe:
            entry["_status"] = None        # stale → re-probe
            entry.pop("_validated", None)  # credential changed → re-verify with Test
            _drop_sessions_for(entry)      # live sessions hold the old credential
        self.save()
        accounts_changed()

    def of_kind(self, kind_name):
        return sorted((entry for entry in self.values() if entry.get("kind") == kind_name),
                      key=lambda entry: (entry["id"] != kind_name, entry["id"]))

    def default_account(self, kind_name):
        """The kind's DEFAULT account — what `account(kind, "default")` and
        the providers' `account="default"` resolve to: the entry named
        after its kind, else (that one removed) the first remaining row of
        the kind. Ids never change on promotion, so `account="anthropic-2"`
        references, profile files and panel state all stay put."""
        entry = self.get(kind_name)
        if entry is not None and entry.get("kind") == kind_name:
            return entry
        rows = self.of_kind(kind_name)
        return rows[0] if rows else None

    def removable(self, account_entry) -> bool:
        """A row can go while its kind keeps at least one other — the last
        one stays (every kind always has a row to act on)."""
        return len(self.of_kind(account_entry.get("kind"))) > 1
