"""Injected state for chat views."""
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.conversion.dict_conversion import DictConversion


@no_save('scrollbars', 'show_all', 'background', 'accounts_view', 'accounts_rect')
class ChatNavigationState(DictConversion):
    """Prepared overlay geometry belongs to one navigation view, not its chats."""
    def __init__(self):
        super().__init__()
        self.scrollbars = []
        self.show_all = None
        self.background = (0, 0, 0)
        self.accounts_view = None
        self.accounts_rect = None


@no_save("revision", "viewports", "text_layouts", "rename", "hovered_folder_action", "pending_smart_collapse")
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
        # Per-pane fold default (Ctrl+Shift+= / - over a column) and how many
        # conversations a folder or the recent feed lists before "Show more".
        self.folder_column_defaults = {}
        self.sidebar_chat_limits = {}
        # This frame's fold shortcut, applied by the hovered sidebar column;
        # the pane whose small folders stay open while the rest collapse.
        self.hovered_folder_action = None
        self.pending_smart_collapse = None
