"""Multiplexed, persistent version of the account-only stdio transport."""
import json
import queue
import threading

from meltygui.completion.providers.codex_accounts import AppServer


class CodexTransport(AppServer):
    def __init__(self, home, executable="", timeout=30, on_event=None):
        self.pending = {}
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.on_event = on_event or (lambda event: None)
        super().__init__(home, executable, timeout)

    def _send(self, message):
        with self.write_lock:
            super()._send(message)

    def _read(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line)
                if "method" in message:
                    self.on_event(message)
                else:
                    with self.pending_lock:
                        response = self.pending.get(message.get("id"))
                    if response is not None:
                        response.put(message)
        except (ValueError, OSError) as error:
            self.on_event({"method": "transport/error", "params": {"message": str(error)}})
        finally:
            with self.pending_lock:
                for response in self.pending.values():
                    response.put({"error": {"message": "Codex disconnected"}})
            self.on_event({"method": "transport/error", "params": {"message": "Codex disconnected"}})

    def request(self, method, params=None):
        response = queue.Queue()
        with self.pending_lock:
            self.sequence += 1
            identifier = self.sequence
            self.pending[identifier] = response
        try:
            self._send({"method": method, "id": identifier, "params": params or {}})
            try:
                message = response.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError(f"Codex timed out: {method}") from None
            if "error" in message:
                raise RuntimeError(message["error"].get("message", "Codex request failed"))
            return message.get("result") or {}
        finally:
            with self.pending_lock:
                self.pending.pop(identifier, None)

    def answer(self, request_id, result=None, error=None):
        self._send({"id": request_id, **({"error": error} if error else {"result": result})})
