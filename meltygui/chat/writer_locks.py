"""Read Linux's held file locks without acquiring or creating session locks."""
import os
from pathlib import Path


def codex_writer_locks(home, locks_path='/proc/locks'):
    try:
        held = {}
        for line in Path(locks_path).read_text().splitlines():
            fields = line.split()
            # Lock requests have a '->' marker; only held WRITE locks
            # mean the session has a writer. File existence alone is not a lock.
            if len(fields) < 6 or fields[1] == '->' or fields[3] != 'WRITE':
                continue
            major, minor, inode = fields[5].split(':')
            held[(int(major, 16), int(minor, 16), int(inode))] = int(fields[4])
        locked = {}
        for path in (Path(home) / 'thread-writer-locks').glob('*.lock'):
            if path.name.startswith('.'):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino) in held:
                locked[path.stem] = lock_owner(held[(os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)])
        return locked
    except (OSError, ValueError):
        return None  # unavailable is not evidence that previous locks released


def lock_owner(pid):
    info = {"pid": pid, "client": "Unknown client", "location": "local machine"}
    try:
        executable = os.readlink(f"/proc/{pid}/exe")
        name = Path(f"/proc/{pid}/comm").read_text().strip()
        info["client"] = "Codex desktop app" if "/chatgpt/" in executable else name
        # Read only process names/parent IDs; never arguments or environment
        # variables, which may contain credentials or conversation contents.
        current = pid
        for _ in range(12):
            status = Path(f"/proc/{current}/status").read_text().splitlines()
            parent = int(next(line.split()[1] for line in status if line.startswith("PPid:")))
            if not parent:
                break
            parent_name = Path(f"/proc/{parent}/comm").read_text().strip()
            if parent_name in ("sshd", "sshd-session"):
                info["location"] = "local machine, launched through SSH"
                break
            current = parent
    except (OSError, ValueError, StopIteration):
        pass
    return info


def lock_message(owner=None):
    owner = owner or {}
    identity = owner.get("client", "another client")
    if owner.get("pid", -1) > 0:
        identity += f" (PID {owner['pid']}, {owner.get('location', 'local machine')})"
    return (f"This conversation is open for writing in {identity}.\n"
            "To release it, finish or stop its active turn, then close the owning app or CLI session. "
            "Stopping a turn alone may leave the session locked. You can still try Send if this status looks stale.\n"
            "Your unsent message stays in the composer. A remotely connected client cannot be identified from this local lock.")
