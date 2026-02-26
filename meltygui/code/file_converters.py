"""
pathlib.Path ↔ dict/bytes/str converters for the Melty registry.

Reads and writes files through the same converter graph as CST nodes,
so you can chain:

    Path → bytes → str → cst.Module → dict     (disk to editable dict)
    dict → cst.Module → str → bytes → Path      (editable dict back to disk)

Or use convert() with an explicit path for full control:

    convert(Path("app.py"), dict,
            registry=Melty,
            path=[Path, bytes, str, cst.Module, dict])

File dicts carry __path__ (like __cst__) for lossless round-trip:

    {
        "name":     "app.py",
        "stem":     "app",
        "suffix":   ".py",
        "size":     1234,
        "modified": 1709123456.0,
        "data":     b"import sys\\n...",
        "__path__": Path("/absolute/path/app.py"),
    }
"""

import os
from pathlib import Path

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.converter_register import converter


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path → dict (read file into metadata)                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def path_to_dict(value: Path) -> dict:
    """Read a file from disk into a metadata dict.

    Returns name, stem, suffix, size, modified timestamp, raw bytes,
    and the original Path under __path__ for round-trip writes.

    Raises FileNotFoundError if the path doesn't exist.
    Raises IsADirectoryError if the path is a directory.
    """
    value = Path(value)

    if not value.exists():
        raise FileNotFoundError(f"No such file: {value}")
    if value.is_dir():
        raise IsADirectoryError(f"Is a directory, not a file: {value}")

    stat = value.stat()

    return {
        "name": value.name,
        "stem": value.stem,
        "suffix": value.suffix,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "data": value.read_bytes(),
        "__path__": value.resolve(),
    }


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  dict → Path (write file back to disk)                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def dict_to_path(value: dict) -> Path:
    """Write file data back to disk from a metadata dict.

    Requires __path__ (target location) and "data" (bytes or str).
    Creates parent directories if they don't exist.
    Returns the Path written to.
    """
    path = value.get("__path__")
    if path is None:
        raise TypeError("Dict has no __path__ — can't determine write location")
    path = Path(path)

    data = value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' key — nothing to write")

    # Ensure parents exist
    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(data, bytes):
        path.write_bytes(data)
    elif isinstance(data, str):
        path.write_text(data)
    else:
        raise TypeError(f"'data' must be bytes or str, got {type(data).__name__}")

    return path


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path → bytes (raw read)                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def path_to_bytes(value: Path) -> bytes:
    """Read raw bytes from a file path."""
    value = Path(value)
    if not value.exists():
        raise FileNotFoundError(f"No such file: {value}")
    return value.read_bytes()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  bytes ↔ str (encoding bridge)                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def bytes_to_str(value: bytes) -> str:
    """Decode bytes to string. Tries UTF-8, falls back to latin-1."""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1")


@converter(registry=Melty)
def str_to_bytes(value: str) -> bytes:
    """Encode string to UTF-8 bytes."""
    return value.encode("utf-8")