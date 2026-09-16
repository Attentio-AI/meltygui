"""Chat core functions and supporting definitions."""



def _cleanup_chat(draw_state):
    """The window is going: close idle sessions. One with a turn streaming
    stays up (its worker and process keep going, the session file fills in)
    — the studio reuses it when the window reopens; an app waits for it
    before exiting (melty_claude's `finish_turns`)."""
    import meltygui.accounts.internet_accounts as internet_accounts

    for account in internet_accounts.accounts.values():
        proxy = account.get("_chat_proxy")
        if proxy is None:
            continue
        if any(chat["running"] for chat in proxy.values()):
            continue
        account.pop("_chat_proxy", None)
        proxy.close()
