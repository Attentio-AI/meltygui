"""Injected state for chat views."""
from meltygui.rendering.decorators.core_decoration import no_save
from meltygui.state.dict_conversion import DictConversion


@no_save("revision", "viewports", "text_layouts", "rename")
class ChatInterfaceState(DictConversion):
    def __init__(self):
        super().__init__()
        self.provider = ""
        self.account = ""
        self.selected = {}
        self.drafts = {}
        self.draft_generations = {}
        self.projects = {}
        self.follow = True
        self.answers = {}
        self.revision = 0
        self.viewports = {}
        self.text_layouts = {}
        self.rename = None
        self.chat_menu = None
        self.message_expanded = {}
        self.concise = False
        self.action_groups = {}
        self.output_expanded = {}
        self.image_sizes = {}
        # The sidebar's age filter: conversations active within this many
        # hours (0 = all). AGE_FILTERS lists the choices.
        self.age_hours = 0
        # The sources (account ids) whose conversations the sidebar shows -
        # the tabs lit in the source bar: empty = just `account`.
        self.sources = []
        self.folder_expanded = {}
        self.folders_default_expanded = True
        self.sections_expanded = {"recent": True, "all": True, "accounts": True}
