"""Codex catalog capabilities and saved thread settings, shared with the UI."""
import json
from pathlib import Path

from meltygui.chat.chat_proxy import epoch_seconds


def model_service_tiers(row):
    if "serviceTiers" in row:
        return tuple(tier for tier in row["serviceTiers"] if tier.get("id"))
    return tuple({"id": tier, "name": "Fast" if tier in ("fast", "priority") else tier}
                 for tier in row.get("additionalSpeedTiers", []))


def fast_service_tier(proxy, model):
    return next((tier["id"] for tier in getattr(proxy, "model_service_tiers", {}).get(model, ())
                 if tier["id"] in ("priority", "fast") or tier.get("name", "").lower() == "fast"), None)


def effective_settings(chat, defaults, default_model=None):
    saved = chat.get("codex_settings", {})
    saved_at = chat.get("codex_settings_at", 0)
    metadata = chat.metadata
    result = {}
    for field, config_field in (("model", "model"), ("effort", "model_reasoning_effort"),
                                ("service_tier", "service_tier")):
        explicit = field in metadata and (field != "model" or metadata.get("model_explicit"))
        if explicit and metadata.get(field + "_selected_at", 0) >= saved_at:
            value = metadata[field]
            if field != "service_tier" and value in (None, "", "default"):
                value = defaults.get(config_field)
            result[field] = value
        elif field in saved:
            result[field] = saved[field]
        elif config_field in defaults and (field != "service_tier" or defaults[config_field] is not None):
            result[field] = defaults[config_field]
    result["model"] = result.get("model") or default_model
    return result


class ThreadSettingsReader:
    """Worker-only incremental log reader; initial reads scan backwards."""
    def __init__(self):
        self.files = {}

    @staticmethod
    def parse(line):
        if b'"thread_settings_applied"' not in line:
            return None
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return None
        payload = row.get("payload") or {}
        if row.get("type") != "event_msg" or payload.get("type") != "thread_settings_applied":
            return None
        values = payload.get("thread_settings") or {}
        return {"codex_settings": {target: values[source] for source, target in
                (("model", "model"), ("reasoning_effort", "effort"), ("service_tier", "service_tier"))
                if source in values}, "codex_settings_at": epoch_seconds(row.get("timestamp"))}

    def read(self, path):
        if not path:
            return {}
        path = Path(path)
        previous = self.files.get(path)
        try:
            stat = path.stat()
            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if previous and previous[0] == signature:
                return previous[2]
            with path.open("rb") as handle:
                if previous and previous[0][0] == stat.st_ino and stat.st_size > previous[0][1]:
                    offset, settings = previous[1:]
                    handle.seek(offset)
                    while line := handle.readline():
                        if not line.endswith(b'\n'):
                            break
                        settings = self.parse(line) or settings
                        offset = handle.tell()
                else:
                    offset, settings = stat.st_size, {}
                    position, tail, first = stat.st_size, b"", True
                    while position and not settings:
                        count = min(position, 65536)
                        position -= count
                        handle.seek(position)
                        parts = (handle.read(count) + tail).split(b"\n")
                        tail = parts.pop(0) if position else b""
                        if first and parts:
                            offset -= len(parts.pop())  # exclude the partial trailing record
                            first = False
                        for line in reversed(parts):
                            settings = self.parse(line) or {}
                            if settings:
                                break
            self.files[path] = (signature, offset, settings)
            return settings
        except OSError:
            return previous[2] if previous else {}
