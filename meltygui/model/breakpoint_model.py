"""Keyed file breakpoints over an already-built source-site index.

No parsing, file discovery, or cached line numbers: callers supply the file's
metadata and the current scanner index. Source positions belong to that index.
"""
from meltygui.models.file_meta import FileMeta


def file_breakpoints(metadata, path):
    entry = metadata.get(str(path)) if metadata is not None and path is not None else None
    return (entry.get("breakpoints") or {}) if entry is not None else {}


def current_breakpoint_index(tree, source):
    """Return only a prepared whole-file index for this exact source snapshot.

    In particular, never invoke Origin's lazy rebuild from a render function.
    Partial parses lack enclosing file keys and cannot own file breakpoints.
    """
    if not isinstance(tree, dict) or getattr(tree, "line_offset", 0):
        return None
    origin = tree.get("__origin__")
    if origin is None or origin.line_offset:
        return None
    if getattr(origin, "source_input", origin.text) is not source:
        return None
    if getattr(origin, "_site_text", None) is not origin.text:
        return None
    return getattr(origin, "_site_index", None)


def line_has_breakpoint(index, breakpoints, line):
    """O(sites on this line); paint a multiline site's marker only at its start."""
    return any(site.start_line == line and site.path in breakpoints
               and breakpoints[site.path].get("enabled", True)
               for site in index.at_line(line))


def toggle_line_breakpoint(metadata, path, index, line):
    """Resolve the clicked line to keys, then replace the file-meta value.

    A gutter click selects the first source site on a line (no column intent).
    Clicking a marked line removes its existing keys. Continuation lines refer
    to the same statement key. Blanks/comments do not create metadata entries.
    """
    sites = index.at_line(line)
    if not sites or metadata is None or path is None:
        return False
    key = str(path)
    updated = dict(file_breakpoints(metadata, key))
    existing = [site.path for site in sites if site.path in updated]
    if existing:
        for site_key in existing:
            del updated[site_key]
    else:
        updated[sites[0].path] = {"enabled": True}
    entry = metadata.get(key)
    if entry is None:
        entry = metadata[key] = FileMeta()
    if updated:
        entry["breakpoints"] = updated
    else:
        entry.pop("breakpoints", None)
    return True
