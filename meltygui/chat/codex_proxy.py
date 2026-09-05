"""Codex implements the chat mapping. All protocol interpretation lives here."""
from src.lsd.gl_gui.chat.chat_proxy import Chat, ChatProxy
from src.lsd.gl_gui.chat.messages import (from_codex, upsert, set_text, AssistantMessage,
    PlanMessage, CommandExecution, ToolOutput, ReasoningMessage, FileChange, McpToolCall,
    FileTags, diff_counts, match_file)
from src.lsd.gl_gui.chat.codex_transport import CodexTransport
from src.lsd.gl_gui.fim_providers.codex_accounts import account_home


class CodexChats(ChatProxy):
    def __init__(self, account_id, metadata=None, wake=None, transport_factory=None):
        self.source_home = account_home(account_id)
        self.transport = None
        self.transport_factory = transport_factory or CodexTransport
        super().__init__(account_id, metadata, wake)

    def connect(self):
        from src.lsd.gl_gui.toggles import Toggles
        self.transport = self.transport_factory(
            self.source_home, executable=Toggles.InternetAccounts.codex_bin,
            timeout=Toggles.InternetAccounts.codex_request_timeout_s,
            on_event=lambda event: self.publish("event", event))
        account = self.transport.request("account/read", {"refreshToken": False}).get("account")
        if not account:
            raise RuntimeError("Sign in to this account in Internet Accounts first")
        cursor = None
        while not self.closed:
            result = self.transport.request("thread/list", {
                "limit": 100, "cursor": cursor, "sortKey": "updated_at",
                "sourceKinds": ["appServer", "cli", "vscode", "exec"]})
            self.publish("listing", result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.publish("ready", None)

    def disconnect(self):
        if self.transport:
            self.transport.close()

    def execute(self, operation, *args):
        server = self.transport
        if operation == "hydrate":
            key, = args
            chat = self.known.get(key) or dict.get(self, key)
            if chat:
                result = server.request("thread/read", {"threadId": chat.remote_id, "includeTurns": True})
                self.publish("hydrated", (key, result["thread"]))
        elif operation == "create":
            key, project, title = args
            result = server.request("thread/start", {"cwd": project,
                "approvalPolicy": "on-request", "sandbox": "workspace-write"})
            self.publish("created", (key, result["thread"]))
            server.request("thread/name/set", {"threadId": result["thread"]["id"], "name": title})
        elif operation == "archive":
            key, remote_id = args
            server.request("thread/archive", {"threadId": remote_id})
            self.publish("archived", key)
        elif operation == "rename":
            key, remote_id, title = args
            server.request("thread/name/set", {"threadId": remote_id, "name": title})
            self.publish("renamed", (key, title))
        elif operation == "send":
            key, remote_id, message_id, text, resumed = args
            if not resumed:
                server.request("thread/resume", {"threadId": remote_id,
                    "approvalPolicy": "on-request", "sandbox": "workspace-write"})
            result = server.request("turn/start", {"threadId": remote_id,
                "input": [{"type": "text", "text": text}]})
            self.publish("sent", (key, message_id, result["turn"]["id"]))
        elif operation == "interrupt":
            key, remote_id, turn_id = args
            server.request("turn/interrupt", {"threadId": remote_id, "turnId": turn_id})
        elif operation == "unsupported":
            request_id, message = args
            server.answer(request_id, error={"code": -32601, "message": message})
        elif operation == "answer":
            key, request_id, answer = args
            server.answer(request_id, answer)
            self.publish("answered", (key, request_id))

    def receive(self, kind, value):
        if kind == "listing":
            for thread in value:
                key = self.metadata.local_key(self.account_id, thread["id"])
                if key not in self:
                    chat = Chat({"title": thread.get("name") or thread.get("preview") or "Conversation",
                                 "project": thread.get("cwd") or ""}, remote_id=thread["id"])
                    dict.__setitem__(self, key, chat)
                    self.known[key] = chat
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
            key, thread = value
            chat = self.known.get(key)
            if chat:
                for turn in thread.get("turns", []):
                    for item in turn.get("items", []):
                        self._item(chat, item, history=True)
                chat.sent.update(chat["messages"])
                chat.loaded, chat.loading = True, False
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
                chat.sent.add(message_id)
                chat.resumed = True
                chat.inflight.discard("send")
                if chat["running"]:
                    chat.turn_id = turn_id
        elif kind == "answered":
            key, request_id = value
            if key in self.known:
                self.known[key]["requests"].pop(request_id, None)
        elif kind == "failure":
            operation, args, error = value
            self.error = error
            self.loading = False
            chat = self.known.get(args[0]) if args else None
            if chat:
                chat.error = error
                chat.loading = False
                chat["running"] = False
                # Failed deletions come back online instead of silently disappearing.
                if operation == "archive":
                    dict.__setitem__(self, args[0], chat)
        elif kind == "event":
            self._event(value)

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
            self.error = params.get("message")
            for chat in self.values():
                chat["running"] = False
                chat.error = self.error
            return
        remote_id = params.get("threadId") or (params.get("thread") or {}).get("id")
        chat = next((chat for chat in self.known.values() if chat.remote_id == remote_id), None)
        if "id" in event:
            if chat is None:
                self.submit("unsupported", event["id"], "Unknown conversation")
                return
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                chat["requests"][event["id"]] = {"kind": "approval", "data": params,
                    "text": params.get("command") or params.get("reason") or "Approve file changes?"}
            elif method == "item/tool/requestUserInput":
                chat["requests"][event["id"]] = {"kind": "input", "data": params}
            else:
                # Unsupported requests fail explicitly; never silently accept permissions.
                self.submit("unsupported", event["id"], "Not supported by Melty chat yet")
                chat.error = f"Unsupported request: {method}"
            return
        if chat is None:
            return
        if method in ("item/started", "item/completed"):
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