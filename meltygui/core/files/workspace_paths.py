"""Persist workspace file locations without an iOS container's absolute path.

The saved value is a POSIX relative path, not a URL or a replacement filesystem.
Decode it to an ordinary Path before using the existing editor/file APIs. Owners
must explicitly encode their path fields when saving and decode when loading;
arbitrary strings in a session must not be reinterpreted as file locations.

These helpers cover the local Documents/Projects workspace. External document
providers require native security-scoped access and are not represented here.
"""
from pathlib import Path, PurePosixPath

from meltygui.core.runtime.paths import workspace_root


def _root(root):
    return Path(workspace_root() if root is None else root).resolve()


def _inside(path, root):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f'path is outside the workspace: {path}')
    return resolved


def encode_workspace_path(path, *, root=None):
    """Return a stable relative location, including for a not-yet-created file.

    Relative input is relative to the workspace, never the process's cwd.
    Resolve symlinks before encoding so links cannot reference outside files.
    The workspace directory itself is encoded as '.'.
    """
    root = _root(root)
    path = Path(path)
    absolute = path if path.is_absolute() else root / path
    return _inside(absolute, root).relative_to(root).as_posix()


def decode_workspace_path(value, *, root=None):
    """Resolve a saved location within the current workspace container.

    Reject absolute paths, parent traversal, and symlinks escaping the root.
    Missing files are valid: callers decide whether to create or omit them.
    This validates a saved location, not concurrent filesystem access; it does
    not replace Files coordination or the operating system's sandbox.
    """
    if not isinstance(value, str):
        raise TypeError('workspace location must be a string')
    relative = PurePosixPath(value)
    if not value or '\0' in value or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('workspace location must be a relative path without parent traversal')
    root = _root(root)
    # A POSIX component can become a drive or separator on Windows. Refuse
    # those encodings there while retaining normal POSIX filename characters.
    native = Path(*relative.parts)
    if native.is_absolute() or native.drive or len(native.parts) != len(relative.parts):
        raise ValueError('workspace location is not a relative path on this platform')
    return _inside(root / native, root)
