"""Incremental user-message timestamps from provider session logs (worker only)."""
import json
from pathlib import Path
from .chat_proxy import epoch_seconds


class UserMessageTimes:
    def __init__(self, provider):
        self.provider = provider
        self.files = {}

    def timestamp(self, line):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return 0.0
        if self.provider == 'codex':
            payload = row.get('payload') or {}
            user = ((row.get('type') == 'event_msg' and payload.get('type') == 'user_message')
                    or (row.get('type') == 'response_item' and payload.get('type') == 'message'
                        and payload.get('role') == 'user'))
        else:
            message = row.get('message') or {}
            content = message.get('content', [])
            user = (row.get('type') == 'user' and not row.get('isSidechain')
                    and not row.get('parentToolUseId') and not row.get('isMeta')
                    and (isinstance(content, str) or any(
                        block.get('type') in ('text', 'image') for block in content if isinstance(block, dict))))
        return epoch_seconds(row.get('timestamp')) if user else 0.0

    def read(self, path):
        if not path:
            return 0.0
        path = Path(path)
        previous = self.files.get(path)
        try:
            stat = path.stat()
            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, 2)
            if previous and previous[0] == signature:
                return previous[2]
            with path.open('rb') as handle:
                if (previous and previous[0][-1] == 2 and len(previous[0]) == 4
                        and previous[0][0] == stat.st_ino and stat.st_size > previous[0][1]):
                    offset, latest = previous[1:]
                    handle.seek(offset)
                    while line := handle.readline():
                        if not line.endswith(b'\n'):
                            break
                        latest = max(latest, self.timestamp(line))
                        offset = handle.tell()
                else:
                    # Initial scan starts at the end; long histories normally
                    # need only the final turn. Leave partial rows for next poll.
                    offset, latest, position, tail = stat.st_size, 0.0, stat.st_size, b''
                    first = True
                    while position and not latest:
                        count = min(position, 65536)
                        position -= count
                        handle.seek(position)
                        parts = (handle.read(count) + tail).split(b'\n')
                        tail = parts.pop(0) if position else b''
                        if first and not parts:
                            continue
                        if first:
                            partial = parts.pop()
                            offset -= len(partial)
                            first = False
                        for line in reversed(parts):
                            latest = self.timestamp(line)
                            if latest:
                                break
            self.files[path] = (signature, offset, latest)
            return latest
        except OSError:
            return previous[2] if previous else 0.0
