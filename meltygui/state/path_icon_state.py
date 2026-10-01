"""Owned runtime resources for a path's compact icon."""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save
from meltygui.model.folder_icon_model import FolderIcons


@no_save('folders', 'watches', 'requested', 'watch_owner')
class PathIconState(DictConversion):
    def __init__(self):
        super().__init__()
        self.folders = FolderIcons()
        self.watches = {}
        self.requested = set()
        self.watch_owner = None

    def ensure_owned_resources(self):
        """Detach the shared template cache left by older session loaders, live."""
        default = type(self).__dict__.get('default_instance')
        if default is not None and self is not default and self.folders is default.folders:
            self.folders = FolderIcons()
            self.requested.clear()
            if self.watch_owner is not None:
                self.watch_owner.folders = self.folders
