"""Injected state for inspection views."""



class _InfoRow:
    """One info-tab row: a param name plus the shared per-render tab context
    (stamped onto `.ctx` by draw_info_tab each render). Exists so the tab's
    rows go through draw_collection — the collection owns iteration, row
    clipping, key search and scroll-to-match; this object type-routes each
    row to draw_info_param."""
    __slots__ = ("param", "group", "ctx")

    def __init__(self, param):
        self.param = param
        self.group = None
        self.ctx = None

    def __repr__(self):
        return f"_InfoRow({self.param!r})"


class ContextMenuState:
    def __init__(self):
        self.decoration_key = None
        self.decoration_str = None
        self.decoration_dict = None
        self.render_func_str = None
        self.render_func_dict = None
        self.class_str = None
        self.class_dict = None
        # One (str_host, dict_host) pair per call site shown - the direct
        # caller, its caller, ... up to Toggles.caller_walk_steps. Innermost-first.
        self.call_site_hosts = []
        self.mode_str = None
        self.mode_dict = None
        # What the cached hosts above were built FOR. This menu's up/down nav
        # retargets the same tab draw_state (and thus this same cm_state) at an
        # ancestor view, so the hosts must rebuild when the target changes.
        self.host_key = None
        # Tuple of (filename, lineno) the caller hosts above were built for.
        self.call_site_keys = None
        self.mode_key = None
        # (file, lineno) of class_to_show's definition - for the class-var /
        # class-default jump buttons. Cached because inspect.getsourcelines()
        # AST-parses the WHOLE module file (see the host_key block below), so it
        # must not run per frame. Invariant for a given host_key.
        self.class_loc = None
