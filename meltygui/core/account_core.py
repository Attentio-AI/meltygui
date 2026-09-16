"""Account core functions and supporting definitions."""



def _cleanup_accounts(draw_state):
    from meltygui.accounts.internet_accounts import KINDS
    from meltygui.accounts.internet_accounts import accounts

    for entry in list(accounts.values()):
        KINDS[entry["kind"]].close(entry)
