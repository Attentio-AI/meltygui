"""Internet Accounts — one window to manage every login the studio's
network features use (Anthropic API key, GitHub Copilot device-flow sign-in,
Ollama host + model placement), modelled on fast_dock: rows are plain
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
the Ollama model list).

Layout: every row measures its buttons FIRST; if the text would be left
less than min_text_width the buttons wrap onto a second line inside the row
(the row grows), otherwise status text is ellipsized to what's left — so
nothing ever overlaps at any window width.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

# Where the store lives on disk - a data file, not a styling knob; shared
# by the store methods, the footer row, and the tests' monkeypatch.
ACCOUNTS_PATH = Path.home() / ".lsd" / "accounts.json"


# ──────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────

class AccountStore(dict):
    """id -> account dict. `load()` reads the JSON; every mutation goes
    through `set_field` / `add` / `remove` so the file stays in sync and
    the window repaints. Runtime-only keys start with '_' and are never
    written."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self.loaded = False
        self.error = None

    def load(self):
        with self._lock:
            self.clear()
            try:
                if ACCOUNTS_PATH.exists():
                    data = json.loads(ACCOUNTS_PATH.read_text())
                    for entry in data.get("accounts", []):
                        if isinstance(entry, dict) and entry.get("id") and entry.get("kind") in KINDS:
                            self[entry["id"]] = entry
                self.error = None
            except Exception as error:
                self.error = f"accounts.json: {error}"
            self.loaded = True
        # default accounts so every kind has a row to act on: the id is the
        # kind name ("anthropic", "copilot", "ollama"); sessions asking for
        # account="default" resolve to it (see `account`).
        for kind in KINDS.values():
            if not any(entry.get("kind") == kind.name for entry in self.values()):
                self.add(kind.name, account_id=kind.name, save=False)
            for entry in self.of_kind(kind.name):
                for field in kind.fields:
                    entry.setdefault(field.name, field.default)
        return self

    def save(self):
        with self._lock:
            data = {"accounts": [{key: value for key, value in entry.items()
                                  if not key.startswith("_")}
                                 for entry in self.values()]}
            try:
                ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = ACCOUNTS_PATH.with_suffix(".json.tmp")
                with open(tmp, "w") as file:
                    file.write(json.dumps(data, indent=2))
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(tmp, ACCOUNTS_PATH)
                self.error = None
            except Exception as error:
                self.error = f"accounts.json: {error}"

    def add(self, kind_name, account_id=None, save=True, **fields):
        kind = KINDS[kind_name]
        if account_id is None:
            suffix = 2
            while f"{kind_name}-{suffix}" in self:
                suffix += 1
            account_id = f"{kind_name}-{suffix}"
        entry = {"id": account_id, "kind": kind_name,
                 "label": fields.pop("label", None) or kind.default_label(account_id)}
        for field in kind.fields:
            entry[field.name] = fields.get(field.name, field.default)
        self[account_id] = entry
        if save:
            self.save()
        accounts_changed()
        return entry

    def remove(self, account_id):
        entry = self.pop(account_id, None)
        if entry is not None:
            _drop_sessions_for(entry)
            self.save()
            accounts_changed()

    def set_field(self, account_id, field, value, reprobe=True):
        entry = self.get(account_id)
        if entry is None or entry.get(field) == value:
            return
        entry[field] = value
        if reprobe:
            entry["_status"] = None        # stale → re-probe
            entry.pop("_validated", None)  # credential changed → re-verify with Test
            _drop_sessions_for(entry)      # live sessions hold the old credential
        self.save()
        accounts_changed()

    def of_kind(self, kind_name):
        return sorted((entry for entry in self.values() if entry.get("kind") == kind_name),
                      key=lambda entry: (entry["id"] != kind_name, entry["id"]))


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
    return account.get("id") == account.get("kind")


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
        account_id = kind_name
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
        from src.lsd.gl_gui.fim import _wake
        _wake(_window_draw_state)
    except Exception:
        pass


_window_draw_state = None   # draw_internet_accounts' draw_state - the wake target


def _drop_sessions_for(account_entry):
    """Close pooled FIM sessions built on this account so the next request
    re-acquires with the new credential."""
    ids = {account_entry.get("id"), session_account_id(account_entry)}
    try:
        from src.lsd.gl_gui import fim
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
    name = "base"
    label = "Account"
    icon = ""
    tint = (0.5, 0.5, 0.55)
    fields = ()

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
        """Extra rows under the account: ("code", (user_code, url)) |
        ("model", model dict) | ("note", text)."""
        return []


@account_kind
class AnthropicKind(AccountKind):
    name = "anthropic"
    label = "Anthropic"
    icon = f""
    tint = (0.85, 0.55, 0.35)
    fields = (Field("api_key", "API key", secret=True, placeholder="sk-ant-…"),
              Field("base_url", "Base URL", placeholder="(default)"))

    @staticmethod
    def _profile_present():
        config_dir = Path(os.environ.get("ANTHROPIC_CONFIG_DIR")
                          or (Path.home() / ".config" / "anthropic"))
        return (config_dir / "credentials").is_dir() and any((config_dir / "credentials").glob("*.json"))

    def _source(self, account):
        key = account.get("api_key") or ""
        if key:
            return f"key …{key[-4:]}"
        if is_default(account) and os.environ.get("ANTHROPIC_API_KEY"):
            return "env ANTHROPIC_API_KEY"
        if is_default(account) and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return "env ANTHROPIC_AUTH_TOKEN"
        if is_default(account) and self._profile_present():
            return "ant auth profile"
        return None

    def probe(self, account):
        # Passive probe: NO network (and no `import anthropic`). Just report
        # whether a credential exists - the studio should not fire a web
        # request or import the SDK at startup just to show status. The
        # "Test" button (below) does the one real network check on demand.
        source = self._source(account)
        if source is None:
            return ("needs_login", "no credentials — paste an API key")
        if account.get("_validated"):
            return ("ready", f"{source} · verified")
        return ("ready", source)

    def validate(self, account):
        """The Test button: the ONLY Anthropic web request — list one model
        to confirm the key works. Imports the SDK lazily."""
        source = self._source(account) or "?"
        try:
            import anthropic
            client_kwargs = {"timeout": 15.0, "max_retries": 0}
            if account.get("api_key"):
                client_kwargs["api_key"] = account["api_key"]
            if account.get("base_url"):
                client_kwargs["base_url"] = account["base_url"]
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

    def actions(self, account):
        return [Button("Paste key", lambda account: _paste_into(account, "api_key"), primary=True),
                Button("Edit", _toggle_edit),
                Button("Test",
                       lambda account: _run_in_background(
                           account, lambda: self.validate(account), reprobe=False),
                       tip="Verify the key (one web request)",
                       enabled=self._source(account) is not None),
                Button("Clear", lambda account: accounts.set_field(account["id"], "api_key", ""),
                       enabled=bool(account.get("api_key")))]


@account_kind
class CopilotKind(AccountKind):
    name = "copilot"
    label = "GitHub Copilot"
    icon = f""
    tint = (0.45, 0.6, 0.85)
    fields = (Field("config_dir", "Config dir",
                    placeholder="(default ~/.config — shared with the IDE plugins)"),)

    def _session(self, account, create=True):
        from src.lsd.gl_gui import fim
        from src.lsd.gl_gui.fim_providers.copilot import CopilotSession
        session_kwargs = {"account": session_account_id(account)}
        return fim.session_for(CopilotSession, session_kwargs, create=create)

    def probe(self, account):
        # Passive probe: NO language-server spawn and NO web request. Node +
        # install are filesystem checks; sign-in state is read from a token
        # file on disk. The LS is spawned only when the user clicks Sign in
        # or when FIM actually asks Copilot for a completion - so opening the
        # accounts window (even at startup) costs nothing.
        from src.lsd.gl_gui.fim_providers import copilot
        if copilot.find_node() is None:
            return ("error", "node ≥ 20.8 not found")
        if not copilot.server_installed():
            return ("needs_login", "language server not installed — Install")
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
                return ("ready", f"signed in as {session.user}")
            if state == "error":
                return ("needs_login", text or "not signed in")
        user = copilot.cached_login_user(account.get("config_dir"))
        if user:
            return ("ready", f"signed in as {user}")
        return ("needs_login", "not signed in — Sign in")

    def actions(self, account):
        from src.lsd.gl_gui.fim_providers import copilot
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
            return [("code", session.login)]
        return []


@account_kind
class OllamaKind(AccountKind):
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
        from src.lsd.gl_gui.fim_providers import ollama
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
        from src.lsd.gl_gui.fim_providers import ollama
        models_open = bool(account.get("_models_open"))
        device = account.get("device") or "auto"
        return [Button(None, lambda account: _toggle(account, "_models_open"),
                       icon=f"" if models_open else f"", tip="Models"),
                Button(f" {ollama.device_label(device, account.get('_gpus'))}",
                       self._cycle_device, tip="Device models load onto (click to cycle)"),
                Button("Edit", _toggle_edit),
                Button(None, refresh, icon=f"", tip="Refresh")]

    def _cycle_device(self, account):
        from src.lsd.gl_gui.fim_providers import ollama
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
        from src.lsd.gl_gui.fim_providers import ollama
        device = account.get("device") or "auto"
        target = ollama.device_label(device, account.get("_gpus"))

        def load(account, name=model["name"]):
            from src.lsd.gl_gui.toggles import Toggles
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
    return imgui.get_color_u32_rgba(color[0], color[1], color[2], alpha)


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


@window(input_value=accounts, tint=(0.72, 0.71, 0.67), icon=f"",
        display_name="Internet Accounts", initial={"width": 760, "height": 460})
@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True,
             is_default_for="AccountStore", tint=(0.62, 0.47, 0.88))
def draw_internet_accounts(
        # [tint=(0.85, 0.75, 0.05)]
        input_value: AccountStore,
        draw_state, style_manager=None,
        non_blocking_left_mouse_down=False, **kwargs):
    global _window_draw_state
    _window_draw_state = draw_state
    store = input_value
    if not store.loaded:
        store.load()

    # ---- styling (fast_dock recipe) ----
    row_bg_value, row_text_value = 0.06, 0.95
    factor, saturation = 0.90, 1.0
    button_bg_value, button_text_value = 0.13, 1.25
    primary_bg_value = 0.22
    hover_bg_boost, hover_text_boost = 0.05, 0.5
    text_saturation = 0.8

    # Status lamp colours (and the status text that inherits them), by
    # probe state — add a state here when a kind's probe grows one.
    # [tint=(0.35, 0.9, 0.45)]
    state_tints = {
        "ready": (0.35, 0.85, 0.45),
        "busy": (0.85, 0.75, 0.35),
        "needs_login": (0.95, 0.65, 0.25),
        "warning": (0.95, 0.7, 0.3),
        "error": (0.95, 0.35, 0.35),
        "unknown": (0.55, 0.58, 0.65),
    }

    # ---- row geometry, authored at ui_scale 1.0 and scaled once per frame ----
    px = Melty.px
    # [tint=(0.939, 0.453, 0.245)]
    row_height = px(34.0)
    row_gap = px(6.0)
    pad_x = px(10.0)
    corner = px(6.0)
    button_height = px(24.0)
    button_pad_x = px(10.0)
    button_gap = px(6.0)
    sub_row_height = px(30.0)         # secondary rows (field editors, device code card, model rows)
    kind_header_height = px(26.0)
    # Below this much text room the buttons wrap onto their own line inside
    # the row (the row grows) — raise it and narrow windows wrap sooner.
    # [tint=(0.994, 0.872, 0.0)]
    min_text_width = px(150.0)
    # [tint=(0.35, 0.85, 0.94)]
    text_inset = px(30.0)             # label x inset (after the status lamp)
    text_nudge_y = px(-1.0)

    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    origin_x, origin_y = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width or (draw_state.width or 300)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    press = non_blocking_left_mouse_down
    # [tint=(0.62, 0.47, 0.95)]
    click = (press.x, press.y) if (press and hasattr(press, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)
    line_height = imgui.get_text_line_height()
    row_left, row_right = origin_x + pad_x, origin_x + content_width - pad_x
    # True whenever a button handler or a field edit mutated the store this
    # frame - it is the view's `changed` return (style guide rule 10).
    pressed = [False]

    def visible(top, bottom):
        return clip is None or not (bottom < clip[1] or top > clip[3])

    def button_width(button):
        if button.icon is not None and button.label is None:
            return button_height
        return imgui.calc_text_size(button.label)[0] + 2 * button_pad_x

    def buttons_width(buttons):
        return (sum(button_width(button) for button in buttons)
                + button_gap * max(0, len(buttons) - 1))

    def draw_buttons(buttons, right, top, tint, account, hint_slot):
        """Right-aligned button strip ending at `right`. Runs click handlers."""
        strip_right = right
        for button in reversed(buttons):
            width = button_width(button)
            left = strip_right - width
            bottom = top + button_height
            enabled = button.enabled and not (account or {}).get("_busy")
            hovered = (enabled and hover_ok
                       and left <= mouse_x <= strip_right and top <= mouse_y <= bottom)
            bg_value = ((primary_bg_value if button.primary else button_bg_value)
                        + (hover_bg_boost if hovered else 0.0))
            button_tint = (0.85, 0.35, 0.35) if button.danger else tint
            bg_color = _mix(style_manager, button_tint,
                            bg_value if enabled else button_bg_value * 0.5, factor, saturation)
            text_color = _mix(style_manager, button_tint,
                              (button_text_value + (hover_text_boost if hovered else 0.0))
                              if enabled else 0.45,
                              factor, text_saturation)
            if enabled and visible(top, bottom):
                add_shadow((left, top, width, button_height), offset=8 if button.primary else 4,
                           corner_radius=corner, clip=clip)
            draw_list.add_rect_filled(left, top, strip_right, bottom,
                                      _color_u32(bg_color), rounding=corner)
            if button.icon is not None and button.label is None:
                icon_size = imgui.calc_text_size(button.icon)
                draw_list.add_text(left + (width - icon_size[0]) / 2.0,
                                   top + (button_height - icon_size[1]) / 2.0 + text_nudge_y,
                                   _color_u32(text_color), button.icon)
            else:
                label_size = imgui.calc_text_size(button.label)
                draw_list.add_text(left + button_pad_x,
                                   top + (button_height - label_size[1]) / 2.0 + text_nudge_y,
                                   _color_u32(text_color), button.label)
            if hovered and button.tip:
                hint_slot[0] = button.tip
            if (enabled and click is not None
                    and left <= click[0] <= strip_right and top <= click[1] <= bottom):
                try:
                    button.on_click(account)
                except Exception as error:
                    if account is not None:
                        account["_status"] = ("error", str(error)[:90])
                pressed[0] = True
                request_render()
            strip_right = left - button_gap
        return strip_right + button_gap          # left edge of the strip

    # ---- layout pass ----
    # (kind, account, y, row height, buttons, wrap, subs) - heights are final
    # here so the scroll dummy and the hit-tests agree.
    y = origin_y
    # [tint=(0.989, 0.17, 0.497)]
    layout = []
    for kind in KINDS.values():
        layout.append(("head", kind, None, y, kind_header_height, None, False, []))
        y += kind_header_height + px(2)
        for account_entry in store.of_kind(kind.name):
            buttons = list(kind.actions(account_entry))
            if not is_default(account_entry):
                buttons.append(Button(None, lambda account: store.remove(account["id"]),
                                      icon=f"", tip="Remove account", danger=True))
            label = account_entry.get("label") or kind.default_label(account_entry["id"])
            label_width = imgui.calc_text_size(label)[0]
            text_avail = ((row_right - px(6)) - (row_left + text_inset)
                          - buttons_width(buttons) - button_gap)
            wrap = text_avail < max(min_text_width, label_width + px(40))
            height = row_height + (button_height + px(6) if wrap else 0)
            subs = []
            if account_entry.get("_edit"):
                subs.extend(("field", field) for field in kind.fields if not field.hidden)
            subs.extend(kind.sub_rows(account_entry))
            layout.append(("account", kind, account_entry, y, height, buttons, wrap, subs))
            y += height + len(subs) * sub_row_height + (px(4) if subs else 0) + row_gap
        y += px(4)
    footer_y = y
    total_height = (footer_y - origin_y) + row_height
    top_inset = (origin_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(content_width, max(1.0, total_height + max(0.0, top_inset)))

    # [tint=(0.939, 0.836, 0.595)]
    hint = [None]

    for item in layout:
        what, kind, account_entry, row_top, height, buttons, wrap, subs = item
        tint = kind.tint
        if what == "head":
            if visible(row_top, row_top + height):
                text_color = _mix(style_manager, tint, 1.0, factor, text_saturation)
                draw_list.add_text(row_left + px(2), row_top + (height - line_height) / 2.0,
                                   _color_u32(text_color, 0.85), f"{kind.icon}  {kind.label}")
                draw_buttons([Button(f" account",
                                     lambda _account, kind=kind: store.add(kind.name))],
                             row_right, row_top + (height - button_height) / 2.0, tint, None, hint)
            continue

        _refresh_stale(account_entry)
        row_bottom = row_top + height
        state, status_text = kind.status(account_entry)
        if account_entry.get("_busy") or account_entry.get("_probing"):
            state, status_text = "busy", (status_text if state != "unknown" else "…")
        # Hoisted above the visibility gate: the model sub-rows below read
        # these even when their parent row is scrolled offscreen.
        lamp_color = state_tints.get(state, state_tints["unknown"])
        text_color = _mix(style_manager, tint, row_text_value, factor, text_saturation)
        if visible(row_top, row_bottom):
            row_hovered = (hover_ok and row_left <= mouse_x <= row_right
                           and row_top <= mouse_y <= row_bottom)
            bg_color = _mix(style_manager, tint, row_bg_value + (0.02 if row_hovered else 0.0),
                            factor, saturation)
            draw_list.add_rect_filled(row_left, row_top, row_right, row_bottom,
                                      _color_u32(bg_color), rounding=corner)
            # status lamp, recessed
            lamp_radius = px(4.5)
            lamp_x, lamp_y = row_left + px(14), row_top + row_height / 2.0
            add_shadow((lamp_x - lamp_radius, lamp_y - lamp_radius, 2 * lamp_radius, 2 * lamp_radius),
                       offset=-1, corner_radius=lamp_radius, clip=clip)
            draw_list.add_circle_filled(lamp_x, lamp_y, lamp_radius, _color_u32(lamp_color), 16)
            # buttons: on the row line, or wrapped onto their own line
            if wrap:
                strip_left = row_right - px(6)
                draw_buttons(buttons, row_right - px(6), row_top + row_height + px(2),
                             tint, account_entry, hint)
            else:
                strip_left = draw_buttons(buttons, row_right - px(6),
                                          row_top + (row_height - button_height) / 2.0,
                                          tint, account_entry, hint)
            # label + status text, fitted to the space left of the strip
            label = account_entry.get("label") or kind.default_label(account_entry["id"])
            text_x = row_left + text_inset
            text_right = strip_left - button_gap - px(4)
            text_y = row_top + (row_height - line_height) / 2.0 + text_nudge_y
            label_fit = _ellipsize(label, text_right - text_x)
            draw_list.add_text(text_x, text_y, _color_u32(text_color), label_fit)
            status_x = text_x + imgui.calc_text_size(label_fit)[0] + px(12)
            status_fit = _ellipsize(status_text, text_right - status_x)
            if status_fit:
                draw_list.add_text(status_x, text_y, _color_u32(lamp_color, 0.9), status_fit)

        # ---- sub rows ----
        sub_top = row_bottom + px(4)
        for sub in subs:
            sub_bottom = sub_top + sub_row_height
            sub_left, sub_right = row_left + text_inset, row_right - px(6)
            if sub[0] == "field":
                field = sub[1]
                if visible(sub_top, sub_bottom):
                    label_color = _mix(style_manager, tint, 0.7, factor, text_saturation)
                    draw_list.add_text(sub_left,
                                       sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y,
                                       _color_u32(label_color, 0.85), field.label)
                    field_left = sub_left + px(110)
                    field_width = max(px(120), sub_right - field_left)
                    if _draw_field(account_entry, field, field_left,
                                   sub_top + (sub_row_height - px(26)) / 2.0, field_width, px(26)):
                        pressed[0] = True
            elif sub[0] == "code":
                code, url = sub[1]
                if visible(sub_top, sub_bottom):
                    add_shadow((sub_left, sub_top, sub_right - sub_left, sub_row_height),
                               offset=11, corner_radius=corner, clip=clip)
                    card_bg_color = _mix(style_manager, tint, 0.16, factor, saturation)
                    draw_list.add_rect_filled(sub_left, sub_top, sub_right, sub_bottom,
                                              _color_u32(card_bg_color), rounding=corner)

                    def open_browser(account, url=url):
                        from src.lsd.gl_gui.fim_providers.copilot import open_url
                        open_url(url)

                    def copy_code(account, code=code):
                        try:
                            imgui.set_clipboard_text(code)
                        except Exception:
                            pass

                    code_buttons = [Button("Copy code", copy_code),
                                    Button("Open browser", open_browser, primary=True)]
                    strip_left = draw_buttons(code_buttons, sub_right - px(6),
                                              sub_top + (sub_row_height - button_height) / 2.0,
                                              tint, account_entry, hint)
                    message = _ellipsize(f"Enter code  {code}  at {url}",
                                         strip_left - button_gap - (sub_left + px(10)))
                    draw_list.add_text(sub_left + px(10),
                                       sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y,
                                       _color_u32((1.0, 0.95, 0.85)), message)
            elif sub[0] == "note":
                if visible(sub_top, sub_bottom):
                    draw_list.add_text(sub_left + px(6),
                                       sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y,
                                       _color_u32((0.7, 0.72, 0.8), 0.8), sub[1])
            elif sub[0] == "model":
                model = sub[1]
                if visible(sub_top, sub_bottom):
                    model_bg_color = _mix(style_manager, tint, 0.09 if model["loaded"] else 0.04,
                                          factor, saturation)
                    add_shadow((sub_left, sub_top, sub_right - sub_left, sub_row_height),
                               offset=2 if model["loaded"] else 1, corner_radius=corner, clip=clip)
                    draw_list.add_rect_filled(sub_left, sub_top, sub_right, sub_bottom,
                                              _color_u32(model_bg_color), rounding=corner)
                    model_buttons = kind.model_actions(account_entry, model)
                    strip_left = draw_buttons(model_buttons, sub_right - px(6),
                                              sub_top + (sub_row_height - button_height) / 2.0,
                                              tint, account_entry, hint)
                    model_lamp = state_tints["ready"] if model["loaded"] else state_tints["unknown"]
                    draw_list.add_circle_filled(sub_left + px(12), sub_top + sub_row_height / 2.0,
                                                px(3.5), _color_u32(model_lamp), 12)
                    name_x = sub_left + px(24)
                    text_y = sub_top + (sub_row_height - line_height) / 2.0 + text_nudge_y
                    name_fit = _ellipsize(model["name"],
                                          min(px(260), strip_left - button_gap - name_x))
                    draw_list.add_text(name_x, text_y, _color_u32(text_color), name_fit)
                    info_x = name_x + imgui.calc_text_size(name_fit)[0] + px(10)
                    info = _format_gb(model["size"])
                    if model["loaded"]:
                        info += f" · loaded on {model['where'] or '?'}"
                    info_fit = _ellipsize(info, strip_left - button_gap - px(4) - info_x)
                    if info_fit:
                        draw_list.add_text(info_x, text_y,
                                           _color_u32((0.72, 0.75, 0.82), 0.85), info_fit)
            sub_top = sub_bottom

    # ---- footer: file hints / notes / errors ----
    if visible(footer_y, footer_y + row_height):
        note = hint[0] or f"{ACCOUNTS_PATH}"
        if store.error:
            note += f"   ·   {store.error}"
        draw_list.add_text(row_left, footer_y + (row_height - line_height) / 2.0,
                           _color_u32((0.6, 0.62, 0.7), 0.6),
                           _ellipsize(note, row_right - row_left))

    return pressed[0], input_value


def _draw_field(account, field, left, top, width, height):
    """An editable field: a single-line draw_text row (the editor, so focus,
    selection and paste all work). Returns True when the edit changed the
    stored value."""
    from src.lsd.gl_gui.view.core_views.text_editor import draw_text
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