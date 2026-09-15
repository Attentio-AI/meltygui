"""Codex account RPC over stdio. No model requests and no GUI dependencies.

The default row shares the native Codex home; additional rows are isolated.
Codex manages OAuth and token refresh; accounts.json never receives tokens.
"""
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from urllib.parse import quote


class AppServer:
    def __init__(self, home, executable="", timeout=30.0):
        executable = shutil.which(executable or "codex")
        if not executable:
            raise RuntimeError("Codex not found — install Codex CLI or set the Codex executable")
        home = Path(home).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = dict(os.environ, CODEX_HOME=str(home))
        # These can otherwise select an unrelated account in the child.
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            environment.pop(key, None)
        self.timeout = timeout
        self.cancelled = threading.Event()
        self.messages = queue.Queue()
        self.notifications = deque(maxlen=64)
        self.sequence = 0
        command = [os.path.abspath(executable), "app-server"]
        if home != native_home():
            command += ["-c", 'cli_auth_credentials_store="file"']
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, env=environment, close_fds=False)
        self.reader = threading.Thread(target=self._read, daemon=True, name="codex-account-rpc")
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {
                "name": "meltygui", "title": "Melty", "version": "0.1.0"}})
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self.messages.put(None)

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex account request timed out")
        try:
            message = self.messages.get(timeout=min(remaining, 0.2))
        except queue.Empty:
            if self.process.poll() is not None:
                raise RuntimeError("Codex app-server stopped; try Refresh")
            return {}
        if message is None:
            raise RuntimeError("Codex app-server disconnected; try Refresh")
        # No tools are exposed by this account-only client.
        if "method" in message and "id" in message:
            self._send({"id": message["id"], "error": {
                "code": -32601, "message": "Unsupported by Melty accounts"}})
            return {}
        return message

    def request(self, method, params=None):
        """Single worker per client; notifications may precede the reply."""
        self.sequence += 1
        identifier = self.sequence
        self._send({"method": method, "id": identifier, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._receive(deadline)
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "Codex request failed"))
                return message.get("result") or {}
            if message.get("method"):
                self.notifications.append(message)

    def wait_login(self, login_id, timeout):
        deadline = time.monotonic() + timeout
        try:
            while not self.cancelled.is_set():
                message = (self.notifications.popleft() if self.notifications
                           else self._receive(deadline))
                params = message.get("params") or {}
                if (message.get("method") == "account/login/completed"
                        and params.get("loginId") == login_id):
                    if not params.get("success"):
                        raise RuntimeError(params.get("error") or "Codex sign-in failed")
                    return True
            return False
        finally:
            if self.cancelled.is_set() or time.monotonic() >= deadline:
                self.request("account/login/cancel", {"loginId": login_id})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def native_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def account_home(account_id):
    if account_id == "codex":
        return native_home()
    return Path.home() / ".lsd" / "codex" / ("account-" + quote(account_id, safe=""))


def usage_rows(payload):
    """Adapt all returned limit buckets to the Internet Accounts bar schema.

    Missing windows/percentages mean unavailable, never zero usage.
    """
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = payload.get("rateLimits")
        buckets = {legacy.get("limitId") or "codex": legacy} if legacy else {}
    rows = []
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            percent = window.get("usedPercent")
            if not isinstance(percent, (int, float)) or not math.isfinite(percent):
                continue
            percent = max(0.0, min(100.0, percent))
            minutes = window.get("windowDurationMins")
            label = slot.title()
            if isinstance(minutes, (int, float)) and minutes > 0:
                label = (f"{minutes / 1440:g}d" if minutes % 1440 == 0 else
                         f"{minutes / 60:g}h" if minutes % 60 == 0 else f"{minutes:g}m")
            if len(buckets) > 1 or bucket_id != "codex":
                label = f"{bucket.get('limitName') or bucket_id} · {label}"
            severity = ("exceeded" if percent >= 100 else "critical" if percent >= 90
                        else "warning" if percent >= 75 else "normal")
            resets = window.get("resetsAt")
            rows.append({"key": f"{bucket_id}:{slot}", "label": label,
                         "percent": percent, "severity": severity,
                         "resets_at": resets if isinstance(resets, (int, float)) else None,
                         "active": True, "detail": ""})
    return rows
