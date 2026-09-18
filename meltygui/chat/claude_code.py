"""Claude Code as a melty chat backend, through the Claude Agent SDK.

`ClaudeCodeChats` is the ChatProxy for the "anthropic" account kind (the
"Claude Code" provider in the chat window). It maps melty's dict-first
contract onto Claude Code's own session files and the SDK's streaming
client:

* the conversation list is `list_sessions()` — every session Claude Code
  has under ~/.claude/projects, grouped by its working directory
  (`chat["project"]`); removing a row tags the session `__hidden` (the
  SDK's soft delete: the transcript stays on disk, `claude --resume` still
  sees it) rather than deleting the file;
* opening a conversation reads its transcript (`get_session_messages`) and
  renders it with the same mapping the live turn uses;
* a new conversation is a fresh session id; nothing runs until the first
  message, which starts a `claude` process for that session
  (`ClaudeSDKClient`, resumed by id afterwards). Processes close after each
  turn so switching to the CLI and back reloads the latest session history;
* a turn streams: partial text and thinking arrive as `StreamEvent`s,
  tool calls as `AssistantMessage` blocks (Bash → a command row with its
  output, Edit/Write → a file change with a unified diff, the others →
  tool rows), tool results as `UserMessage` blocks, and the turn ends with
  the `ResultMessage`. Stop is `interrupt()`;
* tool permissions come through the SDK's `can_use_tool` callback and
  appear as the window's approval prompts (Accept / Decline); an
  AskUserQuestion call becomes its input prompt.

Authentication is Claude Code's own (`claude` login); an account with an
`api_key` field runs the process with ANTHROPIC_API_KEY set instead. The
`claude` on PATH is used when there is one (the version you run in a
terminal), else the SDK's bundled CLI; MELTY_CLAUDE_BIN overrides
("bundled" forces the bundled one).

Threads: the ChatProxy worker owns the store (session files) and hands
process work to an asyncio loop on its own thread, where the SDK client
lives; everything the UI sees arrives through `publish` and is applied on
the render thread in `receive`. The SDK is imported on first use.
"""
import asyncio
import difflib
import json
import os
import pathlib
import shutil
import threading
import time
import uuid
from meltygui.chat.chat_proxy import writer_conflict
from collections import deque
from contextlib import aclosing

from meltygui.chat.chat_proxy import Chat, ChatProxy
from meltygui.chat.messages import (
    AssistantMessage, CollabAgentToolCall, CommandExecution, DynamicToolCall, FileChange,
    McpToolCall, ReasoningMessage, WebSearch, UserMessage,
    BashString, DiffString, FileReference, FileTags, ImageReference, JsonData, MarkdownString, TextString, ToolOutput,
    changed_files, parse_command, set_text, tagged, text_blocks, upsert, user_message)

HIDDEN_TAG = "__hidden"          # the SDK's documented soft-delete marker
HELPER_PROMPT = "Name a chat session for the request below"   # Claude Code's title-generation side sessions
NEW_TITLE = "New conversation"   # the window's default title; replaced by Claude Code's own after the first turn
LIST_LIMIT = 300                 # most recently used sessions listed
LIVE_CLIENTS = 2                 # `claude` processes kept alive for recent conversations
CONNECT_TIMEOUT = 120.0          # seconds to start a process and accept a message
REQUEST_TIMEOUT = 30.0
STDERR_LINES = 30


def _sdk():
    import claude_agent_sdk
    return claude_agent_sdk


def claude_executable():
    """The CLI the sessions run on: MELTY_CLAUDE_BIN, else `claude` on PATH,
    else None for the SDK's bundled copy."""
    chosen = os.environ.get("MELTY_CLAUDE_BIN", "")
    if chosen == "bundled":
        return None
    return chosen or shutil.which("claude")


# ── the provider side: session files and the live process ────────────────────

class SessionStore:
    """Claude Code's sessions on disk, through the SDK's read/mutate helpers."""

    def __init__(self, limit=LIST_LIMIT):
        from meltygui.chat.activity import UserMessageTimes
        self.user_times = UserMessageTimes("claude")
        self.last_user_times = {}
        self.limit = limit

    def list(self):
        rows = []
        for info in _sdk().list_sessions(limit=self.limit):
            if (info.first_prompt or "").startswith(HELPER_PROMPT):
                continue  # Claude Code's own title-generation calls, not conversations
            rows.append({"id": info.session_id, "project": info.cwd or "",
                         "title": info.custom_title or info.summary or info.first_prompt or "Conversation",
                         "hidden": info.tag == HIDDEN_TAG, "size_bytes": getattr(info, "file_size", None),
                         "created_at": (getattr(info, "created_at", None) or 0) / 1000.0,
                         "last_user_at": self.user_times.read(self.path(info.session_id)),
                         "updated": (info.last_modified or 0) / 1000.0})   # the SDK stamps milliseconds
        return rows

    def messages(self, session_id, project):
        return [(row.type, row.uuid, row.message, row.parent_tool_use_id)
                for row in _sdk().get_session_messages(session_id, directory=project or None)]

    def path(self, session_id):
        """The session's transcript file, found once under the config dir."""
        paths = self.__dict__.setdefault("paths", {})
        path = paths.get(session_id)
        if path is None or not path.exists():
            root = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude") / "projects"
            path = next(iter(root.glob(f"*/{session_id}.jsonl")), None)
            if path is not None:
                paths[session_id] = path
        return path

    def size(self, session_id, project=None):
        """The transcript's size now: where a later `tail` starts."""
        path = self.path(session_id)
        try:
            return path.stat().st_size if path is not None else 0
        except OSError:
            return 0

    def tail(self, session_id, offset):
        """The rows appended past `offset` bytes — (rows as `messages` gives
        them, the new offset) — reading only that tail: a session being
        written by another client re-reads a few lines, not its history.
        A line still being written stays for next time; a file that shrank
        (rewritten) answers None so the caller reads it whole again."""
        path = self.path(session_id)
        if path is None:
            return [], offset
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end < offset:
                return None
            handle.seek(offset)
            data = handle.read(end - offset)
        cut = data.rfind(b"\n") + 1          # only whole lines
        rows = []
        for line in data[:cut].splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") in ("user", "assistant") and isinstance(row.get("message"), dict):
                rows.append((row["type"], row.get("uuid"), row["message"],
                             row.get("parentToolUseId") or (row.get("parentUuid") if row.get("isSidechain") else None)))
        return rows, offset + cut

    def activity(self):
        """{session id: last write, epoch seconds} for every session file on
        disk — one stat each, no reading: what the window polls to light up
        sessions another client (a terminal's Claude Code) is driving."""
        root = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude") / "projects"
        seen = {}
        self.sizes = {}
        try:
            for project_dir in root.iterdir():
                try:
                    for path in project_dir.iterdir():
                        if path.suffix == ".jsonl":
                            stat = path.stat()
                            seen[path.stem] = stat.st_mtime
                            self.sizes[path.stem] = stat.st_size
                except OSError:
                    continue
        except OSError:
            pass
        self.last_user_times = {path.stem: self.user_times.read(path)
                                for path in list(self.user_times.files)}
        return seen

    def states(self):
        root = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
        states = {}
        for path in (root / "sessions").glob("*.json"):
            try:
                row = json.loads(path.read_text())
                pid = row["pid"]
                proc = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                if row.get("procStart") and str(row["procStart"]) != proc[19]:
                    continue
                states[row["sessionId"]] = {"busy": row.get("status") == "busy"}
            except (OSError, ValueError, KeyError, IndexError):
                continue
        cache = self.__dict__.setdefault("state_cache", {})
        for path in list(self.user_times.files):
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                old = cache.get(path)
                if old and old[0] == signature:
                    values = old[1]
                else:
                    values = {}
                    with path.open("rb") as handle:
                        pos, tail = stat.st_size, b""
                        while pos and ("model" not in values or "permissions" not in values):
                            size = min(pos, 65536)
                            pos -= size
                            handle.seek(pos)
                            lines = (handle.read(size) + tail).split(b"\n")
                            tail = lines.pop(0) if pos else b""
                            for line in reversed(lines):
                                try:
                                    row = json.loads(line)
                                except (ValueError, UnicodeError):
                                    continue
                                if row.get("isSidechain") or row.get("parentToolUseId"):
                                    continue
                                if row.get("type") == "permission-mode" and "permissions" not in values:
                                    values["permissions"] = "full" if row.get("permissionMode") == "bypassPermissions" else "ask"
                                    values["permissions_at"] = stat.st_mtime
                                model = (row.get("message") or {}).get("model")
                                if row.get("type") == "assistant" and model and model != "<synthetic>" and "model" not in values:
                                    from meltygui.chat.chat_proxy import epoch_seconds
                                    values["model"] = model
                                    values["model_at"] = epoch_seconds(row.get("timestamp"))
                    cache[path] = signature, values
                states.setdefault(path.stem, {}).update(values)
            except OSError:
                continue
        return states

    def title(self, session_id, project):
        info = _sdk().get_session_info(session_id, directory=project or None)
        return (info.custom_title or info.summary or "") if info else ""

    def rename(self, session_id, title, project):
        _sdk().rename_session(session_id, title, directory=project or None)

    def hide(self, session_id, project):
        _sdk().tag_session(session_id, HIDDEN_TAG, directory=project or None)


def configured_effort(project, model, env=None):
    """Resolve saved per-model effort before the global setting, off the UI thread."""
    environment = {**os.environ, **(env or {})}
    config_home = pathlib.Path(environment.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
    paths = [config_home / "settings.json"]
    if project:
        paths += [pathlib.Path(project) / ".claude" / filename
                  for filename in ("settings.json", "settings.local.json")]
    resolved = (model or "").split("[")[0]
    effort = None
    for path in paths:
        try:
            settings = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        per_model = settings.get("modelSettings", {}).get(resolved, {})
        effort = per_model.get("effortLevel", settings.get("effortLevel", effort))
    override = environment.get("CLAUDE_CODE_EFFORT_LEVEL")
    if override:
        effort = None if override == "auto" else override
    return effort or ("xhigh" if resolved.startswith("claude-opus-4-7") else "high")


class SdkClient:
    """One `claude` process: a ClaudeSDKClient bound to a session."""

    def __init__(self, session_id, project, fresh, can_use_tool, env=None, model=None, permission_mode="default", effort=None):
        sdk = _sdk()
        self.stderr_tail = deque(maxlen=STDERR_LINES)
        self.options = sdk.ClaudeAgentOptions(
            cwd=project or None,
            session_id=session_id if fresh else None,
            resume=None if fresh else session_id,
            can_use_tool=can_use_tool if permission_mode != "bypassPermissions" else None,
            permission_mode=permission_mode,
            model=model,
            effort=effort,
            include_partial_messages=True,
            system_prompt={"type": "preset", "preset": "claude_code"},
            cli_path=claude_executable(),
            env=env or {},
            stderr=self.stderr_tail.append)
        self.client = sdk.ClaudeSDKClient(self.options)

    @staticmethod
    async def discover_models(env=None):
        """Read the CLI's authenticated catalog without starting a conversation."""
        sdk = _sdk()
        client = sdk.ClaudeSDKClient(sdk.ClaudeAgentOptions(
            cli_path=claude_executable(), env=env or {},
            extra_args={"no-session-persistence": None}))
        try:
            await client.connect()
            info = await client.get_server_info()
            if not info or "models" not in info:
                raise RuntimeError("Claude Code did not return a model catalog")
            return info["models"]
        finally:
            await client.disconnect()

    async def connect(self):
        await self.client.connect()

    async def send(self, text):
        await self.client.query(text)

    def messages(self):
        return self.client.receive_messages()

    async def interrupt(self):
        await self.client.interrupt()

    async def close(self):
        await self.client.disconnect()


# ── the melty side ───────────────────────────────────────────────────────────

class ClaudeCodeChats(ChatProxy):
    def __init__(self, account_id, metadata=None, wake=None, account=None,
                 store=None, client_factory=None, live_clients=LIVE_CLIENTS):
        self.models = {}
        self.models_error = None
        self.account = account or {}
        self.store = store or SessionStore()
        self.client_factory = client_factory or SdkClient
        self.live_clients = live_clients
        self.clients = {}       # key -> live client, oldest first (loop thread)
        self.busy = set()       # keys with a turn in flight (loop thread)
        self.interrupted = set()
        self.answers = {}       # request_id -> (key, future) (loop thread)
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="claude-code-loop")
        super().__init__(account_id, metadata, wake)

    # -- worker thread ------------------------------------------------------

    def connect(self):
        self.loop_thread.start()
        # The listing is capped and excludes helper sessions. Compare disk
        # activity with its own snapshot, not with that partial listing.
        self.seen_activity = getattr(self.store, "activity", lambda: {})()
        rows = self.store.list()
        self.listed_ids = {row["id"] for row in rows}
        self.publish("listing", rows)
        self.publish("session_states", getattr(self.store, "states", lambda: {})())
        self.publish("ready", None)
        self.load_models()

    def load_models(self):
        discover = getattr(self.client_factory, "discover_models", None)
        if discover is None:
            return
        try:
            self.publish("models", self._run(discover(env=self._env())))
        except Exception as error:
            self.publish("models_error", str(error))

    def disconnect(self):
        async def close_all():
            for client in list(self.clients.values()):
                try:
                    await client.close()
                except Exception:
                    pass
            self.clients.clear()
        if self.loop_thread.is_alive():
            try:
                self._run(close_all(), timeout=15)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.loop_thread.join(5)
            if not self.loop_thread.is_alive():
                self.loop.close()

    def _run(self, coroutine, timeout=REQUEST_TIMEOUT):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def _env(self):
        env = {}
        if self.account.get("api_key"):
            env["ANTHROPIC_API_KEY"] = self.account["api_key"]
        if self.account.get("base_url"):
            env["ANTHROPIC_BASE_URL"] = self.account["base_url"]
        return env

    def refresh(self):
        self.submit("refresh")

    def default_effort_for(self, project, model):
        model = getattr(self, "resolved_models", {}).get(model, model)
        key = (project, model)
        cache = self.__dict__.setdefault("effort_defaults", {})
        requested = self.__dict__.setdefault("effort_requested", {})
        if time.monotonic() - requested.get(key, 0) > 5:
            requested[key] = time.monotonic()
            self.submit("effort_default", project, model)
        return cache.get(key)

    def execute(self, operation, *args):
        if operation == "effort_default":
            project, model = args
            self.publish("effort_default", ((project, model), configured_effort(project, model, self._env())))
        elif operation == "fork":
            key, remote_id, project, title = args
            result = _sdk().fork_session(remote_id, directory=project or None, title=title)
            self.publish("forked", (key, result.session_id))
        elif operation == "tail":
            key, = args
            chat = self.known.get(key)
            if chat:
                tail = getattr(self.store, "tail", None)
                result = tail(chat.remote_id, getattr(chat, "read_offset", 0)) if tail is not None else None
                if result is None:
                    # No tail reader, or the file was rewritten: read it whole.
                    size = getattr(self.store, "size", lambda *a: 0)(chat.remote_id, chat["project"])
                    self.publish("hydrated", (key, self.store.messages(chat.remote_id, chat["project"]), size))
                else:
                    self.publish("hydrated", (key, result[0], result[1]))
        elif operation == "refresh":
            activity = getattr(self.store, "activity", lambda: {})()
            self.publish("session_states", getattr(self.store, "states", lambda: {})())
            self.publish("activity", activity)
            self.publish("user_times", dict(getattr(self.store, "last_user_times", {})))
            self.publish("sizes", dict(getattr(self.store, "sizes", {})))
            # A session file this window has never listed: someone started a
            # conversation elsewhere — list again so it appears.
            listed = getattr(self, "listed_ids", None)
            previous = getattr(self, "seen_activity", {})
            if listed is not None and any(session_id not in listed and previous.get(session_id) != written
                                          for session_id, written in activity.items()):
                rows = self.store.list()
                self.listed_ids = {row["id"] for row in rows}
                self.publish("listing", rows)
            self.seen_activity = activity
        elif operation == "hydrate":
            key, = args
            chat = self.known.get(key) or dict.get(self, key)
            if chat:
                # The size BEFORE the read: rows landing during it are read by the next tail.
                size = getattr(self.store, "size", lambda *a: 0)(chat.remote_id, chat["project"])
                self.publish("hydrated", (key, self.store.messages(chat.remote_id, chat["project"]), size))
        elif operation == "create":
            key, project, title = args
            self.publish("created", (key, str(uuid.uuid4())))
        elif operation == "archive":
            key, remote_id = args
            chat = self.known.get(key)
            self._run(self._drop(key))
            if chat is not None and not chat.fresh:
                self.store.hide(remote_id, chat["project"])
            self.publish("archived", key)
        elif operation == "rename":
            key, remote_id, title = args
            chat = self.known.get(key)
            if chat is not None and not chat.fresh:
                self.store.rename(remote_id, title, chat["project"])
            self.publish("renamed", (key, title))
        elif operation == "title":
            key, remote_id, project = args
            title = self.store.title(remote_id, project)
            if title:
                self.publish("titled", (key, title))
        elif operation == "send":
            key, remote_id, message_id, text, resumed = args
            chat = self.known[key]
            turn_id = self._run(self._send(key, remote_id, chat["project"], chat.fresh, text), CONNECT_TIMEOUT)
            self.publish("sent", (key, message_id, turn_id))
        elif operation == "interrupt":
            key, remote_id, turn_id = args
            if not self._run(self._interrupt(key)):
                self.publish("completed", (key, None))
        elif operation == "answer":
            key, request_id, answer = args
            self.loop.call_soon_threadsafe(self._resolve, request_id, answer)
            self.publish("answered", (key, request_id))

    # -- loop thread --------------------------------------------------------

    async def _send(self, key, remote_id, project, fresh, text):
        client = self.clients.pop(key, None)
        if client is None:
            await self._evict()
            settings = self.known[key].metadata
            options = {}
            if settings.get("model"):
                options["model"] = settings["model"]
            effort = settings.get("effort")
            if effort in (None, "", "default"):
                model = settings.get("model") or getattr(self, "default_model", None)
                model = getattr(self, "resolved_models", {}).get(model, model)
                effort = configured_effort(project, model, self._env())
            supported = getattr(self, "model_efforts", {}).get(settings.get("model"))
            if effort not in (None, "", "default") and (supported is None or effort in supported):
                options["effort"] = effort
            if settings.get("permissions") == "full":
                options["permission_mode"] = "bypassPermissions"
            client = self.client_factory(remote_id, project, fresh, self._permission(key), env=self._env(), **options)
            try:
                await client.connect()
            except Exception as error:
                raise RuntimeError(_describe(error, client)) from None
        self.clients[key] = client
        await client.send(text)
        turn_id = str(uuid.uuid4())
        self.busy.add(key)
        self.interrupted.discard(key)
        self.loop.create_task(self._pump(key, client))
        return turn_id

    async def _evict(self):
        for key in list(self.clients):
            if len(self.clients) < self.live_clients:
                return
            if key not in self.busy:
                await self._drop(key)

    async def _drop(self, key):
        client = self.clients.pop(key, None)
        for request_id, (owner, future) in list(self.answers.items()):
            if owner == key and not future.done():
                future.set_result(None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def _interrupt(self, key):
        client = self.clients.get(key)
        if client is None or key not in self.busy:
            return False
        self.interrupted.add(key)
        for request_id, (owner, future) in list(self.answers.items()):
            if owner == key and not future.done():
                future.set_result(None)
        await client.interrupt()
        return True

    async def _pump(self, key, client):
        sdk = _sdk()
        error = None
        try:
            async with aclosing(client.messages()) as stream:
                async for message in stream:
                    self.publish("message", (key, message))
                    if isinstance(message, sdk.ResultMessage):
                        if message.is_error and key not in self.interrupted:
                            error = (message.result or "; ".join(message.errors or [])
                                     or f"Claude Code stopped: {message.subtype}")
                        break
        except Exception as failure:
            error = _describe(failure, client)
            self.clients.pop(key, None)
        finally:
            self.busy.discard(key)
            self.interrupted.discard(key)
            await self._drop(key)
        self.publish("completed", (key, error))

    def _permission(self, key):
        async def can_use_tool(tool_name, tool_input, context):
            sdk = _sdk()
            request_id = context.tool_use_id or str(uuid.uuid4())
            future = self.loop.create_future()
            self.answers[request_id] = (key, future)
            self.publish("permission", (key, request_id, tool_name, tool_input, {
                "title": context.title, "description": context.description,
                "display_name": context.display_name, "reason": context.decision_reason,
                "blocked_path": context.blocked_path}))
            try:
                answer = await future
            finally:
                self.answers.pop(request_id, None)
            if not answer:
                return sdk.PermissionResultDeny(message="Cancelled in melty", interrupt=True)
            if "answers" in answer:
                # AskUserQuestion: the tool's answers travel back in its input.
                replies = {question: (entry.get("answers") or [""])[0]
                           for question, entry in answer["answers"].items()}
                return sdk.PermissionResultAllow(updated_input={**tool_input, "answers": replies})
            if answer.get("decision") == "accept":
                return sdk.PermissionResultAllow()
            return sdk.PermissionResultDeny(message="Declined in melty")
        return can_use_tool

    def _resolve(self, request_id, answer):
        entry = self.answers.get(request_id)
        if entry is not None and not entry[1].done():
            entry[1].set_result(answer)

    # -- render thread ------------------------------------------------------

    def receive(self, kind, value):
        if kind == "forked":
            self.finish_fork(*value)
        elif kind == "listing":
            for row in value:
                if row["hidden"]:
                    continue
                key = self.metadata.local_key(self.account_id, row["id"])
                if key not in self:
                    chat = Chat({"title": row["title"], "project": row["project"],
                                 "updated": row.get("updated", 0.0),
                                 "last_user_at": row.get("last_user_at", 0.0),
                                 "created_at": row.get("created_at", 0.0)}, remote_id=row["id"])
                    chat["size_bytes"] = row.get("size_bytes")
                    chat.fresh = False
                    chat.refreshing = False
                    dict.__setitem__(self, key, chat)
                    self.known[key] = chat
            self.metadata.apply(self.account_id, self)
        elif kind == "session_states":
            for chat in self.values():
                remote = value.get(chat.remote_id, {})
                chat["external_busy"] = remote.get("busy", False)
                for field in ("model", "permissions"):
                    if field in remote and remote.get(field + "_at", 0) >= chat.metadata.get(field + "_selected_at", 0):
                        chat.metadata[field] = remote[field]
                        chat.metadata[field + "_explicit"] = False
        elif kind == "user_times":
            for chat in self.values():
                chat["last_user_at"] = max(chat.get("last_user_at", 0), value.get(chat.remote_id, 0))
        elif kind == "sizes":
            for chat in self.values():
                if chat.remote_id in value:
                    chat["size_bytes"] = value[chat.remote_id]
        elif kind == "activity":
            for key, chat in self.known.items():
                written = value.get(chat.remote_id)
                if written and written > (chat.get("updated") or 0.0) + 0.5:
                    chat["updated"] = written
                    # Another client is writing this session: read it again so
                    # the transcript fills in as it goes (a turn of ours streams
                    # its own events; a chat not yet opened hydrates on open).
                    if not chat["running"]:
                        self.refresh_history(key, "tail")
        elif kind == "effort_default":
            key, effort = value
            self.__dict__.setdefault("effort_defaults", {})[key] = effort
        elif kind == "models":
            self.resolved_models = {row["value"]: row.get("resolvedModel") or row["value"]
                                    for row in value if row.get("value")}
            self.model_efforts = {model: tuple(row.get("supportedEffortLevels") or ())
                for row in value for model in (row.get("value"), row.get("resolvedModel")) if model}
            default = next((row for row in value if row.get("value") == "default"), {})
            resolved = default.get("resolvedModel")
            self.default_model = next((row["value"] for row in value
                if row.get("value") != "default" and resolved
                and row.get("resolvedModel") == resolved), resolved)
            self.models = {row.get("displayName") or row["value"]: row["value"]
                           for row in value if row.get("value") and row["value"] != "default"}
            if self.default_model and self.default_model not in self.models.values():
                self.models[self.default_model] = self.default_model
            self.models_error = None
        elif kind == "models_error":
            self.models_error = value
        elif kind == "ready":
            self.loading = False
        elif kind == "created":
            key, remote_id = value
            chat = self.known.get(key)
            if chat:
                chat.remote_id = remote_id
                chat.fresh = True
                chat.inflight.discard("create")
                self.metadata.conversation(self.account_id, chat["project"], key)["remote_id"] = remote_id
                chat.inflight.discard("archive")
        elif kind == "hydrated":
            key, rows, offset = value if len(value) == 3 else (*value, 0)
            chat = self.known.get(key)
            if chat:
                for role, row_id, message, parent in rows:
                    self._history(chat, role, row_id, message, parent)
                chat.sent.update(chat["messages"])
                chat.read_offset = offset
                chat.loaded, chat.loading, chat.refreshing = True, False, False
        elif kind == "archived":
            self.known.pop(value, None)
        elif kind == "renamed":
            key, title = value
            chat = self.known.get(key)
            if chat:
                chat.saved_title = title
                chat.inflight.discard("rename")
        elif kind == "titled":
            key, title = value
            chat = self.known.get(key)
            if chat and chat["title"] == NEW_TITLE:
                chat["title"] = chat.saved_title = title
        elif kind == "sent":
            key, message_id, turn_id = value
            chat = self.known.get(key)
            if chat:
                chat["locked"] = False
                chat.sent.add(message_id)
                chat.inflight.discard("send")
                if chat["running"]:
                    chat.turn_id = turn_id
        elif kind == "answered":
            key, request_id = value
            if key in self.known:
                self.known[key]["requests"].pop(request_id, None)
        elif kind == "permission":
            key, request_id, tool_name, tool_input, info = value
            chat = self.known.get(key)
            if chat:
                chat["requests"][request_id] = _request(tool_name, tool_input, info)
        elif kind == "message":
            key, message = value
            chat = self.known.get(key)
            if chat:
                chat["updated"] = time.time()
                self._live(chat, message)
        elif kind == "completed":
            key, error = value
            chat = self.known.get(key)
            if chat:
                chat["updated"] = time.time()
                chat.turn_id = None
                chat["running"] = False
                chat.inflight.discard("interrupt")
                chat.inflight.discard("interrupt_requested")
                chat["requests"].clear()
                if error:
                    chat["locked"] = writer_conflict(error)
                    chat.error = error
                if chat.fresh:
                    chat.fresh = False
                    if chat["title"] != NEW_TITLE:
                        chat.saved_title = None   # push the local title onto the new session
                    else:
                        self.submit("title", key, chat.remote_id, chat["project"])
        elif kind == "failure":
            operation, args, error = value
            self.error = error
            self.loading = False
            chat = self.known.get(args[0]) if args else None
            if chat:
                if writer_conflict(error):
                    chat["locked"] = True
                chat.error = error
                chat.loading = False
                chat["running"] = False
                if operation == "archive":
                    dict.__setitem__(self, args[0], chat)

    # -- message mapping ----------------------------------------------------

    @staticmethod
    def _remember_model(chat, model):
        if not model or model == "<synthetic>":
            return
        chat["model"] = model
        if not chat.metadata.get("model_explicit"):
            chat.metadata["model"] = model

    @staticmethod
    def _usage(chat, usage):
        """The header's token badge: the context the last request carried."""
        if isinstance(usage, dict) and "input_tokens" in usage:
            chat["context_tokens"] = sum(usage.get(key, 0) or 0 for key in
                ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))

    def _history(self, chat, role, row_id, message, parent):
        if parent:
            return
        if role == "assistant":
            self._usage(chat, message.get("usage"))
        if role == "assistant":
            self._remember_model(chat, message.get("model"))
        content = message.get("content", "")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        self._blocks(chat, role, content, message.get("id") or row_id, row_id, history=True)

    def _live(self, chat, message):
        sdk = _sdk()
        if isinstance(message, sdk.StreamEvent):
            if not message.parent_tool_use_id:
                self._stream(chat, message.event)
        elif isinstance(message, sdk.AssistantMessage):
            if message.error:
                chat.error = _assistant_error(message.error)
            if not message.parent_tool_use_id:
                self._remember_model(chat, message.model)
                self._blocks(chat, "assistant", [_block_dict(b) for b in message.content],
                             message.message_id or message.uuid or "live", message.uuid)
        elif isinstance(message, sdk.UserMessage):
            if not message.parent_tool_use_id and isinstance(message.content, list):
                self._blocks(chat, "user", [_block_dict(b) for b in message.content], None, message.uuid)

    def _blocks(self, chat, role, blocks, message_id, row_id, history=False):
        scripts = _attr(chat, "command_scripts", dict)
        counts = _attr(chat, "block_counts", dict)
        messages = chat["messages"]
        if role == "user":
            # One user row per message: its text blocks as prose, its image
            # blocks (pasted pictures) as ImageReferences drawn inline.
            texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
            images = [block for block in blocks if block.get("type") == "image"]
            if history and row_id and (any(text.strip() for text in texts) or images):
                incoming = user_message("\n\n".join(text for text in texts if text.strip()))
                for index, block in enumerate(images):
                    incoming["content"]["image" if index == 0 else f"image {index + 1}"] = _image_reference(block)
                upsert(messages, row_id, incoming)
            for block in blocks:
                if block.get("type") in ("tool_result", "web_search_tool_result", "web_fetch_tool_result"):
                    target = messages.get(block.get("tool_use_id"))
                    if target is not None:
                        _tool_result(target, block)
            return
        for block in blocks:
            kind = block.get("type")
            if kind in ("text", "thinking"):
                ordinal = counts.get(message_id, 0)
                counts[message_id] = ordinal + 1
                key = f"{message_id}:{ordinal}"
                if kind == "text":
                    incoming = AssistantMessage("agentMessage", status="completed")
                    set_text(incoming, block.get("text", ""))
                else:
                    if not block.get("thinking"):
                        continue
                    incoming = ReasoningMessage("reasoning", status="completed")
                    incoming["content"]["content"] = {"0": ToolOutput(block["thinking"])}
                upsert(messages, key, incoming)
            elif kind in ("tool_use", "server_tool_use"):
                incoming = tool_message(block.get("name", ""), block.get("input") or {}, chat["project"], scripts)
                incoming["status"] = "completed" if history else "running"
                upsert(messages, block.get("id") or f"{message_id}:tool", incoming)

    def _stream(self, chat, event):
        kind = event.get("type")
        if kind == "message_start":
            self._usage(chat, (event.get("message") or {}).get("usage"))
            chat["_usage_input"] = chat.get("context_tokens")
        elif kind == "message_delta" and chat.get("_usage_input") is not None:
            chat["context_tokens"] = chat["_usage_input"] + (event.get("usage") or {}).get("output_tokens", 0)
        messages = chat["messages"]
        keys = _attr(chat, "stream_keys", dict)
        if kind == "message_start":
            chat.stream_message = (event.get("message") or {}).get("id") or "live"
            keys.clear()
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            index = event.get("index", 0)
            message_id = getattr(chat, "stream_message", "live")
            if block.get("type") == "text":
                keys[index] = f"{message_id}:{index}"
                messages.setdefault(keys[index], AssistantMessage("agentMessage", status="running"))
            elif block.get("type") == "thinking":
                keys[index] = f"{message_id}:{index}"
                message = messages.setdefault(keys[index], ReasoningMessage("reasoning", status="running"))
                message["content"].setdefault("content", {})
            elif block.get("type") in ("tool_use", "server_tool_use") and block.get("id"):
                placeholder = tool_message(block.get("name", ""), {}, chat["project"],
                                           _attr(chat, "command_scripts", dict))
                placeholder["status"] = "running"
                messages.setdefault(block["id"], placeholder)
        elif kind == "content_block_delta":
            key = keys.get(event.get("index", 0))
            delta = event.get("delta") or {}
            message = messages.get(key) if key else None
            if message is None:
                return
            if delta.get("type") == "text_delta" and isinstance(message, AssistantMessage):
                set_text(message, message.source_text + delta.get("text", ""))
            elif delta.get("type") == "thinking_delta" and isinstance(message, ReasoningMessage):
                parts = message["content"].setdefault("content", {})
                parts["0"] = ToolOutput(str(parts.get("0", "")) + delta.get("thinking", ""))
        elif kind == "content_block_stop":
            key = keys.get(event.get("index", 0))
            message = messages.get(key) if key else None
            if message is not None:
                message["status"] = "completed"
                counts = _attr(chat, "block_counts", dict)
                message_id = getattr(chat, "stream_message", "live")
                counts[message_id] = max(counts.get(message_id, 0), event.get("index", 0) + 1)


# ── helpers ──────────────────────────────────────────────────────────────────

def _attr(chat, name, factory):
    value = getattr(chat, name, None)
    if value is None:
        value = factory()
        setattr(chat, name, value)
    return value


def _describe(error, client):
    tail = "\n".join(getattr(client, "stderr_tail", ()) or ())
    text = str(error) or type(error).__name__
    return f"{text}\n{tail}" if tail else text


def _assistant_error(error):
    return {"authentication_failed": "Claude Code is not signed in: run `claude` once to log in",
            "billing_error": "Claude Code billing error", "rate_limit": "Claude Code rate limit reached",
            "invalid_request": "Claude Code rejected the request", "server_error": "Claude API server error",
            "unknown": "Claude Code error"}.get(str(error), str(error))


def _block_dict(block):
    """An SDK content block dataclass as the API's dict shape."""
    name = type(block).__name__
    if name == "TextBlock":
        return {"type": "text", "text": block.text}
    if name == "ThinkingBlock":
        return {"type": "thinking", "thinking": block.thinking}
    if name in ("ToolUseBlock", "ServerToolUseBlock"):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if name == "ToolResultBlock":
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content,
                "is_error": block.is_error}
    if name == "ServerToolResultBlock":
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
    return {"type": name}


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    if isinstance(content, dict):
        return json.dumps(content, indent=1)
    return ""


def _image_reference(block):
    """An API image block ({"type": "image", "source": {"type": "base64",
    "media_type", "data"}}) as the transcript's ImageReference: the payload
    kept as it is (the window decodes it once, keyed by its hash)."""
    source = block.get("source") or {}
    return ImageReference(media_type=TextString(str(source.get("media_type", ""))),
                          data=TextString(str(source.get("data", ""))),
                          **({"path": TextString(str(source["path"]))} if source.get("path") else {}))


def _tool_result(message, block):
    content = block.get("content")
    text = _result_text(content)
    message["content"]["output"] = ToolOutput(text)
    if isinstance(content, list):
        # A Read of a picture answers with an image block: show it under the row.
        for index, part in enumerate(part for part in content if isinstance(part, dict) and part.get("type") == "image"):
            message["content"]["image" if index == 0 else f"image {index + 1}"] = _image_reference(part)
    message["status"] = "failed" if block.get("is_error") else "completed"
    if block.get("is_error"):
        message["details"]["error"] = TextString(text.splitlines()[0] if text else "failed")


def _unified_diff(path, old, new):
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=path, tofile=path)
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _file_change(message, path, old, new, kind="update"):
    index = str(len(message["content"]))
    message["content"][index] = {"file": FileReference(path=TextString(path)),
                                 "diff": DiffString(_unified_diff(path, old, new), "diff"),
                                 "kind": TextString(kind)}


def tool_message(name, tool_input, cwd, scripts):
    """A melty ToolCall for a Claude Code tool call; the result fills `output` later."""
    if name == "Bash":
        command = str(tool_input.get("command", ""))
        source, files = parse_command(command, None, scripts, cwd=cwd or "")
        message = CommandExecution("commandExecution")
        message["summary"] = FileTags((path, {"file": FileReference(path=TextString(path)),
                                              "access": access, "added": None, "removed": None})
                                      for path, access in files.items())
        message["content"].update(command=BashString(source), output=ToolOutput(""))
        if tool_input.get("description"):
            message["details"]["description"] = TextString(str(tool_input["description"]))
    elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        message = FileChange("fileChange")
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if name == "Edit":
            _file_change(message, path, str(tool_input.get("old_string", "")), str(tool_input.get("new_string", "")))
        elif name == "Write":
            _file_change(message, path, "", str(tool_input.get("content", "")), kind="write")
        elif name == "MultiEdit":
            for edit in tool_input.get("edits") or []:
                _file_change(message, path, str(edit.get("old_string", "")), str(edit.get("new_string", "")))
        else:
            _file_change(message, path, "", str(tool_input.get("new_source", "")), kind=str(tool_input.get("edit_mode", "replace")))
        message["summary"] = changed_files(message)
    elif name == "Read":
        message = DynamicToolCall("read")
        path = str(tool_input.get("file_path", ""))
        message["summary"] = FileTags({path: {"file": FileReference(path=TextString(path)), "access": "read",
                                              "added": None, "removed": None}} if path else {})
        message["content"]["file"] = FileReference(path=TextString(path))
    elif name in ("WebSearch", "WebFetch", "web_search", "web_fetch"):
        message = WebSearch("webSearch")
        for field in ("query", "url", "prompt"):
            if tool_input.get(field):
                message["content"][field] = TextString(str(tool_input[field]))
    elif name in ("Task", "Agent"):
        message = CollabAgentToolCall("collabAgentToolCall")
        if tool_input.get("description"):
            message["content"]["description"] = TextString(str(tool_input["description"]))
        if tool_input.get("prompt"):
            message["content"]["prompt"] = MarkdownString(str(tool_input["prompt"]))
        if tool_input.get("subagent_type"):
            message["details"]["agent"] = TextString(str(tool_input["subagent_type"]))
    elif name.startswith("mcp__"):
        message = McpToolCall("mcpToolCall")
        parts = name.split("__", 2)
        message["details"]["server"] = TextString(parts[1] if len(parts) > 1 else "")
        message["content"]["arguments"] = JsonData(tagged(tool_input))
    else:
        message = DynamicToolCall("dynamicToolCall")
        if tool_input:
            message["content"]["arguments"] = JsonData(tagged(tool_input))
    message["details"]["tool"] = TextString(name)
    return message


def _request(tool_name, tool_input, info):
    """The window's request dict for a permission prompt."""
    if tool_name == "AskUserQuestion":
        questions = []
        for question in tool_input.get("questions") or []:
            options = ", ".join(str(option.get("label", "")) for option in question.get("options") or [])
            label = str(question.get("question", ""))
            if question.get("header"):
                label = f"{question['header']}: {label}"
            if options:
                label += f"  [{options}]"
            questions.append({"id": str(question.get("question", "")), "question": label})
        return {"kind": "input", "data": {"questions": questions, "tool": tool_name}}
    title = info.get("title") or f"Claude wants to use {tool_name}"
    detail = ""
    if tool_name == "Bash":
        detail = str(tool_input.get("command", ""))
    elif tool_input.get("file_path"):
        detail = str(tool_input["file_path"])
    elif tool_input:
        detail = json.dumps(tool_input)[:400]
    text = title if not detail else f"{title}\n{detail}"
    if info.get("reason"):
        text += f"\n{info['reason']}"
    return {"kind": "approval", "text": text,
            "data": {"tool": tool_name, "input": tool_input, "description": info.get("description")}}
