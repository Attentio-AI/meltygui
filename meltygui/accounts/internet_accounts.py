"""Internet Accounts — one window to manage every login the studio's
network features use (Anthropic browser sign-in — the OAuth login `ant auth
login` does, see fim_providers/anthropic_oauth.py — or a pasted API key,
GitHub Copilot device-flow sign-in, Ollama host + model placement; the Anthropic row also
shows the Claude plan's usage limits, read through Claude Code's login),
modelled on fast_dock: rows are plain
draw-list rects/text with manual hit-testing, hover boosts + clicks resolve
inside the body while the view is hovered (the wrapper repaints every frame
then), and the idle tile is a cached blit that background probes repaint
via `accounts_changed()`.

Model: `accounts` (AccountStore, a dict id → account dict) persisted to
`~/.lsd/accounts.json` (0600 — it holds secrets). An account has a `kind`
(one of the registered `@account_kind` classes) and the kind's fields.
Providers resolve credentials through `account_field(kind, id, field)` —
`ClaudeSession(account="work")`, `CopilotSession(account="home")`, … — so
a FIM profile selects an account by name and two accounts of one kind are
two live sessions. A kind's DEFAULT account has id == kind name; sessions
asking for account="default" resolve to it.

Adding a kind: subclass AccountKind, decorate with @account_kind. The window
renders whatever is registered — the kind supplies its status probe, its
editable fields, its action buttons and any extra rows (a device-code card,
the Ollama model list). A kind with a `chat_label` appears in the Chat
window's provider dropdown; its conversations come from the ChatProxy
factory registered for the kind name with `register_chat_backend`
(chat/backends.py) — the studio registers Codex's below, an external
package (melty_agents) registers Claude Code's against "anthropic".

Layout: every row measures its buttons FIRST (`strip_layout`); if the text
would be left less than min_text_width, or the strip is wider than the row,
the buttons wrap onto as many right-aligned lines as they need under the
text (`pack_buttons`; the row grows — card and model sub-rows do the same),
otherwise status text is ellipsized to what's left. Kind headers, notes and
field labels ellipsize; usage names stay complete and wrap above their
bars in narrow windows — so nothing overlaps at any window width.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.melty import Melty
from meltygui.chat.backends import chat_backend
from meltygui.chat.backends import register_chat_backend
from meltygui.state.dict_conversion import DictConversion
from meltygui.utils.glfw_utils import request_render
from meltygui.core.tile_cache import add_shadow
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.window_decoration import window

# Where the store lives on disk - a data file, not a styling knob; shared
# by the store methods, the footer row, and the tests' monkeypatch.
ACCOUNTS_PATH = Path.home() / ".lsd" / "accounts.json"


# ──────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────

from meltygui.model.account_model import AccountStore


accounts = AccountStore()

# Hotswap-safe: keep the live store object (and its file) across re-exec.
# Sits right after the fresh instance so everything below - the KINDS default
# fill and the @window registration included - binds the LIVE store, never
# the throwaway one this re-exec just built.
_previous_store = Melty.__dict__.get("_internet_accounts_store")
if _previous_store is not None and _previous_store is not accounts:
    accounts = _previous_store
Melty._internet_accounts_store = accounts


def is_default(account) -> bool:
    """The kind's default row (`AccountStore.default_account`) — the top row."""
    return accounts.default_account(account.get("kind")) is account


def session_account_id(account) -> str:
    """The `account=` value a session uses for this account — "default" for
    a kind's default entry, so the UI and the providers' default profiles
    pool the SAME session."""
    return "default" if is_default(account) else account["id"]


def account(kind_name, account_id="default"):
    """The account dict for (kind, id), or None. "default" is the kind's
    default entry (id == kind name). Loads the store lazily."""
    if not accounts.loaded:
        accounts.load()
    if account_id in ("default", None, ""):
        return accounts.default_account(kind_name)
    entry = accounts.get(account_id)
    if entry is not None and entry.get("kind") == kind_name:
        return entry
    return None


def account_field(kind_name, account_id, field, default=None):
    entry = account(kind_name, account_id)
    if entry is None:
        return default
    value = entry.get(field)
    return value if value not in (None, "") else default


def accounts_changed():
    """Repaint the window (from any thread)."""
    try:
        Melty.cache.invalidate_up_by_obj(accounts, force=True)
    except Exception:
        pass
    try:
        from meltygui.completion.fim import _wake
        _wake(_window_draw_state)
    except Exception:
        pass


_window_draw_state = None   # draw_internet_accounts' draw_state — the wake target


def _drop_sessions_for(account_entry):
    """Close pooled FIM sessions built on this account so the next request
    re-acquires with the new credential."""
    ids = {account_entry.get("id"), session_account_id(account_entry)}
    try:
        import meltygui.completion.fim as fim
        fim.drop_sessions(lambda session: getattr(session, "account", None) in ids
                          and getattr(session, "KIND", None) == account_entry.get("kind"))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Kinds
# ──────────────────────────────────────────────────────────────────────────

class Field:
    __slots__ = ("name", "label", "secret", "default", "placeholder", "hidden")

    def __init__(self, name, label, secret=False, default="", placeholder="", hidden=False):
        self.name = name
        self.label = label
        self.secret = secret
        self.default = default
        self.placeholder = placeholder
        self.hidden = hidden        # set by a control, not by the Edit rows


class Button:
    """One action on a row. `icon` draws a square icon-only button (with
    `tip` as the hover hint); `label` a text button."""
    __slots__ = ("label", "on_click", "primary", "icon", "tip", "enabled", "danger")

    def __init__(self, label, on_click, primary=False, icon=None, tip="", enabled=True, danger=False):
        self.label = label
        self.on_click = on_click
        self.primary = primary
        self.icon = icon
        self.tip = tip
        self.enabled = enabled
        self.danger = danger


KINDS = {}


def account_kind(cls):
    """Register an AccountKind subclass (its `name` is the kind key)."""
    KINDS[cls.name] = cls()
    return cls


class AccountKind:
    chat_label = None

    @property
    def chat_available(self):
        """A backend is registered for this kind (chat/backends.py)."""
        return chat_backend(self.name) is not None

    def chat_proxy(self, account, metadata=None, wake=None):
        """The kind's conversations: the registered backend's proxy, or None
        (no backend, or the backend declines for now — signing in, busy)."""
        factory = chat_backend(self.name)
        return factory(account, metadata, wake) if factory is not None else None

    def chats(self, account, wake=None):
        proxy = account.get("_chat_proxy")
        if proxy is not None and getattr(proxy, "session_version", None) != 3:
            self.close_chat(account)
            proxy = None
        if proxy is None or proxy.closed:
            proxy = self.chat_proxy(account, wake=wake)
            if proxy is not None:
                account["_chat_proxy"] = proxy
        return proxy

    def close_chat(self, account):
        proxy = account.pop("_chat_proxy", None)
        if proxy is not None:
            proxy.close()

    name = "base"
    label = "Account"
    icon = ""
    tint = (0.5, 0.5, 0.55)
    fields = ()

    def close(self, account):
        """Release account-owned background work on removal / cleanup."""
        self.close_chat(account)

    def default_label(self, account_id):
        return self.label if account_id == self.name else f"{self.label} ({account_id})"

    def status(self, account):
        """(state, text) from the cached probe; state is one of the
        state_tints keys in draw_internet_accounts ("ready", "busy",
        "needs_login", "warning", "error", "unknown")."""
        status = account.get("_status")
        if status is None:
            return ("unknown", "…")
        return status

    def probe(self, account):
        """Worker thread: return (state, text). May touch the network."""
        return ("unknown", "")

    def actions(self, account):
        """[Button] — buttons on the row, left to right."""
        return []

    def sub_rows(self, account):
        """Extra rows under the account: ("card", (message, [Button…])) —
        a highlighted strip with its own buttons (a device code, a
        browser sign-in in progress) | ("model", model dict) |
        ("note", text)."""
        return []


@account_kind
class AnthropicKind(AccountKind):
    chat_label = "Claude Code"
    name = "anthropic"
    label = "Anthropic"
    icon = f""
    tint = (0.85, 0.55, 0.35)
    fields = (Field("api_key", "API key", secret=True,
                    placeholder="sk-ant-… (optional — Sign in needs no key)"),
              Field("profile", "Login profile", placeholder="(lsd)"),
              Field("base_url", "Base URL", placeholder="(default)"),
              Field("claude_code_login", "Claude Code login", default="~/.claude/.credentials.json",
                    placeholder="~/.claude/.credentials.json — the plan's usage limits are read through it"))

    # -- Credential sources ------------------------------------------------
    # Precedence, highest first: a pasted key → the browser sign-in (the
    # account's SDK profile) → env vars → an active `ant auth login`
    # profile. Only the default account reads the env / active profile.

    @staticmethod
    def _profile_present():
        """An ACTIVE `ant auth login` profile a bare Anthropic() picks up on
        its own. The account's own sign-in is `login_info`, not this."""
        from meltygui.completion.providers.anthropic_oauth import active_profile_present
        return active_profile_present()

    @staticmethod
    def profile_name(account) -> str:
        """The SDK profile this account signs in to: its `profile` field,
        else Toggles.InternetAccounts.anthropic_profile ("lsd") for the
        account NAMED after the kind and "<that>-<account id>" for the
        others — keyed on the id, not on default-ness, so a row promoted to
        default (the kind-named one removed) keeps its profile files."""
        from meltygui.toggles import Toggles
        name = (account.get("profile") or "").strip()
        if name:
            return name
        base = Toggles.InternetAccounts.anthropic_profile
        return base if account.get("id") == account.get("kind") else f"{base}-{account['id']}"

    def login_info(self, account, fresh=False):
        """anthropic_oauth.read_profile() of the account's profile, cached
        on the account (`_login_info`) so the per-frame button layout never
        touches the disk; probes, sign-in and sign-out refresh it."""
        if fresh or "_login_info" not in account:
            import meltygui.completion.providers.anthropic_oauth as anthropic_oauth
            account["_login_info"] = anthropic_oauth.read_profile(self.profile_name(account))
        return account["_login_info"]

    def client_kwargs(self, account):
        """`anthropic.Anthropic(**kwargs)` for this account — the pasted
        `api_key` wins, else the signed-in `profile`, else nothing (the
        SDK's own env / active-profile chain); plus `base_url`. No SDK
        import — shared with the FIM provider (claude.account_client_kwargs)."""
        out = {}
        key = account.get("api_key") or ""
        info = self.login_info(account)
        if info is None:
            # A sign-in may have landed since the last probe (one stat, and
            # this runs at session construction, never per-frame).
            info = self.login_info(account, fresh=True)
        if key:
            out["api_key"] = key
        elif info is not None:
            out["profile"] = self.profile_name(account)
        if account.get("base_url"):
            out["base_url"] = account["base_url"]
        return out

    def _source(self, account):
        import meltygui.completion.providers.anthropic_oauth as anthropic_oauth
        key = account.get("api_key") or ""
        if key:
            return f"key …{key[-4:]}"
        info = self.login_info(account)
        if info is not None:
            return anthropic_oauth.summary(info)
        if is_default(account) and os.environ.get("ANTHROPIC_API_KEY"):
            return "env ANTHROPIC_API_KEY"
        if is_default(account) and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return "env ANTHROPIC_AUTH_TOKEN"
        if is_default(account) and self._profile_present():
            return "ant auth profile"
        return None

    def status(self, account):
        flow = account.get("_login")
        if flow is not None and not flow.done:
            return ("busy", "waiting for the browser…")
        return super().status(account)

    def probe(self, account):
        # Passive probe: NO network (and no `import anthropic`). Just report
        # whether a credential exists - the studio should not fire a web
        # request or import the SDK at startup just to show status. The
        # "Test" button (below) does the one real network check on demand.
        self.login_info(account, fresh=True)
        for sibling in accounts.of_kind(self.name):
            if sibling is not account:
                self.login_info(sibling, fresh=True)   # ownership of a shared Claude Code login reads their emails
        self._claude_login_for(account)
        source = self._source(account)
        if source is None:
            return ("needs_login", "not signed in")
        return ("ready", f"{source} · verified" if account.get("_validated") else source)

    # -- Claude plan usage (through Claude Code's login) ----------------------
    # The plan's rate-limit windows (session, weekly all-models, weekly
    # per-model - Fable - etc) and extra-usage spend come from GET
    # /api/oauth/usage, which only answers a claude.ai token: the Console
    # sign-in above is an API-org token and is refused ("Usage limits are
    # not applicable to API organizations"), so the row reads Claude Code's
    # OWN login file - read-only, never refreshed by the studio, a stale
    # token says "open Claude Code first". See fim_providers/claude_usage.py.

    def _claude_code_login_path(self, account):
        return str(Path(account.get("claude_code_login") or self.fields[-1].default).expanduser())

    def _claude_login_for(self, account):
        """Claude Code's login for this row's file, through
        claude_usage.cached_login (re-read only when the files change). When
        the identity behind the file changes — a switch by the
        Use-in-Claude-Code button or a `claude auth login` elsewhere — the
        numbers this row cached belonged to the previous account: drop them
        (`_forget_usage`), and ownership is re-decided on this very draw."""
        import meltygui.completion.providers.claude_usage as claude_usage
        login = claude_usage.cached_login(self._claude_code_login_path(account))
        previous = account.get("_claude_login")
        known = "_claude_login" in account
        account["_claude_login"] = login

        # An identity CHANGE = a different email (both known), or - with no
        # identity file at all - a different token. Never on a momentary
        # blank (a read mid-rewrite of ~/.claude.json), and never on a
        # same-account token refresh: both would blank the bars and refetch.
        old_email = (previous or {}).get("email") or ""
        new_email = (login or {}).get("email") or ""
        changed = False
        if old_email and new_email:
            changed = old_email.lower() != new_email.lower()
        elif not old_email and not new_email:
            changed = (previous or {}).get("token") != (login or {}).get("token")
        if known and changed:
            self._forget_usage(account)
        return login

    def _forget_usage(self, account):
        for key in ("_usage_rows", "_usage_summary", "_usage_fetched_wall", "_usage_error"):
            account.pop(key, None)
        account["_usage_fetched_at"] = None
        self._disarm_usage_fetch(account)

    # -- last session's numbers --------------------------------------------------
    # The bars persist in the window (AccountsPanelState.usage, keyed by
    # account id) so a fresh process shows the previous session's numbers
    # at once and the delayed fetch (usage_fetch_delay_s) updates them.

    def usage_cache_entry(self, account):
        """What the window persists for this row: the rows, the summary,
        when they were fetched and WHOSE they are (the login's email — a
        different login next session must not inherit them). None = nothing."""
        rows = account.get("_usage_rows")
        if not rows or not account.get("_usage_fetched_wall"):
            return None
        return {"rows": rows,
                "summary": account.get("_usage_summary") or "",
                "fetched_wall": account["_usage_fetched_wall"],
                "email": ((account.get("_claude_login") or {}).get("email") or "").lower()}

    def restore_usage(self, account, cached):
        """A fresh account dict (boot / restart / reload) takes the persisted
        numbers when they belong to the CURRENT Claude Code login (same
        email; a login with no identity file is trusted). Restored numbers
        are stale by definition: `_usage_fetched_at` stays None so an open
        panel arms the delayed fetch. Runs once per account dict."""
        if account.get("_usage_restored") or "_usage_rows" in account:
            return
        account["_usage_restored"] = True
        if not cached or not cached.get("rows"):
            return
        login = self._claude_login_for(account)
        if login is None:
            return
        login_email = (login.get("email") or "").lower()
        if login_email and login_email != (cached.get("email") or ""):
            return
        account["_usage_rows"] = list(cached["rows"])
        account["_usage_summary"] = cached.get("summary") or ""
        account["_usage_fetched_wall"] = cached.get("fetched_wall")
        account["_usage_fetched_at"] = None

    # -- Changing Claude Code's login -------------------------------------------
    # Claude Code holds ONE login per config dir; the Use-in-Claude-Code
    # button runs its own `claude auth login --email <this row's email>`
    # (claude_usage.ClaudeCodeLogin): the browser opens to the login page
    # with the email filled in, Claude Code's loopback callback completes it,
    # and its rewritten files flip the usage panel to this row on the next
    # draw. The Console page's paste-a-code fallback is covered by the card's
    # Paste code (clipboard → Claude Code's stdin).

    def switch_claude_code(self, account):
        import meltygui.completion.providers.claude_usage as claude_usage
        from meltygui.toggles import Toggles
        email = (self.login_info(account) or {}).get("email")
        if not email:
            return
        current = account.get("_claude_switch")
        if current is not None and not current.done:
            current.open_in_browser()
            return
        path = Path(self._claude_code_login_path(account))
        default_dir = Path(self.fields[-1].default).expanduser().parent
        login = claude_usage.ClaudeCodeLogin(
            email, executable=claude_usage.find_claude(Toggles.InternetAccounts.claude_code_bin),
            config_dir=None if path.parent == default_dir else path.parent,
            on_change=lambda flow: self._switch_changed(account, flow))
        account["_claude_switch"] = login
        account["_claude_switch_error"] = None
        try:
            login.start()
        except Exception as error:
            account["_claude_switch"] = None
            account["_claude_switch_error"] = str(error)[:120]
        accounts_changed()

    def _switch_changed(self, account, flow):
        """Worker thread: the URL arrived, or `claude auth login` finished."""
        if flow.done and account.get("_claude_switch") is flow:
            account["_claude_switch"] = None
            if flow.ok:
                # Claude Code rewrote its files: every row sharing them re-reads
                # on its next draw; drop cached numbers now so nothing stale paints.
                for entry in accounts.of_kind(self.name):
                    self._forget_usage(entry)
                    entry["_status"] = None
            else:
                account["_claude_switch_error"] = (flow.error or "sign-in failed")[:120]
        accounts_changed()

    def cancel_switch(self, account):
        flow = account.pop("_claude_switch", None)
        if flow is not None:
            flow.cancel()
        account["_claude_switch_error"] = None
        accounts_changed()

    def paste_switch_code(self, account):
        """The Console page showed a code (browser couldn't reach the loopback
        callback): clipboard → Claude Code's stdin."""
        flow = account.get("_claude_switch")
        try:
            code = (imgui.get_clipboard_text() or "").strip()
        except Exception:
            code = ""
        if flow is not None and code:
            flow.submit_code(code)

    def _owns_claude_login(self, account, login):
        """One Claude Code login file = one claude.ai account, and every
        Anthropic row points at the same default file — so rows sharing a
        file must not ALL paint its numbers (work + personal rows showing
        one account's bars twice). The file goes to the row whose sign-in
        email is the login's account email; when none matches, to the
        first row sharing that path (the default account)."""
        path = self._claude_code_login_path(account)
        sharing = [entry for entry in accounts.of_kind(self.name)
                   if self._claude_code_login_path(entry) == path]
        if len(sharing) <= 1:
            return True
        email = (login.get("email") or "").lower()
        if email:
            matching = [entry for entry in sharing
                        if ((self.login_info(entry) or {}).get("email") or "").lower() == email]
            if matching:
                return matching[0] is account
        return sharing[0] is account

    def refresh_all(self, account):
        """The Refresh button: re-read the logins and, for an open panel,
        fetch now — the one caller allowed under the request floor / a
        429 back-off (a person clicked)."""
        account["_usage_backoff_until"] = 0.0
        if account.get("_usage_open"):
            self.fetch_usage(account, force=True)
        refresh(account)

    def fetch_usage(self, account, force=False):
        """One GET /api/oauth/usage on a worker; rows land in `_usage_rows`,
        the compact summary in `_usage_summary`, an error in `_usage_error`
        (a note under the bars). Rate protection: never within
        usage_min_interval_s of the previous request (except `force`, the
        Refresh button) and never inside a 429 back-off window."""
        import meltygui.completion.providers.claude_usage as claude_usage
        from meltygui.toggles import Toggles
        if account.get("_usage_loading"):
            return
        now = time.monotonic()
        last_request = account.get("_usage_last_request")
        if not force and last_request is not None and now - last_request < Toggles.InternetAccounts.usage_min_interval_s:
            account["_usage_fetched_at"] = last_request       # keep the poller on the floor, not on top
            return
        if not force and now < account.get("_usage_backoff_until", 0.0):
            account["_usage_fetched_at"] = now
            return
        login = self._claude_login_for(account)
        account["_usage_fetched_at"] = now
        self._disarm_usage_fetch(account)      # a launch (Refresh) supersedes a pending delayed fetch
        if login is None or login["expired"]:
            account["_usage_rows"] = None
            account["_usage_error"] = ("sign in to Claude Code to view usage" if login is None
                                       else "Claude Code login expired — run claude")
            accounts_changed()
            return
        account["_usage_last_request"] = now   # stamp only when a request actually launches
        account["_usage_loading"] = True
        accounts_changed()

        def run():
            try:
                rows = claude_usage.parse_usage(claude_usage.fetch_usage(login["token"]))
                account["_usage_rows"] = rows
                account["_usage_error"] = None
                account["_usage_summary"] = claude_usage.summary(rows)
                account["_usage_fetched_wall"] = time.time()
            except claude_usage.UsageRateLimited as error:
                # back off: the server's Retry-After, or usage_backoff_s
                # doubling per repeat (reset by a successful fetch)
                streak = account.get("_usage_429_streak", 0) + 1
                account["_usage_429_streak"] = streak
                backoff = error.retry_after or min(
                    Toggles.InternetAccounts.usage_backoff_s * (2 ** (streak - 1)),
                    Toggles.InternetAccounts.usage_backoff_max_s)
                account["_usage_backoff_until"] = time.monotonic() + backoff
                account["_usage_error"] = f"rate limited — next try in {int(backoff // 60)} min"
            except Exception as error:
                account["_usage_error"] = str(error)[:120]
            else:
                account["_usage_429_streak"] = 0
                account["_usage_backoff_until"] = 0.0
            finally:
                account["_usage_loading"] = False
                account["_usage_fetched_at"] = time.monotonic()
            accounts_changed()

        threading.Thread(target=run, daemon=True, name=f"claude-usage-{account['id']}").start()

    def _arm_usage_fetch(self, account):
        """An automatic fetch never fires on the spot: it waits
        Toggles.InternetAccounts.usage_fetch_delay_s (a daemon Timer on
        `_usage_timer`, one per account) and fires only if the panel is
        still open then — so the persisted-open panel at boot shows last
        session's bars and a restart within the delay costs no request.
        A delay of 0 fetches at once (the tests' setting)."""
        from meltygui.toggles import Toggles
        delay = Toggles.InternetAccounts.usage_fetch_delay_s
        if delay <= 0:
            self.fetch_usage(account)
            return
        if account.get("_usage_timer") is not None:
            return

        def fire():
            if account.get("_usage_timer") is not timer:   # disarmed / re-armed meanwhile
                return
            account.pop("_usage_timer", None)
            if account.get("_usage_open"):
                self.fetch_usage(account)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        account["_usage_timer"] = timer
        timer.start()

    @staticmethod
    def _disarm_usage_fetch(account):
        timer = account.pop("_usage_timer", None)
        if timer is not None:
            timer.cancel()

    def _usage_rows(self, account):
        """The open panel's rows; arms the delayed fetch on open and once the
        numbers are older than Toggles.InternetAccounts.usage_refresh_s
        (`_arm_usage_fetch`)."""
        if not account.get("_usage_open"):
            self._disarm_usage_fetch(account)
            return []
        from meltygui.toggles import Toggles
        login = self._claude_login_for(account)
        if login is not None and not self._owns_claude_login(account, login):
            # Another row is the login's account (or the default row for the
            # shared file): say whose numbers are, and how this row gets
            # its own. Claude Code keeps one login per config dir.
            return [("note", "sign in to Claude Code to view usage")]
        fetched_at = account.get("_usage_fetched_at")
        if not account.get("_usage_loading") and (
                fetched_at is None
                or time.monotonic() - fetched_at > Toggles.InternetAccounts.usage_refresh_s):
            self._arm_usage_fetch(account)
        rows = account.get("_usage_rows") or []
        out = [("usage", row) for row in rows]
        if account.get("_usage_error"):
            out.append(("note", account["_usage_error"]))
        elif not rows and account.get("_usage_timer") is None:
            # (a pending delayed fetch shows nothing - no longer, Lukas 08-25)
            out.append(("note", "loading usage…" if account.get("_usage_loading") else "no usage data"))
        fetched_wall = account.get("_usage_fetched_wall")
        if fetched_wall:
            # "as of 21:42:10" — when these numbers were fetched, so a stale
            # panel (hidden window, network trouble, last session's numbers)
            # is visibly older; another day's fetch carries its date.
            fetched = time.localtime(fetched_wall)
            same_day = fetched[:3] == time.localtime()[:3]
            stamp = "as of " + time.strftime("%H:%M:%S" if same_day else "%m-%d %H:%M", fetched)
            if account.get("_usage_loading"):
                stamp += " · refreshing…"
            out.append(("stamp", stamp))
        return out

    @staticmethod
    def _usage_window_visible(period):
        """POLLING REMOVED (08-25: the usage endpoint rate-limits). This stub
        stays one hotswap generation so a timer chain armed by the previous
        code ends quietly on its next tick (it checks this before anything
        else). The panel now fetches on open, on a draw that finds the data
        older than usage_refresh_s, and on Refresh — never on a timer."""
        return False

    def validate(self, account):
        """The Test button: the ONLY Anthropic web request — list one model
        to confirm the credential works. Imports the SDK lazily. On a
        sign-in this also exercises the SDK's own token refresh."""
        source = self._source(account) or "?"
        try:
            import anthropic
            from meltygui.completion.providers.anthropic_requests import sdk_middleware
            client_kwargs = {"timeout": 15.0, "max_retries": 0,
                             "middleware": [sdk_middleware()]}
            client_kwargs.update(self.client_kwargs(account))
            client = anthropic.Anthropic(**client_kwargs)
            client.models.list(limit=1)
            client.close()
            account["_validated"] = True
            account["_status"] = ("ready", f"{source} · verified")
        except ImportError:
            account["_status"] = ("error", "anthropic package not installed")
        except Exception as error:
            account["_validated"] = False
            message = getattr(error, "message", None) or str(error)
            account["_status"] = ("error", f"{source} · {message[:90]}")
        accounts_changed()

    # -- browser sign-in ---------------------------------------------------

    def sign_in(self, account):
        """The Sign in button: open the Console consent page in the browser
        and wait for its redirect (anthropic_oauth.LoginFlow on a worker);
        clicked again while one is open it just re-opens the browser."""
        import meltygui.completion.providers.anthropic_oauth as anthropic_oauth
        flow = account.get("_login")
        if flow is not None and not flow.done:
            flow.open_in_browser()
            return
        flow = anthropic_oauth.LoginFlow(
            self.profile_name(account), base_url=account.get("base_url") or None,
            on_change=lambda flow: self._login_changed(account, flow))
        account["_login"] = flow
        account.pop("_validated", None)
        try:
            flow.start()
        except Exception as error:
            account["_login"] = None
            account["_status"] = ("error", f"sign-in: {error}"[:90])
        accounts_changed()

    def _login_changed(self, account, flow):
        """Worker thread: the flow finished — profile written, or an error."""
        if flow.done and account.get("_login") is flow:
            account["_login"] = None
            if flow.error:
                state = "needs_login" if "cancelled" in flow.error else "error"
                account["_status"] = (state, flow.error[:90])
                self.login_info(account, fresh=True)
            else:
                account["_status"] = None         # → the passive re-probe reads the new login
                self._reprobe_siblings(account)
                _drop_sessions_for(account)       # sessions that failed for want of a credential
        accounts_changed()

    def _reprobe_siblings(self, account):
        """A sign-in change on one row can move a shared Claude Code login's
        usage to another row — clear the siblings' status so they re-probe."""
        for sibling in accounts.of_kind(self.name):
            if sibling is not account:
                sibling["_status"] = None

    def cancel_sign_in(self, account):
        flow = account.get("_login")
        account["_login"] = None
        if flow is not None:
            flow.cancel()
        account["_status"] = ("needs_login", "sign-in cancelled")
        accounts_changed()

    def sign_out(self, account):
        """Forget the browser sign-in: removes the profile's credentials
        file (its org/workspace config stays, so a re-login skips the
        pickers) and drops the live sessions built on it."""
        import meltygui.completion.providers.anthropic_oauth as anthropic_oauth
        anthropic_oauth.sign_out(self.profile_name(account))
        account.pop("_validated", None)
        account["_status"] = None
        self.login_info(account, fresh=True)
        self._reprobe_siblings(account)
        _drop_sessions_for(account)
        accounts_changed()

    def actions(self, account):
        usage_open = bool(account.get("_usage_open"))
        usage = Button(None, lambda account: _toggle(account, "_usage_open"),
                       icon=f"" if usage_open else f"",
                       tip="Claude plan usage limits (read through Claude Code's login)")
        refresh_button = Button(None, self.refresh_all, icon=f"",
                                tip="Refresh (re-reads the logins and the usage)")
        flow = account.get("_login")
        if flow is not None and not flow.done:
            return [usage, Button("Cancel", self.cancel_sign_in, danger=True),
                    Button("Edit", _toggle_edit), refresh_button]
        has_key = bool(account.get("api_key"))
        signed_in = self.login_info(account) is not None
        switch = account.get("_claude_switch")
        email = (self.login_info(account) or {}).get("email")
        claude_login = self._claude_login_for(account)
        use_in_claude_code = []
        if email and (switch is None or switch.done) and (
                claude_login is None or not self._owns_claude_login(account, claude_login)):
            use_in_claude_code = [Button("Use in Claude Code", self.switch_claude_code,
                                         tip="Sign Claude Code in as this account (replaces its current login)")]
        test = Button("Test",
                      lambda account: _run_in_background(
                          account, lambda: self.validate(account), reprobe=False),
                      tip="Verify the credential (one web request)",
                      enabled=self._source(account) is not None)
        if has_key:
            middle = [Button("Edit", _toggle_edit), test,
                      Button("Clear", lambda account: accounts.set_field(account["id"], "api_key", ""),
                             tip="Forget the pasted key")]
        elif signed_in:
            middle = [Button("Sign out", self.sign_out, tip="Forget this browser sign-in"),
                      Button("Edit", _toggle_edit), test]
        else:
            middle = [Button("Sign in", self.sign_in, primary=True,
                             tip="Sign in with your Anthropic account (Google works) — opens your browser"),
                      Button("Paste key", lambda account: _paste_into(account, "api_key"),
                             tip="Or paste an API key from the clipboard"),
                      Button("Edit", _toggle_edit), test]
        return [usage] + middle + use_in_claude_code + [refresh_button]

    def sub_rows(self, account):
        out = []
        flow = account.get("_login")
        if flow is not None and not flow.done and flow.url:
            url = flow.url
            buttons = [Button("Copy link", lambda account, url=url: _copy_text(url),
                              tip="Copy the sign-in link"),
                       Button("Open browser", lambda account, flow=flow: flow.open_in_browser(),
                              primary=True, tip="Open the sign-in page again")]
            out.append(("card", ("finish signing in in the browser tab", buttons)))
        switch = account.get("_claude_switch")
        if switch is not None and not switch.done:
            buttons = [Button("Paste code", self.paste_switch_code,
                              tip="If the page showed a code instead of finishing: copy it, then paste it here"),
                       Button("Open browser", lambda account, switch=switch: switch.open_in_browser(),
                              primary=True, enabled=bool(switch.url), tip="Open the login page again"),
                       Button("Cancel", self.cancel_switch, danger=True)]
            out.append(("card", (f"signing Claude Code in as {switch.email} — finish in the browser", buttons)))
        elif account.get("_claude_switch_error"):
            out.append(("note", "Claude Code sign-in failed: " + account["_claude_switch_error"]))
        out.extend(self._usage_rows(account))
        return out


@register_chat_backend("codex")
def codex_chats(account, metadata=None, wake=None):
    if account.get("_codex_signing_in") or account.get("_busy"):
        return None
    from meltygui.chat.codex_proxy import CodexChats
    return CodexChats(account["id"], metadata, wake)


@account_kind
class CodexKind(AccountKind):
    chat_label = "Codex"
    name = "codex"
    label = "Codex"
    icon = f""
    tint = (0.35, 0.8, 0.65)
    fields = ()

    def _server(self, account):
        from meltygui.completion.providers.codex_accounts import AppServer
        from meltygui.completion.providers.codex_accounts import account_home
        from meltygui.toggles import Toggles
        return AppServer(account_home(account["id"]),
                         executable=Toggles.InternetAccounts.codex_bin,
                         timeout=Toggles.InternetAccounts.codex_request_timeout_s)

    def _read_account(self, account, server):
        identity = server.request("account/read", {"refreshToken": False}).get("account")
        if identity != account.get("_codex_account"):
            account.pop("_usage_rows", None)
            account.pop("_usage_fetched_wall", None)
            account.pop("_usage_error", None)
        account["_codex_account"] = identity
        cached = account.pop("_codex_cached_usage", None)
        if cached and identity and cached.get("identity") == identity:
            account["_usage_rows"] = cached.get("rows") or []
            account["_usage_fetched_wall"] = cached.get("fetched_wall")
        if not identity:
            return ("needs_login", "not signed in")
        if identity.get("type") != "chatgpt":
            return ("warning", "ChatGPT sign-in required for plan usage")
        return ("ready", " · ".join(filter(None, (
            identity.get("email") or "ChatGPT", identity.get("planType")))))

    def probe(self, account):
        with self._server(account) as server:
            status = self._read_account(account, server)
            if account.get("_usage_open"):
                self._read_usage(account, server)
            return status

    def usage_cache_entry(self, account):
        if account.get("_usage_fetched_wall") and account.get("_codex_account"):
            return {"identity": account["_codex_account"],
                    "rows": account.get("_usage_rows") or [],
                    "fetched_wall": account["_usage_fetched_wall"]}
        return account.get("_codex_cached_usage")

    def restore_usage(self, account, cached):
        if not account.get("_usage_restored"):
            account["_usage_restored"] = True
            # Display only after account/read confirms whose cached limits these are.
            if cached and "_codex_account" not in account:
                account["_codex_cached_usage"] = cached

    def sign_in(self, account):
        # A separate login flag keeps Cancel / Open browser usable while waiting.
        if account.get("_codex_signing_in") or account.get("_probing") or account.get("_usage_loading"):
            return
        self.close_chat(account)
        account["_codex_signing_in"] = True
        account["_codex_cancel"] = threading.Event()
        account["_status"] = ("busy", "starting sign-in…")
        accounts_changed()

        def run():
            from meltygui.toggles import Toggles
            try:
                with self._server(account) as server:
                    server.cancelled = account["_codex_cancel"]
                    login = server.request("account/login/start", {"type": "chatgpt"})
                    account["_codex_login"] = login
                    account["_status"] = ("busy", "finish sign-in in your browser")
                    accounts_changed()
                    if not server.cancelled.is_set():
                        _open_url(login["authUrl"])
                    completed = server.wait_login(login["loginId"],
                                                  Toggles.InternetAccounts.codex_login_timeout_s)
                    account["_status"] = self._read_account(account, server)
                    if completed and account.get("_usage_open"):
                        self._read_usage(account, server)
            except Exception as error:
                account["_status"] = ("error", str(error)[:160])
            finally:
                account.pop("_codex_login", None)
                account["_codex_signing_in"] = False
                accounts_changed()

        threading.Thread(target=run, daemon=True, name="codex-sign-in").start()

    def close(self, account):
        self.close_chat(account)
        cancel = account.get("_codex_cancel")
        if cancel is not None:
            cancel.set()

    def sign_out(self, account):
        self.close_chat(account)
        with self._server(account) as server:
            server.request("account/logout")
            account["_status"] = self._read_account(account, server)

    def _read_usage(self, account, server):
        from meltygui.completion.providers.codex_accounts import usage_rows
        try:
            account["_status"] = self._read_account(account, server)
            if (account.get("_codex_account") or {}).get("type") != "chatgpt":
                account["_usage_error"] = "Sign in with ChatGPT to view usage"
                return
            payload = server.request("account/rateLimits/read")
            account["_usage_rows"] = usage_rows(payload)
            account["_usage_fetched_wall"] = time.time()
            account.pop("_usage_error", None)
        except Exception as error:
            account["_usage_error"] = "Usage unavailable: " + str(error)[:120]

    def fetch_usage(self, account):
        if account.get("_usage_loading") or account.get("_codex_signing_in") or account.get("_busy") or account.get("_probing"):
            return
        account["_usage_loading"] = True
        accounts_changed()

        def run():
            try:
                with self._server(account) as server:
                    self._read_usage(account, server)
            except Exception as error:
                account["_usage_error"] = "Usage unavailable: " + str(error)[:120]
            finally:
                account["_usage_loading"] = False
                accounts_changed()

        threading.Thread(target=run, daemon=True, name="codex-usage").start()

    def toggle_usage(self, account):
        _toggle(account, "_usage_open")
        if account.get("_usage_open"):
            self.fetch_usage(account)

    def actions(self, account):
        if account.get("_codex_signing_in"):
            return [Button("Cancel", self.close)]
        idle = not account.get("_usage_loading") and not account.get("_probing")
        out = [Button(None, self.toggle_usage,
                      icon=f"" if account.get("_usage_open") else f"",
                      tip="Codex plan usage limits")]
        if account.get("_codex_account"):
            out.append(Button("Sign out", lambda account: _run_in_background(
                account, lambda: self.sign_out(account), reprobe=False), enabled=idle))
        else:
            out.append(Button("Sign in", self.sign_in, primary=True, enabled=idle))
        out.append(Button(None, lambda account: self.fetch_usage(account)
                          if account.get("_usage_open") else refresh(account), enabled=idle,
                          icon=f"", tip="Refresh account and usage"))
        return out

    def sub_rows(self, account):
        out = []
        login = account.get("_codex_login")
        if login:
            out.append(("card", ("Finish signing in with ChatGPT", [
                Button("Open browser", lambda account: _open_url(login["authUrl"])),
                Button("Cancel", self.close)])))
        if account.get("_usage_open"):
            rows = account.get("_usage_rows") or []
            out.extend(("usage", row) for row in rows)
            error = account.get("_usage_error")
            if error:
                out.append(("note", error))
            elif not rows:
                out.append(("note", "loading usage…" if account.get("_usage_loading")
                            else "Usage unavailable — Refresh to check"))
            fetched = account.get("_usage_fetched_wall")
            if fetched:
                stamp = "as of " + time.strftime("%m-%d %H:%M:%S", time.localtime(fetched))
                if account.get("_usage_loading"):
                    stamp += " · refreshing…"
                out.append(("stamp", stamp))
        out.append(("note", "Shared with Codex desktop / CLI" if account["id"] == "codex"
                    else "Separate Codex account"))
        return out


@account_kind
class CopilotKind(AccountKind):
    name = "copilot"
    label = "GitHub Copilot"
    icon = f""
    tint = (0.45, 0.6, 0.85)
    fields = (Field("config_dir", "Config dir",
                    placeholder="(default ~/.config — shared with the IDE plugins)"),)

    def _session(self, account, create=True):
        import meltygui.completion.fim as fim
        from meltygui.completion.providers.copilot import CopilotSession
        session_kwargs = {"account": session_account_id(account)}
        return fim.session_for(CopilotSession, session_kwargs, create=create)

    def probe(self, account):
        # Passive probe: NO language-server spawn and NO web request. Node +
        # install are filesystem checks; sign-in state is read from a token
        # file on disk. The LS is spawned only when the user clicks Sign in
        # or when FIM actually asks Copilot for a completion - so opening the
        # accounts window (even at startup) costs nothing.
        import meltygui.completion.providers.copilot as copilot
        if copilot.find_node() is None:
            return ("error", "node ≥ 20.8 not found")
        if not copilot.server_installed():
            return ("needs_login", "not installed")
        # If a session is already running (FIM used it, or the user signed in),
        # trust its status instead of the on-disk file.
        try:
            session = self._session(account, create=False)
        except Exception:
            session = None
        if session is not None and session.alive():
            state, text = session.status()
            if state == "needs_login":
                return ("needs_login", f"sign in: code {text}")
            if session.user:
                return ("ready", session.user)
            if state == "error":
                return ("needs_login", text or "not signed in")
        user = copilot.cached_login_user(account.get("config_dir"))
        if user:
            return ("ready", user)
        return ("needs_login", "not signed in")

    def actions(self, account):
        import meltygui.completion.providers.copilot as copilot
        out = []
        if not copilot.server_installed():
            out.append(Button("Install",
                              lambda account: _run_in_background(account, copilot.install_server),
                              primary=True))
            return out
        state = (account.get("_status") or ("unknown", ""))[0]
        if state == "ready":
            out.append(Button("Sign out", lambda account: _run_in_background(
                account, lambda: self._session(account).sign_out())))
        else:
            out.append(Button("Sign in", lambda account: _run_in_background(
                account, lambda: self._session(account).sign_in()), primary=True))
        out.append(Button("Edit", _toggle_edit))
        out.append(Button(None, refresh, icon=f"", tip="Refresh"))
        return out

    def sub_rows(self, account):
        try:
            session = self._session(account, create=False)
        except Exception:
            session = None
        if session is not None and session.login is not None:
            code, url = session.login
            buttons = [Button("Copy code", lambda account, code=code: _copy_text(code)),
                       Button("Open browser", lambda account, url=url: _open_url(url), primary=True)]
            return [("card", (f"Enter code  {code}  at {url}", buttons))]
        return []


@account_kind
class OllamaKind(AccountKind):
    chat_label = "Ollama"
    name = "ollama"
    label = "Ollama"
    icon = f""
    tint = (0.5, 0.75, 0.6)
    fields = (Field("host", "Host", default="http://localhost:11434"),
              Field("device", "Device", default="auto", hidden=True))

    @staticmethod
    def _client(account):
        import httpx
        host = (account.get("host") or "http://localhost:11434").rstrip("/")
        # Short connect timeout: a down local server fails in ~1s instead of
        # hanging the probe thread. (Probes always run on a worker, never the
        # render thread - so a fast failure keeps status snappy.)
        return httpx.Client(base_url=host, timeout=httpx.Timeout(4.0, connect=1.0))

    def probe(self, account):
        import meltygui.completion.providers.ollama as ollama
        host = (account.get("host") or "http://localhost:11434").rstrip("/")
        try:
            with self._client(account) as client:
                models = ollama.list_models(client)
        except Exception as error:
            account["_models"] = []
            return ("error", f"{host} · {str(error)[:60]}")
        account["_models"] = models
        try:
            account["_gpus"] = ollama.gpu_inventory()   # best-effort GPU names for the device menu
        except Exception:
            account["_gpus"] = []
        loaded = [model for model in models if model["loaded"]]
        fim_like = [model["name"] for model in models
                    if any(key in model["name"]
                           for key in ("coder", "codellama", "starcoder", "codestral", "deepseek-coder"))]
        text = f"{len(models)} models"
        if loaded:
            text += f" · {len(loaded)} loaded on " + ", ".join(
                sorted({model['where'] or '?' for model in loaded}))
        if not fim_like:
            text += " · no FIM model (pull qwen2.5-coder)"
        return ("ready", text)

    def actions(self, account):
        import meltygui.completion.providers.ollama as ollama
        models_open = bool(account.get("_models_open"))
        device = account.get("device") or "auto"
        return [Button(None, lambda account: _toggle(account, "_models_open"),
                       icon=f"" if models_open else f"", tip="Models"),
                Button(f" {ollama.device_label(device, account.get('_gpus'))}",
                       self._cycle_device, tip="Device models load onto (click to cycle)"),
                Button("Edit", _toggle_edit),
                Button(None, refresh, icon=f"", tip="Refresh")]

    def _cycle_device(self, account):
        import meltygui.completion.providers.ollama as ollama
        choices = ollama.device_choices(account.get("_gpus"))
        current = account.get("device") or "auto"
        next_device = (choices[(choices.index(current) + 1) % len(choices)]
                       if current in choices else choices[0])
        accounts.set_field(account["id"], "device", next_device, reprobe=False)

    def sub_rows(self, account):
        if not account.get("_models_open"):
            return []
        models = account.get("_models")
        if models is None:
            return [("note", "loading…")]
        if not models:
            return [("note", "no models — `ollama pull qwen2.5-coder:7b`")]
        return [("model", model) for model in models]

    def model_actions(self, account, model):
        import meltygui.completion.providers.ollama as ollama
        device = account.get("device") or "auto"
        target = ollama.device_label(device, account.get("_gpus"))

        def load(account, name=model["name"]):
            from meltygui.toggles import Toggles
            _run_in_background(account, lambda: self._with_client(
                account, lambda client: ollama.load_model(
                    client, name, account.get("device") or "auto", Toggles.Fim.ollama_keep_alive)))

        def unload(account, name=model["name"]):
            _run_in_background(account, lambda: self._with_client(
                account, lambda client: ollama.unload_model(client, name)))

        out = [Button(("Move" if model["loaded"] else "Load") + f" → {target}", load,
                      primary=not model["loaded"])]
        if model["loaded"]:
            out.append(Button(None, unload, icon=f"", tip="Unload"))
        return out

    def _with_client(self, account, fn):
        with self._client(account) as client:
            return fn(client)


# ──────────────────────────────────────────────────────────────────────────
# Actions / probes
# ──────────────────────────────────────────────────────────────────────────

def refresh(account):
    """Probe one account on a worker and repaint when it answers."""
    if account.get("_probing"):
        return
    account["_probing"] = True
    kind = KINDS[account["kind"]]

    def run():
        try:
            account["_status"] = kind.probe(account)
        except Exception as error:
            account["_status"] = ("error", str(error)[:90])
        finally:
            account["_probing"] = False
            account["_probed_at"] = time.monotonic()
        accounts_changed()

    threading.Thread(target=run, daemon=True, name=f"acct-probe-{account['id']}").start()


def _run_in_background(account, fn, reprobe=True):
    """Run `fn()` on a worker with the row's busy flag set. `reprobe` re-runs
    the passive probe afterwards (default) — pass False when `fn` already set
    the status itself (e.g. validate), so the trailing probe doesn't clobber
    it."""
    account["_busy"] = True
    accounts_changed()

    def run():
        try:
            fn()
        except Exception as error:
            account["_status"] = ("error", str(error)[:90])
        finally:
            account["_busy"] = False
        if reprobe:
            refresh(account)
        else:
            accounts_changed()

    threading.Thread(target=run, daemon=True, name=f"acct-action-{account['id']}").start()


def _paste_into(account, field_name):
    try:
        text = (imgui.get_clipboard_text() or "").strip()
    except Exception:
        text = ""
    if text:
        accounts.set_field(account["id"], field_name, text)
        refresh(account)


def _copy_text(text):
    try:
        imgui.set_clipboard_text(text)
    except Exception:
        pass


def _open_url(url):
    """A sign-in page: the placed Xwayland popup (oauth_popup), which falls
    back to plain xdg-open by itself."""
    import meltygui.completion.providers.oauth_popup as oauth_popup
    oauth_popup.open_auth_popup(url)


def _toggle(account, key):
    account[key] = not account.get(key)
    accounts_changed()


def _toggle_edit(account):
    _toggle(account, "_edit")


def _refresh_stale(account):
    """Probe an account ONCE (when its status is first unknown). No timer-
    based re-probe: the window must not fire a recurring web request every
    couple of minutes just for being open — the user refreshes on demand
    (Refresh button / a credential edit clears _status)."""
    if account.get("_status") is None and not account.get("_probing"):
        refresh(account)


# ──────────────────────────────────────────────────────────────────────────
# Window
# ──────────────────────────────────────────────────────────────────────────

def _mix(style_manager, tint, value, factor, saturation):
    return style_manager.make_color_rgb(tint[0], tint[1], tint[2], value=value,
                                        factor=factor, saturation_scale=saturation)


def _color_u32(color, alpha=1.0):
    return pack_color(color[0], color[1], color[2], alpha)


def _wrap_usage_label(text, max_width):
    """Keep the complete name; wrap at spaces, or within a long model name."""
    lines = []
    while text:
        if imgui.calc_text_size(text)[0] <= max_width:
            lines.append(text)
            break
        low, high = 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if imgui.calc_text_size(text[:middle])[0] <= max_width:
                low = middle
            else:
                high = middle - 1
        end = text.rfind(" ", 0, low + 1)
        if end <= 0:
            end = low
        lines.append(text[:end])
        text = text[end:].lstrip()
    return lines


def _ellipsize(text, max_width):
    """`text` ellipsized to `max_width` pixels in the current font."""
    if max_width <= 0:
        return ""
    if imgui.calc_text_size(text)[0] <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if imgui.calc_text_size(text[:mid] + "…")[0] <= max_width:
            low = mid
        else:
            high = mid - 1
    return (text[:low] + "…") if low > 0 else ""


def _format_gb(size_bytes):
    return f"{size_bytes / 1e9:.1f} GB"


from meltygui.state.account_state import AccountsPanelState


from meltygui.core.account_core import _cleanup_accounts


from meltygui.view.account_view import draw_internet_accounts
draw_internet_accounts = window(input_value=accounts, tint=(0.19, 0.16, 0.14), icon=f'\uf0c2', display_name='Internet Accounts', initial={'width': 760, 'height': 460})(draw_internet_accounts)


def _draw_field(account, field, left, top, width, height):
    """An editable field: a single-line draw_text row (the editor, so focus,
    selection and paste all work). Returns True when the edit changed the
    stored value."""
    from meltygui.editor.text_editor import draw_text
    key = f"acct_{account['id']}_{field.name}"
    value = account.get(field.name) or ""
    imgui.set_cursor_screen_pos((left, top))
    changed, new_value = draw_text(value, name=key, single_line=True, width=width, height=height,
                                   show_widgets=False, show_root_backgrounds=False,
                                   show_header=False, show_file_header=False, show_jump_bar=False,
                                   shadow=False, use_cache=True, temp=True, autocomplete=False,
                                   syntax_highlight=False, line_numbers=False, fim="")
    if changed and isinstance(new_value, str) and new_value != value:
        accounts.set_field(account["id"], field.name, new_value.strip())
        return True
    return False

# Register the companion window on initial import and on an Accounts hotswap.
# The import is last so its provider registry is already available.
import meltygui.chat.chat_interface  # noqa: E402,F401