"""meltygui.chat — the code-assistant chat: the window, the data contract a
backend implements, and the registry that plugs a backend into a provider.

    import meltygui
    from meltygui.chat import draw_chat_interface, persistent_metadata
    import melty_agents                       # registers the Claude Code backend

    persistent_metadata('~/.cache/my-app/chat_metadata.json')
    meltygui.glfw_window(name='Chat', app_id='my-app')(draw_chat_interface)

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

A backend registers a factory for an account kind name:

    from meltygui.chat import ChatProxy, Chat, messages, register_chat_backend

    @register_chat_backend('anthropic')     # the "Claude Code" provider
    def claude_chats(account, metadata=None, wake=None):
        return ClaudeCodeChats(account['id'], metadata, wake, account=account)

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
    from meltygui.chat.chat_interface import draw_chat_interface
    from meltygui.chat.chat_interface import ChatInterfaceState
    from meltygui.accounts.internet_accounts import accounts
    from meltygui.accounts.internet_accounts import KINDS
    from meltygui.accounts.internet_accounts import AccountKind
    from meltygui.accounts.internet_accounts import account_kind

_LAZY = {
    'ChatMetadata': ('meltygui.chat.metadata', 'ChatMetadata'),
    'shared_metadata': ('meltygui.chat.metadata', 'shared_metadata'),
    'persistent_metadata': ('meltygui.chat.metadata', 'persistent_metadata'),
    'draw_chat_interface': ('meltygui.chat.chat_interface', 'draw_chat_interface'),
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
