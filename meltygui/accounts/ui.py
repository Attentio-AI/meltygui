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
less than MIN_TEXT_W the buttons wrap onto a second line inside the row
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

ACCOUNTS_PATH = Path.home() / ".lsd" / "accounts.json"

# Row metrics at ui_scale 1.0 (scaled through Melty.px per frame).
ROW_H = 34.0
ROW_GAP = 6.0
PAD_X = 10.0
CORNER = 6.0
BTN_H = 24.0
BTN_PAD_X = 10.0
BTN_GAP = 6.0
SUB_H = 30.0          # secondary rows (field editors, device-code card, model rows)
KIND_HEAD_H = 26.0
MIN_TEXT_W = 150.0    # below this the buttons wrap to their own line
TEXT_INSET = 30.0     # label x inset (after the status lamp)

# Glyphs verified against resources/fontawesome-webfont.ttf (FA 4.x subset).
ICON_TRASH = ""
ICON_REFRESH = ""
ICON_EJECT = ""
ICON_CHEVRON_R = ""
ICON_CHEVRON_D = ""
ICON_PLUS = ""
ICON_KEY = ""
ICON_PASTE = ""
ICON_POWER = ""
ICON_CHIP = ""

STATE_TINTS = {
    "ready": (0.35, 0.85, 0.45),
    "busy": (0.85, 0.75, 0.35),
    "needs_login": (0.95, 0.65, 0.25),
    "warning": (0.95, 0.7, 0.3),
    "error": (0.95, 0.35, 0.35),
    "unknown": (0.55, 0.58, 0.65),
}


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
                    for acct in data.get("accounts", []):
                        if isinstance(acct, dict) and acct.get("id") and acct.get("kind") in KINDS:
                            self[acct["id"]] = acct
                self.error = None
            except Exception as e:
                self.error = f"accounts.json: {e}"
            self.loaded = True
        # default accounts so every kind has a row to act on: the id is the
        # kind name ("anthropic", "copilot", "ollama"); sessions asking for
        # account="default" resolve to it (see `account`).
        for kind in KINDS.values():
            if not any(a.get("kind") == kind.name for a in self.values()):
                self.add(kind.name, account_id=kind.name, save=False)
            for a in self.of_kind(kind.name):
                for f in kind.fields:
                    a.setdefault(f.name, f.default)
        return self

    def save(self):
        with self._lock:
            data = {"accounts": [{k: v for k, v in a.items() if not k.startswith("_")}
                                 for a in self.values()]}
            try:
                ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = ACCOUNTS_PATH.with_suffix(".json.tmp")
                with open(tmp, "w") as f:
                    f.write(json.dumps(data, indent=2))
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(tmp, ACCOUNTS_PATH)
                self.error = None
            except Exception as e:
                self.error = f"accounts.json: {e}"

    def add(self, kind_name, account_id=None, save=True, **fields):
        kind = KINDS[kind_name]
        if account_id is None:
            n = 2
            while f"{kind_name}-{n}" in self:
                n += 1
            account_id = f"{kind_name}-{n}"
        acct = {"id": account_id, "kind": kind_name,
                "label": fields.pop("label", None) or kind.default_label(account_id)}
        for f in kind.fields:
            acct[f.name] = fields.get(f.name, f.default)
        self[account_id] = acct
        if save:
            self.save()
        accounts_changed()
        return acct

    def remove(self, account_id):
        acct = self.pop(account_id, None)
        if acct is not None:
            _drop_sessions_for(acct)
            self.save()
            accounts_changed()

    def set_field(self, account_id, field, value, reprobe=True):
        acct = self.get(account_id)
        if acct is None or acct.get(field) == value:
            return
        acct[field] = value
        if reprobe:
            acct["_status"] = None       # stale - re-probe
            acct.pop("_validated", None)  # credential changed → re-verify with Test
            _drop_sessions_for(acct)     # live sessions hold the old credential
        self.save()
        accounts_changed()

    def of_kind(self, kind_name):
        return sorted((a for a in self.values() if a.get("kind") == kind_name),
                      key=lambda a: (a["id"] != kind_name, a["id"]))


accounts = AccountStore()


def is_default(acct) -> bool:
    return acct.get("id") == acct.get("kind")


def session_account_id(acct) -> str:
    """The `account=` value a session uses for this account — "default" for
    a kind's default entry, so the UI and the providers' default profiles
    pool the SAME session."""
    return "default" if is_default(acct) else acct["id"]


def account(kind_name, account_id="default"):
    """The account dict for (kind, id), or None. "default" is the kind's
    default entry (id == kind name). Loads the store lazily."""
    if not accounts.loaded:
        accounts.load()
    if account_id in ("default", None, ""):
        account_id = kind_name
    acct = accounts.get(account_id)
    if acct is not None and acct.get("kind") == kind_name:
        return acct
    return None


def account_field(kind_name, account_id, field, default=None):
    acct = account(kind_name, account_id)
    if acct is None:
        return default
    v = acct.get(field)
    return v if v not in (None, "") else default


def accounts_changed():
    """Repaint the window (from any thread)."""
    try:
        Melty.cache.invalidate_up_by_obj(accounts, force=True)
    except Exception:
        pass
    try:
        from src.lsd.gl_gui.fim import _wake
        _wake(_window_ds)
    except Exception:
        pass


_window_ds = None


def _drop_sessions_for(acct):
    """Close pooled FIM sessions built on this account so the next request
    re-acquires with the new credential."""
    ids = {acct.get("id"), session_account_id(acct)}
    try:
        from src.lsd.gl_gui import fim
        fim.drop_sessions(lambda s: getattr(s, "account", None) in ids
                          and getattr(s, "KIND", None) == acct.get("kind"))
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
    __slots__ = ("label", "fn", "primary", "icon", "tip", "enabled", "danger")

    def __init__(self, label, fn, primary=False, icon=None, tip="", enabled=True, danger=False):
        self.label = label
        self.fn = fn
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

    def status(self, acct):
        """(state, text) from the cached probe; state ∈ STATE_TINTS."""
        st = acct.get("_status")
        if st is None:
            return ("unknown", "…")
        return st

    def probe(self, acct):
        """Worker thread: return (state, text). May touch the network."""
        return ("unknown", "")

    def actions(self, acct):
        """[Button] — buttons on the row, left to right."""
        return []

    def sub_rows(self, acct):
        """Extra rows under the account: ("code", (user_code, url)) |
        ("model", model dict) | ("note", text)."""
        return []


@account_kind
class AnthropicKind(AccountKind):
    name = "anthropic"
    label = "Anthropic"
    icon = ""
    tint = (0.85, 0.55, 0.35)
    fields = (Field("api_key", "API key", secret=True, placeholder="sk-ant-…"),
              Field("base_url", "Base URL", placeholder="(default)"))

    @staticmethod
    def _profile_present():
        d = Path(os.environ.get("ANTHROPIC_CONFIG_DIR") or (Path.home() / ".config" / "anthropic"))
        return (d / "credentials").is_dir() and any((d / "credentials").glob("*.json"))

    def _source(self, acct):
        key = acct.get("api_key") or ""
        if key:
            return f"key …{key[-4:]}"
        if is_default(acct) and os.environ.get("ANTHROPIC_API_KEY"):
            return "env ANTHROPIC_API_KEY"
        if is_default(acct) and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return "env ANTHROPIC_AUTH_TOKEN"
        if is_default(acct) and self._profile_present():
            return "ant auth profile"
        return None

    def probe(self, acct):
        # Passive probe: NO network (and no `import anthropic`). Just report
        # whether a credential exists - the studio should not fire a web
        # request or import the SDK at startup just to show status. The
        # "Test" button (below) does the one real network check on demand.
        source = self._source(acct)
        if source is None:
            return ("needs_login", "no credentials — paste an API key")
        if acct.get("_validated"):
            return ("ready", f"{source} · verified")
        return ("ready", source)

    def validate(self, acct):
        """The Test button: the ONLY Anthropic web request — list one model
        to confirm the key works. Imports the SDK lazily."""
        source = self._source(acct) or "?"
        try:
            import anthropic
            kw = {"timeout": 15.0, "max_retries": 0}
            if acct.get("api_key"):
                kw["api_key"] = acct["api_key"]
            if acct.get("base_url"):
                kw["base_url"] = acct["base_url"]
            client = anthropic.Anthropic(**kw)
            client.models.list(limit=1)
            client.close()
            acct["_validated"] = True
            acct["_status"] = ("ready", f"{source} · verified")
        except ImportError:
            acct["_status"] = ("error", "anthropic package not installed")
        except Exception as e:
            acct["_validated"] = False
            msg = getattr(e, "message", None) or str(e)
            acct["_status"] = ("error", f"{source} · {msg[:90]}")
        accounts_changed()

    def actions(self, acct):
        return [Button("Paste key", lambda a: _paste_into(a, "api_key"), primary=True),
                Button("Edit", _toggle_edit),
                Button("Test", lambda a: _run_bg(a, lambda: self.validate(a), reprobe=False),
                       tip="Verify the key (one web request)", enabled=self._source(acct) is not None),
                Button("Clear", lambda a: accounts.set_field(a["id"], "api_key", ""),
                       enabled=bool(acct.get("api_key")))]


@account_kind
class CopilotKind(AccountKind):
    name = "copilot"
    label = "GitHub Copilot"
    icon = ""
    tint = (0.45, 0.6, 0.85)
    fields = (Field("config_dir", "Config dir", placeholder="(default ~/.config — shared with the IDE plugins)"),)

    def _session(self, acct, create=True):
        from src.lsd.gl_gui import fim
        from src.lsd.gl_gui.fim_providers.copilot import CopilotSession
        kw = {"account": session_account_id(acct)}
        return fim.session_for(CopilotSession, kw, create=create)

    def probe(self, acct):
        # Passive probe: NO language-server spawn and NO web request. Node +
        # install are filesystem checks; sign-in state is read from a token
        # file on disk. The LS is spawned only when the user clicks Sign in
        # or when FIM actually asks Copilot for a completion - so opening the
        # accounts window (even at startup) costs nothing.
        from src.lsd.gl_gui.fim_providers import copilot as cp
        if cp.find_node() is None:
            return ("error", "node ≥ 20.8 not found")
        if not cp.server_installed():
            return ("needs_login", "language server not installed — Install")
        # If a session is already running (FIM used it, or the user signed in),
        # trust its status instead of the on-disk file.
        try:
            sess = self._session(acct, create=False)
        except Exception:
            sess = None
        if sess is not None and sess.alive():
            st = sess.status()
            if st[0] == "needs_login":
                return ("needs_login", f"sign in: code {st[1]}")
            if sess.user:
                return ("ready", f"signed in as {sess.user}")
            if st[0] == "error":
                return ("needs_login", st[1] or "not signed in")
        user = cp.cached_login_user(acct.get("config_dir"))
        if user:
            return ("ready", f"signed in as {user}")
        return ("needs_login", "not signed in — Sign in")

    def actions(self, acct):
        from src.lsd.gl_gui.fim_providers import copilot as cp
        out = []
        if not cp.server_installed():
            out.append(Button("Install", lambda a: _run_bg(a, lambda: cp.install_server()), primary=True))
            return out
        st = acct.get("_status") or ("unknown", "")
        if st[0] == "ready":
            out.append(Button("Sign out", lambda a: _run_bg(a, lambda: self._session(a).sign_out())))
        else:
            out.append(Button("Sign in", lambda a: _run_bg(a, lambda: self._session(a).sign_in()), primary=True))
        out.append(Button("Edit", _toggle_edit))
        out.append(Button(None, refresh, icon=ICON_REFRESH, tip="Refresh"))
        return out

    def sub_rows(self, acct):
        try:
            sess = self._session(acct, create=False)
        except Exception:
            sess = None
        if sess is not None and sess.login is not None:
            return [("code", sess.login)]
        return []


@account_kind
class OllamaKind(AccountKind):
    name = "ollama"
    label = "Ollama"
    icon = ""
    tint = (0.5, 0.75, 0.6)
    fields = (Field("host", "Host", default="http://localhost:11434"),
              Field("device", "Device", default="auto", hidden=True))

    @staticmethod
    def _client(acct):
        import httpx
        host = (acct.get("host") or "http://localhost:11434").rstrip("/")
        return httpx.Client(base_url=host, timeout=10.0)

    def probe(self, acct):
        from src.lsd.gl_gui.fim_providers import ollama as om
        host = (acct.get("host") or "http://localhost:11434").rstrip("/")
        try:
            with self._client(acct) as c:
                models = om.list_models(c)
        except Exception as e:
            acct["_models"] = []
            return ("error", f"{host} · {str(e)[:60]}")
        acct["_models"] = models
        acct["_gpus"] = om.gpu_inventory()
        loaded = [m for m in models if m["loaded"]]
        fim_like = [m["name"] for m in models
                    if any(k in m["name"] for k in ("coder", "codellama", "starcoder", "codestral", "deepseek-coder"))]
        text = f"{len(models)} models"
        if loaded:
            text += f" · {len(loaded)} loaded on " + ", ".join(sorted({m['where'] or '?' for m in loaded}))
        if not fim_like:
            text += " · no FIM model (pull qwen2.5-coder)"
        return ("ready", text)

    def actions(self, acct):
        from src.lsd.gl_gui.fim_providers import ollama as om
        open_ = bool(acct.get("_models_open"))
        dev = acct.get("device") or "auto"
        return [Button(None, lambda a: _toggle(a, "_models_open"),
                       icon=ICON_CHEVRON_D if open_ else ICON_CHEVRON_R, tip="Models"),
                Button(f"{ICON_CHIP} {om.device_label(dev, acct.get('_gpus'))}", self._cycle_device,
                       tip="Device models load onto (click to cycle)"),
                Button("Edit", _toggle_edit),
                Button(None, refresh, icon=ICON_REFRESH, tip="Refresh")]

    def _cycle_device(self, acct):
        from src.lsd.gl_gui.fim_providers import ollama as om
        choices = om.device_choices(acct.get("_gpus"))
        cur = acct.get("device") or "auto"
        nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
        accounts.set_field(acct["id"], "device", nxt, reprobe=False)

    def sub_rows(self, acct):
        if not acct.get("_models_open"):
            return []
        models = acct.get("_models")
        if models is None:
            return [("note", "loading…")]
        if not models:
            return [("note", "no models — `ollama pull qwen2.5-coder:7b`")]
        return [("model", m) for m in models]

    def model_actions(self, acct, m):
        from src.lsd.gl_gui.fim_providers import ollama as om
        dev = acct.get("device") or "auto"
        target = om.device_label(dev, acct.get("_gpus"))

        def load(a, name=m["name"]):
            from src.lsd.gl_gui.toggles import Toggles
            _run_bg(a, lambda: self._with_client(a, lambda c: om.load_model(
                c, name, a.get("device") or "auto", Toggles.Fim.ollama_keep_alive)))

        def unload(a, name=m["name"]):
            _run_bg(a, lambda: self._with_client(a, lambda c: om.unload_model(c, name)))

        out = [Button(("Move" if m["loaded"] else "Load") + f" → {target}", load, primary=not m["loaded"])]
        if m["loaded"]:
            out.append(Button(None, unload, icon=ICON_EJECT, tip="Unload"))
        return out

    def _with_client(self, acct, fn):
        with self._client(acct) as c:
            return fn(c)


# ──────────────────────────────────────────────────────────────────────────
# Actions / probes
# ──────────────────────────────────────────────────────────────────────────

def refresh(acct):
    """Probe one account on a worker and repaint when it answers."""
    if acct.get("_probing"):
        return
    acct["_probing"] = True
    kind = KINDS[acct["kind"]]

    def run():
        try:
            acct["_status"] = kind.probe(acct)
        except Exception as e:
            acct["_status"] = ("error", str(e)[:90])
        finally:
            acct["_probing"] = False
            acct["_probed_at"] = time.monotonic()
        accounts_changed()

    threading.Thread(target=run, daemon=True, name=f"acct-probe-{acct['id']}").start()


def _run_bg(acct, fn, reprobe=True):
    """Run `fn()` on a worker with the row's busy flag set. `reprobe` re-runs
    the passive probe afterwards (default) — pass False when `fn` already set
    the status itself (e.g. validate), so the trailing probe doesn't clobber
    it."""
    acct["_busy"] = True
    accounts_changed()

    def run():
        try:
            fn()
        except Exception as e:
            acct["_status"] = ("error", str(e)[:90])
        finally:
            acct["_busy"] = False
        if reprobe:
            refresh(acct)
        else:
            accounts_changed()

    threading.Thread(target=run, daemon=True, name=f"acct-action-{acct['id']}").start()


def _paste_into(acct, field):
    try:
        txt = (imgui.get_clipboard_text() or "").strip()
    except Exception:
        txt = ""
    if txt:
        accounts.set_field(acct["id"], field, txt)
        refresh(acct)


def _toggle(acct, key):
    acct[key] = not acct.get(key)
    accounts_changed()


def _toggle_edit(acct):
    _toggle(acct, "_edit")


def _refresh_stale(acct, max_age=120.0):
    st = acct.get("_status")
    at = acct.get("_probed_at", 0.0)
    if (st is None or time.monotonic() - at > max_age) and not acct.get("_probing"):
        refresh(acct)


# ──────────────────────────────────────────────────────────────────────────
# Window
# ──────────────────────────────────────────────────────────────────────────

def _mix(style_manager, tint, value, factor, saturation):
    return style_manager.make_color_rgb(tint[0], tint[1], tint[2], value=value,
                                        factor=factor, saturation_scale=saturation)


def _u32(c, a=1.0):
    return imgui.get_color_u32_rgba(c[0], c[1], c[2], a)


def _fit(text, max_w):
    """`text` ellipsized to `max_w` pixels in the current font."""
    if max_w <= 0:
        return ""
    if imgui.calc_text_size(text)[0] <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if imgui.calc_text_size(text[:mid] + "…")[0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo > 0 else ""


def _fmt_gb(n):
    return f"{n / 1e9:.1f} GB"


@window(input_value=accounts, tint=(0.93, 0.775, 0.46), icon="",
        display_name="Internet Accounts", initial={"width": 760, "height": 460})
@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True)
def draw_internet_accounts(input_value, draw_state, style_manager=None,
                           left_mouse_down=False, **kwargs):
    global _window_ds
    _window_ds = draw_state
    store = input_value
    if not store.loaded:
        store.load()

    # ---- styling (fast_dock recipe) ----
    row_bg_value, row_text_value = 0.06, 0.95
    factor, saturation = 0.90, 1.0
    btn_bg_value, btn_text_value = 0.13, 1.25
    primary_bg_value = 0.22
    hover_bg_boost, hover_text_boost = 0.05, 0.5
    text_saturation = 0.8

    px = Melty.px
    row_h, row_gap = px(ROW_H), px(ROW_GAP)
    pad_x, corner = px(PAD_X), px(CORNER)
    btn_h, btn_pad_x, btn_gap = px(BTN_H), px(BTN_PAD_X), px(BTN_GAP)
    sub_h, head_h = px(SUB_H), px(KIND_HEAD_H)
    min_text_w, text_inset = px(MIN_TEXT_W), px(TEXT_INSET)
    text_nudge_y = px(-1.0)

    if style_manager is None:
        style_manager = Melty.style_manager
    dl = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    cw = draw_state.content_width or (draw_state.width or 300)
    mx, my = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    ev = left_mouse_down
    click = (ev.x, ev.y) if (ev and hasattr(ev, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)
    th = imgui.get_text_line_height()
    lx0, lx1 = x0 + pad_x, x0 + cw - pad_x

    def visible(ry0, ry1):
        return clip is None or not (ry1 < clip[1] or ry0 > clip[3])

    def btn_w(b):
        if b.icon is not None and b.label is None:
            return btn_h
        return imgui.calc_text_size(b.label)[0] + 2 * btn_pad_x

    def buttons_w(btns):
        return sum(btn_w(b) for b in btns) + btn_gap * max(0, len(btns) - 1)

    def draw_buttons(btns, right, by, tint, acct, hint_slot):
        """Right-aligned button strip ending at `right`. Runs click handlers."""
        bx1 = right
        for b in reversed(btns):
            w = btn_w(b)
            bx0 = bx1 - w
            by0, by1 = by, by + btn_h
            enabled = b.enabled and not (acct or {}).get("_busy")
            hov = enabled and hover_ok and bx0 <= mx <= bx1 and by0 <= my <= by1
            bgv = (primary_bg_value if b.primary else btn_bg_value) + (hover_bg_boost if hov else 0.0)
            btint = (0.85, 0.35, 0.35) if b.danger else tint
            bg = _mix(style_manager, btint, bgv if enabled else btn_bg_value * 0.5, factor, saturation)
            tx = _mix(style_manager, btint,
                      (btn_text_value + (hover_text_boost if hov else 0.0)) if enabled else 0.45,
                      factor, text_saturation)
            if enabled and visible(by0, by1):
                add_shadow((bx0, by0, w, btn_h), offset=8 if b.primary else 4,
                           corner_radius=corner, clip=clip)
            dl.add_rect_filled(bx0, by0, bx1, by1, _u32(bg), rounding=corner)
            if b.icon is not None and b.label is None:
                ts = imgui.calc_text_size(b.icon)
                dl.add_text(bx0 + (w - ts[0]) / 2.0, by0 + (btn_h - ts[1]) / 2.0 + text_nudge_y,
                            _u32(tx), b.icon)
                if hov and b.tip:
                    hint_slot[0] = b.tip
            else:
                ts = imgui.calc_text_size(b.label)
                dl.add_text(bx0 + btn_pad_x, by0 + (btn_h - ts[1]) / 2.0 + text_nudge_y, _u32(tx), b.label)
                if hov and b.tip:
                    hint_slot[0] = b.tip
            if enabled and click is not None and bx0 <= click[0] <= bx1 and by0 <= click[1] <= by1:
                try:
                    b.fn(acct)
                except Exception as e:
                    if acct is not None:
                        acct["_status"] = ("error", str(e)[:90])
                request_render()
            bx1 = bx0 - btn_gap
        return bx1 + btn_gap          # left edge of the strip

    # ---- layout pass ----
    # (kind, acct, y, row_h, buttons, wrap, subs) - these are final
    # here so the scroll dummy and the hit-tests agree.
    y = y0
    layout = []
    for kind in KINDS.values():
        layout.append(("head", kind, None, y, head_h, None, False, []))
        y += head_h + px(2)
        for acct in store.of_kind(kind.name):
            btns = list(kind.actions(acct))
            if not is_default(acct):
                btns.append(Button(None, lambda a: store.remove(a["id"]), icon=ICON_TRASH,
                                   tip="Remove account", danger=True))
            label = acct.get("label") or kind.default_label(acct["id"])
            label_w = imgui.calc_text_size(label)[0]
            text_avail = (lx1 - px(6)) - (lx0 + text_inset) - buttons_w(btns) - btn_gap
            wrap = text_avail < max(min_text_w, label_w + px(40))
            rh = row_h + (btn_h + px(6) if wrap else 0)
            subs = []
            if acct.get("_edit"):
                subs.extend(("field", f) for f in kind.fields if not f.hidden)
            subs.extend(kind.sub_rows(acct))
            layout.append(("acct", kind, acct, y, rh, btns, wrap, subs))
            y += rh + len(subs) * sub_h + (px(4) if subs else 0) + row_gap
        y += px(4)
    footer_y = y
    total_h = (footer_y - y0) + row_h
    top_inset = (y0 + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(cw, max(1.0, total_h + max(0.0, top_inset)))

    hint = [None]

    for item in layout:
        what, kind, acct, ry0, rh, btns, wrap, subs = item
        tint = kind.tint
        if what == "head":
            if visible(ry0, ry0 + rh):
                tx = _mix(style_manager, tint, 1.0, factor, text_saturation)
                dl.add_text(lx0 + px(2), ry0 + (rh - th) / 2.0, _u32(tx, 0.85),
                            f"{kind.icon}  {kind.label}")
                draw_buttons([Button(f"{ICON_PLUS} account", lambda a, k=kind: store.add(k.name))],
                             lx1, ry0 + (rh - btn_h) / 2.0, tint, None, hint)
            continue

        _refresh_stale(acct)
        ry1 = ry0 + rh
        st = kind.status(acct)
        if acct.get("_busy") or acct.get("_probing"):
            st = ("busy", st[1] if st[0] != "unknown" else "…")
        if visible(ry0, ry1):
            hov_row = hover_ok and lx0 <= mx <= lx1 and ry0 <= my <= ry1
            bg = _mix(style_manager, tint, row_bg_value + (0.02 if hov_row else 0.0), factor, saturation)
            tx = _mix(style_manager, tint, row_text_value, factor, text_saturation)
            add_shadow((lx0, ry0, lx1 - lx0, rh), offset=2, corner_radius=corner, clip=clip)
            dl.add_rect_filled(lx0, ry0, lx1, ry1, _u32(bg), rounding=corner)
            # status lamp, recessed
            dot_c = STATE_TINTS.get(st[0], STATE_TINTS["unknown"])
            dot_r = px(4.5)
            dcx, dcy = lx0 + px(14), ry0 + row_h / 2.0
            add_shadow((dcx - dot_r, dcy - dot_r, 2 * dot_r, 2 * dot_r), offset=-2,
                       corner_radius=dot_r, clip=clip)
            dl.add_circle_filled(dcx, dcy, dot_r, _u32(dot_c), 16)
            # buttons: on the row line, or wrapped onto their own line
            if wrap:
                strip_left = lx1 - px(6)
                draw_buttons(btns, lx1 - px(6), ry0 + row_h + px(2), tint, acct, hint)
            else:
                strip_left = draw_buttons(btns, lx1 - px(6), ry0 + (row_h - btn_h) / 2.0, tint, acct, hint)
            # label + status text, fitted to the space left of the strip
            label = acct.get("label") or kind.default_label(acct["id"])
            text_x = lx0 + text_inset
            text_right = strip_left - btn_gap - px(4)
            ty = ry0 + (row_h - th) / 2.0 + text_nudge_y
            label_fit = _fit(label, text_right - text_x)
            dl.add_text(text_x, ty, _u32(tx), label_fit)
            sx = text_x + imgui.calc_text_size(label_fit)[0] + px(12)
            status_fit = _fit(st[1], text_right - sx)
            if status_fit:
                dl.add_text(sx, ty, _u32(dot_c, 0.9), status_fit)

        # ---- sub rows ----
        sy = ry1 + px(4)
        for sub in subs:
            sy0, sy1 = sy, sy + sub_h
            sx0, sx1 = lx0 + text_inset, lx1 - px(6)
            if sub[0] == "field":
                f = sub[1]
                if visible(sy0, sy1):
                    lab = _mix(style_manager, tint, 0.7, factor, text_saturation)
                    dl.add_text(sx0, sy0 + (sub_h - th) / 2.0 + text_nudge_y, _u32(lab, 0.85), f.label)
                    fx0 = sx0 + px(110)
                    fw = max(px(120), sx1 - fx0)
                    _draw_field(acct, f, fx0, sy0 + (sub_h - px(26)) / 2.0, fw, px(26), tint)
            elif sub[0] == "code":
                code, url = sub[1]
                if visible(sy0, sy1):
                    add_shadow((sx0, sy0, sx1 - sx0, sub_h), offset=11, corner_radius=corner, clip=clip)
                    cbg = _mix(style_manager, tint, 0.16, factor, saturation)
                    dl.add_rect_filled(sx0, sy0, sx1, sy1, _u32(cbg), rounding=corner)

                    def _open(a, u=url):
                        from src.lsd.gl_gui.fim_providers.copilot import open_url
                        open_url(u)

                    def _copy(a, c=code):
                        try:
                            imgui.set_clipboard_text(c)
                        except Exception:
                            pass

                    cb = [Button("Copy code", _copy), Button("Open browser", _open, primary=True)]
                    left = draw_buttons(cb, sx1 - px(6), sy0 + (sub_h - btn_h) / 2.0, tint, acct, hint)
                    msg = _fit(f"Enter code  {code}  at {url}", left - btn_gap - (sx0 + px(10)))
                    dl.add_text(sx0 + px(10), sy0 + (sub_h - th) / 2.0 + text_nudge_y,
                                _u32((1.0, 0.95, 0.85)), msg)
            elif sub[0] == "note":
                if visible(sy0, sy1):
                    dl.add_text(sx0 + px(6), sy0 + (sub_h - th) / 2.0 + text_nudge_y,
                                _u32((0.7, 0.72, 0.8), 0.8), sub[1])
            elif sub[0] == "model":
                m = sub[1]
                if visible(sy0, sy1):
                    mbg = _mix(style_manager, tint, 0.09 if m["loaded"] else 0.04, factor, saturation)
                    add_shadow((sx0, sy0, sx1 - sx0, sub_h), offset=2 if m["loaded"] else 1,
                               corner_radius=corner, clip=clip)
                    dl.add_rect_filled(sx0, sy0, sx1, sy1, _u32(mbg), rounding=corner)
                    mb = kind.model_actions(acct, m)
                    left = draw_buttons(mb, sx1 - px(6), sy0 + (sub_h - btn_h) / 2.0, tint, acct, hint)
                    lamp = STATE_TINTS["ready"] if m["loaded"] else STATE_TINTS["unknown"]
                    dl.add_circle_filled(sx0 + px(12), sy0 + sub_h / 2.0, px(3.5), _u32(lamp), 12)
                    name_x = sx0 + px(24)
                    tyy = sy0 + (sub_h - th) / 2.0 + text_nudge_y
                    name_fit = _fit(m["name"], min(px(260), left - btn_gap - name_x))
                    dl.add_text(name_x, tyy, _u32(tx), name_fit)
                    ix = name_x + imgui.calc_text_size(name_fit)[0] + px(10)
                    info = _fmt_gb(m["size"])
                    if m["loaded"]:
                        info += f" · loaded on {m['where'] or '?'}"
                    info_fit = _fit(info, left - btn_gap - px(4) - ix)
                    if info_fit:
                        dl.add_text(ix, tyy, _u32((0.72, 0.75, 0.82), 0.85), info_fit)
            sy = sy1

    # ---- footer: file hints / notes / errors ----
    if visible(footer_y, footer_y + row_h):
        note = hint[0] or f"{ACCOUNTS_PATH}"
        if store.error:
            note += f"   ·   {store.error}"
        dl.add_text(lx0, footer_y + (row_h - th) / 2.0, _u32((0.6, 0.62, 0.7), 0.6),
                    _fit(note, lx1 - lx0))

    return False, input_value


def _draw_field(acct, f, x, y, w, h, tint):
    """An editable field: a single-line draw_text row (the editor, so focus,
    selection and paste all work)."""
    from src.lsd.gl_gui.view.core_views.text_editor import draw_text
    key = f"acct_{acct['id']}_{f.name}"
    val = acct.get(f.name) or ""
    imgui.set_cursor_screen_pos((x, y))
    changed, new = draw_text(val, name=key, single_line=True, width=w, height=h,
                             show_widgets=False, show_root_backgrounds=False,
                             show_header=False, show_file_header=False, show_jump_bar=False,
                             shadow=False, use_cache=True, temp=True, autocomplete=False,
                             syntax_highlight=False, line_numbers=False, fim="")
    if changed and isinstance(new, str) and new != val:
        accounts.set_field(acct["id"], f.name, new.strip())


# Hotswap-safe: keep the store object (and its file) across re-exec.
try:
    _prev = Melty.__dict__.get("_internet_accounts_store")
except Exception:
    _prev = None
if _prev is not None and _prev is not accounts:
    accounts = _prev
Melty._internet_accounts_store = accounts
