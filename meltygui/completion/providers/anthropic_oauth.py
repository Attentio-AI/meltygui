"""Anthropic sign-in — the interactive OAuth (PKCE) login that the official
`ant auth login` performs, run in-process so the Internet Accounts window
signs an Anthropic (Google / email) account in with one click and no key
to copy out of the Console.

What it produces is an SDK PROFILE, not a key: `configs/<profile>.json` +
`credentials/<profile>.json` under `$ANTHROPIC_CONFIG_DIR`
(`~/.config/anthropic`) — the exact files `anthropic.Anthropic(profile=…)`
reads and REFRESHES itself (refresh_token grant on expiry, rotated tokens
written back, `anthropic-workspace-id` header from the config), so after
a sign-in nothing here runs again until Sign out. The login runs as the
CLI's public OAuth client: a profile is bound to the client that minted
it (the SDK's refresh sends that client_id), and this way the profile is
one `ant` / the SDKs share. The studio always names its profile
explicitly (`Toggles.InternetAccounts.anthropic_profile`, never "default"
and never the `active_config` pointer), so Claude Code / a bare
`Anthropic()` elsewhere keep whatever login they have.

Flow (`LoginFlow`): bind 127.0.0.1:<ephemeral> → open the Console's
/oauth/authorize consent page in the SYSTEM browser (Google blocks its
sign-in inside embedded web views, so a real browser is the path that
works) → the Console redirects to http://localhost:<port>/callback with
`code` + `state` → exchange the code at /v1/oauth/token (form-encoded,
no beta header — the authorization_code grant) → write the profile.
Everything after `start()` runs on a worker thread; the window reads
`url` / `done` / `error` and calls `cancel()`.

Browser launch goes through copilot.open_url (full-path xdg-open via
posix_spawn — a fork of the CUDA/GL address space stalls the render
thread, project_subprocess_fork_stall).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_BASE_URL = "https://api.anthropic.com"


# ──────────────────────────────────────────────────────────────────────────
# Profile files (the SDK's on-disk format, `anthropic/lib/credentials`)
# ──────────────────────────────────────────────────────────────────────────

def config_dir() -> Path:
    """`$ANTHROPIC_CONFIG_DIR` or `~/.config/anthropic` — the SDK's rule."""
    env = os.environ.get("ANTHROPIC_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "anthropic"


def profile_paths(profile: str) -> tuple[Path, Path]:
    """(configs/<profile>.json, credentials/<profile>.json)."""
    _check_profile_name(profile)
    base = config_dir()
    return base / "configs" / f"{profile}.json", base / "credentials" / f"{profile}.json"


def _check_profile_name(profile: str):
    if (not profile or profile in (".", "..") or "/" in profile or "\\" in profile
            or profile.startswith(".")):
        raise ValueError(f"invalid profile name {profile!r}")


def active_profile_present() -> bool:
    """True when a bare `anthropic.Anthropic()` would find a login on its own:
    an `active_config` pointer or a `configs/default.json` (what `ant auth
    login` / Claude Code write). The studio's named profile is NOT this —
    it is passed as `profile=` explicitly."""
    base = config_dir()
    return (base / "active_config").is_file() or (base / "configs" / "default.json").is_file()


def read_profile(profile: str):
    """Who this profile is signed in as, from the credentials file (no
    network): {"profile", "email", "organization", "workspace",
    "expires_at", "refreshable", "path"} — or None when not signed in."""
    try:
        _, credentials_path = profile_paths(profile)
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(credentials, dict) or not credentials.get("access_token"):
        return None
    return {"profile": profile,
            "email": credentials.get("account_email") or "",
            "organization": credentials.get("organization_name") or "",
            "workspace": credentials.get("workspace_name") or "",
            "expires_at": credentials.get("expires_at"),
            "refreshable": bool(credentials.get("refresh_token")),
            "path": credentials_path}


def summary(info) -> str:
    """The status-row text for read_profile(): the account email (the row
    carries no other label — the kind header names the service)."""
    who = info.get("email") or "signed in"
    if not info.get("refreshable"):
        who += " · no refresh"
    return who


def write_profile(profile: str, token: dict, client_id: str,
                  base_url: str | None = None, console_url: str | None = None) -> Path:
    """Persist a /v1/oauth/token response as the SDK profile `profile`.
    The config file (non-secret intent: org, workspace, base_url) is kept
    when it already exists for this client — like `ant`, a re-login never
    rewrites it — and the credentials file (0600, dir 0700) is always
    rewritten. Returns the credentials path."""
    config_path, credentials_path = profile_paths(profile)
    organization = token.get("organization") or {}
    account = token.get("account") or {}
    workspace = token.get("workspace") or {}

    existing = None
    try:
        existing = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    keep = (isinstance(existing, dict)
            and (existing.get("authentication") or {}).get("type") == "user_oauth"
            and (existing.get("authentication") or {}).get("client_id") == client_id)
    if not keep:
        config = {"version": "1.0",
                  "authentication": {"type": "user_oauth", "client_id": client_id}}
        if organization.get("uuid"):
            config["organization_id"] = organization["uuid"]
        if workspace.get("id"):
            config["workspace_id"] = workspace["id"]
        if base_url and base_url.rstrip("/") != DEFAULT_BASE_URL:
            config["base_url"] = base_url.rstrip("/")
        if console_url:
            config["authentication"]["console_url"] = console_url.rstrip("/")
        _atomic_write(config_path, config, file_mode=0o644, dir_mode=0o755)

    try:
        expires_in = int(token.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    credentials = {"version": "1.0", "type": "oauth_token",
                   "access_token": token["access_token"],
                   "expires_at": int(time.time()) + expires_in}
    if token.get("refresh_token"):
        credentials["refresh_token"] = token["refresh_token"]
    for key, value in (("scope", token.get("scope")),
                       ("organization_uuid", organization.get("uuid")),
                       ("organization_name", organization.get("name")),
                       ("account_email", account.get("email_address")),
                       ("workspace_id", workspace.get("id")),
                       ("workspace_name", workspace.get("name"))):
        if value:
            credentials[key] = value
    _atomic_write(credentials_path, credentials, file_mode=0o600, dir_mode=0o700)
    return credentials_path


def sign_out(profile: str) -> bool:
    """Remove the profile's credentials (the config — org/workspace intent —
    stays, so a re-login lands in the same org without the picker; same as
    `ant auth logout`). True when a credentials file was removed."""
    _, credentials_path = profile_paths(profile)
    try:
        credentials_path.unlink()
        return True
    except FileNotFoundError:
        return False


def _atomic_write(path: Path, data: dict, file_mode: int, dir_mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, dir_mode)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        file.write(json.dumps(data, indent=2))
        file.write("\n")
    os.chmod(tmp, file_mode)
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────────
# OAuth helpers (RFC 7636 PKCE, authorization_code grant)
# ──────────────────────────────────────────────────────────────────────────

def random_urlsafe(n_bytes: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(n_bytes)).decode().rstrip("=")


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_authorize_url(console_url: str, client_id: str, redirect_uri: str, scope: str,
                        state: str, challenge: str,
                        organization_id: str | None = None, workspace_id: str | None = None) -> str:
    """The Console consent page. `organization_id` (`?orgUUID=`) makes the
    Console skip its org picker when the signed-in account is a member;
    `workspace_id` skips the workspace picker. Either omitted → picker."""
    params = [("client_id", client_id), ("redirect_uri", redirect_uri), ("response_type", "code"),
              ("scope", scope), ("state", state), ("code_challenge", challenge),
              ("code_challenge_method", "S256")]
    if workspace_id:
        params.append(("workspace_id", workspace_id))
    if organization_id:
        params.append(("orgUUID", organization_id))
    return f"{console_url.rstrip('/')}/oauth/authorize?{urllib.parse.urlencode(params)}"


def exchange_code(base_url: str, client_id: str, code: str, verifier: str, redirect_uri: str,
                  state: str, timeout_s: float = 30.0) -> dict:
    """Redeem the authorization code at /v1/oauth/token. Form-encoded and
    WITHOUT an anthropic-beta header — that is the authorization_code
    grant's route (`state` is required on this leg too: it is the bound
    CSRF check across both legs)."""
    from meltygui.completion.providers.anthropic_requests import notify_request
    notify_request("POST /v1/oauth/token", "browser sign-in code exchange")
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                   "code_verifier": verifier, "client_id": client_id,
                                   "redirect_uri": redirect_uri, "state": state}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/oauth/token", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json", "User-Agent": "latent-descent"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()
        request_id = error.headers.get("request-id", "") if error.headers else ""
        raise RuntimeError(f"token endpoint returned {error.code}"
                           + (f" (request_id={request_id})" if request_id else "")
                           + (f": {detail[:200]}" if detail else "")) from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"token endpoint unreachable: {error.reason}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RuntimeError("token endpoint returned a non-JSON response") from None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("token endpoint returned no access_token")
    return payload


# ──────────────────────────────────────────────────────────────────────────
# The interactive flow
# ──────────────────────────────────────────────────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    """The loopback redirect target. Only /callback counts (a favicon or a
    stray local request must not read as a failed sign-in); `state` gates
    the code; the FIRST result wins."""

    def log_message(self, *args):        # keep the server's stdout clean
        pass

    def do_GET(self):
        flow = self.server.flow
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/callback":
            self._page(404, "Not found", "")
            return
        query = urllib.parse.parse_qs(parsed.query)

        def param(name):
            return (query.get(name) or [""])[0]

        if param("error"):
            flow._deliver(error=f"authorization denied: {param('error')}: {param('error_description')}")
            self._page(400, "✗ Sign-in failed", f"{param('error')}: {param('error_description')}")
        elif param("state") != flow.state:
            flow._deliver(error="state mismatch (possible CSRF) — start the sign-in again")
            self._page(400, "✗ State mismatch",
                       "The callback state did not match this sign-in attempt. "
                       "Do not retry from this browser session.")
        elif not param("code"):
            flow._deliver(error="the authorization callback carried no code")
            self._page(400, "✗ Missing code", "The authorization callback did not include a code.")
        else:
            flow._deliver(code=param("code"))
            self._page(200, "✓ Signed in", "You can close this tab and return to Latent Descent.")

    def _page(self, status, heading, body):
        content = (f'<html><body style="font-family:system-ui;text-align:center;padding:4em">'
                   f'<h2>{html.escape(heading)}</h2><p>{html.escape(body)}</p></body></html>').encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class LoginFlow:
    """One browser sign-in. `start()` binds the loopback port, builds the
    consent URL (`url` — the window's Copy link / Open browser), opens the
    browser and returns; a worker then waits for the redirect, exchanges
    the code and writes the profile. Read `done` / `error` / `result`
    (read_profile() of the new login); `on_change(flow)` fires from the
    worker on every transition. `cancel()` ends the wait within
    `poll_s`."""

    def __init__(self, profile: str, *, client_id: str | None = None, scope: str | None = None,
                 console_url: str | None = None, base_url: str | None = None,
                 organization_id: str | None = None, workspace_id: str | None = None,
                 open_browser: bool = True, timeout_s: float | None = None, on_change=None):
        from meltygui.toggles import Toggles
        self.profile = profile
        self.client_id = client_id or Toggles.InternetAccounts.anthropic_oauth_client_id
        self.scope = scope or Toggles.InternetAccounts.anthropic_oauth_scope
        self.console_url = (console_url or Toggles.InternetAccounts.anthropic_console_url).rstrip("/")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self.open_browser = open_browser
        self.timeout_s = (Toggles.InternetAccounts.anthropic_login_timeout_s
                          if timeout_s is None else timeout_s)
        self.on_change = on_change
        # How often the worker re-checks cancel / timeout between redirects.
        self.poll_s = 0.5

        self.verifier = random_urlsafe(48)
        self.state = random_urlsafe(24)
        self.redirect_uri = None
        self.url = None
        self.error = None
        self.result = None
        self.done = False
        self.started_at = None
        self._popup = None
        self._server = None
        self._thread = None
        self._code = None
        self._callback_error = None
        self._got = threading.Event()
        self._cancelled = threading.Event()

    # -- public -----------------------------------------------------------

    def start(self) -> str:
        if self.organization_id is None:
            self.organization_id = self._stored_organization_id()
        self._server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self._server.flow = self
        self._server.timeout = self.poll_s
        port = self._server.server_address[1]
        # `localhost` in the redirect (the callback form), bound on 127.0.0.1 -
        # the same pairing the CLI uses.
        self.redirect_uri = f"http://localhost:{port}/callback"
        self.url = build_authorize_url(self.console_url, self.client_id, self.redirect_uri,
                                       self.scope, self.state, pkce_challenge(self.verifier),
                                       organization_id=self.organization_id,
                                       workspace_id=self.workspace_id)
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"anthropic-login-{self.profile}")
        self._thread.start()
        if self.open_browser:
            self.open_in_browser()
        return self.url

    def open_in_browser(self) -> bool:
        if not self.url:
            return False
        import meltygui.completion.providers.oauth_popup as oauth_popup
        popup = oauth_popup.open_auth_popup(self.url)   # falls back to xdg-open itself
        if popup is not None:
            self._popup = popup
        return True

    def cancel(self):
        self._cancelled.set()

    @property
    def alive(self) -> bool:
        return self._thread is not None and not self.done

    # -- worker -----------------------------------------------------------

    def _deliver(self, code=None, error=None):
        if self._got.is_set():
            return                      # first request wins (browser prefetch, reloads)
        self._code, self._callback_error = code, error
        self._got.set()

    def _run(self):
        deadline = time.monotonic() + self.timeout_s
        try:
            while (not self._got.is_set() and not self._cancelled.is_set()
                   and time.monotonic() < deadline):
                self._server.handle_request()
        except Exception as error:
            self._callback_error = f"callback listener failed: {error}"
            self._got.set()
        finally:
            try:
                self._server.server_close()
            except Exception:
                pass
        if self._cancelled.is_set():
            self.error = "sign-in cancelled"
        elif not self._got.is_set():
            self.error = "timed out waiting for the browser sign-in"
        elif self._callback_error:
            self.error = self._callback_error
        else:
            try:
                token = exchange_code(self.base_url, self.client_id, self._code, self.verifier,
                                      self.redirect_uri, self.state)
                write_profile(self.profile, token, self.client_id,
                              base_url=self.base_url, console_url=self.console_url)
                self.result = read_profile(self.profile)
            except Exception as error:
                self.error = f"sign-in failed: {error}"
        self.done = True
        if self._popup is not None:
            self._popup.close()                 # the flow is over - take the popup with it
        self._notify()

    def _notify(self):
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:
                pass

    def _stored_organization_id(self):
        """The org a previous login of this profile bound (config file) —
        passed as the Console's ?orgUUID hint so a re-login skips the org
        picker, like `ant`."""
        try:
            config_path, _ = profile_paths(self.profile)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            return config.get("organization_id") or None
        except (OSError, ValueError, AttributeError):
            return None