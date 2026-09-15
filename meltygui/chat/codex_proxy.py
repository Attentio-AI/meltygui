"""Codex implements the chat mapping. All protocol interpretation lives here."""
import time
from pathlib import Path

from meltygui.chat.codex_settings import ThreadSettingsReader
from meltygui.chat.codex_settings import effective_settings
from meltygui.chat.codex_settings import fast_service_tier
from meltygui.chat.codex_settings import model_service_tiers

from meltygui.chat.chat_proxy import Chat
from meltygui.chat.chat_proxy import ChatProxy
from meltygui.chat.chat_proxy import epoch_seconds
from meltygui.chat.messages import from_codex
from meltygui.chat.messages import upsert
from meltygui.chat.messages import set_text
from meltygui.chat.messages import AssistantMessage
from meltygui.chat.messages import PlanMessage
from meltygui.chat.messages import CommandExecution
from meltygui.chat.messages import ToolOutput
from meltygui.chat.messages import ReasoningMessage
from meltygui.chat.messages import FileChange
from meltygui.chat.messages import McpToolCall
from meltygui.chat.messages import FileTags
from meltygui.chat.messages import diff_counts
from meltygui.chat.messages import match_file
from meltygui.chat.codex_transport import CodexTransport
from meltygui.chat.writer_locks import codex_writer_locks
from meltygui.chat.writer_locks import lock_message
from meltygui.completion.providers.codex_accounts import account_home


class CodexChats(ChatProxy):
    inherits_defaults = True

    def __init__(self, account_id, metadata=None, wake=None, transport_factory=None):
        from meltygui.chat.activity import UserMessageTimes
        self.user_times = UserMessageTimes("codex")
        self.settings_reader = ThreadSettingsReader()
        self.source_home = account_home(account_id)
        self.transport = None
        self.writers = {}
        self.refresh_pending = False
        self.config_defaults = {}
        self.config_pending = set()
        self.transport_factory = transport_factory or CodexTransport
        super().__init__(account_id, metadata, wake)

    def connect(self):
        self.transport = self._open_transport()
        account = self.transport.request("account/read", {"refreshToken": False}).get("account")
        if not account:
            raise RuntimeError("Sign in to this account in Internet Accounts first")
        cursor = None
        while not self.closed:
            result = self.transport.request("thread/list", {
                "limit": 100, "cursor": cursor, "sortKey": "updated_at",
                "sourceKinds": ["appServer", "cli", "vscode", "exec"]})
            self.publish("listing", self._listing_sizes(result.get("data", [])))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        models = []
        cursor = None
        try:
            while True:
                result = self.transport.request("model/list", {"limit": 100, "cursor": cursor})
                models.extend(result.get("data", []))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        except Exception:
            pass  # Default model remains usable with old servers.
        self.publish("models", models)
        self._read_defaults(self.transport, "")
        self.publish("writer_locks", codex_writer_locks(self.source_home))
        self.publish("ready", None)

    def defaults_for(self, project):
        """Queue project-aware config reads without blocking the render thread."""
        # Existing backend instances survive source hotswaps.
        self.__dict__.setdefault("config_defaults", {})
        self.__dict__.setdefault("config_pending", set())
        if project not in self.config_defaults and project not in self.config_pending:
            self.config_pending.add(project)
            self.submit("defaults", project)
        return self.config_defaults.get(project, self.config_defaults.get("", {}))

    def _read_defaults(self, server, project):
        config = server.request("config/read", {
            "includeLayers": False, **({"cwd": project} if project else {})}).get("config", {})
        self.publish("defaults", (project, config))
        return config

    def _thread_options(self, server, key, project):
        config = self._read_defaults(server, project)
        settings = self.known[key].metadata
        effective = effective_settings(self.known[key], config, self.default_model)
        options = {"cwd": project}
        # Older Melty versions stamped catalog defaults into settings without
        # an explicit-selection marker. Those should not override config.toml.
        model = effective.get("model")
        if model:
            options["model"] = model
        permissions = settings.get("permissions")
        if permissions in ("ask", "full"):
            options.update(approvalPolicy="never" if permissions == "full" else "on-request",
                           sandbox="danger-full-access" if permissions == "full" else "workspace-write")
        else:
            if config.get("approval_policy") is not None:
                options["approvalPolicy"] = config["approval_policy"]
            if config.get("sandbox_mode") is not None:
                options["sandbox"] = config["sandbox_mode"]
        effort = effective.get("effort")
        supported = getattr(self, "model_efforts", {}).get(model)
        if supported is not None and effort not in supported:
            effort = None
        if effort in (None, "", "default"):
            effort = config.get("model_reasoning_effort")
        if effort:
            options["config"] = {"model_reasoning_effort": effort}
        if "service_tier" in effective:
            tier = effective["service_tier"]
            tiers = getattr(self, "model_service_tiers", {}).get(model)
            if tier in ("fast", "priority"):
                tier = fast_service_tier(self, model) or tier
            if tier in ("default", "") or tiers is not None and tier not in {entry["id"] for entry in tiers}:
                tier = None
            options["serviceTier"] = tier
        return options

    def _listing_sizes(self, threads):
        # Filesystem data is read from the backend worker, never while drawing.
        for thread in threads:
            thread["last_user_at"] = self.user_times.read(thread.get("path"))
            if not hasattr(self, "settings_reader"):
                self.settings_reader = ThreadSettingsReader()
            thread.update(self.settings_reader.read(thread.get("path")))
            try:
                thread["size_bytes"] = Path(thread["path"]).stat().st_size if thread.get("path") else None
            except OSError:
                thread["size_bytes"] = None
        return threads

    def _open_transport(self, remote_id=None):
        from meltygui.toggles import Toggles
        holder = {}
        def event(value):
            if not holder.get("closing"):
                if value.get("method") == "transport/error" and remote_id:
                    value = {**value, "params": {**value.get("params", {}), "threadId": remote_id}}
                if value.get("method") == "thread/settings/updated":
                    value = {**value, "settings_observed_at": time.time()}
                self.publish("event", value)
        server = self.transport_factory(self.source_home, executable=Toggles.InternetAccounts.codex_bin,
            timeout=Toggles.InternetAccounts.codex_request_timeout_s, on_event=event)
        server._melty_holder = holder
        return server

    def _close_transport(self, server):
        server._melty_holder["closing"] = True
        server.close()

    def disconnect(self):
        for server in list(self.writers.values()):
            self._close_transport(server)
        self.writers.clear()
        if self.transport:
            self._close_transport(self.transport)

    def refresh(self):
        if not self.refresh_pending:
            self.refresh_pending = True
            self.submit("refresh")

    def execute(self, operation, *args):
        server = self.transport
        if operation == "defaults":
            self._read_defaults(server, args[0])
        elif operation == "fork":
            key, remote_id, project, title = args
            writer = self._open_transport()
            try:
                result = writer.request("thread/fork", {"threadId": remote_id, "cwd": project})
                fork_id = result["thread"]["id"]
                writer.request("thread/name/set", {"threadId": fork_id, "name": title})
                self.publish("forked", (key, fork_id))
            finally:
                self._close_transport(writer)
        elif operation == "refresh":
            cursor = None
            while not self.closed:
                result = server.request("thread/list", {"limit": 100, "cursor": cursor,
                    "sortKey": "updated_at", "sourceKinds": ["appServer", "cli", "vscode", "exec"]})
                self.publish("listing", self._listing_sizes(result.get("data", [])))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            locks = codex_writer_locks(self.source_home)
            self.publish("writer_locks", {key: owner for key, owner in locks.items() if key not in self.writers} if locks is not None else None)
            self.publish("refreshed", None)
        elif operation == "release":
            remote_id, = args
            writer = self.writers.pop(remote_id, None)
            if writer:
                self._close_transport(writer)
        elif operation == "hydrate":
            key, = args
            chat = self.known.get(key) or dict.get(self, key)
            if chat:
                result = server.request("thread/read", {"threadId": chat.remote_id, "includeTurns": True})
                self.publish("hydrated", (key, self._history(result["thread"], chat["project"])))
        elif operation == "create":
            key, project, title = args
            writer = self._open_transport()
            try:
                options = self._thread_options(writer, key, project)
                observed_at = time.time()
                result = writer.request("thread/start", options)
                writer.request("thread/name/set", {"threadId": result["thread"]["id"], "name": title})
                self.writers[result["thread"]["id"]] = writer
                self.publish("settings", (key, result, observed_at))
                self.publish("created", (key, result["thread"]))
            except Exception:
                self._close_transport(writer)
                raise
        elif operation == "archive":
            key, remote_id = args
            try:
                self.writers.get(remote_id, server).request("thread/archive", {"threadId": remote_id})
            except Exception as error:
                # Unsent threads have no rollout file to archive yet.
                chat = self.known.get(key)
                if (chat is None or not chat.loaded or chat["messages"] or chat["running"]
                        or "no rollout found for thread id" not in str(error).lower()):
                    raise
            writer = self.writers.pop(remote_id, None)
            if writer:
                self._close_transport(writer)
            self.publish("archived", key)
        elif operation == "rename":
            key, remote_id, title = args
            server.request("thread/name/set", {"threadId": remote_id, "name": title})
            self.publish("renamed", (key, title))
        elif operation == "send":
            key, remote_id, message_id, text, resumed = args
            writer = self.writers.get(remote_id) or self._open_transport(remote_id)
            self.writers[remote_id] = writer
            try:
                options = self._thread_options(writer, key, self.known[key]["project"])
                model = options.get("model")
                observed_at = time.time()
                resumed_thread = writer.request("thread/resume", {"threadId": remote_id, **options})
                self.publish("settings", (key, resumed_thread, observed_at))
                result = writer.request("turn/start", {"threadId": remote_id,
                    **({"model": model} if model else {}),
                    **({"effort": options["config"]["model_reasoning_effort"]} if "config" in options else {}),
                    **({"serviceTier": options["serviceTier"]} if "serviceTier" in options else {}),
                    "input": [{"type": "text", "text": text}]})
                self.publish("sent", (key, message_id, result["turn"]["id"]))
            except Exception:
                self.writers.pop(remote_id, None)
                self._close_transport(writer)
                raise
        elif operation == "interrupt":
            key, remote_id, turn_id = args
            self.writers.get(remote_id, server).request("turn/interrupt", {"threadId": remote_id, "turnId": turn_id})
        elif operation == "unsupported":
            request_id, message, remote_id = args
            self.writers.get(remote_id, server).answer(request_id, error={"code": -32601, "message": message})
        elif operation == "answer":
            key, request_id, answer = args
            chat = self.known[key]
            self.writers.get(chat.remote_id, server).answer(request_id, answer)
            self.publish("answered", (key, request_id))

    def receive(self, kind, value):
        if kind == "defaults":
            project, config = value
            self.__dict__.setdefault("config_defaults", {})[project] = config
            self.__dict__.setdefault("config_pending", set()).discard(project)
            if not project and config.get("model"):
                self.default_model = config["model"]
        elif kind == "forked":
            self.finish_fork(*value)
        elif kind == "settings":
            key, settings = value[:2]
            chat = self.known.get(key)
            if chat is not None:
                self._apply_settings(chat, settings, value[2] if len(value) > 2 else None)
        elif kind == "models":
            self.model_service_tiers = {row["model"]: model_service_tiers(row)
                                        for row in value if row.get("model")}
            self.model_default_service_tiers = {row["model"]: row.get("defaultServiceTier")
                                                for row in value if row.get("model")}
            self.model_default_efforts = {row["model"]: row.get("defaultReasoningEffort")
                                         for row in value if row.get("model")}
            self.model_efforts = {row["model"]: tuple(option["reasoningEffort"]
                for option in row.get("supportedReasoningEfforts", []) if option.get("reasoningEffort"))
                for row in value if row.get("model")}
            self.default_model = next((row["model"] for row in value
                                       if row.get("isDefault") and row.get("model")), None)
            self.models = {row.get("displayName") or row["model"]: row["model"]
                           for row in value if row.get("model") and not row.get("hidden")}
        elif kind == "writer_locks":
            if value is not None:
                for chat in self.values():
                    locked = chat.remote_id in value
                    if (chat.get("locked") and not locked and chat.get("retryable_error")
                            and str(chat.error or "").startswith("This conversation is open for writing")):
                        chat.error = None
                    chat["lock_owner"] = value.get(chat.remote_id) if isinstance(value, dict) else None
                    chat["locked"] = locked
                    if locked and chat.get("retryable_error"):
                        chat.error = lock_message(chat["lock_owner"])
        elif kind == "refreshed":
            self.refresh_pending = False
        elif kind == "listing":
            for thread in value:
                key = self.metadata.local_key(self.account_id, thread["id"])
                if key not in self:
                    chat = Chat({"title": thread.get("name") or thread.get("preview") or "Conversation",
                                 "project": thread.get("cwd") or "",
                                 "created_at": epoch_seconds(thread.get("createdAt", thread.get("created_at"))),
                                 "updated": epoch_seconds(thread.get("updatedAt", thread.get("updated_at")))},
                                remote_id=thread["id"])
                    chat["size_bytes"] = thread.get("size_bytes")
                    dict.__setitem__(self, key, chat)
                    self.known[key] = chat
                else:
                    chat = dict.get(self, key)
                    chat["size_bytes"] = thread.get("size_bytes")
                    updated = epoch_seconds(thread.get("updatedAt", thread.get("updated_at")))
                    if thread.get("name") and chat["title"] == chat.saved_title and "rename" not in chat.inflight:
                        chat["title"] = chat.saved_title = thread["name"]
                    if updated > chat["updated"] and not chat["running"]:
                        chat["updated"] = updated
                        self.refresh_history(key)
                chat = dict.get(self, key)
                if thread.get("codex_settings_at", 0) >= chat.get("codex_settings_at", 0):
                    chat.update({field: thread[field] for field in ("codex_settings", "codex_settings_at") if field in thread})
                # Session-naming jobs contain real model/assistant messages, so
                # emptiness and a rounded 0.0 MB size cannot identify them.
                preview = str(thread.get("preview") or "").lstrip()
                chat["title_helper"] = preview.startswith("Name a chat session for the request below.")
                chat["last_user_at"] = max(chat.get("last_user_at", 0), thread.get("last_user_at", 0))
            self.metadata.apply(self.account_id, self)
        elif kind == "ready":
            self.loading = False
        elif kind == "created":
            key, thread = value
            chat = self.known.get(key)
            if chat:
                chat.remote_id = thread["id"]
                chat.inflight.discard("create")
                # Persist remote identity even if the row was removed while creation ran.
                self.metadata.conversation(self.account_id, chat["project"], key)["remote_id"] = chat.remote_id
                chat.inflight.discard("archive")
        elif kind == "hydrated":
            key, history = value
            chat = self.known.get(key)
            if chat:
                # A pre-hotswap worker may already have queued an old raw payload.
                if "messages" not in history:
                    history = self._history(history, chat["project"])
                # Disk history is authoritative; keep only the unsent local drafts.
                pending = {identifier: message for identifier, message in chat["messages"].items()
                           if identifier not in chat.sent and message.get("role") == "user"}
                chat["messages"].clear()
                chat["messages"].update(history["messages"])
                chat.update({field: history[field] for field in ("codex_settings", "codex_settings_at") if field in history
                             and history.get("codex_settings_at", 0) >= chat.get("codex_settings_at", 0)})
                chat.command_scripts = getattr(history, "command_scripts", {})
                chat.sent = set(chat["messages"])
                chat["messages"].update(pending)
                chat.loaded, chat.loading, chat.refreshing = True, False, False
        elif kind == "archived":
            self.known.pop(value, None)
        elif kind == "renamed":
            key, title = value
            chat = self.known.get(key)
            if chat:
                chat.saved_title = title
                chat.inflight.discard("rename")
        elif kind == "sent":
            key, message_id, turn_id = value
            chat = self.known.get(key)
            if chat:
                chat["locked"] = False
                chat.sent.add(message_id)
                chat.resumed = False
                chat.inflight.discard("send")
                if chat["running"]:
                    chat.turn_id = turn_id
        elif kind == "answered":
            key, request_id = value
            if key in self.known:
                self.known[key]["requests"].pop(request_id, None)
        elif kind == "failure":
            operation, args, error = value
            if operation == "defaults":
                self.config_pending.discard(args[0])
                self.models_error = "Could not read Codex defaults: " + error
                return
            if operation == "refresh":
                self.refresh_pending = False
                return
            chat = self.known.get(args[0]) if args else None
            if operation == "send" and chat is not None and "active writer" in error.lower():
                _, _, message_id, text, _ = args
                chat.error = lock_message(chat.get("lock_owner"))
                chat["recovered_draft"] = text
                chat["retryable_error"] = True
                chat["locked"] = True
                chat["messages"].pop(message_id, None)
                chat.scanned = -1
                chat.inflight.difference_update({"send", "interrupt", "interrupt_requested"})
                chat.turn_id = None
                chat["running"] = False
                chat["updated"] = 0.0
                chat.resumed = False
                return
            if operation != "hydrate":
                self.error = error
                self.loading = False
            if chat:
                chat.error = error
                chat.loading = chat.refreshing = False
                chat["running"] = False
                if operation == "archive":
                    dict.__setitem__(self, args[0], chat)
        elif kind == "event":
            self._event(value)

    def _apply_settings(self, chat, settings, observed_at=None):
        observed_at = time.time() if observed_at is None else observed_at
        if observed_at < chat.get("codex_settings_at", 0):
            return
        values = {target: settings[source] for source, target in
                  (("model", "model"), ("reasoningEffort", "effort"), ("effort", "effort"), ("serviceTier", "service_tier"))
                  if source in settings}
        if values:
            chat["codex_settings"] = {**chat.get("codex_settings", {}), **values}
            chat["codex_settings_at"] = observed_at

    def _history(self, thread, project):
        history = Chat({"project": project})
        if not hasattr(self, "settings_reader"):
            self.settings_reader = ThreadSettingsReader()
        history.update(self.settings_reader.read(thread.get("path")))
        for turn in thread.get("turns", []):
            for item in turn.get("items", []):
                self._item(history, item, history=True)
        return history

    def _item(self, chat, item, history=False):
        identifier = item.get("id")
        if not identifier:
            return
        if item.get("type") == "userMessage" and not history:
            return  # the local submitted message already occupies a stable row
        if not hasattr(chat, "command_scripts"):
            chat.command_scripts = {}
        incoming = from_codex(item, scripts=chat.command_scripts)
        incoming["details"].setdefault("cwd", chat["project"])
        old = chat["messages"].get(identifier)
        if isinstance(incoming, CommandExecution) and isinstance(old, CommandExecution):
            if item.get("aggregatedOutput") is None:
                incoming["content"]["output"] = old["content"].get("output", ToolOutput(""))
            # Line counts come from the turn diff, not the item: keep what the
            # started item was stamped with when its completion re-parses the tags.
            for path, entry in incoming.get("summary", {}).items():
                previous = old.get("summary", {}).get(path)
                if previous is not None and entry.get("added") is None and previous.get("added") is not None:
                    entry["added"], entry["removed"] = previous["added"], previous["removed"]
        upsert(chat["messages"], identifier, incoming)

    def _turn_diff(self, chat, diff):
        """Attribute the turn's cumulative diff growth to the latest command writing each file.

        Codex reports no per-command diff; `turn/diff/updated` carries the whole
        turn's unified diff after each item. The growth since the previous update
        is what the newest item did, so it lands on the most recent command whose
        parsed tags name that file (a fileChange item already carries its own).
        """
        counts = diff_counts(diff)
        previous = getattr(chat, "turn_counts", {})
        chat.turn_counts = counts
        for diff_path, (added, removed) in counts.items():
            old_added, old_removed = previous.get(diff_path, (0, 0))
            delta = (max(0, added - old_added), max(0, removed - old_removed))
            for message in reversed(list(chat["messages"].values())):
                if not isinstance(message, CommandExecution) or not isinstance(message.get("summary"), FileTags):
                    continue
                entry = next((entry for path, entry in message["summary"].items()
                              if entry.get("access", "write") == "write" and match_file(path, diff_path)), None)
                if entry is None:
                    continue
                if entry.get("added") is None:
                    if delta == (0, 0):
                        break  # nothing new to count; an unstamped tag stays count-less
                    entry["added"], entry["removed"] = delta
                else:
                    entry["added"] += delta[0]
                    entry["removed"] += delta[1]
                break

    def _event(self, event):
        method, params = event.get("method", ""), event.get("params") or {}
        if method == "transport/error":
            error = params.get("message")
            remote_id = params.get("threadId")
            if not remote_id:
                self.error = error
            for chat in self.values():
                if not remote_id or chat.remote_id == remote_id:
                    chat["running"] = False
                    chat.error = error
            return
        remote_id = params.get("threadId") or (params.get("thread") or {}).get("id")
        chat = next((chat for chat in self.known.values() if chat.remote_id == remote_id), None)
        if chat is not None:
            chat["updated"] = time.time()   # any event for a thread is activity on it
        if "id" in event:
            if chat is None:
                self.submit("unsupported", event["id"], "Unknown conversation", remote_id)
                return
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                chat["requests"][event["id"]] = {"kind": "approval", "data": params,
                    "text": params.get("command") or params.get("reason") or "Approve file changes?"}
            elif method == "item/tool/requestUserInput":
                chat["requests"][event["id"]] = {"kind": "input", "data": params}
            else:
                # Unsupported requests fail explicitly; never silently accept permissions.
                self.submit("unsupported", event["id"], "Not supported by Melty chat yet", remote_id)
                chat.error = f"Unsupported request: {method}"
            return
        if chat is None:
            return
        if method == "thread/settings/updated":
            self._apply_settings(chat, params.get("threadSettings") or {}, event.get("settings_observed_at"))
        elif method in ("item/started", "item/completed"):
            self._item(chat, params.get("item") or {})
        elif method in ("item/agentMessage/delta", "item/commandExecution/outputDelta", "item/plan/delta"):
            identifier = params.get("itemId")
            if not identifier:
                return
            cls, kind = ((CommandExecution, "commandExecution") if "commandExecution" in method else
                         (PlanMessage, "plan") if "plan" in method else (AssistantMessage, "agentMessage"))
            message = chat["messages"].setdefault(identifier, cls(kind))
            delta = params.get("delta", "")
            if isinstance(message, CommandExecution):
                message["content"]["output"] = ToolOutput(message["content"].get("output", "") + delta)
            else:
                set_text(message, message.source_text + delta)
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            identifier = params.get("itemId")
            if identifier:
                message = chat["messages"].setdefault(identifier, ReasoningMessage("reasoning"))
                field = "summary" if "summary" in method else "content"
                index = str(params.get("summaryIndex", params.get("contentIndex", 0)))
                parts = message["content"].setdefault(field, {})
                parts[index] = ToolOutput(str(parts.get(index, "")) + params.get("delta", ""))
        elif method in ("item/fileChange/outputDelta", "item/mcpToolCall/progress"):
            identifier = params.get("itemId")
            if identifier:
                is_file = "fileChange" in method
                cls = FileChange if is_file else McpToolCall
                message = chat["messages"].setdefault(identifier, cls("fileChange" if is_file else "mcpToolCall"))
                field = "output" if is_file else "progress"
                delta = params.get("delta", "") if is_file else params.get("message", "") + "\n"
                message["content"][field] = ToolOutput(message["content"].get(field, "") + delta)
        elif method == "turn/diff/updated":
            self._turn_diff(chat, params.get("diff") or params.get("unifiedDiff") or "")
        elif method == "turn/started":
            chat.turn_counts = {}
            chat.turn_id = params["turn"]["id"]
            chat["running"] = "interrupt_requested" not in chat.inflight
        elif method == "turn/completed":
            self.submit("release", chat.remote_id)
            chat.resumed = False
            chat.turn_id = None
            chat["running"] = False
            chat.inflight.discard("interrupt")
            chat.inflight.discard("interrupt_requested")
            chat["requests"].clear()
            error = params.get("turn", {}).get("error")
            if error:
                chat.error = error.get("message", str(error))
        elif method == "error":
            chat.error = params.get("error", {}).get("message", "Codex error")
        elif method == "serverRequest/resolved":
            chat["requests"].pop(params.get("requestId"), None)
