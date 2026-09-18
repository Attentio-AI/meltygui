"""The chat backend registry: account kind name -> ChatProxy factory.

A backend is a ChatProxy subclass living anywhere (the studio's Codex one,
an external package). Registering it against an
account kind gives that kind's accounts a conversation list in the Chat
window; nothing else in the UI knows which package supplied it.

    from meltygui.chat import register_chat_backend

    def claude_chats(account, metadata=None, wake=None):
        return ClaudeCodeChats(account["id"], metadata, wake, account=account)

    register_chat_backend("anthropic", claude_chats)

The factory receives the account dict (its ``id`` and fields), the shared
ChatMetadata (or None for the default) and the wake callback; it returns a
ChatProxy, or None to leave the kind blank for now (signing in, busy).
This module imports nothing heavy so backends can register at import time.
"""

CHAT_BACKENDS = {}


def register_chat_backend(kind_name, factory=None):
    """Register ``factory(account, metadata=None, wake=None) -> ChatProxy | None``
    for the account kind named ``kind_name`` ("anthropic", "codex", ...).
    Re-registering replaces the previous factory. Usable as a decorator:
    ``@register_chat_backend("anthropic")`` over the factory."""
    if factory is None:
        return lambda fn: register_chat_backend(kind_name, fn)
    CHAT_BACKENDS[kind_name] = factory
    return factory


def chat_backend(kind_name):
    return CHAT_BACKENDS.get(kind_name)
