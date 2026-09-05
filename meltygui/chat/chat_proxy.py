"""Dict-first UI contract. Reconcile once AFTER an edit, not inside pop/del.

Backends publish inbound changes on the owner thread via drain(). The UI
only mutates dicts; no RPC or remote lifecycle vocabulary crosses this boundary.
"""
import queue
import threading

from src.lsd.gl_gui.chat.messages import Message, UserMessage, user_message, input_text


class Chat(dict):
    def __init__(self, value=None, remote_id=None):
        super().__init__(title="New conversation", project="", messages={}, requests={}, running=False)
        self.update(value or {})
        self.remote_id = remote_id
        self.applied_tint = None
        self.loaded = remote_id is None
        self.loading = False
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
        self.worker = threading.Thread(target=self._work, daemon=True, name="chat-backend")
        self.worker.start()

    def __setitem__(self, key, value):
        chat = value if isinstance(value, Chat) else Chat(value)
        chat.metadata = self.metadata.conversation(self.account_id, chat["project"], key)
        super().__setitem__(key, chat)

    def __getitem__(self, key):
        chat = super().__getitem__(key)
        if not chat.loaded and not chat.loading:
            chat.loading = True
            self.submit("hydrate", key)
        return chat

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
                job = self.jobs.get()
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
        self.metadata.collect(self.account_id, self)
        self.metadata.apply(self.account_id, self)
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
            if chat.loaded and not chat["running"] and not chat.turn_id and "send" not in chat.inflight:
                for message_id, message in chat["messages"].items():
                    # Adopt dict-inserted prompts from UI callers at the model boundary.
                    if not isinstance(message, Message) and message.get("role") == "user":
                        message = chat["messages"][message_id] = (UserMessage("userMessage", content=message["content"])
                            if "content" in message else user_message(message.get("text", "")))
                    if message_id not in chat.sent and message.get("role") == "user":
                        chat.inflight.add("send")
                        chat["running"] = True
                        self.submit("send", key, chat.remote_id, message_id, input_text(message), chat.resumed)
                        break
            for request_id, request in chat["requests"].items():
                if "answer" in request and not request.get("submitted"):
                    request["submitted"] = True
                    self.submit("answer", key, request_id, request["answer"])

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