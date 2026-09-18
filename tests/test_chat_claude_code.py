"""ClaudeCodeChats against a scripted client and an in-memory session store.

The SDK's message dataclasses are real; only the `claude` process
(`SdkClient`) and the session files (`SessionStore`) are faked.
"""
import asyncio
import time

import pytest

pytest.importorskip("claude_agent_sdk")   # the meltygui[claude] extra
from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, ToolPermissionContext, PermissionResultAllow,
                              PermissionResultDeny)

from meltygui.chat import ChatMetadata
from meltygui.chat.messages import (CommandExecution, FileChange, McpToolCall, DynamicToolCall,
                                 AssistantMessage as MeltyAssistant, UserMessage as MeltyUser, input_text)
from meltygui.chat.claude_code import ClaudeCodeChats, tool_message, NEW_TITLE, HIDDEN_TAG


def result(session_id="s1", is_error=False, subtype="success", text=None):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
                         session_id=session_id, result=text)


class FakeStore:
    def __init__(self):
        self.sessions = [
            {"id": "old", "project": "/project", "title": "Old chat", "hidden": False},
            {"id": "gone", "project": "/project", "title": "Hidden", "hidden": True},
        ]
        self.history = {"old": [
            ("user", "u1", {"role": "user", "content": "Hello there"}, None),
            ("assistant", "a1", {"id": "msg1", "role": "assistant", "content": [{"type": "text", "text": "Hi! Running ls."}]}, None),
            ("assistant", "a2", {"id": "msg1", "role": "assistant", "content": [
                {"type": "tool_use", "id": "tool1", "name": "Bash", "input": {"command": "ls /project"}}]}, None),
            ("user", "u2", {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tool1", "content": "a.py\nb.py"}]}, None),
            ("assistant", "a3", {"id": "msg2", "role": "assistant", "content": [{"type": "text", "text": "Two files."}]}, "sub"),
        ]}
        self.calls = []

    def list(self):
        return list(self.sessions)

    def messages(self, session_id, project):
        self.calls.append(("messages", session_id, project))
        return list(self.history.get(session_id, []))

    def title(self, session_id, project):
        self.calls.append(("title", session_id, project))
        return "Claude's title"

    def rename(self, session_id, title, project):
        self.calls.append(("rename", session_id, title, project))

    def hide(self, session_id, project):
        self.calls.append(("hide", session_id, project))


class FakeClient:
    instances = []
    script = None   # async def script(client, text) -> list of messages

    def __init__(self, session_id, project, fresh, can_use_tool, env=None, effort=None):
        self.session_id, self.project, self.fresh, self.can_use_tool, self.env = session_id, project, fresh, can_use_tool, env
        self.sent, self.closed, self.interrupts, self.connected = [], False, 0, False
        self.queue = asyncio.Queue()
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def send(self, text):
        self.sent.append(text)
        asyncio.get_running_loop().create_task(self._play(text))

    async def _play(self, text):
        for message in await FakeClient.script(self, text):
            await self.queue.put(message)

    async def messages(self):
        while True:
            message = await self.queue.get()
            yield message
            if isinstance(message, ResultMessage):
                return

    async def interrupt(self):
        self.interrupts += 1
        await self.queue.put(result(self.session_id, is_error=True, subtype="error_during_execution",
                                    text="Interrupted"))

    async def close(self):
        self.closed = True


async def plain_reply(client, text):
    return [
        StreamEvent(uuid="e1", session_id=client.session_id, event={"type": "message_start", "message": {"id": "m1"}}),
        StreamEvent(uuid="e2", session_id=client.session_id, event={"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        StreamEvent(uuid="e3", session_id=client.session_id, event={"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}}),
        StreamEvent(uuid="e4", session_id=client.session_id, event={"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world"}}),
        StreamEvent(uuid="e5", session_id=client.session_id, event={"type": "content_block_stop", "index": 0}),
        AssistantMessage(content=[TextBlock(text="Hello world")], model="claude", message_id="m1", uuid="a1"),
        result(client.session_id),
    ]


def settle(proxy, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        proxy.drain()
        if predicate():
            return
        assert time.monotonic() < deadline, (proxy.error, [c.error for c in proxy.known.values()])
        time.sleep(0.005)


@pytest.fixture
def proxy():
    FakeClient.instances = []
    FakeClient.script = plain_reply
    store = FakeStore()
    proxy = ClaudeCodeChats("anthropic", ChatMetadata(), store=store, client_factory=FakeClient, live_clients=2)
    settle(proxy, lambda: not proxy.loading)
    yield proxy
    proxy.close()
    proxy.worker.join(3)
    assert not proxy.worker.is_alive()
    assert not proxy.loop_thread.is_alive()


def test_listing_and_history(proxy):
    assert list(proxy) == ["old"]
    chat = proxy["old"]
    assert chat["project"] == "/project" and chat["title"] == "Old chat"
    settle(proxy, lambda: chat.loaded)
    messages = chat["messages"]
    assert isinstance(messages["u1"], MeltyUser) and input_text(messages["u1"]) == "Hello there"
    assert isinstance(messages["msg1:0"], MeltyAssistant) and input_text(messages["msg1:0"]) == "Hi! Running ls."
    command = messages["tool1"]
    assert isinstance(command, CommandExecution)
    assert str(command["content"]["command"]) == "ls /project"
    assert str(command["content"]["output"]) == "a.py\nb.py" and command["status"] == "completed"
    assert "msg2:0" not in messages            # sub-agent traffic stays out of the transcript
    assert set(chat.sent) == set(messages)     # nothing from history is re-sent
    proxy.reconcile()
    assert not chat["running"]


def test_new_conversation_streams_and_titles(proxy):
    proxy["new"] = {"title": NEW_TITLE, "project": "/project"}
    proxy.reconcile()
    settle(proxy, lambda: proxy["new"].remote_id is not None)
    chat = proxy["new"]
    assert chat.fresh and chat.loaded
    chat["messages"]["local"] = {"role": "user", "text": "Say hi"}
    proxy.reconcile()
    assert chat["running"]
    settle(proxy, lambda: "local" in chat.sent and chat.turn_id is not None or not chat["running"])
    settle(proxy, lambda: not chat["running"])
    client, = FakeClient.instances
    assert client.fresh and client.session_id == chat.remote_id and client.project == "/project"
    assert client.sent == ["Say hi"]
    assert input_text(chat["messages"]["m1:0"]) == "Hello world"
    assert chat["messages"]["m1:0"]["status"] == "completed"
    assert chat.error is None and chat.turn_id is None
    settle(proxy, lambda: chat["title"] == "Claude's title")
    assert ("title", chat.remote_id, "/project") in proxy.store.calls
    assert not chat.fresh
    chat["title"] = "Renamed"
    proxy.reconcile()
    settle(proxy, lambda: chat.saved_title == "Renamed")
    assert ("rename", chat.remote_id, "Renamed", "/project") in proxy.store.calls


def test_rename_before_first_turn_lands_after_it(proxy):
    proxy["new"] = {"title": NEW_TITLE, "project": "/project"}
    proxy.reconcile()
    settle(proxy, lambda: proxy["new"].remote_id is not None)
    chat = proxy["new"]
    chat["title"] = "Planned"
    proxy.reconcile()
    settle(proxy, lambda: chat.saved_title == "Planned")
    assert not any(call[0] == "rename" for call in proxy.store.calls)
    chat["messages"]["local"] = {"role": "user", "text": "go"}
    proxy.reconcile()
    settle(proxy, lambda: not chat["running"])
    proxy.reconcile()
    settle(proxy, lambda: ("rename", chat.remote_id, "Planned", "/project") in proxy.store.calls)
    assert not any(call[0] == "title" for call in proxy.store.calls)


def test_tool_call_permission_and_result(proxy):
    async def script(client, text):
        decision = await client.can_use_tool("Bash", {"command": "rm -rf build"},
                                             ToolPermissionContext(tool_use_id="req1", title="Claude wants to run a command"))
        assert isinstance(decision, PermissionResultAllow)
        return [
            AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "rm -rf build"})],
                             model="claude", message_id="m2", uuid="a2"),
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="removed")], uuid="u9"),
            AssistantMessage(content=[TextBlock(text="Done.")], model="claude", message_id="m2", uuid="a3"),
            result(client.session_id),
        ]
    FakeClient.script = script
    chat = proxy["old"]
    settle(proxy, lambda: chat.loaded)
    chat["messages"]["local"] = {"role": "user", "text": "clean"}
    proxy.reconcile()
    settle(proxy, lambda: "req1" in chat["requests"])
    request = chat["requests"]["req1"]
    assert request["kind"] == "approval" and "rm -rf build" in request["text"]
    assert chat["running"]
    request["answer"] = {"decision": "accept"}
    proxy.reconcile()
    settle(proxy, lambda: not chat["running"])
    assert "req1" not in chat["requests"]
    command = chat["messages"]["t1"]
    assert isinstance(command, CommandExecution)
    assert str(command["content"]["output"]) == "removed" and command["status"] == "completed"
    assert input_text(chat["messages"]["m2:0"]) == "Done."
    client, = FakeClient.instances
    assert not client.fresh and client.session_id == "old"


def test_declined_permission(proxy):
    outcome = {}

    async def script(client, text):
        outcome["decision"] = await client.can_use_tool("Edit", {"file_path": "/project/a.py"},
                                                        ToolPermissionContext(tool_use_id="req2"))
        return [result(client.session_id)]
    FakeClient.script = script
    chat = proxy["old"]
    settle(proxy, lambda: chat.loaded)
    chat["messages"]["local"] = {"role": "user", "text": "edit"}
    proxy.reconcile()
    settle(proxy, lambda: "req2" in chat["requests"])
    assert "/project/a.py" in chat["requests"]["req2"]["text"]
    chat["requests"]["req2"]["answer"] = {"decision": "decline"}
    proxy.reconcile()
    settle(proxy, lambda: not chat["running"])
    assert isinstance(outcome["decision"], PermissionResultDeny)


def test_ask_user_question(proxy):
    outcome = {}

    async def script(client, text):
        outcome["decision"] = await client.can_use_tool("AskUserQuestion", {"questions": [
            {"question": "Which colour?", "header": "Theme", "options": [{"label": "Red"}, {"label": "Blue"}]}]},
            ToolPermissionContext(tool_use_id="q1"))
        return [result(client.session_id)]
    FakeClient.script = script
    chat = proxy["old"]
    settle(proxy, lambda: chat.loaded)
    chat["messages"]["local"] = {"role": "user", "text": "ask"}
    proxy.reconcile()
    settle(proxy, lambda: "q1" in chat["requests"])
    request = chat["requests"]["q1"]
    assert request["kind"] == "input"
    assert request["data"]["questions"][0]["id"] == "Which colour?"
    assert "Red, Blue" in request["data"]["questions"][0]["question"]
    request["answer"] = {"answers": {"Which colour?": {"answers": ["Blue"]}}}
    proxy.reconcile()
    settle(proxy, lambda: not chat["running"])
    assert outcome["decision"].updated_input["answers"] == {"Which colour?": "Blue"}


def test_stop_interrupts_without_error(proxy):
    async def script(client, text):
        return [StreamEvent(uuid="e1", session_id=client.session_id,
                            event={"type": "message_start", "message": {"id": "m3"}})]  # never completes
    FakeClient.script = script
    chat = proxy["old"]
    settle(proxy, lambda: chat.loaded)
    chat["messages"]["local"] = {"role": "user", "text": "long task"}
    proxy.reconcile()
    settle(proxy, lambda: chat.turn_id is not None)
    chat["running"] = False
    proxy.reconcile()
    settle(proxy, lambda: chat.turn_id is None and "interrupt" not in chat.inflight)
    client, = FakeClient.instances
    assert client.interrupts == 1
    assert chat.error is None


def test_error_result_surfaces(proxy):
    async def script(client, text):
        return [result(client.session_id, is_error=True, subtype="error_during_execution", text="boom")]
    FakeClient.script = script
    chat = proxy["old"]
    settle(proxy, lambda: chat.loaded)
    chat["messages"]["local"] = {"role": "user", "text": "x"}
    proxy.reconcile()
    settle(proxy, lambda: not chat["running"])
    assert chat.error == "boom"


def test_remove_hides_session_and_closes_process(proxy):
    chat = proxy["old"]
    settle(proxy, lambda: chat.loaded)
    chat["messages"]["local"] = {"role": "user", "text": "hi"}
    proxy.reconcile()
    settle(proxy, lambda: not chat["running"])
    del proxy["old"]
    proxy.reconcile()
    settle(proxy, lambda: "old" not in proxy.known)
    assert ("hide", "old", "/project") in proxy.store.calls
    assert FakeClient.instances[0].closed
    # A never-started conversation has no session file to hide.
    proxy["fresh"] = {"project": "/project"}
    proxy.reconcile()
    settle(proxy, lambda: proxy["fresh"].remote_id is not None)
    del proxy["fresh"]
    proxy.reconcile()
    settle(proxy, lambda: "fresh" not in proxy.known)
    assert sum(call[0] == "hide" for call in proxy.store.calls) == 1


def test_idle_processes_release_sessions_for_other_clients(proxy):
    proxy.live_clients = 1
    for key in ("a", "b"):
        proxy[key] = {"project": "/project"}
    proxy.reconcile()
    settle(proxy, lambda: all(proxy[k].remote_id for k in ("a", "b")))
    for key in ("a", "b"):
        proxy[key]["messages"]["local"] = {"role": "user", "text": key}
        proxy.reconcile()
        settle(proxy, lambda: not proxy[key]["running"])
    first, second = FakeClient.instances
    assert first.closed and second.closed


def test_tool_message_mapping():
    edit = tool_message("Edit", {"file_path": "/p/a.py", "old_string": "x = 1\n", "new_string": "x = 2\ny = 3\n"}, "/p", {})
    assert isinstance(edit, FileChange)
    entry = edit["summary"]["/p/a.py"]
    assert (entry["added"], entry["removed"]) == (2, 1)
    write = tool_message("Write", {"file_path": "/p/new.txt", "content": "one\ntwo\n"}, "/p", {})
    assert write["summary"]["/p/new.txt"]["added"] == 2
    read = tool_message("Read", {"file_path": "/p/a.py"}, "/p", {})
    assert isinstance(read, DynamicToolCall) and read["summary"]["/p/a.py"]["access"] == "read"
    assert str(read["details"]["tool"]) == "Read"
    mcp = tool_message("mcp__github__list_issues", {"repo": "x"}, "/p", {})
    assert isinstance(mcp, McpToolCall) and str(mcp["details"]["server"]) == "github"
    bash = tool_message("Bash", {"command": "cat /p/a.py > /p/b.py", "description": "copy"}, "/p", {})
    assert bash["summary"]["/p/b.py"]["access"] == "write"


def test_listing_and_turn_stamp_activity(proxy):
    chat = proxy["old"]
    assert chat["updated"] == 0.0          # the fake listing carries no timestamp
    settle(proxy, lambda: chat.loaded)
    before = time.time()
    chat["messages"]["u9"] = {"role": "user", "content": "ping"}
    proxy.reconcile()
    assert chat["updated"] >= before       # send stamps it
    settle(proxy, lambda: not chat["running"])
    assert chat["updated"] >= before       # the turn's events keep it current


def test_session_store_rows_carry_last_modified_seconds(monkeypatch):
    from meltygui.chat import claude_code
    from types import SimpleNamespace
    info = SimpleNamespace(session_id="s", cwd="/p", custom_title=None, summary="Sum", first_prompt="hi",
                           tag=None, last_modified=1789262896784)
    monkeypatch.setattr(claude_code, "_sdk", lambda: SimpleNamespace(list_sessions=lambda limit: [info]))
    rows = claude_code.SessionStore().list()
    assert rows[0]["updated"] == 1789262896.784


def test_images_in_user_messages_and_tool_results():
    from meltygui.chat.messages import ImageReference
    from meltygui.chat import images as chat_images
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    store = FakeStore()
    store.sessions.append({"id": "pics", "project": "/project", "title": "Pictures", "hidden": False})
    store.history["pics"] = [
        ("user", "u1", {"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}]}, None),
        ("assistant", "a1", {"id": "m1", "role": "assistant", "content": [
            {"type": "tool_use", "id": "read1", "name": "Read", "input": {"file_path": "/project/shot.png"}}]}, None),
        ("user", "u2", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "read1", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}]}]}, None),
    ]
    proxy = ClaudeCodeChats("anthropic", ChatMetadata(), store=store, client_factory=FakeClient, live_clients=2)
    settle(proxy, lambda: not proxy.loading)
    chat = proxy["pics"]
    settle(proxy, lambda: chat.loaded)
    user = chat["messages"]["u1"]
    assert input_text(user).strip() == "What is this?"
    image = user["content"]["image"]
    assert isinstance(image, ImageReference) and image["media_type"] == "image/png"
    assert chat_images.image_key(image).startswith("data:")
    read = chat["messages"]["read1"]
    assert isinstance(read["content"]["image"], ImageReference)
    assert chat_images.image_key(read["content"]["image"]) == chat_images.image_key(image)  # same picture, one decode
    proxy.close()
    proxy.worker.join(3)


def test_refresh_lights_sessions_written_elsewhere_and_lists_new_ones(proxy):
    store = proxy.store
    chat = proxy["old"]
    assert chat["updated"] == 0.0
    store.activity = lambda: {"old": 1_700_000_000.0}
    proxy.refresh()
    settle(proxy, lambda: chat["updated"] == 1_700_000_000.0)
    # a session file this window never listed: a terminal started it
    store.sessions.append({"id": "terminal", "project": "/project", "title": "From a terminal", "hidden": False,
                           "updated": 1_700_000_100.0})
    store.activity = lambda: {"old": 1_700_000_000.0, "terminal": 1_700_000_100.0}
    proxy.refresh()
    settle(proxy, lambda: "terminal" in proxy)
    assert proxy["terminal"]["title"] == "From a terminal" and proxy["terminal"]["updated"] == 1_700_000_100.0


def test_activity_on_an_open_session_reads_it_again(proxy):
    store = proxy.store
    chat = proxy["old"]
    settle(proxy, lambda: chat.loaded)
    assert "u9" not in chat["messages"]
    store.history["old"].append(("user", "u9", {"role": "user", "content": "typed in a terminal"}, None))
    store.history["old"].append(("assistant", "a9", {"id": "msg9", "role": "assistant",
                                                    "content": [{"type": "text", "text": "answered there"}]}, None))
    store.activity = lambda: {"old": 1_700_000_000.0}
    proxy.refresh()
    settle(proxy, lambda: "msg9:0" in chat["messages"])
    assert input_text(chat["messages"]["u9"]) == "typed in a terminal"
    assert not chat.refreshing and chat.loaded


def test_tail_reads_only_appended_rows(tmp_path, monkeypatch):
    import json
    from meltygui.chat.claude_code import SessionStore
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    session_dir = tmp_path / "projects" / "-tmp"
    session_dir.mkdir(parents=True)
    path = session_dir / "abc.jsonl"
    rows = [{"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "uuid": "a1", "message": {"id": "m1", "role": "assistant",
                                                             "content": [{"type": "text", "text": "hello"}]}},
            {"type": "attachment", "uuid": "x1", "attachment": {}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"type": "assistant", "uuid": "a2", "mess')
    store = SessionStore()
    assert store.size("abc") == path.stat().st_size
    got, offset = store.tail("abc", 0)
    assert [r[1] for r in got] == ["u1", "a1"]           # user / assistant only, the partial line waits
    assert offset == len("".join(json.dumps(r) + "\n" for r in rows))
    with open(path, "a") as handle:
        handle.write('age": {"id": "m2", "role": "assistant", "content": [{"type": "text", "text": "more"}]}}\n')
    got, offset2 = store.tail("abc", offset)
    assert [r[1] for r in got] == ["a2"] and offset2 == path.stat().st_size
    path.write_text("")
    assert store.tail("abc", offset2) is None             # rewritten: read it whole again


def test_refresh_does_not_relist_unchanged_sessions_outside_limit(proxy):
    store = proxy.store
    calls = []
    original = store.list
    store.list = lambda: (calls.append(1), original())[1]
    store.activity = lambda: {'old': 10.0, 'outside-list': 20.0}
    proxy.execute('refresh')
    proxy.drain()
    assert len(calls) == 1
    for _ in range(3):
        proxy.execute('refresh')
        proxy.drain()
    assert len(calls) == 1
    # An old unlisted chat becoming active must still be discovered.
    store.activity = lambda: {'old': 10.0, 'outside-list': 21.0}
    proxy.execute('refresh')
    assert len(calls) == 2


def test_inactive_external_updates_refresh_on_return(proxy):
    chat = proxy['old']
    settle(proxy, lambda: chat.loaded)
    proxy.active_key = None  # another source is displayed
    proxy.store.calls.clear()
    proxy.store.history['old'].append(('user', 'external', {'content': 'From the CLI'}, None))
    proxy.receive('activity', {'old': 1_700_000_000.0})
    assert chat.pending_history == 'tail'
    assert not chat.refreshing
    assert 'external' not in chat['messages']
    assert proxy.store.calls == []
    assert proxy['old'] is chat
    settle(proxy, lambda: 'external' in chat['messages'])
    assert input_text(chat['messages']['external']) == 'From the CLI'


def test_claude_full_access_and_model_options(proxy):
    from meltygui.chat.messages import user_message
    received = []
    def factory(*args, **kwargs):
        received.append(dict(kwargs))
        kwargs.pop('model', None)
        kwargs.pop('permission_mode', None)
        return FakeClient(*args, **kwargs)
    proxy.client_factory = factory
    chat = proxy['old']
    settle(proxy, lambda: chat.loaded)
    chat.metadata.update(model='opus', permissions='full')
    chat['messages']['send-full'] = user_message('hello')
    proxy.reconcile()
    settle(proxy, lambda: 'send-full' in chat.sent and not chat['running'])
    assert received[0]['model'] == 'opus'
    assert received[0]['permission_mode'] == 'bypassPermissions'


def test_model_catalog_is_discovered_for_account():
    class CatalogClient(FakeClient):
        @staticmethod
        async def discover_models(env=None):
            assert env == {"ANTHROPIC_BASE_URL": "https://example.test"}
            return [{"displayName": "Server model", "value": "server-model-id"}]
    chats = ClaudeCodeChats("anthropic", ChatMetadata(), store=FakeStore(),
        client_factory=CatalogClient, account={"base_url": "https://example.test"})
    try:
        settle(chats, lambda: bool(chats.models))
        assert chats.models == {"Server model": "server-model-id"}
    finally:
        chats.close()
        chats.worker.join(3)


def test_model_discovery_failure_keeps_conversations_available():
    class UnavailableClient(FakeClient):
        @staticmethod
        async def discover_models(env=None):
            raise RuntimeError("catalog unavailable")
    chats = ClaudeCodeChats("anthropic", ChatMetadata(), store=FakeStore(), client_factory=UnavailableClient)
    try:
        settle(chats, lambda: chats.models_error is not None)
        assert chats.models == {}
        assert chats.models_error == "catalog unavailable"
        assert not chats.loading and chats.error is None
        assert "old" in chats
    finally:
        chats.close()
        chats.worker.join(3)


def test_claude_default_resolves_to_real_catalog_option(proxy):
    proxy.receive("models", [
        {"value": "default", "displayName": "Default (recommended)", "resolvedModel": "actual-model"},
        {"value": "model-alias", "displayName": "Actual model", "resolvedModel": "actual-model"}])
    assert proxy.default_model == "model-alias"
    assert proxy.models == {"Actual model": "model-alias"}


def test_history_model_replaces_default_but_preserves_explicit_selection(proxy):
    chat = proxy['old']
    chat.metadata['model'] = 'opus'
    proxy._history(chat, 'assistant', 'fable', {'model': 'claude-fable-5-1', 'content': []}, None)
    assert chat.metadata['model'] == 'claude-fable-5-1'
    proxy._history(chat, 'assistant', 'subagent', {'model': 'haiku', 'content': []}, 'parent-tool')
    assert chat.metadata['model'] == 'claude-fable-5-1'
    proxy._history(chat, 'assistant', 'synthetic', {'model': '<synthetic>', 'content': []}, None)
    assert chat.metadata['model'] == 'claude-fable-5-1'
    chat.metadata.update(model='sonnet', model_explicit=True)
    proxy._history(chat, 'assistant', 'old-output', {'model': 'claude-fable-5-1', 'content': []}, None)
    assert chat.metadata['model'] == 'sonnet'
    assert chat['model'] == 'claude-fable-5-1'


def test_external_session_state_updates_model_permissions_and_busy(proxy):
    chat = proxy['old']
    chat.metadata.update(model='haiku', model_explicit=True, permissions='ask')
    proxy.receive('session_states', {'old': {'model': 'claude-fable-5-1', 'model_at': 100,
        'permissions': 'full', 'permissions_at': 100, 'busy': True}})
    assert chat.metadata['model'] == 'claude-fable-5-1'
    assert chat.metadata['permissions'] == 'full'
    assert chat['external_busy']
    chat.metadata.update(model='sonnet', model_selected_at=200)
    proxy.receive('session_states', {'old': {'model': 'claude-fable-5-1', 'model_at': 100, 'busy': False}})
    assert chat.metadata['model'] == 'sonnet'
    assert not chat['external_busy']


def test_claude_effort_applies_to_each_turn(proxy, monkeypatch):
    monkeypatch.setattr("meltygui.chat.claude_code.configured_effort", lambda *args: "medium")
    from meltygui.chat.messages import user_message
    received = []
    def factory(*args, **kwargs):
        received.append(kwargs.pop('effort', None))
        kwargs.pop('model', None)
        kwargs.pop('permission_mode', None)
        return FakeClient(*args, **kwargs)
    proxy.client_factory = factory
    chat = proxy['old']
    settle(proxy, lambda: chat.loaded)
    for index, effort in enumerate(('high', 'low', 'default')):
        chat.metadata['effort'] = effort
        message_id = f'effort-{index}'
        chat['messages'][message_id] = user_message('hello')
        proxy.reconcile()
        settle(proxy, lambda: message_id in chat.sent and not chat['running'])
    assert received == ['high', 'low', 'medium']


def test_sdk_effort_option():
    from meltygui.chat.claude_code import SdkClient
    client = SdkClient('test-session', '/tmp', True, None, effort='high')
    assert client.options.effort == 'high'


def test_configured_effort_uses_per_model_settings(tmp_path, monkeypatch):
    import json
    from meltygui.chat.claude_code import configured_effort
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    monkeypatch.delenv('CLAUDE_CODE_EFFORT_LEVEL', raising=False)
    (tmp_path / 'settings.json').write_text(json.dumps({
        'effortLevel': 'xhigh', 'modelSettings': {'claude-fable-5-1': {'effortLevel': 'medium'}}}))
    assert configured_effort('', 'claude-fable-5-1[1m]') == 'medium'
    assert configured_effort('', 'claude-opus-5') == 'xhigh'
    monkeypatch.setenv('CLAUDE_CODE_EFFORT_LEVEL', 'low')
    assert configured_effort('', 'claude-fable-5-1') == 'low'
