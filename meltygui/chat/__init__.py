"""meltygui.chat — the code-assistant chat: the window, the data contract a
backend implements, and the registry that plugs a backend into a provider.

    from meltygui.chat import draw_claude_chat

    draw_tiles(..., multi_instance_renderers=(draw_claude_chat,))   # a tile, or
    draw_claude_chat(None, width=w, height=h)                       # in a window body

`draw_claude_chat` is the chat ready for a window body or a tile (`python -m
meltygui.chat [PROJECT]` is it as the melty-claude app): a plain function, one
call of `draw_chat_interface` (the only render boundary) with folder tints from the file-meta store and its sidebar metadata kept between
runs. Its input value picks where new conversations start (`chat_project`).
Conversations run in the detached chat service (`chat_service.py`): a turn
outlives the window that sent it and every app shows the same live state.
Claude Code's backend (`claude_code.py`) needs the `meltygui[claude]` extra.

The window (`draw_chat_interface`, the studio's Chat playground) lists the
account kinds that carry a `chat_label` in a provider dropdown, the accounts
of that kind next to it, and draws the selected account's conversations:
a project-grouped sidebar, the transcript, the composer, approval prompts.
Conversations come from a **ChatProxy** — a dict of `Chat`s the UI edits
directly (insert a dict to create, `del` to remove, set `title`, append a
user message to `chat["messages"]`, set `running=False` to stop; `updated`
is its last activity in epoch seconds, for the sidebar's age filter) and the
backend mirrors to its provider from a worker thread. `chat_proxy.py` is
the contract; `codex_proxy.py` in the same package is the reference
implementation, and its docstrings say what each operation and event means.

A backend registers a factory for an account kind name
(`accounts/internet_accounts.py` registers the chat service's mirror for
"codex" and "anthropic"; the service builds `CodexChats` / `ClaudeCodeChats`):

    from meltygui.chat import ChatProxy, Chat, messages, register_chat_backend

    @register_chat_backend('my-kind')
    def my_chats(account, metadata=None, wake=None):
        return MyChats(account['id'], metadata, wake)

Pictures (`images`): an ImageReference in a message — a base64 payload, a
path or a data URL — is decoded once and drawn inline by the window, HDR
sources (PQ PNGs, PQ ICC profiles) as RGB16F so an HDR desktop shows them.

Message values (`messages`: UserMessage, AssistantMessage, ReasoningMessage,
CommandExecution, FileChange, ... and `set_text`, `upsert`, `text_blocks`)
are provider-neutral; the UI selects a view by type, so a backend only maps
its events onto them.

Light names (the contract, the registry, the message model) import
eagerly; the window, the accounts store and the metadata mirror
(`ChatMetadata`, `shared_metadata`, `persistent_metadata`) load lazily on
first access, after meltygui's import thread, like the views in `meltygui` itself.
"""
from typing import TYPE_CHECKING

from meltygui.chat import messages
import meltygui.chat.images as images   # message pictures: image_key, ImageCache, fitted_size
from meltygui.chat.backends import CHAT_BACKENDS
from meltygui.chat.backends import chat_backend
from meltygui.chat.backends import register_chat_backend
from meltygui.chat.chat_proxy import Chat
from meltygui.chat.chat_proxy import ChatProxy

if TYPE_CHECKING:   # IDE and type checkers only; never executed
    from meltygui.chat.metadata import ChatMetadata
    from meltygui.chat.metadata import persistent_metadata
    from meltygui.chat.metadata import shared_metadata
    from meltygui.view.chat_view import draw_chat_interface
    from meltygui.view.chat_view import draw_claude_chat
    from meltygui.chat.chat_interface import stop_running
    from meltygui.chat.chat_interface import disconnect_chats
    from meltygui.model.chat_model import chat_project
    from meltygui.state.chat_state import ChatInterfaceState
    from meltygui.accounts.internet_accounts import accounts
    from meltygui.accounts.internet_accounts import KINDS
    from meltygui.accounts.internet_accounts import AccountKind
    from meltygui.accounts.internet_accounts import account_kind

_LAZY = {
    'ChatMetadata': ('meltygui.chat.metadata', 'ChatMetadata'),
    'shared_metadata': ('meltygui.chat.metadata', 'shared_metadata'),
    'persistent_metadata': ('meltygui.chat.metadata', 'persistent_metadata'),
    'draw_chat_interface': ('meltygui.chat.chat_interface', 'draw_chat_interface'),
    'draw_claude_chat': ('meltygui.view.chat_view', 'draw_claude_chat'),
    'stop_running': ('meltygui.chat.chat_interface', 'stop_running'),
    'disconnect_chats': ('meltygui.chat.chat_interface', 'disconnect_chats'),
    'chat_project': ('meltygui.model.chat_model', 'chat_project'),
    'ChatInterfaceState': ('meltygui.chat.chat_interface', 'ChatInterfaceState'),
    'accounts': ('meltygui.accounts.internet_accounts', 'accounts'),
    'KINDS': ('meltygui.accounts.internet_accounts', 'KINDS'),
    'AccountKind': ('meltygui.accounts.internet_accounts', 'AccountKind'),
    'account_kind': ('meltygui.accounts.internet_accounts', 'account_kind'),
}


def __getattr__(name):
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(name)
    import importlib
    from meltygui.core.runtime.app import _wait_imports
    _wait_imports()
    value = getattr(importlib.import_module(spec[0]), spec[1])
    globals()[name] = value
    return value


__all__ = ['messages', 'images', 'Chat', 'ChatProxy', 'CHAT_BACKENDS', 'chat_backend', 'register_chat_backend', *_LAZY]
