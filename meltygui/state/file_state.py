"""Injected state for file views."""
from meltygui.rendering.decorators.core_decoration import no_save
from meltygui.state.dict_conversion import DictConversion
from pathlib import Path


@no_save("_listing", "_last_dir", "_watched", "_search", "_search_for")
class FileExplorerState(DictConversion):
    """The listing's injected state (`explorer_state: FileExplorerState`).
    Persists the selection, the scroll position per directory visited
    (going back lands where you left) and the hidden-files switch."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.selected = None        # str path of the single-clicked row
        self.scroll_by_dir = {}     # str dir -> scroll y
        self.show_hidden = False    # dotfiles
        self._listing = None        # (dir, mtime_ns, show_hidden, rows) memo
        self._last_dir = None       # the dir of the last run: navigation detection
        self._watched = None        # the dir the view's FileWatch emitter is on
        self._search = ""           # the type-to-search query
        self._search_for = None


class ShortcutState(DictConversion):
    """Persist sidebar order independently of directory listing order."""

    def __init__(self):
        super().__init__()
        self.order = []


class FileSelectorState(DictConversion):
    def __init__(self):
        super().__init__()
        self.directory = None


class FileTreeState(DictConversion):
    """Injected per-draw_state state (`file_tree_state: FileTreeState = None`).
    A DictConversion so it PERSISTS: every non-underscore attribute is saved
    between sessions (style guide rules 6–7) — which folders were open and
    what was selected come back on restart. Paths are stored as STRINGS
    (a Path inside a set is not on the legacy to_dict primitive list)."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.expanded = set()   # str paths of open folders
        self.selected = None    # str path of the last single-clicked file
        self._graph = None      # file_graph.ImportGraph once built (not saved)
        self._build = None      # file_graph.ImportBuildBuilder while building

    def is_expanded(self, path):
        return str(path) in self.expanded

    def toggle(self, path):
        self.expanded.symmetric_difference_update({str(path)})

    def select(self, path):
        self.selected = str(path) if path is not None else None

    @property
    def selected_path(self):
        return Path(self.selected) if self.selected else None

    def open_file(self, path):
        from meltygui.core.file_tree_core import open_file

        open_file(path)


from meltygui.paths import application_root
ROOT = application_root()
