"""Injected terminal integration, owned by the screen's render lifecycle."""
import weakref
from meltygui.core.melty import Melty
from meltygui.core.windowing.glfw_utils import request_render


class _TerminalOutputBridge:
    """Let an already-running pre-migration reader notify current subscribers.

    Hotswap replaces function definitions, not executing Python frames. Those
    older frames still call ``self._ds.invalidate()`` until their PTY exits.
    This transition object contains no draw state and is never used by fresh
    terminals or by the current reader implementation.
    """

    def __init__(self, terminal):
        self._terminal = weakref.ref(terminal)

    def invalidate(self):
        terminal = self._terminal()
        if terminal is not None:
            terminal._notify_changed()


class TerminalRuntime:
    def __init__(self):
        self._owner_ds = None
        self._terminal = None
        self._appeared = False

    def prepare(self, terminal, cols, rows):
        if terminal is not self._terminal:
            if self._terminal is not None:
                self._terminal.unsubscribe(self.output_changed)
            self._terminal = terminal
            terminal.subscribe(self.output_changed)
            if '_ds' in vars(terminal):
                terminal._ds = _TerminalOutputBridge(terminal)
            self._appeared = False
        if not self._appeared:
            self._appeared = True
            if self._owner_ds.closable:
                self.focus()
        terminal.start(cols, rows)
        if terminal.screen is not None:
            terminal.resize(cols, rows)

    def output_changed(self):
        # Invalidating the screen also dirties cached ancestors. Weak model
        # subscriptions allow several views of one PTY without retaining either.
        self._owner_ds.invalidate_up(force=True)
        try:
            request_render()
        except Exception:
            # A reader can publish while the window backend is starting/stopping.
            pass

    @property
    def focused(self):
        return Melty.text_focused_ds is self._owner_ds

    def focus(self):
        previous = Melty.text_focused_ds
        if previous is not None and previous is not self._owner_ds:
            previous.invalidate()
        Melty.text_focused_ds = self._owner_ds

    def forward_keys(self, terminal, view_state):
        if self.focused:
            from meltygui.core.services.terminal_core import _forward_keys
            _forward_keys(terminal, view_state)

    def open_link(self, path, line):
        import threading
        from meltygui.core.services.terminal_core import _resolve_path
        from meltygui.utils.jump_to_code import open_in_intellij
        threading.Thread(target=open_in_intellij, args=(_resolve_path(path),),
                         kwargs={"line_number": line}, daemon=True).start()
