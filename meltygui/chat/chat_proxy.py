"""Dict-first UI contract. Reconcile once AFTER an edit, not inside pop/del.

Backends publish inbound changes on the owner thread via drain(). The UI
only mutates dicts; no RPC or remote lifecycle vocabulary crosses this boundary.
"""
import queue
import threading
import time

from src.lsd.gl_gui.chat.messages import Message, UserMessage, user_message, input_text


def writer_conflict(error):
    message = str(error or "").lower()
    return ("active writer" in message or
            ("session" in message and "already in use" in message))


def epoch_seconds(value):
    """A provider's timestamp as epoch seconds: seconds or milliseconds
    (anything past 1e11 is milliseconds), an ISO-8601 string, or 0 when
    absent / unreadable."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, str):
        try:
            from datetime import datetime, timezone
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000.0 if number > 1e11 else number


class Chat(dict):
    """``updated`` is the conversation's last activity as epoch seconds (0 =
    unknown): the provider's listing stamps it, a backend bumps it as a turn
    streams, the proxy bumps it on send. The sidebar's age filter reads it."""

    def __init__(self, value=None, remote_id=None):
        super().__init__(title="New conversation", project="", messages={}, requests={}, running=False,
                         updated=0.0)
        self.update(value or {})
        self.remote_id = remote_id
        self.applied_tint = None
        self.loaded = remote_id is None
        self.loading = False
        self.refreshing = False     # re-reading the session another client is writing
        self.inflight = set()
        self.sent = set()
        self.saved_title = self["title"]
        self.turn_id = None
        self.resumed = remote_id is None
        self.error = None


class ChatProxy(dict):
    def __init__(self, account_id, metadata=None, wake=None):
        super().__init__()
        self.session_version = 3
        self.account_id = account_id
        from src.lsd.gl_gui.chat.metadata import shared_metadata
        self.metadata = metadata if metadata is not None else shared_metadata()
        self.projects = self.metadata.account(account_id)["projects"]
        self.wake = wake or (lambda: None)
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.closed = False
        self.loading = True
        self.revision = 0
        self.error = None
        self.known = {}
        self.applied_order = None
        self.refreshed = 0.0        # when the window last asked for `refresh`
        self._applied_revision = -1
        self.worker = threading.Thread(target=self._work, daemon=True, name="chat-backend")
        self.worker.start()

    def __setitem__(self, key, value):
        chat = value if isinstance(value, Chat) else Chat(value)
        chat.metadata = self.metadata.conversation(self.account_id, chat["project"], key)
        super().__setitem__(key, chat)

    def __getitem__(self, key):
        chat = super().__getitem__(key)
        self.active_key = key
        self._load_history(key, chat)
        return chat

    def _load_history(self, key, chat):
        operation = getattr(chat, "pending_history", None)
        if ((not chat.loaded or operation) and not chat.loading and not chat.refreshing
                and not chat["running"] and "send" not in chat.inflight):
            chat.loading = not chat.loaded
            chat.refreshing = chat.loaded
            chat.pending_history = None
            self.submit(operation or "hydrate", key)

    def refresh_history(self, key, operation="hydrate"):
        """Keep inactive transcripts cached; refresh them when displayed again."""
        chat = dict.get(self, key)
        if chat is not None and chat.loaded:
            chat.pending_history = operation
            if getattr(self, "active_key", None) == key:
                self._load_history(key, chat)

    def publish(self, kind, value):
        if not self.closed:
            needs_wake = self.events.empty()
            self.events.put((kind, value))
            if needs_wake:
                self.wake()

    def submit(self, operation, *args):
        if not self.closed:
            self.jobs.put((operation, args))

    def _work(self):
        try:
            self.connect()
            while not self.closed:
                try:
                    job = self.jobs.get(timeout=5.0)
                except queue.Empty:
                    self.refresh()
                    continue
                if job is None:
                    break
                operation, args = job
                try:
                    self.execute(operation, *args)
                except Exception as error:
                    self.publish("failure", (operation, args, str(error)))
        except Exception as error:
            self.publish("failure", ("connect", (), str(error)))
        finally:
            self.disconnect()

    def drain(self):
        changed = False
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            self.receive(kind, value)
            changed = True
        if changed:
            self.revision += 1
        return changed

    def reconcile(self):
        """A pop+insert is order-only. A missing key at this boundary is removal."""
        # Normalize dict.update/setdefault insertions before mirroring them.
        for key, value in list(self.items()):
            if not isinstance(value, Chat):
                self[key] = value
        # The metadata passes walk every conversation: only if the set,
        # the order, an event batch or a tint override changed since last time.
        keys = list(self)
        if (keys != self.applied_order or self.revision != self._applied_revision or self._tints_changed()):
            self.metadata.collect(self.account_id, self)
            self.metadata.apply(self.account_id, self)
            self._applied_revision = self.revision
        for key, chat in list(self.known.items()):
            if key not in self and "archive" not in chat.inflight and "create" not in chat.inflight:
                chat.inflight.add("archive")
                if chat.remote_id:
                    self.submit("archive", key, chat.remote_id)
                else:
                    self.known.pop(key, None)
        for key, value in list(self.items()):
            if not isinstance(value, Chat):
                self[key] = value
            chat = dict.__getitem__(self, key)
            self.known[key] = chat
            if chat.error:
                continue
            if not chat.remote_id:
                if "create" not in chat.inflight:
                    chat.inflight.add("create")
                    chat["updated"] = chat["updated"] or time.time()
                    self.submit("create", key, chat["project"], chat["title"])
                continue
            if chat["title"] != chat.saved_title and "rename" not in chat.inflight:
                chat.inflight.add("rename")
                self.submit("rename", key, chat.remote_id, chat["title"])
            if not chat["running"] and "send" in chat.inflight:
                chat.inflight.add("interrupt_requested")
            if not chat["running"] and chat.turn_id and "interrupt" not in chat.inflight:
                chat.inflight.add("interrupt")
                self.submit("interrupt", key, chat.remote_id, chat.turn_id)
            if (chat.loaded and not chat["running"] and not chat.turn_id and "send" not in chat.inflight
                    and len(chat["messages"]) != getattr(chat, "scanned", -1)):
                # Only a chat that grew since the last scan can hold an
                # unsent prompt (walking every message in every open chat each
                # frame was the proxy's main cost).
                chat.scanned = len(chat["messages"])
                for message_id, message in chat["messages"].items():
                    # Adopt dict-inserted prompts from UI callers at the model boundary.
                    if not isinstance(message, Message) and message.get("role") == "user":
                        message = chat["messages"][message_id] = (UserMessage("userMessage", content=message["content"])
                            if "content" in message else user_message(message.get("text", "")))
                    if message_id not in chat.sent and message.get("role") == "user":
                        chat.inflight.add("send")
                        chat["running"] = True
                        chat["updated"] = time.time()
                        chat["last_user_at"] = chat["updated"]
                        self.submit("send", key, chat.remote_id, message_id, input_text(message), chat.resumed)
                        break
            for request_id, request in chat["requests"].items():
                if "answer" in request and not request.get("submitted"):
                    request["submitted"] = True
                    self.submit("answer", key, request_id, request["answer"])

    def _tints_changed(self):
        """A tint edited on a chat's metadata entry (the sidebar's chip) or
        pushed through its __overrides__ since the last apply."""
        for chat in self.values():
            applied = getattr(chat, "applied_tint", None)
            if chat.get("__overrides__", {}).get("tint") != applied:
                return True
            entry = getattr(chat, "metadata", None)
            if entry is not None and entry.get("tint") != applied:
                return True
        return False

    def fork(self, key):
        import uuid
        source = self[key]
        if not source.remote_id:
            return None
        new_key = str(uuid.uuid4())
        self[new_key] = {"title": source["title"] + " (fork)", "project": source["project"],
                         "created_at": time.time(), "updated": time.time()}
        chat = dict.__getitem__(self, new_key)
        chat.loaded, chat.loading = False, True
        chat.inflight.add("create")
        self.known[new_key] = chat
        for field in ("model", "permissions", "effort", "model_explicit", "model_selected_at", "permissions_selected_at"):
            if field in source.metadata:
                chat.metadata[field] = source.metadata[field]
        self.submit("fork", new_key, source.remote_id, source["project"], chat["title"])
        return new_key

    def finish_fork(self, key, remote_id):
        chat = self.known.get(key)
        if chat is not None:
            chat.remote_id = remote_id
            chat.metadata["remote_id"] = remote_id
            chat.inflight.discard("create")
            chat.loading = False
            chat.fresh = False
            chat.resumed = False

    def refresh(self):
        """Look for activity outside this window — a session another client
        (a terminal) is writing — and publish it: a chat's ``updated`` moves,
        a new session is listed. The window asks every few seconds. Default:
        nothing to look at."""

    def close(self):
        self.closed = True
        self.jobs.put(None)
        # Transport closes on the worker after its bounded in-flight call.

    def connect(self):
        raise NotImplementedError

    def execute(self, operation, *args):
        raise NotImplementedError

    def receive(self, kind, value):
        raise NotImplementedError

    def disconnect(self):
        pass