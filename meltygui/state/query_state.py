"""Injected state for query views."""
from meltygui.core.conversion.dict_conversion import DictConversion


class MCPQueryState(DictConversion):
    """The window's own state, injected 1:1 with its draw_state.

    Everything without a leading underscore persists between sessions, so the
    window reopens on the tool and the arguments it was left on. The ANSWER
    does not: `_result` / `_json` / `_error` are rebuilt by every run and
    would only bloat the session pickle.
    """

    def __init__(self):
        super().__init__()
        self.tool = "find_views"
        self.args = {}              # tool -> its argument dict (seeded from _TOOL_ARGS)
        self.as_json = False        # False: the answer as a tree. True: the tool's bytes.
        self.follow_pointer = False
        self._result = {}           # the SAME dict every run - see _run
        self._json = ""
        self._stamp = ""
        self._error = None
        self._signature = None
