"""GitHub Copilot FIM provider — drives the official Copilot Language
Server (`@github/copilot-language-server`, LSP over stdio) the way
copilot.vim / the JetBrains plugin do. One `CopilotSession` = one LS
process = one GitHub account: the LS keeps its token under
`$XDG_CONFIG_HOME/github-copilot/`, so a profile with a different
`config_dir` is a different login (work vs home). The default (no
config_dir) shares `~/.config/github-copilot` with the IDE plugins.

Install (one-off, also offered by the Internet Accounts window):
    npm install --prefix ~/.lsd/copilot-ls @github/copilot-language-server

Process spawn uses a full node path, `cwd=None`, `close_fds=False` so
CPython takes the posix_spawn path (a fork of the CUDA/GL address space
stalls the render thread — project_subprocess_fork_stall).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from meltygui.completion.fim import FimRequest
from meltygui.completion.fim import FimResult
from meltygui.completion.fim import FimSession
from meltygui.completion.fim import fim_provider

LS_ROOT = Path.home() / ".lsd" / "copilot-ls"
LS_ENTRY = LS_ROOT / "node_modules" / "@github" / "copilot-language-server" / "dist" / "language-server.js"
EDITOR_INFO = {"name": "LatentDescent", "version": "1.0"}
PLUGIN_INFO = {"name": "meltygui-fim", "version": "1.0"}


def find_node() -> str | None:
    """Full path to a node ≥ 20.8 (PATH, then nvm installs, newest first)."""
    p = shutil.which("node")
    if p:
        return p
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        vers = sorted(nvm.iterdir(), key=lambda d: d.name, reverse=True)
        for d in vers:
            cand = d / "bin" / "node"
            if cand.exists():
                return str(cand)
    return None


def server_installed() -> bool:
    return LS_ENTRY.exists()


def cached_login_user(config_dir=None):
    """The signed-in GitHub user from the Copilot token file on disk, or
    None — WITHOUT spawning the language server. Lets the accounts window
    show sign-in state at startup with no process spawn and no web request.
    `config_dir` is the account's XDG_CONFIG_HOME (None = the user default,
    shared with the IDE plugins)."""
    base = Path(os.path.expanduser(config_dir)) if config_dir else (Path.home() / ".config")
    for name in ("apps.json", "hosts.json"):
        f = base / "github-copilot" / name
        try:
            if not f.exists():
                continue
            data = json.loads(f.read_text())
        except Exception:
            continue
        for entry in (data.values() if isinstance(data, dict) else []):
            if isinstance(entry, dict):
                if entry.get("oauth_token") or entry.get("user"):
                    return entry.get("user") or "signed in"
    return None


def install_server(log=print) -> bool:
    """`npm install` the language server under LS_ROOT (network). Blocking —
    call from a worker thread."""
    node = find_node()
    if node is None:
        log("copilot: node not found")
        return False
    npm = str(Path(node).parent / "npm")
    if not os.path.exists(npm):
        npm = shutil.which("npm") or npm
    LS_ROOT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run([npm, "install", "--prefix", str(LS_ROOT), "--no-fund", "--no-audit",
                            "@github/copilot-language-server"],
                           capture_output=True, text=True, env=env, close_fds=False, timeout=600)
    except Exception as e:
        log(f"copilot: npm install failed: {e}")
        return False
    if r.returncode != 0:
        log(f"copilot: npm install failed: {r.stderr[-400:]}")
        return False
    return server_installed()


# ──────────────────────────────────────────────────────────────────────────
# JSON-RPC over stdio
# ──────────────────────────────────────────────────────────────────────────

class _RpcError(RuntimeError):
    pass


class _Rpc:
    """Minimal LSP transport: Content-Length framing, id-correlated
    requests from any thread, notification handlers, and replies to the
    few server→client requests the LS makes."""

    def __init__(self, proc, on_notification):
        self.proc = proc
        self._on_notification = on_notification
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._pending = {}        # id -> [Event, result, error]
        self._next_id = 1
        self.alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="copilot-ls-reader")
        self._reader.start()

    def _send(self, msg):
        data = json.dumps(msg).encode("utf-8")
        head = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        with self._wlock:
            try:
                self.proc.stdin.write(head + data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                self.alive = False
                raise _RpcError(f"language server pipe closed: {e}")

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params=None, timeout=60.0, cancel_event=None):
        with self._plock:
            rid = self._next_id
            self._next_id += 1
            slot = [threading.Event(), None, None]
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout if timeout else None
        while not slot[0].wait(0.05):
            if cancel_event is not None and cancel_event.is_set():
                with self._plock:
                    self._pending.pop(rid, None)
                try:
                    self.notify("$/cancelRequest", {"id": rid})
                except _RpcError:
                    pass
                return None
            if not self.alive:
                raise _RpcError("language server exited")
            if deadline is not None and time.monotonic() > deadline:
                with self._plock:
                    self._pending.pop(rid, None)
                raise _RpcError(f"{method}: timed out")
        if slot[2] is not None:
            err = slot[2]
            raise _RpcError(f"{method}: {err.get('message') if isinstance(err, dict) else err}")
        return slot[1]

    def _reply(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        try:
            self._send(msg)
        except _RpcError:
            pass

    def _read_loop(self):
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        raise EOFError
                    if line in (b"\r\n", b"\n"):
                        break
                    k, _, v = line.decode("ascii", "replace").partition(":")
                    if k.strip().lower() == "content-length":
                        length = int(v.strip())
                if length is None:
                    continue
                body = out.read(length)
                if len(body) < length:
                    raise EOFError
                try:
                    msg = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._dispatch(msg)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            self.alive = False
            with self._plock:
                for slot in self._pending.values():
                    slot[2] = {"message": "language server exited"}
                    slot[0].set()
                self._pending.clear()

    def _dispatch(self, msg):
        if "id" in msg and "method" in msg:
            # server → client request: answer what we can, null otherwise
            method = msg["method"]
            params = msg.get("params") or {}
            if method == "workspace/configuration":
                items = params.get("items") or []
                self._reply(msg["id"], [{} for _ in items])
            elif method == "window/showMessageRequest":
                self._reply(msg["id"], None)
            elif method == "window/showDocument":
                uri = params.get("uri")
                if uri:
                    open_url(uri)
                self._reply(msg["id"], {"success": bool(uri)})
            else:
                self._reply(msg["id"], None)
            return
        if "id" in msg:
            with self._plock:
                slot = self._pending.pop(msg["id"], None)
            if slot is not None:
                slot[1] = msg.get("result")
                slot[2] = msg.get("error")
                slot[0].set()
            return
        method = msg.get("method")
        if method:
            try:
                self._on_notification(method, msg.get("params") or {})
            except Exception:
                pass


def open_url(url: str):
    """Open a URL in the user's browser without fork()ing the studio:
    full-path xdg-open via posix_spawn (see module docstring)."""
    for cmd in ("xdg-open", "gio", "open"):
        exe = shutil.which(cmd)
        if exe:
            args = [exe, url] if cmd != "gio" else [exe, "open", url]
            try:
                subprocess.Popen(args, close_fds=False, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    return False


# ──────────────────────────────────────────────────────────────────────────
# Session
# ──────────────────────────────────────────────────────────────────────────

class CopilotSession(FimSession):
    """One language-server process / one GitHub login. `account` names an
    Internet Accounts entry of kind "copilot" whose `config_dir` is the
    login's XDG_CONFIG_HOME (empty = the user's default, shared with the
    IDE plugins); an explicit `config_dir` overrides it. `workspace` is
    the folder reported to the LS."""
    KIND = "copilot"

    def __init__(self, account="default", config_dir=None, workspace=None, node=None):
        self.account = account
        if config_dir is None:
            from meltygui.accounts.internet_accounts import account_field
            config_dir = account_field("copilot", account, "config_dir")
        self.config_dir = os.path.expanduser(config_dir) if config_dir else None
        from meltygui.core.paths import application_root
        self.workspace = workspace or str(application_root())
        self.node = node or find_node()
        self._lock = threading.RLock()
        self._docs = {}            # uri -> (version, text)
        self._status = ("starting",)
        self._kind = "Unknown"
        self._message = ""
        self._busy = False
        self.user = None
        self.login = None          # (user_code, verification_uri) while a login flow is open
        self.proc = None
        self.rpc = None
        self._start()

    # ── lifecycle ───────────────────────────────────────────────────────
    def _start(self):
        if self.node is None:
            raise RuntimeError("node not found (need Node ≥ 20.8 for the Copilot language server)")
        if not server_installed():
            raise RuntimeError(f"Copilot language server not installed under {LS_ROOT} "
                               f"(Internet Accounts → Copilot → Install)")
        env = dict(os.environ)
        if self.config_dir:
            env["XDG_CONFIG_HOME"] = self.config_dir
            Path(self.config_dir).mkdir(parents=True, exist_ok=True)
        # --dns-result-order=ipv4first: Node ≥ 17 tries AAAA records first;
        # on a host whose IPv6 route is dead every GitHub access inside the LS
        # ends in an undici timeout (~30 s) before its fallback fetcher kicks
        # in - the first completion took 30 s here. IPv4-first makes it fast.
        self.proc = subprocess.Popen(
            [self.node, "--dns-result-order=ipv4first", str(LS_ENTRY), "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, close_fds=False)
        self.rpc = _Rpc(self.proc, self._on_notification)
        init = {
            "processId": os.getpid(),
            "rootUri": Path(self.workspace).as_uri(),
            "workspaceFolders": [{"uri": Path(self.workspace).as_uri(), "name": Path(self.workspace).name}],
            "capabilities": {"workspace": {"workspaceFolders": True, "configuration": True},
                             "window": {"showDocument": {"support": True}}},
            "initializationOptions": {"editorInfo": EDITOR_INFO, "editorPluginInfo": PLUGIN_INFO},
        }
        self.rpc.request("initialize", init, timeout=60.0)
        self.rpc.notify("initialized", {})
        self.rpc.notify("workspace/didChangeConfiguration",
                        {"settings": {"telemetry": {"telemetryLevel": "off"}}})
        self._status = ("ready",)
        # Warm-up: the LS's FIRST completion pays for auth + fallback fetcher
        # negotiation (~30 s on a host whose primary fetcher can't reach
        # GitHub); do it on a throwaway document now so the user's first
        # real request is the ~0.3 s warm-up.
        threading.Thread(target=self._warm, daemon=True, name="copilot-warm").start()

    def _warm(self):
        try:
            self.check_status()
            if self._kind == "Normal":
                text = "def add(a, b):\n    return "
                self.complete("untitled:warmup.py", text, len(text), threading.Event())
        except Exception:
            pass
        _notify_account_change()

    def close(self):
        rpc, proc = self.rpc, self.proc
        self.rpc = None
        self.proc = None
        if rpc is not None and rpc.alive:
            try:
                rpc.request("shutdown", {}, timeout=5.0)
                rpc.notify("exit", {})
            except Exception:
                pass
        if proc is not None:
            try:
                proc.wait(timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def restart(self):
        self.close()
        self._docs.clear()
        self._start()

    # ── status & auth ───────────────────────────────────────────────────
    def _on_notification(self, method, params):
        if method == "didChangeStatus":
            self._kind = params.get("kind") or "Normal"
            self._message = params.get("message") or ""
            self._busy = bool(params.get("busy"))
            if self._kind == "Normal" and self.login is not None and not self._busy:
                self.login = None
            _notify_account_change()
        elif method == "window/logMessage":
            pass

    def alive(self):
        return self.rpc is not None and self.rpc.alive

    def status(self):
        if self.rpc is None or not self.rpc.alive:
            return ("error", "language server exited")
        if self.login is not None:
            code, uri = self.login
            return ("needs_login", code, uri)
        if self._kind == "Error":
            return ("error", self._message or "not signed in")
        if self._kind == "Warning":
            return ("warning", self._message)
        return ("ready",)

    def check_status(self):
        """Ask the LS who is signed in (`signIn` answers AlreadySignedIn
        without starting a flow when a token exists). Returns the user
        login or None."""
        try:
            res = self.rpc.request("signIn", {}, timeout=30.0) or {}
        except _RpcError as e:
            self._kind, self._message = "Error", str(e)
            return None
        st = res.get("status")
        if st == "AlreadySignedIn" or res.get("user"):
            self.user = res.get("user") or self.user
            self._kind, self._message = "Normal", ""
            return self.user
        # A device flow was started that we did not ask for: save its
        # details so the UI can show them, but don't block on it.
        if res.get("userCode"):
            self.login = (res.get("userCode"), res.get("verificationUri") or "https://github.com/login/device")
            self._pending_login_cmd = res.get("command")
            self._kind, self._message = "Error", "not signed in"
        return None

    _pending_login_cmd = None

    def sign_in(self, open_browser=True):
        """Start (or resume) the device flow. Returns (user_code, uri) or
        the signed-in user. The completion runs on a worker: the LS's
        finish command blocks until the user approves in the browser."""
        if self.login is None:
            res = self.rpc.request("signIn", {}, timeout=30.0) or {}
            if res.get("status") == "AlreadySignedIn" or (res.get("user") and not res.get("userCode")):
                self.user = res.get("user") or self.user
                self._kind, self._message = "Normal", ""
                return self.user
            if not res.get("userCode"):
                raise RuntimeError(f"unexpected signIn reply: {res}")
            self.login = (res["userCode"], res.get("verificationUri") or "https://github.com/login/device")
            self._pending_login_cmd = res.get("command")
        code, uri = self.login
        if open_browser:
            open_url(uri)
        cmd = self._pending_login_cmd
        if cmd:
            self._pending_login_cmd = None

            def finish():
                try:
                    res = self.rpc.request("workspace/executeCommand",
                                           {"command": cmd.get("command"),
                                            "arguments": cmd.get("arguments") or []},
                                           timeout=600.0) or {}
                    if isinstance(res, dict):
                        self.user = res.get("user") or self.user
                        if res.get("status") in ("OK", "AlreadySignedIn") or res.get("user"):
                            self._kind, self._message = "Normal", ""
                except Exception as e:
                    self._kind, self._message = "Error", str(e)
                self.login = None
                _notify_account_change()

            threading.Thread(target=finish, daemon=True, name="copilot-signin").start()
        return (code, uri)

    def sign_out(self):
        try:
            self.rpc.request("signOut", {}, timeout=30.0)
        except _RpcError as e:
            self._kind, self._message = "Error", str(e)
        self.user = None
        self.login = None
        self._kind, self._message = "Error", "not signed in"
        _notify_account_change()

    # ── documents / completion ──────────────────────────────────────────
    @staticmethod
    def _uri(path):
        try:
            return Path(path).resolve().as_uri()
        except Exception:
            return "untitled:" + str(path).replace(" ", "_")

    def sync_document(self, path, text, version=None, language="python"):
        uri = self._uri(path)
        with self._lock:
            prev = self._docs.get(uri)
            if prev is not None and prev[1] == text:
                return uri, prev[0]
            v = (prev[0] + 1) if prev is not None else 1
            if prev is None:
                self.rpc.notify("textDocument/didOpen",
                                {"textDocument": {"uri": uri, "languageId": language,
                                                  "version": v, "text": text}})
            else:
                self.rpc.notify("textDocument/didChange",
                                {"textDocument": {"uri": uri, "version": v},
                                 "contentChanges": [{"text": text}]})
            self._docs[uri] = (v, text)
            return uri, v

    def complete(self, path, text, cursor, cancelled, language="python") -> FimResult:
        uri, v = self.sync_document(path, text, language=language)
        line = text.count("\n", 0, cursor)
        ls = text.rfind("\n", 0, cursor) + 1
        char = _utf16_len(text[ls:cursor])
        res = self.rpc.request("textDocument/inlineCompletion",
                               {"textDocument": {"uri": uri, "version": v},
                                "position": {"line": line, "character": char},
                                "context": {"triggerKind": 2},
                                "formattingOptions": {"tabSize": 4, "insertSpaces": True}},
                               timeout=30.0, cancel_event=cancelled)
        items = (res or {}).get("items") or []
        if not items:
            return FimResult("", provider="copilot")
        texts = []
        for it in items:
            ins = it.get("insertText") or ""
            rng = it.get("range") or {}
            start = rng.get("start") or {}
            end = rng.get("end") or {}
            # The item's range usually starts at the line start and
            # re-states what's already typed: trim what precedes the cursor
            # and count what follows it as a suffix.
            if start.get("line") == line and start.get("character", char) < char:
                already = text[ls + _utf16_to_index(text[ls:], start["character"]):cursor]
                if ins.startswith(already):
                    ins = ins[len(already):]
            replace_to = 0
            if end.get("line") == line and end.get("character", char) > char:
                replace_to = _utf16_to_index(text[cursor:], end["character"] - char)
            texts.append((ins, replace_to, it))
        first_ins, first_rep, first_item = texts[0]
        return FimResult(first_ins, replace_to=first_rep,
                         alternatives=tuple(t for t, _r, _i in texts[1:] if t),
                         token=first_item, provider="copilot")

    def shown(self, item):
        try:
            self.rpc.notify("textDocument/didShowCompletion", {"item": item})
        except _RpcError:
            pass

    def accepted(self, item, accepted_length):
        try:
            full = len(item.get("insertText") or "")
            if accepted_length >= full:
                cmd = item.get("command") or {}
                if cmd.get("command"):
                    self.rpc.request("workspace/executeCommand",
                                     {"command": cmd["command"], "arguments": cmd.get("arguments") or []},
                                     timeout=10.0)
            else:
                self.rpc.notify("textDocument/didPartiallyAcceptCompletion",
                                {"item": item, "acceptedLength": accepted_length})
        except _RpcError:
            pass


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _utf16_to_index(s: str, units: int) -> int:
    n = 0
    for i, c in enumerate(s):
        if n >= units:
            return i
        n += 2 if ord(c) > 0xFFFF else 1
    return len(s)


def _notify_account_change():
    """Repaint whoever shows account status (the Internet Accounts window)."""
    try:
        from meltygui.accounts.internet_accounts import accounts_changed
        accounts_changed()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Provider
# ──────────────────────────────────────────────────────────────────────────

@fim_provider(name="copilot", session=CopilotSession)
def copilot_fim(req: FimRequest, session: CopilotSession, neighbors=4) -> FimResult:
    """Copilot completion for the request's file. The LS builds its own
    prompt from the documents it has seen, so the editor's context reaches
    it as neighbor documents: the files of up to `neighbors` referenced
    definitions are opened alongside."""
    st = session.status()
    if st[0] == "needs_login":
        raise RuntimeError(f"Copilot: sign in with code {st[1]} at {st[2]}")
    if st[0] == "error":
        raise RuntimeError(f"Copilot: {st[1]}")
    if req.context is not None and neighbors:
        seen = 0
        for it in req.context.items:
            if it.kind in ("definition", "signature") and it.path and it.path != req.path:
                try:
                    from meltygui.code.symbol_roster import file_text
                    session.sync_document(it.path, file_text(it.path))
                    seen += 1
                except Exception:
                    pass
                if seen >= neighbors:
                    break
    return session.complete(req.path or "untitled", req.text, req.cursor, req.cancelled,
                            language=req.language or "python")


def _copilot_shown(req, result):
    pass


def _copilot_accept(result, accepted_chars):
    pass


copilot_fim._on_shown = _copilot_shown
copilot_fim._on_accept = _copilot_accept
